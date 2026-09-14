"""Final offline A/B validation of CURRENT versus conservative Adaptive Stop v2."""

from __future__ import annotations

import argparse
import csv
import io
from pathlib import Path
from typing import Sequence

from research.analyze_adaptive_stop_v2 import (
    AdaptiveStopState,
    adaptive_detail_rows,
    adaptive_setup,
    aggregate_by_setup,
    aggregate_global,
    simulate_adaptive_tick_order,
)
from research.analyze_historical_dataset import _atomic_write, _load_outcomes
from research.analyze_stop_loss_quality import (
    M1RangeIndex,
    SignalState,
    StopState,
    _is_number,
    build_stop_candidates,
    finalize_tick_metrics,
    prepare_signals,
)
from research.enrich_historical_decisions import _load_bars
from research.tick_historical_replay import IngestionStats, discover_inputs, iter_ticks


ADAPTIVE = "ADAPTIVE_STOP_V2_CONSERVATIVE"
MAX_STOP_ATR_WITH_PRICE_GRID = 2.01


def _copy_stop(name: str, source: StopState) -> AdaptiveStopState:
    return AdaptiveStopState(name, source.price, source.distance, source.distance_atr)


def build_final_ab_candidates(signal: SignalState, entry_price: float) -> dict[str, AdaptiveStopState]:
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

    def wider(floor_name: str) -> StopState:
        floor = base[floor_name]
        return floor if floor.distance > current.distance else current

    setup = adaptive_setup(signal)
    if setup == "MOMENTUM" and signal.direction == "LONG":
        selected = wider("ATR_2_0")
    elif setup in {"CONTINUATION_LONG", "CONTINUATION_SHORT", "PULLBACK"}:
        selected = wider("ATR_1_5")
    else:
        selected = current
    return {
        "CURRENT": _copy_stop("CURRENT", current),
        ADAPTIVE: _copy_stop(ADAPTIVE, selected),
    }


def _index(rows: Sequence[dict]) -> dict[tuple[str, str, str, str], dict]:
    return {
        (row["period"], row["direction"], row["candidate"], row["horizon"]): row
        for row in rows
    }


def evaluate_paper_acceptance(global_rows: Sequence[dict], detail_rows: Sequence[dict]) -> dict:
    indexed = _index(global_rows)
    checks: dict[str, bool] = {}
    for period in ("VALIDATION", "OOS"):
        current = indexed[(period, "ALL", "CURRENT", "4h")]
        adaptive = indexed[(period, "ALL", ADAPTIVE, "4h")]
        checks[f"{period.lower()}_survival_maintained"] = adaptive["survival_rate"] >= current["survival_rate"]
        checks[f"{period.lower()}_premature_not_worse"] = (
            adaptive["premature_stop_rate"] <= current["premature_stop_rate"]
        )
    current_oos = indexed[("OOS", "ALL", "CURRENT", "4h")]
    adaptive_oos = indexed[("OOS", "ALL", ADAPTIVE, "4h")]
    checks["oos_premature_reduced"] = adaptive_oos["premature_stop_rate"] < current_oos["premature_stop_rate"]
    checks["oos_plus_1r_within_1pp"] = adaptive_oos["reached_1r_rate"] >= current_oos["reached_1r_rate"] - 0.01
    checks["oos_plus_2r_within_1pp"] = adaptive_oos["reached_2r_rate"] >= current_oos["reached_2r_rate"] - 0.01
    adaptive_details = [row for row in detail_rows if row["candidate"] == ADAPTIVE]
    max_risk_error = max(
        (abs(float(row["planned_monetary_risk_ratio_vs_current"]) - 1.0) for row in adaptive_details),
        default=0.0,
    )
    max_distance_atr = max((float(row["stop_distance_atr"]) for row in adaptive_details), default=0.0)
    checks["monetary_risk_unchanged"] = max_risk_error <= 1e-9
    checks["no_extreme_stop"] = max_distance_atr <= MAX_STOP_ATR_WITH_PRICE_GRID
    checks["not_train_only"] = all(
        checks[name]
        for name in (
            "validation_survival_maintained",
            "validation_premature_not_worse",
            "oos_survival_maintained",
            "oos_premature_not_worse",
        )
    )
    return {
        "accepted_for_paper": all(checks.values()),
        "checks": checks,
        "max_abs_planned_risk_error": max_risk_error,
        "max_stop_distance_atr": max_distance_atr,
        "tp_reproducible": False,
        "tp_note": "historical sidecar does not reproduce the exact live adaptive TP state; realized R, expectancy, win rate and profit factor are not invented",
    }


def _combined_rows(global_rows: Sequence[dict], setup_rows: Sequence[dict]) -> list[dict]:
    output: list[dict] = []
    for row in global_rows:
        output.append({"scope": "GLOBAL" if row["direction"] == "ALL" else "DIRECTION", "setup": "ALL", **row})
    for row in setup_rows:
        output.append({"scope": "SETUP", **row})
    return output


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
    return f"{100.0 * float(value):.2f}%" if _is_number(value) else "N/A"


