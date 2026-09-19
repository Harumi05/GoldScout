from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import json, os, threading, time
try:
    from news_service import run_once as update_news
except Exception:
    update_news = None
try:
    from external_signal_service import (
        diagnostic_log_lines,
        mark_snapshot_freshness,
        run_once as update_external,
    )
except Exception:
    diagnostic_log_lines = None
    mark_snapshot_freshness = None
    update_external = None
try:
    from tradingview_signal_service import (
        SlidingWindowRateLimiter,
        ingest_webhook as ingest_tradingview_webhook,
        observation_snapshot as read_tradingview_snapshot,
        record_service_status as record_tradingview_status,
    )
except Exception:
    SlidingWindowRateLimiter = None
    ingest_tradingview_webhook = None
    read_tradingview_snapshot = None
    record_tradingview_status = None
try:
    from market_observer_service import observation_snapshot as read_market_observer_snapshot
except Exception:
    read_market_observer_snapshot = None

try:
    _tv_rate_limit=max(1,int(os.environ.get('GOLDSCOUT_TRADINGVIEW_RATE_LIMIT_PER_MINUTE','120')))
except ValueError:
    _tv_rate_limit=120
TRADINGVIEW_RATE_LIMITER=SlidingWindowRateLimiter(_tv_rate_limit,60) if SlidingWindowRateLimiter else None

ROOT = Path(__file__).resolve().parent
CANDIDATES = []
custom = os.environ.get('MT5_COMMON_FILES','').strip()
if custom:
    CANDIDATES.append(Path(custom))
for base in (os.environ.get('APPDATA',''), os.environ.get('LOCALAPPDATA','')):
    if base:
        CANDIDATES.append(Path(base)/'MetaQuotes'/'Terminal'/'Common'/'Files')
CANDIDATES.append(ROOT/'data')
_seen=set(); _u=[]
for p in CANDIDATES:
    s=str(p).lower()
    if s not in _seen:
        _seen.add(s); _u.append(p)
CANDIDATES=_u

OFFLINE={"updated_at":None,"symbol":"XAUUSD","timeframe":"H1","live_trading":False,"connected":False,
"account_currency":"USD","balance":None,"equity":None,"start_of_day_equity":None,"daily_pnl":None,
"risk_amount":0.0,"target_risk":0.0,"daily_loss_limit_percent":5.0,"daily_loss_budget":0.0,
"daily_loss_used":0.0,"remaining_daily_budget":0.0,"effective_planned_risk":0.0,"risk_percent":5.0,"last_score":None,
"last_setup":"-","last_direction":"-","last_decision":"Esperando datos MT5...","analysis":{"bar":None},
"armed_status":{"enabled":False,"status":"DISABLED","direction":"NONE","setup":"-","score":0,
"previous_direction":"NONE","previous_setup":"-","previous_score":0,"reason":"FEATURE_DISABLED",
"breakout_direction":"NONE","rsi":0.0,"impulse_atr":0.0,"structure_state":"INSUFICIENTE"},
"active_trade":None,"closed_trades":[],"news":{"available":False,"bias":0,"confidence":0,"risk":"UNKNOWN",
"data_risk":"HIGH","direction":"NEUTRO","summary":"Esperando análisis de noticias...","article_count":0,"top_headlines":[],"source_health":{}},
"external_signal":{"source":"etoro","symbol":"XAUUSD","available":False,"api_status":"NO_DATA",
"long_weight":0.0,"short_weight":0.0,"neutral_weight":0.0,"valid_traders":0,"quality":0,
"freshness_sec":None,"data_quality":"LOW","score_effect":0,"read_only":True},
"tradingview":{"source":"tradingview","symbol":"XAUUSD","available":False,"api_status":"NO_DATA",
"last_signal":None,"freshness_sec":None,"event_count":0,"score_effect":0,"read_only":True,
"observation_only":True},
"market_observer":{"source":"MT5","observer_only":True,"score_effect":0,"status":"NO_DATA",
"observation_count":0,"latest_by_timeframe":{"M15":None,"H1":None,"H4":None},
"last_decision":None,"freshness_sec":None}}

def gold(s):
    return (s or '').upper().strip().startswith('XAUUSD')

def read_json(name):
    for p in CANDIDATES:
        f=p/name
        if f.exists():
            try:
                return json.loads(f.read_text(encoding='utf-8', errors='ignore'))
            except Exception:
                pass
    return None

