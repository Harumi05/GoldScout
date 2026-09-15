"""Research-only structure-aware take-profit analysis for GoldScout.

The engine is deliberately downstream from the historical decision replay.  It
uses only bars whose close timestamp is not later than the observation, keeps
the live CURRENT stop unchanged, and resolves hypothetical targets against the
original BID/ASK ticks.  Nothing in this module is imported by the live EA.
"""

from __future__ import annotations

import argparse
from bisect import bisect_right
from collections import defaultdict
import csv
from dataclasses import dataclass, field
import io
import math
from pathlib import Path
from typing import Iterable, Sequence

from research.analyze_adaptive_stop_v2 import adaptive_setup
from research.analyze_historical_dataset import _atomic_write, _directed_values, _load_outcomes
from research.analyze_stop_loss_quality import HORIZON_MS, M1RangeIndex, StopState, _is_number, _mean, _median, prepare_signals
from research.analyze_take_profit_and_account_size import (
    DEFAULT_MAX_DEVIATION_POINTS,
    DEFAULT_POINT_SIZE,
    DEFAULT_TICK_SIZE,
    MIN_REWARD_RISK,
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
from research.enrich_historical_decisions import _detect_pivots, _normalize_pivots, build_state_points
from research.tick_historical_replay import IngestionStats, Tick, discover_inputs, iter_ticks


SOURCE = "HISTORICAL_MT5_TICKS"
OBSERVER_ONLY = True
SCORE_EFFECT = 0
H1_CONTEXT_BARS = 30
H1_RECENT_BARS = 10
H4_CONTEXT_BARS = 12
M15_PRECISION_BARS = 40
EQUAL_LEVEL_TOLERANCE_ATR = 0.20
ZONE_MERGE_TOLERANCE_ATR = 0.15
TARGET_BUFFER_ATR = 0.05
MIN_CONSOLIDATION_BARS = 5
MAX_CONSOLIDATION_RANGE_ATR = 2.0
MAX_EXTENDED_DISTANCE_ATR = 4.0
MIN_SAMPLE = 30


@dataclass(frozen=True)
class LevelEvidence:
    kind: str
    price: float
    timestamp_ms: int
    timeframe: str
    source: str
    strength: int


@dataclass(frozen=True)
class StructuralZone:
    kind: str
    price: float
    touches: int
    strength: int
    major: bool
    sources: tuple[str, ...]
    first_timestamp_ms: int
    last_timestamp_ms: int


@dataclass(frozen=True)
class StructuralContext:
    h1_structure: str
    h4_structure: str
    zones: tuple[StructuralZone, ...]
    breakout_projection: float | None
    context_timestamp_ms: int


@dataclass
class StructureAwareSignal(TPSignal):
    context: StructuralContext | None = None
    structural_candidates: dict[str, float] = field(default_factory=dict)
    structural_candidate_r: dict[str, float] = field(default_factory=dict)
    selected_structural_candidate: str | None = None
    structural_rejection: str | None = None
    first_major_obstacle: StructuralZone | None = None
    current_crosses_major_sr: bool = False


def last_closed_index(close_timestamps: Sequence[int], observation_ms: int) -> int:
    """Return the last bar closed at observation_ms; an open bar is excluded."""
    return bisect_right(close_timestamps, observation_ms) - 1


def cluster_level_evidence(
    evidence: Sequence[LevelEvidence], atr: float, point_size: float = DEFAULT_POINT_SIZE
) -> tuple[StructuralZone, ...]:
    """Merge nearby same-side evidence into deterministic reaction zones."""
    if not _is_number(atr) or float(atr) <= 0.0 or point_size <= 0.0:
        return ()
    tolerance = max(point_size, float(atr) * ZONE_MERGE_TOLERANCE_ATR)
    equal_tolerance = max(point_size, float(atr) * EQUAL_LEVEL_TOLERANCE_ATR)
    zones: list[StructuralZone] = []
    for kind in ("HIGH", "LOW"):
        ordered = sorted(
            (item for item in evidence if item.kind == kind and _is_number(item.price)),
            key=lambda item: (item.price, item.timestamp_ms, item.source),
        )
        groups: list[list[LevelEvidence]] = []
        for item in ordered:
            if groups and item.price - groups[-1][0].price <= tolerance:
                groups[-1].append(item)
            else:
                groups.append([item])
        for group in groups:
            weight = sum(max(1, item.strength) for item in group)
            price = sum(item.price * max(1, item.strength) for item in group) / weight
            timestamps = sorted({item.timestamp_ms for item in group})
            sources = sorted({item.source for item in group})
            touches = len(timestamps)
            strongest = max(item.strength for item in group)
            if touches >= 2 and max(item.price for item in group) - min(item.price for item in group) <= equal_tolerance:
                sources.append("EQUAL_HIGH" if kind == "HIGH" else "EQUAL_LOW")
            strength = min(5, strongest + min(2, max(0, touches - 1)))
            major = strength >= 3 or touches >= 2 or any(item.timeframe == "H4" for item in group)
            zones.append(
                StructuralZone(
                    kind=kind,
                    price=price,
                    touches=touches,
                    strength=strength,
                    major=major,
                    sources=tuple(sorted(set(sources))),
                    first_timestamp_ms=timestamps[0],
                    last_timestamp_ms=timestamps[-1],
                )
            )
    return tuple(sorted(zones, key=lambda item: (item.price, item.kind)))


class CausalStructureIndex:
    """Closed-bar-only H4/H1/M15 level index used by the research engine."""

    def __init__(self, input_dir: Path, point_size: float = DEFAULT_POINT_SIZE):
        self.point_size = point_size
        self.bars = {timeframe: _load_bars(input_dir, "XAUUSD", timeframe) for timeframe in ("M15", "H1", "H4")}
        self.points = {timeframe: build_state_points(items, point_size) for timeframe, items in self.bars.items()}
        self.close_times = {
            timeframe: [int(item["close_timestamp"]) for item in items]
            for timeframe, items in self.bars.items()
        }

    def _pivots(self, timeframe: str, index: int, lookback: int):
        start = max(0, index - lookback + 1)
        bars = self.bars[timeframe][start : index + 1]
        atrs = [point.indicators.get("atr") for point in self.points[timeframe][start : index + 1]]
        return _normalize_pivots(_detect_pivots(bars, atrs, self.point_size))

    @staticmethod
    def _extreme_evidence(bars: Sequence[dict], timeframe: str, strength: int) -> list[LevelEvidence]:
        if not bars:
            return []
        high = max(bars, key=lambda item: (float(item["high"]), -int(item["timestamp"])))
        low = min(bars, key=lambda item: (float(item["low"]), int(item["timestamp"])))
        return [
            LevelEvidence("HIGH", float(high["high"]), int(high["timestamp"]), timeframe, f"RECENT_{timeframe}_HIGH", strength),
            LevelEvidence("LOW", float(low["low"]), int(low["timestamp"]), timeframe, f"RECENT_{timeframe}_LOW", strength),
        ]

    def context(self, signal) -> StructuralContext | None:
        indexes = {
            timeframe: last_closed_index(close_times, signal.anchor_ms)
            for timeframe, close_times in self.close_times.items()
        }
        if indexes["H1"] < H1_CONTEXT_BARS - 1 or indexes["M15"] < M15_PRECISION_BARS - 1 or indexes["H4"] < 1:
            return None
        h1_index, h4_index, m15_index = indexes["H1"], indexes["H4"], indexes["M15"]
        h1_bars = self.bars["H1"][h1_index - H1_CONTEXT_BARS + 1 : h1_index + 1]
        recent_h1 = h1_bars[-H1_RECENT_BARS:]
        m15_bars = self.bars["M15"][m15_index - M15_PRECISION_BARS + 1 : m15_index + 1]
        h4_start = max(0, h4_index - H4_CONTEXT_BARS + 1)
        h4_bars = self.bars["H4"][h4_start : h4_index + 1]
        atr = float(signal.atr)
        evidence: list[LevelEvidence] = []
        for timeframe, index, lookback, strength in (
            ("H1", h1_index, H1_CONTEXT_BARS, 3),
            ("H4", h4_index, H4_CONTEXT_BARS, 4),
            ("M15", m15_index, M15_PRECISION_BARS, 1),
        ):
            recent_cutoff = int(recent_h1[0]["timestamp"]) if timeframe == "H1" else -1
            for pivot in self._pivots(timeframe, index, lookback):
                boost = 1 if timeframe == "H1" and pivot.timestamp_ms >= recent_cutoff else 0
                evidence.append(
                    LevelEvidence(
                        pivot.kind,
                        pivot.price,
                        pivot.timestamp_ms,
                        timeframe,
                        f"CONFIRMED_{timeframe}_PIVOT_{pivot.kind}",
                        min(5, strength + boost),
                    )
                )
        evidence.extend(self._extreme_evidence(recent_h1[:-1], "H1", 2))
        evidence.extend(self._extreme_evidence(m15_bars[:-1], "M15", 1))

        consolidation = recent_h1[-MIN_CONSOLIDATION_BARS:]
        consolidation_high = max(float(item["high"]) for item in consolidation)
        consolidation_low = min(float(item["low"]) for item in consolidation)
        consolidation_range = consolidation_high - consolidation_low
        if 0.50 * atr <= consolidation_range <= MAX_CONSOLIDATION_RANGE_ATR * atr:
            stamp = int(consolidation[-1]["timestamp"])
            evidence.extend(
                (
                    LevelEvidence("HIGH", consolidation_high, stamp, "H1", "H1_CONSOLIDATION_RESISTANCE", 2),
                    LevelEvidence("LOW", consolidation_low, stamp, "H1", "H1_CONSOLIDATION_SUPPORT", 2),
                )
            )

        h1_state = self.points["H1"][h1_index].structure.state
        h4_state = self.points["H4"][h4_index].structure.state
        projection = None
        aligned_h1 = (signal.direction == "LONG" and h1_state == "BULLISH") or (
            signal.direction == "SHORT" and h1_state == "BEARISH"
        )
        aligned_h4 = h4_state in {"NEUTRAL", "INSUFFICIENT"} or (
            (signal.direction == "LONG" and h4_state == "BULLISH")
            or (signal.direction == "SHORT" and h4_state == "BEARISH")
        )
        if signal.breakout and signal.momentum and aligned_h1 and aligned_h4:
            measured = min(3.0 * atr, max(atr, max(float(item["high"]) for item in recent_h1[:-1]) - min(float(item["low"]) for item in recent_h1[:-1])))
            projection = float(signal.reference_close) + measured * (1.0 if signal.direction == "LONG" else -1.0)
        return StructuralContext(
            h1_structure=h1_state,
            h4_structure=h4_state,
            zones=cluster_level_evidence(evidence, atr, self.point_size),
            breakout_projection=projection,
            context_timestamp_ms=max(int(self.bars[timeframe][index]["close_timestamp"]) for timeframe, index in indexes.items()),
        )


def _target_before_zone(zone: StructuralZone, direction: str, atr: float, tick_size: float) -> float:
    raw = zone.price - TARGET_BUFFER_ATR * atr if direction == "LONG" else zone.price + TARGET_BUFFER_ATR * atr
    return _align_price(raw, tick_size, up=direction == "SHORT")


def _is_beyond(price: float, obstacle: float, direction: str) -> bool:
    return price > obstacle + 1e-9 if direction == "LONG" else price < obstacle - 1e-9


def _distance_from_entry(price: float, entry: float, direction: str) -> float:
    return price - entry if direction == "LONG" else entry - price


def _continuation_is_strong(item: StructureAwareSignal) -> bool:
    if item.context is None or not item.base.momentum:
        return False
    h1_aligned = (item.base.direction == "LONG" and item.context.h1_structure == "BULLISH") or (
        item.base.direction == "SHORT" and item.context.h1_structure == "BEARISH"
    )
    h4_not_opposed = item.context.h4_structure in {"BULLISH", "NEUTRAL", "INSUFFICIENT"} if item.base.direction == "LONG" else item.context.h4_structure in {"BEARISH", "NEUTRAL", "INSUFFICIENT"}
    continuation_event = item.base.breakout or adaptive_setup(item.base).startswith("CONTINUATION")
    return h1_aligned and h4_not_opposed and continuation_event


def build_structure_aware_targets(
    item: TPSignal,
    entry_price: float,
    stop: StopState,
    *,
    tick_size: float = DEFAULT_TICK_SIZE,
) -> tuple[float, float, dict[str, TargetState]]:
    """Build CURRENT, prior-best, structural candidates and selected policy."""
    if not isinstance(item, StructureAwareSignal):
        raise TypeError("StructureAwareSignal required")
    worst_fill, planned_risk, all_fixed = build_target_candidates(
        trade_class=item.trade_class,
        current_target_r=item.current_target_r,
        direction=item.base.direction,
        entry_price=entry_price,
        stop_price=stop.price,
        tick_size=tick_size,
    )
    fixed_name = "R_0_75" if item.trade_class == "CHILL" else "R_1_25"
    targets = {"CURRENT": all_fixed["CURRENT"], fixed_name: all_fixed[fixed_name]}
    item.structural_candidates.clear()
    item.structural_candidate_r.clear()
    item.selected_structural_candidate = None
    item.structural_rejection = None
    item.first_major_obstacle = None
    item.current_crosses_major_sr = False
    if item.context is None:
        item.structural_rejection = "NO_STRUCTURAL_CONTEXT"
        return worst_fill, planned_risk, targets

    direction = item.base.direction
    wanted_kind = "HIGH" if direction == "LONG" else "LOW"
    zones = [
        zone for zone in item.context.zones
        if zone.kind == wanted_kind and _distance_from_entry(zone.price, entry_price, direction) > TARGET_BUFFER_ATR * item.base.atr
    ]
    zones.sort(key=lambda zone: _distance_from_entry(zone.price, entry_price, direction))
    major_zones = [zone for zone in zones if zone.major]
    item.first_major_obstacle = major_zones[0] if major_zones else None
    if item.first_major_obstacle is not None:
        obstacle_target = _target_before_zone(item.first_major_obstacle, direction, item.base.atr, tick_size)
        item.current_crosses_major_sr = _is_beyond(targets["CURRENT"].price, obstacle_target, direction)

    definitions: list[tuple[str, float]] = []
    if zones:
        definitions.append(("TP_STRUCTURAL_NEAR", _target_before_zone(zones[0], direction, item.base.atr, tick_size)))
        base_zone = major_zones[0] if major_zones else zones[min(1, len(zones) - 1)]
        definitions.append(("TP_STRUCTURAL_BASE", _target_before_zone(base_zone, direction, item.base.atr, tick_size)))

    if _continuation_is_strong(item) and _is_number(item.context.breakout_projection):
        projection = float(item.context.breakout_projection)
        projection_distance = _distance_from_entry(projection, entry_price, direction)
        prior_major = any(
            _distance_from_entry(zone.price, entry_price, direction) + 1e-9 < projection_distance
            for zone in major_zones
        )
        if 0.0 < projection_distance <= MAX_EXTENDED_DISTANCE_ATR * item.base.atr and not prior_major:
            definitions.append(("TP_STRUCTURAL_EXTENDED", _align_price(projection, tick_size, up=direction == "LONG")))

    unique_prices: set[float] = set()
    for name, price in definitions:
        distance = _distance_from_entry(price, entry_price, direction)
        if distance <= 0.0 or price in unique_prices:
            continue
        unique_prices.add(price)
        multiple = distance / planned_risk
        item.structural_candidates[name] = price
        item.structural_candidate_r[name] = multiple
        targets[name] = TargetState(name, multiple, price)

    if item.first_major_obstacle is not None:
        obstacle_price = _target_before_zone(item.first_major_obstacle, direction, item.base.atr, tick_size)
        obstacle_r = _distance_from_entry(obstacle_price, entry_price, direction) / planned_risk
        if obstacle_r + 1e-9 < MIN_REWARD_RISK:
            item.structural_rejection = "POOR_REWARD"
            return worst_fill, planned_risk, targets

    if item.trade_class == "GOD" and _continuation_is_strong(item):
        preference = ("TP_STRUCTURAL_EXTENDED", "TP_STRUCTURAL_BASE", "TP_STRUCTURAL_NEAR")
    elif item.trade_class == "GOD":
        preference = ("TP_STRUCTURAL_NEAR",)
    else:
        preference = ("TP_STRUCTURAL_NEAR", "TP_STRUCTURAL_BASE")

    for name in preference:
        candidate = targets.get(name)
        if candidate is None or candidate.r_multiple + 1e-9 < MIN_REWARD_RISK:
            continue
        if item.first_major_obstacle is not None:
            obstacle_price = _target_before_zone(item.first_major_obstacle, direction, item.base.atr, tick_size)
            if _is_beyond(candidate.price, obstacle_price, direction):
                continue
        item.selected_structural_candidate = name
        targets["STRUCTURE_AWARE"] = TargetState("STRUCTURE_AWARE", candidate.r_multiple, candidate.price)
        break
    if item.selected_structural_candidate is None:
        item.structural_rejection = "POOR_REWARD" if item.structural_candidates else "NO_STRUCTURAL_TARGET"
    return worst_fill, planned_risk, targets


def annotate_structure_context(signals: Sequence[TPSignal], input_dir: Path) -> list[StructureAwareSignal]:
    index = CausalStructureIndex(input_dir)
    output: list[StructureAwareSignal] = []
    for item in signals:
        wrapped = StructureAwareSignal(item.base, item.trade_class, item.current_target_r)
        wrapped.context = index.context(item.base)
        output.append(wrapped)
    return output


def _empty_policy_row(item: StructureAwareSignal, horizon: str) -> dict:
    signal = item.base
    return {
        "event_id": signal.event_id,
        "timestamp": signal.anchor_ms,
        "period": signal.period,
        "trade_class": item.trade_class,
        "direction": signal.direction,
        "setup": adaptive_setup(signal),
        "candidate": "STRUCTURE_AWARE",
        "selected_structural_candidate": None,
        "target_r": None,
        "horizon": horizon,
        "eligible": False,
        "rejection_reason": item.structural_rejection,
        "poor_reward": item.structural_rejection == "POOR_REWARD",
        "tp_hit": False,
        "sl_hit": False,
        "tp_first": False,
        "sl_first": False,
        "unresolved": False,
        "time_to_tp_minutes": None,
        "hypothetical_realized_r": 0.0,
        "mfe_r": None,
        "mae_r": None,
        "mfe_after_close_r": None,
        "current_crosses_major_sr": item.current_crosses_major_sr,
        "observer_only": True,
        "diagnostic_only": True,
        "score_effect": 0,
    }


def structure_tp_detail_rows(
    signals: Sequence[StructureAwareSignal], outcomes: dict, ranges: M1RangeIndex
) -> list[dict]:
    rows: list[dict] = []
    for item in signals:
        signal, stop = item.base, item.stop
        if signal.entry_mid is None or item.entry_price is None or stop is None or item.planned_risk_distance is None:
            continue
        planned = item.planned_risk_distance
        stop_loss_r = float(item.actual_stop_distance) / planned
        candidates = ["CURRENT", "R_0_75" if item.trade_class == "CHILL" else "R_1_25", "STRUCTURE_AWARE"]
        for candidate_name in candidates:
            target = item.targets.get(candidate_name)
            if target is None:
                for horizon in HORIZON_MS:
                    rows.append(_empty_policy_row(item, horizon))
                continue
            for horizon, duration in HORIZON_MS.items():
                end = signal.anchor_ms + duration
                tp_in = target.hit_ms is not None and target.hit_ms <= end
                sl_in = stop.hit_ms is not None and stop.hit_ms <= end
                tp_first = tp_in and (not sl_in or int(target.hit_ms) < int(stop.hit_ms))
                sl_first = sl_in and (not tp_in or int(stop.hit_ms) <= int(target.hit_ms))
                outcome = outcomes.get(signal.event_id, {}).get(horizon)
                directed = _directed_values(outcome, signal.direction) if outcome else (None, None, None)
                _, mfe_return, mae_return = directed
                mfe_price = float(mfe_return) * signal.reference_close if _is_number(mfe_return) else None
                mae_price = abs(float(mae_return)) * signal.reference_close if _is_number(mae_return) else None
                post_tp = _post_tp_favorable(item, target, end, ranges) if tp_first else None
                target_distance = abs(target.price - item.entry_price)
                after_close_r = max(0.0, float(post_tp) - target_distance) / planned if _is_number(post_tp) else None
                realized_r = target.r_multiple if tp_first else -stop_loss_r if sl_first else None
                rows.append(
                    {
                        "event_id": signal.event_id,
                        "timestamp": signal.anchor_ms,
                        "period": signal.period,
                        "trade_class": item.trade_class,
                        "direction": signal.direction,
                        "setup": adaptive_setup(signal),
                        "candidate": candidate_name,
                        "selected_structural_candidate": item.selected_structural_candidate if candidate_name == "STRUCTURE_AWARE" else None,
                        "target_r": target.r_multiple,
                        "horizon": horizon,
                        "eligible": True,
                        "rejection_reason": None,
                        "poor_reward": False,
                        "tp_hit": tp_in,
                        "sl_hit": sl_in,
                        "tp_first": tp_first,
                        "sl_first": sl_first,
                        "unresolved": not tp_first and not sl_first,
                        "time_to_tp_minutes": (int(target.hit_ms) - signal.anchor_ms) / 60_000.0 if tp_first else None,
                        "hypothetical_realized_r": realized_r,
                        "mfe_r": float(mfe_price) / planned if _is_number(mfe_price) else None,
                        "mae_r": float(mae_price) / planned if _is_number(mae_price) else None,
                        "mfe_after_close_r": after_close_r,
                        "current_crosses_major_sr": item.current_crosses_major_sr,
                        "observer_only": True,
                        "diagnostic_only": True,
                        "score_effect": 0,
                    }
                )
    return rows


def summarize_structure_tp(rows: Sequence[dict]) -> dict:
    total = len(rows)
    eligible = [row for row in rows if row["eligible"]]
    resolved = [row for row in eligible if _is_number(row.get("hypothetical_realized_r"))]
    wins = [float(row["hypothetical_realized_r"]) for row in resolved if float(row["hypothetical_realized_r"]) > 0.0]
    losses = [float(row["hypothetical_realized_r"]) for row in resolved if float(row["hypothetical_realized_r"]) < 0.0]
    gross_profit, gross_loss = sum(wins), abs(sum(losses))
    return {
        "cases": total,
        "eligible_cases": len(eligible),
        "rejected_cases": total - len(eligible),
        "acceptance_rate": len(eligible) / total if total else None,
        "poor_reward_rate": sum(bool(row["poor_reward"]) for row in rows) / total if total else None,
        "resolved_cases": len(resolved),
        "censored_cases": len(eligible) - len(resolved),
        "tp_hit_rate": sum(bool(row["tp_hit"]) for row in eligible) / len(eligible) if eligible else None,
        "sl_hit_rate": sum(bool(row["sl_hit"]) for row in eligible) / len(eligible) if eligible else None,
        "tp_first_rate": sum(bool(row["tp_first"]) for row in eligible) / len(eligible) if eligible else None,
        "sl_first_rate": sum(bool(row["sl_first"]) for row in eligible) / len(eligible) if eligible else None,
        "win_rate_resolved": len(wins) / len(resolved) if resolved else None,
        "expectancy_r_resolved": _mean(row["hypothetical_realized_r"] for row in resolved),
        "expectancy_r_all_cases": sum(float(row["hypothetical_realized_r"]) for row in resolved) / total if total else None,
        "profit_factor_resolved": gross_profit / gross_loss if gross_loss > 0.0 else None,
        "mean_time_to_tp_minutes": _mean(row["time_to_tp_minutes"] for row in eligible),
        "median_time_to_tp_minutes": _median(row["time_to_tp_minutes"] for row in eligible),
        "median_mfe_r": _median(row["mfe_r"] for row in eligible),
        "median_mae_r": _median(row["mae_r"] for row in eligible),
        "median_mfe_after_close_r": _median(row["mfe_after_close_r"] for row in eligible),
        "current_crosses_major_sr_rate": sum(bool(row["current_crosses_major_sr"]) for row in rows) / total if total else None,
    }


def aggregate_structure_tp(rows: Sequence[dict]) -> list[dict]:
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
            **summarize_structure_tp(members),
            "observer_only": True,
            "diagnostic_only": True,
            "score_effect": 0,
        }
        for key, members in sorted(groups.items())
    ]


