"""Causal closed-bar decision enrichment for GoldScout historical observations.

The original append-only observation dataset is never rewritten. Enrichment is
stored in a sidecar keyed by event_id. The implementation mirrors the closed-bar
core of the EA's BuildSignal logic and explicitly declares components that cannot
be reconstructed from historical bars alone.
"""

from __future__ import annotations

import argparse
from bisect import bisect_right
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
from typing import Iterable, Sequence

from research.tick_historical_replay import Bar, DeduplicatingJsonlWriter, IndicatorState


SOURCE = "HISTORICAL_MT5_TICKS"
ENRICHMENT_VERSION = "goldscout-decision-replay-v1"
THRESHOLD_ARM = 58
THRESHOLD_TRADE = 74
PIVOT_LOOKBACK = 120
PIVOT_LEFT = 2
PIVOT_RIGHT = 2
PIVOT_MIN_BARS = 2
PIVOT_PROMINENCE_ATR = 0.20
PIVOT_TOLERANCE_ATR = 0.20
STRUCTURE_LOOKBACK = 20
VOLUME_LOOKBACK = 20
MIN_ADX = 20.0
M15_BREAKOUT_BUFFER_ATR = 0.05
M15_RECOVERY_PROXIMITY_ATR = 0.50
M15_MOMENTUM_BODY_ATR = 0.50
TIMEFRAME_SECONDS = {"M15": 900, "H1": 3600, "H4": 14400}
UNAVAILABLE_COMPONENTS = (
    "confirmed_pattern_bonus (W/M, flags/pennants, triangles/wedges, HCH)",
    "historical world-news context and scheduled macro calendar",
    "intrabar tick trigger/boost",
    "account, position, margin and broker execution guards",
)


class EnrichmentError(RuntimeError):
    """Raised when enrichment cannot proceed without inventing state."""


@dataclass(frozen=True)
class ReplayPivot:
    kind: str
    price: float
    index: int
    timestamp_ms: int
    atr: float
    shift: int


@dataclass
class ReplayStructure:
    state: str = "INSUFFICIENT"
    sufficient: bool = False
    hh: bool = False
    hl: bool = False
    lh: bool = False
    ll: bool = False
    alternating_pivots: int = 0
    last_high: float | None = None
    last_low: float | None = None


@dataclass
class StatePoint:
    bar: dict
    indicators: dict
    structure: ReplayStructure


class EmaState:
    def __init__(self, period: int):
        self.period = period
        self.seed: deque[float] = deque(maxlen=period)
        self.value: float | None = None

    def update(self, close: float) -> float | None:
        if self.value is None:
            self.seed.append(close)
            if len(self.seed) == self.period:
                self.value = sum(self.seed) / self.period
        else:
            self.value += 2.0 / (self.period + 1.0) * (close - self.value)
        return self.value


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _read_jsonl(path: Path) -> Iterable[dict]:
    try:
        stream = Path(path).open("r", encoding="utf-8-sig")
    except OSError as error:
        raise EnrichmentError(f"cannot open {path}: {error}") from error
    with stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as error:
                raise EnrichmentError(f"invalid JSON at {path}:{line_number}: {error}") from error
            if not isinstance(record, dict):
                raise EnrichmentError(f"non-object JSON at {path}:{line_number}")
            yield record


def _load_bars(input_dir: Path, symbol: str, timeframe: str) -> list[dict]:
    path = Path(input_dir) / "historical_bars" / f"{symbol}_{timeframe}.jsonl"
    bars: list[dict] = []
    previous_close_time = -1
    for record in _read_jsonl(path):
        if record.get("timeframe") != timeframe or record.get("symbol") != symbol:
            raise EnrichmentError(f"unexpected bar contract in {path}")
        required = ("timestamp", "close_timestamp", "open", "high", "low", "close", "tick_count")
        if not all(_is_number(record.get(field)) for field in required):
            raise EnrichmentError(f"invalid bar fields in {path}")
        close_time = int(record["close_timestamp"])
        if close_time <= previous_close_time:
            raise EnrichmentError(f"bars not strictly chronological in {path}")
        previous_close_time = close_time
        bars.append(dict(record))
    if not bars:
        raise EnrichmentError(f"no bars in {path}")
    return bars


