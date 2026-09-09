import html, json, os, re, time, xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse
import requests

ROOT = Path(__file__).resolve().parent
OUT = ROOT / 'data' / 'gold_news_analysis.json'
OUT.parent.mkdir(parents=True, exist_ok=True)
USER_AGENT = 'GoldScout-H1/3.0'
TIMEOUT = float(os.environ.get('GOLDSCOUT_NEWS_TIMEOUT', '8'))
GDELT_URL = 'https://api.gdeltproject.org/api/v2/doc/doc'
GOOGLE_RSS = 'https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en'
BING_RSS = 'https://www.bing.com/news/search?q={query}&qft=sortbydate%3d%221%22&format=rss'
QUERIES = {
    'monetary': '(gold OR XAU OR bullion) (Federal Reserve OR Fed OR interest rates OR yields OR Treasury OR inflation OR CPI OR PCE OR jobs OR payrolls OR Powell OR central bank OR dollar OR DXY)',
    'geopolitics': '(gold OR XAU OR bullion) (war OR conflict OR ceasefire OR sanctions OR Iran OR Israel OR Russia OR Ukraine OR China OR Taiwan OR attack OR crisis OR tensions)',
    'risk': '(gold OR XAU OR bullion) (oil OR tariffs OR trade war OR recession OR banking OR credit OR equities OR stock market OR safe haven OR risk-off OR risk-on)',
}
RSS_QUERIES = {
    'gold': 'gold OR XAU OR bullion',
    'fed': 'gold Federal Reserve OR Fed OR interest rates OR yields OR Treasury OR dollar',
    'inflation_jobs': 'gold CPI OR PCE OR inflation OR payrolls OR employment',
    'geopolitics': 'gold war OR sanctions OR Iran OR Israel OR Russia OR Ukraine OR China OR Taiwan OR crisis',
}
POS = {
    'rate cut': 3,'rate cuts': 3,'lower rates': 3,'dovish': 3,'easing': 2,'yield falls': 3,'yields fall': 3,'yields decline': 3,'lower yield': 2,
    'dollar falls': 3,'dollar weakens': 3,'dollar weaker': 3,'dollar slides': 2,'safe haven': 2,'geopolitical': 2,'war': 2,'conflict': 2,'sanctions': 2,'crisis': 2,'attack': 2,'tensions': 1,'tariffs': 1,'recession': 1,
}
NEG = {
    'rate hike': 4,'rate hikes': 4,'higher rates': 3,'hawkish': 3,'tightening': 3,'yield rises': 3,'yields rise': 3,'yield surge': 3,'higher yield': 2,
    'dollar rises': 3,'dollar strengthens': 3,'dollar stronger': 3,'dollar surges': 3,'risk-on': 1,'stocks rally': 1,'equities rally': 1,
}
HIGH_RISK = ['fomc','federal reserve','powell','cpi','core cpi','pce','nonfarm payroll','payrolls','employment report','interest rate decision','rate decision','emergency meeting','war','missile','invasion','ceasefire','sanctions','iran','israel','russia','ukraine','taiwan','bank failure','credit crisis']

def now_iso(): return datetime.now(timezone.utc).isoformat()
def norm(s): return re.sub(r'\s+',' ',re.sub(r'[^a-z0-9]+',' ',(s or '').lower())).strip()
def classify(text):
    t=(text or '').lower(); score=0; matched=[]
    for k,v in POS.items():
        if k in t: score+=v; matched.append(k)
    for k,v in NEG.items():
        if k in t: score-=v; matched.append(k)
    return max(-8,min(8,score)), matched[:10]
def parse_time(v):
    if not v:return None
    v=v.strip()
    try:return parsedate_to_datetime(v).astimezone(timezone.utc)
    except Exception: pass
    for fmt in ('%Y%m%dT%H%M%SZ','%Y%m%dT%H%M%S'):
        try:return datetime.strptime(v[:15],fmt).replace(tzinfo=timezone.utc)
        except Exception:pass
    try:return datetime.fromisoformat(v.replace('Z','+00:00')).astimezone(timezone.utc)
    except Exception:return None

def fetch(url, params=None):
    r=requests.get(url,params=params,timeout=TIMEOUT,headers={'User-Agent':USER_AGENT,'Accept':'application/rss+xml, application/atom+xml, application/json, text/xml, */*'})
    r.raise_for_status(); return r

def fetch_gdelt(query):
    p={'query':query,'mode':'artlist','format':'json','maxrecords':25,'timespan':'3h','sort':'datedesc'}
    r=fetch(GDELT_URL,p); payload=r.json(); return payload.get('articles',[]) or []

