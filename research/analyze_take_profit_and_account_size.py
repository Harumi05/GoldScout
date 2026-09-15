"""Offline TP-efficiency and minimum-account analysis for GoldScout.

This module is research-only.  It replays executable BID/ASK ticks after each
reproducible historical setup, reproduces the live CURRENT stop and target
classification, and never imports or changes the live EA.
"""

from __future__ import annotations

import argparse
from bisect import bisect_right
from collections import defaultdict
import csv
from dataclasses import dataclass, field
import heapq
import io
import itertools
import math
from pathlib import Path
import time
from typing import Callable, Iterable, Sequence

from research.analyze_adaptive_stop_v2 import adaptive_setup
from research.analyze_historical_dataset import (
    _atomic_write,
    _directed_values,
    _load_observations,
    _load_outcomes,
    _merge_decision_enrichment,
)
from research.analyze_stop_loss_quality import (
    HORIZON_MS,
    M1RangeIndex,
    SignalState,
    StopState,
    _is_number,
    _mean,
    _median,
    _percentile,
    _round_price,
    build_stop_candidates,
    prepare_signals,
)
from research.enrich_historical_decisions import _load_bars, build_state_points
from research.tick_historical_replay import IngestionStats, Tick, discover_inputs, iter_ticks


SOURCE = "HISTORICAL_MT5_TICKS"
RISK_PERCENT = 5.0
DAILY_LOSS_LIMIT_PERCENT = 5.0
CHILL_TARGET_R = 1.25
GOD_TARGET_R = 2.0
MIN_REWARD_RISK = 1.25
TP_R_BY_CLASS = {
    "CHILL": (0.75, 1.0, 1.25),
    "GOD": (1.25, 1.5, 2.0),
}
ACCOUNT_EQUITIES = (200, 300, 500, 750, 1000)
PERCENTILES = (0.10, 0.25, 0.50, 0.75, 0.90, 0.95)

# Capital.com XAUUSD contract observed and already covered by broker tests.
DEFAULT_CONTRACT_SIZE = 100.0
DEFAULT_VOLUME_MIN = 0.01
DEFAULT_VOLUME_STEP = 0.01
DEFAULT_TICK_SIZE = 0.01
DEFAULT_POINT_SIZE = 0.01
DEFAULT_MAX_DEVIATION_POINTS = 30
DEFAULT_COMMISSION_PER_LOT = 0.0

# Explicit diagnostic definitions requested by the analysis.
SIGNIFICANT_FAVORABLE_R = 0.75
POST_TP_CONTINUATION_R = 0.50


@dataclass
class TargetState:
    name: str
    r_multiple: float
    price: float
    hit_ms: int | None = None
    hit_mid: float | None = None
    post_hit_same_minute_high: float | None = None
    post_hit_same_minute_low: float | None = None


@dataclass
class TPSignal:
    base: SignalState
    trade_class: str
    current_target_r: float
    entry_price: float | None = None
    worst_fill_price: float | None = None
    planned_risk_distance: float | None = None
    actual_stop_distance: float | None = None
    stop: StopState | None = None
    targets: dict[str, TargetState] = field(default_factory=dict)


def _align_price(price: float, tick_size: float, *, up: bool) -> float:
    if price <= 0.0 or tick_size <= 0.0:
        raise ValueError("price and tick_size must be positive")
    units = price / tick_size
    value = math.ceil(units - 1e-10) if up else math.floor(units + 1e-10)
    return round(value * tick_size, 10)


def classify_trade_class(
    *,
    direction: str,
    h4_trend: str,
    h1_ema_trend: str,
    adx: float,
    rsi: float,
    hh: bool,
    hl: bool,
    lh: bool,
    ll: bool,
    breakout: bool,
    pullback: bool,
    htf_pullback: bool,
) -> tuple[str, float]:
    """Reproduce BuildSignal's clear/GOD versus CHILL decision."""
    if direction == "LONG":
        clear = (
            h4_trend == "BULLISH"
            and (h1_ema_trend == "BULLISH" or htf_pullback)
            and adx >= 25.0
            and (50.0 <= rsi <= 68.0 or htf_pullback)
            and (hh or hl or breakout or pullback)
        )
    elif direction == "SHORT":
        clear = (
            h4_trend == "BEARISH"
            and (h1_ema_trend == "BEARISH" or htf_pullback)
            and adx >= 25.0
            and (32.0 <= rsi <= 50.0 or htf_pullback)
            and (ll or lh or breakout or pullback)
        )
    else:
        raise ValueError("direction must be LONG or SHORT")
    return ("GOD", max(GOD_TARGET_R, MIN_REWARD_RISK)) if clear else (
        "CHILL",
        max(CHILL_TARGET_R, MIN_REWARD_RISK),
    )