def _to_bar(record: dict) -> Bar:
    return Bar(
        timeframe=str(record["timeframe"]),
        timestamp_ms=int(record["timestamp"]),
        close_timestamp_ms=int(record["close_timestamp"]),
        open=float(record["open"]),
        high=float(record["high"]),
        low=float(record["low"]),
        close=float(record["close"]),
        tick_count=int(record["tick_count"]),
        avg_spread=float(record.get("avg_spread", 0.0)),
        min_spread=float(record.get("min_spread", 0.0)),
        max_spread=float(record.get("max_spread", 0.0)),
        close_spread=float(record.get("avg_spread", 0.0)),
    )


def _detect_pivots(bars: Sequence[dict], atr_values: Sequence[float | None], point_size: float) -> list[ReplayPivot]:
    if point_size <= 0.0 or len(bars) != len(atr_values):
        return []
    offset = max(0, len(bars) - PIVOT_LOOKBACK)
    rates = bars[offset:]
    atrs = atr_values[offset:]
    if any(not _is_number(value) or float(value) <= 0.0 for value in atrs):
        return []
    pivots: list[ReplayPivot] = []
    last_accepted = -1
    count = len(rates)
    for index in range(PIVOT_LEFT, count - PIVOT_RIGHT):
        candidate = rates[index]
        neighbours = rates[index - PIVOT_LEFT : index] + rates[index + 1 : index + PIVOT_RIGHT + 1]
        is_high = all(float(candidate["high"]) > float(other["high"]) for other in neighbours)
        is_low = all(float(candidate["low"]) < float(other["low"]) for other in neighbours)
        neighbour_high = max(float(other["high"]) for other in neighbours)
        neighbour_low = min(float(other["low"]) for other in neighbours)
        required = PIVOT_PROMINENCE_ATR * float(atrs[index])
        if is_high and float(candidate["high"]) - neighbour_high + 1e-12 < required:
            is_high = False
        if is_low and neighbour_low - float(candidate["low"]) + 1e-12 < required:
            is_low = False
        if is_high == is_low:
            continue
        if last_accepted >= 0 and index - last_accepted < PIVOT_MIN_BARS:
            continue
        pivots.append(
            ReplayPivot(
                kind="HIGH" if is_high else "LOW",
                price=float(candidate["high"] if is_high else candidate["low"]),
                index=offset + index,
                timestamp_ms=int(candidate["timestamp"]),
                atr=float(atrs[index]),
                shift=count - index,
            )
        )
        last_accepted = index
    return pivots


def _normalize_pivots(pivots: Sequence[ReplayPivot]) -> list[ReplayPivot]:
    alternating: list[ReplayPivot] = []
    for pivot in pivots:
        if alternating and alternating[-1].kind == pivot.kind:
            more_extreme = pivot.price > alternating[-1].price if pivot.kind == "HIGH" else pivot.price < alternating[-1].price
            if more_extreme:
                alternating[-1] = pivot
        else:
            alternating.append(pivot)
    return alternating


def _classify_pivots(pivots: Sequence[ReplayPivot], point_size: float) -> ReplayStructure:
    alternating = _normalize_pivots(pivots)
    result = ReplayStructure(alternating_pivots=len(alternating))
    highs = [pivot for pivot in alternating if pivot.kind == "HIGH"]
    lows = [pivot for pivot in alternating if pivot.kind == "LOW"]
    result.last_high = highs[-1].price if highs else None
    result.last_low = lows[-1].price if lows else None
    if len(highs) < 2 or len(lows) < 2:
        return result
    previous_high, current_high = highs[-2], highs[-1]
    previous_low, current_low = lows[-2], lows[-1]
    high_tolerance = max(point_size, max(previous_high.atr, current_high.atr) * PIVOT_TOLERANCE_ATR)
    low_tolerance = max(point_size, max(previous_low.atr, current_low.atr) * PIVOT_TOLERANCE_ATR)
    result.sufficient = True
    result.hh = current_high.price > previous_high.price + high_tolerance
    result.lh = current_high.price < previous_high.price - high_tolerance
    result.hl = current_low.price > previous_low.price + low_tolerance
    result.ll = current_low.price < previous_low.price - low_tolerance
    if result.hh and result.hl:
        result.state = "BULLISH"
    elif result.lh and result.ll:
        result.state = "BEARISH"
    else:
        result.state = "NEUTRAL"
    return result


