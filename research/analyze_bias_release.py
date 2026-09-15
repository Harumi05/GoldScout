"""Offline analysis of early release from a stale GoldScout directional bias.

Rules in this module can only propose ``OLD_BIAS -> NEUTRAL_RELEASE``. They do
not create an opposite trade, mutate scores, or interact with the live EA.
Every feature is derived from an already closed M15 snapshot and the latest H1
bar whose close timestamp is not later than the snapshot observation time.
Future bars are used only after a trigger to label diagnostic outcomes.
"""

from __future__ import annotations

import argparse
from bisect import bisect_left, bisect_right
from collections import Counter
import csv
from pathlib import Path
import statistics
from typing import Mapping, Sequence

from research.analyze_historical_dataset import (
    AnalysisError,
    PERIODS,
    _atomic_write,
    _is_number,
    _load_observations,
    _merge_decision_enrichment,
    _write_csv,
)
from research.enrich_historical_decisions import _load_bars, build_state_points


RULE_NAMES = ("RULE_A", "RULE_B", "RULE_C", "RULE_D", "RULE_E")
TIMEFRAME_MS = {"M15": 900_000, "H1": 3_600_000, "H4": 14_400_000}
OPPOSITE_MOVE_ATR = 0.75
ORIGINAL_RESUME_ATR = 0.50
ACCELERATION_MIN_ABS = 0.15
DISPLACEMENT_MIN_ABS = 0.50
MIN_RULE_SAMPLE_PER_PERIOD = 20


def opposite_direction(direction: str) -> str:
    normalized = str(direction).upper()
    if normalized == "LONG":
        return "SHORT"
    if normalized == "SHORT":
        return "LONG"
    raise ValueError("direction must be LONG or SHORT")


def transition_name(stale_direction: str) -> str:
    return "LONG_TO_BEARISH" if stale_direction == "LONG" else "SHORT_TO_BULLISH"


def _aligned(direction: str, state: object) -> bool:
    text = str(state or "").strip().upper()
    return (direction == "LONG" and text == "BULLISH") or (direction == "SHORT" and text == "BEARISH")


def _event(direction: str, value: object) -> bool:
    return str(value or "").strip().upper() == direction


class H1CausalResolver:
    """Resolve H1 indicator state without crossing an observation timestamp."""

    def __init__(self, input_dir: Path, symbol: str = "XAUUSD", point_size: float = 0.01):
        bars = _load_bars(Path(input_dir), symbol, "H1")
        self.points = build_state_points(bars, point_size)
        self.close_times = [int(point.bar["close_timestamp"]) for point in self.points]

    def resolve(self, observed_at_ms: int):
        index = bisect_right(self.close_times, int(observed_at_ms)) - 1
        if index < 2:
            return None
        return {
            "current": self.points[index],
            "previous": self.points[index - 1],
            "previous2": self.points[index - 2],
            "prior20": self.points[max(0, index - 20) : index],
            "resolved_close_timestamp": self.close_times[index],
        }


def _momentum(point, previous, direction: str) -> bool:
    if direction == "LONG":
        return float(point.bar["close"]) > float(previous.bar["high"])
    return float(point.bar["close"]) < float(previous.bar["low"])


