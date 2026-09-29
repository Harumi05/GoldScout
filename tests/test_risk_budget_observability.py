"""Phase 1 characterization, not a new risk policy or MT5 integration substitute.

Frozen function fingerprints are from main 64b96fb. Numerical cases characterize
the existing guards; Node tests execute the actual dashboard projection.
"""
import hashlib
import itertools
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import unittest
from unittest.mock import Mock, patch

from dashboard import server
from dashboard.server import OFFLINE, canonical_lifecycle_value, unknown_risk_snapshot
from test_demo_execution import remaining_budget
from test_ea_safety_invariants import H1Reservation

ROOT = Path(__file__).resolve().parents[1]
EA = (ROOT / 'MT5/Experts/XAU_GoldScout_H1.mq5').read_text(encoding='utf-8')
MODULE = (ROOT / 'MT5/Include/GoldScout/RiskBudgetSnapshot.mqh').read_text(encoding='utf-8')
OBSERVER = (ROOT / 'MT5/Include/GoldScout/MarketObserver.mqh').read_text(encoding='utf-8')
INDEX = (ROOT / 'dashboard/index.html').read_text(encoding='utf-8')


def function(source, name):
    match = re.search(r'\b'+name+r'\([^{};]*\)\s*\{.*?^\}', source, re.M | re.S)
    if not match:
        raise AssertionError(name)
    return match.group()


def daily_code(known, open_known, remaining, loss, opened):
    if not known:
        return 'RISK_STATE_UNKNOWN'
    if not open_known:
        return 'OPEN_RISK_UNKNOWN'
    if remaining > 0:
        return 'NONE'
    if loss > 0 and opened > 0:
        return 'DAILY_BUDGET_EXHAUSTED_COMBINED'
    if loss > 0:
        return 'DAILY_BUDGET_EXHAUSTED_REALIZED_LOSS'
    if opened > 0:
        return 'DAILY_BUDGET_EXHAUSTED_OPEN_RISK'
    return 'DAILY_BUDGET_EXHAUSTED'


def before(known, open_known, budget, loss, opened, dd, spread, phase):
    if not known or dd >= 15:
        return False
    remaining = remaining_budget(budget, loss, opened) if open_known else 0
    return remaining > 0 and spread <= .60 and phase == 'NONE'


def after(known, open_known, budget, loss, opened, dd, spread, phase):
    # Mirrors instrumented guards: classification is observational only.
    remaining = remaining_budget(budget, loss, opened) if open_known else 0
    code = daily_code(known, open_known, remaining, loss, opened)
    if not known or dd >= 15 or not open_known or remaining <= 0:
        return False, code
    return spread <= .60 and phase == 'NONE', code


