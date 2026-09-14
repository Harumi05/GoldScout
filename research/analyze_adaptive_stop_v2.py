"""Offline Adaptive Stop v2 comparison for GoldScout.

The module is diagnostic-only. It reuses the historical decision sidecar and
tick stream, never imports or mutates the live EA, and keeps monetary risk
constant through an inverse-distance position-size multiplier.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass, field
import csv
import heapq
import io
import itertools
import json
from pathlib import Path
import time
from typing import Iterable, Sequence

from research.analyze_historical_dataset import _atomic_write, _directed_values, _load_outcomes
from research.analyze_stop_loss_quality import (
    HORIZON_MS,
    MIN_SEGMENT_CASES,
    M1RangeIndex,
    SignalState,
    StopState,
    _is_number,
    _mean,
    _median,
    _percentile,
    _post_stop_favorable,
    _round_price,
    build_stop_candidates,
    finalize_tick_metrics,
    prepare_signals,
)
from research.enrich_historical_decisions import _load_bars
from research.tick_historical_replay import IngestionStats, Tick, discover_inputs, iter_ticks


CANDIDATES = ("CURRENT", "FLOOR_1_5", "FLOOR_2_0", "SETUP_ADAPTIVE_V1")
R_MULTIPLES = (0.5, 1.0, 1.5, 2.0)
MAX_DIAGNOSTIC_STOP_ATR = 2.0
PRICE_GRID_ATR_TOLERANCE = 0.01


@dataclass
class AdaptiveStopState(StopState):
    r_hit_ms: dict[float, int] = field(default_factory=dict)


def adaptive_setup(signal: SignalState) -> str:
    """Keep the EA evidence priority and expose recovery without double counting."""
    if signal.breakout:
        return "BREAKOUT"
    if signal.pullback:
        return "PULLBACK"
    if signal.momentum:
        return "MOMENTUM"
    if signal.recovery:
        return "RECOVERY"
    return f"CONTINUATION_{signal.direction}"


def _copy_stop(name: str, source: StopState) -> AdaptiveStopState:
    return AdaptiveStopState(name, source.price, source.distance, source.distance_atr)


def build_adaptive_candidates(signal: SignalState, entry_price: float) -> dict[str, AdaptiveStopState]:
    base = build_stop_candidates(
        signal.direction,
        entry_price,
        signal.atr,
        signal.recent_high,
        signal.recent_low,
        signal.swing_high,
        signal.swing_low,
    )
    if not all(name in base for name in ("CURRENT", "ATR_1_5", "ATR_2_0")):
        return {}
    current = base["CURRENT"]

    def wider(left: StopState, right: StopState) -> StopState:
        return right if right.distance > left.distance else left

    floor_15 = wider(current, base["ATR_1_5"])
    floor_20 = wider(current, base["ATR_2_0"])
    setup = adaptive_setup(signal)
    adaptive = floor_20 if setup in {"BREAKOUT", "MOMENTUM", "CONTINUATION_LONG"} else floor_15
    return {
        "CURRENT": _copy_stop("CURRENT", current),
        "FLOOR_1_5": _copy_stop("FLOOR_1_5", floor_15),
        "FLOOR_2_0": _copy_stop("FLOOR_2_0", floor_20),
        "SETUP_ADAPTIVE_V1": _copy_stop("SETUP_ADAPTIVE_V1", adaptive),
    }


def constant_risk_lot_multiplier(current_distance: float, candidate_distance: float) -> float | None:
    if current_distance <= 0.0 or candidate_distance <= 0.0:
        return None
    return current_distance / candidate_distance


def planned_risk_ratio(current_distance: float, candidate_distance: float) -> float | None:
    multiplier = constant_risk_lot_multiplier(current_distance, candidate_distance)
    return multiplier * candidate_distance / current_distance if multiplier is not None else None


def reached_r_before_stop(stop: AdaptiveStopState, multiple: float, horizon_end: int) -> bool:
    reached_at = stop.r_hit_ms.get(multiple)
    hit_in_horizon = stop.hit_ms is not None and stop.hit_ms <= horizon_end
    return reached_at is not None and reached_at <= horizon_end and (
        not hit_in_horizon or reached_at < int(stop.hit_ms)
    )


def simulate_adaptive_tick_order(
    signals: Sequence[SignalState],
    ticks: Iterable[Tick],
    *,
    progress_every: int = 5_000_000,
    candidate_builder=build_adaptive_candidates,
) -> dict:
    """Resolve entries, stops and R targets from one chronological tick pass."""
    pending = sorted(signals, key=lambda item: (item.anchor_ms, item.event_id))
    signal_index = 0
    counter = itertools.count()
    long_stops: list[tuple[float, int, SignalState, AdaptiveStopState]] = []
    short_stops: list[tuple[float, int, SignalState, AdaptiveStopState]] = []
    long_r_targets: list[tuple[float, int, SignalState, AdaptiveStopState, float]] = []
    short_r_targets: list[tuple[float, int, SignalState, AdaptiveStopState, float]] = []
    long_atr_targets: list[tuple[float, int, SignalState]] = []
    short_atr_targets: list[tuple[float, int, SignalState]] = []
    minute_hits: list[AdaptiveStopState] = []
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
            signal.stops = candidate_builder(signal, entry)
            for stop in signal.stops.values():
                if signal.direction == "LONG":
                    heapq.heappush(long_stops, (-stop.price, next(counter), signal, stop))
                else:
                    heapq.heappush(short_stops, (stop.price, next(counter), signal, stop))
                for multiple in R_MULTIPLES:
                    target = _round_price(entry + multiple * stop.distance) if signal.direction == "LONG" else _round_price(entry - multiple * stop.distance)
                    item = (target if signal.direction == "LONG" else -target, next(counter), signal, stop, multiple)
                    heapq.heappush(long_r_targets if signal.direction == "LONG" else short_r_targets, item)
            atr_target = signal.entry_mid + signal.atr if signal.direction == "LONG" else signal.entry_mid - signal.atr
            if signal.direction == "LONG":
                heapq.heappush(long_atr_targets, (atr_target, next(counter), signal))
            else:
                heapq.heappush(short_atr_targets, (-atr_target, next(counter), signal))

        while long_stops:
            stop_price = -long_stops[0][0]
            _, _, signal, stop = long_stops[0]
            if stop.hit_ms is not None or expired(signal, tick.timestamp_ms):
                heapq.heappop(long_stops)
            elif stop_price >= tick.bid:
                heapq.heappop(long_stops)
                stop.hit_ms, stop.hit_mid = tick.timestamp_ms, tick.mid
                stop.post_stop_same_minute_high = stop.post_stop_same_minute_low = tick.mid
                minute_hits.append(stop)
            else:
                break
        while short_stops:
            stop_price, _, signal, stop = short_stops[0]
            if stop.hit_ms is not None or expired(signal, tick.timestamp_ms):
                heapq.heappop(short_stops)
            elif stop_price <= tick.ask:
                heapq.heappop(short_stops)
                stop.hit_ms, stop.hit_mid = tick.timestamp_ms, tick.mid
                stop.post_stop_same_minute_high = stop.post_stop_same_minute_low = tick.mid
                minute_hits.append(stop)
            else:
                break

        while long_r_targets:
            target, _, signal, stop, multiple = long_r_targets[0]
            if multiple in stop.r_hit_ms or expired(signal, tick.timestamp_ms):
                heapq.heappop(long_r_targets)
            elif target <= tick.bid:
                heapq.heappop(long_r_targets)
                stop.r_hit_ms[multiple] = tick.timestamp_ms
            else:
                break
        while short_r_targets:
            target = -short_r_targets[0][0]
            _, _, signal, stop, multiple = short_r_targets[0]
            if multiple in stop.r_hit_ms or expired(signal, tick.timestamp_ms):
                heapq.heappop(short_r_targets)
            elif target >= tick.ask:
                heapq.heappop(short_r_targets)
                stop.r_hit_ms[multiple] = tick.timestamp_ms
            else:
                break

        while long_atr_targets:
            target, _, signal = long_atr_targets[0]
            if signal.favorable_time_ms is not None or expired(signal, tick.timestamp_ms):
                heapq.heappop(long_atr_targets)
            elif target <= tick.mid:
                heapq.heappop(long_atr_targets)
                signal.favorable_time_ms = tick.timestamp_ms
                signal.favorable_partial_high, signal.favorable_partial_low = float(minute_high), float(minute_low)
            else:
                break
        while short_atr_targets:
            target = -short_atr_targets[0][0]
            _, _, signal = short_atr_targets[0]
            if signal.favorable_time_ms is not None or expired(signal, tick.timestamp_ms):
                heapq.heappop(short_atr_targets)
            elif target >= tick.mid:
                heapq.heappop(short_atr_targets)
                signal.favorable_time_ms = tick.timestamp_ms
                signal.favorable_partial_high, signal.favorable_partial_low = float(minute_high), float(minute_low)
            else:
                break
        for stop in minute_hits:
            stop.post_stop_same_minute_high = max(float(stop.post_stop_same_minute_high), tick.mid)
            stop.post_stop_same_minute_low = min(float(stop.post_stop_same_minute_low), tick.mid)

        if progress_every and ticks_seen % progress_every == 0:
            elapsed = time.perf_counter() - started
            print(f"[ADAPTIVE_STOP] ticks={ticks_seen:,} | ticks/s={ticks_seen / elapsed:,.0f} | elapsed={elapsed:.1f}s")
    return {
        "ticks_seen": ticks_seen,
        "signals_requested": len(signals),
        "signals_with_entry_tick": sum(item.entry_tick_ms is not None for item in signals),
        "elapsed_seconds": time.perf_counter() - started,
    }


def _excess_thresholds(signals: Sequence[SignalState]) -> dict[tuple[str, str], float]:
    groups: dict[tuple[str, str], list[float]] = defaultdict(list)
    fallback: dict[str, list[float]] = defaultdict(list)
    for signal in signals:
        if signal.period != "TRAIN" or signal.pre_favorable_mae_price is None or signal.atr <= 0.0:
            continue
        setup = adaptive_setup(signal)
        value = signal.pre_favorable_mae_price / signal.atr
        groups[(setup, signal.direction)].append(value)
        fallback[signal.direction].append(value)
    output: dict[tuple[str, str], float] = {}
    for signal in signals:
        key = (adaptive_setup(signal), signal.direction)
        if key in output:
            continue
        sample = groups[key] if len(groups[key]) >= MIN_SEGMENT_CASES else fallback[signal.direction]
        p90 = _percentile(sample, 0.90)
        if p90 is not None:
            output[key] = 1.5 * p90
    return output


def adaptive_detail_rows(signals: Sequence[SignalState], outcomes: dict, ranges: M1RangeIndex) -> list[dict]:
    thresholds = _excess_thresholds(signals)
    rows: list[dict] = []
    for signal in signals:
        if signal.entry_mid is None or "CURRENT" not in signal.stops:
            continue
        setup = adaptive_setup(signal)
        current_distance = signal.stops["CURRENT"].distance
        for candidate, stop in signal.stops.items():
            lot_multiplier = constant_risk_lot_multiplier(current_distance, stop.distance)
            for horizon, duration in HORIZON_MS.items():
                end = signal.anchor_ms + duration
                hit = stop.hit_ms is not None and stop.hit_ms <= end
                post_stop = _post_stop_favorable(signal, stop, end, ranges) if hit else None
                favorable_before_end = signal.favorable_time_ms is not None and signal.favorable_time_ms <= end
                outcome = outcomes.get(signal.event_id, {}).get(horizon)
                directed = _directed_values(outcome, signal.direction) if outcome else (None, None, None)
                mae_price = abs(float(directed[2])) * signal.reference_close if _is_number(directed[2]) else None
                mfe_price = float(directed[1]) * signal.reference_close if _is_number(directed[1]) else None
                adverse_before = (
                    signal.pre_favorable_mae_price
                    if favorable_before_end and signal.pre_favorable_mae_price is not None
                    else mae_price
                )
                excess_limit = thresholds.get((setup, signal.direction))
                row = {
                    "event_id": signal.event_id,
                    "period": signal.period,
                    "direction": signal.direction,
                    "setup": setup,
                    "candidate": candidate,
                    "horizon": horizon,
                    "stop_distance": stop.distance,
                    "stop_distance_atr": stop.distance_atr,
                    "stop_hit": hit,
                    "stop_survived": not hit,
                    "premature_stop": hit and _is_number(post_stop) and float(post_stop) >= signal.atr,
                    "time_to_stop_minutes": (int(stop.hit_ms) - signal.anchor_ms) / 60_000.0 if hit else None,
                    "mae_atr": mae_price / signal.atr if _is_number(mae_price) else None,
                    "mfe_atr": mfe_price / signal.atr if _is_number(mfe_price) else None,
                    "adverse_before_favorable_atr": adverse_before / signal.atr if _is_number(adverse_before) else None,
                    "potential_mfe_r": mfe_price / stop.distance if _is_number(mfe_price) else None,
                    "position_size_multiplier_vs_current": lot_multiplier,
                    "planned_monetary_risk_ratio_vs_current": planned_risk_ratio(current_distance, stop.distance),
                    "excessively_wide": stop.distance_atr > excess_limit if excess_limit is not None else None,
                    "observer_only": True,
                    "diagnostic_only": True,
                    "score_effect": 0,
                }
                for multiple in R_MULTIPLES:
                    row[f"reached_{multiple:g}r"] = reached_r_before_stop(stop, multiple, end)
                rows.append(row)
    return rows


def summarize(rows: Sequence[dict]) -> dict:
    count = len(rows)
    hits = [row for row in rows if row["stop_hit"]]
    return {
        "cases": count,
        "median_stop_distance_atr": _median(row["stop_distance_atr"] for row in rows),
        "stop_hit_rate": len(hits) / count if count else None,
        "survival_rate": (count - len(hits)) / count if count else None,
        "premature_stop_rate": sum(bool(row["premature_stop"]) for row in rows) / count if count else None,
        "median_mae_atr": _median(row["mae_atr"] for row in rows),
        "median_mfe_atr": _median(row["mfe_atr"] for row in rows),
        "median_potential_mfe_r": _median(row["potential_mfe_r"] for row in rows),
        "median_adverse_before_favorable_atr": _median(row["adverse_before_favorable_atr"] for row in rows),
        "mean_time_to_stop_minutes": _mean(row["time_to_stop_minutes"] for row in hits),
        "excess_distance_rate": (
            sum(row["excessively_wide"] is True for row in rows)
            / sum(row["excessively_wide"] is not None for row in rows)
            if any(row["excessively_wide"] is not None for row in rows)
            else None
        ),
        "median_position_size_multiplier_vs_current": _median(
            row["position_size_multiplier_vs_current"] for row in rows
        ),
        "max_abs_planned_risk_error": max(
            (abs(float(row["planned_monetary_risk_ratio_vs_current"]) - 1.0) for row in rows),
            default=0.0,
        ),
        **{
            f"reached_{multiple:g}r_rate": sum(bool(row[f"reached_{multiple:g}r"]) for row in rows) / count
            if count
            else None
            for multiple in R_MULTIPLES
        },
    }


def _with_baseline_deltas(rows: list[dict], identity_fields: Sequence[str]) -> None:
    lookup = {
        tuple(row[field] for field in identity_fields): row
        for row in rows
        if row["candidate"] == "CURRENT"
    }
    metrics = (
        "stop_hit_rate",
        "survival_rate",
        "premature_stop_rate",
        "median_potential_mfe_r",
        "reached_0.5r_rate",
        "reached_1r_rate",
        "reached_1.5r_rate",
        "reached_2r_rate",
    )
    for row in rows:
        baseline = lookup.get(tuple(row[field] for field in identity_fields))
        for metric in metrics:
            value = row.get(metric)
            base = baseline.get(metric) if baseline else None
            row[f"delta_vs_current_{metric}"] = float(value) - float(base) if _is_number(value) and _is_number(base) else None


def aggregate_global(rows: Sequence[dict]) -> list[dict]:
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for row in rows:
        for period in ("ALL", row["period"]):
            for direction in ("ALL", row["direction"]):
                groups[(period, direction, row["candidate"], row["horizon"])].append(row)
    output = [
        {
            "period": key[0],
            "direction": key[1],
            "candidate": key[2],
            "horizon": key[3],
            **summarize(members),
            "observer_only": True,
            "diagnostic_only": True,
            "score_effect": 0,
        }
        for key, members in sorted(groups.items())
    ]
    _with_baseline_deltas(output, ("period", "direction", "horizon"))
    return output


def aggregate_by_setup(rows: Sequence[dict]) -> list[dict]:
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for row in rows:
        for period in ("ALL", row["period"]):
            groups[(period, row["direction"], row["setup"], row["candidate"], row["horizon"])].append(row)
    output = []
    for key, members in sorted(groups.items()):
        if len(members) < MIN_SEGMENT_CASES:
            continue
        output.append(
            {
                "period": key[0],
                "direction": key[1],
                "setup": key[2],
                "candidate": key[3],
                "horizon": key[4],
                **summarize(members),
                "observer_only": True,
                "diagnostic_only": True,
                "score_effect": 0,
            }
        )
    _with_baseline_deltas(output, ("period", "direction", "setup", "horizon"))
    return output


def assess_variants(rows: Sequence[dict], identity_fields: Sequence[str]) -> dict[tuple, str]:
    indexed = {
        tuple(row[field] for field in identity_fields) + (row["period"], row["candidate"]): row
        for row in rows
        if row["horizon"] == "4h"
    }
    identities = {tuple(row[field] for field in identity_fields) for row in rows if row["horizon"] == "4h"}
    result: dict[tuple, str] = {}
    for identity in identities:
        result[identity + ("CURRENT",)] = "BASELINE_CURRENT"
        for candidate in CANDIDATES[1:]:
            pairs = []
            for period in ("TRAIN", "VALIDATION", "OOS"):
                current = indexed.get(identity + (period, "CURRENT"))
                variant = indexed.get(identity + (period, candidate))
                if current is None or variant is None:
                    continue
                benefit = (
                    variant["survival_rate"] >= current["survival_rate"] + 0.0025
                    or variant["premature_stop_rate"] <= current["premature_stop_rate"] - 0.001
                )
                nonworse = (
                    variant["premature_stop_rate"] <= current["premature_stop_rate"] + 0.0025
                    and variant["reached_1r_rate"] >= current["reached_1r_rate"] - 0.05
                    and variant["median_stop_distance_atr"]
                    <= MAX_DIAGNOSTIC_STOP_ATR + PRICE_GRID_ATR_TOLERANCE
                )
                equivalent = all(
                    abs(float(variant[field]) - float(current[field])) <= 1e-12
                    for field in ("survival_rate", "premature_stop_rate", "reached_1r_rate", "median_stop_distance_atr")
                )
                pairs.append((period, benefit, nonworse, equivalent))
            by_period = {period: (benefit, nonworse, equivalent) for period, benefit, nonworse, equivalent in pairs}
            benefit_count = sum(item[1] for item in pairs)
            validated_benefit = (
                by_period.get("VALIDATION", (False, False, False))[0]
                or by_period.get("OOS", (False, False, False))[0]
            )
            if len(pairs) < 3:
                assessment = "PROMISING"
            elif all(item[2] for item in pairs) and benefit_count >= 2 and validated_benefit:
                assessment = "ROBUST"
            elif by_period["VALIDATION"][1] and by_period["OOS"][1] and (
                by_period["VALIDATION"][0] or by_period["OOS"][0]
            ):
                assessment = "PROMISING"
            elif by_period["TRAIN"][0] and (not by_period["VALIDATION"][1] or not by_period["OOS"][1]):
                assessment = "UNSTABLE"
            elif all(item[2] for item in pairs):
                assessment = "PROMISING"
            else:
                assessment = "WORSE"
            result[identity + (candidate,)] = assessment
    return result


def _attach_assessments(rows: list[dict], identity_fields: Sequence[str]) -> None:
    assessments = assess_variants(rows, identity_fields)
    for row in rows:
        row["robustness"] = assessments.get(
            tuple(row[field] for field in identity_fields) + (row["candidate"],), "PROMISING"
        )


def _csv_text(rows: Sequence[dict]) -> str:
    if not rows:
        return "period,direction,candidate,horizon\n"
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def _load_aggregate_csv(path: Path) -> list[dict]:
    string_fields = {"period", "direction", "setup", "candidate", "horizon", "robustness"}
    bool_fields = {"observer_only", "diagnostic_only"}
    rows: list[dict] = []
    with Path(path).open("r", encoding="utf-8-sig", newline="") as stream:
        for source in csv.DictReader(stream):
            row: dict = {}
            for key, value in source.items():
                if key in string_fields:
                    row[key] = value
                elif key in bool_fields:
                    row[key] = value.lower() == "true"
                elif value == "":
                    row[key] = None
                elif key in {"cases", "score_effect"}:
                    row[key] = int(value)
                else:
                    row[key] = float(value)
            rows.append(row)
    return rows


def _pct(value: float | None) -> str:
    return f"{100.0 * value:.2f}%" if _is_number(value) else "N/A"


def _markdown(summary: dict, global_rows: Sequence[dict], setup_rows: Sequence[dict]) -> str:
    lines = [
        "# Adaptive Stop v2 — comparación histórica",
        "",
        "> Solo diagnóstico · score_effect=0 · RiskPercent no modificado.",
        "",
        "## Baseline CURRENT y comparación global 4h",
        "",
        "| Candidato | Distancia ATR | Supervivencia | Prematuro | +0.5R | +1R | +1.5R | +2R | Robustez |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for row in global_rows:
        if row["period"] == "ALL" and row["direction"] == "ALL" and row["horizon"] == "4h":
            lines.append(
                f"| {row['candidate']} | {row['median_stop_distance_atr']:.3f} | {_pct(row['survival_rate'])} | "
                f"{_pct(row['premature_stop_rate'])} | {_pct(row['reached_0.5r_rate'])} | "
                f"{_pct(row['reached_1r_rate'])} | {_pct(row['reached_1.5r_rate'])} | "
                f"{_pct(row['reached_2r_rate'])} | {row['robustness']} |"
            )
    lines.extend(["", "## LONG / SHORT", "", "| Dirección | Candidato | Supervivencia | Prematuro | +1R | Robustez |", "|---|---|---:|---:|---:|---|"])
    for row in global_rows:
        if row["period"] == "ALL" and row["direction"] in {"LONG", "SHORT"} and row["horizon"] == "4h":
            lines.append(
                f"| {row['direction']} | {row['candidate']} | {_pct(row['survival_rate'])} | "
                f"{_pct(row['premature_stop_rate'])} | {_pct(row['reached_1r_rate'])} | {row['robustness']} |"
            )
    lines.extend(["", "## Comparación por setup 4h", "", "| Dirección | Setup | Candidato | Casos | Distancia ATR | Supervivencia | Prematuro | +1R | Robustez |", "|---|---|---|---:|---:|---:|---:|---:|---|"])
    for row in setup_rows:
        if row["period"] == "ALL" and row["horizon"] == "4h":
            lines.append(
                f"| {row['direction']} | {row['setup']} | {row['candidate']} | {row['cases']} | "
                f"{row['median_stop_distance_atr']:.3f} | {_pct(row['survival_rate'])} | "
                f"{_pct(row['premature_stop_rate'])} | {_pct(row['reached_1r_rate'])} | {row['robustness']} |"
            )
    lines.extend(["", "## TRAIN / VALIDATION / OOS", "", "| Periodo | Candidato | Supervivencia | Prematuro | +1R |", "|---|---|---:|---:|---:|"])
    for row in global_rows:
        if row["period"] != "ALL" and row["direction"] == "ALL" and row["horizon"] == "4h":
            lines.append(
                f"| {row['period']} | {row['candidate']} | {_pct(row['survival_rate'])} | "
                f"{_pct(row['premature_stop_rate'])} | {_pct(row['reached_1r_rate'])} |"
            )
    lines.extend(
        [
            "",
            "## Conclusión",
            "",
            *[f"- {candidate}: {assessment}." for candidate, assessment in summary["global_assessment"].items()],
            "- La clasificación exige evidencia fuera de TRAIN y tolera como máximo 5 puntos porcentuales de deterioro en +1R.",
            "- Ningún resultado cambia el stop live; la normalización real de lotaje y margen permanece a cargo del contrato del broker.",
            "",
        ]
    )
    return "\n".join(lines)


def analyze_adaptive_stop_v2(input_dir: Path, tick_files: Sequence[Path], output_dir: Path) -> dict:
    input_dir, output_dir = Path(input_dir), Path(output_dir)
    signals, split_counts, enrichment = prepare_signals(input_dir)
    outcomes, _, _ = _load_outcomes(input_dir / "historical_outcomes.jsonl")
    stats = IngestionStats()
    tick_report = simulate_adaptive_tick_order(signals, iter_ticks(tick_files, stats))
    ranges = M1RangeIndex(_load_bars(input_dir, "XAUUSD", "M1"))
    finalize_tick_metrics(signals, ranges)
    details = adaptive_detail_rows(signals, outcomes, ranges)
    global_rows = aggregate_global(details)
    setup_rows = aggregate_by_setup(details)
    _attach_assessments(global_rows, ("direction",))
    _attach_assessments(setup_rows, ("direction", "setup"))
    global_assessment = {
        row["candidate"]: row["robustness"]
        for row in global_rows
        if row["period"] == "ALL" and row["direction"] == "ALL" and row["horizon"] == "4h"
    }
    summary = {
        "analysis_only": True,
        "diagnostic_only": True,
        "observer_only": True,
        "score_effect": 0,
        "risk_percent_changed": False,
        "risk_policy": "constant monetary risk via CURRENT_distance / candidate_distance",
        "split_counts": split_counts,
        "decision_enrichment": enrichment,
        "signals_reproducible": len(signals),
        "tick_scan": {
            **tick_report,
            "ticks_valid": stats.ticks_valid,
            "ticks_discarded": stats.ticks_discarded,
            "duplicates": stats.duplicates,
        },
        "setup_priority": "BREAKOUT > PULLBACK > MOMENTUM > RECOVERY > CONTINUATION_DIRECTION",
        "global_assessment": global_assessment,
        "robustness_policy": {
            "ROBUST": "non-worse premature/+1R/distance criteria in TRAIN, VALIDATION and OOS",
            "PROMISING": "non-worse OOS and improvement in VALIDATION or OOS, or limited sample",
            "UNSTABLE": "TRAIN benefit with out-of-sample failure",
            "WORSE": "clear out-of-sample deterioration or no validated benefit",
        },
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write(output_dir / "adaptive_stop_v2.csv", _csv_text(global_rows))
    _atomic_write(output_dir / "adaptive_stop_by_setup.csv", _csv_text(setup_rows))
    _atomic_write(output_dir / "adaptive_stop_v2.md", _markdown(summary, global_rows, setup_rows))
    _atomic_write(output_dir / "adaptive_stop_v2.json", json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    return summary


def reassess_existing_outputs(output_dir: Path) -> dict:
    """Recompute labels/markdown without changing completed tick metrics."""
    output_dir = Path(output_dir)
    global_rows = _load_aggregate_csv(output_dir / "adaptive_stop_v2.csv")
    setup_rows = _load_aggregate_csv(output_dir / "adaptive_stop_by_setup.csv")
    summary = json.loads((output_dir / "adaptive_stop_v2.json").read_text(encoding="utf-8-sig"))
    _attach_assessments(global_rows, ("direction",))
    _attach_assessments(setup_rows, ("direction", "setup"))
    summary["global_assessment"] = {
        row["candidate"]: row["robustness"]
        for row in global_rows
        if row["period"] == "ALL" and row["direction"] == "ALL" and row["horizon"] == "4h"
    }
    _atomic_write(output_dir / "adaptive_stop_v2.csv", _csv_text(global_rows))
    _atomic_write(output_dir / "adaptive_stop_by_setup.csv", _csv_text(setup_rows))
    _atomic_write(output_dir / "adaptive_stop_v2.md", _markdown(summary, global_rows, setup_rows))
    _atomic_write(output_dir / "adaptive_stop_v2.json", json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    return summary


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    root = Path(__file__).resolve().parent
    parser.add_argument("--input-dir", type=Path, default=root / "output")
    parser.add_argument("--tick-input-dir", type=Path)
    parser.add_argument("--output-dir", type=Path, default=root / "analysis")
    parser.add_argument("--reassess-only", action="store_true")
    arguments = parser.parse_args(argv)
    if arguments.reassess_only:
        summary = reassess_existing_outputs(arguments.output_dir)
        print(f"[ADAPTIVE_STOP] reassessed={summary['global_assessment']}")
        return 0
    if arguments.tick_input_dir is None:
        parser.error("--tick-input-dir is required unless --reassess-only is used")
    discovery = discover_inputs(input_dir=arguments.tick_input_dir, symbol="XAUUSD")
    if not discovery.files:
        parser.error("no valid XAUUSD tick CSV files found")
    summary = analyze_adaptive_stop_v2(
        arguments.input_dir,
        [item.path for item in discovery.files],
        arguments.output_dir,
    )
    print(f"[ADAPTIVE_STOP] signals={summary['signals_reproducible']}")
    print(f"[ADAPTIVE_STOP] score_effect={summary['score_effect']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
