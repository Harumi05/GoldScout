"""Offline H1 behavior-shift lead-time analysis for GoldScout.

The analysis consumes append-only historical observations/outcomes plus the
decision enrichment sidecar. It never imports or modifies the live EA and every
derived record is diagnostic-only with score_effect=0.
"""

from __future__ import annotations

import argparse
from bisect import bisect_right
import csv
from collections import Counter
from dataclasses import dataclass
import io
import json
import math
from pathlib import Path
import statistics
from typing import Iterable, Sequence

from research.analyze_historical_dataset import (
    _atomic_write,
    _load_observations,
    _load_outcomes,
    _merge_decision_enrichment,
    _missed_opportunities,
    _stale_bias,
    temporal_split,
)


BEARISH_SHIFT = "POSSIBLE_BEARISH_REVERSAL"
BULLISH_SHIFT = "POSSIBLE_BULLISH_REVERSAL"
MIN_RULE_CASES_PER_PERIOD = 10
MAX_BEHAVIOR_LEAD_H1_BARS = 30


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _median(values: Iterable[float | int | None]) -> float | None:
    usable = [float(value) for value in values if _is_number(value)]
    return statistics.median(usable) if usable else None


def _mean(values: Iterable[float | int | None]) -> float | None:
    usable = [float(value) for value in values if _is_number(value)]
    return statistics.fmean(usable) if usable else None


def _nested(item: dict, *path: str) -> object:
    value: object = item
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _classic_state(item: dict) -> str:
    value = str(_nested(item, "h1_structure", "pivot_state") or "INSUFFICIENT").upper()
    return value if value in {"BULLISH", "BEARISH", "NEUTRAL", "INSUFFICIENT"} else "INSUFFICIENT"


def _behavior_state(item: dict) -> str:
    return str(_nested(item, "h1_regime", "behavior_shift") or "NONE").upper()


def _expected_shift(classic_state: str) -> str:
    return BEARISH_SHIFT if classic_state == "BULLISH" else BULLISH_SHIFT


def _direction_label(classic_state: str) -> str:
    return "LONG_TO_BEARISH" if classic_state == "BULLISH" else "SHORT_TO_BULLISH"


def _adverse_move(sequence: Sequence[dict], signal_index: int, flip_index: int, classic_state: str) -> tuple[float, float | None]:
    signal = sequence[signal_index]
    entry = float(signal["close"])
    if classic_state == "BULLISH":
        move = max(0.0, entry - min(float(item["low"]) for item in sequence[signal_index : flip_index + 1]))
    else:
        move = max(0.0, max(float(item["high"]) for item in sequence[signal_index : flip_index + 1]) - entry)
    atr = _nested(signal, "h1_regime", "atr")
    return move, move / float(atr) if _is_number(atr) and float(atr) > 0.0 else None


def _flatten_signal_metrics(record: dict, signal: dict | None) -> None:
    regime = signal.get("h1_regime", {}) if signal else {}
    record["ema20_relation"] = regime.get("ema20_relation")
    record["recent_acceleration"] = regime.get("recent_acceleration")
    for size in (30, 10, 5, 3):
        window = regime.get(f"window_{size}", {})
        prefix = f"w{size}_"
        for field in (
            "slope_atr_per_bar",
            "net_displacement_atr",
            "bullish_candle_ratio",
            "bearish_candle_ratio",
            "position_in_range",
            "structure",
            "hh",
            "hl",
            "lh",
            "ll",
        ):
            record[prefix + field] = window.get(field)


