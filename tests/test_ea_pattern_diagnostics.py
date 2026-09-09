"""Deterministic tests for diagnostic W/M detection from confirmed pivots."""

from dataclasses import dataclass, replace
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
EA_PATH = ROOT / "MT5" / "Experts" / "XAU_GoldScout_H1.mq5"
STRUCTURE_PATH = ROOT / "MT5" / "Include" / "GoldScout" / "MarketStructure.mqh"


@dataclass(frozen=True)
class PatternConfig:
    extreme_tolerance_atr: float = 0.30
    min_depth_atr: float = 1.0
    min_pivot_bars: int = 3
    max_pivot_bars: int = 36
    max_confirmation_bars: int = 24
    breakout_buffer_atr: float = 0.05
    invalidation_atr: float = 0.25
    min_leg_balance: float = 0.25
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


def make_bar(index, close, *, open_price=None, high=None, low=None, volume=100):
    open_price = close if open_price is None else open_price
    high = max(open_price, close) + 0.5 if high is None else high
    low = min(open_price, close) - 0.5 if low is None else low
    return Bar((index + 1) * 3600, open_price, high, low, close, volume)


def normalize(pivots):
    alternating = []
    previous_time = None
    previous_shift = None
    for pivot in pivots:
        if not pivot.confirmed:
            continue
        if (
            pivot.kind not in {"HIGH", "LOW"}
            or pivot.price <= 0
            or pivot.atr <= 0
            or pivot.timestamp <= 0
            or pivot.shift < 1
        ):
            return None
        if previous_time is not None and (
            pivot.timestamp <= previous_time or pivot.shift >= previous_shift
        ):
            return None
        previous_time = pivot.timestamp
        previous_shift = pivot.shift
        if alternating and alternating[-1].kind == pivot.kind:
            more_extreme = (
                pivot.price > alternating[-1].price
                if pivot.kind == "HIGH"
                else pivot.price < alternating[-1].price
            )
            if more_extreme:
                alternating[-1] = pivot
        else:
            alternating.append(pivot)
    return alternating


def volume_confirmation(bars, breakout_index, config):
    if not config.use_volume_quality or breakout_index <= 0:
        return False
    previous = [
        bar.volume
        for bar in bars[max(0, breakout_index - config.volume_lookback) : breakout_index]
        if bar.volume > 0
    ]
    return bool(
        previous
        and bars[breakout_index].volume > 0
        and bars[breakout_index].volume
        >= (sum(previous) / len(previous)) * config.volume_multiplier
    )


def pattern_quality(pattern, config):
    if not pattern["detected"]:
        return 0.0
    similarity = max(
        0.0,
        min(
            1.0,
            1.0
            - abs(pattern["first"].price - pattern["second"].price)
            / pattern["tolerance"],
        ),
    )
    minimum_depth = config.min_depth_atr * pattern["reference_atr"]
    depth_score = max(0.0, min(1.0, pattern["depth"] / (2.0 * minimum_depth)))
    balance = min(pattern["first_leg"], pattern["second_leg"]) / max(
        pattern["first_leg"], pattern["second_leg"]
    )
    breakout_score = (
        max(0.0, min(1.0, pattern["breakout_strength_atr"]))
        if pattern["state"] == "CONFIRMED"
        else 0.0
    )
    quality = 25 * similarity + 25 * depth_score + 20 * balance + 20 * breakout_score
    quality += 5 if pattern["volume_confirmed"] else 0
    quality += 5 if pattern["momentum_confirmed"] else 0
    return max(0.0, min(100.0, quality))


def quality_name(quality):
    return "HIGH" if quality >= 75 else "MEDIUM" if quality >= 50 else "LOW"


