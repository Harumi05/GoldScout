"""Characterization tests for legacy scoring and the unused pivot foundation."""

from dataclasses import dataclass
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
EA_PATH = ROOT / "MT5" / "Experts" / "XAU_GoldScout_H1.mq5"
PIVOT_PATH = ROOT / "MT5" / "Include" / "GoldScout" / "MarketStructure.mqh"


def legacy_scores(
    *,
    bull_htf=False,
    bear_htf=False,
    bull_ltf=False,
    bear_ltf=False,
    adx_build=False,
    rsi_long=False,
    rsi_short=False,
    htf_pull_long=False,
    htf_pull_short=False,
    hh=False,
    hl=False,
    lh=False,
    ll=False,
    pull_long=False,
    pull_short=False,
    break_long=False,
    break_short=False,
    momentum_long=False,
    momentum_short=False,
    volume_long=False,
    volume_short=False,
):
    """Exact pre-pivot technical-scoring behavior of BuildSignal()."""
    long_score = 20 if bull_htf else 0
    short_score = 20 if bear_htf else 0
    long_score += 15 if bull_ltf else 0
    short_score += 15 if bear_ltf else 0
    long_score += 5 if bull_htf and bull_ltf else 0
    short_score += 5 if bear_htf and bear_ltf else 0
    if adx_build:
        long_score += 10 if bull_htf or bull_ltf else 0
        short_score += 10 if bear_htf or bear_ltf else 0
    long_score += 10 if rsi_long else 0
    short_score += 10 if rsi_short else 0
    long_score += 5 if htf_pull_long else 0
    short_score += 5 if htf_pull_short else 0

    structure_long = break_long or hh or hl or pull_long
    structure_short = break_short or ll or lh or pull_short
    long_score += 15 if structure_long else 0
    short_score += 15 if structure_short else 0
    long_score += 10 if pull_long else 0
    short_score += 10 if pull_short else 0
    long_score += 10 if break_long else 0
    short_score += 10 if break_short else 0
    long_score += 5 if momentum_long else 0
    short_score += 5 if momentum_short else 0
    long_score += 5 if volume_long else 0
    short_score += 5 if volume_short else 0
    return min(100, long_score), min(100, short_score)


def legacy_setup(direction, breakout=False, pullback=False, momentum=False):
    if breakout:
        return "BREAKOUT"
    if pullback:
        return "PULLBACK"
    if momentum:
        return "MOMENTUM"
    return "CONTINUATION LONG" if direction > 0 else "CONTINUATION SHORT"


class LegacyStructureCharacterizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ea = EA_PATH.read_text(encoding="utf-8")

    def test_source_keeps_closed_bar_and_fixed_window_baseline(self):
        required = (
            "GetValue(hEmaFast,0,1,emaF)",
            "GetValue(hEmaHTFFast,0,1,h4F)",
            "double close1=iClose(_Symbol,PERIOD_H1,1)",
            "iHighest(_Symbol,PERIOD_H1,MODE_HIGH,StructureLookback,2)",
            "iHighest(_Symbol,PERIOD_H1,MODE_HIGH,StructureLookback,StructureLookback+2)",
            "bool hh = recentHigh > priorHigh + atr*0.20;",
            "bool hl = recentLow  > priorLow  + atr*0.20;",
            "bool lh = recentHigh < priorHigh - atr*0.20;",
            "bool ll = recentLow  < priorLow  - atr*0.20;",
        )
        for fragment in required:
            self.assertIn(fragment, self.ea)

    def test_h4_bullish_and_bearish_bias(self):
        self.assertEqual(legacy_scores(bull_htf=True), (20, 0))
        self.assertEqual(legacy_scores(bear_htf=True), (0, 20))

    def test_h1_bullish_and_bearish_bias(self):
        self.assertEqual(legacy_scores(bull_ltf=True), (15, 0))
        self.assertEqual(legacy_scores(bear_ltf=True), (0, 15))

    def test_hh_and_hl_reward_long_structure_once(self):
        self.assertEqual(legacy_scores(hh=True), (15, 0))
        self.assertEqual(legacy_scores(hl=True), (15, 0))
        self.assertEqual(legacy_scores(hh=True, hl=True), (15, 0))

    def test_lh_and_ll_reward_short_structure_once(self):
        self.assertEqual(legacy_scores(lh=True), (0, 15))
        self.assertEqual(legacy_scores(ll=True), (0, 15))
        self.assertEqual(legacy_scores(lh=True, ll=True), (0, 15))

    def test_breakout_also_receives_structure_and_momentum_points(self):
        self.assertEqual(
            legacy_scores(break_long=True, momentum_long=True), (30, 0)
        )
        self.assertEqual(
            legacy_scores(break_short=True, momentum_short=True), (0, 30)
        )
        self.assertEqual(legacy_setup(1, breakout=True, momentum=True), "BREAKOUT")

    def test_pullback_receives_structure_and_pullback_points(self):
        self.assertEqual(legacy_scores(pull_long=True), (25, 0))
        self.assertEqual(legacy_scores(pull_short=True), (0, 25))
        self.assertEqual(legacy_setup(1, pullback=True), "PULLBACK")

    def test_continuation_is_the_setup_fallback(self):
        self.assertEqual(legacy_setup(1), "CONTINUATION LONG")
        self.assertEqual(legacy_setup(-1), "CONTINUATION SHORT")
        self.assertEqual(
            legacy_scores(
                bull_htf=True,
                bull_ltf=True,
                adx_build=True,
                rsi_long=True,
            ),
            (60, 0),
        )

    def test_momentum_contributes_five_points(self):
        self.assertEqual(legacy_scores(momentum_long=True), (5, 0))
        self.assertEqual(legacy_scores(momentum_short=True), (0, 5))
        self.assertEqual(legacy_setup(1, momentum=True), "MOMENTUM")

    def test_h4_h1_conflict_rewards_both_directions_when_adx_builds(self):
        self.assertEqual(
            legacy_scores(bull_htf=True, bear_ltf=True, adx_build=True),
            (30, 25),
        )
        self.assertEqual(
            legacy_scores(bear_htf=True, bull_ltf=True, adx_build=True),
            (25, 30),
        )

    def test_source_keeps_legacy_score_weights_and_setup_priority(self):
        required = (
            "bool bullHTF = h4F > h4S;",
            "bool bearHTF = h4F < h4S;",
            "bool bullLTF = emaF > emaS;",
            "bool bearLTF = emaF < emaS;",
            "bool breakLong = close1 > recentHigh;",
            "bool breakShort = close1 < recentLow;",
            "bool momLong = close1 > high2;",
            "bool momShort = close1 < low2;",
            "bool pullLong = atr>0.0 && nearFast && close1 <= emaF && rsi<=48.0 && (bullHTF || bullLTF) && hl;",
            "bool pullShort = atr>0.0 && nearFast && close1 >= emaF && rsi>=52.0 && (bearHTF || bearLTF) && lh;",
            "if(bullHTF) longScore+=20;",
            "if(bearHTF) shortScore+=20;",
            "if(bullLTF) longScore+=15;",
            "if(bearLTF) shortScore+=15;",
            "if(structureLong) longScore+=15;",
            "if(structureShort) shortScore+=15;",
            "if(pullLong) longScore+=10;",
            "if(pullShort) shortScore+=10;",
            "if(breakLong) longScore+=10;",
            "if(breakShort) shortScore+=10;",
            "if(momLong) longScore+=5;",
            "if(momShort) shortScore+=5;",
            'if(breakout) return "BREAKOUT";',
            'if(pullback) return "PULLBACK";',
            'if(momentum) return "MOMENTUM";',
            'return dir>0 ? "CONTINUATION LONG" : "CONTINUATION SHORT";',
            'else if(strongTrend) g_diagStructure="CONTINUACION";',
        )
        for fragment in required:
            self.assertIn(fragment, self.ea)


@dataclass(frozen=True)
class SyntheticBar:
    timestamp: int
    high: float
    low: float
    atr: float = 1.0


@dataclass(frozen=True)
class PivotConfig:
    left: int = 1
    right: int = 1
    min_bars_between: int = 1
    min_prominence_atr: float = 0.25
    tolerance_atr: float = 0.30


