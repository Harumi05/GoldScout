"""Deterministic diagnostics for triangles and wedges from confirmed pivots."""

from dataclasses import dataclass, replace
from pathlib import Path
import math
import unittest


ROOT = Path(__file__).resolve().parents[1]
EA_PATH = ROOT / "MT5" / "Experts" / "XAU_GoldScout_H1.mq5"
STRUCTURE_PATH = ROOT / "MT5" / "Include" / "GoldScout" / "MarketStructure.mqh"


@dataclass(frozen=True)
class Config:
    min_pattern_bars: int = 8
    max_pattern_bars: int = 72
    max_confirmation_bars: int = 12
    horizontal_slope_atr: float = 0.03
    min_slope_atr: float = 0.05
    min_slope_separation_atr: float = 0.02
    max_width_ratio: float = 0.80
    max_line_fit_atr: float = 0.25
    min_bars_before_apex: int = 1
    min_breakout_bars_before_apex: int = 2
    max_apex_distance_ratio: float = 2.0
    breakout_buffer_atr: float = 0.05
    use_volume_quality: bool = True
    volume_lookback: int = 20
    volume_multiplier: float = 1.05
    use_momentum_quality: bool = True
    momentum_body_atr: float = 0.50


@dataclass(frozen=True)
class Bar:
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: int = 100


@dataclass(frozen=True)
class Pivot:
    kind: str
    price: float
    shift: int
    timestamp: int
    atr: float = 1.0
    confirmed: bool = True


VALUES = {
    "ASC_TRIANGLE": (
        ("HIGH", 110.0), ("LOW", 100.0), ("HIGH", 110.0),
        ("LOW", 102.0), ("HIGH", 110.0), ("LOW", 104.0),
    ),
    "DESC_TRIANGLE": (
        ("LOW", 100.0), ("HIGH", 110.0), ("LOW", 100.0),
        ("HIGH", 108.0), ("LOW", 100.0), ("HIGH", 106.0),
    ),
    "SYMM_TRIANGLE": (
        ("HIGH", 110.0), ("LOW", 100.0), ("HIGH", 109.0),
        ("LOW", 101.0), ("HIGH", 108.0), ("LOW", 102.0),
    ),
    "RISING_WEDGE": (
        ("HIGH", 106.0), ("LOW", 100.0), ("HIGH", 107.0),
        ("LOW", 102.0), ("HIGH", 108.0), ("LOW", 104.0),
    ),
    "FALLING_WEDGE": (
        ("HIGH", 110.0), ("LOW", 104.0), ("HIGH", 108.0),
        ("LOW", 103.0), ("HIGH", 106.0), ("LOW", 102.0),
    ),
}


def bar(index, close=105.0, *, open_price=None, high=None, low=None, volume=100):
    open_price = close if open_price is None else open_price
    return Bar(
        (index + 1) * 3600,
        open_price,
        max(open_price, close) + 0.4 if high is None else high,
        min(open_price, close) - 0.4 if low is None else low,
        close,
        volume,
    )


def normalize(pivots):
    result = []
    previous_time = None
    previous_shift = None
    for pivot in pivots:
        if not pivot.confirmed:
            continue
        if previous_time is not None and (
            pivot.timestamp <= previous_time or pivot.shift >= previous_shift
        ):
            return None
        previous_time = pivot.timestamp
        previous_shift = pivot.shift
        if result and result[-1].kind == pivot.kind:
            more_extreme = (
                pivot.price > result[-1].price
                if pivot.kind == "HIGH"
                else pivot.price < result[-1].price
            )
            if more_extreme:
                result[-1] = pivot
        else:
            result.append(pivot)
    return result


def fit_line(points, indices, reference_atr, max_line_fit_atr):
    mean_x = sum(indices) / 3
    mean_y = sum(point.price for point in points) / 3
    denominator = sum((index - mean_x) ** 2 for index in indices)
    if denominator <= 0:
        return None
    slope = sum(
        (index - mean_x) * (point.price - mean_y)
        for point, index in zip(points, indices)
    ) / denominator
    intercept = mean_y - slope * mean_x
    rms = math.sqrt(
        sum(
            (point.price - (intercept + slope * index)) ** 2
            for point, index in zip(points, indices)
        )
        / 3
    )
    maximum = max_line_fit_atr * reference_atr
    if rms > maximum:
        return None
    return slope, intercept, max(0.0, min(1.0, 1 - rms / maximum))