def structure_examples(signals: Sequence[StructureAwareSignal], limit: int = 200) -> list[dict]:
    rows = []
    for item in signals:
        if not item.current_crosses_major_sr or item.first_major_obstacle is None or item.entry_price is None:
            continue
        current = item.targets.get("CURRENT")
        selected = item.targets.get("STRUCTURE_AWARE")
        rows.append(
            {
                "event_id": item.base.event_id,
                "timestamp": item.base.anchor_ms,
                "period": item.base.period,
                "trade_class": item.trade_class,
                "direction": item.base.direction,
                "setup": adaptive_setup(item.base),
                "entry_price": item.entry_price,
                "stop_price": item.stop.price if item.stop else None,
                "current_tp": current.price if current else None,
                "major_obstacle": item.first_major_obstacle.price,
                "obstacle_strength": item.first_major_obstacle.strength,
                "obstacle_sources": "+".join(item.first_major_obstacle.sources),
                "structural_near": item.structural_candidates.get("TP_STRUCTURAL_NEAR"),
                "structural_base": item.structural_candidates.get("TP_STRUCTURAL_BASE"),
                "structural_extended": item.structural_candidates.get("TP_STRUCTURAL_EXTENDED"),
                "selected_candidate": item.selected_structural_candidate,
                "selected_tp": selected.price if selected else None,
                "rejection_reason": item.structural_rejection,
                "avoided_target_beyond_sr": True,
                "observer_only": True,
                "diagnostic_only": True,
                "score_effect": 0,
            }
        )
    return rows[:limit]