def _annotate_trade_classes(signals: Sequence[SignalState], input_dir: Path) -> list[TPSignal]:
    observations, _ = _load_observations(Path(input_dir) / "historical_observations.jsonl")
    _merge_decision_enrichment(observations, Path(input_dir) / "historical_decisions.jsonl")
    records = {item["event_id"]: item for item in observations}
    points = build_state_points(_load_bars(input_dir, "XAUUSD", "H1"), DEFAULT_POINT_SIZE)
    close_times = [int(point.bar["close_timestamp"]) for point in points]
    output: list[TPSignal] = []
    for signal in signals:
        record = records.get(signal.event_id)
        index = bisect_right(close_times, signal.anchor_ms) - 1
        if record is None or index < 1:
            continue
        point, previous = points[index], points[index - 1]
        rsi, adx, ema20 = (
            point.indicators.get("rsi"),
            point.indicators.get("adx"),
            point.indicators.get("ema20"),
        )
        if not all(_is_number(value) for value in (rsi, adx, ema20)):
            continue
        h4 = record.get("h4_context") if isinstance(record.get("h4_context"), dict) else {}
        h1 = record.get("h1_structure") if isinstance(record.get("h1_structure"), dict) else {}
        h4_trend = str(h4.get("trend", "NEUTRAL"))
        h1_trend = str(h1.get("ema_trend", "NEUTRAL"))
        close = float(point.bar["close"])
        near_fast = abs(close - float(ema20)) <= signal.atr * 0.75
        if signal.direction == "LONG":
            htf_pull = (
                h4_trend == "BULLISH"
                and h1_trend != "BULLISH"
                and near_fast
                and float(rsi) <= 42.0
                and bool(h1.get("hl"))
                and close >= float(previous.bar["low"])
            )
            breakout = bool(h1.get("breakout_long"))
            pullback = bool(h1.get("pullback_long"))
        else:
            htf_pull = (
                h4_trend == "BEARISH"
                and h1_trend != "BEARISH"
                and near_fast
                and float(rsi) >= 58.0
                and bool(h1.get("lh"))
                and close <= float(previous.bar["high"])
            )
            breakout = bool(h1.get("breakout_short"))
            pullback = bool(h1.get("pullback_short"))
        trade_class, current_r = classify_trade_class(
            direction=signal.direction,
            h4_trend=h4_trend,
            h1_ema_trend=h1_trend,
            adx=float(adx),
            rsi=float(rsi),
            hh=bool(h1.get("hh")),
            hl=bool(h1.get("hl")),
            lh=bool(h1.get("lh")),
            ll=bool(h1.get("ll")),
            breakout=breakout,
            pullback=pullback,
            htf_pullback=htf_pull,
        )
        output.append(TPSignal(signal, trade_class, current_r))
    return output


def build_target_candidates(
    *,
    trade_class: str,
    current_target_r: float,
    direction: str,
    entry_price: float,
    stop_price: float,
    tick_size: float = DEFAULT_TICK_SIZE,
    max_deviation_points: int = DEFAULT_MAX_DEVIATION_POINTS,
    point_size: float = DEFAULT_POINT_SIZE,
) -> tuple[float, float, dict[str, TargetState]]:
    """Build targets from live planned monetary risk, including fill deviation."""
    if trade_class not in TP_R_BY_CLASS or direction not in {"LONG", "SHORT"}:
        raise ValueError("unsupported trade class or direction")
    deviation = max_deviation_points * point_size
    worst_fill = _align_price(
        entry_price + deviation if direction == "LONG" else entry_price - deviation,
        tick_size,
        up=direction == "LONG",
    )
    risk_distance = abs(worst_fill - stop_price)
    if risk_distance <= 0.0:
        raise ValueError("planned risk distance must be positive")
    definitions = [("CURRENT", current_target_r)] + [
        (f"R_{str(value).replace('.', '_')}", value) for value in TP_R_BY_CLASS[trade_class]
    ]
    targets: dict[str, TargetState] = {}
    for name, multiple in definitions:
        raw = entry_price + multiple * risk_distance if direction == "LONG" else entry_price - multiple * risk_distance
        target = _align_price(raw, tick_size, up=direction == "LONG")
        targets[name] = TargetState(name, multiple, target)
    return worst_fill, risk_distance, targets


def risk_at_minimum_lot(
    *,
    direction: str,
    entry_price: float,
    stop_price: float,
    contract_size: float = DEFAULT_CONTRACT_SIZE,
    volume_min: float = DEFAULT_VOLUME_MIN,
    tick_size: float = DEFAULT_TICK_SIZE,
    point_size: float = DEFAULT_POINT_SIZE,
    max_deviation_points: int = DEFAULT_MAX_DEVIATION_POINTS,
    commission_per_lot: float = DEFAULT_COMMISSION_PER_LOT,
) -> float:
    """Linear XAUUSD equivalent of OrderCalcProfit at the broker minimum lot."""
    if contract_size <= 0.0 or volume_min <= 0.0:
        raise ValueError("contract_size and volume_min must be positive")
    deviation = max_deviation_points * point_size
    worst_fill = _align_price(
        entry_price + deviation if direction == "LONG" else entry_price - deviation,
        tick_size,
        up=direction == "LONG",
    )
    return abs(worst_fill - stop_price) * contract_size * volume_min + commission_per_lot * volume_min


def minimum_equity_for_risk(risk_at_min_lot: float, risk_percent: float = RISK_PERCENT) -> float:
    if risk_at_min_lot < 0.0 or risk_percent <= 0.0:
        raise ValueError("risk and risk_percent must be valid")
    return risk_at_min_lot / (risk_percent / 100.0)


def account_can_execute(equity: float, risk_at_min_lot: float) -> bool:
    """First-trade-of-day viability: per-trade and daily budgets are both 5%."""
    if equity <= 0.0:
        return False
    target = equity * RISK_PERCENT / 100.0
    daily = equity * DAILY_LOSS_LIMIT_PERCENT / 100.0
    return risk_at_min_lot <= min(target, daily) + 1e-9


