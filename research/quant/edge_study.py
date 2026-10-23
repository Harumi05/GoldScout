"""Quant Research V1 edge study (offline, observer_only, score_effect=0).

Pipeline: replayed EA-proxy candidates -> causal features -> M1 triple-barrier
R outcomes under explicit cost scenarios -> purged walk-forward meta-labeling,
down-only volatility targeting, CSCV overfitting probability and conditional
edge tables with Benjamini-Hochberg control.

Nothing here feeds the EA. Findings are hypotheses for a later, separately
approved PAPER/DEMO challenger; they never authorize orders or change risk.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

from research.quant import barriers, features, stats
from research.quant.model import fit_logistic

STUDY_VERSION = "quant-edge-study-v1"
HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS
WEEK_MS = 7 * DAY_MS
LABEL_COST = "BASE"
DIRECTIONAL = ("h1_tsmom_6", "h1_tsmom_24", "h1_tsmom_120", "h1_dist_ema20_atr",
               "h1_dist_ema50_atr", "h1_ema50_slope_atr", "h4_trend_sign",
               "h4_dist_ema50_atr", "m15_ret4_atr")
CENTERED = {"h1_range_pos_50": 0.5, "h1_rsi14": 50.0, "m15_rsi14": 50.0}
EXCLUDED_FROM_MODEL = ("h1_atr14",)  # price level, not scale-free
BENCHMARK_RULES = ("BASELINE", "LONG_ONLY", "SHORT_ONLY")


@dataclass
class Candidate:
    event_id: str
    signal_time_ms: int
    direction: int
    score: int
    features: dict[str, float | None]
    stop_dist: float | None
    outcomes: dict[tuple[str, float], dict] = field(default_factory=dict)

    def r(self, cost: str, tp_r: float) -> float | None:
        out = self.outcomes.get((cost, tp_r))
        return None if out is None or out.get("status") != "OK" else out["r_multiple"]

    def exit_time(self, tp_r: float, cost: str = LABEL_COST) -> int | None:
        out = self.outcomes.get((cost, tp_r))
        return None if out is None or out.get("status") != "OK" else out["exit_time_ms"]

    def slot_window(self, tp_r: float, cost: str, max_hold_ms: int) -> tuple[int, int] | None:
        """(entry, release) of the single slot, using only executable knowledge.

        Intrabar SL/TP exits are only known by the close of their M1 bar, so the
        slot stays busy until then. A censored trade still occupies the slot for
        its whole horizon: admission must not depend on label availability.
        """
        out = self.outcomes.get((cost, tp_r))
        if out is None or out.get("entry_time_ms") is None:
            return None
        entry = out["entry_time_ms"]
        if out["status"] == "OK":
            release = out["exit_time_ms"]
            if out["exit_reason"] in ("SL", "TP"):
                release += barriers.BAR_MS
            return entry, release
        if out["status"] == "INSUFFICIENT_PATH":
            return entry, entry + max_hold_ms + barriers.BAR_MS
        return None


# --------------------------------------------------------------------------- loading

def iter_candidates(decisions_path: Path, *, min_score: int) -> Iterable[dict]:
    """First ARMED decision per (H1 hour, direction) whose own score >= min_score."""
    seen: set[tuple[int, str]] = set()
    with decisions_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("decision") != "ARMED" or row.get("armed_direction") not in ("LONG", "SHORT"):
                continue
            direction = row["armed_direction"]
            score = row["long_score"] if direction == "LONG" else row["short_score"]
            if score is None or score < min_score:
                continue
            key = (row["observed_at"] // HOUR_MS, direction)
            if key in seen:
                continue
            seen.add(key)
            yield {"event_id": row["event_id"], "observed_at": int(row["observed_at"]),
                   "direction": 1 if direction == "LONG" else -1, "score": int(score)}


def structure_distance(h1: features.CausalBarSeries, as_of_ms: int, direction: int,
                       lookback: int = 20) -> float | None:
    window = h1.window(as_of_ms, lookback)
    if window is None:
        return None
    close = window[-1].close
    if direction > 0:
        return max(0.0, close - min(b.low for b in window))
    return max(0.0, max(b.high for b in window) - close)


def model_vector(cand: Candidate, names: Sequence[str], *,
                 include_direction: bool = True) -> list[float | None]:
    """Direction-aligned feature vector: positive values mean 'with the trade'."""
    vec: list[float | None] = []
    for name in names:
        value = cand.features.get(name)
        if value is not None:
            if name in CENTERED:
                value = (value - CENTERED[name]) * cand.direction
            elif name in DIRECTIONAL:
                value = value * cand.direction
        vec.append(value)
    vec.append(float(cand.score))
    if include_direction:
        vec.append(float(cand.direction))
    return vec


def direction_cuts(model, train: Sequence[Candidate], names: Sequence[str], *,
                   quantile: float, min_per_direction: int = 30) -> dict[int, float]:
    """Per-direction train-quantile cuts so a directional base rate is not the signal."""
    preds = {1: [], -1: []}
    for c in train:
        preds[c.direction].append(model.predict(model_vector(c, names, include_direction=False)))
    pooled = sorted(preds[1] + preds[-1])
    cuts = {}
    for d, values in preds.items():
        src = sorted(values) if len(values) >= min_per_direction else pooled
        cuts[d] = src[int(len(src) * quantile)]
    return cuts


# --------------------------------------------------------------------------- evaluation

def summarize(values: Sequence[float], *, n_trials: int, sr_variance: float | None,
              block_size: int, seed: int) -> dict:
    vals = [v for v in values if v is not None]
    # Few long blocks under-sample the variance of the mean; keep >= ~10 blocks.
    block_size = max(1, min(block_size, len(vals) // 10))
    ci = stats.bootstrap_mean_ci(vals, block_size=block_size, seed=seed)
    return {
        "n": len(vals),
        "expectancy_r": stats.expectancy(vals),
        "ci_lower": ci["lower"], "ci_upper": ci["upper"],
        "p_value_mean_gt_0": stats.bootstrap_pvalue_mean_gt_zero(vals, block_size=block_size, seed=seed),
        "profit_factor": stats.profit_factor(vals),
        "win_rate": stats.win_rate(vals),
        "sharpe_per_trade": stats.sharpe_ratio(vals),
        "psr": stats.probabilistic_sharpe_ratio(vals) if len(vals) > 2 else None,
        # No discovery variance -> DSR unknown (None), which fails the gate.
        "dsr": (stats.deflated_sharpe_ratio(vals, n_trials=n_trials, sr_variance=sr_variance)
                if len(vals) > 2 and sr_variance is not None else None),
        "total_r": sum(vals) if vals else None,
        "bootstrap_block_size": block_size,
        "max_drawdown_r": max_drawdown(vals),
    }


def max_drawdown(values: Sequence[float]) -> float | None:
    if not values:
        return None
    peak = equity = worst = 0.0
    for v in values:
        equity += v
        peak = max(peak, equity)
        worst = min(worst, equity - peak)
    return worst


def non_overlapping(cands: Sequence[Candidate], tp_r: float,
                    cost: str = LABEL_COST, max_hold_ms: int = DAY_MS) -> list[Candidate]:
    """Greedy one-position-at-a-time sequence, mirroring the EA's single slot.

    Uses actual entry times and the executable release of the evaluated cost
    scenario. Admitted censored candidates are returned too (their r() is None)
    so callers drop them from statistics without freeing their slot.
    """
    windows = [(w, c) for c in cands if (w := c.slot_window(tp_r, cost, max_hold_ms))]
    out: list[Candidate] = []
    busy_until = -1
    for (entry, release), c in sorted(windows, key=lambda x: x[0][0]):
        if entry < busy_until:
            continue
        out.append(c)
        busy_until = release
    return out


def discovery_sr_variance(cands: Sequence[Candidate], rules: Sequence[tuple[str, float, int, int]],
                          cutoff_ms: int, max_hold_ms: int,
                          min_trades: int = 20) -> float | None:
    """Cross-trial variance of per-trade single-slot Sharpe, on discovery data only.

    Same unit (per trade) as the Sharpe being deflated, and frozen before any
    walk-forward test block, so the DSR penalty is not informed by OOS data.
    Rules with fewer than ``min_trades`` discovery trades are excluded: their
    Sharpe is dominated by small-sample noise (2-15 trade rules reached
    |SR| > 1, which would make any per-trade benchmark unreachable).
    """
    disc = [c for c in cands if c.signal_time_ms < cutoff_ms]
    sharpes = []
    for _, tp_r, min_score, direction in rules:
        picked = [c for c in disc if c.score >= min_score and (not direction or c.direction == direction)]
        vals = [r for c in non_overlapping(picked, tp_r, LABEL_COST, max_hold_ms)
                if (r := c.r(LABEL_COST, tp_r)) is not None]
        sr = stats.sharpe_ratio(vals) if len(vals) >= min_trades else None
        if sr is not None:
            sharpes.append(sr)
    if len(sharpes) < 2:
        return None
    mean = sum(sharpes) / len(sharpes)
    return sum((x - mean) ** 2 for x in sharpes) / (len(sharpes) - 1)


def walk_forward(cands: list[Candidate], *, tp_r: float, names: Sequence[str],
                 n_folds: int, embargo_ms: int, min_train: int) -> tuple[dict[str, list[tuple[Candidate, float]]], list[dict]]:
    """Return per-rule OOS (candidate, risk_weight) selections and per-fold rows."""
    usable = sorted((c for c in cands if c.r(LABEL_COST, tp_r) is not None),
                    key=lambda c: c.signal_time_ms)
    folds = stats.purged_walk_forward_folds(
        [c.signal_time_ms for c in usable], [c.exit_time(tp_r) for c in usable],
        n_folds=n_folds, embargo_ms=embargo_ms, min_train=min_train)
    selections: dict[str, list[tuple[Candidate, float]]] = defaultdict(list)
    fold_rows: list[dict] = []
    for fold in folds:
        train = [usable[i] for i in fold.train]
        test = [usable[i] for i in fold.test]
        rows = [model_vector(c, names) for c in train]
        labels = [1 if c.r(LABEL_COST, tp_r) > 0 else 0 for c in train]
        model = fit_logistic(rows, labels, l2=5.0)
        train_p = sorted(model.predict(r) for r in rows)
        cut50 = train_p[len(train_p) // 2]
        cut70 = train_p[int(len(train_p) * 0.7)]
        # POST-HOC (added after fold 3 showed an all-SHORT test block rejected by
        # a LONG-heavy train base rate); counted in n_trials like every rule.
        neutral = fit_logistic([model_vector(c, names, include_direction=False) for c in train],
                               labels, l2=5.0)
        neutral_cuts = direction_cuts(neutral, train, names, quantile=0.5)
        rv_train = sorted(c.features["h1_rv24"] for c in train if c.features.get("h1_rv24"))
        target_vol = rv_train[len(rv_train) // 2] if rv_train else None
        for c in test:
            p = model.predict(model_vector(c, names))
            selections["BASELINE"].append((c, 1.0))
            # Direction-only benchmarks: a meta rule must beat these, otherwise its
            # gain is a directional/regime base rate, not a feature signal.
            selections["LONG_ONLY" if c.direction > 0 else "SHORT_ONLY"].append((c, 1.0))
            if p >= cut50:
                selections["META_TOP50"].append((c, 1.0))
            if p >= cut70:
                selections["META_TOP30"].append((c, 1.0))
            mult = (barriers.vol_target_multiplier(c.features.get("h1_rv24"), target_vol)
                    if target_vol else None)
            selections["VOL_TARGET_DOWN_ONLY"].append((c, 1.0 if mult is None else mult))
            p_neutral = neutral.predict(model_vector(c, names, include_direction=False))
            if p_neutral >= neutral_cuts[c.direction]:
                selections["META_TOP50_DIR_NEUTRAL"].append((c, 1.0))
            if p >= cut50:
                selections["META_TOP50_VOL_TARGET"].append((c, 1.0 if mult is None else mult))
        base_test = [c.r(LABEL_COST, tp_r) for c in test]
        fold_rows.append({
            "tp_r": tp_r, "fold": fold.index, "train_n": len(train), "test_n": len(test),
            "test_start_ms": fold.test_start_ms, "test_end_ms": fold.test_end_ms,
            "train_win_rate": sum(labels) / len(labels),
            "train_long_share": sum(c.direction > 0 for c in train) / len(train),
            "test_long_share": sum(c.direction > 0 for c in test) / len(test),
            "test_baseline_expectancy_r": stats.expectancy(base_test),
            "test_meta50_expectancy_r": stats.expectancy(
                [c.r(LABEL_COST, tp_r) for c in test if model.predict(model_vector(c, names)) >= cut50]),
            "weights": {n: round(w, 4) for n, w in zip([*names, "score", "direction"], model.weights)},
        })
    return selections, fold_rows


def rule_matrix(cands: Sequence[Candidate], rules: Sequence[tuple[str, float, int, int]],
                bucket_ms: int) -> tuple[list[list[float]], list[str]]:
    """Per-bucket summed BASE R of simple untrained rules (for CSCV PBO)."""
    if not cands:
        return [], []
    start = min(c.signal_time_ms for c in cands)
    n_buckets = (max(c.signal_time_ms for c in cands) - start) // bucket_ms + 1
    matrix = [[0.0] * len(rules) for _ in range(n_buckets)]
    for j, (_, tp_r, min_score, direction) in enumerate(rules):
        for c in cands:
            r = c.r(LABEL_COST, tp_r)
            if r is None or c.score < min_score or (direction and c.direction != direction):
                continue
            matrix[(c.signal_time_ms - start) // bucket_ms][j] += r
    return matrix, [r[0] for r in rules]


def conditional_edge(cands: Sequence[Candidate], names: Sequence[str], *, tp_r: float,
                     discovery_fraction: float, q: float, seed: int,
                     block_size: int = 20) -> list[dict]:
    """Quintiles fixed on the discovery segment, re-checked on the later segment."""
    usable = sorted((c for c in cands if c.r(LABEL_COST, tp_r) is not None),
                    key=lambda c: c.signal_time_ms)
    split = int(len(usable) * discovery_fraction)
    disc, later = usable[:split], usable[split:]
    rows: list[dict] = []
    for idx, name in enumerate(names):
        values = sorted(v for v in (model_vector(c, names)[idx] for c in disc) if v is not None)
        if len(values) < 50:
            continue
        cuts = [values[int(len(values) * k / 5)] for k in (1, 2, 3, 4)]

        def bucket(v: float) -> int:
            return sum(v >= cut for cut in cuts)

        for qi in range(5):
            d = [c.r(LABEL_COST, tp_r) for c in disc
                 if (v := model_vector(c, names)[idx]) is not None and bucket(v) == qi]
            lt = [c.r(LABEL_COST, tp_r) for c in later
                  if (v := model_vector(c, names)[idx]) is not None and bucket(v) == qi]
            p_pos = stats.bootstrap_pvalue_mean_gt_zero(d, block_size=block_size, seed=seed)
            p_neg = stats.bootstrap_pvalue_mean_gt_zero([-x for x in d], block_size=block_size,
                                                        seed=seed)
            p_two = None if p_pos is None or p_neg is None else min(1.0, 2 * min(p_pos, p_neg))
            rows.append({"feature": name, "quintile": qi + 1, "tp_r": tp_r,
                         "lower_cut": None if qi == 0 else cuts[qi - 1],
                         "discovery_n": len(d), "discovery_expectancy_r": stats.expectancy(d),
                         "discovery_p_two_sided": p_two,
                         "later_n": len(lt), "later_expectancy_r": stats.expectancy(lt)})
    passed = stats.benjamini_hochberg([r["discovery_p_two_sided"] for r in rows], q=q)
    for row, ok in zip(rows, passed):
        row["bh_discovery"] = ok
        de, le = row["discovery_expectancy_r"], row["later_expectancy_r"]
        row["sign_persists_later"] = (None if de is None or le is None or row["later_n"] < 20
                                      else (de > 0) == (le > 0))
    return rows


# --------------------------------------------------------------------------- CLI

def run(args: argparse.Namespace) -> dict:
    data = Path(args.input_dir)
    bars_dir = data / "historical_bars"
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    print("[quant] loading bars", file=sys.stderr)
    h1 = features.CausalBarSeries(features.load_bars(bars_dir / "XAUUSD_H1.jsonl", timeframe="H1"))
    h4 = features.CausalBarSeries(features.load_bars(bars_dir / "XAUUSD_H4.jsonl", timeframe="H4"))
    m15 = features.CausalBarSeries(features.load_bars(bars_dir / "XAUUSD_M15.jsonl", timeframe="M15"))
    m1 = barriers.M1Path.from_jsonl(bars_dir / "XAUUSD_M1.jsonl")

    tp_grid = tuple(args.tp_r)
    max_hold_ms = int(args.max_hold_hours * HOUR_MS)
    names = [n for n in features.FEATURE_NAMES if n not in EXCLUDED_FROM_MODEL]
    cands: list[Candidate] = []
    print("[quant] building candidates/outcomes", file=sys.stderr)
    for raw in iter_candidates(data / "historical_decisions.jsonl", min_score=args.min_score):
        t = raw["observed_at"]
        feats = features.build_features(h1, h4, m15, t)["features"]
        atr = feats.get("h1_atr14")
        stop = barriers.stop_distance(atr, structure_distance=structure_distance(h1, t, raw["direction"])) \
            if atr else None
        cand = Candidate(raw["event_id"], t, raw["direction"], raw["score"], feats, stop)
        if stop is not None:
            for cost_name in ("ZERO", "BASE", "STRESS"):
                for tp_r in tp_grid:
                    cand.outcomes[(cost_name, tp_r)] = barriers.triple_barrier(
                        m1, signal_time_ms=t, direction=raw["direction"], stop_dist=stop,
                        tp_r=tp_r, max_hold_ms=max_hold_ms, cost=barriers.COST_SCENARIOS[cost_name])
        cands.append(cand)

    rules = [(f"tp{tp}_s{s}_{ {0: 'BOTH', 1: 'LONG', -1: 'SHORT'}[d] }".replace(" ", ""), tp, s, d)
             for tp in tp_grid for s in (args.min_score, 80, 86) for d in (0, 1, -1)]
    meta_rules = ("BASELINE", "LONG_ONLY", "SHORT_ONLY", "META_TOP50", "META_TOP30",
                  "VOL_TARGET_DOWN_ONLY", "META_TOP50_VOL_TARGET", "META_TOP50_DIR_NEUTRAL")
    n_trials = len(rules) + len(meta_rules) * len(tp_grid)
    matrix, rule_names = rule_matrix(cands, rules, WEEK_MS)
    # DSR variance: per-trade single-slot Sharpe across simple rules, measured
    # only on the first walk-forward block (train of fold 0, never an OOS test).
    labelled = sorted(c.signal_time_ms for c in cands if c.r(LABEL_COST, tp_grid[0]) is not None)
    cutoff = labelled[len(labelled) // (args.n_folds + 1)] if labelled else 0
    sr_var = discovery_sr_variance(cands, rules, cutoff, max_hold_ms)
    pbo = stats.cscv_pbo(matrix, n_splits=args.cscv_splits) if len(matrix) >= args.cscv_splits else None

    report: dict = {"study_version": STUDY_VERSION, "feature_version": features.FEATURE_VERSION,
                    "observer_only": True, "score_effect": 0, "strategy_validated": False,
                    "params": {"min_score": args.min_score, "tp_r": list(tp_grid),
                               "max_hold_hours": args.max_hold_hours, "n_folds": args.n_folds,
                               "embargo_hours": args.embargo_hours, "label_cost": LABEL_COST,
                               "n_trials_for_dsr": n_trials,
                               "sr_variance_discovery_per_trade": sr_var,
                               "sr_variance_discovery_cutoff_ms": cutoff,
                               "bootstrap_block_size_overlapping": args.block_size,
                               "bootstrap_block_size_single_slot": args.slot_block_size},
                    "coverage": coverage(cands, tp_grid), "cscv_pbo_simple_rules": pbo,
                    "full_sample": {}, "walk_forward_oos": {}, "folds": []}

    block = args.block_size

    def summ(values: list[float | None], *, overlapping: bool = False) -> dict:
        return summarize([v for v in values if v is not None], n_trials=n_trials,
                         sr_variance=sr_var,
                         block_size=block if overlapping else args.slot_block_size,
                         seed=args.seed)

    for tp_r in tp_grid:
        for cost_name in ("ZERO", "BASE", "STRESS"):
            seq = non_overlapping(cands, tp_r, cost_name, max_hold_ms)
            report["full_sample"][f"tp{tp_r}_{cost_name}_all_events"] = summ(
                [c.r(cost_name, tp_r) for c in cands if c.r(cost_name, tp_r) is not None],
                overlapping=True)
            report["full_sample"][f"tp{tp_r}_{cost_name}_single_slot"] = summ(
                [c.r(cost_name, tp_r) for c in seq])
        selections, fold_rows = walk_forward(cands, tp_r=tp_r, names=names, n_folds=args.n_folds,
                                             embargo_ms=int(args.embargo_hours * HOUR_MS),
                                             min_train=args.min_train)
        report["folds"].extend(fold_rows)
        for rule, picked in selections.items():
            for cost_name in ("BASE", "STRESS"):
                # Sequence first (censored trades keep their slot), then drop
                # censored outcomes from statistics; never impute them.
                kept = [(c, w) for c, w in picked if c.r(cost_name, tp_r) is not None]
                chosen = {id(c): w for c, w in picked}
                seq = non_overlapping([c for c, _ in picked], tp_r, cost_name, max_hold_ms)
                slot = [(c, r * chosen[id(c)]) for c in seq
                        if (r := c.r(cost_name, tp_r)) is not None]
                report["walk_forward_oos"][f"tp{tp_r}_{rule}_{cost_name}"] = {
                    "risk_units": "scaled_r" if "VOL_TARGET" in rule else "r",
                    "all_events": summ([c.r(cost_name, tp_r) * w for c, w in kept],
                                       overlapping=True),
                    "single_slot": summ([r for _, r in slot]),
                    "single_slot_long": summ([r for c, r in slot if c.direction > 0]),
                    "single_slot_short": summ([r for c, r in slot if c.direction < 0]),
                }

    edge_rows = [row for tp_r in tp_grid for row in conditional_edge(
        cands, names, tp_r=tp_r, discovery_fraction=0.6, q=args.fdr_q, seed=args.seed,
        block_size=block)]
    # One BH family across every feature x quintile x TP hypothesis.
    for row, ok in zip(edge_rows, stats.benjamini_hochberg(
            [r["discovery_p_two_sided"] for r in edge_rows], q=args.fdr_q)):
        row["bh_discovery"] = ok
    report["params"]["bh_family_size"] = len(edge_rows)
    report["conditional_edge_survivors"] = [
        r for r in edge_rows if r["bh_discovery"] and r["sign_persists_later"]]
    report["verdict"] = verdict(report)

    write_outputs(out_dir, report, cands, edge_rows, tp_grid)
    return report


def coverage(cands: Sequence[Candidate], tp_grid: Sequence[float]) -> dict:
    status: dict[str, int] = defaultdict(int)
    ambiguous = 0
    for c in cands:
        out = c.outcomes.get((LABEL_COST, tp_grid[0]))
        status["NO_STOP" if out is None else out["status"]] += 1
        ambiguous += bool(out and out.get("ambiguous_bar"))
    times = [c.signal_time_ms for c in cands]
    return {"candidates": len(cands), "long": sum(c.direction > 0 for c in cands),
            "short": sum(c.direction < 0 for c in cands), "status_first_tp": dict(status),
            "ambiguous_bars_first_tp": ambiguous,
            "first_signal_ms": min(times) if times else None,
            "last_signal_ms": max(times) if times else None}


def verdict(report: dict) -> dict:
    """Conservative pre-registered gate; it can only say 'candidate for PAPER'.

    A rule must also beat BASELINE and both direction-only benchmarks at the same
    TP/cost, so a directional base rate cannot pass as a feature signal.
    """
    oos = report["walk_forward_oos"]
    findings = []
    for key, value in oos.items():
        if not key.endswith("_STRESS"):
            continue
        tp_part, rest = key.split("_", 1)
        rule = rest[: -len("_STRESS")]
        if rule in BENCHMARK_RULES:
            continue
        s = value["single_slot"]
        bench = [oos.get(f"{tp_part}_{b}_STRESS", {}).get("single_slot", {}).get("expectancy_r")
                 for b in BENCHMARK_RULES]
        complete = all(b is not None for b in bench)
        best_bench = max(bench) if complete else None
        beats = complete and s["expectancy_r"] is not None and s["expectancy_r"] > best_bench
        ok = (s["n"] >= 100 and s["ci_lower"] is not None and s["ci_lower"] > 0
              and (s["dsr"] or 0) >= 0.95 and (s["profit_factor"] or 0) >= 1.2 and beats)
        findings.append({"rule": key, "passes_gate": ok, "n": s["n"],
                         "expectancy_r": s["expectancy_r"], "ci_lower": s["ci_lower"],
                         "dsr": s["dsr"], "profit_factor": s["profit_factor"],
                         "best_benchmark_expectancy_r": best_bench, "beats_benchmarks": beats,
                         "benchmarks_complete": complete})
    pbo = report.get("cscv_pbo_simple_rules") or {}
    return {"gate": ("STRESS single-slot OOS: n>=100, CI95 lower>0, DSR>=0.95, PF>=1.2, "
                     "E[R] above BASELINE/LONG_ONLY/SHORT_ONLY"),
            "pbo_simple_rules": pbo.get("pbo"),
            "candidates_for_paper": [f["rule"] for f in findings if f["passes_gate"]],
            "rules": findings,
            "note": "Passing the gate only nominates a PAPER challenger; it never changes the EA."}


def write_outputs(out_dir: Path, report: dict, cands: Sequence[Candidate],
                  edge_rows: Sequence[dict], tp_grid: Sequence[float]) -> None:
    (out_dir / "quant_edge_study.json").write_text(json.dumps(report, indent=2, default=str),
                                                   encoding="utf-8")
    with (out_dir / "quant_candidates.csv").open("w", newline="", encoding="utf-8") as fh:
        names = list(features.FEATURE_NAMES)
        w = csv.writer(fh)
        w.writerow(["event_id", "signal_time_ms", "direction", "score", "stop_dist",
                    *[f"r_{c}_tp{t}" for c in ("ZERO", "BASE", "STRESS") for t in tp_grid], *names])
        for c in cands:
            w.writerow([c.event_id, c.signal_time_ms, c.direction, c.score, c.stop_dist,
                        *[c.r(cn, t) for cn in ("ZERO", "BASE", "STRESS") for t in tp_grid],
                        *[c.features.get(n) for n in names]])
    if edge_rows:
        with (out_dir / "quant_conditional_edge.csv").open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(edge_rows[0].keys()))
            w.writeheader()
            w.writerows(edge_rows)
    (out_dir / "quant_edge_study.md").write_text(render_markdown(report), encoding="utf-8")


def _f(v: object, digits: int = 3) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        return "inf" if math.isinf(v) else f"{v:.{digits}f}"
    return str(v)


def render_markdown(report: dict) -> str:
    lines = ["# Quant Research V1 — edge study", "",
             "> Offline, observer_only=true, score_effect=0, strategy_validated=false. "
             "R outcomes are M1-bar triple-barrier simulations with assumed cost scenarios, "
             "not broker fills.", "", "## Coverage", "",
             "```json", json.dumps(report["coverage"], indent=2), "```", "",
             "## Full sample (in-sample, descriptive)", "",
             "| Key | n | E[R] | CI95 | PF | Win | DSR | MaxDD R |", "|---|---:|---:|---|---:|---:|---:|---:|"]
    for key, s in report["full_sample"].items():
        lines.append(f"| {key} | {s['n']} | {_f(s['expectancy_r'])} | [{_f(s['ci_lower'])}, "
                     f"{_f(s['ci_upper'])}] | {_f(s['profit_factor'])} | {_f(s['win_rate'])} | "
                     f"{_f(s['dsr'])} | {_f(s['max_drawdown_r'], 1)} |")
    lines += ["", "## Purged walk-forward OOS (single slot)", "",
              "| Rule | n | E[R] | CI95 | p(E>0) | PF | DSR | MaxDD | n L/S | E[R] L/S |",
              "|---|---:|---:|---|---:|---:|---:|---:|---|---|"]
    for key, v in report["walk_forward_oos"].items():
        s, lo, sh = v["single_slot"], v["single_slot_long"], v["single_slot_short"]
        unit = " (scaled R)" if v["risk_units"] == "scaled_r" else ""
        lines.append(f"| {key}{unit} | {s['n']} | {_f(s['expectancy_r'])} | [{_f(s['ci_lower'])}, "
                     f"{_f(s['ci_upper'])}] | {_f(s['p_value_mean_gt_0'])} | "
                     f"{_f(s['profit_factor'])} | {_f(s['dsr'])} | {_f(s['max_drawdown_r'], 1)} | "
                     f"{lo['n']}/{sh['n']} | {_f(lo['expectancy_r'])}/{_f(sh['expectancy_r'])} |")
    pbo = report.get("cscv_pbo_simple_rules")
    lines += ["", "## Overfitting", "",
              f"CSCV PBO over {len(report['params']['tp_r']) * 9} simple rules: "
              f"{_f(pbo.get('pbo') if pbo else None)} (n_trials for DSR = "
              f"{report['params']['n_trials_for_dsr']}).", "",
              "## Conditional edge surviving BH-FDR and later-segment sign check", ""]
    surv = report["conditional_edge_survivors"]
    if not surv:
        lines.append("None.")
    for r in surv:
        lines.append(f"- {r['feature']} Q{r['quintile']} tp{r['tp_r']}: discovery "
                     f"{_f(r['discovery_expectancy_r'])} (n={r['discovery_n']}), later "
                     f"{_f(r['later_expectancy_r'])} (n={r['later_n']})")
    v = report["verdict"]
    lines += ["", "## Verdict", "", f"Gate: {v['gate']}.", "",
              f"PAPER challenger candidates: {', '.join(v['candidates_for_paper']) or 'none'}.", "",
              v["note"], ""]
    return "\n".join(lines)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input-dir", default="research/output")
    p.add_argument("--output-dir", default="research/analysis/quant")
    p.add_argument("--min-score", type=int, default=74)
    p.add_argument("--tp-r", type=float, nargs="+", default=[1.25, 2.0])
    p.add_argument("--max-hold-hours", type=float, default=24.0)
    p.add_argument("--n-folds", type=int, default=5)
    p.add_argument("--embargo-hours", type=float, default=24.0)
    p.add_argument("--min-train", type=int, default=150)
    p.add_argument("--cscv-splits", type=int, default=10)
    p.add_argument("--fdr-q", type=float, default=0.10)
    p.add_argument("--seed", type=int, default=0)
    # Candidates are hourly but held up to 24h: BASE R autocorrelation is ~0.75 at
    # lag 1 and only fades by lag ~12-20, so short blocks understate uncertainty.
    p.add_argument("--block-size", type=int, default=20)
    # Single-slot trades do not overlap in time; a short block suffices.
    p.add_argument("--slot-block-size", type=int, default=5)
    return p.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    report = run(parse_args(argv))
    print(json.dumps(report["verdict"], indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