def optional_volume(bars, breakout_index, config):
    if not config.use_volume_quality:
        return False
    samples = [
        item.volume
        for item in bars[max(0, breakout_index - config.volume_lookback) : breakout_index]
        if item.volume > 0
    ]
    return bool(
        samples
        and bars[breakout_index].volume > 0
        and bars[breakout_index].volume
        >= sum(samples) / len(samples) * config.volume_multiplier
    )


def slope_quality(pattern_type, upper, lower, config):
    separation = max(
        0.0,
        min(1.0, (lower - upper) / (2 * config.min_slope_separation_atr)),
    )
    if pattern_type == "ASC_TRIANGLE":
        horizontal = max(0.0, min(1.0, 1 - abs(upper) / config.horizontal_slope_atr))
        directional = max(0.0, min(1.0, lower / (2 * config.min_slope_atr)))
        return (horizontal + directional + separation) / 3
    if pattern_type == "DESC_TRIANGLE":
        horizontal = max(0.0, min(1.0, 1 - abs(lower) / config.horizontal_slope_atr))
        directional = max(0.0, min(1.0, -upper / (2 * config.min_slope_atr)))
        return (horizontal + directional + separation) / 3
    directional = max(
        0.0, min(1.0, min(abs(upper), abs(lower)) / (2 * config.min_slope_atr))
    )
    return (directional + separation) / 2


def quality(pattern, config):
    ideal_duration = (config.min_pattern_bars + config.max_pattern_bars) / 2
    duration_half = max(0.5, (config.max_pattern_bars - config.min_pattern_bars) / 2)
    duration = max(
        0.0,
        min(1.0, 1 - abs(pattern["duration_bars"] - ideal_duration) / duration_half),
    )
    contraction = max(0.0, min(1.0, 1 - pattern["width_ratio"]))
    breakout = (
        max(0.0, min(1.0, pattern["breakout_strength_atr"]))
        if pattern["state"] == "CONFIRMED"
        else 0.0
    )
    result = (
        20 * max(0.0, min(1.0, pattern["line_fit_quality"]))
        + 15 * max(0.0, min(1.0, pattern["slope_quality"]))
        + 15 * max(0.0, min(1.0, pattern["convergence_quality"]))
        + 10 * duration
        + 15 * contraction
        + 10 * max(0.0, min(1.0, pattern["apex_position_quality"]))
        + 10 * breakout
        + (2.5 if pattern["volume_confirmed"] else 0)
        + (2.5 if pattern["momentum_confirmed"] else 0)
    )
    return max(0.0, min(100.0, result))


def quality_name(value):
    return "HIGH" if value >= 75 else "MEDIUM" if value >= 50 else "LOW"


