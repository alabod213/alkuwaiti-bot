"""Alkuwaiti Bot V45 - Ultimate Enterprise Edition (Anti-Ban & WebSocket Optimized)
Fully integrated Binance Futures automated trading, WebSocket real-time market streams,
ban protection, Yahoo live prices, daily PnL, tradingview grid, tiers, referrals, and backtesting.
"""
import os, time, hmac, hashlib, secrets, logging, threading, re, json
from datetime import datetime, timezone, timedelta
from decimal import Decimal, ROUND_DOWN
from functools import wraps
from urllib.parse import urlencode
import requests, psycopg2, websocket
from psycopg2.extras import RealDictCursor
from flask import Flask, request, session, redirect, url_for, flash, render_template_string
from werkzeug.security import check_password_hash, generate_password_hash
from cryptography.fernet import Fernet

app=Flask(__name__)
app.secret_key=os.environ.get('SECRET_KEY') or secrets.token_hex(32)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SECURE=True, SESSION_COOKIE_SAMESITE='Lax', PERMANENT_SESSION_LIFETIME=3600)
BASE=os.environ.get('BINANCE_BASE_URL','https://fapi.binance.com').rstrip('/')

MAX_DAILY_TRADES = 100 

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

SYMBOLS=('BTCUSDT','ETHUSDT','BNBUSDT','SOLUSDT','XRPUSDT','XAUUSDT','XAGUSDT','CLUSDT','BZUSDT','ADAUSDT','DOGEUSDT','AVAXUSDT','LINKUSDT','DOTUSDT','NEARUSDT')

LIVE=True
EXECUTION_AUDITED=True

D=Decimal
TARGET_NET_ROI=D('0.025')
ESTIMATED_ROUND_TRIP_COST=D('0.002')
logging.basicConfig(level=logging.INFO)

# ذاكرة مؤقتة لبيانات الأسعار الحية المستلمة عبر WebSocket لتفريغ ضغط الـ REST API
_ws_market_data = {}
_ws_lock = threading.Lock()

def on_message(ws, message):
    try:
        data = json.loads(message)
        if 's' in data and 'c' in data.get('k', {}):
            symbol = data['s']
            close_price = data['k']['c']
            with _ws_lock:
                _ws_market_data[symbol] = {
                    'price': float(close_price),
                    'time': time.time()
                }
    except Exception:
        pass

def on_error(ws, error):
    pass

def on_close(ws, close_status_code, close_msg):
    time.sleep(5)
    start_binance_ws()

def on_open(ws):
    streams = [f"{s.lower()}@kline_5m" for s in SYMBOLS]
    sub_payload = {"method": "SUBSCRIBE", "params": streams, "id": 1}
    ws.send(json.dumps(sub_payload))

def start_binance_ws():
    ws_url = "wss://fstream.binance.com/ws"
    ws = websocket.WebSocketApp(ws_url, on_open=on_open, on_message=on_message, on_error=on_error, on_close=on_close)
    wst = threading.Thread(target=ws.run_forever, daemon=True)
    wst.start()

try:
    start_binance_ws()
except Exception:
    pass

def db():
    return psycopg2.connect(os.environ['DATABASE_URL'], connect_timeout=8, sslmode='require')