def _csv_text(rows: Sequence[dict]) -> str:
    if not rows:
        return "\n"
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


def _policy_name(trade_class: str, candidate: str) -> str:
    if candidate == "CURRENT":
        return "CURRENT 1.25R" if trade_class == "CHILL" else "CURRENT 2.0R"
    if candidate == "R_0_75":
        return "FIXED 0.75R"
    if candidate == "R_1_25":
        return "FIXED 1.25R"
    return candidate


def choose_diagnostic_policy(rows: Sequence[dict], trade_class: str) -> tuple[str, str]:
    indexed = {
        (row["period"], row["candidate"]): row
        for row in rows
        if row["scope"] == "GLOBAL" and row["trade_class"] == trade_class and row["horizon"] == "4h"
    }
    candidates = ("CURRENT", "R_0_75" if trade_class == "CHILL" else "R_1_25", "STRUCTURE_AWARE")
    ranked = []
    for candidate in candidates:
        validation, oos = indexed.get(("VALIDATION", candidate)), indexed.get(("OOS", candidate))
        if not validation or not oos or not all(_is_number(row.get("expectancy_r_all_cases")) for row in (validation, oos)):
            continue
        if candidate == "STRUCTURE_AWARE" and any(
            not _is_number(row.get("acceptance_rate")) or float(row["acceptance_rate"]) < 0.20
            for row in (validation, oos)
        ):
            continue
        ranked.append(((float(validation["expectancy_r_all_cases"]) + float(oos["expectancy_r_all_cases"])) / 2.0, candidate))
    if not ranked:
        return "CURRENT", "evidencia VALIDATION/OOS insuficiente"
    _, candidate = max(ranked)
    return candidate, "mayor expectancy media por oportunidad en VALIDATION/OOS, exigiendo al menos 20% de aceptación a políticas selectivas; solo diagnóstico"


