import os, time, json, hmac, hashlib, secrets, logging, threading
from datetime import datetime, timezone
from decimal import Decimal
from functools import wraps
from urllib.parse import urlencode
import requests
from flask import Flask, request, session, redirect, url_for, flash, render_template_string, jsonify, abort
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix

try:
 import psycopg2
 from psycopg2.extras import RealDictCursor
except ImportError:
 psycopg2 = None

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
app.secret_key = os.environ.get('SECRET_KEY') or secrets.token_hex(32)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SECURE=os.environ.get('LOCAL_DEV') != '1', SESSION_COOKIE_SAMESITE='Lax', PERMANENT_SESSION_LIFETIME=3600)
logging.basicConfig(level=logging.INFO)
DATABASE_URL = os.environ.get('DATABASE_URL', '')
BASE = 'https://fapi.binance.com'
from live_engine import execute, account_snapshot, daily_pnl, TradingError
SYMBOLS = ('BTCUSDT','ETHUSDT','BNBUSDT','SOLUSDT','XRPUSDT','ADAUSDT','DOGEUSDT','AVAXUSDT','LINKUSDT','DOTUSDT','NEARUSDT')
HTTP = requests.Session()
_lock = threading.RLock()
_state = {'cooldown_until':0.0,'reason':'Ø§Ù„Ø§ØªØµØ§Ù„ Ù…Ø³ØªÙ‚Ø±','last_request':0.0,'balance':None,'balance_at':0.0,'prices':{},'prices_at':0.0,'last_scan':0.0,'opportunities':[]}
MIN_REQUEST_GAP = 3.0

def db():
 if not DATABASE_URL or psycopg2 is None: raise RuntimeError('DATABASE_URL required; configure Render PostgreSQL')
 return psycopg2.connect(DATABASE_URL, connect_timeout=8, sslmode='require' if os.environ.get('LOCAL_DEV') != '1' else 'prefer')

def init():
 with db() as conn, conn.cursor() as c:
  c.execute('''CREATE TABLE IF NOT EXISTS bot_preferences(id INTEGER PRIMARY KEY CHECK(id=1), is_paused BOOLEAN NOT NULL DEFAULT TRUE, target_roi_pct NUMERIC DEFAULT 20, updated_at TIMESTAMPTZ DEFAULT NOW())''')
  c.execute('INSERT INTO bot_preferences(id) VALUES(1) ON CONFLICT DO NOTHING')
  c.execute('''CREATE TABLE IF NOT EXISTS bot_orders_v2(id BIGSERIAL PRIMARY KEY, symbol TEXT NOT NULL, direction TEXT NOT NULL, entry_reference NUMERIC NOT NULL, stop_reference NUMERIC NOT NULL, take_reference NUMERIC NOT NULL, created_at TIMESTAMPTZ DEFAULT NOW(), status TEXT NOT NULL DEFAULT 'SIMULATED', note TEXT)''')
  c.execute('ALTER TABLE bot_preferences ADD COLUMN IF NOT EXISTS max_total_exposure_pct NUMERIC NOT NULL DEFAULT 10')
  c.execute('ALTER TABLE bot_preferences ADD COLUMN IF NOT EXISTS leverage INTEGER NOT NULL DEFAULT 20')
  c.execute('ALTER TABLE bot_preferences ADD COLUMN IF NOT EXISTS max_daily_trades INTEGER NOT NULL DEFAULT 100')
  c.execute('ALTER TABLE bot_preferences ADD COLUMN IF NOT EXISTS daily_target_pct NUMERIC NOT NULL DEFAULT 20')
  c.execute('ALTER TABLE bot_orders_v2 ADD COLUMN IF NOT EXISTS exchange_order_id TEXT')
  c.execute('''CREATE TABLE IF NOT EXISTS bot_binance_cooldown(id INTEGER PRIMARY KEY CHECK(id=1), until_ms BIGINT NOT NULL DEFAULT 0, reason TEXT DEFAULT '')''')
  c.execute('INSERT INTO bot_binance_cooldown(id) VALUES(1) ON CONFLICT DO NOTHING')
  c.execute('''CREATE TABLE IF NOT EXISTS investor_accounts(id BIGSERIAL PRIMARY KEY, email TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL, phone_number TEXT DEFAULT '', subscription_status TEXT DEFAULT 'PENDING', agreed_terms BOOLEAN DEFAULT FALSE, created_at TIMESTAMPTZ DEFAULT NOW())''')
  c.execute('''CREATE TABLE IF NOT EXISTS admin_bank_accounts(id BIGSERIAL PRIMARY KEY, bank_name TEXT NOT NULL, account_holder TEXT NOT NULL, iban TEXT NOT NULL, swift_code TEXT DEFAULT '', currency TEXT DEFAULT 'KWD', is_active BOOLEAN DEFAULT TRUE, created_at TIMESTAMPTZ DEFAULT NOW())''')
  c.execute('''CREATE TABLE IF NOT EXISTS bot_scan_status(id INTEGER PRIMARY KEY CHECK(id=1), checked_at TIMESTAMPTZ, opportunities INTEGER DEFAULT 0, reason TEXT)''')
  c.execute('INSERT INTO bot_scan_status(id) VALUES(1) ON CONFLICT DO NOTHING')