def build_state_points(bars: Sequence[dict], point_size: float) -> list[StatePoint]:
    indicators = IndicatorState(period=14)
    ema50 = EmaState(50)
    atr_values: list[float | None] = []
    points: list[StatePoint] = []
    for index, record in enumerate(bars):
        values = indicators.update(_to_bar(record))
        values["ema50"] = ema50.update(float(record["close"]))
        atr_values.append(values["atr"])
        window_start = max(0, index - PIVOT_LOOKBACK + 1)
        pivots = _detect_pivots(bars[window_start : index + 1], atr_values[window_start : index + 1], point_size)
        points.append(StatePoint(dict(record), values, _classify_pivots(pivots, point_size)))
    return points


def structural_bucket(state: str, direction: int, pullback: bool, breakout: bool, momentum: bool) -> int:
    aligned = (direction > 0 and state == "BULLISH") or (direction < 0 and state == "BEARISH")
    points = 0
    if aligned:
        points = 15
        if pullback:
            points += 5
        if breakout:
            points += 10
        elif momentum:
            points += 5
    return min(25, points)


def m15_timing_adjustment(structure: str, direction: int, breakout: int, recovery: int, momentum: int) -> int:
    if structure not in {"BULLISH", "BEARISH"} or direction not in {-1, 1}:
        return 0
    aligned_structure = (direction > 0 and structure == "BULLISH") or (direction < 0 and structure == "BEARISH")
    opposing_structure = (direction > 0 and structure == "BEARISH") or (direction < 0 and structure == "BULLISH")
    aligned_event = breakout == direction or recovery == direction
    opposing_event = breakout == -direction or recovery == -direction
    aligned_momentum = momentum == direction
    opposing_momentum = momentum == -direction
    if opposing_structure:
        return -4 if opposing_event or opposing_momentum else -3
    if opposing_event:
        return -3 if opposing_momentum else -2
    return max(-4, min(4, 1 + (2 if aligned_event else 0) + (1 if aligned_momentum else 0))) if aligned_structure else 0


def _gated_adjustment(technical_score: int, requested: int) -> int:
    bounded = max(-4, min(4, requested))
    return 0 if bounded > 0 and technical_score < THRESHOLD_ARM else bounded


def _m15_evidence(points: Sequence[StatePoint], index: int, point_size: float) -> dict:
    if index < 1:
        return {"available": False, "structure": "INSUFFICIENT", "long_adjustment": 0, "short_adjustment": 0}
    current, previous = points[index], points[index - 1]
    atr = current.indicators.get("atr")
    structure = current.structure
    if not _is_number(atr) or float(atr) <= 0.0 or not structure.sufficient:
        return {"available": False, "structure": structure.state, "long_adjustment": 0, "short_adjustment": 0}
    atr = float(atr)
    breakout = 0
    buffer = max(point_size, M15_BREAKOUT_BUFFER_ATR * atr)
    if structure.last_high is not None and float(previous.bar["close"]) <= structure.last_high + buffer and float(current.bar["close"]) > structure.last_high + buffer:
        breakout = 1
    elif structure.last_low is not None and float(previous.bar["close"]) >= structure.last_low - buffer and float(current.bar["close"]) < structure.last_low - buffer:
        breakout = -1
    recovery = 0
    distance = M15_RECOVERY_PROXIMITY_ATR * atr
    if structure.state == "BULLISH" and structure.last_low is not None and abs(float(previous.bar["low"]) - structure.last_low) <= distance and float(current.bar["close"]) > float(previous.bar["high"]):
        recovery = 1
    elif structure.state == "BEARISH" and structure.last_high is not None and abs(float(previous.bar["high"]) - structure.last_high) <= distance and float(current.bar["close"]) < float(previous.bar["low"]):
        recovery = -1
    body = float(current.bar["close"]) - float(current.bar["open"])
    momentum = 1 if body >= M15_MOMENTUM_BODY_ATR * atr else -1 if body <= -M15_MOMENTUM_BODY_ATR * atr else 0
    return {
        "available": True,
        "closed_bar_timestamp": current.bar["timestamp"],
        "structure": structure.state,
        "last_swing_high": structure.last_high,
        "last_swing_low": structure.last_low,
        "breakout": "LONG" if breakout > 0 else "SHORT" if breakout < 0 else "NONE",
        "recovery": "LONG" if recovery > 0 else "SHORT" if recovery < 0 else "NONE",
        "momentum": "LONG" if momentum > 0 else "SHORT" if momentum < 0 else "NONE",
        "requested_long_adjustment": m15_timing_adjustment(structure.state, 1, breakout, recovery, momentum),
        "requested_short_adjustment": m15_timing_adjustment(structure.state, -1, breakout, recovery, momentum),
    }


