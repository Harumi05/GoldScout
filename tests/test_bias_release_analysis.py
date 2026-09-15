import unittest
from pathlib import Path
from types import SimpleNamespace

from research.analyze_bias_release import (
    RULE_NAMES,
    detect_rule_events,
    evaluate_release_path,
    opposite_direction,
    rule_triggered,
    summarize_rules,
    transition_features,
)


ROOT = Path(__file__).resolve().parents[1]


def point(*, open_=100.0, high=101.0, low=99.0, close=100.0, ticks=100, **indicators):
    return SimpleNamespace(
        bar={"open": open_, "high": high, "low": low, "close": close, "tick_count": ticks},
        indicators=indicators,
    )


def h1_context_for_short_transition():
    previous2 = point(high=100.0, low=98.0, close=99.5, plus_di=32.0, minus_di=15.0, rsi=56.0, ema20=99.0, adx=17.0)
    previous = point(open_=99.0, high=102.0, low=99.0, close=101.0, plus_di=28.0, minus_di=18.0, rsi=54.0, ema20=100.0, adx=18.0)
    current = point(open_=101.0, high=101.2, low=97.5, close=98.0, ticks=130, plus_di=14.0, minus_di=29.0, rsi=44.0, ema20=99.5, adx=21.0, atr=2.0)
    prior = [point(ticks=100) for _ in range(20)]
    return {"current": current, "previous": previous, "previous2": previous2, "prior20": prior}


class StaticResolver:
    def __init__(self, context):
        self.context = context

    def resolve(self, _timestamp):
        return self.context


