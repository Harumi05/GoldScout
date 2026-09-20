"""Deterministic policy/source contracts for recovered Adaptive Stop v2."""

from __future__ import annotations

from pathlib import Path
import unittest

from tests.test_ea_broker_contract import Contract, size_for_risk


ROOT = Path(__file__).resolve().parents[1]
EA = (ROOT / "MT5" / "Experts" / "XAU_GoldScout_H1.mq5").read_text(encoding="utf-8")
OBSERVER = (ROOT / "MT5" / "Include" / "GoldScout" / "MarketObserver.mqh").read_text(encoding="utf-8")
DASHBOARD = (ROOT / "dashboard" / "index.html").read_text(encoding="utf-8")
SERVER = (ROOT / "dashboard" / "server.py").read_text(encoding="utf-8")


def conservative_distance(current: float, atr: float, direction: str, setup: str) -> float:
    floor = 0.0
    if setup == "MOMENTUM" and direction == "LONG":
        floor = 2.0
    elif setup in {"CONTINUATION LONG", "CONTINUATION SHORT", "PULLBACK"}:
        floor = 1.5
    return max(current, floor * atr)


def adaptive_allowed(
    *, toggle: bool, enable_live: bool, enable_demo: bool, account_mode: str | None
) -> bool:
    if not toggle or account_mode is None or account_mode in {"REAL", "UNKNOWN"} or enable_live:
        return False
    if not enable_demo:
        return True
    return account_mode == "DEMO"


def selected_stop_mode(**kwargs) -> str:
    return "ADAPTIVE_V2" if adaptive_allowed(**kwargs) else "CURRENT"