def detect_latest_pattern(pivots, bars, config=PatternConfig(), minimum_step=0.01):
    none = {"detected": False, "type": "NONE", "state": "NONE", "quality": 0.0}
    if minimum_step <= 0 or len(bars) < 3:
        return None
    if any(
        bar.timestamp <= 0
        or bar.open <= 0
        or bar.high < max(bar.open, bar.close)
        or bar.low > min(bar.open, bar.close)
        or bar.volume < 0
        for bar in bars
    ):
        return None
    if any(bars[index].timestamp <= bars[index - 1].timestamp for index in range(1, len(bars))):
        return None

    alternating = normalize(pivots)
    if alternating is None:
        return None
    index_by_time = {bar.timestamp: index for index, bar in enumerate(bars)}
    for start in range(len(alternating) - 3, -1, -1):
        first, neckline, second = alternating[start : start + 3]
        kinds = (first.kind, neckline.kind, second.kind)
        pattern_type = "W" if kinds == ("LOW", "HIGH", "LOW") else "M" if kinds == ("HIGH", "LOW", "HIGH") else None
        if pattern_type is None:
            continue
        indices = [index_by_time.get(item.timestamp, -1) for item in (first, neckline, second)]
        if indices[0] < 0 or not indices[0] < indices[1] < indices[2]:
            continue
        expected_shifts = [len(bars) - index for index in indices]
        if [first.shift, neckline.shift, second.shift] != expected_shifts:
            continue
        first_leg = first.shift - neckline.shift
        second_leg = neckline.shift - second.shift
        if not all(
            config.min_pivot_bars <= leg <= config.max_pivot_bars
            for leg in (first_leg, second_leg)
        ):
            continue
        if min(first_leg, second_leg) / max(first_leg, second_leg) < config.min_leg_balance:
            continue
        tolerance = max(minimum_step, max(first.atr, second.atr) * config.extreme_tolerance_atr)
        if abs(first.price - second.price) > tolerance:
            continue
        reference_atr = max(first.atr, neckline.atr, second.atr)
        depth = (
            min(neckline.price - first.price, neckline.price - second.price)
            if pattern_type == "W"
            else min(first.price - neckline.price, second.price - neckline.price)
        )
        if depth < config.min_depth_atr * reference_atr:
            continue

        result = {
            "detected": True,
            "type": pattern_type,
            "state": "CANDIDATE",
            "first": first,
            "neckline": neckline,
            "second": second,
            "event_time": 0,
            "first_leg": first_leg,
            "second_leg": second_leg,
            "bars_after_second": 0,
            "reference_atr": reference_atr,
            "tolerance": tolerance,
            "depth": depth,
            "breakout_strength_atr": 0.0,
            "volume_confirmed": False,
            "momentum_confirmed": False,
            "identity": f"{pattern_type}:{first.timestamp}:{neckline.timestamp}:{second.timestamp}",
        }
        for bar_index in range(indices[2] + 1, len(bars)):
            result["bars_after_second"] += 1
            bar = bars[bar_index]
            if result["bars_after_second"] > config.max_confirmation_bars:
                result["state"] = "EXPIRED"
                result["event_time"] = bar.timestamp
                break
            invalidated = (
                bar.close < min(first.price, second.price) - config.invalidation_atr * reference_atr
                if pattern_type == "W"
                else bar.close > max(first.price, second.price) + config.invalidation_atr * reference_atr
            )
            if invalidated:
                result["state"] = "INVALIDATED"
                result["event_time"] = bar.timestamp
                break
            confirmed = (
                bar.close > neckline.price + config.breakout_buffer_atr * reference_atr
                if pattern_type == "W"
                else bar.close < neckline.price - config.breakout_buffer_atr * reference_atr
            )
            if confirmed:
                result["state"] = "CONFIRMED"
                result["event_time"] = bar.timestamp
                result["breakout_strength_atr"] = (
                    (bar.close - neckline.price) / reference_atr
                    if pattern_type == "W"
                    else (neckline.price - bar.close) / reference_atr
                )
                result["volume_confirmed"] = volume_confirmation(bars, bar_index, config)
                directional_body = bar.close - bar.open if pattern_type == "W" else bar.open - bar.close
                result["momentum_confirmed"] = bool(
                    config.use_momentum_quality
                    and directional_body >= config.momentum_body_atr * reference_atr
                )
                break
        result["quality"] = pattern_quality(result, config)
        return result
    return none


def scenario(pattern_type="W", post_bars=None, *, first_gap=4, second_gap=4, second_offset=0.1):
    post_bars = list(post_bars or [])
    first_index = 1
    neckline_index = first_index + first_gap
    second_index = neckline_index + second_gap
    total = second_index + 1 + len(post_bars)
    base_close = 102.0 if pattern_type == "W" else 108.0
    bars = [make_bar(index, base_close) for index in range(total)]
    if pattern_type == "W":
        values = (("LOW", 100.0), ("HIGH", 104.0), ("LOW", 100.0 + second_offset))
    else:
        values = (("HIGH", 110.0), ("LOW", 106.0), ("HIGH", 110.0 - second_offset))
    pivot_indices = (first_index, neckline_index, second_index)
    pivots = [
        Pivot(kind, price, total - index, bars[index].timestamp)
        for (kind, price), index in zip(values, pivot_indices)
    ]
    for offset, bar in enumerate(post_bars, start=second_index + 1):
        bars[offset] = replace(bar, timestamp=bars[offset].timestamp)
    return pivots, bars