class BiasReleaseAnalysisTests(unittest.TestCase):
    def test_long_and_short_release_only_target_neutral_opposite_diagnostic(self):
        self.assertEqual(opposite_direction("LONG"), "SHORT")
        self.assertEqual(opposite_direction("SHORT"), "LONG")
        with self.assertRaises(ValueError):
            opposite_direction("NEUTRAL")

    def test_m15_opposite_alignment_and_momentum_trigger_rule_a(self):
        observation = {
            "m15_timing": {"available": True, "structure": "BEARISH", "momentum": "SHORT"},
            "h1_structure": {},
            "h1_regime": {},
        }
        features = transition_features(observation, "LONG", None)
        self.assertTrue(features["m15_opposite_alignment"])
        self.assertTrue(features["m15_opposite_momentum"])
        self.assertTrue(rule_triggered("RULE_A", features))

    def test_dmi_cross_and_rsi_confirmation_trigger_rule_b(self):
        observation = {
            "m15_timing": {"available": True, "structure": "BEARISH"},
            "h1_structure": {},
            "h1_regime": {},
        }
        features = transition_features(observation, "LONG", h1_context_for_short_transition())
        self.assertTrue(features["dmi_cross"])
        self.assertTrue(features["rsi_confirmation"])
        self.assertTrue(features["rsi_cross"])
        self.assertTrue(rule_triggered("RULE_B", features))

    def test_breakout_recovery_and_ema20_cross_trigger_rule_c(self):
        observation = {
            "m15_timing": {"available": True, "structure": "BEARISH", "recovery": "SHORT"},
            "h1_structure": {"breakout_short": True},
            "h1_regime": {},
        }
        features = transition_features(observation, "LONG", h1_context_for_short_transition())
        self.assertTrue(features["opposite_breakout_recovery"])
        self.assertTrue(features["ema20_reclaim_loss"])
        self.assertTrue(rule_triggered("RULE_C", features))

    def test_false_release_uses_temporal_order_and_tracks_later_opposite_move(self):
        bars = [
            {"close_timestamp": 1_800_000, "high": 101.2, "low": 99.7},
            {"close_timestamp": 2_700_000, "high": 101.4, "low": 97.0},
        ]
        result = evaluate_release_path(
            bars,
            trigger_timestamp=900_000,
            end_timestamp=3_600_000,
            reference_price=100.0,
            atr=2.0,
            stale_direction="LONG",
        )
        self.assertEqual(result["release_outcome"], "FALSE_RELEASE")
        self.assertTrue(result["false_release"])
        self.assertAlmostEqual(result["potential_adverse_avoided_atr"], 1.5)

    def test_no_lookahead_bars_outside_causal_window_are_ignored(self):
        bars = [
            {"close_timestamp": 900_000, "high": 110.0, "low": 90.0},
            {"close_timestamp": 1_800_000, "high": 100.2, "low": 98.0},
            {"close_timestamp": 4_500_000, "high": 110.0, "low": 90.0},
        ]
        result = evaluate_release_path(
            bars,
            trigger_timestamp=900_000,
            end_timestamp=3_600_000,
            reference_price=100.0,
            atr=2.0,
            stale_direction="LONG",
        )
        self.assertEqual(result["release_outcome"], "TRUE_RELEASE")
        self.assertEqual(result["bars_evaluated"], 1)

    def test_detection_reports_lead_time_and_keeps_score_effect_zero(self):
        episode = {
            "episode_id": "episode-1",
            "period": "OOS",
            "source_timeframe": "M15",
            "stale_direction": "LONG",
            "new_direction": "SHORT",
            "transition": "LONG_TO_BEARISH",
            "start_timestamp": 0,
            "classic_change_timestamp": 7_200_000,
        }
        observation = {
            "_timestamp_ms": 900_000,
            "event_id": "obs-1",
            "long_score": 70,
            "short_score": 60,
            "close": 100.0,
            "m15_timing": {"available": True, "structure": "BEARISH", "momentum": "SHORT"},
            "h1_structure": {},
            "h1_regime": {},
        }
        bars = [
            {"close_timestamp": 1_800_000, "high": 100.2, "low": 98.0},
            {"close_timestamp": 7_200_000, "high": 100.0, "low": 97.0},
        ]
        _, events = detect_rule_events([episode], [observation], StaticResolver(h1_context_for_short_transition()), bars)
        event = next(item for item in events if item["rule"] == "RULE_A")
        self.assertEqual(event["lead_minutes"], 105.0)
        self.assertEqual(event["bias_release_state"], "BIAS_RELEASE_CANDIDATE")
        self.assertEqual(event["simulated_action"], "LONG_BIAS_TO_NEUTRAL")
        self.assertEqual(event["score_effect"], 0)
        self.assertTrue(event["diagnostic_only"])

    def test_summary_preserves_train_validation_oos_and_long_short(self):
        states = []
        events = []
        for period, transition in (
            ("TRAIN", "LONG_TO_BEARISH"),
            ("VALIDATION", "SHORT_TO_BULLISH"),
            ("OOS", "LONG_TO_BEARISH"),
        ):
            states.append({"period": period, "transition": transition, "score_lag": True})
            events.append({
                "period": period,
                "transition": transition,
                "rule": "RULE_A",
                "release_outcome": "TRUE_RELEASE",
                "lead_minutes": 60.0,
                "potential_adverse_avoided_atr": 1.0,
                "potential_adverse_avoided_price": 2.0,
                "opposite_mfe_atr": 1.0,
                "opposite_mae_atr": 0.1,
                "original_direction_resumed": False,
            })
        rows = summarize_rules(states, events)
        for period in ("TRAIN", "VALIDATION", "OOS"):
            row = next(item for item in rows if item["period"] == period and item["transition"] == "ALL" and item["rule"] == "RULE_A")
            self.assertEqual(row["episodes_detected"], 1)
        long_row = next(item for item in rows if item["period"] == "ALL" and item["transition"] == "LONG_TO_BEARISH" and item["rule"] == "RULE_A")
        short_row = next(item for item in rows if item["period"] == "ALL" and item["transition"] == "SHORT_TO_BULLISH" and item["rule"] == "RULE_A")
        self.assertEqual(long_row["episodes_detected"], 2)
        self.assertEqual(short_row["episodes_detected"], 1)

    def test_analysis_is_offline_and_live_safety_defaults_are_intact(self):
        source = (ROOT / "research" / "analyze_bias_release.py").read_text(encoding="utf-8")
        ea = (ROOT / "MT5" / "Experts" / "XAU_GoldScout_H1.mq5").read_text(encoding="utf-8")
        self.assertNotIn("OrderSend(", source)
        self.assertNotIn("trade.Buy", source)
        self.assertIn("score_effect\": 0", source)
        self.assertRegex(ea, r"input\s+bool\s+EnableLiveTrading\s*=\s*false\s*;")
        self.assertRegex(ea, r"input\s+double\s+RiskPercent\s*=\s*5\.0\s*;")

    def test_all_five_rules_are_fixed_and_no_automatic_rule_search_exists(self):
        self.assertEqual(RULE_NAMES, ("RULE_A", "RULE_B", "RULE_C", "RULE_D", "RULE_E"))


if __name__ == "__main__":
    unittest.main()