@dataclass(frozen=True)
class SyntheticPivot:
    kind: str
    price: float
    shift: int
    timestamp: int
    atr: float
    confirmed: bool


def valid_config(config):
    return (
        config.left >= 1
        and config.right >= 1
        and config.min_bars_between >= 1
        and config.min_prominence_atr >= 0.0
        and config.tolerance_atr > 0.0
    )


def atr_tolerance(first_atr, second_atr, multiplier, minimum_price_step):
    values = (first_atr, second_atr, multiplier, minimum_price_step)
    if not all(value > 0.0 for value in values):
        return None
    return max(minimum_price_step, max(first_atr, second_atr) * multiplier)


def detect_confirmed_pivots(closed_bars, config=PivotConfig(), newest_shift=1):
    """Python twin of the MQL detector; input is oldest-to-newest and closed."""
    if newest_shift < 1 or not valid_config(config):
        return None
    if any(
        bar.timestamp <= 0 or bar.high < bar.low or bar.atr <= 0.0
        for bar in closed_bars
    ):
        return None
    if any(
        closed_bars[i].timestamp <= closed_bars[i - 1].timestamp
        for i in range(1, len(closed_bars))
    ):
        return None

    pivots = []
    last_accepted = None
    for index in range(config.left, len(closed_bars) - config.right):
        bar = closed_bars[index]
        neighbours = (
            closed_bars[index - config.left : index]
            + closed_bars[index + 1 : index + config.right + 1]
        )
        neighbour_high = max(item.high for item in neighbours)
        neighbour_low = min(item.low for item in neighbours)
        required = config.min_prominence_atr * bar.atr
        is_high = bar.high > neighbour_high and bar.high - neighbour_high >= required
        is_low = bar.low < neighbour_low and neighbour_low - bar.low >= required
        if is_high == is_low:
            continue
        if (
            last_accepted is not None
            and index - last_accepted < config.min_bars_between
        ):
            continue
        pivots.append(
            SyntheticPivot(
                kind="HIGH" if is_high else "LOW",
                price=bar.high if is_high else bar.low,
                shift=newest_shift + len(closed_bars) - 1 - index,
                timestamp=bar.timestamp,
                atr=bar.atr,
                confirmed=True,
            )
        )
        last_accepted = index
    return pivots


def detect_from_terminal_series(newest_first_bars, config=PivotConfig()):
    """Models CopyRates(start_pos=1): the element at shift 0 is ignored."""
    return detect_confirmed_pivots(list(reversed(newest_first_bars[1:])), config)


def bars(highs, lows, atr=1.0):
    return [
        SyntheticBar(timestamp=1_000 + index * 3_600, high=high, low=low, atr=atr)
        for index, (high, low) in enumerate(zip(highs, lows))
    ]


class ConfirmedPivotEngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ea = EA_PATH.read_text(encoding="utf-8")
        cls.pivot_source = PIVOT_PATH.read_text(encoding="utf-8")

    def test_ea_includes_engine_but_does_not_call_it(self):
        self.assertIn("#include <GoldScout/MarketStructure.mqh>", self.ea)
        self.assertEqual(self.ea.count("GS_DetectConfirmedPivots"), 0)
        self.assertEqual(self.ea.count("GS_LoadConfirmedPivots"), 0)

    def test_pivot_contract_has_required_fields_and_chronological_guard(self):
        required = (
            "GoldScoutPivotType type;",
            "double             price;",
            "int                shift;",
            "datetime           time;",
            "double             atr;",
            "bool               confirmed;",
            "int    leftBars;",
            "int    rightBars;",
            "int    minBarsBetween;",
            "if(i>0 && closedRates[i].time<=closedRates[i-1].time) return false;",
        )
        for fragment in required:
            self.assertIn(fragment, self.pivot_source)

        duplicate_time = bars([10, 11, 14, 11, 10], [8, 9, 10, 9, 8])
        duplicate_time[2] = SyntheticBar(
            duplicate_time[1].timestamp, 14, 10, 1.0
        )
        self.assertIsNone(detect_confirmed_pivots(duplicate_time))

    def test_clear_swing_high(self):
        sample = bars([10, 11, 14, 11, 10], [8, 9, 10, 9, 8])
        pivots = detect_confirmed_pivots(
            sample, PivotConfig(left=2, right=2, min_prominence_atr=1.0)
        )
        self.assertEqual(pivots, [SyntheticPivot("HIGH", 14, 3, sample[2].timestamp, 1.0, True)])

    def test_clear_swing_low(self):
        sample = bars([12, 11, 10, 11, 12], [10, 9, 6, 9, 10])
        pivots = detect_confirmed_pivots(
            sample, PivotConfig(left=2, right=2, min_prominence_atr=1.0)
        )
        self.assertEqual(pivots, [SyntheticPivot("LOW", 6, 3, sample[2].timestamp, 1.0, True)])

    def test_noise_below_atr_prominence_has_no_pivot(self):
        sample = bars(
            [10.00, 10.05, 10.10, 10.05, 10.00],
            [9.00, 9.02, 9.04, 9.02, 9.00],
        )
        pivots = detect_confirmed_pivots(
            sample, PivotConfig(left=2, right=2, min_prominence_atr=0.20)
        )
        self.assertEqual(pivots, [])

    def test_pivots_that_are_too_close_are_not_duplicated(self):
        sample = bars(
            [10, 14, 11, 12, 11, 10],
            [8, 10, 7, 9, 8, 7],
        )
        pivots = detect_confirmed_pivots(
            sample,
            PivotConfig(
                left=1,
                right=1,
                min_bars_between=3,
                min_prominence_atr=0.50,
            ),
        )
        self.assertEqual([pivot.kind for pivot in pivots], ["HIGH"])

    def test_alternating_high_low_sequence_is_chronological(self):
        sample = bars(
            [10, 12, 10, 13, 9, 12, 10],
            [9, 10, 8, 10, 7, 9, 8],
        )
        pivots = detect_confirmed_pivots(
            sample, PivotConfig(min_prominence_atr=0.50)
        )
        self.assertEqual(
            [pivot.kind for pivot in pivots],
            ["HIGH", "LOW", "HIGH", "LOW", "HIGH"],
        )
        self.assertEqual(
            [pivot.timestamp for pivot in pivots],
            sorted(pivot.timestamp for pivot in pivots),
        )

    def test_mutating_open_bar_does_not_change_confirmed_pivots(self):
        closed = bars([10, 11, 14, 11, 10], [8, 9, 10, 9, 8])
        open_a = SyntheticBar(50_000, 100, 1, 5)
        open_b = SyntheticBar(50_000, 1_000, 0.1, 50)
        newest_first_a = [open_a] + list(reversed(closed))
        newest_first_b = [open_b] + list(reversed(closed))
        self.assertEqual(
            detect_from_terminal_series(
                newest_first_a,
                PivotConfig(left=2, right=2, min_prominence_atr=1.0),
            ),
            detect_from_terminal_series(
                newest_first_b,
                PivotConfig(left=2, right=2, min_prominence_atr=1.0),
            ),
        )
        self.assertIn("CopyRates(symbol,timeframe,1,barsToLoad,closedRates)", self.pivot_source)
        self.assertIn("CopyBuffer(atrHandle,0,1,copied,closedAtr)", self.pivot_source)

    def test_invalid_atr_or_tolerance_fails_safely(self):
        invalid_atr = bars([10, 11, 14, 11, 10], [8, 9, 10, 9, 8])
        invalid_atr[2] = SyntheticBar(invalid_atr[2].timestamp, 14, 10, 0.0)
        self.assertIsNone(detect_confirmed_pivots(invalid_atr))
        self.assertIsNone(
            detect_confirmed_pivots(
                bars([10, 11, 14, 11, 10], [8, 9, 10, 9, 8]),
                PivotConfig(tolerance_atr=0.0),
            )
        )
        self.assertIsNone(atr_tolerance(1.0, 1.0, 0.0, 0.01))
        self.assertIsNone(atr_tolerance(0.0, 1.0, 0.3, 0.01))
        self.assertEqual(atr_tolerance(1.0, 2.0, 0.3, 0.01), 0.6)


if __name__ == "__main__":
    unittest.main()
