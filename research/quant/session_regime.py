"""Session / time-of-day and volatility-regime filters (offline research).

observer_only=True, score_effect=0, strategy_validated=False. Nothing here is
imported by the EA, the dashboard or any execution path.

The MT5 server UTC offset is NOT established (broker_clock_analysis status
UNKNOWN), so no Asia/London/NY session is inferred: time buckets use raw
server wall-clock hour and server weekday only.

Pre-registered family (see docs/QUANT_SESSION_REGIME_V1.md): 17 buckets x
3 directions x TP grid. Train-dependent cuts (terciles) use the purged train
fold only. Single-slot sequencing compares actual entry times and releases the
slot at the CLOSE of the exit M1 bar; censored trades reserve the slot for the
full max hold.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Sequence

from research.quant import stats

HOUR_MS = 3_600_000
M1_MS = 60_000
STUDY_VERSION = "quant-session-regime-v1"
DIRECTIONS = {"BOTH": 0, "LONG": 1, "SHORT": -1}
BENCHMARK_RULES = ("BASELINE", "LONG_ONLY", "SHORT_ONLY")
HOUR_BUCKETS = ("HOUR_00_05", "HOUR_06_11", "HOUR_12_17", "HOUR_18_23")
DOW_NAMES = ("DOW_MON", "DOW_TUE", "DOW_WED", "DOW_THU", "DOW_FRI")
TERCILE_NAMES = ("LOW", "MID", "HIGH")
# family -> bucket labels (order is the pre-registered order)
FAMILIES: dict[str, tuple[str, ...]] = {
    "server_hour": HOUR_BUCKETS,
    "rv24_tercile": tuple(f"RV_{t}" for t in TERCILE_NAMES),
    "vol_ratio": ("VR_EXPANSION", "VR_CONTRACTION"),
    "h4_strength_tercile": tuple(f"H4S_{t}" for t in TERCILE_NAMES),
    "day_of_week": DOW_NAMES,
}
ALL_BUCKETS: tuple[str, ...] = tuple(b for bs in FAMILIES.values() for b in bs)


# --------------------------------------------------------------------------- buckets

def server_hour(signal_time_ms: int) -> int:
    """Server wall-clock hour; timestamps encode server wall time as epoch ms."""
    return (int(signal_time_ms) // HOUR_MS) % 24


def server_hour_bucket(signal_time_ms: int) -> str:
    return HOUR_BUCKETS[server_hour(signal_time_ms) // 6]


def dow_bucket(signal_time_ms: int) -> str | None:
    """Server weekday; Sunday (weekly reopen) merges into Monday; Saturday -> None."""
    wd = datetime.fromtimestamp(int(signal_time_ms) / 1000, timezone.utc).weekday()
    if wd == 6:
        return "DOW_MON"
    return DOW_NAMES[wd] if wd < 5 else None


def tercile_cuts(values: Sequence[float | None]) -> tuple[float, float] | None:
    """Train-only tercile cut points; None when fewer than 3 known values."""
    vals = sorted(v for v in values if v is not None)
    if len(vals) < 3:
        return None
    n = len(vals)
    return vals[n // 3], vals[2 * n // 3]


def tercile_label(value: float | None, cuts: tuple[float, float] | None) -> str | None:
    if value is None or cuts is None:
        return None
    if value < cuts[0]:
        return "LOW"
    if value < cuts[1]:
        return "MID"
    return "HIGH"


def vol_ratio_bucket(value: float | None) -> str | None:
    if value is None:
        return None
    return "VR_EXPANSION" if value > 1.0 else "VR_CONTRACTION"


def h4_strength(features: dict) -> float | None:
    v = features.get("h4_dist_ema50_atr")
    return None if v is None else abs(v)


@dataclass(frozen=True)
class TrainCuts:
    rv24: tuple[float, float] | None
    h4_strength: tuple[float, float] | None

    @classmethod
    def from_train(cls, train_features: Sequence[dict]) -> "TrainCuts":
        return cls(tercile_cuts([f.get("h1_rv24") for f in train_features]),
                   tercile_cuts([h4_strength(f) for f in train_features]))


def candidate_buckets(signal_time_ms: int, features: dict, cuts: TrainCuts) -> set[str]:
    """All pre-registered buckets a candidate belongs to (one per known family)."""
    out = {server_hour_bucket(signal_time_ms)}
    dow = dow_bucket(signal_time_ms)
    if dow:
        out.add(dow)
    rv = tercile_label(features.get("h1_rv24"), cuts.rv24)
    if rv:
        out.add(f"RV_{rv}")
    vr = vol_ratio_bucket(features.get("h1_vol_ratio"))
    if vr:
        out.add(vr)
    h4 = tercile_label(h4_strength(features), cuts.h4_strength)
    if h4:
        out.add(f"H4S_{h4}")
    return out


def hypothesis_names(tp_grid: Sequence[float]) -> list[str]:
    return [hypothesis_key(tp, b, d) for tp in tp_grid for b in ALL_BUCKETS for d in DIRECTIONS]


def hypothesis_key(tp_r: float, bucket: str, direction: str) -> str:
    return f"tp{tp_r}_{bucket}_{direction}"


# --------------------------------------------------------------------------- single slot

@dataclass(frozen=True)
class SlotTrade:
    """One candidate's outcome under one cost/TP, as seen by the slot sequencer."""

    key: object            # caller's identity (e.g. Candidate)
    direction: int
    entry_time_ms: int
    release_time_ms: int   # slot free from this instant (inclusive)
    r: float | None        # None => censored: reserves slot, contributes no R