def h1_classic_transition_rows(observations: Sequence[dict], assignments: dict[str, str]) -> list[dict]:
    """Compare the first diagnostic shift with the next opposite H1 pivot state."""
    sequence = sorted(
        (item for item in observations if str(item.get("timeframe", "")).upper() == "H1"),
        key=lambda item: (item["_timestamp_ms"], item["event_id"]),
    )
    rows: list[dict] = []
    active_state: str | None = None
    episode_start = 0
    for index, item in enumerate(sequence):
        state = _classic_state(item)
        if state not in {"BULLISH", "BEARISH"}:
            continue
        if active_state is None:
            active_state, episode_start = state, index
            continue
        if state == active_state:
            continue
        expected = _expected_shift(active_state)
        signal_index = next(
            (
                candidate
                for candidate in range(index - 1, max(episode_start, index - MAX_BEHAVIOR_LEAD_H1_BARS) - 1, -1)
                if _behavior_state(sequence[candidate]) == expected
            ),
            None,
        )
        detected = signal_index is not None
        signal = sequence[signal_index] if signal_index is not None else None
        reference = signal or item
        lead_bars = index - signal_index if signal_index is not None else None
        lead_minutes = (
            (int(item["_timestamp_ms"]) - int(signal["_timestamp_ms"])) / 60_000.0 if signal is not None else None
        )
        adverse_price, adverse_atr = (
            _adverse_move(sequence, signal_index, index, active_state) if signal_index is not None else (None, None)
        )
        row = {
            "cohort": "H1_CLASSIC_TRANSITION",
            "event_id": reference["event_id"],
            "period": assignments[reference["event_id"]],
            "direction_change": _direction_label(active_state),
            "classic_from": active_state,
            "classic_to": state,
            "episode_start": sequence[episode_start].get("timestamp"),
            "behavior_shift_timestamp": signal.get("timestamp") if signal else None,
            "classic_change_timestamp": item.get("timestamp"),
            "detected_before_classic": detected,
            "lead_minutes": lead_minutes,
            "lead_h1_bars": lead_bars,
            "potential_adverse_avoided_price": adverse_price,
            "potential_adverse_avoided_atr": adverse_atr,
            "diagnostic_only": True,
            "score_effect": 0,
        }
        _flatten_signal_metrics(row, signal)
        rows.append(row)
        active_state, episode_start = state, index
    return rows


def baseline_stale_enrichment(
    stale_rows: Sequence[dict], merged: Sequence[dict], assignments: dict[str, str]
) -> list[dict]:
    """Attach H1 behavior state to the original 1,331 all-timeframe stale episodes."""
    by_timeframe: dict[str, list[dict]] = {}
    for item in merged:
        by_timeframe.setdefault(str(item.get("timeframe", "UNKNOWN")), []).append(item)
    for values in by_timeframe.values():
        values.sort(key=lambda item: (item["_timestamp_ms"], item["event_id"]))
    h1_sequence = by_timeframe.get("H1", [])
    h1_timestamps = [item["_timestamp_ms"] for item in h1_sequence]
    m15_sequence = by_timeframe.get("M15", [])
    m15_timestamps = [item["_timestamp_ms"] for item in m15_sequence]

    def adverse_after_signal(signal: dict, end_ms: int, direction: str) -> tuple[float | None, float | None]:
        start_ms = int(signal["_timestamp_ms"])
        left = bisect_right(m15_timestamps, start_ms)
        right = bisect_right(m15_timestamps, end_ms)
        future = m15_sequence[left:right]
        if not _is_number(signal.get("close")):
            return None, None
        entry = float(signal["close"])
        if direction == "LONG_TO_BEARISH":
            lows = [float(item["low"]) for item in future if _is_number(item.get("low"))]
            move = max(0.0, entry - min(lows)) if lows else 0.0
        else:
            highs = [float(item["high"]) for item in future if _is_number(item.get("high"))]
            move = max(0.0, max(highs) - entry) if highs else 0.0
        atr = _nested(signal, "h1_regime", "atr")
        return move, move / float(atr) if _is_number(atr) and float(atr) > 0.0 else None

    output: list[dict] = []
    for stale in stale_rows:
        start_ms = int(stale["started_at"])
        end_ms = int(stale["changed_at"])
        expected = BEARISH_SHIFT if stale["bias"] == "LONG" else BULLISH_SHIFT
        candidates = [
            item
            for item in h1_sequence
            if start_ms <= item["_timestamp_ms"] < end_ms and _behavior_state(item) == expected
        ]
        signal = candidates[-1] if candidates else None
        lead_h1_bars = (
            bisect_right(h1_timestamps, end_ms) - bisect_right(h1_timestamps, signal["_timestamp_ms"])
            if signal
            else None
        )
        if lead_h1_bars is not None and lead_h1_bars > MAX_BEHAVIOR_LEAD_H1_BARS:
            signal = None
            lead_h1_bars = None
        lead_minutes = (end_ms - signal["_timestamp_ms"]) / 60_000.0 if signal else None
        direction_change = "LONG_TO_BEARISH" if stale["bias"] == "LONG" else "SHORT_TO_BULLISH"
        adverse_price, adverse_atr = (
            adverse_after_signal(signal, end_ms, direction_change) if signal else (None, None)
        )
        row = {
            "cohort": "BASELINE_STALE_EPISODE",
            "event_id": stale["event_id"],
            "period": assignments[stale["event_id"]],
            "direction_change": direction_change,
            "classic_from": "BULLISH" if stale["bias"] == "LONG" else "BEARISH",
            "classic_to": "CHANGED",
            "episode_start": stale["started_at"],
            "behavior_shift_timestamp": signal.get("timestamp") if signal else None,
            "classic_change_timestamp": end_ms,
            "detected_before_classic": signal is not None,
            "lead_minutes": lead_minutes,
            "lead_h1_bars": lead_h1_bars,
            "potential_adverse_avoided_price": adverse_price,
            "potential_adverse_avoided_atr": adverse_atr,
            "diagnostic_only": True,
            "score_effect": 0,
        }
        _flatten_signal_metrics(row, signal)
        output.append(row)
    return output


