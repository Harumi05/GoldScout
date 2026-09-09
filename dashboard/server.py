from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import json, os, threading, time
try:
    from news_service import run_once as update_news
except Exception:
    update_news = None

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
"balance":None,"equity":None,"daily_pnl":None,"risk_usd":10.0,"risk_percent":5.0,"last_score":None,
"last_setup":"-","last_direction":"-","last_decision":"Esperando datos MT5...","analysis":{"bar":None},
"active_trade":None,"closed_trades":[],"news":{"available":False,"bias":0,"confidence":0,"risk":"UNKNOWN",
"data_risk":"HIGH","direction":"NEUTRO","summary":"Esperando análisis de noticias...","article_count":0,"top_headlines":[],"source_health":{}}}

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

class H(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return
    def send_payload(self, code, ctype, payload):
        try:
            self.send_response(code)
            self.send_header('Content-Type', ctype)
            self.send_header('Cache-Control','no-store')
            self.send_header('Connection','close')
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
            self.send_payload(200,'application/json; charset=utf-8',json.dumps(d,ensure_ascii=False).encode()); return
        if self.path.startswith('/api/health'):
            payload={'ok':True,'candidates':[str(p) for p in CANDIDATES], 'news_file':bool(read_json('gold_news_analysis.json'))}
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

if __name__=='__main__':
    print('GoldScout dashboard: http://127.0.0.1:8787')
    threading.Thread(target=news_loop,daemon=True).start()
    ThreadingHTTPServer(('127.0.0.1',8787),H).serve_forever()
