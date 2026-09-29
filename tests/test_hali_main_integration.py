"""Hali presentation/auth coexist with authoritative main risk and ledger data."""

import http.client
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'dashboard'))
from dashboard import server
import auth_gateway
import auth_service
from dashboard.demo_trade_ledger import project_events

INDEX = (ROOT / 'dashboard/index.html').read_text(encoding='utf-8')


def javascript_function(name):
    start = INDEX.index('function ' + name + '(')
    end = INDEX.index('\n}', start) + 2
    return INDEX[start:end]


class HaliMainUiTests(unittest.TestCase):
    def javascript(self, names, invocation):
        node = os.environ.get('NODE_BINARY') or shutil.which('node')
        if not node:
            self.skipTest('Node is required for executed UI contracts')
        prefix = '''
const els={};const $=id=>els[id]??={textContent:'',innerHTML:'',style:{setProperty(k,v){this[k]=v}}};
const displayMoney=(v,c='USD')=>v==null?'—':Number(v).toFixed(2)+' '+c;
const money=displayMoney;const esc=v=>String(v??'');
let performanceRows=[], historyFilter='ALL';
function drawEquityCurve(rows){els.curveRows=rows}
'''
        script = prefix + '\n'.join(javascript_function(n) for n in names) + '\n' + invocation
        script += '\nconsole.log(JSON.stringify(els));'
        result = subprocess.run([node, '-e', script], text=True, encoding='utf-8',
                                capture_output=True, check=True)
        return json.loads(result.stdout)

    def risk(self, snapshot):
        return self.javascript(['renderRiskSnapshot'],
                               'renderRiskSnapshot(' + json.dumps(snapshot) + ');')

    def performance(self, data):
        return self.javascript(['tradeRowsValid', 'tradeCloseOrder', 'streakLabel',
                                'renderPerformance'],
                               'renderPerformance(' + json.dumps(data) + ');')

    def test_authoritative_risk_overrides_legacy_conflicting_amounts(self):
        risk = {**server.unknown_risk_snapshot(), 'account_currency': 'EUR',
                'target_risk_amount': 50, 'planned_risk_amount': 4,
                'remaining_daily_budget': 4, 'daily_budget_amount': 10,
                'realized_daily_loss_used': 6, 'account_open_risk': 0}
        result = self.risk({'risk_budget': risk, 'risk_amount': 50, 'open_risk': 900})
        for name in ('risk', 'heroRisk', 'effectiveRisk'):
            self.assertEqual(result[name]['textContent'], '4.00 EUR')
        self.assertEqual(result['openRisk']['textContent'], '0.00 EUR')
        self.assertEqual(result['dailyMeter']['style']['width'], '60%')

    def test_missing_snapshot_does_not_invent_zero_or_healthy_state(self):
        result = self.risk({'open_risk': 0, 'equity': 1000, 'risk_percent': 5})
        for name in ('risk', 'heroRisk', 'effectiveRisk', 'openRisk', 'equity'):
            self.assertEqual(result[name]['textContent'], '—')
        self.assertEqual(result['riskState']['textContent'], 'UNKNOWN')
        self.assertEqual(result['riskUsedPct']['textContent'], '—')

    def test_small_positive_remaining_is_not_a_ui_hard_block(self):
        risk = {**server.unknown_risk_snapshot(), 'risk_state_known': True,
                'risk_state_health': 'HEALTHY', 'budget_status_code': 'NONE',
                'block_code': 'NONE', 'daily_budget_amount': 50,
                'remaining_daily_budget': .01, 'planned_risk_amount': .01}
        result = self.risk({'risk_budget': risk})
        self.assertEqual(result['riskState']['textContent'], 'CAPACIDAD BAJA')
        self.assertNotIn('BLOQUEADAS', result['riskGuardTitle']['textContent'])

    def test_h1_and_budget_block_codes_are_distinct_and_preserved(self):
        risk = {**server.unknown_risk_snapshot(), 'h1_reservation_state': 'CONFIRMED',
                'budget_status_code': 'DAILY_BUDGET_EXHAUSTED_OPEN_RISK',
                'block_code': 'H1_ENTRY_ALREADY_CONFIRMED', 'block_reason': 'same H1'}
        result = self.risk({'risk_budget': risk})
        self.assertEqual(result['riskBudgetStatus']['textContent'], risk['budget_status_code'])
        self.assertEqual(result['riskBlockCode']['textContent'], risk['block_code'])
        self.assertEqual(result['riskBlockReason']['textContent'], 'same H1')

    def test_ledger_v2_risk_and_r_are_read_without_reconstruction(self):
        result = self.performance({'closed_trades': [{'close_time': 1000, 'net_pnl': 10,
            'initial_risk_account_currency': 25, 'realized_r_net': .4}],
            'demo_ledger': {'status': 'OK', 'event_count': 5, 'ledger_conflicts': 0}})
        self.assertEqual(result['statsExpectancy']['textContent'], '+0.400R')
        self.assertEqual(result['statsRSamples']['textContent'], '1 muestras R')
        self.assertIn('RECONCILIADO', result['statsQuality']['textContent'])

    def test_unknown_costs_are_visible_and_not_zero_pnl_or_r(self):
        result = self.performance({'closed_trades': [{'close_time': 1000, 'net_pnl': None,
            'initial_risk_account_currency': None, 'realized_r_net': None}],
            'demo_ledger': {'status': 'OK'}, 'session_stats': {'net_pnl': None}})
        self.assertEqual(result['statsTrades']['textContent'], 1)
        self.assertEqual(result['statsNet']['textContent'], '—')
        self.assertEqual(result['todayNet']['textContent'], '—')
        self.assertEqual(result['statsExpectancy']['textContent'], '—')
        self.assertEqual(result['curveRows'], [])
        self.assertIn('COSTES PARCIALES', result['statsQuality']['textContent'])

    def test_ledger_conflicts_and_ids_are_visible(self):
        result = self.performance({'demo_ledger': {'status': 'CONFLICT', 'ledger_conflicts': 2,
                                                 'conflicting_trade_ids': ['T1', 'T2']}})
        self.assertIn('2 CONFLICTO', result['statsQuality']['textContent'])
        self.assertEqual(result['ledgerStatus']['title'], 'T1, T2')

    def test_history_preserves_unknown_close_and_attempt_identity(self):
        rows = [{'close_time': 1000, 'net_pnl': None, 'realized_r_net': None,
                 'trade_id': 'T1', 'execution_attempt_id': 'A1', 'cost_quality': 'UNKNOWN'}]
        result = self.javascript(['tradeRowsValid', 'tradeCloseOrder', 'renderLog'],
                                 'renderLog(' + json.dumps(rows) + ');')
        self.assertIn('T1', result['log']['innerHTML'])
        self.assertIn('A1', result['log']['innerHTML'])
        self.assertIn('R desconocido', result['log']['innerHTML'])
        self.assertIn('UNKNOWN', result['log']['innerHTML'])

    def test_epoch_and_legacy_close_times_sort_chronologically(self):
        result = self.javascript(['tradeCloseOrder'],
            'els.order=[{close_time:10000},{close_time:900}].sort((a,b)=>tradeCloseOrder(a)-tradeCloseOrder(b));'
            'els.legacy=tradeCloseOrder({close_time:"2026.09.29 12:00:00"});')
        self.assertEqual(result['order'][0]['close_time'], 900)
        self.assertIsInstance(result['legacy'], (int, float))

    def test_entire_dashboard_script_parses_and_dom_ids_are_unique(self):
        node = os.environ.get('NODE_BINARY') or shutil.which('node')
        if not node:
            self.skipTest('Node required')
        script = re.search(r'<script>(.*?)</script>', INDEX, re.S).group(1)
        subprocess.run([node, '-e', "new Function(require('fs').readFileSync(0,'utf8'));"],
                       input=script, text=True, encoding='utf-8', check=True)
        ids = re.findall(r'\bid="([\w-]+)"', INDEX)
        self.assertEqual(len(ids), len(set(ids)))
        for name in ('riskHealth', 'pendingRisk', 'ledgerStatus', 'priceChart', 'authUser'):
            self.assertIn(name, ids)

    def test_full_hali_render_handles_successive_unknown_and_known_snapshots(self):
        node = os.environ.get('NODE_BINARY') or shutil.which('node')
        if not node:
            self.skipTest('Node required')
        script = re.search(r'<script>(.*?)</script>', INDEX, re.S).group(1)
        script = script.rsplit('\ninitNavObserver();', 1)[0]
        ids = re.findall(r'\bid="([\w-]+)"', INDEX)
        prefix = '''
const els={};
const document={getElementById(id){if(!ids.includes(id))throw Error('Missing DOM id: '+id);
 return els[id]??={textContent:'',innerHTML:'',style:{setProperty(k,v){this[k]=v}},
 classList:{add(){},remove(){},toggle(){}},closest(){return {className:''}}}},
 querySelectorAll(){return []}};
const localStorage={getItem(){return null}};
const window={};
'''
        risk = {**server.unknown_risk_snapshot(), 'account_currency': 'EUR',
                'planned_risk_amount': 4, 'remaining_daily_budget': 4}
        ending = ('\nrender(' + json.dumps({'risk_budget': risk}) + ');'
                  'if(els.heroRisk.textContent.indexOf("4.00")<0)throw Error("Risk not projected");'
                  'render({equity:1000,risk_amount:50});'
                  'console.log(JSON.stringify({risk:els.heroRisk.textContent,health:els.riskState.textContent}));')
        source = 'const ids=' + json.dumps(ids) + ';\n' + prefix + script + ending
        result = subprocess.run([node, '-'], input=source, text=True, encoding='utf-8',
                                capture_output=True, check=True)
        self.assertEqual(json.loads(result.stdout), {'risk': '—', 'health': 'UNKNOWN'})


