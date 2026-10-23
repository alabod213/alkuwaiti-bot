"""Alkuwaiti Bot - human approved USDT-M futures execution.
Live trading & background scans with smooth internal weight thresholds.
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
app.secret_key=os.environ.get('SECRET_KEY') or secrets.token_hex(32)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SECURE=True, SESSION_COOKIE_SAMESITE='Lax', PERMANENT_SESSION_LIFETIME=3600)
BASE=os.environ.get('BINANCE_BASE_URL','https://fapi.binance.com').rstrip('/')

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

LIVE=True
EXECUTION_AUDITED=True

D=Decimal
TARGET_NET_ROI=D('0.10')
ESTIMATED_ROUND_TRIP_COST=D('0.002')
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
        reason='حظر IP رسمي من Binance (418)'
    else:
        try: wait=min(86400,max(60,int(float(response.headers.get('Retry-After','120')))))
        except (ValueError,TypeError): wait=120
        if response.status_code==418: wait=max(wait,900)
        until=now_ms+wait*1000
        reason='حظر Binance الرسمي' if response.status_code==418 else 'تقييد طلبات Binance (429)'
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

def _assert_binance_available():
    state=_cooldown_state()
    if state['remaining_seconds']>0:
        raise ValueError('Binance محظور مؤقتًا؛ المتبقي '+str(state['remaining_seconds'])+' ثانية.')

REST_MIN_INTERVAL_SECONDS = max(2.0, float(os.environ.get('BINANCE_MIN_REQUEST_INTERVAL', '2.0')))
BINANCE_MARKET_SCANS_ENABLED = os.environ.get('BINANCE_MARKET_SCANS_ENABLED','true').lower() == 'true'
BINANCE_SCAN_INTERVAL_SECONDS = max(180, int(os.environ.get('BINANCE_SCAN_INTERVAL_SECONDS','180')))
_scan_local = threading.local()

BINANCE_WEIGHT_BUDGET_1M = min(1200, max(100, int(os.environ.get('BINANCE_WEIGHT_BUDGET_1M', '800'))))
BINANCE_DASHBOARD_ACCOUNT_REFRESH = os.environ.get('BINANCE_DASHBOARD_ACCOUNT_REFRESH', 'true').lower() == 'true'

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
            raise ValueError('تأجيل آمن للطلب لضمان الموازنة مع Binance')
        c.execute('''CREATE TABLE IF NOT EXISTS bot_binance_rate_limit
                     (id INTEGER PRIMARY KEY CHECK(id=1), next_allowed TIMESTAMPTZ NOT NULL DEFAULT NOW())''')
        c.execute('INSERT INTO bot_binance_rate_limit(id) VALUES(1) ON CONFLICT DO NOTHING')
        c.execute('SELECT next_allowed FROM bot_binance_rate_limit WHERE id=1 FOR UPDATE')
        next_allowed=c.fetchone()[0]
        now=datetime.now(timezone.utc)
        delay=max(0.0,(next_allowed-now).total_seconds())
        if delay>8: raise ValueError('تم تأجيل طلب Binance لتجنب تكدس الطلبات')
        reserved_at=max(now,next_allowed)
        c.execute('UPDATE bot_binance_rate_limit SET next_allowed=%s WHERE id=1',
                  (reserved_at+timedelta(seconds=REST_MIN_INTERVAL_SECONDS),))
        c.execute('INSERT INTO bot_binance_weight_events(reserved_at,weight) VALUES(%s,%s)',(reserved_at,weight))
    if delay: time.sleep(delay)
    _assert_binance_available()

def _observe_binance_weight(response):
    raw=response.headers.get('X-MBX-USED-WEIGHT-1M')
    if not raw:return
    try: used=int(raw)
    except (ValueError,TypeError):return
    # رفع السقف ليكون 1800 بدلاً من 150 لتجنب الإيقاف الاحترازي الخاطئ
    threshold=max(500, int(os.environ.get('BINANCE_IP_WEIGHT_SOFT_LIMIT','1800')))
    if used<threshold:return
    global _ban_until_ms,_ban_reason
    until=int(time.time()*1000)+120000
    with _ban_lock:
        if until>_ban_until_ms:
            _ban_until_ms=until
            _ban_reason='إيقاف احترازي: وزن طلبات عنوان IP مرتفع جداً'
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
    if not KEY or not SECRET: raise ValueError('مفاتيح Binance غير موجودة في الإعدادات')
    p=dict(params or {});p['timestamp']=int(time.time()*1000);p['recvWindow']=5000
    q=urlencode(p);sig=hmac.new(SECRET.encode(),q.encode(),hashlib.sha256).hexdigest()
    r=requests.request(method,BASE+path,params=q+'&signature='+sig,headers={'X-MBX-APIKEY':KEY},timeout=15)
    _record_limit(r)
    _observe_binance_weight(r)
    if not r.ok:
        if r.status_code in (418,429): raise ValueError('Binance أوقف الطلبات مؤقتًا')
        raise ValueError('Binance '+str(r.status_code)+': '+r.text[:220])
    data=r.json()
    if method=='GET' and cache is not None and path in ('/fapi/v2/account','/fapi/v2/positionRisk','/fapi/v1/openOrders','/fapi/v1/leverageBracket'):
        cache[cache_key]=data
    return data

def public(path,p=None):
    _assert_binance_available()
    key=(path,tuple(sorted((str(k),str(v)) for k,v in (p or {}).items())))
    ttl=3600 if path=='/fapi/v1/exchangeInfo' else (180 if path=='/fapi/v1/klines' else 60)
    with _public_cache_lock:
        item=_public_cache.get(key)
        if item and time.monotonic()-item[0]<ttl:return item[1]
    _reserve_binance_request(path,p)
    r=requests.get(BASE+path,params=p,timeout=12)
    _record_limit(r)
    _observe_binance_weight(r)
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

@app.route('/api/v32-connection-status')
@auth
def v32_connection_status():
    state=_cooldown_state(force=True)
    return {'binance':state, 'market_scans_enabled':BINANCE_MARKET_SCANS_ENABLED,
            'scan_interval_seconds':BINANCE_SCAN_INTERVAL_SECONDS,
            'minimum_request_interval_seconds':REST_MIN_INTERVAL_SECONDS,
            'weighted_budget_per_60_seconds':BINANCE_WEIGHT_BUDGET_1M,
            'dashboard_binance_refresh_enabled':BINANCE_DASHBOARD_ACCOUNT_REFRESH,
            'external_quotes':'Yahoo Finance'}

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
    candles=public('/fapi/v1/klines',{'symbol':symbol,'interval':'15m','limit':120})[:-1]
    closes=[float(c[4]) for c in candles]; highs=[float(c[2]) for c in candles]; lows=[float(c[3]) for c in candles]
    if len(closes)<60: return None
    fast,slow=ema(closes,20),ema(closes,50)
    direction='LONG' if fast>slow else 'SHORT'
    trs=[max(highs[i]-lows[i],abs(highs[i]-closes[i-1]),abs(lows[i]-closes[i-1])) for i in range(1,len(closes))]
    atr=sum(trs[-14:])/14
    if atr<=0: return None
    entry=D(str(closes[-1])); distance=D(str(atr))*D('1.5')
    stop=entry-distance if direction=='LONG' else entry+distance
    target_distance=distance*2
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

def wallet(): return D(signed('GET','/fapi/v2/account')['totalWalletBalance'])

def feasibility(symbol, entry, stop, take, info=None):
    info=info or symbol_info(symbol)
    account=signed('GET','/fapi/v2/account')
    equity=D(account['totalMarginBalance'])
    free=D(account['availableBalance'])
    if equity<=0 or free<=0: raise ValueError('لا يوجد رصيد متاح')
    ps=positions()
    if any(x['symbol']==symbol for x in ps): raise ValueError('مركز مفتوح على نفس الرمز')
    exposure=sum(abs(D(x['positionAmt']))*D(x['markPrice']) for x in ps)
    for order in signed('GET','/fapi/v1/openOrders'):
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

def entry_order(row):
    if is_bot_paused(): raise ValueError('البوت متوقف بأمر المسؤول؛ لا يمكن فتح صفقة جديدة')
    
    symbol=row['symbol']; info=symbol_info(symbol)
    account=signed('GET','/fapi/v2/account')
    if account.get('dualSidePosition'): raise ValueError('يرجى تعطيل Hedge Mode؛ هذا الإصدار يدعم One-way فقط')
    if (datetime.now(timezone.utc)-row['created_at']).total_seconds()>900:
        raise ValueError('انتهت صلاحية الإشارة (15 دقيقة). أعد التحليل')
    qty,mark,net=feasibility(symbol,D(row['entry_reference']),D(row['stop_reference']),D(row['take_reference']),info)
    take=D(row['take_reference'])
    if row['direction']=='LONG':
        live_net=(take-mark)/mark-ESTIMATED_ROUND_TRIP_COST
    else:
        live_net=(mark-take)/mark-ESTIMATED_ROUND_TRIP_COST
    prefs=preferences()
    lev=allowed_leverage(symbol,prefs['max_leverage'])
    if live_net*D(lev)<D(str(prefs['target_roi_pct']))/100:
        raise ValueError('بعد تغير السعر لم يعد الهدف يحقق صافي العائد التقديري المحدد')
    filters={f['filterType']:f for f in info['filters']}
    stop=D(row['stop_reference']); side='BUY' if row['direction']=='LONG' else 'SELL'
    if (side=='BUY' and stop>=mark) or (side=='SELL' and stop<=mark):
        raise ValueError('سعر وقف الخسارة لم يعد صالحًا بعد تحرك السوق؛ أعد التحليل')
    price_filter=filters['PRICE_FILTER']; tick=D(price_filter['tickSize'])
    stop=floor_step(stop,tick)
    if stop<=0: raise ValueError('وقف الخسارة غير صالح')
    signed('POST','/fapi/v1/leverage',{'symbol':symbol,'leverage':lev})
    cid='ak'+str(row['id'])+'o'
    order=signed('POST','/fapi/v1/order',{'symbol':symbol,'side':side,'type':'MARKET','quantity':strnum(qty),'newClientOrderId':cid,'newOrderRespType':'RESULT'})
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
            raise ValueError('خطر: فشل وقف الخسارة والإغلاق الطارئ! افتح Binance فورًا وتحقق من المركز')
        raise ValueError('فشل إنشاء وقف الخسارة؛ أرسلنا أمر إغلاق طارئ. تحقق من Binance: '+str(stop_error)[:90])
    return order,protect,qty

def auto_scan():
    if not BINANCE_MARKET_SCANS_ENABLED:
        return {'status':'binance_scans_disabled','created':0,'message':'تحليل شموع Binance متوقف'}
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
                if _cooldown_state()['remaining_seconds']>0: break
                try:
                    info=symbol_info(sym)
                    signal=proposal(sym)
                    if not signal:
                        rejected[sym]='لا توجد إشارة'
                        time.sleep(2.0)
                        continue
                    direction,entry,stop,take=signal
                    feasibility(sym,entry,stop,take,info)
                    c.execute("SELECT 1 FROM bot_orders_v2 WHERE symbol=%s AND status IN ('PENDING','SUBMITTING','OPEN','CLOSING','REQUIRES_REVIEW') LIMIT 1",(sym,))
                    if c.fetchone():
                        rejected[sym]='فرصة أو مركز سابق قيد المتابعة'
                        time.sleep(2.0)
                        continue
                    c.execute("INSERT INTO bot_orders_v2(symbol,direction,entry_reference,stop_reference,take_reference) VALUES(%s,%s,%s,%s,%s)",(sym,direction,entry,stop,take))
                    created+=1
                except Exception as e:
                    rejected[sym]=str(e)[:110]
                time.sleep(2.0)
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
        if now-_close_check_at<120: return {'status':'cached'}
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
        flash('تم رفض اقتراح جني الربح، والمركز لا يزال مفتوحًا.')
    except Exception as e:
        flash(str(e)[:160])
    return redirect(url_for('home'))

@app.route('/close-suggestions/<symbol>/approve',methods=['POST'])
@auth
def approve_close_suggestion(symbol):
    try:
        check_csrf()
        with db() as conn,conn.cursor() as c:
            c.execute("SELECT 1 FROM bot_close_suggestions WHERE symbol=%s AND status='PENDING' AND created_at>NOW()-INTERVAL '10 minutes'",(symbol,))
            if not c.fetchone(): raise ValueError('اقتراح الإغلاق انتهت صلاحيته')
        return close_position(symbol)
    except Exception as e:
        flash(str(e)[:160])
        return redirect(url_for('home'))

PAGE='''<!doctype html><html lang="ar" dir="rtl"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>البوت الكويتي</title><style>
body{background:#101827;color:#f5f7fb;font:16px system-ui;margin:0}main{max-width:850px;margin:auto;padding:16px}article{background:#202d40;padding:16px;border-radius:14px;margin:14px 0;overflow:auto}button,input{font:inherit;padding:10px;border:0;border-radius:8px;margin:4px}button{background:#238657;color:white}button.danger{background:#b64040}a{color:#9fc8ff}table{width:100%;border-collapse:collapse}td,th{padding:8px;border-bottom:1px solid #435069;text-align:right}small{color:#bdc9da}.warn{color:#ffcf83}

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
  function apply(){let pref;try{pref=localStorage.getItem('alkuwaiti-theme')}catch(e){};let theme=pref==='light'||pref==='dark'?pref:(hour()>=6&&hour()<18?'light':'dark');document.documentElement.dataset.theme=theme;let b=document.getElementById('theme-switch');if(b)b.textContent='🎨 المظهر: '+(theme==='light'?'نهاري':'ليلي')+' (اضغط للتغيير)'}
  window.toggleAlkuwaitiTheme=function(){let theme=document.documentElement.dataset.theme==='light'?'dark':'light';try{localStorage.setItem('alkuwaiti-theme',theme)}catch(e){};apply()};
  apply();document.addEventListener('DOMContentLoaded',apply);setInterval(apply,60000);
})();</script><main><button id="theme-switch" type="button" class="theme-switch" onclick="toggleAlkuwaitiTheme()">🎨 المظهر</button><h1>🇰🇼 البوت الكويتي V38 (تداول حقيقي مفعل)</h1><p class="warn">{{'⚠️ التداول الحقيقي مفعل تماماً' if live else '🔒 التداول الحقيقي معطل'}}</p>
{% for m in get_flashed_messages() %}<article>{{m}}</article>{% endfor %}
{% if logged %}<article id="binance-ban-panel" style="border:2px solid #d97706" aria-live="polite"><h2>⏳ حالة اتصال Binance / حظر IP</h2><p id="binance-ban-message">جاري التحقق من حالة Binance...</p><p id="binance-ban-timer" style="font-size:1.6em;font-weight:bold">—</p><small>العداد يستند إلى تاريخ انتهاء الحظر الذي ترسله Binance.</small></article>{% endif %}
{% if not logged %}<article><h2>دخول المدير</h2><form method="post" action="{{url_for('login')}}"><input type="hidden" name="csrf" value="{{csrf}}"><input type="email" name="email" placeholder="البريد" required><input type="password" name="password" placeholder="كلمة المرور" required><button>دخول</button></form></article>
{% else %}<p><a href="{{url_for('logout')}}" class="logout-link">🚪 تسجيل خروج المسؤول</a> | <a href="/investors">واجهة المستثمرين</a></p>
<article style="border:2px solid #d97706">
<h2>🛑 مفتاح إيقاف البوت</h2>
{% if prefs and prefs.is_paused %}
<p style="color:#fca5a5;font-weight:bold">🔴 البوت متوقف: لا فحص تلقائي ولا فرص أو صفقات دخول جديدة.</p>
{% else %}
<p style="color:#86efac;font-weight:bold">🟢 البوت يعمل بكامل صلاحيات التداول.</p>
{% endif %}
<form method="post" action="{{url_for('toggle_bot')}}">
<input type="hidden" name="csrf" value="{{csrf}}">
{% if prefs and prefs.is_paused %}
<input type="hidden" name="action" value="resume"><button type="submit" style="background:#15803d" onclick="return confirm('هل تريد استئناف التداول والفحص؟')">▶️ إعادة تشغيل البوت</button>
{% else %}
<input type="hidden" name="action" value="stop"><button type="submit" class="danger" onclick="return confirm('إيقاف البوت بالكامل؟')">🛑 إيقاف البوت بالكامل</button>
{% endif %}
</form>
</article>

<article><h2>🛡️ إدارة البوت الكويتي</h2>
<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px">
<a href="{{url_for('owner_investors')}}" style="display:block;background:#315174;color:white;text-align:center;padding:14px;border-radius:10px;text-decoration:none;font-weight:bold">👥 إدارة المستثمرين والاشتراكات</a>
<a href="{{url_for('owner_requests_page')}}" style="display:block;background:#315174;color:white;text-align:center;padding:14px;border-radius:10px;text-decoration:none;font-weight:bold">✅ الطلبات والموافقات</a>
<a href="{{url_for('owner_payouts')}}" style="display:block;background:#315174;color:white;text-align:center;padding:14px;border-radius:10px;text-decoration:none;font-weight:bold">💰 مستحقاتي وطرق الاستلام</a>
</div></article><article><h2>💰 رصيد Binance المؤكد</h2><p id="v37-balance">{{balance}}</p><small id="v37-account-time">آخر تحديث: {{account_snapshot_at or 'لم يتم التحديث بعد'}}</small><p><small id="v37-account-status">الاتصال المباشر متوازن وبدون قيود...</small></p><form method="post" action="/api/v34-snapshot-refresh"><input type="hidden" name="csrf" value="{{csrf}}"><button type="submit">تحديث الرصيد والمراكز يدويًا</button></form></article>
<article><h2>🔑 تغيير مفاتيح Binance API</h2><form method="post" action="{{url_for('update_api_credentials')}}"><input type="hidden" name="csrf" value="{{csrf}}"><input name="api_key" type="password" autocomplete="off" placeholder="API Key الجديد" required style="width:90%"><input name="api_secret" type="password" autocomplete="off" placeholder="API Secret الجديد" required style="width:90%"><button>فحص الاتصال وحفظ المفاتيح الجديدة</button></form></article>
<article><h2>⚙️ إعدادات رأس المال والرافعة</h2>
<form method="post" action="{{url_for('save_preferences')}}">
<input type="hidden" name="csrf" value="{{csrf}}">
<label>الحد الأقصى للتعرض الاسمي من المحفظة (%)</label><input type="number" name="max_exposure_pct" min="1" max="100" step="1" value="{{prefs.max_exposure_pct}}" required>
<label>الحد الأعلى للرافعة (1 إلى 150)</label><input type="number" name="max_leverage" min="1" max="150" step="1" value="{{prefs.max_leverage}}" required>
<label>هدف صافي العائد على هامش الصفقة (%)</label><input type="number" name="target_roi_pct" min="1" max="100" step="0.5" value="{{prefs.target_roi_pct}}" required>
<button>حفظ الإعدادات</button></form>
</article>

<article><h2>📈 أسعار الأسواق المباشرة (Yahoo Finance)</h2>
<table><thead><tr><th>السوق</th><th>السعر بالدولار تقريبًا</th><th>وقت البيانات</th></tr></thead><tbody id="external-prices"><tr><td colspan="3">جاري جلب الأسعار...</td></tr></tbody></table><small id="price-state"></small></article>

<article><h2>📊 أرباح وخسائر اليوم</h2><div id="daily-pnl">جاري العرض...</div><form method="post" action="/api/v34-pnl-refresh"><input type="hidden" name="csrf" value="{{csrf}}"><button type="submit">تحديث أرباح وخسائر اليوم</button></form></article>
<article><h2>الأسواق المتابعة</h2><p>BTCUSDT, ETHUSDT, BNBUSDT, SOLUSDT, XRPUSDT, XAUUSDT, XAGUSDT, CLUSDT, BZUSDT</p></article>
<article><h2>🔎 الفحص التلقائي لفرص التداول</h2><p id="auto-scan-status">⏳ الفحص الآمن لبايننس قيد التشغيل بمعدل متوازن...</p></article>
<article id="entry-suggestions"><h2>فرص بانتظار الموافقة للتنفيذ الفعلي</h2><table><tr><th>الرمز</th><th>الاتجاه</th><th>الدخول التقريبي</th><th>الهدف</th><th>وقف الخسارة</th><th>صافي الهدف</th><th>الأمر</th></tr>{% for x in pending %}<tr><td>{{x.symbol}}</td><td>{{x.direction}}</td><td>{{x.entry_reference}}</td><td>{{x.take_reference}}</td><td>{{x.stop_reference}}</td><td>≥ {{prefs.target_roi_pct}}%</td><td><form method="post" action="{{url_for('approve',order_id=x.id)}}"><input type="hidden" name="csrf" value="{{csrf}}"><button style="background:#e11d48">موافقة وتنفيذ فوري ⚡</button></form><form method="post" action="{{url_for('reject',order_id=x.id)}}"><input type="hidden" name="csrf" value="{{csrf}}"><button class="danger">رفض</button></form></td></tr>{% else %}<tr><td colspan="7">لا توجد فرص حالياً</td></tr>{% endfor %}</table></article>
<article><h2>مراكز Binance الحالية</h2><table><tr><th>الرمز</th><th>الكمية</th><th>الدخول</th><th>ربح/خسارة غير محققة</th><th>إغلاق</th></tr>{% for p in positions %}<tr><td>{{p.symbol}}</td><td>{{p.positionAmt}}</td><td>{{p.entryPrice}}</td><td>{{p.unRealizedProfit}} USDT</td><td><form method="post" action="{{url_for('close_position',symbol=p.symbol)}}"><input type="hidden" name="csrf" value="{{csrf}}"><button class="danger">إغلاق الآن</button></form></td></tr>{% else %}<tr><td colspan="5">لا توجد مراكز مفتوحة في آخر لقطة مؤكدة</td></tr>{% endfor %}</table></article>
<article><h2>🕒 مراقبة تلقائية</h2>
<p>آخر فحص: {{scan_state.checked_at if scan_state and scan_state.checked_at else 'لم يبدأ بعد'}}</p>
<p>فرص جديدة: {{scan_state.opportunities if scan_state else 0}}</p>
<div id="watch-status" aria-live="polite">بدء المراقبة...</div></article>
<article id="exit-suggestions"><h2>🔔 اقتراحات إغلاق جني الأرباح</h2>
<div id="live-exit-rows">جارٍ الفحص...</div>
</article>
<article><h2>آخر الأوامر المنفذة</h2><table><tr><th>الرمز</th><th>الاتجاه</th><th>الحالة</th><th>ملاحظة</th></tr>{% for x in recent %}<tr><td>{{x.symbol}}</td><td>{{x.direction}}</td><td>{{x.status}}</td><td>{{x.note or '—'}}</td></tr>{% endfor %}</table></article>{% endif %}</main><script>
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
       for(const x of data.entries){const tr=document.createElement('tr');td(tr,x.symbol);td(tr,x.direction);td(tr,x.entry_reference);td(tr,x.take_reference);td(tr,x.stop_reference);td(tr,'هدف صافي تقديري');td(tr,'');action(tr,'/approve/'+encodeURIComponent(x.id),'موافقة وتنفيذ فوري ⚡',false);action(tr,'/reject/'+encodeURIComponent(x.id),'رفض',true);entry.appendChild(tr)}
       if(!data.entries.length){const tr=document.createElement('tr');const c=td(tr,'لا توجد فرص مناسبة حاليًا');c.colSpan=7;entry.appendChild(tr)}
     }
     const exit=document.getElementById('exit-suggestions');
     if(exit){let holder=document.getElementById('live-exit-rows');if(!holder){holder=document.createElement('div');holder.id='live-exit-rows';exit.insertBefore(holder,exit.children[1]||null)}holder.replaceChildren();
       for(const x of data.exits){const p=document.createElement('p');p.textContent=x.symbol+' | صافي تقديري '+(Number(x.estimated_net_roi)*100).toFixed(2)+'% | '+x.estimated_net_usdt+' USDT';holder.appendChild(p);
         const tr=document.createElement('div');td(tr,'');action(tr,'/close-suggestions/'+encodeURIComponent(x.symbol)+'/approve','موافقة وإغلاق الآن',true);action(tr,'/close-suggestions/'+encodeURIComponent(x.symbol)+'/reject','رفض الاقتراح',false);holder.appendChild(tr)}
       if(!data.exits.length){const p=document.createElement('p');p.textContent='لا توجد اقتراحات إغلاق تحقق الهدف حالياً.';holder.appendChild(p)}
     }
     watch.textContent='المراقبة تعمل | آخر فحص: '+(data.last_scan?new Date(data.last_scan).toLocaleTimeString('ar-KW'):'لم يبدأ');
   }catch(e){watch.textContent='تعذر تحديث الفرص مؤقتًا';}
   finally{loading=false;}
 }
 async function trigger(){
   if(watching||document.hidden)return;
   watching=true;
   const label=document.getElementById('auto-scan-status');
   try{
     const response=await fetch('/api/browser-watch',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:'csrf='+encodeURIComponent(csrf),credentials:'same-origin',cache:'no-store'});
     const result=await response.json();
     if(!response.ok)throw Error(result.error||'تعذر الفحص');
     if(label)label.textContent=result.status==='binance_scans_disabled'?'الفحص التلقائي متوقف':(result.status==='binance_banned'?'⏳ Binance محظور مؤقتًا':'✅ تم الفحص التلقائي الآمن لشموع بايننس');
   }catch(e){if(label)label.textContent='⚠️ فشل الفحص: '+e.message;}
   finally{watching=false;refresh();}
 }
 const symbols=[['بيتكوين','BTCUSDT'],['إيثريوم','ETHUSDT'],['BNB','BNBUSDT'],['سولانا','SOLUSDT'],['XRP','XRPUSDT'],['الذهب','XAUUSDT'],['الفضة','XAGUSDT'],['نفط WTI','CLUSDT'],['نفط برنت','BZUSDT']];
 async function refreshMarket(){
   const tbody=document.getElementById('external-prices');
   if(!tbody)return;
   try{
     const r=await fetch('/api/market-prices',{credentials:'same-origin',cache:'no-store'});
     if(!r.ok)throw Error('price request');
     const data=await r.json();tbody.replaceChildren();
     for(const [name,sym] of symbols){
       const tr=document.createElement('tr'),item=data.prices[sym];
       td(tr,name+' '+sym);td(tr,item&&item.price!==null?Number(item.price).toLocaleString('en-US',{maximumFractionDigits:6}):'غير متاح');
       td(tr,item&&item.asof?new Date(item.asof).toLocaleTimeString('ar-KW'):'—');tbody.appendChild(tr);
     }
     document.getElementById('price-state').textContent='المصدر: Yahoo Finance | آخر تحديث: '+(data.checked_at?new Date(data.checked_at).toLocaleTimeString('ar-KW'):'غير متاح');
   }catch(e){document.getElementById('price-state').textContent='تعذر تحديث الأسعار مؤقتًا';}
 }
 async function refreshPnL(){
   try{
     const r=await fetch('/api/daily-pnl',{credentials:'same-origin',cache:'no-store'});
     if(!r.ok)throw Error('pnl');
     const p=await r.json();
     document.getElementById('daily-pnl').textContent='أرباح '+p.gains+' USDT | خسائر '+p.losses+' USDT | رسوم وتمويل '+p.fees_funding+' USDT | الصافي '+p.net+' USDT';
   }catch(e){document.getElementById('daily-pnl').textContent='لا توجد نتائج يومية مؤكدة بعد.';}
 }
 refreshMarket();refreshPnL();setInterval(refreshMarket,30000);setInterval(refreshPnL,120000);
 refresh();trigger();setInterval(refresh,45000);setInterval(trigger,300000);
})();
</script><script>(function(){
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
     if(!r.ok)throw Error('تعذر قراءة الحساب');
     const x=await r.json();
     if(x.balance!==null){bal.textContent=x.balance+' USDT';at.textContent='آخر لقطة محفوظة: '+new Date(x.captured_at).toLocaleString('ar-KW');}
     else{bal.textContent='بانتظار التحديث...';}
     status.textContent=x.message||'حالة الحساب';
   }catch(e){status.textContent='تعذر التحديث مؤقتًا';}
   finally{busy=false;}
 }
 poll();
})();
</script><script>(function(){
 let until=0,reason='';const msg=document.getElementById('binance-ban-message'),timer=document.getElementById('binance-ban-timer');
 if(!msg||!timer)return;
 function render(){if(!until){timer.textContent='لا يوجد حظر مسجل';msg.textContent='الاتصال سليم 100%.';return}
 const seconds=Math.max(0,Math.ceil((until-Date.now())/1000));
 const h=Math.floor(seconds/3600),m=Math.floor((seconds%3600)/60),s=seconds%60;
 timer.textContent=seconds?`${String(h).padStart(2,'0')}:${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`:'انتهى الوقت';
 msg.textContent=reason;
 }
 async function update(){try{const r=await fetch('/api/binance-ban-status',{cache:'no-store',credentials:'same-origin'});if(!r.ok)return;const d=await r.json();until=Number(d.until_ms)||0;reason=d.reason||'';render()}catch(e){}}
 update();render();setInterval(render,1000);setInterval(update,30000);
})();</script></html>'''

_price_lock=threading.Lock()
_price_cache={'at':0.0,'data':{}}

@app.route('/api/browser-watch',methods=['POST'])
@auth
def browser_watch():
    try:
        check_csrf()
        if is_bot_paused(): return {'status':'paused','scanned':False}
        if not BINANCE_MARKET_SCANS_ENABLED:
            return {'status':'binance_scans_disabled','scanned':False}
        if _cooldown_state()['remaining_seconds']>0:
            return {'status':'binance_banned','scanned':False}
        init()
        result=auto_scan()
        should_scan=result.get('status')=='ok'
        if _cooldown_state()['remaining_seconds']==0:
            try: monitor_close_suggestions()
            except Exception: logging.exception('Browser close monitor failed')
        return {'status':'ok','scanned':should_scan}
    except Exception:
        logging.exception('Browser watch failed')
        return {'error':'تعذرت المراقبة'},503

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
        return {'entries':[{k:str(v) for k,v in x.items()} for x in entries],
                'exits':[{k:str(v) for k,v in x.items()} for x in exits],
                'last_scan':status['checked_at'].isoformat() if status and status['checked_at'] else None}
    except Exception:
        logging.exception('Suggestions refresh failed')
        return {'error':'تعذر جلب الفرص'},503

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
        return {'prices':_external_cache['prices'],'checked_at':_external_cache['checked_at'],'source':'Yahoo Finance','realtime':False}

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
        if now-_v37_last_check<120:return
        _v37_last_check=now
    try:
        _assert_binance_available()
        if not BINANCE_AUTO_ACCOUNT_ENABLED:return
        if _snapshot_reserve('v37_auto_account',1200):
            try:
                account=signed('GET','/fapi/v2/account')
                positions_data=signed('GET','/fapi/v2/positionRisk')
                visible=[{k:str(p.get(k,'')) for k in ('symbol','positionAmt','entryPrice','unRealizedProfit')}
                         for p in positions_data if D(str(p.get('positionAmt','0')))!=0]
                _snapshot_put('account',{'balance':str(account['totalWalletBalance']),'positions':visible})
            except Exception as e:
                logging.warning('V37 automatic account update deferred: %s',str(e)[:140])
        if _snapshot_reserve('v37_auto_pnl',3600):
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
        return {'balance':None,'captured_at':None,'message':'استخدم زر التحديث اليدوي للحفظ...'}
    except Exception:
        logging.exception('V37 snapshot read failed')
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
        flash('فشل تحديث Binance: '+str(e)[:120])
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
INVESTOR_TERMS="هذه منصة التداول الحقيقي."

INVESTOR_PAGE="""<!doctype html><html lang="ar" dir="rtl"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>المستثمرون | البوت الكويتي</title>
<style>body{font-family:system-ui;background:#101827;color:#f3f6ff;margin:0;padding:20px}main{max-width:820px;margin:auto}article{background:#1d2b40;padding:20px;border-radius:14px;margin:16px 0}input,button{font:inherit;padding:12px;margin:7px 0;max-width:100%;box-sizing:border-box}input{width:100%;border-radius:8px}button{background:#36bb9a;border:0;border-radius:8px;cursor:pointer}table{width:100%;border-collapse:collapse}td,th{padding:10px;border-bottom:1px solid #41506a}small{color:#b6c6d9}a{color:#70d9ff}label{display:block;margin:8px 0}.terms{max-height:160px;overflow:auto;background:#101827;padding:12px;border-radius:8px}
</style><main><h1>🇰🇼 البوت الكويتي — المستثمرون</h1>
{% for msg in get_flashed_messages() %}<article>{{msg}}</article>{% endfor %}
{% if user %}<article><h2>حسابك</h2><p>{{user.email}}</p><p><a href="{{url_for('investor_logout')}}" class="logout-link">🚪 تسجيل خروج</a></p></article>
{% else %}<article><h2>تسجيل الدخول</h2><form method="post" action="{{url_for('investor_login')}}"><input type="hidden" name="csrf" value="{{csrf}}"><input type="email" name="email" placeholder="البريد الإلكتروني" required><input type="password" name="password" placeholder="كلمة المرور" required><button>دخول</button></form></article>{% endif %}
<article style="border:1px solid #577b9e"><h2>🔐 دخول المسؤول</h2>
{% if admin_logged %}<p><a href="{{url_for('home')}}">لوحة التحكم والتداول الحقيقي</a></p>{% else %}<form method="post" action="{{url_for('login')}}"><input type="hidden" name="csrf" value="{{csrf}}"><label>بريد المسؤول<input type="email" name="email" required></label><label>كلمة المرور<input type="password" name="password" required></label><button type="submit">دخول الإدارة</button></form></article>{% endif %}</main></html>"""

@app.route('/investors')
def investors():
    user=None
    if session.get('investor_id'):
        try:
            init_investors()
            with db() as conn,conn.cursor(cursor_factory=RealDictCursor) as c:
                c.execute('SELECT email,subscription_status,trial_end,paid_until FROM investor_accounts WHERE id=%s',(session['investor_id'],))
                user=c.fetchone()
            if user is None: session.pop('investor_id', None)
        except Exception:
            pass
    return render_template_string(INVESTOR_PAGE,user=user,csrf=csrf(),terms=INVESTOR_TERMS,admin_logged=bool(session.get('admin')))

@app.route('/investors/login',methods=['POST'])
def investor_login():
    try:
        check_csrf();init_investors()
        with db() as conn,conn.cursor() as c:
            c.execute('SELECT id,password_hash FROM investor_accounts WHERE email=%s',(request.form.get('email','').strip().lower(),))
            row=c.fetchone()
        if not row or not check_password_hash(row[1],request.form.get('password','')):raise ValueError('بيانات غير صحيحة')
        session.clear();session['investor_id']=row[0];session.permanent=True;csrf()
    except Exception as e:flash(str(e)[:140])
    return redirect(url_for('investors'))

@app.route('/investors/logout')
def investor_logout():
    session.clear();return redirect(url_for('investors'))

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

OWNER_PAGE="""<!doctype html><html lang=ar dir=rtl><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>إدارة المستثمرين</title><main><h1>لوحة المالك</h1><p><a href='/'>الرئيسية</a></p></main></html>"""

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
    labels={'SUBSCRIPTION':'اشتراكات','INVESTMENT_DECLARED':'استثمار','TRADE_PROFIT':'ربح','TRADE_LOSS':'خسارة','PROFIT_SHARE_PROPOSED':'حصة'}
    return render_template_string(OWNER_PAGE,settings=settings,count=count,users=users,totals=totals,records=records,labels=labels,csrf=csrf())

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
          created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), decided_at TIMESTAMPTZ)""")

