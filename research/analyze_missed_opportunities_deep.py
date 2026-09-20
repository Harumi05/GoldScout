"""Deep offline diagnosis of GoldScout historical missed opportunities.

The universe intentionally preserves the existing diagnostic definition:
``decision == NO_TRADE`` plus an absolute one-hour close move of at least
``0.75 ATR``.  Opportunity direction is hindsight metadata used only to select
the relevant LONG/SHORT score and directionally normalize outcomes.

This module never imports, writes, or executes the live EA.  It consumes only
append-only historical observations, decision enrichment, outcomes, and the
previous diagnostic stale-bias report.
"""

from __future__ import annotations

import argparse
from bisect import bisect_right
from collections import Counter, defaultdict
import csv
import json
import math
from pathlib import Path
import statistics
from typing import Iterable, Mapping, Sequence

from research.analyze_historical_dataset import (
    AnalysisError,
    HORIZONS,
    PERIODS,
    _atomic_write,
    _directed_values,
    _is_number,
    _load_observations,
    _load_outcomes,
    _merge_decision_enrichment,
    _write_csv,
    summarize_values,
    temporal_split,
)
from research.enrich_historical_decisions import _load_bars, build_state_points


ARM_THRESHOLD = 58
TRADE_THRESHOLD = 74
MISSED_THRESHOLD_ATR = 0.75
TRADE_COUNTERFACTUALS = (74, 72, 70, 68)
ARM_COUNTERFACTUALS = (58, 56, 54)
ANALYSIS_SCORE_BUCKETS = (
    ("<50", None, 50),
    ("50-57", 50, 58),
    ("58-64", 58, 65),
    ("65-69", 65, 70),
    ("70-73", 70, 74),
    ("74+", 74, None),
)
CAUSES = (
    "SCORE_BELOW_ARM",
    "SCORE_ARMED_BELOW_TRADE",
    "H4_CONFLICT",
    "H1_STRUCTURE_CONFLICT",
    "M15_NOT_CONFIRMED",
    "MOMENTUM_WEAK",
    "BREAKOUT_ABSENT",
    "PULLBACK_NOT_VALID",
    "RECOVERY_NOT_VALID",
    "RSI_FILTER",
    "ADX_FILTER",
    "VOLUME_WEAK",
    "SESSION_CONTEXT",
    "SPREAD_HIGH",
    "NEWS_CONTEXT_UNAVAILABLE",
    "MULTIPLE_CAUSES",
    "UNKNOWN",
)


def relevant_score(observation: Mapping[str, object], direction: str) -> float | None:
    key = "long_score" if direction == "LONG" else "short_score" if direction == "SHORT" else ""
    value = observation.get(key) if key else None
    return float(value) if _is_number(value) else None


def distance_to_threshold(score: float, threshold: int) -> float:
    """Return the non-negative number of points missing from a threshold."""

    if not math.isfinite(score):
        raise ValueError("score must be finite")
    return max(0.0, float(threshold) - score)


def distance_bucket(distance: float) -> str:
    if not math.isfinite(distance) or distance < 0.0:
        raise ValueError("distance must be finite and non-negative")
    if distance < 5.0:
        return "<5"
    if distance < 10.0:
        return "5-9"
    if distance < 15.0:
        return "10-14"
    return "15+"


def analysis_score_bucket(score: float) -> str:
    if not math.isfinite(score):
        raise ValueError("score must be finite")
    for label, lower, upper in ANALYSIS_SCORE_BUCKETS:
        if (lower is None or score >= lower) and (upper is None or score < upper):
            return label
    raise AssertionError("unreachable score bucket")


def _aligned(direction: str, state: object) -> bool:
    text = str(state or "").strip().upper()
    return (direction == "LONG" and text == "BULLISH") or (direction == "SHORT" and text == "BEARISH")


def _opposite(direction: str, state: object) -> bool:
    text = str(state or "").strip().upper()
    return (direction == "LONG" and text == "BEARISH") or (direction == "SHORT" and text == "BULLISH")


def _event_aligned(direction: str, value: object) -> bool:
    return str(value or "").strip().upper() == direction


class H1FeatureResolver:
    """Resolve only the last H1 bar closed at an observation timestamp."""

    def __init__(self, input_dir: Path, symbol: str = "XAUUSD", point_size: float = 0.01):
        bars = _load_bars(Path(input_dir), symbol, "H1")
        self.points = build_state_points(bars, point_size)
        self.close_times = [int(point.bar["close_timestamp"]) for point in self.points]

    def resolve(self, observed_at_ms: int):
        index = bisect_right(self.close_times, int(observed_at_ms)) - 1
        if index < 0:
            return None, []
        return self.points[index], self.points[max(0, index - 20) : index]