def init():
    with db() as conn, conn.cursor() as c:
        c.execute('''CREATE TABLE IF NOT EXISTS bot_preferences (
            id INTEGER PRIMARY KEY CHECK (id=1),
            max_exposure_pct NUMERIC NOT NULL DEFAULT 20,
            max_leverage INTEGER NOT NULL DEFAULT 15,
            target_roi_pct NUMERIC NOT NULL DEFAULT 2.5,
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
        c.execute("""CREATE TABLE IF NOT EXISTS bot_binance_cooldown
                     (id INTEGER PRIMARY KEY CHECK(id=1), until_ms BIGINT NOT NULL DEFAULT 0,
                      reason TEXT NOT NULL DEFAULT '', updated_at TIMESTAMPTZ DEFAULT NOW())""")
        c.execute("INSERT INTO bot_binance_cooldown(id, until_ms, reason) VALUES(1, 0, '') ON CONFLICT(id) DO UPDATE SET until_ms=0, reason=''")

def init_investors_system():
    with db() as conn, conn.cursor() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS investor_accounts (
            id BIGSERIAL PRIMARY KEY, email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL, subscription_tier TEXT NOT NULL DEFAULT 'FREE',
            referral_code TEXT UNIQUE, referred_by TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            paid_until TIMESTAMPTZ)""")

def get_today_trades_count():
    with db() as conn, conn.cursor() as c:
        c.execute("SELECT COUNT(*) FROM bot_orders_v2 WHERE created_at >= CURRENT_DATE AND status LIKE 'OPEN%'")
        row = c.fetchone()
        return row[0] if row else 0

def preferences():
    with db() as conn,conn.cursor(cursor_factory=RealDictCursor) as c:
        c.execute('SELECT * FROM bot_preferences WHERE id=1')
        return c.fetchone()

def is_bot_paused():
    with db() as conn, conn.cursor() as c:
        c.execute('SELECT is_paused FROM bot_preferences WHERE id=1')
        row = c.fetchone()
    if row is None:
        raise RuntimeError('تعذر قراءة حالة إيقاف البوت')
    return bool(row[0])

def allowed_leverage(symbol, requested):
    brackets=signed('GET','/fapi/v1/leverageBracket',{'symbol':symbol})
    item=brackets[0] if isinstance(brackets,list) else brackets
    cap=max(int(x['initialLeverage']) for x in item['brackets'])
    return max(1,min(int(requested),cap,150))

_ban_lock=threading.RLock()
_ban_until_ms=0
_ban_reason=''
_last_shared_check=0.0
_public_cache={}
_public_cache_lock=threading.RLock()

def _cooldown_state(force=False):
    global _ban_until_ms,_ban_reason,_last_shared_check
    now=time.time()
    with _ban_lock:
        if force or now-_last_shared_check>=5:
            _last_shared_check=now
            try:
                with db() as conn,conn.cursor() as c:
                    c.execute('SELECT until_ms,reason FROM bot_binance_cooldown WHERE id=1')
                    row=c.fetchone()
                if row:
                    _ban_until_ms=int(row[0]); _ban_reason=row[1]
            except Exception:
                pass
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
        reason='حظر رسمي مؤقت من Binance (رمز الحظر 418)'
    else:
        try: wait=min(86400,max(60,int(float(response.headers.get('Retry-After','120')))))
        except (ValueError,TypeError): wait=120
        if response.status_code==418: wait=max(wait,900)
        until=now_ms+wait*1000
        reason='حظر Binance الرسمي (418)' if response.status_code==418 else 'تجاوز حدود معدل الطلبات (رمز الحظر 429)'
    with _ban_lock:
        if until>_ban_until_ms:
            _ban_until_ms=until;_ban_reason=reason
        try:
            with db() as conn,conn.cursor() as c:
                c.execute("""UPDATE bot_binance_cooldown SET until_ms=GREATEST(until_ms,%s),
                             reason=CASE WHEN %s>=until_ms THEN %s ELSE reason END,
                             updated_at=NOW() WHERE id=1""",(until,until,reason))
        except Exception:
            logging.exception('Could not persist Binance cooldown')

def _assert_binance_available():
    state=_cooldown_state()
    if state['remaining_seconds']>0:
        raise ValueError('⚠️ محظور مؤقتًا من Binance. نوع الحظر: '+str(state['reason'])+' | المتبقي: '+str(state['remaining_seconds'])+' ثانية.')

REST_MIN_INTERVAL_SECONDS = max(2.0, float(os.environ.get('BINANCE_MIN_REQUEST_INTERVAL', '2.0')))
BINANCE_MARKET_SCANS_ENABLED = os.environ.get('BINANCE_MARKET_SCANS_ENABLED','true').lower() == 'true'
_scan_local = threading.local()

BINANCE_WEIGHT_BUDGET_1M = min(600, max(200, int(os.environ.get('BINANCE_WEIGHT_BUDGET_1M', '400'))))

def _endpoint_weight(path, params=None):
    if path == '/fapi/v1/income': return 40
    if path == '/fapi/v1/klines': return 10
    if path == '/fapi/v1/exchangeInfo': return 10
    if path in ('/fapi/v2/account','/fapi/v3/account'): return 10
    if path in ('/fapi/v2/positionRisk','/fapi/v3/positionRisk'): return 10
    if path == '/fapi/v1/openOrders': return 40 if not (params or {}).get('symbol') else 5
    if path == '/fapi/v1/leverageBracket': return 10
    if path == '/fapi/v1/premiumIndex': return 10
    if path in ('/fapi/v1/order','/fapi/v1/leverage','/fapi/v1/algoOrder','/fapi/v1/allOpenOrders'): return 10
    return 10

def _reserve_binance_request(path, params=None):
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
            raise ValueError('تأجيل آمن للطلب لحماية الحساب من تجاوز وزن الـ API')
        c.execute('''CREATE TABLE IF NOT EXISTS bot_binance_rate_limit
                     (id INTEGER PRIMARY KEY CHECK(id=1), next_allowed TIMESTAMPTZ NOT NULL DEFAULT NOW())''')
        c.execute('INSERT INTO bot_binance_rate_limit(id) VALUES(1) ON CONFLICT DO NOTHING')
        c.execute('SELECT next_allowed FROM bot_binance_rate_limit WHERE id=1 FOR UPDATE')
        next_allowed=c.fetchone()[0]
        now=datetime.now(timezone.utc)
        delay=max(0.0,(next_allowed-now).total_seconds())
        if delay>8: raise ValueError('تم تأجيل طلب Binance لحماية الحساب من الحظر')
        reserved_at=max(now,next_allowed)
        c.execute('UPDATE bot_binance_rate_limit SET next_allowed=%s WHERE id=1',
                  (reserved_at+timedelta(seconds=REST_MIN_INTERVAL_SECONDS),))
        c.execute('INSERT INTO bot_binance_weight_events(reserved_at,weight) VALUES(%s,%s)',(reserved_at,weight))
    if delay: time.sleep(delay)
    _assert_binance_available()

def signed(method,path,params=None):
    _assert_binance_available()
    cache=getattr(_scan_local,'signed_cache',None)
    cache_key=(path,tuple(sorted((str(k),str(v)) for k,v in (params or {}).items())))
    if method=='GET' and cache is not None and path in ('/fapi/v2/account','/fapi/v2/positionRisk','/fapi/v1/openOrders','/fapi/v1/leverageBracket') and cache_key in cache:
        return cache[cache_key]
    _reserve_binance_request(path,params)
    KEY,SECRET=credentials()
    if not KEY or not SECRET: raise ValueError('مفاتيح Binance غير موجودة في الإعدادات')
    p=dict(params or {});p['timestamp']=int(time.time()*1000);p['recvWindow']=5000
    q=urlencode(p);sig=hmac.new(SECRET.encode(),q.encode(),hashlib.sha256).hexdigest()
    r=requests.request(method,BASE+path,params=q+'&signature='+sig,headers={'X-MBX-APIKEY':KEY},timeout=15)
    _record_limit(r)
    if not r.ok:
        if r.status_code in (418,429): raise ValueError('Binance أوقف الطلبات مؤقتًا لحماية الحساب')
        raise ValueError('Binance '+str(r.status_code)+': '+r.text[:220])
    data=r.json()
    if method=='GET' and cache is not None and path in ('/fapi/v2/account','/fapi/v2/positionRisk','/fapi/v1/openOrders','/fapi/v1/leverageBracket'):
        cache[cache_key]=data
    return data

def public(path,p=None):
    _assert_binance_available()
    key=(path,tuple(sorted((str(k),str(v)) for k,v in (p or {}).items())))
    ttl=3600 if path=='/fapi/v1/exchangeInfo' else (60 if path=='/fapi/v1/klines' else 15)
    with _public_cache_lock:
        item=_public_cache.get(key)
        if item and time.monotonic()-item[0]<ttl:return item[1]
    _reserve_binance_request(path,p)
    r=requests.get(BASE+path,params=p,timeout=12)
    _record_limit(r)
    if r.status_code in (418,429): raise ValueError('Binance أوقف الطلبات مؤقتًا')
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

@app.route('/api/binance-ban-status')
@auth
def binance_ban_status():
    return _cooldown_state(force=True)

def csrf():
    if 'csrf' not in session: session['csrf']=secrets.token_hex(24)
    return session['csrf']

def check_csrf():
    if not hmac.compare_digest(request.form.get('csrf',''),session.get('csrf','')): raise ValueError('انتهت صلاحية النموذج، حدّث الصفحة')

def ema(vals,n):
    v=sum(vals[:n])/n
    for x in vals[n:]: v+=(x-v)*2/(n+1)
    return v

def proposal(symbol):
    candles=public('/fapi/v1/klines',{'symbol':symbol,'interval':'5m','limit':100})[:-1]
    closes=[float(c[4]) for c in candles]; highs=[float(c[2]) for c in candles]; lows=[float(c[3]) for c in candles]
    if len(closes)<30: return None
    fast,slow=ema(closes,10),ema(closes,30)
    direction='LONG' if fast>slow else 'SHORT'
    trs=[max(highs[i]-lows[i],abs(highs[i]-closes[i-1]),abs(lows[i]-closes[i-1])) for i in range(1,len(closes))]
    atr=sum(trs[-14:])/14
    if atr<=0: return None
    entry=D(str(closes[-1])); distance=D(str(atr))*D('1.2')
    stop=entry-distance if direction=='LONG' else entry+distance
    target_distance=distance*D('1.8')
    take=entry+target_distance if direction=='LONG' else entry-target_distance
    if stop<=0 or take<=0: return None
    return direction,entry,stop,take

def symbol_info(symbol):
    data=public('/fapi/v1/exchangeInfo')
    item=next((s for s in data['symbols'] if s['symbol']==symbol and s['status']=='TRADING'),None)
    if not item: raise ValueError('هذا الرمز غير متاح للتداول حاليًا على Binance Futures')
    return item

def floor_step(x,step): return (x/step).to_integral_value(rounding=ROUND_DOWN)*step

def strnum(x): return format(x,'f')

def positions():
    return [p for p in signed('GET','/fapi/v2/positionRisk') if D(p['positionAmt'])!=0]

def feasibility(symbol, entry, stop, take, info=None):
    info=info or symbol_info(symbol)
    account = getattr(_scan_local, 'cached_account', None)
    if not account:
        account = signed('GET', '/fapi/v2/account')
    equity=D(account['totalMarginBalance'])
    free=D(account['availableBalance'])
    if equity<=0 or free<=0: raise ValueError('لا يوجد رصيد متاح')
    ps = getattr(_scan_local, 'cached_positions', None)
    if ps is None:
        ps = [p for p in signed('GET', '/fapi/v2/positionRisk') if D(p['positionAmt'])!=0]
    if any(x['symbol']==symbol for x in ps): raise ValueError('مركز مفتوح على نفس الرمز')
    exposure=sum(abs(D(x['positionAmt']))*D(x['markPrice']) for x in ps)
    open_ords = getattr(_scan_local, 'cached_open_orders', None)
    if open_ords is None:
        open_ords = signed('GET', '/fapi/v1/openOrders')
    for order in open_ords:
        if order.get('reduceOnly') or order.get('closePosition'): continue
        if order.get('type') in ('STOP_MARKET','TAKE_PROFIT_MARKET'): continue
        remaining=max(D('0'),D(order.get('origQty','0'))-D(order.get('executedQty','0')))
        px=D(order.get('price') or '0')
        if px<=0:
            try: px=D(public('/fapi/v1/premiumIndex',{'symbol':order['symbol']})['markPrice'])
            except Exception: raise ValueError('تعذر احتساب الأوامر المعلقة')
        exposure+=remaining*px
    prefs=preferences()
    room=equity*D(str(prefs['max_exposure_pct']))/D('100')-exposure
    filters={f['filterType']:f for f in info['filters']}
    lot=filters.get('MARKET_LOT_SIZE') or filters.get('LOT_SIZE')
    if not lot: raise ValueError('شروط حجم الأمر غير متاحة')
    step=D(lot['stepSize']); minimum=D(lot['minQty'])
    mark=D(public('/fapi/v1/premiumIndex',{'symbol':symbol})['markPrice'])
    min_value=D(filters.get('MIN_NOTIONAL',{}).get('notional','5'))
    from decimal import ROUND_UP
    qty=max((minimum/step).to_integral_value(rounding=ROUND_UP)*step,(min_value/mark/step).to_integral_value(rounding=ROUND_UP)*step)
    if step<=0 or mark<=0 or qty<=0: raise ValueError('شروط كمية التداول غير صالحة')
    if D(lot.get('maxQty','0'))>0 and qty>D(lot['maxQty']): raise ValueError('الكمية أكبر من حد Binance')
    lev=allowed_leverage(symbol,prefs['max_leverage'])
    if D(str(prefs['max_exposure_pct']))>100 or D(str(prefs['max_exposure_pct']))<=0: raise ValueError('الحد الأقصى للتعرض يجب أن يكون بين 0 و100 بالمئة')
    if qty*mark>room or qty*mark/D(lev)>free*D('0.95'):
        raise ValueError('الحد الأدنى يتجاوز سقف التعرض أو الهامش المتاح')
    gross=((take-entry)/entry) if take>entry and stop<entry else ((entry-take)/entry) if take<entry and stop>entry else D('-1')
    net=(gross-ESTIMATED_ROUND_TRIP_COST)*D(lev)
    if gross<=0 or net<D(str(prefs['target_roi_pct']))/100:
        raise ValueError('الهدف الفني لا يحقق نسبة الربح الصافي المستهدفة على الهامش')
    return qty,mark,net

def direct_execute_trade(symbol, direction, entry, stop, take, info):
    if is_bot_paused(): raise ValueError('البوت متوقف')
    if get_today_trades_count() >= MAX_DAILY_TRADES:
        raise ValueError('تم الوصول للحد الأقصى المسموح اليوم (100 صفقة)')
    account=signed('GET','/fapi/v2/account')
    if account.get('dualSidePosition'): raise ValueError('يرجى تعطيل Hedge Mode')
    qty, mark, net = feasibility(symbol, entry, stop, take, info)
    prefs = preferences()
    lev = allowed_leverage(symbol, prefs['max_leverage'])
    filters = {f['filterType']:f for f in info['filters']}
    side = 'BUY' if direction == 'LONG' else 'SELL'
    price_filter = filters['PRICE_FILTER']
    tick = D(price_filter['tickSize'])
    stop_price = floor_step(stop, tick)
    if stop_price <= 0: raise ValueError('وقف الخسارة غير صالح')
    signed('POST', '/fapi/v1/leverage', {'symbol': symbol, 'leverage': lev})
    cid = 'ak' + str(int(time.time())) + 'a'
    order = signed('POST', '/fapi/v1/order', {'symbol': symbol, 'side': side, 'type': 'MARKET', 'quantity': strnum(qty), 'newClientOrderId': cid, 'newOrderRespType': 'RESULT'})
    try:
        protect = signed('POST', '/fapi/v1/algoOrder', {'algoType': 'CONDITIONAL', 'symbol': symbol,
            'side': 'SELL' if side == 'BUY' else 'BUY', 'type': 'STOP_MARKET',
            'triggerPrice': strnum(stop_price), 'workingType': 'MARK_PRICE', 'closePosition': 'true'})
    except Exception as stop_error:
        try:
            signed('POST', '/fapi/v1/order', {'symbol': symbol, 'side': 'SELL' if side == 'BUY' else 'BUY',
                'type': 'MARKET', 'quantity': strnum(qty), 'reduceOnly': 'true',
                'newClientOrderId': 'ak' + str(int(time.time())) + 'e'})
        except Exception:
            logging.exception('CRITICAL: EMERGENCY FLATTEN FAILED for %s', symbol)
        raise ValueError('فشل وقف الخسارة وتم الإغلاق: ' + str(stop_error)[:90])
    return order, protect, qty

def auto_scan():
    if not BINANCE_MARKET_SCANS_ENABLED:
        return {'status':'binance_scans_disabled','created':0,'message':'التداول الآلي متوقف'}
    if _cooldown_state()['remaining_seconds']>0:
        return {'status':'binance_banned','created':0}
    if is_bot_paused(): return {'status':'paused','created':0}
    if get_today_trades_count() >= MAX_DAILY_TRADES:
        return {'status':'max_daily_reached','created':0,'message':'وصلت للحد الأقصى (100 صفقة اليوم)'}
    init()
    with db() as conn, conn.cursor() as c:
        c.execute('''CREATE TABLE IF NOT EXISTS bot_scan_gate
                     (id INTEGER PRIMARY KEY CHECK(id=1), started_at TIMESTAMPTZ)''')
        c.execute('INSERT INTO bot_scan_gate(id) VALUES(1) ON CONFLICT DO NOTHING')
        c.execute('''UPDATE bot_scan_gate SET started_at=NOW() WHERE id=1
                     AND (started_at IS NULL OR started_at < NOW()-(%s * INTERVAL '1 second')) RETURNING id''',
                  (60,))
        if not c.fetchone(): return {'status':'rate_limited','created':0}
    _scan_local.signed_cache={}
    try:
        return _auto_scan_body()
    finally:
        _scan_local.signed_cache=None

def _auto_scan_body():
    executed=0
    rejected={}
    with db() as conn,conn.cursor() as c:
        c.execute("SELECT pg_try_advisory_lock(68210314)")
        locked=c.fetchone()[0]
        if not locked:
            return {'status':'busy','created':0}
        try:
            _scan_local.signed_cache = {}
            try:
                _scan_local.cached_account = signed('GET', '/fapi/v2/account')
                _scan_local.cached_positions = [p for p in signed('GET', '/fapi/v2/positionRisk') if D(p['positionAmt']) != 0]
                _scan_local.cached_open_orders = signed('GET', '/fapi/v1/openOrders')
            except Exception as e:
                return {'status':'error', 'message': str(e)[:100]}

            for sym in SYMBOLS:
                if get_today_trades_count() >= MAX_DAILY_TRADES:
                    break
                if _cooldown_state()['remaining_seconds']>0: break
                try:
                    info=symbol_info(sym)
                    signal=proposal(sym)
                    if not signal:
                        rejected[sym]='لا توجد إشارة'
                        c.execute("""INSERT INTO bot_orders_v2(symbol,direction,entry_reference,stop_reference,take_reference,status,note)
                                     VALUES(%s,'LONG',0,0,0,'EXPIRED','لا توجد إشارة فنية مطابقة')""", (sym,))
                        time.sleep(1.5)
                        continue
                    direction,entry,stop,take=signal
                    if any(x['symbol'] == sym for x in _scan_local.cached_positions):
                        rejected[sym]='مركز سابق مفتوح'
                        time.sleep(1.5)
                        continue
                    order, protect, qty = direct_execute_trade(sym, direction, entry, stop, take, info)
                    c.execute("""INSERT INTO bot_orders_v2(symbol,direction,entry_reference,stop_reference,take_reference,status,order_id,stop_algo_id,quantity,entry_price,note)
                                 VALUES(%s,%s,%s,%s,%s,'OPEN_AUTO',%s,%s,%s,%s,'Fully automated execution')""",
                              (sym, direction, entry, stop, take, str(order.get('orderId')), str(protect.get('algoId')), qty, order.get('avgPrice','0')))
                    executed+=1
                except Exception as e:
                    rejected[sym]=str(e)[:110]
                    c.execute("""INSERT INTO bot_orders_v2(symbol,direction,entry_reference,stop_reference,take_reference,status,note)
                                 VALUES(%s,'LONG',0,0,0,'EXPIRED',%s)""", (sym, str(e)[:100]))
                time.sleep(1.5)
            c.execute("UPDATE bot_scan_status SET checked_at=NOW(),opportunities=%s,reason=%s WHERE id=1",(executed,str(rejected)[:1800]))
            return {'status':'ok','executed':executed,'excluded':len(rejected)}
        finally:
            c.execute("SELECT pg_advisory_unlock(68210314)")
            _scan_local.signed_cache = None
            _scan_local.cached_account = None
            _scan_local.cached_positions = None
            _scan_local.cached_open_orders = None

_close_check_lock=threading.Lock()
_close_check_at=0.0
def monitor_close_suggestions():
    global _close_check_at
    with _close_check_lock:
        now=time.monotonic()
        if now-_close_check_at<30: return {'status':'cached'}
        _close_check_at=now
    init()
    prefs=preferences()
    threshold=D(str(prefs['target_roi_pct']))/100
    ps=positions()
    with db() as conn,conn.cursor() as c:
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
                try:
                    order=signed('POST','/fapi/v1/order',{'symbol':symbol,'side':'SELL' if amount>0 else 'BUY',
                        'type':'MARKET','quantity':strnum(abs(amount)),'reduceOnly':'true','newOrderRespType':'RESULT'})
                    c.execute("UPDATE bot_orders_v2 SET status='CLOSED_AUTO',close_id=%s,note='Auto take profit executed' WHERE symbol=%s AND status IN ('OPEN','OPEN_AUTO')",
                              (str(order.get('orderId')),symbol))
                except Exception as e:
                    logging.exception('Auto close failed for %s', symbol)
    return {'status':'ok'}

@app.route('/tasks/scan', methods=['POST'])
def scheduled_scan():
    expected = str(os.environ.get('SCAN_CRON_TOKEN', '')).strip()
    provided = str(request.headers.get('X-Scan-Token', '')).strip()
    expected_bytes = expected.encode('utf-8', errors='ignore')
    provided_bytes = provided.encode('utf-8', errors='ignore')
    if not expected_bytes or not provided_bytes or not hmac.compare_digest(expected_bytes, provided_bytes):
        return {'error': 'unauthorized'}, 401
    try:
        return auto_scan()
    except Exception:
        logging.exception('Scheduled scan failed')
        return {'error': 'scan failed'}, 500

YAHOO_TICKERS={'BTCUSDT':'BTC-USD','ETHUSDT':'ETH-USD','BNBUSDT':'BNB-USD','SOLUSDT':'SOL-USD','XRPUSDT':'XRP-USD','XAUUSDT':'GC=F','XAGUSDT':'SI=F','CLUSDT':'CL=F','BZUSDT':'BZ=F'}
_external_cache={'at':0.0,'checked_at':None,'prices':{}}
_price_lock=threading.Lock()

@app.route('/api/market-prices')
@auth
def market_prices():
    with _price_lock:
        now=time.monotonic()
        if now-_external_cache['at']>=15:
            result={}
            for symbol,ticker in YAHOO_TICKERS.items():
                with _ws_lock:
                    ws_item = _ws_market_data.get(symbol)
                if ws_item and (time.time() - ws_item['time'] < 10):
                    result[symbol] = {'price': ws_item['price'], 'asof': datetime.fromtimestamp(ws_item['time'], timezone.utc).isoformat()}
                    continue
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
        return {'prices':_external_cache['prices'],'checked_at':_external_cache['checked_at'],'source':'Yahoo Finance / WS','realtime':True}

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
    if len(rows)>=1000: raise ValueError('سجل اليوم تجاوز 1000 عملية')
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
    return {'day':day_start.strftime('%Y-%m-%d')+' (الكويت)','gains':fmt(gains),
            'losses':fmt(losses),'fees_funding':fmt(totals['COMMISSION']+totals['FUNDING_FEE']),
            'net':fmt(sum(totals.values(),D('0')))}

BINANCE_AUTO_ACCOUNT_ENABLED=os.environ.get('BINANCE_AUTO_ACCOUNT_ENABLED','true').lower()=='true'
_v37_refresh_lock=threading.Lock()
_v37_last_check=0.0

def _v37_auto_refresh():
    global _v37_last_check
    with _v37_refresh_lock:
        now=time.monotonic()
        if now-_v37_last_check<180:return 
        _v37_last_check=now
    try:
        _assert_binance_available()
        if not BINANCE_AUTO_ACCOUNT_ENABLED:return
        if _snapshot_reserve('v37_auto_account',1800):
            try:
                account=signed('GET','/fapi/v2/account')
                positions_data=signed('GET','/fapi/v2/positionRisk')
                visible=[{k:str(p.get(k,'')) for k in ('symbol','positionAmt','entryPrice','unRealizedProfit')}
                         for p in positions_data if D(str(p.get('positionAmt','0')))!=0]
                _snapshot_put('account',{'balance':str(account['totalWalletBalance']),'positions':visible})
            except Exception as e:
                logging.warning('V37 automatic account update deferred: %s',str(e)[:140])
        if _snapshot_reserve('v37_auto_pnl',7200):
            try:_snapshot_put('pnl',_daily_pnl_from_binance())
            except Exception as e:logging.warning('V37 automatic PnL update deferred: %s',str(e)[:140])
    except Exception as e:
        logging.warning('V37 automatic refresh deferred: %s',str(e)[:140])

@app.route('/api/v37-live-account')
@auth
def v37_live_account():
    if BINANCE_AUTO_ACCOUNT_ENABLED and _v37_refresh_lock.acquire(blocking=False):
        _v37_refresh_lock.release()
        threading.Thread(target=_v37_auto_refresh,daemon=True).start()
    try:
        row=_snapshot_get('account')
        if row:
            return {'balance':str(row['payload'].get('balance')),
                    'captured_at':row['captured_at'].isoformat(),
                    'message':'الرصيد المحفوظ'}
        return {'balance':None,'captured_at':None,'message':'اضغط تحديث يدوي...'}
    except Exception:
        return {'error':'تعذر قراءة بيانات الحساب'},503

@app.route('/api/v34-snapshot-refresh',methods=['POST'])
@auth
def v34_snapshot_refresh():
    try:
        check_csrf()
        _assert_binance_available()
        account=signed('GET','/fapi/v2/account')
        positions_data=signed('GET','/fapi/v2/positionRisk')
        visible=[{k:str(p.get(k,'')) for k in ('symbol','positionAmt','entryPrice','unRealizedProfit')}
                 for p in positions_data if D(str(p.get('positionAmt','0')))!=0]
        _snapshot_put('account',{'balance':str(account['totalWalletBalance']),'positions':visible})
        flash('تم تحديث الرصيد والمراكز الحقيقية بنجاح.')
    except Exception as e:
        flash('فشل التحديث: '+str(e)[:120])
    return redirect(url_for('home'))

@app.route('/api/v34-pnl-refresh',methods=['POST'])
@auth
def v34_pnl_refresh():
    try:
        check_csrf()
        _assert_binance_available()
        _snapshot_put('pnl',_daily_pnl_from_binance())
        flash('تم تحديث أرباح وخسائر اليوم.')
    except Exception as e:
        flash('فشل التحديث: '+str(e)[:120])
    return redirect(url_for('home'))

@app.route('/api/daily-pnl')
@auth
def daily_pnl():
    try:
        row=_snapshot_get('pnl')
        if not row:return {'error':'لا توجد نتائج محفوظة بعد.'},503
        data=dict(row['payload'])
        data['captured_at']=row['captured_at'].isoformat()
        return data
    except Exception:
        return {'error':'تعذر القراءة'},503

PAGE='''<!doctype html><html lang="ar" dir="rtl"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>البوت الكويتي - V45 Enterprise Edition</title><style>
body{background:#101827;color:#f5f7fb;font:16px system-ui;margin:0}main{max-width:950px;margin:auto;padding:16px}article{background:#202d40;padding:16px;border-radius:14px;margin:14px 0;overflow:auto}button,input{font:inherit;padding:10px;border:0;border-radius:8px;margin:4px}button{background:#238657;color:white}button.danger{background:#b64040}a{color:#9fc8ff}table{width:100%;border-collapse:collapse}td,th{padding:8px;border-bottom:1px solid #435069;text-align:right}small{color:#bdc9da}.warn{color:#ffcf83}

:root{color-scheme:dark}body{transition:background-color .3s,color .3s}
html[data-theme="light"]{color-scheme:light}
html[data-theme="light"] body{background:#f4f7fc!important;color:#182438!important}
html[data-theme="light"] article{background:#fff!important;color:#182438!important;box-shadow:0 2px 14px #23385813}
html[data-theme="light"] a{color:#0759a7!important}
html[data-theme="light"] small{color:#46576a!important}
html[data-theme="light"] .warn{color:#8b5500!important}
html[data-theme="light"] td,html[data-theme="light"] th{border-color:#dce4ee!important}
html[data-theme="light"] input,html[data-theme="light"] select{background:#fff;color:#172438;border:1px solid #b9c9d9}
.logout-link{display:inline-block;padding:10px 14px;background:#a52e45!important;color:white!important;border-radius:10px;text-decoration:none;font-weight:700}
.theme-switch{display:inline-block;margin:8px 0;padding:9px 12px;background:#315174;color:white;border:0;border-radius:9px;cursor:pointer}
.charts-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:16px;margin-top:10px}
.chart-box{background:#182232;border:1px solid #33445c;border-radius:10px;padding:8px}
.chart-title{font-weight:bold;margin-bottom:6px;color:#38bdf8;text-align:center}
</style><script>(function(){
  function hour(){return Number(new Intl.DateTimeFormat('en-GB',{timeZone:'Asia/Kuwait',hour:'2-digit',hourCycle:'h23'}).format(new Date()))}
  function apply(){let pref;try{pref=localStorage.getItem('alkuwaiti-theme')}catch(e){};let theme=pref==='light'||pref==='dark'?pref:(hour()>=6&&hour()<18?'light':'dark');document.documentElement.dataset.theme=theme;let b=document.getElementById('theme-switch');if(b)b.textContent='🎨 المظهر: '+(theme==='light'?'نهاري':'ليلي')+' (اضغط للتغيير)'}
  window.toggleAlkuwaitiTheme=function(){let theme=document.documentElement.dataset.theme==='light'?'dark':'light';try{localStorage.setItem('alkuwaiti-theme',theme)}catch(e){};apply()};
  apply();document.addEventListener('DOMContentLoaded',apply);setInterval(apply,60000);
})();</script><main><button id="theme-switch" type="button" class="theme-switch" onclick="toggleAlkuwaitiTheme()">🎨 المظهر</button><h1>🇰🇼 البوت الكويتي V45 (Enterprise Edition)</h1><p class="warn">⚠️ التداول الحقيقي والآلي مفعل تماماً (الحد الأقصى: 100 صفقة يومياً)</p>
{% for m in get_flashed_messages() %}<article>{{m}}</article>{% endfor %}
{% if not logged %}<article><h2>دخول المدير</h2><form method="post" action="{{url_for('login')}}"><input type="hidden" name="csrf" value="{{csrf}}"><input type="email" name="email" placeholder="البريد" required><input type="password" name="password" placeholder="كلمة المرور" required><button>دخول</button></form></article>
{% else %}<p><a href="{{url_for('logout')}}" class="logout-link">🚪 تسجيل خروج المسؤول</a> | <a href="/investors">واجهة المستثمرين</a> | <a href="/backtest" target="_blank">📈 محاكي الاختبار التاريخي</a></p>

<article id="ban-alert-box" style="border:2px solid #ef4444;background:#2d1519;display:none;">
  <h2 style="color:#f87171;margin-top:0">🚨 تنبيه: حساب Binance قيد القيود أو الحظر المؤقت</h2>
  <p><strong>السبب:</strong> <span id="ban-reason-desc" style="color:#fca5a5;">—</span></p>
  <p><strong>الوقت المتبقي:</strong> <span id="ban-countdown-timer" style="font-size:1.4em;font-weight:bold;color:#fca5a5;">0 ثانية</span></p>
</article>

<article style="border:2px solid #d97706">
<h2>🛑 مفتاح إيقاف البوت</h2>
{% if prefs and prefs.is_paused %}
<p style="color:#fca5a5;font-weight:bold">🔴 البوت متوقف.</p>
{% else %}
<p style="color:#86efac;font-weight:bold">🟢 البوت يعمل بكامل الصلاحيات.</p>
{% endif %}
<form method="post" action="{{url_for('toggle_bot')}}"><input type="hidden" name="csrf" value="{{csrf}}">
{% if prefs and prefs.is_paused %}<input type="hidden" name="action" value="resume"><button type="submit" style="background:#15803d">▶️ استئناف التشغيل</button>
{% else %}<input type="hidden" name="action" value="stop"><button type="submit" class="danger">🛑 إيقاف البوت</button>{% endif %}
</form>
</article>

<article><h2>📊 معدل التداول اليومي</h2>
<p style="font-size:1.4em;font-weight:bold">الصفقات اليوم: <span style="color:#38bdf8">{{today_trades}}</span> / 100 صفقة</p>
</article>

<article><h2>📈 الشارتات التفاعلية المباشرة (العملات الرئيسية)</h2>
<div class="charts-grid">
  <div class="chart-box"><div class="chart-title">BTCUSDT</div><div style="height:250px"><iframe scrolling="no" allowtransparency="true" frameborder="0" src="https://s.tradingview.com/embed-widget/mini-symbol-overview/?locale=ar#%7B%22symbol%22%3A%22BINANCE%3ABTCUSDT%22%2C%22width%22%3A%22100%25%22%2C%22height%22%3A%22100%25%22%2C%22dateRange%22%3A%221D%22%2C%22colorTheme%22%3A%22dark%22%2C%22isTransparent%22%3Atrue%2C%22autosize%22%3Atrue%7D" style="width:100%;height:100%;"></iframe></div></div>
  <div class="chart-box"><div class="chart-title">ETHUSDT</div><div style="height:250px"><iframe scrolling="no" allowtransparency="true" frameborder="0" src="https://s.tradingview.com/embed-widget/mini-symbol-overview/?locale=ar#%7B%22symbol%22%3A%22BINANCE%3AETHUSDT%22%2C%22width%22%3A%22100%25%22%2C%22height%22%3A%22100%25%22%2C%22dateRange%22%3A%221D%22%2C%22colorTheme%22%3A%22dark%22%2C%22isTransparent%22%3Atrue%2C%22autosize%22%3Atrue%7D" style="width:100%;height:100%;"></iframe></div></div>
  <div class="chart-box"><div class="chart-title">BNBUSDT</div><div style="height:250px"><iframe scrolling="no" allowtransparency="true" frameborder="0" src="https://s.tradingview.com/embed-widget/mini-symbol-overview/?locale=ar#%7B%22symbol%22%3A%22BINANCE%3ABNBUSDT%22%2C%22width%22%3A%22100%25%22%2C%22height%22%3A%22100%25%22%2C%22dateRange%22%3A%221D%22%2C%22colorTheme%22%3A%22dark%22%2C%22isTransparent%22%3Atrue%2C%22autosize%22%3Atrue%7D" style="width:100%;height:100%;"></iframe></div></div>
</div>
</article>

<article><h2>💰 رصيد Binance المؤكد</h2><p id="v37-balance">{{balance}}</p><small id="v37-account-time">آخر تحديث: {{account_snapshot_at or 'لم يتم التحديث بعد'}}</small><form method="post" action="/api/v34-snapshot-refresh"><input type="hidden" name="csrf" value="{{csrf}}"><button type="submit">تحديث الرصيد والمراكز</button></form></article>

<article><h2>🔑 تغيير مفاتيح Binance API</h2><form method="post" action="{{url_for('update_api_credentials')}}"><input type="hidden" name="csrf" value="{{csrf}}"><input name="api_key" type="password" placeholder="API Key الجديد" required style="width:90%"><input name="api_secret" type="password" placeholder="API Secret الجديد" required style="width:90%"><button>حفظ المفاتيح</button></form></article>

<article><h2>⚙️ إعدادات رأس المال والرافعة</h2>
<form method="post" action="{{url_for('save_preferences')}}"><input type="hidden" name="csrf" value="{{csrf}}">
<label>الحد الأقصى للتعرض (%)</label><input type="number" name="max_exposure_pct" value="{{prefs.max_exposure_pct}}" required>
<label>الرافعة المالية القصوى</label><input type="number" name="max_leverage" value="{{prefs.max_leverage}}" required>
<label>هدف العائد الصافي (%)</label><input type="number" name="target_roi_pct" step="0.5" value="{{prefs.target_roi_pct}}" required>
<button>حفظ الإعدادات</button></form></article>

<article><h2>📈 أسعار الأسواق المباشرة (WebSocket & Yahoo)</h2>
<table><thead><tr><th>السوق</th><th>السعر بالدولار تقريبًا</th><th>وقت البيانات</th></tr></thead><tbody id="external-prices"><tr><td colspan="3">جاري جلب الأسعار...</td></tr></tbody></table><small id="price-state"></small></article>

<article><h2>مراكز Binance الحالية</h2><table><tr><th>الرمز</th><th>الكمية</th><th>الدخول</th><th>الربح/الخسارة</th><th>إغلاق</th></tr>{% for p in positions %}<tr><td>{{p.symbol}}</td><td>{{p.positionAmt}}</td><td>{{p.entryPrice}}</td><td>{{p.unRealizedProfit}} USDT</td><td><form method="post" action="{{url_for('close_position',symbol=p.symbol)}}"><input type="hidden" name="csrf" value="{{csrf}}"><button class="danger">إغلاق</button></form></td></tr>{% else %}<tr><td colspan="5">لا توجد مراكز مفتوحة</td></tr>{% endfor %}</table></article>

<article><h2>سجل الأوامر المنفذة</h2>
<table><thead><tr><th>الوقت والتاريخ</th><th>الرمز</th><th>الاتجاه</th><th>الحالة</th><th>ملاحظة</th></tr></thead><tbody>
{% for x in recent %}
<tr>
  <td style="color:#38bdf8;font-size:0.9em;">{{ x.created_at.strftime('%Y-%m-%d %H:%M:%S') if x.created_at else '—' }}</td>
  <td>{{x.symbol}}</td>
  <td>{{x.direction}}</td>
  <td>{{x.status}}</td>
  <td><small>{{x.note or '—'}}</small></td>
</tr>
{% else %}
<tr><td colspan="5">لا توجد محاولات مسجلة بعد</td></tr>
{% endfor %}
</tbody></table></article>{% endif %}</main>
<script>
(function(){
 const symbols=[['بيتكوين','BTCUSDT'],['إيثريوم','ETHUSDT'],['BNB','BNBUSDT'],['سولانا','SOLUSDT'],['XRP','XRPUSDT'],['الذهب','XAUUSDT'],['الفضة','XAGUSDT'],['نفط WTI','CLUSDT'],['نفط برنت','BZUSDT'],['إعدادات ADA','ADAUSDT'],['دوجكوين','DOGEUSDT'],['افلانش','AVAXUSDT'],['لينك','LINKUSDT'],['بولكادوت','DOTUSDT'],['نير','NEARUSDT']];
 async function refreshMarket(){
   const tbody=document.getElementById('external-prices');
   if(!tbody)return;
   try{
     const r=await fetch('/api/market-prices',{credentials:'same-origin',cache:'no-store'});
     if(!r.ok)throw Error('price request');
     const data=await r.json();tbody.replaceChildren();
     for(const [name,sym] of symbols){
       const tr=document.createElement('tr'),item=data.prices[sym];
       const cell1=document.createElement('td');cell1.textContent=name+' '+sym;tr.appendChild(cell1);
       const cell2=document.createElement('td');cell2.textContent=item&&item.price!==null?Number(item.price).toLocaleString('en-US',{maximumFractionDigits:6}):'غير متاح';tr.appendChild(cell2);
       const cell3=document.createElement('td');cell3.textContent=item&&item.asof?new Date(item.asof).toLocaleTimeString('ar-KW'):'—';tr.appendChild(cell3);
       tbody.appendChild(tr);
     }
     document.getElementById('price-state').textContent='المصدر: WebSocket & Yahoo Finance';
   }catch(e){document.getElementById('price-state').textContent='تعذر تحديث الأسعار مؤقتًا';}
 }
 refreshMarket();
 setInterval(refreshMarket,10000);

 async function checkBan(){
   try{
     const r=await fetch('/api/binance-ban-status',{credentials:'same-origin',cache:'no-store'});
     if(!r.ok)return;
     const data=await r.json();
     const box=document.getElementById('ban-alert-box');
     const reasonText=document.getElementById('ban-reason-desc');
     const timerText=document.getElementById('ban-countdown-timer');
     if(data.remaining_seconds>0){
       if(box)box.style.display='block';
       if(reasonText)reasonText.textContent=data.reason;
       let secs=data.remaining_seconds;
       const m=Math.floor(secs/60), s=secs%60;
       if(timerText)timerText.textContent=(m>0?m+' دقيقة و ':'')+s+' ثانية';
     } else { if(box)box.style.display='none'; }
   }catch(e){}
 }
 setInterval(checkBan, 5000);
})();
</script></html>'''

@app.route('/investors')
def investors():
    user=None
    if session.get('investor_id'):
        try:
            init_investors_system()
            with db() as conn,conn.cursor(cursor_factory=RealDictCursor) as c:
                c.execute('SELECT email,subscription_tier,referral_code FROM investor_accounts WHERE id=%s',(session['investor_id'],))
                user=c.fetchone()
            if user is None: session.pop('investor_id', None)
        except Exception:
            pass
    return render_template_string('<!doctype html><html lang="ar" dir="rtl"><meta charset="utf-8"><main><h1>واجهة المستثمرين</h1><a href="/">الرئيسية</a></main></html>',user=user)

@app.route('/toggle-bot', methods=['POST'])
@auth
def toggle_bot():
    try:
        check_csrf()
        action = request.form.get('action', '')
        if action not in ('stop', 'resume'): raise ValueError('أمر غير صالح')
        paused = action == 'stop'
        init()
        with db() as conn, conn.cursor() as c:
            c.execute('UPDATE bot_preferences SET is_paused=%s, updated_at=NOW() WHERE id=1', (paused,))
        flash('🛑 تم إيقاف التداول الآلي.' if paused else '▶️ تم استئناف التداول الآلي.')
    except Exception:
        flash('تعذر تغيير حالة البوت.')
    return redirect(url_for('home'))

@app.route('/health')
def health(): return {'status':'web_running','version':'V45','enterprise':True,'live_trading_enabled':LIVE}

@app.route('/login',methods=['GET','POST'])
def login():
    if request.method=='POST':
        try:
            check_csrf()
            email=os.environ.get('ADMIN_EMAIL',''); ph=os.environ.get('ADMIN_PASSWORD_HASH','')
            if email and ph and hmac.compare_digest(request.form.get('email','').lower(),email.lower()) and check_password_hash(ph,request.form.get('password','')):
                session.clear(); session['admin']=True; session.permanent=True; csrf(); return redirect(url_for('home'))
            flash('بيانات الدخول غير صحيحة')
        except Exception as e: flash(str(e)[:160])
    return render_template_string(PAGE,logged=False,csrf=csrf(),live=LIVE)

@app.route('/logout')
def logout(): session.clear(); return redirect(url_for('login'))

@app.route('/')
@auth
def home():
    recent=[];pos=[];scan_state=None;bal='غير متاح'
    today_trades = 0
    try:
        init()
        today_trades = get_today_trades_count()
        monitor_close_suggestions()
        with db() as conn,conn.cursor(cursor_factory=RealDictCursor) as c:
            c.execute('SELECT * FROM bot_orders_v2 ORDER BY id DESC LIMIT 30');recent=c.fetchall()
            c.execute('SELECT * FROM bot_scan_status WHERE id=1');scan_state=c.fetchone()
    except Exception as e: flash('DB Error: '+str(e)[:160])
    snapshot_at=None
    try:
        snapshot=_snapshot_get('account')
        if snapshot:
            bal=str(snapshot['payload'].get('balance','غير متاح'))+' USDT'
            pos=snapshot['payload'].get('positions',[])
            snapshot_at=snapshot['captured_at'].isoformat()
        else:
            bal='اضغط تحديث الرصيد لجلبه'
    except Exception:
        bal='تعذر القراءة'
    return render_template_string(PAGE,logged=True,csrf=csrf(),live=LIVE,balance=bal,account_snapshot_at=snapshot_at,positions=pos,recent=recent,prefs=preferences(),scan_state=scan_state,today_trades=today_trades)

@app.route('/settings/api',methods=['POST'])
@auth
def update_api_credentials():
    try:
        check_csrf()
        master=os.environ.get('CREDENTIALS_ENCRYPTION_KEY','')
        if not master: raise ValueError('أضف CREDENTIALS_ENCRYPTION_KEY أولاً')
        f=Fernet(master.encode())
        key=request.form.get('api_key','').strip()
        secret=request.form.get('api_secret','').strip()
        if not key or not secret: raise ValueError('المفاتيح غير صالحة')
        _assert_binance_available()
        params={'timestamp':int(time.time()*1000),'recvWindow':5000}
        q=urlencode(params)
        sig=hmac.new(secret.encode(),q.encode(),hashlib.sha256).hexdigest()
        _reserve_binance_request('/fapi/v2/account')
        r=requests.get(BASE+'/fapi/v2/account',params=q+'&signature='+sig,headers={'X-MBX-APIKEY':key},timeout=15)
        _record_limit(r)
        if not r.ok: raise ValueError('فشل اختبار Binance. تحقق من المفاتيح.')
        init()
        with db() as conn,conn.cursor() as c:
            c.execute('''INSERT INTO bot_api_credentials(id,api_key_cipher,api_secret_cipher,updated_at)
                         VALUES(1,%s,%s,NOW()) ON CONFLICT(id) DO UPDATE
                         SET api_key_cipher=EXCLUDED.api_key_cipher,api_secret_cipher=EXCLUDED.api_secret_cipher,updated_at=NOW()''',
                      (psycopg2.Binary(f.encrypt(key.encode())),psycopg2.Binary(f.encrypt(secret.encode()))))
        flash('تم حفظ مفاتيح Binance بنجاح.')
    except Exception as e:
        flash('تعذر حفظ المفاتيح: '+str(e)[:160])
    return redirect(url_for('home'))

@app.route('/settings/preferences',methods=['POST'])
@auth
def save_preferences():
    try:
        check_csrf()
        exposure=D(request.form['max_exposure_pct'])
        leverage=int(request.form['max_leverage'])
        target=D(request.form['target_roi_pct'])
        init()
        with db() as conn,conn.cursor() as c:
            c.execute('UPDATE bot_preferences SET max_exposure_pct=%s,max_leverage=%s,target_roi_pct=%s,updated_at=NOW() WHERE id=1',(exposure,leverage,target))
        flash('تم حفظ الإعدادات بنجاح.')
    except Exception as e:
        flash('تعذر الحفظ: '+str(e)[:150])
    return redirect(url_for('home'))

@app.route('/close/<symbol>',methods=['POST'])
@auth
def close_position(symbol):
    try:
        check_csrf()
        if symbol not in SYMBOLS: raise ValueError('رمز غير معتمد')
        ps=[p for p in positions() if p['symbol']==symbol]
        if not ps: raise ValueError('المركز مغلق بالفعل')
        p=ps[0]
        amt=D(p['positionAmt'])
        order=signed('POST','/fapi/v1/order',{'symbol':symbol,'side':'SELL' if amt>0 else 'BUY',
            'type':'MARKET','quantity':strnum(abs(amt)),'reduceOnly':'true','newOrderRespType':'RESULT'})
        with db() as conn,conn.cursor(cursor_factory=RealDictCursor) as c:
            c.execute("SELECT id,stop_algo_id FROM bot_orders_v2 WHERE symbol=%s AND status IN ('OPEN','OPEN_AUTO') ORDER BY id DESC LIMIT 1",(symbol,))
            tracked=c.fetchone()
        warning=''
        if tracked and tracked['stop_algo_id']:
            try: signed('DELETE','/fapi/v1/algoOrder',{'symbol':symbol,'algoId':tracked['stop_algo_id']})
            except Exception as e: warning=' تنبيه وقف الخسارة: '+str(e)[:90]
        with db() as conn,conn.cursor() as c:
            c.execute("UPDATE bot_orders_v2 SET status='CLOSED_MANUAL',close_id=%s,note=%s WHERE symbol=%s AND status IN ('OPEN','OPEN_AUTO')",
                      (str(order.get('orderId')),'Manual close'+warning,symbol))
        flash('تم إرسال أمر إغلاق المركز بنجاح.'+warning)
    except Exception as e: flash('تعذر إغلاق المركز: '+str(e)[:200])
    return redirect(url_for('home'))

if __name__=='__main__': app.run(host='0.0.0.0',port=int(os.environ.get('PORT','5000')))