def admin_required(f):
 @wraps(f)
 def inner(*args,**kwargs):
  if not session.get('admin'): return redirect(url_for('login'))
  return f(*args,**kwargs)
 return inner

def csrf():
 if 'csrf' not in session: session['csrf']=secrets.token_hex(24)
 return session['csrf']

def check_csrf():
 if not hmac.compare_digest(request.form.get('csrf',''),session.get('csrf','')): abort(400,'CSRF token invalid')

def cooldown():
 with _lock:
  remaining=max(0,int(_state['cooldown_until']-time.time()+0.999))
  return {'remaining_seconds':remaining,'reason':_state['reason'] if remaining else 'Ø§Ù„Ø§ØªØµØ§Ù„ Ù…Ø³ØªÙ‚Ø±','until_ms':int(_state['cooldown_until']*1000)}

def _request(path, params=None, signed=False):
 with _lock:
  if time.time() < _state['cooldown_until']: raise RuntimeError('Binance cooldown active')
  wait=MIN_REQUEST_GAP-(time.monotonic()-_state['last_request'])
  if wait>0: time.sleep(wait)
  _state['last_request']=time.monotonic()
  p=dict(params or {})
  headers={}
  if signed:
   key=os.environ.get('BINANCE_API_KEY',''); secret=os.environ.get('BINANCE_API_SECRET','')
   if not key or not secret: raise RuntimeError('Binance LIVE API keys not configured')
   p.update(timestamp=int(time.time()*1000),recvWindow=5000)
   query=urlencode(p)
   p['signature']=hmac.new(secret.encode(),query.encode(),hashlib.sha256).hexdigest()
   headers['X-MBX-APIKEY']=key
  try:
   r=HTTP.get(BASE+path,params=p,headers=headers,timeout=12)
  except requests.RequestException as e: raise RuntimeError('Binance network error') from e
  if r.status_code in (418,429):
   retry=r.headers.get('Retry-After','')
   try: delay=max(120,min(int(retry),86400))
   except (ValueError,TypeError): delay=3600 if r.status_code==418 else 300
   _state['cooldown_until']=time.time()+delay
   _state['reason']=f'Binance HTTP {r.status_code} â€” Ø§Ù†ØªØ¸Ø§Ø± {delay} Ø«Ø§Ù†ÙŠØ©'
   try:
    with db() as conn, conn.cursor() as c:
     c.execute('UPDATE bot_binance_cooldown SET until_ms=%s,reason=%s WHERE id=1',(int(_state['cooldown_until']*1000),_state['reason']))
   except Exception: logging.exception('Cooldown persistence failed')
   raise RuntimeError(_state['reason'])
  if not r.ok: raise RuntimeError(f'Binance HTTP {r.status_code}: {r.text[:120]}')
  return r.json()

def load_cooldown():
 try:
  with db() as conn, conn.cursor() as c:
   c.execute('SELECT until_ms,reason FROM bot_binance_cooldown WHERE id=1')
   row=c.fetchone()
  if row:
   with _lock:
    _state['cooldown_until']=max(_state['cooldown_until'],int(row[0])/1000)
    if _state['cooldown_until']>time.time(): _state['reason']=row[1]
 except Exception: logging.exception('Could not restore cooldown')

def balance_cached():
 with _lock:
  if time.time()-_state['balance_at']<180: return _state['balance']
  _state['balance_at']=time.time()
 try:
  data=_request('/fapi/v2/account',signed=True)
  asset=next((a for a in data.get('assets',[]) if a.get('asset')=='USDT'),None)
  value=float(asset['availableBalance']) if asset else None
  with _lock: _state['balance']=value
 except Exception as e: logging.warning('Demo balance unavailable: %s',e)
 return _state['balance']

def ema(vals,n):
 if len(vals)<n: return None
 v=sum(vals[:n])/n
 for x in vals[n:]: v+=(x-v)*2/(n+1)
 return v

def proposal(symbol):
 candles=_request('/fapi/v1/klines',{'symbol':symbol,'interval':'5m','limit':100})[:-1]
 if len(candles)<35: return None
 closes=[float(x[4]) for x in candles]; highs=[float(x[2]) for x in candles]; lows=[float(x[3]) for x in candles]
 fast=ema(closes,10);slow=ema(closes,30)
 tr=[max(highs[i]-lows[i],abs(highs[i]-closes[i-1]),abs(lows[i]-closes[i-1])) for i in range(1,len(closes))]
 atr=sum(tr[-14:])/14
 if not atr: return None
 direction='LONG' if fast>slow else 'SHORT'
 entry=closes[-1]; distance=atr*1.2
 return {'symbol':symbol,'direction':direction,'entry':round(entry,6),'stop':round(entry-distance if direction=='LONG' else entry+distance,6),'take':round(entry+distance*1.8 if direction=='LONG' else entry-distance*1.8,6)}