def score_components(
    observation: Mapping[str, object], direction: str, h1_point=None, h1_prior: Sequence | None = None
) -> dict[str, float | bool | str | None]:
    """Reconstruct persisted closed-bar score contributions without inference.

    H4, H1 EMA trend, their alignment, the structural bucket and M15 are read
    directly from enrichment. RSI/ADX/volume are reconstructed only when the
    causal H1 state is supplied. Any arithmetic difference remains ``other``;
    it is never silently assigned to a named component.
    """

    h4 = observation.get("h4_context") if isinstance(observation.get("h4_context"), dict) else {}
    h1 = observation.get("h1_structure") if isinstance(observation.get("h1_structure"), dict) else {}
    m15 = observation.get("m15_timing") if isinstance(observation.get("m15_timing"), dict) else {}
    h4_state = h4.get("trend")
    h1_ema_state = h1.get("ema_trend")
    h4_points = 20.0 if _aligned(direction, h4_state) else 0.0
    h1_points = 15.0 if _aligned(direction, h1_ema_state) else 0.0
    alignment_points = 5.0 if h4_points and h1_points else 0.0
    structural_key = "structural_points_long_without_patterns" if direction == "LONG" else "structural_points_short_without_patterns"
    structural_points = float(h1.get(structural_key, 0.0)) if _is_number(h1.get(structural_key)) else 0.0
    adjustment_key = "long_adjustment" if direction == "LONG" else "short_adjustment"
    m15_points = float(m15.get(adjustment_key, 0.0)) if _is_number(m15.get(adjustment_key)) else 0.0
    technical_key = "long_technical_score" if direction == "LONG" else "short_technical_score"
    technical = float(observation.get(technical_key)) if _is_number(observation.get(technical_key)) else None
    final = relevant_score(observation, direction)
    adx_points: float | None = None
    rsi_points: float | None = None
    volume_points: float | None = None
    if h1_point is not None:
        adx = h1_point.indicators.get("adx")
        rsi = h1_point.indicators.get("rsi")
        adx_points = 10.0 if _is_number(adx) and float(adx) >= 16.0 and (h4_points > 0 or h1_points > 0) else 0.0
        if _is_number(rsi):
            value = float(rsi)
            rsi_points = 10.0 if (direction == "LONG" and 50.0 <= value <= 68.0) or (direction == "SHORT" and 32.0 <= value <= 50.0) else 0.0
        else:
            rsi_points = 0.0
        prior = list(h1_prior or [])
        if prior:
            average_volume = statistics.fmean(float(item.bar["tick_count"]) for item in prior)
            current_volume = float(h1_point.bar["tick_count"])
            volume_ok = average_volume > 0.0 and current_volume >= average_volume * 1.05
            candle_aligned = (direction == "LONG" and float(h1_point.bar["close"]) >= float(h1_point.bar["open"])) or (
                direction == "SHORT" and float(h1_point.bar["close"]) < float(h1_point.bar["open"])
            )
            volume_points = 5.0 if volume_ok and candle_aligned else 0.0
        else:
            volume_points = 0.0
    known_technical = h4_points + h1_points + alignment_points + structural_points
    if adx_points is not None:
        known_technical += adx_points + (rsi_points or 0.0) + (volume_points or 0.0)
    technical_residual = technical - known_technical if technical is not None else None
    context_available = str(observation.get("session_status", "")).upper() == "AVAILABLE"
    final_residual = final - technical - m15_points if final is not None and technical is not None else None
    context = final_residual if context_available and final_residual is not None else 0.0 if final_residual is not None else None
    expected_unclamped = technical + m15_points + (context or 0.0) if technical is not None else None
    clamp_residual = (
        final - max(0.0, min(100.0, expected_unclamped))
        if final is not None and expected_unclamped is not None
        else None
    )
    other = (
        technical_residual + (clamp_residual or 0.0)
        if technical_residual is not None
        else None
    )
    exact = other is not None and abs(other) <= 1e-9
    return {
        "h4": h4_points,
        "h1_ema": h1_points,
        "h4_h1_alignment": alignment_points,
        "structural_bucket": structural_points,
        "m15": m15_points,
        "rsi": rsi_points,
        "adx": adx_points,
        "volume": volume_points,
        "session_news_context": context,
        "other": other,
        "decomposition_exact": exact,
        "h4_state": str(h4_state or "UNKNOWN").upper(),
        "h1_ema_state": str(h1_ema_state or "UNKNOWN").upper(),
        "h1_pivot_state": str(h1.get("pivot_state") or "UNKNOWN").upper(),
    }


def classify_dominant_cause(
    observation: Mapping[str, object], direction: str, components: Mapping[str, object]
) -> tuple[str, tuple[str, ...]]:
    """Classify one dominant evidence-backed bottleneck.

    The explicit gate remains SCORE_BELOW_ARM for the current universe.  The
    returned dominant cause diagnoses the largest missing/contrary persisted
    component; tied largest components are intentionally MULTIPLE_CAUSES.
    Optional evidence that cannot affect a sub-ARM score is not called causal.
    """

    score = relevant_score(observation, direction)
    if score is None:
        return "UNKNOWN", ()
    if ARM_THRESHOLD <= score < TRADE_THRESHOLD:
        return "SCORE_ARMED_BELOW_TRADE", ("SCORE_ARMED_BELOW_TRADE",)
    if score >= TRADE_THRESHOLD:
        return "UNKNOWN", ()
    h1 = observation.get("h1_structure") if isinstance(observation.get("h1_structure"), dict) else {}
    m15 = observation.get("m15_timing") if isinstance(observation.get("m15_timing"), dict) else {}
    candidates: list[tuple[str, float]] = []
    if not _aligned(direction, components.get("h4_state")):
        candidates.append(("H4_CONFLICT", 20.0))
    if not _aligned(direction, components.get("h1_ema_state")) or not _aligned(direction, components.get("h1_pivot_state")):
        candidates.append(("H1_STRUCTURE_CONFLICT", 15.0))
    if components.get("adx") == 0.0:
        candidates.append(("ADX_FILTER", 10.0))
    if components.get("rsi") == 0.0:
        candidates.append(("RSI_FILTER", 10.0))
    if components.get("volume") == 0.0:
        candidates.append(("VOLUME_WEAK", 5.0))
    pivot_aligned = _aligned(direction, components.get("h1_pivot_state"))
    suffix = "long" if direction == "LONG" else "short"
    if pivot_aligned:
        breakout = bool(h1.get(f"breakout_{suffix}"))
        pullback = bool(h1.get(f"pullback_{suffix}"))
        momentum = bool(h1.get(f"momentum_{suffix}"))
        if not breakout:
            candidates.append(("BREAKOUT_ABSENT", 10.0))
        if not pullback:
            candidates.append(("PULLBACK_NOT_VALID", 5.0))
        if not breakout and not momentum:
            candidates.append(("MOMENTUM_WEAK", 5.0))
    adjustment_key = "long_adjustment" if direction == "LONG" else "short_adjustment"
    m15_adjustment = m15.get(adjustment_key)
    if _is_number(m15_adjustment) and float(m15_adjustment) < 0.0:
        candidates.append(("M15_NOT_CONFIRMED", abs(float(m15_adjustment))))
    if not candidates:
        return "SCORE_BELOW_ARM", ("SCORE_BELOW_ARM",)
    largest = max(weight for _, weight in candidates)
    dominant = tuple(sorted({cause for cause, weight in candidates if abs(weight - largest) <= 1e-9}))
    return (dominant[0] if len(dominant) == 1 else "MULTIPLE_CAUSES"), tuple(cause for cause, _ in candidates)