def slot_trade(key: object, direction: int, outcome: dict | None, *,
               max_hold_ms: int, bar_ms: int = M1_MS) -> SlotTrade | None:
    """Build a SlotTrade from a barriers.triple_barrier outcome.

    SL/TP exits are stamped at the exit M1 bar OPEN (intrabar), so the slot is
    released at that bar's close (+bar_ms). TIME exits are already stamped at
    the close. INSUFFICIENT_PATH reserves the slot for the full max hold.
    Outcomes without an entry (NO_ENTRY_BAR, missing) return None.
    """
    if outcome is None or outcome.get("entry_time_ms") is None:
        return None
    entry = int(outcome["entry_time_ms"])
    status = outcome.get("status")
    if status == "OK":
        exit_ms = int(outcome["exit_time_ms"])
        release = exit_ms if outcome.get("exit_reason") == "TIME" else exit_ms + bar_ms
        return SlotTrade(key, direction, entry, release, float(outcome["r_multiple"]))
    if status == "INSUFFICIENT_PATH":
        return SlotTrade(key, direction, entry, entry + int(max_hold_ms), None)
    return None


def single_slot(trades: Sequence[SlotTrade]) -> list[SlotTrade]:
    """Greedy one-position-at-a-time sequence ordered by actual entry time.

    A trade is taken only if its entry_time_ms >= the release time of the
    previously taken trade (censored trades included, they hold the slot).
    """
    taken: list[SlotTrade] = []
    free_at = None
    for t in sorted(trades, key=lambda x: (x.entry_time_ms, str(getattr(x.key, "event_id", "")))):
        if free_at is not None and t.entry_time_ms < free_at:
            continue
        taken.append(t)
        free_at = t.release_time_ms
    return taken


# --------------------------------------------------------------------------- evaluation

def summarize(values: Sequence[float], *, n_trials: int, block_size: int, seed: int) -> dict:
    """Per-sequence stats; DSR uses sr_variance = 1/(n-1) (noise of a per-trade SR)."""
    vals = [float(v) for v in values if v is not None]
    n = len(vals)
    block = max(1, min(block_size, n // 10))
    ci = stats.bootstrap_mean_ci(vals, block_size=block, seed=seed)
    sr_var = 1.0 / (n - 1) if n > 1 else None
    return {
        "n": n,
        "expectancy_r": stats.expectancy(vals),
        "ci_lower": ci["lower"], "ci_upper": ci["upper"],
        "p_value_mean_gt_0": stats.bootstrap_pvalue_mean_gt_zero(vals, block_size=block, seed=seed),
        "profit_factor": stats.profit_factor(vals),
        "win_rate": stats.win_rate(vals),
        "sharpe_per_trade": stats.sharpe_ratio(vals),
        "dsr": (stats.deflated_sharpe_ratio(vals, n_trials=n_trials, sr_variance=sr_var)
                if n > 2 else None),
        "dsr_sr_variance": sr_var,
        "total_r": sum(vals) if vals else None,
        "bootstrap_block_size": block,
    }


def apply_bh(rows: list[dict], *, q: float) -> None:
    """Joint BH-FDR across the whole family (in place, key 'bh_reject')."""
    flags = stats.benjamini_hochberg([r["single_slot"]["p_value_mean_gt_0"] for r in rows], q=q)
    for row, ok in zip(rows, flags):
        row["bh_reject"] = ok


GATE_TEXT = ("STRESS single-slot purged walk-forward OOS: n>=100, CI95 lower>0, DSR>=0.95 "
             "(n_trials declared, sr_variance=1/(n-1)), PF>=1.2, E[R] above BASELINE/LONG_ONLY/"
             "SHORT_ONLY (all present and known) at the same TP, and joint BH-FDR rejection")


def gate(row: dict, benchmarks: dict[str, dict | None]) -> dict:
    """Shared pre-registered gate; any unknown benchmark fails closed (R7)."""
    s = row["single_slot"]
    bench = [None if benchmarks.get(b) is None else benchmarks[b].get("expectancy_r")
             for b in BENCHMARK_RULES]
    benchmarks_known = all(b is not None for b in bench)
    best = max(bench) if benchmarks_known else None
    e = s["expectancy_r"]
    checks = {
        "n_ge_100": s["n"] >= 100,
        "ci_lower_gt_0": s["ci_lower"] is not None and s["ci_lower"] > 0,
        "dsr_ge_0_95": s["dsr"] is not None and s["dsr"] >= 0.95,
        "pf_ge_1_2": s["profit_factor"] is not None and s["profit_factor"] >= 1.2,
        "benchmarks_known": benchmarks_known,
        "beats_benchmarks": benchmarks_known and e is not None and e > best,
        "bh_reject": bool(row.get("bh_reject")),
    }
    return {"passes": all(checks.values()), "checks": checks, "best_benchmark_expectancy_r": best}


def select(direction_filter: int, bucket: str | None,
           buckets_of: Callable[[object], set[str]], direction_of: Callable[[object], int],
           items: Sequence[object]) -> list[object]:
    """Items matching a (bucket, direction) rule; bucket None = no bucket filter."""
    out = []
    for it in items:
        if direction_filter and direction_of(it) != direction_filter:
            continue
        if bucket is not None and bucket not in buckets_of(it):
            continue
        out.append(it)
    return out