STYLE='''<style>body{font:16px system-ui;background:#101827;color:#f5f7fb;margin:0}main{max-width:1100px;margin:auto;padding:18px}article{background:#202d40;border-radius:12px;padding:18px;margin:14px 0}a{color:#91c9ff}input,button,select{font:inherit;padding:10px;border-radius:7px;margin:5px;max-width:95%}button{background:#238657;color:white;border:0;cursor:pointer}table{width:100%;border-collapse:collapse}td,th{padding:9px;border-bottom:1px solid #435069;text-align:right}small{color:#cbd5e1}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px}iframe{width:100%;height:260px;border:0}</style>'''
PAGE='''<!doctype html><html lang="ar" dir="rtl"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Ø§Ù„Ø¨ÙˆØª Ø§Ù„ÙƒÙˆÙŠØªÙŠ V57 LIVE</title>'''+STYLE+'''<main><h1>ðŸ‡°ðŸ‡¼ Ø§Ù„Ø¨ÙˆØª Ø§Ù„ÙƒÙˆÙŠØªÙŠ V57 â€” ØªØ¯Ø§ÙˆÙ„ Ø­Ù‚ÙŠÙ‚ÙŠ LIVE</h1><p>âš ï¸ Ø§Ù„ØªØ¯Ø§ÙˆÙ„ Ø§Ù„Ø­Ù‚ÙŠÙ‚ÙŠ Ù…ÙØ¹Ù„ Ø¹Ù†Ø¯ Ø§Ø³ØªØ¦Ù†Ø§Ù Ø§Ù„Ø¨ÙˆØª. Ù…Ø¬Ù…ÙˆØ¹ Ø§Ù„Ù‚ÙŠÙ…Ø© Ø§Ù„Ø§Ø³Ù…ÙŠØ© Ù„ÙƒÙ„ Ø§Ù„Ù…Ø±Ø§ÙƒØ² Ù„Ø§ ÙŠØªØ¬Ø§ÙˆØ² 10% Ù…Ù† Ø­Ù‚ÙˆÙ‚ Ø§Ù„Ù…Ø­ÙØ¸Ø© Ø­Ø³Ø¨ ÙØ­Øµ Ù…Ø§ Ù‚Ø¨Ù„ Ø§Ù„Ø£Ù…Ø±Ø› ØªØºÙŠØ±Ø§Øª Ø§Ù„Ø£Ø³Ø¹Ø§Ø± ÙˆØ§Ù„Ø·Ù„Ø¨Ø§Øª Ø§Ù„Ø®Ø§Ø±Ø¬ÙŠØ© Ù‚Ø¯ ØªØ±ÙØ¹ Ø§Ù„Ù†Ø³Ø¨Ø© Ù„Ø§Ø­Ù‚Ø§Ù‹.</p><p><a href="/investors">Ø§Ù„Ù…Ø³ØªØ«Ù…Ø±ÙˆÙ†</a> | <a href="/owner/investors">Ø·Ù„Ø¨Ø§Øª Ø§Ù„Ø§Ø´ØªØ±Ø§Ùƒ</a> | <a href="/owner/bank-accounts">Ø§Ù„Ø­Ø³Ø§Ø¨Ø§Øª Ø§Ù„Ø¨Ù†ÙƒÙŠØ©</a> | <a href="/backtest">Ø§Ù„Ø§Ø®ØªØ¨Ø§Ø± Ø§Ù„ØªØ§Ø±ÙŠØ®ÙŠ</a> | <a href="/logout">Ø®Ø±ÙˆØ¬</a></p>{% for msg in get_flashed_messages() %}<article>{{msg}}</article>{% endfor %}<article><h2>ðŸ’° Ø±ØµÙŠØ¯ Binance Ø§Ù„Ø­Ù‚ÙŠÙ‚ÙŠ</h2><p id="balance">{% if balance is not none %}{{balance}} USDT{% else %}Ù„Ø§ ÙŠÙˆØ¬Ø¯ Ø±ØµÙŠØ¯ Ù…Ø¤ÙƒØ¯ Ù…Ø­ÙÙˆØ¸{% endif %}</p><small>Ø§Ù„Ø±ØµÙŠØ¯ Ù…Ø®Ø²Ù† Ù…Ø¤Ù‚ØªØ§Ù‹ 3 Ø¯Ù‚Ø§Ø¦Ù‚. Ù‡Ø¯Ù 20% ÙŠÙˆÙ…ÙŠØ§Ù‹ ØºÙŠØ± Ù…Ø¶Ù…ÙˆÙ†.</small></article><article><h2>âš™ï¸ Ø¥Ø¹Ø¯Ø§Ø¯Ø§Øª Ø§Ù„ØªØ¯Ø§ÙˆÙ„ Ø§Ù„Ø­Ù‚ÙŠÙ‚ÙŠ</h2><form action='/settings' method='post'><input type='hidden' name='csrf' value='{{csrf}}'><label>Ø§Ù„Ø­Ø¯ Ø§Ù„Ø£Ù‚ØµÙ‰ Ù„Ù…Ø¬Ù…ÙˆØ¹ Ø§Ù„Ù…Ø±Ø§ÙƒØ² Ù…Ù† Ù‚ÙŠÙ…Ø© Ø§Ù„Ù…Ø­ÙØ¸Ø© (%) <input type='number' step='0.1' min='0.1' max='10' name='exposure' value='{{settings.max_total_exposure_pct}}'></label><label>Ø§Ù„Ø±Ø§ÙØ¹Ø© (1â€“20) <input type='number' min='1' max='20' name='leverage' value='{{settings.leverage}}'></label><label>Ø§Ù„Ø­Ø¯ Ø§Ù„ÙŠÙˆÙ…ÙŠ Ù„Ù„ØµÙÙ‚Ø§Øª (1â€“100) <input type='number' min='1' max='100' name='daily' value='{{settings.max_daily_trades}}'></label><label>Ù‡Ø¯Ù Ø§Ù„Ø±Ø¨Ø­ Ø§Ù„ÙŠÙˆÙ…ÙŠ (%) <input type='number' step='0.1' min='0' max='100' name='target' value='{{settings.daily_target_pct}}'></label><button>Ø­ÙØ¸ Ø§Ù„Ø¥Ø¹Ø¯Ø§Ø¯Ø§Øª</button></form><p id='pnl'>Ø§Ù„Ø±Ø¨Ø­ ÙˆØ§Ù„Ø®Ø³Ø§Ø±Ø© Ø§Ù„ÙŠÙˆÙ…ÙŠØ©: Ø§Ø¶ØºØ· ØªØ­Ø¯ÙŠØ« Ø§Ù„ØªÙ‚Ø±ÙŠØ±</p><button type='button' onclick='loadPnl()'>ØªØ­Ø¯ÙŠØ« ØªÙ‚Ø±ÙŠØ± Ø§Ù„Ø£Ø±Ø¨Ø§Ø­ ÙˆØ§Ù„Ø®Ø³Ø§Ø¦Ø±</button><small>Ø§Ù„Ø¥ÙŠÙ‚Ø§Ù ÙŠÙ…Ù†Ø¹ ØµÙÙ‚Ø§Øª Ø¬Ø¯ÙŠØ¯Ø© ÙˆÙ„Ø§ ÙŠØºÙ„Ù‚ Ø§Ù„Ù…Ø±Ø§ÙƒØ² Ø§Ù„Ù…ÙØªÙˆØ­Ø©. Ù†Ø³Ø¨Ø© 10% ØªØ´Ù…Ù„ Ø¬Ù…ÙŠØ¹ Ø§Ù„Ù…Ø±Ø§ÙƒØ² Ø§Ù„Ù…ÙØªÙˆØ­Ø© ÙÙŠ Ø§Ù„Ø­Ø³Ø§Ø¨.</small></article><article><h2>ðŸ›¡ï¸ Ø­Ø§Ù„Ø© Binance</h2><p id="ban">Ø¬Ø§Ø±Ù Ø§Ù„ÙØ­Øµ</p></article><article><h2>ðŸ›‘ Ø¥ÙŠÙ‚Ø§Ù / Ø§Ø³ØªØ¦Ù†Ø§Ù ÙØ­Øµ Ø§Ù„Ø¥Ø´Ø§Ø±Ø§Øª</h2><p>{{'Ù…ØªÙˆÙ‚Ù' if paused else 'Ù†Ø´Ø· (ØªØ¯Ø§ÙˆÙ„ Ø­Ù‚ÙŠÙ‚ÙŠ)'}}</p><form action="/toggle-bot" method="post"><input type="hidden" name="csrf" value="{{csrf}}"><button name="action" value="{{'resume' if paused else 'stop'}}">{{'Ø§Ø³ØªØ¦Ù†Ø§Ù Ø§Ù„ÙØ­Øµ' if paused else 'Ø¥ÙŠÙ‚Ø§Ù Ø§Ù„ÙØ­Øµ'}}</button></form></article><article><h2>ðŸ“Š Ø£Ø³Ø¹Ø§Ø± Binance</h2><p id="prices">Ø¬Ø§Ø±Ù Ø§Ù„ØªØ­Ù…ÙŠÙ„</p></article><article><h2>ðŸ“ˆ TradingView</h2><div class="grid"><iframe loading="lazy" src="https://s.tradingview.com/widgetembed/?symbol=BINANCE%3ABTCUSDT&interval=5&theme=dark"></iframe><iframe loading="lazy" src="https://s.tradingview.com/widgetembed/?symbol=BINANCE%3AETHUSDT&interval=5&theme=dark"></iframe></div></article><article><h2>ðŸ”Ž ÙØ­Øµ Ø§Ù„Ø¥Ø´Ø§Ø±Ø§Øª</h2><form action="/api/scan" method="post"><input type="hidden" name="csrf" value="{{csrf}}"><button>ÙØ­Øµ BTC Ùˆ ETH ÙˆØªÙ†ÙÙŠØ° Ø§Ù„ÙØ±Øµ ÙˆÙÙ‚ Ø§Ù„Ø­Ø¯ÙˆØ¯</button></form><small>ÙŠÙ…ÙƒÙ† Ø¬Ø¯ÙˆÙ„Ø© /cron/scan Ù…Ø¹ CRON_SECRET.</small></article><article><h2>Ø¢Ø®Ø± Ø£ÙˆØ§Ù…Ø± Ø§Ù„Ø¨ÙˆØª</h2><table><tr><th>Ø§Ù„ÙˆÙ‚Øª</th><th>Ø§Ù„Ø±Ù…Ø²</th><th>Ø§Ù„Ø§ØªØ¬Ø§Ù‡</th><th>Ø§Ù„Ø­Ø§Ù„Ø©</th></tr>{% for x in recent %}<tr><td>{{x.created_at}}</td><td>{{x.symbol}}</td><td>{{x.direction}}</td><td>{{x.status}}</td></tr>{% else %}<tr><td colspan="4">Ù„Ø§ ØªÙˆØ¬Ø¯ Ø¥Ø´Ø§Ø±Ø§Øª</td></tr>{% endfor %}</table></article></main><script>async function loadPnl(){let el=document.getElementById('pnl');el.textContent='Ø¬Ø§Ø±Ù Ø§Ù„ØªØ­Ø¯ÙŠØ«...';try{let r=await fetch('/api/live-status');let d=await r.json();el.textContent=d.error||('Ø§Ù„Ø±Ø¨Ø­ ÙˆØ§Ù„Ø®Ø³Ø§Ø±Ø© Ø§Ù„Ù…Ø­Ù‚Ù‚Ø© Ø§Ù„ÙŠÙˆÙ…: '+d.daily_realized_pnl+' USDT ('+d.daily_realized_pct+'%) | Ø¥Ø¬Ù…Ø§Ù„ÙŠ Ø§Ù„Ù…Ø±Ø§ÙƒØ²: '+d.exposure_pct+'% Ù…Ù† Ø§Ù„Ù…Ø­ÙØ¸Ø©')}catch(e){el.textContent='ØªØ¹Ø°Ø± Ø¬Ù„Ø¨ Ø§Ù„ØªÙ‚Ø±ÙŠØ±'}}async function update(){try{let b=await(await fetch('/api/binance-ban-status')).json();document.getElementById('ban').textContent=b.remaining_seconds?b.reason+' | '+b.remaining_seconds+' Ø«Ø§Ù†ÙŠØ©':'Ø§Ù„Ø§ØªØµØ§Ù„ Ù…Ø³ØªÙ‚Ø±';let p=await(await fetch('/api/market-prices')).json();document.getElementById('prices').textContent=Object.entries(p.prices||{}).map(([s,v])=>s+': '+(v.price??'â€”')).join(' | ')}catch(e){}}update();setInterval(update,15000)</script></html>'''
LOGIN='''<!doctype html><html lang="ar" dir="rtl"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'''+STYLE+'''<main><h1>ðŸ‡°ðŸ‡¼ Ø¯Ø®ÙˆÙ„ Ù…Ø³Ø¤ÙˆÙ„ Ø§Ù„Ø¨ÙˆØª</h1>{% for msg in get_flashed_messages() %}<p>{{msg}}</p>{% endfor %}<form method="post"><input type="hidden" name="csrf" value="{{csrf}}"><input type="password" name="password" placeholder="ÙƒÙ„Ù…Ø© Ù…Ø±ÙˆØ± Ø§Ù„Ù…Ø³Ø¤ÙˆÙ„" required><button>Ø¯Ø®ÙˆÙ„</button></form></main></html>'''