class PatternDetectionTests(unittest.TestCase):
    def test_valid_w_is_confirmed_by_closed_breakout(self):
        breakout = make_bar(0, 105.2, open_price=103.8, volume=180)
        pattern = detect_latest_pattern(*scenario("W", [breakout]))
        self.assertEqual((pattern["type"], pattern["state"]), ("W", "CONFIRMED"))

    def test_valid_m_is_confirmed_by_closed_breakout(self):
        breakout = make_bar(0, 104.8, open_price=106.2, volume=180)
        pattern = detect_latest_pattern(*scenario("M", [breakout]))
        self.assertEqual((pattern["type"], pattern["state"]), ("M", "CONFIRMED"))

    def test_w_and_m_remain_candidates_without_breakout(self):
        for pattern_type, close in (("W", 103.8), ("M", 106.2)):
            with self.subTest(pattern_type=pattern_type):
                pattern = detect_latest_pattern(*scenario(pattern_type, [make_bar(0, close)]))
                self.assertEqual(pattern["state"], "CANDIDATE")

    def test_neckline_wick_without_close_does_not_confirm(self):
        for pattern_type, close, high, low in (
            ("W", 103.8, 105.0, 103.0),
            ("M", 106.2, 107.0, 105.0),
        ):
            with self.subTest(pattern_type=pattern_type):
                wick = make_bar(0, close, high=high, low=low)
                pattern = detect_latest_pattern(*scenario(pattern_type, [wick]))
                self.assertEqual(pattern["state"], "CANDIDATE")

    def test_extremes_outside_atr_tolerance_are_rejected(self):
        for pattern_type in ("W", "M"):
            with self.subTest(pattern_type=pattern_type):
                pattern = detect_latest_pattern(*scenario(pattern_type, second_offset=0.31))
                self.assertFalse(pattern["detected"])

    def test_insufficient_depth_is_rejected(self):
        for pattern_type in ("W", "M"):
            pivots, bars = scenario(pattern_type)
            shallow_neckline = 100.8 if pattern_type == "W" else 109.2
            pivots[1] = replace(pivots[1], price=shallow_neckline)
            with self.subTest(pattern_type=pattern_type):
                self.assertFalse(detect_latest_pattern(pivots, bars)["detected"])

    def test_pivots_too_close_are_rejected(self):
        pattern = detect_latest_pattern(*scenario("W", first_gap=2, second_gap=4))
        self.assertFalse(pattern["detected"])

    def test_pivots_too_far_are_rejected(self):
        pattern = detect_latest_pattern(*scenario("M", first_gap=4, second_gap=37))
        self.assertFalse(pattern["detected"])

    def test_unbalanced_legs_reject_deformed_pattern(self):
        pattern = detect_latest_pattern(*scenario("W", first_gap=3, second_gap=13))
        self.assertFalse(pattern["detected"])

    def test_duplicate_same_type_pivot_does_not_duplicate_pattern(self):
        pivots, bars = scenario("W")
        duplicate_index = 2
        duplicate = Pivot(
            "LOW",
            100.2,
            len(bars) - duplicate_index,
            bars[duplicate_index].timestamp,
        )
        pivots.insert(1, duplicate)
        pattern = detect_latest_pattern(pivots, bars)
        self.assertTrue(pattern["detected"])
        self.assertEqual(pattern["first"].timestamp, pivots[0].timestamp)
        self.assertEqual(pattern["type"], "W")

    def test_valid_pattern_can_be_invalidated_before_breakout(self):
        for pattern_type, close in (("W", 99.6), ("M", 110.4)):
            with self.subTest(pattern_type=pattern_type):
                pattern = detect_latest_pattern(*scenario(pattern_type, [make_bar(0, close)]))
                self.assertEqual(pattern["state"], "INVALIDATED")

    def test_candidate_expires_before_a_late_breakout(self):
        for pattern_type, quiet, breakout in (("W", 103.0, 105.0), ("M", 107.0, 105.0)):
            post = [make_bar(0, quiet) for _ in range(24)] + [make_bar(0, breakout)]
            with self.subTest(pattern_type=pattern_type):
                pattern = detect_latest_pattern(*scenario(pattern_type, post))
                self.assertEqual(pattern["state"], "EXPIRED")

    def test_confirmation_does_not_repaint_after_later_invalidation(self):
        confirmed = make_bar(0, 105.2, open_price=103.8, volume=180)
        first = detect_latest_pattern(*scenario("W", [confirmed]))
        later = detect_latest_pattern(*scenario("W", [confirmed, make_bar(0, 99.0)]))
        self.assertEqual(first["state"], "CONFIRMED")
        self.assertEqual(later["state"], "CONFIRMED")
        self.assertEqual(first["event_time"], later["event_time"])
        self.assertEqual(first["quality"], later["quality"])

    def test_same_pattern_has_stable_identity_for_deduplication(self):
        pivots, bars = scenario("M", [make_bar(0, 104.8)])
        first = detect_latest_pattern(pivots, bars)
        second = detect_latest_pattern(pivots, bars)
        self.assertEqual(first["identity"], second["identity"])
        self.assertEqual(first["state"], second["state"])

    def test_missing_optional_volume_and_momentum_do_not_reject_pattern(self):
        breakout = make_bar(0, 105.2, open_price=105.0, volume=0)
        pattern = detect_latest_pattern(*scenario("W", [breakout]))
        self.assertEqual(pattern["state"], "CONFIRMED")
        self.assertFalse(pattern["volume_confirmed"])
        self.assertFalse(pattern["momentum_confirmed"])

    def test_high_quality_pattern(self):
        breakout = make_bar(0, 105.2, open_price=103.8, volume=180)
        pattern = detect_latest_pattern(*scenario("W", [breakout], second_offset=0.0))
        self.assertEqual(quality_name(pattern["quality"]), "HIGH")
        self.assertEqual(pattern["quality"], 100.0)

    def test_medium_quality_pattern(self):
        pattern = detect_latest_pattern(*scenario("W", second_offset=0.0))
        self.assertEqual(quality_name(pattern["quality"]), "MEDIUM")
        self.assertEqual(pattern["quality"], 70.0)

    def test_low_quality_pattern(self):
        pivots, bars = scenario("W", first_gap=3, second_gap=12, second_offset=0.29)
        pivots[1] = replace(pivots[1], price=101.30)
        pattern = detect_latest_pattern(pivots, bars)
        self.assertEqual(quality_name(pattern["quality"]), "LOW")

    def test_unconfirmed_pivot_cannot_form_pattern(self):
        pivots, bars = scenario("W")
        pivots[2] = replace(pivots[2], confirmed=False)
        self.assertFalse(detect_latest_pattern(pivots, bars)["detected"])


class PatternSourceContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ea = EA_PATH.read_text(encoding="utf-8")
        cls.structure = STRUCTURE_PATH.read_text(encoding="utf-8")

    def test_explicit_states_and_closed_bar_terminal_adapter(self):
        for state in ("NONE", "CANDIDATE", "CONFIRMED", "INVALIDATED", "EXPIRED"):
            self.assertIn(f"GOLDSCOUT_PATTERN_STATE_{state}", self.structure)
        self.assertIn("CopyRates(symbol,timeframe,1,barsToLoad,closedRates)", self.structure)
        self.assertIn("closedRates[barIndex].close>neckline.price+breakoutDistance", self.structure)
        self.assertIn("closedRates[barIndex].close<neckline.price-breakoutDistance", self.structure)

    def test_pattern_diagnostics_never_enter_scoring(self):
        score_start = self.ea.index("int longScore=0, shortScore=0;")
        score_end = self.ea.index("longScore=(int)MathMin(100,longScore);", score_start)
        scoring = self.ea[score_start:score_end]
        self.assertNotIn("Pattern", scoring)
        self.assertNotIn("pattern", scoring)
        self.assertIn("RefreshStructurePatternDiagnostics(confirmedPivots,pivotDataAvailable);", self.ea)
        self.assertIn("input int    MinScoreToTrade         = 74;", self.ea)
        self.assertIn("input int    ArmScoreThreshold       = 58;", self.ea)
        self.assertIn("input bool   EnableLiveTrading      = false;", self.ea)
        self.assertIn("input double RiskPercent             = 5.0;", self.ea)

    def test_logging_is_optional_rate_limited_and_deduplicated(self):
        self.assertIn("input bool   DebugStructurePatternLogs       = false;", self.ea)
        self.assertIn("const int STRUCTURE_PATTERN_LOG_INTERVAL_SECONDS=30;", self.ea)
        self.assertIn('if(signature==g_lastPatternLogSignature) return;', self.ea)
        self.assertIn('PrintFormat("[GoldScout][STRUCTURE] pattern=%s | state=%s', self.ea)


if __name__ == "__main__":
    unittest.main()
