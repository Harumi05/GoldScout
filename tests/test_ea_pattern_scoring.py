"""Deterministic scoring integration for confirmed structural patterns."""

from dataclasses import dataclass
from itertools import product
from pathlib import Path
import unittest

from tests.test_ea_market_structure import structural_bucket_points


ROOT = Path(__file__).resolve().parents[1]
EA_PATH = ROOT / "MT5" / "Experts" / "XAU_GoldScout_H1.mq5"
STRUCTURE_PATH = ROOT / "MT5" / "Include" / "GoldScout" / "MarketStructure.mqh"


@dataclass(frozen=True)
class Pattern:
    name: str
    identity: str
    state: str
    direction: int
    quality: float


def quality_bonus(quality):
    if quality < 50:
        return 0
    if quality < 65:
        return 1
    if quality < 75:
        return 2
    if quality < 85:
        return 3
    return 4


def best_evidence(patterns):
    best = {
        1: {"name": "NONE", "quality": 0.0, "bonus": 0},
        -1: {"name": "NONE", "quality": 0.0, "bonus": 0},
    }
    for pattern in patterns:
        if pattern.state != "CONFIRMED" or pattern.direction not in (-1, 1):
            continue
        quality = max(0.0, min(100.0, pattern.quality))
        bonus = quality_bonus(quality)
        if bonus <= 0 or quality <= best[pattern.direction]["quality"]:
            continue
        best[pattern.direction] = {
            "name": pattern.name,
            "quality": quality,
            "bonus": bonus,
        }
    return best


def allowed_bonus(structure, direction, requested):
    contradicted = (direction > 0 and structure == "BAJISTA") or (
        direction < 0 and structure == "ALCISTA"
    )
    return 0 if contradicted else max(0, min(4, requested))


class PatternScoringPolicyTests(unittest.TestCase):
    def test_w_quality_90_adds_four_long(self):
        best = best_evidence([Pattern("W", "w1", "CONFIRMED", 1, 90.0)])
        self.assertEqual(best[1]["bonus"], 4)
        self.assertEqual(
            structural_bucket_points("ALCISTA", 1, pattern_bonus=best[1]["bonus"]),
            19,
        )

    def test_w_quality_60_adds_one_long(self):
        best = best_evidence([Pattern("W", "w1", "CONFIRMED", 1, 60.0)])
        self.assertEqual(best[1]["bonus"], 1)

    def test_quality_below_50_adds_zero(self):
        best = best_evidence([Pattern("W", "w1", "CONFIRMED", 1, 49.99)])
        self.assertEqual((best[1]["name"], best[1]["bonus"]), ("NONE", 0))

    def test_confirmed_m_adds_short_bonus(self):
        best = best_evidence([Pattern("M", "m1", "CONFIRMED", -1, 76.0)])
        self.assertEqual((best[-1]["name"], best[-1]["bonus"]), ("M", 3))

    def test_candidate_adds_zero(self):
        best = best_evidence([Pattern("W", "w1", "CANDIDATE", 1, 95.0)])
        self.assertEqual(best[1]["bonus"], 0)

    def test_expired_adds_zero(self):
        best = best_evidence([Pattern("M", "m1", "EXPIRED", -1, 95.0)])
        self.assertEqual(best[-1]["bonus"], 0)

    def test_invalidated_adds_zero(self):
        best = best_evidence([Pattern("W", "w1", "INVALIDATED", 1, 95.0)])
        self.assertEqual(best[1]["bonus"], 0)

    def test_quality_bonus_boundaries(self):
        for quality, expected in (
            (49.99, 0), (50.0, 1), (64.99, 1), (65.0, 2),
            (74.99, 2), (75.0, 3), (84.99, 3), (85.0, 4), (100.0, 4),
        ):
            with self.subTest(quality=quality):
                self.assertEqual(quality_bonus(quality), expected)

    def test_multiple_long_patterns_select_only_highest_quality(self):
        best = best_evidence(
            [
                Pattern("W", "w1", "CONFIRMED", 1, 82.0),
                Pattern("BULL_FLAG", "f1", "CONFIRMED", 1, 87.0),
                Pattern("HCH_INVERTED", "h1", "CONFIRMED", 1, 91.0),
            ]
        )
        self.assertEqual(best[1], {"name": "HCH_INVERTED", "quality": 91.0, "bonus": 4})
        self.assertEqual(structural_bucket_points("ALCISTA", 1, pattern_bonus=4), 19)

    def test_long_and_short_patterns_are_selected_independently(self):
        best = best_evidence(
            [
                Pattern("W", "w1", "CONFIRMED", 1, 86.0),
                Pattern("HCH", "h1", "CONFIRMED", -1, 78.0),
            ]
        )
        self.assertEqual(best[1]["bonus"], 4)
        self.assertEqual(best[-1]["bonus"], 3)

    def test_pattern_contradicting_confirmed_structure_is_annulled(self):
        self.assertEqual(allowed_bonus("BAJISTA", 1, 4), 0)
        self.assertEqual(structural_bucket_points("BAJISTA", 1, pattern_bonus=4), 0)
        self.assertEqual(allowed_bonus("ALCISTA", -1, 4), 0)

    def test_bucket_never_exceeds_twenty_five(self):
        for state, direction, pullback, breakout, momentum, pattern_bonus in product(
            ("ALCISTA", "BAJISTA", "NEUTRA", "INSUFICIENTE"),
            (-1, 1),
            (False, True),
            (False, True),
            (False, True),
            range(5),
        ):
            points = structural_bucket_points(
                state, direction, pullback, breakout, momentum, pattern_bonus
            )
            self.assertGreaterEqual(points, 0)
            self.assertLessEqual(points, 25)
        self.assertEqual(
            structural_bucket_points("ALCISTA", 1, breakout=True, pattern_bonus=4),
            25,
        )

    def test_absence_of_patterns_preserves_existing_bucket_scores(self):
        self.assertEqual(structural_bucket_points("ALCISTA", 1), 15)
        self.assertEqual(
            structural_bucket_points("ALCISTA", 1, pullback=True, momentum=True),
            25,
        )
        self.assertEqual(structural_bucket_points("NEUTRA", 1), 0)

    def test_absence_of_pattern_does_not_block_frequency(self):
        base_score = 60
        pattern_bonus = best_evidence([])[1]["bonus"]
        self.assertEqual(base_score + pattern_bonus, base_score)


class PatternScoringSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ea = EA_PATH.read_text(encoding="utf-8")
        cls.structure = STRUCTURE_PATH.read_text(encoding="utf-8")

    def test_quality_tiers_and_confirmed_only_policy_are_explicit(self):
        for fragment in (
            "if(!MathIsValidNumber(quality) || quality<50.0) return 0;",
            "if(quality<65.0) return 1;",
            "if(quality<75.0) return 2;",
            "if(quality<85.0) return 3;",
            "return GOLDSCOUT_MAX_PATTERN_BONUS_POINTS;",
            "if(state!=GOLDSCOUT_PATTERN_STATE_CONFIRMED) return;",
        ):
            self.assertIn(fragment, self.structure)

    def test_all_pattern_families_feed_one_best_evidence_selector(self):
        for fragment in (
            "GS_PatternTypeName(g_patternDiagnostic.type)",
            "GS_ContinuationPatternTypeName(g_continuationPatternDiagnostic.type)",
            "GS_ConvergencePatternTypeName(g_convergencePatternDiagnostic.type)",
            "GS_HeadShouldersPatternTypeName(g_headShouldersPatternDiagnostic.type)",
        ):
            self.assertIn(fragment, self.ea)
        for fragment in (
            "g_patternDiagnostic.type==GOLDSCOUT_PATTERN_W) direction=1",
            "g_patternDiagnostic.type==GOLDSCOUT_PATTERN_M) direction=-1",
            "g_continuationPatternDiagnostic.type==GOLDSCOUT_CONTINUATION_BULL_FLAG",
            "g_continuationPatternDiagnostic.type==GOLDSCOUT_CONTINUATION_BEAR_FLAG",
            "g_convergencePatternDiagnostic.breakoutDirection",
            "g_headShouldersPatternDiagnostic.breakoutDirection",
        ):
            self.assertIn(fragment, self.ea)
        self.assertIn("boundedQuality<=best.quality", self.structure)

    def test_existing_bucket_and_thresholds_remain_bounded(self):
        self.assertIn("GOLDSCOUT_MAX_STRUCTURAL_BUCKET_POINTS=25", self.structure)
        self.assertIn("GOLDSCOUT_MAX_PATTERN_BONUS_POINTS=4", self.structure)
        self.assertIn("longPatternBonus", self.ea)
        self.assertIn("shortPatternBonus", self.ea)
        self.assertIn("input int    ArmScoreThreshold       = 58;", self.ea)
        self.assertIn("input int    MinScoreToTrade         = 74;", self.ea)
        self.assertIn("input bool   EnableLiveTrading      = false;", self.ea)
        self.assertIn("input double RiskPercent             = 5.0;", self.ea)

    def test_diagnostic_reports_best_pattern_per_direction(self):
        self.assertIn("PATTERN BEST LONG=%s quality=%.1f bonus=%d", self.ea)
        self.assertIn("PATTERN BEST SHORT=%s quality=%.1f bonus=%d", self.ea)


if __name__ == "__main__":
    unittest.main()