@app.route('/health')
def health(): return {'status':'ok','mode':'LIVE','version':'V57-live-total-cap-10'}
@app.route('/login',methods=['GET','POST'])
def login():
 if request.method=='POST':
  check_csrf(); password=os.environ.get('ADMIN_PASSWORD','')
  if password and secrets.compare_digest(request.form.get('password',''),password):
   session.clear();session['admin']=True;session.permanent=True;return redirect(url_for('home'))
  flash('ÙƒÙ„Ù…Ø© Ø§Ù„Ù…Ø±ÙˆØ± ØºÙŠØ± ØµØ­ÙŠØ­Ø©')
 return render_template_string(LOGIN,csrf=csrf())
@app.route('/logout')
def logout(): session.clear();return redirect(url_for('login'))
@app.route('/')
@admin_required
def home():
 try:
  with db() as conn,conn.cursor(cursor_factory=RealDictCursor) as c:
   c.execute('SELECT is_paused FROM bot_preferences WHERE id=1'); paused=c.fetchone()['is_paused']
   c.execute('SELECT * FROM bot_orders_v2 ORDER BY id DESC LIMIT 25');recent=c.fetchall()
 except Exception: paused=True;recent=[]
 
 try:
  with db() as conn,conn.cursor(cursor_factory=RealDictCursor) as c:
   c.execute('SELECT * FROM bot_preferences WHERE id=1');settings=c.fetchone()
 except Exception:settings={'max_total_exposure_pct':10,'leverage':20,'max_daily_trades':100,'daily_target_pct':20}
 realized=pct=None
 # PnL is fetched only on explicit /api/live-status, not every page load.
 return render_template_string(PAGE,csrf=csrf(),balance=_state['balance'],paused=paused,recent=recent,settings=settings,daily_realized=realized,daily_pct=pct)
