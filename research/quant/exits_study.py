"""Offline, pre-registered trade-management study; Python 3.12 stdlib only."""

from __future__ import annotations

import argparse
from collections import Counter
import csv
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import sys
from typing import Sequence

from research.quant import barriers, edge_study, features, stats
from research.quant.exits import HOUR_MS, MAX_HOLD_MS, POLICIES, simulate_exit

DIRECTIONS = (("BOTH", 0), ("LONG_ONLY", 1), ("SHORT_ONLY", -1))
COSTS = ("BASE", "STRESS")
MODES = ("all_events", "single_slot")
N_TRIALS = len(POLICIES) * len(DIRECTIONS) * len(COSTS) * len(MODES) + 34


@dataclass
class Event:
    candidate: edge_study.Candidate
    outcomes: dict[tuple[str, str], dict] = field(default_factory=dict)


def select(events: Sequence[Event], policy: str, cost: str, direction: int = 0,
           *, single_slot: bool) -> list[Event]:
    """Chronological selection without dropping censored trades before slotting."""
    busy_until = -1
    chosen = []
    for event in sorted(events, key=lambda e: e.candidate.signal_time_ms):
        c = event.candidate
        if direction and c.direction != direction:
            continue
        result = event.outcomes.get((policy, cost))
        if result is None or result["entry_time_ms"] is None:
            continue
        if single_slot and result["entry_time_ms"] < busy_until:
            continue
        chosen.append(event)
        busy_until = (result["exit_time_ms"] if result["status"] == "OK"
                      else result["entry_time_ms"] + MAX_HOLD_MS)
    return chosen


def values(events: Sequence[Event], policy: str, cost: str) -> list[float]:
    return [e.outcomes[(policy, cost)]["r_multiple"] for e in events
            if e.outcomes[(policy, cost)]["status"] == "OK"]


def trial_sequences(events: Sequence[Event]):
    for policy in POLICIES:
        for name, direction in DIRECTIONS:
            for cost in COSTS:
                for mode in MODES:
                    chosen = select(events, policy.name, cost, direction,
                                    single_slot=mode == "single_slot")
                    yield policy.name, name, cost, mode, chosen


def discovery_sr_variance(events: Sequence[Event]) -> float | None:
    """Cross-trial per-trade SR variance, never using OOS outcomes."""
    sharpes, counts = [], []
    for policy, _, cost, _, chosen in trial_sequences(events):
        vals = values(chosen, policy, cost)
        sr = stats.sharpe_ratio(vals)
        if sr is not None and len(vals) >= 3:
            sharpes.append(sr)
            counts.append(len(vals))
    if len(sharpes) < 2:
        return None
    mean = sum(sharpes) / len(sharpes)
    variance = sum((s - mean) ** 2 for s in sharpes) / (len(sharpes) - 1)
    return max(variance, 1 / (min(counts) - 1))


def summary(vals: Sequence[float], *, block: int, sr_variance: float | None,
            seed: int) -> dict:
    ci = stats.bootstrap_mean_ci(vals, block_size=block, seed=seed)
    return dict(n=len(vals), expectancy_r=stats.expectancy(vals),
                ci_lower=ci["lower"], ci_upper=ci["upper"],
                profit_factor=stats.profit_factor(vals),
                p_value_mean_gt_0=stats.bootstrap_pvalue_mean_gt_zero(
                    vals, block_size=block, seed=seed),
                dsr=None if sr_variance is None else stats.deflated_sharpe_ratio(
                    vals, n_trials=N_TRIALS, sr_variance=sr_variance),
                max_drawdown_r=edge_study.max_drawdown(vals), total_r=sum(vals),
                bootstrap_block_size=block)


def gate(rows: list[dict]) -> list[dict]:
    """Same gate thresholds as edge study; stricter fixed-TP benchmark family."""
    benchmarks = {(r["policy"], r["direction"]): r["expectancy_r"] for r in rows
                  if r["cost"] == "STRESS" and r["mode"] == "single_slot"
                  and r["policy"] in ("FIXED_1.25", "FIXED_2")}
    complete = all(benchmarks.get((p, d)) is not None
                   for p in ("FIXED_1.25", "FIXED_2") for d, _ in DIRECTIONS)
    best = max((v for v in benchmarks.values() if v is not None), default=None)
    findings = []
    for row in rows:
        if row["cost"] != "STRESS" or row["mode"] != "single_slot":
            continue
        if row["policy"].startswith("FIXED_"):
            continue
        beats = complete and best is not None and row["expectancy_r"] is not None and row["expectancy_r"] > best
        ok = (row["n"] >= 100 and row["ci_lower"] is not None and row["ci_lower"] > 0
              and (row["dsr"] or 0) >= 0.95 and (row["profit_factor"] or 0) >= 1.2 and beats)
        findings.append(dict(policy=row["policy"], direction=row["direction"],
                             passes_gate=ok, beats_benchmarks=beats,
                             benchmarks_complete=complete,
                             best_benchmark_expectancy_r=best))
    return findings