@app.route('/owner/requests')
@auth
def owner_requests_page():
    init_approval_workflow()
    with db() as conn,conn.cursor(cursor_factory=RealDictCursor) as c:
        c.execute('SELECT * FROM owner_payment_methods ORDER BY id DESC');methods=c.fetchall()
        c.execute('SELECT r.*,a.email,m.label AS method FROM investor_requests r JOIN investor_accounts a ON a.id=r.investor_id LEFT JOIN owner_payment_methods m ON m.id=r.method_id ORDER BY r.id DESC LIMIT 300');rows=c.fetchall()
    return render_template_string("<html><body><h1>الطلبات</h1><a href='/owner/investors'>إدارة المستثمرين</a></body></html>")

@app.route('/owner/payouts')
@auth
def owner_payouts():
    return render_template_string("<html><body><h1>المستحقات</h1><a href='/owner/investors'>إدارة المستثمرين</a></body></html>")

@app.route('/toggle-bot', methods=['POST'])
@auth
def toggle_bot():
    try:
        check_csrf()
        action = request.form.get('action', '')
        if action not in ('stop', 'resume'):
            raise ValueError('أمر غير صالح')
        paused = action == 'stop'
        init()
        with db() as conn, conn.cursor() as c:
            c.execute('UPDATE bot_preferences SET is_paused=%s, updated_at=NOW() WHERE id=1', (paused,))
            if paused:
                c.execute("UPDATE bot_orders_v2 SET status='CANCELLED', note='Cancelled by admin stop' WHERE status='PENDING'")
        flash('🛑 تم إيقاف البوت.' if paused else '▶️ تم استئناف العمل.')
    except Exception:
        flash('تعذر تغيير الحالة.')
    return redirect(url_for('home'))