@app.route('/api/binance-ban-status')
@admin_required
def ban_status():return jsonify(cooldown())
@app.route('/api/market-prices')
@admin_required
def market_prices():
 with _lock:
  if time.time()-_state['prices_at']<30:return jsonify(prices=_state['prices'],source='Binance LIVE REST cache',realtime=False)
  _state['prices_at']=time.time()
 try:
  result=_request('/fapi/v1/ticker/price',{'symbol':'BTCUSDT'})
  prices={'BTCUSDT':{'price':float(result['price'])}}
  with _lock:_state['prices']=prices
 except Exception as e: logging.warning('Prices unavailable: %s',e)
 return jsonify(prices=_state['prices'],source='Binance LIVE REST cache',realtime=False)
@app.route('/api/balance')
@admin_required
def api_balance():return jsonify(balance=balance_cached(),mode='LIVE')
@app.route('/toggle-bot',methods=['POST'])
@admin_required
def toggle_bot():
 check_csrf();paused=request.form.get('action')!='resume'
 with db() as conn,conn.cursor() as c:c.execute('UPDATE bot_preferences SET is_paused=%s,updated_at=NOW() WHERE id=1',(paused,))
 flash('ØªÙ… ØªØ­Ø¯ÙŠØ« Ø­Ø§Ù„Ø© ÙØ­Øµ Ø§Ù„Ø¥Ø´Ø§Ø±Ø§Øª');return redirect(url_for('home'))