def detect(pivots, bars, config=Config()):
    none = {"detected": False, "type": "NONE", "state": "NONE", "quality": 0.0}
    alternating = normalize(pivots)
    if alternating is None:
        return None
    by_time = {item.timestamp: index for index, item in enumerate(bars)}
    for start in range(len(alternating) - 6, -1, -1):
        sequence = alternating[start : start + 6]
        indices = [by_time.get(item.timestamp, -1) for item in sequence]
        if indices[0] < 0 or any(indices[i] <= indices[i - 1] for i in range(1, 6)):
            continue
        if [item.shift for item in sequence] != [len(bars) - index for index in indices]:
            continue
        duration = indices[5] - indices[0]
        if not config.min_pattern_bars <= duration <= config.max_pattern_bars:
            continue
        upper = [(item, index) for item, index in zip(sequence, indices) if item.kind == "HIGH"]
        lower = [(item, index) for item, index in zip(sequence, indices) if item.kind == "LOW"]
        if len(upper) != 3 or len(lower) != 3:
            continue
        reference_atr = max(item.atr for item in sequence)
        upper_fit = fit_line(
            [item for item, _ in upper], [index for _, index in upper],
            reference_atr, config.max_line_fit_atr,
        )
        lower_fit = fit_line(
            [item for item, _ in lower], [index for _, index in lower],
            reference_atr, config.max_line_fit_atr,
        )
        if upper_fit is None or lower_fit is None:
            continue
        upper_slope, upper_intercept, upper_quality = upper_fit
        lower_slope, lower_intercept, lower_quality = lower_fit
        upper_normalized = upper_slope / reference_atr
        lower_normalized = lower_slope / reference_atr
        separation = lower_normalized - upper_normalized
        if separation < config.min_slope_separation_atr:
            continue
        upper_horizontal = abs(upper_normalized) <= config.horizontal_slope_atr
        lower_horizontal = abs(lower_normalized) <= config.horizontal_slope_atr
        upper_rising = upper_normalized >= config.min_slope_atr
        lower_rising = lower_normalized >= config.min_slope_atr
        upper_falling = upper_normalized <= -config.min_slope_atr
        lower_falling = lower_normalized <= -config.min_slope_atr
        pattern_type = None
        if upper_horizontal and lower_rising:
            pattern_type = "ASC_TRIANGLE"
        elif lower_horizontal and upper_falling:
            pattern_type = "DESC_TRIANGLE"
        elif upper_falling and lower_rising:
            pattern_type = "SYMM_TRIANGLE"
        elif upper_rising and lower_rising and lower_normalized > upper_normalized:
            pattern_type = "RISING_WEDGE"
        elif upper_falling and lower_falling and upper_normalized < lower_normalized:
            pattern_type = "FALLING_WEDGE"
        if pattern_type is None:
            continue
        initial_width = (
            upper_intercept + upper_slope * indices[0]
            - lower_intercept - lower_slope * indices[0]
        )
        final_width = (
            upper_intercept + upper_slope * indices[5]
            - lower_intercept - lower_slope * indices[5]
        )
        if initial_width <= 0 or final_width <= 0:
            continue
        width_ratio = final_width / initial_width
        if not 0 < width_ratio <= config.max_width_ratio:
            continue
        apex = (upper_intercept - lower_intercept) / (lower_slope - upper_slope)
        apex_distance = apex - indices[5]
        if not (
            apex_distance >= config.min_bars_before_apex
            and apex_distance <= duration * config.max_apex_distance_ratio
        ):
            continue
        result = {
            "detected": True,
            "type": pattern_type,
            "state": "CANDIDATE",
            "identity": f"{pattern_type}:" + ":".join(str(item.timestamp) for item in sequence),
            "duration_bars": duration,
            "width_ratio": width_ratio,
            "line_fit_quality": (upper_quality + lower_quality) / 2,
            "slope_quality": slope_quality(
                pattern_type, upper_normalized, lower_normalized, config
            ),
            "convergence_quality": max(
                0.0, min(1.0, separation / (2 * config.min_slope_separation_atr))
            ),
            "apex_position_quality": max(
                0.0,
                min(1.0, 1 - abs(apex_distance / duration - 0.75) / 0.75),
            ),
            "breakout_direction": 0,
            "breakout_strength_atr": 0.0,
            "volume_confirmed": False,
            "momentum_confirmed": False,
            "event_time": 0,
        }
        breakout_buffer = max(0.01, config.breakout_buffer_atr * reference_atr)
        bars_after = 0
        for current in range(indices[5] + 1, len(bars)):
            bars_after += 1
            item = bars[current]
            bars_before_apex = apex - current
            if (
                bars_after > config.max_confirmation_bars
                or current >= apex
                or bars_before_apex < config.min_breakout_bars_before_apex
            ):
                result.update(state="EXPIRED", event_time=item.timestamp)
                break
            upper_boundary = upper_intercept + upper_slope * current
            lower_boundary = lower_intercept + lower_slope * current
            long_break = item.close > upper_boundary + breakout_buffer
            short_break = item.close < lower_boundary - breakout_buffer
            primary_long = pattern_type == "ASC_TRIANGLE"
            primary_short = pattern_type == "DESC_TRIANGLE"
            if (primary_long and short_break) or (primary_short and long_break):
                result.update(
                    state="INVALIDATED",
                    event_time=item.timestamp,
                    breakout_direction=-1 if short_break else 1,
                )
                break
            confirmed = (
                (primary_long and long_break)
                or (primary_short and short_break)
                or (not primary_long and not primary_short and (long_break or short_break))
            )
            if confirmed:
                direction = 1 if long_break else -1
                strength = (
                    (item.close - upper_boundary) / reference_atr
                    if long_break
                    else (lower_boundary - item.close) / reference_atr
                )
                progress = (current - indices[5]) / (apex - indices[5])
                result.update(
                    state="CONFIRMED",
                    event_time=item.timestamp,
                    breakout_direction=direction,
                    breakout_strength_atr=strength,
                    volume_confirmed=optional_volume(bars, current, config),
                    momentum_confirmed=config.use_momentum_quality
                    and ((item.close - item.open) if direction > 0 else (item.open - item.close))
                    >= config.momentum_body_atr * reference_atr,
                    apex_position_quality=max(
                        0.0, min(1.0, 1 - abs(progress - 0.65) / 0.65)
                    ),
                )
                break
        result["quality"] = quality(result, config)
        return result
    return none


