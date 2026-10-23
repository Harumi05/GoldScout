"""M1 triple-barrier outcomes with explicit cost scenarios (offline research).

observer_only=True, score_effect=0. Nothing in this module is used by the EA,
the dashboard or any execution path. It never changes RiskPercent,
DailyLossLimit, SL/TP, scoring, thresholds or order authorization.

Price conventions
-----------------
* Bar prices (open/high/low/close) are treated as MID. The historical export's
  spread is a near-constant 0.30, so trading costs come exclusively from an
  explicit :class:`CostScenario`, never from the bar data.
* A LONG buys at ask (mid + spread/2) and closes at bid (mid - spread/2).
  A SHORT sells at bid (mid - spread/2) and closes at ask (mid + spread/2).
* Entry fill  = entry_bar.open + direction * (spread/2 + slippage).
* Exit fill   = closing-side price - direction * slippage (always adverse).
* Commission (``commission_price``, round turn, price units) is subtracted once.
* Round-turn cost in price = spread + 2*slippage + commission_price.
"""

from __future__ import annotations

import json
import math
from bisect import bisect_left
from dataclasses import dataclass
from typing import Any, Iterable

BAR_MS = 60_000
_CHUNK = 32  # bars examined per C-level min()/max() skip test


# --------------------------------------------------------------------------
# Cost scenarios
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class CostScenario:
    """Trading-cost assumption, all values in XAUUSD price units per unit traded.

    ``spread``: full bid/ask spread. ``slippage``: adverse slippage applied on
    EACH side (entry and exit). ``commission_price``: round-turn commission
    expressed in price units, subtracted once.
    """

    name: str
    spread: float
    slippage: float
    commission_price: float

    @property
    def round_turn(self) -> float:
        return self.spread + 2.0 * self.slippage + self.commission_price


# ZERO is a frictionless reference. BASE and STRESS are research ASSUMPTIONS,
# NOT broker-verified costs: BASE mirrors the export's ~0.30 spread plus a
# small slippage allowance; STRESS uses the EA's MaxSpreadUSD=0.60 gate, a
# heavier slippage and a commission allowance.
COST_SCENARIOS: dict[str, CostScenario] = {
    "ZERO": CostScenario("ZERO", 0.0, 0.0, 0.0),
    "BASE": CostScenario("BASE", 0.30, 0.05, 0.0),
    "STRESS": CostScenario("STRESS", 0.60, 0.15, 0.07),
}


# --------------------------------------------------------------------------
# M1 path
# --------------------------------------------------------------------------
def _field(row: Any, name: str) -> Any:
    if isinstance(row, dict):
        return row[name]
    return getattr(row, name)


class M1Path:
    """Parallel arrays of M1 bars sorted by strictly increasing timestamp (ms)."""

    __slots__ = ("timestamps", "opens", "highs", "lows", "closes", "bar_ms")

    def __init__(self, bars: Iterable[Any], *, bar_ms: int = BAR_MS) -> None:
        ts: list[int] = []
        o: list[float] = []
        h: list[float] = []
        lo: list[float] = []
        c: list[float] = []
        prev = None
        for row in bars:
            t = int(_field(row, "timestamp"))
            if prev is not None and t <= prev:
                raise ValueError(f"M1 timestamps must be strictly increasing ({prev} -> {t})")
            prev = t
            ts.append(t)
            o.append(float(_field(row, "open")))
            h.append(float(_field(row, "high")))
            lo.append(float(_field(row, "low")))
            c.append(float(_field(row, "close")))
        self.timestamps = ts
        self.opens = o
        self.highs = h
        self.lows = lo
        self.closes = c
        self.bar_ms = int(bar_ms)

    def __len__(self) -> int:
        return len(self.timestamps)

    @classmethod
    def from_jsonl(cls, path: Any, *, bar_ms: int = BAR_MS) -> "M1Path":
        """Stream a JSONL bar file (blank lines skipped)."""

        def rows():
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        yield json.loads(line)

        return cls(rows(), bar_ms=bar_ms)

    def index_at_or_after(self, ts: int) -> int | None:
        i = bisect_left(self.timestamps, ts)
        return i if i < len(self.timestamps) else None


