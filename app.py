"""Alkuwaiti Bot - human approved USDT-M futures execution.
Requires explicit LIVE_TRADING_ENABLED=true. Test with Binance testnet first.
"""
import os, time, hmac, hashlib, secrets, logging, threading, smtplib, ssl, re, json
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from datetime import timedelta
from functools import wraps
from urllib.parse import urlencode
from email.message import EmailMessage
import requests, psycopg2
from psycopg2.extras import RealDictCursor
from flask import Flask, request, session, redirect, url_for, flash, render_template_string
from werkzeug.security import check_password_hash, generate_password_hash
from cryptography.fernet import Fernet, InvalidToken

app=Flask(__name__)
app.secret_key=os.environ.get('SECRET_KEY') or secrets.token_hex(32)  # Configure SECRET_KEY on Render; otherwise sessions reset on restart
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SECURE=True, SESSION_COOKIE_SAMESITE='Lax', PERMANENT_SESSION_LIFETIME=3600)
BASE=os.environ.get('BINANCE_BASE_URL','https://fapi.binance.com').rstrip('/')
# API credentials can be rotated from the admin dashboard. Encrypted at rest in Neon.
def credentials():
    master=os.environ.get('CREDENTIALS_ENCRYPTION_KEY','')
    if master:
        with db() as conn,conn.cursor() as c:
            c.execute("SELECT api_key_cipher,api_secret_cipher FROM bot_api_credentials WHERE id=1")
            row=c.fetchone()
        if row:
            f=Fernet(master.encode())
            return f.decrypt(bytes(row[0])).decode(),f.decrypt(bytes(row[1])).decode()
    return os.environ.get('BINANCE_API_KEY',''),os.environ.get('BINANCE_API_SECRET','')

SYMBOLS=('BTCUSDT','ETHUSDT','BNBUSDT','SOLUSDT','XRPUSDT','XAUUSDT','XAGUSDT','CLUSDT','BZUSDT')
LIVE=False  # V34 safety lock: real orders disabled until independent audit and testnet verification
# Separate safety interlock: live trading is blocked until execution is audited/tested.
EXECUTION_AUDITED=os.environ.get('EXECUTION_AUDITED','false').lower()=='true'
D=Decimal
TARGET_NET_ROI=D('0.10')
ESTIMATED_ROUND_TRIP_COST=D('0.002')  # conservative fee/slippage buffer; funding unknown
logging.basicConfig(level=logging.INFO)

def db():
    return psycopg2.connect(os.environ['DATABASE_URL'], connect_timeout=8, sslmode='require')

def init():
    with db() as conn, conn.cursor() as c:
        c.execute('''CREATE TABLE IF NOT EXISTS bot_preferences (
            id INTEGER PRIMARY KEY CHECK (id=1),
            max_exposure_pct NUMERIC NOT NULL DEFAULT 10,
            max_leverage INTEGER NOT NULL DEFAULT 10,
            target_roi_pct NUMERIC NOT NULL DEFAULT 10,
            updated_at TIMESTAMPTZ DEFAULT NOW())''')
        c.execute('''INSERT INTO bot_preferences(id) VALUES(1) ON CONFLICT(id) DO NOTHING''')
        c.execute('''ALTER TABLE bot_preferences ADD COLUMN IF NOT EXISTS is_paused BOOLEAN NOT NULL DEFAULT FALSE''')
        c.execute('''CREATE TABLE IF NOT EXISTS bot_api_credentials (id INTEGER PRIMARY KEY CHECK (id=1), api_key_cipher BYTEA NOT NULL, api_secret_cipher BYTEA NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())''')
        c.execute('''CREATE TABLE IF NOT EXISTS bot_close_suggestions (
            symbol TEXT PRIMARY KEY, position_amt NUMERIC NOT NULL,
            estimated_net_roi NUMERIC NOT NULL, estimated_net_usdt NUMERIC NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            status TEXT NOT NULL DEFAULT 'PENDING')''')
        c.execute('''CREATE TABLE IF NOT EXISTS bot_scan_status (
            id INTEGER PRIMARY KEY CHECK(id=1),
            checked_at TIMESTAMPTZ, opportunities INTEGER DEFAULT 0,
            reason TEXT)''')
        c.execute("INSERT INTO bot_scan_status(id) VALUES(1) ON CONFLICT(id) DO NOTHING")
        c.execute('''CREATE TABLE IF NOT EXISTS bot_orders_v2 (
            id BIGSERIAL PRIMARY KEY, symbol TEXT NOT NULL, direction TEXT NOT NULL,
            entry_reference NUMERIC NOT NULL, stop_reference NUMERIC NOT NULL,
            take_reference NUMERIC NOT NULL, created_at TIMESTAMPTZ DEFAULT NOW(),
            status TEXT NOT NULL DEFAULT 'PENDING', order_id TEXT, close_id TEXT,
            entry_price NUMERIC, quantity NUMERIC, stop_algo_id TEXT, note TEXT)''')

def preferences():
    with db() as conn,conn.cursor(cursor_factory=RealDictCursor) as c:
        c.execute('SELECT * FROM bot_preferences WHERE id=1')
        return c.fetchone()

def is_bot_paused():
    # Fail closed: a database failure must not permit a new trade.
    with db() as conn, conn.cursor() as c:
        c.execute('SELECT is_paused FROM bot_preferences WHERE id=1')
        row = c.fetchone()
    if row is None:
        raise RuntimeError('ØªØ¹Ø°Ø± Ù‚Ø±Ø§Ø¡Ø© Ø­Ø§Ù„Ø© Ø¥ÙŠÙ‚Ø§Ù Ø§Ù„Ø¨ÙˆØª')
    return bool(row[0])

def allowed_leverage(symbol, requested):
    # Binance leverage brackets impose symbol-specific limits.
    brackets=signed('GET','/fapi/v1/leverageBracket',{'symbol':symbol})
    item=brackets[0] if isinstance(brackets,list) else brackets
    cap=max(int(x['initialLeverage']) for x in item['brackets'])
    return max(1,min(int(requested),cap,150))

# Binance 418/-1003 cooldown. Shared across Render workers via Neon.
# Countdown is based on Binance's "banned until" epoch (milliseconds), NOT an estimate.
_ban_lock=threading.RLock()
_ban_until_ms=0
_ban_reason=''
_last_shared_check=0.0
_public_cache={}
_public_cache_lock=threading.RLock()

