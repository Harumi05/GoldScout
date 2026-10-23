"""Causal bar store and causal H1/H4/M15 feature vector (quant research v1).

Offline, diagnostic-only. Every record declares ``observer_only=True`` and
``score_effect=0``. Nothing here is imported by the EA or execution path.

Causality rule: a feature computed "as of" ``as_of_ms`` only uses bars whose
``close_timestamp <= as_of_ms``. The forming bar (open <= as_of < close) is
never visible. Every access path into a :class:`CausalBarSeries` goes through
``_last_index`` which asserts this and raises :class:`LookAheadError`.

Indicator arrays (EMA, Wilder ATR, Wilder RSI, log returns) are precomputed
once per series and indexed by bar index; the value at index ``i`` depends
only on bars ``0..i``, so reading index ``closed_upto(t) - 1`` is causal.

Timestamps are epoch ms of MT5 server wall time. Calendar features read the
wall-clock fields directly (UTC arithmetic on the raw number); no timezone or
session is inferred.

Python 3.12 standard library only.
"""

from __future__ import annotations

import json
import math
from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

FEATURE_VERSION = "quant-features-v1"

FEATURE_NAMES: tuple[str, ...] = (
    "h1_atr14",
    "h1_atr_pct",
    "h1_rv24",
    "h1_rv120",
    "h1_vol_ratio",
    "h1_tsmom_6",
    "h1_tsmom_24",
    "h1_tsmom_120",
    "h1_dist_ema20_atr",
    "h1_dist_ema50_atr",
    "h1_ema50_slope_atr",
    "h1_efficiency_20",
    "h1_variance_ratio_4",
    "h1_range_pos_50",
    "h1_rsi14",
    "h4_trend_sign",
    "h4_dist_ema50_atr",
    "m15_ret4_atr",
    "m15_rsi14",
    "server_hour",
    "server_weekday",
    "h1_spread_atr",
)

ATR_PERIOD = 14
RSI_PERIOD = 14
EMA_PERIODS = (20, 50, 200)
EMA_SLOPE_BARS = 5
RV_SHORT = 24
RV_LONG = 120
TSMOM_HORIZONS = (6, 24, 120)
EFFICIENCY_BARS = 20
VR_Q = 4
VR_WINDOW = 120
RANGE_BARS = 50
M15_RET_BARS = 4


class LookAheadError(ValueError):
    """Raised when a computation would read a bar not closed as of its time."""


@dataclass(frozen=True, slots=True)
class Bar:
    timeframe: str
    timestamp: int
    close_timestamp: int
    open: float
    high: float
    low: float
    close: float
    avg_spread: float | None


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #

def _finite_float(value: object, field: str, line_no: int) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"line {line_no}: field {field!r} is not numeric")
    out = float(value)
    if not math.isfinite(out):
        raise ValueError(f"line {line_no}: field {field!r} is not finite")
    return out


def _bar_from_record(record: dict, line_no: int) -> Bar:
    try:
        timeframe = record["timeframe"]
        ts = record["timestamp"]
        cts = record["close_timestamp"]
    except KeyError as exc:
        raise ValueError(f"line {line_no}: missing field {exc.args[0]!r}") from None
    if not isinstance(timeframe, str) or not timeframe:
        raise ValueError(f"line {line_no}: invalid timeframe")
    if isinstance(ts, bool) or isinstance(cts, bool) or not isinstance(ts, int) or not isinstance(cts, int):
        raise ValueError(f"line {line_no}: timestamps must be integer epoch ms")
    if cts <= ts:
        raise ValueError(f"line {line_no}: close_timestamp must be > timestamp")
    o = _finite_float(record.get("open"), "open", line_no)
    h = _finite_float(record.get("high"), "high", line_no)
    lo = _finite_float(record.get("low"), "low", line_no)
    c = _finite_float(record.get("close"), "close", line_no)
    if h < lo or h < max(o, c) or lo > min(o, c):
        raise ValueError(f"line {line_no}: inconsistent OHLC")
    if o <= 0 or lo <= 0:
        raise ValueError(f"line {line_no}: non-positive price")
    spread_raw = record.get("avg_spread")
    spread: float | None = None
    if spread_raw is not None:
        if isinstance(spread_raw, (int, float)) and not isinstance(spread_raw, bool) \
                and math.isfinite(float(spread_raw)) and float(spread_raw) >= 0:
            spread = float(spread_raw)
    return Bar(timeframe, ts, cts, o, h, lo, c, spread)