class AdaptiveStopV2RecoveryTests(unittest.TestCase):
    def test_policy_for_every_setup_and_direction(self):
        cases = {
            ("LONG", "BREAKOUT"): 1.2,
            ("SHORT", "BREAKOUT"): 1.2,
            ("LONG", "MOMENTUM"): 2.0,
            ("SHORT", "MOMENTUM"): 1.2,
            ("LONG", "CONTINUATION LONG"): 1.5,
            ("SHORT", "CONTINUATION SHORT"): 1.5,
            ("LONG", "PULLBACK"): 1.5,
            ("SHORT", "PULLBACK"): 1.5,
            ("LONG", "RECOVERY"): 1.2,
            ("SHORT", "RECOVERY"): 1.2,
        }
        for (direction, setup), expected in cases.items():
            with self.subTest(direction=direction, setup=setup):
                self.assertAlmostEqual(conservative_distance(1.2, 1.0, direction, setup), expected)

    def test_feature_flag_false_preserves_current(self):
        self.assertFalse(adaptive_allowed(toggle=False, enable_live=False, enable_demo=False, account_mode="DEMO"))
        self.assertIn("input bool   UseAdaptiveStopV2       = false;", EA)
        self.assertIn("g_tempSL=useAdaptive?adaptiveSL:currentSL;", EA)
        self.assertIn("double sl=g_tempSL;", EA)

    def test_paper_and_verified_demo_gating(self):
        self.assertTrue(adaptive_allowed(toggle=True, enable_live=False, enable_demo=False, account_mode="DEMO"))
        self.assertTrue(adaptive_allowed(toggle=True, enable_live=False, enable_demo=True, account_mode="DEMO"))
        self.assertFalse(adaptive_allowed(toggle=True, enable_live=False, enable_demo=False, account_mode="REAL"))
        self.assertFalse(adaptive_allowed(toggle=True, enable_live=False, enable_demo=True, account_mode="REAL"))
        self.assertFalse(adaptive_allowed(toggle=True, enable_live=False, enable_demo=True, account_mode="UNKNOWN"))
        self.assertFalse(adaptive_allowed(toggle=True, enable_live=False, enable_demo=False, account_mode=None))
        self.assertIn("if(!ReadAccountTradeMode(accountMode))", EA)
        self.assertIn('reason="ACCOUNT_MODE_UNAVAILABLE";', EA)
        self.assertIn('reason="ACCOUNT_MODE_UNKNOWN";', EA)
        self.assertIn("if(!DemoExecutionAllowedNow(demoReason))", EA)
        self.assertIn('reason="DEMO_ACCOUNT_CONFIRMED";', EA)

    def test_real_execution_is_always_blocked(self):
        self.assertFalse(adaptive_allowed(toggle=True, enable_live=True, enable_demo=False, account_mode="REAL"))
        self.assertEqual(
            selected_stop_mode(toggle=True, enable_live=False, enable_demo=False, account_mode="REAL"),
            "CURRENT",
        )
        self.assertIn("if(accountMode==ACCOUNT_TRADE_MODE_REAL)", EA)
        self.assertIn('reason="REAL_ACCOUNT_NO_EFFECT";', EA)
        self.assertIn('g_stopMode=useAdaptive?"ADAPTIVE_V2":"CURRENT";', EA)
        self.assertIn("if(EnableLiveTrading)", EA)
        self.assertIn('reason="REAL_EXECUTION_HARD_BLOCK";', EA)
        self.assertIn("input bool   EnableLiveTrading      = false;", EA)
        self.assertIn("ACCOUNT_TRADE_MODE_DEMO", EA)

    def test_inverse_lotage_preserves_monetary_risk(self):
        contract = Contract(0.01, 100.0, 0.01, 0.01, 2)
        budget = 60.0
        current_lot = size_for_risk(budget, 120.0, 0.0, contract)
        adaptive_lot = size_for_risk(budget, 200.0, 0.0, contract)
        self.assertEqual(current_lot, 0.50)
        self.assertEqual(adaptive_lot, 0.30)
        self.assertLess(adaptive_lot, current_lot)
        self.assertLessEqual(adaptive_lot * 200.0, budget)

    def test_existing_broker_constraints_are_reused(self):
        for token in (
            "ApplyInitialStopConstraints(direction,tick.bid,tick.ask,contract,currentRequestedSL,currentSL)",
            "ApplyInitialStopConstraints(direction,tick.bid,tick.ask,contract,adaptiveRequestedSL,adaptiveSL)",
            "PositionSizeForRisk(type,worstCasePrice,sl,plannedRisk,contract,lots)",
            "RiskAtSL(type,worstCasePrice,sl,lots,actualRisk)",
            "MarginAllowsOrder(type,price,lots,contract,marginMsg)",
            "OrderCalcProfit",
            "OrderCalcMargin",
            "contract.stopsLevel",
            "SYMBOL_TRADE_FREEZE_LEVEL",
            "AlignPriceToTick",
            "NormalizeVolumeDown",
        ):
            self.assertIn(token, EA)

    def test_shadow_telemetry_resets_per_decision(self):
        try_trade = EA[EA.index("void TryTrade()") : EA.index("string ExtractTag")]
        self.assertLess(try_trade.index("ResetAdaptiveStopDiagnostics();"), try_trade.index("BuildSignal("))
        for token in (
            "current_stop_distance",
            "adaptive_stop_distance",
            "selected_stop_distance",
            "stop_evaluated",
        ):
            self.assertIn(token, OBSERVER)
        self.assertIn("STOP MODE — PAPER/DEMO A/B", DASHBOARD)
        self.assertIn('"stop_diagnostics"', SERVER)

    def test_tp_demo_lifecycle_and_safety_contracts_remain_present(self):
        for token in (
            "UseTakeProfitV2",
            "StoreTakeProfitDiagnostics(tpDecision)",
            'AppendExecutionEvent("ORDER_REJECTED"',
            'AppendExecutionEvent("POSITION_CLOSED"',
            "UseArmedInvalidationV1 = false;",
        ):
            self.assertIn(token, EA)
        self.assertIn("input double RiskPercent             = 5.0;", EA)
        self.assertIn("input double DailyLossLimitPercent   = 5.0;", EA)
        self.assertIn("input int    ArmScoreThreshold       = 58;", EA)
        self.assertIn("input int    MinScoreToTrade         = 74;", EA)


if __name__ == "__main__":
    unittest.main()
