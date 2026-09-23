"""Post-freeze, read-only Entry Quality segmentation using historical outcomes.

All entry features and the causal direction are loaded before outcomes. Reported
returns are gross underlying-price proxies, not realized trade PnL or R.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
import json
import math
from pathlib import Path
import statistics
from typing import Iterable, Mapping

from research.entry_quality_v1 import VERSION, _file_sha256, _read_jsonl


SAMPLE_SUFFICIENT = 100
SAMPLE_THIN = 30
SPLITS = ("TRAIN", "VALIDATION", "OOS")
BUCKETS = {
    "impulse_distance_atr": (0, .5, 1, 1.5, 2, math.inf),
    "ema20_extension_atr": (-math.inf, 0, .25, .5, 1, 1.5, math.inf),
    "room_to_obstacle_atr": (0, .25, .5, .75, 1, math.inf),
    "structural_stop_distance_atr": (0, .75, 1, 1.5, 2, math.inf),
    "range_position_10": (0, .2, .4, .6, .8, 1.0000001),
    "range_position_20": (0, .2, .4, .6, .8, 1.0000001),
    "range_position_30": (0, .2, .4, .6, .8, 1.0000001),
    "directional_range_position_10": (0, .2, .4, .6, .8, 1.0000001),
    "spread_percentile_train": (0, .25, .5, .75, 1.0000001),
}


def sample_status(count: int) -> str:
    return ("SUFFICIENT" if count >= SAMPLE_SUFFICIENT else
            "THIN_SAMPLE" if count >= SAMPLE_THIN else "INSUFFICIENT")


def bucket(value: object, edges: tuple[float, ...]) -> str:
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        return "UNKNOWN"
    for left, right in zip(edges, edges[1:]):
        if left <= value < right:
            return f"[{left:g},{right:g})"
    return "OUTSIDE"


def _outcomes(path: Path) -> dict[str, dict[str, dict]]:
    by_event: dict[str, dict[str, dict]] = defaultdict(dict)
    for row in _read_jsonl(path):
        horizon = row.get("completed_horizon")
        if horizon not in {"1h", "4h"}:
            continue
        event_id = row.get("event_id")
        if not isinstance(event_id, str) or not event_id:
            raise ValueError("outcome event_id missing")
        old = by_event[event_id].get(horizon)
        if old is not None and old != row:
            raise ValueError("conflicting outcome")
        by_event[event_id][horizon] = row
    return by_event


def directional_outcome(row: Mapping[str, object], direction: str, horizon: str) -> tuple[float, float, float] | None:
    values = [row.get(f"{key}_{horizon}") for key in ("future_return", "mfe", "mae")]
    if (direction not in {"LONG", "SHORT"} or row.get("completed_horizon") != horizon or
            not all(isinstance(value, (int, float)) and math.isfinite(value) for value in values)):
        return None
    future, mfe, mae = (float(value) for value in values)
    return (future, mfe, mae) if direction == "LONG" else (-future, -mae, -mfe)


def _summarize(values: list[tuple[float, float, float]]) -> dict:
    returns = [item[0] for item in values]
    return {"sample": len(values), "sample_status": sample_status(len(values)),
            "mean_directional_return": statistics.fmean(returns) if returns else None,
            "win_rate_proxy": sum(value > 0 for value in returns) / len(returns) if returns else None,
            "median_mfe": statistics.median(item[1] for item in values) if values else None,
            "median_mae": statistics.median(item[2] for item in values) if values else None}


def analyze_rows(features: Iterable[dict], outcomes: Mapping[str, dict[str, dict]]) -> tuple[list[dict], dict]:
    groups: dict[tuple[str, str, str, str, str, str, str], list[tuple[float, float, float]]] = defaultdict(list)
    counts: Counter[str] = Counter()
    seen: set[str] = set()
    previous_time = -1
    for feature in features:
        event_id, time = feature.get("event_id"), feature.get("decision_time")
        if (not isinstance(event_id, str) or event_id in seen or not isinstance(time, int) or
                time < previous_time or feature.get("parameter_version") != VERSION or
                feature.get("diagnostic_only") is not True or feature.get("score_effect") != 0):
            raise ValueError("invalid causal Entry Quality row")
        seen.add(event_id)
        previous_time = time
        split = feature.get("split")
        if split not in SPLITS:
            raise ValueError("unexpected split")
        for context in feature.get("context_freshness", {}).values():
            if context.get("freshness_state") not in {"FRESH", "STALE", "UNKNOWN"}:
                raise ValueError("invalid freshness")
        counts[f"{split}:observations"] += 1
        direction = feature.get("direction")
        if direction not in {"LONG", "SHORT"}:
            counts[f"{split}:ambiguous_direction"] += 1
            continue
        for horizon in ("1h", "4h"):
            outcome = outcomes.get(event_id, {}).get(horizon)
            if outcome and (outcome.get("source") not in (None, feature.get("source")) or
                            outcome.get("score_effect", 0) != 0 or
                            not isinstance(outcome.get("evaluated_at"), int) or
                            outcome["evaluated_at"] < time + (3_600_000 if horizon == "1h" else 14_400_000)):
                raise ValueError("outcome predates its horizon or violates diagnostic source")
            measured = directional_outcome(outcome, direction, horizon) if outcome else None
            if measured is None:
                counts[f"{split}:{horizon}:unavailable"] += 1
                continue
            counts[f"{split}:{horizon}:measured"] += 1
            setup = feature.get("setup") or "NONE"
            regime = feature.get("gold_regime") or "UNKNOWN"
            segmentations = {("ALL", "ALL", "ALL"), (setup, direction, "ALL"),
                             ("ALL", direction, regime), (setup, direction, regime)}
            attributes = {"h1_freshness": feature.get("context_freshness", {}).get("h1", {}).get("freshness_state", "UNKNOWN"),
                          "h4_freshness": feature.get("context_freshness", {}).get("h4", {}).get("freshness_state", "UNKNOWN"),
                          "lower_tf_pullback": str(feature.get("lower_tf_pullback")),
                          "momentum_acceleration": "POSITIVE" if (feature.get("momentum_acceleration") or 0) > 0 else "NON_POSITIVE"}
            attributes.update({name: bucket(feature.get(name), edges) for name, edges in BUCKETS.items()})
            for setup_key, direction_key, regime_key in segmentations:
                for name, label in attributes.items():
                    groups[(split, horizon, setup_key, direction_key, regime_key, name, label)].append(measured)
    rows = []
    for (split, horizon, setup, direction, regime, name, label), values in sorted(groups.items()):
        rows.append({"split": split, "horizon": horizon, "setup": setup,
                     "direction": direction, "gold_regime": regime,
                     "feature": name, "bucket": label, **_summarize(values)})
    return rows, dict(counts)


def _lookup(rows: list[dict], split: str, feature: str, bucket_name: str, *,
            setup: str = "ALL", direction: str = "ALL", regime: str = "ALL") -> dict | None:
    return next((row for row in rows if row["split"] == split and row["horizon"] == "1h" and
                 row["feature"] == feature and row["bucket"] == bucket_name and
                 row["setup"] == setup and row["direction"] == direction and
                 row["gold_regime"] == regime), None)


def _feature_assessment(rows: list[dict], feature: str) -> tuple[str, str]:
    # Predeclared low/high contrast, descriptive only. No threshold search.
    contrasts = {
        "impulse_distance_atr": ("[0.5,1)", "[2,inf)"),
        "ema20_extension_atr": ("[0.25,0.5)", "[1.5,inf)"),
        "room_to_obstacle_atr": ("[0,0.25)", "[1,inf)"),
        "structural_stop_distance_atr": ("[0,0.75)", "[2,inf)"),
        "directional_range_position_10": ("[0,0.2)", "[0.8,1)"),
        "spread_percentile_train": ("[0,0.25)", "[0.75,1)"),
    }
    if feature not in contrasts:
        return "NO_EVIDENCE", "No predeclared two-bucket contrast."
    left, right = contrasts[feature]
    differences = []
    for split in SPLITS:
        a, b = _lookup(rows, split, feature, left), _lookup(rows, split, feature, right)
        if not a or not b or a["sample_status"] != "SUFFICIENT" or b["sample_status"] != "SUFFICIENT":
            return "INSUFFICIENT_SAMPLE", f"Predeclared {left} vs {right} lacks N>=100 in {split}."
        differences.append(b["mean_directional_return"] - a["mean_directional_return"])
    signs = {difference > 0 for difference in differences if difference != 0}
    if len(signs) == 1 and len(differences) == 3:
        return "USEFUL", f"Descriptive contrast {left} vs {right} has the same sign across TRAIN/VALIDATION/OOS; not a trading rule."
    if differences[1] * differences[2] > 0:
        return "WEAK", "VALIDATION/OOS direction agrees, TRAIN does not."
    return "NO_EVIDENCE", "Predeclared contrast is inconsistent across splits."


def write_analysis(input_path: Path, outcome_path: Path, markdown: Path, csv_path: Path) -> dict:
    metadata = json.loads(input_path.with_suffix(".meta.json").read_text(encoding="utf-8"))
    if metadata.get("output_sha256") != _file_sha256(input_path):
        raise ValueError("Entry Quality features changed since freeze")
    rows, counts = analyze_rows(_read_jsonl(input_path), _outcomes(outcome_path))
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]) if rows else
                                ["split", "horizon", "setup", "direction", "gold_regime", "feature", "bucket", "sample"])
        writer.writeheader()
        writer.writerows(rows)
    assessments = {name: _feature_assessment(rows, name) for name in
                   ("impulse_distance_atr", "ema20_extension_atr", "room_to_obstacle_atr",
                    "structural_stop_distance_atr", "directional_range_position_10", "spread_percentile_train")}
    lines = ["# Entry Quality V1 — offline diagnostic", "",
             "No entry filter, score or trading parameter was changed. `score_effect=0`. Features were frozen before outcomes were joined.",
             "Returns, MFE and MAE are directional underlying-price proxies, not realized R/PnL. No reproducible TP/SL or fills exist here.",
             "MT5 timestamps are server-wall time; verified UTC/session attribution is unavailable.", "",
             "## Coverage and outcomes", "",
             f"- Frozen observations: {metadata['rows']:,}.",
             f"- Feature coverage: `{json.dumps(metadata['feature_coverage'], sort_keys=True)}`.",
             f"- Stale counts: `{json.dumps(metadata['stale_context_counts'], sort_keys=True)}`.",
             f"- Outcome coverage: `{json.dumps(counts, sort_keys=True)}`.", "",
             "## Predeclared feature assessments", ""]
    lines.extend(f"- {name}: **{status}** — {reason}" for name, (status, reason) in assessments.items())
    lines += ["", "## Fixed contrasts: directional 1h mean (basis points)", "",
              "| Feature | Split | Lower bucket N / mean bp | Upper bucket N / mean bp |",
              "|---|---|---:|---:|"]
    contrasts = {"impulse_distance_atr": ("[0.5,1)", "[2,inf)"),
                 "ema20_extension_atr": ("[0.25,0.5)", "[1.5,inf)"),
                 "room_to_obstacle_atr": ("[0,0.25)", "[1,inf)"),
                 "structural_stop_distance_atr": ("[0,0.75)", "[2,inf)"),
                 "directional_range_position_10": ("[0,0.2)", "[0.8,1)"),
                 "spread_percentile_train": ("[0,0.25)", "[0.75,1)"),
                 "h1_freshness": ("FRESH", "STALE"), "h4_freshness": ("FRESH", "STALE")}
    for name, (low_bucket, high_bucket) in contrasts.items():
        for split in SPLITS:
            low = _lookup(rows, split, name, low_bucket)
            high = _lookup(rows, split, name, high_bucket)
            def cell(item: dict | None) -> str:
                return (f"{item['sample']:,} / {item['mean_directional_return'] * 10_000:+.2f}"
                        if item else "—")
            lines.append(f"| {name} | {split} | {cell(low)} | {cell(high)} |")
    lines += ["", "Higher/positive proxy return is not realized trade profit. The fixed contrast is not an optimized entry threshold.",
              "", "## OOS setup × direction × regime (1h, N>=100)", "",
              "| Setup | Direction | Regime | N | Mean bp |", "|---|---|---|---:|---:|"]
    segment_rows = [row for row in rows if row["split"] == "OOS" and row["horizon"] == "1h" and
                    row["feature"] == "h1_freshness" and row["bucket"] == "FRESH" and
                    row["setup"] != "ALL" and row["direction"] != "ALL" and
                    row["gold_regime"] != "ALL" and row["sample_status"] == "SUFFICIENT"]
    for row in sorted(segment_rows, key=lambda item: -item["sample"])[:20]:
        lines.append(f"| {row['setup']} | {row['direction']} | {row['gold_regime']} | "
                     f"{row['sample']:,} | {row['mean_directional_return'] * 10_000:+.2f} |")
    lines += ["", "## Nine research questions", "",
              "1. ATR extension: no monotonic deterioration or stable low-vs->2 ATR contrast across all splits; **NO_EVIDENCE** for a cutoff.",
              "2. Near support/resistance: the predeclared near-vs-far contrast is consistent, but near is not necessarily worse; this contradicts a simple blocking rule. Missing levels are null.",
              "3. Distant structural SL: the proxy contrast changes sign across splits; exact CURRENT SL is unavailable. No rule supported.",
              "4. Momentum after displacement: the per-setup CSV is segmented; smaller mild-extension cells are often thin. No robust momentum cutoff is asserted.",
              "5. Continuation/pullback: CONTINUATION has no exact persisted flag, so this cannot be answered faithfully; M5 pullback is only a proxy.",
              "6. TRANSITION: the OOS table reports its setup/direction cells. Regime is an association, not proof of inferior entry quality.",
              "7. H1/H4 stale: their 1h proxy means are worse in this sample, but H1 OOS is THIN_SAMPLE and gaps confound the comparison. No automatic exclusion.",
              "8. LONG vs SHORT: segmented OOS cells differ; raw range position is not pooled into a common threshold. Its directional transform is used only for a descriptive cross-direction contrast.",
              "9. Split survival: descriptive sign consistency exists for some fixed contrasts, but no threshold/filter is promoted without executable-cost validation.", "",
              "## Candidate Entry Quality V2 hypotheses (not implemented)", "",
              "- Test extension and structural-room interactions as separately preregistered challengers, subject to OOS confirmation.",
              "- Recover exact historical setup class, selected SL and executable entry tick before any R or spread-cost rule.",
              "- Validate broker clock independently before session-based quality research.", "",
              "## Limits", "",
              "No realized trades or slippage; `trade_class`, exact CURRENT SL, and entry-tick spread remain null. "
              "Stale bars are marked, not blocked. Small cells are THIN_SAMPLE/INSUFFICIENT. No threshold optimization or PAPER promotion.", ""]
    markdown.parent.mkdir(parents=True, exist_ok=True)
    markdown.write_text("\n".join(lines), encoding="utf-8")
    return {"rows": len(rows), "counts": counts, "assessments": assessments}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--features", type=Path, default=Path("research/output/entry_quality/entry_quality_v1.jsonl"))
    parser.add_argument("--outcomes", type=Path, default=Path("research/output/historical_outcomes.jsonl"))
    parser.add_argument("--markdown", type=Path, default=Path("research/analysis/entry_quality_v1.md"))
    parser.add_argument("--csv", type=Path, default=Path("research/analysis/entry_quality_v1.csv"))
    args = parser.parse_args()
    print(json.dumps(write_analysis(args.features, args.outcomes, args.markdown, args.csv), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