def parse_rss(xml_text,category,source_name):
    root=ET.fromstring(xml_text); rows=[]
    for item in root.findall('.//item'):
        title=html.unescape((item.findtext('title') or '').strip()); link=(item.findtext('link') or '').strip(); pub=(item.findtext('pubDate') or item.findtext('published') or item.findtext('updated') or '').strip(); desc=html.unescape(re.sub(r'<[^>]+>',' ',item.findtext('description') or '')).strip(); src=(item.findtext('source') or source_name).strip()
        if title: rows.append({'title':title,'url':link,'seen':pub,'domain':src,'description':desc,'category':category,'source':source_name})
    return rows

def unwrap_bing(url):
    try:return parse_qs(urlparse(url).query).get('url',[None])[0] or url
    except Exception:return url

def normalize_row(row):
    title=str(row.get('title') or '').strip(); body=str(row.get('description') or '').strip(); score,signals=classify(f'{title} {body}'); dt=parse_time(str(row.get('seen') or ''))
    return {'category':row.get('category') or 'news','title':title,'url':unwrap_bing(str(row.get('url') or '')),'domain':str(row.get('domain') or row.get('source') or '').strip(),'seen':dt.isoformat() if dt else str(row.get('seen') or ''),'impact_score':score,'signals':signals,'source':row.get('source') or 'unknown'}

def collect(name, category, fn):
    start=time.perf_counter()
    try:
        rows=[normalize_row({**r,'category':category,'source':name}) for r in fn()]
        return name, {'status':'OK','articles':len(rows),'latency_ms':round((time.perf_counter()-start)*1000),'error':''}, rows
    except Exception as exc:
        return name, {'status':'ERROR','articles':0,'latency_ms':round((time.perf_counter()-start)*1000),'error':str(exc)[:300]}, []

def collect_all():
    jobs=[]
    for cat,q in QUERIES.items(): jobs.append((f'GDELT/{cat}',cat,lambda q=q:fetch_gdelt(q)))
    for cat,q in RSS_QUERIES.items():
        jobs.append((f'GoogleRSS/{cat}',cat,lambda q=q:parse_rss(fetch(GOOGLE_RSS.format(query=requests.utils.quote(q))).text,cat,'Google News RSS')))
        jobs.append((f'BingRSS/{cat}',cat,lambda q=q:parse_rss(fetch(BING_RSS.format(query=requests.utils.quote(q))).text,cat,'Bing News RSS')))
    health={}; rows=[]
    with ThreadPoolExecutor(max_workers=min(12,len(jobs))) as ex:
        futures=[ex.submit(collect,*j) for j in jobs]
        for fut in as_completed(futures):
            name,h,r=fut.result(); health[name]=h; rows.extend(r)
    dedup={}; now=datetime.now(timezone.utc)
    for r in rows:
        key=norm(r['title'])
        if not key: continue
        dt=parse_time(r.get('seen',''))
        if dt and (now-dt).total_seconds()>8*3600: continue
        if key not in dedup or (not dedup[key].get('url') and r.get('url')): dedup[key]=r
    rows=list(dedup.values()); rows.sort(key=lambda r:(r.get('seen') or ''),reverse=True)
    return rows[:100],health

def synthesize(rows,health):
    if not rows:
        return {'available':False,'bias':0,'confidence':0,'risk':'UNKNOWN','data_risk':'HIGH','direction':'NEUTRO','summary':'No se pudieron recuperar titulares de ninguna fuente. Nuevas operaciones bloqueadas.','rationale':'El motor de noticias no dispone de información reciente.','top_headlines':[]}
    now=datetime.now(timezone.utc)
    def rank(r):
        dt=parse_time(r.get('seen','')); rec=0
        if dt: rec=max(0,6-max(0,(now-dt).total_seconds()/3600))
        return abs(r.get('impact_score',0))*3+rec
    working=sorted(rows,key=rank,reverse=True)[:30]
    avg=sum(r['impact_score'] for r in working)/len(working)
    bias=int(max(-100,min(100,round(avg*14))))
    high=[r for r in working if any(k in r['title'].lower() for k in HIGH_RISK)]
    risk='HIGH' if len(high)>=2 else ('MEDIUM' if high else 'LOW')
    ok=sum(1 for h in health.values() if h['status']=='OK')
    conf=min(96,35+len(working)*2+(8 if ok>=2 else 0)+(8 if abs(bias)>=45 else 0))
    direction='ALCISTA' if bias>20 else ('BAJISTA' if bias<-20 else 'NEUTRO')
    summary={'ALCISTA':'El flujo reciente favorece al oro por menor presión de tasas/rendimientos, dólar más débil o demanda de refugio.','BAJISTA':'El flujo reciente es desfavorable para el oro por mayor presión de tasas/rendimientos, dólar más fuerte o menor demanda de refugio.','NEUTRO':'Los titulares recientes no muestran una ventaja direccional suficientemente clara para el oro.'}[direction]
    top=[{'title':r['title'],'domain':r['domain'],'url':r['url'],'impact':r['impact_score'],'category':r['category'],'source':r['source']} for r in working[:10]]
    return {'available':True,'bias':bias,'confidence':conf,'risk':risk,'data_risk':'LOW' if ok>=2 else 'MEDIUM','direction':direction,'summary':summary,'rationale':f'Análisis agregado de {len(working)} titulares recientes; se ponderan tasas/rendimientos, dólar, inflación/empleo, geopolítica, petróleo y refugio.','top_headlines':top}