@app.route('/settings',methods=['POST'])
@admin_required
def settings_update():
 check_csrf()
 try:
  exposure=Decimal(request.form['exposure']); leverage=int(request.form['leverage']);daily=int(request.form['daily']);target=Decimal(request.form['target'])
  if not (Decimal('0.1')<=exposure<=Decimal('10') and 1<=leverage<=20 and 1<=daily<=100 and 0<=target<=100):raise ValueError()
 except (ValueError,KeyError):abort(400,'Invalid settings')
 with db() as conn,conn.cursor() as c:
  c.execute('UPDATE bot_preferences SET max_total_exposure_pct=%s,leverage=%s,max_daily_trades=%s,daily_target_pct=%s,updated_at=NOW() WHERE id=1',(exposure,leverage,daily,target))
 flash('ØªÙ… Ø­ÙØ¸ Ø§Ù„Ø¥Ø¹Ø¯Ø§Ø¯Ø§Øª. Ø³Ù‚Ù Ù…Ø¬Ù…ÙˆØ¹ Ø§Ù„Ù…Ø±Ø§ÙƒØ² Ù„Ø§ ÙŠØªØ¬Ø§ÙˆØ² 10%.')
 return redirect(url_for('home'))

@app.route('/api/live-status')
@admin_required
def live_status():
 try:
  _,equity,used,positions=account_snapshot()
  pnl=daily_pnl()
  return jsonify(equity=str(equity),open_notional=str(used),exposure_pct=str(round(used/equity*100,3)) if equity>0 else None,daily_realized_pnl=str(pnl),daily_realized_pct=str(round(pnl/equity*100,3)) if equity>0 else None,open_positions=len(positions))
 except Exception as e:return jsonify(error=str(e)),503

@app.route('/api/scan',methods=['POST'])
@app.route('/cron/scan',methods=['GET','POST'])
def scan():
 if request.path=='/cron/scan':
  secret=os.environ.get('CRON_SECRET','')
  if not secret or not secrets.compare_digest(request.headers.get('X-Cron-Secret',''),secret):abort(403)
 else:
  if not session.get('admin'):abort(403)
  check_csrf()
 # DB advisory lock serializes workers / cron calls; skip overlapping scans.
 with db() as conn:
  with conn.cursor() as c:
   c.execute('SELECT pg_try_advisory_lock(57105710)');locked=c.fetchone()[0]
  if not locked:return jsonify(status='BUSY'),409
  try:
   with conn.cursor(cursor_factory=RealDictCursor) as c:
    c.execute('SELECT * FROM bot_preferences WHERE id=1');prefs=c.fetchone()
    if prefs['is_paused']:return jsonify(status='PAUSED')
    c.execute("SELECT COUNT(*) FROM bot_orders_v2 WHERE created_at >= date_trunc('day',NOW() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC' AND status='LIVE_SENT'")
    count=c.fetchone()['count']
   if count>=prefs['max_daily_trades']:return jsonify(status='DAILY_CAP')
   # Do not force trades merely to reach 100; evaluate at most two symbols.
   results=[]
   for symbol in SYMBOLS[:2]:
    if count>=prefs['max_daily_trades']:break
    try:
     idea=proposal(symbol)
     if not idea:continue
     # The 10% is TOTAL notional across ALL positions, never 10% per trade.
     trade=execute(idea,Decimal(str(prefs['max_total_exposure_pct'])),int(prefs['leverage']))
     with conn.cursor() as c:
      c.execute('INSERT INTO bot_orders_v2(symbol,direction,entry_reference,stop_reference,take_reference,status,note,exchange_order_id) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)',(symbol,idea['direction'],idea['entry'],idea['stop'],idea['take'],'LIVE_SENT','Protective stop submitted: '+str(trade['stop_id']),str(trade['order_id'])))
     conn.commit();count+=1;results.append(trade)
    except Exception as e:
     logging.exception('LIVE scan failed for %s',symbol)
     results.append({'symbol':symbol,'error':str(e)})
     # Stop the scan on any error, especially ambiguous order/stop errors.
     break
   return jsonify(status='COMPLETE',mode='LIVE',results=results)
  finally:
   with conn.cursor() as c:c.execute('SELECT pg_advisory_unlock(57105710)')