def transition_features(
    observation: Mapping[str, object], stale_direction: str, h1_context: Mapping[str, object] | None
) -> dict[str, object]:
    """Extract only causal transition evidence available at this snapshot."""

    new_direction = opposite_direction(stale_direction)
    m15 = observation.get("m15_timing") if isinstance(observation.get("m15_timing"), dict) else {}
    h1 = observation.get("h1_structure") if isinstance(observation.get("h1_structure"), dict) else {}
    regime = observation.get("h1_regime") if isinstance(observation.get("h1_regime"), dict) else {}
    m15_alignment = bool(m15.get("available")) and _aligned(new_direction, m15.get("structure"))
    m15_momentum = _event(new_direction, m15.get("momentum"))
    m15_breakout = _event(new_direction, m15.get("breakout"))
    m15_recovery = _event(new_direction, m15.get("recovery"))
    opposite_event = m15_breakout or m15_recovery
    h1_suffix = "long" if new_direction == "LONG" else "short"
    old_suffix = "long" if stale_direction == "LONG" else "short"
    h1_breakout = bool(h1.get(f"breakout_{h1_suffix}"))
    h1_pullback = bool(h1.get(f"pullback_{h1_suffix}"))
    opposite_breakout_recovery = opposite_event or h1_breakout or h1_pullback
    dmi_cross = rsi_confirmation = rsi_cross = ema20_cross = volume_change = adx_change = False
    h1_momentum_loss = False
    dmi_value = rsi_value = adx_value = adx_delta = None
    if h1_context:
        current = h1_context["current"]
        previous = h1_context["previous"]
        previous2 = h1_context["previous2"]
        plus_now, minus_now = current.indicators.get("plus_di"), current.indicators.get("minus_di")
        plus_prev, minus_prev = previous.indicators.get("plus_di"), previous.indicators.get("minus_di")
        if all(_is_number(value) for value in (plus_now, minus_now, plus_prev, minus_prev)):
            dmi_cross = (
                new_direction == "LONG" and float(plus_prev) <= float(minus_prev) and float(plus_now) > float(minus_now)
            ) or (
                new_direction == "SHORT" and float(minus_prev) <= float(plus_prev) and float(minus_now) > float(plus_now)
            )
            dmi_value = float(plus_now) - float(minus_now) if new_direction == "LONG" else float(minus_now) - float(plus_now)
        rsi_now, rsi_prev = current.indicators.get("rsi"), previous.indicators.get("rsi")
        if _is_number(rsi_now):
            rsi_value = float(rsi_now)
            rsi_confirmation = (new_direction == "LONG" and rsi_value >= 50.0) or (new_direction == "SHORT" and rsi_value <= 50.0)
            if _is_number(rsi_prev):
                rsi_cross = (
                    new_direction == "LONG" and float(rsi_prev) < 50.0 <= rsi_value
                ) or (
                    new_direction == "SHORT" and float(rsi_prev) > 50.0 >= rsi_value
                )
        ema_now, ema_prev = current.indicators.get("ema20"), previous.indicators.get("ema20")
        if _is_number(ema_now) and _is_number(ema_prev):
            ema20_cross = (
                new_direction == "LONG"
                and float(previous.bar["close"]) <= float(ema_prev)
                and float(current.bar["close"]) > float(ema_now)
            ) or (
                new_direction == "SHORT"
                and float(previous.bar["close"]) >= float(ema_prev)
                and float(current.bar["close"]) < float(ema_now)
            )
        adx_now, adx_prev = current.indicators.get("adx"), previous.indicators.get("adx")
        if _is_number(adx_now):
            adx_value = float(adx_now)
            if _is_number(adx_prev):
                adx_delta = adx_value - float(adx_prev)
                adx_change = adx_value >= 16.0 and adx_delta > 0.0
        prior = list(h1_context.get("prior20", []))
        if prior:
            average = statistics.fmean(float(point.bar["tick_count"]) for point in prior)
            current_volume = float(current.bar["tick_count"])
            candle_aligned = (
                new_direction == "LONG" and float(current.bar["close"]) >= float(current.bar["open"])
            ) or (
                new_direction == "SHORT" and float(current.bar["close"]) < float(current.bar["open"])
            )
            volume_change = average > 0.0 and current_volume >= average * 1.05 and candle_aligned
        previous_old_momentum = _momentum(previous, previous2, stale_direction)
        current_old_momentum = _momentum(current, previous, stale_direction)
        h1_momentum_loss = previous_old_momentum and not current_old_momentum
    acceleration = regime.get("recent_acceleration")
    window3 = regime.get("window_3") if isinstance(regime.get("window_3"), dict) else {}
    displacement = window3.get("net_displacement_atr")
    acceleration_aligned = _is_number(acceleration) and (
        (new_direction == "LONG" and float(acceleration) >= ACCELERATION_MIN_ABS)
        or (new_direction == "SHORT" and float(acceleration) <= -ACCELERATION_MIN_ABS)
    )
    displacement_aligned = _is_number(displacement) and (
        (new_direction == "LONG" and float(displacement) >= DISPLACEMENT_MIN_ABS)
        or (new_direction == "SHORT" and float(displacement) <= -DISPLACEMENT_MIN_ABS)
    )
    return {
        "stale_direction": stale_direction,
        "new_direction": new_direction,
        "m15_opposite_alignment": m15_alignment,
        "m15_opposite_momentum": m15_momentum,
        "m15_opposite_breakout": m15_breakout,
        "m15_opposite_recovery": m15_recovery,
        "opposite_breakout_recovery": opposite_breakout_recovery,
        "dmi_cross": dmi_cross,
        "dmi_value": dmi_value,
        "rsi_confirmation": rsi_confirmation,
        "rsi_cross": rsi_cross,
        "rsi": rsi_value,
        "ema20_reclaim_loss": ema20_cross,
        "acceleration_aligned": acceleration_aligned,
        "recent_acceleration": float(acceleration) if _is_number(acceleration) else None,
        "displacement_aligned": displacement_aligned,
        "recent_displacement_atr": float(displacement) if _is_number(displacement) else None,
        "volume_change": volume_change,
        "adx_change": adx_change,
        "adx": adx_value,
        "adx_delta": adx_delta,
        "h1_momentum_loss": h1_momentum_loss,
        "h1_opposite_breakout": h1_breakout,
        "h1_opposite_pullback": h1_pullback,
        "h1_old_momentum_active": bool(h1.get(f"momentum_{old_suffix}")),
    }