def maybe_openai(analysis,rows):
    key=os.environ.get('OPENAI_API_KEY','').strip()
    if not key or not rows:return analysis
    try:
        from openai import OpenAI
        client=OpenAI(api_key=key); pack=[{'title':r['title'],'category':r['category'],'domain':r['domain'],'source':r['source']} for r in rows[:20]]
        prompt=('Analiza titulares recientes para contexto de XAUUSD H1. No des una orden. Devuelve JSON con bias (-100..100, positivo favorece oro), confidence, risk LOW/MEDIUM/HIGH, direction ALCISTA/NEUTRO/BAJISTA, summary <=280 chars y rationale <=600 chars. Distingue hechos de inferencias. Considera Fed/tasas/rendimientos/dólar, inflación/empleo, geopolítica, petróleo, China y riesgo. Si no hay evidencia clara usa NEUTRO.\n'+json.dumps(pack,ensure_ascii=False))
        model=os.environ.get('GOLDSCOUT_NEWS_MODEL','gpt-5.6')
        resp=client.responses.create(model=model,input=prompt); text=resp.output_text.strip(); m=re.search(r'\{.*\}',text,re.S)
        if m:
            obj=json.loads(m.group(0));
            for k in ('bias','confidence','risk','direction','summary','rationale'):
                if k in obj: analysis[k]=obj[k]
            analysis['source']=analysis.get('source','')+' + OpenAI'
    except Exception as exc: analysis['ai_error']=str(exc)[:250]
    return analysis

def common_dirs():
    out=[]
    custom=os.environ.get('MT5_COMMON_FILES','').strip()
    if custom: out.append(Path(custom))
    for base in (os.environ.get('APPDATA',''),os.environ.get('LOCALAPPDATA','')):
        if base: out.append(Path(base)/'MetaQuotes'/'Terminal'/'Common'/'Files')
    # de-dup
    seen=set(); res=[]
    for p in out:
        s=str(p).lower()
        if s not in seen: seen.add(s); res.append(p)
    return res

def run_once():
    rows,health=collect_all(); a=synthesize(rows,health); a=maybe_openai(a,rows); now=now_iso()
    out={'updated_at':now,'updated_epoch':int(datetime.now(timezone.utc).timestamp()),'stale_after_seconds':int(os.environ.get('GOLDSCOUT_NEWS_STALE_SECONDS','900')),'article_count':len(rows),'source_count':len(health),'sources_ok':sum(1 for v in health.values() if v['status']=='OK'),'errors':[f'{k}: {v.get("error")}' for k,v in health.items() if v['status']!='OK'],'source_health':health,**a}
    payload=json.dumps(out,ensure_ascii=False,indent=2); OUT.write_text(payload,encoding='utf-8')
    for d in common_dirs():
        try:d.mkdir(parents=True,exist_ok=True); (d/'gold_news_analysis.json').write_text(payload,encoding='utf-8')
        except Exception as exc: print(f'[news] common mirror failed {d}: {exc}')
    return out

def main():
    interval=int(os.environ.get('GOLDSCOUT_NEWS_INTERVAL','300')); print(f'GoldScout News Engine: {OUT}')
    while True:
        try:
            d=run_once(); print(f"[news] refresh OK | bias={d.get('bias')} conf={d.get('confidence')} risk={d.get('risk')} articles={d.get('article_count')} sources={d.get('sources_ok')}/{d.get('source_count')}")
        except Exception as exc: print(f'[news] refresh ERROR: {exc}')
        time.sleep(max(60,interval))
if __name__=='__main__': main()