@app.route('/backtest')
@admin_required
def backtest():
 try:
  candles=_request('/fapi/v1/klines',{'symbol':'BTCUSDT','interval':'5m','limit':100})[:-1]
  closes=[float(c[4]) for c in candles]
  wins=losses=0
  for i in range(30,len(closes)-1):
   fast=ema(closes[:i+1],10);slow=ema(closes[:i+1],30)
   if (closes[i+1]>closes[i])==(fast>slow):wins+=1
   else:losses+=1
  rate=round(100*wins/max(wins+losses,1),2)
  note='Ù‡Ø°Ø§ Ù‚ÙŠØ§Ø³ Ø§ØªØ¬Ø§Ù‡ Ø´Ù…Ø¹Ø© ÙˆØ§Ø­Ø¯Ø©ØŒ ÙˆÙ„ÙŠØ³ Ø§Ø®ØªØ¨Ø§Ø± Ø±Ø¨Ø­ÙŠØ© Ø£Ùˆ ØªÙ†ÙÙŠØ° Ø£ÙˆØ§Ù…Ø±.'
 except Exception as e:wins=losses=rate=0;note='ØªØ¹Ø°Ø± Ø¬Ù„Ø¨ Ø§Ù„Ø¨ÙŠØ§Ù†Ø§Øª: '+str(e)[:100]
 return render_template_string('<html lang="ar" dir="rtl"><meta charset="utf-8">'+STYLE+'<main><h1>ðŸ“ˆ Ø§Ø®ØªØ¨Ø§Ø± ØªØ§Ø±ÙŠØ®ÙŠ ØªØ¹Ù„ÙŠÙ…ÙŠ</h1><article>Ù†Ø³Ø¨Ø© ØªÙˆÙ‚Ø¹ Ø§Ù„Ø§ØªØ¬Ø§Ù‡: {{rate}}% | ØµØ­ÙŠØ­: {{wins}} | ØºÙŠØ± ØµØ­ÙŠØ­: {{losses}}<p>{{note}}</p></article><a href="/">Ø§Ù„Ø¹ÙˆØ¯Ø©</a></main></html>',rate=rate,wins=wins,losses=losses,note=note)
@app.route('/investors')
def investors():
 return render_template_string('<html lang="ar" dir="rtl"><meta charset="utf-8">'+STYLE+'<main><h1>Ø¨ÙˆØ§Ø¨Ø© Ø§Ù„Ù…Ø³ØªØ«Ù…Ø±ÙŠÙ† â€” ØªØ¬Ø±ÙŠØ¨ÙŠØ©</h1>{% for m in get_flashed_messages() %}<p>{{m}}</p>{% endfor %}<article><form method="post" action="/investors/register"><input type="hidden" name="csrf" value="{{csrf}}"><input type="email" name="email" placeholder="Ø§Ù„Ø¨Ø±ÙŠØ¯ Ø§Ù„Ø¥Ù„ÙƒØªØ±ÙˆÙ†ÙŠ" required><input type="password" name="password" placeholder="ÙƒÙ„Ù…Ø© Ø§Ù„Ù…Ø±ÙˆØ± (8 Ø£Ø­Ø±Ù Ø¹Ù„Ù‰ Ø§Ù„Ø£Ù‚Ù„)" minlength="8" required><input name="phone" placeholder="Ø§Ù„Ù‡Ø§ØªÙ"><label><input type="checkbox" name="terms" required>Ø£ÙˆØ§ÙÙ‚ Ø¹Ù„Ù‰ Ø´Ø±ÙˆØ· Ø§Ù„Ø§Ø³ØªØ®Ø¯Ø§Ù… Ø§Ù„ØªØ¬Ø±ÙŠØ¨ÙŠ ÙˆØ¹Ø¯Ù… Ø¶Ù…Ø§Ù† Ø§Ù„Ø¹ÙˆØ§Ø¦Ø¯</label><button>ØªØ³Ø¬ÙŠÙ„ Ø·Ù„Ø¨</button></form></article><a href="/">Ø§Ù„Ø±Ø¦ÙŠØ³ÙŠØ©</a></main></html>',csrf=csrf())
@app.route('/investors/register',methods=['POST'])
def investor_register():
 check_csrf();email=request.form.get('email','').strip().lower();pw=request.form.get('password','')
 if '@' not in email or len(pw)<8 or request.form.get('terms')!='on':abort(400)
 try:
  with db() as conn,conn.cursor() as c:c.execute('INSERT INTO investor_accounts(email,password_hash,phone_number,agreed_terms) VALUES(%s,%s,%s,TRUE)',(email,generate_password_hash(pw),request.form.get('phone','')[:40]))
  flash('ØªÙ… ØªØ³Ø¬ÙŠÙ„ Ø§Ù„Ø·Ù„Ø¨ØŒ Ø¨Ø§Ù†ØªØ¸Ø§Ø± Ù…ÙˆØ§ÙÙ‚Ø© Ø§Ù„Ù…Ø³Ø¤ÙˆÙ„')
 except Exception:flash('ØªØ¹Ø°Ø± Ø§Ù„ØªØ³Ø¬ÙŠÙ„ØŒ Ø±Ø¨Ù…Ø§ Ø§Ù„Ø¨Ø±ÙŠØ¯ Ù…Ø³Ø¬Ù„ Ø³Ø§Ø¨Ù‚Ø§Ù‹')
 return redirect(url_for('investors'))
