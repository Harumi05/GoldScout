"""CLI: pre-registered session / volatility-regime filter study (offline).

observer_only=True, score_effect=0, strategy_validated=False.

Same candidates, outcomes and purged walk-forward folds as
research.quant.edge_study; evaluates the pre-registered bucket family of
research.quant.session_regime in STRESS single-slot OOS per direction, with
joint BH-FDR and the shared gate. Server hour only: the MT5 UTC offset is not
established, so no named sessions are produced.

    python -m research.quant.session_regime_study --input-dir <research/output> \
        --output-dir research/analysis/session_regime
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from pathlib import Path
from typing import Sequence

from research.quant import barriers, features, stats
from research.quant import edge_study as es
from research.quant import session_regime as sr

GATE_COST = "STRESS"
FOLD_COST = es.LABEL_COST  # folds/purge exactly as edge_study (BASE label ends)
PRIOR_DECLARED_TRIALS = 34  # edge_study n_trials, counted cumulatively for DSR


def build_candidates(data: Path, *, min_score: int, tp_grid: Sequence[float],
                     max_hold_ms: int) -> list[es.Candidate]:
    bars_dir = data / "historical_bars"
    print("[session_regime] loading bars", file=sys.stderr)
    h1 = features.CausalBarSeries(features.load_bars(bars_dir / "XAUUSD_H1.jsonl", timeframe="H1"))
    h4 = features.CausalBarSeries(features.load_bars(bars_dir / "XAUUSD_H4.jsonl", timeframe="H4"))
    m15 = features.CausalBarSeries(features.load_bars(bars_dir / "XAUUSD_M15.jsonl", timeframe="M15"))
    m1 = barriers.M1Path.from_jsonl(bars_dir / "XAUUSD_M1.jsonl")
    print("[session_regime] building candidates/outcomes", file=sys.stderr)
    cands = []
    for raw in es.iter_candidates(data / "historical_decisions.jsonl", min_score=min_score):
        t = raw["observed_at"]
        feats = features.build_features(h1, h4, m15, t)["features"]
        atr = feats.get("h1_atr14")
        stop = (barriers.stop_distance(atr, structure_distance=es.structure_distance(
            h1, t, raw["direction"])) if atr else None)
        cand = es.Candidate(raw["event_id"], t, raw["direction"], raw["score"], feats, stop)
        if stop is not None:
            for cost_name in (FOLD_COST, GATE_COST):
                for tp_r in tp_grid:
                    cand.outcomes[(cost_name, tp_r)] = barriers.triple_barrier(
                        m1, signal_time_ms=t, direction=raw["direction"], stop_dist=stop,
                        tp_r=tp_r, max_hold_ms=max_hold_ms,
                        cost=barriers.COST_SCENARIOS[cost_name])
        cands.append(cand)
    return cands


def oos_assignments(cands: Sequence[es.Candidate], tp_r: float, *, n_folds: int,
                    embargo_ms: int, min_train: int) -> tuple[list[tuple[es.Candidate, set[str]]],
                                                              list[dict]]:
    """OOS (candidate, buckets) with train-only cuts per fold, plus fold diagnostics.

    Folds are built exactly as edge_study.walk_forward. Censored candidates
    (INSUFFICIENT_PATH) are appended to the fold whose test window contains
    them, so they still reserve the slot in sequencing.
    """
    usable = sorted((c for c in cands if c.r(FOLD_COST, tp_r) is not None),
                    key=lambda c: c.signal_time_ms)
    folds = stats.purged_walk_forward_folds(
        [c.signal_time_ms for c in usable], [c.exit_time(tp_r, FOLD_COST) for c in usable],
        n_folds=n_folds, embargo_ms=embargo_ms, min_train=min_train)
    censored = sorted((c for c in cands
                       if (c.outcomes.get((GATE_COST, tp_r)) or {}).get("status") == "INSUFFICIENT_PATH"
                       or (c.outcomes.get((FOLD_COST, tp_r)) or {}).get("status") == "INSUFFICIENT_PATH"),
                      key=lambda c: c.signal_time_ms)
    out: list[tuple[es.Candidate, set[str]]] = []
    rows: list[dict] = []
    for k, fold in enumerate(folds):
        train = [usable[i] for i in fold.train]
        test = [usable[i] for i in fold.test]
        nxt = folds[k + 1].test_start_ms if k + 1 < len(folds) else None
        test += [c for c in censored if c.signal_time_ms >= fold.test_start_ms
                 and (nxt is None or c.signal_time_ms < nxt) and c not in test]
        cuts = sr.TrainCuts.from_train([c.features for c in train])
        for c in test:
            out.append((c, sr.candidate_buckets(c.signal_time_ms, c.features, cuts)))
        rows.append({"tp_r": tp_r, "fold": fold.index, "train_n": len(train), "test_n": len(test),
                     "test_start_ms": fold.test_start_ms, "test_end_ms": fold.test_end_ms,
                     "rv24_cuts": cuts.rv24, "h4_strength_cuts": cuts.h4_strength,
                     "test_long_share": sum(c.direction > 0 for c in test) / len(test)})
    return out, rows


def evaluate_rule(items: Sequence[tuple[es.Candidate, set[str]]], tp_r: float, *, bucket: str | None,
                  direction: int, max_hold_ms: int, n_trials: int, slot_block: int,
                  event_block: int, seed: int) -> dict:
    picked = sr.select(direction, bucket, lambda it: it[1], lambda it: it[0].direction, items)
    trades = [t for t in (sr.slot_trade(c, c.direction, c.outcomes.get((GATE_COST, tp_r)),
                                        max_hold_ms=max_hold_ms) for c, _ in picked) if t]
    seq = sr.single_slot(trades)
    rs = [t.r for t in seq if t.r is not None]

    def summ(vals, block):
        return sr.summarize(vals, n_trials=n_trials, block_size=block, seed=seed)

    return {
        "tp_r": tp_r, "bucket": bucket, "direction": direction,
        "single_slot": summ(rs, slot_block),
        "single_slot_long": summ([t.r for t in seq if t.r is not None and t.direction > 0], slot_block),
        "single_slot_short": summ([t.r for t in seq if t.r is not None and t.direction < 0], slot_block),
        "censored_in_sequence": sum(t.r is None for t in seq),
        "all_events": summ([t.r for t in trades if t.r is not None], event_block),
    }


def run(args: argparse.Namespace) -> dict:
    data = Path(args.input_dir)
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    tp_grid = tuple(args.tp_r)
    max_hold_ms = int(args.max_hold_hours * sr.HOUR_MS)
    names = sr.hypothesis_names(tp_grid)
    n_trials = len(names) + len(sr.BENCHMARK_RULES) * len(tp_grid) + PRIOR_DECLARED_TRIALS
    cands = build_candidates(data, min_score=args.min_score, tp_grid=tp_grid, max_hold_ms=max_hold_ms)

    clock_path = data / "broker_clock_analysis.json"
    clock = json.loads(clock_path.read_text(encoding="utf-8")) if clock_path.is_file() else {}
    common = dict(max_hold_ms=max_hold_ms, n_trials=n_trials, slot_block=args.slot_block_size,
                  event_block=args.block_size, seed=args.seed)
    benchmarks: dict[str, dict] = {}
    hypotheses: list[dict] = []
    fold_rows: list[dict] = []
    for tp_r in tp_grid:
        items, rows = oos_assignments(cands, tp_r, n_folds=args.n_folds,
                                      embargo_ms=int(args.embargo_hours * sr.HOUR_MS),
                                      min_train=args.min_train)
        fold_rows += rows
        for name, d in zip(sr.BENCHMARK_RULES, (0, 1, -1)):
            benchmarks[f"tp{tp_r}_{name}"] = evaluate_rule(items, tp_r, bucket=None, direction=d,
                                                           **common)
        for bucket in sr.ALL_BUCKETS:
            for dname, d in sr.DIRECTIONS.items():
                row = evaluate_rule(items, tp_r, bucket=bucket, direction=d, **common)
                row["hypothesis"] = sr.hypothesis_key(tp_r, bucket, dname)
                row["direction_name"] = dname
                hypotheses.append(row)
    assert [h["hypothesis"] for h in hypotheses] == names
    sr.apply_bh(hypotheses, q=args.fdr_q)
    for h in hypotheses:
        tp = h["tp_r"]
        h["gate"] = sr.gate(h, {b: benchmarks.get(f"tp{tp}_{b}", {}).get("single_slot")
                                for b in sr.BENCHMARK_RULES})

    report = {
        "study_version": sr.STUDY_VERSION, "observer_only": True, "score_effect": 0,
        "strategy_validated": False,
        "broker_clock": {k: clock.get(k) for k in ("broker_clock_status", "broker_server_utc_offset",
                                                    "dst_behavior", "session_attribution_allowed")},
        "time_buckets": "server wall-clock hour/weekday (no UTC sessions inferred)",
        "params": {"min_score": args.min_score, "tp_r": list(tp_grid),
                   "max_hold_hours": args.max_hold_hours, "n_folds": args.n_folds,
                   "embargo_hours": args.embargo_hours, "min_train": args.min_train,
                   "gate_cost": GATE_COST, "fold_cost": FOLD_COST, "fdr_q": args.fdr_q,
                   "n_hypotheses": len(names), "n_trials_for_dsr": n_trials,
                   "dsr_sr_variance": "1/(n-1) per evaluated sequence",
                   "bootstrap_block_single_slot": args.slot_block_size,
                   "bootstrap_block_all_events": args.block_size, "seed": args.seed},
        "coverage": es.coverage(cands, tp_grid),
        "folds": fold_rows, "benchmarks": benchmarks, "hypotheses": hypotheses,
        "verdict": {"gate": sr.GATE_TEXT,
                    "bh_rejections": [h["hypothesis"] for h in hypotheses if h["bh_reject"]],
                    "candidates_for_paper": [h["hypothesis"] for h in hypotheses
                                             if h["gate"]["passes"]],
                    "note": "Passing only nominates a PAPER challenger; it never changes the EA."},
    }
    write_outputs(out_dir, report)
    return report


def _f(v: object, digits: int = 3) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        return "inf" if math.isinf(v) else f"{v:.{digits}f}"
    return str(v)


def _row(label: str, s: dict) -> str:
    return (f"| {label} | {s['n']} | {_f(s['expectancy_r'])} | [{_f(s['ci_lower'])}, "
            f"{_f(s['ci_upper'])}] | {_f(s['p_value_mean_gt_0'])} | {_f(s['profit_factor'])} | "
            f"{_f(s['dsr'])} |")


def render_markdown(report: dict) -> str:
    head = "| Rule | n | E[R] | CI95 | p(E>0) | PF | DSR |\n|---|---:|---:|---|---:|---:|---:|"
    lines = ["# Quant session / regime V1", "",
             "> Offline, observer_only=true, score_effect=0, strategy_validated=false. "
             "Server wall-clock hour only (UTC offset not established).", "",
             f"Broker clock: `{json.dumps(report['broker_clock'])}`", "",
             f"n_hypotheses={report['params']['n_hypotheses']}, n_trials(DSR)="
             f"{report['params']['n_trials_for_dsr']}, cost={report['params']['gate_cost']}", "",
             "## Benchmarks (STRESS single slot OOS)", "", head]
    for k, v in report["benchmarks"].items():
        lines.append(_row(k, v["single_slot"]))
    lines += ["", "## Hypotheses (STRESS single slot OOS)", "",
              "| Hypothesis | n | E[R] | CI95 | p(E>0) | PF | DSR | BH | Gate | all-events n / E[R] |",
              "|---|---:|---:|---|---:|---:|---:|---|---|---|"]
    for h in report["hypotheses"]:
        s, a = h["single_slot"], h["all_events"]
        lines.append(_row(h["hypothesis"], s)[:-1] + f" {'Y' if h['bh_reject'] else '-'} | "
                     f"{'PASS' if h['gate']['passes'] else '-'} | {a['n']} / {_f(a['expectancy_r'])} |")
    v = report["verdict"]
    lines += ["", "## Verdict", "", f"Gate: {v['gate']}.", "",
              f"BH rejections: {', '.join(v['bh_rejections']) or 'none'}.", "",
              f"PAPER candidates: {', '.join(v['candidates_for_paper']) or 'none'}.", "", v["note"], ""]
    return "\n".join(lines)


def write_outputs(out_dir: Path, report: dict) -> None:
    (out_dir / "session_regime_study.json").write_text(json.dumps(report, indent=2, default=str),
                                                       encoding="utf-8")
    (out_dir / "session_regime_study.md").write_text(render_markdown(report), encoding="utf-8")
    with (out_dir / "session_regime_hypotheses.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["hypothesis", "tp_r", "bucket", "direction", "n", "expectancy_r", "ci_lower",
                    "ci_upper", "p_value", "profit_factor", "dsr", "bh_reject", "passes_gate",
                    "all_events_n", "all_events_expectancy_r"])
        for h in report["hypotheses"]:
            s, a = h["single_slot"], h["all_events"]
            w.writerow([h["hypothesis"], h["tp_r"], h["bucket"], h["direction_name"], s["n"],
                        s["expectancy_r"], s["ci_lower"], s["ci_upper"], s["p_value_mean_gt_0"],
                        s["profit_factor"], s["dsr"], h["bh_reject"], h["gate"]["passes"],
                        a["n"], a["expectancy_r"]])


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input-dir", default="research/output")
    p.add_argument("--output-dir", default="research/analysis/session_regime")
    p.add_argument("--min-score", type=int, default=74)
    p.add_argument("--tp-r", type=float, nargs="+", default=[1.25, 2.0])
    p.add_argument("--max-hold-hours", type=float, default=24.0)
    p.add_argument("--n-folds", type=int, default=5)
    p.add_argument("--embargo-hours", type=float, default=24.0)
    p.add_argument("--min-train", type=int, default=150)
    p.add_argument("--fdr-q", type=float, default=0.10)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--block-size", type=int, default=20)
    p.add_argument("--slot-block-size", type=int, default=5)
    return p.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    report = run(parse_args(argv))
    print(json.dumps(report["verdict"], indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
