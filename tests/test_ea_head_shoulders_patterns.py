"""Deterministic diagnostics for HCH and inverted HCH patterns."""

from dataclasses import dataclass, replace
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
EA_PATH = ROOT / "MT5" / "Experts" / "XAU_GoldScout_H1.mq5"
STRUCTURE_PATH = ROOT / "MT5" / "Include" / "GoldScout" / "MarketStructure.mqh"


@dataclass(frozen=True)
class Config:
    shoulder_tolerance_atr: float = 0.75
    min_head_prominence_atr: float = 0.75
    min_depth_atr: float = 2.0
    min_pivot_bars: int = 3
    max_pivot_bars: int = 24
    min_temporal_balance: float = 0.50
    max_neckline_slope_atr: float = 0.15
    max_confirmation_bars: int = 12
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


VALUES = {
    "HCH": (
        ("HIGH", 108.0), ("LOW", 100.0), ("HIGH", 112.0),
        ("LOW", 100.2), ("HIGH", 108.2),
    ),
    "HCH_INVERTED": (
        ("LOW", 100.0), ("HIGH", 108.0), ("LOW", 96.0),
        ("HIGH", 107.8), ("LOW", 100.2),
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
    shoulder_symmetry = max(
        0.0,
        min(1.0, 1 - pattern["shoulder_difference"] / pattern["shoulder_tolerance"]),
    )
    prominence = max(
        0.0,
        min(
            1.0,
            pattern["head_prominence"]
            / (2 * config.min_head_prominence_atr * pattern["reference_atr"]),
        ),
    )
    neckline = max(
        0.0,
        min(1.0, 1 - abs(pattern["neckline_slope_atr"]) / config.max_neckline_slope_atr),
    )
    depth = max(
        0.0,
        min(
            1.0,
            pattern["depth"] / (2 * config.min_depth_atr * pattern["reference_atr"]),
        ),
    )
    breakout = (
        max(0.0, min(1.0, pattern["breakout_strength_atr"]))
        if pattern["state"] == "CONFIRMED"
        else 0.0
    )
    result = (
        20 * shoulder_symmetry
        + 20 * prominence
        + 15 * max(0.0, min(1.0, pattern["temporal_balance"]))
        + 15 * neckline
        + 15 * depth
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
    for start in range(len(alternating) - 5, -1, -1):
        sequence = alternating[start : start + 5]
        kinds = tuple(item.kind for item in sequence)
        bearish = kinds == ("HIGH", "LOW", "HIGH", "LOW", "HIGH")
        bullish = kinds == ("LOW", "HIGH", "LOW", "HIGH", "LOW")
        if not bearish and not bullish:
            continue
        indices = [by_time.get(item.timestamp, -1) for item in sequence]
        if indices[0] < 0 or any(indices[index] <= indices[index - 1] for index in range(1, 5)):
            continue
        if [item.shift for item in sequence] != [len(bars) - index for index in indices]:
            continue
        legs = [indices[index] - indices[index - 1] for index in range(1, 5)]
        if any(not config.min_pivot_bars <= leg <= config.max_pivot_bars for leg in legs):
            continue
        left_span = indices[2] - indices[0]
        right_span = indices[4] - indices[2]
        temporal_balance = min(left_span, right_span) / max(left_span, right_span)
        if temporal_balance < config.min_temporal_balance:
            continue
        reference_atr = max(item.atr for item in sequence)
        shoulder_tolerance = max(0.01, max(sequence[0].atr, sequence[4].atr) * config.shoulder_tolerance_atr)
        shoulder_difference = abs(sequence[0].price - sequence[4].price)
        if shoulder_difference > shoulder_tolerance:
            continue
        head_prominence = (
            min(sequence[2].price - sequence[0].price, sequence[2].price - sequence[4].price)
            if bearish
            else min(sequence[0].price - sequence[2].price, sequence[4].price - sequence[2].price)
        )
        if head_prominence < config.min_head_prominence_atr * reference_atr:
            continue
        neckline_slope = (sequence[3].price - sequence[1].price) / (indices[3] - indices[1])
        neckline_slope_atr = neckline_slope / reference_atr
        if abs(neckline_slope_atr) > config.max_neckline_slope_atr:
            continue
        neckline_intercept = sequence[1].price - neckline_slope * indices[1]
        neckline_at_head = neckline_intercept + neckline_slope * indices[2]
        depth = sequence[2].price - neckline_at_head if bearish else neckline_at_head - sequence[2].price
        if depth < config.min_depth_atr * reference_atr:
            continue
        neckline_at_left = neckline_intercept + neckline_slope * indices[0]
        neckline_at_right = neckline_intercept + neckline_slope * indices[4]
        shoulders_valid = (
            sequence[0].price > neckline_at_left + 0.01
            and sequence[4].price > neckline_at_right + 0.01
            if bearish
            else sequence[0].price < neckline_at_left - 0.01
            and sequence[4].price < neckline_at_right - 0.01
        )
        if not shoulders_valid:
            continue
        pattern_type = "HCH" if bearish else "HCH_INVERTED"
        result = {
            "detected": True,
            "type": pattern_type,
            "state": "CANDIDATE",
            "identity": f"{pattern_type}:" + ":".join(str(item.timestamp) for item in sequence),
            "reference_atr": reference_atr,
            "shoulder_tolerance": shoulder_tolerance,
            "shoulder_difference": shoulder_difference,
            "head_prominence": head_prominence,
            "depth": depth,
            "temporal_balance": temporal_balance,
            "neckline_slope_atr": neckline_slope_atr,
            "breakout_direction": 0,
            "breakout_strength_atr": 0.0,
            "volume_confirmed": False,
            "momentum_confirmed": False,
            "event_time": 0,
        }
        breakout_buffer = max(0.01, config.breakout_buffer_atr * reference_atr)
        invalidation_buffer = max(0.01, config.invalidation_atr * reference_atr)
        bars_after = 0
        for current in range(indices[4] + 1, len(bars)):
            bars_after += 1
            item = bars[current]
            if bars_after > config.max_confirmation_bars:
                result.update(state="EXPIRED", event_time=item.timestamp)
                break
            neckline = neckline_intercept + neckline_slope * current
            invalidated = (
                item.close > sequence[2].price + invalidation_buffer
                if bearish
                else item.close < sequence[2].price - invalidation_buffer
            )
            if invalidated:
                result.update(state="INVALIDATED", event_time=item.timestamp)
                break
            confirmed = (
                item.close < neckline - breakout_buffer
                if bearish
                else item.close > neckline + breakout_buffer
            )
            if confirmed:
                direction = -1 if bearish else 1
                strength = (
                    (neckline - item.close) / reference_atr
                    if bearish
                    else (item.close - neckline) / reference_atr
                )
                directional_body = item.open - item.close if bearish else item.close - item.open
                result.update(
                    state="CONFIRMED",
                    event_time=item.timestamp,
                    breakout_direction=direction,
                    breakout_strength_atr=strength,
                    volume_confirmed=optional_volume(bars, current, config),
                    momentum_confirmed=config.use_momentum_quality
                    and directional_body >= config.momentum_body_atr * reference_atr,
                )
                break
        result["quality"] = quality(result, config)
        return result
    return none


def scenario(pattern_type, post=None, indices=(1, 5, 9, 13, 17), values=None):
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


class HeadShouldersPatternTests(unittest.TestCase):
    def test_valid_hch(self):
        result = detect(*scenario("HCH", [bar(0, 99.0, open_price=101.0, volume=180)]))
        self.assertEqual((result["type"], result["state"], result["breakout_direction"]),
                         ("HCH", "CONFIRMED", -1))

    def test_valid_inverted_hch(self):
        result = detect(*scenario("HCH_INVERTED", [bar(0, 109.0, open_price=107.0, volume=180)]))
        self.assertEqual((result["type"], result["state"], result["breakout_direction"]),
                         ("HCH_INVERTED", "CONFIRMED", 1))

    def test_insufficient_head_is_rejected(self):
        pivots, bars = scenario("HCH")
        pivots[2] = replace(pivots[2], price=108.6)
        self.assertFalse(detect(pivots, bars)["detected"])

    def test_unbalanced_shoulders_are_rejected(self):
        pivots, bars = scenario("HCH")
        pivots[4] = replace(pivots[4], price=110.0)
        self.assertFalse(detect(pivots, bars)["detected"])

    def test_excessive_neckline_slope_is_rejected(self):
        pivots, bars = scenario("HCH")
        pivots[3] = replace(pivots[3], price=102.0)
        self.assertFalse(detect(pivots, bars)["detected"])

    def test_wick_without_close_does_not_confirm(self):
        wick = bar(0, 105.0, high=106.0, low=98.0)
        result = detect(*scenario("HCH", [wick]))
        self.assertEqual(result["state"], "CANDIDATE")

    def test_candidate_without_breakout(self):
        result = detect(*scenario("HCH"))
        self.assertEqual((result["state"], result["breakout_direction"]),
                         ("CANDIDATE", 0))

    def test_pattern_expires(self):
        result = detect(*scenario("HCH", [bar(0, 105.0) for _ in range(13)]))
        self.assertEqual(result["state"], "EXPIRED")

    def test_pattern_invalidates_beyond_head(self):
        result = detect(*scenario("HCH", [bar(0, 113.0)]))
        self.assertEqual(result["state"], "INVALIDATED")

    def test_identity_is_stable_for_deduplication(self):
        pivots, bars = scenario("HCH_INVERTED")
        self.assertEqual(detect(pivots, bars)["identity"], detect(pivots, bars)["identity"])

    def test_confirmation_does_not_repaint(self):
        breakout = bar(0, 99.0, open_price=101.0, volume=180)
        first = detect(*scenario("HCH", [breakout]))
        later = detect(*scenario("HCH", [breakout, bar(0, 114.0)]))
        self.assertEqual(first["state"], "CONFIRMED")
        self.assertEqual(later["state"], "CONFIRMED")
        self.assertEqual(first["event_time"], later["event_time"])
        self.assertEqual(first["quality"], later["quality"])

    def test_high_quality_is_bounded(self):
        result = detect(*scenario("HCH", [bar(0, 99.0, open_price=101.0, volume=180)]))
        self.assertEqual(quality_name(result["quality"]), "HIGH")
        self.assertLessEqual(result["quality"], 100.0)

    def test_medium_quality(self):
        pattern = {
            "reference_atr": 1.0, "shoulder_tolerance": 0.75,
            "shoulder_difference": 0.30, "head_prominence": 1.0,
            "depth": 3.0, "temporal_balance": 0.75,
            "neckline_slope_atr": 0.075, "state": "CANDIDATE",
            "breakout_strength_atr": 0.0, "volume_confirmed": False,
            "momentum_confirmed": False,
        }
        self.assertEqual(quality_name(quality(pattern, Config())), "MEDIUM")

    def test_low_quality(self):
        pattern = {
            "reference_atr": 1.0, "shoulder_tolerance": 0.75,
            "shoulder_difference": 0.70, "head_prominence": 0.75,
            "depth": 2.0, "temporal_balance": 0.50,
            "neckline_slope_atr": 0.145, "state": "CANDIDATE",
            "breakout_strength_atr": 0.0, "volume_confirmed": False,
            "momentum_confirmed": False,
        }
        self.assertEqual(quality_name(quality(pattern, Config())), "LOW")
        self.assertGreaterEqual(quality(pattern, Config()), 0.0)

    def test_volume_and_momentum_are_optional(self):
        breakout = bar(0, 99.0, open_price=99.2, volume=0)
        result = detect(*scenario("HCH", [breakout]))
        self.assertEqual(result["state"], "CONFIRMED")
        self.assertFalse(result["volume_confirmed"])
        self.assertFalse(result["momentum_confirmed"])

    def test_compressed_pattern_is_rejected(self):
        self.assertFalse(detect(*scenario("HCH", indices=(1, 2, 3, 4, 5)))["detected"])

    def test_overlong_pattern_is_rejected(self):
        self.assertFalse(
            detect(*scenario("HCH", indices=(1, 26, 51, 76, 101)))["detected"]
        )

    def test_unconfirmed_pivot_cannot_form_pattern(self):
        pivots, bars = scenario("HCH_INVERTED")
        pivots[2] = replace(pivots[2], confirmed=False)
        self.assertFalse(detect(pivots, bars)["detected"])


class HeadShouldersPatternSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ea = EA_PATH.read_text(encoding="utf-8")
        cls.structure = STRUCTURE_PATH.read_text(encoding="utf-8")

    def test_closed_bars_five_pivots_and_states_exist(self):
        self.assertIn("CopyRates(symbol,timeframe,1,barsToLoad,closedRates)", self.structure)
        self.assertIn("ArraySize(alternating)-5", self.structure)
        self.assertIn("GOLDSCOUT_HEAD_SHOULDERS_HCH", self.structure)
        self.assertIn("GOLDSCOUT_HEAD_SHOULDERS_INVERTED", self.structure)
        for state in ("CANDIDATE", "CONFIRMED", "INVALIDATED", "EXPIRED"):
            self.assertIn(f"GOLDSCOUT_PATTERN_STATE_{state}", self.structure)

    def test_hch_patterns_never_enter_scoring(self):
        start = self.ea.index("int longScore=0, shortScore=0;")
        end = self.ea.index("longScore=(int)MathMin(100,longScore);", start)
        scoring = self.ea[start:end]
        self.assertNotIn("HeadShoulders", scoring)
        self.assertNotIn("headShoulders", scoring)
        self.assertIn("input int    ArmScoreThreshold       = 58;", self.ea)
        self.assertIn("input int    MinScoreToTrade         = 74;", self.ea)
        self.assertIn("input bool   EnableLiveTrading      = false;", self.ea)
        self.assertIn("input double RiskPercent             = 5.0;", self.ea)

    def test_optional_logging_is_deduplicated_and_rate_limited(self):
        self.assertIn("input bool   DebugHeadShouldersPatternLogs    = false;", self.ea)
        self.assertIn("HEAD_SHOULDERS_PATTERN_LOG_INTERVAL_SECONDS=30", self.ea)
        self.assertIn("if(signature==g_lastHeadShouldersPatternLogSignature) return;", self.ea)


if __name__ == "__main__":
    unittest.main()