def load_bars(path: str | Path, *, timeframe: str | None = None) -> list[Bar]:
    """Stream a bar JSONL file. Blank lines are skipped.

    Raises ValueError on malformed records, mixed timeframes, a timeframe
    different from ``timeframe`` (when given), duplicate timestamps, or
    non-monotonic timestamps.
    """
    bars: list[Bar] = []
    expected = timeframe
    prev_ts: int | None = None
    prev_cts: int | None = None
    with open(path, "r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            text = line.strip()
            if not text:
                continue
            try:
                record = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ValueError(f"line {line_no}: invalid JSON ({exc.msg})") from None
            if not isinstance(record, dict):
                raise ValueError(f"line {line_no}: record is not an object")
            bar = _bar_from_record(record, line_no)
            if expected is None:
                expected = bar.timeframe
            elif bar.timeframe != expected:
                raise ValueError(f"line {line_no}: timeframe {bar.timeframe!r} != {expected!r}")
            if prev_ts is not None:
                if bar.timestamp == prev_ts:
                    raise ValueError(f"line {line_no}: duplicate timestamp {bar.timestamp}")
                if bar.timestamp < prev_ts or (prev_cts is not None and bar.close_timestamp <= prev_cts):
                    raise ValueError(f"line {line_no}: non-monotonic timestamp {bar.timestamp}")
            prev_ts, prev_cts = bar.timestamp, bar.close_timestamp
            bars.append(bar)
    return bars


# --------------------------------------------------------------------------- #
# Indicator arrays (value at index i depends only on bars 0..i)
# --------------------------------------------------------------------------- #

def ema_array(closes: Sequence[float], period: int) -> list[float | None]:
    """EMA seeded with the SMA of the first ``period`` closes; None before."""
    n = len(closes)
    out: list[float | None] = [None] * n
    if n < period:
        return out
    alpha = 2.0 / (period + 1)
    value = sum(closes[:period]) / period
    out[period - 1] = value
    for i in range(period, n):
        value = value + alpha * (closes[i] - value)
        out[i] = value
    return out


def true_range_array(highs: Sequence[float], lows: Sequence[float],
                     closes: Sequence[float]) -> list[float | None]:
    """TR[i] for i >= 1 (needs previous close); TR[0] is None."""
    n = len(closes)
    out: list[float | None] = [None] * n
    for i in range(1, n):
        pc = closes[i - 1]
        out[i] = max(highs[i] - lows[i], abs(highs[i] - pc), abs(lows[i] - pc))
    return out


def wilder_atr_array(highs: Sequence[float], lows: Sequence[float],
                     closes: Sequence[float], period: int = ATR_PERIOD) -> list[float | None]:
    """Wilder ATR: seed at index ``period`` = mean(TR[1..period]),
    then ATR[i] = (ATR[i-1]*(period-1) + TR[i]) / period."""
    n = len(closes)
    out: list[float | None] = [None] * n
    if n <= period:
        return out
    tr = true_range_array(highs, lows, closes)
    value = sum(tr[1:period + 1]) / period  # type: ignore[arg-type]
    out[period] = value
    for i in range(period + 1, n):
        value = (value * (period - 1) + tr[i]) / period  # type: ignore[operator]
        out[i] = value
    return out


def _rsi_from_avgs(avg_gain: float, avg_loss: float) -> float | None:
    if avg_loss == 0.0:
        return 100.0 if avg_gain > 0.0 else None  # flat window: undefined
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def wilder_rsi_array(closes: Sequence[float], period: int = RSI_PERIOD) -> list[float | None]:
    """Wilder RSI: seed at index ``period`` from the mean of the first
    ``period`` gains/losses, then Wilder smoothing. Flat (no gain, no loss)
    -> None."""
    n = len(closes)
    out: list[float | None] = [None] * n
    if n <= period:
        return out
    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        if d > 0:
            gains += d
        else:
            losses -= d
    avg_g = gains / period
    avg_l = losses / period
    out[period] = _rsi_from_avgs(avg_g, avg_l)
    for i in range(period + 1, n):
        d = closes[i] - closes[i - 1]
        g = d if d > 0 else 0.0
        lo = -d if d < 0 else 0.0
        avg_g = (avg_g * (period - 1) + g) / period
        avg_l = (avg_l * (period - 1) + lo) / period
        out[i] = _rsi_from_avgs(avg_g, avg_l)
    return out


def log_return_array(closes: Sequence[float]) -> list[float | None]:
    out: list[float | None] = [None] * len(closes)
    for i in range(1, len(closes)):
        out[i] = math.log(closes[i] / closes[i - 1])
    return out


# --------------------------------------------------------------------------- #
# Causal bar series
# --------------------------------------------------------------------------- #

class CausalBarSeries:
    """Immutable, single-timeframe, time-ordered bar store with causal access."""

    __slots__ = ("timeframe", "_bars", "_close_ts", "_cache")

    def __init__(self, bars: Iterable[Bar]):
        items = tuple(bars)
        if not items:
            raise ValueError("CausalBarSeries requires at least one bar")
        tf = items[0].timeframe
        prev: Bar | None = None
        for bar in items:
            if bar.timeframe != tf:
                raise ValueError(f"mixed timeframes: {bar.timeframe!r} != {tf!r}")
            if bar.close_timestamp <= bar.timestamp:
                raise ValueError("close_timestamp must be > timestamp")
            if prev is not None:
                if bar.timestamp == prev.timestamp:
                    raise ValueError(f"duplicate timestamp {bar.timestamp}")
                if bar.timestamp < prev.timestamp or bar.close_timestamp <= prev.close_timestamp:
                    raise ValueError(f"non-monotonic timestamp {bar.timestamp}")
            prev = bar
        self.timeframe = tf
        self._bars = items
        self._close_ts = tuple(b.close_timestamp for b in items)
        self._cache: dict[str, list] | None = None

    def __len__(self) -> int:
        return len(self._bars)

    def closed_upto(self, as_of_ms: int) -> int:
        """Number of bars with ``close_timestamp <= as_of_ms``."""
        return bisect_right(self._close_ts, as_of_ms)

    def _last_index(self, as_of_ms: int) -> int:
        """Index of the last closed bar as of ``as_of_ms`` (-1 if none).

        Single choke point for every read: asserts causality."""
        idx = self.closed_upto(as_of_ms) - 1
        if idx >= 0 and self._close_ts[idx] > as_of_ms:
            raise LookAheadError(f"{self.timeframe} bar {idx} closes after as_of {as_of_ms}")
        if idx + 1 < len(self._close_ts) and self._close_ts[idx + 1] <= as_of_ms:
            raise LookAheadError(f"{self.timeframe} closed_upto inconsistent at {as_of_ms}")
        return idx

    def window(self, as_of_ms: int, n: int) -> list[Bar] | None:
        """The last ``n`` closed bars as of ``as_of_ms``; None if fewer exist."""
        if n <= 0:
            raise ValueError("n must be positive")
        idx = self._last_index(as_of_ms)
        if idx + 1 < n:
            return None
        out = list(self._bars[idx + 1 - n: idx + 1])
        if out[-1].close_timestamp > as_of_ms:
            raise LookAheadError(f"{self.timeframe} window leaks a forming/future bar")
        return out

    @property
    def indicator_cache(self) -> dict[str, list]:
        """Lazily built per-index indicator arrays (causal by construction)."""
        if self._cache is None:
            bars = self._bars
            closes = [b.close for b in bars]
            highs = [b.high for b in bars]
            lows = [b.low for b in bars]
            cache: dict[str, list] = {
                "close": closes,
                "high": highs,
                "low": lows,
                "log_ret": log_return_array(closes),
                "atr14": wilder_atr_array(highs, lows, closes, ATR_PERIOD),
                "rsi14": wilder_rsi_array(closes, RSI_PERIOD),
            }
            for p in EMA_PERIODS:
                cache[f"ema{p}"] = ema_array(closes, p)
            self._cache = cache
        return self._cache

    def value_at(self, name: str, as_of_ms: int, lag: int = 0):
        """Indicator ``name`` at the last closed bar minus ``lag`` (None if absent)."""
        idx = self._last_index(as_of_ms) - lag
        if idx < 0:
            return None
        return self.indicator_cache[name][idx]

    def bar_at(self, as_of_ms: int) -> Bar | None:
        idx = self._last_index(as_of_ms)
        return self._bars[idx] if idx >= 0 else None


# --------------------------------------------------------------------------- #
# Feature vector
# --------------------------------------------------------------------------- #

def _ratio(num: float | None, den: float | None) -> float | None:
    if num is None or den is None or den == 0.0:
        return None
    return num / den


def _sample_std(values: Sequence[float]) -> float:
    n = len(values)
    mean = sum(values) / n
    return math.sqrt(sum((v - mean) ** 2 for v in values) / (n - 1))


def _returns_window(rets: list, idx: int, n: int) -> list[float] | None:
    """Last ``n`` log returns ending at bar ``idx`` (return i uses bars i-1, i)."""
    start = idx - n + 1
    if start < 1:
        return None
    return rets[start: idx + 1]


def _variance_ratio(rets: Sequence[float], q: int) -> float | None:
    """Lo-MacKinlay VR(q) with overlapping q-sums: var(q-sum)/(q*var(1))."""
    n = len(rets)
    if n < q + 1:
        return None
    mu = sum(rets) / n
    var1 = sum((r - mu) ** 2 for r in rets) / (n - 1)
    if var1 == 0.0:
        return None
    sums = []
    running = sum(rets[:q])
    sums.append(running)
    for t in range(q, n):
        running += rets[t] - rets[t - q]
        sums.append(running)
    m = len(sums)
    if m < 2:
        return None
    varq = sum((s - q * mu) ** 2 for s in sums) / (m - 1)
    return varq / (q * var1)


def _require_tf(series: CausalBarSeries, tf: str) -> None:
    if not isinstance(series, CausalBarSeries):
        raise TypeError(f"{tf} input must be a CausalBarSeries")
    if series.timeframe != tf:
        raise ValueError(f"expected {tf} series, got {series.timeframe!r}")


def build_features(h1: CausalBarSeries, h4: CausalBarSeries, m15: CausalBarSeries,
                   as_of_ms: int) -> dict:
    """Causal feature vector as of ``as_of_ms`` from CLOSED bars only."""
    if isinstance(as_of_ms, bool) or not isinstance(as_of_ms, int):
        raise TypeError("as_of_ms must be integer epoch ms")
    _require_tf(h1, "H1")
    _require_tf(h4, "H4")
    _require_tf(m15, "M15")

    f: dict[str, float | None] = {name: None for name in FEATURE_NAMES}

    # ---------------- H1 ----------------
    j = h1._last_index(as_of_ms)
    atr: float | None = None
    if j >= 0:
        c1 = h1.indicator_cache
        closes = c1["close"]
        rets = c1["log_ret"]
        close = closes[j]
        atr = c1["atr14"][j]
        f["h1_atr14"] = atr
        f["h1_atr_pct"] = _ratio(atr, close)

        w24 = _returns_window(rets, j, RV_SHORT)
        w120 = _returns_window(rets, j, RV_LONG)
        rv24 = _sample_std(w24) if w24 is not None else None
        rv120 = _sample_std(w120) if w120 is not None else None
        f["h1_rv24"] = rv24
        f["h1_rv120"] = rv120
        f["h1_vol_ratio"] = _ratio(rv24, rv120)

        for k in TSMOM_HORIZONS:
            if j - k >= 0 and rv24 is not None and rv24 > 0.0:
                f[f"h1_tsmom_{k}"] = math.log(close / closes[j - k]) / (rv24 * math.sqrt(k))

        ema20 = c1["ema20"][j]
        ema50 = c1["ema50"][j]
        if ema20 is not None:
            f["h1_dist_ema20_atr"] = _ratio(close - ema20, atr)
        if ema50 is not None:
            f["h1_dist_ema50_atr"] = _ratio(close - ema50, atr)
            if j - EMA_SLOPE_BARS >= 0:
                prev50 = c1["ema50"][j - EMA_SLOPE_BARS]
                if prev50 is not None and atr:
                    f["h1_ema50_slope_atr"] = (ema50 - prev50) / (EMA_SLOPE_BARS * atr)

        if j - EFFICIENCY_BARS >= 0:
            path = 0.0
            for i in range(j - EFFICIENCY_BARS + 1, j + 1):
                path += abs(closes[i] - closes[i - 1])
            f["h1_efficiency_20"] = _ratio(abs(close - closes[j - EFFICIENCY_BARS]), path)

        wvr = _returns_window(rets, j, VR_WINDOW)
        if wvr is not None:
            f["h1_variance_ratio_4"] = _variance_ratio(wvr, VR_Q)

        if j + 1 >= RANGE_BARS:
            hi = max(c1["high"][j + 1 - RANGE_BARS: j + 1])
            lo = min(c1["low"][j + 1 - RANGE_BARS: j + 1])
            f["h1_range_pos_50"] = _ratio(close - lo, hi - lo)

        f["h1_rsi14"] = c1["rsi14"][j]

        bar = h1.bar_at(as_of_ms)
        assert bar is not None
        wall = datetime.fromtimestamp(bar.timestamp / 1000.0, tz=timezone.utc)
        f["server_hour"] = float(wall.hour)
        f["server_weekday"] = float(wall.weekday())
        if bar.avg_spread is not None:
            f["h1_spread_atr"] = _ratio(bar.avg_spread, atr)

    # ---------------- H4 ----------------
    k4 = h4._last_index(as_of_ms)
    if k4 >= 0:
        c4 = h4.indicator_cache
        e50 = c4["ema50"][k4]
        e200 = c4["ema200"][k4]
        if e50 is not None and e200 is not None:
            f["h4_trend_sign"] = 1.0 if e50 > e200 else (-1.0 if e50 < e200 else 0.0)
        if e50 is not None:
            f["h4_dist_ema50_atr"] = _ratio(c4["close"][k4] - e50, c4["atr14"][k4])

    # ---------------- M15 ----------------
    m = m15._last_index(as_of_ms)
    if m >= 0:
        c15 = m15.indicator_cache
        f["m15_rsi14"] = c15["rsi14"][m]
        atr_pct = f["h1_atr_pct"]
        if m - M15_RET_BARS >= 0 and atr_pct:
            ret = math.log(c15["close"][m] / c15["close"][m - M15_RET_BARS])
            f["m15_ret4_atr"] = ret / atr_pct

    return {
        "feature_version": FEATURE_VERSION,
        "as_of_ms": as_of_ms,
        "observer_only": True,
        "score_effect": 0,
        "features": f,
    }


__all__ = [
    "Bar",
    "CausalBarSeries",
    "FEATURE_NAMES",
    "FEATURE_VERSION",
    "LookAheadError",
    "build_features",
    "load_bars",
    "ema_array",
    "wilder_atr_array",
    "wilder_rsi_array",
]