def simulate_tp_tick_order(
    signals: Sequence[TPSignal],
    ticks: Iterable[Tick],
    *,
    progress_every: int = 5_000_000,
    target_builder: Callable[[TPSignal, float, StopState], tuple[float, float, dict[str, TargetState]]] | None = None,
) -> dict:
    """Resolve entries, CURRENT SL and every class-specific TP in tick order."""
    pending = sorted(signals, key=lambda item: (item.base.anchor_ms, item.base.event_id))
    signal_index = 0
    counter = itertools.count()
    long_stops: list[tuple[float, int, TPSignal]] = []
    short_stops: list[tuple[float, int, TPSignal]] = []
    long_targets: list[tuple[float, int, TPSignal, tuple[TargetState, ...]]] = []
    short_targets: list[tuple[float, int, TPSignal, tuple[TargetState, ...]]] = []
    minute_target_hits: list[TargetState] = []
    current_minute: int | None = None
    minute_high = minute_low = None
    ticks_seen = 0
    started = time.perf_counter()

    def expired(item: TPSignal, stamp: int) -> bool:
        return stamp > item.base.anchor_ms + HORIZON_MS["4h"]

    for tick in ticks:
        ticks_seen += 1
        minute = tick.timestamp_ms // 60_000 * 60_000
        if minute != current_minute:
            current_minute = minute
            minute_target_hits = []
            minute_high = minute_low = tick.mid
        else:
            minute_high = max(float(minute_high), tick.mid)
            minute_low = min(float(minute_low), tick.mid)

        while signal_index < len(pending) and pending[signal_index].base.anchor_ms <= tick.timestamp_ms:
            item = pending[signal_index]
            signal_index += 1
            if expired(item, tick.timestamp_ms):
                continue
            signal = item.base
            signal.entry_mid, signal.entry_bid, signal.entry_ask = tick.mid, tick.bid, tick.ask
            signal.entry_spread, signal.entry_tick_ms = tick.spread, tick.timestamp_ms
            entry = tick.ask if signal.direction == "LONG" else tick.bid
            current = build_stop_candidates(
                signal.direction,
                entry,
                signal.atr,
                signal.recent_high,
                signal.recent_low,
                signal.swing_high,
                signal.swing_low,
            ).get("CURRENT")
            if current is None:
                continue
            item.entry_price, item.stop = entry, current
            item.actual_stop_distance = abs(entry - current.price)
            if target_builder is None:
                item.worst_fill_price, item.planned_risk_distance, item.targets = build_target_candidates(
                    trade_class=item.trade_class,
                    current_target_r=item.current_target_r,
                    direction=signal.direction,
                    entry_price=entry,
                    stop_price=current.price,
                )
            else:
                item.worst_fill_price, item.planned_risk_distance, item.targets = target_builder(item, entry, current)
            if signal.direction == "LONG":
                heapq.heappush(long_stops, (-current.price, next(counter), item))
            else:
                heapq.heappush(short_stops, (current.price, next(counter), item))
            targets_by_price: dict[float, list[TargetState]] = defaultdict(list)
            for target in item.targets.values():
                targets_by_price[target.price].append(target)
            for target_price, target_group in targets_by_price.items():
                heap = long_targets if signal.direction == "LONG" else short_targets
                heapq.heappush(
                    heap,
                    (
                        target_price if signal.direction == "LONG" else -target_price,
                        next(counter),
                        item,
                        tuple(target_group),
                    ),
                )

        while long_stops:
            stop_price = -long_stops[0][0]
            _, _, item = long_stops[0]
            if item.stop is None or item.stop.hit_ms is not None or expired(item, tick.timestamp_ms):
                heapq.heappop(long_stops)
            elif stop_price >= tick.bid:
                heapq.heappop(long_stops)
                item.stop.hit_ms, item.stop.hit_mid = tick.timestamp_ms, tick.mid
            else:
                break
        while short_stops:
            stop_price, _, item = short_stops[0]
            if item.stop is None or item.stop.hit_ms is not None or expired(item, tick.timestamp_ms):
                heapq.heappop(short_stops)
            elif stop_price <= tick.ask:
                heapq.heappop(short_stops)
                item.stop.hit_ms, item.stop.hit_mid = tick.timestamp_ms, tick.mid
            else:
                break

        while long_targets:
            target_price, _, item, target_group = long_targets[0]
            if target_group[0].hit_ms is not None or expired(item, tick.timestamp_ms):
                heapq.heappop(long_targets)
            elif target_price <= tick.bid:
                heapq.heappop(long_targets)
                for target in target_group:
                    target.hit_ms, target.hit_mid = tick.timestamp_ms, tick.mid
                    target.post_hit_same_minute_high = target.post_hit_same_minute_low = tick.mid
                    minute_target_hits.append(target)
            else:
                break
        while short_targets:
            target_price = -short_targets[0][0]
            _, _, item, target_group = short_targets[0]
            if target_group[0].hit_ms is not None or expired(item, tick.timestamp_ms):
                heapq.heappop(short_targets)
            elif target_price >= tick.ask:
                heapq.heappop(short_targets)
                for target in target_group:
                    target.hit_ms, target.hit_mid = tick.timestamp_ms, tick.mid
                    target.post_hit_same_minute_high = target.post_hit_same_minute_low = tick.mid
                    minute_target_hits.append(target)
            else:
                break
        for target in minute_target_hits:
            target.post_hit_same_minute_high = max(float(target.post_hit_same_minute_high), tick.mid)
            target.post_hit_same_minute_low = min(float(target.post_hit_same_minute_low), tick.mid)

        if progress_every and ticks_seen % progress_every == 0:
            elapsed = time.perf_counter() - started
            print(f"[TP_ANALYSIS] ticks={ticks_seen:,} | ticks/s={ticks_seen / elapsed:,.0f} | elapsed={elapsed:.1f}s")
    return {
        "ticks_seen": ticks_seen,
        "signals_requested": len(signals),
        "signals_with_entry_tick": sum(item.base.entry_tick_ms is not None for item in signals),
        "elapsed_seconds": time.perf_counter() - started,
    }


