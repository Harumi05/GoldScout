"""Runtime audit must not turn stale, legacy, or conflicted evidence into profit."""

from datetime import date, datetime
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from research.audit_runtime_trades import audit, server_time


ROOT = Path(__file__).resolve().parents[1]
LEDGER = 'market_execution_events.jsonl'
SNAPSHOT = 'xau_goldscout_dashboard.json'
STAMP = int((datetime(2026, 10, 20, 12) - datetime(1970, 1, 1)).total_seconds())


def close(**fields):
    return dict(dict(schema_version=2, source='MT5', account_mode='DEMO',
                     account_login=123, event_id='E1', trade_id='T1',
                     event='POSITION_CLOSED', timestamp=STAMP, close_time=STAMP,
                     position_identifier=71, deal_ticket=99, gross_pnl=12.,
                     commission=-1., swap=-.5, fees=-.25, net_pnl=10.25,
                     initial_risk_account_currency=25., realized_r_net=.41), **fields)


class RuntimeAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write(self, rows):
        (self.root / LEDGER).write_text(''.join(json.dumps(r) + '\n' for r in rows), encoding='utf-8')

    def result(self, **kwargs):
        return audit(self.root, as_of_date=date(2026, 10, 22), **kwargs)

    def test_missing_source_is_unknown_not_zero_performance(self):
        result = self.result()
        self.assertEqual(result['audit_status'], 'MISSING')
        self.assertIsNone(result['requested_period_v2_stats'])
        self.assertFalse(result['recent_v2_evidence_available'])

    def test_old_legacy_ledger_and_snapshot_are_not_validated_statistics(self):
        self.write([{'event_id': 'old', 'event': 'POSITION_CLOSED',
                     'close_time': '2026.09.23 12:00:00', 'net_pnl': 100}])
        (self.root / SNAPSHOT).write_text(json.dumps({'updated_at': '2026.09.28 12:00:00',
            'closed_trades': [{'net_pnl': 100, 'setup': 'UNKNOWN'}]}))
        result = self.result()
        self.assertEqual(result['audit_status'], 'NO_V2_EVIDENCE')
        self.assertEqual(result['event_counts']['POSITION_CLOSED'], 1)
        self.assertTrue(result['sources'][LEDGER]['latest_before_requested_dates'])
        self.assertIsNone(result['requested_period_v2_stats'])
        self.assertIsNone(result['unverified_snapshot']['net_pnl'])
        self.assertFalse(result['strategy_validated'])

    def test_account_scoped_demo_v2_metrics_are_available_without_snapshot(self):
        self.write([close()])
        result = self.result()
        self.assertTrue(result['recent_v2_evidence_available'])
        self.assertEqual(result['requested_period_v2_stats']['net_pnl'], 10.25)
        self.assertEqual(result['requested_period_v2_stats']['r_known_trades'], 1)
        self.assertIsNone(result['requested_period_v2_stats']['profit_factor'])
        self.assertFalse(result['strategy_validated'])

    def test_project_before_date_filter_preserves_earlier_fill_risk_checks(self):
        fill = close(event_id='fill', event='ORDER_FILLED', close_time=None,
                     timestamp=STAMP - 10 * 86400, deal_ticket=90,
                     fill_risk_account_currency=50.)
        self.write([fill, close()])
        result = self.result()
        self.assertEqual(result['audit_status'], 'CONFLICT')
        self.assertIsNone(result['requested_period_v2_stats'])

    def test_corrupt_suffix_duplicate_fields_and_nan_invalidate_the_whole_ledger(self):
        for suffix in ('{', '{"event":"SIGNAL","event":"POSITION_CLOSED"}',
                       '{"timestamp":NaN}', '{"timestamp":1e309}', '[1,2]'):
            with self.subTest(suffix=suffix):
                self.write([close()])
                with (self.root / LEDGER).open('a') as handle:
                    handle.write(suffix)
                result = self.result()
                self.assertEqual(result['audit_status'], 'INVALID_JSON')
                self.assertIsNone(result['requested_period_v2_stats'])

    def test_invalid_utf8_is_not_replaced_and_counted_as_valid_evidence(self):
        (self.root / LEDGER).write_bytes(b'{"event":"SIGNAL","bad":"\xff"}\n')
        self.assertEqual(self.result()['audit_status'], 'INVALID_UTF8')

    def test_unknown_costs_keep_net_profit_and_r_unknown(self):
        self.write([close(gross_pnl=12., commission=None, swap=None, fees=None,
                          net_pnl=None, realized_r_net=None)])
        result = self.result()['requested_period_v2_stats']
        self.assertEqual(result['net_unknown_trades'], 1)
        self.assertIsNone(result['net_pnl'])
        self.assertIsNone(result['profit_factor'])
        self.assertIsNone(result['expectancy_r_known_subset'])

    def test_r_without_positive_initial_risk_is_not_treated_as_proven(self):
        self.write([close(initial_risk_account_currency=None)])
        result = self.result()['requested_period_v2_stats']
        self.assertEqual(result['r_known_trades'], 0)
        self.assertIsNone(result['expectancy_r_known_subset'])

    def test_input_changed_while_reading_is_not_accepted_as_a_snapshot(self):
        self.write([close()])
        original = Path.read_bytes

        def changing_read(path):
            content = original(path)
            if path.name == LEDGER:
                path.write_bytes(content + b'\n')
            return content

        with patch.object(Path, 'read_bytes', changing_read):
            result = self.result()
        self.assertEqual(result['audit_status'], 'CHANGED_DURING_READ')
        self.assertIsNone(result['requested_period_v2_stats'])

    def test_multiple_accounts_require_selection_and_never_merge_their_pnl(self):
        self.write([close(), close(account_login=456, event_id='E2', trade_id='T2')])
        self.assertEqual(self.result()['audit_status'], 'ACCOUNT_SELECTION_REQUIRED')
        self.assertEqual(self.result(account_login=456)['requested_period_v2_stats']['closed_trades'], 1)
        self.assertEqual(self.result(account_login=456)['requested_period_v2_stats']['net_pnl'], 10.25)
        self.assertEqual(self.result(account_login=789)['audit_status'], 'SELECTED_ACCOUNT_NOT_FOUND')

    def test_real_unknown_source_and_missing_account_cannot_validate_demo(self):
        for fields, expected in (({'account_mode': 'REAL'}, 'SOURCE_OR_DEMO_MODE_UNVERIFIED'),
                                 ({'source': 'OTHER'}, 'SOURCE_OR_DEMO_MODE_UNVERIFIED'),
                                 ({'account_login': None}, 'ACCOUNT_IDENTITY_MISSING')):
            with self.subTest(fields=fields):
                self.write([close(**fields)])
                self.assertEqual(self.result()['audit_status'], expected)

    def test_exact_duplicates_deduplicate_but_altered_event_id_fails_closed(self):
        self.write([close(), close()])
        self.assertEqual(self.result()['requested_period_v2_stats']['closed_trades'], 1)
        self.write([close(), close(gross_pnl=100)])
        self.assertEqual(self.result()['audit_status'], 'EVENT_IDENTITY_CONFLICT')

    def test_current_mql_reservation_and_close_format_needs_matching_demo_trade(self):
        reserved = close(event_id='reserve', event='RESERVED')
        terminal = close()
        reserved.pop('account_mode')
        terminal.pop('account_mode')
        fill = close(event_id='fill', event='ORDER_FILLED', deal_ticket=90,
                     fill_risk_account_currency=25.)
        self.write([reserved, fill, terminal])
        with (self.root / LEDGER).open('a') as handle:
            handle.write('\n \t\n')
        self.assertTrue(self.result()['recent_v2_evidence_available'])
        self.write([terminal])
        self.assertEqual(self.result()['audit_status'], 'SOURCE_OR_DEMO_MODE_UNVERIFIED')
        self.write([close(event_id='unrelated', trade_id='T2', event='SIGNAL'), terminal])
        self.assertEqual(self.result()['audit_status'], 'SOURCE_OR_DEMO_MODE_UNVERIFIED')

    def test_recent_legacy_events_do_not_make_old_v2_evidence_recent(self):
        old = close(timestamp=STAMP - 30 * 86400, close_time=STAMP - 30 * 86400)
        self.write([old, {'event_id': 'newlegacy', 'timestamp': STAMP, 'event': 'SIGNAL'}])
        result = self.result()
        self.assertFalse(result['recent_v2_evidence_available'])
        self.assertTrue(result['selected_v2_coverage']['latest_before_requested_dates'])
        self.assertEqual(result['requested_period_v2_stats']['closed_trades'], 0)
        self.assertIsNone(result['requested_period_v2_stats']['net_pnl'])

    def test_future_and_missing_close_dates_do_not_qualify_as_current_data(self):
        self.write([close(timestamp=STAMP + 10 * 86400, close_time=STAMP + 10 * 86400)])
        self.assertFalse(self.result()['recent_v2_evidence_available'])
        self.write([close(timestamp=None, close_time='not-a-date')])
        self.assertEqual(self.result()['audit_status'], 'CLOSE_TIME_UNKNOWN')

    def test_explicit_calendar_window_is_inclusive(self):
        self.write([close(close_time='2026.10.16 00:00:00'),
                    close(event_id='E2', trade_id='T2', close_time='2026.10.22 23:59:59'),
                    close(event_id='E3', trade_id='T3', close_time='2026.10.15 23:59:59')])
        self.assertEqual(self.result()['requested_period_v2_stats']['closed_trades'], 2)

    def test_input_is_unchanged_and_report_omits_account_ids_paths_and_raw_reasons(self):
        self.write([close(reason='private-text-must-not-escape')])
        before = (self.root / LEDGER).read_bytes()
        result = self.result()
        self.assertEqual((self.root / LEDGER).read_bytes(), before)
        self.assertEqual(result['sources'][LEDGER]['sha256'], hashlib.sha256(before).hexdigest())
        report = json.dumps(result)
        self.assertNotIn('private-text-must-not-escape', report)
        self.assertNotIn('account_login', report)
        self.assertNotIn(str(self.root), report)

    def test_server_labels_do_not_assume_utc_or_accept_bool_as_time(self):
        self.assertEqual(server_time('2026.10.20 12:00:00'), server_time(STAMP))
        self.assertIsNone(server_time(True))
        self.assertIsNone(server_time('2026-10-20T12:00:00+00:00'))
        self.assertEqual(server_time('2026-10-20T12:00:00.500').microsecond, 500000)

    def test_cli_missing_evidence_has_explicit_nonzero_status_and_valid_json(self):
        result = subprocess.run([sys.executable, '-m', 'research.audit_runtime_trades',
            '--input-dir', str(self.root), '--as-of-date', '2026-10-22'],
            cwd=ROOT, text=True, capture_output=True)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)['audit_status'], 'MISSING')


if __name__ == '__main__':
    unittest.main()