def _ban_table():
    with db() as conn,conn.cursor() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS bot_binance_cooldown
                     (id INTEGER PRIMARY KEY CHECK(id=1), until_ms BIGINT NOT NULL DEFAULT 0,
                      reason TEXT NOT NULL DEFAULT '', updated_at TIMESTAMPTZ DEFAULT NOW())""")
        c.execute("INSERT INTO bot_binance_cooldown(id) VALUES(1) ON CONFLICT(id) DO NOTHING")

def _cooldown_state(force=False):
    global _ban_until_ms,_ban_reason,_last_shared_check
    now=time.time()
    with _ban_lock:
        if force or now-_last_shared_check>=10:
            _last_shared_check=now
            try:
                _ban_table()
                with db() as conn,conn.cursor() as c:
                    c.execute('SELECT until_ms,reason FROM bot_binance_cooldown WHERE id=1')
                    row=c.fetchone()
                if row and int(row[0])>_ban_until_ms:
                    _ban_until_ms=int(row[0]);_ban_reason=row[1]
            except Exception:
                logging.exception('Could not sync Binance cooldown from database')
        return {'until_ms':_ban_until_ms,'reason':_ban_reason,
                'remaining_seconds':max(0,(_ban_until_ms-int(now*1000)+999)//1000),
                'checked_at':datetime.now(timezone.utc).isoformat()}

def _record_limit(response):
    global _ban_until_ms,_ban_reason
    if response.status_code not in (418,429): return
    body=response.text[:700]
    now_ms=int(time.time()*1000)
    match=re.search(r'banned\s+until\s+(\d{13})',body,re.IGNORECASE)
    if not match:
        match=re.search(r'\b(1[6-9]\d{11}|2\d{12})\b',body) if response.status_code==418 else None
    if match:
        until=int(match.group(1))
        reason='Ø­Ø¸Ø± IP Ù…Ù† Binance (418)'
    else:
        try: wait=min(86400,max(60,int(float(response.headers.get('Retry-After','120')))))
        except (ValueError,TypeError): wait=120
        if response.status_code==418: wait=max(wait,900)
        until=now_ms+wait*1000
        reason='Ø­Ø¸Ø± BinanceØ› Ù…Ø¯Ø© Ø§Ù†ØªØ¸Ø§Ø± Ø§Ø­ØªÙŠØ§Ø·ÙŠØ© ÙˆÙ„ÙŠØ³Øª ÙˆÙ‚Øª Ø±ÙØ¹ Ø§Ù„Ø­Ø¸Ø± Ø§Ù„Ù…Ø¤ÙƒØ¯' if response.status_code==418 else 'ØªÙ‚ÙŠÙŠØ¯ Ø·Ù„Ø¨Ø§Øª Binance (429)'
    with _ban_lock:
        if until>_ban_until_ms:
            _ban_until_ms=until;_ban_reason=reason
        try:
            _ban_table()
            with db() as conn,conn.cursor() as c:
                c.execute("""UPDATE bot_binance_cooldown SET until_ms=GREATEST(until_ms,%s),
                             reason=CASE WHEN %s>=until_ms THEN %s ELSE reason END,
                             updated_at=NOW() WHERE id=1""",(until,until,reason))
        except Exception:
            logging.exception('Could not persist Binance cooldown')
    logging.warning('Binance throttled requests: %s; cooldown until %s',response.status_code,until)

def _assert_binance_available():
    state=_cooldown_state()
    if state['remaining_seconds']>0:
        raise ValueError('Binance Ù…Ø­Ø¸ÙˆØ± Ù…Ø¤Ù‚ØªÙ‹Ø§Ø› Ø§Ù„Ù…ØªØ¨Ù‚ÙŠ '+str(state['remaining_seconds'])+' Ø«Ø§Ù†ÙŠØ©. Ø£ÙˆÙ‚ÙÙ†Ø§ Ø·Ù„Ø¨Ø§Øª Binance Ø­ØªÙ‰ Ø§Ù†ØªÙ‡Ø§Ø¡ Ø§Ù„Ù…Ù‡Ù„Ø©.')

# V29: conservative cross-worker REST limiter. Binance limits are weighted,
# endpoint-specific and may also include other services on Render's shared IP.
# Minimum spacing is a conservative ceiling, not a guaranteed safe quota.
REST_MIN_INTERVAL_SECONDS = max(5.0, float(os.environ.get('BINANCE_MIN_REQUEST_INTERVAL', '5.0')))
# Public Yahoo quotes remain available, but Binance signal scans require explicit opt-in.
BINANCE_MARKET_SCANS_ENABLED = os.environ.get('BINANCE_MARKET_SCANS_ENABLED','false').lower() == 'true'
BINANCE_SCAN_INTERVAL_SECONDS = max(90, int(os.environ.get('BINANCE_SCAN_INTERVAL_SECONDS','90')))
_scan_local = threading.local()

# V33: cross-worker weighted budget. This is deliberately BELOW exchange quotas,
# not a representation of the Binance IP-wide limit. Other Render tenants may share IP.
BINANCE_WEIGHT_BUDGET_1M = min(100, max(10, int(os.environ.get('BINANCE_WEIGHT_BUDGET_1M', '50'))))
BINANCE_DASHBOARD_ACCOUNT_REFRESH = os.environ.get('BINANCE_DASHBOARD_ACCOUNT_REFRESH', 'false').lower() == 'true'

def _endpoint_weight(path, params=None):
    # Conservative upper bounds for the endpoints used here; reject unknown endpoints.
    # Actual weights can change at Binance. Update after checking official docs.
    if path == '/fapi/v1/income': return 40  # conservative internal estimate; verify against Binance headers
    if path == '/fapi/v1/klines': return 10
    if path == '/fapi/v1/exchangeInfo': return 10
    if path in ('/fapi/v2/account','/fapi/v3/account'): return 10
    if path in ('/fapi/v2/positionRisk','/fapi/v3/positionRisk'): return 10
    if path == '/fapi/v1/openOrders': return 40 if not (params or {}).get('symbol') else 5
    if path == '/fapi/v1/leverageBracket': return 10
    if path == '/fapi/v1/premiumIndex': return 10
    if path in ('/fapi/v1/order','/fapi/v1/leverage','/fapi/v1/algoOrder','/fapi/v1/allOpenOrders'): return 10
    raise ValueError('Binance endpoint not approved for rate limiter: '+path)

def _reserve_binance_request(path, params=None):
    # Atomic DB reservation across workers and services using the same Neon DB.
    # A rolling 60-second weighted budget prevents bursts at minute boundaries.
    _assert_binance_available()
    weight=_endpoint_weight(path,params)
    with db() as conn, conn.cursor() as c:
        c.execute('''CREATE TABLE IF NOT EXISTS bot_binance_weight_events
                     (id BIGSERIAL PRIMARY KEY, reserved_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                      weight INTEGER NOT NULL)''')
        c.execute('SELECT pg_advisory_xact_lock(918273645)')
        c.execute("DELETE FROM bot_binance_weight_events WHERE reserved_at < NOW() - INTERVAL '120 seconds'")
        c.execute("SELECT COALESCE(SUM(weight),0) FROM bot_binance_weight_events WHERE reserved_at > NOW() - INTERVAL '60 seconds'")
        used=int(c.fetchone()[0])
        if used+weight > BINANCE_WEIGHT_BUDGET_1M:
            raise ValueError('ØªÙ… ØªØ£Ø¬ÙŠÙ„ Ø·Ù„Ø¨ Binance: Ù…ÙŠØ²Ø§Ù†ÙŠØ© ÙˆØ²Ù† Ø§Ù„Ø·Ù„Ø¨Ø§Øª Ø®Ù„Ø§Ù„ 60 Ø«Ø§Ù†ÙŠØ© Ù…Ù…ØªÙ„Ø¦Ø©')
        # Keep the older minimum-gap mechanism; fail closed on a backlog.
        c.execute('''CREATE TABLE IF NOT EXISTS bot_binance_rate_limit
                     (id INTEGER PRIMARY KEY CHECK(id=1), next_allowed TIMESTAMPTZ NOT NULL DEFAULT NOW())''')
        c.execute('INSERT INTO bot_binance_rate_limit(id) VALUES(1) ON CONFLICT DO NOTHING')
        c.execute('SELECT next_allowed FROM bot_binance_rate_limit WHERE id=1 FOR UPDATE')
        next_allowed=c.fetchone()[0]
        now=datetime.now(timezone.utc)
        delay=max(0.0,(next_allowed-now).total_seconds())
        if delay>8: raise ValueError('ØªÙ… ØªØ£Ø¬ÙŠÙ„ Ø·Ù„Ø¨ Binance Ù„ØªØ¬Ù†Ø¨ ØªÙƒØ¯Ø³ Ø§Ù„Ø·Ù„Ø¨Ø§Øª')
        reserved_at=max(now,next_allowed)
        c.execute('UPDATE bot_binance_rate_limit SET next_allowed=%s WHERE id=1',
                  (reserved_at+timedelta(seconds=REST_MIN_INTERVAL_SECONDS),))
        c.execute('INSERT INTO bot_binance_weight_events(reserved_at,weight) VALUES(%s,%s)',(reserved_at,weight))
    if delay: time.sleep(delay)
    _assert_binance_available()

def _observe_binance_weight(response):
    # Observe Binance IP-wide weight, independent of local budget.

    # Do not compare an IP-wide count against our *bot-only* quota: that
    # caused repeated false 65-second cooldowns in earlier releases.
    # End note
    raw=response.headers.get('X-MBX-USED-WEIGHT-1M')
    if not raw:return
    try: used=int(raw)
    except (ValueError,TypeError):return
    # Conservative circuit breaker, not a promise of an exchange-wide limit.
    threshold=max(100, int(os.environ.get('BINANCE_IP_WEIGHT_SOFT_LIMIT','600')))
    if used<threshold:return
    global _ban_until_ms,_ban_reason
    until=int(time.time()*1000)+120000
    with _ban_lock:
        if until>_ban_until_ms:
            _ban_until_ms=until
            _ban_reason='Ø¥ÙŠÙ‚Ø§Ù Ø§Ø­ØªØ±Ø§Ø²ÙŠ: ÙˆØ²Ù† Ø·Ù„Ø¨Ø§Øª Ø¹Ù†ÙˆØ§Ù† IP Ù…Ø±ØªÙØ¹'
    try:
        _ban_table()
        with db() as conn,conn.cursor() as c:
            c.execute('''UPDATE bot_binance_cooldown SET until_ms=GREATEST(until_ms,%s),
                         reason=CASE WHEN %s>=until_ms THEN %s ELSE reason END,
                         updated_at=NOW() WHERE id=1''',(until,until,_ban_reason))
    except Exception:
        logging.exception('Could not persist IP weight protection')


def signed(method,path,params=None):
    _assert_binance_available()
    cache=getattr(_scan_local,'signed_cache',None)
    cache_key=(path,tuple(sorted((str(k),str(v)) for k,v in (params or {}).items())))
    if method=='GET' and cache is not None and path in ('/fapi/v2/account','/fapi/v2/positionRisk','/fapi/v1/openOrders','/fapi/v1/leverageBracket') and cache_key in cache:
        return cache[cache_key]
    _reserve_binance_request(path,params)
    KEY,SECRET=credentials()
    if not KEY or not SECRET: raise ValueError('Ù…ÙØ§ØªÙŠØ­ Binance ØºÙŠØ± Ù…ÙˆØ¬ÙˆØ¯Ø© ÙÙŠ Render')
    p=dict(params or {});p['timestamp']=int(time.time()*1000);p['recvWindow']=5000
    q=urlencode(p);sig=hmac.new(SECRET.encode(),q.encode(),hashlib.sha256).hexdigest()
    r=requests.request(method,BASE+path,params=q+'&signature='+sig,headers={'X-MBX-APIKEY':KEY},timeout=15)
    _record_limit(r)
    _observe_binance_weight(r)
    if not r.ok:
        if r.status_code in (418,429): raise ValueError('Binance Ø£ÙˆÙ‚Ù Ø§Ù„Ø·Ù„Ø¨Ø§Øª Ù…Ø¤Ù‚ØªÙ‹Ø§Ø› Ø±Ø§Ø¬Ø¹ Ø¹Ø¯Ø§Ø¯ Ø§Ù„Ø­Ø¸Ø± ÙÙŠ Ù„ÙˆØ­Ø© Ø§Ù„Ù…Ø³Ø¤ÙˆÙ„')
        raise ValueError('Binance '+str(r.status_code)+': '+r.text[:220])
    data=r.json()
    if method=='GET' and cache is not None and path in ('/fapi/v2/account','/fapi/v2/positionRisk','/fapi/v1/openOrders','/fapi/v1/leverageBracket'):
        cache[cache_key]=data
    return data

def public(path,p=None):
    _assert_binance_available()
    # Exchange metadata and closed candles do not need re-fetching every scan.
    key=(path,tuple(sorted((str(k),str(v)) for k,v in (p or {}).items())))
    ttl=3600 if path=='/fapi/v1/exchangeInfo' else (75 if path=='/fapi/v1/klines' else 15)
    with _public_cache_lock:
        item=_public_cache.get(key)
        if item and time.monotonic()-item[0]<ttl:return item[1]
    _reserve_binance_request(path,p)
    r=requests.get(BASE+path,params=p,timeout=12)
    _record_limit(r)
    _observe_binance_weight(r)
    if r.status_code in (418,429): raise ValueError('Binance Ø£ÙˆÙ‚Ù Ø§Ù„Ø·Ù„Ø¨Ø§Øª Ù…Ø¤Ù‚ØªÙ‹Ø§Ø› Ø±Ø§Ø¬Ø¹ Ø¹Ø¯Ø§Ø¯ Ø§Ù„Ø­Ø¸Ø±')
    r.raise_for_status()
    data=r.json()
    with _public_cache_lock:
        if len(_public_cache)>200:_public_cache.clear()
        _public_cache[key]=(time.monotonic(),data)
    return data

def auth(f):
    @wraps(f)
    def wrap(*a,**kw):
        if not session.get('admin'): return redirect(url_for('login'))
        return f(*a,**kw)
    return wrap

@app.route('/api/v32-connection-status')
@auth
def v32_connection_status():
    state=_cooldown_state(force=True)
    return {'binance':state, 'market_scans_enabled':BINANCE_MARKET_SCANS_ENABLED,
            'scan_interval_seconds':BINANCE_SCAN_INTERVAL_SECONDS,
            'minimum_request_interval_seconds':REST_MIN_INTERVAL_SECONDS,
            'weighted_budget_per_60_seconds':BINANCE_WEIGHT_BUDGET_1M,
            'dashboard_binance_refresh_enabled':BINANCE_DASHBOARD_ACCOUNT_REFRESH,
            'external_quotes':'Yahoo Finance (indicative; not Binance execution prices)'}

@app.route('/api/binance-ban-status')
@auth
def binance_ban_status():
    return _cooldown_state(force=True)

def csrf():
    if 'csrf' not in session: session['csrf']=secrets.token_hex(24)
    return session['csrf']

def check_csrf():
    if not hmac.compare_digest(request.form.get('csrf',''),session.get('csrf','')): raise ValueError('Ø§Ù†ØªÙ‡Øª ØµÙ„Ø§Ø­ÙŠØ© Ø§Ù„Ù†Ù…ÙˆØ°Ø¬ØŒ Ø­Ø¯Ù‘Ø« Ø§Ù„ØµÙØ­Ø©')

def ema(vals,n):
    v=sum(vals[:n])/n
    for x in vals[n:]: v+=(x-v)*2/(n+1)
    return v

def proposal(symbol):
    candles=public('/fapi/v1/klines',{'symbol':symbol,'interval':'15m','limit':120})[:-1]
    closes=[float(c[4]) for c in candles]; highs=[float(c[2]) for c in candles]; lows=[float(c[3]) for c in candles]
    if len(closes)<60: return None
    fast,slow=ema(closes,20),ema(closes,50)
    direction='LONG' if fast>slow else 'SHORT'
    # ATR-like average true range; not a guarantee of price movement.
    trs=[max(highs[i]-lows[i],abs(highs[i]-closes[i-1]),abs(lows[i]-closes[i-1])) for i in range(1,len(closes))]
    atr=sum(trs[-14:])/14
    if atr<=0: return None
    entry=D(str(closes[-1])); distance=D(str(atr))*D('1.5')
    stop=entry-distance if direction=='LONG' else entry+distance
    # Technical target based on ATR; reject rather than invent a 5% target.
    target_distance=distance*2
    take=entry+target_distance if direction=='LONG' else entry-target_distance
    if stop<=0 or take<=0: return None
    return direction,entry,stop,take

def symbol_info(symbol):
    data=public('/fapi/v1/exchangeInfo')
    item=next((s for s in data['symbols'] if s['symbol']==symbol and s['status']=='TRADING'),None)
    if not item: raise ValueError('Ù‡Ø°Ø§ Ø§Ù„Ø±Ù…Ø² ØºÙŠØ± Ù…ØªØ§Ø­ Ù„Ù„ØªØ¯Ø§ÙˆÙ„ Ø­Ø§Ù„ÙŠÙ‹Ø§ Ø¹Ù„Ù‰ Binance Futures')
    return item

def floor_step(x,step): return (x/step).to_integral_value(rounding=ROUND_DOWN)*step

def strnum(x): return format(x,'f')

def positions():
    return [p for p in signed('GET','/fapi/v2/positionRisk') if D(p['positionAmt'])!=0]

def wallet(): return D(signed('GET','/fapi/v2/account')['totalWalletBalance'])

def feasibility(symbol, entry, stop, take, info=None):
    """Check market min size, free funds and aggregate 10% notional exposure."""
    info=info or symbol_info(symbol)
    account=signed('GET','/fapi/v2/account')
    equity=D(account['totalMarginBalance'])
    free=D(account['availableBalance'])
    if equity<=0 or free<=0: raise ValueError('Ù„Ø§ ÙŠÙˆØ¬Ø¯ Ø±ØµÙŠØ¯ Ù…ØªØ§Ø­')
    ps=positions()
    if any(x['symbol']==symbol for x in ps): raise ValueError('Ù…Ø±ÙƒØ² Ù…ÙØªÙˆØ­ Ø¹Ù„Ù‰ Ù†ÙØ³ Ø§Ù„Ø±Ù…Ø²')
    exposure=sum(abs(D(x['positionAmt']))*D(x['markPrice']) for x in ps)
    # Reserve notional for other pending opening orders, not protective/closing orders.
    for order in signed('GET','/fapi/v1/openOrders'):
        if order.get('reduceOnly') or order.get('closePosition'): continue
        if order.get('type') in ('STOP_MARKET','TAKE_PROFIT_MARKET'): continue
        remaining=max(D('0'),D(order.get('origQty','0'))-D(order.get('executedQty','0')))
        px=D(order.get('price') or '0')
        if px<=0:
            try: px=D(public('/fapi/v1/premiumIndex',{'symbol':order['symbol']})['markPrice'])
            except Exception: raise ValueError('ØªØ¹Ø°Ø± Ø§Ø­ØªØ³Ø§Ø¨ Ø§Ù„Ø£ÙˆØ§Ù…Ø± Ø§Ù„Ù…Ø¹Ù„Ù‚Ø©')
        exposure+=remaining*px
    prefs=preferences()
    room=equity*D(str(prefs['max_exposure_pct']))/D('100')-exposure
    filters={f['filterType']:f for f in info['filters']}
    lot=filters.get('MARKET_LOT_SIZE') or filters.get('LOT_SIZE')
    if not lot: raise ValueError('Ø´Ø±ÙˆØ· Ø­Ø¬Ù… Ø§Ù„Ø£Ù…Ø± ØºÙŠØ± Ù…ØªØ§Ø­Ø©')
    step=D(lot['stepSize']); minimum=D(lot['minQty'])
    mark=D(public('/fapi/v1/premiumIndex',{'symbol':symbol})['markPrice'])
    min_value=D(filters.get('MIN_NOTIONAL',{}).get('notional','5'))
    # Smallest possible valid order, round up to step.
    from decimal import ROUND_UP
    qty=max((minimum/step).to_integral_value(rounding=ROUND_UP)*step,(min_value/mark/step).to_integral_value(rounding=ROUND_UP)*step)
    if step<=0 or mark<=0 or qty<=0: raise ValueError('Ø´Ø±ÙˆØ· ÙƒÙ…ÙŠØ© Ø§Ù„ØªØ¯Ø§ÙˆÙ„ ØºÙŠØ± ØµØ§Ù„Ø­Ø©')
    if D(lot.get('maxQty','0'))>0 and qty>D(lot['maxQty']): raise ValueError('Ø§Ù„ÙƒÙ…ÙŠØ© Ø£ÙƒØ¨Ø± Ù…Ù† Ø­Ø¯ Binance')
    lev=allowed_leverage(symbol,prefs['max_leverage'])
    if D(str(prefs['max_exposure_pct']))>10 or D(str(prefs['max_exposure_pct']))<=0: raise ValueError('Ø§Ù„Ø­Ø¯ Ø§Ù„Ø£Ù‚ØµÙ‰ Ù„Ù„ØªØ¹Ø±Ø¶ ÙŠØ¬Ø¨ Ø£Ù† ÙŠÙƒÙˆÙ† Ø¨ÙŠÙ† 0 Ùˆ10 Ø¨Ø§Ù„Ù…Ø¦Ø©')
    if qty*mark>room or qty*mark/D(lev)>free*D('0.95'):
        raise ValueError('Ø§Ù„Ø­Ø¯ Ø§Ù„Ø£Ø¯Ù†Ù‰ ÙŠØªØ¬Ø§ÙˆØ² Ø³Ù‚Ù Ø§Ù„ØªØ¹Ø±Ø¶ Ø£Ùˆ Ø§Ù„Ù‡Ø§Ù…Ø´ Ø§Ù„Ù…ØªØ§Ø­')
    # Approximate net ROI on initial margin, not an assurance of profit.
    gross=((take-entry)/entry) if take>entry and stop<entry else ((entry-take)/entry) if take<entry and stop>entry else D('-1')
    net=(gross-ESTIMATED_ROUND_TRIP_COST)*D(lev)
    if gross<=0 or net<D(str(prefs['target_roi_pct']))/100:
        raise ValueError('Ø§Ù„Ù‡Ø¯Ù Ø§Ù„ÙÙ†ÙŠ Ù„Ø§ ÙŠØ­Ù‚Ù‚ Ù†Ø³Ø¨Ø© Ø§Ù„Ø±Ø¨Ø­ Ø§Ù„ØµØ§ÙÙŠ Ø§Ù„Ù…Ø³ØªÙ‡Ø¯ÙØ© Ø¹Ù„Ù‰ Ø§Ù„Ù‡Ø§Ù…Ø´')
    return qty,mark,net

def entry_order(row):
    if is_bot_paused(): raise ValueError('Ø§Ù„Ø¨ÙˆØª Ù…ØªÙˆÙ‚Ù Ø¨Ø£Ù…Ø± Ø§Ù„Ù…Ø³Ø¤ÙˆÙ„Ø› Ù„Ø§ ÙŠÙ…ÙƒÙ† ÙØªØ­ ØµÙÙ‚Ø© Ø¬Ø¯ÙŠØ¯Ø©')
    if not LIVE or not EXECUTION_AUDITED: raise ValueError('ØªÙ†ÙÙŠØ° Ø§Ù„ØªØ¯Ø§ÙˆÙ„ Ù…Ù‚ÙÙ„ Ù„Ø­ÙŠÙ† Ø§Ø®ØªØ¨Ø§Ø± Ø§Ù„Ø£ÙˆØ§Ù…Ø± ÙˆÙˆÙ‚Ù Ø§Ù„Ø®Ø³Ø§Ø±Ø© ÙˆØ§Ø¹ØªÙ…Ø§Ø¯ EXECUTION_AUDITED')
    symbol=row['symbol']; info=symbol_info(symbol)
    account=signed('GET','/fapi/v2/account')
    if account.get('dualSidePosition'): raise ValueError('ÙŠØ±Ø¬Ù‰ ØªØ¹Ø·ÙŠÙ„ Hedge ModeØ› Ù‡Ø°Ø§ Ø§Ù„Ø¥ØµØ¯Ø§Ø± ÙŠØ¯Ø¹Ù… One-way ÙÙ‚Ø·')
    if (datetime.now(timezone.utc)-row['created_at']).total_seconds()>900:
        raise ValueError('Ø§Ù†ØªÙ‡Øª ØµÙ„Ø§Ø­ÙŠØ© Ø§Ù„Ø¥Ø´Ø§Ø±Ø© (15 Ø¯Ù‚ÙŠÙ‚Ø©). Ø£Ø¹Ø¯ Ø§Ù„ØªØ­Ù„ÙŠÙ„')
    qty,mark,net=feasibility(symbol,D(row['entry_reference']),D(row['stop_reference']),D(row['take_reference']),info)
    take=D(row['take_reference'])
    if row['direction']=='LONG':
        live_net=(take-mark)/mark-ESTIMATED_ROUND_TRIP_COST
    else:
        live_net=(mark-take)/mark-ESTIMATED_ROUND_TRIP_COST
    prefs=preferences()
    lev=allowed_leverage(symbol,prefs['max_leverage'])
    if live_net*D(lev)<D(str(prefs['target_roi_pct']))/100:
        raise ValueError('Ø¨Ø¹Ø¯ ØªØºÙŠØ± Ø§Ù„Ø³Ø¹Ø± Ù„Ù… ÙŠØ¹Ø¯ Ø§Ù„Ù‡Ø¯Ù ÙŠØ­Ù‚Ù‚ ØµØ§ÙÙŠ Ø§Ù„Ø¹Ø§Ø¦Ø¯ Ø§Ù„ØªÙ‚Ø¯ÙŠØ±ÙŠ Ø§Ù„Ù…Ø­Ø¯Ø¯')
    filters={f['filterType']:f for f in info['filters']}
    # Ensure protective stop is on correct side of live mark price.
    stop=D(row['stop_reference']); side='BUY' if row['direction']=='LONG' else 'SELL'
    if (side=='BUY' and stop>=mark) or (side=='SELL' and stop<=mark):
        raise ValueError('Ø³Ø¹Ø± ÙˆÙ‚Ù Ø§Ù„Ø®Ø³Ø§Ø±Ø© Ù„Ù… ÙŠØ¹Ø¯ ØµØ§Ù„Ø­Ù‹Ø§ Ø¨Ø¹Ø¯ ØªØ­Ø±Ùƒ Ø§Ù„Ø³ÙˆÙ‚Ø› Ø£Ø¹Ø¯ Ø§Ù„ØªØ­Ù„ÙŠÙ„')
    price_filter=filters['PRICE_FILTER']; tick=D(price_filter['tickSize'])
    stop=floor_step(stop,tick)
    if stop<=0: raise ValueError('ÙˆÙ‚Ù Ø§Ù„Ø®Ø³Ø§Ø±Ø© ØºÙŠØ± ØµØ§Ù„Ø­')
    # Set isolated leverage conservatively. Do not change leverage on existing positions.
    signed('POST','/fapi/v1/marginType',{'symbol':symbol,'marginType':'ISOLATED'}) if False else None
    signed('POST','/fapi/v1/leverage',{'symbol':symbol,'leverage':lev})
    cid='ak'+str(row['id'])+'o'
    order=signed('POST','/fapi/v1/order',{'symbol':symbol,'side':side,'type':'MARKET','quantity':strnum(qty),'newClientOrderId':cid,'newOrderRespType':'RESULT'})
    # Binance conditional stop orders use Algo Service. If stop fails, immediately flatten.
    try:
        protect=signed('POST','/fapi/v1/algoOrder',{'algoType':'CONDITIONAL','symbol':symbol,
            'side':'SELL' if side=='BUY' else 'BUY','type':'STOP_MARKET',
            'triggerPrice':strnum(stop),'workingType':'MARK_PRICE','closePosition':'true'})
    except Exception as stop_error:
        try:
            signed('POST','/fapi/v1/order',{'symbol':symbol,'side':'SELL' if side=='BUY' else 'BUY',
                'type':'MARKET','quantity':strnum(qty),'reduceOnly':'true',
                'newClientOrderId':'ak'+str(row['id'])+'e'})
        except Exception:
            logging.exception('CRITICAL: STOP PLACEMENT AND EMERGENCY FLATTEN FAILED for %s',symbol)
            raise ValueError('Ø®Ø·Ø±: ÙØ´Ù„ ÙˆÙ‚Ù Ø§Ù„Ø®Ø³Ø§Ø±Ø© ÙˆØ§Ù„Ø¥ØºÙ„Ø§Ù‚ Ø§Ù„Ø·Ø§Ø±Ø¦! Ø§ÙØªØ­ Binance ÙÙˆØ±Ù‹Ø§ ÙˆØªØ­Ù‚Ù‚ Ù…Ù† Ø§Ù„Ù…Ø±ÙƒØ²')
        raise ValueError('ÙØ´Ù„ Ø¥Ù†Ø´Ø§Ø¡ ÙˆÙ‚Ù Ø§Ù„Ø®Ø³Ø§Ø±Ø©Ø› Ø£Ø±Ø³Ù„Ù†Ø§ Ø£Ù…Ø± Ø¥ØºÙ„Ø§Ù‚ Ø·Ø§Ø±Ø¦. ØªØ­Ù‚Ù‚ Ù…Ù† Binance: '+str(stop_error)[:90])
    return order,protect,qty


def auto_scan():
    """Generate proposals only; never place orders. Globally max once per 90 seconds."""
    if not BINANCE_MARKET_SCANS_ENABLED:
        return {'status':'binance_scans_disabled','created':0,'message':'Ø£Ø³Ø¹Ø§Ø± Yahoo Ù…Ø³ØªÙ…Ø±Ø©Ø› ØªØ­Ù„ÙŠÙ„ Ø´Ù…ÙˆØ¹ Binance Ù…ØªÙˆÙ‚Ù Ø§Ø­ØªØ±Ø§Ø²ÙŠÙ‹Ø§ Ø­ØªÙ‰ ØªÙØ¹ÙŠÙ„Ù‡ ÙŠØ¯ÙˆÙŠÙ‹Ø§'}
    if _cooldown_state()['remaining_seconds']>0:
        return {'status':'binance_banned','created':0}
    if is_bot_paused(): return {'status':'paused','created':0}
    init()
    with db() as conn, conn.cursor() as c:
        c.execute('''CREATE TABLE IF NOT EXISTS bot_scan_gate
                     (id INTEGER PRIMARY KEY CHECK(id=1), started_at TIMESTAMPTZ)''')
        c.execute('INSERT INTO bot_scan_gate(id) VALUES(1) ON CONFLICT DO NOTHING')
        c.execute('''UPDATE bot_scan_gate SET started_at=NOW() WHERE id=1
                     AND (started_at IS NULL OR started_at < NOW()-(%s * INTERVAL '1 second')) RETURNING id''',
                  (BINANCE_SCAN_INTERVAL_SECONDS,))
        if not c.fetchone(): return {'status':'rate_limited','created':0}
    if _cooldown_state()['remaining_seconds']>0:
        return {'status':'binance_banned','created':0}

    if is_bot_paused(): return {'status':'paused','created':0}
    created=0
    rejected={}
    _scan_local.signed_cache={}
    try:
        return _auto_scan_body()
    finally:
        _scan_local.signed_cache=None

def _auto_scan_body():
    created=0
    rejected={}
    with db() as conn,conn.cursor() as c:
        c.execute("SELECT pg_try_advisory_lock(68210314)")
        locked=c.fetchone()[0]
        if not locked:
            return {'status':'busy','created':0}
        try:
            c.execute("UPDATE bot_orders_v2 SET status='EXPIRED' WHERE status='PENDING' AND created_at<NOW()-INTERVAL '15 minutes'")
            c.execute("DELETE FROM bot_close_suggestions WHERE created_at<NOW()-INTERVAL '10 minutes' OR status!='PENDING'")
            for sym in SYMBOLS:
                try:
                    info=symbol_info(sym)
                    signal=proposal(sym)
                    if not signal:
                        rejected[sym]='Ù„Ø§ ØªÙˆØ¬Ø¯ Ø¥Ø´Ø§Ø±Ø©'
                        continue
                    direction,entry,stop,take=signal
                    feasibility(sym,entry,stop,take,info)
                    c.execute("SELECT 1 FROM bot_orders_v2 WHERE symbol=%s AND status IN ('PENDING','SUBMITTING','OPEN','CLOSING','REQUIRES_REVIEW') LIMIT 1",(sym,))
                    if c.fetchone():
                        rejected[sym]='ÙØ±ØµØ© Ø£Ùˆ Ù…Ø±ÙƒØ² Ø³Ø§Ø¨Ù‚ Ù‚ÙŠØ¯ Ø§Ù„Ù…ØªØ§Ø¨Ø¹Ø©'
                        continue
                    c.execute("INSERT INTO bot_orders_v2(symbol,direction,entry_reference,stop_reference,take_reference) VALUES(%s,%s,%s,%s,%s)",(sym,direction,entry,stop,take))
                    created+=1
                except Exception as e:
                    rejected[sym]=str(e)[:110]
            c.execute("UPDATE bot_scan_status SET checked_at=NOW(),opportunities=%s,reason=%s WHERE id=1",(created,str(rejected)[:1800]))
            return {'status':'ok','created':created,'excluded':len(rejected)}
        finally:
            c.execute("SELECT pg_advisory_unlock(68210314)")


_close_check_lock=threading.Lock()
_close_check_at=0.0
def monitor_close_suggestions():
    global _close_check_at
    with _close_check_lock:
        now=time.monotonic()
        if now-_close_check_at<60: return {'status':'cached'}
        _close_check_at=now
    init()
    prefs=preferences()
    threshold=D(str(prefs['target_roi_pct']))/100
    ps=positions()
    with db() as conn,conn.cursor() as c:
        c.execute("DELETE FROM bot_close_suggestions WHERE created_at<NOW()-INTERVAL '10 minutes'")
        for p in ps:
            symbol=p['symbol']
            if symbol not in SYMBOLS: continue
            amount=D(p['positionAmt']);mark=D(p['markPrice']);entry=D(p['entryPrice'])
            lev=D(str(p.get('leverage','1')))
            if entry<=0 or mark<=0 or amount==0 or lev<=0: continue
            gross=(mark-entry)/entry if amount>0 else (entry-mark)/entry
            roi=(gross-ESTIMATED_ROUND_TRIP_COST)*lev
            est_usdt=abs(amount)*(mark-entry)*(1 if amount>0 else -1)-abs(amount)*mark*ESTIMATED_ROUND_TRIP_COST
            if roi>=threshold and est_usdt>0:
                c.execute("""INSERT INTO bot_close_suggestions(symbol,position_amt,estimated_net_roi,estimated_net_usdt,created_at,status)
                  VALUES(%s,%s,%s,%s,NOW(),'PENDING')
                  ON CONFLICT(symbol) DO UPDATE SET position_amt=EXCLUDED.position_amt,
                  estimated_net_roi=EXCLUDED.estimated_net_roi,estimated_net_usdt=EXCLUDED.estimated_net_usdt,
                  created_at=NOW(),status=CASE WHEN bot_close_suggestions.status='REJECTED' AND bot_close_suggestions.created_at>NOW()-INTERVAL '10 minutes' THEN 'REJECTED' ELSE 'PENDING' END""",(symbol,amount,roi,est_usdt))
    return {'status':'ok'}

@app.route('/tasks/scan',methods=['POST'])
def scheduled_scan():
    expected=os.environ.get('SCAN_CRON_TOKEN','')
    provided=request.headers.get('X-Scan-Token','')
    if not expected or not hmac.compare_digest(expected,provided):
        return {'error':'unauthorized'},401
    try:
        return auto_scan()
    except Exception:
        logging.exception('Scheduled scan failed')
        return {'error':'scan failed'},500

@app.route('/close-suggestions/<symbol>/reject',methods=['POST'])
@auth
def reject_close_suggestion(symbol):
    try:
        check_csrf()
        with db() as conn,conn.cursor() as c:
            c.execute("UPDATE bot_close_suggestions SET status='REJECTED' WHERE symbol=%s AND status='PENDING'",(symbol,))
        flash('ØªÙ… Ø±ÙØ¶ Ø§Ù‚ØªØ±Ø§Ø­ Ø¬Ù†ÙŠ Ø§Ù„Ø±Ø¨Ø­ØŒ ÙˆØ§Ù„Ù…Ø±ÙƒØ² Ù„Ø§ ÙŠØ²Ø§Ù„ Ù…ÙØªÙˆØ­Ù‹Ø§.')
    except Exception as e:
        flash(str(e)[:160])
    return redirect(url_for('home'))

@app.route('/close-suggestions/<symbol>/approve',methods=['POST'])
@auth
def approve_close_suggestion(symbol):
    # The existing close handler sends the actual order only if live trading is enabled.
    try:
        check_csrf()
        with db() as conn,conn.cursor() as c:
            c.execute("SELECT 1 FROM bot_close_suggestions WHERE symbol=%s AND status='PENDING' AND created_at>NOW()-INTERVAL '10 minutes'",(symbol,))
            if not c.fetchone(): raise ValueError('Ø§Ù‚ØªØ±Ø§Ø­ Ø§Ù„Ø¥ØºÙ„Ø§Ù‚ Ø§Ù†ØªÙ‡Øª ØµÙ„Ø§Ø­ÙŠØªÙ‡')
        return close_position(symbol)
    except Exception as e:
        flash(str(e)[:160])
        return redirect(url_for('home'))

PAGE='''<!doctype html><html lang="ar" dir="rtl"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Ø§Ù„Ø¨ÙˆØª Ø§Ù„ÙƒÙˆÙŠØªÙŠ</title><style>
body{background:#101827;color:#f5f7fb;font:16px system-ui;margin:0}main{max-width:850px;margin:auto;padding:16px}article{background:#202d40;padding:16px;border-radius:14px;margin:14px 0;overflow:auto}button,input{font:inherit;padding:10px;border:0;border-radius:8px;margin:4px}button{background:#238657;color:white}button.danger{background:#b64040}a{color:#9fc8ff}table{width:100%;border-collapse:collapse}td,th{padding:8px;border-bottom:1px solid #435069;text-align:right}small{color:#bdc9da}.warn{color:#ffcf83}

/* Automatic daytime/nighttime palette */
:root{color-scheme:dark}body{transition:background-color .3s,color .3s}
html[data-theme="light"]{color-scheme:light}
html[data-theme="light"] body{background:#f4f7fc!important;color:#182438!important}
html[data-theme="light"] article{background:#fff!important;color:#182438!important;box-shadow:0 2px 14px #23385813}
html[data-theme="light"] a{color:#0759a7!important}
html[data-theme="light"] small{color:#46576a!important}
html[data-theme="light"] .warn{color:#8b5500!important}
html[data-theme="light"] td,html[data-theme="light"] th{border-color:#dce4ee!important}
html[data-theme="light"] input,html[data-theme="light"] select{background:#fff;color:#172438;border:1px solid #b9c9d9}
html[data-theme="light"] .terms{background:#eef3f9!important;color:#182438}
.logout-link{display:inline-block;padding:10px 14px;background:#a52e45!important;color:white!important;border-radius:10px;text-decoration:none;font-weight:700}
.theme-switch{display:inline-block;margin:8px 0;padding:9px 12px;background:#315174;color:white;border:0;border-radius:9px;cursor:pointer}
</style><script>(function(){
  function hour(){return Number(new Intl.DateTimeFormat('en-GB',{timeZone:'Asia/Kuwait',hour:'2-digit',hourCycle:'h23'}).format(new Date()))}
  function apply(){let pref;try{pref=localStorage.getItem('alkuwaiti-theme')}catch(e){};let theme=pref==='light'||pref==='dark'?pref:(hour()>=6&&hour()<18?'light':'dark');document.documentElement.dataset.theme=theme;let b=document.getElementById('theme-switch');if(b)b.textContent='ðŸŽ¨ Ø§Ù„Ù…Ø¸Ù‡Ø±: '+(theme==='light'?'Ù†Ù‡Ø§Ø±ÙŠ':'Ù„ÙŠÙ„ÙŠ')+' (Ø§Ø¶ØºØ· Ù„Ù„ØªØºÙŠÙŠØ±)'}
  window.toggleAlkuwaitiTheme=function(){let theme=document.documentElement.dataset.theme==='light'?'dark':'light';try{localStorage.setItem('alkuwaiti-theme',theme)}catch(e){};apply()};
  apply();document.addEventListener('DOMContentLoaded',apply);setInterval(apply,60000);
})();</script><main><button id="theme-switch" type="button" class="theme-switch" onclick="toggleAlkuwaitiTheme()">ðŸŽ¨ Ø§Ù„Ù…Ø¸Ù‡Ø±</button><h1>ðŸ‡°ðŸ‡¼ Ø§Ù„Ø¨ÙˆØª Ø§Ù„ÙƒÙˆÙŠØªÙŠ V38</h1><p class="warn">{{'âš ï¸ Ø§Ù„ØªØ¯Ø§ÙˆÙ„ Ø§Ù„Ø­Ù‚ÙŠÙ‚ÙŠ Ù…ÙØ¹Ù„' if live else 'ðŸ”’ Ø§Ù„ØªØ¯Ø§ÙˆÙ„ Ø§Ù„Ø­Ù‚ÙŠÙ‚ÙŠ Ù…Ø¹Ø·Ù„ â€” ÙˆØ¶Ø¹ Ø§Ù„ØªØ­Ù„ÙŠÙ„ ÙÙ‚Ø·'}}</p>
{% for m in get_flashed_messages() %}<article>{{m}}</article>{% endfor %}
{% if logged %}<article id="binance-ban-panel" style="border:2px solid #d97706" aria-live="polite"><h2>â³ Ø­Ø§Ù„Ø© Ø§ØªØµØ§Ù„ Binance / Ø­Ø¸Ø± IP</h2><p id="binance-ban-message">Ø¬Ø§Ø±ÙŠ Ø§Ù„ØªØ­Ù‚Ù‚ Ù…Ù† Ø­Ø§Ù„Ø© Binance...</p><p id="binance-ban-timer" style="font-size:1.6em;font-weight:bold">â€”</p><small>Ø§Ù„Ø¹Ø¯Ø§Ø¯ ÙŠØ³ØªÙ†Ø¯ Ø¥Ù„Ù‰ ØªØ§Ø±ÙŠØ® Ø§Ù†ØªÙ‡Ø§Ø¡ Ø§Ù„Ø­Ø¸Ø± Ø§Ù„Ø°ÙŠ ØªØ±Ø³Ù„Ù‡ Binance Ø¹Ù†Ø¯ ØªÙˆÙØ±Ù‡. Ù„Ø§ ÙŠØ¹Ù†ÙŠ Ø§Ù†ØªÙ‡Ø§Ø¡ Ø§Ù„Ø¹Ø¯Ø§Ø¯ Ø£Ù† Ø§Ù„Ø§ØªØµØ§Ù„ Ø¹Ø§Ø¯ Ù…Ø¤ÙƒØ¯Ù‹Ø§. ÙŠØªÙˆÙ‚Ù Ø¥Ø±Ø³Ø§Ù„ Ø·Ù„Ø¨Ø§Øª Binance Ø£Ø«Ù†Ø§Ø¡ Ø§Ù„Ø­Ø¸Ø±.</small></article>{% endif %}
{% if not logged %}<article><h2>Ø¯Ø®ÙˆÙ„ Ø§Ù„Ù…Ø¯ÙŠØ±</h2><form method="post" action="{{url_for('login')}}"><input type="hidden" name="csrf" value="{{csrf}}"><input type="email" name="email" placeholder="Ø§Ù„Ø¨Ø±ÙŠØ¯" required><input type="password" name="password" placeholder="ÙƒÙ„Ù…Ø© Ø§Ù„Ù…Ø±ÙˆØ±" required><button>Ø¯Ø®ÙˆÙ„</button></form></article>
{% else %}<p><a href="{{url_for('logout')}}" class="logout-link">ðŸšª ØªØ³Ø¬ÙŠÙ„ Ø®Ø±ÙˆØ¬ Ø§Ù„Ù…Ø³Ø¤ÙˆÙ„</a> | <a href="/investors">ÙˆØ§Ø¬Ù‡Ø© Ø§Ù„Ù…Ø³ØªØ«Ù…Ø±ÙŠÙ†</a></p>
<article style="border:2px solid #d97706">
<h2>ðŸ›‘ Ù…ÙØªØ§Ø­ Ø¥ÙŠÙ‚Ø§Ù Ø§Ù„Ø¨ÙˆØª</h2>
{% if prefs and prefs.is_paused %}
<p style="color:#fca5a5;font-weight:bold">ðŸ”´ Ø§Ù„Ø¨ÙˆØª Ù…ØªÙˆÙ‚Ù: Ù„Ø§ ÙØ­Øµ ØªÙ„Ù‚Ø§Ø¦ÙŠ ÙˆÙ„Ø§ ÙØ±Øµ Ø£Ùˆ ØµÙÙ‚Ø§Øª Ø¯Ø®ÙˆÙ„ Ø¬Ø¯ÙŠØ¯Ø©.</p>
{% else %}
<p style="color:#86efac;font-weight:bold">ðŸŸ¢ Ø§Ù„Ø¨ÙˆØª ÙŠØ¹Ù…Ù„: ÙŠÙ…ÙƒÙ†Ùƒ Ø¥ÙŠÙ‚Ø§ÙÙ‡ ÙÙŠ Ø£ÙŠ ÙˆÙ‚Øª.</p>
{% endif %}
<form method="post" action="{{url_for('toggle_bot')}}">
<input type="hidden" name="csrf" value="{{csrf}}">
{% if prefs and prefs.is_paused %}
<input type="hidden" name="action" value="resume"><button type="submit" style="background:#15803d" onclick="return confirm('Ù‡Ù„ ØªØ±ÙŠØ¯ Ø§Ø³ØªØ¦Ù†Ø§Ù Ø§Ù„ÙØ­ØµØŸ Ù„Ù† ÙŠØªÙ… ØªÙØ¹ÙŠÙ„ Ø§Ù„ØªØ¯Ø§ÙˆÙ„ Ø§Ù„Ø­Ù‚ÙŠÙ‚ÙŠ ØªÙ„Ù‚Ø§Ø¦ÙŠØ§Ù‹.')">â–¶ï¸ Ø¥Ø¹Ø§Ø¯Ø© ØªØ´ØºÙŠÙ„ Ø§Ù„ÙØ­Øµ</button>
{% else %}
<input type="hidden" name="action" value="stop"><button type="submit" class="danger" onclick="return confirm('Ø¥ÙŠÙ‚Ø§Ù Ø§Ù„ÙØ­Øµ ÙˆØ§Ù„Ø¯Ø®ÙˆÙ„ ÙÙŠ ØµÙÙ‚Ø§Øª Ø¬Ø¯ÙŠØ¯Ø©ØŸ Ø§Ù„Ù…Ø±Ø§ÙƒØ² Ø§Ù„Ø­Ø§Ù„ÙŠØ© Ù„Ù† ØªÙØºÙ„Ù‚ ØªÙ„Ù‚Ø§Ø¦ÙŠØ§Ù‹.')">ðŸ›‘ Ø¥ÙŠÙ‚Ø§Ù Ø§Ù„Ø¨ÙˆØª Ø¨Ø§Ù„ÙƒØ§Ù…Ù„</button>
{% endif %}
</form>
<small>Ø§Ù„Ø¥ÙŠÙ‚Ø§Ù Ù…Ø­ÙÙˆØ¸ ÙÙŠ Neon ÙˆÙŠØ³ØªÙ…Ø± Ø¨Ø¹Ø¯ Ø¥Ø¹Ø§Ø¯Ø© ØªØ´ØºÙŠÙ„ Render. Ø§Ù„Ù…Ø±Ø§ÙƒØ² ÙˆØ§Ù„Ø£ÙˆØ§Ù…Ø± Ø§Ù„Ù…ÙØªÙˆØ­Ø© ÙÙŠ Binance Ù„Ø§ ØªÙØºÙ„Ù‚ ØªÙ„Ù‚Ø§Ø¦ÙŠÙ‹Ø§. ØªØ¸Ù„ Ù„ÙˆØ­Ø© Ø§Ù„Ø¥Ø¯Ø§Ø±Ø© Ù…ØªØ§Ø­Ø©.</small>
</article>

<article><h2>ðŸ›¡ï¸ Ø¥Ø¯Ø§Ø±Ø© Ø§Ù„Ø¨ÙˆØª Ø§Ù„ÙƒÙˆÙŠØªÙŠ</h2><p><small>Ø±ÙˆØ§Ø¨Ø· Ø§Ù„Ø¥Ø¯Ø§Ø±Ø© Ø§Ù„Ø®Ø§ØµØ© Ø¨Ùƒ â€” Ù„Ø§ ØªØ¸Ù‡Ø± Ù„Ù„Ù…Ø³ØªØ«Ù…Ø±ÙŠÙ†.</small></p>
<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px">
<a href="{{url_for('owner_investors')}}" style="display:block;background:#315174;color:white;text-align:center;padding:14px;border-radius:10px;text-decoration:none;font-weight:bold">ðŸ‘¥ Ø¥Ø¯Ø§Ø±Ø© Ø§Ù„Ù…Ø³ØªØ«Ù…Ø±ÙŠÙ† ÙˆØ§Ù„Ø§Ø´ØªØ±Ø§ÙƒØ§Øª</a>
<a href="{{url_for('owner_requests_page')}}" style="display:block;background:#315174;color:white;text-align:center;padding:14px;border-radius:10px;text-decoration:none;font-weight:bold">âœ… Ø§Ù„Ø·Ù„Ø¨Ø§Øª ÙˆØ§Ù„Ù…ÙˆØ§ÙÙ‚Ø§Øª</a>
<a href="{{url_for('owner_payouts')}}" style="display:block;background:#315174;color:white;text-align:center;padding:14px;border-radius:10px;text-decoration:none;font-weight:bold">ðŸ’° Ù…Ø³ØªØ­Ù‚Ø§ØªÙŠ ÙˆØ·Ø±Ù‚ Ø§Ù„Ø§Ø³ØªÙ„Ø§Ù…</a>
</div></article><article><h2>ðŸ’° Ø±ØµÙŠØ¯ Binance Ø§Ù„Ù…Ø¤ÙƒØ¯</h2><p id="v37-balance">{{balance}}</p><small id="v37-account-time">Ø¢Ø®Ø± ØªØ­Ø¯ÙŠØ« Ù…Ø¤ÙƒØ¯: {{account_snapshot_at or 'Ù„Ù… ÙŠØªÙ… Ø§Ù„ØªØ­Ø¯ÙŠØ« Ø¨Ø¹Ø¯'}} â€” Ù„Ø§ Ù†Ø¹Ø±Ø¶ Ø¨ÙŠØ§Ù†Ø§Øª ØºÙŠØ± Ù…Ø¤ÙƒØ¯Ø© ÙƒØ±ØµÙŠØ¯ Ù„Ø­Ø¸ÙŠ.</small><p><small id="v37-account-status">Ø¬Ø§Ø±Ù ÙØ­Øµ Ø§Ù„Ø§ØªØµØ§Ù„ Ø§Ù„Ø¢Ù…Ù†...</small></p><form method="post" action="/api/v34-snapshot-refresh"><input type="hidden" name="csrf" value="{{csrf}}"><button type="submit">ØªØ­Ø¯ÙŠØ« Ø§Ù„Ø±ØµÙŠØ¯ ÙˆØ§Ù„Ù…Ø±Ø§ÙƒØ² ÙŠØ¯ÙˆÙŠÙ‹Ø§ (ÙƒÙ„ 10 Ø¯Ù‚Ø§Ø¦Ù‚)</button></form><small>Ù„Ø§ ØªØ¶ØºØ· Ø£Ø«Ù†Ø§Ø¡ Ø­Ø¸Ø± Binance. ÙØªØ­ Ø§Ù„ØµÙØ­Ø© Ù„Ø§ ÙŠØ±Ø³Ù„ Ø·Ù„Ø¨Ø§Øª Binance.</small></article>
<article><h2>ðŸ”‘ ØªØºÙŠÙŠØ± Ù…ÙØ§ØªÙŠØ­ Binance API</h2><p><small>ÙŠÙ…ÙƒÙ†Ùƒ Ø§Ø³ØªØ¨Ø¯Ø§Ù„ Ø§Ù„Ù…ÙØ§ØªÙŠØ­ Ù…ØªÙ‰ Ø´Ø¦Øª. Ù„Ø§ ØªÙØ¹Ø±Ø¶ Ø§Ù„Ù…ÙØ§ØªÙŠØ­ Ø§Ù„Ù‚Ø¯ÙŠÙ…Ø©ØŒ ÙˆØªÙØ­ÙØ¸ Ø§Ù„Ø¬Ø¯ÙŠØ¯Ø© Ù…Ø´ÙØ±Ø© ÙÙŠ Neon. ÙŠÙ„Ø²Ù… CREDENTIALS_ENCRYPTION_KEY ÙÙŠ Render.</small></p><form method="post" action="{{url_for('update_api_credentials')}}"><input type="hidden" name="csrf" value="{{csrf}}"><input name="api_key" type="password" autocomplete="off" placeholder="API Key Ø§Ù„Ø¬Ø¯ÙŠØ¯" required style="width:90%"><input name="api_secret" type="password" autocomplete="off" placeholder="API Secret Ø§Ù„Ø¬Ø¯ÙŠØ¯" required style="width:90%"><button>ÙØ­Øµ Ø§Ù„Ø§ØªØµØ§Ù„ ÙˆØ­ÙØ¸ Ø§Ù„Ù…ÙØ§ØªÙŠØ­ Ø§Ù„Ø¬Ø¯ÙŠØ¯Ø©</button></form><small>Ø£Ù†Ø´Ø¦ Ø§Ù„Ù…ÙØ§ØªÙŠØ­ Ø¨ØµÙ„Ø§Ø­ÙŠØ© Futures ÙÙ‚Ø·ØŒ ÙˆØ¹Ø·Ù‘Ù„ Ø§Ù„Ø³Ø­Ø¨ Withdrawal. ØªØºÙŠÙŠØ± Ø§Ù„Ù…ÙØ§ØªÙŠØ­ Ù„Ø§ ÙŠÙ„ØºÙŠ Ø§Ù„ØµÙÙ‚Ø§Øª Ø§Ù„Ù…ÙØªÙˆØ­Ø©.</small></article>
<article><h2>âš™ï¸ Ø¥Ø¹Ø¯Ø§Ø¯Ø§Øª Ø±Ø£Ø³ Ø§Ù„Ù…Ø§Ù„ ÙˆØ§Ù„Ø±Ø§ÙØ¹Ø©</h2>
<p><small>Ø§Ù„Ù‚ÙŠÙ… Ø§Ù„ØªØ§Ù„ÙŠØ© Ø­Ø¯ÙˆØ¯ Ù‚ØµÙˆÙ‰ ÙˆÙ„ÙŠØ³Øª ØªØ¹Ù„ÙŠÙ…Ø§Øª Ù„Ø§Ø³ØªØ«Ù…Ø§Ø± Ø§Ù„Ø±ØµÙŠØ¯ ÙƒØ§Ù…Ù„Ù‹Ø§. Ù„Ø§ ÙŠÙ…ÙƒÙ† Ø¥Ø¶Ø§ÙØ© USDT Ø­Ù‚ÙŠÙ‚ÙŠ Ù…Ù† Ù„ÙˆØ­Ø© Ø§Ù„Ø¨ÙˆØªØ› Ø§Ù„Ø¥ÙŠØ¯Ø§Ø¹ ÙˆØ§Ù„ØªØ­ÙˆÙŠÙ„ ÙŠØªÙ…Ø§Ù† Ù…Ù† Binance ÙÙ‚Ø·.</small></p>
<form method="post" action="{{url_for('save_preferences')}}">
<input type="hidden" name="csrf" value="{{csrf}}">
<label>Ø§Ù„Ø­Ø¯ Ø§Ù„Ø£Ù‚ØµÙ‰ Ù„Ù„ØªØ¹Ø±Ø¶ Ø§Ù„Ø§Ø³Ù…ÙŠ Ù…Ù† Ø§Ù„Ù…Ø­ÙØ¸Ø© (%)</label><input type="number" name="max_exposure_pct" min="1" max="100" step="1" value="{{prefs.max_exposure_pct}}" required>
<label>Ø§Ù„Ø­Ø¯ Ø§Ù„Ø£Ø¹Ù„Ù‰ Ù„Ù„Ø±Ø§ÙØ¹Ø© (1 Ø¥Ù„Ù‰ 150ØŒ Ø­Ø³Ø¨ Ø­Ø¯ÙˆØ¯ Binance)</label><input type="number" name="max_leverage" min="1" max="150" step="1" value="{{prefs.max_leverage}}" required>
<label>Ù‡Ø¯Ù ØµØ§ÙÙŠ Ø§Ù„Ø¹Ø§Ø¦Ø¯ Ø¹Ù„Ù‰ Ù‡Ø§Ù…Ø´ Ø§Ù„ØµÙÙ‚Ø© (%)</label><input type="number" name="target_roi_pct" min="1" max="100" step="0.5" value="{{prefs.target_roi_pct}}" required>
<button>Ø­ÙØ¸ Ø¥Ø¹Ø¯Ø§Ø¯Ø§Øª Ø§Ù„ØªØ­Ù„ÙŠÙ„</button></form>
<p class="warn">ØªØ­Ø°ÙŠØ±: Ø§Ù„Ø±Ø§ÙØ¹Ø© 150x Ù‚Ø¯ ØªØ¤Ø¯ÙŠ Ø¥Ù„Ù‰ ØªØµÙÙŠØ© Ø³Ø±ÙŠØ¹Ø© ÙˆØ®Ø³Ø§Ø±Ø© Ø§Ù„Ù‡Ø§Ù…Ø´. Ø§Ù„Ø¨ÙˆØª Ù„Ø§ ÙŠØ³ØªØ·ÙŠØ¹ Ø²ÙŠØ§Ø¯Ø© Ø±ØµÙŠØ¯ Binance Ø£Ùˆ Ø¶Ù…Ø§Ù† 10% Ø±Ø¨Ø­.</p>
</article>
<article style="border:2px solid #1d9b83"><h2>âš¡ Ø£Ø³Ø¹Ø§Ø± Binance Futures Ø§Ù„Ù…Ø¨Ø§Ø´Ø±Ø© â€” WebSocket</h2>
<p id="v36-ws-status" aria-live="polite">Ø¬Ø§Ø±ÙŠ Ø§Ù„Ø§ØªØµØ§Ù„ Ø¨Ø§Ù„Ø¨Ø« Ø§Ù„Ù…Ø¨Ø§Ø´Ø±...</p>
<small>Ø£Ø³Ø¹Ø§Ø± Mark Price Ù„Ø¹Ù‚ÙˆØ¯ USDT-M Ø§Ù„Ù…ØªØ§Ø­Ø©Ø› Ù„ÙŠØ³Øª Ø³Ø¹Ø± ØªÙ†ÙÙŠØ° Ù…Ø¶Ù…ÙˆÙ†Ù‹Ø§. ÙŠØªÙ… ØªØ­Ø¯ÙŠØ«Ù‡Ø§ Ù…Ù† Binance WebSocket Ø¯ÙˆÙ† Ø·Ù„Ø¨ REST ÙƒÙ„ Ø«Ø§Ù†ÙŠØ©. Ø¹Ù†Ø¯ Ø§Ù†Ù‚Ø·Ø§Ø¹ Ø§Ù„Ø¨Ø« ØªØ¸Ù‡Ø± Ø­Ø§Ù„Ø© Ø§Ù„Ø§Ù†Ù‚Ø·Ø§Ø¹ ÙˆÙ„Ø§ ØªÙØ¹Ø±Ø¶ Ø§Ù„Ø£Ø³Ø¹Ø§Ø± Ø§Ù„Ù‚Ø¯ÙŠÙ…Ø© Ø¹Ù„Ù‰ Ø£Ù†Ù‡Ø§ Ù…Ø¨Ø§Ø´Ø±Ø©.</small>
<table><thead><tr><th>Ø§Ù„Ø±Ù…Ø²</th><th>Mark Price (USDT)</th><th>ÙˆÙ‚Øª Ø§Ù„ØªØ­Ø¯ÙŠØ«</th></tr></thead><tbody id="v36-live-market"><tr><td colspan="3">Ø¨Ø§Ù†ØªØ¸Ø§Ø± Ø§Ù„Ø¨Ø«...</td></tr></tbody></table></article>
<article><h2>ðŸ’¹ Ø§Ù„Ù…Ø±Ø§ÙƒØ²: Ø±Ø¨Ø­ ÙˆØ®Ø³Ø§Ø±Ø© Ù…ØªØºÙŠØ±Ø© ØªÙ‚Ø¯ÙŠØ±ÙŠÙ‹Ø§</h2>
<p id="v36-position-status">Ø¨Ø§Ù†ØªØ¸Ø§Ø± Ø§Ù„Ø¨Ø« Ø§Ù„Ù…Ø¨Ø§Ø´Ø±...</p>
<table><thead><tr><th>Ø§Ù„Ø±Ù…Ø²</th><th>Ø§Ù„ÙƒÙ…ÙŠØ© Ø§Ù„Ù…Ø­ÙÙˆØ¸Ø©</th><th>Ø³Ø¹Ø± Ø§Ù„Ø¯Ø®ÙˆÙ„ Ø§Ù„Ù…Ø­ÙÙˆØ¸</th><th>Mark Price</th><th>Ø±Ø¨Ø­/Ø®Ø³Ø§Ø±Ø© ØªÙ‚Ø¯ÙŠØ±ÙŠ</th></tr></thead><tbody id="v36-live-positions"></tbody></table>
<p id="v36-equity">Ø±ØµÙŠØ¯ Ø§Ù„Ù…Ø­ÙØ¸Ø© Ø§Ù„Ù…Ø¤ÙƒØ¯ ÙŠØ¨Ù‚Ù‰ Ø¢Ø®Ø± Ù„Ù‚Ø·Ø© Ù…Ø­ÙÙˆØ¸Ø©Ø› Ø§Ù„Ù‚ÙŠÙ…Ø© Ø§Ù„Ø¥Ø¬Ù…Ø§Ù„ÙŠØ© Ø§Ù„Ù…ØªØºÙŠØ±Ø© Ù‡Ù†Ø§ ØªÙ‚Ø¯ÙŠØ±ÙŠØ© ÙˆÙ„ÙŠØ³Øª Ø±ØµÙŠØ¯ Binance Ø§Ù„Ù…Ø¤ÙƒØ¯.</p>
<small>Ø­Ø¬Ù… Ø§Ù„Ù…Ø±ÙƒØ² ÙˆØ³Ø¹Ø± Ø¯Ø®ÙˆÙ„Ù‡ Ù…Ø£Ø®ÙˆØ°Ø§Ù† Ù…Ù† Ø¢Ø®Ø± Ù„Ù‚Ø·Ø© Ù…Ø­ÙÙˆØ¸Ø©ØŒ ÙˆÙ‚Ø¯ ÙŠØªØºÙŠØ±Ø§Ù† ÙÙŠ Binance Ø¯ÙˆÙ† Ø¹Ù„Ù… Ø§Ù„Ø´Ø§Ø´Ø©. Ø§Ù„ØªÙ‚Ø¯ÙŠØ± Ù„Ø§ ÙŠØ´Ù…Ù„ Ø§Ù„Ø±Ø³ÙˆÙ… Ø£Ùˆ Ø§Ù„ØªÙ…ÙˆÙŠÙ„ØŒ ÙˆÙ„Ø§ ÙŠÙØ³ØªØ®Ø¯Ù… Ù„ØªÙ†ÙÙŠØ° Ø§Ù„Ø£ÙˆØ§Ù…Ø±. Ù„ØªØ­Ø¯ÙŠØ« Ø§Ù„Ù…Ø±Ø§ÙƒØ² Ø§Ù„Ø­Ù‚ÙŠÙ‚ÙŠØ© Ø§Ø³ØªØ®Ø¯Ù… Ø²Ø± ØªØ­Ø¯ÙŠØ« Ø§Ù„Ø±ØµÙŠØ¯ Ø§Ù„Ù…Ø­Ø¯ÙˆØ¯ Ø£Ø¹Ù„Ø§Ù‡.</small></article>
<article><h2>ðŸ“ˆ Ø§Ù„Ø£Ø³Ø¹Ø§Ø± Ø¯Ø§Ø®Ù„ Ø§Ù„Ø¨ÙˆØª</h2><p><small>Ø¨ÙŠØ§Ù†Ø§Øª Yahoo Finance Ø§Ù„Ø¹Ø§Ù…Ø© ØºÙŠØ± Ø§Ù„Ø±Ø³Ù…ÙŠØ©ØŒ Ù‚Ø¯ ØªÙƒÙˆÙ† Ù…ØªØ£Ø®Ø±Ø© Ø£Ùˆ ØºÙŠØ± Ù…ØªØ§Ø­Ø©Ø› ØªØ®ØªÙ„Ù Ø¹Ù† Ø¹Ù‚ÙˆØ¯ Binance. ÙŠØªÙ… ØªØ­Ø¯ÙŠØ« Ø§Ù„Ø´Ø§Ø´Ø© ÙƒÙ„ Ø«Ø§Ù†ÙŠØ©ØŒ ÙˆØ¬Ù„Ø¨ Ø§Ù„Ù…ØµØ¯Ø± ÙƒÙ„ 30 Ø«Ø§Ù†ÙŠØ© ØªÙ‚Ø±ÙŠØ¨Ù‹Ø§.</small></p>
<table><thead><tr><th>Ø§Ù„Ø³ÙˆÙ‚</th><th>Ø§Ù„Ø³Ø¹Ø± Ø¨Ø§Ù„Ø¯ÙˆÙ„Ø§Ø± ØªÙ‚Ø±ÙŠØ¨Ù‹Ø§</th><th>ÙˆÙ‚Øª Ø§Ù„Ø¨ÙŠØ§Ù†Ø§Øª</th></tr></thead><tbody id="external-prices"><tr><td colspan="3">Ø¬Ø§Ø±ÙŠ Ø¬Ù„Ø¨ Ø§Ù„Ø£Ø³Ø¹Ø§Ø±...</td></tr></tbody></table><small id="price-state">Ù…ØµØ¯Ø± Ø®Ø§Ø±Ø¬ÙŠØŒ ÙˆÙ„ÙŠØ³ Ø³Ø¹Ø± ØªÙ†ÙÙŠØ° Binance.</small></article>
<article><h2>ðŸ“Š Ø£Ø±Ø¨Ø§Ø­ ÙˆØ®Ø³Ø§Ø¦Ø± Ø§Ù„ÙŠÙˆÙ…</h2><p><small>Ø§Ù„ÙŠÙˆÙ… Ø¨ØªÙˆÙ‚ÙŠØª Ø§Ù„ÙƒÙˆÙŠØªØŒ Ù…Ù† Ø³Ø¬Ù„ Ø¯Ø®Ù„ Binance Futures: Ø§Ù„Ø±Ø¨Ø­ ÙˆØ§Ù„Ø®Ø³Ø§Ø±Ø© Ø§Ù„Ù…Ø­Ù‚Ù‚Ø©ØŒ Ø§Ù„Ø¹Ù…ÙˆÙ„Ø§ØªØŒ ÙˆØ§Ù„ØªÙ…ÙˆÙŠÙ„. Ù„Ø§ ÙŠØ´Ù…Ù„ Ø£Ø±Ø¨Ø§Ø­ Ø§Ù„Ù…Ø±Ø§ÙƒØ² ØºÙŠØ± Ø§Ù„Ù…ØºÙ„Ù‚Ø©.</small></p><div id="daily-pnl">Ø¬Ø§Ø±ÙŠ Ø¹Ø±Ø¶ Ø¢Ø®Ø± Ù†ØªÙŠØ¬Ø© Ù…Ø­ÙÙˆØ¸Ø©...</div><form method="post" action="/api/v34-pnl-refresh"><input type="hidden" name="csrf" value="{{csrf}}"><button type="submit">ØªØ­Ø¯ÙŠØ« Ø£Ø±Ø¨Ø§Ø­ ÙˆØ®Ø³Ø§Ø¦Ø± Ø§Ù„ÙŠÙˆÙ… (ÙƒÙ„ 15 Ø¯Ù‚ÙŠÙ‚Ø©)</button></form><small>Ø§Ù„Ù†ØªØ§Ø¦Ø¬ Ø§Ù„Ù…Ø­ÙÙˆØ¸Ø© Ù‚Ø¯ÙŠÙ…Ø© Ø­ØªÙ‰ ØªØ¤ÙƒØ¯ Binance ØªØ­Ø¯ÙŠØ«Ù‡Ø§Ø› Ù„Ø§ ØªØ´Ù…Ù„ Ø£Ø±Ø¨Ø§Ø­ Ø§Ù„Ù…Ø±Ø§ÙƒØ² Ø§Ù„Ù…ÙØªÙˆØ­Ø©.</small></article>
<article><h2>Ø§Ù„Ø£Ø³ÙˆØ§Ù‚ Ø§Ù„Ù…ØªØ§Ø¨Ø¹Ø©</h2><p>Ø¹Ù…Ù„Ø§Øª Ø±Ù‚Ù…ÙŠØ©: BTCUSDTØŒ ETHUSDTØŒ BNBUSDTØŒ SOLUSDTØŒ XRPUSDT</p><p>Ø°Ù‡Ø¨: XAUUSDT | ÙØ¶Ø©: XAGUSDT</p><p>Ù†ÙØ· WTI: CLUSDT | Ù†ÙØ· Ø¨Ø±Ù†Øª: BZUSDT</p><small>ÙŠØªÙ… Ø§Ø³ØªØ¨Ø¹Ø§Ø¯ Ø£ÙŠ Ø±Ù…Ø² ØºÙŠØ± Ù…ØªØ§Ø­ ÙÙŠ Binance Futures Ù„Ø­Ø³Ø§Ø¨Ùƒ.</small></article>
<article><h2>ðŸ”Ž Ø§Ù„ÙØ­Øµ Ø§Ù„ØªÙ„Ù‚Ø§Ø¦ÙŠ Ù„ÙØ±Øµ Ø§Ù„ØªØ¯Ø§ÙˆÙ„</h2><p class=warn>ðŸ›¡ï¸ V34: ØªØ³ØªÙ…Ø± Ø£Ø³Ø¹Ø§Ø± Yahoo Ø§Ù„Ø®Ø§Ø±Ø¬ÙŠØ© Ø­ØªÙ‰ Ø¹Ù†Ø¯ Ø­Ø¸Ø± Binance. ØªØ­Ù„ÙŠÙ„ Ø´Ù…ÙˆØ¹ Binance Ù…ØªÙˆÙ‚Ù Ø§ÙØªØ±Ø§Ø¶ÙŠÙ‹Ø§ Ù„Ù…Ù†Ø¹ ØªÙƒØ±Ø§Ø± Ø­Ø¸Ø± IPØ› ÙŠÙ…ÙƒÙ† ØªÙØ¹ÙŠÙ„Ù‡ Ù„Ø§Ø­Ù‚Ù‹Ø§ Ø¹Ø¨Ø± BINANCE_MARKET_SCANS_ENABLED=true ÙÙŠ Render Ø¨Ø¹Ø¯ Ø§Ù„ØªØ£ÙƒØ¯ Ù…Ù† Ù…Ø¹Ø§Ù„Ø¬Ø© Ø­Ø¯ÙˆØ¯ Ø§Ù„Ø·Ù„Ø¨Ø§Øª. Ø£Ø³Ø¹Ø§Ø± Yahoo Ù„ÙŠØ³Øª Ø£Ø³Ø¹Ø§Ø± ØªÙ†ÙÙŠØ° ÙˆÙ„Ø§ ØªÙˆÙ„Ù‘Ø¯ ØªÙˆØµÙŠØ§Øª ØªØ¯Ø§ÙˆÙ„.</p><p id="auto-scan-status">â³ ÙŠØ¨Ø¯Ø£ Ø§Ù„ØªØ­Ù„ÙŠÙ„ ØªÙ„Ù‚Ø§Ø¦ÙŠÙ‹Ø§ Ø¨Ø¹Ø¯ ÙØªØ­ Ø§Ù„ØµÙØ­Ø©...</p><small>Ù„Ø§ ØªØ­ØªØ§Ø¬ Ø¥Ù„Ù‰ Ø§Ù„Ø¶ØºØ· Ø¹Ù„Ù‰ Ø£ÙŠ Ø²Ø±. ØªÙØ­Ø¯Ø« Ø§Ù„Ø´Ø§Ø´Ø© ÙƒÙ„ Ø«Ø§Ù†ÙŠØ© ÙˆÙŠÙØ¬Ø±Ù‰ ÙØ­Øµ ÙÙ†ÙŠ Ø¹Ù†Ø¯ ØªÙØ¹ÙŠÙ„Ù‡ ÙƒÙ„ 90 Ø«Ø§Ù†ÙŠØ© ÙƒØ­Ø¯ Ø£Ø¯Ù†Ù‰ Ø£Ø«Ù†Ø§Ø¡ Ø¨Ù‚Ø§Ø¡ Ø§Ù„ØµÙØ­Ø© Ù…ÙØªÙˆØ­Ø© ÙˆÙ†Ø´Ø·Ø©. Ù‚Ø¯ Ù„Ø§ ØªÙˆØ¬Ø¯ ÙØ±Øµ ØªØ³ØªÙˆÙÙŠ Ø§Ù„Ø´Ø±ÙˆØ·. Ù„Ø§ ØªÙØ±Ø³Ù„ Ø£ÙˆØ§Ù…Ø± Ø´Ø±Ø§Ø¡ Ø£Ùˆ Ø¨ÙŠØ¹ Ø¥Ù„Ø§ Ø¨Ø¹Ø¯ Ù…ÙˆØ§ÙÙ‚ØªÙƒØŒ ÙˆÙ…Ø¹ ØªÙØ¹ÙŠÙ„ Ø§Ù„ØªØ¯Ø§ÙˆÙ„ Ø§Ù„Ø­Ù‚ÙŠÙ‚ÙŠ Ø¨Ø´ÙƒÙ„ Ù…Ù†ÙØµÙ„.</small></article>
<article id="entry-suggestions"><h2>ÙØ±Øµ Ø¨Ø§Ù†ØªØ¸Ø§Ø± Ø§Ù„Ù…ÙˆØ§ÙÙ‚Ø©</h2><table><tr><th>Ø§Ù„Ø±Ù…Ø²</th><th>Ø§Ù„Ø§ØªØ¬Ø§Ù‡</th><th>Ø§Ù„Ø¯Ø®ÙˆÙ„ Ø§Ù„ØªÙ‚Ø±ÙŠØ¨ÙŠ</th><th>Ø§Ù„Ù‡Ø¯Ù</th><th>ÙˆÙ‚Ù Ø§Ù„Ø®Ø³Ø§Ø±Ø©</th><th>ØµØ§ÙÙŠ Ø§Ù„Ù‡Ø¯Ù Ø§Ù„ØªÙ‚Ø¯ÙŠØ±ÙŠ</th><th>Ø§Ù„Ø£Ù…Ø±</th></tr>{% for x in pending %}<tr><td>{{x.symbol}}</td><td>{{x.direction}}</td><td>{{x.entry_reference}}</td><td>{{x.take_reference}}</td><td>{{x.stop_reference}}</td><td>â‰¥ {{prefs.target_roi_pct}}% Ø¹Ù„Ù‰ Ø§Ù„Ù‡Ø§Ù…Ø´ (ØªÙ‚Ø¯ÙŠØ±ÙŠ)</td><td><form method="post" action="{{url_for('approve',order_id=x.id)}}"><input type="hidden" name="csrf" value="{{csrf}}"><button>Ù…ÙˆØ§ÙÙ‚Ø© ÙˆÙØªØ­ ÙÙˆØ±Ù‹Ø§</button></form><form method="post" action="{{url_for('reject',order_id=x.id)}}"><input type="hidden" name="csrf" value="{{csrf}}"><button class="danger">Ø±ÙØ¶</button></form></td></tr>{% else %}<tr><td colspan="7">Ù„Ø§ ØªÙˆØ¬Ø¯ ÙØ±Øµ</td></tr>{% endfor %}</table></article>
<article><h2>Ø¢Ø®Ø± Ù…Ø±Ø§ÙƒØ² Binance Ø§Ù„Ù…Ø­ÙÙˆØ¸Ø© (Ù‚Ø¯ ØªÙƒÙˆÙ† ØªØºÙŠÙ‘Ø±Øª)</h2><table><tr><th>Ø§Ù„Ø±Ù…Ø²</th><th>Ø§Ù„ÙƒÙ…ÙŠØ©</th><th>Ø§Ù„Ø¯Ø®ÙˆÙ„</th><th>Ø±Ø¨Ø­/Ø®Ø³Ø§Ø±Ø© ØºÙŠØ± Ù…Ø­Ù‚Ù‚Ø©</th><th>Ø¥ØºÙ„Ø§Ù‚</th></tr>{% for p in positions %}<tr><td>{{p.symbol}}</td><td>{{p.positionAmt}}</td><td>{{p.entryPrice}}</td><td>{{p.unRealizedProfit}} USDT</td><td><form method="post" action="{{url_for('close_position',symbol=p.symbol)}}"><input type="hidden" name="csrf" value="{{csrf}}"><button class="danger">Ø¥ØºÙ„Ø§Ù‚ Ø§Ù„Ø¢Ù†</button></form></td></tr>{% else %}<tr><td colspan="5">Ù„Ø§ ØªÙˆØ¬Ø¯ Ù…Ø±Ø§ÙƒØ² ÙÙŠ Ø¢Ø®Ø± Ù„Ù‚Ø·Ø© Ù…Ø­ÙÙˆØ¸Ø© Ø£Ùˆ Ù„Ù… ØªÙØ­ÙØ¸ Ø¨ÙŠØ§Ù†Ø§Øª Ø¨Ø¹Ø¯</td></tr>{% endfor %}</table><small>Ø§Ù„Ø¥ØºÙ„Ø§Ù‚ MarketØ› Ø§Ù„Ø³Ø¹Ø± Ø§Ù„Ù†Ù‡Ø§Ø¦ÙŠ ÙˆØ§Ù„Ø±Ø³ÙˆÙ… Ù‚Ø¯ ÙŠØ®ØªÙ„ÙØ§Ù†. Ø£ÙˆØ§Ù…Ø± Ø§Ù„Ø­Ù…Ø§ÙŠØ© Ù‚Ø¯ ØªØºÙ„Ù‚ Ø§Ù„Ù…Ø±ÙƒØ² ØªÙ„Ù‚Ø§Ø¦ÙŠÙ‹Ø§.</small></article>
<article><h2>ðŸ•’ Ù…Ø±Ø§Ù‚Ø¨Ø© ØªÙ„Ù‚Ø§Ø¦ÙŠØ© Ø£Ø«Ù†Ø§Ø¡ ÙØªØ­ Ø§Ù„ØµÙØ­Ø©</h2>
<p>Ø¢Ø®Ø± ÙØ­Øµ: {{scan_state.checked_at if scan_state and scan_state.checked_at else 'Ù„Ù… ÙŠØ¨Ø¯Ø£ Ø§Ù„ØªØ­Ù„ÙŠÙ„ Ø¨Ø¹Ø¯'}}</p>
<p>ÙØ±Øµ Ø¬Ø¯ÙŠØ¯Ø© ÙÙŠ Ø¢Ø®Ø± ÙØ­Øµ: {{scan_state.opportunities if scan_state else 0}}</p>
<details><summary>Ø£Ø³Ø¨Ø§Ø¨ Ø§Ø³ØªØ¨Ø¹Ø§Ø¯ Ø§Ù„ÙØ±Øµ</summary><small>{{scan_state.reason if scan_state else 'Ù„Ø§ ØªÙˆØ¬Ø¯ Ø¨ÙŠØ§Ù†Ø§Øª'}}</small></details>
<div id="watch-status" aria-live="polite">Ø¨Ø¯Ø¡ Ø§Ù„Ù…Ø±Ø§Ù‚Ø¨Ø©...</div><small>ÙŠØªØ­Ø¯Ø« ÙˆÙ‚Øª Ø§Ù„Ø´Ø§Ø´Ø© ÙƒÙ„ Ø«Ø§Ù†ÙŠØ©Ø› Ø§Ù„ØªØ­Ù„ÙŠÙ„ Ø§Ù„ÙÙ†ÙŠ Ù„Ø§ ÙŠØ²Ø§Ù„ ÙŠØ¹ØªÙ…Ø¯ Ø¹Ù„Ù‰ Ø¨ÙŠØ§Ù†Ø§Øª Ø´Ù…ÙˆØ¹ Binance ÙƒÙ„ 90 Ø«Ø§Ù†ÙŠØ© ØªÙ‚Ø±ÙŠØ¨Ù‹Ø§ (Google Ù„Ø§ ÙŠÙ‚Ø¯Ù… Ø´Ù…ÙˆØ¹Ù‹Ø§ Ø¢Ù„ÙŠØ© Ù…ÙˆØ«ÙˆÙ‚Ø©)ØŒ ÙˆØªÙØ±Ø§Ø¬Ø¹ Ø§Ù‚ØªØ±Ø§Ø­Ø§Øª Ø§Ù„Ø¥ØºÙ„Ø§Ù‚ ÙƒÙ„ 5 Ø«ÙˆØ§Ù†Ù Ù…Ø§ Ø¯Ø§Ù…Øª Ø§Ù„ØµÙØ­Ø© Ù…ÙØªÙˆØ­Ø©. Ù„Ø§ ØªÙ†ÙØ° Ø§Ù„Ù…Ø±Ø§Ù‚Ø¨Ø© Ø£ÙŠ Ø£ÙˆØ§Ù…Ø±.</small></article>
<article id="exit-suggestions"><h2>ðŸ”” Ø§Ù‚ØªØ±Ø§Ø­Ø§Øª Ø¥ØºÙ„Ø§Ù‚ Ø¨Ø§Ù†ØªØ¸Ø§Ø± Ù‚Ø±Ø§Ø±Ùƒ</h2>
<div id="live-exit-rows">Ø¬Ø§Ø±Ù ÙØ­Øµ Ø§Ù„Ù…Ø±Ø§ÙƒØ²...</div>
<small>Ø§Ù„Ø±Ø¨Ø­ ØªÙ‚Ø¯ÙŠØ±ÙŠØ› Ø§Ù„Ø±Ø³ÙˆÙ… Ø§Ù„ÙØ¹Ù„ÙŠØ© ÙˆØ§Ù„ØªÙ…ÙˆÙŠÙ„ ÙˆØ§Ù„Ø§Ù†Ø²Ù„Ø§Ù‚ Ù‚Ø¯ ØªØºÙŠØ± Ø§Ù„Ù†ØªÙŠØ¬Ø©. ÙŠÙ…ÙƒÙ†Ùƒ Ø¥ØºÙ„Ø§Ù‚ Ø£ÙŠ Ù…Ø±ÙƒØ² ÙŠØ¯ÙˆÙŠÙ‹Ø§ Ù…Ù† Ø¬Ø¯ÙˆÙ„ Ø§Ù„Ù…Ø±Ø§ÙƒØ².</small>
</article>
<article><h2>Ø¢Ø®Ø± Ø§Ù„Ø£ÙˆØ§Ù…Ø±</h2><table><tr><th>Ø§Ù„Ø±Ù…Ø²</th><th>Ø§Ù„Ø§ØªØ¬Ø§Ù‡</th><th>Ø§Ù„Ø­Ø§Ù„Ø©</th><th>Ù…Ù„Ø§Ø­Ø¸Ø©</th></tr>{% for x in recent %}<tr><td>{{x.symbol}}</td><td>{{x.direction}}</td><td>{{x.status}}</td><td>{{x.note or 'â€”'}}</td></tr>{% endfor %}</table></article>{% endif %}</main><script>
(function(){
 const el=document.getElementById('google-clock');
 if(!el)return;
 function tick(){if(!document.hidden)el.textContent='ÙˆÙ‚Øª Ø§Ù„Ø´Ø§Ø´Ø©: '+new Date().toLocaleTimeString('ar-KW');}
 tick();setInterval(tick,1000);
})();

(function(){
 const watch=document.getElementById('watch-status');
 if(!watch)return;
 const csrf={{ csrf|tojson }};
 let watching=false,loading=false;
 function td(tr,value){const cell=document.createElement('td');cell.textContent=String(value);tr.appendChild(cell);return cell;}
 function action(tr,path,label,danger){
   const form=document.createElement('form');form.method='post';form.action=path;
   const token=document.createElement('input');token.type='hidden';token.name='csrf';token.value=csrf;form.appendChild(token);
   const btn=document.createElement('button');btn.textContent=label;if(danger)btn.className='danger';form.appendChild(btn);
   tr.lastChild.appendChild(form);
 }
 async function refresh(){
   if(loading||document.hidden)return;loading=true;
   try{
     const res=await fetch('/api/browser-suggestions',{credentials:'same-origin',cache:'no-store'});
     if(!res.ok)throw Error('suggestions');
     const data=await res.json();
     const entry=document.querySelector('#entry-suggestions table');
     if(entry){const rows=entry.querySelectorAll('tr');rows.forEach((r,i)=>{if(i>0)r.remove()});
       for(const x of data.entries){const tr=document.createElement('tr');td(tr,x.symbol);td(tr,x.direction);td(tr,x.entry_reference);td(tr,x.take_reference);td(tr,x.stop_reference);td(tr,'Ù‡Ø¯Ù ØµØ§ÙÙŠ ØªÙ‚Ø¯ÙŠØ±ÙŠ');td(tr,'');action(tr,'/approve/'+encodeURIComponent(x.id),'Ù…ÙˆØ§ÙÙ‚Ø© ÙˆÙØªØ­ ÙÙˆØ±Ù‹Ø§',false);action(tr,'/reject/'+encodeURIComponent(x.id),'Ø±ÙØ¶',true);entry.appendChild(tr)}
       if(!data.entries.length){const tr=document.createElement('tr');const c=td(tr,'Ù„Ø§ ØªÙˆØ¬Ø¯ ÙØ±Øµ Ù…Ù†Ø§Ø³Ø¨Ø© Ø­Ø§Ù„ÙŠÙ‹Ø§');c.colSpan=7;entry.appendChild(tr)}
     }
     const exit=document.getElementById('exit-suggestions');
     if(exit){let holder=document.getElementById('live-exit-rows');if(!holder){holder=document.createElement('div');holder.id='live-exit-rows';exit.insertBefore(holder,exit.children[1]||null)}holder.replaceChildren();
       for(const x of data.exits){const p=document.createElement('p');p.textContent=x.symbol+' | ØµØ§ÙÙŠ ØªÙ‚Ø¯ÙŠØ±ÙŠ '+(Number(x.estimated_net_roi)*100).toFixed(2)+'% | '+x.estimated_net_usdt+' USDT';holder.appendChild(p);
         const tr=document.createElement('div');td(tr,'');action(tr,'/close-suggestions/'+encodeURIComponent(x.symbol)+'/approve','Ù…ÙˆØ§ÙÙ‚Ø© ÙˆØ¥ØºÙ„Ø§Ù‚ Ø§Ù„Ø¢Ù†',true);action(tr,'/close-suggestions/'+encodeURIComponent(x.symbol)+'/reject','Ø±ÙØ¶ Ø§Ù„Ø§Ù‚ØªØ±Ø§Ø­',false);holder.appendChild(tr)}
       if(!data.exits.length){const p=document.createElement('p');p.textContent='Ù„Ø§ ØªÙˆØ¬Ø¯ Ø§Ù‚ØªØ±Ø§Ø­Ø§Øª Ø¥ØºÙ„Ø§Ù‚ ØªØ­Ù‚Ù‚ Ø§Ù„Ù‡Ø¯Ù Ø­Ø§Ù„ÙŠÙ‹Ø§.';holder.appendChild(p)}
     }
     watch.textContent='Ø§Ù„Ù…Ø±Ø§Ù‚Ø¨Ø© ØªØ¹Ù…Ù„ | Ø¢Ø®Ø± ÙØ­Øµ ÙÙ†ÙŠ: '+(data.last_scan?new Date(data.last_scan).toLocaleTimeString('ar-KW'):'Ù„Ù… ÙŠØ¨Ø¯Ø£');
   }catch(e){watch.textContent='ØªØ¹Ø°Ø± ØªØ­Ø¯ÙŠØ« Ø§Ù„ÙØ±Øµ Ù…Ø¤Ù‚ØªÙ‹Ø§';}
   finally{loading=false;}
 }
 async function trigger(){
   if(watching||document.hidden)return;
   watching=true;
   const label=document.getElementById('auto-scan-status');
   if(label)label.textContent='â³ Ø§Ù„Ù…Ø±Ø§Ù‚Ø¨Ø© ØªØ¹Ù…Ù„Ø› Ø¬Ø§Ø±ÙŠ Ø§Ù„ØªØ­Ù‚Ù‚ Ù…Ù† ÙØ±Øµ Ø§Ù„ØªØ¯Ø§ÙˆÙ„...';
   try{
     const response=await fetch('/api/browser-watch',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:'csrf='+encodeURIComponent(csrf),credentials:'same-origin',cache:'no-store'});
     const result=await response.json();
     if(!response.ok)throw Error(result.error||'ØªØ¹Ø°Ø± Ø§Ù„ÙØ­Øµ');
     if(label)label.textContent=result.status==='binance_scans_disabled'?'ðŸ›¡ï¸ Ø£Ø³Ø¹Ø§Ø± Ø§Ù„Ø£Ø³ÙˆØ§Ù‚ Ø§Ù„Ø®Ø§Ø±Ø¬ÙŠØ© Ù…Ø³ØªÙ…Ø±Ø©Ø› ÙØ­Øµ Binance Ù…ØªÙˆÙ‚Ù Ø§Ø­ØªØ±Ø§Ø²ÙŠÙ‹Ø§':(result.status==='binance_banned'?'â³ Binance Ù…Ø­Ø¸ÙˆØ±Ø› Ø§Ù„ØªØ­Ù„ÙŠÙ„ Ø§Ù„Ù…Ø¹ØªÙ…Ø¯ Ø¹Ù„ÙŠÙ‡ Ù…Ø¤Ø¬Ù„':(result.scanned?'âœ… Ø§ÙƒØªÙ…Ù„ ÙØ­Øµ Binance':'ðŸŸ¢ Ø§Ù„Ù…Ø±Ø§Ù‚Ø¨Ø© ØªØ¹Ù…Ù„Ø› Ø§Ù„ÙØ­Øµ Ø§Ù„ØªØ§Ù„ÙŠ Ø¨Ø¹Ø¯ Ø§Ù„Ù…Ù‡Ù„Ø© Ø§Ù„Ù…Ø­Ø¯Ø¯Ø©'));
   }catch(e){if(label)label.textContent='âš ï¸ ÙØ´Ù„ Ø§Ù„ÙØ­Øµ Ø§Ù„ØªÙ„Ù‚Ø§Ø¦ÙŠ: '+e.message;}
   finally{watching=false;refresh();}
 }
 const symbols=[['Ø¨ÙŠØªÙƒÙˆÙŠÙ†','BTCUSDT'],['Ø¥ÙŠØ«Ø±ÙŠÙˆÙ…','ETHUSDT'],['BNB','BNBUSDT'],['Ø³ÙˆÙ„Ø§Ù†Ø§','SOLUSDT'],['XRP','XRPUSDT'],['Ø§Ù„Ø°Ù‡Ø¨','XAUUSDT'],['Ø§Ù„ÙØ¶Ø©','XAGUSDT'],['Ù†ÙØ· WTI','CLUSDT'],['Ù†ÙØ· Ø¨Ø±Ù†Øª','BZUSDT']];
 async function refreshMarket(){
   const tbody=document.getElementById('external-prices');
   if(!tbody)return;
   try{
     const r=await fetch('/api/market-prices',{credentials:'same-origin',cache:'no-store'});
     if(!r.ok)throw Error('price request');
     const data=await r.json();tbody.replaceChildren();
     for(const [name,sym] of symbols){
       const tr=document.createElement('tr'),item=data.prices[sym];
       td(tr,name+' '+sym);td(tr,item&&item.price!==null?Number(item.price).toLocaleString('en-US',{maximumFractionDigits:6}):'ØºÙŠØ± Ù…ØªØ§Ø­');
       td(tr,item&&item.asof?new Date(item.asof).toLocaleTimeString('ar-KW'):'â€”');tbody.appendChild(tr);
     }
     document.getElementById('price-state').textContent='Ø¢Ø®Ø± ØªØ­Ø¯ÙŠØ« Ù…Ù† Ø§Ù„Ù…ØµØ¯Ø±: '+(data.checked_at?new Date(data.checked_at).toLocaleTimeString('ar-KW'):'ØºÙŠØ± Ù…ØªØ§Ø­')+' | Ø§Ù„Ø¨ÙŠØ§Ù†Ø§Øª Ù‚Ø¯ ØªÙƒÙˆÙ† Ù…ØªØ£Ø®Ø±Ø©';
   }catch(e){document.getElementById('price-state').textContent='ØªØ¹Ø°Ø± ØªØ­Ø¯ÙŠØ« Ø§Ù„Ø£Ø³Ø¹Ø§Ø± Ø§Ù„Ø®Ø§Ø±Ø¬ÙŠØ© Ù…Ø¤Ù‚ØªÙ‹Ø§';}
 }
 async function refreshPnL(){
   try{
     const r=await fetch('/api/daily-pnl',{credentials:'same-origin',cache:'no-store'});
     if(!r.ok)throw Error('pnl');
     const p=await r.json();
     document.getElementById('daily-pnl').textContent='Ø¢Ø®Ø± Ù†ØªÙŠØ¬Ø© Ù…Ø­ÙÙˆØ¸Ø© (Ù„ÙŠØ³Øª Ù„Ø­Ø¸ÙŠØ©): Ø£Ø±Ø¨Ø§Ø­ '+p.gains+' USDT | Ø®Ø³Ø§Ø¦Ø± '+p.losses+' USDT | Ø±Ø³ÙˆÙ… ÙˆØªÙ…ÙˆÙŠÙ„ '+p.fees_funding+' USDT | Ø§Ù„ØµØ§ÙÙŠ '+p.net+' USDT | '+p.day+' | Ø¢Ø®Ø± ØªØ­Ø¯ÙŠØ« '+(p.captured_at?new Date(p.captured_at).toLocaleString('ar-KW'):'ØºÙŠØ± Ù…Ø¹Ø±ÙˆÙ');
   }catch(e){document.getElementById('daily-pnl').textContent='Ù„Ø§ ØªÙˆØ¬Ø¯ Ù†ØªØ§Ø¦Ø¬ ÙŠÙˆÙ…ÙŠØ© Ù…Ø¤ÙƒØ¯Ø© Ù…Ø­ÙÙˆØ¸Ø© Ø¨Ø¹Ø¯Ø› Ø§Ø³ØªØ®Ø¯Ù… Ø²Ø± Ø§Ù„ØªØ­Ø¯ÙŠØ« Ø¨Ø¹Ø¯ Ø§Ù†ØªÙ‡Ø§Ø¡ Ø§Ù„Ø­Ø¸Ø±.';}
 }
 refreshMarket();refreshPnL();setInterval(refreshMarket,15000);setInterval(refreshPnL,1000);
 refresh();trigger();setInterval(refresh,10000);setInterval(trigger,90000);
})();
</script><script>(function(){
 // Refresh the display every second; REST is rate-gated on the server.
 const bal=document.getElementById('v37-balance');
 if(!bal)return;
 const status=document.getElementById('v37-account-status');
 const at=document.getElementById('v37-account-time');
 let busy=false;
 async function poll(){
   if(document.hidden||busy)return;
   busy=true;
   try{
     const r=await fetch('/api/v37-live-account',{cache:'no-store',credentials:'same-origin'});
     if(!r.ok)throw Error('ØªØ¹Ø°Ø± Ù‚Ø±Ø§Ø¡Ø© Ø§Ù„Ø­Ø³Ø§Ø¨');
     const x=await r.json();
     if(x.balance!==null){bal.textContent=x.balance+' USDT';at.textContent='Ø¢Ø®Ø± ØªØ£ÙƒÙŠØ¯ Ù…Ù† Binance: '+new Date(x.captured_at).toLocaleString('ar-KW');}
     else{bal.textContent='Ø¨Ø§Ù†ØªØ¸Ø§Ø± Ø£ÙˆÙ„ ØªØ­Ø¯ÙŠØ« Ù…Ø¤ÙƒØ¯ Ù…Ù† Binance';}
     status.textContent=x.message||'Ø¢Ø®Ø± Ø¨ÙŠØ§Ù†Ø§Øª Ù…Ø¤ÙƒØ¯Ø© Ù…Ø­ÙÙˆØ¸Ø©';
   }catch(e){status.textContent='ØªØ¹Ø°Ø± Ø§Ù„ØªØ­Ø¯ÙŠØ« Ù…Ø¤Ù‚ØªÙ‹Ø§Ø› Ø¢Ø®Ø± Ø±ØµÙŠØ¯ Ù…Ø­ÙÙˆØ¸ Ù„Ø§ ÙŠØ²Ø§Ù„ Ø¸Ø§Ù‡Ø±Ù‹Ø§';}
   finally{busy=false;}
 }
 poll();setInterval(poll,1000);
})();
</script><script>(function(){
 let until=0,reason='';const msg=document.getElementById('binance-ban-message'),timer=document.getElementById('binance-ban-timer');
 if(!msg||!timer)return;
 function render(){if(!until){timer.textContent='Ù„Ø§ ÙŠÙˆØ¬Ø¯ Ø­Ø¸Ø± Ù…Ø³Ø¬Ù„';msg.textContent='Ø¢Ø®Ø± Ø­Ø§Ù„Ø© Ù…Ø­ÙÙˆØ¸Ø©: Ù„Ø§ ÙŠÙˆØ¬Ø¯ Ø­Ø¸Ø± Ù†Ø´Ø· Ù…Ø¹Ø±ÙˆÙ.';return}
 const seconds=Math.max(0,Math.ceil((until-Date.now())/1000));
 const h=Math.floor(seconds/3600),m=Math.floor((seconds%3600)/60),s=seconds%60;
 timer.textContent=seconds?`${String(h).padStart(2,'0')}:${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`:'Ø§Ù†ØªÙ‡Ù‰ Ø§Ù„ÙˆÙ‚Øª Ø§Ù„Ù…Ø³Ø¬Ù„ â€” Ù„Ù… ÙŠØªÙ… ØªØ£ÙƒÙŠØ¯ Ø±ÙØ¹ Ø§Ù„Ø­Ø¸Ø±';
 msg.textContent=reason+(seconds?' | Ù…ÙˆØ¹Ø¯ Ø§Ù„Ø§Ù†ØªÙ‡Ø§Ø¡ Ø¨ØªÙˆÙ‚ÙŠØª Ø§Ù„ÙƒÙˆÙŠØª: '+new Date(until).toLocaleString('ar-KW',{timeZone:'Asia/Kuwait'}):'');
 }
 async function update(){try{const r=await fetch('/api/binance-ban-status',{cache:'no-store',credentials:'same-origin'});if(!r.ok)return;const d=await r.json();until=Number(d.until_ms)||0;reason=d.reason||'';render()}catch(e){msg.textContent='ØªØ¹Ø°Ø± Ù‚Ø±Ø§Ø¡Ø© Ø­Ø§Ù„Ø© Ø§Ù„Ø­Ø¸Ø± Ø­Ø§Ù„ÙŠÙ‹Ø§'}}
 update();render();setInterval(render,1000);setInterval(update,15000);
})();</script><script>
(function(){
  const prices=new Map();
  const symbols=['btcusdt','ethusdt','bnbusdt','solusdt','xrpusdt'];
  const saved={{ positions|default([],true)|tojson }};
  const rawBalance={{ balance|default('ØºÙŠØ± Ù…ØªØ§Ø­')|tojson }};
  const wallet=Number(String(rawBalance).split(' ')[0]);
  const status=document.getElementById('v36-ws-status');
  if(!status)return;
  const body=document.getElementById('v36-live-market');
  const posBody=document.getElementById('v36-live-positions');
  const posStatus=document.getElementById('v36-position-status');
  const equity=document.getElementById('v36-equity');
  const fmt=x=>Number(x).toLocaleString('en-US',{maximumFractionDigits:6});
  let ws=null,retry=0,stopped=false,lastEvent=0;
  const formatTime=t=>new Date(t).toLocaleTimeString('ar-KW',{timeZone:'Asia/Kuwait'});
  const allSymbols=[...new Set([...symbols,...saved.map(p=>String(p.symbol||'').toLowerCase())])]
    .filter(x=>/^[a-z0-9]{3,25}$/.test(x));
  function paint(){
    const now=Date.now();
    const fresh=now-lastEvent<12000;
    if(!fresh){status.textContent='âš ï¸ Ø§Ù„Ø¨Ø« Ø§Ù„Ù…Ø¨Ø§Ø´Ø± ØºÙŠØ± Ù…ØªØµÙ„ Ø£Ùˆ Ù…ØªØ£Ø®Ø± â€” Ù„Ø§ ØªØ¹ØªÙ…Ø¯ Ø¹Ù„Ù‰ Ø§Ù„Ø£Ø³Ø¹Ø§Ø± Ø§Ù„Ù‚Ø¯ÙŠÙ…Ø©';}
    body.replaceChildren();
    for(const sym of allSymbols){
      const d=prices.get(sym);const tr=document.createElement('tr');
      for(const value of [sym.toUpperCase(),fresh&&d?fmt(d.price):'â€”',fresh&&d?formatTime(d.time):'â€”']){
        const td=document.createElement('td');td.textContent=value;tr.appendChild(td);
      }body.appendChild(tr);
    }
    posBody.replaceChildren();let total=0,complete=fresh&&saved.length>0;
    for(const p of saved){
      const sym=String(p.symbol||'').toLowerCase(),d=prices.get(sym);
      const amount=Number(p.positionAmt),entry=Number(p.entryPrice);
      const valid=fresh&&d&&Number.isFinite(amount)&&Number.isFinite(entry)&&entry>0;
      const pnl=valid?amount*(d.price-entry):null;
      if(valid)total+=pnl;else complete=false;
      const tr=document.createElement('tr');
      for(const value of [sym.toUpperCase(),p.positionAmt,p.entryPrice,valid?fmt(d.price):'â€”',valid?fmt(pnl)+' USDT':'ØºÙŠØ± Ù…ØªØ§Ø­']){
        const td=document.createElement('td');td.textContent=String(value);tr.appendChild(td);
      }posBody.appendChild(tr);
    }
    posStatus.textContent=!saved.length?'Ù„Ø§ ØªÙˆØ¬Ø¯ Ù…Ø±Ø§ÙƒØ² Ù…Ø­ÙÙˆØ¸Ø©Ø› Ø­Ø¯Ù‘Ø« Ù„Ù‚Ø·Ø© Ø§Ù„Ø­Ø³Ø§Ø¨ Ù„Ø¹Ø±Ø¶ Ù…Ø±Ø§ÙƒØ²Ùƒ.':
      complete?'ðŸŸ¢ Ø§Ù„Ø±Ø¨Ø­ ÙˆØ§Ù„Ø®Ø³Ø§Ø±Ø© ÙŠØªØºÙŠØ±Ø§Ù† Ù…Ø¹ Ø§Ù„Ø³Ø¹Ø±ØŒ Ù„ÙƒÙ† Ø¨ÙŠØ§Ù†Ø§Øª Ø§Ù„Ù…Ø±Ø§ÙƒØ² Ù…Ø­ÙÙˆØ¸Ø© ÙˆÙ‚Ø¯ÙŠÙ…Ø©':'âš ï¸ ØªÙ‚Ø¯ÙŠØ± Ø§Ù„Ù…Ø±Ø§ÙƒØ² ØºÙŠØ± Ù…ÙƒØªÙ…Ù„ Ø£Ùˆ Ø§Ù„Ø¨Ø« Ù…Ù†Ù‚Ø·Ø¹';
    equity.textContent=complete&&Number.isFinite(wallet)?
      'Ø§Ù„Ù‚ÙŠÙ…Ø© Ø§Ù„ØªÙ‚Ø¯ÙŠØ±ÙŠØ© (Ø±ØµÙŠØ¯ Ø§Ù„Ù…Ø­ÙØ¸Ø© Ø§Ù„Ù…Ø­ÙÙˆØ¸ + PnL Ø§Ù„Ù…Ø±Ø§ÙƒØ² Ø§Ù„Ù…Ø­ÙÙˆØ¸Ø©): '+fmt(wallet+total)+' USDT â€” Ù„ÙŠØ³Øª Ø±ØµÙŠØ¯Ù‹Ø§ Ù…Ø¨Ø§Ø´Ø±Ù‹Ø§ Ù…Ø¤ÙƒØ¯Ù‹Ø§':
      'Ø±ØµÙŠØ¯ Ø§Ù„Ù…Ø­ÙØ¸Ø© Ø§Ù„Ù…Ø¤ÙƒØ¯ ÙŠØ¨Ù‚Ù‰ Ø¢Ø®Ø± Ù„Ù‚Ø·Ø© Ù…Ø­ÙÙˆØ¸Ø©Ø› Ù„Ø§ ÙŠÙ…ÙƒÙ† Ø­Ø³Ø§Ø¨ Ø§Ù„Ù‚ÙŠÙ…Ø© Ø§Ù„Ø­Ø§Ù„ÙŠØ© Ø¨Ø«Ù‚Ø©.';
  }
  function connect(){
    if(stopped||document.hidden)return;
    const streams=allSymbols.map(s=>s+'@markPrice@1s').join('/');
    if(!streams)return;
    ws=new WebSocket('wss://fstream.binance.com/stream?streams='+streams);
    ws.onopen=()=>{retry=0;status.textContent='ðŸŸ¢ Ù…ØªØµÙ„ Ø¨Ø¨Ø« Binance Futures â€” Ø¨Ø§Ù†ØªØ¸Ø§Ø± Ø§Ù„Ø£Ø³Ø¹Ø§Ø±';};
    ws.onmessage=event=>{
      try{const msg=JSON.parse(event.data),p=msg.data||msg;
        if(p.e!=='markPriceUpdate'||!p.s)return;
        const price=Number(p.p);if(!Number.isFinite(price)||price<=0)return;
        prices.set(p.s.toLowerCase(),{price,time:Number(p.E)||Date.now()});
        lastEvent=Date.now();status.textContent='ðŸŸ¢ Ø§Ù„Ø£Ø³Ø¹Ø§Ø± Ù…Ø¨Ø§Ø´Ø±Ø© â€” Ø¢Ø®Ø± ØªØ­Ø¯ÙŠØ« '+formatTime(lastEvent);
        paint();
      }catch(e){}
    };
    ws.onerror=()=>{status.textContent='âš ï¸ ØªØ¹Ø°Ø± Ø§Ù„Ø§ØªØµØ§Ù„ Ø¨Ø¨Ø« Binance';};
    ws.onclose=()=>{
      if(stopped)return;
      status.textContent='âš ï¸ Ø§Ù†Ù‚Ø·Ø¹ Ø¨Ø« Binance â€” Ø¥Ø¹Ø§Ø¯Ø© Ø§ØªØµØ§Ù„ ØªØ¯Ø±ÙŠØ¬ÙŠØ©';
      const delay=Math.min(60000,2000*Math.pow(2,Math.min(retry++,5)));
      setTimeout(()=>{if(!document.hidden)connect();},delay);
    };
  }
  document.addEventListener('visibilitychange',()=>{
    if(document.hidden){if(ws){ws.onclose=null;ws.close();ws=null;}lastEvent=0;paint();}
    else if(!ws||ws.readyState===WebSocket.CLOSED)connect();
  });
  window.addEventListener('pagehide',()=>{stopped=true;if(ws){ws.onclose=null;ws.close();}});
  setInterval(paint,3000);paint();connect();
})();
</script></html>'''

# Public market data, cached to protect the exchange from one request per viewer per second.
_price_lock=threading.Lock()
_price_cache={'at':0.0,'data':{}}


@app.route('/api/browser-watch',methods=['POST'])
@auth
def browser_watch():
    try:
        check_csrf()
        if is_bot_paused(): return {'status':'paused','scanned':False}
        if not BINANCE_MARKET_SCANS_ENABLED:
            return {'status':'binance_scans_disabled','scanned':False,'message':'Ø§Ù„Ø£Ø³Ø¹Ø§Ø± Ø§Ù„Ø®Ø§Ø±Ø¬ÙŠØ© ØªØ¹Ù…Ù„ØŒ ÙˆÙØ­Øµ Binance Ù…ØªÙˆÙ‚Ù Ø§Ø­ØªØ±Ø§Ø²ÙŠÙ‹Ø§'}
        # Cross-process rate limiting: only one browser scan per 90 seconds.
        if _cooldown_state()['remaining_seconds']>0:
            return {'status':'binance_banned','scanned':False,'remaining_seconds':_cooldown_state()['remaining_seconds']}
        init()
        result=auto_scan()
        should_scan=result.get('status')=='ok'
        if _cooldown_state()['remaining_seconds']==0:
            try: monitor_close_suggestions()
            except Exception: logging.exception('Browser close monitor failed')
        return {'status':'ok','scanned':should_scan}
    except Exception:
        logging.exception('Browser watch failed')
        return {'error':'ØªØ¹Ø°Ø±Øª Ø§Ù„Ù…Ø±Ø§Ù‚Ø¨Ø© Ù…Ø¤Ù‚ØªÙ‹Ø§'},503

@app.route('/api/browser-suggestions')
@auth
def browser_suggestions():
    try:
        init()
        with db() as conn,conn.cursor(cursor_factory=RealDictCursor) as c:
            c.execute("SELECT id,symbol,direction,entry_reference,take_reference,stop_reference FROM bot_orders_v2 WHERE status='PENDING' AND created_at>NOW()-INTERVAL '15 minutes' ORDER BY id DESC LIMIT 25")
            entries=c.fetchall()
            c.execute("SELECT symbol,estimated_net_roi,estimated_net_usdt FROM bot_close_suggestions WHERE status='PENDING' AND created_at>NOW()-INTERVAL '10 minutes' ORDER BY estimated_net_roi DESC")
            exits=c.fetchall()
            c.execute('SELECT checked_at,opportunities FROM bot_scan_status WHERE id=1')
            status=c.fetchone()
        # Render values as strings to avoid JSON Decimal serialization differences.
        return {'entries':[{k:str(v) for k,v in x.items()} for x in entries],
                'exits':[{k:str(v) for k,v in x.items()} for x in exits],
                'last_scan':status['checked_at'].isoformat() if status and status['checked_at'] else None}
    except Exception:
        logging.exception('Suggestions refresh failed')
        return {'error':'ØªØ¹Ø°Ø± Ø¬Ù„Ø¨ Ø§Ù„ÙØ±Øµ'},503

# External quotes are indicative Yahoo Finance data (unofficial; may be delayed/blocked).
YAHOO_TICKERS={'BTCUSDT':'BTC-USD','ETHUSDT':'ETH-USD','BNBUSDT':'BNB-USD','SOLUSDT':'SOL-USD','XRPUSDT':'XRP-USD','XAUUSDT':'GC=F','XAGUSDT':'SI=F','CLUSDT':'CL=F','BZUSDT':'BZ=F'}
_external_cache={'at':0.0,'checked_at':None,'prices':{}}
_pnl_cache={'at':0.0,'data':None}

def account_auth(f):
    @wraps(f)
    def wrapped(*args,**kwargs):
        if not (session.get('admin') or session.get('investor_id')):
            return {'error':'login required'},401
        return f(*args,**kwargs)
    return wrapped

@app.route('/api/market-prices')
@account_auth
def market_prices():
    with _price_lock:
        now=time.monotonic()
        if now-_external_cache['at']>=30:
            result={}
            for symbol,ticker in YAHOO_TICKERS.items():
                try:
                    r=requests.get('https://query1.finance.yahoo.com/v8/finance/chart/'+requests.utils.quote(ticker,safe=''),
                                   params={'interval':'1m','range':'1d'},
                                   headers={'User-Agent':'Mozilla/5.0 AlkuwaitiBot/1.0'},timeout=3)
                    r.raise_for_status()
                    item=r.json()['chart']['result'][0]
                    price=item['meta'].get('regularMarketPrice')
                    timestamp=item['meta'].get('regularMarketTime')
                    result[symbol]={'price':price,'asof':datetime.fromtimestamp(timestamp,timezone.utc).isoformat() if timestamp else None}
                except Exception:
                    result[symbol]={'price':None,'asof':None}
            _external_cache.update(at=now,checked_at=datetime.now(timezone.utc).isoformat(),prices=result)
        return {'prices':_external_cache['prices'],'checked_at':_external_cache['checked_at'],'source':'Yahoo Finance (unofficial, possibly delayed)','realtime':False}

# V34: verified account/PnL snapshots; never fabricate Binance balances.
# Reads do not contact Binance. Refresh requires an explicit admin action and a shared cooldown.
def _snapshot_table():
    with db() as conn,conn.cursor() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS bot_verified_snapshots (
          kind TEXT PRIMARY KEY, payload JSONB NOT NULL,
          captured_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")

def _snapshot_get(kind):
    _snapshot_table()
    with db() as conn,conn.cursor(cursor_factory=RealDictCursor) as c:
        c.execute('SELECT payload,captured_at FROM bot_verified_snapshots WHERE kind=%s',(kind,))
        row=c.fetchone()
    return row

def _snapshot_put(kind,data):
    _snapshot_table()
    with db() as conn,conn.cursor() as c:
        c.execute("""INSERT INTO bot_verified_snapshots(kind,payload,captured_at)
                     VALUES(%s,%s::jsonb,NOW()) ON CONFLICT(kind) DO UPDATE
                     SET payload=EXCLUDED.payload,captured_at=NOW()""",(kind,json.dumps(data)))

def _snapshot_reserve(kind,seconds):
    # DB-backed cross-worker gate, including failed attempts, prevents retry storms.
    with db() as conn,conn.cursor() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS bot_snapshot_refresh_gate (
                     kind TEXT PRIMARY KEY, attempted_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
        c.execute("""INSERT INTO bot_snapshot_refresh_gate(kind,attempted_at)
                     VALUES(%s,NOW()) ON CONFLICT(kind) DO UPDATE
                     SET attempted_at=NOW() WHERE bot_snapshot_refresh_gate.attempted_at
                     < NOW()-(%s * INTERVAL '1 second') RETURNING kind""",(kind,seconds))
        return c.fetchone() is not None

def _daily_pnl_from_binance():
    from zoneinfo import ZoneInfo
    local_now=datetime.now(ZoneInfo('Asia/Kuwait'))
    day_start=local_now.replace(hour=0,minute=0,second=0,microsecond=0)
    rows=signed('GET','/fapi/v1/income',{'startTime':int(day_start.timestamp()*1000),
               'endTime':int(local_now.timestamp()*1000),'limit':1000})
    if len(rows)>=1000: raise ValueError('Ø³Ø¬Ù„ Ø§Ù„ÙŠÙˆÙ… ØªØ¬Ø§ÙˆØ² 1000 Ø¹Ù…Ù„ÙŠØ©Ø› Ø§Ù„Ù†ØªÙŠØ¬Ø© ØºÙŠØ± Ù…ÙƒØªÙ…Ù„Ø©')
    totals={'REALIZED_PNL':D('0'),'COMMISSION':D('0'),'FUNDING_FEE':D('0')}
    gains=losses=D('0')
    for row in rows:
        typ=row.get('incomeType')
        if typ not in totals or row.get('asset')!='USDT':continue
        amount=D(str(row['income']));totals[typ]+=amount
        if typ=='REALIZED_PNL':
            if amount>0:gains+=amount
            elif amount<0:losses+=amount
    fmt=lambda n:format(n.quantize(D('0.0001')),'f')
    return {'day':day_start.strftime('%Y-%m-%d')+' (Ø§Ù„ÙƒÙˆÙŠØª)','gains':fmt(gains),
            'losses':fmt(losses),'fees_funding':fmt(totals['COMMISSION']+totals['FUNDING_FEE']),
            'net':fmt(sum(totals.values(),D('0')))}

# V38: opt-in automatic snapshots with bounded refresh. Every-second browser polls
# only this app. Binance REST is gated globally via Neon and the weight budget.
BINANCE_AUTO_ACCOUNT_ENABLED=os.environ.get('BINANCE_AUTO_ACCOUNT_ENABLED','false').lower()=='true'
_v37_refresh_lock=threading.Lock()
_v37_last_check=0.0

def _v37_auto_refresh():
    global _v37_last_check
    with _v37_refresh_lock:
        now=time.monotonic()
        if now-_v37_last_check<10:return
        _v37_last_check=now
    try:
        _assert_binance_available()
        if not BINANCE_AUTO_ACCOUNT_ENABLED:return
        if _snapshot_reserve('v37_auto_account',600):
            try:
                account=signed('GET','/fapi/v2/account')
                positions_data=signed('GET','/fapi/v2/positionRisk')
                visible=[{k:str(p.get(k,'')) for k in ('symbol','positionAmt','entryPrice','unRealizedProfit')}
                         for p in positions_data if D(str(p.get('positionAmt','0')))!=0]
                _snapshot_put('account',{'balance':str(account['totalWalletBalance']),'positions':visible})
            except Exception as e:
                logging.warning('V37 automatic account update deferred: %s',str(e)[:140])
        # Daily income is more expensive; refresh at most once every 15 minutes.
        if _snapshot_reserve('v37_auto_pnl',1800):
            try:_snapshot_put('pnl',_daily_pnl_from_binance())
            except Exception as e:logging.warning('V37 automatic PnL update deferred: %s',str(e)[:140])
    except Exception as e:
        logging.warning('V37 automatic refresh deferred: %s',str(e)[:140])

@app.route('/api/v37-live-account')
@auth
def v37_live_account():
    # Do not delay the browser poll with Binance requests.
    # Run refresh asynchronously in this worker; shared Neon gates protect workers.
    if BINANCE_AUTO_ACCOUNT_ENABLED and _v37_refresh_lock.acquire(blocking=False):
        _v37_refresh_lock.release()
        threading.Thread(target=_v37_auto_refresh,daemon=True).start()
    try:
        row=_snapshot_get('account')
        if row:
            return {'balance':str(row['payload'].get('balance')),
                    'captured_at':row['captured_at'].isoformat(),
                    'message':'Ø¹Ø±Ø¶ Ø§Ù„Ø±ØµÙŠØ¯ Ø§Ù„Ù…Ø­ÙÙˆØ¸Ø› ÙˆÙ‚Øª ØªØ£ÙƒÙŠØ¯ Binance Ù…ÙˆØ¶Ø­ Ø£Ø¹Ù„Ø§Ù‡ØŒ ÙˆÙ„ÙŠØ³ Ø±ØµÙŠØ¯Ù‹Ø§ Ù„Ø­Ø¸ÙŠÙ‹Ø§'}
        return {'balance':None,'captured_at':None,'message':'Ù„Ø§ ÙŠÙˆØ¬Ø¯ Ø±ØµÙŠØ¯ Ù…Ø¤ÙƒØ¯ Ù…Ø­ÙÙˆØ¸. Ø¨Ø¹Ø¯ Ø§Ù†ØªÙ‡Ø§Ø¡ Ø§Ù„Ø­Ø¸Ø± Ø§Ø³ØªØ®Ø¯Ù… Ø²Ø± ØªØ­Ø¯ÙŠØ« Ø§Ù„Ø±ØµÙŠØ¯Ø› Ø§Ù„ØªØ­Ø¯ÙŠØ« Ø§Ù„ØªÙ„Ù‚Ø§Ø¦ÙŠ Ø§Ø®ØªÙŠØ§Ø±ÙŠ Ù…Ù† Render'}
    except Exception:
        logging.exception('V37 snapshot read failed')
        return {'error':'ØªØ¹Ø°Ø± Ù‚Ø±Ø§Ø¡Ø© Ø¨ÙŠØ§Ù†Ø§Øª Ø§Ù„Ø­Ø³Ø§Ø¨'},503

@app.route('/api/v34-snapshot-refresh',methods=['POST'])
@auth
def v34_snapshot_refresh():
    try:
        check_csrf()
        _assert_binance_available()
        if not _snapshot_reserve('account',600):
            flash('ØªØ­Ø¯ÙŠØ« Ø§Ù„Ø­Ø³Ø§Ø¨ Ù…Ø³Ù…ÙˆØ­ Ù…Ø±Ø© ÙƒÙ„ 10 Ø¯Ù‚Ø§Ø¦Ù‚Ø› Ø¢Ø®Ø± Ø§Ù„Ø¨ÙŠØ§Ù†Ø§Øª Ø§Ù„Ù…Ø­ÙÙˆØ¸Ø© Ù…Ø§ Ø²Ø§Ù„Øª Ø¸Ø§Ù‡Ø±Ø©.')
            return redirect(url_for('home'))
        _assert_binance_available()
        account=signed('GET','/fapi/v2/account')
        positions_data=signed('GET','/fapi/v2/positionRisk')
        visible=[{k:str(p.get(k,'')) for k in ('symbol','positionAmt','entryPrice','unRealizedProfit')}
                 for p in positions_data if D(str(p.get('positionAmt','0')))!=0]
        _snapshot_put('account',{'balance':str(account['totalWalletBalance']),'positions':visible})
        flash('ØªÙ… ØªØ­Ø¯ÙŠØ« Ø§Ù„Ø±ØµÙŠØ¯ ÙˆØ§Ù„Ù…Ø±Ø§ÙƒØ² Ù…Ù† Binance ÙˆØ­ÙØ¸ ÙˆÙ‚Øª Ø§Ù„ØªØ­Ø¯ÙŠØ«.')
    except Exception as e:
        logging.warning('V34 account refresh deferred: %s',str(e)[:180])
        flash('Ù„Ù… ÙŠØªÙ… ØªØ­Ø¯ÙŠØ« BinanceØ› Ø§Ù„Ø¨ÙŠØ§Ù†Ø§Øª Ø§Ù„Ù‚Ø¯ÙŠÙ…Ø© Ù…Ø­ÙÙˆØ¸Ø©. '+str(e)[:120])
    return redirect(url_for('home'))

@app.route('/api/v34-pnl-refresh',methods=['POST'])
@auth
def v34_pnl_refresh():
    try:
        check_csrf()
        _assert_binance_available()
        if not _snapshot_reserve('pnl',900):
            flash('ØªØ­Ø¯ÙŠØ« Ø§Ù„Ø£Ø±Ø¨Ø§Ø­ Ù…ØªØ§Ø­ ÙƒÙ„ 15 Ø¯Ù‚ÙŠÙ‚Ø©.')
            return redirect(url_for('home'))
        _assert_binance_available()
        _snapshot_put('pnl',_daily_pnl_from_binance())
        flash('ØªÙ… ØªØ­Ø¯ÙŠØ« Ø£Ø±Ø¨Ø§Ø­ ÙˆØ®Ø³Ø§Ø¦Ø± Ø§Ù„ÙŠÙˆÙ… Ù…Ù† Binance.')
    except Exception as e:
        logging.warning('V34 PnL refresh deferred: %s',str(e)[:180])
        flash('Ù„Ù… ÙŠØªÙ… ØªØ­Ø¯ÙŠØ« Ø§Ù„Ø£Ø±Ø¨Ø§Ø­Ø› Ø§Ù„Ø¨ÙŠØ§Ù†Ø§Øª Ø§Ù„Ù‚Ø¯ÙŠÙ…Ø© Ù…Ø­ÙÙˆØ¸Ø©. '+str(e)[:120])
    return redirect(url_for('home'))

@app.route('/api/daily-pnl')
@auth
def daily_pnl():
    try:
        row=_snapshot_get('pnl')
        if not row:return {'error':'Ù„Ø§ ØªÙˆØ¬Ø¯ Ù†ØªØ§Ø¦Ø¬ Ù…Ø¤ÙƒØ¯Ø© Ù…Ø­ÙÙˆØ¸Ø© Ø¨Ø¹Ø¯. Ø§Ø³ØªØ®Ø¯Ù… Ø²Ø± ØªØ­Ø¯ÙŠØ« Ø§Ù„Ø£Ø±Ø¨Ø§Ø­ Ù…Ù† Ù„ÙˆØ­Ø© Ø§Ù„Ù…Ø¯ÙŠØ±.'},503
        data=dict(row['payload'])
        from zoneinfo import ZoneInfo
        if row['captured_at'].astimezone(ZoneInfo('Asia/Kuwait')).date()!=datetime.now(ZoneInfo('Asia/Kuwait')).date():
            return {'error':'Ù†ØªÙŠØ¬Ø© Ø§Ù„Ø£Ø±Ø¨Ø§Ø­ Ø§Ù„Ù…Ø­ÙÙˆØ¸Ø© ØªØ®Øµ ÙŠÙˆÙ…Ù‹Ø§ Ø³Ø§Ø¨Ù‚Ù‹Ø§Ø› ÙŠÙ„Ø²Ù… ØªØ­Ø¯ÙŠØ«Ù‡Ø§ Ø¨Ø¹Ø¯ Ø§Ù†ØªÙ‡Ø§Ø¡ Ø§Ù„Ø­Ø¸Ø±'},503
        data['captured_at']=row['captured_at'].isoformat()
        data['stale']=True  # historical snapshot, not a live Binance quote
        return data
    except Exception:
        logging.exception('Snapshot PnL read failed')
        return {'error':'ØªØ¹Ø°Ø± Ù‚Ø±Ø§Ø¡Ø© Ø¢Ø®Ø± Ù†ØªØ§Ø¦Ø¬ Ù…Ø­ÙÙˆØ¸Ø©'},503


def init_investors():
    with db() as conn,conn.cursor() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS investor_accounts (
            id BIGSERIAL PRIMARY KEY, email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL, accepted_terms_version TEXT NOT NULL,
            accepted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            subscription_status TEXT NOT NULL DEFAULT 'TRIAL',
            trial_end TIMESTAMPTZ NOT NULL DEFAULT (NOW()+INTERVAL '7 days'),
            paid_until TIMESTAMPTZ)""")