@app.route('/health')
def health(): return {'status':'web_running','version':'V38','live_trading_enabled':LIVE,'execution_audited':EXECUTION_AUDITED,'trading_orders_enabled':True}

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
    pending=[];recent=[];pos=[];close_suggestions=[];scan_state=None;bal='غير متاح'
    try:
        init()
        with db() as conn,conn.cursor(cursor_factory=RealDictCursor) as c:
            c.execute("SELECT * FROM bot_orders_v2 WHERE status='PENDING' AND created_at>NOW()-INTERVAL '15 minutes' ORDER BY id DESC LIMIT 25");pending=c.fetchall()
            c.execute('SELECT * FROM bot_orders_v2 ORDER BY id DESC LIMIT 25');recent=c.fetchall()
            c.execute("SELECT * FROM bot_close_suggestions WHERE status='PENDING' AND created_at>NOW()-INTERVAL '10 minutes' ORDER BY estimated_net_roi DESC");close_suggestions=c.fetchall()
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
    return render_template_string(PAGE,logged=True,csrf=csrf(),live=LIVE,balance=bal,account_snapshot_at=snapshot_at,positions=pos,pending=pending,recent=recent,prefs=preferences() if scan_state is not None else None,close_suggestions=close_suggestions,scan_state=scan_state)

