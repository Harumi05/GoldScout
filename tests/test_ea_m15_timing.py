"""Deterministic tests for the soft M15 timing-confirmation layer."""

from itertools import product
from pathlib import Path
import unittest

from tests.test_ea_market_structure import (
    PivotConfig,
    SyntheticBar,
    bars,
    classify_confirmed_structure,
    detect_from_terminal_series,
)


ROOT = Path(__file__).resolve().parents[1]
EA_PATH = ROOT / "MT5" / "Experts" / "XAU_GoldScout_H1.mq5"
STRUCTURE_PATH = ROOT / "MT5" / "Include" / "GoldScout" / "MarketStructure.mqh"
MAX_M15_POINTS = 4
ARM_SCORE = 58


def m15_adjustment(
    available,
    structure,
    direction,
    breakout=0,
    recovery=0,
    momentum=0,
):
    if not available or direction not in (-1, 1):
        return 0
    if structure not in {"ALCISTA", "BAJISTA"}:
        return 0
    aligned_structure = (direction > 0 and structure == "ALCISTA") or (
        direction < 0 and structure == "BAJISTA"
    )
    opposing_structure = (direction > 0 and structure == "BAJISTA") or (
        direction < 0 and structure == "ALCISTA"
    )
    aligned_event = breakout == direction or recovery == direction
    opposing_event = breakout == -direction or recovery == -direction
    aligned_momentum = momentum == direction
    opposing_momentum = momentum == -direction

    if opposing_structure:
        return -4 if opposing_event or opposing_momentum else -3
    if opposing_event:
        return -3 if opposing_momentum else -2
    points = int(aligned_structure) + 2 * int(aligned_event) + int(aligned_momentum)
    return max(-MAX_M15_POINTS, min(MAX_M15_POINTS, points))


def gated_adjustment(h1_technical_score, requested, arm_threshold=ARM_SCORE):
    bounded = max(-MAX_M15_POINTS, min(MAX_M15_POINTS, requested))
    if bounded > 0 and h1_technical_score < arm_threshold:
        return 0
    return bounded


class M15TimingPolicyTests(unittest.TestCase):
    def test_h1_long_with_strong_m15_confirmation(self):
        raw = m15_adjustment(True, "ALCISTA", 1, breakout=1, momentum=1)
        self.assertEqual(raw, 4)
        self.assertEqual(70 + gated_adjustment(70, raw), 74)

    def test_h1_short_with_strong_m15_confirmation(self):
        raw = m15_adjustment(True, "BAJISTA", -1, breakout=-1, momentum=-1)
        self.assertEqual(raw, 4)
        self.assertEqual(70 + gated_adjustment(70, raw), 74)

    def test_neutral_m15_without_timing_event_adds_zero(self):
        self.assertEqual(m15_adjustment(True, "NEUTRA", 1), 0)
        self.assertEqual(m15_adjustment(True, "NEUTRA", -1), 0)

    def test_neutral_m15_with_breakout_adds_zero(self):
        self.assertEqual(m15_adjustment(True, "NEUTRA", 1, breakout=1), 0)
        self.assertEqual(m15_adjustment(True, "NEUTRA", -1, breakout=-1), 0)

    def test_neutral_m15_with_momentum_adds_zero(self):
        self.assertEqual(m15_adjustment(True, "NEUTRA", 1, momentum=1), 0)
        self.assertEqual(m15_adjustment(True, "NEUTRA", -1, momentum=-1), 0)

    def test_neutral_m15_with_breakout_and_momentum_adds_zero(self):
        self.assertEqual(
            m15_adjustment(True, "NEUTRA", 1, breakout=1, momentum=1), 0
        )
        self.assertEqual(
            m15_adjustment(True, "INSUFICIENTE", -1, breakout=-1, momentum=-1), 0
        )

    def test_aligned_m15_keeps_structure_event_momentum_policy(self):
        self.assertEqual(
            m15_adjustment(True, "ALCISTA", 1, breakout=1, momentum=1), 4
        )
        self.assertEqual(
            m15_adjustment(True, "BAJISTA", -1, recovery=-1, momentum=-1), 4
        )

    def test_strong_structural_contradiction_is_bounded(self):
        self.assertEqual(
            m15_adjustment(True, "BAJISTA", 1, breakout=-1, momentum=-1), -4
        )
        self.assertEqual(m15_adjustment(True, "ALCISTA", -1), -3)

    def test_breakout_without_momentum(self):
        self.assertEqual(m15_adjustment(True, "ALCISTA", 1, breakout=1), 3)

    def test_momentum_without_breakout(self):
        self.assertEqual(m15_adjustment(True, "ALCISTA", 1, momentum=1), 2)

    def test_aligned_structure_without_breakout(self):
        self.assertEqual(m15_adjustment(True, "ALCISTA", 1), 1)
        self.assertEqual(m15_adjustment(True, "BAJISTA", -1), 1)

    def test_m15_adjustment_never_exceeds_four_absolute_points(self):
        for available, structure, direction, breakout, recovery, momentum in product(
            (False, True),
            ("ALCISTA", "BAJISTA", "NEUTRA", "INSUFICIENTE"),
            (-1, 0, 1),
            (-1, 0, 1),
            (-1, 0, 1),
            (-1, 0, 1),
        ):
            value = m15_adjustment(
                available, structure, direction, breakout, recovery, momentum
            )
            self.assertGreaterEqual(value, -4)
            self.assertLessEqual(value, 4)

    def test_missing_m15_is_zero_and_does_not_block(self):
        base = 74
        raw = m15_adjustment(False, "INSUFICIENTE", 1, breakout=1, momentum=1)
        self.assertEqual(raw, 0)
        self.assertEqual(base + gated_adjustment(base, raw), base)

    def test_m15_cannot_create_or_rescue_a_weak_h1_setup(self):
        self.assertEqual(gated_adjustment(57, 4), 0)
        self.assertEqual(57 + gated_adjustment(57, 4), 57)
        self.assertLess(57 + gated_adjustment(57, 4), ARM_SCORE)

    def test_confirmed_m15_structure_does_not_repaint_with_open_bar(self):
        closed = bars(
            [10, 13, 11, 14, 12, 15, 13],
            [8, 10, 9, 11, 10, 12, 11],
        )
        open_a = SyntheticBar(50_000, 16, 12, 1)
        open_b = SyntheticBar(50_000, 100, 1, 20)
        config = PivotConfig(min_prominence_atr=0.5, tolerance_atr=0.2)
        pivots_a = detect_from_terminal_series([open_a] + list(reversed(closed)), config)
        pivots_b = detect_from_terminal_series([open_b] + list(reversed(closed)), config)
        self.assertEqual(pivots_a, pivots_b)
        self.assertEqual(
            classify_confirmed_structure(pivots_a),
            classify_confirmed_structure(pivots_b),
        )