INVESTOR_TERMS_VERSION='draft-2026-10-22'
INVESTOR_TERMS="""Ù‡Ø°Ù‡ Ù†Ø³Ø®Ø© ØªØ¬Ø±ÙŠØ¨ÙŠØ© Ù„Ø¹Ø±Ø¶ Ø§Ù„Ø£Ø³Ø¹Ø§Ø± ÙˆØ¥Ø¯Ø§Ø±Ø© Ø­Ø³Ø§Ø¨ Ø§Ù„Ø§Ø´ØªØ±Ø§Ùƒ ÙÙ‚Ø·. Ù„Ø§ ÙŠÙˆØ¯Ø¹ Ø§Ù„Ù…Ø³ØªØ®Ø¯Ù… Ø£Ù…ÙˆØ§Ù„Ø§Ù‹ ÙˆÙ„Ø§ ÙŠÙÙˆØ¶ Ø§Ù„Ø¨ÙˆØª Ø¨Ø§Ù„ØªØ¯Ø§ÙˆÙ„ Ù†ÙŠØ§Ø¨Ø© Ø¹Ù†Ù‡ Ø¨Ù…ÙˆØ¬Ø¨ Ù‡Ø°Ù‡ Ø§Ù„Ù…ÙˆØ§ÙÙ‚Ø©. Ø§Ù„Ø¨Ù†ÙˆØ¯ Ø§Ù„Ù…Ù‚ØªØ±Ø­Ø© Ø¨Ø´Ø£Ù† Ù…Ø´Ø§Ø±ÙƒØ© 50% Ù…Ù† Ø§Ù„Ø£Ø±Ø¨Ø§Ø­ ÙˆØ¥Ø¹Ø§Ø¯Ø© Ø§Ø³ØªØ«Ù…Ø§Ø± Ø±Ø£Ø³ Ø§Ù„Ù…Ø§Ù„ ÙˆÙ…Ù†Ø¹ Ø§Ù„Ø³Ø­Ø¨ ØºÙŠØ± Ù†Ø§ÙØ°Ø© ÙÙŠ Ù‡Ø°Ù‡ Ø§Ù„Ù†Ø³Ø®Ø©ØŒ ÙˆØªØªØ·Ù„Ø¨ Ø§ØªÙØ§Ù‚ÙŠØ© Ù…Ø³ØªÙ‚Ù„Ø© ÙˆÙ…Ø±Ø§Ø¬Ø¹Ø© Ù‚Ø§Ù†ÙˆÙ†ÙŠØ© ÙˆØªØ±Ø®ÙŠØµØ§Ù‹ Ù…Ù„Ø§Ø¦Ù…Ø§Ù‹ Ø­ÙŠØ« ÙŠÙ„Ø²Ù…. Ø§Ù„Ø£Ø³Ø¹Ø§Ø± Ø§Ù„Ø®Ø§Ø±Ø¬ÙŠØ© Ø¥Ø±Ø´Ø§Ø¯ÙŠØ© ÙˆÙ‚Ø¯ ØªØªØ£Ø®Ø±. Ø§Ù„Ø§Ø´ØªØ±Ø§ÙƒØ§Øª Ø§Ù„Ù…Ø¯ÙÙˆØ¹Ø© ØºÙŠØ± Ù…ÙØ¹Ù„Ø© Ø­ØªÙ‰ ØªØªØ¶Ø­ Ø§Ù„Ø£Ø³Ø¹Ø§Ø± ÙˆØ´Ø±ÙˆØ· Ø§Ù„Ø¯ÙØ¹ ÙˆØ§Ù„Ø¥Ù„ØºØ§Ø¡ ÙˆØ§Ù„Ø§Ø³ØªØ±Ø¯Ø§Ø¯."""