def _post_tp_favorable(item: TPSignal, target: TargetState, horizon_end: int, ranges: M1RangeIndex) -> float | None:
    signal = item.base
    if signal.entry_mid is None or target.hit_ms is None or target.hit_ms > horizon_end:
        return None
    hit_minute = target.hit_ms // 60_000 * 60_000
    maximum, minimum = ranges.extrema(hit_minute + 60_000, horizon_end)
    highs = [value for value in (maximum, target.post_hit_same_minute_high) if _is_number(value)]
    lows = [value for value in (minimum, target.post_hit_same_minute_low) if _is_number(value)]
    if signal.direction == "LONG" and highs:
        return max(0.0, max(highs) - signal.entry_mid)
    if signal.direction == "SHORT" and lows:
        return max(0.0, signal.entry_mid - min(lows))
    return None


def tp_detail_rows(
    signals: Sequence[TPSignal], outcomes: dict, ranges: M1RangeIndex
) -> list[dict]:
    rows: list[dict] = []
    for item in signals:
        signal, stop = item.base, item.stop
        if signal.entry_mid is None or item.entry_price is None or stop is None or item.planned_risk_distance is None:
            continue
        planned = item.planned_risk_distance
        stop_loss_r = float(item.actual_stop_distance) / planned
        for target in item.targets.values():
            for horizon, duration in HORIZON_MS.items():
                end = signal.anchor_ms + duration
                tp_in = target.hit_ms is not None and target.hit_ms <= end
                sl_in = stop.hit_ms is not None and stop.hit_ms <= end
                tp_first = tp_in and (not sl_in or int(target.hit_ms) < int(stop.hit_ms))
                sl_first = sl_in and (not tp_in or int(stop.hit_ms) <= int(target.hit_ms))
                outcome = outcomes.get(signal.event_id, {}).get(horizon)
                directed = _directed_values(outcome, signal.direction) if outcome else (None, None, None)
                future_return, mfe_return, mae_return = directed
                mfe_price = float(mfe_return) * signal.reference_close if _is_number(mfe_return) else None
                mae_price = abs(float(mae_return)) * signal.reference_close if _is_number(mae_return) else None
                post_tp = _post_tp_favorable(item, target, end, ranges) if tp_first else None
                target_distance = abs(target.price - item.entry_price)
                post_tp_extra_r = (
                    max(0.0, float(post_tp) - target_distance) / planned if _is_number(post_tp) else None
                )
                realized_r = target.r_multiple if tp_first else -stop_loss_r if sl_first else None
                adverse_finish = _is_number(future_return) and float(future_return) <= 0.0
                too_far = (
                    not tp_first
                    and _is_number(mfe_price)
                    and float(mfe_price) / planned >= SIGNIFICANT_FAVORABLE_R
                    and (sl_first or adverse_finish)
                )
                too_short = tp_first and _is_number(post_tp_extra_r) and float(post_tp_extra_r) >= POST_TP_CONTINUATION_R
                rows.append(
                    {
                        "event_id": signal.event_id,
                        "period": signal.period,
                        "trade_class": item.trade_class,
                        "direction": signal.direction,
                        "setup": adaptive_setup(signal),
                        "candidate": target.name,
                        "target_r": target.r_multiple,
                        "horizon": horizon,
                        "tp_hit": tp_in,
                        "sl_hit": sl_in,
                        "tp_first": tp_first,
                        "sl_first": sl_first,
                        "unresolved": not tp_first and not sl_first,
                        "time_to_tp_minutes": (int(target.hit_ms) - signal.anchor_ms) / 60_000.0 if tp_first else None,
                        "time_to_sl_minutes": (int(stop.hit_ms) - signal.anchor_ms) / 60_000.0 if sl_first else None,
                        "hypothetical_realized_r": realized_r,
                        "mfe_r": float(mfe_price) / planned if _is_number(mfe_price) else None,
                        "mae_r": float(mae_price) / planned if _is_number(mae_price) else None,
                        "mfe_lost_after_tp_r": post_tp_extra_r,
                        "tp_too_far": too_far,
                        "tp_too_short": too_short,
                        "observer_only": True,
                        "diagnostic_only": True,
                        "score_effect": 0,
                    }
                )
    return rows


