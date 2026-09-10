"""Deterministic policy and source contracts for session/news context scoring."""

from itertools import product
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
EA_PATH = ROOT / "MT5" / "Experts" / "XAU_GoldScout_H1.mq5"
CONTEXT_PATH = ROOT / "MT5" / "Include" / "GoldScout" / "ContextScoring.mqh"
MAX_CONTEXT_POINTS = 4
MAX_NEWS_POINTS = 2
ARM_SCORE = 58


def session_points(
    *, london=False, new_york=False, asia_centers=0, available=True, market_open=True
):
    if not available or not market_open:
        return 0
    if london and new_york:
        return 2
    if london or new_york:
        return 1
    if asia_centers > 0:
        return 0
    return 0


def news_points(
    *,
    direction,
    enabled=True,
    available=True,
    fresh=True,
    bias=0,
    confidence=0,
    data_risk="LOW",
    declared_direction="NEUTRO",
    sources_ok=0,
    source_count=0,
    configured_max=2,
):
    max_points = max(0, min(MAX_NEWS_POINTS, configured_max))
    if not enabled or max_points <= 0 or direction not in (-1, 1):
        return 0
    if not available or not fresh:
        return -1
    risk = data_risk.upper()
    declared = declared_direction.upper()
    if risk == "HIGH":
        return -1
    bounded_bias = max(-100, min(100, bias))
    bounded_confidence = max(0, min(100, confidence))
    bias_direction = 1 if bounded_bias > 20 else -1 if bounded_bias < -20 else 0
    if bias_direction == 0 or declared in {"NEUTRO", "NEUTRAL"}:
        return 0
    declared_aligned = (
        bias_direction > 0 and declared in {"ALCISTA", "LONG"}
    ) or (bias_direction < 0 and declared in {"BAJISTA", "SHORT"})
    if not declared_aligned or bounded_confidence < 50:
        return 0
    points = min(1, max_points)
    reliable = source_count >= 2 and 2 <= sources_ok <= source_count
    if risk == "LOW" and reliable and bounded_confidence >= 70 and abs(bounded_bias) >= 50:
        points = max_points
    return points if bias_direction == direction else -points


def combined_context(session, news):
    return max(-MAX_CONTEXT_POINTS, min(MAX_CONTEXT_POINTS, session + news))


def gated_context(h1_score, requested, arm_threshold=ARM_SCORE):
    bounded = max(-MAX_CONTEXT_POINTS, min(MAX_CONTEXT_POINTS, requested))
    if bounded > 0 and h1_score < arm_threshold:
        return 0
    return bounded


class ContextScoringPolicyTests(unittest.TestCase):
    def test_london_active_is_favorable(self):
        self.assertEqual(session_points(london=True), 1)

    def test_new_york_active_is_favorable(self):
        self.assertEqual(session_points(new_york=True), 1)

    def test_asia_low_activity_is_neutral(self):
        self.assertEqual(session_points(asia_centers=7), 0)

    def test_weekend_clock_window_is_neutral(self):
        self.assertEqual(session_points(london=True, new_york=True, market_open=False), 0)

    def test_aligned_news_is_small_bonus(self):
        value = news_points(
            direction=1,
            bias=70,
            confidence=80,
            declared_direction="ALCISTA",
            sources_ok=3,
            source_count=4,
        )
        self.assertEqual(value, 2)

    def test_contrary_news_is_small_penalty(self):
        value = news_points(
            direction=-1,
            bias=70,
            confidence=80,
            declared_direction="ALCISTA",
            sources_ok=3,
            source_count=4,
        )
        self.assertEqual(value, -2)

    def test_neutral_news_is_zero(self):
        self.assertEqual(
            news_points(direction=1, bias=20, confidence=90, declared_direction="NEUTRO"),
            0,
        )

    def test_high_data_risk_does_not_invent_direction(self):
        self.assertEqual(
            news_points(
                direction=1,
                bias=90,
                confidence=95,
                data_risk="HIGH",
                declared_direction="ALCISTA",
                sources_ok=3,
                source_count=3,
            ),
            -1,
        )

    def test_stale_news_is_soft_penalty(self):
        self.assertEqual(news_points(direction=1, fresh=False), -1)

    def test_unavailable_news_is_soft_penalty(self):
        self.assertEqual(news_points(direction=-1, available=False), -1)

    def test_provider_fallback_caps_directional_evidence_at_one(self):
        self.assertEqual(
            news_points(
                direction=1,
                bias=90,
                confidence=95,
                data_risk="MEDIUM",
                declared_direction="ALCISTA",
                sources_ok=1,
                source_count=4,
            ),
            1,
        )

    def test_declared_direction_must_agree_with_bias(self):
        self.assertEqual(
            news_points(
                direction=1,
                bias=80,
                confidence=90,
                declared_direction="BAJISTA",
                sources_ok=3,
                source_count=3,
            ),
            0,
        )

    def test_session_and_news_combine_once(self):
        session = session_points(london=True, new_york=True)
        news = news_points(
            direction=1,
            bias=80,
            confidence=90,
            declared_direction="ALCISTA",
            sources_ok=3,
            source_count=3,
        )
        self.assertEqual(combined_context(session, news), 4)

    def test_combined_context_is_always_bounded(self):
        for session, news in product(range(-8, 9), repeat=2):
            value = combined_context(session, news)
            self.assertGreaterEqual(value, -4)
            self.assertLessEqual(value, 4)
        self.assertEqual(combined_context(8, 8), 4)
        self.assertEqual(combined_context(-8, -8), -4)

    def test_absent_context_is_zero_and_never_a_boolean_block(self):
        session = session_points(available=False)
        news = news_points(direction=1, enabled=False)
        self.assertEqual(combined_context(session, news), 0)

    def test_zero_context_preserves_previous_score(self):
        previous_score = 74
        self.assertEqual(previous_score + gated_context(previous_score, 0), previous_score)

    def test_positive_context_cannot_create_a_weak_h1_setup(self):
        self.assertEqual(gated_context(57, 4), 0)
        self.assertEqual(57 + gated_context(57, 4), 57)


class ContextScoringSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ea = EA_PATH.read_text(encoding="utf-8")
        cls.context = CONTEXT_PATH.read_text(encoding="utf-8")

    def test_all_market_centers_and_dst_rules_are_explicit(self):
        for center in (
            "TOKIO",
            "SEUL",
            "SHANGHAI",
            "HONG KONG",
            "SINGAPUR",
            "MUMBAI",
            "DUBAI",
            "LONDRES",
            "NUEVA YORK/COMEX",
            "LBMA AM",
            "LBMA PM",
        ):
            self.assertIn(f'"{center}"', self.context)
        self.assertIn("GS_LondonDst", self.context)
        self.assertIn("GS_NewYorkDst", self.context)
        self.assertIn("Colombia %02d:%02d", self.context)
        self.assertIn("utc.day_of_week==0 || utc.day_of_week==6", self.context)

    def test_opening_impulse_and_volatility_are_diagnostic_not_extra_points(self):
        self.assertIn("SessionOpeningImpulseATR", self.ea)
        self.assertIn("session_open_impulse_atr", self.ea)
        self.assertNotIn("g_diagSessionOpenImpulseATR+", self.ea)
        self.assertNotIn("openingWindow) context.points", self.context)

    def test_limits_defaults_and_hierarchy_are_unchanged(self):
        self.assertIn("GOLDSCOUT_MAX_CONTEXT_POINTS=4", self.context)
        self.assertIn("GOLDSCOUT_MAX_NEWS_CONTEXT_POINTS=2", self.context)
        self.assertIn("input int    NewsScoreMaxPoints       = 2;", self.ea)
        self.assertIn("input bool   RequireFreshWorldNews    = false;", self.ea)
        self.assertIn("input bool   BlockHighNewsRisk        = false;", self.ea)
        self.assertIn("input int    ArmScoreThreshold       = 58;", self.ea)
        self.assertIn("input int    MinScoreToTrade         = 74;", self.ea)
        self.assertIn("input bool   EnableLiveTrading      = false;", self.ea)
        self.assertIn("input double RiskPercent             = 5.0;", self.ea)
        self.assertNotIn("PERIOD_M30", self.ea)

    def test_critical_scheduled_usd_macro_block_is_preserved(self):
        self.assertIn("CalendarValueHistory(values, from, to, \"US\", \"USD\")", self.ea)
        self.assertIn("ev.importance == CALENDAR_IMPORTANCE_HIGH", self.ea)
        self.assertIn("if(HasHighImpactUSDNews())", self.ea)

    def test_dashboard_contract_exposes_session_and_news_quality(self):
        for field in (
            "session_phase",
            "session_volatility",
            "session_open_impulse_atr",
            "session_long_points",
            "session_short_points",
            "active_market_centers",
            "data_risk",
            "sources_ok",
            "source_count",
        ):
            self.assertIn(f'\\"{field}\\"', self.ea)


if __name__ == "__main__":
    unittest.main()