INVESTOR_PAGE="""<!doctype html><html lang="ar" dir="rtl"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Ø§Ù„Ù…Ø³ØªØ«Ù…Ø±ÙˆÙ† | Ø§Ù„Ø¨ÙˆØª Ø§Ù„ÙƒÙˆÙŠØªÙŠ</title>
<style>body{font-family:system-ui;background:#101827;color:#f3f6ff;margin:0;padding:20px}main{max-width:820px;margin:auto}article{background:#1d2b40;padding:20px;border-radius:14px;margin:16px 0}input,button{font:inherit;padding:12px;margin:7px 0;max-width:100%;box-sizing:border-box}input{width:100%;border-radius:8px}button{background:#36bb9a;border:0;border-radius:8px;cursor:pointer}table{width:100%;border-collapse:collapse}td,th{padding:10px;border-bottom:1px solid #41506a}small{color:#b6c6d9}a{color:#70d9ff}label{display:block;margin:8px 0}.terms{max-height:160px;overflow:auto;background:#101827;padding:12px;border-radius:8px}

/* Automatic daytime/nighttime palette */
:root{color-scheme:dark}body{transition:background-color .3s,color .3s}
html[data-theme="light"]{color-scheme:light}
html[data-theme="light"] body{background:#f4f7fc!important;color:#182438!important}
html[data-theme="light"] article{background:#fff!important;color:#182438!important;box-shadow:0 2px 14px #23385813}
html[data-theme="light"] a{color:#0759a7!important}
html[data-theme="light"] small{color:#46576a!important}
html[data-theme="light"] .warn{color:#8b5500!important}
html[data-theme="light"] td,html[data-theme="light"] th{border-color:#dce4ee!important}
html[data-theme="light"] input,html[data-theme="light"] select{background:#fff;color:#172438;border:1px solid #b9c9d9}
html[data-theme="light"] .terms{background:#eef3f9!important;color:#182438}
.logout-link{display:inline-block;padding:10px 14px;background:#a52e45!important;color:white!important;border-radius:10px;text-decoration:none;font-weight:700}
.theme-switch{display:inline-block;margin:8px 0;padding:9px 12px;background:#315174;color:white;border:0;border-radius:9px;cursor:pointer}
</style>
<script>(function(){
  function hour(){return Number(new Intl.DateTimeFormat('en-GB',{timeZone:'Asia/Kuwait',hour:'2-digit',hourCycle:'h23'}).format(new Date()))}
  function apply(){let pref;try{pref=localStorage.getItem('alkuwaiti-theme')}catch(e){};let theme=pref==='light'||pref==='dark'?pref:(hour()>=6&&hour()<18?'light':'dark');document.documentElement.dataset.theme=theme;let b=document.getElementById('theme-switch');if(b)b.textContent='ðŸŽ¨ Ø§Ù„Ù…Ø¸Ù‡Ø±: '+(theme==='light'?'Ù†Ù‡Ø§Ø±ÙŠ':'Ù„ÙŠÙ„ÙŠ')+' (Ø§Ø¶ØºØ· Ù„Ù„ØªØºÙŠÙŠØ±)'}
  window.toggleAlkuwaitiTheme=function(){let theme=document.documentElement.dataset.theme==='light'?'dark':'light';try{localStorage.setItem('alkuwaiti-theme',theme)}catch(e){};apply()};
  apply();document.addEventListener('DOMContentLoaded',apply);setInterval(apply,60000);
})();</script><main><button id="theme-switch" type="button" class="theme-switch" onclick="toggleAlkuwaitiTheme()">ðŸŽ¨ Ø§Ù„Ù…Ø¸Ù‡Ø±</button><div style='text-align:left'><label>ðŸŒ Language <select id='language' onchange='setLang(this.value)' style='padding:9px;border-radius:8px'><option value='ar'>Ø§Ù„Ø¹Ø±Ø¨ÙŠØ©</option><option value='en'>English</option><option value='fr'>FranÃ§ais</option><option value='es'>EspaÃ±ol</option><option value='hi'>à¤¹à¤¿à¤¨à¥à¤¦à¥€</option><option value='ur'>Ø§Ø±Ø¯Ùˆ</option><option value='tr'>TÃ¼rkÃ§e</option><option value='zh'>ä¸­æ–‡</option><option value='ru'>Ð ÑƒÑÑÐºÐ¸Ð¹</option><option value='pt'>PortuguÃªs</option><option value='de'>Deutsch</option><option value='id'>Bahasa Indonesia</option></select></label></div><h1>ðŸ‡°ðŸ‡¼ Ø§Ù„Ø¨ÙˆØª Ø§Ù„ÙƒÙˆÙŠØªÙŠ â€” Ø§Ù„Ù…Ø³ØªØ«Ù…Ø±ÙˆÙ†</h1><p><small>Ø§Ù„Ø­Ø¯ Ø§Ù„Ø£Ø¯Ù†Ù‰ Ù„Ù„Ø§Ø³ØªØ«Ù…Ø§Ø± Ø§Ù„Ù…Ù‚ØªØ±Ø­: 50 USDT. Ø§Ù„Ø¥ÙŠØ¯Ø§Ø¹ ÙˆØ¥Ø¯Ø§Ø±Ø© Ø§Ù„Ø£Ù…ÙˆØ§Ù„ ØºÙŠØ± Ù…ÙØ¹Ù„ÙŠÙ†.</small></p>
{% for msg in get_flashed_messages() %}<article>{{msg}}</article>{% endfor %}
{% if user %}<article><h2>Ø­Ø³Ø§Ø¨Ùƒ</h2><p>{{user.email}}</p><p>Ø­Ø§Ù„Ø© Ø§Ù„Ø§Ø´ØªØ±Ø§Ùƒ: {{user.subscription_status}}</p><p>Ø§Ù†ØªÙ‡Ø§Ø¡ Ø§Ù„ØªØ¬Ø±Ø¨Ø©: {{user.trial_end}}</p><p>Ø§Ù†ØªÙ‡Ø§Ø¡ Ø§Ù„Ø§Ø´ØªØ±Ø§Ùƒ Ø§Ù„Ù…Ø¯ÙÙˆØ¹: {{user.paid_until or 'ØºÙŠØ± Ù…ÙØ¹Ù„'}}</p><p><small>Ù„Ø§ ØªÙˆØ¬Ø¯ Ù…Ø­ÙØ¸Ø© Ø¥ÙŠØ¯Ø§Ø¹ Ø£Ùˆ Ø³Ø­Ø¨ Ø£Ùˆ ØªÙ†ÙÙŠØ° ØªØ¯Ø§ÙˆÙ„ ÙÙŠ Ø­Ø³Ø§Ø¨ Ø§Ù„Ù…Ø³ØªØ«Ù…Ø±.</small></p><p><a href='/investors/requests'>Ø·Ù„Ø¨Ø§Øª Ø§Ù„Ø§Ø´ØªØ±Ø§Ùƒ ÙˆÙ…Ø¨Ø§Ù„Øº Ø§Ù„Ø§Ø³ØªØ«Ù…Ø§Ø±</a></p><a href="{{url_for('investor_logout')}}" class="logout-link">ðŸšª ØªØ³Ø¬ÙŠÙ„ Ø®Ø±ÙˆØ¬ Ø§Ù„Ù…Ø³ØªØ«Ù…Ø±</a></article>
<article><h2>Ø£Ø³Ø¹Ø§Ø± Ø§Ù„Ø£Ø³ÙˆØ§Ù‚</h2><small>Ù…Ù† Yahoo Finance ØºÙŠØ± Ø§Ù„Ø±Ø³Ù…ÙŠØ› Ù‚Ø¯ ØªØªØ£Ø®Ø± Ø§Ù„Ø£Ø³Ø¹Ø§Ø± Ø£Ùˆ ØªÙƒÙˆÙ† ØºÙŠØ± Ù…ØªØ§Ø­Ø©. ØªØªØ­Ø¯Ø« Ø§Ù„Ø´Ø§Ø´Ø© ÙƒÙ„ Ø«Ø§Ù†ÙŠØ© ÙˆØ§Ù„Ù…ØµØ¯Ø± Ù†Ø­Ùˆ ÙƒÙ„ 30 Ø«Ø§Ù†ÙŠØ©.</small><table><thead><tr><th>Ø§Ù„Ø³ÙˆÙ‚</th><th>Ø§Ù„Ø³Ø¹Ø± Ø§Ù„Ø¥Ø±Ø´Ø§Ø¯ÙŠ</th><th>ÙˆÙ‚Øª Ø§Ù„Ù…ØµØ¯Ø±</th></tr></thead><tbody id="prices"><tr><td colspan="3">Ø¬Ø§Ø±ÙŠ Ø§Ù„ØªØ­Ù…ÙŠÙ„...</td></tr></tbody></table></article>
{% else %}<article><h2>Ø¥Ù†Ø´Ø§Ø¡ Ø­Ø³Ø§Ø¨ Ø¬Ø¯ÙŠØ¯</h2><form method="post" action="{{url_for('investor_register')}}"><input type="hidden" name="csrf" value="{{csrf}}"><label>Ø§Ù„Ø¨Ø±ÙŠØ¯ Ø§Ù„Ø¥Ù„ÙƒØªØ±ÙˆÙ†ÙŠ<input type="email" name="email" autocomplete="email" required maxlength="254"></label><label>ÙƒÙ„Ù…Ø© Ø§Ù„Ù…Ø±ÙˆØ± (12 Ø­Ø±ÙØ§Ù‹ Ø¹Ù„Ù‰ Ø§Ù„Ø£Ù‚Ù„)<input type="password" name="password" autocomplete="new-password" minlength="12" required></label><div class="terms">{{terms}}</div><label><input style="width:auto" type="checkbox" name="accept" value="yes" required> Ù‚Ø±Ø£Øª ÙˆÙˆØ§ÙÙ‚Øª Ø¹Ù„Ù‰ Ø´Ø±ÙˆØ· Ø§Ù„Ù†Ø³Ø®Ø© Ø§Ù„ØªØ¬Ø±ÙŠØ¨ÙŠØ© Ø£Ø¹Ù„Ø§Ù‡</label><button>ØªØ³Ø¬ÙŠÙ„ ÙˆØ¨Ø¯Ø¡ 7 Ø£ÙŠØ§Ù… ØªØ¬Ø±ÙŠØ¨ÙŠØ©</button></form></article>
<article><h2>ØªØ³Ø¬ÙŠÙ„ Ø§Ù„Ø¯Ø®ÙˆÙ„</h2><form method="post" action="{{url_for('investor_login')}}"><input type="hidden" name="csrf" value="{{csrf}}"><input type="email" name="email" placeholder="Ø§Ù„Ø¨Ø±ÙŠØ¯ Ø§Ù„Ø¥Ù„ÙƒØªØ±ÙˆÙ†ÙŠ" required><input type="password" name="password" placeholder="ÙƒÙ„Ù…Ø© Ø§Ù„Ù…Ø±ÙˆØ±" required><button>Ø¯Ø®ÙˆÙ„</button></form><p><a href="{{url_for('investor_forgot')}}">Ù†Ø³ÙŠØª ÙƒÙ„Ù…Ø© Ø§Ù„Ù…Ø±ÙˆØ±ØŸ / Forgot password?</a></p></article>{% endif %}
<article style="border:1px solid #577b9e"><h2>ðŸ” Ø¯Ø®ÙˆÙ„ Ù…Ø³Ø¤ÙˆÙ„ Ø§Ù„Ø¨ÙˆØª</h2><p><small>Ù‡Ø°Ù‡ Ø§Ù„Ø®Ø§Ù†Ø© Ø®Ø§ØµØ© Ø¨Ø§Ù„Ù…Ø³Ø¤ÙˆÙ„ ÙÙ‚Ø·. Ù„Ø§ ØªØ³ØªØ®Ø¯Ù… Ø¨ÙŠØ§Ù†Ø§Øª Ø§Ù„Ù…Ø³ØªØ«Ù…Ø± Ù„Ù„Ø¯Ø®ÙˆÙ„ Ø¥Ù„Ù‰ Ø§Ù„Ø¥Ø¯Ø§Ø±Ø©.</small></p>
{% if admin_logged %}<p>Ø£Ù†Øª Ù…Ø³Ø¬Ù„ Ø¯Ø®ÙˆÙ„Ùƒ ÙƒÙ…Ø³Ø¤ÙˆÙ„.</p><p><a href="{{url_for('logout')}}" class="logout-link">ðŸšª ØªØ³Ø¬ÙŠÙ„ Ø®Ø±ÙˆØ¬ Ø§Ù„Ù…Ø³Ø¤ÙˆÙ„</a></p><p><a href="{{url_for('home')}}">Ù„ÙˆØ­Ø© Ø§Ù„ØªØ¯Ø§ÙˆÙ„</a> | <a href="{{url_for('owner_investors')}}">Ø¥Ø¯Ø§Ø±Ø© Ø§Ù„Ù…Ø³ØªØ«Ù…Ø±ÙŠÙ†</a> | <a href="{{url_for('owner_requests_page')}}">Ø§Ù„Ù…ÙˆØ§ÙÙ‚Ø§Øª</a></p>
{% else %}<form method="post" action="{{url_for('login')}}"><input type="hidden" name="csrf" value="{{csrf}}"><label>Ø¨Ø±ÙŠØ¯ Ø§Ù„Ù…Ø³Ø¤ÙˆÙ„<input type="email" name="email" autocomplete="username" required></label><label>ÙƒÙ„Ù…Ø© Ù…Ø±ÙˆØ± Ø§Ù„Ù…Ø³Ø¤ÙˆÙ„<input type="password" name="password" autocomplete="current-password" required></label><button type="submit">Ø§Ù„Ø¯Ø®ÙˆÙ„ Ø¥Ù„Ù‰ Ø§Ù„Ø¥Ø¯Ø§Ø±Ø©</button></form>{% endif %}</article>
<article><h2>Ø§Ù„Ø¹Ø±ÙˆØ¶ Ø§Ù„Ù…Ù‚ØªØ±Ø­Ø© â€” Ø§Ù„Ø¯ÙØ¹ ØºÙŠØ± Ù…ÙØ¹Ù„</h2><table><tr><th>Ø§Ù„Ø¹Ø±Ø¶</th><th>Ø§Ù„Ø³Ø¹Ø± Ø§Ù„Ù…Ø¨Ø¯Ø¦ÙŠ</th></tr><tr><td>7 Ø£ÙŠØ§Ù… ØªØ¬Ø±Ø¨Ø©</td><td>Ù…Ø¬Ø§Ù†Ø§Ù‹</td></tr><tr><td>Ø£Ø³Ø¨ÙˆØ¹</td><td>19.99$</td></tr><tr><td>3 Ø£Ø³Ø§Ø¨ÙŠØ¹ + Ø§Ù„Ø±Ø§Ø¨Ø¹ Ù…Ø¬Ø§Ù†Ø§Ù‹</td><td>ÙŠØ­ØªØ§Ø¬ ØªØ£ÙƒÙŠØ¯ Ø·Ø±ÙŠÙ‚Ø© Ø§Ù„Ø­Ø³Ø§Ø¨</td></tr><tr><td>3 Ø£Ø´Ù‡Ø± + Ø§Ù„Ø±Ø§Ø¨Ø¹ Ù…Ø¬Ø§Ù†Ø§Ù‹</td><td>49.99$ â€” ÙŠØ­ØªØ§Ø¬ ØªØ£ÙƒÙŠØ¯ Ù‡Ù„ Ø§Ù„Ø³Ø¹Ø± Ø´Ù‡Ø±ÙŠ Ø£Ù… Ø¥Ø¬Ù…Ø§Ù„ÙŠ</td></tr><tr><td>Ø¹Ø§Ù… + Ø´Ù‡Ø±Ø§Ù† Ù…Ø¬Ø§Ù†Ø§Ù‹</td><td>99.99$ â€” ÙŠØ­ØªØ§Ø¬ ØªØ£ÙƒÙŠØ¯ Ù‡Ù„ Ø§Ù„Ø³Ø¹Ø± Ø³Ù†ÙˆÙŠ</td></tr></table><p><small>Ø®ØµÙ… Ù†Ù‡Ø§ÙŠØ© Ø§Ù„Ø£Ø³Ø¨ÙˆØ¹ Ø§Ù„Ù…Ù‚ØªØ±Ø­: 10%ØŒ Ù„Ù… ÙŠÙÙØ¹Ù‘Ù„ Ù„Ø¹Ø¯Ù… ÙˆØ¬ÙˆØ¯ Ø¯ÙØ¹ Ø¨Ø¹Ø¯.</small></p></article></main>
{% if user %}<script>const markets=[['Ø¨ÙŠØªÙƒÙˆÙŠÙ†','BTCUSDT'],['Ø¥ÙŠØ«Ø±ÙŠÙˆÙ…','ETHUSDT'],['BNB','BNBUSDT'],['Ø³ÙˆÙ„Ø§Ù†Ø§','SOLUSDT'],['XRP','XRPUSDT'],['Ø§Ù„Ø°Ù‡Ø¨','XAUUSDT'],['Ø§Ù„ÙØ¶Ø©','XAGUSDT'],['WTI','CLUSDT'],['Ø¨Ø±Ù†Øª','BZUSDT']];async function prices(){if(document.hidden)return;try{const r=await fetch('/api/market-prices',{cache:'no-store'});if(!r.ok)throw Error('network');const d=await r.json(),body=document.getElementById('prices');body.replaceChildren();for(const [name,sym] of markets){const tr=document.createElement('tr'),p=d.prices[sym];for(const value of [name,p&&p.price!=null?Number(p.price).toLocaleString('en-US',{maximumFractionDigits:6}):'ØºÙŠØ± Ù…ØªØ§Ø­',p&&p.asof?new Date(p.asof).toLocaleTimeString('ar-KW'):'â€”']){const td=document.createElement('td');td.textContent=value;tr.appendChild(td)}body.appendChild(tr)}}catch(e){}}prices();setInterval(prices,1000)</script>{% endif %}<script>
const labels={
 en:['Kuwaiti Bot â€” Investors','Create a new account','Sign in','Email','Password','Login','Forgot password?','Market prices','Subscription offers'],
 fr:['Bot KoweÃ¯tien â€” Investisseurs','CrÃ©er un compte','Connexion','E-mail','Mot de passe','Connexion','Mot de passe oubliÃ© ?','Prix des marchÃ©s','Offres dâ€™abonnement'],
 es:['Bot KuwaitÃ­ â€” Inversores','Crear cuenta','Iniciar sesiÃ³n','Correo','ContraseÃ±a','Entrar','Â¿OlvidÃ³ su contraseÃ±a?','Precios de mercado','Ofertas de suscripciÃ³n'],
 hi:['à¤•à¥à¤µà¥ˆà¤¤à¥€ à¤¬à¥‰à¤Ÿ â€” à¤¨à¤¿à¤µà¥‡à¤¶à¤•','à¤¨à¤¯à¤¾ à¤–à¤¾à¤¤à¤¾','à¤²à¥‰à¤— à¤‡à¤¨','à¤ˆà¤®à¥‡à¤²','à¤ªà¤¾à¤¸à¤µà¤°à¥à¤¡','à¤²à¥‰à¤— à¤‡à¤¨','à¤ªà¤¾à¤¸à¤µà¤°à¥à¤¡ à¤­à¥‚à¤² à¤—à¤?','à¤¬à¤¾à¤œà¤¾à¤° à¤­à¤¾à¤µ','à¤¸à¤¦à¤¸à¥à¤¯à¤¤à¤¾'],
 ur:['Ú©ÙˆÛŒØªÛŒ Ø¨ÙˆÙ¹ â€” Ø³Ø±Ù…Ø§ÛŒÛ Ú©Ø§Ø±','Ù†ÛŒØ§ Ø§Ú©Ø§Ø¤Ù†Ù¹','Ù„Ø§Ú¯ Ø§Ù†','Ø§ÛŒ Ù…ÛŒÙ„','Ù¾Ø§Ø³ ÙˆØ±Úˆ','Ø¯Ø§Ø®Ù„ ÛÙˆÚº','Ù¾Ø§Ø³ ÙˆØ±Úˆ Ø¨Ú¾ÙˆÙ„ Ú¯Ø¦Û’ØŸ','Ù…Ø§Ø±Ú©ÛŒÙ¹ Ù‚ÛŒÙ…ØªÛŒÚº','Ø³Ø¨Ø³Ú©Ø±Ù¾Ø´Ù†'],
 tr:['Kuveyt Botu â€” YatÄ±rÄ±mcÄ±lar','Yeni hesap','GiriÅŸ','E-posta','Åžifre','GiriÅŸ','Åžifremi unuttum','Piyasa fiyatlarÄ±','Abonelikler'],
 zh:['ç§‘å¨ç‰¹æœºå™¨äºº â€” æŠ•èµ„è€…','åˆ›å»ºè´¦æˆ·','ç™»å½•','ç”µå­é‚®ä»¶','å¯†ç ','ç™»å½•','å¿˜è®°å¯†ç ï¼Ÿ','å¸‚åœºä»·æ ¼','è®¢é˜…æ–¹æ¡ˆ'],
 ru:['ÐšÑƒÐ²ÐµÐ¹Ñ‚ÑÐºÐ¸Ð¹ Ð±Ð¾Ñ‚ â€” Ð˜Ð½Ð²ÐµÑÑ‚Ð¾Ñ€Ñ‹','Ð¡Ð¾Ð·Ð´Ð°Ñ‚ÑŒ Ð°ÐºÐºÐ°ÑƒÐ½Ñ‚','Ð’Ð¾Ð¹Ñ‚Ð¸','ÐŸÐ¾Ñ‡Ñ‚Ð°','ÐŸÐ°Ñ€Ð¾Ð»ÑŒ','Ð’Ð¾Ð¹Ñ‚Ð¸','Ð—Ð°Ð±Ñ‹Ð»Ð¸ Ð¿Ð°Ñ€Ð¾Ð»ÑŒ?','Ð¦ÐµÐ½Ñ‹ Ñ€Ñ‹Ð½ÐºÐ¾Ð²','ÐŸÐ¾Ð´Ð¿Ð¸ÑÐºÐ¸'],
 pt:['Bot Kuwaitiano â€” Investidores','Criar conta','Entrar','E-mail','Senha','Entrar','Esqueceu a senha?','PreÃ§os de mercado','Planos'],
 de:['Kuwait-Bot â€” Anleger','Konto erstellen','Anmelden','E-Mail','Passwort','Anmelden','Passwort vergessen?','Marktpreise','Abonnements'],
 id:['Bot Kuwait â€” Investor','Buat akun','Masuk','Email','Kata sandi','Masuk','Lupa kata sandi?','Harga pasar','Langganan']};
const originals=['Ø§Ù„Ø¨ÙˆØª Ø§Ù„ÙƒÙˆÙŠØªÙŠ â€” Ø§Ù„Ù…Ø³ØªØ«Ù…Ø±ÙˆÙ†','Ø¥Ù†Ø´Ø§Ø¡ Ø­Ø³Ø§Ø¨ Ø¬Ø¯ÙŠØ¯','ØªØ³Ø¬ÙŠÙ„ Ø§Ù„Ø¯Ø®ÙˆÙ„','Ø§Ù„Ø¨Ø±ÙŠØ¯ Ø§Ù„Ø¥Ù„ÙƒØªØ±ÙˆÙ†ÙŠ','ÙƒÙ„Ù…Ø© Ø§Ù„Ù…Ø±ÙˆØ±','Ø¯Ø®ÙˆÙ„','Ù†Ø³ÙŠØª ÙƒÙ„Ù…Ø© Ø§Ù„Ù…Ø±ÙˆØ±ØŸ','Ø£Ø³Ø¹Ø§Ø± Ø§Ù„Ø£Ø³ÙˆØ§Ù‚','Ø§Ù„Ø¹Ø±ÙˆØ¶ Ø§Ù„Ù…Ù‚ØªØ±Ø­Ø©'];
function setLang(lang){localStorage.setItem('investor_lang',lang);document.documentElement.lang=lang;document.documentElement.dir=['ar','ur'].includes(lang)?'rtl':'ltr';const dict=labels[lang];if(!dict)return;document.querySelectorAll('h1,h2,label,button,a').forEach(el=>{if(el.children.length>0 && el.tagName!=='A')return;let text=el.textContent.trim();for(let i=0;i<originals.length;i++)if(text.includes(originals[i])){el.textContent=text.replace(originals[i],dict[i]);break;}})}
try{const lang=localStorage.getItem('investor_lang')||((navigator.language||'ar').split('-')[0]);document.getElementById('language').value=labels[lang]?lang:'ar';setLang(lang)}catch(e){}
</script></html>"""

