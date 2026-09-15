"""Structure-Aware Take Profit v2 research/PAPER evaluation.

V2 starts from the previously supported fixed target (0.75R for CHILL and
1.25R for GOD), then shortens it before the first HIGH/MEDIUM-confidence
closed-bar obstacle.  LOW_REWARD can either be accepted or rejected for the
diagnostic comparison.  Live trading is neither imported nor modified.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import csv
from dataclasses import dataclass, field
import io
import math
from pathlib import Path
import statistics
from typing import Sequence

from research.analyze_adaptive_stop_v2 import adaptive_setup
from research.analyze_historical_dataset import _atomic_write, _directed_values, _load_outcomes
from research.analyze_stop_loss_quality import HORIZON_MS, M1RangeIndex, StopState, _is_number, _mean, _median, prepare_signals
from research.analyze_structure_aware_tp import (
    CausalStructureIndex,
    StructuralContext,
    StructuralZone,
    StructureAwareSignal,
)
from research.analyze_take_profit_and_account_size import (
    DEFAULT_TICK_SIZE,
    TPSignal,
    TargetState,
    _align_price,
    _annotate_trade_classes,
    _load_bars,
    _number,
    _pct,
    _post_tp_favorable,
    build_target_candidates,
    simulate_tp_tick_order,
)
from research.tick_historical_replay import IngestionStats, discover_inputs, iter_ticks


SOURCE = "HISTORICAL_MT5_TICKS"
V2_BASELINE_R = {"CHILL": 0.75, "GOD": 1.25}
STRUCTURE_BUFFERS_ATR = (0.10, 0.15, 0.20)
REWARD_FLOORS_R = (0.50, 0.60, 0.70)
LOW_REWARD_ACTIONS = ("ACCEPT", "REJECT")
CONFIDENCE_ORDER = {"LOW": 1, "MEDIUM": 2, "HIGH": 3}
MIN_SELECTION_ACCEPTANCE = 0.50
MIN_SETUP_SAMPLE = 30


@dataclass(frozen=True)
class V2TargetMeta:
    candidate: str
    buffer_atr: float | None
    reward_floor_r: float | None
    low_reward_action: str | None
    obstacle_price: float | None
    obstacle_confidence: str | None
    obstacle_sources: tuple[str, ...]
    shortened_by_structure: bool
    low_reward: bool
    accepted_below_0_75r: bool
    rejected: bool
    rejection_reason: str | None
    realized_target_r: float | None


@dataclass
class StructureAwareV2Signal(StructureAwareSignal):
    v2_meta: dict[str, V2TargetMeta] = field(default_factory=dict)


def v2_candidate_name(buffer_atr: float, floor_r: float, action: str) -> str:
    buffer = int(round(buffer_atr * 100))
    floor = int(round(floor_r * 100))
    return f"STRUCTURE_AWARE_V2_B{buffer:02d}_F{floor:02d}_{action}"


def v2_candidate_names() -> tuple[str, ...]:
    return tuple(
        v2_candidate_name(buffer_atr, floor_r, action)
        for buffer_atr in STRUCTURE_BUFFERS_ATR
        for floor_r in REWARD_FLOORS_R
        for action in LOW_REWARD_ACTIONS
    )


def _candidate_floor(candidate: str) -> float:
    try:
        return int(candidate.split("_F", 1)[1].split("_", 1)[0]) / 100.0
    except (IndexError, ValueError):
        return math.inf


def classify_zone_confidence(zone: StructuralZone) -> str:
    """Translate causal evidence into the v2 HIGH/MEDIUM/LOW contract."""
    sources = set(zone.sources)
    has_h1_pivot = any("CONFIRMED_H1_PIVOT" in source for source in sources)
    has_h4_pivot = any("CONFIRMED_H4_PIVOT" in source for source in sources)
    has_m15 = any("M15" in source for source in sources)
    has_equal = "EQUAL_HIGH" in sources or "EQUAL_LOW" in sources
    if has_h4_pivot or has_equal or zone.touches >= 2 or (has_h1_pivot and has_m15):
        return "HIGH"
    if has_h1_pivot or any("CONSOLIDATION" in source or "RECENT_H1" in source or "BREAKOUT" in source for source in sources):
        return "MEDIUM"
    return "LOW"


def first_relevant_obstacle(
    zones: Sequence[StructuralZone], direction: str, entry_price: float, baseline_target: float
) -> tuple[StructuralZone, str] | None:
    """Find the nearest HIGH/MEDIUM obstacle strictly between entry and TP."""
    if direction not in {"LONG", "SHORT"}:
        return None
    kind = "HIGH" if direction == "LONG" else "LOW"
    candidates = []
    for zone in zones:
        if zone.kind != kind:
            continue
        inside = entry_price < zone.price <= baseline_target if direction == "LONG" else baseline_target <= zone.price < entry_price
        confidence = classify_zone_confidence(zone)
        if inside and CONFIDENCE_ORDER[confidence] >= CONFIDENCE_ORDER["MEDIUM"]:
            distance = abs(zone.price - entry_price)
            candidates.append((distance, zone.price, zone, confidence))
    if not candidates:
        return None
    _, _, zone, confidence = min(candidates, key=lambda item: (item[0], item[1]))
    return zone, confidence


def buffered_target(
    *,
    obstacle_price: float,
    direction: str,
    atr: float,
    buffer_atr: float,
    tick_size: float = DEFAULT_TICK_SIZE,
) -> float:
    if atr <= 0.0 or buffer_atr <= 0.0:
        raise ValueError("ATR and structural buffer must be positive")
    raw = obstacle_price - atr * buffer_atr if direction == "LONG" else obstacle_price + atr * buffer_atr
    return _align_price(raw, tick_size, up=direction == "SHORT")


def annotate_v2_context(signals: Sequence[TPSignal], input_dir: Path) -> list[StructureAwareV2Signal]:
    index = CausalStructureIndex(input_dir)
    output = []
    for item in signals:
        output.append(
            StructureAwareV2Signal(
                item.base,
                item.trade_class,
                item.current_target_r,
                context=index.context(item.base),
            )
        )
    return output


def build_v2_targets(
    item: TPSignal,
    entry_price: float,
    stop: StopState,
    *,
    tick_size: float = DEFAULT_TICK_SIZE,
) -> tuple[float, float, dict[str, TargetState]]:
    if not isinstance(item, StructureAwareV2Signal):
        raise TypeError("StructureAwareV2Signal required")
    worst_fill, planned_risk, fixed = build_target_candidates(
        trade_class=item.trade_class,
        current_target_r=item.current_target_r,
        direction=item.base.direction,
        entry_price=entry_price,
        stop_price=stop.price,
        tick_size=tick_size,
    )
    baseline_name = "R_0_75" if item.trade_class == "CHILL" else "R_1_25"
    baseline = fixed[baseline_name]
    targets = {"CURRENT": fixed["CURRENT"], baseline_name: baseline}
    item.v2_meta.clear()
    context = item.context
    obstacle = first_relevant_obstacle(context.zones, item.base.direction, entry_price, baseline.price) if context else None

    for buffer_atr in STRUCTURE_BUFFERS_ATR:
        if obstacle is None:
            target_price = baseline.price
            confidence = None
            obstacle_zone = None
            shortened = False
        else:
            obstacle_zone, confidence = obstacle
            target_price = buffered_target(
                obstacle_price=obstacle_zone.price,
                direction=item.base.direction,
                atr=item.base.atr,
                buffer_atr=buffer_atr,
                tick_size=tick_size,
            )
            shortened = abs(target_price - entry_price) + 1e-9 < abs(baseline.price - entry_price)
        distance = target_price - entry_price if item.base.direction == "LONG" else entry_price - target_price
        realized_target_r = distance / planned_risk if distance > 0.0 else None
        for floor_r in REWARD_FLOORS_R:
            low_reward = realized_target_r is None or realized_target_r + 1e-9 < floor_r
            for action in LOW_REWARD_ACTIONS:
                name = v2_candidate_name(buffer_atr, floor_r, action)
                rejected = realized_target_r is None or (low_reward and action == "REJECT")
                reason = "INVALID_STRUCTURAL_TARGET" if realized_target_r is None else "LOW_REWARD" if rejected else None
                meta = V2TargetMeta(
                    candidate=name,
                    buffer_atr=buffer_atr,
                    reward_floor_r=floor_r,
                    low_reward_action=action,
                    obstacle_price=obstacle_zone.price if obstacle_zone else None,
                    obstacle_confidence=confidence,
                    obstacle_sources=obstacle_zone.sources if obstacle_zone else (),
                    shortened_by_structure=shortened,
                    low_reward=low_reward,
                    accepted_below_0_75r=not rejected and realized_target_r is not None and realized_target_r < 0.75 - 1e-9,
                    rejected=rejected,
                    rejection_reason=reason,
                    realized_target_r=realized_target_r,
                )
                item.v2_meta[name] = meta
                if not rejected:
                    targets[name] = TargetState(name, float(realized_target_r), target_price)
    return worst_fill, planned_risk, targets


def _base_row(item: StructureAwareV2Signal, candidate: str, horizon: str, meta: V2TargetMeta | None) -> dict:
    signal = item.base
    return {
        "event_id": signal.event_id,
        "timestamp": signal.anchor_ms,
        "period": signal.period,
        "trade_class": item.trade_class,
        "direction": signal.direction,
        "setup": adaptive_setup(signal),
        "candidate": candidate,
        "horizon": horizon,
        "buffer_atr": meta.buffer_atr if meta else None,
        "reward_floor_r": meta.reward_floor_r if meta else None,
        "low_reward_action": meta.low_reward_action if meta else None,
        "obstacle_price": meta.obstacle_price if meta else None,
        "obstacle_confidence": meta.obstacle_confidence if meta else None,
        "shortened_by_structure": meta.shortened_by_structure if meta else False,
        "low_reward": meta.low_reward if meta else False,
        "accepted_below_0_75r": meta.accepted_below_0_75r if meta else False,
        "rejected": meta.rejected if meta else False,
        "rejection_reason": meta.rejection_reason if meta else None,
        "observer_only": True,
        "diagnostic_only": True,
        "score_effect": 0,
    }


def v2_detail_rows(signals: Sequence[StructureAwareV2Signal], outcomes: dict, ranges: M1RangeIndex) -> list[dict]:
    rows = []
    for item in signals:
        signal, stop = item.base, item.stop
        if signal.entry_mid is None or item.entry_price is None or stop is None or item.planned_risk_distance is None:
            continue
        planned = item.planned_risk_distance
        stop_loss_r = float(item.actual_stop_distance) / planned
        baseline_name = "R_0_75" if item.trade_class == "CHILL" else "R_1_25"
        for candidate in ("CURRENT", baseline_name, *v2_candidate_names()):
            target = item.targets.get(candidate)
            meta = item.v2_meta.get(candidate)
            for horizon, duration in HORIZON_MS.items():
                row = _base_row(item, candidate, horizon, meta)
                if target is None:
                    row.update(
                        {
                            "target_r": meta.realized_target_r if meta else None,
                            "eligible": False,
                            "tp_hit": False,
                            "sl_hit": False,
                            "tp_first": False,
                            "sl_first": False,
                            "unresolved": False,
                            "time_to_tp_minutes": None,
                            "time_to_sl_minutes": None,
                            "hypothetical_realized_r": 0.0,
                            "mfe_r": None,
                            "mae_r": None,
                            "mfe_after_exit_r": None,
                        }
                    )
                    rows.append(row)
                    continue
                end = signal.anchor_ms + duration
                tp_in = target.hit_ms is not None and target.hit_ms <= end
                sl_in = stop.hit_ms is not None and stop.hit_ms <= end
                tp_first = tp_in and (not sl_in or int(target.hit_ms) < int(stop.hit_ms))
                sl_first = sl_in and (not tp_in or int(stop.hit_ms) <= int(target.hit_ms))
                outcome = outcomes.get(signal.event_id, {}).get(horizon)
                _, mfe_return, mae_return = _directed_values(outcome, signal.direction) if outcome else (None, None, None)
                mfe_price = float(mfe_return) * signal.reference_close if _is_number(mfe_return) else None
                mae_price = abs(float(mae_return)) * signal.reference_close if _is_number(mae_return) else None
                post_exit = _post_tp_favorable(item, target, end, ranges) if tp_first else None
                target_distance = abs(target.price - item.entry_price)
                if _is_number(post_exit):
                    mfe_after_exit = max(0.0, float(post_exit) - target_distance) / planned
                elif sl_first and stop.hit_ms is not None and stop.hit_mid is not None:
                    next_minute = stop.hit_ms // 60_000 * 60_000 + 60_000
                    maximum, minimum = ranges.extrema(next_minute, end)
                    if signal.direction == "LONG" and _is_number(maximum):
                        mfe_after_exit = max(0.0, float(maximum) - float(stop.hit_mid)) / planned
                    elif signal.direction == "SHORT" and _is_number(minimum):
                        mfe_after_exit = max(0.0, float(stop.hit_mid) - float(minimum)) / planned
                    else:
                        mfe_after_exit = None
                else:
                    mfe_after_exit = None
                realized_r = target.r_multiple if tp_first else -stop_loss_r if sl_first else None
                row.update(
                    {
                        "target_r": target.r_multiple,
                        "eligible": True,
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
                        "mfe_after_exit_r": mfe_after_exit,
                    }
                )
                rows.append(row)
    return rows


def _drawdown_proxy(rows: Sequence[dict]) -> float:
    cumulative = peak = maximum_drawdown = 0.0
    for row in sorted(rows, key=lambda item: (int(item["timestamp"]), item["event_id"])):
        value = row.get("hypothetical_realized_r")
        if row.get("eligible") and _is_number(value):
            cumulative += float(value)
            peak = max(peak, cumulative)
            maximum_drawdown = max(maximum_drawdown, peak - cumulative)
    return maximum_drawdown


def summarize_v2(rows: Sequence[dict]) -> dict:
    total = len(rows)
    eligible = [row for row in rows if row["eligible"]]
    resolved = [row for row in eligible if _is_number(row.get("hypothetical_realized_r"))]
    realized = [float(row["hypothetical_realized_r"]) for row in resolved]
    wins = [value for value in realized if value > 0.0]
    losses = [value for value in realized if value < 0.0]
    gross_profit, gross_loss = sum(wins), abs(sum(losses))
    return {
        "cases": total,
        "eligible_cases": len(eligible),
        "resolved_cases": len(resolved),
        "acceptance_rate": len(eligible) / total if total else None,
        "rejection_rate": (total - len(eligible)) / total if total else None,
        "structural_tp_rate": sum(bool(row["eligible"] and row["shortened_by_structure"]) for row in rows) / total if total else None,
        "low_reward_rate": sum(bool(row["low_reward"]) for row in rows) / total if total else None,
        "accepted_below_0_75r_rate": sum(bool(row["accepted_below_0_75r"]) for row in rows) / total if total else None,
        "tp_before_sl_rate": sum(bool(row["tp_first"]) for row in eligible) / len(eligible) if eligible else None,
        "sl_before_tp_rate": sum(bool(row["sl_first"]) for row in eligible) / len(eligible) if eligible else None,
        "win_rate_resolved": len(wins) / len(resolved) if resolved else None,
        "expectancy_r_resolved": _mean(realized),
        "expectancy_r_all_cases": sum(realized) / total if total else None,
        "median_realized_r": _median(realized),
        "profit_factor_resolved": gross_profit / gross_loss if gross_loss > 0.0 else None,
        "realized_r_stddev": statistics.pstdev(realized) if len(realized) >= 2 else None,
        "drawdown_proxy_r": _drawdown_proxy(rows),
        "mean_time_to_tp_minutes": _mean(row["time_to_tp_minutes"] for row in eligible),
        "median_time_to_tp_minutes": _median(row["time_to_tp_minutes"] for row in eligible),
        "mean_time_to_sl_minutes": _mean(row["time_to_sl_minutes"] for row in eligible),
        "median_time_to_sl_minutes": _median(row["time_to_sl_minutes"] for row in eligible),
        "median_mfe_after_exit_r": _median(row["mfe_after_exit_r"] for row in eligible),
    }


def aggregate_v2(rows: Sequence[dict]) -> list[dict]:
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for row in rows:
        for period in ("ALL", row["period"]):
            groups[("GLOBAL", period, row["trade_class"], "ALL", "ALL", row["candidate"], row["horizon"])].append(row)
            groups[("DIRECTION", period, row["trade_class"], row["direction"], "ALL", row["candidate"], row["horizon"])].append(row)
            groups[("SETUP", period, row["trade_class"], row["direction"], row["setup"], row["candidate"], row["horizon"])].append(row)
    return [
        {
            "scope": key[0],
            "period": key[1],
            "trade_class": key[2],
            "direction": key[3],
            "setup": key[4],
            "candidate": key[5],
            "horizon": key[6],
            **summarize_v2(members),
            "observer_only": True,
            "diagnostic_only": True,
            "score_effect": 0,
        }
        for key, members in sorted(groups.items())
    ]


def _index_global_4h(rows: Sequence[dict], trade_class: str) -> dict[tuple[str, str], dict]:
    return {
        (row["period"], row["candidate"]): row
        for row in rows
        if row["scope"] == "GLOBAL" and row["trade_class"] == trade_class and row["horizon"] == "4h"
    }


def _setup_stability(rows: Sequence[dict], trade_class: str, candidate: str) -> float:
    samples = [
        row for row in rows
        if row["scope"] == "SETUP"
        and row["period"] == "OOS"
        and row["trade_class"] == trade_class
        and row["candidate"] == candidate
        and row["horizon"] == "4h"
        and int(row["cases"]) >= MIN_SETUP_SAMPLE
        and _is_number(row.get("expectancy_r_resolved"))
        and _is_number(row.get("profit_factor_resolved"))
    ]
    if not samples:
        return 0.0
    return sum(float(row["expectancy_r_resolved"]) > 0.0 and float(row["profit_factor_resolved"]) > 1.0 for row in samples) / len(samples)


def select_v2_policy(rows: Sequence[dict], trade_class: str) -> tuple[str, str]:
    """Rank v2 using OOS profitability first, then stability and variance."""
    indexed = _index_global_4h(rows, trade_class)
    ranked = []
    for candidate in v2_candidate_names():
        periods = [indexed.get((period, candidate)) for period in ("TRAIN", "VALIDATION", "OOS")]
        if any(row is None for row in periods):
            continue
        train, validation, oos = periods
        if any(not _is_number(row.get("expectancy_r_resolved")) or not _is_number(row.get("profit_factor_resolved")) for row in periods):
            continue
        acceptance = min(float(validation["acceptance_rate"]), float(oos["acceptance_rate"]))
        robust = (
            float(oos["expectancy_r_resolved"]) > 0.0
            and float(oos["profit_factor_resolved"]) > 1.0
            and float(validation["expectancy_r_resolved"]) >= 0.0
            and float(validation["profit_factor_resolved"]) >= 1.0
            and float(train["expectancy_r_resolved"]) >= 0.0
            and acceptance >= MIN_SELECTION_ACCEPTANCE
        )
        stddev = float(oos["realized_r_stddev"]) if _is_number(oos.get("realized_r_stddev")) else math.inf
        sl_rate = float(oos["sl_before_tp_rate"]) if _is_number(oos.get("sl_before_tp_rate")) else 1.0
        score = (
            int(robust),
            float(oos["expectancy_r_resolved"]),
            float(validation["expectancy_r_resolved"]),
            _setup_stability(rows, trade_class, candidate),
            -stddev,
            -sl_rate,
            acceptance,
            -_candidate_floor(candidate),
            candidate,
        )
        ranked.append((score, candidate))
    if not ranked:
        raise ValueError(f"no evaluable v2 policy for {trade_class}")
    score, candidate = max(ranked)
    status = "ROBUST" if score[0] else "NO_ROBUST_V2"
    return candidate, status


def select_best_overall(rows: Sequence[dict], trade_class: str, selected_v2: str) -> tuple[str, str]:
    indexed = _index_global_4h(rows, trade_class)
    fixed = "R_0_75" if trade_class == "CHILL" else "R_1_25"
    candidates = ("CURRENT", fixed, selected_v2)
    valid = []
    for candidate in candidates:
        train, validation, oos = (indexed.get((period, candidate)) for period in ("TRAIN", "VALIDATION", "OOS"))
        if not train or not validation or not oos:
            continue
        acceptance = min(float(validation["acceptance_rate"]), float(oos["acceptance_rate"]))
        if acceptance < MIN_SELECTION_ACCEPTANCE:
            continue
        oos_expectancy = oos.get("expectancy_r_resolved")
        oos_pf = oos.get("profit_factor_resolved")
        validation_expectancy = validation.get("expectancy_r_resolved")
        if not all(_is_number(value) for value in (oos_expectancy, oos_pf, validation_expectancy)):
            continue
        robust = float(oos_expectancy) > 0.0 and float(oos_pf) > 1.0 and float(validation_expectancy) >= 0.0
        stddev = float(oos["realized_r_stddev"]) if _is_number(oos.get("realized_r_stddev")) else math.inf
        valid.append(((int(robust), float(oos_expectancy), float(validation_expectancy), _setup_stability(rows, trade_class, candidate), -stddev), candidate))
    if not valid:
        return "CURRENT", "sin política evaluable"
    score, candidate = max(valid)
    return candidate, "cumple prioridad OOS/PF/consistencia" if score[0] else "mejor resultado relativo; no cumple aceptación PAPER"


def _candidate_label(trade_class: str, candidate: str) -> str:
    if candidate == "CURRENT":
        return "CURRENT 1.25R" if trade_class == "CHILL" else "CURRENT 2.0R"
    if candidate == "R_0_75":
        return "FIXED 0.75R"
    if candidate == "R_1_25":
        return "FIXED 1.25R"
    return candidate.replace("STRUCTURE_AWARE_V2_", "V2 ")


def v2_examples(
    signals: Sequence[StructureAwareV2Signal], selected: dict[str, str], limit_per_result: int = 100
) -> list[dict]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for item in signals:
        candidate = selected[item.trade_class]
        meta = item.v2_meta.get(candidate)
        v2_target = item.targets.get(candidate)
        baseline_name = "R_0_75" if item.trade_class == "CHILL" else "R_1_25"
        baseline = item.targets.get(baseline_name)
        stop = item.stop
        if not meta or not meta.shortened_by_structure or item.entry_price is None or baseline is None or stop is None:
            continue
        baseline_tp_first = baseline.hit_ms is not None and (stop.hit_ms is None or baseline.hit_ms < stop.hit_ms)
        v2_tp_first = v2_target is not None and v2_target.hit_ms is not None and (stop.hit_ms is None or v2_target.hit_ms < stop.hit_ms)
        if v2_tp_first and not baseline_tp_first:
            comparison = "BETTER_AVOIDED_SL_OR_CENSOR"
        elif v2_tp_first and baseline_tp_first:
            comparison = "EARLIER_LOWER_REWARD"
        elif meta.rejected:
            comparison = "REJECTED_LOW_REWARD"
        else:
            comparison = "NO_IMPROVEMENT"
        grouped[comparison].append(
            {
                "event_id": item.base.event_id,
                "timestamp": item.base.anchor_ms,
                "period": item.base.period,
                "trade_class": item.trade_class,
                "direction": item.base.direction,
                "setup": adaptive_setup(item.base),
                "entry_price": item.entry_price,
                "stop_price": stop.price,
                "baseline_candidate": baseline_name,
                "baseline_tp": baseline.price,
                "obstacle_price": meta.obstacle_price,
                "obstacle_confidence": meta.obstacle_confidence,
                "obstacle_sources": "+".join(meta.obstacle_sources),
                "v2_candidate": candidate,
                "v2_tp": v2_target.price if v2_target else None,
                "v2_target_r": meta.realized_target_r,
                "low_reward": meta.low_reward,
                "rejected": meta.rejected,
                "comparison": comparison,
                "baseline_tp_before_sl": baseline_tp_first,
                "v2_tp_before_sl": v2_tp_first,
                "observer_only": True,
                "diagnostic_only": True,
                "score_effect": 0,
            }
        )
    output = []
    for comparison in sorted(grouped):
        output.extend(grouped[comparison][:limit_per_result])
    return output


def _csv_text(rows: Sequence[dict]) -> str:
    if not rows:
        return "\n"
    fields = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def _markdown(rows: Sequence[dict], summary: dict) -> str:
    selected = summary["selected_v2"]
    compared = {"CHILL": ("CURRENT", "R_0_75", selected["CHILL"]), "GOD": ("CURRENT", "R_1_25", selected["GOD"])}
    lines = [
        "# Structure-Aware Take Profit v2",
        "",
        "> Research/PAPER only · source=HISTORICAL_MT5_TICKS · observer_only=true · score_effect=0.",
        "",
        "V2 parte de 0,75R (CHILL) o 1,25R (GOD). Solo recorta el TP ante el primer obstáculo HIGH/MEDIUM entre entrada y baseline; LOW se ignora. El objetivo queda antes de la zona según el buffer ATR. `LOW_REWARD` se evaluó tanto aceptándolo como rechazándolo, sin mover el objetivo más lejos.",
        "",
        f"Barrido: {summary['tick_scan']['ticks_seen']:,} ticks; {summary['signals_reproducible']:,} setups; {summary['signals_with_entry_tick']:,} con entrada.",
        "",
        "## Comparación global y temporal — 4h",
        "",
        "| Clase | Periodo | Política | n | Acepta | TP recortado | Low reward | Acepta <0,75R | TP→SL | SL→TP | Exp R | PF | Mediana R | σ R | DD proxy R |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        if row["scope"] == "GLOBAL" and row["horizon"] == "4h" and row["candidate"] in compared[row["trade_class"]]:
            lines.append(
                f"| {row['trade_class']} | {row['period']} | {_candidate_label(row['trade_class'], row['candidate'])} | {row['cases']} | "
                f"{_pct(row['acceptance_rate'])} | {_pct(row['structural_tp_rate'])} | {_pct(row['low_reward_rate'])} | "
                f"{_pct(row['accepted_below_0_75r_rate'])} | {_pct(row['tp_before_sl_rate'])} | {_pct(row['sl_before_tp_rate'])} | "
                f"{_number(row['expectancy_r_resolved'])} | {_number(row['profit_factor_resolved'])} | {_number(row['median_realized_r'])} | "
                f"{_number(row['realized_r_stddev'])} | {_number(row['drawdown_proxy_r'])} |"
            )
    lines.extend(["", "## LONG / SHORT — OOS, 4h", "", "| Clase | Dirección | Política | n | Acepta | TP recortado | TP→SL | SL→TP | Exp R | PF |", "|---|---|---|---:|---:|---:|---:|---:|---:|---:|"])
    for row in rows:
        if row["scope"] == "DIRECTION" and row["period"] == "OOS" and row["horizon"] == "4h" and row["candidate"] in compared[row["trade_class"]]:
            lines.append(
                f"| {row['trade_class']} | {row['direction']} | {_candidate_label(row['trade_class'], row['candidate'])} | {row['cases']} | "
                f"{_pct(row['acceptance_rate'])} | {_pct(row['structural_tp_rate'])} | {_pct(row['tp_before_sl_rate'])} | "
                f"{_pct(row['sl_before_tp_rate'])} | {_number(row['expectancy_r_resolved'])} | {_number(row['profit_factor_resolved'])} |"
            )
    lines.extend(["", "## Por setup — OOS, 4h (n ≥ 30)", "", "| Clase | Dirección | Setup | Política | n | Acepta | TP recortado | Exp R | PF |", "|---|---|---|---|---:|---:|---:|---:|---:|"])
    for row in rows:
        if row["scope"] == "SETUP" and row["period"] == "OOS" and row["horizon"] == "4h" and row["candidate"] in compared[row["trade_class"]] and int(row["cases"]) >= MIN_SETUP_SAMPLE:
            lines.append(
                f"| {row['trade_class']} | {row['direction']} | {row['setup']} | {_candidate_label(row['trade_class'], row['candidate'])} | "
                f"{row['cases']} | {_pct(row['acceptance_rate'])} | {_pct(row['structural_tp_rate'])} | "
                f"{_number(row['expectancy_r_resolved'])} | {_number(row['profit_factor_resolved'])} |"
            )
    lines.extend(["", "## Selección diagnóstica", ""])
    for trade_class in ("CHILL", "GOD"):
        lines.append(f"- Mejor v2 {trade_class}: `{selected[trade_class]}` ({summary['v2_status'][trade_class]}).")
        lines.append(f"- Mejor comparación {trade_class}: **{_candidate_label(trade_class, summary['best_overall'][trade_class][0])}** — {summary['best_overall'][trade_class][1]}.")
    lines.extend(
        [
            "",
            "## Definiciones y limitaciones",
            "",
            "- Expectancy, PF, win rate, mediana y desviación usan únicamente casos resueltos TP/SL. Los censurados no reciben una salida inventada.",
            "- DD proxy acumula R resuelto en orden cronológico, asignando 0 a censurados/no-trade; no es drawdown de una cuenta ejecutada. MFE posterior al SL empieza en el minuto siguiente para no incluir ticks anteriores al stop dentro de la misma M1.",
            "- Los niveles usan solo H4/H1/M15 cerrados. Las barras son mid; el orden TP/SL se decide con BID/ASK real.",
            "- En la rama ACCEPT, el floor solo etiqueta LOW_REWARD y no cambia la ejecución; si las métricas empatan se muestra 0,50R como representación canónica, no como superior a 0,60/0,70R.",
            "- No se modelan comisión, fills parciales ni slippage adicional. El motor v2 no está conectado al EA ni autorizado para PAPER operativo.",
            "",
        ]
    )
    return "\n".join(lines)


def analyze_structure_aware_tp_v2(input_dir: Path, tick_files: Sequence[Path], output_dir: Path) -> dict:
    base_signals, split_counts, enrichment = prepare_signals(input_dir)
    classified = _annotate_trade_classes(base_signals, input_dir)
    signals = annotate_v2_context(classified, input_dir)
    outcomes, _, _ = _load_outcomes(Path(input_dir) / "historical_outcomes.jsonl")
    stats = IngestionStats()
    tick_scan = simulate_tp_tick_order(signals, iter_ticks(tick_files, stats), target_builder=build_v2_targets)
    ranges = M1RangeIndex(_load_bars(input_dir, "XAUUSD", "M1"))
    details = v2_detail_rows(signals, outcomes, ranges)
    aggregates = aggregate_v2(details)
    selected = {trade_class: select_v2_policy(aggregates, trade_class)[0] for trade_class in ("CHILL", "GOD")}
    v2_status = {trade_class: select_v2_policy(aggregates, trade_class)[1] for trade_class in ("CHILL", "GOD")}
    best = {trade_class: select_best_overall(aggregates, trade_class, selected[trade_class]) for trade_class in ("CHILL", "GOD")}
    examples = v2_examples(signals, selected)
    summary = {
        "analysis_only": True,
        "observer_only": True,
        "score_effect": 0,
        "signals_reproducible": len(signals),
        "signals_with_entry_tick": tick_scan["signals_with_entry_tick"],
        "split_counts": split_counts,
        "decision_enrichment": enrichment,
        "tick_scan": {**tick_scan, "ticks_valid": stats.ticks_valid, "ticks_discarded": stats.ticks_discarded, "duplicates": stats.duplicates},
        "selected_v2": selected,
        "v2_status": v2_status,
        "best_overall": best,
        "examples_count": len(examples),
    }
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write(output_dir / "structure_aware_tp_v2.csv", _csv_text(aggregates))
    _atomic_write(output_dir / "structure_aware_tp_v2_examples.csv", _csv_text(examples))
    _atomic_write(output_dir / "structure_aware_tp_v2.md", _markdown(aggregates, summary))
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
    summary = analyze_structure_aware_tp_v2(arguments.input_dir, [item.path for item in discovery.files], arguments.output_dir)
    print(f"[STRUCTURE_TP_V2] ticks={summary['tick_scan']['ticks_seen']} | signals={summary['signals_reproducible']}")
    print(f"[STRUCTURE_TP_V2] CHILL={summary['selected_v2']['CHILL']} | GOD={summary['selected_v2']['GOD']}")
    print("[STRUCTURE_TP_V2] observer_only=true | score_effect=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