def read_news():
    d=read_json('gold_news_analysis.json')
    if not d:
        return {**OFFLINE['news'], 'data_risk':'HIGH', 'summary':'El motor de noticias aún no ha generado un snapshot.'}
    try:
        from datetime import datetime, timezone
        ts=d.get('updated_at'); stale=False
        if ts:
            stale=(datetime.now(timezone.utc)-datetime.fromisoformat(ts.replace('Z','+00:00'))).total_seconds()>float(d.get('stale_after_seconds',900))
        d['available']=bool(d.get('available',True)) and not stale
        if stale:
            d['data_risk']='HIGH'; d['summary']='El análisis de noticias está desactualizado; se bloquean nuevas entradas.'; d['risk']='UNKNOWN'
        return d
    except Exception:
        return d

def read_external():
    d=read_json('external_signal_etoro.json')
    if not isinstance(d,dict):
        return OFFLINE['external_signal'].copy()
    d['score_effect']=0
    d['read_only']=True
    if mark_snapshot_freshness:
        try:
            d=mark_snapshot_freshness(d)
        except Exception:
            d={**OFFLINE['external_signal'],'api_status':'INVALID_DATA'}
    return d

def read_tradingview():
    if not read_tradingview_snapshot:
        return OFFLINE['tradingview'].copy()
    try:
        d=read_tradingview_snapshot()
        d['score_effect']=0
        d['read_only']=True
        d['observation_only']=True
        return d
    except Exception:
        return OFFLINE['tradingview'].copy()

def read_market_observer():
    if not read_market_observer_snapshot:
        return OFFLINE['market_observer'].copy()
    try:
        paths=[candidate/'market_observations.jsonl' for candidate in CANDIDATES]
        d=read_market_observer_snapshot(paths)
        d['source']='MT5'
        d['observer_only']=True
        d['score_effect']=0
        return d
    except Exception:
        return {**OFFLINE['market_observer'],'status':'PERSISTENCE_ERROR'}

def request_token(headers):
    token=(headers.get('X-GoldScout-Token') or '').strip()
    if token:
        return token
    authorization=(headers.get('Authorization') or '').strip()
    if authorization.lower().startswith('bearer '):
        return authorization[7:].strip()
    return ''

def news_loop():
    interval=int(os.environ.get('GOLDSCOUT_NEWS_INTERVAL','300'))
    if update_news:
        try:
            print('[news] actualización inicial...')
            update_news()
            print('[news] actualización inicial completada')
        except Exception as exc:
            print(f'[news] actualización inicial falló: {exc}')
    else:
        print('[news] news_service.py no está disponible; se continuará sin motor de noticias')
    while True:
        time.sleep(max(60,interval))
        if not update_news:
            continue
        try:
            update_news(); print('[news] actualización completada')
        except Exception as exc:
            print(f'[news] actualización falló: {exc}')

def external_loop():
    interval=max(60,int(os.environ.get('GOLDSCOUT_ETORO_INTERVAL','300')))
    if not update_external:
        print('[etoro] external_signal_service.py no está disponible; GoldScout continúa sin contexto externo')
        return
    limits_logged=False
    while True:
        try:
            d=update_external()
            if diagnostic_log_lines:
                for line in diagnostic_log_lines(d,include_limits=not limits_logged):
                    print(line)
            limits_logged=True
            print(f"[etoro] actualización {d.get('api_status')} | traders={d.get('valid_traders',0)} | score_effect=0")
        except Exception as exc:
            print(f'[etoro] actualización falló sin afectar GoldScout: {type(exc).__name__}')
        time.sleep(interval)