def summarize_tp(rows: Sequence[dict]) -> dict:
    count = len(rows)
    resolved = [row for row in rows if _is_number(row.get("hypothetical_realized_r"))]
    wins = [float(row["hypothetical_realized_r"]) for row in resolved if float(row["hypothetical_realized_r"]) > 0.0]
    losses = [float(row["hypothetical_realized_r"]) for row in resolved if float(row["hypothetical_realized_r"]) < 0.0]
    gross_profit, gross_loss = sum(wins), abs(sum(losses))
    return {
        "cases": count,
        "resolved_cases": len(resolved),
        "censored_cases": count - len(resolved),
        "tp_hit_rate": sum(bool(row["tp_hit"]) for row in rows) / count if count else None,
        "sl_hit_rate": sum(bool(row["sl_hit"]) for row in rows) / count if count else None,
        "tp_first_rate": sum(bool(row["tp_first"]) for row in rows) / count if count else None,
        "sl_first_rate": sum(bool(row["sl_first"]) for row in rows) / count if count else None,
        "unresolved_rate": sum(bool(row["unresolved"]) for row in rows) / count if count else None,
        "win_rate_resolved": len(wins) / len(resolved) if resolved else None,
        "expectancy_r_resolved": _mean(row["hypothetical_realized_r"] for row in resolved),
        "profit_factor_resolved": gross_profit / gross_loss if gross_loss > 0.0 else None,
        "median_time_to_tp_minutes": _median(row["time_to_tp_minutes"] for row in rows),
        "median_time_to_sl_minutes": _median(row["time_to_sl_minutes"] for row in rows),
        "median_mfe_r": _median(row["mfe_r"] for row in rows),
        "median_mae_r": _median(row["mae_r"] for row in rows),
        "median_mfe_lost_after_tp_r": _median(row["mfe_lost_after_tp_r"] for row in rows),
        "too_far_rate": sum(bool(row["tp_too_far"]) for row in rows) / count if count else None,
        "too_short_rate": sum(bool(row["tp_too_short"]) for row in rows) / count if count else None,
    }


def aggregate_tp(rows: Sequence[dict]) -> list[dict]:
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for row in rows:
        common = (row["trade_class"], row["candidate"], row["horizon"])
        for period in ("ALL", row["period"]):
            groups[("GLOBAL", period, row["trade_class"], "ALL", "ALL", row["candidate"], row["horizon"])].append(row)
            groups[("DIRECTION", period, row["trade_class"], row["direction"], "ALL", row["candidate"], row["horizon"])].append(row)
            groups[("SETUP", period, row["trade_class"], row["direction"], row["setup"], row["candidate"], row["horizon"])].append(row)
    output = []
    for key, members in sorted(groups.items()):
        output.append(
            {
                "scope": key[0],
                "period": key[1],
                "trade_class": key[2],
                "direction": key[3],
                "setup": key[4],
                "candidate": key[5],
                "horizon": key[6],
                **summarize_tp(members),
                "observer_only": True,
                "diagnostic_only": True,
                "score_effect": 0,
            }
        )
    baseline = {
        (row["scope"], row["period"], row["trade_class"], row["direction"], row["setup"], row["horizon"]): row
        for row in output
        if row["candidate"] == "CURRENT"
    }
    for row in output:
        base = baseline.get((row["scope"], row["period"], row["trade_class"], row["direction"], row["setup"], row["horizon"]))
        for metric in ("tp_first_rate", "expectancy_r_resolved", "too_far_rate", "too_short_rate"):
            left, right = row.get(metric), base.get(metric) if base else None
            row[f"delta_vs_current_{metric}"] = float(left) - float(right) if _is_number(left) and _is_number(right) else None
    return output


def capital_detail_rows(
    signals: Sequence[TPSignal],
    *,
    contract_size: float = DEFAULT_CONTRACT_SIZE,
    volume_min: float = DEFAULT_VOLUME_MIN,
    volume_step: float = DEFAULT_VOLUME_STEP,
    commission_per_lot: float = DEFAULT_COMMISSION_PER_LOT,
) -> list[dict]:
    rows: list[dict] = []
    for item in signals:
        signal = item.base
        if item.entry_price is None or item.stop is None:
            continue
        risk_min = risk_at_minimum_lot(
            direction=signal.direction,
            entry_price=item.entry_price,
            stop_price=item.stop.price,
            contract_size=contract_size,
            volume_min=volume_min,
            commission_per_lot=commission_per_lot,
        )
        row = {
            "event_id": signal.event_id,
            "period": signal.period,
            "trade_class": item.trade_class,
            "direction": signal.direction,
            "setup": adaptive_setup(signal),
            "stop_distance": item.actual_stop_distance,
            "planned_risk_distance": item.planned_risk_distance,
            "risk_at_min_lot": risk_min,
            "minimum_equity": minimum_equity_for_risk(risk_min),
            "volume_min": volume_min,
            "volume_step": volume_step,
            "contract_size": contract_size,
            "risk_percent": RISK_PERCENT,
            "daily_loss_limit_percent": DAILY_LOSS_LIMIT_PERCENT,
            "observer_only": True,
            "diagnostic_only": True,
            "score_effect": 0,
        }
        for equity in ACCOUNT_EQUITIES:
            row[f"executable_{equity}"] = account_can_execute(equity, risk_min)
            row[f"status_{equity}"] = "EXECUTABLE" if row[f"executable_{equity}"] else "MIN_LOT_RISK_TOO_HIGH"
        rows.append(row)
    return rows