def _summarize(rows: Sequence[dict]) -> dict:
    result: dict = {}
    for period in ("ALL", "TRAIN", "VALIDATION", "OOS"):
        members = list(rows) if period == "ALL" else [row for row in rows if row["period"] == period]
        detected = [row for row in members if row["detected_before_classic"]]
        result[period] = {
            "episodes": len(members),
            "detected_before_classic": len(detected),
            "detection_rate_pct": len(detected) * 100.0 / len(members) if members else None,
            "median_lead_minutes": _median(row["lead_minutes"] for row in detected),
            "median_lead_h1_bars": _median(row["lead_h1_bars"] for row in detected),
            "median_potential_adverse_avoided_price": _median(
                row["potential_adverse_avoided_price"] for row in detected
            ),
            "median_potential_adverse_avoided_atr": _median(
                row["potential_adverse_avoided_atr"] for row in detected
            ),
            "by_direction": dict(Counter(row["direction_change"] for row in members)),
        }
    return result


def _missed_summary(missed_rows: Sequence[dict], observations: Sequence[dict]) -> dict:
    by_id = {item["event_id"]: item for item in observations}
    shifts = Counter()
    for row in missed_rows:
        shifts[_behavior_state(by_id[row["event_id"]])] += 1
    return {"cases": len(missed_rows), "behavior_shift_at_observation": dict(shifts)}