def _markdown(global_rows: Sequence[dict], setup_rows: Sequence[dict], acceptance: dict) -> str:
    lines = [
        "# Adaptive Stop v2 Conservative — validación A/B final",
        "",
        "> Diagnóstico histórico · score_effect=0 · riesgo monetario constante.",
        "",
        "No se calcula realized R, expectancy R, win rate ni profit factor porque el TP adaptativo live no puede reproducirse exactamente con el sidecar histórico disponible.",
        "",
        "## Comparación global 4h",
        "",
        "| Candidato | Casos | Distancia ATR | Supervivencia | Prematuro | +0.5R | +1R | +1.5R | +2R | MFE ATR | MAE ATR | Tiempo medio stop |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in global_rows:
        if row["period"] == "ALL" and row["direction"] == "ALL" and row["horizon"] == "4h":
            lines.append(
                f"| {row['candidate']} | {row['cases']} | {row['median_stop_distance_atr']:.3f} | "
                f"{_pct(row['survival_rate'])} | {_pct(row['premature_stop_rate'])} | "
                f"{_pct(row['reached_0.5r_rate'])} | {_pct(row['reached_1r_rate'])} | "
                f"{_pct(row['reached_1.5r_rate'])} | {_pct(row['reached_2r_rate'])} | "
                f"{row['median_mfe_atr']:.3f} | {row['median_mae_atr']:.3f} | "
                f"{row['mean_time_to_stop_minutes']:.1f} min |"
            )
    lines.extend(["", "## TRAIN / VALIDATION / OOS — 4h", "", "| Periodo | Candidato | Supervivencia | Prematuro | +1R | +2R |", "|---|---|---:|---:|---:|---:|"])
    for row in global_rows:
        if row["period"] != "ALL" and row["direction"] == "ALL" and row["horizon"] == "4h":
            lines.append(
                f"| {row['period']} | {row['candidate']} | {_pct(row['survival_rate'])} | "
                f"{_pct(row['premature_stop_rate'])} | {_pct(row['reached_1r_rate'])} | {_pct(row['reached_2r_rate'])} |"
            )
    lines.extend(["", "## LONG / SHORT — 4h", "", "| Dirección | Candidato | Casos | Supervivencia | Prematuro | +1R | +2R |", "|---|---|---:|---:|---:|---:|---:|"])
    for row in global_rows:
        if row["period"] == "ALL" and row["direction"] != "ALL" and row["horizon"] == "4h":
            lines.append(
                f"| {row['direction']} | {row['candidate']} | {row['cases']} | {_pct(row['survival_rate'])} | "
                f"{_pct(row['premature_stop_rate'])} | {_pct(row['reached_1r_rate'])} | {_pct(row['reached_2r_rate'])} |"
            )
    lines.extend(["", "## Por setup — 4h", "", "| Dirección | Setup | Candidato | Casos | Distancia ATR | Supervivencia | Prematuro | +1R | +2R |", "|---|---|---|---:|---:|---:|---:|---:|---:|"])
    for row in setup_rows:
        if row["period"] == "ALL" and row["horizon"] == "4h":
            lines.append(
                f"| {row['direction']} | {row['setup']} | {row['candidate']} | {row['cases']} | "
                f"{row['median_stop_distance_atr']:.3f} | {_pct(row['survival_rate'])} | "
                f"{_pct(row['premature_stop_rate'])} | {_pct(row['reached_1r_rate'])} | {_pct(row['reached_2r_rate'])} |"
            )
    verdict = "PASA" if acceptance["accepted_for_paper"] else "NO PASA"
    lines.extend(["", "## Criterio de aceptación PAPER", "", f"**{verdict}.**", ""])
    for name, passed in acceptance["checks"].items():
        lines.append(f"- {'PASS' if passed else 'FAIL'} — {name}")
    lines.extend(
        [
            "",
            f"- Máxima distancia observada: {acceptance['max_stop_distance_atr']:.6f} ATR.",
            f"- Error máximo de riesgo planificado: {acceptance['max_abs_planned_risk_error']:.3e}.",
            "- Este veredicto solo habilita una hipótesis PAPER; no modifica el EA live.",
            "",
        ]
    )
    return "\n".join(lines)


def analyze_final_ab(input_dir: Path, tick_files: Sequence[Path], output_dir: Path) -> dict:
    input_dir, output_dir = Path(input_dir), Path(output_dir)
    signals, split_counts, enrichment = prepare_signals(input_dir)
    outcomes, _, _ = _load_outcomes(input_dir / "historical_outcomes.jsonl")
    stats = IngestionStats()
    tick_report = simulate_adaptive_tick_order(
        signals,
        iter_ticks(tick_files, stats),
        candidate_builder=build_final_ab_candidates,
    )
    ranges = M1RangeIndex(_load_bars(input_dir, "XAUUSD", "M1"))
    finalize_tick_metrics(signals, ranges)
    details = adaptive_detail_rows(signals, outcomes, ranges)
    global_rows = aggregate_global(details)
    setup_rows = aggregate_by_setup(details)
    acceptance = evaluate_paper_acceptance(global_rows, details)
    output_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write(output_dir / "adaptive_stop_final_ab.csv", _csv_text(_combined_rows(global_rows, setup_rows)))
    _atomic_write(output_dir / "adaptive_stop_final_ab.md", _markdown(global_rows, setup_rows, acceptance))
    return {
        "analysis_only": True,
        "observer_only": True,
        "score_effect": 0,
        "risk_percent_changed": False,
        "split_counts": split_counts,
        "decision_enrichment": enrichment,
        "signals_reproducible": len(signals),
        "tick_scan": {
            **tick_report,
            "ticks_valid": stats.ticks_valid,
            "ticks_discarded": stats.ticks_discarded,
            "duplicates": stats.duplicates,
        },
        "acceptance": acceptance,
    }


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
    summary = analyze_final_ab(arguments.input_dir, [item.path for item in discovery.files], arguments.output_dir)
    print(f"[FINAL_AB] signals={summary['signals_reproducible']}")
    print(f"[FINAL_AB] accepted_for_paper={summary['acceptance']['accepted_for_paper']}")
    print(f"[FINAL_AB] score_effect={summary['score_effect']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
