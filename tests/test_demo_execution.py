"""Deterministic contracts for DEMO-only MT5 execution."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EA_PATH = ROOT / "MT5" / "Experts" / "XAU_GoldScout_H1.mq5"
POLICY_PATH = ROOT / "MT5" / "Include" / "GoldScout" / "DemoExecution.mqh"
DASHBOARD_PATH = ROOT / "dashboard" / "index.html"


def demo_execution_allowed(requested: bool, account_mode: str) -> tuple[bool, str]:
    if account_mode == "REAL":
        return False, "REAL_ACCOUNT_HARD_BLOCK"
    if account_mode != "DEMO":
        return False, "NON_DEMO_ACCOUNT_HARD_BLOCK"
    if not requested:
        return False, "DEMO_EXECUTION_DISABLED"
    return True, "DEMO_ACCOUNT_CONFIRMED"


def remaining_budget(daily_budget: float, realized_loss: float, open_risk: float) -> float:
    return max(0.0, max(0.0, daily_budget) - max(0.0, realized_loss) - max(0.0, open_risk))


def execution_price(direction: str, bid: float, ask: float) -> float:
    return ask if direction == "LONG" else bid


class DemoExecutionPolicyTests(unittest.TestCase):
    def test_demo_account_allows_execution_when_requested(self):
        self.assertEqual(demo_execution_allowed(True, "DEMO"), (True, "DEMO_ACCOUNT_CONFIRMED"))

    def test_real_account_is_hard_blocked(self):
        self.assertEqual(demo_execution_allowed(True, "REAL"), (False, "REAL_ACCOUNT_HARD_BLOCK"))
        self.assertEqual(demo_execution_allowed(False, "REAL"), (False, "REAL_ACCOUNT_HARD_BLOCK"))

    def test_unknown_and_contest_accounts_are_blocked(self):
        for mode in ("UNKNOWN", "CONTEST"):
            self.assertFalse(demo_execution_allowed(True, mode)[0])

    def test_disabled_toggle_never_submits(self):
        self.assertEqual(demo_execution_allowed(False, "DEMO"), (False, "DEMO_EXECUTION_DISABLED"))

    def test_buy_uses_ask_and_sell_uses_bid(self):
        self.assertEqual(execution_price("LONG", 2350.10, 2350.30), 2350.30)
        self.assertEqual(execution_price("SHORT", 2350.10, 2350.30), 2350.10)

    def test_aggregate_open_risk_reduces_daily_budget(self):
        self.assertEqual(remaining_budget(50.0, 10.0, 25.0), 15.0)
        self.assertEqual(remaining_budget(50.0, 20.0, 35.0), 0.0)

    def test_realized_loss_is_not_offset_by_open_capacity(self):
        self.assertEqual(remaining_budget(10.0, 6.0, 0.0), 4.0)
        self.assertEqual(remaining_budget(10.0, 6.0, 4.0), 0.0)


class DemoExecutionSourceContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ea = EA_PATH.read_text(encoding="utf-8")
        cls.policy = POLICY_PATH.read_text(encoding="utf-8")
        cls.dashboard = DASHBOARD_PATH.read_text(encoding="utf-8")

    def test_safe_defaults_and_original_risk_are_intact(self):
        self.assertIn("input bool   EnableDemoExecution    = false;", self.ea)
        self.assertIn("input bool   EnableLiveTrading      = false;", self.ea)
        self.assertIn("input double RiskPercent             = 5.0;", self.ea)
        self.assertIn("input double DailyLossLimitPercent   = 5.0;", self.ea)
        self.assertIn("input int    MinScoreToTrade         = 74;", self.ea)
        self.assertIn("input int    ArmScoreThreshold       = 58;", self.ea)

    def test_account_mode_hard_block_is_not_input_overridable(self):
        self.assertIn("accountMode==ACCOUNT_TRADE_MODE_REAL", self.policy)
        self.assertIn('reason="REAL_ACCOUNT_HARD_BLOCK"', self.policy)
        self.assertIn("accountMode!=ACCOUNT_TRADE_MODE_DEMO", self.policy)
        self.assertIn("AccountInfoInteger(ACCOUNT_TRADE_MODE)", self.ea)
        self.assertIn("EnableLiveTrading debe permanecer false", self.ea)

    def test_only_order_submission_is_immediately_guarded(self):
        self.assertEqual(self.ea.count("trade.PositionOpen("), 1)
        call = self.ea.index("trade.PositionOpen(_Symbol,type,lots,price,sl,tp,comment)")
        guard = self.ea.rfind("DemoExecutionAllowedNow(finalAuthorization)", 0, call)
        self.assertGreater(guard, 0)
        self.assertLess(call - guard, 2500)
        self.assertNotIn("if(EnableLiveTrading)\n   {\n      requestSent=trade.PositionOpen", self.ea)

    def test_chill_and_god_submit_selected_values(self):
        self.assertIn('string tradeClass=(targetR>=GodTargetR-1e-9?"GOD":"CHILL")', self.ea)
        self.assertIn("tp=tpDecision.selectedTP;", self.ea)
        self.assertIn("double sl=g_tempSL;", self.ea)
        self.assertIn("trade.PositionOpen(_Symbol,type,lots,price,sl,tp,comment)", self.ea)

    def test_partial_fill_and_rejection_are_handled(self):
        self.assertIn("TRADE_RETCODE_DONE_PARTIAL", self.ea)
        self.assertIn("trade.ResultVolume()", self.ea)
        self.assertIn('AppendExecutionEvent("ORDER_REJECTED"', self.ea)
        self.assertIn("ExecutionFailureIsSafelyFinal", self.ea)
        self.assertIn("ReleasePendingH1Entry(entryBar)", self.ea)

    def test_restart_recovers_real_positions_by_magic_and_symbol(self):
        self.assertIn("void RecoverDemoPositionState()", self.ea)
        self.assertIn("POSITION_IDENTIFIER", self.ea)
        self.assertIn("POSITION_MAGIC", self.ea)
        self.assertIn("RecoverDemoPositionState();", self.ea)

    def test_close_reasons_and_realized_pnl_are_recorded(self):
        for value in ("DEAL_REASON_TP", "DEAL_REASON_SL", "DEAL_REASON_CLIENT", "MANUAL", "OTHER"):
            self.assertIn(value, self.policy)
        for field in (
            '\\"gross_pnl\\"', '\\"commission\\"', '\\"swap\\"',
            '\\"net_pnl\\"', '\\"realized_r\\"', '\\"close_reason\\"',
        ):
            self.assertIn(field, self.ea)

    def test_daily_budget_uses_gross_losses_and_open_risk(self):
        self.assertIn("TodayAccountRealizedLoss", self.ea)
        self.assertIn("AccountOpenRisk", self.ea)
        self.assertIn("GSDE_RemainingDailyBudget", self.ea)
        self.assertIn("actualRisk>finalRemainingBudget+1e-6", self.ea)

    def test_multiple_demo_positions_only_bypass_single_position_in_hedging(self):
        self.assertIn("allowMultipleGoldScout || !OnePositionAtATime", self.ea)
        self.assertIn("accountMode==ACCOUNT_TRADE_MODE_DEMO", self.ea)
        netting_branch = self.ea[self.ea.index("bool PositionStateAllowsEntry"):self.ea.index("bool DemoMultiplePositionsAllowed")]
        self.assertIn("requireOurMagic=isHedging", netting_branch)

    def test_same_h1_event_cannot_duplicate(self):
        self.assertIn("ReserveH1EntryPending(entryBar)", self.ea)
        self.assertIn("ConfirmH1Entry(entryBar)", self.ea)
        self.assertIn("SignalEventId(entryBar,direction,setup,score)", self.ea)

    def test_execution_lifecycle_is_append_only_and_linked(self):
        for event in ("SIGNAL", "ORDER_REQUEST", "ORDER_FILLED", "ORDER_REJECTED", "POSITION_OPEN", "POSITION_CLOSE"):
            self.assertIn(f'"{event}"', self.ea)
        self.assertIn('\\"signal_event_id\\"', self.ea)
        self.assertIn("FileSeek(handle,0,SEEK_END)", self.ea)
        self.assertNotIn("FILE_WRITE|FILE_TXT|FILE_ANSI);\n   FileWriteString(handle", self.ea)

    def test_dashboard_has_execution_positions_closures_and_stats(self):
        for label in ("DEMO EXECUTION", "OPEN POSITIONS", "SESSION STATS"):
            self.assertIn(label, self.dashboard)
        for field in ("open_positions", "session_stats", "demo_execution", "open_risk"):
            self.assertIn(field, self.ea)

    def test_observer_links_execution_without_score_effect(self):
        observer = (ROOT / "MT5" / "Include" / "GoldScout" / "MarketObserver.mqh").read_text(encoding="utf-8")
        self.assertIn('\\"signal_event_id\\"', observer)
        self.assertIn('\\"execution_state\\"', observer)
        self.assertIn('\\"source\\":\\"MT5\\",\\"observer_only\\":true,\\"score_effect\\":0', observer)

    def test_scoring_and_stops_are_not_changed_by_execution_mode(self):
        forbidden_assignments = (
            "RiskPercent", "DailyLossLimitPercent", "MinScoreToTrade",
            "ArmScoreThreshold", "ATRStopMultiplier", "MinStopATR", "MaxStopATR",
        )
        execution_block = self.ea[self.ea.index("bool DemoExecutionAllowedNow"):]
        for assignment in forbidden_assignments:
            self.assertNotRegex(execution_block, rf"(?<![A-Za-z0-9_]){assignment}\s*=")
        self.assertNotRegex(self.policy, re.compile(r"longScore|shortScore|BuildSignal"))


if __name__ == "__main__":
    unittest.main()