# --------------------------------------------------------------------------
# Stop distance (APPROXIMATION of EA CURRENT; not parity)
# --------------------------------------------------------------------------
def stop_distance(
    atr: float | None,
    *,
    structure_distance: float | None = None,
    atr_multiplier: float = 1.20,
    min_atr: float = 0.80,
    max_atr: float = 2.00,
    structure_buffer_atr: float = 0.25,
) -> float | None:
    """Approximate the EA CURRENT "ATR + structure" stop distance (price units).

    This is an APPROXIMATION for research, NOT a parity implementation of the
    EA's stop logic. Defaults are the EA source defaults (ATRStopMultiplier,
    MinStopATR, MaxStopATR, StructureBufferATR), not verified VPS inputs.

    base = atr*atr_multiplier; with a structure distance the candidate is
    structure_distance + structure_buffer_atr*atr and the wider of the two is
    used; the result is clamped to [min_atr*atr, max_atr*atr].
    Returns None when atr is missing, non-finite or <= 0.
    """
    if atr is None:
        return None
    atr = float(atr)
    if not math.isfinite(atr) or atr <= 0:
        return None
    dist = atr * atr_multiplier
    if structure_distance is not None:
        sd = float(structure_distance)
        if math.isfinite(sd):
            dist = max(dist, sd + structure_buffer_atr * atr)
    return min(max(dist, min_atr * atr), max_atr * atr)


# --------------------------------------------------------------------------
# Triple barrier
# --------------------------------------------------------------------------
def _result(status: str, direction: int, cost: CostScenario, **kw: Any) -> dict:
    out = {
        "status": status,
        "direction": direction,
        "entry_time_ms": None,
        "entry_price": None,
        "exit_time_ms": None,
        "exit_price": None,
        "exit_reason": None,
        "r_multiple": None,
        "gross_r": None,
        "mfe_r": None,
        "mae_r": None,
        "bars_held": None,
        "ambiguous_bar": False,
        "cost_scenario": cost.name,
        "observer_only": True,
        "score_effect": 0,
    }
    out.update(kw)
    return out