def _candidate_rules(rows: Sequence[dict]) -> dict:
    detected = [row for row in rows if row["detected_before_classic"]]
    train_acceleration = _median(abs(float(row["recent_acceleration"])) for row in detected if row["period"] == "TRAIN" and _is_number(row["recent_acceleration"]))
    train_displacement = _median(abs(float(row["w3_net_displacement_atr"])) for row in detected if row["period"] == "TRAIN" and _is_number(row["w3_net_displacement_atr"]))

    def aligned_ratio(row: dict) -> bool:
        field = "w3_bearish_candle_ratio" if row["direction_change"] == "LONG_TO_BEARISH" else "w3_bullish_candle_ratio"
        return _is_number(row.get(field)) and float(row[field]) >= 2.0 / 3.0

    def range_extreme(row: dict) -> bool:
        value = row.get("w30_position_in_range")
        return _is_number(value) and (
            (row["direction_change"] == "LONG_TO_BEARISH" and float(value) <= 0.40)
            or (row["direction_change"] == "SHORT_TO_BULLISH" and float(value) >= 0.60)
        )

    rules = {
        "BASE_BEHAVIOR_SHIFT": lambda row: True,
        "STRONG_ACCELERATION_TRAIN_MEDIAN": lambda row: _is_number(row.get("recent_acceleration"))
        and train_acceleration is not None
        and abs(float(row["recent_acceleration"])) >= train_acceleration,
        "STRONG_3BAR_DISPLACEMENT_TRAIN_MEDIAN": lambda row: _is_number(row.get("w3_net_displacement_atr"))
        and train_displacement is not None
        and abs(float(row["w3_net_displacement_atr"])) >= train_displacement,
        "ALIGNED_3BAR_CANDLE_RATIO": aligned_ratio,
        "EXTREME_30BAR_RANGE_POSITION": range_extreme,
    }
    output: dict = {}
    consistent: list[str] = []
    for name, predicate in rules.items():
        periods: dict = {}
        for period in ("TRAIN", "VALIDATION", "OOS"):
            members = [row for row in detected if row["period"] == period and predicate(row)]
            periods[period] = {
                "cases": len(members),
                "median_lead_h1_bars": _median(row["lead_h1_bars"] for row in members),
                "median_adverse_avoided_atr": _median(row["potential_adverse_avoided_atr"] for row in members),
            }
        stable = all(
            periods[period]["cases"] >= MIN_RULE_CASES_PER_PERIOD
            and _is_number(periods[period]["median_lead_h1_bars"])
            and float(periods[period]["median_lead_h1_bars"]) > 0.0
            and _is_number(periods[period]["median_adverse_avoided_atr"])
            and float(periods[period]["median_adverse_avoided_atr"]) > 0.0
            for period in ("TRAIN", "VALIDATION", "OOS")
        )
        output[name] = {"periods": periods, "reasonably_consistent": stable}
        if stable:
            consistent.append(name)
    return {
        "definitions": {
            "strong_acceleration_abs_train_median": train_acceleration,
            "strong_3bar_displacement_abs_train_median": train_displacement,
            "minimum_cases_per_period": MIN_RULE_CASES_PER_PERIOD,
            "maximum_behavior_lead_h1_bars": MAX_BEHAVIOR_LEAD_H1_BARS,
        },
        "rules": output,
        "reasonably_consistent_rules": consistent,
    }


def _csv_text(rows: Sequence[dict]) -> str:
    if not rows:
        return "cohort,event_id\n"
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


