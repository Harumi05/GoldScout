"""Pre-registered offline exit simulation; never used for order authorization."""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
import math

from research.quant.barriers import CostScenario, M1Path

HOUR_MS = 3_600_000
MAX_HOLD_MS = 24 * HOUR_MS


@dataclass(frozen=True)
class ExitPolicy:
    name: str
    tp_r: float | None = None
    breakeven: bool = False
    chandelier_atr: float | None = None
    partial: bool = False
    time_stop: bool = False


POLICIES = (
    ExitPolicy("FIXED_1.25", 1.25),
    ExitPolicy("FIXED_2", 2.0),
    ExitPolicy("BE_1_TO_2", 2.0, breakeven=True),
    ExitPolicy("CHANDELIER_1.5", chandelier_atr=1.5),
    ExitPolicy("CHANDELIER_2", chandelier_atr=2.0),
    ExitPolicy("CHANDELIER_3", chandelier_atr=3.0),
    ExitPolicy("PARTIAL_1_TO_2", 2.0, partial=True),
    ExitPolicy("PARTIAL_1_TO_3", 3.0, partial=True),
    ExitPolicy("TIME_8H_TO_2", 2.0, time_stop=True),
)


def simulate_exit(path: M1Path, *, signal_time_ms: int, direction: int,
                  stop_dist: float, atr: float, policy: ExitPolicy,
                  cost: CostScenario, max_hold_ms: int = MAX_HOLD_MS) -> dict:
    """MID bars; closing-side stops, adverse fills, SL first on ambiguity.

    Dynamic stops use completed M1 bars only. ATR is frozen at signal time.
    Intrabar fills reserve the slot through minute close; known open fills
    release it immediately. Partial weights sum to one, including commission.
    """
    if direction not in (-1, 1):
        raise ValueError("direction must be +/-1")
    if any(not math.isfinite(v) or v <= 0 for v in (stop_dist, atr)):
        raise ValueError("stop and ATR must be positive and finite")
    if policy not in POLICIES:
        raise ValueError("policy must belong to the pre-registered grid")
    if max_hold_ms <= 0:
        raise ValueError("max_hold_ms must be positive")
    if any(not math.isfinite(v) or v < 0 for v in
           (cost.spread, cost.slippage, cost.commission_price)):
        raise ValueError("costs must be finite and nonnegative")
    out = dict(status="NO_ENTRY_BAR", policy=policy.name, direction=direction,
               cost_scenario=cost.name, entry_time_ms=None, exit_time_ms=None,
               r_multiple=None, exit_reason=None, ambiguous_bar=False,
               partial_taken=False, legs=[], observer_only=True, score_effect=0,
               strategy_validated=False, horizon_gap_ms=None)
    ts = path.timestamps
    i0 = bisect_left(ts, signal_time_ms)
    if i0 == len(ts):
        return out
    d = direction
    half = cost.spread / 2
    entry = path.opens[i0] + d * (half + cost.slippage)
    start = ts[i0]
    deadline = start + max_hold_ms
    end = bisect_left(ts, deadline, i0)
    covered = end < len(ts) or ts[-1] + path.bar_ms >= deadline
    sl = entry - d * stop_dist
    target = None if policy.tp_r is None else entry + d * policy.tp_r * stop_dist
    first_target = entry + d * stop_dist
    best = path.opens[i0] - d * half
    remaining = 1.0
    net_r = 0.0
    out.update(entry_time_ms=start, entry_price=entry)

    def leg(side: float, weight: float, when: int, reason: str) -> None:
        nonlocal net_r, remaining
        price = side - d * cost.slippage
        result = weight * (d * (price - entry) - cost.commission_price) / stop_dist
        net_r += result
        remaining -= weight
        out["legs"].append(dict(weight=weight, exit_price=price,
                                exit_time_ms=when, reason=reason, r_multiple=result))
        out.update(exit_time_ms=when, exit_reason=reason)

    for i in range(i0, end):
        o = path.opens[i] - d * half
        fav = (path.highs[i] if d == 1 else path.lows[i]) - d * half
        adv = (path.lows[i] if d == 1 else path.highs[i]) - d * half
        close_time = ts[i] + path.bar_ms
        # Known opening gaps precede any unknown intrabar path.
        if d * (o - sl) <= 0:
            leg(o, remaining, ts[i], "SL_GAP")
            break
        if policy.partial and not out["partial_taken"] and d * (o - first_target) >= 0:
            leg(first_target, 0.5, ts[i], "PARTIAL")
            out["partial_taken"] = True
        if target is not None and d * (o - target) >= 0:
            leg(target, remaining, ts[i], "TP_GAP")
            break
        if policy.time_stop and ts[i] >= start + 8 * HOUR_MS:
            executable_r = (d * (o - d * cost.slippage - entry) - cost.commission_price) / stop_dist
            if executable_r < 0.25:
                leg(o, remaining, ts[i], "TIME_8H")
                break
        stop_hit = d * (adv - sl) <= 0
        partial_hit = policy.partial and not out["partial_taken"] and d * (fav - first_target) >= 0
        tp_hit = target is not None and d * (fav - target) >= 0
        if stop_hit:
            out["ambiguous_bar"] = bool(partial_hit or tp_hit)
            leg(sl, remaining, close_time, "SL")
            break
        if partial_hit:
            leg(first_target, 0.5, close_time, "PARTIAL")
            out["partial_taken"] = True
        if tp_hit:
            leg(target, remaining, close_time, "TP")
            break
        # These levels cannot apply retrospectively within bar i.
        if d * (fav - best) > 0:
            best = fav
        if policy.breakeven and d * (best - entry) >= stop_dist:
            if d * (entry - sl) > 0:
                sl = entry
        if policy.chandelier_atr is not None:
            proposed = best - d * policy.chandelier_atr * atr
            if d * (proposed - sl) > 0:
                sl = proposed
    else:
        if not covered:
            out.update(status="INSUFFICIENT_PATH", exit_time_ms=None, exit_reason=None)
            return out
        i = end - 1
        leg(path.closes[i] - d * half, remaining, ts[i] + path.bar_ms, "TIME")
        out["horizon_gap_ms"] = max(0, deadline - (ts[i] + path.bar_ms))
    out.update(status="OK", r_multiple=net_r, bars_held=i - i0 + 1)
    return out
