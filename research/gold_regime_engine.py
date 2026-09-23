"""Offline, causal XAUUSD regime diagnostics. Never imported by the live EA.

All times are MT5 server-wall encoded milliseconds, not verified UTC. M5 comes
from closed M1; a *later available M5* confirms each persisted H1/H4 bar.
Outcomes, session labels, D1, DXY and yields are deliberately absent here.
"""

from __future__ import annotations

import argparse
from bisect import bisect_left, bisect_right
from collections import deque
from dataclasses import dataclass
from hashlib import sha256
import json
import math
from pathlib import Path
import statistics
from typing import Iterable, Mapping, Sequence

from research.multi_strategy_causal_bars import LookAheadError


VERSION = "gold-regime-v1"
PARAMETER_VERSION = "gold-regime-v1-train60-online-prefix"
SOURCE = "HISTORICAL_MT5_TICKS"
TIMEFRAME_MS = {"M5": 300_000, "H1": 3_600_000, "H4": 14_400_000}
MIN_CALIBRATION = 30
TRAIN_FRACTION = 0.60
VALIDATION_FRACTION = 0.20


def _finite(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _percentile(values: Sequence[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    low = int(position)
    return ordered[low] + (ordered[min(low + 1, len(ordered) - 1)] - ordered[low]) * (position - low)


@dataclass(frozen=True)
class CausalFrame:
    timeframe: str
    start: int
    end: int
    available_at: int
    open: float
    high: float
    low: float
    close: float
    tick_count: int
    avg_spread: float
    provenance: Mapping[str, object]
    bar_id: str
    closed: bool = True

    def require(self, decision_time: int) -> None:
        if (not self.closed or self.available_at < self.end or
                self.end > decision_time or self.available_at > decision_time):
            raise LookAheadError(f"open or unavailable {self.timeframe} bar")


def _validate_bar(row: Mapping[str, object], timeframe: str) -> None:
    if (row.get("timeframe") != timeframe or row.get("source") != SOURCE or
            row.get("symbol") != "XAUUSD" or row.get("timestamp_unit") != "epoch_ms_mt5_server_wall_time"):
        raise ValueError(f"unexpected {timeframe} bar identity")
    start = row.get("start", row.get("timestamp"))
    end = row.get("end", row.get("close_timestamp"))
    if (not isinstance(start, int) or not isinstance(end, int) or
            end != start + TIMEFRAME_MS[timeframe] or start % TIMEFRAME_MS[timeframe]):
        raise ValueError(f"invalid {timeframe} bar boundaries")
    if not all(_finite(row.get(key)) for key in ("open", "high", "low", "close", "avg_spread")):
        raise ValueError(f"invalid {timeframe} OHLC/spread")
    if (row["high"] < max(row["open"], row["close"], row["low"]) or
            row["low"] > min(row["open"], row["close"], row["high"]) or
            row["avg_spread"] < 0 or not isinstance(row.get("tick_count"), int) or row["tick_count"] < 1):
        raise ValueError(f"inconsistent {timeframe} OHLC/activity")


def causal_m5(row: Mapping[str, object]) -> CausalFrame:
    _validate_bar(row, "M5")
    start, end, available = int(row["start"]), int(row["end"]), row.get("available_at")
    if (row.get("closed", True) is not True or not isinstance(available, int) or
            available < end or not isinstance(row.get("provenance"), dict) or
            row["provenance"].get("aggregation") != "CLOSED_M1_ONLY"):
        raise LookAheadError("M5 lacks closed-M1 availability evidence")
    return CausalFrame("M5", start, end, available, float(row["open"]), float(row["high"]),
                       float(row["low"]), float(row["close"]), int(row["tick_count"]),
                       float(row["avg_spread"]), row["provenance"], str(row["bar_id"]))


def causal_higher_bar(row: Mapping[str, object], confirming_m5: CausalFrame, timeframe: str) -> CausalFrame:
    """A later *closed and available* M5 proves H1/H4 are no longer open."""
    if timeframe not in {"H1", "H4"}:
        raise ValueError("only H1/H4 require this wrapper")
    _validate_bar(row, timeframe)
    end = int(row["close_timestamp"])
    if confirming_m5.timeframe != "M5" or confirming_m5.start < end:
        raise LookAheadError("H1/H4 needs a later M5 bucket")
    confirming_m5.require(confirming_m5.available_at)
    if row.get("closed", True) is not True:
        raise LookAheadError("higher-timeframe bar is open")
    return CausalFrame(timeframe, int(row["timestamp"]), end,
                       max(end, confirming_m5.available_at), float(row["open"]),
                       float(row["high"]), float(row["low"]), float(row["close"]),
                       int(row["tick_count"]), float(row["avg_spread"]),
                       {"source_bar_id": row["bar_id"], "confirmation_bar_id": confirming_m5.bar_id,
                        "basis": "NEXT_AVAILABLE_CLOSED_M5", "source_timeframe": timeframe},
                       str(row["bar_id"]))


def wrap_higher_bars(rows: Iterable[Mapping[str, object]], m5: Sequence[CausalFrame],
                     timeframe: str) -> list[CausalFrame]:
    starts = [bar.start for bar in m5]
    result: list[CausalFrame] = []
    previous = -1
    for row in rows:
        _validate_bar(row, timeframe)
        start = int(row["timestamp"])
        if start <= previous:
            raise ValueError(f"{timeframe} source bars must be strictly ordered")
        previous = start
        index = bisect_left(starts, int(row["close_timestamp"]))
        if index < len(m5):
            result.append(causal_higher_bar(row, m5[index], timeframe))
    return result


def asof(frames: Sequence[CausalFrame], decision_time: int) -> tuple[int, CausalFrame | None]:
    availability = [bar.available_at for bar in frames]
    index = bisect_right(availability, decision_time) - 1
    if index < 0:
        return -1, None
    frames[index].require(decision_time)
    return index, frames[index]


def split_for_index(index: int, count: int) -> str:
    if count < 1:
        raise ValueError("no decisions")
    if index < int(count * TRAIN_FRACTION):
        return "TRAIN"
    if index < int(count * (TRAIN_FRACTION + VALIDATION_FRACTION)):
        return "VALIDATION"
    return "OOS"


def _ema(previous: float | None, value: float, period: int) -> float:
    return value if previous is None else previous + 2 / (period + 1) * (value - previous)


def features_for_frames(frames: Sequence[CausalFrame]) -> list[dict]:
    """Streaming causal features; confirmed pivots require two right-side closed bars."""
    output: list[dict] = []
    bars: deque[CausalFrame] = deque(maxlen=210)
    trs: deque[float] = deque(maxlen=30)
    atr_history: deque[float] = deque(maxlen=101)
    tick_history: deque[int] = deque(maxlen=101)
    spread_history: deque[float] = deque(maxlen=101)
    ranges: deque[float] = deque(maxlen=101)
    ema20 = ema50 = ema200 = None
    ema20_history: deque[float] = deque(maxlen=6)
    ema50_history: deque[float] = deque(maxlen=6)
    ema200_history: deque[float] = deque(maxlen=6)
    alternating: list[tuple[str, int, float]] = []
    for index, bar in enumerate(frames):
        bar.require(bar.available_at)
        previous_close = bars[-1].close if bars else bar.close
        tr = max(bar.high - bar.low, abs(bar.high - previous_close), abs(bar.low - previous_close))
        trs.append(tr)
        bars.append(bar)
        ema20, ema50, ema200 = (_ema(ema20, bar.close, 20), _ema(ema50, bar.close, 50),
                                _ema(ema200, bar.close, 200))
        ema20_history.append(ema20)
        ema50_history.append(ema50)
        ema200_history.append(ema200)
        atr = sum(list(trs)[-14:]) / 14 if len(trs) >= 14 else None
        prior_atrs = list(atr_history)
        prior_ticks = list(tick_history)
        prior_spreads = list(spread_history)
        prior_ranges = list(ranges)
        candle_range = bar.high - bar.low
        closes = [item.close for item in bars]
        tail = closes[-11:]
        path = sum(abs(b - a) for a, b in zip(tail, tail[1:]))
        efficiency = abs(tail[-1] - tail[0]) / path if len(tail) == 11 and path > 0 else None
        realized = (statistics.pstdev([math.log(b / a) for a, b in zip(tail, tail[1:])])
                    if len(tail) == 11 and all(a > 0 and b > 0 for a, b in zip(tail, tail[1:])) else None)
        ema_slope = ((ema20_history[-1] - ema20_history[0]) / atr
                     if atr and len(ema20_history) == 6 else None)
        ema50_slope = ((ema50_history[-1] - ema50_history[0]) / atr
                       if atr and index >= 49 and len(ema50_history) == 6 else None)
        ema200_slope = ((ema200_history[-1] - ema200_history[0]) / atr
                        if atr and index >= 199 and len(ema200_history) == 6 else None)
        # A pivot at index-2 is confirmed only now, using five closed bars.
        if len(bars) >= 5 and atr:
            five = list(bars)[-5:]
            center = five[2]
            is_high = center.high > max(five[i].high for i in (0, 1, 3, 4))
            is_low = center.low < min(five[i].low for i in (0, 1, 3, 4))
            if is_high != is_low:
                kind, price = ("HIGH", center.high) if is_high else ("LOW", center.low)
                candidate = (kind, index - 2, price)
                if alternating and alternating[-1][0] == kind:
                    old_price = alternating[-1][2]
                    if (kind == "HIGH" and price > old_price or
                            kind == "LOW" and price < old_price):
                        alternating[-1] = candidate
                else:
                    alternating.append(candidate)
                    if len(alternating) > 12:
                        alternating.pop(0)
        structure = "UNKNOWN"
        last_highs = [pivot for pivot in alternating if pivot[0] == "HIGH"][-2:]
        last_lows = [pivot for pivot in alternating if pivot[0] == "LOW"][-2:]
        hh = hl = lh = ll = None
        if len(last_highs) == 2 and len(last_lows) == 2 and atr:
            high_delta = last_highs[-1][2] - last_highs[0][2]
            low_delta = last_lows[-1][2] - last_lows[0][2]
            tolerance = 0.1 * atr
            hh, lh = high_delta > tolerance, high_delta < -tolerance
            hl, ll = low_delta > tolerance, low_delta < -tolerance
            if high_delta > tolerance and low_delta > tolerance:
                structure = "BULLISH"
            elif high_delta < -tolerance and low_delta < -tolerance:
                structure = "BEARISH"
            elif abs(high_delta) <= tolerance and abs(low_delta) <= tolerance:
                structure = "RANGE"
            else:
                structure = "MIXED"
        breakout = "NONE"
        if len(bars) >= 11:
            previous = list(bars)[-11:-1]
            if bar.close > max(item.high for item in previous):
                breakout = "LONG"
            elif bar.close < min(item.low for item in previous):
                breakout = "SHORT"
        if len(bars) >= 15:
            previous = list(bars)[-15:]
            plus = minus = tr14 = 0.0
            for prior, current in zip(previous, previous[1:]):
                up, down = current.high - prior.high, prior.low - current.low
                plus += up if up > down and up > 0 else 0
                minus += down if down > up and down > 0 else 0
                tr14 += max(current.high - current.low, abs(current.high - prior.close),
                            abs(current.low - prior.close))
            plus_di, minus_di = (100 * plus / tr14, 100 * minus / tr14) if tr14 > 0 else (None, None)
            adx = 100 * abs(plus - minus) / (plus + minus) if plus + minus > 0 else 0.0
        else:
            plus_di = minus_di = adx = None
        range_width = max(item.high for item in list(bars)[-30:]) - min(item.low for item in list(bars)[-30:]) if len(bars) >= 30 else None
        row = {
            "available_at": bar.available_at, "bar_id": bar.bar_id, "atr": atr,
            "atr_percentile": sum(value <= atr for value in prior_atrs) / len(prior_atrs) if atr and len(prior_atrs) >= 30 else None,
            "realized_volatility": realized,
            "range_percentile": sum(value <= candle_range for value in prior_ranges) / len(prior_ranges) if len(prior_ranges) >= 30 else None,
            "compression_ratio": candle_range / (sum(prior_ranges[-20:]) / len(prior_ranges[-20:])) if len(prior_ranges) >= 20 and sum(prior_ranges[-20:]) > 0 else None,
            "ema20": ema20 if index >= 19 else None, "ema50": ema50 if index >= 49 else None,
            "ema200": ema200 if index >= 199 else None, "ema20_slope_atr": ema_slope,
            "ema50_slope_atr": ema50_slope,
            "ema200_slope_atr": ema200_slope,
            "ema20_ema200_distance_atr": (ema20 - ema200) / atr if index >= 199 and atr else None,
            "adx": adx, "plus_di": plus_di, "minus_di": minus_di,
            "directional_efficiency": efficiency, "structure": structure,
            "hh": hh, "hl": hl, "lh": lh, "ll": ll,
            "last_confirmed_high": last_highs[-1][2] if last_highs else None,
            "last_confirmed_low": last_lows[-1][2] if last_lows else None,
            "breakout": breakout, "range_width": range_width,
            "structural_compression": range_width / atr if range_width is not None and atr else None,
            "tick_count_percentile": sum(value <= bar.tick_count for value in prior_ticks) / len(prior_ticks) if len(prior_ticks) >= 30 else None,
            "spread_percentile": sum(value <= bar.avg_spread for value in prior_spreads) / len(prior_spreads) if len(prior_spreads) >= 30 else None,
            "spread_expansion": bar.avg_spread / (sum(prior_spreads[-20:]) / 20) if len(prior_spreads) >= 20 and sum(prior_spreads[-20:]) > 0 else None,
        }
        output.append(row)
        if atr is not None:
            atr_history.append(atr)
        tick_history.append(bar.tick_count)
        spread_history.append(bar.avg_spread)
        ranges.append(candle_range)
    return output


def fit_training_thresholds(features: Sequence[Mapping[str, object]], available_times: Sequence[int],
                            train_end: int, *, evaluation_time: int | None = None) -> dict:
    """Freeze only samples available inside TRAIN; refuse any OOS/VAL cutoff."""
    if evaluation_time is not None and evaluation_time < train_end:
        raise ValueError("cannot fit thresholds before TRAIN ends")
    if len(features) != len(available_times):
        raise ValueError("feature/availability mismatch")
    usable = [feature for feature, time in zip(features, available_times) if time <= train_end]
    atrs = [float(row["atr"]) for row in usable if _finite(row.get("atr")) and row["atr"] > 0]
    slopes = [abs(float(row["ema20_slope_atr"])) for row in usable if _finite(row.get("ema20_slope_atr"))]
    if len(atrs) < MIN_CALIBRATION or len(slopes) < MIN_CALIBRATION:
        raise ValueError("insufficient causal TRAIN calibration samples")
    return {"atr_low": _percentile(atrs, .20), "atr_high": _percentile(atrs, .80),
            "slope_entry": max(.05, _percentile(slopes, .60)),
            "slope_exit": max(.025, _percentile(slopes, .35)),
            "sample_count": len(usable), "trained_through": train_end,
            "parameter_version": PARAMETER_VERSION}


def classify(feature: Mapping[str, object], params: Mapping[str, object] | None,
             previous_trend: str = "UNKNOWN") -> dict:
    if params is None or not _finite(feature.get("atr")) or not _finite(feature.get("ema20_slope_atr")):
        return {"trend_state": "UNKNOWN", "volatility_state": "UNKNOWN",
                "structure_state": "UNKNOWN", "transition_state": "UNKNOWN",
                "gold_regime": "UNKNOWN", "confidence": 0.0}
    slope = float(feature["ema20_slope_atr"])
    limit = float(params["slope_exit"] if previous_trend in {"UP", "DOWN"} else params["slope_entry"])
    trend = "UP" if slope >= limit else "DOWN" if slope <= -limit else "FLAT"
    atr = float(feature["atr"])
    volatility = "EXPANSION" if atr >= params["atr_high"] else "CONTRACTION" if atr <= params["atr_low"] else "NORMAL"
    structure = feature.get("structure", "UNKNOWN")
    if structure not in {"BULLISH", "BEARISH", "RANGE", "MIXED"}:
        structure = "UNKNOWN"
    shock = (feature.get("atr_percentile") is not None and feature["atr_percentile"] >= .98 and
             (feature.get("range_percentile") is not None and feature["range_percentile"] >= .98 or
              feature.get("spread_percentile") is not None and feature["spread_percentile"] >= .98))
    transition = ("SHOCK" if shock else "TRANSITION" if previous_trend in {"UP", "DOWN", "FLAT"} and
                  trend != previous_trend else "STABLE")
    if shock:
        regime = "EVENT_SHOCK"
    elif transition == "TRANSITION":
        regime = "TRANSITION"
    elif trend == "UP" and structure != "BEARISH":
        regime = "TREND_UP"
    elif trend == "DOWN" and structure != "BULLISH":
        regime = "TREND_DOWN"
    elif structure == "RANGE" or trend == "FLAT":
        regime = "RANGE"
    elif volatility == "EXPANSION":
        regime = "VOL_EXPANSION"
    elif volatility == "CONTRACTION":
        regime = "VOL_CONTRACTION"
    else:
        regime = "UNKNOWN"
    confidence = min(1.0, max(0.0, abs(slope) / max(float(params["slope_entry"]) * 2, .01)))
    if regime == "EVENT_SHOCK":
        confidence = 1.0  # contemporaneous extremes, not a future outcome
    elif regime == "TRANSITION":
        confidence = max(.75, confidence)  # observed current-vs-previous closed-state change
    elif regime in {"RANGE", "UNKNOWN"}:
        confidence = .5 if regime == "RANGE" else 0.0
    return {"trend_state": trend, "volatility_state": volatility,
            "structure_state": structure, "transition_state": transition,
            "gold_regime": regime, "confidence": round(confidence, 4)}


class RegimeHysteresis:
    def __init__(self, minimum_bars: int = 2, entry_confidence: float = .55,
                 exit_confidence: float = .45) -> None:
        if minimum_bars < 1 or not 0 <= exit_confidence <= entry_confidence <= 1:
            raise ValueError("invalid hysteresis settings")
        self.minimum_bars = minimum_bars
        self.entry_confidence = entry_confidence
        self.exit_confidence = exit_confidence
        self.state = "UNKNOWN"
        self.candidate = None
        self.candidate_bars = 0
        self.age_bars = 0
        self.last_h1_index = -1

    def update(self, candidate: str, confidence: float, h1_index: int) -> tuple[str, str | None, str | None, int]:
        if h1_index == self.last_h1_index:
            return self.state, None, None, self.age_bars
        if h1_index < self.last_h1_index:
            raise ValueError("hysteresis requires chronological H1 bars")
        self.last_h1_index = h1_index
        self.age_bars += 1
        if candidate == self.state or candidate == "UNKNOWN":
            self.candidate, self.candidate_bars = None, 0
            return self.state, None, None, self.age_bars
        if confidence < (self.exit_confidence if self.state != "UNKNOWN" else self.entry_confidence):
            self.candidate, self.candidate_bars = None, 0
            return self.state, None, None, self.age_bars
        if candidate != self.candidate:
            self.candidate, self.candidate_bars = candidate, 1
        else:
            self.candidate_bars += 1
        if candidate not in {"EVENT_SHOCK", "TRANSITION"} and self.candidate_bars < self.minimum_bars:
            return self.state, None, None, self.age_bars
        old, self.state = self.state, candidate
        self.candidate, self.candidate_bars, self.age_bars = None, 0, 1
        return self.state, old, self.state, self.age_bars


def alignment(m5: Mapping[str, object], h1: Mapping[str, object], h4: Mapping[str, object]) -> tuple[str, str]:
    a, b, c = (item.get("trend_state") for item in (m5, h1, h4))
    if b == c == "UP":
        return ("ALIGNED_UP", "higher_tf_up_with_lower_tf_pullback" if a == "DOWN" else "higher_tf_up")
    if b == c == "DOWN":
        return ("ALIGNED_DOWN", "higher_tf_down_with_lower_tf_pullback" if a == "UP" else "higher_tf_down")
    if "UNKNOWN" in (b, c) or b is None or c is None:
        return "INSUFFICIENT", "insufficient_higher_tf"
    if {b, c} == {"UP", "DOWN"}:
        return "CONFLICT", "higher_tf_conflict"
    return "NEUTRAL", "higher_tf_neutral"


def _read_jsonl(path: Path) -> Iterable[dict]:
    with path.open(encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            if line.strip():
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError(f"non-object {path}:{number}")
                yield value


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _load_m5(path: Path) -> list[CausalFrame]:
    frames = [causal_m5(row) for row in _read_jsonl(path)]
    if any(next_bar.start <= bar.start or next_bar.available_at < bar.available_at
           for bar, next_bar in zip(frames, frames[1:])):
        raise ValueError("M5 must be chronological by start and availability")
    return frames


def _frame_at(frames: Sequence[CausalFrame], features: Sequence[dict], time: int) -> tuple[int, dict]:
    index = bisect_right([bar.available_at for bar in frames], time) - 1
    if index < 0:
        return -1, {}
    frames[index].require(time)
    return index, features[index]


def generate(root: Path, output: Path) -> dict:
    """Materialize one diagnostic row per persisted decision, never reading outcomes."""
    root, output = Path(root), Path(output)
    manifest = json.loads((root.parent / "data_quality_manifest.json").read_text(encoding="utf-8"))
    if manifest.get("session_attribution_allowed") is not False:
        raise ValueError("this version expects broker clock UNKNOWN and forbids session attribution")
    index = json.loads((root / "causal_bars" / "causal_bars_index.json").read_text(encoding="utf-8"))
    if index.get("source_manifest_id") != manifest["dataset_id"]:
        raise ValueError("M5 index and frozen manifest disagree")
    m5_path = root / "causal_bars" / "XAUUSD_M5.jsonl"
    metadata = index.get("outputs", {}).get("M5", {})
    if (metadata.get("size_bytes") != m5_path.stat().st_size or
            metadata.get("sha256") != _file_sha256(m5_path)):
        raise ValueError("M5 content differs from phase-2 materialization index")
    m5 = _load_m5(m5_path)
    h1 = wrap_higher_bars(_read_jsonl(root / "historical_bars" / "XAUUSD_H1.jsonl"), m5, "H1")
    h4 = wrap_higher_bars(_read_jsonl(root / "historical_bars" / "XAUUSD_H4.jsonl"), m5, "H4")
    frames = {"m5": m5, "h1": h1, "h4": h4}
    features = {name: features_for_frames(rows) for name, rows in frames.items()}
    decisions = list(_read_jsonl(root / "historical_decisions.jsonl"))
    decisions.sort(key=lambda row: (row.get("observed_at", -1), row.get("event_id", "")))
    if not decisions or any(row.get("observer_only") is not True or row.get("score_effect") != 0 or
                            row.get("source") != SOURCE or
                            not isinstance(row.get("observed_at"), int) or not row.get("event_id")
                            for row in decisions):
        raise ValueError("invalid diagnostic decision input")
    train_end = int(decisions[int(len(decisions) * TRAIN_FRACTION) - 1]["observed_at"])
    frozen = {name: fit_training_thresholds(values, [bar.available_at for bar in frames[name]], train_end)
              for name, values in features.items()}
    # TRAIN labels use an expanding causal prefix; VAL/OOS use TRAIN-frozen parameters.
    availability = {name: [bar.available_at for bar in rows] for name, rows in frames.items()}
    previous_trend = {name: "UNKNOWN" for name in frames}
    last_index = {name: -1 for name in frames}
    states: dict[str, dict] = {name: classify({}, None) for name in frames}
    hysteresis = RegimeHysteresis()
    seen: set[str] = set()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    counts: dict[str, int] = {}
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            for ordinal, decision in enumerate(decisions):
                event_id, time = str(decision["event_id"]), int(decision["observed_at"])
                if event_id in seen:
                    raise ValueError(f"duplicate decision event_id={event_id}")
                seen.add(event_id)
                split = split_for_index(ordinal, len(decisions))
                selected: dict[str, tuple[int, dict]] = {}
                for name, bars in frames.items():
                    at = bisect_right(availability[name], time) - 1
                    if at >= 0:
                        bars[at].require(time)
                    if at != last_index[name]:
                        for cursor in range(last_index[name] + 1, at + 1):
                            feat = features[name][cursor]
                            if split == "TRAIN":
                                # Prefix excludes current bar: a past-only threshold profile.
                                # Bounded causal calibration keeps the long M5
                                # stream linear without looking past this bar.
                                lower = max(0, cursor - 256)
                                prefix = features[name][lower:cursor]
                                past_times = availability[name][lower:cursor]
                                try:
                                    params = fit_training_thresholds(prefix, past_times, availability[name][cursor - 1]) if cursor else None
                                except ValueError:
                                    params = None
                            else:
                                params = frozen[name]
                            states[name] = classify(feat, params, previous_trend[name])
                            previous_trend[name] = states[name]["trend_state"]
                        last_index[name] = at
                    selected[name] = (at, states[name] if at >= 0 else classify({}, None))
                align, context = alignment(selected["m5"][1], selected["h1"][1], selected["h4"][1])
                h1_idx = selected["h1"][0]
                raw = selected["h1"][1]
                gold, transitioned_from, transitioned_to, age = hysteresis.update(
                    raw["gold_regime"], raw["confidence"], h1_idx) if h1_idx >= 0 else ("UNKNOWN", None, None, 0)
                row = {"event_id": event_id, "decision_time": time, "source": SOURCE,
                       "parameter_version": PARAMETER_VERSION, "split": split,
                       "diagnostic_only": True, "score_effect": 0,
                       "m5": {}, "h1": {}, "h4": {}, "alignment": align,
                       "alignment_context": context, "gold_regime": gold,
                       "regime_confidence": raw["confidence"], "regime_age_bars": age,
                       "transition_from": transitioned_from, "transition_to": transitioned_to,
                       "decision": decision.get("decision"), "armed_direction": decision.get("armed_direction"),
                       "long_score": decision.get("long_score"), "short_score": decision.get("short_score")}
                for name in frames:
                    at, state = selected[name]
                    if at >= 0:
                        bar = frames[name][at]
                        row[name] = {"bar_id": bar.bar_id, "start": bar.start, "end": bar.end,
                                     "available_at": bar.available_at, "provenance": bar.provenance,
                                     "features": features[name][at], "state": state}
                stream.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")
                counts[gold] = counts.get(gold, 0) + 1
        temporary.replace(output)
    finally:
        if temporary.exists():
            temporary.unlink()
    result = {"rows": len(seen), "train_end": train_end, "distribution": counts,
              "parameter_version": PARAMETER_VERSION, "frozen_train_thresholds": frozen,
              "source_manifest_id": manifest["dataset_id"], "output_sha256": _file_sha256(output)}
    metadata = output.with_suffix(".meta.json")
    metadata_temporary = metadata.with_suffix(".meta.json.tmp")
    try:
        metadata_temporary.write_text(json.dumps(result, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        metadata_temporary.replace(metadata)
    finally:
        if metadata_temporary.exists():
            metadata_temporary.unlink()
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-root", type=Path, default=Path("research/output"))
    parser.add_argument("--output", type=Path, default=Path("research/output/regimes/gold_regime_v1.jsonl"))
    args = parser.parse_args()
    result = generate(args.input_root, args.output)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