def _last_sunday(year: int, month: int) -> int:
    if month == 12:
        last = datetime(year + 1, 1, 1, tzinfo=timezone.utc) - timedelta(days=1)
    else:
        last = datetime(year, month + 1, 1, tzinfo=timezone.utc) - timedelta(days=1)
    return last.day - ((last.weekday() + 1) % 7)


def _nth_sunday(year: int, month: int, occurrence: int) -> int:
    first = datetime(year, month, 1, tzinfo=timezone.utc)
    return 1 + ((6 - first.weekday()) % 7) + (occurrence - 1) * 7


def _london_dst(utc: datetime) -> bool:
    if utc.month < 3 or utc.month > 10:
        return False
    if 3 < utc.month < 10:
        return True
    if utc.month == 3:
        start = _last_sunday(utc.year, 3)
        return utc.day > start or (utc.day == start and utc.hour >= 1)
    end = _last_sunday(utc.year, 10)
    return utc.day < end or (utc.day == end and utc.hour < 1)


def _new_york_dst(utc: datetime) -> bool:
    if utc.month < 3 or utc.month > 11:
        return False
    if 3 < utc.month < 11:
        return True
    if utc.month == 3:
        start = _nth_sunday(utc.year, 3, 2)
        return utc.day > start or (utc.day == start and utc.hour >= 7)
    end = _nth_sunday(utc.year, 11, 1)
    return utc.day < end or (utc.day == end and utc.hour < 6)


def session_context(server_wall_ms: int, server_utc_offset_hours: float | None) -> dict:
    if server_utc_offset_hours is None:
        return {"available": False, "name": None, "points": 0, "reason": "MT5 server UTC offset not supplied"}
    utc_ms = server_wall_ms - int(server_utc_offset_hours * 3_600_000)
    utc = datetime.fromtimestamp(utc_ms / 1000, timezone.utc)
    if utc.weekday() >= 5:
        return {"available": True, "name": "MERCADO CERRADO", "phase": "FIN DE SEMANA", "points": 0, "utc": utc.isoformat()}
    minute = utc.hour * 60 + utc.minute
    london_open = 7 * 60 if _london_dst(utc) else 8 * 60
    london_close = 16 * 60 if _london_dst(utc) else 17 * 60
    ny_open = 12 * 60 + 20 if _new_york_dst(utc) else 13 * 60 + 20
    ny_close = 20 * 60 if _new_york_dst(utc) else 21 * 60
    london = london_open <= minute < london_close
    new_york = ny_open <= minute < ny_close
    asia_windows = (
        ("TOKIO", 0, 540),
        ("SEUL", 0, 540),
        ("SHANGHAI", 60, 540),
        ("HONG KONG", 60, 540),
        ("SINGAPUR", 60, 540),
        ("MUMBAI", 210, 690),
        ("DUBAI", 300, 780),
    )
    active_centers = [name for name, start, end in asia_windows if start <= minute < end]
    opening_window = any(start <= minute < start + 90 for _, start, _ in asia_windows)
    if london:
        active_centers.append("LONDRES")
        opening_window = opening_window or minute < london_open + 90
    if new_york:
        active_centers.append("NUEVA YORK/COMEX")
        opening_window = opening_window or minute < ny_open + 90
    lbma_am_open = 9 * 60 + 30 if _london_dst(utc) else 10 * 60 + 30
    lbma_pm_open = 14 * 60 if _london_dst(utc) else 15 * 60
    if lbma_am_open <= minute < lbma_am_open + 20:
        active_centers.append("LBMA AM")
        opening_window = True
    if lbma_pm_open <= minute < lbma_pm_open + 20:
        active_centers.append("LBMA PM")
        opening_window = True
    asia = sum(start <= minute < end for _, start, end in asia_windows)
    if london and new_york:
        name, phase, volatility, points = "LONDRES+NUEVA YORK", "SOLAPE", "ALTA", 2
    elif london:
        name, phase, volatility, points = "LONDRES", "APERTURA" if opening_window else "ACTIVA", "MEDIA", 1
    elif new_york:
        name, phase, volatility, points = "NUEVA YORK", "APERTURA" if opening_window else "ACTIVA", "MEDIA", 1
    elif asia:
        name, phase, volatility, points = "ASIA", "APERTURA" if opening_window else "ACTIVA", "BAJA", 0
    else:
        name, phase, volatility, points = "TRANSICION", "BAJA LIQUIDEZ", "BAJA", 0
    return {
        "available": True,
        "name": name,
        "phase": phase,
        "volatility": volatility,
        "points": points,
        "opening_window": opening_window,
        "active_centers": active_centers,
        "utc": utc.isoformat(),
    }