def rule_triggered(rule: str, features: Mapping[str, object]) -> bool:
    if rule == "RULE_A":
        return bool(features.get("m15_opposite_alignment") and features.get("m15_opposite_momentum"))
    if rule == "RULE_B":
        return bool(features.get("m15_opposite_alignment") and features.get("dmi_cross") and features.get("rsi_confirmation"))
    if rule == "RULE_C":
        return bool(features.get("opposite_breakout_recovery") and features.get("ema20_reclaim_loss"))
    if rule == "RULE_D":
        return bool(features.get("m15_opposite_alignment") and features.get("acceleration_aligned") and features.get("h1_momentum_loss"))
    if rule == "RULE_E":
        votes = sum(bool(features.get(name)) for name in (
            "m15_opposite_alignment", "dmi_cross", "rsi_confirmation", "opposite_breakout_recovery"
        ))
        return votes >= 2
    raise ValueError(f"unknown rule: {rule}")


def evaluate_release_path(
    bars: Sequence[Mapping[str, object]],
    *,
    trigger_timestamp: int,
    end_timestamp: int,
    reference_price: float,
    atr: float,
    stale_direction: str,
) -> dict[str, object]:
    """Label a trigger using only bars closed after it.

    If both directional thresholds are touched in the same M15 bar, ordering is
    unknowable and the event is AMBIGUOUS rather than forced into true/false.
    """

    if reference_price <= 0.0 or atr <= 0.0 or end_timestamp <= trigger_timestamp:
        return {
            "release_outcome": "UNRESOLVABLE",
            "false_release": None,
            "bars_evaluated": 0,
            "opposite_mfe_atr": None,
            "opposite_mae_atr": None,
            "potential_adverse_avoided_price": None,
            "potential_adverse_avoided_atr": None,
            "foregone_original_move_price": None,
            "foregone_original_move_atr": None,
            "original_direction_resumed": None,
        }
    new_direction = opposite_direction(stale_direction)
    max_opposite = max_original = 0.0
    bars_evaluated = 0
    first_outcome = "INCONCLUSIVE"
    for bar in bars:
        close_time = int(bar["close_timestamp"])
        if close_time <= trigger_timestamp:
            continue
        if close_time > end_timestamp:
            break
        bars_evaluated += 1
        high, low = float(bar["high"]), float(bar["low"])
        if new_direction == "LONG":
            opposite = max(0.0, high - reference_price)
            original = max(0.0, reference_price - low)
        else:
            opposite = max(0.0, reference_price - low)
            original = max(0.0, high - reference_price)
        max_opposite = max(max_opposite, opposite)
        max_original = max(max_original, original)
        opposite_hit = opposite >= OPPOSITE_MOVE_ATR * atr
        original_hit = original >= ORIGINAL_RESUME_ATR * atr
        if first_outcome == "INCONCLUSIVE" and opposite_hit and original_hit:
            first_outcome = "AMBIGUOUS"
        elif first_outcome == "INCONCLUSIVE" and opposite_hit:
            first_outcome = "TRUE_RELEASE"
        elif first_outcome == "INCONCLUSIVE" and original_hit:
            first_outcome = "FALSE_RELEASE"
    if bars_evaluated == 0:
        first_outcome = "UNRESOLVABLE"
    return {
        "release_outcome": first_outcome,
        "false_release": first_outcome == "FALSE_RELEASE",
        "bars_evaluated": bars_evaluated,
        "opposite_mfe_atr": max_opposite / atr,
        "opposite_mae_atr": max_original / atr,
        "potential_adverse_avoided_price": max_opposite,
        "potential_adverse_avoided_atr": max_opposite / atr,
        "foregone_original_move_price": max_original if first_outcome == "FALSE_RELEASE" else 0.0,
        "foregone_original_move_atr": max_original / atr if first_outcome == "FALSE_RELEASE" else 0.0,
        "original_direction_resumed": max_original >= ORIGINAL_RESUME_ATR * atr,
    }