class H(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return
    def send_payload(self, code, ctype, payload, extra_headers=None):
        try:
            self.send_response(code)
            self.send_header('Content-Type', ctype)
            self.send_header('Cache-Control','no-store')
            self.send_header('Connection','close')
            for name,value in (extra_headers or {}).items():
                self.send_header(name,value)
            self.end_headers()
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            # The browser cancelled/replaced the request; this is harmless.
            pass
    def do_GET(self):
        if self.path.startswith('/api/status'):
            d=read_json('xau_goldscout_dashboard.json') or OFFLINE.copy()
            if not gold(d.get('symbol')):
                d={**OFFLINE,'last_decision':f"Ignorando datos de {d.get('symbol','OTRO')}; esperando XAUUSD..."}
            else:
                d={**d,'connected':True}
            d['news']=read_news()
            d['external_signal']=read_external()
            d['tradingview']=read_tradingview()
            d['market_observer']=read_market_observer()
            self.send_payload(200,'application/json; charset=utf-8',json.dumps(d,ensure_ascii=False).encode()); return
        if self.path.startswith('/api/health'):
            payload={'ok':True,'candidates':[str(p) for p in CANDIDATES],
                'news_file':bool(read_json('gold_news_analysis.json')),
                'external_signal_file':bool(read_json('external_signal_etoro.json')),
                'tradingview_events':read_tradingview().get('event_count',0),
                'market_observations':read_market_observer().get('observation_count',0)}
            self.send_payload(200,'application/json; charset=utf-8',json.dumps(payload,ensure_ascii=False).encode()); return
        if self.path.startswith('/api/log'):
            import csv
            rows=[]
            for p in CANDIDATES:
                f=p/'xau_goldscout_trade_log.csv'
                if f.exists():
                    try:
                        with f.open('r',encoding='cp1252',errors='ignore',newline='') as fh:
                            rows=list(csv.DictReader(fh,delimiter=';'))
                        break
                    except Exception:
                        pass
            self.send_payload(200,'application/json; charset=utf-8',json.dumps(rows[-100:],ensure_ascii=False).encode()); return
        rel=self.path.split('?',1)[0]
        file=ROOT/('index.html' if rel=='/' else rel.lstrip('/'))
        if file.exists() and file.is_file():
            ctype='text/html; charset=utf-8' if file.suffix.lower()=='.html' else 'text/plain; charset=utf-8'
            self.send_payload(200,ctype,file.read_bytes()); return
        self.send_payload(404,'text/plain; charset=utf-8',b'404')

    def do_POST(self):
        if self.path.split('?',1)[0] != '/api/tradingview/webhook':
            self.send_payload(404,'text/plain; charset=utf-8',b'404'); return
        if not ingest_tradingview_webhook:
            payload={'status':'NO_DATA','score_effect':0}
            self.send_payload(503,'application/json; charset=utf-8',json.dumps(payload).encode()); return
        client_key=self.client_address[0] if self.client_address else 'unknown'
        if TRADINGVIEW_RATE_LIMITER and not TRADINGVIEW_RATE_LIMITER.allow(client_key):
            if record_tradingview_status:
                record_tradingview_status('RATE_LIMITED')
            payload={'status':'RATE_LIMITED','score_effect':0}
            self.send_payload(429,'application/json; charset=utf-8',json.dumps(payload).encode(),{'Retry-After':'60'}); return
        if self.headers.get_content_type() != 'application/json':
            if record_tradingview_status:
                record_tradingview_status('INVALID_PAYLOAD')
            payload={'status':'INVALID_PAYLOAD','reason':'CONTENT_TYPE_INVALID','score_effect':0}
            self.send_payload(400,'application/json; charset=utf-8',json.dumps(payload).encode()); return
        try:
            length=int(self.headers.get('Content-Length','0'))
        except ValueError:
            length=0
        if length<=0 or length>32768:
            if record_tradingview_status:
                record_tradingview_status('INVALID_PAYLOAD')
            payload={'status':'INVALID_PAYLOAD','reason':'CONTENT_LENGTH_INVALID','score_effect':0}
            self.send_payload(400,'application/json; charset=utf-8',json.dumps(payload).encode()); return
        try:
            raw=self.rfile.read(length)
            body=json.loads(raw.decode('utf-8'))
        except (UnicodeDecodeError,json.JSONDecodeError):
            if record_tradingview_status:
                record_tradingview_status('INVALID_PAYLOAD')
            payload={'status':'INVALID_PAYLOAD','reason':'JSON_INVALID','score_effect':0}
            self.send_payload(400,'application/json; charset=utf-8',json.dumps(payload).encode()); return
        code,result=ingest_tradingview_webhook(body,provided_token=request_token(self.headers))
        result['score_effect']=0
        self.send_payload(code,'application/json; charset=utf-8',json.dumps(result).encode())

if __name__=='__main__':
    print('GoldScout dashboard: http://127.0.0.1:8787')
    threading.Thread(target=news_loop,daemon=True).start()
    threading.Thread(target=external_loop,daemon=True).start()
    ThreadingHTTPServer(('127.0.0.1',8787),H).serve_forever()