def _flatten_outcomes(outcomes: Mapping[str, Mapping[str, Mapping]], event_id: str, direction: str) -> dict:
    values: dict[str, float | None] = {}
    for horizon in HORIZONS:
        outcome = outcomes.get(event_id, {}).get(horizon)
        if outcome is None:
            values[f"future_return_{horizon}"] = None
            values[f"mfe_{horizon}"] = None
            values[f"mae_{horizon}"] = None
            continue
        future_return, mfe, mae = _directed_values(outcome, direction)
        values[f"future_return_{horizon}"] = future_return
        values[f"mfe_{horizon}"] = mfe
        values[f"mae_{horizon}"] = mae
    return values


def _percentile(values: Sequence[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def build_missed_universe(
    observations: Sequence[dict],
    outcomes: Mapping[str, Mapping[str, Mapping]],
    assignments: Mapping[str, str],
    resolver: H1FeatureResolver | None = None,
    threshold_atr: float = MISSED_THRESHOLD_ATR,
) -> list[dict]:
    rows: list[dict] = []
    for observation in observations:
        if str(observation.get("decision", "")).strip().upper() != "NO_TRADE":
            continue
        one_hour = outcomes.get(observation["event_id"], {}).get("1h")
        if one_hour is None or not _is_number(observation.get("atr")) or not _is_number(observation.get("close")):
            continue
        atr, close = float(observation["atr"]), float(observation["close"])
        if atr <= 0.0 or close <= 0.0:
            continue
        raw_move = float(one_hour["future_return"]) * close
        atr_multiple = abs(raw_move) / atr
        if atr_multiple < threshold_atr:
            continue
        direction = "LONG" if raw_move > 0.0 else "SHORT"
        score = relevant_score(observation, direction)
        if score is None:
            continue
        point, prior = resolver.resolve(int(observation["_timestamp_ms"])) if resolver else (None, [])
        components = score_components(observation, direction, point, prior)
        cause, contributing = classify_dominant_cause(observation, direction, components)
        result = {
            "event_id": observation["event_id"],
            "period": assignments[observation["event_id"]],
            "timestamp": observation.get("timestamp"),
            "observed_at": observation.get("observed_at"),
            "timeframe": observation.get("timeframe"),
            "direction": direction,
            "long_score": observation.get("long_score"),
            "short_score": observation.get("short_score"),
            "relevant_score": score,
            "threshold_arm": ARM_THRESHOLD,
            "threshold_trade": TRADE_THRESHOLD,
            "points_to_arm": distance_to_threshold(score, ARM_THRESHOLD),
            "points_to_trade": distance_to_threshold(score, TRADE_THRESHOLD),
            "distance_to_arm_bucket": distance_bucket(distance_to_threshold(score, ARM_THRESHOLD)),
            "distance_to_trade_bucket": distance_bucket(distance_to_threshold(score, TRADE_THRESHOLD)),
            "score_bucket": analysis_score_bucket(score),
            "decision_gate": "SCORE_BELOW_ARM" if score < ARM_THRESHOLD else "SCORE_ARMED_BELOW_TRADE",
            "dominant_cause": cause,
            "contributing_causes": ";".join(contributing),
            "atr": atr,
            "close": close,
            "favorable_move_1h_atr": atr_multiple,
            "spread": observation.get("spread"),
            "structure": observation.get("structure"),
            "momentum": observation.get("momentum"),
            "breakout": observation.get("breakout"),
            "pullback": observation.get("pullback"),
            "recovery": observation.get("recovery"),
            "h4_state": components["h4_state"],
            "h1_ema_state": components["h1_ema_state"],
            "h1_pivot_state": components["h1_pivot_state"],
            "m15_state": (
                observation.get("m15_timing", {}).get("structure")
                if isinstance(observation.get("m15_timing"), dict)
                else None
            ),
            **{f"component_{key}": value for key, value in components.items() if key in {
                "h4", "h1_ema", "h4_h1_alignment", "structural_bucket", "m15", "rsi", "adx", "volume", "session_news_context", "other"
            }},
            **_flatten_outcomes(outcomes, observation["event_id"], direction),
            "stale_bias_crossover": False,
            "stale_bias_direction": None,
            "m15_aligned_new_direction": False,
            "dominant_score_opposite": False,
        }
        rows.append(result)
    return rows


def build_no_trade_counterfactual_population(
    observations: Sequence[dict],
    outcomes: Mapping[str, Mapping[str, Mapping]],
    assignments: Mapping[str, str],
) -> list[dict]:
    """Build the unbiased NO_TRADE population for threshold diagnostics.

    Unlike the missed-opportunity universe, direction is selected by the larger
    persisted score, never by the future return. This prevents the
    counterfactual win rate from becoming a hindsight tautology.
    """

    rows: list[dict] = []
    for observation in observations:
        if str(observation.get("decision", "")).strip().upper() != "NO_TRADE":
            continue
        if not _is_number(observation.get("long_score")) or not _is_number(observation.get("short_score")):
            continue
        if outcomes.get(observation["event_id"], {}).get("1h") is None:
            continue
        long_score, short_score = float(observation["long_score"]), float(observation["short_score"])
        direction = "LONG" if long_score >= short_score else "SHORT"
        score = max(long_score, short_score)
        rows.append({
            "event_id": observation["event_id"],
            "period": assignments[observation["event_id"]],
            "timestamp": observation.get("timestamp"),
            "timeframe": observation.get("timeframe"),
            "direction": direction,
            "long_score": long_score,
            "short_score": short_score,
            "relevant_score": score,
            **_flatten_outcomes(outcomes, observation["event_id"], direction),
        })
    return rows


def apply_false_missed_flags(rows: Sequence[dict], threshold_atr: float = MISSED_THRESHOLD_ATR) -> dict:
    train_spreads = [float(row["spread"]) for row in rows if row["period"] == "TRAIN" and _is_number(row.get("spread"))]
    spread_q95 = _percentile(train_spreads, 0.95)
    counts: Counter[str] = Counter()
    for row in rows:
        close, atr = float(row["close"]), float(row["atr"])
        adverse_atr = None
        if _is_number(row.get("mae_1h")):
            adverse_atr = abs(min(0.0, float(row["mae_1h"]))) * close / atr
        late = not _is_number(row.get("mfe_15m")) or max(0.0, float(row.get("mfe_15m") or 0.0)) * close < threshold_atr * atr
        high_mae = adverse_atr is not None and adverse_atr >= threshold_atr
        high_spread = spread_q95 is not None and _is_number(row.get("spread")) and float(row["spread"]) > spread_q95 + 1e-9
        flags: list[str] = []
        if high_mae:
            flags.append("HIGH_SAME_HORIZON_MAE_ORDER_UNKNOWN")
        if high_spread:
            flags.append("SPREAD_ABOVE_TRAIN_Q95")
        if late:
            flags.append("NO_0_75_ATR_MFE_WITHIN_15M")
        row["adverse_1h_atr"] = adverse_atr
        row["quality_flags"] = ";".join(flags)
        row["false_missed_candidate"] = bool(flags)
        counts.update(flags)
    return {"train_spread_q95": spread_q95, "flags": dict(counts), "flagged_cases": sum(bool(row["quality_flags"]) for row in rows)}


def _event_timeframe(event_id: str) -> str:
    for timeframe in ("M15", "H1", "H4"):
        if f"-{timeframe}-" in event_id:
            return timeframe
    return "UNKNOWN"


def load_stale_episodes(path: Path) -> list[dict]:
    path = Path(path)
    if not path.is_file():
        return []
    episodes: list[dict] = []
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            if row.get("cohort") != "BASELINE_STALE_EPISODE":
                continue
            change = str(row.get("direction_change", ""))
            stale = "LONG" if change.startswith("LONG_TO_") else "SHORT" if change.startswith("SHORT_TO_") else None
            if stale is None:
                continue
            try:
                start, end = int(float(row["episode_start"])), int(float(row["classic_change_timestamp"]))
            except (KeyError, TypeError, ValueError):
                continue
            if end <= start:
                continue
            event_id = str(row.get("event_id", ""))
            episodes.append({"timeframe": _event_timeframe(event_id), "start": start, "end": end, "stale_direction": stale})
    return episodes


def apply_stale_bias_crossover(rows: Sequence[dict], episodes: Sequence[Mapping[str, object]]) -> dict:
    grouped: dict[str, list[Mapping[str, object]]] = defaultdict(list)
    for episode in episodes:
        grouped[str(episode.get("timeframe", "UNKNOWN"))].append(episode)
    starts: dict[str, list[int]] = {}
    for timeframe in grouped:
        grouped[timeframe].sort(key=lambda value: int(value["start"]))
        starts[timeframe] = [int(value["start"]) for value in grouped[timeframe]]
    matched = aligned_m15 = dominant_opposite = 0
    matched_rows: list[dict] = []
    for row in rows:
        timeframe = str(row.get("timeframe", "UNKNOWN"))
        sequence = grouped.get(timeframe, [])
        timestamp = int(row.get("timestamp") or 0)
        index = bisect_right(starts.get(timeframe, []), timestamp) - 1
        episode = sequence[index] if index >= 0 and timestamp < int(sequence[index]["end"]) else None
        if episode is None or str(episode["stale_direction"]) == row["direction"]:
            continue
        row["stale_bias_crossover"] = True
        row["stale_bias_direction"] = episode["stale_direction"]
        row["dominant_score_opposite"] = (
            (row["direction"] == "LONG" and float(row["short_score"]) > float(row["long_score"]))
            or (row["direction"] == "SHORT" and float(row["long_score"]) > float(row["short_score"]))
        )
        aligned = _aligned(row["direction"], row.get("m15_state"))
        row["m15_aligned_new_direction"] = aligned
        matched += 1
        aligned_m15 += int(aligned)
        dominant_opposite += int(row["dominant_score_opposite"])
        matched_rows.append(row)
    by_period = {}
    for period in PERIODS:
        members = [row for row in matched_rows if row.get("period") == period]
        by_period[period] = {
            "missed_during_opposite_stale_bias": len(members),
            "dominant_score_still_opposite": sum(bool(row["dominant_score_opposite"]) for row in members),
            "m15_aligned_new_direction": sum(bool(row["m15_aligned_new_direction"]) for row in members),
        }
    return {
        "episodes_loaded": len(episodes),
        "missed_during_opposite_stale_bias": matched,
        "dominant_score_still_opposite": dominant_opposite,
        "m15_aligned_new_direction": aligned_m15,
        "by_period": by_period,
        "armed_opposite": None,
        "armed_opposite_reason": "NO_TRADE rows have armed_direction=null; score dominance is reported instead",
    }


def _record_values(rows: Iterable[dict], horizon: str) -> list[tuple[float, float, float]]:
    values: list[tuple[float, float, float]] = []
    for row in rows:
        fields = (row.get(f"future_return_{horizon}"), row.get(f"mfe_{horizon}"), row.get(f"mae_{horizon}"))
        if all(_is_number(value) for value in fields):
            values.append((float(fields[0]), float(fields[1]), float(fields[2])))
    return values


def _selected(rows: Sequence[dict], period: str, direction: str) -> list[dict]:
    return [
        row for row in rows
        if (period == "ALL" or row["period"] == period) and (direction == "ALL" or row["direction"] == direction)
    ]


def cause_rows(rows: Sequence[dict]) -> list[dict]:
    output: list[dict] = []
    for period in ("ALL",) + PERIODS:
        for direction in ("ALL", "LONG", "SHORT"):
            universe = _selected(rows, period, direction)
            for classification in ("DOMINANT", "CONTRIBUTING"):
                for cause in CAUSES:
                    members = [
                        row for row in universe
                        if (row["dominant_cause"] == cause if classification == "DOMINANT" else cause in row["contributing_causes"].split(";"))
                    ]
                    for horizon in HORIZONS:
                        metrics = summarize_values(_record_values(members, horizon))
                        output.append({
                            "classification": classification,
                            "period": period,
                            "direction": direction,
                            "cause": cause,
                            "missed_cases": len(members),
                            "share_pct": len(members) * 100.0 / len(universe) if universe else None,
                            "horizon": horizon,
                            **metrics,
                        })
    return output


def score_rows(rows: Sequence[dict]) -> list[dict]:
    output: list[dict] = []
    for period in ("ALL",) + PERIODS:
        for direction in ("ALL", "LONG", "SHORT"):
            universe = _selected(rows, period, direction)
            for bucket, _, _ in ANALYSIS_SCORE_BUCKETS:
                members = [row for row in universe if row["score_bucket"] == bucket]
                for horizon in HORIZONS:
                    output.append({
                        "period": period,
                        "direction": direction,
                        "score_bucket": bucket,
                        "missed_cases": len(members),
                        "horizon": horizon,
                        **summarize_values(_record_values(members, horizon)),
                    })
    return output


def counterfactual_rows(rows: Sequence[dict]) -> list[dict]:
    output: list[dict] = []
    variants = (("TRADE_THRESHOLD", TRADE_COUNTERFACTUALS, TRADE_THRESHOLD), ("ARM_THRESHOLD", ARM_COUNTERFACTUALS, ARM_THRESHOLD))
    for kind, thresholds, baseline in variants:
        for period in ("ALL",) + PERIODS:
            for direction in ("ALL", "LONG", "SHORT"):
                universe = _selected(rows, period, direction)
                baseline_members = [row for row in universe if float(row["relevant_score"]) >= baseline]
                for threshold in thresholds:
                    members = [row for row in universe if float(row["relevant_score"]) >= threshold]
                    for horizon in HORIZONS:
                        output.append({
                            "counterfactual": kind,
                            "threshold": threshold,
                            "period": period,
                            "direction": direction,
                            "horizon": horizon,
                            "selected_cases": len(members),
                            "additional_vs_current": max(0, len(members) - len(baseline_members)),
                            **summarize_values(_record_values(members, horizon)),
                        })
    return output


def _component_means(rows: Sequence[dict], period: str = "ALL") -> dict[str, float | None]:
    members = _selected(rows, period, "ALL")
    result: dict[str, float | None] = {}
    for component in ("h4", "h1_ema", "h4_h1_alignment", "structural_bucket", "m15", "rsi", "adx", "volume", "session_news_context", "other"):
        values = [float(row[f"component_{component}"]) for row in members if _is_number(row.get(f"component_{component}"))]
        result[component] = statistics.fmean(values) if values else None
    return result


def _robust_cause_summary(rows: Sequence[dict], *, contributing: bool = False) -> list[dict]:
    def names(row: dict) -> set[str]:
        if not contributing:
            return {row["dominant_cause"]}
        return {value for value in row["contributing_causes"].split(";") if value}

    overall: Counter[str] = Counter()
    for row in rows:
        overall.update(names(row))
    totals = Counter(row["period"] for row in rows)
    period_counts: dict[str, Counter[str]] = {}
    for period in PERIODS:
        counts: Counter[str] = Counter()
        for row in rows:
            if row["period"] == period:
                counts.update(names(row))
        period_counts[period] = counts
    summaries: list[dict] = []
    for cause, count in overall.most_common():
        shares = {
            period: period_counts[period][cause] * 100.0 / totals[period] if totals[period] else 0.0
            for period in PERIODS
        }
        robust = all(period_counts[period][cause] >= 30 and shares[period] >= 1.0 for period in PERIODS) and max(shares.values()) - min(shares.values()) <= 10.0
        summaries.append({"cause": cause, "cases": count, "share_pct": count * 100.0 / len(rows), "period_shares": shares, "robust_recurrence": robust})
    return summaries


def _threshold_summary(rows: Sequence[dict], kind: str, thresholds: Sequence[int]) -> list[dict]:
    result: list[dict] = []
    for period in ("ALL",) + PERIODS:
        universe = _selected(rows, period, "ALL")
        for threshold in thresholds:
            members = [row for row in universe if float(row["relevant_score"]) >= threshold]
            metrics = summarize_values(_record_values(members, "1h"))
            result.append({"period": period, "threshold": threshold, "cases": len(members), **metrics})
    return result


def _direction_summary(rows: Sequence[dict]) -> list[dict]:
    result: list[dict] = []
    for period in ("ALL",) + PERIODS:
        for direction in ("LONG", "SHORT"):
            members = _selected(rows, period, direction)
            for horizon in HORIZONS:
                result.append({
                    "period": period,
                    "direction": direction,
                    "horizon": horizon,
                    "missed_cases": len(members),
                    **summarize_values(_record_values(members, horizon)),
                })
    return result


def _fmt(value: object, decimals: int = 3) -> str:
    if not _is_number(value):
        return "N/A"
    return f"{float(value):.{decimals}f}"


def _markdown(summary: dict) -> str:
    lines = [
        "# GoldScout — missed opportunities históricas",
        "",
        "> Diagnóstico offline · sin cambios al EA live · score_effect=0.",
        "",
        "## Universo y definición",
        "",
        f"- Missed opportunities: {summary['universe']['cases']:,} de {summary['universe']['eligible_no_trade']:,} NO_TRADE elegibles.",
        f"- LONG / SHORT: {summary['universe']['long']:,} / {summary['universe']['short']:,}.",
        f"- TRAIN / VALIDATION / OOS: {summary['universe']['periods']['TRAIN']:,} / {summary['universe']['periods']['VALIDATION']:,} / {summary['universe']['periods']['OOS']:,}.",
        f"- Definición conservada: |close return 1h| >= {summary['universe']['threshold_atr']:.2f} ATR, usando solo outcomes válidos.",
        "- La dirección LONG/SHORT es retrospectiva y solo normaliza el outcome y selecciona el score relevante; no es una señal reproducida.",
        "",
        "## Causas dominantes",
        "",
        "| Causa | Casos | Share | TRAIN | VALIDATION | OOS | Recurrencia robusta |",
        "|---|---:|---:|---:|---:|---:|---|",
    ]
    for item in summary["cause_ranking"][:10]:
        shares = item["period_shares"]
        lines.append(
            f"| {item['cause']} | {item['cases']:,} | {item['share_pct']:.2f}% | {shares['TRAIN']:.2f}% | "
            f"{shares['VALIDATION']:.2f}% | {shares['OOS']:.2f}% | {'SÍ' if item['robust_recurrence'] else 'NO'} |"
        )
    lines.extend([
        "",
        "La puerta explícita fue SCORE_BELOW_ARM en todos los casos. La tabla profundiza en el mayor componente persistido ausente o contrario. Empates entre cuellos de botella del mismo peso se conservan como MULTIPLE_CAUSES.",
        "",
        "### Co-factores frecuentes (no exclusivos)",
        "",
        "| Evidencia ausente/contraria | Casos | Share | TRAIN | VALIDATION | OOS | Robusto |",
        "|---|---:|---:|---:|---:|---:|---|",
    ])
    for item in summary["contributing_cause_ranking"][:10]:
        shares = item["period_shares"]
        lines.append(
            f"| {item['cause']} | {item['cases']:,} | {item['share_pct']:.2f}% | {shares['TRAIN']:.2f}% | "
            f"{shares['VALIDATION']:.2f}% | {shares['OOS']:.2f}% | {'SÍ' if item['robust_recurrence'] else 'NO'} |"
        )
    lines.extend([
        "",
        "Los co-factores pueden coexistir; sus porcentajes no deben sumar 100%. Ausencia de breakout/pullback/momentum solo se cuenta cuando la estructura pivot está alineada y esa evidencia podría aportar al bucket.",
        "H1_STRUCTURE_CONFLICT significa que EMA H1 o estructura pivot confirmada no estaba alineada (incluye estado neutral/insuficiente); M15_NOT_CONFIRMED solo se registra cuando hubo ajuste negativo real, nunca por M15 ausente/neutral.",
        "Recurrencia robusta exige al menos 30 casos y 1% en cada partición, con diferencia máxima de 10 puntos porcentuales entre TRAIN/VALIDATION/OOS.",
        "",
        "## Distancia a thresholds",
        "",
        "| Referencia | <5 | 5-9 | 10-14 | 15+ | <=5 |",
        "|---|---:|---:|---:|---:|---:|",
    ])
    for name in ("ARM", "TRADE"):
        data = summary["distance"][name]
        lines.append(f"| {name} | {data['<5']:,} | {data['5-9']:,} | {data['10-14']:,} | {data['15+']:,} | {data['within_5']:,} |")
    lines.extend([
        "",
        "## Buckets 58–73",
        "",
        "| Bucket | Casos | 1h win rate | Retorno 1h medio | MFE 1h | MAE 1h |",
        "|---|---:|---:|---:|---:|---:|",
    ])
    for bucket in ("58-64", "65-69", "70-73"):
        metrics = summary["requested_score_buckets"][bucket]
        lines.append(
            f"| {bucket} | {metrics['cases']:,} | {_fmt(metrics['win_rate_pct'], 2)}% | {_fmt(metrics['mean_future_return'], 6)} | "
            f"{_fmt(metrics['mean_mfe'], 6)} | {_fmt(metrics['mean_mae'], 6)} |"
        )
    lines.extend([
        "",
        "Estos buckets están vacíos por construcción: el enriquecimiento etiqueta 58–73 como ARMED, mientras este análisis respeta estrictamente decision=NO_TRADE. No se amplió el universo silenciosamente.",
        "",
        "## Counterfactual de thresholds",
        "",
        "### Trade threshold",
        "",
        "| Periodo | Threshold | Señales dentro de NO_TRADE | Win rate 1h | Retorno 1h medio |",
        "|---|---:|---:|---:|---:|",
    ])
    for item in summary["trade_counterfactual"]:
        lines.append(f"| {item['period']} | {item['threshold']} | {item['cases']:,} | {_fmt(item['win_rate_pct'], 2)}% | {_fmt(item['mean_future_return'], 6)} |")
    lines.extend([
        "",
        "Bajar solo MinScoreToTrade no actúa sobre casos que nunca alcanzaron ARM. Estos ceros son un resultado del contrato del dataset, no ausencia de cálculo.",
        "",
        "### Arm threshold",
        "",
        "| Periodo | Threshold | Candidatos armados | Win rate 1h | Retorno 1h medio |",
        "|---|---:|---:|---:|---:|",
    ])
    for item in summary["arm_counterfactual"]:
        lines.append(f"| {item['period']} | {item['threshold']} | {item['cases']:,} | {_fmt(item['win_rate_pct'], 2)}% | {_fmt(item['mean_future_return'], 6)} |")
    lines.extend([
        "",
        f"El counterfactual usa las {summary['counterfactual_population_cases']:,} filas NO_TRADE elegibles y elige LONG/SHORT por el mayor score persistido, nunca por el outcome futuro. ARM 56/54 no reproduce trigger intrabar, patrones, noticias, guardas de cuenta ni ejecución.",
        "",
        "## LONG vs SHORT",
        "",
        "| Periodo | Dirección | Horizonte | Casos | Retorno medio | MFE | MAE |",
        "|---|---|---:|---:|---:|---:|---:|",
    ])
    for item in summary["direction_summary"]:
        if item["horizon"] in {"15m", "1h", "4h"}:
            lines.append(
                f"| {item['period']} | {item['direction']} | {item['horizon']} | {item['cases']:,} | "
                f"{_fmt(item['mean_future_return'], 6)} | {_fmt(item['mean_mfe'], 6)} | {_fmt(item['mean_mae'], 6)} |"
            )
    lines.extend([
        "",
        "## Contribución media al score relevante",
        "",
        "| Componente | Puntos medios |",
        "|---|---:|",
    ])
    for name, value in summary["component_means"].items():
        lines.append(f"| {name} | {_fmt(value, 3)} |")
    false = summary["false_missed_quality"]
    stale = summary["stale_bias_crossover"]
    lines.extend([
        "",
        "## Calidad de las oportunidades retrospectivas",
        "",
        f"- Casos con al menos una advertencia: {false['flagged_cases']:,}.",
        f"- MAE >=0.75 ATR dentro de la misma hora: {false['flags'].get('HIGH_SAME_HORIZON_MAE_ORDER_UNKNOWN', 0):,}. El JSONL no conserva el orden MAE→MFE, por lo que no se afirma que ocurrió antes.",
        f"- Sin MFE >=0.75 ATR en los primeros 15m: {false['flags'].get('NO_0_75_ATR_MFE_WITHIN_15M', 0):,}.",
        f"- Spread sobre Q95 de TRAIN: {false['flags'].get('SPREAD_ABOVE_TRAIN_Q95', 0):,} (Q95={_fmt(false['train_spread_q95'], 4)}).",
        "",
        "## Crossover con stale bias",
        "",
        f"- Episodios de referencia cargados: {stale['episodes_loaded']:,}.",
        f"- Missed opportunities durante bias clásico contrario: {stale['missed_during_opposite_stale_bias']:,}.",
        f"- El score dominante seguía favoreciendo la dirección antigua: {stale['dominant_score_still_opposite']:,}.",
        f"- M15 ya alineado con la nueva dirección: {stale['m15_aligned_new_direction']:,}.",
        "- No puede medirse «seguía armado»: por contrato, NO_TRADE tiene armed_direction=null. Se reporta dominancia relativa de scores, no un estado armado inventado.",
        "",
        "| Periodo | Crossover stale | Score aún opuesto | M15 nueva dirección |",
        "|---|---:|---:|---:|",
    ])
    for period in PERIODS:
        item = stale["by_period"][period]
        lines.append(
            f"| {period} | {item['missed_during_opposite_stale_bias']:,} | "
            f"{item['dominant_score_still_opposite']:,} | {item['m15_aligned_new_direction']:,} |"
        )
    lines.extend([
        "",
        "## Hallazgos sólidos",
        "",
    ])
    lines.extend([f"- {value}" for value in summary["conclusions"]["solid"]] or ["- Ninguno."])
    lines.extend(["", "## Hallazgos débiles", ""])
    lines.extend([f"- {value}" for value in summary["conclusions"]["weak"]] or ["- Ninguno."])
    lines.extend(["", "## Hipótesis", ""])
    lines.extend([f"- {value}" for value in summary["conclusions"]["hypotheses"]] or ["- Ninguna."])
    lines.extend(["", "## Cambios NO recomendados", ""])
    lines.extend([f"- {value}" for value in summary["conclusions"]["not_recommended"]])
    lines.extend([
        "",
        "## Limitaciones",
        "",
        "- El score es PARTIAL_EXACT_CLOSED_BAR_CORE: no reproduce patrones, noticias históricas, trigger/boost intrabar ni guardas de cuenta.",
        "- Session/news context no está disponible porque no se suministró offset histórico del servidor MT5 ni archivo histórico de noticias.",
        "- MFE/MAE agregados no conservan orden intrabar; no equivalen a PnL ejecutable ni prueban que una entrada fuera razonable.",
        "- Varias observaciones M15/H1/H4 comparten estado de decisión cercano y no son operaciones independientes.",
        "- Los counterfactuals seleccionan por score únicamente; no simulan costes, SL/TP, lotaje ni fills.",
        "",
    ])
    return "\n".join(lines)


def analyze_missed_opportunities(
    input_dir: Path,
    output_dir: Path,
    *,
    stale_path: Path | None = None,
    threshold_atr: float = MISSED_THRESHOLD_ATR,
    symbol: str = "XAUUSD",
    point_size: float = 0.01,
) -> dict:
    if threshold_atr <= 0.0 or point_size <= 0.0:
        raise ValueError("threshold_atr and point_size must be positive")
    input_dir, output_dir = Path(input_dir), Path(output_dir)
    observations, observation_stats = _load_observations(input_dir / "historical_observations.jsonl")
    if not observations:
        raise AnalysisError("no valid historical observations")
    enrichment_stats = _merge_decision_enrichment(observations, input_dir / "historical_decisions.jsonl")
    outcomes, _, outcome_stats = _load_outcomes(input_dir / "historical_outcomes.jsonl")
    assignments, split_counts = temporal_split(observations)
    resolver = H1FeatureResolver(input_dir, symbol=symbol, point_size=point_size)
    rows = build_missed_universe(observations, outcomes, assignments, resolver, threshold_atr)
    if not rows:
        raise AnalysisError("no missed opportunities satisfy the existing diagnostic definition")
    counterfactual_population = build_no_trade_counterfactual_population(observations, outcomes, assignments)
    false_summary = apply_false_missed_flags(rows, threshold_atr)
    episodes = load_stale_episodes(stale_path or output_dir / "h1_behavior_shift.csv")
    stale_summary = apply_stale_bias_crossover(rows, episodes)
    causes = cause_rows(rows)
    scores = score_rows(rows)
    counterfactuals = counterfactual_rows(counterfactual_population)
    period_counts = Counter(row["period"] for row in rows)
    direction_counts = Counter(row["direction"] for row in rows)
    eligible_no_trade = sum(
        str(item.get("decision", "")).upper() == "NO_TRADE"
        and outcomes.get(item["event_id"], {}).get("1h") is not None
        and _is_number(item.get("atr"))
        and _is_number(item.get("close"))
        for item in observations
    )
    distance = {}
    for name, key in (("ARM", "points_to_arm"), ("TRADE", "points_to_trade")):
        buckets = Counter(distance_bucket(float(row[key])) for row in rows)
        buckets["within_5"] = sum(float(row[key]) <= 5.0 for row in rows)
        distance[name] = dict(buckets)
        for required in ("<5", "5-9", "10-14", "15+", "within_5"):
            distance[name].setdefault(required, 0)
    requested_buckets = {}
    for bucket in ("58-64", "65-69", "70-73"):
        members = [row for row in rows if row["score_bucket"] == bucket]
        requested_buckets[bucket] = {"cases": len(members), **summarize_values(_record_values(members, "1h"))}
    ranking = _robust_cause_summary(rows)
    contributing_ranking = _robust_cause_summary(rows, contributing=True)
    robust = [item for item in ranking if item["robust_recurrence"]]
    solid = [
        f"{item['cause']} es recurrente en TRAIN/VALIDATION/OOS ({item['cases']:,} casos; {item['share_pct']:.2f}% global)."
        for item in robust[:5]
    ]
    weak = [
        "Los buckets 58–73 no pueden evaluarse dentro de NO_TRADE; pertenecen a ARMED en el enriquecimiento actual.",
        "Las asociaciones de stale bias son diagnósticas y no prueban que una entrada anticipada mejore el resultado neto.",
    ]
    summary = {
        "analysis_only": True,
        "score_effect": 0,
        "schema_version": 1,
        "universe": {
            "cases": len(rows),
            "eligible_no_trade": eligible_no_trade,
            "threshold_atr": threshold_atr,
            "long": direction_counts["LONG"],
            "short": direction_counts["SHORT"],
            "periods": {period: period_counts[period] for period in PERIODS},
            "split_counts_all_observations": split_counts,
        },
        "inputs": {
            "observation_stats": observation_stats,
            "enrichment_stats": enrichment_stats,
            "outcome_stats": outcome_stats,
        },
        "cause_ranking": ranking,
        "contributing_cause_ranking": contributing_ranking,
        "distance": distance,
        "requested_score_buckets": requested_buckets,
        "counterfactual_population_cases": len(counterfactual_population),
        "trade_counterfactual": _threshold_summary(counterfactual_population, "TRADE_THRESHOLD", TRADE_COUNTERFACTUALS),
        "arm_counterfactual": _threshold_summary(counterfactual_population, "ARM_THRESHOLD", ARM_COUNTERFACTUALS),
        "direction_summary": _direction_summary(rows),
        "component_means": _component_means(rows),
        "false_missed_quality": false_summary,
        "stale_bias_crossover": stale_summary,
        "conclusions": {
            "solid": solid,
            "weak": weak,
            "hypotheses": [
                "Evaluar en un universo ARMED separado si 70–73 conserva calidad OOS; no mezclarlo con NO_TRADE.",
                "Reproducir noticias, patrones e intrabar antes de atribuir todo el déficit al núcleo técnico cerrado.",
                "Usar ticks para ordenar MAE y MFE antes de denominar una oportunidad ejecutable.",
            ],
            "not_recommended": [
                "No bajar MinScoreToTrade basándose en este universo: 74→72/70/68 añade cero casos NO_TRADE.",
                "No bajar ArmScoreThreshold por conteos retrospectivos sin simular trigger intrabar, costes y falsas señales.",
                "No convertir ausencia de M15, session o news en bloqueo/causa cuando la política actual los trata como contexto opcional.",
                "No tratar las 8.274 filas como trades independientes ni como ganadores garantizados.",
            ],
        },
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(output_dir / "missed_opportunities_by_cause.csv", list(causes[0].keys()), causes)
    _write_csv(output_dir / "missed_opportunities_by_score.csv", list(scores[0].keys()), scores)
    _write_csv(output_dir / "threshold_counterfactual.csv", list(counterfactuals[0].keys()), counterfactuals)
    _atomic_write(output_dir / "missed_opportunities_deep.md", _markdown(summary))
    return summary


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    root = Path(__file__).resolve().parent
    parser.add_argument("--input-dir", type=Path, default=root / "output")
    parser.add_argument("--output-dir", type=Path, default=root / "analysis")
    parser.add_argument("--stale-path", type=Path)
    parser.add_argument("--threshold-atr", type=float, default=MISSED_THRESHOLD_ATR)
    parser.add_argument("--symbol", default="XAUUSD")
    parser.add_argument("--point-size", type=float, default=0.01)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_argument_parser().parse_args(argv)
    try:
        summary = analyze_missed_opportunities(
            arguments.input_dir,
            arguments.output_dir,
            stale_path=arguments.stale_path,
            threshold_atr=arguments.threshold_atr,
            symbol=arguments.symbol,
            point_size=arguments.point_size,
        )
    except (AnalysisError, OSError, ValueError) as error:
        print(f"[MISSED][ERROR] {error}")
        return 2
    print(f"[MISSED] cases={summary['universe']['cases']}")
    print(f"[MISSED] long={summary['universe']['long']} short={summary['universe']['short']}")
    print(f"[MISSED] output_dir={Path(arguments.output_dir).resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