def load_stale_episodes(path: Path) -> list[dict]:
    episodes: list[dict] = []
    with Path(path).open("r", encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            if row.get("cohort") != "BASELINE_STALE_EPISODE":
                continue
            change = str(row.get("direction_change", ""))
            stale = "LONG" if change.startswith("LONG_TO_") else "SHORT" if change.startswith("SHORT_TO_") else None
            if stale is None:
                continue
            event_id = str(row.get("event_id", ""))
            timeframe = next((value for value in TIMEFRAME_MS if f"-{value}-" in event_id), "UNKNOWN")
            try:
                start = int(float(row["episode_start"]))
                classic = int(float(row["classic_change_timestamp"])) + TIMEFRAME_MS[timeframe]
            except (KeyError, TypeError, ValueError):
                continue
            if classic <= start:
                continue
            episodes.append({
                "episode_id": event_id,
                "period": str(row.get("period", "UNKNOWN")),
                "source_timeframe": timeframe,
                "stale_direction": stale,
                "new_direction": opposite_direction(stale),
                "transition": transition_name(stale),
                "start_timestamp": start,
                "classic_change_timestamp": classic,
            })
    return episodes


def _score_still_old(observation: Mapping[str, object], stale_direction: str) -> bool:
    old_key = "long_score" if stale_direction == "LONG" else "short_score"
    new_key = "short_score" if stale_direction == "LONG" else "long_score"
    return _is_number(observation.get(old_key)) and _is_number(observation.get(new_key)) and float(observation[old_key]) > float(observation[new_key])


def _observation_slice(observations: Sequence[dict], times: Sequence[int], start: int, end: int) -> Sequence[dict]:
    return observations[bisect_left(times, start) : bisect_left(times, end)]


def detect_rule_events(
    episodes: Sequence[dict],
    observations: Sequence[dict],
    h1_resolver: H1CausalResolver,
    m15_bars: Sequence[dict],
) -> tuple[list[dict], list[dict]]:
    times = [int(item["_timestamp_ms"]) for item in observations]
    m15_close_times = [int(item["close_timestamp"]) for item in m15_bars]
    episode_states: list[dict] = []
    events: list[dict] = []
    for episode in episodes:
        sequence = list(_observation_slice(
            observations, times, int(episode["start_timestamp"]), int(episode["classic_change_timestamp"])
        ))
        lag_sequence = [item for item in sequence if _score_still_old(item, episode["stale_direction"])]
        score_release = next(
            (int(item["_timestamp_ms"]) for item in sequence if not _score_still_old(item, episode["stale_direction"])),
            None,
        )
        state = {
            **episode,
            "snapshots": len(sequence),
            "score_lag": bool(lag_sequence),
            "score_release_timestamp": score_release,
        }
        episode_states.append(state)
        if not lag_sequence:
            continue
        unresolved_rules = list(RULE_NAMES)
        for observation in lag_sequence:
            if not unresolved_rules:
                break
            observed_at = int(observation["_timestamp_ms"])
            h1_context = h1_resolver.resolve(observed_at)
            features = transition_features(observation, episode["stale_direction"], h1_context)
            for rule in tuple(unresolved_rules):
                if not rule_triggered(rule, features):
                    continue
                atr = None
                if h1_context and _is_number(h1_context["current"].indicators.get("atr")):
                    atr = float(h1_context["current"].indicators["atr"])
                if atr is None or atr <= 0.0 or not _is_number(observation.get("close")):
                    continue
                path_start = bisect_right(m15_close_times, observed_at)
                path_end = bisect_right(m15_close_times, int(episode["classic_change_timestamp"]))
                path = evaluate_release_path(
                    m15_bars[path_start:path_end],
                    trigger_timestamp=observed_at,
                    end_timestamp=int(episode["classic_change_timestamp"]),
                    reference_price=float(observation["close"]),
                    atr=atr,
                    stale_direction=episode["stale_direction"],
                )
                old_key = "long_score" if episode["stale_direction"] == "LONG" else "short_score"
                new_key = "short_score" if episode["stale_direction"] == "LONG" else "long_score"
                events.append({
                    "episode_id": episode["episode_id"],
                    "rule": rule,
                    "bias_release_state": "BIAS_RELEASE_CANDIDATE",
                    "simulated_action": f"{episode['stale_direction']}_BIAS_TO_NEUTRAL",
                    "period": episode["period"],
                    "transition": episode["transition"],
                    "stale_direction": episode["stale_direction"],
                    "new_direction": episode["new_direction"],
                    "source_timeframe": episode["source_timeframe"],
                    "episode_start": episode["start_timestamp"],
                    "trigger_timestamp": observed_at,
                    "score_release_timestamp": score_release,
                    "classic_change_timestamp": episode["classic_change_timestamp"],
                    "lead_minutes": (int(episode["classic_change_timestamp"]) - observed_at) / 60_000.0,
                    "lead_vs_score_release_minutes": (
                        (score_release - observed_at) / 60_000.0 if score_release is not None and score_release >= observed_at else None
                    ),
                    "old_score": observation.get(old_key),
                    "new_score": observation.get(new_key),
                    "reference_price": observation.get("close"),
                    "atr": atr,
                    "diagnostic_only": True,
                    "score_effect": 0,
                    **features,
                    **path,
                })
                unresolved_rules.remove(rule)
    return episode_states, events


def _median(events: Sequence[dict], key: str) -> float | None:
    values = [float(item[key]) for item in events if _is_number(item.get(key))]
    return statistics.median(values) if values else None


def summarize_rules(episode_states: Sequence[dict], events: Sequence[dict]) -> list[dict]:
    rows: list[dict] = []
    for period in ("ALL",) + PERIODS:
        for transition in ("ALL", "LONG_TO_BEARISH", "SHORT_TO_BULLISH"):
            stale = [
                item for item in episode_states
                if (period == "ALL" or item["period"] == period) and (transition == "ALL" or item["transition"] == transition)
            ]
            lag = [item for item in stale if item["score_lag"]]
            for rule in RULE_NAMES:
                selected = [
                    item for item in events
                    if item["rule"] == rule and (period == "ALL" or item["period"] == period)
                    and (transition == "ALL" or item["transition"] == transition)
                ]
                outcomes = Counter(item["release_outcome"] for item in selected)
                resolved = outcomes["TRUE_RELEASE"] + outcomes["FALSE_RELEASE"]
                rows.append({
                    "period": period,
                    "transition": transition,
                    "rule": rule,
                    "stale_episodes": len(stale),
                    "score_lag_episodes": len(lag),
                    "episodes_detected": len(selected),
                    "coverage_stale_pct": len(selected) * 100.0 / len(stale) if stale else None,
                    "coverage_score_lag_pct": len(selected) * 100.0 / len(lag) if lag else None,
                    "true_release": outcomes["TRUE_RELEASE"],
                    "false_release": outcomes["FALSE_RELEASE"],
                    "ambiguous": outcomes["AMBIGUOUS"],
                    "inconclusive": outcomes["INCONCLUSIVE"],
                    "unresolvable": outcomes["UNRESOLVABLE"],
                    "false_release_rate_pct": outcomes["FALSE_RELEASE"] * 100.0 / resolved if resolved else None,
                    "original_direction_resume_pct": sum(bool(item.get("original_direction_resumed")) for item in selected) * 100.0 / len(selected) if selected else None,
                    "median_lead_minutes": _median(selected, "lead_minutes"),
                    "median_lead_h1_bars": (_median(selected, "lead_minutes") / 60.0) if _median(selected, "lead_minutes") is not None else None,
                    "median_adverse_move_avoided_price": _median(selected, "potential_adverse_avoided_price"),
                    "median_adverse_move_avoided_atr": _median(selected, "potential_adverse_avoided_atr"),
                    "median_opposite_mfe_atr": _median(selected, "opposite_mfe_atr"),
                    "median_opposite_mae_atr": _median(selected, "opposite_mae_atr"),
                    "median_false_release_foregone_atr": _median(
                        [item for item in selected if item["release_outcome"] == "FALSE_RELEASE"], "foregone_original_move_atr"
                    ),
                })
    return rows


def _row(rows: Sequence[dict], period: str, transition: str, rule: str) -> dict:
    return next(item for item in rows if item["period"] == period and item["transition"] == transition and item["rule"] == rule)


def candidate_assessment(rows: Sequence[dict]) -> list[dict]:
    result: list[dict] = []
    for rule in RULE_NAMES:
        periods = {period: _row(rows, period, "ALL", rule) for period in PERIODS}
        enough = all(item["episodes_detected"] >= MIN_RULE_SAMPLE_PER_PERIOD for item in periods.values())
        false_ok = all(_is_number(item["false_release_rate_pct"]) and float(item["false_release_rate_pct"]) <= 35.0 for item in periods.values())
        avoided = all(_is_number(item["median_adverse_move_avoided_atr"]) and float(item["median_adverse_move_avoided_atr"]) >= 0.50 for item in periods.values())
        oos_coverage = float(periods["OOS"]["coverage_score_lag_pct"] or 0.0)
        qualifies = enough and false_ok and avoided and oos_coverage >= 5.0
        oos_false_value = periods["OOS"]["false_release_rate_pct"]
        oos_false = float(oos_false_value) if _is_number(oos_false_value) else 100.0
        oos_avoided = float(periods["OOS"]["median_adverse_move_avoided_atr"] or 0.0)
        diagnostic_rank = oos_coverage * max(0.0, 1.0 - oos_false / 100.0) * oos_avoided
        result.append({
            "rule": rule,
            "qualifies_for_paper_diagnostic": qualifies,
            "minimum_sample_each_period": enough,
            "false_release_acceptable_each_period": false_ok,
            "adverse_move_reduction_each_period": avoided,
            "oos_coverage_score_lag_pct": oos_coverage,
            "oos_false_release_rate_pct": periods["OOS"]["false_release_rate_pct"],
            "oos_median_adverse_avoided_atr": periods["OOS"]["median_adverse_move_avoided_atr"],
            "diagnostic_rank": diagnostic_rank,
        })
    return sorted(result, key=lambda item: (-int(item["qualifies_for_paper_diagnostic"]), -item["diagnostic_rank"], item["rule"]))


def _fmt(value: object, decimals: int = 2) -> str:
    return "N/A" if not _is_number(value) else f"{float(value):.{decimals}f}"


def _markdown(summary: dict, rules: Sequence[dict]) -> str:
    best = summary["best_rule"]
    lines = [
        "# GoldScout — Bias Release histórico",
        "",
        "> Solo diagnóstico · OLD_BIAS → NEUTRAL_RELEASE · nunca genera una entrada opuesta · score_effect=0.",
        "",
        "## Universo",
        "",
        f"- Episodios stale cargados: {summary['universe']['stale_episodes']:,}.",
        f"- Episodios con snapshots M15 evaluables: {summary['universe']['evaluable_episodes']:,}.",
        f"- Episodios donde el score todavía favorecía el bias viejo: {summary['universe']['score_lag_episodes']:,}.",
        f"- LONG→bearish / SHORT→bullish: {summary['universe']['long_to_bearish']:,} / {summary['universe']['short_to_bullish']:,}.",
        "",
        "## Reglas interpretables",
        "",
        "- RULE_A: M15 alineado en sentido opuesto + momentum M15 opuesto.",
        "- RULE_B: M15 opuesto + cruce DMI H1 + RSI H1 al lado opuesto de 50.",
        "- RULE_C: breakout/recovery/pullback opuesto + cruce causal de EMA20 H1.",
        "- RULE_D: M15 opuesto + aceleración H1 >=0.15 + pérdida de momentum H1 viejo.",
        "- RULE_E: al menos 2 de 4: M15 opuesto, cruce DMI, RSI confirmado, breakout/recovery opuesto.",
        "",
        "## Comparación global",
        "",
        "| Regla | Detectados | Cobertura stale | Cobertura score-lag | Lead mediano | Adverso evitado ATR | False release | Retoma bias viejo |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for rule in RULE_NAMES:
        item = _row(rules, "ALL", "ALL", rule)
        lines.append(
            f"| {rule} | {item['episodes_detected']:,} | {_fmt(item['coverage_stale_pct'])}% | "
            f"{_fmt(item['coverage_score_lag_pct'])}% | {_fmt(item['median_lead_minutes'])}m | "
            f"{_fmt(item['median_adverse_move_avoided_atr'], 3)} | {_fmt(item['false_release_rate_pct'])}% | "
            f"{_fmt(item['original_direction_resume_pct'])}% |"
        )
    lines.extend([
        "",
        "## TRAIN / VALIDATION / OOS",
        "",
        "| Regla | Periodo | N | Cobertura score-lag | Lead mediano | Adverso evitado ATR | False release |",
        "|---|---|---:|---:|---:|---:|---:|",
    ])
    for rule in RULE_NAMES:
        for period in PERIODS:
            item = _row(rules, period, "ALL", rule)
            lines.append(
                f"| {rule} | {period} | {item['episodes_detected']:,} | {_fmt(item['coverage_score_lag_pct'])}% | "
                f"{_fmt(item['median_lead_minutes'])}m | {_fmt(item['median_adverse_move_avoided_atr'], 3)} | "
                f"{_fmt(item['false_release_rate_pct'])}% |"
            )
    lines.extend([
        "",
        "## LONG vs SHORT",
        "",
        "| Regla | Transición | N | Lead mediano | Adverso evitado ATR | False release |",
        "|---|---|---:|---:|---:|---:|",
    ])
    for rule in RULE_NAMES:
        for transition in ("LONG_TO_BEARISH", "SHORT_TO_BULLISH"):
            item = _row(rules, "ALL", transition, rule)
            lines.append(
                f"| {rule} | {transition} | {item['episodes_detected']:,} | {_fmt(item['median_lead_minutes'])}m | "
                f"{_fmt(item['median_adverse_move_avoided_atr'], 3)} | {_fmt(item['false_release_rate_pct'])}% |"
            )
    lines.extend([
        "",
        "## Mejor regla",
        "",
        f"- Mejor ranking diagnóstico: **{best['rule']}**.",
        f"- Pasa criterio para PAPER diagnostic: **{'SÍ' if best['qualifies_for_paper_diagnostic'] else 'NO'}**.",
        f"- OOS: cobertura score-lag {_fmt(best['oos_coverage_score_lag_pct'])}%, false release {_fmt(best['oos_false_release_rate_pct'])}%, adverso evitado mediano {_fmt(best['oos_median_adverse_avoided_atr'], 3)} ATR.",
        "",
        "El ranking solo ordena cinco reglas predefinidas mediante cobertura OOS × precisión de release × adverso evitado; no optimiza pesos ni umbrales.",
        "",
        "## Definición de false release",
        "",
        f"- TRUE_RELEASE: el movimiento contrario alcanza {OPPOSITE_MOVE_ATR:.2f} ATR antes de que el precio retome {ORIGINAL_RESUME_ATR:.2f} ATR en la dirección vieja.",
        f"- FALSE_RELEASE: la dirección vieja retoma {ORIGINAL_RESUME_ATR:.2f} ATR antes de un movimiento contrario de {OPPOSITE_MOVE_ATR:.2f} ATR.",
        "- Si ambos niveles se tocan dentro de la misma vela M15, el caso es AMBIGUOUS y se excluye de la tasa true/false.",
        "- El coste de neutralizar temprano es oportunidad dejada de capturar, no una pérdida monetaria ejecutada.",
        "",
        "## Limitaciones",
        "",
        "- Los 1.331 episodios incluyen cohortes M15/H1/H4 que pueden solaparse temporalmente; no equivalen a trades independientes.",
        "- La secuencia posterior usa OHLC M15. El orden intrabar es desconocido cuando ambos umbrales aparecen en una vela.",
        "- No existe histórico completo de noticias, patrones o triggers intrabar del EA; el score reproducido sigue siendo parcial.",
        "- DMI, RSI, EMA20, ADX y volumen se reconstruyen únicamente desde H1 cerrado disponible en el timestamp de observación.",
        "- Una neutralización útil históricamente no demuestra que deba añadirse al score o a ejecución live.",
        "",
    ])
    return "\n".join(lines)


def analyze_bias_release(
    input_dir: Path,
    output_dir: Path,
    *,
    stale_path: Path | None = None,
    symbol: str = "XAUUSD",
    point_size: float = 0.01,
) -> dict:
    input_dir, output_dir = Path(input_dir), Path(output_dir)
    stale_path = Path(stale_path) if stale_path is not None else output_dir / "h1_behavior_shift.csv"
    observations, _ = _load_observations(input_dir / "historical_observations.jsonl")
    _merge_decision_enrichment(observations, input_dir / "historical_decisions.jsonl")
    m15_observations = sorted(
        [
            item for item in observations
            if item.get("timeframe") == "M15"
            and _is_number(item.get("long_score"))
            and _is_number(item.get("short_score"))
            and int(item.get("observed_at") or item["_timestamp_ms"]) <= int(item["_timestamp_ms"])
        ],
        key=lambda item: (item["_timestamp_ms"], item["event_id"]),
    )
    if not m15_observations:
        raise AnalysisError("no enriched causal M15 observations")
    episodes = load_stale_episodes(stale_path)
    if not episodes:
        raise AnalysisError(f"no baseline stale episodes in {stale_path}")
    h1_resolver = H1CausalResolver(input_dir, symbol=symbol, point_size=point_size)
    m15_bars = _load_bars(input_dir, symbol, "M15")
    episode_states, events = detect_rule_events(episodes, m15_observations, h1_resolver, m15_bars)
    rules = summarize_rules(episode_states, events)
    assessment = candidate_assessment(rules)
    best = assessment[0]
    counts = Counter(item["transition"] for item in episode_states)
    summary = {
        "analysis_only": True,
        "diagnostic_only": True,
        "score_effect": 0,
        "universe": {
            "stale_episodes": len(episode_states),
            "evaluable_episodes": sum(item["snapshots"] > 0 for item in episode_states),
            "score_lag_episodes": sum(item["score_lag"] for item in episode_states),
            "long_to_bearish": counts["LONG_TO_BEARISH"],
            "short_to_bullish": counts["SHORT_TO_BULLISH"],
            "periods": dict(Counter(item["period"] for item in episode_states)),
        },
        "definitions": {
            "opposite_significant_atr": OPPOSITE_MOVE_ATR,
            "original_resume_atr": ORIGINAL_RESUME_ATR,
            "acceleration_min_abs": ACCELERATION_MIN_ABS,
            "displacement_min_abs": DISPLACEMENT_MIN_ABS,
            "minimum_rule_sample_per_period": MIN_RULE_SAMPLE_PER_PERIOD,
        },
        "candidate_assessment": assessment,
        "best_rule": best,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(output_dir / "bias_release_rules.csv", list(rules[0].keys()), rules)
    if events:
        _write_csv(output_dir / "bias_release_events.csv", list(events[0].keys()), events)
    else:
        _write_csv(output_dir / "bias_release_events.csv", ["episode_id", "rule"], [])
    _atomic_write(output_dir / "bias_release_analysis.md", _markdown(summary, rules))
    return summary


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    root = Path(__file__).resolve().parent
    parser.add_argument("--input-dir", type=Path, default=root / "output")
    parser.add_argument("--output-dir", type=Path, default=root / "analysis")
    parser.add_argument("--stale-path", type=Path)
    parser.add_argument("--symbol", default="XAUUSD")
    parser.add_argument("--point-size", type=float, default=0.01)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_argument_parser().parse_args(argv)
    try:
        summary = analyze_bias_release(
            arguments.input_dir,
            arguments.output_dir,
            stale_path=arguments.stale_path,
            symbol=arguments.symbol,
            point_size=arguments.point_size,
        )
    except (AnalysisError, OSError, ValueError) as error:
        print(f"[BIAS_RELEASE][ERROR] {error}")
        return 2
    print(f"[BIAS_RELEASE] stale_episodes={summary['universe']['stale_episodes']}")
    print(f"[BIAS_RELEASE] score_lag_episodes={summary['universe']['score_lag_episodes']}")
    print(f"[BIAS_RELEASE] best_rule={summary['best_rule']['rule']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