def _linear_slope(values: Sequence[float]) -> float | None:
    if len(values) < 2:
        return None
    mean_x = (len(values) - 1) / 2.0
    mean_y = sum(values) / len(values)
    denominator = sum((index - mean_x) ** 2 for index in range(len(values)))
    return sum((index - mean_x) * (value - mean_y) for index, value in enumerate(values)) / denominator if denominator else None


def _window_behavior(points: Sequence[StatePoint], end: int, size: int, point_size: float) -> dict:
    if end + 1 < size:
        return {"available": False, "bars": end + 1}
    window = points[end - size + 1 : end + 1]
    closes = [float(point.bar["close"]) for point in window]
    atr = points[end].indicators.get("atr")
    slope = _linear_slope(closes)
    highest = max(float(point.bar["high"]) for point in window)
    lowest = min(float(point.bar["low"]) for point in window)
    span = highest - lowest
    bulls = sum(float(point.bar["close"]) > float(point.bar["open"]) for point in window)
    bears = sum(float(point.bar["close"]) < float(point.bar["open"]) for point in window)
    atr_value = float(atr) if _is_number(atr) and float(atr) > 0.0 else None
    local_bars = [point.bar for point in window]
    local_atrs = [point.indicators.get("atr") for point in window]
    structure = _classify_pivots(_detect_pivots(local_bars, local_atrs, point_size), point_size)
    return {
        "available": atr_value is not None,
        "bars": size,
        "slope_price_per_bar": slope,
        "slope_atr_per_bar": slope / atr_value if slope is not None and atr_value else None,
        "net_displacement": closes[-1] - closes[0],
        "net_displacement_atr": (closes[-1] - closes[0]) / atr_value if atr_value else None,
        "bullish_candle_ratio": bulls / size,
        "bearish_candle_ratio": bears / size,
        "position_in_range": (closes[-1] - lowest) / span if span > 0.0 else 0.5,
        "structure": structure.state,
        "hh": structure.hh,
        "hl": structure.hl,
        "lh": structure.lh,
        "ll": structure.ll,
    }


def h1_regime(points: Sequence[StatePoint], index: int, point_size: float) -> dict:
    windows = {size: _window_behavior(points, index, size, point_size) for size in (30, 10, 5, 3)}
    current = points[index]
    atr = current.indicators.get("atr")
    ema20 = current.indicators.get("ema20")
    close = float(current.bar["close"])
    relation = "ABOVE" if _is_number(ema20) and close > float(ema20) else "BELOW" if _is_number(ema20) and close < float(ema20) else "AT_OR_UNAVAILABLE"
    slope10 = windows[10].get("slope_atr_per_bar")
    slope3 = windows[3].get("slope_atr_per_bar")
    acceleration = slope3 - slope10 if _is_number(slope3) and _is_number(slope10) else None
    structure30 = windows[30].get("structure")
    bearish_shift = structure30 == "BULLISH" and _is_number(slope3) and float(slope3) < 0.0 and relation == "BELOW" and _is_number(acceleration) and float(acceleration) < 0.0
    bullish_shift = structure30 == "BEARISH" and _is_number(slope3) and float(slope3) > 0.0 and relation == "ABOVE" and _is_number(acceleration) and float(acceleration) > 0.0
    shift = "POSSIBLE_BEARISH_REVERSAL" if bearish_shift else "POSSIBLE_BULLISH_REVERSAL" if bullish_shift else "NONE"
    return {
        "available": windows[30].get("available", False),
        "window_30": windows[30],
        "window_10": windows[10],
        "window_5": windows[5],
        "window_3": windows[3],
        "ema20_relation": relation,
        "recent_acceleration": acceleration,
        "possible_reversal": shift != "NONE",
        "behavior_shift": shift,
        "atr": atr,
        "score_effect": 0,
    }