FROZEN = {
    'DailyLossBudgetAmount': '787ce4a8379b8c00dfc97d49549766d8c7c37feb0ba2b450a82e9b8a5ccfe4a9',
    'AccountOpenRisk': 'ddaefff985cfa7e081b4f4cad2b8d404ef3d7f7db37081a6a66501f48c3f4fa6',
    'CurrentDailyBudgetState': '47847c18c79387c98ddca9bdc3cbaf20c4a877dce95ab33beaa4663ee6e041be',
    'RemainingDailyLossBudget': 'c6fc3f7338ce90387fd8feb48b858dbfea0ebac1eb8437fa12ede2a236aa637c',
    'TodayAccountRealizedLoss': '508038fe7bb3703fafa34b3959852fdf1c490268b6df4aded5ff3ce22af4a7e3',
    'PlannedRiskAmount': '6d335e16235f40d161e622b580e3055b12813ac1eb0e531a28e570273a15b6e2',
    'EntrySafetyStateKey': '8286ff394f44f6971d78f5305e1ff0e662390af36fa0bc5a2a39aaf3b80fbd89',
    'EntryReservationForBar': '66f9065db814f8370bf83429746cf98ad8710b5a303007a71ad0b0fcbc773630',
    'ReserveH1EntryPending': '87956caefcfe8950512a87c045bf80d2e661cccfd402c84e975d3b1310325713',
    'ConfirmH1Entry': '71a96b61799ff9524eb57c25e10b680d7415c800c595a943c651a76966695fe6',
    'ReleasePendingH1Entry': 'a929786f80ccd2688d1e1d302d36ee0d37f3fdc963d34c90ebee97ef07f8bebb',
    'PositionSizeForRisk': '65dc6e3c0a703adde0b3fc29e61bc796ffc5d689e6f3d9ccbaa84392491c2035',
    'RiskAtSL': '616f700ae3885e09a1065dc31ccb2e5fad357bd559dc69c83a8179fd9a45617d',
    'LoadBrokerContract': 'c7da2af6c680593801cd7ebfeee188fe38eebf27ea2b3ddf6428e60ab7309058',
    'NormalizeVolumeDown': '657d1d09554848ecc32d64bd1939e675bfb34ab851c0a7a4bd2f27b570647f1f',
    'MarginAllowsOrder': '9d8b2c53291295f210bdc6fdb5b70efbfd11a3e94bef4c0633a9af4d19f36d70',
    'BuildDynamicStop': '2147934257e0a196799318235656749e716947f4c4aaf258121f509335021c55',
    'BuildAdaptiveStopV2': 'c55ae08c8cdc472933960b17ee4fc0ef935d300ccad72d21c7fc79487d3d02f8',
    'AdaptiveStopV2Allowed': 'c9f9573cbff6125dcc51aea1c2031fa7db29e619404f7c3ef86d4e29066e3edc',
    'BuildTakeProfitStructuralZones': '726df31deb7aaa7fa46ef2efa5fc60049d94010d78bebd4639b0b29ce5a29e1d',
    'BuildSignal': 'cee9fbc5413c7b58f497cb62f98c9decf8f8948074e1a048e5c57b6219dadc6e',
    'IntrabarTrigger': 'fd56d09e977b1f81d9fcd1507f952a3fb428b23345cc53008633b44ec6137b1b',
    'DemoExecutionAllowedNow': '53c199c5a61977a08ddb70aa06e096c69656479bcc5ac007f12cd7fbbc1bfc31',
    'PositionStateAllowsEntry': '6b9ee7cf5d87a1a8c4dfa45dbfe388fbbdb8bb3ef03c7d14c8f57f2ff06ec14a',
}


