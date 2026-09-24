"""Deterministic DEMO ledger projection and runtime source contracts."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dashboard.demo_trade_ledger import project_events, read_ledger
from dashboard import server


ROOT = Path(__file__).resolve().parents[1]
EA = (ROOT / "MT5/Experts/XAU_GoldScout_H1.mq5").read_text(encoding="utf-8")
LEDGER = (ROOT / "MT5/Include/GoldScout/DemoTradeLedger.mqh").read_text(encoding="utf-8")
POLICY = (ROOT / "MT5/Include/GoldScout/DemoExecution.mqh").read_text(encoding="utf-8")


def event(kind: str, number: int, trade: str = "T1", **fields):
    return {"event_id": f"E{number}", "event": kind, "trade_id": trade,
            "account_login": 123, "source": "MT5", "schema_version": 2, **fields}


def close(number: int, trade: str = "T1", **fields):
    row = event("POSITION_CLOSED", number, trade, position_identifier=71, deal_ticket=99,
                close_time=1000, direction="LONG", setup="MOMENTUM",
                gross_pnl=12.0, commission=-1.0, swap=-.5, fees=-.25,
                net_pnl=10.25, initial_risk_account_currency=25.0,
                realized_r_net=.41, close_reason="TP")
    row.update(fields)
    return row


class LedgerProjectionTests(unittest.TestCase):
    def test_successful_fill_and_close_one_trade(self):
        rows = [event("SIGNAL", 1), event("RESERVED", 2),
                event("ORDER_REQUESTED", 3), event("ORDER_FILLED", 4, deal_ticket=91),
                event("POSITION_OPEN", 5, position_identifier=71), close(6)]
        result = project_events(rows)
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["trade_stats"]["trades_closed"], 1)
        self.assertEqual(result["trade_stats"]["net_pnl"], 10.25)
        self.assertEqual(result["closed_trades"][0]["realized_r"], .41)

    def test_rejected_order_never_creates_trade(self):
        result = project_events([event("ORDER_REQUESTED", 1), event("ORDER_REJECTED", 2)])
        self.assertEqual(result["closed_trades"], [])
        self.assertEqual(result["trade_stats"]["trades_closed"], 0)

    def test_rejected_then_open_is_conflict(self):
        result = project_events([event("ORDER_REJECTED", 1),
                                 event("POSITION_OPEN", 2), close(3)])
        self.assertEqual(result["status"], "CONFLICT")
        self.assertEqual(result["closed_trades"], [])

    def test_partial_fill_multiple_deals_and_partial_close_are_one_trade(self):
        rows = [event("ORDER_FILLED", 1, deal_ticket=91, order_ticket=80),
                event("ORDER_FILLED", 2, deal_ticket=92, order_ticket=80),
                event("POSITION_OPEN", 3), event("PARTIALLY_CLOSED", 4, deal_ticket=93),
                close(5, deal_ticket=94)]
        result = project_events(rows)
        self.assertEqual(result["event_count"], 5)
        self.assertEqual(result["trade_stats"]["trades_closed"], 1)
        self.assertEqual(result["closed_trades"][0]["order_tickets"], [80])
        self.assertEqual(result["closed_trades"][0]["deal_tickets"], [91, 92, 93, 94])

    def test_duplicate_deal_event_and_double_restart_do_not_duplicate_stats(self):
        rows = [event("ORDER_FILLED", 1), event("POSITION_OPEN", 2), close(3)]
        for _ in range(3):
            result = project_events(rows + rows)
            self.assertEqual(result["event_count"], 3)
            self.assertEqual(result["trade_stats"]["trades_closed"], 1)

    def test_two_distinct_final_close_records_are_conflict(self):
        result = project_events([close(1), close(2)])
        self.assertEqual(result["status"], "CONFLICT")
        self.assertEqual(result["closed_trades"], [])

    def test_close_reasons_are_broker_values_not_price_guesses(self):
        for reason in ("TP", "SL", "MANUAL", "EA_CLOSE", "BROKER", "OTHER", "UNKNOWN"):
            with self.subTest(reason=reason):
                self.assertEqual(project_events([close(1, close_reason=reason)])
                                 ["closed_trades"][0]["close_reason"], reason)

    def test_commission_swap_fees_and_gross_net_are_distinct(self):
        stats = project_events([close(1)])["trade_stats"]
        self.assertEqual(stats["gross_pnl"], 12)
        self.assertEqual(stats["commission_total"], -1)
        self.assertEqual(stats["swap_total"], -.5)
        self.assertEqual(stats["net_pnl"], 10.25)

    def test_inconsistent_broker_net_is_not_counted(self):
        result = project_events([close(1, net_pnl=50)])
        self.assertEqual(result["status"], "CONFLICT")
        self.assertEqual(result["trade_stats"]["trades_closed"], 0)

    def test_inconsistent_realized_r_is_not_counted(self):
        result = project_events([close(1, realized_r_net=4.1)])
        self.assertEqual(result["status"], "CONFLICT")

    def test_unknown_initial_risk_does_not_invent_realized_r(self):
        row = close(1, initial_risk_account_currency=None, realized_r_net=None)
        result = project_events([row])
        self.assertIsNone(result["closed_trades"][0]["realized_r"])
        self.assertIsNone(result["trade_stats"]["expectancy_r"])

    def test_incomplete_close_marker_is_not_counted_as_reconciled_trade(self):
        result = project_events([close(1, deal_ticket=None)])
        self.assertEqual(result["status"], "CONFLICT")
        self.assertEqual(result["trade_stats"]["trades_closed"], 0)

    def test_reconciliation_error_excludes_until_valid_close(self):
        self.assertEqual(project_events([event("RECONCILIATION_ERROR", 1), close(2)])
                         ["trade_stats"]["trades_closed"], 1)
        self.assertEqual(project_events([close(1), event("RECONCILIATION_ERROR", 2)])
                         ["status"], "CONFLICT")

    def test_aliases_are_read_only(self):
        result = project_events([event("ORDER_REQUEST", 1),
                                 event("POSITION_PARTIAL_CLOSE", 2),
                                 close(3)])
        self.assertEqual(result["trade_stats"]["trades_closed"], 1)

    def test_segments_by_setup_direction_class(self):
        rows = [close(1, trade="T1", **{"class": "GOD"}),
                close(2, trade="T2", position_identifier=72,
                      direction="SHORT", setup="BREAKOUT", **{"class": "CHILL"})]
        result = project_events(rows)
        self.assertEqual(result["segments"]["setup"]["MOMENTUM"]["trades"], 1)
        self.assertEqual(result["segments"]["direction"]["SHORT"]["trades"], 1)
        self.assertEqual(result["segments"]["class"]["CHILL"]["trades"], 1)

    def test_account_scope(self):
        result = project_events([close(1), close(2, trade="T2", account_login=999)],
                                account_login=123)
        self.assertEqual(result["trade_stats"]["trades"], 1)

    def test_64_bit_tickets_keep_exact_identity(self):
        first = 2**53 + 1
        second = 2**53 + 2
        rows = [event("ORDER_FILLED", 1, deal_ticket=first, order_ticket=first),
                event("ORDER_FILLED", 2, deal_ticket=second, order_ticket=second),
                close(3, deal_ticket=second, position_identifier=second)]
        result = project_events(rows)
        self.assertEqual(result["closed_trades"][0]["deal_tickets"], [first, second])
        self.assertEqual(result["closed_trades"][0]["order_tickets"], [first, second])

    def test_corrupt_last_line_is_flagged_not_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "market_execution_events.jsonl"
            path.write_text(json.dumps(close(1)) + "\n" + '{"event_id":"BROKEN"', encoding="utf-8")
            result = read_ledger([path])
        self.assertEqual(result["status"], "CORRUPT_LEDGER")
        self.assertEqual(result["trade_stats"]["trades_closed"], 1)
        self.assertEqual(result["errors"][0]["line"], 2)

    def test_missing_file_is_no_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = read_ledger([Path(tmp) / "absent.jsonl"])
        self.assertEqual(result["status"], "NO_DATA")

    def test_dashboard_stats_are_projected_from_common_files_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "market_execution_events.jsonl"
            path.write_text(json.dumps(close(1)) + "\n", encoding="utf-8")
            with patch.object(server, "CANDIDATES", [Path(tmp)]):
                result = server.read_demo_execution_ledger(account_login=123)
        self.assertEqual(result["session_stats"]["trades_closed"], 1)
        self.assertEqual(result["closed_trades"][0]["trade_id"], "T1")

    def test_dashboard_does_not_mix_unknown_account(self):
        self.assertEqual(server.read_demo_execution_ledger(account_login=None)["status"],
                         "ACCOUNT_UNAVAILABLE")

    def test_session_day_filters_closed_rows(self):
        rows = [close(1, trade="T1", close_time=1750000000),
                close(2, trade="T2", close_time=1750100000)]
        day = "2025-06-15"
        result = project_events(rows, day=day)
        self.assertLess(result["session_stats"]["trades"], result["trade_stats"]["trades"])

    def test_slippage_p95_requires_sufficient_sample(self):
        rows = [close(i + 1, trade=f"T{i}", position_identifier=100 + i,
                      entry_slippage_price=float(i) / 100) for i in range(19)]
        self.assertIsNone(project_events(rows)["trade_stats"]["entry_slippage_p95"])
        rows.append(close(20, trade="T19", position_identifier=119, entry_slippage_price=.19))
        self.assertIsNotNone(project_events(rows)["trade_stats"]["entry_slippage_p95"])


class RuntimeSourceContracts(unittest.TestCase):
    def test_safe_defaults_unchanged(self):
        for line in ("EnableLiveTrading      = false", "RiskPercent             = 5.0",
                     "DailyLossLimitPercent   = 5.0", "ArmScoreThreshold       = 58",
                     "MinScoreToTrade         = 74"):
            self.assertIn(line, EA)

    def test_only_one_order_call_still_hard_guarded(self):
        self.assertEqual(EA.count("trade.PositionOpen("), 1)
        call = EA.index("trade.PositionOpen(_Symbol,type,lots,price,sl,tp,comment)")
        self.assertIn("DemoExecutionAllowedNow(finalAuthorization)", EA[call - 1800:call])
        self.assertIn('reason="REAL_ACCOUNT_HARD_BLOCK"', POLICY)

    def test_new_events_use_canonical_names(self):
        self.assertIn('AppendExecutionEvent("ORDER_REQUESTED"', EA)
        self.assertIn('AppendExecutionEvent("PARTIALLY_CLOSED"', EA)
        self.assertNotIn('AppendExecutionEvent("ORDER_REQUEST"', EA)
        self.assertNotIn('AppendExecutionEvent("POSITION_PARTIAL_CLOSE"', EA)

    def test_initial_risk_never_recomputed_from_final_stop(self):
        summary = EA[EA.index("bool BuildClosedTradeSummary"):EA.index("bool AppendClosedTradeRecord")]
        self.assertIn("ledgerState.initialRisk", summary)
        self.assertNotIn("PositionRiskAtStop(_Symbol,positionType,entryVolume", summary)
        self.assertIn("DEAL_FEE", summary)

    def test_broker_entry_and_manual_close_ownership(self):
        self.assertIn("PositionBelongsToGoldScout(positionId)", EA)
        self.assertIn("RecordEntryDeal(trans.deal,result.retcode)", EA)
        self.assertIn('if(dealReason==DEAL_REASON_CLIENT', POLICY)
        self.assertIn('return "MANUAL"', POLICY)

    def test_restart_reconciles_both_directions(self):
        recovery = EA[EA.index("void RecoverDemoPositionState()"):EA.index("bool DemoLedgerNeedsReconciliation()")]
        self.assertIn('"LEDGER_CLOSED_BROKER_OPEN"', recovery)
        self.assertIn('"RECOVERED_ORPHAN"', recovery)
        self.assertIn("BuildClosedTradeSummary(state.positionId,closedTrade)", recovery)
        self.assertIn("g_ledgerReconciliationBlocked=true", recovery)

    def test_ledger_detects_partial_jsonl_and_flushes(self):
        self.assertIn("GSDL_ValidJsonObject(line)", LEDGER)
        self.assertIn("return !quoted && !escaped && depth==0;", LEDGER)
        self.assertIn("FileFlush(handle)", LEDGER)
        self.assertIn("HasEvent(eventId)", LEDGER)

    def test_unknown_fill_evidence_serialized_null(self):
        self.assertIn('"null"', EA)
        self.assertIn("JsonNumberOrNull(signedSlippageKnown,signedSlippage)", EA)
        self.assertIn("JsonNumberOrNull(fillQuoteKnown,fillTick.ask-fillTick.bid)", EA)

    def test_ambiguous_order_is_not_recorded_as_rejection(self):
        failed = EA[EA.index('bool released=false;', EA.index('requestSent=trade.PositionOpen(')):
                    EA.index('UpdateDashboard();', EA.index('bool released=false;', EA.index('requestSent=trade.PositionOpen(')))]
        self.assertIn('ExecutionFailureIsSafelyFinal(requestSent,retcode,deal)', failed)
        self.assertIn('safelyFinal?"ORDER_REJECTED":"RECONCILIATION_ERROR"', failed)
        self.assertIn('"AMBIGUOUS_ORDER_RESULT"', failed)
        self.assertIn('g_ledgerReconciliationBlocked=true', failed)

    def test_ambiguous_order_survives_restart_and_blocks_new_demo(self):
        self.assertIn('GSDL_JsonField(json,"reconciliation_status")=="AMBIGUOUS_ORDER_RESULT"', LEDGER)
        self.assertIn('ArrayResize(m_ambiguousTrades,0)', LEDGER)
        self.assertIn('g_demoLedger.AmbiguousOrders()', EA)
        self.assertIn('if(eventForOrder=="ORDER_FILLED" && tradeIdForOrder!="")', LEDGER)
        self.assertLess(LEDGER.index('if(recordAccount!=""'),
                        LEDGER.index('GSDL_JsonField(json,"reconciliation_status")'))

    def test_ledger_recovery_and_deal_capture_are_demo_only(self):
        startup = EA[EA.index('int OnInit()'):EA.index('void OnDeinit(')]
        timer = EA[EA.index('void OnTimer()'):EA.index('void OnTradeTransaction(')]
        transaction = EA[EA.index('void OnTradeTransaction('):EA.index('void OnTick()')]
        for block in (startup, timer):
            self.assertIn('ledgerAccountMode==ACCOUNT_TRADE_MODE_DEMO', block)
        self.assertIn('ledgerAccountMode!=ACCOUNT_TRADE_MODE_DEMO) return;', transaction)


if __name__ == "__main__":
    unittest.main()
