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

    def test_reject_then_retry_fill_keeps_attempts_separate(self):
        signal = "GS-SIGNAL-1"
        rows = [event("ORDER_REJECTED", 1, "ATTEMPT-1", signal_event_id=signal,
                      execution_attempt_id="A1"),
                event("ORDER_FILLED", 2, "ATTEMPT-2", signal_event_id=signal,
                      execution_attempt_id="A2", deal_ticket=90),
                event("POSITION_OPEN", 3, "ATTEMPT-2", position_identifier=71),
                close(4, "ATTEMPT-2", signal_event_id=signal,
                      execution_attempt_id="A2")]
        result = project_events(rows)
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["trade_stats"]["trades_closed"], 1)
        self.assertEqual(result["closed_trades"][0]["trade_id"], "ATTEMPT-2")

    def test_two_rejected_retries_same_signal_remain_distinct_after_restart(self):
        rows = [event("ORDER_REJECTED", 1, "A1", signal_event_id="S"),
                event("ORDER_REJECTED", 2, "A2", signal_event_id="S")]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.jsonl"
            path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            for _ in range(2):
                result = read_ledger([path])
                self.assertEqual(result["event_count"], 2)
                self.assertEqual(result["ledger_conflicts"], 0)

    def test_complete_partial_fill_risk_is_sum_not_repeated_order_risk(self):
        for portions in ((25,), (10, 15), (5, 8, 12)):
            with self.subTest(portions=portions):
                fills = [event("ORDER_FILLED", i + 1, deal_ticket=200 + i,
                               fill_risk_account_currency=float(risk))
                         for i, risk in enumerate(portions)]
                result = project_events(fills + [close(99)])
                self.assertEqual(result["status"], "OK")
                self.assertEqual(result["closed_trades"][0]["realized_r"], .41)

    def test_missing_middle_fill_risk_never_divides_by_partial_risk(self):
        fills = [event("ORDER_FILLED", i + 1, deal_ticket=200 + i,
                       fill_risk_account_currency=risk)
                 for i, risk in enumerate((5.0, None, 12.0))]
        valid_unknown = project_events(fills + [close(99,
            initial_risk_account_currency=None, realized_r_net=None)])
        self.assertEqual(valid_unknown["status"], "OK")
        self.assertIsNone(valid_unknown["trade_stats"]["expectancy_r"])
        invented = project_events(fills + [close(99)])
        self.assertEqual(invented["status"], "CONFLICT")

    def test_duplicate_fill_id_ignored_but_distinct_id_same_deal_conflicts(self):
        fill = event("ORDER_FILLED", 1, deal_ticket=201,
                     fill_risk_account_currency=25.0)
        self.assertEqual(project_events([fill, fill, close(3)])["status"], "OK")
        second = event("ORDER_FILLED", 2, deal_ticket=201,
                       fill_risk_account_currency=25.0)
        self.assertEqual(project_events([fill, second, close(3)])["status"], "CONFLICT")

    def test_partial_fill_then_rejected_remainder_retains_known_risk(self):
        rows = [event("ORDER_FILLED", 1, deal_ticket=201,
                      fill_risk_account_currency=25.0),
                event("POSITION_OPEN", 2, position_identifier=71),
                event("ORDER_REJECTED", 3), close(4)]
        self.assertEqual(project_events(rows)["trade_stats"]["trades_closed"], 1)

    def test_cost_getter_missing_keeps_gross_but_excludes_net_cost_stats(self):
        row = close(1, commission=None, net_pnl=None, realized_r_net=None)
        result = project_events([row])
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["trade_stats"]["gross_pnl"], 12.0)
        self.assertIsNone(result["trade_stats"]["net_pnl"])
        self.assertEqual(result["trade_stats"]["net_cost_unknown_trades"], 1)
        self.assertEqual(result["closed_trades"][0]["cost_quality"], "UNKNOWN")

    def test_negative_costs_are_not_invalid(self):
        self.assertEqual(project_events([close(1)])["trade_stats"]["commission_total"], -1.0)

    def test_corrupt_json_duplicate_field_and_invalid_number_are_flagged(self):
        bad = ('{"event_id":"E1","event_id":"E2"}',
               '{"event_id":"E1","gross_pnl":1e+}',
               '{"event_id":"E1","gross_pnl":NaN}',
               '{"event_id":"E1","reason":"unfinished}')
        for line in bad:
            with self.subTest(line=line), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "ledger.jsonl"
                path.write_text(line + "\n", encoding="utf-8")
                self.assertEqual(read_ledger([path])["status"], "CORRUPT_LEDGER")

    def test_escaped_quote_backslash_and_unicode_roundtrip(self):
        row = close(1, reason='broker said "retry" \\ revisión')
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.jsonl"
            path.write_text(json.dumps(row, ensure_ascii=True) + "\n", encoding="utf-8")
            result = read_ledger([path])
            self.assertEqual(result["closed_trades"][0]["reason"], row["reason"])

    def test_large_ledger_restart_does_not_double_count_old_event(self):
        old = close(1)
        filler = [event("SIGNAL", i + 2, f"T{i}", reason="x" * 400)
                  for i in range(700)]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.jsonl"
            path.write_text("".join(json.dumps(row) + "\n" for row in [old, *filler, old]),
                            encoding="utf-8")
            self.assertGreater(path.stat().st_size, 256 * 1024)
            for _ in range(2):
                result = read_ledger([path])
                self.assertEqual(result["trade_stats"]["trades_closed"], 1)
                self.assertEqual(result["event_count"], len(filler) + 1)

    def test_netting_reversal_conflict_visible_even_after_close(self):
        error = event("RECONCILIATION_ERROR", 1,
                      reconciliation_status="NETTING_INOUT_REVERSAL_REQUIRES_SPLIT")
        result = project_events([error, close(2)])
        self.assertEqual(result["status"], "CONFLICT")
        self.assertEqual(result["ledger_conflicts"], 1)
        self.assertEqual(result["conflicting_trade_ids"], ["T1"])
        self.assertEqual(result["closed_trades"], [])

    def test_long_short_netting_reversal_survives_restart(self):
        for direction in ("LONG", "SHORT"):
            with self.subTest(direction=direction), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "ledger.jsonl"
                rows = [event("POSITION_OPEN", 1, position_identifier=71,
                              direction=direction),
                        event("RECONCILIATION_ERROR", 2,
                              position_identifier=71, direction=direction,
                              reconciliation_status="NETTING_INOUT_REVERSAL_REQUIRES_SPLIT")]
                path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
                for _ in range(2):
                    result = read_ledger([path])
                    self.assertEqual(result["status"], "CONFLICT")
                    self.assertEqual(result["closed_trades"], [])

    def test_partial_out_and_hedging_out_by_do_not_create_reversal_conflict(self):
        for reason in ("PARTIAL_OUT", "OUT_BY"):
            with self.subTest(reason=reason):
                rows = [event("POSITION_OPEN", 1, position_identifier=71),
                        event("PARTIALLY_CLOSED", 2, deal_ticket=98,
                              position_identifier=71, close_reason=reason), close(3)]
                self.assertEqual(project_events(rows)["status"], "OK")


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
        self.assertIn("bool GSDL_ParseString(", LEDGER)
        self.assertIn("bool GSDL_ParseNumber(", LEDGER)
        self.assertIn('StringFind(seen,"|"+key+"|")>=0', LEDGER)
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

    def test_attempt_is_claimed_durably_before_order_and_retry_gets_new_id(self):
        claim = LEDGER[LEDGER.index("bool ClaimAttempt("):LEDGER.index("bool Append(")]
        self.assertIn("RefreshAll(handle)", claim)
        self.assertIn("attemptId=StringFormat", claim)
        self.assertIn("FileFlush(handle)", claim)
        self.assertIn("bool AttemptMatches(", LEDGER)
        self.assertIn("g_demoLedger.AttemptMatches(", EA)
        request = EA[EA.index("g_demoLedger.ClaimAttempt("):EA.index("requestSent=trade.PositionOpen(")]
        self.assertIn("g_ledgerRequestAttemptId", request)
        self.assertIn("DemoTradeComment(", request)

    def test_two_instance_critical_dedup_scans_entire_ledger_under_lock(self):
        append = LEDGER[LEDGER.index("bool Append("):LEDGER.index("void MarkUnsafe(")]
        self.assertIn("FILE_SHARE_READ", append)
        self.assertIn("if(!RefreshAll(handle))", append)
        self.assertLess(append.index("if(!RefreshAll(handle))"),
                        append.index("if(!FileSeek(handle,0,SEEK_END))"))
        self.assertIn("if(HasEvent(eventId))", append)

    def test_partial_fill_risk_only_known_when_all_fill_risks_known(self):
        self.assertIn("m_positions[index].fillCount++", LEDGER)
        self.assertIn("m_positions[index].riskFillCount++", LEDGER)
        self.assertIn("m_positions[index].fillCount==m_positions[index].riskFillCount", LEDGER)
        self.assertIn("JsonNumberOrNull(item.initialRiskKnown,item.initialRisk,2)", EA)
        self.assertIn("ledgerState.fillCount==entryDealCount", EA)
        self.assertIn("ledgerState.entryVolume-entryVolume", EA)

    def test_netting_inout_fails_closed_and_restart_does_not_reopen(self):
        self.assertIn('reason="NETTING_INOUT_REVERSAL_REQUIRES_SPLIT"', EA)
        self.assertIn("marginMode==ACCOUNT_MARGIN_MODE_RETAIL_HEDGING", EA)
        self.assertIn("if(entry==DEAL_ENTRY_INOUT) return false;", EA)
        self.assertIn("if(prior.reversalConflict)", EA)
        self.assertIn("if(inLedger && ledgerState.reversalConflict)", EA)
        self.assertIn("!m_positions[index].reversalConflict", LEDGER)

    def test_partial_reversal_and_out_by_keep_existing_close_path(self):
        transaction = EA[EA.index("void OnTradeTransaction("):EA.index("void OnTick()")]
        self.assertIn("entry!=DEAL_ENTRY_OUT_BY", transaction)
        self.assertIn('AppendExecutionEvent("PARTIALLY_CLOSED"', transaction)
        self.assertIn("if(entry==DEAL_ENTRY_INOUT)", transaction)

    def test_all_broker_money_getters_are_checked(self):
        summary = EA[EA.index("bool BuildClosedTradeSummary"):EA.index("bool AppendClosedTradeRecord")]
        for field in ("DEAL_PROFIT", "DEAL_COMMISSION", "DEAL_SWAP", "DEAL_FEE"):
            self.assertIn(f"DealMoneyEvidence(deal,{field},money)", summary)
        self.assertIn("item.grossPnlKnown && item.costsKnown", EA)

    def test_dashboard_exposes_conflict_count(self):
        dashboard = (ROOT / "dashboard/index.html").read_text(encoding="utf-8")
        self.assertIn("ledger.ledger_conflicts", dashboard)
        self.assertIn("ledger.conflicting_trade_ids", dashboard)


if __name__ == "__main__":
    unittest.main()
