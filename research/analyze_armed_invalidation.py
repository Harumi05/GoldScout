"""Historical evaluation of GoldScout ARMED invalidation V1.

The module is diagnostic-only. It reconstructs one armed episode per H1 cycle
from closed M15 observations, resolves H1 RSI/ATR only as-of each checkpoint,
and applies the same conservative rule used by the optional EA feature:

* confirmed opposite M15 breakout from confirmed pivots;
* H1 RSI >=55 against SHORT or <=45 against LONG;
* displacement from the current H1 open to the closed M15 close >=1.5 H1 ATR.

Outcomes produced from historical ticks are joined only after a cancellation to
label what happened later. They are never inputs to the trigger.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
import io
import json
from pathlib import Path
import statistics
from typing import Mapping, Sequence

from research.analyze_bias_release import H1CausalResolver
from research.analyze_historical_dataset import (
    PERIODS,
    _atomic_write,
    _is_number,
    _load_observations,
    _load_outcomes,
    _merge_decision_enrichment,
)


MIN_IMPULSE_ATR = 1.5
HOUR_MS = 3_600_000
SOURCE = "HISTORICAL_MT5_TICKS"


def evaluate_invalidation_v1(
    armed_direction: str,
    breakout_direction: str,
    rsi: float | None,
    signed_impulse_atr: float | None,
) -> tuple[str, str]:
    """Return CANCEL/KEEP using only causal values available at the checkpoint."""

    armed = str(armed_direction).upper()
    breakout = str(breakout_direction or "NONE").upper()
    if armed not in {"LONG", "SHORT"}:
        return "KEEP", "NO_ARMED_SETUP"
    expected = "SHORT" if armed == "LONG" else "LONG"
    if breakout != expected:
        return "KEEP", "NO_CONFIRMED_COUNTER_BREAKOUT"
    if not _is_number(rsi) or not ((armed == "SHORT" and float(rsi) >= 55.0) or (armed == "LONG" and float(rsi) <= 45.0)):
        return "KEEP", "RSI_NOT_CONFIRMED"
    expected_sign = 1.0 if breakout == "LONG" else -1.0
    if not _is_number(signed_impulse_atr) or expected_sign * float(signed_impulse_atr) < MIN_IMPULSE_ATR:
        return "KEEP", "IMPULSE_BELOW_THRESHOLD"
    return "CANCEL", "COUNTER_BREAKOUT_CONFIRMED"


def infer_setup(observation: Mapping[str, object], direction: str) -> str:
    structure = observation.get("h1_structure") if isinstance(observation.get("h1_structure"), dict) else {}
    suffix = "long" if direction == "LONG" else "short"
    if structure.get(f"breakout_{suffix}"):
        return "BREAKOUT"
    if structure.get(f"pullback_{suffix}"):
        return "PULLBACK"
    if structure.get(f"momentum_{suffix}"):
        return "MOMENTUM"
    return f"CONTINUATION {direction}"


def _m15_breakout(observation: Mapping[str, object]) -> str:
    timing = observation.get("m15_timing") if isinstance(observation.get("m15_timing"), dict) else {}
    if not timing.get("available"):
        return "NONE"
    breakout = str(timing.get("breakout") or "NONE").upper()
    return breakout if breakout in {"LONG", "SHORT"} else "NONE"


def _checkpoint_features(
    observation: Mapping[str, object],
    h1_open: float,
    resolver: H1CausalResolver,
) -> dict[str, object]:
    context = resolver.resolve(int(observation["_timestamp_ms"]))
    if context is None:
        return {"available": False}
    current = context["current"]
    rsi = current.indicators.get("rsi")
    atr = current.indicators.get("atr")
    close = observation.get("close")
    if not all(_is_number(value) for value in (rsi, atr, close)) or float(atr) <= 0.0:
        return {"available": False}
    return {
        "available": True,
        "rsi": float(rsi),
        "atr": float(atr),
        "signed_impulse_atr": (float(close) - h1_open) / float(atr),
        "breakout_direction": _m15_breakout(observation),
        "resolved_h1_close_timestamp": int(context["resolved_close_timestamp"]),
    }


def _directed_outcome(outcome: Mapping[str, object], direction: str, price: float, atr: float) -> dict[str, float]:
    future_return = float(outcome["future_return"])
    mfe = float(outcome["mfe"])
    mae = float(outcome["mae"])
    if direction == "SHORT":
        future_return, mfe, mae = -future_return, -mae, -mfe
    return {
        "future_return": future_return,
        "future_return_atr": future_return * price / atr,
        "mfe_atr": mfe * price / atr,
        "mae_atr": mae * price / atr,
    }


def classify_canceled_outcome(values: Mapping[str, float]) -> str:
    """Conservative diagnostic labels; unclassified paths stay INCONCLUSIVE."""

    if values["future_return_atr"] <= -0.5 and values["mae_atr"] <= -1.0:
        return "SAVED_LOSER"
    if values["future_return_atr"] > 0.0 and values["mfe_atr"] >= 1.0:
        return "KILLED_WINNER"
    return "INCONCLUSIVE"


def _periods(episodes: Sequence[dict]) -> None:
    ordered = sorted(episodes, key=lambda row: (row["armed_at"], row["episode_id"]))
    train_end = int(len(ordered) * 0.60)
    validation_end = train_end + int(len(ordered) * 0.20)
    for index, row in enumerate(ordered):
        row["period"] = "TRAIN" if index < train_end else "VALIDATION" if index < validation_end else "OOS"


def build_armed_episodes(
    observations: Sequence[dict],
    outcomes: Mapping[str, Mapping[str, Mapping[str, object]]],
    resolver: H1CausalResolver,
) -> list[dict]:
    """Build one causal armed episode per H1 cycle from M15 checkpoints."""

    m15 = [
        row for row in observations
        if "-M15-" in str(row.get("event_id", "")) and row.get("decision") == "ARMED"
        and str(row.get("armed_direction", "")).upper() in {"LONG", "SHORT"}
    ]
    cycles: dict[int, list[dict]] = defaultdict(list)
    for row in m15:
        timestamp = int(row.get("timestamp", row["_timestamp_ms"]))
        cycles[timestamp // HOUR_MS].append(row)

    episodes: list[dict] = []
    for cycle, checkpoints in sorted(cycles.items()):
        checkpoints.sort(key=lambda row: (row["_timestamp_ms"], row["event_id"]))
        first = checkpoints[0]
        direction = str(first["armed_direction"]).upper()
        setup = infer_setup(first, direction)
        h1_open = float(first["open"])
        episode = {
            "episode_id": f"ARMED-{cycle}-{direction}-{setup}",
            "armed_event_id": first["event_id"],
            "armed_at": int(first["_timestamp_ms"]),
            "direction": direction,
            "setup": setup,
            "armed_score": int(first.get("long_score" if direction == "LONG" else "short_score") or 0),
            "cancelled": False,
            "cancel_event_id": "",
            "cancelled_at": None,
            "time_to_cancel_minutes": None,
            "reason": "NO_V1_TRIGGER",
            "breakout_direction": "NONE",
            "rsi": None,
            "impulse_atr": None,
            "atr": None,
            "future_return_atr": None,
            "mfe_atr": None,
            "mae_atr": None,
            "outcome_label": "NOT_CANCELLED",
            "score_effect": 0,
            "diagnostic_only": True,
        }
        for checkpoint in checkpoints:
            features = _checkpoint_features(checkpoint, h1_open, resolver)
            if not features.get("available"):
                continue
            action, reason = evaluate_invalidation_v1(
                direction,
                str(features["breakout_direction"]),
                float(features["rsi"]),
                float(features["signed_impulse_atr"]),
            )
            if action != "CANCEL":
                continue
            episode.update({
                "cancelled": True,
                "cancel_event_id": checkpoint["event_id"],
                "cancelled_at": int(checkpoint["_timestamp_ms"]),
                "time_to_cancel_minutes": (int(checkpoint["_timestamp_ms"]) - int(first["_timestamp_ms"])) / 60_000.0,
                "reason": reason,
                "breakout_direction": features["breakout_direction"],
                "rsi": features["rsi"],
                "impulse_atr": abs(float(features["signed_impulse_atr"])),
                "atr": features["atr"],
                "resolved_h1_close_timestamp": features["resolved_h1_close_timestamp"],
            })
            outcome = outcomes.get(checkpoint["event_id"], {}).get("1h")
            if outcome is not None:
                directed = _directed_outcome(outcome, direction, float(checkpoint["close"]), float(features["atr"]))
                episode.update(directed)
                episode["outcome_label"] = classify_canceled_outcome(directed)
            else:
                episode["outcome_label"] = "OUTCOME_UNAVAILABLE"
            break
        episodes.append(episode)
    _periods(episodes)
    return episodes


def summarize(episodes: Sequence[dict]) -> list[dict]:
    rows: list[dict] = []
    dimensions: list[tuple[str, str, list[dict]]] = []
    for period in ("ALL",) + PERIODS:
        period_rows = list(episodes) if period == "ALL" else [row for row in episodes if row["period"] == period]
        dimensions.append((period, "ALL", period_rows))
        for direction in ("LONG", "SHORT"):
            dimensions.append((period, direction, [row for row in period_rows if row["direction"] == direction]))
        for setup in sorted({row["setup"] for row in period_rows}):
            dimensions.append((period, setup, [row for row in period_rows if row["setup"] == setup]))
    for period, segment, items in dimensions:
        cancelled = [row for row in items if row["cancelled"]]
        labelled = [row for row in cancelled if _is_number(row.get("future_return_atr"))]
        labels = Counter(row["outcome_label"] for row in cancelled)
        proxy = [float(row["future_return_atr"]) for row in labelled]
        rows.append({
            "period": period,
            "segment": segment,
            "armed_setups": len(items),
            "cancelled": len(cancelled),
            "cancellation_rate": len(cancelled) / len(items) if items else 0.0,
            "labelled": len(labelled),
            "saved_losers": labels["SAVED_LOSER"],
            "killed_winners": labels["KILLED_WINNER"],
            "saved_loser_rate": labels["SAVED_LOSER"] / len(labelled) if labelled else 0.0,
            "killed_winner_rate": labels["KILLED_WINNER"] / len(labelled) if labelled else 0.0,
            "baseline_cancelled_expectancy_proxy_atr": statistics.fmean(proxy) if proxy else None,
            "invalidation_cancelled_expectancy_proxy_atr": 0.0 if proxy else None,
            "expectancy_delta_atr": -statistics.fmean(proxy) if proxy else None,
            "median_mfe_atr": statistics.median(float(row["mfe_atr"]) for row in labelled) if labelled else None,
            "median_mae_atr": statistics.median(float(row["mae_atr"]) for row in labelled) if labelled else None,
            "median_minutes_to_cancel": statistics.median(float(row["time_to_cancel_minutes"]) for row in cancelled) if cancelled else None,
        })
    return rows


def recommendation(summary_rows: Sequence[dict]) -> str:
    rows = {(row["period"], row["segment"]): row for row in summary_rows}
    validation = rows.get(("VALIDATION", "ALL"), {})
    oos = rows.get(("OOS", "ALL"), {})
    if int(oos.get("labelled") or 0) >= 30:
        oos_delta = oos.get("expectancy_delta_atr")
        val_delta = validation.get("expectancy_delta_atr")
        if _is_number(oos_delta) and _is_number(val_delta) and float(oos_delta) > 0.0 and float(val_delta) >= 0.0 and float(oos.get("saved_loser_rate") or 0.0) > float(oos.get("killed_winner_rate") or 0.0):
            return "PAPER CANDIDATE"
        if _is_number(oos_delta) and float(oos_delta) < 0.0:
            return "REJECT"
    return "KEEP DIAGNOSTIC"


def _format(value: object, digits: int = 3) -> str:
    return "—" if not _is_number(value) else f"{float(value):.{digits}f}"


def render_markdown(episodes: Sequence[dict], rows: Sequence[dict], recommendation_value: str) -> str:
    by_key = {(row["period"], row["segment"]): row for row in rows}
    lines = [
        "# Armed Setup Invalidation V1 — diagnóstico histórico",
        "",
        "Regla causal: breakout contrario M15 confirmado con cierre y pivots confirmados + RSI H1 cerrado "
        "(>=55 contra SHORT, <=45 contra LONG) + desplazamiento desde apertura H1 hasta cierre M15 >=1.5 ATR H1.",
        "Outcomes de ticks se usan únicamente después de cancelar para etiquetar. `score_effect=0`; no se invierte dirección.",
        "",
        "## Resultado global y temporal",
        "",
        "| Periodo | Armados | Cancelados | Saved losers | Killed winners | Delta expectancy proxy (ATR) | Mediana min |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for period in ("ALL",) + PERIODS:
        row = by_key[(period, "ALL")]
        lines.append(
            f"| {period} | {row['armed_setups']} | {row['cancelled']} | {row['saved_losers']} "
            f"| {row['killed_winners']} | {_format(row['expectancy_delta_atr'])} | {_format(row['median_minutes_to_cancel'], 1)} |"
        )
    lines += [
        "",
        "Definiciones conservadoras: `SAVED_LOSER` exige cierre 1h <= -0.5 ATR y MAE <= -1 ATR "
        "en la dirección original. `KILLED_WINNER` exige cierre 1h positivo y MFE >= +1 ATR. "
        "El resto es inconcluso. La expectancy es un proxy de retorno a 1h/ATR; cancelar equivale a 0, no a una entrada contraria.",
        "",
        "## LONG / SHORT",
        "",
        "| Periodo | Dirección | Cancelados | Saved rate | Killed rate | Delta ATR |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for period in ("ALL",) + PERIODS:
        for direction in ("LONG", "SHORT"):
            row = by_key[(period, direction)]
            lines.append(
                f"| {period} | {direction} | {row['cancelled']} | {row['saved_loser_rate']:.1%} "
                f"| {row['killed_winner_rate']:.1%} | {_format(row['expectancy_delta_atr'])} |"
            )
    lines += ["", "## Por setup", "", "| Periodo | Setup | Cancelados | Saved | Killed | Delta ATR |", "|---|---|---:|---:|---:|---:|"]
    setup_rows = [row for row in rows if row["segment"] not in {"ALL", "LONG", "SHORT"} and row["cancelled"]]
    for row in setup_rows:
        lines.append(
            f"| {row['period']} | {row['segment']} | {row['cancelled']} | {row['saved_losers']} "
            f"| {row['killed_winners']} | {_format(row['expectancy_delta_atr'])} |"
        )
    lines += [
        "",
        "## Recomendación",
        "",
        f"**{recommendation_value}**",
        "",
        "Limitaciones: replay en checkpoints M15, scoring histórico parcialmente reproducible sin news/pattern bonus intrabar, "
        "y proxy 1h sin simular fill/SL/TP. No demuestra causalidad ni autoriza LIVE/REAL.",
        "",
    ]
    return "\n".join(lines)


def write_event_csv(path: Path, episodes: Sequence[dict]) -> None:
    fields = (
        "episode_id", "period", "armed_event_id", "armed_at", "direction", "setup", "armed_score",
        "cancelled", "cancel_event_id", "cancelled_at", "time_to_cancel_minutes", "reason",
        "breakout_direction", "rsi", "impulse_atr", "atr", "future_return_atr", "mfe_atr",
        "mae_atr", "outcome_label", "score_effect", "diagnostic_only",
    )
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(episodes)
    _atomic_write(path, buffer.getvalue())


def run_analysis(
    observations_path: Path,
    decisions_path: Path,
    outcomes_path: Path,
    replay_output_dir: Path,
    markdown_path: Path,
    csv_path: Path,
) -> dict[str, object]:
    observations, observation_stats = _load_observations(observations_path)
    decision_stats = _merge_decision_enrichment(observations, decisions_path)
    outcomes, _, outcome_stats = _load_outcomes(outcomes_path)
    resolver = H1CausalResolver(replay_output_dir)
    episodes = build_armed_episodes(observations, outcomes, resolver)
    rows = summarize(episodes)
    recommendation_value = recommendation(rows)
    write_event_csv(csv_path, episodes)
    _atomic_write(markdown_path, render_markdown(episodes, rows, recommendation_value))
    all_row = next(row for row in rows if row["period"] == "ALL" and row["segment"] == "ALL")
    return {
        "source": SOURCE,
        "observer_only": True,
        "score_effect": 0,
        "episodes": len(episodes),
        "cancelled": all_row["cancelled"],
        "saved_losers": all_row["saved_losers"],
        "killed_winners": all_row["killed_winners"],
        "expectancy_delta_atr": all_row["expectancy_delta_atr"],
        "recommendation": recommendation_value,
        "observation_stats": observation_stats,
        "decision_stats": decision_stats,
        "outcome_stats": outcome_stats,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Analyze ARMED invalidation V1 without changing live trading")
    parser.add_argument("--observations", type=Path, default=Path("research/output/historical_observations.jsonl"))
    parser.add_argument("--decisions", type=Path, default=Path("research/output/historical_decisions.jsonl"))
    parser.add_argument("--outcomes", type=Path, default=Path("research/output/historical_outcomes.jsonl"))
    parser.add_argument("--replay-output-dir", type=Path, default=Path("research/output"))
    parser.add_argument("--markdown", type=Path, default=Path("research/analysis/armed_invalidation_v1.md"))
    parser.add_argument("--csv", type=Path, default=Path("research/analysis/armed_invalidation_v1.csv"))
    args = parser.parse_args(argv)
    result = run_analysis(args.observations, args.decisions, args.outcomes, args.replay_output_dir, args.markdown, args.csv)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