class HaliMainApiTests(unittest.TestCase):
    def test_status_preserves_nested_risk_and_ledger_with_hali_chart_reader(self):
        risk = {**server.unknown_risk_snapshot(), 'block_code': 'OPEN_RISK_UNKNOWN'}
        handler = object.__new__(server.H)
        handler.path = '/api/status'
        handler.send_payload = Mock()
        ledger = project_events((), account_login=123)
        with patch.object(server, 'read_json', return_value={'symbol': 'XAUUSD',
                          'account_login': 123, 'risk_budget': risk}), \
             patch.object(server, 'read_news', return_value={}), \
             patch.object(server, 'read_external', return_value={}), \
             patch.object(server, 'read_tradingview', return_value={}), \
             patch.object(server, 'read_market_observer', return_value={}), \
             patch.object(server, 'read_demo_execution_ledger', return_value=ledger):
            handler.do_GET()
        status, _, body = handler.send_payload.call_args.args
        payload = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(payload['risk_budget'], risk)
        self.assertEqual(payload['demo_ledger']['status'], 'OK')
        self.assertEqual(payload['closed_trades'], [])
        self.assertIsNotNone(server.read_market_bar_series)

    def test_observer_keeps_nested_risk_and_closed_chart_bars_together(self):
        from dashboard import market_observer_service as observer
        risk = server.unknown_risk_snapshot()
        row = {'event_id': 'E1', 'source': 'MT5', 'observer_only': True, 'score_effect': 0,
               'timeframe': 'H1', 'timestamp': 1000, 'received_at': 1000,
               'snapshot_type': 'BAR_CLOSE', 'bar_closed': True, 'symbol': 'XAUUSD',
               'open': 100, 'high': 102, 'low': 99, 'close': 101, 'risk_budget': risk}
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'market_observations.jsonl'
            path.write_text(json.dumps(row) + '\n', encoding='utf-8')
            observer.reset_cache()
            snapshot = observer.observation_snapshot([path], now=1000)
            bars = observer.recent_bar_series([path])
        self.assertEqual(snapshot['latest_by_timeframe']['H1']['risk_budget'], risk)
        self.assertEqual(bars['count'], 1)
        self.assertEqual(bars['score_effect'], 0)

    def test_auth_gates_status_chart_health_and_dashboard(self):
        for path in ('/api/status', '/api/chart', '/api/health', '/'):
            handler = object.__new__(auth_gateway.HaliGateway)
            handler.path = path
            handler.current_user = Mock(return_value=None)
            handler.send_json = Mock()
            handler.redirect = Mock()
            handler.proxy = Mock()
            handler.do_GET()
            handler.proxy.assert_not_called()
            if path.startswith('/api/'):
                self.assertEqual(handler.send_json.call_args.args[0], 401)
            else:
                handler.redirect.assert_called_once_with('/login')

    def test_gateway_remains_local_and_auth_config_failure_is_closed(self):
        self.assertEqual(auth_gateway.BACKEND_HOST, '127.0.0.1')
        self.assertEqual(auth_gateway.GATEWAY_HOST, '127.0.0.1')
        with tempfile.TemporaryDirectory() as temp:
            self.assertIsNone(auth_service.load_auth_config(Path(temp) / 'missing.json'))

    def test_authenticated_gateway_serves_dashboard_status_health_and_chart(self):
        config = auth_service.AuthConfig({'test': {'enabled': True}}, secrets.token_bytes(32), Path('unused'))
        cookie = auth_service.COOKIE_NAME + '=' + auth_service.create_session_token('test', 'Test', config.session_secret)
        risk = {**server.unknown_risk_snapshot(), 'block_code': 'OPEN_RISK_UNKNOWN'}
        with tempfile.TemporaryDirectory() as temp, \
             patch.object(server, 'CANDIDATES', [Path(temp)]), \
             patch.object(server, 'read_news', return_value={}), \
             patch.object(server, 'read_external', return_value={}), \
             patch.object(server, 'read_tradingview', return_value={}), \
             patch.object(auth_gateway, 'get_auth_config', return_value=config):
            root = Path(temp)
            (root / 'xau_goldscout_dashboard.json').write_text(json.dumps({
                'symbol': 'XAUUSD', 'account_login': 123, 'risk_budget': risk,
                'demo_execution': {'account_mode': 'DEMO', 'broker_connected': True}}), encoding='utf-8')
            (root / 'hali_live_market.json').write_text(json.dumps({
                'bid': 100, 'ask': 100.1, 'tick_time_msc': 100000,
                'bars': {'H1': {'timestamp': 100, 'close': 100}}}), encoding='utf-8')
            backend = server.ThreadingHTTPServer(('127.0.0.1', 0), server.H)
            with patch.object(auth_gateway, 'BACKEND_PORT', backend.server_address[1]):
                gateway = auth_gateway.ThreadingHTTPServer(('127.0.0.1', 0), auth_gateway.HaliGateway)
                threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in (backend, gateway)]
                for thread in threads:
                    thread.start()
                connection = http.client.HTTPConnection(*gateway.server_address, timeout=5)
                try:
                    connection.request('GET', '/api/status')
                    response = connection.getresponse()
                    self.assertEqual(response.status, 401)
                    response.read()
                    bodies = {}
                    for path in ('/', '/api/status', '/api/health', '/api/chart?timeframe=H1', '/manifest.webmanifest'):
                        connection.request('GET', path, headers={'Cookie': cookie})
                        response = connection.getresponse()
                        self.assertEqual(response.status, 200, path)
                        bodies[path] = response.read().decode('utf-8')
                    self.assertIn('HALI', bodies['/'])
                    self.assertIn('id="riskHealth"', bodies['/'])
                    self.assertEqual(json.loads(bodies['/api/status'])['risk_budget'], risk)
                    self.assertEqual(json.loads(bodies['/api/health'])['backend']['status'], 'ONLINE')
                    chart = json.loads(bodies['/api/chart?timeframe=H1'])
                    self.assertEqual(chart['live_bar']['close'], 100)
                    self.assertEqual(chart['tick_time_msc'], 100000)
                finally:
                    connection.close()
                    for httpd in (gateway, backend):
                        httpd.shutdown()
                        httpd.server_close()
                    for thread in threads:
                        thread.join(timeout=5)

    def test_http_login_cookie_status_and_logout_round_trip(self):
        salt = secrets.token_bytes(16)
        record = {'enabled': True, 'display_name': 'Integration test', 'iterations': 1000,
                  'salt': auth_service._b64e(salt),
                  'password_hash': auth_service.derive_password_hash('test-only', salt, 1000)}
        config = auth_service.AuthConfig({'test': record}, secrets.token_bytes(32), Path('unused'))
        with patch.object(auth_gateway, 'get_auth_config', return_value=config):
            httpd = auth_gateway.ThreadingHTTPServer(('127.0.0.1', 0), auth_gateway.HaliGateway)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            connection = http.client.HTTPConnection(*httpd.server_address, timeout=5)
            try:
                connection.request('POST', '/api/login', json.dumps({'username': 'test',
                    'password': 'test-only'}), {'Content-Type': 'application/json'})
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                cookie = response.getheader('Set-Cookie')
                response.read()
                self.assertIn('HttpOnly; Secure; SameSite=Lax', cookie)
                connection.request('GET', '/api/auth/status', headers={'Cookie': cookie.split(';')[0]})
                response = connection.getresponse()
                self.assertTrue(json.loads(response.read())['authenticated'])
                connection.request('POST', '/api/logout')
                response = connection.getresponse()
                self.assertIn('Max-Age=0', response.getheader('Set-Cookie'))
                response.read()
            finally:
                connection.close()
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=5)


if __name__ == '__main__':
    unittest.main()