@app.route('/investors')
def investors():
    user=None
    if session.get('investor_id'):
        try:
            init_investors()
            with db() as conn,conn.cursor(cursor_factory=RealDictCursor) as c:
                c.execute('SELECT email,subscription_status,trial_end,paid_until FROM investor_accounts WHERE id=%s',(session['investor_id'],))
                user=c.fetchone()
            if user is None:
                session.pop('investor_id', None)
        except Exception:
            logging.exception('Investor profile unavailable')
            flash('ØªØ¹Ø°Ø± ØªØ­Ù…ÙŠÙ„ Ø­Ø³Ø§Ø¨ Ø§Ù„Ù…Ø³ØªØ«Ù…Ø± Ù…Ø¤Ù‚ØªØ§Ù‹. ÙŠØ±Ø¬Ù‰ Ø§Ù„Ù…Ø­Ø§ÙˆÙ„Ø© Ù„Ø§Ø­Ù‚Ø§Ù‹.')
    return render_template_string(INVESTOR_PAGE,user=user,csrf=csrf(),terms=INVESTOR_TERMS,admin_logged=bool(session.get('admin')))

@app.route('/investors/register',methods=['POST'])
def investor_register():
    try:
        check_csrf()
        if request.form.get('accept')!='yes':raise ValueError('ÙŠØ¬Ø¨ Ø§Ù„Ù…ÙˆØ§ÙÙ‚Ø© Ø¹Ù„Ù‰ Ø´Ø±ÙˆØ· Ø§Ù„ØªØ¬Ø±Ø¨Ø©')
        email=request.form.get('email','').strip().lower();pw=request.form.get('password','')
        if '@' not in email or len(email)>254 or len(pw)<12 or len(pw)>256:raise ValueError('ØªØ­Ù‚Ù‚ Ù…Ù† Ø§Ù„Ø¨Ø±ÙŠØ¯ ÙˆÙƒÙ„Ù…Ø© Ø§Ù„Ù…Ø±ÙˆØ± (12 Ø­Ø±ÙØ§Ù‹ Ø¹Ù„Ù‰ Ø§Ù„Ø£Ù‚Ù„)')
        init_investors()
        with db() as conn,conn.cursor() as c:
            c.execute('INSERT INTO investor_accounts(email,password_hash,accepted_terms_version) VALUES(%s,%s,%s) RETURNING id',(email,generate_password_hash(pw),INVESTOR_TERMS_VERSION))
            uid=c.fetchone()[0]
        session.clear();session['investor_id']=uid;session.permanent=True;csrf()
        flash('ØªÙ… Ø¥Ù†Ø´Ø§Ø¡ Ø­Ø³Ø§Ø¨ Ø§Ù„Ø¹Ø±Ø¶ Ø§Ù„ØªØ¬Ø±ÙŠØ¨ÙŠ')
    except psycopg2.errors.UniqueViolation:
        flash('Ø§Ù„Ø¨Ø±ÙŠØ¯ Ù…Ø³Ø¬Ù„ Ø¨Ø§Ù„ÙØ¹Ù„')
    except Exception as e:flash('ØªØ¹Ø°Ø± Ø§Ù„ØªØ³Ø¬ÙŠÙ„: '+str(e)[:140])
    return redirect(url_for('investors'))

@app.route('/investors/login',methods=['POST'])
def investor_login():
    try:
        check_csrf();init_investors()
        with db() as conn,conn.cursor() as c:
            c.execute('SELECT id,password_hash FROM investor_accounts WHERE email=%s',(request.form.get('email','').strip().lower(),))
            row=c.fetchone()
        if not row or not check_password_hash(row[1],request.form.get('password','')):raise ValueError('Ø¨ÙŠØ§Ù†Ø§Øª Ø§Ù„Ø¯Ø®ÙˆÙ„ ØºÙŠØ± ØµØ­ÙŠØ­Ø©')
        session.clear();session['investor_id']=row[0];session.permanent=True;csrf()
    except Exception as e:flash(str(e)[:140])
    return redirect(url_for('investors'))