def _markdown(rows: Sequence[dict], summary: dict) -> str:
    lines = [
        "# Structure-Aware Take Profit",
        "",
        "> Research/PAPER only · source=HISTORICAL_MT5_TICKS · observer_only=true · score_effect=0.",
        "",
        "La política es **STRUCTURE FIRST, R:R SECOND**. Solo usa H4/H1/M15 cerrados disponibles al timestamp de observación; conserva el SL CURRENT. Un nivel mayor anterior invalida objetivos posteriores. Si el primer objetivo estructural mayor ofrece menos de 1,25R, el caso queda `POOR_REWARD` y no se desplaza el TP para fabricar R:R.",
        "",
        f"Universo: {summary['signals_reproducible']:,} setups ARM; {summary['signals_with_entry_tick']:,} con tick de entrada; barrido único de {summary['tick_scan']['ticks_seen']:,} ticks válidos.",
        "",
        "`expectancy_r_resolved` y PF usan solo casos con TP/SL reproducible dentro de 4h. `expectancy_r_all_cases` asigna 0 a no-trade/censurados y permite comparar cobertura; no inventa precios de salida.",
        "",
        "## CURRENT vs fijo anterior vs STRUCTURE_AWARE — 4h",
        "",
        "| Clase | Periodo | Política | Casos | Acepta | Poor reward | TP primero | SL primero | Exp R resuelta | Exp R/caso | PF | Mediana TP | MFE post-TP | CURRENT cruza S/R |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        if row["scope"] == "GLOBAL" and row["horizon"] == "4h":
            lines.append(
                f"| {row['trade_class']} | {row['period']} | {_policy_name(row['trade_class'], row['candidate'])} | {row['cases']} | "
                f"{_pct(row['acceptance_rate'])} | {_pct(row['poor_reward_rate'])} | {_pct(row['tp_first_rate'])} | "
                f"{_pct(row['sl_first_rate'])} | {_number(row['expectancy_r_resolved'])} | {_number(row['expectancy_r_all_cases'])} | "
                f"{_number(row['profit_factor_resolved'])} | {_number(row['median_time_to_tp_minutes'], 1)}m | "
                f"{_number(row['median_mfe_after_close_r'])}R | {_pct(row['current_crosses_major_sr_rate'])} |"
            )
    lines.extend(["", "## LONG / SHORT — OOS, 4h", "", "| Clase | Dirección | Política | Casos | Acepta | TP primero | SL primero | Exp R resuelta | Exp R/caso | PF |", "|---|---|---|---:|---:|---:|---:|---:|---:|---:|"])
    for row in rows:
        if row["scope"] == "DIRECTION" and row["period"] == "OOS" and row["horizon"] == "4h":
            lines.append(
                f"| {row['trade_class']} | {row['direction']} | {_policy_name(row['trade_class'], row['candidate'])} | {row['cases']} | "
                f"{_pct(row['acceptance_rate'])} | {_pct(row['tp_first_rate'])} | {_pct(row['sl_first_rate'])} | "
                f"{_number(row['expectancy_r_resolved'])} | {_number(row['expectancy_r_all_cases'])} | {_number(row['profit_factor_resolved'])} |"
            )
    lines.extend(["", "## Por setup — OOS, 4h (n ≥ 30)", "", "| Clase | Dirección | Setup | Política | n | Acepta | TP primero | Exp R resuelta | Exp R/caso | PF |", "|---|---|---|---|---:|---:|---:|---:|---:|---:|"])
    for row in rows:
        if row["scope"] == "SETUP" and row["period"] == "OOS" and row["horizon"] == "4h" and int(row["cases"]) >= MIN_SAMPLE:
            lines.append(
                f"| {row['trade_class']} | {row['direction']} | {row['setup']} | {_policy_name(row['trade_class'], row['candidate'])} | {row['cases']} | "
                f"{_pct(row['acceptance_rate'])} | {_pct(row['tp_first_rate'])} | {_number(row['expectancy_r_resolved'])} | "
                f"{_number(row['expectancy_r_all_cases'])} | {_number(row['profit_factor_resolved'])} |"
            )
    lines.extend(["", "## Conclusión diagnóstica", ""])
    for trade_class in ("CHILL", "GOD"):
        candidate, reason = summary["best"][trade_class]
        lines.append(f"- {trade_class}: **{_policy_name(trade_class, candidate)}** — {reason}.")
    lines.extend(
        [
            "",
            f"Ejemplos donde CURRENT quedaba detrás de un S/R mayor: {summary['examples_count']} (archivo `structure_aware_tp_examples.csv`, limitado a 200 filas).",
            "",
            "## Limitaciones",
            "",
            "- La reconstrucción replica el SL CURRENT y la clasificación CHILL/GOD, pero los niveles estructurales son un motor research nuevo; todavía no existe paridad MetaTrader/PAPER.",
            "- Las barras se construyen desde mid; la secuencia TP/SL sí se resuelve con BID/ASK real. Los niveles se amortiguan 0,05 ATR antes de la zona.",
            "- La censura a 4h y los casos rechazados requieren leer conjuntamente expectancy resuelta, expectancy por oportunidad y tasa de aceptación.",
            "- La selección resumida no admite una política selectiva con menos de 20% de aceptación tanto en VALIDATION como en OOS; evita premiar artificialmente una estrategia que casi nunca opera.",
            "- No se modelan comisiones, slippage más allá del peor fill configurado ni órdenes parciales. Ningún resultado autoriza cambios live.",
            "",
        ]
    )
    return "\n".join(lines)


def analyze_structure_aware_tp(input_dir: Path, tick_files: Sequence[Path], output_dir: Path) -> dict:
    base_signals, split_counts, enrichment = prepare_signals(input_dir)
    classified = _annotate_trade_classes(base_signals, input_dir)
    signals = annotate_structure_context(classified, input_dir)
    outcomes, _, _ = _load_outcomes(Path(input_dir) / "historical_outcomes.jsonl")
    stats = IngestionStats()
    tick_scan = simulate_tp_tick_order(
        signals,
        iter_ticks(tick_files, stats),
        target_builder=build_structure_aware_targets,
    )
    ranges = M1RangeIndex(_load_bars(input_dir, "XAUUSD", "M1"))
    details = structure_tp_detail_rows(signals, outcomes, ranges)
    aggregates = aggregate_structure_tp(details)
    examples = structure_examples(signals)
    best = {trade_class: choose_diagnostic_policy(aggregates, trade_class) for trade_class in ("CHILL", "GOD")}
    summary = {
        "analysis_only": True,
        "observer_only": True,
        "score_effect": 0,
        "signals_reproducible": len(signals),
        "signals_with_entry_tick": tick_scan["signals_with_entry_tick"],
        "split_counts": split_counts,
        "decision_enrichment": enrichment,
        "tick_scan": {**tick_scan, "ticks_valid": stats.ticks_valid, "ticks_discarded": stats.ticks_discarded, "duplicates": stats.duplicates},
        "best": best,
        "examples_count": len(examples),
    }
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write(output_dir / "structure_aware_tp.csv", _csv_text(aggregates))
    _atomic_write(output_dir / "structure_aware_tp_examples.csv", _csv_text(examples))
    _atomic_write(output_dir / "structure_aware_tp.md", _markdown(aggregates, summary))
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
    summary = analyze_structure_aware_tp(arguments.input_dir, [item.path for item in discovery.files], arguments.output_dir)
    scan = summary["tick_scan"]
    print(f"[STRUCTURE_TP] signals={summary['signals_reproducible']} | entry_ticks={summary['signals_with_entry_tick']}")
    print(f"[STRUCTURE_TP] ticks={scan['ticks_seen']} | elapsed={scan['elapsed_seconds']:.1f}s")
    print(f"[STRUCTURE_TP] best_CHILL={summary['best']['CHILL'][0]} | best_GOD={summary['best']['GOD'][0]}")
    print("[STRUCTURE_TP] observer_only=true | score_effect=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
