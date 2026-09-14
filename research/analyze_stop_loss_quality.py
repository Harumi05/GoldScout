"""Tick-ordered, offline stop-loss quality analysis for GoldScout.

This module never imports or changes the live EA. Stops are hypothetical,
position size is expressed only as an inverse-distance multiplier that preserves
constant monetary risk, and all derived artifacts are diagnostic-only.
"""

from __future__ import annotations

import argparse
from bisect import bisect_left, bisect_right
from collections import defaultdict
from dataclasses import dataclass, field
import csv
import heapq
import io
import itertools
import json
import math
from pathlib import Path
import statistics
import time
from typing import Iterable, Iterator, Sequence

from research.analyze_historical_dataset import (
    _atomic_write,
    _directed_values,
    _load_observations,
    _load_outcomes,
    _merge_decision_enrichment,
    temporal_split,
)
from research.enrich_historical_decisions import _load_bars, build_state_points
from research.tick_historical_replay import IngestionStats, Tick, discover_inputs, iter_ticks


HORIZON_MS = {"15m": 900_000, "1h": 3_600_000, "4h": 14_400_000}
STOP_NAMES = ("CURRENT", "STRUCTURE", "ATR_1_0", "ATR_1_5", "ATR_2_0", "HYBRID")
MIN_STOP_ATR = 0.80
CURRENT_STOP_ATR = 1.20
MAX_STOP_ATR = 2.00
STRUCTURE_BUFFER_ATR = 0.25
STRUCTURE_LOOKBACK = 20
MIN_SEGMENT_CASES = 30
POINT_SIZE = 0.01
PRICE_DIGITS = 2


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _percentile(values: Sequence[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower, upper = math.floor(position), math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _median(values: Iterable[float | int | None]) -> float | None:
    usable = [float(value) for value in values if _is_number(value)]
    return statistics.median(usable) if usable else None


def _mean(values: Iterable[float | int | None]) -> float | None:
    usable = [float(value) for value in values if _is_number(value)]
    return statistics.fmean(usable) if usable else None


def _round_price(value: float) -> float:
    factor = 10**PRICE_DIGITS
    return math.floor(value * factor + 0.5 + 1e-10) / factor


@dataclass
class StopState:
    name: str
    price: float
    distance: float
    distance_atr: float
    hit_ms: int | None = None
    hit_mid: float | None = None
    post_stop_same_minute_high: float | None = None
    post_stop_same_minute_low: float | None = None


@dataclass
class SignalState:
    event_id: str
    anchor_ms: int
    period: str
    direction: str
    setup: str
    reference_close: float
    atr: float
    recent_high: float
    recent_low: float
    swing_high: float | None
    swing_low: float | None
    structure: str
    behavior_shift: str
    momentum: bool
    breakout: bool
    pullback: bool
    recovery: bool
    session: str | None
    spread: float | None
    long_score: float
    short_score: float
    source_timeframe: str
    entry_mid: float | None = None
    entry_bid: float | None = None
    entry_ask: float | None = None
    entry_spread: float | None = None
    entry_tick_ms: int | None = None
    favorable_time_ms: int | None = None
    favorable_partial_high: float | None = None
    favorable_partial_low: float | None = None
    pre_favorable_mae_price: float | None = None
    stops: dict[str, StopState] = field(default_factory=dict)
    atr_regime: str | None = None
    spread_quartile: str | None = None


def build_stop_candidates(
    direction: str,
    entry_price: float,
    atr: float,
    recent_high: float,
    recent_low: float,
    swing_high: float | None,
    swing_low: float | None,
) -> dict[str, StopState]:
    """Reproduce CURRENT and construct the five bounded diagnostic candidates."""
    if direction not in {"LONG", "SHORT"} or entry_price <= 0.0 or atr <= 0.0:
        return {}
    sign = -1.0 if direction == "LONG" else 1.0

    def state(name: str, requested_distance: float) -> StopState | None:
        if not _is_number(requested_distance) or requested_distance <= 0.0:
            return None
        price = _round_price(entry_price + sign * requested_distance)
        actual = entry_price - price if direction == "LONG" else price - entry_price
        if actual <= 0.0:
            return None
        return StopState(name, price, actual, actual / atr)

    fixed_structure = (
        entry_price - (recent_low - STRUCTURE_BUFFER_ATR * atr)
        if direction == "LONG"
        else (recent_high + STRUCTURE_BUFFER_ATR * atr) - entry_price
    )
    current_distance = min(MAX_STOP_ATR * atr, max(MIN_STOP_ATR * atr, CURRENT_STOP_ATR * atr, fixed_structure))
    relevant_swing = swing_low if direction == "LONG" else swing_high
    pivot_structure = None
    if _is_number(relevant_swing):
        pivot_structure = (
            entry_price - (float(relevant_swing) - STRUCTURE_BUFFER_ATR * atr)
            if direction == "LONG"
            else (float(relevant_swing) + STRUCTURE_BUFFER_ATR * atr) - entry_price
        )
        if pivot_structure <= 0.0:
            pivot_structure = None
    distances = {
        "CURRENT": current_distance,
        "STRUCTURE": pivot_structure,
        "ATR_1_0": atr,
        "ATR_1_5": 1.5 * atr,
        "ATR_2_0": 2.0 * atr,
        "HYBRID": max(MIN_STOP_ATR * atr, pivot_structure or 0.0),
    }
    return {name: candidate for name in STOP_NAMES if (candidate := state(name, distances[name])) is not None}


def _setup(record: dict, direction: str) -> tuple[str, bool, bool, bool, bool]:
    h1 = record.get("h1_structure") if isinstance(record.get("h1_structure"), dict) else {}
    suffix = "long" if direction == "LONG" else "short"
    breakout = bool(h1.get(f"breakout_{suffix}"))
    pullback = bool(h1.get(f"pullback_{suffix}"))
    momentum = bool(h1.get(f"momentum_{suffix}"))
    m15 = record.get("m15_timing") if isinstance(record.get("m15_timing"), dict) else {}
    recovery = str(m15.get("recovery", "NONE")).upper() == direction
    name = "BREAKOUT" if breakout else "PULLBACK" if pullback else "MOMENTUM" if momentum else f"CONTINUATION_{direction}"
    return name, momentum, breakout, pullback, recovery


def prepare_signals(input_dir: Path) -> tuple[list[SignalState], dict, dict]:
    observations, _ = _load_observations(Path(input_dir) / "historical_observations.jsonl")
    assignments, split_counts = temporal_split(observations)
    enrichment = _merge_decision_enrichment(observations, Path(input_dir) / "historical_decisions.jsonl")
    h1_bars = _load_bars(Path(input_dir), "XAUUSD", "H1")
    h1_points = build_state_points(h1_bars, POINT_SIZE)
    close_times = [int(point.bar["close_timestamp"]) for point in h1_points]
    priorities = {"H1": 3, "M15": 2, "H4": 1}
    selected: dict[tuple[int, str], dict] = {}
    for item in observations:
        if item.get("decision") != "ARMED" or item.get("armed_direction") not in {"LONG", "SHORT"}:
            continue
        anchor = item.get("observed_at")
        if not _is_number(anchor):
            continue
        key = (int(anchor), str(item["armed_direction"]))
        current = selected.get(key)
        if current is None or priorities.get(str(item.get("timeframe")), 0) > priorities.get(str(current.get("timeframe")), 0):
            selected[key] = item
    signals: list[SignalState] = []
    for item in selected.values():
        anchor = int(item["observed_at"])
        index = bisect_right(close_times, anchor) - 1
        if index < STRUCTURE_LOOKBACK:
            continue
        point = h1_points[index]
        atr = point.indicators.get("atr")
        if not _is_number(atr) or float(atr) <= 0.0:
            continue
        prior = h1_points[index - STRUCTURE_LOOKBACK : index]
        direction = str(item["armed_direction"])
        setup, momentum, breakout, pullback, recovery = _setup(item, direction)
        h1_structure = item.get("h1_structure") if isinstance(item.get("h1_structure"), dict) else {}
        regime = item.get("h1_regime") if isinstance(item.get("h1_regime"), dict) else {}
        signals.append(
            SignalState(
                event_id=item["event_id"],
                anchor_ms=anchor,
                period=assignments[item["event_id"]],
                direction=direction,
                setup=setup,
                reference_close=float(item["close"]),
                atr=float(atr),
                recent_high=max(float(value.bar["high"]) for value in prior),
                recent_low=min(float(value.bar["low"]) for value in prior),
                swing_high=point.structure.last_high,
                swing_low=point.structure.last_low,
                structure=str(h1_structure.get("pivot_state", "INSUFFICIENT")),
                behavior_shift=str(regime.get("behavior_shift", "NONE")),
                momentum=momentum,
                breakout=breakout,
                pullback=pullback,
                recovery=recovery,
                session=item.get("session"),
                spread=float(item["spread"]) if _is_number(item.get("spread")) else None,
                long_score=float(item["long_score"]),
                short_score=float(item["short_score"]),
                source_timeframe=str(item.get("timeframe", "UNKNOWN")),
            )
        )
    signals.sort(key=lambda item: (item.anchor_ms, item.event_id))
    _assign_regimes(signals)
    return signals, split_counts, enrichment


def _bucket(value: float | None, cuts: Sequence[float], labels: Sequence[str]) -> str | None:
    if value is None:
        return None
    for index, cut in enumerate(cuts):
        if value <= cut:
            return labels[index]
    return labels[-1]


def _assign_regimes(signals: Sequence[SignalState]) -> None:
    train_atr = sorted(item.atr / item.reference_close for item in signals if item.period == "TRAIN" and item.reference_close > 0.0)
    train_spread = sorted(item.spread for item in signals if item.period == "TRAIN" and item.spread is not None)
    atr_cuts = [_percentile(train_atr, value) or 0.0 for value in (1 / 3, 2 / 3)]
    spread_cuts = [_percentile(train_spread, value) or 0.0 for value in (0.25, 0.50, 0.75)]
    for item in signals:
        item.atr_regime = _bucket(item.atr / item.reference_close, atr_cuts, ("LOW", "MEDIUM", "HIGH"))
        item.spread_quartile = _bucket(item.spread, spread_cuts, ("Q1", "Q2", "Q3", "Q4"))


def simulate_tick_order(signals: Sequence[SignalState], ticks: Iterable[Tick], *, progress_every: int = 5_000_000) -> dict:
    """Find exact first stop/1-ATR timestamps in one causal chronological scan."""
    pending = sorted(signals, key=lambda item: (item.anchor_ms, item.event_id))
    signal_index = 0
    counter = itertools.count()
    long_stops: list[tuple[float, int, SignalState, StopState]] = []
    short_stops: list[tuple[float, int, SignalState, StopState]] = []
    long_targets: list[tuple[float, int, SignalState]] = []
    short_targets: list[tuple[float, int, SignalState]] = []
    minute_hits: list[StopState] = []
    current_minute: int | None = None
    minute_high = minute_low = None
    ticks_seen = 0
    started = time.perf_counter()

    def expired(signal: SignalState, stamp: int) -> bool:
        return stamp > signal.anchor_ms + HORIZON_MS["4h"]

    for tick in ticks:
        ticks_seen += 1
        minute = tick.timestamp_ms // 60_000 * 60_000
        if minute != current_minute:
            current_minute = minute
            minute_hits = []
            minute_high = minute_low = tick.mid
        else:
            minute_high = max(float(minute_high), tick.mid)
            minute_low = min(float(minute_low), tick.mid)

        while signal_index < len(pending) and pending[signal_index].anchor_ms <= tick.timestamp_ms:
            signal = pending[signal_index]
            signal_index += 1
            if expired(signal, tick.timestamp_ms):
                continue
            signal.entry_mid = tick.mid
            signal.entry_bid = tick.bid
            signal.entry_ask = tick.ask
            signal.entry_spread = tick.spread
            signal.entry_tick_ms = tick.timestamp_ms
            entry = tick.ask if signal.direction == "LONG" else tick.bid
            signal.stops = build_stop_candidates(
                signal.direction,
                entry,
                signal.atr,
                signal.recent_high,
                signal.recent_low,
                signal.swing_high,
                signal.swing_low,
            )
            for stop in signal.stops.values():
                if signal.direction == "LONG":
                    heapq.heappush(long_stops, (-stop.price, next(counter), signal, stop))
                else:
                    heapq.heappush(short_stops, (stop.price, next(counter), signal, stop))
            target = signal.entry_mid + signal.atr if signal.direction == "LONG" else signal.entry_mid - signal.atr
            if signal.direction == "LONG":
                heapq.heappush(long_targets, (target, next(counter), signal))
            else:
                heapq.heappush(short_targets, (-target, next(counter), signal))

        while long_stops:
            stop_price = -long_stops[0][0]
            _, _, signal, stop = long_stops[0]
            if stop.hit_ms is not None or expired(signal, tick.timestamp_ms):
                heapq.heappop(long_stops)
            elif stop_price >= tick.bid:
                heapq.heappop(long_stops)
                stop.hit_ms = tick.timestamp_ms
                stop.hit_mid = tick.mid
                stop.post_stop_same_minute_high = tick.mid
                stop.post_stop_same_minute_low = tick.mid
                minute_hits.append(stop)
            else:
                break
        while short_stops:
            stop_price, _, signal, stop = short_stops[0]
            if stop.hit_ms is not None or expired(signal, tick.timestamp_ms):
                heapq.heappop(short_stops)
            elif stop_price <= tick.ask:
                heapq.heappop(short_stops)
                stop.hit_ms = tick.timestamp_ms
                stop.hit_mid = tick.mid
                stop.post_stop_same_minute_high = tick.mid
                stop.post_stop_same_minute_low = tick.mid
                minute_hits.append(stop)
            else:
                break

        while long_targets:
            target, _, signal = long_targets[0]
            if signal.favorable_time_ms is not None or expired(signal, tick.timestamp_ms):
                heapq.heappop(long_targets)
            elif target <= tick.mid:
                heapq.heappop(long_targets)
                signal.favorable_time_ms = tick.timestamp_ms
                signal.favorable_partial_high = float(minute_high)
                signal.favorable_partial_low = float(minute_low)
            else:
                break
        while short_targets:
            target = -short_targets[0][0]
            _, _, signal = short_targets[0]
            if signal.favorable_time_ms is not None or expired(signal, tick.timestamp_ms):
                heapq.heappop(short_targets)
            elif target >= tick.mid:
                heapq.heappop(short_targets)
                signal.favorable_time_ms = tick.timestamp_ms
                signal.favorable_partial_high = float(minute_high)
                signal.favorable_partial_low = float(minute_low)
            else:
                break
        for stop in minute_hits:
            stop.post_stop_same_minute_high = max(float(stop.post_stop_same_minute_high), tick.mid)
            stop.post_stop_same_minute_low = min(float(stop.post_stop_same_minute_low), tick.mid)
        if progress_every and ticks_seen % progress_every == 0:
            elapsed = time.perf_counter() - started
            print(f"[STOP_ANALYSIS] ticks={ticks_seen:,} | ticks/s={ticks_seen / elapsed:,.0f} | elapsed={elapsed:.1f}s")
    return {
        "ticks_seen": ticks_seen,
        "signals_requested": len(signals),
        "signals_with_entry_tick": sum(item.entry_tick_ms is not None for item in signals),
        "elapsed_seconds": time.perf_counter() - started,
    }


class M1RangeIndex:
    """O(log n) extrema queries over tick-exact M1 mid bars."""

    def __init__(self, bars: Sequence[dict]):
        self.timestamps = [int(item["timestamp"]) for item in bars]
        count = 1
        while count < len(bars):
            count *= 2
        self.size = count
        self.high = [-math.inf] * (2 * count)
        self.low = [math.inf] * (2 * count)
        for index, item in enumerate(bars):
            self.high[count + index] = float(item["high"])
            self.low[count + index] = float(item["low"])
        for index in range(count - 1, 0, -1):
            self.high[index] = max(self.high[2 * index], self.high[2 * index + 1])
            self.low[index] = min(self.low[2 * index], self.low[2 * index + 1])

    def extrema(self, start_ms: int, end_ms: int) -> tuple[float | None, float | None]:
        left = bisect_left(self.timestamps, start_ms) + self.size
        right = bisect_left(self.timestamps, end_ms) + self.size
        maximum, minimum = -math.inf, math.inf
        while left < right:
            if left & 1:
                maximum, minimum = max(maximum, self.high[left]), min(minimum, self.low[left])
                left += 1
            if right & 1:
                right -= 1
                maximum, minimum = max(maximum, self.high[right]), min(minimum, self.low[right])
            left //= 2
            right //= 2
        return (None if maximum == -math.inf else maximum, None if minimum == math.inf else minimum)


def finalize_tick_metrics(signals: Sequence[SignalState], ranges: M1RangeIndex) -> None:
    for signal in signals:
        if signal.entry_mid is None:
            continue
        if signal.favorable_time_ms is not None:
            favorable_minute = signal.favorable_time_ms // 60_000 * 60_000
            entry_minute = signal.anchor_ms // 60_000 * 60_000
            maximum, minimum = ranges.extrema(entry_minute, favorable_minute)
            highs = [value for value in (maximum, signal.favorable_partial_high) if _is_number(value)]
            lows = [value for value in (minimum, signal.favorable_partial_low) if _is_number(value)]
            if signal.direction == "LONG" and lows:
                signal.pre_favorable_mae_price = max(0.0, signal.entry_mid - min(lows))
            elif signal.direction == "SHORT" and highs:
                signal.pre_favorable_mae_price = max(0.0, max(highs) - signal.entry_mid)


def _post_stop_favorable(signal: SignalState, stop: StopState, horizon_end: int, ranges: M1RangeIndex) -> float | None:
    if signal.entry_mid is None or stop.hit_ms is None or stop.hit_ms > horizon_end:
        return None
    stop_minute = stop.hit_ms // 60_000 * 60_000
    next_minute = stop_minute + 60_000
    maximum, minimum = ranges.extrema(next_minute, horizon_end)
    partial_high = stop.hit_mid if stop_minute >= horizon_end else stop.post_stop_same_minute_high
    partial_low = stop.hit_mid if stop_minute >= horizon_end else stop.post_stop_same_minute_low
    highs = [value for value in (maximum, partial_high) if _is_number(value)]
    lows = [value for value in (minimum, partial_low) if _is_number(value)]
    if signal.direction == "LONG" and highs:
        return max(0.0, max(highs) - signal.entry_mid)
    if signal.direction == "SHORT" and lows:
        return max(0.0, signal.entry_mid - min(lows))
    return None


def _excess_thresholds(signals: Sequence[SignalState]) -> dict[tuple[str, str], float]:
    groups: dict[tuple[str, str], list[float]] = defaultdict(list)
    fallback: dict[str, list[float]] = defaultdict(list)
    for signal in signals:
        if signal.period != "TRAIN" or signal.pre_favorable_mae_price is None or signal.atr <= 0.0:
            continue
        value = signal.pre_favorable_mae_price / signal.atr
        groups[(signal.setup, signal.direction)].append(value)
        fallback[signal.direction].append(value)
    result: dict[tuple[str, str], float] = {}
    for signal in signals:
        key = (signal.setup, signal.direction)
        if key in result:
            continue
        sample = groups[key] if len(groups[key]) >= MIN_SEGMENT_CASES else fallback[signal.direction]
        p90 = _percentile(sample, 0.90)
        if p90 is not None:
            result[key] = 1.5 * p90
    return result


def candidate_rows(signals: Sequence[SignalState], outcomes: dict, ranges: M1RangeIndex) -> list[dict]:
    thresholds = _excess_thresholds(signals)
    rows: list[dict] = []
    for signal in signals:
        if signal.entry_mid is None:
            continue
        for name, stop in signal.stops.items():
            for horizon, duration in HORIZON_MS.items():
                end = signal.anchor_ms + duration
                hit = stop.hit_ms is not None and stop.hit_ms <= end
                post_stop = _post_stop_favorable(signal, stop, end, ranges) if hit else None
                favorable_before_end = signal.favorable_time_ms is not None and signal.favorable_time_ms <= end
                hit_before_favorable = hit and (
                    not favorable_before_end or int(stop.hit_ms) < int(signal.favorable_time_ms)
                )
                premature = hit and _is_number(post_stop) and float(post_stop) >= signal.atr
                outcome = outcomes.get(signal.event_id, {}).get(horizon)
                directed = _directed_values(outcome, signal.direction) if outcome else (None, None, None)
                mae_before = (
                    signal.pre_favorable_mae_price
                    if favorable_before_end and signal.pre_favorable_mae_price is not None
                    else (abs(float(directed[2])) * signal.reference_close if _is_number(directed[2]) else None)
                )
                excess_limit = thresholds.get((signal.setup, signal.direction))
                rows.append(
                    {
                        "event_id": signal.event_id,
                        "period": signal.period,
                        "timestamp": signal.anchor_ms,
                        "direction": signal.direction,
                        "setup": signal.setup,
                        "candidate": name,
                        "horizon": horizon,
                        "entry_mid": signal.entry_mid,
                        "entry_spread": signal.entry_spread,
                        "atr": signal.atr,
                        "stop_price": stop.price,
                        "stop_distance": stop.distance,
                        "stop_distance_atr": stop.distance_atr,
                        "stop_hit": hit,
                        "hit_before_favorable_1atr": hit_before_favorable,
                        "premature_stop": premature,
                        "time_to_stop_minutes": (int(stop.hit_ms) - signal.anchor_ms) / 60_000.0 if hit else None,
                        "mae_before_favorable_price": mae_before,
                        "mae_before_favorable_atr": mae_before / signal.atr if _is_number(mae_before) else None,
                        "mfe_after_stop_price": post_stop,
                        "mfe_after_stop_atr": post_stop / signal.atr if _is_number(post_stop) else None,
                        "future_return": directed[0],
                        "mfe": directed[1],
                        "mae": directed[2],
                        "potential_mfe_r": (
                            float(directed[1]) * signal.reference_close / stop.distance if _is_number(directed[1]) else None
                        ),
                        "excess_distance_train_threshold_atr": excess_limit,
                        "excessively_wide": stop.distance_atr > excess_limit if excess_limit is not None else None,
                        "constant_risk_lot_multiplier_vs_current": (
                            signal.stops["CURRENT"].distance / stop.distance if "CURRENT" in signal.stops else None
                        ),
                        "momentum": signal.momentum,
                        "breakout": signal.breakout,
                        "pullback": signal.pullback,
                        "recovery": signal.recovery,
                        "structure": signal.structure,
                        "behavior_shift": signal.behavior_shift,
                        "atr_regime": signal.atr_regime,
                        "spread_quartile": signal.spread_quartile,
                        "session": signal.session,
                        "observer_only": True,
                        "diagnostic_only": True,
                        "score_effect": 0,
                    }
                )
    return rows


def summarize_stop_rows(rows: Sequence[dict]) -> dict:
    count = len(rows)
    hits = [row for row in rows if row["stop_hit"]]
    premature = [row for row in rows if row["premature_stop"]]
    mae_before = [float(row["mae_before_favorable_atr"]) for row in rows if _is_number(row["mae_before_favorable_atr"])]
    distances = [float(row["stop_distance_atr"]) for row in rows if _is_number(row["stop_distance_atr"])]
    return {
        "cases": count,
        "stop_survival_rate": (count - len(hits)) / count if count else None,
        "stop_hit_rate": len(hits) / count if count else None,
        "touched_before_favorable_rate": sum(bool(row["hit_before_favorable_1atr"]) for row in rows) / count if count else None,
        "premature_stop_rate": len(premature) / count if count else None,
        "median_mae_before_mfe_atr": _median(row["mae_before_favorable_atr"] for row in rows),
        "mae_before_mfe_atr_p25": _percentile(mae_before, 0.25),
        "mae_before_mfe_atr_p75": _percentile(mae_before, 0.75),
        "mae_before_mfe_atr_p90": _percentile(mae_before, 0.90),
        "median_distance_atr": _median(row["stop_distance_atr"] for row in rows),
        "distance_atr_p25": _percentile(distances, 0.25),
        "distance_atr_p75": _percentile(distances, 0.75),
        "median_mfe_after_stop_atr": _median(row["mfe_after_stop_atr"] for row in hits),
        "median_time_to_stop_minutes": _median(row["time_to_stop_minutes"] for row in hits),
        "excess_distance_rate": (
            sum(row["excessively_wide"] is True for row in rows)
            / sum(row["excessively_wide"] is not None for row in rows)
            if any(row["excessively_wide"] is not None for row in rows)
            else None
        ),
        "mean_future_return": _mean(row["future_return"] for row in rows),
        "win_rate": (
            sum(_is_number(row["future_return"]) and float(row["future_return"]) > 0.0 for row in rows)
            / sum(_is_number(row["future_return"]) for row in rows)
            if any(_is_number(row["future_return"]) for row in rows)
            else None
        ),
        "mean_mfe": _mean(row["mfe"] for row in rows),
        "mean_mae": _mean(row["mae"] for row in rows),
        "median_potential_mfe_r": _median(row["potential_mfe_r"] for row in rows),
    }


def _aggregate(rows: Sequence[dict], *, segmented: bool) -> list[dict]:
    groups: dict[tuple, list[dict]] = defaultdict(list)
    segment_fields = (
        ("setup", lambda row: row["setup"]),
        ("momentum", lambda row: "YES" if row["momentum"] else "NO"),
        ("breakout", lambda row: "YES" if row["breakout"] else "NO"),
        ("pullback", lambda row: "YES" if row["pullback"] else "NO"),
        ("recovery", lambda row: "YES" if row["recovery"] else "NO"),
        ("structure", lambda row: row["structure"]),
        ("behavior_shift", lambda row: row["behavior_shift"]),
        ("atr_regime", lambda row: row["atr_regime"]),
        ("spread_quartile", lambda row: row["spread_quartile"]),
    )
    for row in rows:
        periods = ("ALL", row["period"])
        directions = ("ALL", row["direction"])
        if segmented:
            for field, getter in segment_fields:
                value = getter(row)
                for period in periods:
                    for direction in directions:
                        groups[(period, direction, field, str(value), row["candidate"], row["horizon"])].append(row)
        else:
            for period in periods:
                for direction in directions:
                    groups[(period, direction, "ALL", "ALL", row["candidate"], row["horizon"])].append(row)
    result: list[dict] = []
    for key, members in sorted(groups.items()):
        if segmented and len(members) < MIN_SEGMENT_CASES:
            continue
        period, direction, segment, value, candidate, horizon = key
        result.append(
            {
                "period": period,
                "direction": direction,
                "segment": segment,
                "segment_value": value,
                "candidate": candidate,
                "horizon": horizon,
                **summarize_stop_rows(members),
                "diagnostic_only": True,
                "score_effect": 0,
            }
        )
    return result


def _csv_text(rows: Sequence[dict]) -> str:
    if not rows:
        return "period,direction,candidate,horizon\n"
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def _markdown(summary: dict, candidates: Sequence[dict]) -> str:
    lines = [
        "# Stop Loss Quality — diagnóstico histórico",
        "",
        "> Solo investigación · score_effect=0 · riesgo monetario conceptual constante.",
        "",
        "## Definiciones",
        "",
        "- Stop hit: primer tick BID <= SL para LONG o ASK >= SL para SHORT.",
        "- Premature stop: el stop se toca y después el mid avanza al menos 1 ATR en la dirección original dentro del horizonte.",
        "- Excessively wide: distancia > 1.5 × P90 TRAIN del MAE previo al primer +1 ATR para el mismo setup/dirección; fallback direccional con muestra insuficiente.",
        "- Un stop sobreviviente no se clasifica automáticamente como ganador.",
        "- El multiplicador de lotaje es solo la relación inversa de distancias para conservar el mismo riesgo monetario; nunca aumenta RiskPercent.",
        "",
        "## Comparación principal 4h",
        "",
        "| Periodo | Candidato | Casos | Supervivencia | Prematuro | Distancia ATR | MFE post-stop ATR | Exceso |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in candidates:
        if row["direction"] != "ALL" or row["horizon"] != "4h":
            continue
        lines.append(
            f"| {row['period']} | {row['candidate']} | {row['cases']} | {100 * row['stop_survival_rate']:.2f}% | "
            f"{100 * row['premature_stop_rate']:.2f}% | {row['median_distance_atr']:.3f} | "
            f"{row['median_mfe_after_stop_atr'] if row['median_mfe_after_stop_atr'] is not None else 0:.3f} | "
            f"{100 * row['excess_distance_rate'] if row['excess_distance_rate'] is not None else 0:.2f}% |"
        )
    lines.extend(
        [
            "",
            "## 1. Hallazgos sólidos",
            "",
            *[f"- {value}" for value in summary["findings"]["solid"]],
            "",
            "## 2. Hallazgos débiles",
            "",
            *[f"- {value}" for value in summary["findings"]["weak"]],
            "",
            "## 3. TRAIN-only",
            "",
            *[f"- {value}" for value in summary["findings"]["train_only"]],
            "",
            "## 4. VALIDATION/OOS confirmados",
            "",
            *[f"- {value}" for value in summary["findings"]["validated"]],
            "",
            "## 5. Recomendaciones de hipótesis para probar",
            "",
            *[f"- {value}" for value in summary["findings"]["hypotheses"]],
            "",
        ]
    )
    return "\n".join(lines)


def _derive_findings(candidates: Sequence[dict]) -> dict:
    by_key = {(row["period"], row["candidate"], row["horizon"], row["direction"]): row for row in candidates}
    solid: list[str] = []
    weak: list[str] = []
    train_only: list[str] = []
    validated: list[str] = []
    for candidate in STOP_NAMES:
        rows = [by_key.get((period, candidate, "4h", "ALL")) for period in ("TRAIN", "VALIDATION", "OOS")]
        if all(rows):
            rates = [float(row["premature_stop_rate"]) for row in rows]
            if max(rates) - min(rates) <= 0.05:
                solid.append(f"{candidate}: premature-stop 4h estable entre periodos ({', '.join(f'{100*v:.1f}%' for v in rates)}).")
            else:
                weak.append(f"{candidate}: premature-stop 4h no es estable ({', '.join(f'{100*v:.1f}%' for v in rates)}).")
            if rates[1] <= rates[0] and rates[2] <= rates[0]:
                validated.append(f"{candidate}: VALIDATION y OOS no empeoran la tasa prematura observada en TRAIN.")
            else:
                train_only.append(f"{candidate}: la ventaja aparente de TRAIN no se conserva plenamente fuera de muestra.")
    return {
        "solid": solid or ["Ningún candidato cumplió todavía el criterio estricto de estabilidad."],
        "weak": weak or ["Sin hallazgos débiles adicionales."],
        "train_only": train_only or ["No se identificó una ventaja exclusiva de TRAIN."],
        "validated": validated or ["Ningún candidato quedó confirmado simultáneamente en VALIDATION y OOS."],
        "hypotheses": [
            "Comparar CURRENT frente a HYBRID por setup sin cambiar RiskPercent ni pesos live.",
            "Validar en Strategy Tester el efecto de BID/ASK, gaps, slippage y stops_level del broker.",
            "Tratar supervivencia y resultado direccional como métricas separadas antes de elegir un SL.",
        ],
    }


def analyze_stop_loss_quality(input_dir: Path, tick_files: Sequence[Path], output_dir: Path) -> dict:
    input_dir, output_dir = Path(input_dir), Path(output_dir)
    signals, split_counts, enrichment = prepare_signals(input_dir)
    outcomes, _, _ = _load_outcomes(input_dir / "historical_outcomes.jsonl")
    stats = IngestionStats()
    tick_report = simulate_tick_order(signals, iter_ticks(tick_files, stats))
    ranges = M1RangeIndex(_load_bars(input_dir, "XAUUSD", "M1"))
    finalize_tick_metrics(signals, ranges)
    details = candidate_rows(signals, outcomes, ranges)
    candidates = _aggregate(details, segmented=False)
    segments = _aggregate(details, segmented=True)
    summary = {
        "analysis_only": True,
        "diagnostic_only": True,
        "observer_only": True,
        "score_effect": 0,
        "risk_percent_changed": False,
        "risk_policy": "constant monetary risk; conceptual lot multiplier is inverse to stop distance",
        "split_counts": split_counts,
        "decision_enrichment": enrichment,
        "signals_reproducible": len(signals),
        "tick_scan": {**tick_report, "ticks_valid": stats.ticks_valid, "ticks_discarded": stats.ticks_discarded, "duplicates": stats.duplicates},
        "definitions": {
            "current": "EA BuildDynamicStop: max(1.2 ATR, prior-20-bar structure + 0.25 ATR), clamped 0.8..2.0 ATR",
            "structure": "last confirmed relevant H1 pivot plus 0.25 ATR buffer",
            "hybrid": "max(confirmed-pivot distance, 0.8 ATR)",
            "premature_stop": "stop touched, then mid reaches >=1 ATR in original direction within horizon",
            "excessively_wide": "distance > 1.5 x TRAIN P90 MAE before first +1 ATR for setup/direction",
            "tp_based_r_available": False,
            "potential_mfe_r": "realized horizon MFE divided by hypothetical stop distance; not a TP or realized R",
        },
    }
    summary["findings"] = _derive_findings(candidates)
    output_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write(output_dir / "stop_loss_candidates.csv", _csv_text(candidates))
    _atomic_write(output_dir / "stop_loss_by_setup.csv", _csv_text(segments))
    _atomic_write(output_dir / "stop_loss_analysis.md", _markdown(summary, candidates))
    _atomic_write(output_dir / "stop_loss_analysis.json", json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
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
    summary = analyze_stop_loss_quality(
        arguments.input_dir,
        [item.path for item in discovery.files],
        arguments.output_dir,
    )
    print(f"[STOP_ANALYSIS] signals={summary['signals_reproducible']}")
    print(f"[STOP_ANALYSIS] score_effect={summary['score_effect']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