class M15TimingSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ea = EA_PATH.read_text(encoding="utf-8")
        cls.structure = STRUCTURE_PATH.read_text(encoding="utf-8")

    def test_hierarchy_uses_m15_and_never_m30(self):
        self.assertIn("PERIOD_H4", self.ea)
        self.assertIn("PERIOD_H1", self.ea)
        self.assertIn("PERIOD_M15", self.ea)
        self.assertNotIn("PERIOD_M30", self.ea)

    def test_m15_structure_and_timing_use_only_closed_bars(self):
        required = (
            "GS_LoadConfirmedPivots(_Symbol,PERIOD_M15,hM15ATR,",
            "datetime latestClosedBar=iTime(_Symbol,PERIOD_M15,1);",
            "double open1=iOpen(_Symbol,PERIOD_M15,1);",
            "double close1=iClose(_Symbol,PERIOD_M15,1);",
            "double close2=iClose(_Symbol,PERIOD_M15,2);",
            "double high2=iHigh(_Symbol,PERIOD_M15,2);",
            "double low2=iLow(_Symbol,PERIOD_M15,2);",
        )
        for fragment in required:
            self.assertIn(fragment, self.ea)
        self.assertNotIn("PERIOD_M15,0", self.ea)

    def test_m15_is_separate_from_h1_structural_bucket(self):
        self.assertEqual(self.ea.count("GS_StructuralBucketPoints"), 2)
        self.assertIn("GOLDSCOUT_MAX_STRUCTURAL_BUCKET_POINTS=25", self.structure)
        self.assertIn("GOLDSCOUT_MAX_M15_TIMING_POINTS=4", self.structure)
        self.assertIn("g_intrabarLongBoost+longM15Adjustment", self.ea)
        self.assertIn("g_intrabarShortBoost+shortM15Adjustment", self.ea)

    def test_positive_timing_is_gated_by_existing_h1_setup(self):
        self.assertIn(
            "structure!=GOLDSCOUT_STRUCTURE_BULLISH &&", self.structure
        )
        self.assertIn("if(bounded>0 && h1TechnicalScore<armThreshold) return 0;", self.structure)
        self.assertIn("longScore,ArmScoreThreshold,g_m15TimingEvidence.longAdjustment", self.ea)
        self.assertIn("shortScore,ArmScoreThreshold,g_m15TimingEvidence.shortAdjustment", self.ea)

    def test_thresholds_and_safety_defaults_are_unchanged(self):
        self.assertIn("input int    ArmScoreThreshold       = 58;", self.ea)
        self.assertIn("input int    MinScoreToTrade         = 74;", self.ea)
        self.assertIn("input bool   EnableLiveTrading      = false;", self.ea)
        self.assertIn("input double RiskPercent             = 5.0;", self.ea)

    def test_compact_logging_is_optional_and_rate_limited(self):
        self.assertIn("input bool   DebugM15TimingLogs                = false;", self.ea)
        self.assertIn("M15_TIMING_LOG_INTERVAL_SECONDS=30", self.ea)
        self.assertIn("[GoldScout][M15] structure=%s", self.ea)


if __name__ == "__main__":
    unittest.main()