def _decision_record(
    observation: dict,
    h1_points: Sequence[StatePoint],
    h1_index: int,
    h4_point: StatePoint,
    m15_points: Sequence[StatePoint],
    m15_index: int,
    point_size: float,
    server_utc_offset_hours: float | None,
) -> dict:
    h1 = h1_points[h1_index]
    h4 = h4_point
    required = (
        h1.indicators.get("ema20"),
        h1.indicators.get("ema50"),
        h1.indicators.get("rsi"),
        h1.indicators.get("adx"),
        h1.indicators.get("atr"),
        h4.indicators.get("ema50"),
        h4.indicators.get("ema200"),
    )
    base = {
        "event_id": observation["event_id"],
        "enrichment_version": ENRICHMENT_VERSION,
        "timestamp": observation["timestamp"],
        "observed_at": observation.get("observed_at"),
        "source": SOURCE,
        "observer_only": True,
        "score_effect": 0,
        "replay_point_size": point_size,
        "decision_observation_basis": "H1_CLOSED_CORE_AS_OF_OBSERVED_AT",
        "threshold_arm": THRESHOLD_ARM,
        "threshold_trade": THRESHOLD_TRADE,
    }
    regime = h1_regime(h1_points, h1_index, point_size)
    if not all(_is_number(value) for value in required) or h1_index < max(STRUCTURE_LOOKBACK, VOLUME_LOOKBACK):
        return {
            **base,
            "long_score": None,
            "short_score": None,
            "armed_direction": None,
            "decision": "INSUFFICIENT_DATA",
            "decision_reason": "EA closed-bar prerequisites are unavailable in replay warmup",
            "h4_context": None,
            "h1_structure": {"state": h1.structure.state},
            "m15_timing": None,
            "session": None,
            "session_status": "UNAVAILABLE_SERVER_UTC_OFFSET" if server_utc_offset_hours is None else "AVAILABLE",
            "h1_regime": regime,
            "score_reproduction": {"status": "UNAVAILABLE_WARMUP", "unavailable_components": list(UNAVAILABLE_COMPONENTS)},
        }
    ema_fast, ema_slow = float(h1.indicators["ema20"]), float(h1.indicators["ema50"])
    h4_fast, h4_slow = float(h4.indicators["ema50"]), float(h4.indicators["ema200"])
    rsi, adx, atr = float(h1.indicators["rsi"]), float(h1.indicators["adx"]), float(h1.indicators["atr"])
    current = h1.bar
    previous = h1_points[h1_index - 1].bar
    prior = h1_points[max(0, h1_index - STRUCTURE_LOOKBACK) : h1_index]
    recent_high = max(float(point.bar["high"]) for point in prior)
    recent_low = min(float(point.bar["low"]) for point in prior)
    volume_prior = h1_points[max(0, h1_index - VOLUME_LOOKBACK) : h1_index]
    average_volume = sum(float(point.bar["tick_count"]) for point in volume_prior) / len(volume_prior)
    current_volume = float(current["tick_count"])
    bull_htf, bear_htf = h4_fast > h4_slow, h4_fast < h4_slow
    bull_ltf, bear_ltf = ema_fast > ema_slow, ema_fast < ema_slow
    adx_build = adx >= 16.0
    volume_ok = average_volume > 0.0 and current_volume >= average_volume * 1.05
    structure = h1.structure
    break_long = float(current["close"]) > recent_high
    break_short = float(current["close"]) < recent_low
    mom_long = float(current["close"]) > float(previous["high"])
    mom_short = float(current["close"]) < float(previous["low"])
    near_fast = atr > 0.0 and abs(float(current["close"]) - ema_fast) <= atr * 0.75
    pull_long = atr > 0.0 and near_fast and float(current["close"]) <= ema_fast and rsi <= 48.0 and (bull_htf or bull_ltf) and structure.hl
    pull_short = atr > 0.0 and near_fast and float(current["close"]) >= ema_fast and rsi >= 52.0 and (bear_htf or bear_ltf) and structure.lh
    htf_pull_long = bull_htf and not bull_ltf and near_fast and rsi <= 42.0 and structure.hl and float(current["close"]) >= float(previous["low"])
    htf_pull_short = bear_htf and not bear_ltf and near_fast and rsi >= 58.0 and structure.lh and float(current["close"]) <= float(previous["high"])
    pull_long = pull_long or htf_pull_long
    pull_short = pull_short or htf_pull_short
    long_structural = structural_bucket(structure.state, 1, pull_long, break_long, mom_long)
    short_structural = structural_bucket(structure.state, -1, pull_short, break_short, mom_short)
    long_technical = (20 if bull_htf else 0) + (15 if bull_ltf else 0) + (5 if bull_htf and bull_ltf else 0)
    short_technical = (20 if bear_htf else 0) + (15 if bear_ltf else 0) + (5 if bear_htf and bear_ltf else 0)
    if adx_build:
        if bull_htf or bull_ltf:
            long_technical += 10
        if bear_htf or bear_ltf:
            short_technical += 10
    if 50.0 <= rsi <= 68.0:
        long_technical += 10
    if 32.0 <= rsi <= 50.0:
        short_technical += 10
    long_technical += long_structural
    short_technical += short_structural
    if volume_ok:
        if float(current["close"]) >= float(current["open"]):
            long_technical += 5
        else:
            short_technical += 5
    long_technical, short_technical = min(100, long_technical), min(100, short_technical)
    m15 = _m15_evidence(m15_points, m15_index, point_size)
    long_m15 = _gated_adjustment(long_technical, int(m15.get("requested_long_adjustment", 0)))
    short_m15 = _gated_adjustment(short_technical, int(m15.get("requested_short_adjustment", 0)))
    m15["long_adjustment"] = long_m15
    m15["short_adjustment"] = short_m15
    session = session_context(int(observation.get("observed_at", current["close_timestamp"])), server_utc_offset_hours)
    session_points = int(session["points"]) if session["available"] else 0
    long_session = session_points if long_technical >= THRESHOLD_ARM else 0
    short_session = session_points if short_technical >= THRESHOLD_ARM else 0
    long_score = max(0, min(100, long_technical + long_m15 + long_session))
    short_score = max(0, min(100, short_technical + short_m15 + short_session))
    best_score = max(long_score, short_score)
    armed_direction = "LONG" if long_score >= short_score else "SHORT"
    if best_score < THRESHOLD_ARM:
        decision = "NO_TRADE"
        armed_direction = None
        reason = f"partial closed-bar score L={long_score}/S={short_score} below arm={THRESHOLD_ARM}"
    elif best_score < THRESHOLD_TRADE:
        decision = "ARMED"
        reason = f"partial closed-bar candidate {armed_direction} score={best_score}; intrabar trigger not replayed"
    else:
        decision = "ARMED"
        reason = f"partial score reaches trade threshold for {armed_direction}; execution cannot be inferred without intrabar/account/news state"
    missing = list(UNAVAILABLE_COMPONENTS)
    if not session["available"]:
        missing.append("session context (MT5 server UTC offset unavailable)")
    return {
        **base,
        "long_score": long_score,
        "short_score": short_score,
        "long_technical_score": long_technical,
        "short_technical_score": short_technical,
        "armed_direction": armed_direction,
        "decision": decision,
        "decision_reason": reason,
        "h4_context": {"trend": "BULLISH" if bull_htf else "BEARISH" if bear_htf else "NEUTRAL", "ema50": h4_fast, "ema200": h4_slow},
        "h1_structure": {
            "ema_trend": "BULLISH" if bull_ltf else "BEARISH" if bear_ltf else "NEUTRAL",
            "pivot_state": structure.state,
            "hh": structure.hh,
            "hl": structure.hl,
            "lh": structure.lh,
            "ll": structure.ll,
            "alternating_pivots": structure.alternating_pivots,
            "breakout_long": break_long,
            "breakout_short": break_short,
            "pullback_long": pull_long,
            "pullback_short": pull_short,
            "momentum_long": mom_long,
            "momentum_short": mom_short,
            "structural_points_long_without_patterns": long_structural,
            "structural_points_short_without_patterns": short_structural,
        },
        "m15_timing": m15,
        "session": session["name"],
        "session_status": "AVAILABLE" if session["available"] else "UNAVAILABLE_SERVER_UTC_OFFSET",
        "h1_regime": regime,
        "score_reproduction": {
            "status": "PARTIAL_EXACT_CLOSED_BAR_CORE",
            "exact_logic": [
                "H4 EMA50/EMA200 direction",
                "H1 EMA20/EMA50, RSI14, ADX14, ATR14 arithmetic",
                "tick-volume confirmation using previous 20 H1 bars",
                "confirmed pivot HH/HL/LH/LL and structural bucket without pattern bonus",
                "M15 timing adjustment and arm-threshold gate",
                "session scoring when server UTC offset is supplied",
            ],
            "unavailable_components": missing,
            "indicator_parity": "formula-matched; terminal iMA/iADX initialization parity still requires MT5 comparison",
            "patterns_assumed_zero": True,
            "news_assumed_zero": True,
            "intrabar_assumed_zero": True,
        },
    }