# Secure, single-use investor password reset. Requires SMTP_* settings in Render.
def init_password_resets():
    init_investors()
    with db() as conn,conn.cursor() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS investor_password_resets (
            id BIGSERIAL PRIMARY KEY,
            investor_id BIGINT NOT NULL REFERENCES investor_accounts(id) ON DELETE CASCADE,
            token_hash CHAR(64) NOT NULL UNIQUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            expires_at TIMESTAMPTZ NOT NULL,
            used_at TIMESTAMPTZ)""")
        c.execute('CREATE INDEX IF NOT EXISTS idx_password_resets_user_time ON investor_password_resets(investor_id,created_at DESC)')

RESET_PAGE="""<!doctype html><html lang='ar' dir='rtl'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Ø§Ø³ØªØ¹Ø§Ø¯Ø© ÙƒÙ„Ù…Ø© Ø§Ù„Ù…Ø±ÙˆØ±</title><style>body{background:#101827;color:white;font:16px system-ui;padding:20px}main{max-width:520px;margin:auto}article{background:#1d2b40;padding:24px;border-radius:14px}input,button{box-sizing:border-box;width:100%;padding:13px;margin:8px 0;border-radius:8px}button{background:#36bb9a;border:0}a{color:#70d9ff}</style><main><article><h1>{{heading}}</h1><p>{{help}}</p>{% for m in get_flashed_messages() %}<p>{{m}}</p>{% endfor %}<form method='post'><input type='hidden' name='csrf' value='{{csrf}}'>{% if token %}<input type='hidden' name='token' value='{{token}}'><label>ÙƒÙ„Ù…Ø© Ø§Ù„Ù…Ø±ÙˆØ± Ø§Ù„Ø¬Ø¯ÙŠØ¯Ø© (12 Ø­Ø±ÙÙ‹Ø§ Ø¹Ù„Ù‰ Ø§Ù„Ø£Ù‚Ù„)</label><input name='password' type='password' minlength='12' maxlength='256' autocomplete='new-password' required><label>ØªØ£ÙƒÙŠØ¯ ÙƒÙ„Ù…Ø© Ø§Ù„Ù…Ø±ÙˆØ±</label><input name='confirm' type='password' minlength='12' maxlength='256' autocomplete='new-password' required>{% else %}<label>Ø§Ù„Ø¨Ø±ÙŠØ¯ Ø§Ù„Ø¥Ù„ÙƒØªØ±ÙˆÙ†ÙŠ Ø§Ù„Ù…Ø³Ø¬Ù„</label><input name='email' type='email' required maxlength='254' autocomplete='email'>{% endif %}<button>{{'ØªØºÙŠÙŠØ± ÙƒÙ„Ù…Ø© Ø§Ù„Ù…Ø±ÙˆØ±' if token else 'Ø¥Ø±Ø³Ø§Ù„ Ø±Ø§Ø¨Ø· Ø§Ù„Ø§Ø³ØªØ¹Ø§Ø¯Ø©'}}</button></form><a href='/investors'>Ø§Ù„Ø¹ÙˆØ¯Ø© Ù„Ù„Ù…Ø³ØªØ«Ù…Ø±ÙŠÙ†</a></article></main></html>"""

def send_reset_mail(email,token):
    host=os.environ.get('SMTP_HOST','');user=os.environ.get('SMTP_USER','')
    password=os.environ.get('SMTP_PASSWORD','');sender=os.environ.get('SMTP_FROM','')
    if not all([host,user,password,sender]):
        app.logger.warning('Password reset SMTP not configured')
        return False
    from urllib.parse import quote
    url=url_for('investor_reset',token=token,_external=True)
    msg=EmailMessage();msg['Subject']='Alkuwaiti Bot - Password reset / Ø§Ø³ØªØ¹Ø§Ø¯Ø© ÙƒÙ„Ù…Ø© Ø§Ù„Ù…Ø±ÙˆØ±'
    msg['From']=sender;msg['To']=email
    msg.set_content('Ø±Ø§Ø¨Ø· ØªØºÙŠÙŠØ± ÙƒÙ„Ù…Ø© Ø§Ù„Ù…Ø±ÙˆØ± ØµØ§Ù„Ø­ Ù„Ù…Ø¯Ø© 20 Ø¯Ù‚ÙŠÙ‚Ø© ÙˆÙŠØ³ØªØ®Ø¯Ù… Ù…Ø±Ø© ÙˆØ§Ø­Ø¯Ø© ÙÙ‚Ø·:\n'+url+'\n\nIf you did not request this, ignore this email.')
    port=int(os.environ.get('SMTP_PORT','465'))
    if port==465:
        with smtplib.SMTP_SSL(host,port,timeout=10,context=ssl.create_default_context()) as smtp:
            smtp.login(user,password);smtp.send_message(msg)
    else:
        with smtplib.SMTP(host,port,timeout=10) as smtp:
            smtp.starttls(context=ssl.create_default_context());smtp.login(user,password);smtp.send_message(msg)
    return True

@app.route('/investors/forgot-password',methods=['GET','POST'])
def investor_forgot():
    if request.method=='POST':
        check_csrf()
        email=request.form.get('email','').strip().lower()
        # Generic response prevents revealing registered emails.
        if len(email)<=254 and '@' in email:
            try:
                init_password_resets()
                with db() as conn,conn.cursor() as c:
                    c.execute('SELECT id FROM investor_accounts WHERE email=%s',(email,));row=c.fetchone()
                    if row:
                        uid=row[0]
                        c.execute("SELECT COUNT(*) FROM investor_password_resets WHERE investor_id=%s AND created_at>NOW()-INTERVAL '1 hour'",(uid,))
                        if c.fetchone()[0]<3:
                            token=secrets.token_urlsafe(40)
                            digest=hashlib.sha256(token.encode()).hexdigest()
                            c.execute("INSERT INTO investor_password_resets(investor_id,token_hash,expires_at) VALUES(%s,%s,NOW()+INTERVAL '20 minutes')",(uid,digest))
                            # Sending only after commit prevents a link referencing an uncommitted token.
                            conn.commit()
                            try:send_reset_mail(email,token)
                            except Exception:app.logger.exception('SMTP reset delivery failed')
            except Exception:app.logger.exception('Password reset request failed')
        flash('Ø¥Ø°Ø§ ÙƒØ§Ù† Ø§Ù„Ø¨Ø±ÙŠØ¯ Ù…Ø³Ø¬Ù„Ø§Ù‹ ÙˆØ®Ø¯Ù…Ø© Ø§Ù„Ø¨Ø±ÙŠØ¯ Ù…ÙØ¹Ù„Ø©ØŒ Ø³ÙŠØµÙ„Ùƒ Ø±Ø§Ø¨Ø· Ø§Ù„Ø§Ø³ØªØ¹Ø§Ø¯Ø©. Ø±Ø§Ø¬Ø¹ Ø§Ù„Ø±Ø³Ø§Ø¦Ù„ ØºÙŠØ± Ø§Ù„Ù…Ø±ØºÙˆØ¨Ø©.')
        return redirect(url_for('investor_forgot'))
    return render_template_string(RESET_PAGE,heading='Ù†Ø³ÙŠØª ÙƒÙ„Ù…Ø© Ø§Ù„Ù…Ø±ÙˆØ±ØŸ',help='Ø£Ø¯Ø®Ù„ Ø¨Ø±ÙŠØ¯Ùƒ Ù„Ø¥Ø±Ø³Ø§Ù„ Ø±Ø§Ø¨Ø· Ø§Ø³ØªØ¹Ø§Ø¯Ø© ØµØ§Ù„Ø­ Ù„Ù…Ø¯Ø© 20 Ø¯Ù‚ÙŠÙ‚Ø©.',csrf=csrf(),token=None)

@app.route('/investors/reset-password/<token>',methods=['GET','POST'])
def investor_reset(token):
    if len(token)>100 or len(token)<30:return 'Ø±Ø§Ø¨Ø· ØºÙŠØ± ØµØ§Ù„Ø­',400
    digest=hashlib.sha256(token.encode()).hexdigest()
    if request.method=='POST':
        check_csrf()
        pw=request.form.get('password','')
        if len(pw)<12 or len(pw)>256 or pw!=request.form.get('confirm'):
            flash('ØªØ£ÙƒØ¯ Ù…Ù† ØªØ·Ø§Ø¨Ù‚ ÙƒÙ„Ù…Ø© Ø§Ù„Ù…Ø±ÙˆØ± ÙˆØ£Ù†Ù‡Ø§ 12 Ø­Ø±ÙÙ‹Ø§ Ø¹Ù„Ù‰ Ø§Ù„Ø£Ù‚Ù„')
            return redirect(url_for('investor_reset',token=token))
        init_password_resets()
        with db() as conn,conn.cursor() as c:
            c.execute("""SELECT id,investor_id FROM investor_password_resets
                WHERE token_hash=%s AND used_at IS NULL AND expires_at>NOW() FOR UPDATE""",(digest,))
            row=c.fetchone()
            if not row:return 'Ø§Ù„Ø±Ø§Ø¨Ø· Ù…Ù†ØªÙ‡ÙŠ Ø§Ù„ØµÙ„Ø§Ø­ÙŠØ© Ø£Ùˆ Ù…Ø³ØªØ®Ø¯Ù… Ø¨Ø§Ù„ÙØ¹Ù„',400
            c.execute('UPDATE investor_accounts SET password_hash=%s WHERE id=%s',(generate_password_hash(pw),row[1]))
            c.execute('UPDATE investor_password_resets SET used_at=NOW() WHERE investor_id=%s AND used_at IS NULL',(row[1],))
        session.clear()
        flash('ØªÙ… ØªØºÙŠÙŠØ± ÙƒÙ„Ù…Ø© Ø§Ù„Ù…Ø±ÙˆØ±. Ø³Ø¬Ù‘Ù„ Ø§Ù„Ø¯Ø®ÙˆÙ„ Ø¨ÙƒÙ„Ù…Ø© Ø§Ù„Ù…Ø±ÙˆØ± Ø§Ù„Ø¬Ø¯ÙŠØ¯Ø©.')
        return redirect(url_for('investors'))
    return render_template_string(RESET_PAGE,heading='ØªØ¹ÙŠÙŠÙ† ÙƒÙ„Ù…Ø© Ù…Ø±ÙˆØ± Ø¬Ø¯ÙŠØ¯Ø©',help='Ø§Ù„Ø±Ø§Ø¨Ø· ØµØ§Ù„Ø­ Ù„Ù…Ø¯Ø© 20 Ø¯Ù‚ÙŠÙ‚Ø© ÙˆÙŠÙØ³ØªØ®Ø¯Ù… Ù…Ø±Ø© ÙˆØ§Ø­Ø¯Ø©.',csrf=csrf(),token=token)

@app.route('/investors/logout')
def investor_logout():
    session.clear();return redirect(url_for('investors'))


# Investor administration: records only, no custody, payment processing or automatic profit fees.
def init_investor_admin():
    init_investors()
    with db() as conn,conn.cursor() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS investor_financial_records (
            id BIGSERIAL PRIMARY KEY, investor_id BIGINT NOT NULL REFERENCES investor_accounts(id),
            category TEXT NOT NULL CHECK (category IN ('SUBSCRIPTION','INVESTMENT_DECLARED','TRADE_PROFIT','TRADE_LOSS','PROFIT_SHARE_PROPOSED')),
            amount_usdt NUMERIC(22,8) NOT NULL CHECK(amount_usdt>=0),
            reference TEXT, notes TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
        c.execute("""CREATE TABLE IF NOT EXISTS owner_settings (
            id INTEGER PRIMARY KEY CHECK(id=1), rain_network TEXT, rain_deposit_address TEXT,
            minimum_investment_usdt NUMERIC NOT NULL DEFAULT 50,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
        c.execute("INSERT INTO owner_settings(id) VALUES(1) ON CONFLICT(id) DO NOTHING")

OWNER_PAGE="""<!doctype html><html lang=ar dir=rtl><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>Ø¥Ø¯Ø§Ø±Ø© Ø§Ù„Ù…Ø³ØªØ«Ù…Ø±ÙŠÙ† | Ø§Ù„Ø¨ÙˆØª Ø§Ù„ÙƒÙˆÙŠØªÙŠ</title>
<style>body{font-family:system-ui;background:#101827;color:#eef3fa;padding:18px}main{max-width:1100px;margin:auto}article{background:#203047;border-radius:12px;padding:16px;margin:14px 0;overflow:auto}table{border-collapse:collapse;width:100%}td,th{padding:10px;border-bottom:1px solid #40536b;text-align:right;white-space:nowrap}input,button{padding:10px;font:inherit;border-radius:6px;max-width:100%}button{background:#31b99a;border:0}label{display:block;margin:12px 0}small{color:#c7d1df}a{color:#84dfff}</style>
<main><h1>ðŸ‡°ðŸ‡¼ Ù„ÙˆØ­Ø© ØµØ§Ø­Ø¨ Ø§Ù„Ø¨ÙˆØª â€” Ø§Ù„Ù…Ø³ØªØ«Ù…Ø±ÙˆÙ†</h1><p><a href='/'>Ø§Ù„Ø¹ÙˆØ¯Ø© Ø¥Ù„Ù‰ Ù„ÙˆØ­Ø© Ø§Ù„Ø¨ÙˆØª</a> | <a href='/owner/requests'>Ø§Ù„Ù…ÙˆØ§ÙÙ‚Ø§Øª ÙˆØ·Ø±Ù‚ Ø§Ù„Ø¯ÙØ¹</a></p>
{% for msg in get_flashed_messages() %}<article>{{msg}}</article>{% endfor %}
<article><h2>Ø§Ù„Ø­Ø¯ Ø§Ù„Ø£Ø¯Ù†Ù‰ Ù„Ù„Ø§Ø³ØªØ«Ù…Ø§Ø±: {{settings.minimum_investment_usdt}} USDT</h2><p><small>Ø§Ù„Ø­Ø¯ Ø§Ù„Ø£Ø¯Ù†Ù‰ Ù…Ø¹Ù„ÙˆÙ…Ø© Ø¸Ø§Ù‡Ø±Ø© ÙÙŠ Ø§Ù„Ù…Ù†ØµØ©ØŒ ÙˆÙ„ÙŠØ³ Ø·Ù„Ø¨ ØªØ­ÙˆÙŠÙ„ Ø£Ùˆ ØªÙÙˆÙŠØ¶ Ù„Ø¥Ø¯Ø§Ø±Ø© Ø£Ù…ÙˆØ§Ù„.</small></p><p>Ø¹Ø¯Ø¯ Ø§Ù„Ù…Ø³ØªØ«Ù…Ø±ÙŠÙ† Ø§Ù„Ù…Ø³Ø¬Ù„ÙŠÙ†: {{count}}</p></article>
<article><h2>Ø¨ÙŠØ§Ù†Ø§Øª Ø¥ÙŠØ¯Ø§Ø¹ Ø±ÙŠÙ† â€” Ù…Ø­ÙÙˆØ¸Ø© Ù„Ù„Ø¥Ø¯Ø§Ø±Ø© ÙÙ‚Ø·</h2><p><small>Ù„Ø§ ØªØ¹Ø±Ø¶ Ù„Ù„Ù…Ø³ØªØ«Ù…Ø±ÙŠÙ† ÙˆÙ„Ø§ ØªÙØ³ØªØ®Ø¯Ù… Ù„ØªØ­ØµÙŠÙ„ Ø£Ù…ÙˆØ§Ù„ ØªÙ„Ù‚Ø§Ø¦ÙŠØ§Ù‹. ØªØ£ÙƒØ¯ Ù…Ù† Ø§Ù„Ø´Ø¨ÙƒØ© ÙˆØ§Ù„Ø¹Ù…Ù„Ø© ÙˆÙ‚ÙˆØ§Ø¹Ø¯ Ø±ÙŠÙ† Ù‚Ø¨Ù„ Ø£ÙŠ Ø§Ø³ØªØ®Ø¯Ø§Ù….</small></p><form method=post action='/owner/rain'><input type=hidden name=csrf value='{{csrf}}'><label>Ø´Ø¨ÙƒØ© Ø§Ù„Ø¥ÙŠØ¯Ø§Ø¹ <input name=network value='{{settings.rain_network or ""}}' maxlength=50 placeholder='Ù…Ø«Ø§Ù„: Ø´Ø¨ÙƒØ© Ù…Ø¯Ø¹ÙˆÙ…Ø© ÙØ¹Ù„Ø§Ù‹ ÙÙŠ Ø±ÙŠÙ†'></label><label>Ø¹Ù†ÙˆØ§Ù† Ø§Ù„Ø¥ÙŠØ¯Ø§Ø¹ <input name=address value='{{settings.rain_deposit_address or ""}}' maxlength=256 autocomplete=off placeholder='Ø£Ø¯Ø®Ù„ Ø§Ù„Ø¹Ù†ÙˆØ§Ù† Ù…Ù† Ø­Ø³Ø§Ø¨ Ø±ÙŠÙ† Ø¨Ù†ÙØ³Ùƒ'></label><button>Ø­ÙØ¸ Ø¥Ø¹Ø¯Ø§Ø¯Ø§Øª Ø±ÙŠÙ†</button></form></article>
<article><h2>Ù…Ù„Ø®Øµ Ø§Ù„Ø³Ø¬Ù„Ø§Øª (USDT)</h2><table><tr><th>Ø§Ù„ØªØµÙ†ÙŠÙ</th><th>Ø§Ù„Ø¥Ø¬Ù…Ø§Ù„ÙŠ Ø§Ù„Ù…Ø³Ø¬Ù„</th></tr>{% for item in totals %}<tr><td>{{labels.get(item.category,item.category)}}</td><td>{{item.total}}</td></tr>{% endfor %}</table><small>Ù‡Ø°Ù‡ Ø³Ø¬Ù„Ø§Øª Ø¥Ø¯Ø§Ø±ÙŠØ© ØºÙŠØ± Ù…ØªØ­Ù‚Ù‘ÙŽÙ‚ Ù…Ù†Ù‡Ø§ Ø¨Ù†Ø¸Ø§Ù… Ø¯ÙØ¹ Ø£Ùˆ Ø­Ø³Ø§Ø¨Ø§Øª ØªØ¯Ø§ÙˆÙ„ Ù…Ù†ÙØµÙ„Ø©Ø› Ù„ÙŠØ³Øª Ø±ØµÙŠØ¯Ø§Ù‹ Ø£Ùˆ Ø¯Ø®Ù„Ø§Ù‹ ÙØ¹Ù„ÙŠØ§Ù‹ Ù…Ø¤ÙƒØ¯Ø§Ù‹. Ù„Ø§ ÙŠØªÙ… ØªØ­ÙˆÙŠÙ„ ÙƒÙ„ 100 Ø¯ÙˆÙ„Ø§Ø± ØªÙ„Ù‚Ø§Ø¦ÙŠØ§Ù‹.</small></article>
<article><h2>ðŸ›¡ï¸ Ø¯Ø¹Ù… Ø§Ù„Ù…Ø³ØªØ«Ù…Ø±ÙŠÙ†</h2><p>Ø§ÙØªØ­ Ø­Ø³Ø§Ø¨ Ø§Ù„Ù…Ø³ØªØ«Ù…Ø± Ù„Ù„Ù…Ø´Ø§Ù‡Ø¯Ø© Ø£Ùˆ Ù„ØªØ¹Ø¯ÙŠÙ„ Ø¨ÙŠØ§Ù†Ø§ØªÙ‡ Ø§Ù„Ø¥Ø¯Ø§Ø±ÙŠØ©. Ù„Ø§ ÙŠØªÙ… ØªØ³Ø¬ÙŠÙ„ Ø§Ù„Ø¯Ø®ÙˆÙ„ Ø¨ÙƒÙ„Ù…Ø© Ù…Ø±ÙˆØ±Ù‡ØŒ ÙˆÙ„Ø§ ÙŠØ³Ù…Ø­ Ù‡Ø°Ø§ Ø§Ù„ÙˆØ¶Ø¹ Ø¨Ø§Ù„ØªØ¯Ø§ÙˆÙ„ Ø£Ùˆ Ø§Ù„Ø³Ø­Ø¨.</p><p><a href='/owner/support'>ÙØªØ­ Ù„ÙˆØ­Ø© Ø¯Ø¹Ù… Ø§Ù„Ù…Ø³ØªØ«Ù…Ø±ÙŠÙ†</a></p></article><article><h2>Ø§Ù„Ù…Ø³ØªØ«Ù…Ø±ÙˆÙ† ÙˆØ§Ù„Ø§Ø´ØªØ±Ø§ÙƒØ§Øª</h2><table><tr><th>Ø±Ù‚Ù…</th><th>Ø§Ù„Ø¨Ø±ÙŠØ¯</th><th>Ø§Ù„Ø­Ø§Ù„Ø©</th><th>Ø§Ù†ØªÙ‡Ø§Ø¡ Ø§Ù„ØªØ¬Ø±Ø¨Ø©</th><th>Ø§Ù†ØªÙ‡Ø§Ø¡ Ø§Ù„Ù…Ø¯ÙÙˆØ¹</th><th>Ø§Ù„ØªØ³Ø¬ÙŠÙ„</th></tr>{% for u in users %}<tr><td>{{u.id}}</td><td>{{u.email}}</td><td>{{u.subscription_status}}</td><td>{{u.trial_end}}</td><td>{{u.paid_until or 'â€”'}}</td><td>{{u.created_at}}</td></tr>{% endfor %}</table></article>
<article><h2>Ø£Ø­Ø¯Ø« Ø³Ø¬Ù„Ø§Øª Ø§Ù„Ø£Ù…ÙˆØ§Ù„ ÙˆØ§Ù„Ø£Ø±Ø¨Ø§Ø­ ÙˆØ§Ù„Ø®Ø³Ø§Ø¦Ø±</h2><table><tr><th>Ø§Ù„Ù…Ø³ØªØ«Ù…Ø±</th><th>Ø§Ù„Ù†ÙˆØ¹</th><th>Ø§Ù„Ù…Ø¨Ù„Øº</th><th>Ø§Ù„Ù…Ø±Ø¬Ø¹</th><th>Ø§Ù„ØªØ§Ø±ÙŠØ®</th></tr>{% for r in records %}<tr><td>{{r.email}}</td><td>{{labels.get(r.category,r.category)}}</td><td>{{r.amount_usdt}}</td><td>{{r.reference or 'â€”'}}</td><td>{{r.created_at}}</td></tr>{% endfor %}</table><p><small>Ù„Ø§ ØªÙØ³Ø¬Ù‘ÙŽÙ„ Ø¹Ù…Ù„ÙŠØ§Øª Ù…Ø§Ù„ÙŠØ© Ø¥Ù„Ø§ Ø¨Ø¹Ø¯ Ø±Ø¨Ø· Ù…ØµØ¯Ø± Ù…ÙˆØ«ÙˆÙ‚ Ù„Ù„ØªØ­Ù‚Ù‚. Ù…Ø´Ø§Ø±ÙƒØ© Ø§Ù„Ø£Ø±Ø¨Ø§Ø­ 50% Ø¨Ù†Ø¯ Ù…Ù‚ØªØ±Ø­ ØºÙŠØ± Ù…ÙÙØ¹Ù‘Ù„.</small></p></article></main></html>"""

@app.route('/owner/investors')
@auth
def owner_investors():
    init_investor_admin()
    with db() as conn,conn.cursor(cursor_factory=RealDictCursor) as c:
        c.execute('SELECT * FROM owner_settings WHERE id=1'); settings=c.fetchone()
        c.execute('SELECT COUNT(*) AS n FROM investor_accounts');count=c.fetchone()['n']
        c.execute('SELECT id,email,subscription_status,trial_end,paid_until,created_at FROM investor_accounts ORDER BY id DESC LIMIT 300');users=c.fetchall()
        c.execute('SELECT category, SUM(amount_usdt) AS total FROM investor_financial_records GROUP BY category ORDER BY category');totals=c.fetchall()
        c.execute('SELECT r.category,r.amount_usdt,r.reference,r.created_at,a.email FROM investor_financial_records r JOIN investor_accounts a ON a.id=r.investor_id ORDER BY r.id DESC LIMIT 100');records=c.fetchall()
    labels={'SUBSCRIPTION':'Ø§Ø´ØªØ±Ø§ÙƒØ§Øª','INVESTMENT_DECLARED':'Ø§Ø³ØªØ«Ù…Ø§Ø± Ù…ÙØ¹Ù„Ù†','TRADE_PROFIT':'Ø±Ø¨Ø­ ØµÙÙ‚Ø©','TRADE_LOSS':'Ø®Ø³Ø§Ø±Ø© ØµÙÙ‚Ø©','PROFIT_SHARE_PROPOSED':'Ø­ØµØ© Ø£Ø±Ø¨Ø§Ø­ Ù…Ù‚ØªØ±Ø­Ø©'}
    return render_template_string(OWNER_PAGE,settings=settings,count=count,users=users,totals=totals,records=records,labels=labels,csrf=csrf())

@app.route('/owner/rain',methods=['POST'])
@auth
def owner_rain():
    check_csrf()
    network=request.form.get('network','').strip()
    address=request.form.get('address','').strip()
    if len(network)>50 or len(address)>256 or (address and not network):
        flash('Ø£Ø¯Ø®Ù„ Ø§Ù„Ø´Ø¨ÙƒØ© ÙˆØ§Ù„Ø¹Ù†ÙˆØ§Ù† Ø§Ù„ØµØ­ÙŠØ­ÙŠÙ†');return redirect(url_for('owner_investors'))
    init_investor_admin()
    with db() as conn,conn.cursor() as c:
        c.execute('UPDATE owner_settings SET rain_network=%s,rain_deposit_address=%s,updated_at=NOW() WHERE id=1',(network or None,address or None))
    flash('Ø­ÙÙØ¸Øª Ø¨ÙŠØ§Ù†Ø§Øª Ø±ÙŠÙ† Ù„Ù„Ø¥Ø¯Ø§Ø±Ø© ÙÙ‚Ø·Ø› Ù„Ù… ÙŠØªÙ… ØªÙØ¹ÙŠÙ„ Ø£ÙŠ ØªØ­ØµÙŠÙ„')
    return redirect(url_for('owner_investors'))


# Approval workflow only: NO transfer instructions, payments, custody or trading activation.
def init_approval_workflow():
    init_investor_admin()
    with db() as conn,conn.cursor() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS owner_payment_methods (
          id BIGSERIAL PRIMARY KEY, label TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
          enabled BOOLEAN NOT NULL DEFAULT TRUE, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
        c.execute("""CREATE TABLE IF NOT EXISTS investor_requests (
          id BIGSERIAL PRIMARY KEY, investor_id BIGINT NOT NULL REFERENCES investor_accounts(id),
          kind TEXT NOT NULL CHECK(kind IN ('SUBSCRIPTION','INVESTMENT')),
          amount_usdt NUMERIC(22,2), method_id BIGINT REFERENCES owner_payment_methods(id),
          status TEXT NOT NULL DEFAULT 'PENDING' CHECK(status IN ('PENDING','APPROVED','REJECTED')),
          created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), decided_at TIMESTAMPTZ,
          CONSTRAINT valid_request_amount CHECK ((kind='SUBSCRIPTION' AND amount_usdt IS NULL) OR (kind='INVESTMENT' AND amount_usdt>=50)))""")
        c.execute("ALTER TABLE investor_requests ADD COLUMN IF NOT EXISTS investment_mode TEXT")
        c.execute("""ALTER TABLE investor_requests DROP CONSTRAINT IF EXISTS investor_requests_investment_mode_check""")
        c.execute("""ALTER TABLE investor_requests ADD CONSTRAINT investor_requests_investment_mode_check CHECK (investment_mode IS NULL OR investment_mode IN ('POOLED','INDIVIDUAL','BOTH'))""")
        c.execute("ALTER TABLE investor_requests ADD COLUMN IF NOT EXISTS pooled_amount_usdt NUMERIC(22,2)")
        c.execute("ALTER TABLE investor_requests ADD COLUMN IF NOT EXISTS individual_amount_usdt NUMERIC(22,2)")
        c.execute("""ALTER TABLE investor_requests DROP CONSTRAINT IF EXISTS investor_requests_split_check""")
        c.execute("""ALTER TABLE investor_requests ADD CONSTRAINT investor_requests_split_check CHECK (
          investment_mode != 'BOTH' OR (kind='INVESTMENT' AND pooled_amount_usdt>=0.01 AND individual_amount_usdt>=0.01
          AND pooled_amount_usdt+individual_amount_usdt=amount_usdt))""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_investor_requests_owner ON investor_requests(status,created_at DESC)")

APPROVAL_STYLE="""<style>body{font-family:system-ui;background:#101827;color:#f2f5fc;margin:0;padding:18px}main{max-width:1050px;margin:auto}article{background:#213047;border-radius:12px;padding:16px;margin:12px 0;overflow:auto}input,select,button{font:inherit;padding:10px;margin:6px;max-width:100%}button{background:#32b99b;border:0;border-radius:7px}table{border-collapse:collapse;width:100%}td,th{padding:9px;border-bottom:1px solid #45556d;text-align:right}a{color:#8adfff}small{color:#c7d1df}</style>"""

def kwt(dt):
    if not dt:return 'â€”'
    from zoneinfo import ZoneInfo
    return dt.astimezone(ZoneInfo('Asia/Kuwait')).strftime('%Y-%m-%d %H:%M:%S')

@app.route('/investors/requests')
def investor_requests_page():
    if not session.get('investor_id'):return redirect(url_for('investors'))
    init_approval_workflow()
    with db() as conn,conn.cursor(cursor_factory=RealDictCursor) as c:
        c.execute('SELECT id,label,description FROM owner_payment_methods WHERE enabled=TRUE ORDER BY id DESC');methods=c.fetchall()
        c.execute('SELECT kind,status,amount_usdt,investment_mode,pooled_amount_usdt,individual_amount_usdt,created_at,decided_at FROM investor_requests WHERE investor_id=%s ORDER BY id DESC LIMIT 100',(session['investor_id'],));rows=c.fetchall()
    html="""<!doctype html><html lang=ar dir=rtl><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>Ø·Ù„Ø¨Ø§Øª Ø§Ù„Ù…Ø³ØªØ«Ù…Ø±</title>"""+APPROVAL_STYLE+"""<main><h1>Ø·Ù„Ø¨Ø§Øª Ø§Ù„Ø§Ø´ØªØ±Ø§Ùƒ ÙˆØ§Ù„Ø§Ø³ØªØ«Ù…Ø§Ø±</h1><p><a href='/investors'>Ø§Ù„Ø¹ÙˆØ¯Ø© Ù„ØµÙØ­Ø© Ø§Ù„Ù…Ø³ØªØ«Ù…Ø±</a></p><article><strong>Ø§Ù„Ø·Ù„Ø¨Ø§Øª Ù„Ù„Ù…Ø±Ø§Ø¬Ø¹Ø© ÙÙ‚Ø·.</strong> Ù„Ø§ ØªØ±Ø³Ù„ Ø£Ù…ÙˆØ§Ù„Ø§Ù‹ ÙˆÙ„Ø§ ØªÙˆØ¬Ø¯ ØªØ¹Ù„ÙŠÙ…Ø§Øª ØªØ­ÙˆÙŠÙ„ Ø£Ùˆ Ø¯ÙØ¹ Ù…ÙØ¹Ù‘Ù„Ø©. Ø§Ù„Ù…ÙˆØ§ÙÙ‚Ø© Ù„Ø§ ØªØ«Ø¨Øª Ø§Ø³ØªÙ„Ø§Ù… Ø§Ù„Ù…Ø§Ù„ ÙˆÙ„Ø§ ØªØ¨Ø¯Ø£ ØªØ¯Ø§ÙˆÙ„Ø§Ù‹.</article>{% for m in get_flashed_messages() %}<article>{{m}}</article>{% endfor %}<article><h2>Ø·Ù„Ø¨ Ù…Ø±Ø§Ø¬Ø¹Ø© Ø§Ø´ØªØ±Ø§Ùƒ</h2><form method=post action='/investors/requests/new'><input type=hidden name=csrf value='{{csrf}}'><input type=hidden name=kind value=SUBSCRIPTION><label>Ø·Ø±ÙŠÙ‚Ø© Ø§Ù„Ø¯ÙØ¹ Ø§Ù„Ù…ÙØ¶Ù„Ø© (Ù„Ù„ØªØ®Ø·ÙŠØ· ÙÙ‚Ø·)</label><select name=method_id><option value=''>Ù„Ù… Ø£Ø­Ø¯Ø¯</option>{% for m in methods %}<option value='{{m.id}}'>{{m.label}}</option>{% endfor %}</select><button>Ø¥Ø±Ø³Ø§Ù„ Ø·Ù„Ø¨ Ø§Ù„Ø§Ø´ØªØ±Ø§Ùƒ Ù„Ù„Ù…Ø±Ø§Ø¬Ø¹Ø©</button></form></article><article><h2>Ø·Ù„Ø¨ Ù…Ø±Ø§Ø¬Ø¹Ø© Ù…Ø¨Ù„Øº Ø§Ø³ØªØ«Ù…Ø§Ø±</h2><form method=post action='/investors/requests/new'><input type=hidden name=csrf value='{{csrf}}'><input type=hidden name=kind value=INVESTMENT><label>Ø§Ù„Ù…Ø¨Ù„Øº Ø§Ù„Ù…Ù‚ØªØ±Ø­ (50 USDT Ø¹Ù„Ù‰ Ø§Ù„Ø£Ù‚Ù„)</label><input name=amount type=number min=50 step=.01 required><label>Ø·Ø±ÙŠÙ‚Ø© Ø§Ù„Ø§Ø³ØªØ«Ù…Ø§Ø±</label><select name=investment_mode required><option value=POOLED>Ù…Ø­ÙØ¸Ø© Ù…Ø´ØªØ±ÙƒØ© â€” ØªÙˆØ²ÙŠØ¹ Ø§Ù„Ù†ØªØ§Ø¦Ø¬ Ø­Ø³Ø¨ Ø§Ù„Ø­ØµØµ</option><option value=INDIVIDUAL>ØµÙÙ‚Ø§Øª Ù…Ø³ØªÙ‚Ù„Ø© Ù…Ø­Ø§Ø³Ø¨ÙŠÙ‹Ø§ â€” Ø³Ø¬Ù„ Ù…Ø®ØµØµ Ù„ÙŠ</option><option value=BOTH>Ø§Ù„Ù†Ø¸Ø§Ù…Ø§Ù† Ù…Ø¹Ù‹Ø§ â€” Ø£ÙˆØ²Ø¹ Ø§Ù„Ù…Ø¨Ù„Øº Ø¨ÙŠÙ†Ù‡Ù…Ø§</option></select><label>Ø¥Ø°Ø§ Ø§Ø®ØªØ±Øª Ø§Ù„Ù†Ø¸Ø§Ù…ÙŠÙ†: Ø§Ù„Ù…Ø¨Ù„Øº Ù„Ù„Ù…Ø­ÙØ¸Ø© Ø§Ù„Ù…Ø´ØªØ±ÙƒØ© (USDT)</label><input name=pooled_amount type=number min=.01 step=.01 placeholder="Ù…Ø«Ø§Ù„: 30"><label>Ø§Ù„Ù…Ø¨Ù„Øº Ù„Ù„ØµÙÙ‚Ø§Øª Ø§Ù„ÙØ±Ø¯ÙŠØ© (USDT)</label><input name=individual_amount type=number min=.01 step=.01 placeholder="Ù…Ø«Ø§Ù„: 20"><small>Ø¹Ù†Ø¯ Ø§Ø®ØªÙŠØ§Ø± Ø§Ù„Ù†Ø¸Ø§Ù…ÙŠÙ† ÙŠØ¬Ø¨ Ø£Ù† ÙŠØ³Ø§ÙˆÙŠ Ù…Ø¬Ù…ÙˆØ¹ Ø§Ù„Ù…Ø¨Ù„ØºÙŠÙ† Ø¥Ø¬Ù…Ø§Ù„ÙŠ Ø§Ù„Ø§Ø³ØªØ«Ù…Ø§Ø±ØŒ Ø¨Ø­Ø¯ Ø£Ø¯Ù†Ù‰ Ø¥Ø¬Ù…Ø§Ù„ÙŠ 50 USDT.</small><p><small>Ø§Ù„Ø§Ø®ØªÙŠØ§Ø± Ù„Ù„Ù…Ø±Ø§Ø¬Ø¹Ø© ÙÙ‚Ø·ØŒ ÙˆÙ„Ø§ ÙŠÙÙØ¹Ù‘Ù„ ØªØ¯Ø§ÙˆÙ„Ù‹Ø§ Ø£Ùˆ Ø¥ÙŠØ¯Ø§Ø¹Ù‹Ø§.</small></p><button>Ø¥Ø±Ø³Ø§Ù„ Ø§Ù„Ù…Ø¨Ù„Øº Ù„Ù„Ù…Ø±Ø§Ø¬Ø¹Ø©</button></form></article><article><h2>Ø·Ø±Ù‚ Ø§Ù„Ø¯ÙØ¹ Ø§Ù„Ù…Ù‚ØªØ±Ø­Ø©</h2>{% for m in methods %}<p><b>{{m.label}}</b> â€” {{m.description}}</p>{% else %}<p>Ù„Ù… ØªÙØ¶Ù Ø·Ø±Ù‚ Ø¨Ø¹Ø¯.</p>{% endfor %}<small>Ù„Ø§ ØªØ¸Ù‡Ø± Ø¹Ù†Ø§ÙˆÙŠÙ† Ø§Ù„Ù…Ø­Ø§ÙØ¸ Ø£Ùˆ Ø¨ÙŠØ§Ù†Ø§Øª Ø§Ù„ØªØ­ÙˆÙŠÙ„ Ù‡Ù†Ø§ Ø­ØªÙ‰ ÙŠÙØ¬Ù‡Ø² Ù†Ø¸Ø§Ù… Ø¯ÙØ¹ Ù…Ø±Ø®Øµ ÙˆØ¢Ù…Ù†.</small></article><article><h2>Ø³Ø¬Ù„ Ø·Ù„Ø¨Ø§ØªÙŠ â€” Ø¨ØªÙˆÙ‚ÙŠØª Ø§Ù„ÙƒÙˆÙŠØª</h2><table><tr><th>Ø§Ù„Ù†ÙˆØ¹</th><th>Ø§Ù„Ù…Ø¨Ù„Øº</th><th>Ø§Ù„Ù†Ø¸Ø§Ù…</th><th>Ø§Ù„Ø­Ø§Ù„Ø©</th><th>Ø§Ù„Ø¥Ù†Ø´Ø§Ø¡</th><th>Ø§Ù„Ù‚Ø±Ø§Ø±</th></tr>{% for r in rows %}<tr><td>{{r.kind}}</td><td>{{r.amount_usdt or 'â€”'}}</td><td>{{'Ù…Ø´ØªØ±Ùƒ' if r.investment_mode=='POOLED' else 'Ù…Ø³ØªÙ‚Ù„ Ù…Ø­Ø§Ø³Ø¨ÙŠÙ‹Ø§' if r.investment_mode=='INDIVIDUAL' else 'Ø§Ù„Ù†Ø¸Ø§Ù…Ø§Ù† Ù…Ø¹Ù‹Ø§' if r.investment_mode=='BOTH' else 'â€”'}}{% if r.investment_mode=='BOTH' %} (Ù…Ø´ØªØ±Ùƒ {{r.pooled_amount_usdt}} / ÙØ±Ø¯ÙŠ {{r.individual_amount_usdt}}){% endif %}</td><td>{{r.status}}</td><td>{{kwt(r.created_at)}}</td><td>{{kwt(r.decided_at)}}</td></tr>{% endfor %}</table></article></main></html>"""
    return render_template_string(html,methods=methods,rows=rows,csrf=csrf(),kwt=kwt)

@app.route('/investors/requests/new',methods=['POST'])
def investor_request_new():
    if not session.get('investor_id'):return redirect(url_for('investors'))
    check_csrf();init_approval_workflow()
    kind=request.form.get('kind')
    if kind not in ('SUBSCRIPTION','INVESTMENT'):return 'Ù†ÙˆØ¹ Ø·Ù„Ø¨ ØºÙŠØ± ØµØ­ÙŠØ­',400
    amount=None;method_id=None;investment_mode=None;pooled_amount=None;individual_amount=None
    if kind=='INVESTMENT':
        try:amount=D(request.form.get('amount',''))
        except Exception:return 'Ù…Ø¨Ù„Øº ØºÙŠØ± ØµØ§Ù„Ø­',400
        if not amount.is_finite() or amount<50 or amount>D('100000000') or amount.as_tuple().exponent < -2:return 'Ø§Ù„Ù…Ø¨Ù„Øº ÙŠØ¬Ø¨ Ø£Ù† ÙŠÙƒÙˆÙ† 50 Ø¹Ù„Ù‰ Ø§Ù„Ø£Ù‚Ù„ ÙˆØ¨Ø¯Ù‚Ø© Ø³Ù†Øª',400
        investment_mode=request.form.get('investment_mode')
        if investment_mode not in ('POOLED','INDIVIDUAL','BOTH'):return 'Ø§Ø®ØªØ± Ø·Ø±ÙŠÙ‚Ø© Ø§Ù„Ø§Ø³ØªØ«Ù…Ø§Ø±',400
        if investment_mode=='BOTH':
            try:
                pooled_amount=D(request.form.get('pooled_amount',''))
                individual_amount=D(request.form.get('individual_amount',''))
            except Exception:return 'Ø£Ø¯Ø®Ù„ Ø§Ù„Ù…Ø¨Ù„Øº Ø§Ù„Ù…Ø®ØµØµ Ù„ÙƒÙ„ Ù†Ø¸Ø§Ù…',400
            for part in (pooled_amount,individual_amount):
                if not part.is_finite() or part<D('0.01') or part.as_tuple().exponent < -2:
                    return 'ÙŠØ¬Ø¨ Ø¥Ø¯Ø®Ø§Ù„ Ù…Ø¨Ù„Øº ØµØ­ÙŠØ­ Ù„ÙƒÙ„ Ù†Ø¸Ø§Ù… Ø¨Ø¯Ù‚Ø© Ø³Ù†Øª',400
            if pooled_amount+individual_amount!=amount:
                return 'Ù…Ø¬Ù…ÙˆØ¹ Ù…Ø¨Ø§Ù„Øº Ø§Ù„Ù†Ø¸Ø§Ù…ÙŠÙ† Ù„Ø§ ÙŠØ³Ø§ÙˆÙŠ Ø¥Ø¬Ù…Ø§Ù„ÙŠ Ø§Ù„Ø§Ø³ØªØ«Ù…Ø§Ø±',400
    else:
        raw=request.form.get('method_id','')
        if raw:
            if not raw.isdecimal():return 'Ø·Ø±ÙŠÙ‚Ø© ØºÙŠØ± ØµØ­ÙŠØ­Ø©',400
            method_id=int(raw)
            with db() as conn,conn.cursor() as c:
                c.execute('SELECT 1 FROM owner_payment_methods WHERE id=%s AND enabled=TRUE',(method_id,))
                if not c.fetchone():return 'Ø·Ø±ÙŠÙ‚Ø© ØºÙŠØ± Ù…ØªØ§Ø­Ø©',400
    with db() as conn,conn.cursor() as c:
        c.execute('INSERT INTO investor_requests(investor_id,kind,amount_usdt,method_id,investment_mode,pooled_amount_usdt,individual_amount_usdt) VALUES(%s,%s,%s,%s,%s,%s,%s)',(session['investor_id'],kind,amount,method_id,investment_mode,pooled_amount,individual_amount))
    flash('ØªÙ… Ø¥Ø±Ø³Ø§Ù„ Ø§Ù„Ø·Ù„Ø¨ Ù„Ù„Ù…Ø±Ø§Ø¬Ø¹Ø©ØŒ ÙˆÙ„Ø§ ÙŠÙØ·Ù„Ø¨ Ù…Ù†Ùƒ ØªØ­ÙˆÙŠÙ„ Ø£Ù…ÙˆØ§Ù„')
    return redirect(url_for('investor_requests_page'))

@app.route('/owner/requests')
@auth
def owner_requests_page():
    init_approval_workflow()
    with db() as conn,conn.cursor(cursor_factory=RealDictCursor) as c:
        c.execute('SELECT * FROM owner_payment_methods ORDER BY id DESC');methods=c.fetchall()
        c.execute('SELECT r.*,a.email,m.label AS method FROM investor_requests r JOIN investor_accounts a ON a.id=r.investor_id LEFT JOIN owner_payment_methods m ON m.id=r.method_id ORDER BY r.id DESC LIMIT 300');rows=c.fetchall()
    html="""<!doctype html><html lang=ar dir=rtl><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>Ù…ÙˆØ§ÙÙ‚Ø§Øª Ø§Ù„Ù…Ø³ØªØ«Ù…Ø±ÙŠÙ†</title>"""+APPROVAL_STYLE+"""<main><h1>ðŸ‡°ðŸ‡¼ Ø§Ù„Ù…ÙˆØ§ÙÙ‚Ø§Øª ÙˆØ·Ø±Ù‚ Ø§Ù„Ø¯ÙØ¹</h1><p><a href='/owner/investors'>Ù„ÙˆØ­Ø© Ø§Ù„Ù…Ø³ØªØ«Ù…Ø±ÙŠÙ†</a> | <a href="/owner/payouts">Ù…Ø³ØªØ­Ù‚Ø§ØªÙŠ ÙˆÙˆØ¬Ù‡Ø© Ø§Ù„ØªØ­ÙˆÙŠÙ„</a></p><article><strong>Ù†Ø¸Ø§Ù… Ù…ÙˆØ§ÙÙ‚Ø§Øª Ù…Ø¨Ø¯Ø¦ÙŠØ© ÙÙ‚Ø·.</strong> Ù„Ø§ ØªÙˆØ¬Ø¯ ØªØ­ÙˆÙŠÙ„Ø§Øª Ø£Ùˆ ØªÙØ¹ÙŠÙ„ Ø§Ø³ØªØ«Ù…Ø§Ø± Ø£Ùˆ Ø§Ù‚ØªØ·Ø§Ø¹ Ø£Ø±Ø¨Ø§Ø­. Ø¬Ù…ÙŠØ¹ Ø§Ù„Ø£ÙˆÙ‚Ø§Øª Ø¨ØªÙˆÙ‚ÙŠØª Ø§Ù„ÙƒÙˆÙŠØª Ù…Ø¹ Ø§Ù„Ø«ÙˆØ§Ù†ÙŠ.</article>{% for msg in get_flashed_messages() %}<article>{{msg}}</article>{% endfor %}<article><h2>Ø¥Ø¶Ø§ÙØ© Ø·Ø±ÙŠÙ‚Ø© Ø¯ÙØ¹ Ù„Ù„Ø¹Ø±Ø¶</h2><form method=post action='/owner/methods/add'><input type=hidden name=csrf value='{{csrf}}'><input name=label maxlength=80 placeholder='Ø§Ø³Ù… Ø§Ù„Ø·Ø±ÙŠÙ‚Ø© Ù…Ø«Ù„ Ø±ÙŠÙ† Ø£Ùˆ ØªØ­ÙˆÙŠÙ„ Ø¨Ù†ÙƒÙŠ' required><input name=description maxlength=200 placeholder='ÙˆØµÙ Ø¹Ø§Ù… ÙÙ‚Ø· â€” Ø¨Ø¯ÙˆÙ† Ø¨ÙŠØ§Ù†Ø§Øª ØªØ­ÙˆÙŠÙ„'><button>Ø¥Ø¶Ø§ÙØ©</button></form><table><tr><th>Ø§Ù„Ø·Ø±ÙŠÙ‚Ø©</th><th>Ø§Ù„ÙˆØµÙ</th><th>Ø§Ù„Ø­Ø§Ù„Ø©</th><th>ØªØ§Ø±ÙŠØ® Ø§Ù„Ø¥Ø¶Ø§ÙØ©</th><th>Ø¥Ø¬Ø±Ø§Ø¡</th></tr>{% for m in methods %}<tr><td>{{m.label}}</td><td>{{m.description}}</td><td>{{'Ù…Ø¹Ø±ÙˆØ¶Ø©' if m.enabled else 'Ù…Ø®ÙÙŠØ©'}}</td><td>{{kwt(m.created_at)}}</td><td><form method=post action='/owner/methods/toggle'><input type=hidden name=csrf value='{{csrf}}'><input type=hidden name=id value='{{m.id}}'><button>{{'Ø¥Ø®ÙØ§Ø¡' if m.enabled else 'Ø¥Ø¸Ù‡Ø§Ø±'}}</button></form></td></tr>{% endfor %}</table></article><article><h2>Ø·Ù„Ø¨Ø§Øª Ø§Ù„Ù…Ø³ØªØ«Ù…Ø±ÙŠÙ†</h2><table><tr><th>Ø§Ù„Ù…Ø³ØªØ«Ù…Ø±</th><th>Ø§Ù„Ù†ÙˆØ¹</th><th>Ø§Ù„Ù…Ø¨Ù„Øº USDT</th><th>Ù†Ø¸Ø§Ù… Ø§Ù„Ø§Ø³ØªØ«Ù…Ø§Ø±</th><th>Ø§Ù„Ø·Ø±ÙŠÙ‚Ø©</th><th>Ø§Ù„Ø­Ø§Ù„Ø©</th><th>ØªØ§Ø±ÙŠØ® Ø§Ù„Ø·Ù„Ø¨</th><th>ØªØ§Ø±ÙŠØ® Ø§Ù„Ù‚Ø±Ø§Ø±</th><th>Ø§Ù„Ù‚Ø±Ø§Ø±</th></tr>{% for r in rows %}<tr><td>{{r.email}}</td><td>{{r.kind}}</td><td>{{r.amount_usdt or 'â€”'}}</td><td>{{'Ù…Ø´ØªØ±Ùƒ' if r.investment_mode=='POOLED' else 'Ù…Ø³ØªÙ‚Ù„ Ù…Ø­Ø§Ø³Ø¨ÙŠÙ‹Ø§' if r.investment_mode=='INDIVIDUAL' else 'Ø§Ù„Ù†Ø¸Ø§Ù…Ø§Ù† Ù…Ø¹Ù‹Ø§' if r.investment_mode=='BOTH' else 'â€”'}}{% if r.investment_mode=='BOTH' %} (Ù…Ø´ØªØ±Ùƒ {{r.pooled_amount_usdt}} / ÙØ±Ø¯ÙŠ {{r.individual_amount_usdt}}){% endif %}</td><td>{{r.method or 'â€”'}}</td><td>{{r.status}}</td><td>{{kwt(r.created_at)}}</td><td>{{kwt(r.decided_at)}}</td><td>{% if r.status=='PENDING' %}<form method=post action='/owner/requests/decide'><input type=hidden name=csrf value='{{csrf}}'><input type=hidden name=id value='{{r.id}}'><button name=decision value=APPROVED>Ù…ÙˆØ§ÙÙ‚Ø© Ù…Ø¨Ø¯Ø¦ÙŠØ©</button><button name=decision value=REJECTED>Ø±ÙØ¶</button></form>{% endif %}</td></tr>{% endfor %}</table></article></main></html>"""
    return render_template_string(html,methods=methods,rows=rows,csrf=csrf(),kwt=kwt)

@app.route('/owner/methods/add',methods=['POST'])
@auth
def owner_method_add():
    check_csrf();init_approval_workflow()
    label=request.form.get('label','').strip();description=request.form.get('description','').strip()
    if not label or len(label)>80 or len(description)>200:return 'Ø§Ù„Ø¨ÙŠØ§Ù†Ø§Øª ØºÙŠØ± ØµØ­ÙŠØ­Ø©',400
    with db() as conn,conn.cursor() as c:c.execute('INSERT INTO owner_payment_methods(label,description) VALUES(%s,%s)',(label,description))
    return redirect(url_for('owner_requests_page'))

@app.route('/owner/methods/toggle',methods=['POST'])
@auth
def owner_method_toggle():
    check_csrf();init_approval_workflow()
    with db() as conn,conn.cursor() as c:c.execute('UPDATE owner_payment_methods SET enabled=NOT enabled WHERE id=%s',(request.form.get('id'),))
    return redirect(url_for('owner_requests_page'))

@app.route('/owner/requests/decide',methods=['POST'])
@auth
def owner_request_decide():
    check_csrf();init_approval_workflow()
    decision=request.form.get('decision')
    if decision not in ('APPROVED','REJECTED'):return 'Ù‚Ø±Ø§Ø± ØºÙŠØ± ØµØ§Ù„Ø­',400
    with db() as conn,conn.cursor() as c:
        c.execute("UPDATE investor_requests SET status=%s,decided_at=NOW() WHERE id=%s AND status='PENDING'",(decision,request.form.get('id')))
        if c.rowcount!=1:flash('Ø§Ù„Ø·Ù„Ø¨ ØºÙŠØ± Ù…ÙˆØ¬ÙˆØ¯ Ø£Ùˆ ØªÙ… Ø§ØªØ®Ø§Ø° Ù‚Ø±Ø§Ø± Ø¨Ø´Ø£Ù†Ù‡ Ù…Ø³Ø¨Ù‚Ø§Ù‹')
    return redirect(url_for('owner_requests_page'))


# Owner-only payout planning. This DOES NOT move funds or validate ownership of any funds.
def init_owner_payouts():
    with db() as conn, conn.cursor() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS owner_payout_settings (
            id INTEGER PRIMARY KEY CHECK(id=1), destination_type TEXT NOT NULL DEFAULT 'RAIN',
            rain_network TEXT, rain_address TEXT, bank_name TEXT, bank_iban TEXT,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
        c.execute("INSERT INTO owner_payout_settings(id) VALUES(1) ON CONFLICT(id) DO NOTHING")
        c.execute("""CREATE TABLE IF NOT EXISTS owner_entitlement_ledger (
            id BIGSERIAL PRIMARY KEY, source_type TEXT NOT NULL CHECK(source_type IN ('SUBSCRIPTION_NET','PROFIT_SHARE_NET','OWNER_TRADING_NET','ADJUSTMENT')),
            amount_usdt NUMERIC(22,8) NOT NULL, reference TEXT NOT NULL UNIQUE,
            verification_status TEXT NOT NULL DEFAULT 'UNVERIFIED',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
        c.execute("""CREATE TABLE IF NOT EXISTS owner_payout_requests (
            id BIGSERIAL PRIMARY KEY, amount_usdt NUMERIC(22,8) NOT NULL,
            destination_type TEXT NOT NULL, destination_masked TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'DRAFT',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")

OWNER_PAYOUT_HTML="""<!doctype html><html lang=ar dir=rtl><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>Ø§Ù„Ù…Ø³ØªØ­Ù‚Ø§Øª ÙˆØ§Ù„ØªØ­ÙˆÙŠÙ„Ø§Øª</title><style>body{font-family:system-ui;background:#101827;color:#fff;padding:18px}main{max-width:1000px;margin:auto}article{background:#203047;padding:18px;border-radius:12px;margin:14px 0}input,select,button{padding:11px;margin:5px;max-width:90%;border-radius:6px}table{width:100%;border-collapse:collapse}td,th{padding:8px;border-bottom:1px solid #607080;text-align:right}a{color:#8de1ff}small{color:#c8d3dd}</style><main><h1>ðŸ‡°ðŸ‡¼ Ù…Ø³ØªØ­Ù‚Ø§Øª ØµØ§Ø­Ø¨ Ø§Ù„Ø¨ÙˆØª</h1><p><a href='/owner/investors'>Ø¥Ø¯Ø§Ø±Ø© Ø§Ù„Ù…Ø³ØªØ«Ù…Ø±ÙŠÙ†</a> | <a href='/'>Ø§Ù„Ø±Ø¦ÙŠØ³ÙŠØ©</a></p>{% for m in get_flashed_messages() %}<article>{{m}}</article>{% endfor %}<article><h2>Ø­Ø¯ Ø§Ù„ØªØ­ÙˆÙŠÙ„ Ø§Ù„Ù…Ù‚ØªØ±Ø­: 100 USDT</h2><p>Ø§Ù„Ù…Ø³ØªØ­Ù‚Ø§Øª Ø§Ù„Ù…Ø¤ÙƒØ¯Ø© ÙˆØ§Ù„Ù…Ø³Ø¬Ù„Ø©: <b>{{balance}} USDT</b></p><p>Ø§Ù„Ù…Ø¨Ø§Ù„Øº ØºÙŠØ± Ø§Ù„Ù…ØªØ­Ù‚Ù‚ Ù…Ù†Ù‡Ø§ Ù„Ø§ ØªØ¯Ø®Ù„ ÙÙŠ Ø§Ù„Ø­Ø³Ø§Ø¨.</p><small>Ù„Ø§ ØªÙˆØ¬Ø¯ ØªØ­ÙˆÙŠÙ„Ø§Øª Ø¢Ù„ÙŠØ© Ø£Ùˆ Ø·Ù„Ø¨Ø§Øª Ø³Ø­Ø¨ ÙØ¹Ù„ÙŠØ© ÙÙŠ Ù‡Ø°Ù‡ Ø§Ù„Ù†Ø³Ø®Ø©. Ù„Ø§ ÙŠÙÙ†Ø´Ø£ Ø·Ù„Ø¨ Ø¯ÙØ¹ Ø¥Ù„Ø§ Ø¨Ø¹Ø¯ Ø¥Ø«Ø¨Ø§Øª Ø§Ù„Ù…ØµØ¯Ø± ÙˆØ§Ù„Ù…Ù„ÙƒÙŠØ© ÙˆØ§Ù„Ø±Ø³ÙˆÙ… ÙˆØ§Ù„Ù…ÙˆØ§ÙÙ‚Ø§Øª Ø§Ù„Ù†Ø¸Ø§Ù…ÙŠØ©.</small></article><article><h2>Ø§Ø®ØªÙŠØ§Ø± ÙˆØ¬Ù‡Ø© Ø§Ø³ØªÙ„Ø§Ù… Ù…Ø³ØªØ­Ù‚Ø§ØªÙƒ</h2><form method=post action='/owner/payout-settings'><input type=hidden name=csrf value='{{csrf}}'><label>Ø§Ù„ÙˆØ¬Ù‡Ø© <select name=destination_type><option value=RAIN {% if settings.destination_type=='RAIN' %}selected{% endif %}>Ø±ÙŠÙ†</option><option value=BANK {% if settings.destination_type=='BANK' %}selected{% endif %}>Ø­Ø³Ø§Ø¨ Ø¨Ù†ÙƒÙŠ</option></select></label><p>Ø´Ø¨ÙƒØ© Ø±ÙŠÙ† <input name=rain_network maxlength=50 value='{{settings.rain_network or ""}}' placeholder='Ø§Ù„Ø´Ø¨ÙƒØ©'></p><p>Ø¹Ù†ÙˆØ§Ù† Ø¥ÙŠØ¯Ø§Ø¹ Ø±ÙŠÙ† <input name=rain_address maxlength=256 value='{{settings.rain_address or ""}}' placeholder='Ø§Ù„Ø¹Ù†ÙˆØ§Ù†'></p><p>Ø§Ø³Ù… Ø§Ù„Ø¨Ù†Ùƒ <input name=bank_name maxlength=120 value='{{settings.bank_name or ""}}' placeholder='Ø§Ø³Ù… Ø§Ù„Ø¨Ù†Ùƒ'></p><p>IBAN <input name=bank_iban maxlength=34 value='{{settings.bank_iban or ""}}' placeholder='Ø±Ù‚Ù… Ø§Ù„Ø­Ø³Ø§Ø¨ Ø§Ù„Ø¯ÙˆÙ„ÙŠ'></p><button>Ø­ÙØ¸ Ø§Ù„ÙˆØ¬Ù‡Ø© ÙÙ‚Ø·</button></form><small>Ù‡Ø°Ù‡ Ø§Ù„Ø¨ÙŠØ§Ù†Ø§Øª Ø®Ø§ØµØ© Ø¨Ø§Ù„Ø¥Ø¯Ø§Ø±Ø© ÙˆÙ„Ø§ ØªÙØ±Ø³Ù„ Ø¥Ù„Ù‰ Binance Ø£Ùˆ Ø±ÙŠÙ† Ø£Ùˆ Ø§Ù„Ø¨Ù†Ùƒ. Ø§Ø­ÙØ¸Ù‡Ø§ ÙÙ‚Ø· Ø¹Ù„Ù‰ Ù…ÙˆÙ‚Ø¹ HTTPS Ø¢Ù…Ù†. Ù„Ø§ ØªØ¯Ø®Ù„ ÙƒÙ„Ù…Ø© Ù…Ø±ÙˆØ± Ø£Ùˆ Ø±Ù…Ø² ØªØ­Ù‚Ù‚ Ø£Ùˆ Ù…ÙØªØ§Ø­ Ù…Ø­ÙØ¸Ø©.</small></article><article><h2>Ø³Ø¬Ù„ Ø§Ù„Ù…Ø³ØªØ­Ù‚Ø§Øª Ø­Ø³Ø¨ Ø§Ù„Ù…ØµØ¯Ø±</h2><table><tr><th>Ø§Ù„Ù†ÙˆØ¹</th><th>Ø§Ù„Ù…Ø¨Ù„Øº</th><th>Ø­Ø§Ù„Ø© Ø§Ù„ØªØ­Ù‚Ù‚</th><th>Ø§Ù„Ù…Ø±Ø¬Ø¹</th><th>Ø§Ù„ÙˆÙ‚Øª (Ø§Ù„ÙƒÙˆÙŠØª)</th></tr>{% for r in rows %}<tr><td>{{r.source_type}}</td><td>{{r.amount_usdt}}</td><td>{{r.verification_status}}</td><td>{{r.reference}}</td><td>{{kwt(r.created_at)}}</td></tr>{% endfor %}</table></article></main></html>"""

@app.route('/owner/payouts')
@auth
def owner_payouts():
    init_owner_payouts()
    with db() as conn,conn.cursor(cursor_factory=RealDictCursor) as c:
        c.execute('SELECT * FROM owner_payout_settings WHERE id=1');settings=c.fetchone()
        c.execute("SELECT COALESCE(SUM(amount_usdt),0) AS balance FROM owner_entitlement_ledger WHERE verification_status='VERIFIED'");balance=c.fetchone()['balance']
        c.execute('SELECT * FROM owner_entitlement_ledger ORDER BY id DESC LIMIT 100');rows=c.fetchall()
    return render_template_string(OWNER_PAYOUT_HTML,settings=settings,balance=balance,rows=rows,csrf=csrf(),kwt=kwt)

@app.route('/owner/payout-settings',methods=['POST'])
@auth
def owner_payout_settings_save():
    check_csrf();init_owner_payouts()
    kind=request.form.get('destination_type','')
    network=request.form.get('rain_network','').strip();address=request.form.get('rain_address','').strip()
    bank=request.form.get('bank_name','').strip();iban=request.form.get('bank_iban','').strip().replace(' ','').upper()
    if kind not in ('RAIN','BANK') or len(network)>50 or len(address)>256 or len(bank)>120 or len(iban)>34:
        flash('Ø¨ÙŠØ§Ù†Ø§Øª Ø§Ù„ÙˆØ¬Ù‡Ø© ØºÙŠØ± ØµØ­ÙŠØ­Ø©');return redirect(url_for('owner_payouts'))
    if kind=='RAIN' and (not network or not address):
        flash('Ø£Ø¯Ø®Ù„ Ø¹Ù†ÙˆØ§Ù† Ø±ÙŠÙ† ÙˆØ§Ù„Ø´Ø¨ÙƒØ©');return redirect(url_for('owner_payouts'))
    if kind=='BANK' and (not bank or not iban or not iban.isalnum()):
        flash('Ø£Ø¯Ø®Ù„ Ø§Ø³Ù… Ø§Ù„Ø¨Ù†Ùƒ ÙˆØ±Ù‚Ù… IBAN');return redirect(url_for('owner_payouts'))
    with db() as conn,conn.cursor() as c:
        c.execute('UPDATE owner_payout_settings SET destination_type=%s,rain_network=%s,rain_address=%s,bank_name=%s,bank_iban=%s,updated_at=NOW() WHERE id=1',(kind,network,address,bank,iban))
    flash('ØªÙ… Ø­ÙØ¸ ÙˆØ¬Ù‡Ø© Ø§Ù„Ø§Ø³ØªÙ„Ø§Ù…. Ù„Ù… ÙŠØªÙ… Ø¥Ø±Ø³Ø§Ù„ Ø£ÙŠ Ø£Ù…ÙˆØ§Ù„.');return redirect(url_for('owner_payouts'))

@app.route('/toggle-bot', methods=['POST'])
@auth
def toggle_bot():
    """Persist the requested stop/resume state. Repeated clicks are idempotent."""
    try:
        check_csrf()
        action = request.form.get('action', '')
        if action not in ('stop', 'resume'):
            raise ValueError('Ø£Ù…Ø± Ø§Ù„Ø¥ÙŠÙ‚Ø§Ù ØºÙŠØ± ØµØ§Ù„Ø­')
        paused = action == 'stop'
        init()
        with db() as conn, conn.cursor() as c:
            c.execute('UPDATE bot_preferences SET is_paused=%s, updated_at=NOW() WHERE id=1', (paused,))
            if c.rowcount != 1:
                raise RuntimeError('ØªØ¹Ø°Ø± Ø­ÙØ¸ Ø­Ø§Ù„Ø© Ø§Ù„Ø¨ÙˆØª')
            if paused:
                c.execute("UPDATE bot_orders_v2 SET status='CANCELLED', note='Cancelled by admin stop' WHERE status='PENDING'")
        flash('ðŸ›‘ ØªÙ… Ø¥ÙŠÙ‚Ø§Ù Ø§Ù„ÙØ­Øµ ÙˆÙØªØ­ Ø§Ù„ØµÙÙ‚Ø§Øª Ø§Ù„Ø¬Ø¯ÙŠØ¯Ø©.' if paused else 'â–¶ï¸ ØªÙ… Ø§Ø³ØªØ¦Ù†Ø§Ù Ø§Ù„ÙØ­Øµ ÙÙ‚Ø·Ø› Ø§Ù„ØªØ¯Ø§ÙˆÙ„ Ø§Ù„Ø­Ù‚ÙŠÙ‚ÙŠ ÙŠØ¨Ù‚Ù‰ Ù…Ø­ÙƒÙˆÙ…Ø§Ù‹ Ø¨Ø¥Ø¹Ø¯Ø§Ø¯Ø§Øª Ø§Ù„Ø£Ù…Ø§Ù†.')
    except Exception:
        logging.exception('Failed to change bot pause state')
        flash('ØªØ¹Ø°Ø± ØªØºÙŠÙŠØ± Ø­Ø§Ù„Ø© Ø§Ù„Ø¨ÙˆØªØ› ØªØ­Ù‚Ù‚ Ù…Ù† Ù‚Ø§Ø¹Ø¯Ø© Ø§Ù„Ø¨ÙŠØ§Ù†Ø§Øª ÙˆØ³Ø¬Ù„Ø§Øª Render.')
    return redirect(url_for('home'))

@app.route('/health')
def health(): return {'status':'web_running','version':'V38','live_trading_enabled':LIVE,'execution_audited':EXECUTION_AUDITED,'trading_orders_enabled':False}

@app.route('/login',methods=['GET','POST'])
def login():
    if request.method=='POST':
        try:
            check_csrf()
            email=os.environ.get('ADMIN_EMAIL',''); ph=os.environ.get('ADMIN_PASSWORD_HASH','')
            if email and ph and hmac.compare_digest(request.form.get('email','').lower(),email.lower()) and check_password_hash(ph,request.form.get('password','')):
                session.clear(); session['admin']=True; session.permanent=True; csrf(); return redirect(url_for('home'))
            flash('Ø¨ÙŠØ§Ù†Ø§Øª Ø§Ù„Ø¯Ø®ÙˆÙ„ ØºÙŠØ± ØµØ­ÙŠØ­Ø©')
        except Exception as e: flash(str(e)[:160])
    return render_template_string(PAGE,logged=False,csrf=csrf(),live=LIVE)

@app.route('/logout')
def logout(): session.clear(); return redirect(url_for('login'))

@app.route('/')
@auth
def home():
    pending=[];recent=[];pos=[];close_suggestions=[];scan_state=None;bal='ØºÙŠØ± Ù…ØªØ§Ø­'
    try:
        init()
        with db() as conn,conn.cursor(cursor_factory=RealDictCursor) as c:
            c.execute("SELECT * FROM bot_orders_v2 WHERE status='PENDING' AND created_at>NOW()-INTERVAL '15 minutes' ORDER BY id DESC LIMIT 25");pending=c.fetchall()
            c.execute('SELECT * FROM bot_orders_v2 ORDER BY id DESC LIMIT 25');recent=c.fetchall()
            c.execute("SELECT * FROM bot_close_suggestions WHERE status='PENDING' AND created_at>NOW()-INTERVAL '10 minutes' ORDER BY estimated_net_roi DESC");close_suggestions=c.fetchall()
            c.execute('SELECT * FROM bot_scan_status WHERE id=1');scan_state=c.fetchone()
        # Display pending opportunities; revalidate risk at approval time.
        # Never perform one set of Binance account requests per row on page load.
    except Exception as e: flash('Neon: '+str(e)[:160])
    snapshot_at=None
    try:
        snapshot=_snapshot_get('account')
        if snapshot:
            bal=str(snapshot['payload'].get('balance','ØºÙŠØ± Ù…ØªØ§Ø­'))+' USDT'
            pos=snapshot['payload'].get('positions',[])
            snapshot_at=kwt(snapshot['captured_at'])
        else:
            bal='Ù„Ø§ ÙŠÙˆØ¬Ø¯ Ø±ØµÙŠØ¯ Ù…Ø¤ÙƒØ¯ Ù…Ø­ÙÙˆØ¸ Ø¨Ø¹Ø¯'
    except Exception:
        logging.exception('Could not load verified account snapshot')
        bal='ØªØ¹Ø°Ø± Ù‚Ø±Ø§Ø¡Ø© Ø¢Ø®Ø± Ø±ØµÙŠØ¯ Ù…Ø­ÙÙˆØ¸'
    return render_template_string(PAGE,logged=True,csrf=csrf(),live=LIVE,balance=bal,account_snapshot_at=snapshot_at,positions=pos,pending=pending,recent=recent,prefs=preferences() if scan_state is not None else None,close_suggestions=close_suggestions,scan_state=scan_state)


@app.route('/settings/api',methods=['POST'])
@auth
def update_api_credentials():
    try:
        check_csrf()
        master=os.environ.get('CREDENTIALS_ENCRYPTION_KEY','')
        if not master: raise ValueError('Ø£Ø¶Ù CREDENTIALS_ENCRYPTION_KEY Ø¥Ù„Ù‰ Render Ø£ÙˆÙ„Ù‹Ø§')
        f=Fernet(master.encode())
        key=request.form.get('api_key','').strip()
        secret=request.form.get('api_secret','').strip()
        if not key or not secret or len(key)>256 or len(secret)>256: raise ValueError('Ø§Ù„Ù…ÙØ§ØªÙŠØ­ ØºÙŠØ± ØµØ§Ù„Ø­Ø©')
        _assert_binance_available()
        # Test new credentials without replacing the active ones.
        params={'timestamp':int(time.time()*1000),'recvWindow':5000}
        q=urlencode(params)
        sig=hmac.new(secret.encode(),q.encode(),hashlib.sha256).hexdigest()
        _reserve_binance_request('/fapi/v2/account')
        r=requests.get(BASE+'/fapi/v2/account',params=q+'&signature='+sig,headers={'X-MBX-APIKEY':key},timeout=15)
        _record_limit(r)
        _observe_binance_weight(r)
        if not r.ok: raise ValueError('ÙØ´Ù„ Ø§Ø®ØªØ¨Ø§Ø± Binance. ØªØ­Ù‚Ù‚ Ù…Ù† ØµÙ„Ø§Ø­ÙŠØ§Øª Futures ÙˆØªÙ‚ÙŠÙŠØ¯ IP ÙˆØ§Ù„Ø¨ÙŠØ¦Ø© (ØªØ¬Ø±ÙŠØ¨ÙŠ/Ø­Ù‚ÙŠÙ‚ÙŠ).')
        init()
        with db() as conn,conn.cursor() as c:
            c.execute('''INSERT INTO bot_api_credentials(id,api_key_cipher,api_secret_cipher,updated_at)
                         VALUES(1,%s,%s,NOW()) ON CONFLICT(id) DO UPDATE
                         SET api_key_cipher=EXCLUDED.api_key_cipher,api_secret_cipher=EXCLUDED.api_secret_cipher,updated_at=NOW()''',
                      (psycopg2.Binary(f.encrypt(key.encode())),psycopg2.Binary(f.encrypt(secret.encode()))))
        flash('ØªÙ… Ø§Ø®ØªØ¨Ø§Ø± Ø§Ù„Ø§ØªØµØ§Ù„ ÙˆØ­ÙØ¸ Ù…ÙØ§ØªÙŠØ­ Binance Ø§Ù„Ø¬Ø¯ÙŠØ¯Ø© Ø¨Ø´ÙƒÙ„ Ù…Ø´ÙØ±.')
    except (InvalidToken,ValueError) as e:
        flash(str(e)[:180])
    except Exception:
        logging.exception('Credential update failed (no secrets logged)')
        flash('ØªØ¹Ø°Ø± Ø­ÙØ¸ Ø§Ù„Ù…ÙØ§ØªÙŠØ­. ØªØ­Ù‚Ù‚ Ù…Ù† Ø¥Ø¹Ø¯Ø§Ø¯Ø§Øª Neon ÙˆÙ…ÙØªØ§Ø­ Ø§Ù„ØªØ´ÙÙŠØ±.')
    return redirect(url_for('home'))

@app.route('/settings/preferences',methods=['POST'])
@auth
def save_preferences():
    try:
        check_csrf()
        exposure=D(request.form['max_exposure_pct'])
        leverage=int(request.form['max_leverage'])
        target=D(request.form['target_roi_pct'])
        if not (D('1')<=exposure<=D('100') and 1<=leverage<=150 and D('1')<=target<=D('100')):
            raise ValueError('Ø§Ù„Ø¥Ø¹Ø¯Ø§Ø¯Ø§Øª Ø®Ø§Ø±Ø¬ Ø§Ù„Ø­Ø¯ÙˆØ¯ Ø§Ù„Ù…Ø³Ù…ÙˆØ­Ø©')
        init()
        with db() as conn,conn.cursor() as c:
            c.execute('UPDATE bot_preferences SET max_exposure_pct=%s,max_leverage=%s,target_roi_pct=%s,updated_at=NOW() WHERE id=1',(exposure,leverage,target))
        flash('ØªÙ… Ø­ÙØ¸ Ø­Ø¯ÙˆØ¯ Ø§Ù„ØªØ­Ù„ÙŠÙ„. Ù‚Ø¯ Ù„Ø§ ÙŠØ³Ù…Ø­ Binance Ø¨Ø§Ù„Ø±Ø§ÙØ¹Ø© Ø§Ù„Ù…Ø·Ù„ÙˆØ¨Ø© Ù„ÙƒÙ„ Ø±Ù…Ø².')
    except Exception as e:
        flash('ØªØ¹Ø°Ø± Ø­ÙØ¸ Ø§Ù„Ø¥Ø¹Ø¯Ø§Ø¯Ø§Øª: '+str(e)[:150])
    return redirect(url_for('home'))

@app.route('/scan',methods=['POST'])
@auth
def scan():
    try:
        check_csrf()
        if not BINANCE_MARKET_SCANS_ENABLED: raise ValueError('ÙØ­Øµ Binance Ù…Ø¹Ø·Ù„ Ø§Ø­ØªØ±Ø§Ø²ÙŠÙ‹Ø§ Ù…Ù† Ø¥Ø¹Ø¯Ø§Ø¯Ø§Øª Render')
        init()
        if is_bot_paused(): raise ValueError('Ø§Ù„Ø¨ÙˆØª Ù…ØªÙˆÙ‚Ù Ø¨Ø£Ù…Ø± Ø§Ù„Ù…Ø³Ø¤ÙˆÙ„')
        count=0
        for sym in SYMBOLS:
            try:
                symbol_info(sym); p=proposal(sym)
                if not p: continue
                direction,entry,stop,take=p
                feasibility(sym,entry,stop,take)
                with db() as conn,conn.cursor() as c:
                    c.execute("SELECT 1 FROM bot_orders_v2 WHERE symbol=%s AND status IN ('PENDING','SUBMITTING','OPEN','CLOSING') LIMIT 1",(sym,))
                    if c.fetchone(): continue
                    c.execute('INSERT INTO bot_orders_v2(symbol,direction,entry_reference,stop_reference,take_reference) VALUES(%s,%s,%s,%s,%s)',(sym,direction,entry,stop,take));count+=1
            except Exception as e: logging.warning('scan %s: %s',sym,str(e)[:180])
        flash(f'Ø£ÙÙ†Ø´Ø¦Øª {count} ÙØ±Øµ. Ø§Ù„ØªØ­Ù„ÙŠÙ„ Ù„ÙŠØ³ Ø¶Ù…Ø§Ù†Ù‹Ø§ Ù„Ù„Ø±Ø¨Ø­.')
    except Exception as e: flash(str(e)[:180])
    return redirect(url_for('home'))

@app.route('/reject/<int:order_id>',methods=['POST'])
@auth
def reject(order_id):
    try:
        check_csrf()
        with db() as conn,conn.cursor() as c: c.execute("UPDATE bot_orders_v2 SET status='REJECTED' WHERE id=%s AND status='PENDING'",(order_id,))
        flash('ØªÙ… Ø±ÙØ¶ Ø§Ù„ÙØ±ØµØ©')
    except Exception as e: flash(str(e)[:180])
    return redirect(url_for('home'))

@app.route('/approve/<int:order_id>',methods=['POST'])
@auth
def approve(order_id):
    try:
        check_csrf()
        if is_bot_paused(): raise ValueError('Ø§Ù„Ø¨ÙˆØª Ù…ØªÙˆÙ‚Ù Ø¨Ø£Ù…Ø± Ø§Ù„Ù…Ø³Ø¤ÙˆÙ„')
        if not LIVE or not EXECUTION_AUDITED: raise ValueError('ØªÙ†ÙÙŠØ° Ø§Ù„ØªØ¯Ø§ÙˆÙ„ Ù…Ù‚ÙÙ„ Ù„Ø­ÙŠÙ† Ø§Ø®ØªØ¨Ø§Ø± Ø§Ù„Ø£ÙˆØ§Ù…Ø± ÙˆØ§Ø¹ØªÙ…Ø§Ø¯ EXECUTION_AUDITED')
        init()
        with db() as conn,conn.cursor(cursor_factory=RealDictCursor) as c:
            c.execute("SELECT COUNT(*) AS n FROM bot_orders_v2 WHERE created_at >= date_trunc('day',NOW() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC' AND status IN ('SUBMITTING','OPEN','CLOSING','CLOSED','ERROR')")
            if c.fetchone()['n']>=100: raise ValueError('Ø¨Ù„ØºØª Ø§Ù„Ø­Ø¯ Ø§Ù„ÙŠÙˆÙ…ÙŠ 100 Ù…Ø­Ø§ÙˆÙ„Ø© ÙØªØ­')
            c.execute("UPDATE bot_orders_v2 SET status='SUBMITTING' WHERE id=%s AND status='PENDING' RETURNING *",(order_id,))
            row=c.fetchone()
            if not row: raise ValueError('Ø³Ø¨Ù‚ ØªÙ†ÙÙŠØ° Ø£Ùˆ Ø±ÙØ¶ Ù‡Ø°Ù‡ Ø§Ù„ÙØ±ØµØ©')
        try:
            order,protect,qty=entry_order(row)
            with db() as conn,conn.cursor() as c:
                c.execute("UPDATE bot_orders_v2 SET status='OPEN',order_id=%s,stop_algo_id=%s,quantity=%s,entry_price=%s,note='Stop placed' WHERE id=%s",(str(order.get('orderId')),str(protect.get('algoId')),qty,order.get('avgPrice','0'),order_id))
            flash('ØªÙ… Ø¥Ø±Ø³Ø§Ù„ Ø§Ù„ÙØªØ­ ÙˆØªØ£ÙƒÙŠØ¯ Ø£Ù…Ø± ÙˆÙ‚Ù Ø§Ù„Ø®Ø³Ø§Ø±Ø©. ØªØ­Ù‚Ù‚ Ù…Ù† Binance.')
        except Exception as e:
            with db() as conn,conn.cursor() as c: c.execute("UPDATE bot_orders_v2 SET status='REQUIRES_REVIEW',note=%s WHERE id=%s",(str(e)[:400],order_id))
            raise
    except Exception as e: flash('Ù„Ù… ÙŠÙƒØªÙ…Ù„ ÙØªØ­ Ø§Ù„ØµÙÙ‚Ø©: '+str(e)[:220])
    return redirect(url_for('home'))

@app.route('/close/<symbol>',methods=['POST'])
@auth
def close_position(symbol):
    try:
        check_csrf()
        if not LIVE: raise ValueError('Ø§Ù„ØªØ¯Ø§ÙˆÙ„ Ø§Ù„Ø­Ù‚ÙŠÙ‚ÙŠ Ù…Ø¹Ø·Ù„')
        if symbol not in SYMBOLS: raise ValueError('Ø±Ù…Ø² ØºÙŠØ± Ù…Ø¹ØªÙ…Ø¯')
        ps=[p for p in positions() if p['symbol']==symbol]
        if not ps: raise ValueError('Ø§Ù„Ù…Ø±ÙƒØ² Ù…ØºÙ„Ù‚ Ø¨Ø§Ù„ÙØ¹Ù„')
        p=ps[0]
        if p.get('positionSide') not in ('BOTH',None): raise ValueError('ÙˆØ¶Ø¹ Hedge ØºÙŠØ± Ù…Ø¯Ø¹ÙˆÙ…')
        amt=D(p['positionAmt'])
        order=signed('POST','/fapi/v1/order',{'symbol':symbol,'side':'SELL' if amt>0 else 'BUY',
            'type':'MARKET','quantity':strnum(abs(amt)),'reduceOnly':'true','newOrderRespType':'RESULT'})
        with db() as conn,conn.cursor(cursor_factory=RealDictCursor) as c:
            c.execute("SELECT id,stop_algo_id FROM bot_orders_v2 WHERE symbol=%s AND status='OPEN' ORDER BY id DESC LIMIT 1",(symbol,))
            tracked=c.fetchone()
        warning=''
        if tracked and tracked['stop_algo_id']:
            try: signed('DELETE','/fapi/v1/algoOrder',{'symbol':symbol,'algoId':tracked['stop_algo_id']})
            except Exception as e: warning=' ØªÙ†Ø¨ÙŠÙ‡: Ù„Ù… Ù†ØªØ£ÙƒØ¯ Ù…Ù† Ø¥Ù„ØºØ§Ø¡ ÙˆÙ‚Ù Ø§Ù„Ø®Ø³Ø§Ø±Ø©Ø› Ø±Ø§Ø¬Ø¹ Ø£ÙˆØ§Ù…Ø± Binance: '+str(e)[:90]
        with db() as conn,conn.cursor() as c:
            c.execute("UPDATE bot_orders_v2 SET status='CLOSED',close_id=%s,note=%s WHERE symbol=%s AND status='OPEN'",(str(order.get('orderId')),'Manual close'+warning,symbol))
        flash('Ø£ÙØ±Ø³Ù„ Ø£Ù…Ø± Ø§Ù„Ø¥ØºÙ„Ø§Ù‚. Ø±Ø§Ø¬Ø¹ Binance Ù„Ù„ØªØ£ÙƒØ¯ Ù…Ù† Ø§Ù„ØªÙ†ÙÙŠØ°.'+warning)
    except Exception as e: flash('ØªØ¹Ø°Ø± Ø§Ù„Ø¥ØºÙ„Ø§Ù‚: '+str(e)[:200])
    return redirect(url_for('home'))




# V31: Owner support mode. Never impersonate an investor's authenticated session.
# Support is a distinct, read-only view, with narrowly scoped audited admin edits.
def init_owner_support():
    init_investors()
    with db() as conn, conn.cursor() as c:
        c.execute("ALTER TABLE investor_accounts ADD COLUMN IF NOT EXISTS display_name TEXT")
        c.execute("ALTER TABLE investor_accounts ADD COLUMN IF NOT EXISTS phone TEXT")
        c.execute("ALTER TABLE investor_accounts ADD COLUMN IF NOT EXISTS bot_paused BOOLEAN NOT NULL DEFAULT TRUE")
        c.execute("""CREATE TABLE IF NOT EXISTS owner_support_audit (
            id BIGSERIAL PRIMARY KEY, investor_id BIGINT NOT NULL REFERENCES investor_accounts(id),
            action TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")

SUPPORT_CSS="""<style>body{font:16px system-ui;background:#101827;color:#f1f5ff;padding:16px}main{max-width:1050px;margin:auto}article{background:#203047;border-radius:12px;padding:18px;margin:15px 0;overflow:auto}a{color:#82deff}table{width:100%;border-collapse:collapse}th,td{padding:9px;text-align:right;border-bottom:1px solid #52617a}input,select,button{padding:11px;margin:6px;border-radius:8px;font:inherit;max-width:95%}button{background:#39c2a2;border:0}label{display:block}small{color:#cbd5e1}</style>"""

@app.route('/owner/support')
@auth
def owner_support_list():
    init_owner_support()
    with db() as conn,conn.cursor(cursor_factory=RealDictCursor) as c:
        c.execute("""SELECT id,email,display_name,phone,subscription_status,bot_paused,created_at
                     FROM investor_accounts ORDER BY id DESC LIMIT 300""")
        users=c.fetchall()
    html="""<!doctype html><html lang=ar dir=rtl><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"""+SUPPORT_CSS+"""<main><h1>ðŸ›¡ï¸ Ø¯Ø¹Ù… Ø§Ù„Ù…Ø³ØªØ«Ù…Ø±ÙŠÙ† â€” Ø­Ø³Ø§Ø¨ Ø§Ù„Ù…Ø¯ÙŠØ±</h1><p><a href='/owner/investors'>Ø§Ù„Ø¹ÙˆØ¯Ø© Ù„Ù„Ù…Ø§Ù„Ùƒ</a></p><article><p>Ù‡Ø°Ø§ Ø§Ù„ÙˆØ¶Ø¹ Ù„Ø§ ÙŠØ¯Ø®Ù„ Ø¨Ø¬Ù„Ø³Ø© Ø§Ù„Ù…Ø³ØªØ«Ù…Ø± ÙˆÙ„Ø§ ÙŠØ³Ù…Ø­ Ø¨ÙØªØ­ Ø£Ùˆ Ø¥ØºÙ„Ø§Ù‚ ØµÙÙ‚Ø§Øª Ø£Ùˆ Ø³Ø­Ø¨ Ø£Ù…ÙˆØ§Ù„.</p><table><tr><th>Ø§Ù„Ø§Ø³Ù…</th><th>Ø§Ù„Ø¨Ø±ÙŠØ¯</th><th>Ø§Ù„Ù‡Ø§ØªÙ</th><th>Ø§Ù„Ø§Ø´ØªØ±Ø§Ùƒ</th><th>Ø§Ù„Ø¨ÙˆØª</th><th>Ø§Ù„Ø¥Ø¬Ø±Ø§Ø¡</th></tr>{% for u in users %}<tr><td>{{u.display_name or 'â€”'}}</td><td>{{u.email}}</td><td>{{u.phone or 'â€”'}}</td><td>{{u.subscription_status}}</td><td>{{'Ù…ØªÙˆÙ‚Ù' if u.bot_paused else 'Ù…Ø³Ù…ÙˆØ­ Ø¥Ø¯Ø§Ø±ÙŠØ§Ù‹'}}</td><td><a href='{{url_for("owner_support_detail",investor_id=u.id)}}'>Ù…Ø´Ø§Ù‡Ø¯Ø© ÙˆØªØ¹Ø¯ÙŠÙ„</a></td></tr>{% endfor %}</table></article></main></html>"""
    return render_template_string(html,users=users)

@app.route('/owner/support/<int:investor_id>')
@auth
def owner_support_detail(investor_id):
    init_owner_support();init_approval_workflow()
    with db() as conn,conn.cursor(cursor_factory=RealDictCursor) as c:
        c.execute("""SELECT id,email,display_name,phone,subscription_status,trial_end,paid_until,
                     bot_paused,created_at FROM investor_accounts WHERE id=%s""",(investor_id,))
        user=c.fetchone()
        if not user:return 'Ø§Ù„Ù…Ø³ØªØ«Ù…Ø± ØºÙŠØ± Ù…ÙˆØ¬ÙˆØ¯',404
        c.execute("""SELECT id,kind,status,amount_usdt,investment_mode,pooled_amount_usdt,
                     individual_amount_usdt,created_at FROM investor_requests
                     WHERE investor_id=%s ORDER BY id DESC LIMIT 40""",(investor_id,))
        requests_rows=c.fetchall()
        c.execute("""SELECT action,detail,created_at FROM owner_support_audit
                     WHERE investor_id=%s ORDER BY id DESC LIMIT 30""",(investor_id,))
        audit=c.fetchall()
    html="""<!doctype html><html lang=ar dir=rtl><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"""+SUPPORT_CSS+"""<main><h1>ðŸ›¡ï¸ ÙˆØ¶Ø¹ Ø¯Ø¹Ù… Ø§Ù„Ù…Ø³ØªØ«Ù…Ø± #{{u.id}}</h1><p><a href='/owner/support'>Ø§Ù„Ø¹ÙˆØ¯Ø© Ø¥Ù„Ù‰ Ù‚Ø§Ø¦Ù…Ø© Ø§Ù„Ù…Ø³ØªØ«Ù…Ø±ÙŠÙ†</a> | <a href='/owner/investors'>Ù„ÙˆØ­Ø© Ø§Ù„Ù…Ø§Ù„Ùƒ</a></p>{% for msg in get_flashed_messages() %}<article>{{msg}}</article>{% endfor %}<article><h2>ÙˆØ¶Ø¹ Ø§Ù„Ù…Ø´Ø§Ù‡Ø¯Ø©</h2><p>Ø§Ù„Ø¨Ø±ÙŠØ¯: {{u.email}} â€” Ø§Ù„Ø§Ø³Ù…: {{u.display_name or 'â€”'}} â€” Ø§Ù„Ù‡Ø§ØªÙ: {{u.phone or 'â€”'}}</p><p>Ø§Ù„Ø§Ø´ØªØ±Ø§Ùƒ: {{u.subscription_status}} | Ù†Ù‡Ø§ÙŠØ© Ø§Ù„ØªØ¬Ø±Ø¨Ø©: {{u.trial_end}} | Ù†Ù‡Ø§ÙŠØ© Ø§Ù„Ù…Ø¯ÙÙˆØ¹: {{u.paid_until or 'â€”'}}</p><p><small>Ø§Ù„Ø¨ÙŠØ§Ù†Ø§Øª Ø§Ù„Ù…Ø§Ù„ÙŠØ© Ø£Ø¯Ù†Ø§Ù‡ Ø·Ù„Ø¨Ø§Øª ÙˆØªØµØ±ÙŠØ­Ø§Øª ÙˆÙ„ÙŠØ³Øª Ø£Ø±ØµØ¯Ø© Binance Ù…Ø¤ÙƒØ¯Ø©. Ù„Ø§ ØªØ¸Ù‡Ø± ÙƒÙ„Ù…Ø§Øª Ø§Ù„Ù…Ø±ÙˆØ± Ø£Ùˆ Ù…ÙØ§ØªÙŠØ­ API.</small></p><h3>Ø·Ù„Ø¨Ø§Øª Ø§Ù„Ù…Ø³ØªØ«Ù…Ø±</h3><table><tr><th>Ø§Ù„Ù†ÙˆØ¹</th><th>Ø§Ù„Ù†Ø¸Ø§Ù…</th><th>Ø§Ù„Ù…Ø¨Ù„Øº</th><th>Ø§Ù„Ø­Ø§Ù„Ø©</th><th>Ø§Ù„ØªØ§Ø±ÙŠØ®</th></tr>{% for r in reqs %}<tr><td>{{r.kind}}</td><td>{{r.investment_mode or 'â€”'}}</td><td>{{r.amount_usdt or 'â€”'}}</td><td>{{r.status}}</td><td>{{r.created_at}}</td></tr>{% else %}<tr><td colspan=5>Ù„Ø§ ØªÙˆØ¬Ø¯ Ø·Ù„Ø¨Ø§Øª</td></tr>{% endfor %}</table></article><article><h2>ÙˆØ¶Ø¹ Ø§Ù„ØªØ¹Ø¯ÙŠÙ„ Ø§Ù„Ø¥Ø¯Ø§Ø±ÙŠ</h2><form method=post action='{{url_for("owner_support_update",investor_id=u.id)}}'><input type=hidden name=csrf value='{{csrf}}'><label>Ø§Ù„Ø§Ø³Ù… <input name=display_name maxlength=120 value='{{u.display_name or ""}}'></label><label>Ø§Ù„Ù‡Ø§ØªÙ <input name=phone maxlength=30 value='{{u.phone or ""}}'></label><label>Ø­Ø§Ù„Ø© Ø§Ù„Ø§Ø´ØªØ±Ø§Ùƒ <select name=subscription_status>{% for state in ['TRIAL','ACTIVE','EXPIRED','SUSPENDED'] %}<option value='{{state}}' {% if u.subscription_status==state %}selected{% endif %}>{{state}}</option>{% endfor %}</select></label><label>Ø¥Ø¹Ø¯Ø§Ø¯ Ø§Ù„Ù…Ø³ØªØ«Ù…Ø±: <select name=bot_paused><option value=1 {% if u.bot_paused %}selected{% endif %}>Ø¥ÙŠÙ‚Ø§Ù Ø¨ÙˆØª Ø§Ù„Ù…Ø³ØªØ«Ù…Ø±</option><option value=0 {% if not u.bot_paused %}selected{% endif %}>Ø§Ù„Ø³Ù…Ø§Ø­ Ø¥Ø¯Ø§Ø±ÙŠÙ‹Ø§ (Ù„Ø§ ÙŠÙØ¹Ù„ Ø§Ù„ØªØ¯Ø§ÙˆÙ„)</option></select></label><button type=submit>Ø­ÙØ¸ Ø§Ù„ØªØ¹Ø¯ÙŠÙ„ ÙˆØªØ³Ø¬ÙŠÙ„Ù‡</button></form><small>ØªØºÙŠÙŠØ± Ø§Ù„Ø§Ø´ØªØ±Ø§Ùƒ Ù„Ø§ ÙŠØ«Ø¨Øª Ø¯ÙØ¹Ù‹Ø§ Ø£Ùˆ ÙŠØ­Ø±Ùƒ Ø£Ù…ÙˆØ§Ù„Ù‹Ø§. Ø§Ù„Ø³Ù…Ø§Ø­ Ø§Ù„Ø¥Ø¯Ø§Ø±ÙŠ Ù„Ø§ ÙŠÙ…Ù†Ø­ ØªÙÙˆÙŠØ¶ ØªØ¯Ø§ÙˆÙ„.</small></article><article><h2>Ø³Ø¬Ù„ Ø§Ù„Ø¥Ø¬Ø±Ø§Ø¡Ø§Øª Ø§Ù„Ø¥Ø¯Ø§Ø±ÙŠØ©</h2><table><tr><th>Ø§Ù„Ø¥Ø¬Ø±Ø§Ø¡</th><th>Ø§Ù„ØªÙØ§ØµÙŠÙ„</th><th>Ø§Ù„ÙˆÙ‚Øª</th></tr>{% for a in audit %}<tr><td>{{a.action}}</td><td>{{a.detail}}</td><td>{{a.created_at}}</td></tr>{% else %}<tr><td colspan=3>Ù„Ø§ ÙŠÙˆØ¬Ø¯ Ø³Ø¬Ù„ Ø¨Ø¹Ø¯</td></tr>{% endfor %}</table></article></main></html>"""
    return render_template_string(html,u=user,reqs=requests_rows,audit=audit,csrf=csrf())

@app.route('/owner/support/<int:investor_id>/update',methods=['POST'])
@auth
def owner_support_update(investor_id):
    check_csrf();init_owner_support()
    name=request.form.get('display_name','').strip()[:120]
    phone=request.form.get('phone','').strip()[:30]
    status=request.form.get('subscription_status','')
    paused=request.form.get('bot_paused','')
    if status not in ('TRIAL','ACTIVE','EXPIRED','SUSPENDED') or paused not in ('0','1'):
        return 'Ø¥Ø¹Ø¯Ø§Ø¯ ØºÙŠØ± ØµØ§Ù„Ø­',400
    if phone and not re.fullmatch(r'[+0-9() .-]{5,30}',phone):return 'Ø±Ù‚Ù… Ù‡Ø§ØªÙ ØºÙŠØ± ØµØ§Ù„Ø­',400
    with db() as conn,conn.cursor() as c:
        c.execute('SELECT display_name,phone,subscription_status,bot_paused FROM investor_accounts WHERE id=%s FOR UPDATE',(investor_id,))
        old=c.fetchone()
        if old is None:return 'Ø§Ù„Ù…Ø³ØªØ«Ù…Ø± ØºÙŠØ± Ù…ÙˆØ¬ÙˆØ¯',404
        c.execute('UPDATE investor_accounts SET display_name=%s,phone=%s,subscription_status=%s,bot_paused=%s WHERE id=%s',(name or None,phone or None,status,paused=='1',investor_id))
        detail=f'status:{old[2]}->{status}; paused:{old[3]}->{paused}; profile_changed:{old[0]!=name or old[1]!=phone}'
        c.execute('INSERT INTO owner_support_audit(investor_id,action,detail) VALUES(%s,%s,%s)',(investor_id,'ADMIN_PROFILE_UPDATE',detail))
    flash('ØªÙ… Ø­ÙØ¸ Ø§Ù„ØªØ¹Ø¯ÙŠÙ„ ÙˆØªØ³Ø¬ÙŠÙ„Ù‡ ÙÙŠ Ø³Ø¬Ù„ Ø§Ù„Ø¥Ø¯Ø§Ø±Ø©')
    return redirect(url_for('owner_support_detail',investor_id=investor_id))

# V57 extension: retain all V38 endpoints and install new risk-capped LIVE controls.
from v57_extension import install as _install_v57
_install_v57(app, db, init, auth, csrf, check_csrf, proposal, SYMBOLS)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))