def _markdown(summary: dict) -> str:
    stale = summary["baseline_stale_episodes"]
    transitions = summary["h1_classic_transitions"]
    missed = summary["missed_opportunities"]
    lines = [
        "# H1 Regime / Behavior Shift — diagnóstico histórico",
        "",
        "> Solo diagnóstico · score_effect=0 · sin cambios al EA live.",
        "",
        "## Universos",
        "",
        f"- Missed opportunities diagnósticas: {missed['cases']:,}.",
        f"- Episodios stale originales M15/H1/H4: {stale['ALL']['episodes']:,}.",
        f"- Transiciones clásicas H1 por pivots: {transitions['ALL']['episodes']:,}.",
        "",
        "Los episodios stale originales se mantienen separados de las transiciones H1 para no mezclar timeframes.",
        "",
        "## Detección antes de estructura clásica",
        "",
        "| Cohorte | Periodo | Episodios | Antes | Tasa | Lead mediano H1 | Adverso evitable mediano ATR |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for cohort, values in (("STALE ORIGINAL", stale), ("H1 PIVOTS", transitions)):
        for period in ("ALL", "TRAIN", "VALIDATION", "OOS"):
            item = values[period]
            lines.append(
                f"| {cohort} | {period} | {item['episodes']} | {item['detected_before_classic']} | "
                f"{item['detection_rate_pct'] if item['detection_rate_pct'] is not None else 0:.2f}% | "
                f"{item['median_lead_h1_bars'] if item['median_lead_h1_bars'] is not None else 0:.2f} | "
                f"{item['median_potential_adverse_avoided_atr'] if item['median_potential_adverse_avoided_atr'] is not None else 0:.3f} |"
            )
    lines.extend(["", "## Reglas razonablemente consistentes", ""])
    rules = summary["candidate_rules"]["reasonably_consistent_rules"]
    lines.extend([f"- {name}" for name in rules] or ["- Ninguna regla superó el criterio mínimo en TRAIN, VALIDATION y OOS."])
    lines.extend(
        [
            "",
            "## Definiciones y límites",
            "",
            "- Lead time: diferencia entre la alerta behavior shift válida más reciente y el primer estado clásico opuesto.",
            f"- Una alerta expira para esta comparación después de {MAX_BEHAVIOR_LEAD_H1_BARS} velas H1 cerradas.",
            "- Movimiento adverso potencialmente evitable: máxima excursión H1 contraria entre ambos eventos; no es PnL.",
            "- Las reglas fuertes usan umbrales derivados solo de TRAIN; no se ajustaron pesos ni thresholds del EA.",
            "- Una asociación histórica no demuestra que salir antes mejore el resultado ejecutable.",
            "",
        ]
    )
    return "\n".join(lines)


def analyze_h1_behavior_shift(input_dir: Path, output_dir: Path) -> dict:
    input_dir, output_dir = Path(input_dir), Path(output_dir)
    original, _ = _load_observations(input_dir / "historical_observations.jsonl")
    outcomes, _, _ = _load_outcomes(input_dir / "historical_outcomes.jsonl")
    assignments, split_counts = temporal_split(original)
    baseline_stale_rows, baseline_stale_summary = _stale_bias(original, outcomes, assignments, 0.75)
    merged = [dict(item) for item in original]
    enrichment = _merge_decision_enrichment(merged, input_dir / "historical_decisions.jsonl")
    missed_rows, missed = _missed_opportunities(merged, outcomes, assignments, 0.75)
    h1_rows = h1_classic_transition_rows(merged, assignments)
    stale_enriched = baseline_stale_enrichment(baseline_stale_rows, merged, assignments)
    summary = {
        "analysis_only": True,
        "diagnostic_only": True,
        "score_effect": 0,
        "split_counts": split_counts,
        "decision_enrichment": enrichment,
        "missed_opportunities": {**missed, **_missed_summary(missed_rows, merged)},
        "baseline_stale_reference": baseline_stale_summary,
        "baseline_stale_episodes": _summarize(stale_enriched),
        "h1_classic_transitions": _summarize(h1_rows),
        "candidate_rules": _candidate_rules(h1_rows),
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write(output_dir / "h1_behavior_shift.csv", _csv_text([*stale_enriched, *h1_rows]))
    _atomic_write(output_dir / "h1_behavior_shift.md", _markdown(summary))
    _atomic_write(output_dir / "h1_behavior_shift.json", json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    return summary


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    root = Path(__file__).resolve().parent
    parser.add_argument("--input-dir", type=Path, default=root / "output")
    parser.add_argument("--output-dir", type=Path, default=root / "analysis")
    arguments = parser.parse_args(argv)
    summary = analyze_h1_behavior_shift(arguments.input_dir, arguments.output_dir)
    print(f"[BEHAVIOR] stale_episodes={summary['baseline_stale_episodes']['ALL']['episodes']}")
    print(f"[BEHAVIOR] h1_transitions={summary['h1_classic_transitions']['ALL']['episodes']}")
    print(f"[BEHAVIOR] score_effect={summary['score_effect']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
