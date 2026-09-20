import unittest
from pathlib import Path
from types import SimpleNamespace

from research.analyze_armed_invalidation import (
    _checkpoint_features,
    _periods,
    classify_canceled_outcome,
    evaluate_invalidation_v1,
    infer_setup,
    recommendation,
    summarize,
)


ROOT = Path(__file__).resolve().parents[1]
EA_PATH = ROOT / "MT5" / "Experts" / "XAU_GoldScout_H1.mq5"
POLICY_PATH = ROOT / "MT5" / "Include" / "GoldScout" / "ArmedInvalidation.mqh"
OBSERVER_PATH = ROOT / "MT5" / "Include" / "GoldScout" / "MarketObserver.mqh"


class StaticResolver:
    def __init__(self, *, rsi=60.3, atr=10.0, close_timestamp=1000):
        self.context = {
            "current": SimpleNamespace(indicators={"rsi": rsi, "atr": atr}),
            "resolved_close_timestamp": close_timestamp,
        }
        self.requested = []

    def resolve(self, timestamp):
        self.requested.append(timestamp)
        return self.context


def episode(index, period="TRAIN", direction="SHORT", setup="MOMENTUM", cancelled=True, label="SAVED_LOSER", ret=-1.0):
    return {
        "episode_id": f"e-{index}",
        "armed_at": index,
        "period": period,
        "direction": direction,
        "setup": setup,
        "cancelled": cancelled,
        "outcome_label": label if cancelled else "NOT_CANCELLED",
        "future_return_atr": ret if cancelled else None,
        "mfe_atr": 0.2 if cancelled else None,
        "mae_atr": -1.2 if cancelled else None,
        "time_to_cancel_minutes": 15.0 if cancelled else None,
    }


class ArmedInvalidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ea = EA_PATH.read_text(encoding="utf-8")
        cls.policy = POLICY_PATH.read_text(encoding="utf-8")
        cls.observer = OBSERVER_PATH.read_text(encoding="utf-8")

    def test_short_full_counter_confluence_cancels(self):
        self.assertEqual(evaluate_invalidation_v1("SHORT", "LONG", 60.3, 2.67), ("CANCEL", "COUNTER_BREAKOUT_CONFIRMED"))

    def test_short_without_breakout_keeps(self):
        self.assertEqual(evaluate_invalidation_v1("SHORT", "NONE", 60.3, 2.67)[0], "KEEP")

    def test_short_low_rsi_keeps(self):
        self.assertEqual(evaluate_invalidation_v1("SHORT", "LONG", 54.9, 2.67)[1], "RSI_NOT_CONFIRMED")

    def test_short_low_impulse_keeps(self):
        self.assertEqual(evaluate_invalidation_v1("SHORT", "LONG", 60.3, 1.49)[1], "IMPULSE_BELOW_THRESHOLD")

    def test_long_rule_is_symmetric(self):
        self.assertEqual(evaluate_invalidation_v1("LONG", "SHORT", 44.0, -1.5)[0], "CANCEL")
        self.assertEqual(evaluate_invalidation_v1("LONG", "SHORT", 45.1, -2.0)[0], "KEEP")

    def test_wick_only_has_no_confirmed_breakout_and_keeps(self):
        self.assertEqual(evaluate_invalidation_v1("LONG", "NONE", 40.0, -3.0)[1], "NO_CONFIRMED_COUNTER_BREAKOUT")
        self.assertIn("latest CLOSED M15 close", self.ea)
        self.assertIn("g_m15TimingEvidence.closedBarTime>0", self.ea)

    def test_checkpoint_is_causal_and_uses_closed_observation(self):
        resolver = StaticResolver(rsi=60.3, atr=10.0, close_timestamp=900)
        observation = {
            "_timestamp_ms": 1000,
            "close": 126.7,
            "m15_timing": {"available": True, "breakout": "LONG"},
        }
        values = _checkpoint_features(observation, 100.0, resolver)
        self.assertEqual(resolver.requested, [1000])
        self.assertEqual(values["resolved_h1_close_timestamp"], 900)
        self.assertAlmostEqual(values["signed_impulse_atr"], 2.67)

    def test_cancel_clears_armed_state_before_trigger_or_order(self):
        monitor = self.ea[self.ea.index("void MonitorIntrabar()") : self.ea.index("bool CanOpenTrade()")]
        self.assertLess(monitor.index("EvaluateArmedSetupInvalidation()"), monitor.index("IntrabarTrigger("))
        cancel = self.ea[self.ea.index("bool EvaluateArmedSetupInvalidation()") : self.ea.index("bool CancelledCandidateSuppressed")]
        for statement in ('g_armed=false;', 'g_armedDirection=0;', 'g_armedScore=0;', 'g_armedSetup="-";'):
            self.assertIn(statement, cancel)
        self.assertNotIn("PositionOpen", cancel)

    def test_same_evidence_same_setup_is_suppressed(self):
        self.assertIn("bool CancelledCandidateSuppressed", self.ea)
        self.assertIn("iTime(_Symbol,PERIOD_H1,0)==g_cancelledPlanH1", self.ea)
        self.assertIn("if(!CancelledCandidateSuppressed(direction,setup))", self.ea)

    def test_feature_false_preserves_original_candidate_assignment(self):
        self.assertIn("input bool   UseArmedInvalidationV1 = false;", self.ea)
        monitor = self.ea[self.ea.index("void MonitorIntrabar()") : self.ea.index("bool CanOpenTrade()")]
        disabled = monitor[monitor.index("if(!ArmedInvalidationRuntimeEnabled())") :]
        self.assertIn("g_armed=true;", disabled)
        self.assertIn("g_armedDirection=direction;", disabled)
        self.assertIn("g_armedScore=score;", disabled)
        self.assertIn("g_armedSetup=setup;", disabled)

    def test_real_account_has_no_feature_effect_and_execution_remains_blocked(self):
        self.assertIn("return mode!=ACCOUNT_TRADE_MODE_REAL;", self.ea)
        self.assertIn("input bool   EnableLiveTrading      = false;", self.ea)
        self.assertIn('decision.reason="REAL_ACCOUNT_NO_EFFECT";', self.policy)

    def test_observer_records_cancel_without_score_effect(self):
        self.assertIn('snapshotType="ARMED_CANCELLED";', self.observer)
        self.assertIn('\\"source\\":\\"MT5\\",\\"observer_only\\":true,\\"score_effect\\":0', self.observer)
        for field in ("previous_direction", "previous_setup", "previous_score", "breakout_direction", "impulse_atr", "structure_state"):
            self.assertIn(field, self.observer)

    def test_setup_inference_matches_existing_priority(self):
        base = {"h1_structure": {"breakout_long": True, "pullback_long": True, "momentum_long": True}}
        self.assertEqual(infer_setup(base, "LONG"), "BREAKOUT")
        self.assertEqual(infer_setup({"h1_structure": {"pullback_short": True}}, "SHORT"), "PULLBACK")
        self.assertEqual(infer_setup({"h1_structure": {}}, "SHORT"), "CONTINUATION SHORT")

    def test_outcome_labels_are_conservative(self):
        self.assertEqual(classify_canceled_outcome({"future_return_atr": -0.5, "mfe_atr": 0.2, "mae_atr": -1.0}), "SAVED_LOSER")
        self.assertEqual(classify_canceled_outcome({"future_return_atr": 0.1, "mfe_atr": 1.0, "mae_atr": -0.2}), "KILLED_WINNER")
        self.assertEqual(classify_canceled_outcome({"future_return_atr": -0.1, "mfe_atr": 0.9, "mae_atr": -0.9}), "INCONCLUSIVE")

    def test_temporal_split_is_60_20_20(self):
        rows = [episode(index, cancelled=False) for index in range(10)]
        _periods(rows)
        self.assertEqual([row["period"] for row in rows].count("TRAIN"), 6)
        self.assertEqual([row["period"] for row in rows].count("VALIDATION"), 2)
        self.assertEqual([row["period"] for row in rows].count("OOS"), 2)

    def test_summary_and_recommendation_do_not_promote_small_sample(self):
        rows = [episode(index, period=("TRAIN" if index < 6 else "VALIDATION" if index < 8 else "OOS")) for index in range(10)]
        summary = summarize(rows)
        self.assertEqual(recommendation(summary), "KEEP DIAGNOSTIC")
        self.assertEqual(next(row for row in summary if row["period"] == "ALL" and row["segment"] == "ALL")["cancelled"], 10)

    def test_scoring_risk_tp_sl_and_thresholds_are_intact(self):
        self.assertIn("input bool   EnableLiveTrading      = false;", self.ea)
        self.assertIn("input double RiskPercent             = 5.0;", self.ea)
        self.assertIn("input double DailyLossLimitPercent   = 5.0;", self.ea)
        self.assertIn("input int    MinScoreToTrade         = 74;", self.ea)
        self.assertIn("input int    ArmScoreThreshold       = 58;", self.ea)
        self.assertEqual(self.ea.count("trade.PositionOpen("), 1)
        self.assertNotIn("score", self.policy.lower().replace("score-neutral", ""))


if __name__ == "__main__":
    unittest.main()
