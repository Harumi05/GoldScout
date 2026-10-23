"""Quant SHORT diagnostic V1 (offline, observer_only, score_effect=0).

Why do SHORT candidates of the EA proxy show ~0/negative expectancy while LONG
carries the edge? Reads the edge study's ``quant_candidates.csv`` (causal
features, stop distance, signal time), re-labels every candidate on M1 with
``barriers.triple_barrier`` to recover exit reason / MFE / MAE / exit time, and
reports:

1. outcome anatomy (exit mix, MFE/MAE in R, time to SL, premature stops,
   forward drift, wider-stop sensitivity) for SHORT vs LONG;
2. context buckets (H4 trend, H1 5-day momentum, RSI, EMA20 extension, score)
   with two-sided bootstrap p-values and BH-FDR across all buckets;
3. four PRE-REGISTERED SHORT rules on the shared purged walk-forward test
   blocks with the shared gate (INSUFFICIENT_SAMPLE when n < 100);
4. calendar-quarter stability.

Nothing here feeds the EA, the dashboard or any execution path.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from bisect import bisect_left
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Sequence

from research.quant import barriers, edge_study as es, features, stats

STUDY_VERSION = "quant-short-diagnostic-v1"
HOUR_MS = es.HOUR_MS
TP_GRID = (1.25, 2.0)
COSTS = ("BASE", "STRESS")
DIR_NAME = {1: "LONG", -1: "SHORT"}
EDGE_STUDY_TRIALS = 34
MIN_GATE_N = 100

Rule = Callable[[es.Candidate], bool]


def _feat(c: es.Candidate, name: str) -> float | None:
    return c.features.get(name)


def _lt(c: es.Candidate, name: str, bound: float) -> bool:
    v = _feat(c, name)
    return v is not None and v < bound


def _ge(c: es.Candidate, name: str, bound: float) -> bool:
    v = _feat(c, name)
    return v is not None and v >= bound


def rule_s1(c: es.Candidate) -> bool:
    return c.direction < 0 and _lt(c, "h4_trend_sign", 0.0)


def rule_s2(c: es.Candidate) -> bool:
    return rule_s1(c) and _lt(c, "h1_tsmom_120", 0.0)


def rule_s3(c: es.Candidate) -> bool:
    return (c.direction < 0 and _ge(c, "h1_rsi14", 30.0)
            and _ge(c, "h1_dist_ema20_atr", -1.5))


def rule_s4(c: es.Candidate) -> bool:
    return rule_s2(c) and rule_s3(c)


# Pre-registered in docs/QUANT_SHORT_DIAGNOSTIC_V1.md before any OOS look.
PREREGISTERED_RULES: dict[str, Rule] = {
    "S1_SHORT_H4_BEAR": rule_s1,
    "S2_SHORT_TREND_ALIGNED": rule_s2,
    "S3_SHORT_NOT_EXHAUSTED": rule_s3,
    "S4_SHORT_ALIGNED_NOT_EXHAUSTED": rule_s4,
}
BENCHMARKS: dict[str, Rule] = {
    "BASELINE": lambda c: True,
    "LONG_ONLY": lambda c: c.direction > 0,
    "SHORT_ONLY": lambda c: c.direction < 0,
}


# --------------------------------------------------------------------------- loading

def _num(text: str) -> float | None:
    if text is None or text == "" or text == "None":
        return None
    v = float(text)
    return v if math.isfinite(v) else None


def load_candidates_csv(path: Path) -> list[es.Candidate]:
    """Candidates with their causal features; CSV R values kept for a parity check."""
    out: list[es.Candidate] = []
    with Path(path).open("r", encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            feats = {n: _num(row.get(n, "")) for n in features.FEATURE_NAMES}
            c = es.Candidate(row["event_id"], int(row["signal_time_ms"]), int(row["direction"]),
                             int(row["score"]), feats, _num(row["stop_dist"]))
            c.csv_r = {(cost, tp): _num(row.get(f"r_{cost}_tp{tp}", ""))  # type: ignore[attr-defined]
                       for cost in COSTS for tp in TP_GRID}
            out.append(c)
    return out


# --------------------------------------------------------------------------- path helpers

def forward_drift_r(path: barriers.M1Path, *, entry_time_ms: int, direction: int,
                    stop_dist: float, horizon_ms: int) -> float | None:
    """Direction-aligned gross mid move from the entry open to the close of the
    last M1 bar starting before ``entry + horizon`` (in units of the stop).

    None when the data does not reach the horizon.
    """
    i0 = path.index_at_or_after(entry_time_ms)
    if i0 is None or stop_dist is None or stop_dist <= 0:
        return None
    ts = path.timestamps
    end = entry_time_ms + horizon_ms
    if ts[-1] + path.bar_ms < end:
        return None
    j = bisect_left(ts, end, i0) - 1
    if j < i0:
        return None
    return direction * (path.closes[j] - path.opens[i0]) / stop_dist


def reached_after_exit(path: barriers.M1Path, *, outcome: dict, direction: int,
                       stop_dist: float, window_end_ms: int, threshold_r: float = 1.0,
                       half_spread: float = 0.0) -> bool | None:
    """After an SL exit, did the closing side later reach fill + d*threshold_r*stop
    before ``window_end_ms``? (premature-stop test). None if not an SL exit or the
    path does not cover the window.
    """
    if outcome.get("status") != "OK" or outcome.get("exit_reason") != "SL":
        return None
    ts = path.timestamps
    if ts[-1] + path.bar_ms < window_end_ms:
        return None
    level_mid = outcome["entry_price"] + direction * threshold_r * stop_dist + direction * half_spread
    start = bisect_left(ts, outcome["exit_time_ms"] + 1)  # bars after the SL bar
    end = bisect_left(ts, window_end_ms, start)
    if start >= end:
        return False
    if direction > 0:
        return max(path.highs[start:end]) >= level_mid
    return min(path.lows[start:end]) <= level_mid


def quantiles(values: Iterable[float], qs: Sequence[float] = (0.25, 0.5, 0.75)) -> list[float | None]:
    data = sorted(v for v in values if v is not None)
    if not data:
        return [None for _ in qs]
    out = []
    for q in qs:
        pos = q * (len(data) - 1)
        lo = int(math.floor(pos))
        hi = min(lo + 1, len(data) - 1)
        out.append(data[lo] + (data[hi] - data[lo]) * (pos - lo))
    return out


def _mean(values: Iterable[float | None]) -> float | None:
    data = [v for v in values if v is not None]
    return sum(data) / len(data) if data else None


def _frac(flags: Iterable[bool | None]) -> tuple[float | None, int]:
    data = [f for f in flags if f is not None]
    return (sum(data) / len(data) if data else None), len(data)


def exit_mix(outcomes: Iterable[dict]) -> dict:
    counts = {"SL": 0, "TP": 0, "TIME": 0}
    n = 0
    for o in outcomes:
        if o.get("status") == "OK":
            counts[o["exit_reason"]] += 1
            n += 1
    return {"n": n, **{k: v for k, v in counts.items()},
            **{f"{k}_share": (v / n if n else None) for k, v in counts.items()}}


# --------------------------------------------------------------------------- buckets

def trend_bucket(c: es.Candidate) -> str | None:
    h4, mom = _feat(c, "h4_trend_sign"), _feat(c, "h1_tsmom_120")
    if h4 is None or mom is None:
        return None
    return f"h4{'+' if h4 > 0 else '-' if h4 < 0 else '0'}_mom120{'+' if mom > 0 else '-'}"


def rsi_bucket(c: es.Candidate) -> str | None:
    v = _feat(c, "h1_rsi14")
    if v is None:
        return None
    for bound, label in ((30, "<30"), (40, "30-40"), (50, "40-50"), (60, "50-60"), (70, "60-70")):
        if v < bound:
            return label
    return ">=70"


def extension_bucket(c: es.Candidate) -> str | None:
    v = _feat(c, "h1_dist_ema20_atr")
    if v is None:
        return None
    for bound, label in ((-1.5, "<-1.5"), (-0.5, "-1.5..-0.5"), (0.5, "-0.5..0.5"), (1.5, "0.5..1.5")):
        if v < bound:
            return label
    return ">=1.5"


LONG_TREND_BARS = 480  # ~1 month of H1 bars; descriptive only (not a pre-registered rule)
LONG_TREND_KEY = "ctx_h1_logret_480"


def long_horizon_logret(h1: features.CausalBarSeries, as_of_ms: int,
                        bars: int = LONG_TREND_BARS) -> float | None:
    """Causal log return over the last ``bars`` CLOSED H1 bars (None on short warmup)."""
    window = h1.window(as_of_ms, bars + 1)
    if window is None:
        return None
    return math.log(window[-1].close / window[0].close)


def long_trend_bucket(c: es.Candidate) -> str | None:
    v = _feat(c, LONG_TREND_KEY)
    if v is None:
        return None
    return "ret480_up" if v > 0 else "ret480_down"


def score_bucket(c: es.Candidate) -> str:
    return "74-79" if c.score < 80 else ("80-85" if c.score < 86 else ">=86")


def quarter_label(ms: int) -> str:
    d = datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
    return f"{d.year}Q{(d.month - 1) // 3 + 1}"


BUCKETERS: dict[str, Callable[[es.Candidate], str | None]] = {
    "trend_h4_x_mom120": trend_bucket,
    "trend_h1_480": long_trend_bucket,
    "h1_rsi14": rsi_bucket,
    "h1_dist_ema20_atr": extension_bucket,
    "score": score_bucket,
}


def two_sided_p(values: Sequence[float], *, block_size: int, seed: int) -> float | None:
    block = max(1, min(block_size, len(values) // 10))
    p_pos = stats.bootstrap_pvalue_mean_gt_zero(values, block_size=block, seed=seed)
    p_neg = stats.bootstrap_pvalue_mean_gt_zero([-v for v in values], block_size=block, seed=seed)
    return None if p_pos is None or p_neg is None else min(1.0, 2 * min(p_pos, p_neg))


def bucket_rows(cands: Sequence[es.Candidate], bucketer: Callable[[es.Candidate], str | None],
                *, family: str, cost: str, tp_r: float, block_size: int, seed: int) -> list[dict]:
    """All-events (overlapping) in-sample bucket table per direction."""
    groups: dict[tuple[str, str], list[float]] = {}
    for c in cands:
        r = c.r(cost, tp_r)
        b = bucketer(c)
        if r is None or b is None:
            continue
        groups.setdefault((DIR_NAME[c.direction], b), []).append(r)
    rows = []
    for (direction, b), vals in sorted(groups.items()):
        block = max(1, min(block_size, len(vals) // 10))
        ci = stats.bootstrap_mean_ci(vals, block_size=block, seed=seed)
        rows.append({"family": family, "direction": direction, "bucket": b, "cost": cost,
                     "tp_r": tp_r, "n": len(vals), "expectancy_r": stats.expectancy(vals),
                     "ci_lower": ci["lower"], "ci_upper": ci["upper"],
                     "win_rate": stats.win_rate(vals), "profit_factor": stats.profit_factor(vals),
                     "p_two_sided": two_sided_p(vals, block_size=block_size, seed=seed)})
    return rows


def apply_bh(rows: list[dict], *, q: float, key: str = "p_two_sided") -> list[dict]:
    flags = stats.benjamini_hochberg([r[key] for r in rows], q=q) if rows else []
    for r, ok in zip(rows, flags):
        r["bh_reject"] = ok
    return rows


# --------------------------------------------------------------------------- rules

def oos_test_candidates(cands: Sequence[es.Candidate], tp_r: float, *, n_folds: int,
                        embargo_ms: int, min_train: int) -> list[es.Candidate]:
    """Union of the shared purged walk-forward TEST blocks (same as edge_study)."""
    usable = sorted((c for c in cands if c.r(es.LABEL_COST, tp_r) is not None),
                    key=lambda c: c.signal_time_ms)
    folds = stats.purged_walk_forward_folds(
        [c.signal_time_ms for c in usable], [c.exit_time(tp_r) for c in usable],
        n_folds=n_folds, embargo_ms=embargo_ms, min_train=min_train)
    return [usable[i] for f in folds for i in f.test]


def evaluate_rules(test: Sequence[es.Candidate], tp_r: float, rules: dict[str, Rule], *,
                   n_trials: int, sr_variance: float, slot_block: int, seed: int) -> dict:
    out = {}
    for name, rule in rules.items():
        for cost in COSTS:
            kept = [c for c in test if rule(c) and c.r(cost, tp_r) is not None]
            seq = es.non_overlapping(kept, tp_r, cost)
            out[f"tp{tp_r}_{name}_{cost}"] = {
                "risk_units": "r",
                "events_n": len(kept),
                "single_slot": es.summarize([c.r(cost, tp_r) for c in seq], n_trials=n_trials,
                                            sr_variance=sr_variance, block_size=slot_block,
                                            seed=seed)}
    return out


def gate(oos: dict, *, q: float) -> dict:
    """Shared edge_study gate + BH over the pre-registered rules + sample status."""
    v = es.verdict({"walk_forward_oos": oos})
    rows = [r for r in v["rules"]]
    pvals = [oos[r["rule"]]["single_slot"]["p_value_mean_gt_0"] for r in rows]
    flags = stats.benjamini_hochberg(pvals, q=q) if rows else []
    for r, p, ok in zip(rows, pvals, flags):
        r["p_value_mean_gt_0"] = p
        r["bh_reject"] = ok
        r["status"] = ("INSUFFICIENT_SAMPLE" if r["n"] < MIN_GATE_N
                       else "PAPER_CANDIDATE" if r["passes_gate"] else "FAILS_GATE")
        short_only = oos.get(r["rule"].split("_S")[0] + "_SHORT_ONLY_STRESS")
        so = short_only["single_slot"]["expectancy_r"] if short_only else None
        r["beats_short_only"] = (None if so is None or r["expectancy_r"] is None
                                 else r["expectancy_r"] > so)
    v["rules"] = rows
    return v


# --------------------------------------------------------------------------- anatomy

def anatomy(cands: Sequence[es.Candidate], *, direction: int, cost: str, tp_r: float,
            extra: dict[str, dict]) -> dict:
    sel = [c for c in cands if c.direction == direction and c.outcomes.get((cost, tp_r))]
    outs = [c.outcomes[(cost, tp_r)] for c in sel]
    ok = [o for o in outs if o["status"] == "OK"]
    sl = [o for o in ok if o["exit_reason"] == "SL"]
    prem, prem_n = _frac(extra[c.event_id].get(("premature", cost, tp_r)) for c in sel)
    wide = [extra[c.event_id].get(("wide", cost, tp_r)) for c in sel]
    wide_ok = [o for o in wide if o and o["status"] == "OK"]
    return {
        "direction": DIR_NAME[direction], "cost": cost, "tp_r": tp_r,
        "exit_mix": exit_mix(outs),
        "expectancy_r": _mean(o["r_multiple"] for o in ok),
        "mfe_r_q25_50_75": quantiles(o["mfe_r"] for o in ok),
        "mae_r_q25_50_75": quantiles(o["mae_r"] for o in ok),
        "sl_mfe_r_q25_50_75": quantiles(o["mfe_r"] for o in sl),
        "sl_share_mfe_ge_0_5r": _frac(o["mfe_r"] >= 0.5 for o in sl)[0],
        "sl_share_mfe_ge_1r": _frac(o["mfe_r"] >= 1.0 for o in sl)[0],
        "hours_to_sl_q25_50_75": quantiles(o["bars_held"] / 60.0 for o in sl),
        "sl_share_within_2h": _frac(o["bars_held"] <= 120 for o in sl)[0],
        "premature_stop_share": prem, "premature_stop_n": prem_n,
        "wide_stop_1_5x_expectancy_r": _mean(o["r_multiple"] for o in wide_ok),
        "wide_stop_1_5x_exit_mix": exit_mix(wide_ok),
    }


def drift_summary(cands: Sequence[es.Candidate], extra: dict[str, dict], direction: int) -> dict:
    out = {"direction": DIR_NAME[direction]}
    for h in (1, 4, 24):
        vals = [extra[c.event_id].get(("drift", h)) for c in cands if c.direction == direction]
        vals = [v for v in vals if v is not None]
        out[f"h{h}"] = {"n": len(vals), "mean_r": _mean(vals),
                        "median_r": quantiles(vals, (0.5,))[0],
                        "share_positive": _frac(v > 0 for v in vals)[0]}
    return out


def quarter_rows(cands: Sequence[es.Candidate], *, cost: str, tp_r: float, block_size: int,
                 seed: int) -> list[dict]:
    rows = bucket_rows(cands, lambda c: quarter_label(c.signal_time_ms), family="quarter",
                       cost=cost, tp_r=tp_r, block_size=block_size, seed=seed)
    for direction in (1, -1):
        seq = es.non_overlapping([c for c in cands if c.direction == direction], tp_r, cost)
        by_q: dict[str, list[float]] = {}
        for c in seq:
            by_q.setdefault(quarter_label(c.signal_time_ms), []).append(c.r(cost, tp_r))
        for r in rows:
            if r["direction"] == DIR_NAME[direction]:
                vals = by_q.get(r["bucket"], [])
                r["single_slot_n"] = len(vals)
                r["single_slot_expectancy_r"] = stats.expectancy(vals)
    return rows


def market_drift_by_quarter(closes: Sequence[float], timestamps: Sequence[int], *,
                            horizon_bars: int = 24) -> dict[str, dict]:
    """Unconditional mean H1 close-to-close move (price units) over ``horizon_bars``
    for every H1 bar start, grouped by calendar quarter: the drift a direction
    would collect with no timing at all (descriptive baseline)."""
    groups: dict[str, list[float]] = {}
    for i in range(len(closes) - horizon_bars):
        groups.setdefault(quarter_label(timestamps[i]), []).append(closes[i + horizon_bars] - closes[i])
    return {k: {"n": len(v), "mean_move": sum(v) / len(v),
                "share_up": sum(x > 0 for x in v) / len(v)} for k, v in sorted(groups.items())}


def candidate_drift_by_quarter(cands: Sequence[es.Candidate], closes: Sequence[float],
                               close_times: Sequence[int], *, horizon_bars: int = 24) -> list[dict]:
    """Direction-aligned H1 move (price units) from the last closed H1 bar at the
    signal to ``horizon_bars`` later, per direction and quarter."""
    groups: dict[tuple[str, str], list[float]] = {}
    for c in cands:
        i = bisect_left(close_times, c.signal_time_ms + 1) - 1  # last bar closed <= signal
        if i < 0 or i + horizon_bars >= len(closes):
            continue
        groups.setdefault((DIR_NAME[c.direction], quarter_label(c.signal_time_ms)), []).append(
            c.direction * (closes[i + horizon_bars] - closes[i]))
    return [{"direction": d, "quarter": q, "n": len(v), "mean_aligned_move": sum(v) / len(v)}
            for (d, q), v in sorted(groups.items())]


# --------------------------------------------------------------------------- CLI

def label_outcomes(cands: Sequence[es.Candidate], m1: barriers.M1Path, *,
                   max_hold_ms: int) -> tuple[dict[str, dict], int]:
    extra: dict[str, dict] = {}
    mismatches = 0
    for c in cands:
        info: dict = {}
        extra[c.event_id] = info
        if c.stop_dist is None:
            continue
        for cost in COSTS:
            sc = barriers.COST_SCENARIOS[cost]
            for tp in TP_GRID:
                o = barriers.triple_barrier(m1, signal_time_ms=c.signal_time_ms,
                                            direction=c.direction, stop_dist=c.stop_dist,
                                            tp_r=tp, max_hold_ms=max_hold_ms, cost=sc)
                c.outcomes[(cost, tp)] = o
                ref = getattr(c, "csv_r", {}).get((cost, tp))
                got = o["r_multiple"]
                if (ref is None) != (got is None) or (ref is not None and abs(ref - got) > 1e-9):
                    mismatches += 1
                if o["status"] == "OK":
                    info[("premature", cost, tp)] = reached_after_exit(
                        m1, outcome=o, direction=c.direction, stop_dist=c.stop_dist,
                        window_end_ms=o["entry_time_ms"] + max_hold_ms,
                        half_spread=sc.spread / 2.0)
                info[("wide", cost, tp)] = barriers.triple_barrier(
                    m1, signal_time_ms=c.signal_time_ms, direction=c.direction,
                    stop_dist=1.5 * c.stop_dist, tp_r=tp, max_hold_ms=max_hold_ms, cost=sc)
        entry = c.outcomes[(COSTS[0], TP_GRID[0])].get("entry_time_ms")
        if entry is not None:
            for h in (1, 4, 24):
                info[("drift", h)] = forward_drift_r(m1, entry_time_ms=entry,
                                                     direction=c.direction,
                                                     stop_dist=c.stop_dist,
                                                     horizon_ms=h * HOUR_MS)
    return extra, mismatches


def run(args: argparse.Namespace) -> dict:
    analysis = Path(args.analysis_dir)
    cands = load_candidates_csv(analysis / "quant_candidates.csv")
    edge = json.loads((analysis / "quant_edge_study.json").read_text(encoding="utf-8"))
    sr_var = float(edge["params"]["sr_variance_across_rules"])
    n_trials = EDGE_STUDY_TRIALS + len(PREREGISTERED_RULES) * len(TP_GRID)

    bars_dir = Path(args.input_dir) / "historical_bars"
    h1_bars = features.load_bars(bars_dir / "XAUUSD_H1.jsonl", timeframe="H1")
    h1 = features.CausalBarSeries(h1_bars)
    for c in cands:
        c.features[LONG_TREND_KEY] = long_horizon_logret(h1, c.signal_time_ms)
    print("[short] loading M1", file=sys.stderr)
    m1 = barriers.M1Path.from_jsonl(bars_dir / "XAUUSD_M1.jsonl")
    max_hold_ms = int(args.max_hold_hours * HOUR_MS)
    print("[short] labelling", file=sys.stderr)
    extra, mismatches = label_outcomes(cands, m1, max_hold_ms=max_hold_ms)

    report: dict = {
        "study_version": STUDY_VERSION, "observer_only": True, "score_effect": 0,
        "strategy_validated": False,
        "params": {"tp_r": list(TP_GRID), "costs": list(COSTS),
                   "max_hold_hours": args.max_hold_hours, "n_trials_for_dsr": n_trials,
                   "sr_variance_across_rules": sr_var, "block_overlapping": args.block_size,
                   "block_single_slot": args.slot_block_size, "fdr_q": args.fdr_q,
                   "n_folds": args.n_folds, "embargo_hours": args.embargo_hours,
                   "min_train": args.min_train},
        "coverage": {"candidates": len(cands), "long": sum(c.direction > 0 for c in cands),
                     "short": sum(c.direction < 0 for c in cands),
                     "csv_r_mismatches": mismatches},
        "anatomy": [anatomy(cands, direction=d, cost=cost, tp_r=tp, extra=extra)
                    for cost in COSTS for tp in TP_GRID for d in (-1, 1)],
        "forward_drift": [drift_summary(cands, extra, d) for d in (-1, 1)],
        "market_drift_24h_by_quarter": market_drift_by_quarter(
            [b.close for b in h1_bars], [b.timestamp for b in h1_bars]),
        "candidate_drift_24h_by_quarter": candidate_drift_by_quarter(
            cands, [b.close for b in h1_bars], [b.close_timestamp for b in h1_bars]),
    }

    bucket_all: list[dict] = []
    for cost in COSTS:
        for tp in TP_GRID:
            for fam, fn in BUCKETERS.items():
                bucket_all += bucket_rows(cands, fn, family=fam, cost=cost, tp_r=tp,
                                          block_size=args.block_size, seed=args.seed)
    report["buckets"] = apply_bh(bucket_all, q=args.fdr_q)
    report["quarters"] = [r for cost in COSTS for tp in TP_GRID
                          for r in quarter_rows(cands, cost=cost, tp_r=tp,
                                                block_size=args.block_size, seed=args.seed)]

    oos: dict = {}
    for tp in TP_GRID:
        test = oos_test_candidates(cands, tp, n_folds=args.n_folds,
                                   embargo_ms=int(args.embargo_hours * HOUR_MS),
                                   min_train=args.min_train)
        oos.update(evaluate_rules(test, tp, {**BENCHMARKS, **PREREGISTERED_RULES},
                                  n_trials=n_trials, sr_variance=sr_var,
                                  slot_block=args.slot_block_size, seed=args.seed))
    report["walk_forward_oos"] = oos
    report["verdict"] = gate(oos, q=args.fdr_q)

    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "short_diagnostic.json").write_text(json.dumps(report, indent=2, default=str),
                                               encoding="utf-8")
    (out / "short_diagnostic.md").write_text(render_markdown(report), encoding="utf-8")
    return report


_f = es._f


def render_markdown(report: dict) -> str:
    L = ["# Quant SHORT diagnostic V1", "",
         "> Offline, observer_only=true, score_effect=0, strategy_validated=false.", "",
         "```json", json.dumps(report["coverage"]), "```", "", "## Anatomy", "",
         "| Dir | Cost | TP | n | SL/TP/TIME % | E[R] | MFE q50 | MAE q50 | SL MFE>=1R | h to SL q50 "
         "| SL<=2h | premature | wide1.5x E[R] |",
         "|---|---|---:|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for a in report["anatomy"]:
        m = a["exit_mix"]
        mix = "/".join(_f(100 * m[f"{k}_share"], 0) if m[f"{k}_share"] is not None else "—"
                       for k in ("SL", "TP", "TIME"))
        L.append(f"| {a['direction']} | {a['cost']} | {a['tp_r']} | {m['n']} | {mix} | "
                 f"{_f(a['expectancy_r'])} | {_f(a['mfe_r_q25_50_75'][1])} | "
                 f"{_f(a['mae_r_q25_50_75'][1])} | {_f(a['sl_share_mfe_ge_1r'])} | "
                 f"{_f(a['hours_to_sl_q25_50_75'][1], 1)} | {_f(a['sl_share_within_2h'])} | "
                 f"{_f(a['premature_stop_share'])} | {_f(a['wide_stop_1_5x_expectancy_r'])} |")
    L += ["", "## Forward drift (gross, direction-aligned, R)", "",
          "| Dir | h | n | mean | median | share>0 |", "|---|---:|---:|---:|---:|---:|"]
    for d in report["forward_drift"]:
        for h in (1, 4, 24):
            s = d[f"h{h}"]
            L.append(f"| {d['direction']} | {h} | {s['n']} | {_f(s['mean_r'])} | "
                     f"{_f(s['median_r'])} | {_f(s['share_positive'])} |")
    L += ["", "## 24h H1 drift vs unconditional market (price units)", "",
          "| Quarter | market mean | share up | LONG aligned (n) | SHORT aligned (n) |",
          "|---|---:|---:|---|---|"]
    cd = {(r["direction"], r["quarter"]): r for r in report["candidate_drift_24h_by_quarter"]}
    for q, m in report["market_drift_24h_by_quarter"].items():
        cells = []
        for d in ("LONG", "SHORT"):
            r = cd.get((d, q))
            cells.append("—" if r is None else f"{_f(r['mean_aligned_move'], 2)} ({r['n']})")
        L.append(f"| {q} | {_f(m['mean_move'], 2)} | {_f(m['share_up'])} | {cells[0]} | {cells[1]} |")
    L += ["", "## Buckets (in-sample, all events, BH over all rows)", "",
          "| Family | Dir | Bucket | Cost | TP | n | E[R] | CI95 | p2 | BH |",
          "|---|---|---|---|---:|---:|---:|---|---:|---|"]
    for r in report["buckets"]:
        L.append(f"| {r['family']} | {r['direction']} | {r['bucket']} | {r['cost']} | {r['tp_r']} | "
                 f"{r['n']} | {_f(r['expectancy_r'])} | [{_f(r['ci_lower'])}, {_f(r['ci_upper'])}] | "
                 f"{_f(r['p_two_sided'])} | {'Y' if r['bh_reject'] else ''} |")
    L += ["", "## Quarters", "", "| Dir | Q | Cost | TP | n | E[R] | slot n | slot E[R] |",
          "|---|---|---|---:|---:|---:|---:|---:|"]
    for r in report["quarters"]:
        L.append(f"| {r['direction']} | {r['bucket']} | {r['cost']} | {r['tp_r']} | {r['n']} | "
                 f"{_f(r['expectancy_r'])} | {r.get('single_slot_n')} | "
                 f"{_f(r.get('single_slot_expectancy_r'))} |")
    L += ["", "## Walk-forward OOS single slot", "",
          "| Key | events | n | E[R] | CI95 | p(E>0) | PF | DSR |", "|---|---:|---:|---:|---|---:|---:|---:|"]
    for k, v in report["walk_forward_oos"].items():
        s = v["single_slot"]
        L.append(f"| {k} | {v['events_n']} | {s['n']} | {_f(s['expectancy_r'])} | "
                 f"[{_f(s['ci_lower'])}, {_f(s['ci_upper'])}] | {_f(s['p_value_mean_gt_0'])} | "
                 f"{_f(s['profit_factor'])} | {_f(s['dsr'])} |")
    L += ["", "## Gate", ""]
    for r in report["verdict"]["rules"]:
        L.append(f"- {r['rule']}: {r['status']} (n={r['n']}, E={_f(r['expectancy_r'])}, "
                 f"BH={r['bh_reject']}, beats SHORT_ONLY={r['beats_short_only']})")
    L.append("")
    return "\n".join(L)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input-dir", default="research/output")
    p.add_argument("--analysis-dir", default="research/analysis/quant")
    p.add_argument("--output-dir", default="research/analysis/quant")
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
    print(json.dumps({"coverage": report["coverage"],
                      "gate": [(r["rule"], r["status"]) for r in report["verdict"]["rules"]]},
                     indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