@app.route('/owner/investors')
@admin_required
def owner_investors():
 with db() as conn,conn.cursor(cursor_factory=RealDictCursor) as c:c.execute('SELECT id,email,subscription_status,created_at FROM investor_accounts ORDER BY id DESC LIMIT 100');rows=c.fetchall()
 return render_template_string('<html lang="ar" dir="rtl"><meta charset="utf-8">'+STYLE+'<main><h1>Ø·Ù„Ø¨Ø§Øª Ø§Ù„Ù…Ø³ØªØ«Ù…Ø±ÙŠÙ†</h1><table><tr><th>Ø§Ù„Ø¨Ø±ÙŠØ¯</th><th>Ø§Ù„Ø­Ø§Ù„Ø©</th><th>Ø§Ù„Ø¥Ø¬Ø±Ø§Ø¡</th></tr>{% for r in rows %}<tr><td>{{r.email}}</td><td>{{r.subscription_status}}</td><td><form method="post" action="/owner/investors/status"><input type="hidden" name="csrf" value="{{csrf}}"><input type="hidden" name="id" value="{{r.id}}"><button name="status" value="APPROVED">Ù…ÙˆØ§ÙÙ‚Ø©</button><button name="status" value="REJECTED">Ø±ÙØ¶</button></form></td></tr>{% endfor %}</table><a href="/">Ø§Ù„Ø±Ø¦ÙŠØ³ÙŠØ©</a></main></html>',rows=rows,csrf=csrf())
@app.route('/owner/investors/status',methods=['POST'])
@admin_required
def investor_status():
 check_csrf();status=request.form.get('status')
 if status not in ('APPROVED','REJECTED'):abort(400)
 with db() as conn,conn.cursor() as c:c.execute('UPDATE investor_accounts SET subscription_status=%s WHERE id=%s',(status,int(request.form['id'])))
 return redirect(url_for('owner_investors'))
@app.route('/owner/bank-accounts')
@admin_required
def banks():
 with db() as conn,conn.cursor(cursor_factory=RealDictCursor) as c:c.execute('SELECT * FROM admin_bank_accounts ORDER BY id DESC');rows=c.fetchall()
 return render_template_string('<html lang="ar" dir="rtl"><meta charset="utf-8">'+STYLE+'<main><h1>ðŸ¦ Ø§Ù„Ø­Ø³Ø§Ø¨Ø§Øª Ø§Ù„Ø¨Ù†ÙƒÙŠØ©</h1><article><form method="post" action="/owner/bank-accounts/add"><input type="hidden" name="csrf" value="{{csrf}}"><input name="bank_name" placeholder="Ø§Ù„Ø¨Ù†Ùƒ" required><input name="account_holder" placeholder="ØµØ§Ø­Ø¨ Ø§Ù„Ø­Ø³Ø§Ø¨" required><input name="iban" placeholder="IBAN" required><input name="swift_code" placeholder="SWIFT"><input name="currency" value="KWD" required><button>Ø¥Ø¶Ø§ÙØ©</button></form></article><table><tr><th>Ø§Ù„Ø¨Ù†Ùƒ</th><th>ØµØ§Ø­Ø¨ Ø§Ù„Ø­Ø³Ø§Ø¨</th><th>Ø§Ù„Ø¹Ù…Ù„Ø©</th><th>Ø§Ù„Ø­Ø§Ù„Ø©</th></tr>{% for r in rows %}<tr><td>{{r.bank_name}}</td><td>{{r.account_holder}}</td><td>{{r.currency}}</td><td>{{"ÙØ¹Ø§Ù„" if r.is_active else "Ù…ÙˆÙ‚ÙˆÙ"}}</td></tr>{% endfor %}</table><a href="/">Ø§Ù„Ø±Ø¦ÙŠØ³ÙŠØ©</a></main></html>',rows=rows,csrf=csrf())
@app.route('/owner/bank-accounts/add',methods=['POST'])
@admin_required
def add_bank():
 check_csrf()
 vals=[request.form.get(x,'').strip() for x in ('bank_name','account_holder','iban','swift_code','currency')]
 if not all(vals[i] for i in (0,1,2,4)):abort(400)
 with db() as conn,conn.cursor() as c:c.execute('INSERT INTO admin_bank_accounts(bank_name,account_holder,iban,swift_code,currency) VALUES(%s,%s,%s,%s,%s)',vals)
 return redirect(url_for('banks'))

try:
 if DATABASE_URL:
  init();load_cooldown()
except Exception:logging.exception('Database startup initialization failed')
if __name__=='__main__':app.run(host='0.0.0.0',port=int(os.environ.get('PORT','10000')))