class RiskBudgetCharacterizationTests(unittest.TestCase):
    def test_a_full_open_risk_blocks(self):
        self.assertEqual(remaining_budget(1000*.05, 0, 50), 0)
        self.assertFalse(before(True, True, 50, 0, 50, 0, .1, 'NONE'))

    def test_b_partial_components_leave_fifteen(self):
        self.assertEqual(remaining_budget(50, 10, 25), 15)

    def test_c_profit_does_not_reimburse_negative_net_deals(self):
        net_deals = [-10, 30]  # profitable close removes open risk, not past loss
        used = max(10, sum(max(0, -net) for net in net_deals))
        self.assertEqual(used, 10)
        self.assertEqual(remaining_budget(50, used, 0), 40)

    def test_d_realized_loss_exhaustion(self):
        self.assertEqual(daily_code(True, True, 0, 50, 0), 'DAILY_BUDGET_EXHAUSTED_REALIZED_LOSS')

    def test_e_open_risk_exhaustion(self):
        self.assertEqual(daily_code(True, True, 0, 0, 50), 'DAILY_BUDGET_EXHAUSTED_OPEN_RISK')

    def test_f_combined_exhaustion(self):
        self.assertEqual(daily_code(True, True, 0, 10, 40), 'DAILY_BUDGET_EXHAUSTED_COMBINED')

    def test_g_state_unknown_fail_closed(self):
        self.assertFalse(after(False, True, 50, 0, 0, 0, .1, 'NONE')[0])
        self.assertEqual(daily_code(False, True, 50, 0, 0), 'RISK_STATE_UNKNOWN')

    def test_h_any_account_position_without_sl_fail_closed(self):
        self.assertFalse(after(True, False, 50, 0, 0, 0, .1, 'NONE')[0])
        self.assertEqual(daily_code(True, False, 0, 0, 0), 'OPEN_RISK_UNKNOWN')
        body = function(EA, 'AccountOpenRisk')
        self.assertIn('sl<=0.0', body)
        self.assertNotIn('POSITION_MAGIC', body)
        self.assertNotIn('_Symbol', body)

    def test_i_same_h1_stays_reserved_after_restart(self):
        reservation = H1Reservation()
        self.assertTrue(reservation.reserve_pending(3600))
        restarted = H1Reservation(reservation.bar, reservation.phase)
        self.assertTrue(restarted.blocks(3600))
        self.assertTrue(restarted.confirm(3600))
        self.assertTrue(restarted.blocks(3600))

    def test_j_magic_does_not_change_account_h1_key(self):
        body = function(EA, 'EntrySafetyStateKey')
        for token in ('MagicNumber', '_Symbol', 'strategy', 'engine'):
            self.assertNotIn(token, body)
        self.assertIn('AccountStateIdentity()', body)

    def test_k_next_h1_can_reserve(self):
        reservation = H1Reservation()
        reservation.reserve_pending(3600)
        reservation.confirm(3600)
        self.assertFalse(reservation.blocks(7200))
        self.assertTrue(reservation.reserve_pending(7200))

    def test_l_spread_boundary_unchanged(self):
        self.assertTrue(after(True, True, 50, 0, 0, 0, .60, 'NONE')[0])
        self.assertFalse(after(True, True, 50, 0, 0, 0, .60001, 'NONE')[0])
        self.assertRegex(function(EA, 'RiskGuardsPass'), r'if\(spread\s*>\s*MaxSpreadUSD\)')

    def test_m_drawdown_boundary_unchanged(self):
        self.assertTrue(after(True, True, 50, 0, 0, 14.999, .1, 'NONE')[0])
        self.assertFalse(after(True, True, 50, 0, 0, 15, .1, 'NONE')[0])
        self.assertRegex(function(EA, 'RiskGuardsPass'), r'if\(dd\s*>=\s*MaxDrawdownPercent\)')

    def test_before_after_grid_equivalence(self):
        cases = itertools.product((False, True), (False, True), (0, 10, 50),
            (0, 10, 50, 60), (0, 25, 50, 60), (0, 14.999, 15, 16),
            (.1, .60, .61), ('NONE', 'PENDING', 'CONFIRMED'))
        for case in cases:
            self.assertEqual(before(*case), after(*case)[0], case)

    def test_negative_net_costs_not_gross_loss_plus_all_costs(self):
        deals = [(10, -2, -1, -1), (-3, -2, 0, -1)]
        self.assertEqual(sum(max(0, -sum(deal)) for deal in deals), 6)
        self.assertIn('if(result<0.0) loss+=-result;', function(EA, 'TodayAccountRealizedLoss'))
        self.assertIn('g_dailyLossUsed=MathMax(observedLoss,MathMax(0.0,storedLoss))', EA)

    def test_protected_function_bodies_frozen_against_main(self):
        for name, expected in FROZEN.items():
            with self.subTest(function=name):
                body = re.sub(r'//[^\n]*|/\*.*?\*/', '', function(EA, name), flags=re.S)
                actual = hashlib.sha256(re.sub(r'\s+', '', body).encode()).hexdigest()
                self.assertEqual(actual, expected)

    def test_snapshot_cannot_write_or_authorize_orders(self):
        for token in ('OrderSend(', 'PositionOpen(', 'GlobalVariableSet', 'FileOpen(', 'longScore', 'shortScore'):
            self.assertNotIn(token, MODULE)
        body = function(EA, 'RefreshRiskSnapshot')
        self.assertNotIn('RefreshPersistentSafetyState()', body)
        self.assertNotIn('ReserveH1EntryPending', body)
        self.assertNotIn('return false', body)

    def test_instrumented_functions_preserve_non_diagnostic_source(self):
        expected = {
            'TryTrade':'0950fc2231a701637bb259c443c945ff5f52520159871a97ad2be4cc5a48e275',
            'CanOpenTrade':'aebd8edc17a01aa877694d830654f82de97cd1620220b936b0ca956c8710a400',
            'RefreshPersistentSafetyState':'72152c67376cb814bbd0c8f5556cb3eb24948285914c916714ef3a5be23aa9f0',
        }
        for name, digest in expected.items():
            with self.subTest(function=name):
                body = function(EA, name)
                body = re.sub(r'SetRiskBlockDiagnostic\([^;]*\);', '', body)
                body = re.sub(r'^\s*g_candidate\w+=[^;]*;', '', body, flags=re.M)
                body = re.sub(r'^\s*g_riskPersistentStateKnown=[^;]*;', '', body, flags=re.M)
                body = body.replace('RefreshRiskSnapshot();', '')
                body = re.sub(r'double candidateOpenRisk=0.0,remainingDailyBudget=0.0;\s*'
                    r'bool candidateBudgetKnown=CurrentDailyBudgetState\(candidateOpenRisk,remainingDailyBudget\);\s*'
                    r'if\(!candidateBudgetKnown \|\| remainingDailyBudget<=0.0\)',
                    'double remainingDailyBudget=RemainingDailyLossBudget(); if(remainingDailyBudget<=0.0)', body)
                body = re.sub(r'string finalCode=[^;]*;', '', body)
                body = re.sub(r'bool finalStateKnown=RefreshPersistentSafetyState\(\);\s*'
                    r'bool finalOpenRiskKnown=finalStateKnown &&\s*'
                    r'CurrentDailyBudgetState\(finalOpenRisk,finalRemainingBudget\);\s*'
                    r'if\(!finalStateKnown \|\| !finalOpenRiskKnown \|\|',
                    'if(!RefreshPersistentSafetyState() || '
                    '!CurrentDailyBudgetState(finalOpenRisk,finalRemainingBudget) ||', body)
                body = re.sub(r'//[^\n]*|/\*.*?\*/', '', body, flags=re.S)
                actual = hashlib.sha256(re.sub(r'\s+', '', body).encode()).hexdigest()
                self.assertEqual(actual, digest)

    def test_acceptance_does_not_read_snapshot(self):
        trade = function(EA, 'TryTrade')
        self.assertNotRegex(trade, r'if\([^\n]*g_riskSnapshot')
        self.assertIn('bool finalOpenRiskKnown=finalStateKnown &&', trade)
        self.assertIn('actualRisk>finalRemainingBudget+1e-6', trade)
        self.assertEqual(trade.count('trade.PositionOpen('), 1)

    def test_pending_money_explicitly_unsupported_and_null(self):
        self.assertIn(r'\"pending_risk_supported\":false', MODULE)
        self.assertIn(r'\"pending_risk_amount\":null', MODULE)
        self.assertIn('NOT_SUPPORTED', MODULE)
        self.assertNotIn('pendingRisk', function(EA, 'CurrentDailyBudgetState'))

    def test_single_nested_contract_dashboard_and_observer(self):
        self.assertIn('context.riskBudgetJson=GSRB_Json(g_riskSnapshot);', EA)
        self.assertIn(r'\"risk_budget\":', function(EA, 'UpdateDashboard'))
        self.assertIn('RefreshRiskSnapshot();\n   CaptureMarketObserverState(true);', EA)
        self.assertIn('context.riskBudgetJson', OBSERVER)
        self.assertIn('context.armedInvalidationReason+"|"+context.riskBlockCode', OBSERVER)

    def test_unknown_server_and_json_preserve_null(self):
        risk = json.loads(json.dumps(unknown_risk_snapshot()))
        for field in ('remaining_daily_budget', 'account_open_risk', 'current_drawdown_percent', 'planned_risk_amount'):
            self.assertIsNone(risk[field])
        self.assertFalse(risk['risk_state_known'])
        self.assertEqual(risk['risk_state_health'], 'UNKNOWN')
        self.assertEqual(canonical_lifecycle_value({'risk_budget': risk})['risk_budget'], risk)
        self.assertEqual(OFFLINE['risk_budget'], risk)

    def test_status_endpoint_passes_snapshot_without_recalculating(self):
        risk = {**unknown_risk_snapshot(), 'block_code':'OPEN_RISK_UNKNOWN',
            'realized_daily_loss_used':10, 'account_open_risk':None}
        handler = object.__new__(server.H)
        handler.path = '/api/status'
        handler.send_payload = Mock()
        with patch.object(server, 'read_json', return_value={'symbol':'XAUUSD','risk_budget':risk}), \
                patch.object(server, 'read_news', return_value={}), \
                patch.object(server, 'read_external', return_value={}), \
                patch.object(server, 'read_tradingview', return_value={}), \
                patch.object(server, 'read_market_observer', return_value={}), \
                patch.object(server, 'read_demo_execution_ledger', return_value={}):
            handler.do_GET()
        status, _, body = handler.send_payload.call_args.args
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['risk_budget'], risk)

    def test_small_positive_remaining_is_not_exhausted(self):
        remaining = remaining_budget(50, 0, 49.84)
        self.assertAlmostEqual(remaining, .16)
        self.assertEqual(daily_code(True, True, remaining, 0, 49.84), 'NONE')
        self.assertIn('SIZING_"+sizingBlockReason', EA)

    def test_block_reset_and_candidate_timestamp(self):
        self.assertIn('g_riskBlockReason!=g_lastDecision', function(EA, 'RefreshRiskSnapshot'))
        body = function(EA, 'TryTrade')
        self.assertLess(body.index('g_candidateRiskKnown=false'), body.index('BuildSignal('))
        self.assertIn('g_candidateRiskEvaluatedAt=TimeTradeServer();', body)
        self.assertIn(r'\"candidate_evaluated_at\"', MODULE)
        self.assertIn(r'\"persistent_state_checked_at\"', MODULE)


