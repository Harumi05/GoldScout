"""Post-freeze, read-only regime/outcome segmentation (gross price-return proxies).

No exact historical fills, TP/SL or executed trade tape exists in this dataset.
Therefore expectancy and profit factor here are *directional underlying returns*,
not realized trade R/PnL. Outcomes are joined only after causal labels are fixed.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
import io
import json
import math
from pathlib import Path
import statistics
from typing import Iterable, Mapping

from research.gold_regime_engine import PARAMETER_VERSION, _read_jsonl


SAMPLE_SUFFICIENT = 100
SAMPLE_THIN = 30
HORIZON = "1h"
SETUPS = ("MOMENTUM", "CONTINUATION", "BREAKOUT", "PULLBACK", "RECOVERY")
SPLITS = ("TRAIN", "VALIDATION", "OOS")


def sample_status(count: int) -> str:
    return "SUFFICIENT" if count >= SAMPLE_SUFFICIENT else "THIN_SAMPLE" if count >= SAMPLE_THIN else "INSUFFICIENT"


def causal_direction(decision: Mapping[str, object]) -> str | None:
    direction = decision.get("armed_direction")
    if direction in ("LONG", "SHORT"):
        return direction
    long, short = decision.get("long_score"), decision.get("short_score")
    if isinstance(long, (int, float)) and isinstance(short, (int, float)):
        if long > short:
            return "LONG"
        if short > long:
            return "SHORT"
    return None


def causal_setups(observation: Mapping[str, object], direction: str) -> list[str]:
    """Direct persisted flags only; CONTINUATION has no exact persisted flag."""
    return [name for name in ("MOMENTUM", "BREAKOUT", "PULLBACK", "RECOVERY")
            if observation.get(name.lower()) == direction]


def directional_outcome(outcome: Mapping[str, object], direction: str) -> tuple[float, float, float] | None:
    keys = ("future_return_1h", "mfe_1h", "mae_1h")
    values = [outcome.get(key) for key in keys]
    if (outcome.get("completed_horizon") != HORIZON or
            not all(isinstance(value, (int, float)) and math.isfinite(value) for value in values)):
        return None
    future_return, mfe, mae = values
    return (float(future_return), float(mfe), float(mae)) if direction == "LONG" else (
        -float(future_return), -float(mae), -float(mfe))


def summarize(values: list[tuple[float, float, float]]) -> dict:
    count = len(values)
    returns = [item[0] for item in values]
    profit = sum(value for value in returns if value > 0)
    loss = -sum(value for value in returns if value < 0)
    return {"sample": count, "sample_status": sample_status(count),
            "win_rate_proxy": sum(value > 0 for value in returns) / count if count else None,
            "expectancy_price_return": statistics.fmean(returns) if count else None,
            "profit_factor_proxy": profit / loss if loss > 0 else None,
            "median_mfe": statistics.median(item[1] for item in values) if count else None,
            "median_mae": statistics.median(item[2] for item in values) if count else None}


def _unique_map(path: Path, key: str) -> dict[str, dict]:
    result: dict[str, dict] = {}
    for row in _read_jsonl(path):
        identity = row.get(key)
        if not isinstance(identity, str) or not identity:
            raise ValueError(f"missing {key} in {path}")
        if identity in result and result[identity] != row:
            raise ValueError(f"conflicting duplicate {key}={identity}")
        result[identity] = row
    return result


def _outcomes(path: Path) -> dict[str, dict]:
    completed: dict[str, dict] = {}
    for row in _read_jsonl(path):
        if row.get("completed_horizon") != HORIZON:
            continue
        event_id = row.get("event_id")
        if not isinstance(event_id, str) or not event_id:
            raise ValueError("outcome lacks event_id")
        if event_id in completed and completed[event_id] != row:
            raise ValueError(f"conflicting 1h outcome for {event_id}")
        completed[event_id] = row
    return completed


def analyze_rows(regimes: Iterable[dict], observations: Mapping[str, dict],
                 outcomes: Mapping[str, dict]) -> tuple[list[dict], dict]:
    groups: dict[tuple[str, str, str, str, str, str], list[tuple[float, float, float]]] = defaultdict(list)
    distribution: Counter[str] = Counter()
    transitions: Counter[str] = Counter()
    durations: dict[str, list[int]] = defaultdict(list)
    last_h1 = None
    current_regime = None
    current_raw_regime = None
    raw_switches = 0
    filtered_switches = 0
    run = 0
    total = causal = valid = ambiguous = 0
    seen: set[str] = set()
    previous_time = -1
    for row in regimes:
        total += 1
        if row.get("diagnostic_only") is not True or row.get("score_effect") != 0 or row.get("parameter_version") != PARAMETER_VERSION:
            raise ValueError("regime contract is not diagnostic v1")
        event_id = row["event_id"]
        decision_time = row.get("decision_time")
        if (event_id in seen or not isinstance(decision_time, int) or
                decision_time < previous_time):
            raise ValueError("duplicate or out-of-order causal regime row")
        seen.add(event_id)
        previous_time = decision_time
        for timeframe in ("m5", "h1", "h4"):
            bar = row.get(timeframe, {})
            if bar and (bar.get("available_at", decision_time + 1) > decision_time or
                        bar.get("end", decision_time + 1) > decision_time):
                raise ValueError("regime row contains future/open bar")
        regime, split = row["gold_regime"], row["split"]
        if split not in SPLITS:
            raise ValueError("invalid temporal split")
        distribution[f"{split}:{regime}"] += 1
        h1_id = row.get("h1", {}).get("bar_id")
        if h1_id is not None and h1_id != last_h1:
            raw_regime = row.get("h1", {}).get("state", {}).get("gold_regime", "UNKNOWN")
            if current_raw_regime is not None and raw_regime != current_raw_regime:
                raw_switches += 1
            current_raw_regime = raw_regime
            if regime == current_regime:
                run += 1
            else:
                if current_regime is not None:
                    durations[current_regime].append(run)
                    transitions[f"{current_regime}->{regime}"] += 1
                    filtered_switches += 1
                current_regime, run = regime, 1
            last_h1 = h1_id
        obs = observations.get(event_id)
        if obs is None or obs.get("observed_at") != row.get("decision_time"):
            continue
        direction = causal_direction(row)
        if direction is None:
            ambiguous += 1
            continue
        causal += 1
        future = outcomes.get(event_id)
        measured = directional_outcome(future, direction) if future else None
        if measured is None:
            continue
        valid += 1
        stage = ("ARMED_PROXY" if row.get("decision") == "ARMED" else
                 "NO_TRADE_PROXY" if row.get("decision") == "NO_TRADE" else "OTHER_DIAGNOSTIC")
        labels = causal_setups(obs, direction)
        h1_transition = row.get("h1", {}).get("state", {}).get("transition_state", "UNKNOWN")
        for group in {(split, regime, "ALL", direction, "ALL", "ALL"),
                      (split, regime, stage, direction, "ALL", "ALL"),
                      (split, regime, "ALL", "ALL", "ALL", "ALL"),
                      (split, "ALL", stage, direction, "ALL", h1_transition)}:
            groups[group].append(measured)
        for label in labels:
            groups[(split, regime, stage, direction, label, "ALL")].append(measured)
    if current_regime is not None:
        durations[current_regime].append(run)
    rows = []
    for (split, regime, stage, direction, setup, transition_state), values in sorted(groups.items()):
        rows.append({"split": split, "gold_regime": regime, "stage": stage,
                     "direction": direction, "setup": setup, "transition_state": transition_state,
                     "horizon": HORIZON,
                     **summarize(values)})
    return rows, {"total_regime_rows": total, "causal_direction_rows": causal,
                  "valid_outcome_rows": valid, "ambiguous_direction_rows": ambiguous,
                  "distribution": dict(distribution), "transitions": dict(transitions),
                  "raw_h1_switches": raw_switches, "hysteresis_h1_switches": filtered_switches,
                  "average_duration_h1_bars": {name: statistics.fmean(lengths) for name, lengths in durations.items()},
                  "sample_policy": {"sufficient": SAMPLE_SUFFICIENT, "thin": SAMPLE_THIN},
                  "unavailable": ["realized trade PnL/R, exact fill/TP/SL path", "CONTINUATION setup flag",
                                  "session attribution (broker clock UNKNOWN)"]}


def write_reports(rows: list[dict], meta: dict, md_path: Path, csv_path: Path) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    columns = ("split", "gold_regime", "stage", "direction", "setup", "transition_state", "horizon", "sample",
               "sample_status", "win_rate_proxy", "expectancy_price_return",
               "profit_factor_proxy", "median_mfe", "median_mae")
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=columns)
    writer.writeheader()
    writer.writerows(rows)
    csv_path.write_text(buffer.getvalue(), encoding="utf-8")
    lines = ["# Gold Regime V1 — offline diagnostic", "",
             "No EA/score effect. These are **gross directional 1h XAUUSD price-return proxies**, "
             "not executed-trade win rate, realized expectancy R or realized profit factor. "
             "Historical decisions contain no filled trade tape or reproducible TP/SL sequence.", "",
             f"Rows: {meta['total_regime_rows']}; causal directions: {meta['causal_direction_rows']}; "
             f"valid 1h outcomes: {meta['valid_outcome_rows']}; ambiguous: {meta['ambiguous_direction_rows']}.",
             f"Sample policy fixed pre-analysis: >= {SAMPLE_SUFFICIENT} SUFFICIENT; "
             f"{SAMPLE_THIN}-{SAMPLE_SUFFICIENT-1} THIN_SAMPLE; below {SAMPLE_THIN} INSUFFICIENT.", "",
             "## Distribution and stability", "",
             "| Split:regime | Decision rows |", "|---|---:|"]
    lines.extend(f"| {key} | {count} |" for key, count in sorted(meta["distribution"].items()))
    lines.extend(["", "Mean run duration (closed H1 bars represented in decisions): " + json.dumps(meta["average_duration_h1_bars"], sort_keys=True),
                  f"", f"Raw H1 switches: {meta['raw_h1_switches']}; "
                  f"after hysteresis: {meta['hysteresis_h1_switches']} (no outcomes used).",
                  "", "Transitions: " + json.dumps(meta["transitions"], sort_keys=True), "",
                  "## TRAIN / VALIDATION / OOS — sufficient groups only", "",
                  "| Split | Regime | Stage | Direction | Setup | N | Win proxy | Mean return | PF proxy |",
                  "|---|---|---|---|---|---:|---:|---:|---:|"])
    for row in rows:
        if row["sample_status"] == "SUFFICIENT" and row["setup"] != "ALL":
            lines.append(f"| {row['split']} | {row['gold_regime']} | {row['stage']} | {row['direction']} | "
                         f"{row['setup']} | {row['sample']} | {row['win_rate_proxy']:.3f} | "
                         f"{row['expectancy_price_return']:.5f} | "
                         f"{row['profit_factor_proxy']:.3f} |" if row["profit_factor_proxy"] is not None else
                         f"| {row['split']} | {row['gold_regime']} | {row['stage']} | {row['direction']} | "
                         f"{row['setup']} | {row['sample']} | {row['win_rate_proxy']:.3f} | "
                         f"{row['expectancy_price_return']:.5f} | undefined |")
    lines.extend(["", "## H1 STABLE versus TRANSITION (ARMED proxy, not filled trades)", "",
                  "| Split | Transition | Direction | N | Sample | Mean 1h return proxy | PF proxy |",
                  "|---|---|---|---:|---|---:|---:|"])
    for row in rows:
        if (row["gold_regime"] == "ALL" and row["stage"] == "ARMED_PROXY" and
                row["transition_state"] in {"STABLE", "TRANSITION", "SHOCK"}):
            sufficient = row["sample_status"] == "SUFFICIENT"
            pf = (f"{row['profit_factor_proxy']:.3f}" if row["profit_factor_proxy"] is not None
                  else "undefined") if sufficient else "not reported"
            expectancy = f"{row['expectancy_price_return']:.5f}" if sufficient else "not reported"
            lines.append(f"| {row['split']} | {row['transition_state']} | {row['direction']} | "
                         f"{row['sample']} | {row['sample_status']} | "
                         f"{expectancy} | {pf} |")
    by_segment: dict[tuple[str, str, str], dict[str, dict]] = defaultdict(dict)
    for row in rows:
        if row["stage"] == "ARMED_PROXY" and row["setup"] != "ALL" and row["transition_state"] == "ALL":
            by_segment[(row["gold_regime"], row["direction"], row["setup"])][row["split"]] = row
    lines.extend(["", "## Cross-split sign consistency (descriptive, not a trading rule)", ""])
    consistent = []
    for key, periods in sorted(by_segment.items()):
        if all(periods.get(split, {}).get("sample_status") == "SUFFICIENT" for split in SPLITS):
            values = [periods[split]["expectancy_price_return"] for split in SPLITS]
            if all(value > 0 for value in values) or all(value < 0 for value in values):
                consistent.append((key, values, [periods[split]["sample"] for split in SPLITS]))
    if consistent:
        for key, values, samples in consistent:
            lines.append(f"- {key[0]} / {key[1]} / {key[2]}: N={samples}; mean returns "
                         f"TRAIN/VALIDATION/OOS={[round(value, 6) for value in values]}. "
                         "Observational only; costs and fills not simulated.")
    else:
        lines.append("No setup × direction × regime segment has sufficient samples and the same return sign in all three periods.")
    lines.extend(["", "## Interpretation limits", "",
                  "- Setup labels are non-exclusive persisted M15 flags; CONTINUATION is unavailable, not inferred.",
                  "- ARMED_PROXY is not a filled trade. STABLE/TRANSITION comparisons are ARMED proxies, not opened positions.",
                  "- These observational associations do not establish a profitable rule or license PAPER/live promotion.",
                  "- Market-session attribution remains forbidden until broker clock/rollover are independently verified.",
                  "- No OOS outcome was used in threshold fitting or regime construction.", ""])
    md_path.write_text("\n".join(lines), encoding="utf-8")


def run(root: Path, regimes_path: Path, md_path: Path, csv_path: Path) -> dict:
    observations = _unique_map(root / "historical_observations.jsonl", "event_id")
    outcomes = _outcomes(root / "historical_outcomes.jsonl")
    rows, meta = analyze_rows(_read_jsonl(regimes_path), observations, outcomes)
    write_reports(rows, meta, md_path, csv_path)
    return {**meta, "csv_rows": len(rows)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-root", type=Path, default=Path("research/output"))
    parser.add_argument("--regimes", type=Path, default=Path("research/output/regimes/gold_regime_v1.jsonl"))
    parser.add_argument("--markdown", type=Path, default=Path("research/analysis/gold_regime_v1.md"))
    parser.add_argument("--csv", type=Path, default=Path("research/analysis/gold_regime_v1.csv"))
    args = parser.parse_args()
    print(json.dumps(run(args.input_root, args.regimes, args.markdown, args.csv), sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