@app.route('/settings/api',methods=['POST'])
@auth
def update_api_credentials():
    try:
        check_csrf()
        master=os.environ.get('CREDENTIALS_ENCRYPTION_KEY','')
        if not master: raise ValueError('أضف CREDENTIALS_ENCRYPTION_KEY إلى البيئة أولًا')
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
        _observe_binance_weight(r)
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

@app.route('/approve/<int:order_id>',methods=['POST'])
@auth
def approve(order_id):
    try:
        check_csrf()
        if is_bot_paused(): raise ValueError('البوت متوقف')
        init()
        with db() as conn,conn.cursor(cursor_factory=RealDictCursor) as c:
            c.execute("UPDATE bot_orders_v2 SET status='SUBMITTING' WHERE id=%s AND status='PENDING' RETURNING *",(order_id,))
            row=c.fetchone()
            if not row: raise ValueError('الفرصة غير متاحة أو نفذت')
        try:
            order,protect,qty=entry_order(row)
            with db() as conn,conn.cursor() as c:
                c.execute("UPDATE bot_orders_v2 SET status='OPEN',order_id=%s,stop_algo_id=%s,quantity=%s,entry_price=%s,note='Live executed' WHERE id=%s",(str(order.get('orderId')),str(protect.get('algoId')),qty,order.get('avgPrice','0'),order_id))
            flash('⚡ تم تنفيذ الصفقة بنجاح على Binance ووضع وقف الخسارة!')
        except Exception as e:
            with db() as conn,conn.cursor() as c: c.execute("UPDATE bot_orders_v2 SET status='REQUIRES_REVIEW',note=%s WHERE id=%s",(str(e)[:400],order_id))
            raise
    except Exception as e: flash('فشل تنفيذ الصفقة: '+str(e)[:220])
    return redirect(url_for('home'))

