"""Causal, offline Entry Quality features; no trading or score integration.

The dataset is a diagnostic sidecar keyed by the existing historical event_id.
It consumes *frozen* Gold Regime V1 labels, closed bars and observations only.
Historical outcomes are never read here.
"""

from __future__ import annotations

import argparse
from bisect import bisect_right
from collections import Counter, deque
from dataclasses import dataclass
import json
import math
from pathlib import Path
import statistics
from typing import Mapping, Sequence

from research.analyze_regime_edge import causal_direction, causal_setups
from research.gold_regime_engine import (CausalFrame, LookAheadError, PARAMETER_VERSION, SOURCE,
                                          _file_sha256, _load_m5, _read_jsonl,
                                          wrap_higher_bars)


VERSION = "entry-quality-v1"
FRESH_LIMIT_MINUTES = {"m5": 15, "h1": 120, "h4": 480}
STALE_STUDY_MINUTES = {"h1": (120, 240, 480), "h4": (480, 720, 1440)}
SETUP_PRIORITY = ("BREAKOUT", "MOMENTUM", "RECOVERY", "PULLBACK")
BAR_MINUTES = {"m5": 5, "h1": 60, "h4": 240}


def _finite(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _safe_ratio(numerator: float | None, denominator: float | None) -> float | None:
    return numerator / denominator if numerator is not None and denominator is not None and denominator > 0 else None


def context_freshness(context: Mapping[str, object], decision_time: int, timeframe: str) -> dict:
    if timeframe not in FRESH_LIMIT_MINUTES:
        raise ValueError("unsupported timeframe")
    if not context:
        return {"context_age_minutes": None, "context_age_bars": None,
                "freshness_state": "UNKNOWN", "stale_thresholds_exceeded": []}
    end, available = context.get("end"), context.get("available_at")
    if not isinstance(end, int) or not isinstance(available, int) or end > available or available > decision_time:
        raise LookAheadError(f"open or unavailable {timeframe} context")
    age = (decision_time - end) / 60_000
    thresholds = STALE_STUDY_MINUTES.get(timeframe, ())
    return {"context_age_minutes": age,
            "context_age_bars": age / BAR_MINUTES[timeframe],  # elapsed-time equivalents, not invented bars
            "freshness_state": "STALE" if age > FRESH_LIMIT_MINUTES[timeframe] else "FRESH",
            "stale_thresholds_exceeded": [int(value) for value in thresholds if age > value]}


@dataclass(frozen=True)
class ConfirmedPivot:
    kind: str
    price: float
    bar_index: int
    available_at: int
    source_bar_id: str
    confirmation_bar_id: str


def confirmed_pivots(frames: Sequence[CausalFrame]) -> list[ConfirmedPivot]:
    """2-left/2-right; candidate is not visible before right bar #2 closes."""
    pivots: list[ConfirmedPivot] = []
    for center in range(2, len(frames) - 2):
        surrounding = (center - 2, center - 1, center + 1, center + 2)
        high = all(frames[center].high > frames[i].high for i in surrounding)
        low = all(frames[center].low < frames[i].low for i in surrounding)
        if high == low:
            continue
        confirming = frames[center + 2]
        for index in (center - 2, center - 1, center, center + 1, center + 2):
            frames[index].require(confirming.available_at)
        pivots.append(ConfirmedPivot("HIGH" if high else "LOW",
                                     frames[center].high if high else frames[center].low,
                                     center, confirming.available_at,
                                     frames[center].bar_id, confirming.bar_id))
    return pivots


def available_pivots(pivots: Sequence[ConfirmedPivot], decision_time: int,
                     current_h1_index: int, lookback_bars: int = 30) -> list[ConfirmedPivot]:
    if current_h1_index < 0:
        return []
    # Confirmation timestamps are chronological; inspect only the recent tail.
    end = bisect_right(pivots, decision_time, key=lambda pivot: pivot.available_at)
    return [pivot for pivot in pivots[max(0, end - 80):end]
            if current_h1_index - lookback_bars < pivot.bar_index <= current_h1_index]


def structural_levels(pivots: Sequence[ConfirmedPivot], entry: float,
                      direction: str) -> dict:
    lows = [pivot for pivot in pivots if pivot.kind == "LOW" and pivot.price < entry]
    highs = [pivot for pivot in pivots if pivot.kind == "HIGH" and pivot.price > entry]
    support = max(lows, key=lambda pivot: pivot.price) if lows else None
    resistance = min(highs, key=lambda pivot: pivot.price) if highs else None
    latest_low = max((pivot for pivot in pivots if pivot.kind == "LOW"),
                     key=lambda pivot: pivot.bar_index, default=None)
    latest_high = max((pivot for pivot in pivots if pivot.kind == "HIGH"),
                      key=lambda pivot: pivot.bar_index, default=None)
    if direction == "LONG":
        obstacle = resistance
        origin = latest_low
        invalidation = support
        room = resistance.price - entry if resistance else None
        stop_distance = entry - support.price if support else None
        impulse = entry - latest_low.price if latest_low else None
    elif direction == "SHORT":
        obstacle = support
        origin = latest_high
        invalidation = resistance
        room = entry - support.price if support else None
        stop_distance = resistance.price - entry if resistance else None
        impulse = latest_high.price - entry if latest_high else None
    else:
        obstacle = origin = invalidation = None
        room = stop_distance = impulse = None
    return {"nearest_support": support.price if support else None,
            "nearest_resistance": resistance.price if resistance else None,
            "obstacle_pivot_id": obstacle.source_bar_id if obstacle else None,
            "impulse_origin_price": origin.price if origin else None,
            "impulse_origin_pivot_id": origin.source_bar_id if origin else None,
            "invalidation_pivot_id": invalidation.source_bar_id if invalidation else None,
            "room_to_obstacle": room, "entry_to_structural_invalidation": stop_distance,
            "impulse_distance": impulse}


def range_position(frames: Sequence[CausalFrame], index: int, entry: float,
                   length: int) -> tuple[float | None, float | None, float | None]:
    if index < length - 1:
        return None, None, None
    window = frames[index - length + 1:index + 1]
    high = max(bar.high for bar in window)
    low = min(bar.low for bar in window)
    position = min(1.0, max(0.0, (entry - low) / (high - low))) if high > low else None
    return position, high, low


def directional_returns(frames: Sequence[CausalFrame], index: int, direction: str) -> dict:
    sign = 1 if direction == "LONG" else -1 if direction == "SHORT" else 0
    values: dict[str, float | None] = {}
    for bars in (1, 3, 5):
        old = frames[index - bars].close if index >= bars else None
        values[f"recent_return_{bars}"] = sign * (frames[index].close / old - 1) if old and sign else None
    acceleration = (values["recent_return_1"] - values["recent_return_3"] / 3
                    if values["recent_return_1"] is not None and values["recent_return_3"] is not None else None)
    values["momentum_acceleration"] = acceleration
    values["momentum_deceleration"] = max(0.0, -acceleration) if acceleration is not None else None
    return values


def _ema_atr(frames: Sequence[CausalFrame]) -> list[tuple[float | None, float | None]]:
    ema: float | None = None
    true_ranges: deque[float] = deque(maxlen=14)
    result = []
    for index, bar in enumerate(frames):
        previous = frames[index - 1].close if index else bar.close
        true_ranges.append(max(bar.high - bar.low, abs(bar.high - previous), abs(bar.low - previous)))
        ema = bar.close if ema is None else ema + 2 / 21 * (bar.close - ema)
        result.append((ema if index >= 19 else None,
                       sum(true_ranges) / 14 if len(true_ranges) == 14 else None))
    return result


def lower_tf_context(frames: Sequence[CausalFrame], ema_atr: Sequence[tuple[float | None, float | None]],
                     index: int, entry: float, direction: str) -> dict:
    empty = {"lower_tf_pullback": None, "lower_tf_reacceleration": None,
             "lower_tf_counter_momentum": None, "lower_tf_breakout_extension": None,
             "distance_from_last_pullback_atr": None}
    if index < 5 or direction not in {"LONG", "SHORT"}:
        return empty
    bar = frames[index]
    ema, atr = ema_atr[index]
    if ema is None or atr is None or atr <= 0:
        return empty
    sign = 1 if direction == "LONG" else -1
    start = max(0, index - 20)
    touched = []
    for i in range(start, index + 1):
        historical_ema, historical_atr = ema_atr[i]
        if historical_ema is None or historical_atr is None:
            continue
        if (direction == "LONG" and frames[i].low <= historical_ema + .25 * historical_atr or
                direction == "SHORT" and frames[i].high >= historical_ema - .25 * historical_atr):
            touched.append(i)
    pullback_index = touched[-1] if touched else None
    previous_ema = ema_atr[index - 1][0]
    reacceleration = (previous_ema is not None and
                      sign * (frames[index - 1].close - previous_ema) <= 0 and
                      sign * (bar.close - ema) > 0)
    counter = sign * (bar.close - frames[index - 3].close) < 0
    prior = frames[max(0, index - 10):index]
    breakout_distance = (bar.close - max(item.high for item in prior) if direction == "LONG" else
                         min(item.low for item in prior) - bar.close)
    return {"lower_tf_pullback": pullback_index is not None,
            "lower_tf_reacceleration": reacceleration,
            "lower_tf_counter_momentum": counter,
            "lower_tf_breakout_extension": breakout_distance > .5 * atr,
            "distance_from_last_pullback_atr": sign * (entry - frames[pullback_index].close) / atr
            if pullback_index is not None else None}


def percentile_rank(value: float, reference: Sequence[float]) -> float | None:
    if not reference:
        return None
    return bisect_right(reference, value) / len(reference)


def select_setup(observation: Mapping[str, object], direction: str | None) -> tuple[str | None, list[str]]:
    if direction is None:
        return None, []
    labels = causal_setups(observation, direction)
    return next((name for name in SETUP_PRIORITY if name in labels), None), labels


def _observations(path: Path) -> dict[str, dict]:
    result = {}
    for row in _read_jsonl(path):
        event_id = row.get("event_id")
        if not isinstance(event_id, str) or not event_id or event_id in result:
            raise ValueError("observation event_id missing or duplicated")
        if row.get("observer_only") is not True or row.get("score_effect") != 0 or row.get("source") != SOURCE:
            raise ValueError("observation is not diagnostic historical MT5 data")
        result[event_id] = row
    return result


def _validate_regime(regime: Mapping[str, object]) -> None:
    time = regime.get("decision_time")
    if (not isinstance(time, int) or regime.get("diagnostic_only") is not True or
            regime.get("score_effect") != 0 or regime.get("source") != SOURCE or
            regime.get("parameter_version") != PARAMETER_VERSION):
        raise ValueError("invalid regime contract")
    for tf in ("m5", "h1", "h4"):
        context = regime.get(tf) or {}
        context_freshness(context, time, tf)


def build_entry_row(regime: Mapping[str, object], observation: Mapping[str, object],
                    h1_frames: Sequence[CausalFrame], m5_frames: Sequence[CausalFrame],
                    pivots: Sequence[ConfirmedPivot], h1_index: Mapping[str, int],
                    m5_index: Mapping[str, int], m5_ema_atr: Sequence[tuple[float | None, float | None]],
                    spread_reference: Sequence[float], recent_spreads: Sequence[float],
                    spread_reference_basis: str, m15_context: Mapping[str, object] | None = None) -> dict:
    _validate_regime(regime)
    time = int(regime["decision_time"])
    if (regime.get("event_id") != observation.get("event_id") or
            observation.get("observed_at") != time or observation.get("bar_closed") is not True or
            not _finite(observation.get("close")) or observation.get("timestamp", time) > time):
        raise ValueError("observation is missing, open or mismatched")
    price = float(observation["close"])
    direction = causal_direction(regime)
    setup, setups = select_setup(observation, direction)
    h1_ctx, h4_ctx, m5_ctx = (regime.get(name) or {} for name in ("h1", "h4", "m5"))
    freshness = {name: context_freshness(regime.get(name) or {}, time, name)
                 for name in ("m5", "h1", "h4")}
    h1_i = h1_index.get(h1_ctx.get("bar_id"), -1)
    m5_i = m5_index.get(m5_ctx.get("bar_id"), -1)
    if (h1_ctx and h1_i < 0) or (m5_ctx and m5_i < 0):
        raise ValueError("regime references an unknown causal source bar")
    if h1_i >= 0:
        h1_frames[h1_i].require(time)
    if m5_i >= 0:
        m5_frames[m5_i].require(time)
    h1_features = h1_ctx.get("features") or {}
    atr = h1_features.get("atr") if _finite(h1_features.get("atr")) and h1_features["atr"] > 0 else None
    pivot_set = available_pivots(pivots, time, h1_i)
    levels = structural_levels(pivot_set, price, direction or "UNKNOWN")
    sign = 1 if direction == "LONG" else -1 if direction == "SHORT" else 0
    ema20 = h1_features.get("ema20")
    ema50 = h1_features.get("ema50")
    raw_ema20 = sign * (price - ema20) if sign and _finite(ema20) else None
    raw_ema50 = sign * (price - ema50) if sign and _finite(ema50) else None
    ranges = {length: range_position(h1_frames, h1_i, price, length)
              for length in (10, 20, 30)}
    recent_high = ranges[10][1]
    recent_low = ranges[10][2]
    h1_returns = directional_returns(h1_frames, h1_i, direction or "UNKNOWN") if h1_i >= 0 else {}
    m5_returns = directional_returns(m5_frames, m5_i, direction or "UNKNOWN") if m5_i >= 0 else {}
    lower = lower_tf_context(m5_frames, m5_ema_atr, m5_i, price, direction or "UNKNOWN") if m5_i >= 0 else {}
    spread = observation.get("spread") if _finite(observation.get("spread")) and observation["spread"] >= 0 else None
    score = regime.get("long_score") if direction == "LONG" else regime.get("short_score") if direction == "SHORT" else None
    if m15_context and (m15_context.get("observed_at", time + 1) > time or
                        m15_context.get("bar_closed") is not True or
                        m15_context.get("timeframe") != "M15"):
        raise LookAheadError("open or future M15 timing context")
    m15_age = ((time - m15_context["observed_at"]) / 60_000) if m15_context else None
    stale_dependent = [name for name in ("m5", "h1", "h4") if freshness[name]["freshness_state"] == "STALE"]
    result = {"event_id": regime["event_id"], "decision_time": time, "source": SOURCE,
              "parameter_version": VERSION, "split": regime["split"],
              "direction": direction, "setup": setup, "setups": setups,
              "trade_class": None, "score": score, "decision": regime.get("decision"),
              "gold_regime": regime.get("gold_regime"), "alignment": regime.get("alignment"),
              "trend_state": h1_ctx.get("state", {}).get("trend_state", "UNKNOWN"),
              "volatility_state": h1_ctx.get("state", {}).get("volatility_state", "UNKNOWN"),
              "structure_state": h1_ctx.get("state", {}).get("structure_state", "UNKNOWN"),
              "transition_state": h1_ctx.get("state", {}).get("transition_state", "UNKNOWN"),
              "context_freshness": freshness,
              "regime_freshness": "STALE" if "h1" in stale_dependent or "h4" in stale_dependent else
                                  "UNKNOWN" if not h1_ctx or not h4_ctx else "FRESH",
              "stale_dependent_features": (["impulse", "ema_extension", "structural_room", "structural_stop", "recent_range", "h1_acceleration", "regime"]
                                           if "h1" in stale_dependent else []) +
                                          (["alignment", "h4_context"] if "h4" in stale_dependent else []) +
                                          (["lower_tf_timing"] if "m5" in stale_dependent else []) +
                                          (["m15_timing"] if m15_age is not None and m15_age > 45 else []),
              "entry_price": price, "h1_atr": atr,
              **levels,
              "impulse_distance_atr": _safe_ratio(levels["impulse_distance"], atr),
              "distance_from_impulse_origin_atr": _safe_ratio(levels["impulse_distance"], atr),
              "distance_to_ema20": raw_ema20, "distance_to_ema20_atr": _safe_ratio(raw_ema20, atr),
              "ema20_extension_atr": _safe_ratio(raw_ema20, atr),
              "distance_to_ema50": raw_ema50, "distance_to_ema50_atr": _safe_ratio(raw_ema50, atr),
              "ema50_extension_atr": _safe_ratio(raw_ema50, atr),
              "room_to_obstacle_atr": _safe_ratio(levels["room_to_obstacle"], atr),
              "room_to_obstacle_R": None,  # exact historical selected SL is unavailable
              "structural_stop_distance_atr": _safe_ratio(levels["entry_to_structural_invalidation"], atr),
              "entry_to_structural_invalidation_atr": _safe_ratio(levels["entry_to_structural_invalidation"], atr),
              "entry_to_current_sl": None, "entry_to_current_sl_atr": None,
              "current_sl_distance_atr": None,
              "range_position_10": ranges[10][0], "range_position_20": ranges[20][0],
              "range_position_30": ranges[30][0],
              "directional_range_position_10": (ranges[10][0] if direction == "LONG" else
                                                1 - ranges[10][0] if direction == "SHORT" else None)
                                               if ranges[10][0] is not None else None,
              "directional_range_position_20": (ranges[20][0] if direction == "LONG" else
                                                1 - ranges[20][0] if direction == "SHORT" else None)
                                               if ranges[20][0] is not None else None,
              "directional_range_position_30": (ranges[30][0] if direction == "LONG" else
                                                1 - ranges[30][0] if direction == "SHORT" else None)
                                               if ranges[30][0] is not None else None,
              "distance_recent_high_atr": _safe_ratio(recent_high - price, atr) if recent_high is not None else None,
              "distance_recent_low_atr": _safe_ratio(price - recent_low, atr) if recent_low is not None else None,
              "body_size_atr": _safe_ratio(abs(float(observation["close"]) - float(observation["open"])), atr)
                                  if _finite(observation.get("open")) else None,
              "range_size_atr": _safe_ratio(float(observation["high"]) - float(observation["low"]), atr)
                                   if _finite(observation.get("high")) and _finite(observation.get("low")) else None,
              "h1_recent_returns": h1_returns, "m5_recent_returns": m5_returns,
              "m15_timing": ({"available_at": m15_context["observed_at"],
                              "event_id": m15_context["event_id"],
                              "age_minutes": m15_age,
                              "freshness_state": "STALE" if m15_age > 45 else "FRESH",
                              **{name: m15_context.get(name) for name in
                                 ("structure", "breakout", "momentum", "pullback", "recovery")}}
                             if m15_context else None),
              "momentum_acceleration": h1_returns.get("momentum_acceleration"),
              "momentum_deceleration": h1_returns.get("momentum_deceleration"),
              "acceleration": h1_returns.get("momentum_acceleration"),
              "deceleration": h1_returns.get("momentum_deceleration"),
              **lower,
              "spread_at_decision": spread,
              "spread_percentile_train": percentile_rank(float(spread), spread_reference) if spread is not None else None,
              "spread_reference_basis": spread_reference_basis,
              "spread_vs_recent_median": _safe_ratio(float(spread), statistics.median(recent_spreads))
                                         if spread is not None and recent_spreads else None,
              "spread_at_entry_tick": None, "slippage": None,
              "diagnostic_only": True, "score_effect": 0}
    return result


def generate(root: Path, output: Path) -> dict:
    root, output = Path(root), Path(output)
    meta_path = root / "regimes" / "gold_regime_v1.meta.json"
    regime_path = root / "regimes" / "gold_regime_v1.jsonl"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    if meta.get("output_sha256") != _file_sha256(regime_path):
        raise ValueError("regime dataset does not match its frozen metadata")
    m5_path = root / "causal_bars" / "XAUUSD_M5.jsonl"
    m5_index = json.loads((root / "causal_bars" / "causal_bars_index.json").read_text(encoding="utf-8"))
    if m5_index["outputs"]["M5"]["sha256"] != _file_sha256(m5_path):
        raise ValueError("M5 source differs from phase-2 index")
    m5_frames = _load_m5(m5_path)
    h1_frames = wrap_higher_bars(_read_jsonl(root / "historical_bars" / "XAUUSD_H1.jsonl"), m5_frames, "H1")
    pivots = confirmed_pivots(h1_frames)
    m5_ema_atr = _ema_atr(m5_frames)
    h1_index = {bar.bar_id: i for i, bar in enumerate(h1_frames)}
    m5_index_by_id = {bar.bar_id: i for i, bar in enumerate(m5_frames)}
    observations = _observations(root / "historical_observations.jsonl")
    m15_observations = sorted((row for row in observations.values() if row.get("timeframe") == "M15"),
                              key=lambda row: (row["observed_at"], row["event_id"]))
    m15_times = [row["observed_at"] for row in m15_observations]
    regime_rows = list(_read_jsonl(regime_path))
    if not regime_rows or len(regime_rows) != meta.get("rows"):
        raise ValueError("regime row count differs from frozen metadata")
    regime_rows.sort(key=lambda row: (row.get("decision_time", -1), row.get("event_id", "")))
    train_end = meta["train_end"]
    train_spreads = sorted(float(observations[row["event_id"]]["spread"])
                           for row in regime_rows if row.get("split") == "TRAIN"
                           and observations[row["event_id"]]["observed_at"] <= train_end
                           and _finite(observations[row["event_id"]].get("spread"))
                           and observations[row["event_id"]]["spread"] >= 0)
    if len(train_spreads) < 30:
        raise ValueError("insufficient TRAIN spread evidence")
    prior_spreads: deque[float] = deque(maxlen=256)
    recent_spreads: deque[float] = deque(maxlen=20)
    counts: Counter[str] = Counter()
    feature_coverage: Counter[str] = Counter()
    seen: set[str] = set()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".jsonl.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            for regime in regime_rows:
                event_id = regime.get("event_id")
                if not isinstance(event_id, str) or event_id in seen:
                    raise ValueError("duplicate or invalid regime event_id")
                seen.add(event_id)
                obs = observations.get(event_id)
                if obs is None:
                    raise ValueError(f"observation missing for {event_id}")
                basis = "TRAIN_ONLINE_PREFIX" if regime.get("split") == "TRAIN" else "TRAIN_FROZEN"
                m15_position = bisect_right(m15_times, regime["decision_time"]) - 1
                row = build_entry_row(regime, obs, h1_frames, m5_frames, pivots,
                                      h1_index, m5_index_by_id, m5_ema_atr,
                                      sorted(prior_spreads) if basis == "TRAIN_ONLINE_PREFIX" else train_spreads,
                                      list(recent_spreads), basis,
                                      m15_observations[m15_position] if m15_position >= 0 else None)
                for tf, context in row["context_freshness"].items():
                    counts[f"{tf}:{context['freshness_state']}"] += 1
                    for threshold in context["stale_thresholds_exceeded"]:
                        counts[f"{tf}:GT_{threshold}M"] += 1
                counts[f"m15:{row['m15_timing']['freshness_state'] if row['m15_timing'] else 'UNKNOWN'}"] += 1
                for key in ("impulse_distance_atr", "ema20_extension_atr", "ema50_extension_atr",
                            "room_to_obstacle_atr", "structural_stop_distance_atr",
                            "range_position_10", "range_position_20", "range_position_30",
                            "directional_range_position_10",
                            "momentum_acceleration", "spread_percentile_train", "m15_timing"):
                    if row.get(key) is not None:
                        feature_coverage[key] += 1
                spread = obs.get("spread")
                if _finite(spread) and spread >= 0:
                    prior_spreads.append(float(spread))
                    recent_spreads.append(float(spread))
                stream.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")
        temporary.replace(output)
    finally:
        if temporary.exists():
            temporary.unlink()
    result = {"rows": len(seen), "parameter_version": VERSION,
              "source_regime_sha256": meta["output_sha256"],
              "stale_context_counts": dict(counts), "feature_coverage": dict(feature_coverage),
              "output_sha256": _file_sha256(output)}
    metadata = output.with_suffix(".meta.json")
    tmp_meta = metadata.with_suffix(".meta.json.tmp")
    try:
        tmp_meta.write_text(json.dumps(result, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        tmp_meta.replace(metadata)
    finally:
        if tmp_meta.exists():
            tmp_meta.unlink()
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-root", type=Path, default=Path("research/output"))
    parser.add_argument("--output", type=Path, default=Path("research/output/entry_quality/entry_quality_v1.jsonl"))
    args = parser.parse_args()
    print(json.dumps(generate(args.input_root, args.output), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
