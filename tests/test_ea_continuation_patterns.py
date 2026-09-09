"""Deterministic diagnostics for flags and pennants from confirmed pivots."""

from dataclasses import dataclass, replace
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
EA_PATH = ROOT / "MT5" / "Experts" / "XAU_GoldScout_H1.mq5"
STRUCTURE_PATH = ROOT / "MT5" / "Include" / "GoldScout" / "MarketStructure.mqh"


@dataclass(frozen=True)
class Config:
    min_pole_atr: float = 3.0
    min_pole_bars: int = 2
    max_pole_bars: int = 24
    min_pole_efficiency: float = 0.60
    min_retracement: float = 0.10
    max_retracement: float = 0.60
    min_consolidation_bars: int = 4
    max_consolidation_bars: int = 24
    max_confirmation_bars: int = 12
    min_geometry_move_atr: float = 0.20
    min_flag_parallel_ratio: float = 0.50
    flag_width_tolerance: float = 0.50
    max_pennant_width_ratio: float = 0.75
    min_pennant_convergence_balance: float = 0.25
    breakout_buffer_atr: float = 0.05
    invalidation_atr: float = 0.20
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


def bar(index, close, *, open_price=None, high=None, low=None, volume=100):
    open_price = close if open_price is None else open_price
    return Bar(
        (index + 1) * 3600,
        open_price,
        max(open_price, close) + 0.5 if high is None else high,
        min(open_price, close) - 0.5 if low is None else low,
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


def quality(pattern, config):
    pole_strength = min(1.0, pattern["pole_strength_atr"] / (2 * config.min_pole_atr))
    pole_factor = (pole_strength + pattern["pole_efficiency"]) / 2
    retracement_mid = (config.min_retracement + config.max_retracement) / 2
    retracement_half = (config.max_retracement - config.min_retracement) / 2
    retracement_factor = max(
        0.0,
        min(1.0, 1 - abs(pattern["retracement"] - retracement_mid) / retracement_half),
    )
    duration_mid = (config.min_consolidation_bars + config.max_consolidation_bars) / 2
    duration_half = max(
        0.5, (config.max_consolidation_bars - config.min_consolidation_bars) / 2
    )
    duration_factor = max(
        0.0,
        min(1.0, 1 - abs(pattern["consolidation_bars"] - duration_mid) / duration_half),
    )
    breakout_factor = (
        max(0.0, min(1.0, pattern["breakout_strength_atr"]))
        if pattern["state"] == "CONFIRMED"
        else 0.0
    )
    result = (
        25 * pole_factor
        + 15 * retracement_factor
        + 10 * duration_factor
        + 25 * max(0.0, min(1.0, pattern["geometry_quality"]))
        + 15 * breakout_factor
        + (5 if pattern["volume_confirmed"] else 0)
        + (5 if pattern["momentum_confirmed"] else 0)
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
    for start in range(len(alternating) - 5, -1, -1):
        sequence = alternating[start : start + 5]
        kinds = tuple(item.kind for item in sequence)
        bullish = kinds == ("LOW", "HIGH", "LOW", "HIGH", "LOW")
        bearish = kinds == ("HIGH", "LOW", "HIGH", "LOW", "HIGH")
        if not bullish and not bearish:
            continue
        indices = [by_time.get(item.timestamp, -1) for item in sequence]
        if indices[0] < 0 or any(indices[i] <= indices[i - 1] for i in range(1, 5)):
            continue
        if [item.shift for item in sequence] != [len(bars) - index for index in indices]:
            continue
        p0, p1, p2, p3, p4 = sequence
        pole_bars = p0.shift - p1.shift
        consolidation_bars = p1.shift - p4.shift
        if not config.min_pole_bars <= pole_bars <= config.max_pole_bars:
            continue
        if not config.min_consolidation_bars <= consolidation_bars <= config.max_consolidation_bars:
            continue
        reference_atr = max(item.atr for item in sequence)
        pole_length = p1.price - p0.price if bullish else p0.price - p1.price
        pole_strength_atr = pole_length / reference_atr
        if pole_length <= 0 or pole_strength_atr < config.min_pole_atr:
            continue
        close_path = sum(
            abs(bars[index].close - bars[index - 1].close)
            for index in range(indices[0] + 1, indices[1] + 1)
        )
        if close_path <= 0:
            pole_efficiency = 0.0
        else:
            directional_close_move = (
                bars[indices[1]].close - bars[indices[0]].close
                if bullish
                else bars[indices[0]].close - bars[indices[1]].close
            )
            pole_efficiency = max(0.0, min(1.0, directional_close_move / close_path))
        if pole_efficiency < config.min_pole_efficiency:
            continue
        retracement_distance = (
            p1.price - min(p2.price, p4.price)
            if bullish
            else max(p2.price, p4.price) - p1.price
        )
        retracement = retracement_distance / pole_length
        if not config.min_retracement <= retracement <= config.max_retracement:
            continue
        if bullish:
            upper_slope = (p3.price - p1.price) / (indices[3] - indices[1])
            lower_slope = (p4.price - p2.price) / (indices[4] - indices[2])
            initial_width = p1.price - p2.price
            final_width = p3.price - p4.price
            upper_move = abs(p3.price - p1.price)
            lower_move = abs(p4.price - p2.price)
        else:
            upper_slope = (p4.price - p2.price) / (indices[4] - indices[2])
            lower_slope = (p3.price - p1.price) / (indices[3] - indices[1])
            initial_width = p2.price - p1.price
            final_width = p4.price - p3.price
            upper_move = abs(p4.price - p2.price)
            lower_move = abs(p3.price - p1.price)
        if initial_width <= 0 or final_width <= 0:
            continue
        if min(upper_move, lower_move) < config.min_geometry_move_atr * reference_atr:
            continue
        pattern_type = None
        geometry_quality = 0.0
        flag_geometry = (
            upper_slope < 0 and lower_slope < 0
            if bullish
            else upper_slope > 0 and lower_slope > 0
        )
        if flag_geometry:
            slope_ratio = min(abs(upper_slope), abs(lower_slope)) / max(
                abs(upper_slope), abs(lower_slope)
            )
            width_ratio = final_width / initial_width
            if (
                slope_ratio >= config.min_flag_parallel_ratio
                and 1 - config.flag_width_tolerance
                <= width_ratio
                <= 1 + config.flag_width_tolerance
            ):
                width_quality = max(
                    0.0,
                    min(1.0, 1 - abs(width_ratio - 1) / config.flag_width_tolerance),
                )
                geometry_quality = (slope_ratio + width_quality) / 2
                pattern_type = "BULL_FLAG" if bullish else "BEAR_FLAG"
        elif upper_slope < 0 and lower_slope > 0:
            width_ratio = final_width / initial_width
            convergence_balance = min(upper_move, lower_move) / max(upper_move, lower_move)
            if (
                width_ratio < 1
                and width_ratio <= config.max_pennant_width_ratio
                and convergence_balance >= config.min_pennant_convergence_balance
            ):
                geometry_quality = ((1 - width_ratio) + convergence_balance) / 2
                pattern_type = "BULL_PENNANT" if bullish else "BEAR_PENNANT"
        if pattern_type is None:
            continue
        result = {
            "detected": True,
            "type": pattern_type,
            "state": "CANDIDATE",
            "identity": f"{pattern_type}:" + ":".join(str(item.timestamp) for item in sequence),
            "pole_strength_atr": pole_strength_atr,
            "pole_efficiency": pole_efficiency,
            "retracement": retracement,
            "consolidation_bars": consolidation_bars,
            "geometry_quality": geometry_quality,
            "breakout_strength_atr": 0.0,
            "volume_confirmed": False,
            "momentum_confirmed": False,
            "event_time": 0,
        }
        upper_anchor_index = indices[3] if bullish else indices[4]
        lower_anchor_index = indices[4] if bullish else indices[3]
        upper_anchor = p3.price if bullish else p4.price
        lower_anchor = p4.price if bullish else p3.price
        bars_after = 0
        for current in range(indices[4] + 1, len(bars)):
            bars_after += 1
            item = bars[current]
            if bars_after > config.max_confirmation_bars:
                result.update(state="EXPIRED", event_time=item.timestamp)
                break
            upper = upper_anchor + upper_slope * (current - upper_anchor_index)
            lower = lower_anchor + lower_slope * (current - lower_anchor_index)
            if upper <= lower:
                result.update(state="EXPIRED", event_time=item.timestamp)
                break
            invalidated = (
                item.close < lower - config.invalidation_atr * reference_atr
                if bullish
                else item.close > upper + config.invalidation_atr * reference_atr
            )
            if invalidated:
                result.update(state="INVALIDATED", event_time=item.timestamp)
                break
            confirmed = (
                item.close > upper + config.breakout_buffer_atr * reference_atr
                if bullish
                else item.close < lower - config.breakout_buffer_atr * reference_atr
            )
            if confirmed:
                strength = (item.close - upper) / reference_atr if bullish else (lower - item.close) / reference_atr
                result.update(
                    state="CONFIRMED",
                    event_time=item.timestamp,
                    breakout_strength_atr=strength,
                    volume_confirmed=optional_volume(bars, current, config),
                    momentum_confirmed=config.use_momentum_quality
                    and ((item.close - item.open) if bullish else (item.open - item.close))
                    >= config.momentum_body_atr * reference_atr,
                )
                break
        result["quality"] = quality(result, config)
        return result
    return none


VALUES = {
    "BULL_FLAG": (("LOW", 100.0), ("HIGH", 106.0), ("LOW", 103.8), ("HIGH", 105.4), ("LOW", 103.2)),
    "BEAR_FLAG": (("HIGH", 110.0), ("LOW", 104.0), ("HIGH", 106.2), ("LOW", 104.6), ("HIGH", 106.8)),
    "BULL_PENNANT": (("LOW", 100.0), ("HIGH", 106.0), ("LOW", 102.8), ("HIGH", 105.0), ("LOW", 104.0)),
    "BEAR_PENNANT": (("HIGH", 110.0), ("LOW", 104.0), ("HIGH", 107.2), ("LOW", 105.0), ("HIGH", 106.0)),
}


def scenario(pattern_type, post=None, indices=(1, 5, 8, 11, 14)):
    post = list(post or [])
    total = indices[-1] + 1 + len(post)
    bullish = pattern_type.startswith("BULL")
    closes = [100.0 if bullish else 110.0] * total
    for index in range(indices[0], indices[1] + 1):
        fraction = (index - indices[0]) / (indices[1] - indices[0])
        closes[index] = 100.0 + 6.0 * fraction if bullish else 110.0 - 6.0 * fraction
    for index in range(indices[1] + 1, total):
        closes[index] = 104.2 if bullish else 105.8
    bars = [bar(index, close) for index, close in enumerate(closes)]
    for index, supplied in enumerate(post, start=indices[-1] + 1):
        bars[index] = replace(supplied, timestamp=bars[index].timestamp)
    pivots = [
        Pivot(kind, price, total - index, bars[index].timestamp)
        for (kind, price), index in zip(VALUES[pattern_type], indices)
    ]
    return pivots, bars


class ContinuationPatternTests(unittest.TestCase):
    def test_valid_bull_flag(self):
        result = detect(*scenario("BULL_FLAG", [bar(0, 105.3, open_price=104.0, volume=180)]))
        self.assertEqual((result["type"], result["state"]), ("BULL_FLAG", "CONFIRMED"))

    def test_valid_bear_flag(self):
        result = detect(*scenario("BEAR_FLAG", [bar(0, 104.7, open_price=106.0, volume=180)]))
        self.assertEqual((result["type"], result["state"]), ("BEAR_FLAG", "CONFIRMED"))

    def test_valid_bull_pennant(self):
        result = detect(*scenario("BULL_PENNANT", [bar(0, 104.6, open_price=103.8, volume=180)]))
        self.assertEqual((result["type"], result["state"]), ("BULL_PENNANT", "CONFIRMED"))

    def test_valid_bear_pennant(self):
        result = detect(*scenario("BEAR_PENNANT", [bar(0, 105.4, open_price=106.2, volume=180)]))
        self.assertEqual((result["type"], result["state"]), ("BEAR_PENNANT", "CONFIRMED"))

    def test_insufficient_flagpole_is_rejected(self):
        pivots, bars = scenario("BULL_FLAG")
        pivots[1] = replace(pivots[1], price=102.9)
        self.assertFalse(detect(pivots, bars)["detected"])

    def test_flagpole_made_only_of_wicks_is_rejected(self):
        pivots, bars = scenario("BULL_FLAG")
        for index in range(1, 6):
            bars[index] = replace(
                bars[index],
                open=100.0,
                high=max(100.5, bars[index].high),
                low=min(99.5, bars[index].low),
                close=100.0,
            )
        self.assertFalse(detect(pivots, bars)["detected"])

    def test_excessive_retracement_is_rejected(self):
        pivots, bars = scenario("BULL_FLAG")
        pivots[4] = replace(pivots[4], price=101.5)
        self.assertFalse(detect(pivots, bars)["detected"])

    def test_non_parallel_flag_channel_is_rejected(self):
        pivots, bars = scenario("BULL_FLAG")
        pivots[4] = replace(pivots[4], price=103.7)
        self.assertFalse(detect(pivots, bars)["detected"])

    def test_pennant_without_real_contraction_is_rejected(self):
        pivots, bars = scenario("BULL_PENNANT")
        pivots[4] = replace(pivots[4], price=102.5)
        self.assertFalse(detect(pivots, bars)["detected"])

    def test_wick_only_does_not_confirm(self):
        wick = bar(0, 104.2, high=106.0, low=103.5)
        result = detect(*scenario("BULL_FLAG", [wick]))
        self.assertEqual(result["state"], "CANDIDATE")

    def test_opposite_breakout_invalidates(self):
        result = detect(*scenario("BULL_FLAG", [bar(0, 102.5)]))
        self.assertEqual(result["state"], "INVALIDATED")

    def test_pattern_expires_before_late_breakout(self):
        safe = [bar(0, 104.05 - 0.1 * index) for index in range(13)]
        safe.append(bar(0, 106.0))
        result = detect(*scenario("BULL_FLAG", safe))
        self.assertEqual(result["state"], "EXPIRED")

    def test_bear_pattern_can_be_invalidated(self):
        result = detect(*scenario("BEAR_PENNANT", [bar(0, 106.2)]))
        self.assertEqual(result["state"], "INVALIDATED")

    def test_invalidated_identity_cannot_confirm_later(self):
        result = detect(*scenario("BULL_FLAG", [bar(0, 102.5), bar(0, 106.0)]))
        self.assertEqual(result["state"], "INVALIDATED")

    def test_identity_is_stable_for_deduplication(self):
        pivots, bars = scenario("BEAR_FLAG")
        first = detect(pivots, bars)
        second = detect(pivots, bars)
        self.assertEqual(first["identity"], second["identity"])

    def test_confirmation_does_not_repaint(self):
        confirmed = bar(0, 105.3, open_price=104.0, volume=180)
        first = detect(*scenario("BULL_FLAG", [confirmed]))
        later = detect(*scenario("BULL_FLAG", [confirmed, bar(0, 101.0)]))
        self.assertEqual(first["state"], "CONFIRMED")
        self.assertEqual(later["state"], "CONFIRMED")
        self.assertEqual(first["event_time"], later["event_time"])
        self.assertEqual(first["quality"], later["quality"])

    def test_high_quality(self):
        result = detect(*scenario("BULL_FLAG", [bar(0, 106.2, open_price=104.0, volume=180)]))
        self.assertEqual(quality_name(result["quality"]), "HIGH")

    def test_medium_quality(self):
        result = detect(*scenario("BULL_FLAG"))
        self.assertEqual(quality_name(result["quality"]), "MEDIUM")

    def test_low_quality(self):
        indices = (1, 3, 5, 7, 9)
        pivots, bars = scenario("BULL_FLAG", indices=indices)
        custom = (100.0, 103.0, 102.7, 102.8, 102.5)
        pivots = [replace(item, price=price) for item, price in zip(pivots, custom)]
        for index in range(indices[0], indices[1] + 1):
            fraction = (index - indices[0]) / (indices[1] - indices[0])
            bars[index] = replace(bars[index], close=100.0 + 3.0 * fraction)
        result = detect(pivots, bars)
        self.assertTrue(result["detected"])
        self.assertEqual(quality_name(result["quality"]), "LOW")

    def test_missing_volume_and_momentum_do_not_block_confirmation(self):
        breakout = bar(0, 105.3, open_price=105.1, volume=0)
        result = detect(*scenario("BULL_FLAG", [breakout]))
        self.assertEqual(result["state"], "CONFIRMED")
        self.assertFalse(result["volume_confirmed"])
        self.assertFalse(result["momentum_confirmed"])

    def test_unconfirmed_pivot_cannot_form_pattern(self):
        pivots, bars = scenario("BEAR_FLAG")
        pivots[4] = replace(pivots[4], confirmed=False)
        self.assertFalse(detect(pivots, bars)["detected"])

    def test_flat_range_is_not_a_flag(self):
        pivots, bars = scenario("BULL_FLAG")
        flat = (100.0, 106.0, 104.0, 106.0, 104.0)
        pivots = [replace(item, price=price) for item, price in zip(pivots, flat)]
        self.assertFalse(detect(pivots, bars)["detected"])


class ContinuationPatternSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ea = EA_PATH.read_text(encoding="utf-8")
        cls.structure = STRUCTURE_PATH.read_text(encoding="utf-8")

    def test_closed_bars_confirm_and_all_states_exist(self):
        self.assertIn("CopyRates(symbol,timeframe,1,barsToLoad,closedRates)", self.structure)
        for state in ("CANDIDATE", "CONFIRMED", "INVALIDATED", "EXPIRED"):
            self.assertIn(f"GOLDSCOUT_PATTERN_STATE_{state}", self.structure)
        for pattern_type in ("BULL_FLAG", "BEAR_FLAG", "BULL_PENNANT", "BEAR_PENNANT"):
            self.assertIn(f"GOLDSCOUT_CONTINUATION_{pattern_type}", self.structure)

    def test_continuation_patterns_never_enter_scoring(self):
        start = self.ea.index("int longScore=0, shortScore=0;")
        end = self.ea.index("longScore=(int)MathMin(100,longScore);", start)
        scoring = self.ea[start:end]
        self.assertNotIn("Continuation", scoring)
        self.assertNotIn("continuation", scoring)
        self.assertIn("input int    ArmScoreThreshold       = 58;", self.ea)
        self.assertIn("input int    MinScoreToTrade         = 74;", self.ea)
        self.assertIn("input bool   EnableLiveTrading      = false;", self.ea)
        self.assertIn("input double RiskPercent             = 5.0;", self.ea)

    def test_logging_is_optional_rate_limited_and_deduplicated(self):
        self.assertIn("input bool   DebugContinuationPatternLogs     = false;", self.ea)
        self.assertIn("const int CONTINUATION_PATTERN_LOG_INTERVAL_SECONDS=30;", self.ea)
        self.assertIn("if(signature==g_lastContinuationPatternLogSignature) return;", self.ea)
        self.assertIn('PrintFormat("[GoldScout][STRUCTURE] pattern=%s | state=%s', self.ea)


if __name__ == "__main__":
    unittest.main()