@app.route('/reject/<int:order_id>',methods=['POST'])
@auth
def reject(order_id):
    try:
        check_csrf()
        with db() as conn,conn.cursor() as c: c.execute("UPDATE bot_orders_v2 SET status='REJECTED' WHERE id=%s AND status='PENDING'",(order_id,))
        flash('تم رفض الفرصة.')
    except Exception as e: flash(str(e)[:180])
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
            c.execute("SELECT id,stop_algo_id FROM bot_orders_v2 WHERE symbol=%s AND status='OPEN' ORDER BY id DESC LIMIT 1",(symbol,))
            tracked=c.fetchone()
        warning=''
        if tracked and tracked['stop_algo_id']:
            try: signed('DELETE','/fapi/v1/algoOrder',{'symbol':symbol,'algoId':tracked['stop_algo_id']})
            except Exception as e: warning=' تنبيه وقف الخسارة: '+str(e)[:90]
        with db() as conn,conn.cursor() as c:
            c.execute("UPDATE bot_orders_v2 SET status='CLOSED',close_id=%s,note=%s WHERE symbol=%s AND status='OPEN'",(str(order.get('orderId')),'Manual close'+warning,symbol))
        flash('تم إرسال أمر إغلاق المركز بنجاح.'+warning)
    except Exception as e: flash('تعذر إغلاق المركز: '+str(e)[:200])
    return redirect(url_for('home'))

if __name__=='__main__': app.run(host='0.0.0.0',port=int(os.environ.get('PORT','5000')))