def summarize_capital(rows: Sequence[dict]) -> dict:
    count = len(rows)
    values = [float(row["minimum_equity"]) for row in rows if _is_number(row.get("minimum_equity"))]
    summary = {"cases": count, "median_risk_at_min_lot": _median(row["risk_at_min_lot"] for row in rows)}
    for percentile in PERCENTILES:
        summary[f"minimum_equity_p{int(percentile * 100)}"] = _percentile(values, percentile)
    for equity in ACCOUNT_EQUITIES:
        executable = sum(bool(row[f"executable_{equity}"]) for row in rows)
        summary[f"executable_count_{equity}"] = executable
        summary[f"executable_rate_{equity}"] = executable / count if count else None
        summary[f"min_lot_risk_too_high_{equity}"] = count - executable
    return summary


def aggregate_capital(rows: Sequence[dict]) -> list[dict]:
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for row in rows:
        for period in ("ALL", row["period"]):
            groups[("GLOBAL", period, "ALL", "ALL", "ALL")].append(row)
            groups[("CLASS", period, row["trade_class"], "ALL", "ALL")].append(row)
            groups[("DIRECTION", period, row["trade_class"], row["direction"], "ALL")].append(row)
            groups[("SETUP", period, row["trade_class"], row["direction"], row["setup"])].append(row)
    return [
        {
            "scope": key[0],
            "period": key[1],
            "trade_class": key[2],
            "direction": key[3],
            "setup": key[4],
            **summarize_capital(members),
            "observer_only": True,
            "diagnostic_only": True,
            "score_effect": 0,
        }
        for key, members in sorted(groups.items())
    ]


def _csv_text(rows: Sequence[dict]) -> str:
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def _pct(value: object) -> str:
    return f"{float(value) * 100.0:.2f}%" if _is_number(value) else "N/A"


def _number(value: object, digits: int = 3) -> str:
    return f"{float(value):.{digits}f}" if _is_number(value) else "N/A"


def select_best_candidate(rows: Sequence[dict], trade_class: str) -> tuple[str, str]:
    """Select only fixed candidates supported outside TRAIN; otherwise CURRENT."""
    indexed = {
        (row["period"], row["candidate"]): row
        for row in rows
        if row["scope"] == "GLOBAL"
        and row["trade_class"] == trade_class
        and row["direction"] == "ALL"
        and row["horizon"] == "4h"
    }
    candidates = [name for name in {key[1] for key in indexed} if name != "CURRENT"]
    current_val, current_oos = indexed.get(("VALIDATION", "CURRENT")), indexed.get(("OOS", "CURRENT"))
    if not current_val or not current_oos:
        return "CURRENT", "insufficient validation/OOS baseline"
    eligible: list[tuple[float, str]] = []
    for name in candidates:
        val, oos = indexed.get(("VALIDATION", name)), indexed.get(("OOS", name))
        if not val or not oos:
            continue
        values = (val.get("expectancy_r_resolved"), oos.get("expectancy_r_resolved"))
        bases = (current_val.get("expectancy_r_resolved"), current_oos.get("expectancy_r_resolved"))
        if not all(_is_number(value) for value in values + bases):
            continue
        if float(values[0]) + 0.02 < float(bases[0]) or float(values[1]) + 0.02 < float(bases[1]):
            continue
        if max(float(values[0]) - float(bases[0]), float(values[1]) - float(bases[1])) <= 0.005:
            continue
        eligible.append(((float(values[0]) + float(values[1])) / 2.0, name))
    if not eligible:
        return "CURRENT", "no fixed candidate improves validation/OOS without material degradation"
    _, name = max(eligible)
    return name, "improves validation/OOS resolved expectancy under the fixed diagnostic rule"