def triple_barrier(
    path: M1Path,
    *,
    signal_time_ms: int,
    direction: int,
    stop_dist: float,
    tp_r: float,
    max_hold_ms: int,
    cost: CostScenario,
) -> dict:
    """Label one hypothetical trade with SL / TP / time barriers on M1 bars.

    Conventions (see module docstring for price sides):
    * Entry at the OPEN of the first bar with timestamp >= signal_time_ms.
    * sl = fill - d*stop_dist, tp = fill + d*tp_r*stop_dist (relative to fill).
    * Bars are walked from the entry bar (inclusive) while
      timestamp < entry_time + max_hold_ms, comparing the closing-side
      (long: bid, short: ask) extremes against the levels.
    * Per bar: if the closing-side OPEN is already through SL -> exit at that
      open (gap, worse than the level); through TP -> exit at the TP level
      (conservative, no positive gap slippage credited). Otherwise, if both SL
      and TP lie inside the bar's range -> SL (pessimistic), ambiguous_bar=True.
    * Time barrier: exit at the close of the last bar inside the window.
    * If the data ends before the window is covered and no barrier was hit the
      outcome is censored: status INSUFFICIENT_PATH, r_multiple None.
    * r_multiple is net of all costs / stop_dist; gross_r is mid-to-mid for the
      same exit event without costs. mfe_r / mae_r are closing-side excursions
      vs the fill (mae_r is <= 0 typically), over bars fully before the exit bar
      plus the entry-time closing price and the exit price itself (intrabar
      ordering of the exit bar is unknown, so its other extreme is ignored).
    """
    if direction not in (1, -1):
        raise ValueError("direction must be +1 (long) or -1 (short)")
    if stop_dist is None or not (float(stop_dist) > 0) or not math.isfinite(float(stop_dist)):
        raise ValueError("stop_dist must be a finite number > 0")
    if tp_r is None or not (float(tp_r) > 0) or not math.isfinite(float(tp_r)):
        raise ValueError("tp_r must be a finite number > 0")
    if max_hold_ms is None or int(max_hold_ms) <= 0:
        raise ValueError("max_hold_ms must be > 0")
    if not isinstance(cost, CostScenario):
        raise ValueError("cost must be a CostScenario")

    d = direction
    stop = float(stop_dist)
    half = cost.spread / 2.0
    slip = cost.slippage
    ts = path.timestamps
    n = len(ts)

    i0 = bisect_left(ts, int(signal_time_ms))
    if i0 >= n:
        return _result("NO_ENTRY_BAR", d, cost)

    opens, highs, lows, closes = path.opens, path.highs, path.lows, path.closes
    entry_time = ts[i0]
    entry_mid = opens[i0]
    fill = entry_mid + d * (half + slip)
    sl = fill - d * stop
    tp = fill + d * float(tp_r) * stop
    # Levels translated into MID space (closing side = mid - d*half).
    sl_mid = sl + d * half
    tp_mid = tp + d * half

    deadline = entry_time + int(max_hold_ms)
    j_end = bisect_left(ts, deadline, i0)  # first bar outside the window
    covered = j_end < n or (ts[n - 1] + path.bar_ms >= deadline)

    # Excursions tracked in MID space; start at the entry mid.
    best_mid = entry_mid
    worst_mid = entry_mid

    exit_idx = -1
    exit_mid = 0.0
    reason = ""
    ambiguous = False

    if d == 1:
        adv, fav = lows, highs
    else:
        adv, fav = highs, lows

    i = i0
    while i < j_end:
        k = i + _CHUNK
        if k > j_end:
            k = j_end
        if d == 1:
            lo = min(lows[i:k])
            hi = max(highs[i:k])
            clear = lo > sl_mid and hi < tp_mid
            if clear:
                if hi > best_mid:
                    best_mid = hi
                if lo < worst_mid:
                    worst_mid = lo
        else:
            lo = min(lows[i:k])
            hi = max(highs[i:k])
            clear = hi < sl_mid and lo > tp_mid
            if clear:
                if lo < best_mid:
                    best_mid = lo
                if hi > worst_mid:
                    worst_mid = hi
        if clear:
            i = k
            continue
        # Scan the chunk bar by bar.
        for b in range(i, k):
            o = opens[b]
            if d * (o - sl_mid) <= 0:  # gapped through SL at the open
                exit_idx, exit_mid, reason = b, o, "SL"
                break
            if d * (o - tp_mid) >= 0:  # gapped through TP: credit the level only
                exit_idx, exit_mid, reason = b, tp_mid, "TP"
                break
            sl_hit = d * (adv[b] - sl_mid) <= 0
            tp_hit = d * (fav[b] - tp_mid) >= 0
            if sl_hit:
                exit_idx, exit_mid, reason = b, sl_mid, "SL"
                ambiguous = tp_hit
                break
            if tp_hit:
                exit_idx, exit_mid, reason = b, tp_mid, "TP"
                break
            if d * (fav[b] - best_mid) > 0:
                best_mid = fav[b]
            if d * (adv[b] - worst_mid) < 0:
                worst_mid = adv[b]
        if exit_idx >= 0:
            break
        i = k

    if exit_idx < 0:
        if not covered:
            return _result(
                "INSUFFICIENT_PATH",
                d,
                cost,
                entry_time_ms=entry_time,
                entry_price=fill,
                bars_held=j_end - i0,
            )
        exit_idx = j_end - 1
        exit_mid = closes[exit_idx]
        reason = "TIME"

    exit_side = exit_mid - d * half  # closing side (bid for long, ask for short)
    exit_price = exit_side - d * slip
    net = d * (exit_price - fill) - cost.commission_price
    gross = d * (exit_mid - entry_mid)
    # Excursions on the closing side relative to the fill, including exit price.
    best_side = best_mid - d * half
    worst_side = worst_mid - d * half
    mfe = max(d * (best_side - fill), d * (exit_side - fill))
    mae = min(d * (worst_side - fill), d * (exit_side - fill))

    return _result(
        "OK",
        d,
        cost,
        entry_time_ms=entry_time,
        entry_price=fill,
        exit_time_ms=ts[exit_idx] + (path.bar_ms if reason == "TIME" else 0),
        exit_price=exit_price,
        exit_reason=reason,
        r_multiple=net / stop,
        gross_r=gross / stop,
        mfe_r=mfe / stop,
        mae_r=mae / stop,
        bars_held=exit_idx - i0 + 1,
        ambiguous_bar=ambiguous,
    )


# --------------------------------------------------------------------------
# Volatility targeting (DOWN-ONLY)
# --------------------------------------------------------------------------
def vol_target_multiplier(
    forecast_vol: float | None, target_vol: float, *, floor: float = 0.25
) -> float | None:
    """Down-only position-size multiplier in [floor, 1.0].

    Returns min(1.0, max(floor, target_vol / forecast_vol)). The result is
    capped at 1.0 so research can never scale ABOVE the protected RiskPercent
    ceiling; it can only scale risk down in high-volatility conditions.
    Returns None when the forecast is missing, non-finite or <= 0.
    """
    if target_vol is None or not math.isfinite(float(target_vol)) or float(target_vol) <= 0:
        raise ValueError("target_vol must be a finite number > 0")
    if not (0.0 < float(floor) <= 1.0):
        raise ValueError("floor must be in (0, 1]")
    if forecast_vol is None:
        return None
    fv = float(forecast_vol)
    if not math.isfinite(fv) or fv <= 0:
        return None
    return min(1.0, max(float(floor), float(target_vol) / fv))