class RiskDashboardProjectionTests(unittest.TestCase):
    def render(self, risk):
        node = os.environ.get('NODE_BINARY') or shutil.which('node')
        if not node:
            self.skipTest('Node unavailable: browser projection requires JavaScript runtime')
        body = 'function '+function(INDEX, 'renderRiskSnapshot')
        script = ('const els={}; const $=id=>els[id]??=( {textContent:"",style:{}});'
            'const displayMoney=(v,c)=>Number(v).toFixed(2)+" "+c;\n'+body+
            '\nrenderRiskSnapshot('+json.dumps({'risk_budget': risk})+'); console.log(JSON.stringify(els));')
        result = subprocess.run([node, '-e', script], text=True, encoding='utf-8', capture_output=True, check=True)
        return json.loads(result.stdout)

    def test_healthy_known_risk_shows_actual_decomposition(self):
        risk = {**unknown_risk_snapshot(), 'account_currency':'EUR', 'current_equity':1000,
            'target_risk_amount':50, 'daily_budget_amount':50, 'realized_daily_loss_used':10,
            'account_open_risk':25, 'remaining_daily_budget':15, 'planned_risk_amount':15,
            'configured_risk_percent':5, 'h1_reservation_state':'CONFIRMED','risk_state_health':'HEALTHY'}
        rendered = self.render(risk)
        self.assertEqual(rendered['targetRisk']['textContent'], '50.00 EUR')
        self.assertEqual(rendered['effectiveRisk']['textContent'], '15.00 EUR')
        self.assertEqual(rendered['dailyMeter']['style']['width'], '70%')
        self.assertEqual(rendered['h1Reservation']['textContent'], 'CONFIRMED')

    def test_unknown_does_not_display_zero_or_healthy(self):
        rendered = self.render(unknown_risk_snapshot())
        for key in ('targetRisk', 'openRisk', 'dailyRemaining', 'effectiveRisk'):
            self.assertEqual(rendered[key]['textContent'], '—')
        self.assertEqual(rendered['riskHealth']['textContent'], 'UNKNOWN')
        self.assertEqual(rendered['pendingRisk']['textContent'], 'NOT_SUPPORTED')

    def test_real_zero_budget_is_not_unknown(self):
        rendered = self.render({**unknown_risk_snapshot(), 'account_currency':'USD',
            'remaining_daily_budget':0, 'block_code':'DAILY_BUDGET_EXHAUSTED_OPEN_RISK'})
        self.assertEqual(rendered['dailyRemaining']['textContent'], '0.00 USD')
        self.assertEqual(rendered['riskBlockCode']['textContent'], 'DAILY_BUDGET_EXHAUSTED_OPEN_RISK')

    def test_legacy_absent_snapshot_does_not_invent_health(self):
        self.assertEqual(self.render(None)['riskHealth']['textContent'], 'UNKNOWN')

    def test_actual_mql_diagnostic_conditions_match_characterization(self):
        node = os.environ.get('NODE_BINARY') or shutil.which('node')
        if not node:
            self.skipTest('Node unavailable')
        # Execute the exact MQL conditional body with JS-compatible types stripped;
        # no independently rewritten branch policy is substituted here.
        body = function(MODULE, 'GSRB_DailyBlockCode').split('{', 1)[1]
        inputs = [[True, True, 50, 0, 0], [True, True, 0, 0, 50],
            [True, True, 0, 50, 0], [True, True, 0, 10, 40],
            [False, True, 50, 0, 0], [True, False, 0, 0, 0], [True, True, 0, 0, 0]]
        script = ('function code(stateKnown,openRiskKnown,remaining,realizedLoss,openRisk){'+body+
            '\nconsole.log(JSON.stringify('+json.dumps(inputs)+'.map(x=>code(...x))));')
        result = subprocess.run([node, '-e', script], text=True, encoding='utf-8', capture_output=True, check=True)
        self.assertEqual(json.loads(result.stdout), [daily_code(*args) for args in inputs])


if __name__ == '__main__':
    unittest.main()