def pbo_matrix(events: Sequence[Event]) -> tuple[list[list[float]], list[str]]:
    """Weekly STRESS R of actual single-slot portfolios, retaining zero weeks."""
    if not events:
        return [], []
    start = min(e.candidate.signal_time_ms for e in events)
    end = max(e.candidate.signal_time_ms + MAX_HOLD_MS for e in events)
    columns = [(p.name, name, d) for p in POLICIES for name, d in DIRECTIONS]
    matrix = [[0.0] * len(columns) for _ in range((end - start) // edge_study.WEEK_MS + 1)]
    for j, (policy, _, direction) in enumerate(columns):
        for e in select(events, policy, "STRESS", direction, single_slot=True):
            out = e.outcomes[(policy, "STRESS")]
            if out["status"] == "OK":
                # Allocate realized partial cashflows to their own exit weeks.
                for leg in out["legs"]:
                    week = (leg["exit_time_ms"] - start) // edge_study.WEEK_MS
                    matrix[week][j] += leg["r_multiple"]
    return matrix, [f"{p}_{n}" for p, n, _ in columns]


def evaluate(events: list[Event], *, seed: int = 0) -> dict:
    events = sorted(events, key=lambda e: e.candidate.signal_time_ms)
    times = [e.candidate.signal_time_ms for e in events]
    # Common fixed horizon: no outcome-dependent sample/fold boundaries.
    folds = stats.purged_walk_forward_folds(
        times, [t + MAX_HOLD_MS + barriers.BAR_MS for t in times],
        n_folds=5, min_train=150, embargo_ms=24 * HOUR_MS)
    if not folds:
        raise ValueError("no eligible purged folds; need more candidates")
    discovery = [events[i] for i in folds[0].train]
    sr_variance = discovery_sr_variance(discovery)
    indices = sorted({i for f in folds for i in f.test})
    oos = [events[i] for i in indices]
    fold_ids = {id(events[i]): f.index for f in folds for i in f.test}
    rows, fold_rows = [], []
    for policy, direction, cost, mode, chosen in trial_sequences(oos):
        vals = values(chosen, policy, cost)
        row = dict(policy=policy, direction=direction, cost=cost, mode=mode,
                   **summary(vals, block=5 if mode == "single_slot" else 20,
                             sr_variance=sr_variance, seed=seed))
        row["selected_n"] = len(chosen)
        row["censored_n"] = len(chosen) - len(vals)
        row["horizon_gap_n"] = sum(bool(e.outcomes[(policy, cost)]["horizon_gap_ms"]) for e in chosen)
        row["attribution"] = {
            name: {"n": len(v), "expectancy_r": stats.expectancy(v)}
            for name, v in (("LONG", values([e for e in chosen if e.candidate.direction == 1], policy, cost)),
                            ("SHORT", values([e for e in chosen if e.candidate.direction == -1], policy, cost)))}
        rows.append(row)
        if mode == "single_slot":
            for fold in folds:
                subset = [e for e in chosen if fold_ids[id(e)] == fold.index]
                fv = values(subset, policy, cost)
                fold_rows.append(dict(policy=policy, direction=direction, cost=cost,
                                      fold=fold.index, n=len(fv), expectancy_r=stats.expectancy(fv),
                                      profit_factor=stats.profit_factor(fv)))
    for row, reject in zip(rows, stats.benjamini_hochberg([r["p_value_mean_gt_0"] for r in rows], q=0.1)):
        row["bh_mean_gt_zero"] = reject
    matrix, columns = pbo_matrix(oos)
    pbo = stats.cscv_pbo(matrix, n_splits=10, seed=seed) if len(matrix) >= 10 else None
    return dict(observer_only=True, score_effect=0, strategy_validated=False,
                study_version="quant-exits-v1", n_trials=N_TRIALS,
                sr_variance_discovery_per_trade=sr_variance, seed=seed,
                candidates_n=len(events), oos_candidates_n=len(oos),
                discovery_n=len(discovery), policies=[p.__dict__ for p in POLICIES],
                folds=[dict(index=f.index, train_n=len(f.train), test_n=len(f.test),
                            test_start_ms=f.test_start_ms, test_end_ms=f.test_end_ms) for f in folds],
                rows=rows, single_slot_by_fold=fold_rows, pbo=pbo, pbo_columns=columns,
                gate=gate(rows), no_untouched_holdout=True)


def build_events(data: Path) -> list[Event]:
    bars_dir = data / "historical_bars"
    h1, h4, m15 = [features.CausalBarSeries(features.load_bars(
        bars_dir / f"XAUUSD_{tf}.jsonl", timeframe=tf)) for tf in ("H1", "H4", "M15")]
    print("[exits] loading M1", file=sys.stderr, flush=True)
    m1 = barriers.M1Path.from_jsonl(bars_dir / "XAUUSD_M1.jsonl")
    events = []
    for raw in edge_study.iter_candidates(data / "historical_decisions.jsonl", min_score=74):
        t, d = raw["observed_at"], raw["direction"]
        feats = features.build_features(h1, h4, m15, t)["features"]
        atr = feats["h1_atr14"]
        stop = barriers.stop_distance(atr, structure_distance=edge_study.structure_distance(h1, t, d))
        event = Event(edge_study.Candidate(raw["event_id"], t, d, raw["score"], feats, stop))
        if stop is not None:
            for policy in POLICIES:
                for cost in COSTS:
                    event.outcomes[(policy.name, cost)] = simulate_exit(
                        m1, signal_time_ms=t, direction=d, stop_dist=stop, atr=atr,
                        policy=policy, cost=barriers.COST_SCENARIOS[cost])
        events.append(event)
        if len(events) % 250 == 0:
            print(f"[exits] {len(events)} candidates", file=sys.stderr, flush=True)
    return events


def fmt(v) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:.2e}" if 0 < abs(v) < .001 else f"{v:.3f}"
    return str(v)


def render_markdown(report: dict) -> str:
    lines = ["# Quant Exits V1", "", "observer_only=true; score_effect=0; strategy_validated=false",
             "", f"Candidatos: {report['candidates_n']}; OOS: {report['oos_candidates_n']}; "
             f"n_trials={report['n_trials']}. Sin holdout intacto.", "",
             "## Walk-forward OOS single-slot", "",
             "| Política | Dirección | Coste | n | E[R] | IC95 | PF | DSR | BH | DD R |",
             "|---|---|---|---:|---:|---|---:|---:|---|---:|"]
    for r in report["rows"]:
        if r["mode"] == "single_slot":
            lines.append(f"| {r['policy']} | {r['direction']} | {r['cost']} | {r['n']} | "
                         f"{fmt(r['expectancy_r'])} | [{fmt(r['ci_lower'])}; {fmt(r['ci_upper'])}] | "
                         f"{fmt(r['profit_factor'])} | {fmt(r['dsr'])} | {r['bh_mean_gt_zero']} | "
                         f"{fmt(r['max_drawdown_r'])} |")
    passed = [f"{r['policy']}:{r['direction']}" for r in report["gate"] if r["passes_gate"]]
    lines += ["", f"PBO CSCV (27 carteras): {fmt(report['pbo']['pbo'] if report['pbo'] else None)}.",
              "", f"Gate: {', '.join(passed) or 'NINGUNA política pasa'}.", "",
              "Ver metodología y limitaciones en docs/QUANT_EXITS_V1.md.", ""]
    return "\n".join(lines)


def run(args: argparse.Namespace) -> dict:
    events = build_events(Path(args.input_dir))
    print("[exits] evaluating frozen grid", file=sys.stderr, flush=True)
    report = evaluate(events, seed=args.seed)
    report["coverage"] = dict(directions=dict(Counter(e.candidate.direction for e in events)),
                              statuses=dict(Counter(o["status"] for e in events for o in e.outcomes.values())))
    # Hash small causal candidate ledger; raw data never enters versioned files.
    ledger = [(e.candidate.event_id, e.candidate.signal_time_ms, e.candidate.direction,
               e.candidate.stop_dist) for e in events]
    report["candidate_stop_sha256"] = hashlib.sha256(json.dumps(ledger).encode()).hexdigest()
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "quant_exits_study.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (out / "quant_exits_study.md").write_text(render_markdown(report), encoding="utf-8")
    with (out / "quant_exits_rows.csv").open("w", encoding="utf-8", newline="") as handle:
        columns = [k for k in report["rows"][0] if k != "attribution"]
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(report["rows"])
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", default="research/output")
    parser.add_argument("--output-dir", default="research/analysis/quant_exits_v1")
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args(argv)
    report = run(args)
    print(json.dumps({"passes": [g for g in report["gate"] if g["passes_gate"]],
                      "pbo": report["pbo"], "n_trials": N_TRIALS}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