def _tp_markdown(rows: Sequence[dict], best: dict[str, tuple[str, str]], summary: dict) -> str:
    lines = [
        "# Take Profit efficiency",
        "",
        "> Investigación histórica · observer_only=true · score_effect=0.",
        "",
        "CURRENT reproduce BuildSignal/TPPriceForMoney: CHILL=1.25R y GOD=2.0R. El R monetario usa el peor fill permitido de 30 puntos; BID/ASK decide en ticks cuál nivel se toca primero.",
        "",
        f"Universo: {summary['signals_reproducible']} setups ARM reproducibles; {summary['signals_with_entry_tick']} tienen tick de entrada. El orden se resolvió sobre {summary['tick_scan']['ticks_seen']:,} ticks.",
        "",
        "`realized R`, expectancy, win rate y profit factor son hipotéticos y se calculan solo sobre casos resueltos por TP/SL dentro del horizonte. Los demás quedan censurados; no se inventa una salida temporal.",
        "",
        f"TP demasiado lejano: no llega primero al TP, alcanza al menos {SIGNIFICANT_FAVORABLE_R:.2f}R de MFE y después toca SL o termina adverso. TP demasiado corto: llega primero al TP y continúa al menos {POST_TP_CONTINUATION_R:.2f}R adicional.",
        "",
        "No existe un TP estructural separado reproducible en el EA actual; por ello no se fabricó esa variante.",
        "",
        "## Comparación global a 4h",
        "",
        "| Clase | Periodo | Candidato | Casos | TP toca | SL toca | TP primero | SL primero | Censurado | Mediana TP | Mediana SL | Expectancy R resuelta | Win rate resuelta | PF resuelto | Muy lejos | Muy corto |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        if row["scope"] == "GLOBAL" and row["direction"] == "ALL" and row["horizon"] == "4h":
            lines.append(
                f"| {row['trade_class']} | {row['period']} | {row['candidate']} | {row['cases']} | "
                f"{_pct(row['tp_hit_rate'])} | {_pct(row['sl_hit_rate'])} | "
                f"{_pct(row['tp_first_rate'])} | {_pct(row['sl_first_rate'])} | {_pct(row['unresolved_rate'])} | "
                f"{_number(row['median_time_to_tp_minutes'], 1)}m | {_number(row['median_time_to_sl_minutes'], 1)}m | "
                f"{_number(row['expectancy_r_resolved'])} | "
                f"{_pct(row['win_rate_resolved'])} | {_number(row['profit_factor_resolved'])} | "
                f"{_pct(row['too_far_rate'])} | {_pct(row['too_short_rate'])} |"
            )
    lines.extend(
        [
            "",
            "## LONG / SHORT — 4h, periodo completo",
            "",
            "| Clase | Dirección | Candidato | Casos | TP primero | SL primero | Expectancy R resuelta | PF resuelto |",
            "|---|---|---|---:|---:|---:|---:|---:|",
        ]
    )
    for row in rows:
        selected = row["candidate"] in {"CURRENT", "R_0_75" if row["trade_class"] == "CHILL" else "R_1_25"}
        if row["scope"] == "DIRECTION" and row["period"] == "ALL" and row["horizon"] == "4h" and selected:
            lines.append(
                f"| {row['trade_class']} | {row['direction']} | {row['candidate']} | {row['cases']} | "
                f"{_pct(row['tp_first_rate'])} | {_pct(row['sl_first_rate'])} | "
                f"{_number(row['expectancy_r_resolved'])} | {_number(row['profit_factor_resolved'])} |"
            )
    lines.extend(
        [
            "",
            "## Por setup — 4h, periodo completo",
            "",
            "| Clase | Dirección | Setup | Candidato | Casos | TP primero | SL primero | Expectancy R resuelta | PF resuelto |",
            "|---|---|---|---|---:|---:|---:|---:|---:|",
        ]
    )
    for row in rows:
        selected = row["candidate"] in {"CURRENT", "R_0_75" if row["trade_class"] == "CHILL" else "R_1_25"}
        if row["scope"] == "SETUP" and row["period"] == "ALL" and row["horizon"] == "4h" and selected:
            lines.append(
                f"| {row['trade_class']} | {row['direction']} | {row['setup']} | {row['candidate']} | "
                f"{row['cases']} | {_pct(row['tp_first_rate'])} | {_pct(row['sl_first_rate'])} | "
                f"{_number(row['expectancy_r_resolved'])} | {_number(row['profit_factor_resolved'])} |"
            )
    lines.extend(["", "## Candidato diagnosticado", ""])
    for trade_class in ("CHILL", "GOD"):
        lines.append(f"- {trade_class}: **{best[trade_class][0]}** — {best[trade_class][1]}.")
    lines.extend(
        [
            "",
            "La selección solo compara el conjunto pequeño solicitado y exige soporte en VALIDATION/OOS; no cambia producción ni constituye optimización automática. La censura a 4h es alta, por lo que el candidato es relativo al universo resuelto y no equivale a aprobación PAPER.",
            "",
        ]
    )
    return "\n".join(lines)


def _capital_markdown(rows: Sequence[dict], summary: dict) -> str:
    global_all = next(row for row in rows if row["scope"] == "GLOBAL" and row["period"] == "ALL")
    lines = [
        "# Minimum viable account size",
        "",
        "> Diagnóstico Capital.com XAUUSD · RiskPercent=5.0 · DailyLossLimitPercent=5.0 · primer trade del día.",
        "",
        "Supuestos reproducibles: contract_size=100 oz, volume_min=0.01, volume_step=0.01, tick/point=0.01, desviación máxima=30 puntos, comisión configurada=0. El riesgo mínimo replica OrderCalcProfit lineal en USD desde el peor fill permitido hasta el SL CURRENT.",
        "",
        "`minimum_equity = risk_at_min_lot / 0.05`. Un caso no ejecutable se clasifica `MIN_LOT_RISK_TOO_HIGH`; el SL nunca se estrecha para forzar entrada. Ganancias del día no elevan el presupuesto diario y pérdidas previas pueden reducir aún más la viabilidad aquí mostrada.",
        "",
        "## Capital mínimo",
        "",
        "| P10 | P25 | P50 | P75 | P90 | P95 |",
        "|---:|---:|---:|---:|---:|---:|",
        "| " + " | ".join(f"${global_all[f'minimum_equity_p{p}']:.2f}" for p in (10, 25, 50, 75, 90, 95)) + " |",
        "",
        "## Señales ejecutables al inicio del día",
        "",
        "| Equity | Ejecutables | Bloqueadas MIN_LOT_RISK_TOO_HIGH |",
        "|---:|---:|---:|",
    ]
    for equity in ACCOUNT_EQUITIES:
        lines.append(
            f"| ${equity} | {_pct(global_all[f'executable_rate_{equity}'])} | "
            f"{global_all[f'min_lot_risk_too_high_{equity}']} |"
        )
    lines.extend(
        [
            "",
            "## Por dirección/clase",
            "",
            "| Clase | Dirección | Casos | P50 | P75 | P90 | P95 | Ejecutable con $200 |",
            "|---|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in rows:
        if row["scope"] == "DIRECTION" and row["period"] == "ALL":
            lines.append(
                f"| {row['trade_class']} | {row['direction']} | {row['cases']} | "
                f"${row['minimum_equity_p50']:.2f} | ${row['minimum_equity_p75']:.2f} | "
                f"${row['minimum_equity_p90']:.2f} | ${row['minimum_equity_p95']:.2f} | "
                f"{_pct(row['executable_rate_200'])} |"
            )
    setup_rows = [
        row for row in rows
        if row["scope"] == "SETUP" and row["period"] == "ALL" and int(row["cases"]) >= 30
    ]
    setup_rows.sort(key=lambda row: float(row["minimum_equity_p90"]), reverse=True)
    lines.extend(
        [
            "",
            "## Setups más exigentes por P90",
            "",
            "| Clase | Dirección | Setup | Casos | P50 | P90 | P95 | Ejecutable con $200 |",
            "|---|---|---|---:|---:|---:|---:|---:|",
        ]
    )
    for row in setup_rows[:5]:
        lines.append(
            f"| {row['trade_class']} | {row['direction']} | {row['setup']} | {row['cases']} | "
            f"${row['minimum_equity_p50']:.2f} | ${row['minimum_equity_p90']:.2f} | "
            f"${row['minimum_equity_p95']:.2f} | {_pct(row['executable_rate_200'])} |"
        )
    lines.extend(
        [
            "",
            "## Recomendación diagnóstica de cobertura",
            "",
            f"- PAPER: alrededor de P75 (${global_all['minimum_equity_p75']:.2f}) permite que el lote mínimo respete el 5% en aproximadamente 75% de estos setups, antes de pérdidas diarias previas.",
            f"- LIVE: P90–P95 (${global_all['minimum_equity_p90']:.2f}–${global_all['minimum_equity_p95']:.2f}) es una referencia más prudente de cobertura, no una autorización ni garantía de ejecución.",
            f"- $200 cubre solo {_pct(global_all['executable_rate_200'])}; resulta demasiado pequeño para este contrato/SL en la gran mayoría de observaciones.",
        ]
    )
    lines.extend(
        [
            "",
            f"Casos con tick/SL reproducible: {global_all['cases']}. Moneda de depósito asumida para este contrato diagnóstico: USD.",
            "",
            "La cuenta mínima depende del SL de cada setup, no de un único saldo universal. stops_level, margen libre, slippage real y especificaciones runtime todavía pueden bloquear una entrada que pase esta prueba de riesgo mínimo.",
            "",
        ]
    )
    return "\n".join(lines)


def analyze_take_profit_and_account_size(
    input_dir: Path,
    tick_files: Sequence[Path],
    output_dir: Path,
) -> dict:
    input_dir, output_dir = Path(input_dir), Path(output_dir)
    base_signals, split_counts, enrichment = prepare_signals(input_dir)
    signals = _annotate_trade_classes(base_signals, input_dir)
    outcomes, _, _ = _load_outcomes(input_dir / "historical_outcomes.jsonl")
    stats = IngestionStats()
    tick_report = simulate_tp_tick_order(signals, iter_ticks(tick_files, stats))
    ranges = M1RangeIndex(_load_bars(input_dir, "XAUUSD", "M1"))
    tp_details = tp_detail_rows(signals, outcomes, ranges)
    tp_rows = aggregate_tp(tp_details)
    capital_details = capital_detail_rows(signals)
    capital_rows = aggregate_capital(capital_details)
    best = {trade_class: select_best_candidate(tp_rows, trade_class) for trade_class in ("CHILL", "GOD")}
    summary = {
        "analysis_only": True,
        "observer_only": True,
        "score_effect": 0,
        "risk_percent": RISK_PERCENT,
        "daily_loss_limit_percent": DAILY_LOSS_LIMIT_PERCENT,
        "signals_reproducible": len(signals),
        "signals_with_entry_tick": tick_report["signals_with_entry_tick"],
        "split_counts": split_counts,
        "decision_enrichment": enrichment,
        "tick_scan": {**tick_report, "ticks_valid": stats.ticks_valid, "ticks_discarded": stats.ticks_discarded, "duplicates": stats.duplicates},
        "best": best,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write(output_dir / "take_profit_by_setup.csv", _csv_text(tp_rows))
    _atomic_write(output_dir / "take_profit_analysis.md", _tp_markdown(tp_rows, best, summary))
    _atomic_write(output_dir / "minimum_account_size.csv", _csv_text(capital_rows))
    _atomic_write(output_dir / "minimum_account_size.md", _capital_markdown(capital_rows, summary))
    return summary


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    root = Path(__file__).resolve().parent
    parser.add_argument("--input-dir", type=Path, default=root / "output")
    parser.add_argument("--tick-input-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=root / "analysis")
    arguments = parser.parse_args(argv)
    discovery = discover_inputs(input_dir=arguments.tick_input_dir, symbol="XAUUSD")
    if not discovery.files:
        parser.error("no valid XAUUSD tick CSV files found")
    summary = analyze_take_profit_and_account_size(
        arguments.input_dir,
        [item.path for item in discovery.files],
        arguments.output_dir,
    )
    scan = summary["tick_scan"]
    print(f"[TP_ANALYSIS] signals={summary['signals_reproducible']} | entry_ticks={summary['signals_with_entry_tick']}")
    print(f"[TP_ANALYSIS] ticks={scan['ticks_seen']} | elapsed={scan['elapsed_seconds']:.1f}s")
    print(f"[TP_ANALYSIS] best_CHILL={summary['best']['CHILL'][0]} | best_GOD={summary['best']['GOD'][0]}")
    print("[TP_ANALYSIS] observer_only=true | score_effect=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