def enrich_historical_decisions(
    input_dir: Path,
    *,
    output_path: Path | None = None,
    symbol: str = "XAUUSD",
    point_size: float = 0.01,
    server_utc_offset_hours: float | None = None,
) -> dict:
    if point_size <= 0.0:
        raise ValueError("point_size must be positive")
    input_dir = Path(input_dir)
    observations_path = input_dir / "historical_observations.jsonl"
    output_path = Path(output_path) if output_path is not None else input_dir / "historical_decisions.jsonl"
    bars = {timeframe: _load_bars(input_dir, symbol, timeframe) for timeframe in ("M15", "H1", "H4")}
    states = {timeframe: build_state_points(values, point_size) for timeframe, values in bars.items()}
    close_times = {timeframe: [int(point.bar["close_timestamp"]) for point in values] for timeframe, values in states.items()}
    writer = DeduplicatingJsonlWriter(output_path, "event_id")
    total = enriched = insufficient = 0
    try:
        for observation in _read_jsonl(observations_path):
            total += 1
            event_id = observation.get("event_id")
            observed_at = observation.get("observed_at")
            if not isinstance(event_id, str) or not _is_number(observed_at):
                raise EnrichmentError(f"invalid observation contract at row {total}")
            indices = {
                timeframe: bisect_right(close_times[timeframe], int(observed_at)) - 1
                for timeframe in ("M15", "H1", "H4")
            }
            if any(index < 0 for index in indices.values()):
                record = {
                    "event_id": event_id,
                    "enrichment_version": ENRICHMENT_VERSION,
                    "timestamp": observation.get("timestamp"),
                    "observed_at": observed_at,
                    "source": SOURCE,
                    "observer_only": True,
                    "score_effect": 0,
                    "replay_point_size": point_size,
                    "decision_observation_basis": "H1_CLOSED_CORE_AS_OF_OBSERVED_AT",
                    "long_score": None,
                    "short_score": None,
                    "armed_direction": None,
                    "decision": "INSUFFICIENT_DATA",
                    "decision_reason": "no causal H1/H4/M15 state exists at observation time",
                    "h4_context": None,
                    "h1_structure": None,
                    "m15_timing": None,
                    "session": None,
                    "threshold_arm": THRESHOLD_ARM,
                    "threshold_trade": THRESHOLD_TRADE,
                    "score_reproduction": {"status": "UNAVAILABLE_WARMUP"},
                    "h1_regime": {"available": False, "score_effect": 0},
                }
            else:
                record = _decision_record(
                    observation,
                    states["H1"],
                    indices["H1"],
                    states["H4"][indices["H4"]],
                    states["M15"],
                    indices["M15"],
                    point_size,
                    server_utc_offset_hours,
                )
            writer.append(record)
            enriched += 1
            insufficient += record["decision"] == "INSUFFICIENT_DATA"
    finally:
        writer.close()
    return {
        "observations_seen": total,
        "observations_enriched": enriched,
        "insufficient_data": insufficient,
        "written": writer.written,
        "deduplicated": writer.skipped,
        "output": str(output_path),
        "score_reproduction": "PARTIAL_EXACT_CLOSED_BAR_CORE",
        "server_utc_offset_hours": server_utc_offset_hours,
        "point_size": point_size,
        "observer_only": True,
        "score_effect": 0,
    }


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    root = Path(__file__).resolve().parent / "output"
    parser.add_argument("--input-dir", type=Path, default=root)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--symbol", default="XAUUSD")
    parser.add_argument("--point-size", type=float, default=0.01)
    parser.add_argument("--server-utc-offset-hours", type=float)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_argument_parser().parse_args(argv)
    try:
        report = enrich_historical_decisions(
            arguments.input_dir,
            output_path=arguments.output,
            symbol=arguments.symbol,
            point_size=arguments.point_size,
            server_utc_offset_hours=arguments.server_utc_offset_hours,
        )
    except (EnrichmentError, OSError, ValueError) as error:
        print(f"[ENRICHMENT][ERROR] {error}")
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