def scenario(pattern_type, post=None, indices=(1, 4, 7, 10, 13, 16), values=None):
    post = list(post or [])
    total = indices[-1] + 1 + len(post)
    bars = [bar(index) for index in range(total)]
    values = VALUES[pattern_type] if values is None else values
    pivots = [
        Pivot(kind, price, total - index, bars[index].timestamp)
        for (kind, price), index in zip(values, indices)
    ]
    for offset, supplied in enumerate(post, start=indices[-1] + 1):
        bars[offset] = replace(supplied, timestamp=bars[offset].timestamp)
    return pivots, bars


class ConvergencePatternTests(unittest.TestCase):
    def test_valid_ascending_triangle(self):
        result = detect(*scenario("ASC_TRIANGLE", [bar(0, 111.0, open_price=109.8, volume=180)]))
        self.assertEqual((result["type"], result["state"], result["breakout_direction"]),
                         ("ASC_TRIANGLE", "CONFIRMED", 1))

    def test_valid_descending_triangle(self):
        result = detect(*scenario("DESC_TRIANGLE", [bar(0, 99.0, open_price=100.2, volume=180)]))
        self.assertEqual((result["type"], result["state"], result["breakout_direction"]),
                         ("DESC_TRIANGLE", "CONFIRMED", -1))

    def test_valid_symmetric_triangle_breakout_long(self):
        result = detect(*scenario("SYMM_TRIANGLE", [bar(0, 108.0, open_price=106.8)]))
        self.assertEqual((result["type"], result["state"], result["breakout_direction"]),
                         ("SYMM_TRIANGLE", "CONFIRMED", 1))

    def test_valid_symmetric_triangle_breakout_short(self):
        result = detect(*scenario("SYMM_TRIANGLE", [bar(0, 101.0, open_price=102.0)]))
        self.assertEqual((result["type"], result["state"], result["breakout_direction"]),
                         ("SYMM_TRIANGLE", "CONFIRMED", -1))

    def test_valid_rising_wedge(self):
        result = detect(*scenario("RISING_WEDGE", [bar(0, 103.5, open_price=104.5)]))
        self.assertEqual((result["type"], result["state"], result["breakout_direction"]),
                         ("RISING_WEDGE", "CONFIRMED", -1))

    def test_valid_falling_wedge(self):
        result = detect(*scenario("FALLING_WEDGE", [bar(0, 105.5, open_price=104.5)]))
        self.assertEqual((result["type"], result["state"], result["breakout_direction"]),
                         ("FALLING_WEDGE", "CONFIRMED", 1))

    def test_lateral_range_is_rejected(self):
        values = (("HIGH", 110.0), ("LOW", 100.0)) * 3
        self.assertFalse(detect(*scenario("ASC_TRIANGLE", values=values))["detected"])

    def test_parallel_channel_is_rejected(self):
        values = (
            ("HIGH", 106.0), ("LOW", 100.0), ("HIGH", 108.0),
            ("LOW", 102.0), ("HIGH", 110.0), ("LOW", 104.0),
        )
        self.assertFalse(detect(*scenario("RISING_WEDGE", values=values))["detected"])

    def test_insufficient_convergence_is_rejected(self):
        config = replace(Config(), max_width_ratio=0.50)
        self.assertFalse(detect(*scenario("ASC_TRIANGLE"), config)["detected"])

    def test_fewer_than_six_pivots_is_rejected(self):
        pivots, bars = scenario("SYMM_TRIANGLE")
        self.assertFalse(detect(pivots[:5], bars)["detected"])

    def test_wick_without_close_does_not_confirm(self):
        wick = bar(0, 107.0, high=112.0, low=105.0)
        result = detect(*scenario("ASC_TRIANGLE", [wick]))
        self.assertEqual(result["state"], "CANDIDATE")

    def test_breakout_after_apex_is_expired(self):
        safe = [bar(0, 104.75) for _ in range(16)]
        safe.append(bar(0, 112.0))
        config = replace(Config(), max_confirmation_bars=40)
        result = detect(*scenario("SYMM_TRIANGLE", safe), config)
        self.assertEqual(result["state"], "EXPIRED")

    def test_breakout_sufficiently_before_apex_is_valid(self):
        result = detect(*scenario("SYMM_TRIANGLE", [bar(0, 108.0)]))
        self.assertEqual((result["state"], result["breakout_direction"]),
                         ("CONFIRMED", 1))

    def test_breakout_too_close_to_apex_is_rejected(self):
        safe = [bar(0, 104.75) for _ in range(14)]
        safe.append(bar(0, 112.0))
        config = replace(Config(), max_confirmation_bars=40)
        result = detect(*scenario("SYMM_TRIANGLE", safe), config)
        self.assertEqual(result["state"], "EXPIRED")

    def test_breakout_exactly_at_apex_is_rejected(self):
        exact_apex_values = (
            ("HIGH", 110.0), ("LOW", 102.625), ("HIGH", 109.25),
            ("LOW", 103.375), ("HIGH", 108.5), ("LOW", 104.125),
        )
        safe = [bar(0, 106.125) for _ in range(15)]
        safe.append(bar(0, 112.0))
        config = replace(
            Config(), min_breakout_bars_before_apex=0, max_confirmation_bars=40
        )
        result = detect(
            *scenario("SYMM_TRIANGLE", safe, values=exact_apex_values), config
        )
        self.assertEqual(result["state"], "EXPIRED")

    def test_breakout_strictly_after_apex_is_rejected(self):
        exact_apex_values = (
            ("HIGH", 110.0), ("LOW", 102.625), ("HIGH", 109.25),
            ("LOW", 103.375), ("HIGH", 108.5), ("LOW", 104.125),
        )
        safe = [bar(0, 106.125) for _ in range(16)]
        safe.append(bar(0, 112.0))
        config = replace(
            Config(), min_breakout_bars_before_apex=0, max_confirmation_bars=40
        )
        result = detect(
            *scenario("SYMM_TRIANGLE", safe, values=exact_apex_values), config
        )
        self.assertEqual(result["state"], "EXPIRED")

    def test_pattern_expires(self):
        safe = [bar(0, 107.1666667 + offset / 6) for offset in range(14)]
        result = detect(*scenario("ASC_TRIANGLE", safe))
        self.assertEqual(result["state"], "EXPIRED")

    def test_opposite_breakout_invalidates_directional_triangle(self):
        result = detect(*scenario("ASC_TRIANGLE", [bar(0, 103.0)]))
        self.assertEqual((result["state"], result["breakout_direction"]),
                         ("INVALIDATED", -1))

    def test_identity_is_stable_for_deduplication(self):
        pivots, bars = scenario("FALLING_WEDGE")
        self.assertEqual(detect(pivots, bars)["identity"], detect(pivots, bars)["identity"])

    def test_confirmation_does_not_repaint(self):
        breakout = bar(0, 111.0, open_price=109.8, volume=180)
        first = detect(*scenario("ASC_TRIANGLE", [breakout]))
        later = detect(*scenario("ASC_TRIANGLE", [breakout, bar(0, 95.0)]))
        self.assertEqual(first["state"], "CONFIRMED")
        self.assertEqual(later["state"], "CONFIRMED")
        self.assertEqual(first["event_time"], later["event_time"])
        self.assertEqual(first["quality"], later["quality"])

    def test_high_quality_is_bounded(self):
        safe = [bar(0, 107.1666667 + offset / 6) for offset in range(11)]
        safe.append(bar(0, 112.0, open_price=109.0, volume=220))
        result = detect(*scenario("ASC_TRIANGLE", safe))
        self.assertEqual(quality_name(result["quality"]), "HIGH")
        self.assertLessEqual(result["quality"], 100.0)

    def test_medium_quality(self):
        result = detect(*scenario("RISING_WEDGE"))
        self.assertEqual(quality_name(result["quality"]), "MEDIUM")

    def test_low_quality(self):
        pattern = {
            "duration_bars": 8, "width_ratio": 0.80, "line_fit_quality": 0.05,
            "slope_quality": 0.50, "convergence_quality": 0.50,
            "apex_position_quality": 0.05, "breakout_strength_atr": 0.0,
            "state": "CANDIDATE", "volume_confirmed": False,
            "momentum_confirmed": False,
        }
        self.assertEqual(quality_name(quality(pattern, Config())), "LOW")

    def test_volume_and_momentum_are_optional(self):
        breakout = bar(0, 108.0, open_price=107.8, volume=0)
        result = detect(*scenario("SYMM_TRIANGLE", [breakout]))
        self.assertEqual(result["state"], "CONFIRMED")
        self.assertFalse(result["volume_confirmed"])
        self.assertFalse(result["momentum_confirmed"])


class ConvergencePatternSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ea = EA_PATH.read_text(encoding="utf-8")
        cls.structure = STRUCTURE_PATH.read_text(encoding="utf-8")

    def test_closed_bars_six_touches_and_all_types_exist(self):
        self.assertIn("CopyRates(symbol,timeframe,1,barsToLoad,closedRates)", self.structure)
        self.assertIn("ArraySize(alternating)-6", self.structure)
        for pattern_type in (
            "ASC_TRIANGLE", "DESC_TRIANGLE", "SYMM_TRIANGLE",
            "RISING_WEDGE", "FALLING_WEDGE",
        ):
            self.assertIn(f"GOLDSCOUT_CONVERGENCE_{pattern_type}", self.structure)

    def test_convergence_patterns_never_enter_scoring(self):
        start = self.ea.index("int longScore=0, shortScore=0;")
        end = self.ea.index("longScore=(int)MathMin(100,longScore);", start)
        scoring = self.ea[start:end]
        self.assertNotIn("Convergence", scoring)
        self.assertNotIn("convergence", scoring)
        self.assertIn("input int    ArmScoreThreshold       = 58;", self.ea)
        self.assertIn("input int    MinScoreToTrade         = 74;", self.ea)
        self.assertIn("input bool   EnableLiveTrading      = false;", self.ea)
        self.assertIn("input double RiskPercent             = 5.0;", self.ea)

    def test_optional_logging_is_deduplicated_and_rate_limited(self):
        self.assertIn("input bool   DebugConvergencePatternLogs      = false;", self.ea)
        self.assertIn("input int    MinBreakoutBarsBeforeApex        = 2;", self.ea)
        self.assertIn("CONVERGENCE_PATTERN_LOG_INTERVAL_SECONDS=30", self.ea)
        self.assertIn("if(signature==g_lastConvergencePatternLogSignature) return;", self.ea)
        self.assertIn("barsBeforeApex<(double)config.minBreakoutBarsBeforeApex", self.structure)
        self.assertIn("breakout=%s", self.ea)


if __name__ == "__main__":
    unittest.main()
