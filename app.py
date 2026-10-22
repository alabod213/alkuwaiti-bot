"""Alkuwaiti Bot - human approved USDT-M futures execution.
Requires explicit LIVE_TRADING_ENABLED=true. Test with Binance testnet first.
"""
import os, time, hmac, hashlib, secrets, logging, threading, smtplib, ssl
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
app.secret_key=os.environ.get('SECRET_KEY') or secrets.token_hex(32)  # Set SECRET_KEY in Render for persistent sessions
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
LIVE=os.environ.get('LIVE_TRADING_ENABLED','false').lower()=='true'
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

def allowed_leverage(symbol, requested):
    # Binance leverage brackets impose symbol-specific limits.
    brackets=signed('GET','/fapi/v1/leverageBracket',{'symbol':symbol})
    item=brackets[0] if isinstance(brackets,list) else brackets
    cap=max(int(x['initialLeverage']) for x in item['brackets'])
    return max(1,min(int(requested),cap,150))

def signed(method,path,params=None):
    KEY,SECRET=credentials()
    if not KEY or not SECRET: raise ValueError('مفاتيح Binance غير موجودة في Render')
    p=dict(params or {}); p['timestamp']=int(time.time()*1000); p['recvWindow']=5000
    q=urlencode(p); sig=hmac.new(SECRET.encode(),q.encode(),hashlib.sha256).hexdigest()
    r=requests.request(method,BASE+path,params=q+'&signature='+sig,headers={'X-MBX-APIKEY':KEY},timeout=15)
    if not r.ok: raise ValueError('Binance '+str(r.status_code)+': '+r.text[:220])
    return r.json()

def public(path,p=None):
    r=requests.get(BASE+path,params=p,timeout=12); r.raise_for_status(); return r.json()

def auth(f):
    @wraps(f)
    def wrap(*a,**kw):
        if not session.get('admin'): return redirect(url_for('login'))
        return f(*a,**kw)
    return wrap

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
    if not item: raise ValueError('هذا الرمز غير متاح للتداول حاليًا على Binance Futures')
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
    if equity<=0 or free<=0: raise ValueError('لا يوجد رصيد متاح')
    ps=positions()
    if any(x['symbol']==symbol for x in ps): raise ValueError('مركز مفتوح على نفس الرمز')
    exposure=sum(abs(D(x['positionAmt']))*D(x['markPrice']) for x in ps)
    # Reserve notional for other pending opening orders, not protective/closing orders.
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
    # Smallest possible valid order, round up to step.
    from decimal import ROUND_UP
    qty=max(minimum,(min_value/mark/step).to_integral_value(rounding=ROUND_UP)*step)
    lev=allowed_leverage(symbol,prefs['max_leverage'])
    if qty*mark>room or qty*mark/D(lev)>free*D('0.95'):
        raise ValueError('الحد الأدنى يتجاوز سقف التعرض أو الهامش المتاح')
    # Approximate net ROI on initial margin, not an assurance of profit.
    gross=abs(take-entry)/entry
    net=(gross-ESTIMATED_ROUND_TRIP_COST)*D(lev)
    if net<D(str(prefs['target_roi_pct']))/100:
        raise ValueError('الهدف الفني لا يحقق نسبة الربح الصافي المستهدفة على الهامش')
    return qty,mark,net

def entry_order(row):
    if not LIVE: raise ValueError('التداول الحقيقي معطّل؛ يلزم اختبار التنفيذ ووقف الخسارة قبل تفعيله')
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
        raise ValueError('بعد تغير السعر لم يعد هدف الصفقة يحقق 5% صافي تقديري')
    filters={f['filterType']:f for f in info['filters']}
    # Ensure protective stop is on correct side of live mark price.
    stop=D(row['stop_reference']); side='BUY' if row['direction']=='LONG' else 'SELL'
    if (side=='BUY' and stop>=mark) or (side=='SELL' and stop<=mark):
        raise ValueError('سعر وقف الخسارة لم يعد صالحًا بعد تحرك السوق؛ أعد التحليل')
    price_filter=filters['PRICE_FILTER']; tick=D(price_filter['tickSize'])
    stop=floor_step(stop,tick)
    if stop<=0: raise ValueError('وقف الخسارة غير صالح')
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
            raise ValueError('خطر: فشل وقف الخسارة والإغلاق الطارئ! افتح Binance فورًا وتحقق من المركز')
        raise ValueError('فشل إنشاء وقف الخسارة؛ أرسلنا أمر إغلاق طارئ. تحقق من Binance: '+str(stop_error)[:90])
    return order,protect,qty


def auto_scan():
    """Called only by a scheduler using a private token; no orders are sent."""
    init()
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
                        rejected[sym]='لا توجد إشارة'
                        continue
                    direction,entry,stop,take=signal
                    feasibility(sym,entry,stop,take,info)
                    c.execute("SELECT 1 FROM bot_orders_v2 WHERE symbol=%s AND status IN ('PENDING','SUBMITTING','OPEN','CLOSING','REQUIRES_REVIEW') LIMIT 1",(sym,))
                    if c.fetchone():
                        rejected[sym]='فرصة أو مركز سابق قيد المتابعة'
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
        if now-_close_check_at<5: return {'status':'cached'}
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
    # The existing close handler sends the actual order only if live trading is enabled.
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
body{background:#101827;color:#f5f7fb;font:16px system-ui;margin:0}main{max-width:850px;margin:auto;padding:16px}article{background:#202d40;padding:16px;border-radius:14px;margin:14px 0;overflow:auto}button,input{font:inherit;padding:10px;border:0;border-radius:8px;margin:4px}button{background:#238657;color:white}button.danger{background:#b64040}a{color:#9fc8ff}table{width:100%;border-collapse:collapse}td,th{padding:8px;border-bottom:1px solid #435069;text-align:right}small{color:#bdc9da}.warn{color:#ffcf83}</style><main><h1>🇰🇼 البوت الكويتي</h1><p class="warn">{{'⚠️ التداول الحقيقي مفعل' if live else '🔒 التداول الحقيقي معطل — وضع التحليل فقط'}}</p>
{% for m in get_flashed_messages() %}<article>{{m}}</article>{% endfor %}
{% if not logged %}<article><h2>دخول المدير</h2><form method="post" action="{{url_for('login')}}"><input type="hidden" name="csrf" value="{{csrf}}"><input type="email" name="email" placeholder="البريد" required><input type="password" name="password" placeholder="كلمة المرور" required><button>دخول</button></form></article>
{% else %}<a href="{{url_for('logout')}}">خروج</a>
<article><h2>🛡️ إدارة البوت الكويتي</h2><p><small>روابط الإدارة الخاصة بك — لا تظهر للمستثمرين.</small></p>
<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px">
<a href="{{url_for('owner_investors')}}" style="display:block;background:#315174;color:white;text-align:center;padding:14px;border-radius:10px;text-decoration:none;font-weight:bold">👥 إدارة المستثمرين والاشتراكات</a>
<a href="{{url_for('owner_requests_page')}}" style="display:block;background:#315174;color:white;text-align:center;padding:14px;border-radius:10px;text-decoration:none;font-weight:bold">✅ الطلبات والموافقات</a>
<a href="{{url_for('owner_payouts')}}" style="display:block;background:#315174;color:white;text-align:center;padding:14px;border-radius:10px;text-decoration:none;font-weight:bold">💰 مستحقاتي وطرق الاستلام</a>
</div></article><article><h2>الرصيد</h2><p>{{balance}}</p><small>رصيد المحفظة USDT وليس الربح المحقق</small></article>
<article><h2>🔑 تغيير مفاتيح Binance API</h2><p><small>يمكنك استبدال المفاتيح متى شئت. لا تُعرض المفاتيح القديمة، وتُحفظ الجديدة مشفرة في Neon. يلزم CREDENTIALS_ENCRYPTION_KEY في Render.</small></p><form method="post" action="{{url_for('update_api_credentials')}}"><input type="hidden" name="csrf" value="{{csrf}}"><input name="api_key" type="password" autocomplete="off" placeholder="API Key الجديد" required style="width:90%"><input name="api_secret" type="password" autocomplete="off" placeholder="API Secret الجديد" required style="width:90%"><button>فحص الاتصال وحفظ المفاتيح الجديدة</button></form><small>أنشئ المفاتيح بصلاحية Futures فقط، وعطّل السحب Withdrawal. تغيير المفاتيح لا يلغي الصفقات المفتوحة.</small></article>
<article><h2>⚙️ إعدادات رأس المال والرافعة</h2>
<p><small>القيم التالية حدود قصوى وليست تعليمات لاستثمار الرصيد كاملًا. لا يمكن إضافة USDT حقيقي من لوحة البوت؛ الإيداع والتحويل يتمان من Binance فقط.</small></p>
<form method="post" action="{{url_for('save_preferences')}}">
<input type="hidden" name="csrf" value="{{csrf}}">
<label>الحد الأقصى للتعرض الاسمي من المحفظة (%)</label><input type="number" name="max_exposure_pct" min="1" max="100" step="1" value="{{prefs.max_exposure_pct}}" required>
<label>الحد الأعلى للرافعة (1 إلى 150، حسب حدود Binance)</label><input type="number" name="max_leverage" min="1" max="150" step="1" value="{{prefs.max_leverage}}" required>
<label>هدف صافي العائد على هامش الصفقة (%)</label><input type="number" name="target_roi_pct" min="1" max="100" step="0.5" value="{{prefs.target_roi_pct}}" required>
<button>حفظ إعدادات التحليل</button></form>
<p class="warn">تحذير: الرافعة 150x قد تؤدي إلى تصفية سريعة وخسارة الهامش. البوت لا يستطيع زيادة رصيد Binance أو ضمان 10% ربح.</p>
</article>
<article><h2>📈 الأسعار داخل البوت</h2><p><small>بيانات Yahoo Finance العامة غير الرسمية، قد تكون متأخرة أو غير متاحة؛ تختلف عن عقود Binance. يتم تحديث الشاشة كل ثانية، وجلب المصدر كل 30 ثانية تقريبًا.</small></p>
<table><thead><tr><th>السوق</th><th>السعر بالدولار تقريبًا</th><th>وقت البيانات</th></tr></thead><tbody id="external-prices"><tr><td colspan="3">جاري جلب الأسعار...</td></tr></tbody></table><small id="price-state">مصدر خارجي، وليس سعر تنفيذ Binance.</small></article>
<article><h2>📊 أرباح وخسائر اليوم</h2><p><small>اليوم بتوقيت الكويت، من سجل دخل Binance Futures: الربح والخسارة المحققة، العمولات، والتمويل. لا يشمل أرباح المراكز غير المغلقة.</small></p><div id="daily-pnl">جاري احتساب النتائج...</div></article>
<article><h2>الأسواق المتابعة</h2><p>عملات رقمية: BTCUSDT، ETHUSDT، BNBUSDT، SOLUSDT، XRPUSDT</p><p>ذهب: XAUUSDT | فضة: XAGUSDT</p><p>نفط WTI: CLUSDT | نفط برنت: BZUSDT</p><small>يتم استبعاد أي رمز غير متاح في Binance Futures لحسابك.</small></article>
<article><h2>🔎 الفحص التلقائي لفرص التداول</h2><p id="auto-scan-status">⏳ يبدأ التحليل تلقائيًا بعد فتح الصفحة...</p><small>لا تحتاج إلى الضغط على أي زر. تُحدث الشاشة كل ثانية ويُجرى فحص فني كامل تقريبًا كل دقيقة أثناء بقاء الصفحة مفتوحة ونشطة. قد لا توجد فرص تستوفي الشروط. لا تُرسل أوامر شراء أو بيع إلا بعد موافقتك، ومع تفعيل التداول الحقيقي بشكل منفصل.</small></article>
<article id="entry-suggestions"><h2>فرص بانتظار الموافقة</h2><table><tr><th>الرمز</th><th>الاتجاه</th><th>الدخول التقريبي</th><th>الهدف</th><th>وقف الخسارة</th><th>صافي الهدف التقديري</th><th>الأمر</th></tr>{% for x in pending %}<tr><td>{{x.symbol}}</td><td>{{x.direction}}</td><td>{{x.entry_reference}}</td><td>{{x.take_reference}}</td><td>{{x.stop_reference}}</td><td>≥ {{prefs.target_roi_pct}}% على الهامش (تقديري)</td><td><form method="post" action="{{url_for('approve',order_id=x.id)}}"><input type="hidden" name="csrf" value="{{csrf}}"><button>موافقة وفتح فورًا</button></form><form method="post" action="{{url_for('reject',order_id=x.id)}}"><input type="hidden" name="csrf" value="{{csrf}}"><button class="danger">رفض</button></form></td></tr>{% else %}<tr><td colspan="7">لا توجد فرص</td></tr>{% endfor %}</table></article>
<article><h2>مراكز Binance المفتوحة</h2><table><tr><th>الرمز</th><th>الكمية</th><th>الدخول</th><th>ربح/خسارة غير محققة</th><th>إغلاق</th></tr>{% for p in positions %}<tr><td>{{p.symbol}}</td><td>{{p.positionAmt}}</td><td>{{p.entryPrice}}</td><td>{{p.unRealizedProfit}} USDT</td><td><form method="post" action="{{url_for('close_position',symbol=p.symbol)}}"><input type="hidden" name="csrf" value="{{csrf}}"><button class="danger">إغلاق الآن</button></form></td></tr>{% else %}<tr><td colspan="5">لا توجد مراكز أو تعذر جلبها</td></tr>{% endfor %}</table><small>الإغلاق Market؛ السعر النهائي والرسوم قد يختلفان. أوامر الحماية قد تغلق المركز تلقائيًا.</small></article>
<article><h2>🕒 مراقبة تلقائية أثناء فتح الصفحة</h2>
<p>آخر فحص: {{scan_state.checked_at if scan_state and scan_state.checked_at else 'لم يبدأ التحليل بعد'}}</p>
<p>فرص جديدة في آخر فحص: {{scan_state.opportunities if scan_state else 0}}</p>
<details><summary>أسباب استبعاد الفرص</summary><small>{{scan_state.reason if scan_state else 'لا توجد بيانات'}}</small></details>
<div id="watch-status" aria-live="polite">بدء المراقبة...</div><small>يتحدث وقت الشاشة كل ثانية؛ التحليل الفني لا يزال يعتمد على بيانات شموع Binance كل 60 ثانية تقريبًا (Google لا يقدم شموعًا آلية موثوقة)، وتُراجع اقتراحات الإغلاق كل 5 ثوانٍ ما دامت الصفحة مفتوحة. لا تنفذ المراقبة أي أوامر.</small></article>
<article id="exit-suggestions"><h2>🔔 اقتراحات إغلاق بانتظار قرارك</h2>
<div id="live-exit-rows">جارٍ فحص المراكز...</div>
<small>الربح تقديري؛ الرسوم الفعلية والتمويل والانزلاق قد تغير النتيجة. يمكنك إغلاق أي مركز يدويًا من جدول المراكز.</small>
</article>
<article><h2>آخر الأوامر</h2><table><tr><th>الرمز</th><th>الاتجاه</th><th>الحالة</th><th>ملاحظة</th></tr>{% for x in recent %}<tr><td>{{x.symbol}}</td><td>{{x.direction}}</td><td>{{x.status}}</td><td>{{x.note or '—'}}</td></tr>{% endfor %}</table></article>{% endif %}</main><script>
(function(){
 const el=document.getElementById('google-clock');
 if(!el)return;
 function tick(){if(!document.hidden)el.textContent='وقت الشاشة: '+new Date().toLocaleTimeString('ar-KW');}
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
       for(const x of data.entries){const tr=document.createElement('tr');td(tr,x.symbol);td(tr,x.direction);td(tr,x.entry_reference);td(tr,x.take_reference);td(tr,x.stop_reference);td(tr,'هدف صافي تقديري');td(tr,'');action(tr,'/approve/'+encodeURIComponent(x.id),'موافقة وفتح فورًا',false);action(tr,'/reject/'+encodeURIComponent(x.id),'رفض',true);entry.appendChild(tr)}
       if(!data.entries.length){const tr=document.createElement('tr');const c=td(tr,'لا توجد فرص مناسبة حاليًا');c.colSpan=7;entry.appendChild(tr)}
     }
     const exit=document.getElementById('exit-suggestions');
     if(exit){let holder=document.getElementById('live-exit-rows');if(!holder){holder=document.createElement('div');holder.id='live-exit-rows';exit.insertBefore(holder,exit.children[1]||null)}holder.replaceChildren();
       for(const x of data.exits){const p=document.createElement('p');p.textContent=x.symbol+' | صافي تقديري '+(Number(x.estimated_net_roi)*100).toFixed(2)+'% | '+x.estimated_net_usdt+' USDT';holder.appendChild(p);
         const tr=document.createElement('div');td(tr,'');action(tr,'/close-suggestions/'+encodeURIComponent(x.symbol)+'/approve','موافقة وإغلاق الآن',true);action(tr,'/close-suggestions/'+encodeURIComponent(x.symbol)+'/reject','رفض الاقتراح',false);holder.appendChild(tr)}
       if(!data.exits.length){const p=document.createElement('p');p.textContent='لا توجد اقتراحات إغلاق تحقق الهدف حاليًا.';holder.appendChild(p)}
     }
     watch.textContent='المراقبة تعمل | آخر فحص فني: '+(data.last_scan?new Date(data.last_scan).toLocaleTimeString('ar-KW'):'لم يبدأ');
   }catch(e){watch.textContent='تعذر تحديث الفرص مؤقتًا';}
   finally{loading=false;}
 }
 async function trigger(){
   if(watching||document.hidden)return;
   watching=true;
   const label=document.getElementById('auto-scan-status');
   if(label)label.textContent='⏳ المراقبة تعمل؛ جاري التحقق من فرص التداول...';
   try{
     const response=await fetch('/api/browser-watch',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:'csrf='+encodeURIComponent(csrf),credentials:'same-origin',cache:'no-store'});
     const result=await response.json();
     if(!response.ok)throw Error(result.error||'تعذر الفحص');
     if(label)label.textContent=result.scanned?'✅ اكتمل الفحص الفني التلقائي':'🟢 المراقبة تعمل؛ الفحص الفني التالي عند اكتمال الدقيقة';
   }catch(e){if(label)label.textContent='⚠️ فشل الفحص التلقائي: '+e.message;}
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
     document.getElementById('price-state').textContent='آخر تحديث من المصدر: '+(data.checked_at?new Date(data.checked_at).toLocaleTimeString('ar-KW'):'غير متاح')+' | البيانات قد تكون متأخرة';
   }catch(e){document.getElementById('price-state').textContent='تعذر تحديث الأسعار الخارجية مؤقتًا';}
 }
 async function refreshPnL(){
   try{
     const r=await fetch('/api/daily-pnl',{credentials:'same-origin',cache:'no-store'});
     if(!r.ok)throw Error('pnl');
     const p=await r.json();
     document.getElementById('daily-pnl').textContent='أرباح محققة: '+p.gains+' USDT | خسائر محققة: '+p.losses+' USDT | عمولات وتمويل: '+p.fees_funding+' USDT | الصافي: '+p.net+' USDT | '+p.day;
   }catch(e){document.getElementById('daily-pnl').textContent='تعذر قراءة نتائج اليوم من Binance';}
 }
 refreshMarket();refreshPnL();setInterval(refreshMarket,1000);setInterval(refreshPnL,60000);
 refresh();trigger();setInterval(refresh,1000);setInterval(trigger,5000);
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
        # Cross-process rate limiting: only one browser scan per 60 seconds.
        init()
        should_scan=False
        with db() as conn,conn.cursor() as c:
            c.execute("SELECT pg_try_advisory_xact_lock(68210315)")
            if c.fetchone()[0]:
                c.execute("SELECT checked_at FROM bot_scan_status WHERE id=1 FOR UPDATE")
                last=c.fetchone()[0]
                if last is None or (datetime.now(timezone.utc)-last).total_seconds()>=60:
                    c.execute("UPDATE bot_scan_status SET checked_at=NOW() WHERE id=1")
                    should_scan=True
        if should_scan:
            try: auto_scan()
            except Exception:
                logging.exception('Browser market scan failed')
                with db() as conn,conn.cursor() as c:
                    c.execute("UPDATE bot_scan_status SET checked_at=NULL,reason=%s WHERE id=1",('تعذر الفحص التلقائي؛ راجع سجلات Render',))
                return {'error':'فشل التحليل الفني؛ راجع سجلات Render'},503
        try: monitor_close_suggestions()
        except Exception: logging.exception('Browser close monitor failed')
        return {'status':'ok','scanned':should_scan}
    except Exception:
        logging.exception('Browser watch failed')
        return {'error':'تعذرت المراقبة مؤقتًا'},503

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
        return {'error':'تعذر جلب الفرص'},503

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

@app.route('/api/daily-pnl')
@auth
def daily_pnl():
    now=time.monotonic()
    if _pnl_cache['data'] is not None and now-_pnl_cache['at']<60:
        return _pnl_cache['data']
    from zoneinfo import ZoneInfo
    local_now=datetime.now(ZoneInfo('Asia/Kuwait'))
    day_start=local_now.replace(hour=0,minute=0,second=0,microsecond=0)
    start_ms=int(day_start.timestamp()*1000)
    end_ms=int(local_now.timestamp()*1000)
    # Binance income API returns signed USDT values; include realized PNL, fees, funding.
    totals={'REALIZED_PNL':D('0'),'COMMISSION':D('0'),'FUNDING_FEE':D('0')}
    gains=D('0');losses=D('0')
    # Limit 1000 rows per request. If history exceeds this, report incomplete rather than wrong totals.
    rows=signed('GET','/fapi/v1/income',{'startTime':start_ms,'endTime':end_ms,'limit':1000})
    if len(rows)>=1000:
        return {'error':'نتائج اليوم تتجاوز 1000 سجل؛ يلزم ترقيم الصفحات'},409
    for row in rows:
        typ=row.get('incomeType')
        if typ not in totals or row.get('asset')!='USDT':continue
        amount=D(str(row['income']))
        totals[typ]+=amount
        if typ=='REALIZED_PNL':
            if amount>0:gains+=amount
            elif amount<0:losses+=amount
    net=sum(totals.values(),D('0'))
    fmt=lambda n:format(n.quantize(D('0.0001')),'f')
    data={'day':day_start.strftime('%Y-%m-%d')+' (الكويت)','gains':fmt(gains),'losses':fmt(losses),
          'fees_funding':fmt(totals['COMMISSION']+totals['FUNDING_FEE']),'net':fmt(net)}
    _pnl_cache.update(at=now,data=data)
    return data

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
INVESTOR_TERMS="""هذه نسخة تجريبية لعرض الأسعار وإدارة حساب الاشتراك فقط. لا يودع المستخدم أموالاً ولا يفوض البوت بالتداول نيابة عنه بموجب هذه الموافقة. البنود المقترحة بشأن مشاركة 50% من الأرباح وإعادة استثمار رأس المال ومنع السحب غير نافذة في هذه النسخة، وتتطلب اتفاقية مستقلة ومراجعة قانونية وترخيصاً ملائماً حيث يلزم. الأسعار الخارجية إرشادية وقد تتأخر. الاشتراكات المدفوعة غير مفعلة حتى تتضح الأسعار وشروط الدفع والإلغاء والاسترداد."""

INVESTOR_PAGE="""<!doctype html><html lang="ar" dir="rtl"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>المستثمرون | البوت الكويتي</title>
<style>body{font-family:system-ui;background:#101827;color:#f3f6ff;margin:0;padding:20px}main{max-width:820px;margin:auto}article{background:#1d2b40;padding:20px;border-radius:14px;margin:16px 0}input,button{font:inherit;padding:12px;margin:7px 0;max-width:100%;box-sizing:border-box}input{width:100%;border-radius:8px}button{background:#36bb9a;border:0;border-radius:8px;cursor:pointer}table{width:100%;border-collapse:collapse}td,th{padding:10px;border-bottom:1px solid #41506a}small{color:#b6c6d9}a{color:#70d9ff}label{display:block;margin:8px 0}.terms{max-height:160px;overflow:auto;background:#101827;padding:12px;border-radius:8px}</style>
<main><div style='text-align:left'><label>🌐 Language <select id='language' onchange='setLang(this.value)' style='padding:9px;border-radius:8px'><option value='ar'>العربية</option><option value='en'>English</option><option value='fr'>Français</option><option value='es'>Español</option><option value='hi'>हिन्दी</option><option value='ur'>اردو</option><option value='tr'>Türkçe</option><option value='zh'>中文</option><option value='ru'>Русский</option><option value='pt'>Português</option><option value='de'>Deutsch</option><option value='id'>Bahasa Indonesia</option></select></label></div><h1>🇰🇼 البوت الكويتي — المستثمرون</h1><p><small>الحد الأدنى للاستثمار المقترح: 50 USDT. الإيداع وإدارة الأموال غير مفعلين.</small></p>
{% for msg in get_flashed_messages() %}<article>{{msg}}</article>{% endfor %}
{% if user %}<article><h2>حسابك</h2><p>{{user.email}}</p><p>حالة الاشتراك: {{user.subscription_status}}</p><p>انتهاء التجربة: {{user.trial_end}}</p><p>انتهاء الاشتراك المدفوع: {{user.paid_until or 'غير مفعل'}}</p><p><small>لا توجد محفظة إيداع أو سحب أو تنفيذ تداول في حساب المستثمر.</small></p><p><a href='/investors/requests'>طلبات الاشتراك ومبالغ الاستثمار</a></p><a href="{{url_for('investor_logout')}}">تسجيل الخروج</a></article>
<article><h2>أسعار الأسواق</h2><small>من Yahoo Finance غير الرسمي؛ قد تتأخر الأسعار أو تكون غير متاحة. تتحدث الشاشة كل ثانية والمصدر نحو كل 30 ثانية.</small><table><thead><tr><th>السوق</th><th>السعر الإرشادي</th><th>وقت المصدر</th></tr></thead><tbody id="prices"><tr><td colspan="3">جاري التحميل...</td></tr></tbody></table></article>
{% else %}<article><h2>إنشاء حساب جديد</h2><form method="post" action="{{url_for('investor_register')}}"><input type="hidden" name="csrf" value="{{csrf}}"><label>البريد الإلكتروني<input type="email" name="email" autocomplete="email" required maxlength="254"></label><label>كلمة المرور (12 حرفاً على الأقل)<input type="password" name="password" autocomplete="new-password" minlength="12" required></label><div class="terms">{{terms}}</div><label><input style="width:auto" type="checkbox" name="accept" value="yes" required> قرأت ووافقت على شروط النسخة التجريبية أعلاه</label><button>تسجيل وبدء 7 أيام تجريبية</button></form></article>
<article><h2>تسجيل الدخول</h2><form method="post" action="{{url_for('investor_login')}}"><input type="hidden" name="csrf" value="{{csrf}}"><input type="email" name="email" placeholder="البريد الإلكتروني" required><input type="password" name="password" placeholder="كلمة المرور" required><button>دخول</button></form><p><a href="{{url_for('investor_forgot')}}">نسيت كلمة المرور؟ / Forgot password?</a></p></article>{% endif %}
<article style="border:1px solid #577b9e"><h2>🔐 دخول مسؤول البوت</h2><p><small>هذه الخانة خاصة بالمسؤول فقط. لا تستخدم بيانات المستثمر للدخول إلى الإدارة.</small></p>
{% if admin_logged %}<p>أنت مسجل دخولك كمسؤول.</p><p><a href="{{url_for('home')}}">لوحة التداول</a> | <a href="{{url_for('owner_investors')}}">إدارة المستثمرين</a> | <a href="{{url_for('owner_requests_page')}}">الموافقات</a></p>
{% else %}<form method="post" action="{{url_for('login')}}"><input type="hidden" name="csrf" value="{{csrf}}"><label>بريد المسؤول<input type="email" name="email" autocomplete="username" required></label><label>كلمة مرور المسؤول<input type="password" name="password" autocomplete="current-password" required></label><button type="submit">الدخول إلى الإدارة</button></form>{% endif %}</article>
<article><h2>العروض المقترحة — الدفع غير مفعل</h2><table><tr><th>العرض</th><th>السعر المبدئي</th></tr><tr><td>7 أيام تجربة</td><td>مجاناً</td></tr><tr><td>أسبوع</td><td>19.99$</td></tr><tr><td>3 أسابيع + الرابع مجاناً</td><td>يحتاج تأكيد طريقة الحساب</td></tr><tr><td>3 أشهر + الرابع مجاناً</td><td>49.99$ — يحتاج تأكيد هل السعر شهري أم إجمالي</td></tr><tr><td>عام + شهران مجاناً</td><td>99.99$ — يحتاج تأكيد هل السعر سنوي</td></tr></table><p><small>خصم نهاية الأسبوع المقترح: 10%، لم يُفعّل لعدم وجود دفع بعد.</small></p></article></main>
{% if user %}<script>const markets=[['بيتكوين','BTCUSDT'],['إيثريوم','ETHUSDT'],['BNB','BNBUSDT'],['سولانا','SOLUSDT'],['XRP','XRPUSDT'],['الذهب','XAUUSDT'],['الفضة','XAGUSDT'],['WTI','CLUSDT'],['برنت','BZUSDT']];async function prices(){if(document.hidden)return;try{const r=await fetch('/api/market-prices',{cache:'no-store'});if(!r.ok)throw Error('network');const d=await r.json(),body=document.getElementById('prices');body.replaceChildren();for(const [name,sym] of markets){const tr=document.createElement('tr'),p=d.prices[sym];for(const value of [name,p&&p.price!=null?Number(p.price).toLocaleString('en-US',{maximumFractionDigits:6}):'غير متاح',p&&p.asof?new Date(p.asof).toLocaleTimeString('ar-KW'):'—']){const td=document.createElement('td');td.textContent=value;tr.appendChild(td)}body.appendChild(tr)}}catch(e){}}prices();setInterval(prices,1000)</script>{% endif %}<script>
const labels={
 en:['Kuwaiti Bot — Investors','Create a new account','Sign in','Email','Password','Login','Forgot password?','Market prices','Subscription offers'],
 fr:['Bot Koweïtien — Investisseurs','Créer un compte','Connexion','E-mail','Mot de passe','Connexion','Mot de passe oublié ?','Prix des marchés','Offres d’abonnement'],
 es:['Bot Kuwaití — Inversores','Crear cuenta','Iniciar sesión','Correo','Contraseña','Entrar','¿Olvidó su contraseña?','Precios de mercado','Ofertas de suscripción'],
 hi:['कुवैती बॉट — निवेशक','नया खाता','लॉग इन','ईमेल','पासवर्ड','लॉग इन','पासवर्ड भूल गए?','बाजार भाव','सदस्यता'],
 ur:['کویتی بوٹ — سرمایہ کار','نیا اکاؤنٹ','لاگ ان','ای میل','پاس ورڈ','داخل ہوں','پاس ورڈ بھول گئے؟','مارکیٹ قیمتیں','سبسکرپشن'],
 tr:['Kuveyt Botu — Yatırımcılar','Yeni hesap','Giriş','E-posta','Şifre','Giriş','Şifremi unuttum','Piyasa fiyatları','Abonelikler'],
 zh:['科威特机器人 — 投资者','创建账户','登录','电子邮件','密码','登录','忘记密码？','市场价格','订阅方案'],
 ru:['Кувейтский бот — Инвесторы','Создать аккаунт','Войти','Почта','Пароль','Войти','Забыли пароль?','Цены рынков','Подписки'],
 pt:['Bot Kuwaitiano — Investidores','Criar conta','Entrar','E-mail','Senha','Entrar','Esqueceu a senha?','Preços de mercado','Planos'],
 de:['Kuwait-Bot — Anleger','Konto erstellen','Anmelden','E-Mail','Passwort','Anmelden','Passwort vergessen?','Marktpreise','Abonnements'],
 id:['Bot Kuwait — Investor','Buat akun','Masuk','Email','Kata sandi','Masuk','Lupa kata sandi?','Harga pasar','Langganan']};
const originals=['البوت الكويتي — المستثمرون','إنشاء حساب جديد','تسجيل الدخول','البريد الإلكتروني','كلمة المرور','دخول','نسيت كلمة المرور؟','أسعار الأسواق','العروض المقترحة'];
function setLang(lang){localStorage.setItem('investor_lang',lang);document.documentElement.lang=lang;document.documentElement.dir=['ar','ur'].includes(lang)?'rtl':'ltr';const dict=labels[lang];if(!dict)return;document.querySelectorAll('h1,h2,label,button,a').forEach(el=>{if(el.children.length>0 && el.tagName!=='A')return;let text=el.textContent.trim();for(let i=0;i<originals.length;i++)if(text.includes(originals[i])){el.textContent=text.replace(originals[i],dict[i]);break;}})}
try{const lang=localStorage.getItem('investor_lang')||((navigator.language||'ar').split('-')[0]);document.getElementById('language').value=labels[lang]?lang:'ar';setLang(lang)}catch(e){}
</script></html>"""

@app.route('/investors')
def investors():
    user=None
    if session.get('investor_id'):
        init_investors()
        with db() as conn,conn.cursor(cursor_factory=RealDictCursor) as c:
            c.execute('SELECT email,subscription_status,trial_end,paid_until FROM investor_accounts WHERE id=%s',(session['investor_id'],))
            user=c.fetchone()
    return render_template_string(INVESTOR_PAGE,user=user,csrf=csrf(),terms=INVESTOR_TERMS,admin_logged=bool(session.get('admin')))

@app.route('/investors/register',methods=['POST'])
def investor_register():
    try:
        check_csrf()
        if request.form.get('accept')!='yes':raise ValueError('يجب الموافقة على شروط التجربة')
        email=request.form.get('email','').strip().lower();pw=request.form.get('password','')
        if '@' not in email or len(email)>254 or len(pw)<12 or len(pw)>256:raise ValueError('تحقق من البريد وكلمة المرور (12 حرفاً على الأقل)')
        init_investors()
        with db() as conn,conn.cursor() as c:
            c.execute('INSERT INTO investor_accounts(email,password_hash,accepted_terms_version) VALUES(%s,%s,%s) RETURNING id',(email,generate_password_hash(pw),INVESTOR_TERMS_VERSION))
            uid=c.fetchone()[0]
        session.clear();session['investor_id']=uid;session.permanent=True;csrf()
        flash('تم إنشاء حساب العرض التجريبي')
    except psycopg2.errors.UniqueViolation:
        flash('البريد مسجل بالفعل')
    except Exception as e:flash('تعذر التسجيل: '+str(e)[:140])
    return redirect(url_for('investors'))

@app.route('/investors/login',methods=['POST'])
def investor_login():
    try:
        check_csrf();init_investors()
        with db() as conn,conn.cursor() as c:
            c.execute('SELECT id,password_hash FROM investor_accounts WHERE email=%s',(request.form.get('email','').strip().lower(),))
            row=c.fetchone()
        if not row or not check_password_hash(row[1],request.form.get('password','')):raise ValueError('بيانات الدخول غير صحيحة')
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

RESET_PAGE="""<!doctype html><html lang='ar' dir='rtl'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>استعادة كلمة المرور</title><style>body{background:#101827;color:white;font:16px system-ui;padding:20px}main{max-width:520px;margin:auto}article{background:#1d2b40;padding:24px;border-radius:14px}input,button{box-sizing:border-box;width:100%;padding:13px;margin:8px 0;border-radius:8px}button{background:#36bb9a;border:0}a{color:#70d9ff}</style><main><article><h1>{{heading}}</h1><p>{{help}}</p>{% for m in get_flashed_messages() %}<p>{{m}}</p>{% endfor %}<form method='post'><input type='hidden' name='csrf' value='{{csrf}}'>{% if token %}<input type='hidden' name='token' value='{{token}}'><label>كلمة المرور الجديدة (12 حرفًا على الأقل)</label><input name='password' type='password' minlength='12' maxlength='256' autocomplete='new-password' required><label>تأكيد كلمة المرور</label><input name='confirm' type='password' minlength='12' maxlength='256' autocomplete='new-password' required>{% else %}<label>البريد الإلكتروني المسجل</label><input name='email' type='email' required maxlength='254' autocomplete='email'>{% endif %}<button>{{'تغيير كلمة المرور' if token else 'إرسال رابط الاستعادة'}}</button></form><a href='/investors'>العودة للمستثمرين</a></article></main></html>"""

def send_reset_mail(email,token):
    host=os.environ.get('SMTP_HOST','');user=os.environ.get('SMTP_USER','')
    password=os.environ.get('SMTP_PASSWORD','');sender=os.environ.get('SMTP_FROM','')
    if not all([host,user,password,sender]):
        app.logger.warning('Password reset SMTP not configured')
        return False
    from urllib.parse import quote
    url=url_for('investor_reset',token=token,_external=True)
    msg=EmailMessage();msg['Subject']='Alkuwaiti Bot - Password reset / استعادة كلمة المرور'
    msg['From']=sender;msg['To']=email
    msg.set_content('رابط تغيير كلمة المرور صالح لمدة 20 دقيقة ويستخدم مرة واحدة فقط:\n'+url+'\n\nIf you did not request this, ignore this email.')
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
        flash('إذا كان البريد مسجلاً وخدمة البريد مفعلة، سيصلك رابط الاستعادة. راجع الرسائل غير المرغوبة.')
        return redirect(url_for('investor_forgot'))
    return render_template_string(RESET_PAGE,heading='نسيت كلمة المرور؟',help='أدخل بريدك لإرسال رابط استعادة صالح لمدة 20 دقيقة.',csrf=csrf(),token=None)

@app.route('/investors/reset-password/<token>',methods=['GET','POST'])
def investor_reset(token):
    if len(token)>100 or len(token)<30:return 'رابط غير صالح',400
    digest=hashlib.sha256(token.encode()).hexdigest()
    if request.method=='POST':
        check_csrf()
        pw=request.form.get('password','')
        if len(pw)<12 or len(pw)>256 or pw!=request.form.get('confirm'):
            flash('تأكد من تطابق كلمة المرور وأنها 12 حرفًا على الأقل')
            return redirect(url_for('investor_reset',token=token))
        init_password_resets()
        with db() as conn,conn.cursor() as c:
            c.execute("""SELECT id,investor_id FROM investor_password_resets
                WHERE token_hash=%s AND used_at IS NULL AND expires_at>NOW() FOR UPDATE""",(digest,))
            row=c.fetchone()
            if not row:return 'الرابط منتهي الصلاحية أو مستخدم بالفعل',400
            c.execute('UPDATE investor_accounts SET password_hash=%s WHERE id=%s',(generate_password_hash(pw),row[1]))
            c.execute('UPDATE investor_password_resets SET used_at=NOW() WHERE investor_id=%s AND used_at IS NULL',(row[1],))
        session.clear()
        flash('تم تغيير كلمة المرور. سجّل الدخول بكلمة المرور الجديدة.')
        return redirect(url_for('investors'))
    return render_template_string(RESET_PAGE,heading='تعيين كلمة مرور جديدة',help='الرابط صالح لمدة 20 دقيقة ويُستخدم مرة واحدة.',csrf=csrf(),token=token)

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

OWNER_PAGE="""<!doctype html><html lang=ar dir=rtl><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>إدارة المستثمرين | البوت الكويتي</title>
<style>body{font-family:system-ui;background:#101827;color:#eef3fa;padding:18px}main{max-width:1100px;margin:auto}article{background:#203047;border-radius:12px;padding:16px;margin:14px 0;overflow:auto}table{border-collapse:collapse;width:100%}td,th{padding:10px;border-bottom:1px solid #40536b;text-align:right;white-space:nowrap}input,button{padding:10px;font:inherit;border-radius:6px;max-width:100%}button{background:#31b99a;border:0}label{display:block;margin:12px 0}small{color:#c7d1df}a{color:#84dfff}</style>
<main><h1>🇰🇼 لوحة صاحب البوت — المستثمرون</h1><p><a href='/'>العودة إلى لوحة البوت</a> | <a href='/owner/requests'>الموافقات وطرق الدفع</a></p>
{% for msg in get_flashed_messages() %}<article>{{msg}}</article>{% endfor %}
<article><h2>الحد الأدنى للاستثمار: {{settings.minimum_investment_usdt}} USDT</h2><p><small>الحد الأدنى معلومة ظاهرة في المنصة، وليس طلب تحويل أو تفويض لإدارة أموال.</small></p><p>عدد المستثمرين المسجلين: {{count}}</p></article>
<article><h2>بيانات إيداع رين — محفوظة للإدارة فقط</h2><p><small>لا تعرض للمستثمرين ولا تُستخدم لتحصيل أموال تلقائياً. تأكد من الشبكة والعملة وقواعد رين قبل أي استخدام.</small></p><form method=post action='/owner/rain'><input type=hidden name=csrf value='{{csrf}}'><label>شبكة الإيداع <input name=network value='{{settings.rain_network or ""}}' maxlength=50 placeholder='مثال: شبكة مدعومة فعلاً في رين'></label><label>عنوان الإيداع <input name=address value='{{settings.rain_deposit_address or ""}}' maxlength=256 autocomplete=off placeholder='أدخل العنوان من حساب رين بنفسك'></label><button>حفظ إعدادات رين</button></form></article>
<article><h2>ملخص السجلات (USDT)</h2><table><tr><th>التصنيف</th><th>الإجمالي المسجل</th></tr>{% for item in totals %}<tr><td>{{labels.get(item.category,item.category)}}</td><td>{{item.total}}</td></tr>{% endfor %}</table><small>هذه سجلات إدارية غير متحقَّق منها بنظام دفع أو حسابات تداول منفصلة؛ ليست رصيداً أو دخلاً فعلياً مؤكداً. لا يتم تحويل كل 100 دولار تلقائياً.</small></article>
<article><h2>المستثمرون والاشتراكات</h2><table><tr><th>رقم</th><th>البريد</th><th>الحالة</th><th>انتهاء التجربة</th><th>انتهاء المدفوع</th><th>التسجيل</th></tr>{% for u in users %}<tr><td>{{u.id}}</td><td>{{u.email}}</td><td>{{u.subscription_status}}</td><td>{{u.trial_end}}</td><td>{{u.paid_until or '—'}}</td><td>{{u.created_at}}</td></tr>{% endfor %}</table></article>
<article><h2>أحدث سجلات الأموال والأرباح والخسائر</h2><table><tr><th>المستثمر</th><th>النوع</th><th>المبلغ</th><th>المرجع</th><th>التاريخ</th></tr>{% for r in records %}<tr><td>{{r.email}}</td><td>{{labels.get(r.category,r.category)}}</td><td>{{r.amount_usdt}}</td><td>{{r.reference or '—'}}</td><td>{{r.created_at}}</td></tr>{% endfor %}</table><p><small>لا تُسجَّل عمليات مالية إلا بعد ربط مصدر موثوق للتحقق. مشاركة الأرباح 50% بند مقترح غير مُفعّل.</small></p></article></main></html>"""

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
    labels={'SUBSCRIPTION':'اشتراكات','INVESTMENT_DECLARED':'استثمار مُعلن','TRADE_PROFIT':'ربح صفقة','TRADE_LOSS':'خسارة صفقة','PROFIT_SHARE_PROPOSED':'حصة أرباح مقترحة'}
    return render_template_string(OWNER_PAGE,settings=settings,count=count,users=users,totals=totals,records=records,labels=labels,csrf=csrf())

@app.route('/owner/rain',methods=['POST'])
@auth
def owner_rain():
    check_csrf()
    network=request.form.get('network','').strip()
    address=request.form.get('address','').strip()
    if len(network)>50 or len(address)>256 or (address and not network):
        flash('أدخل الشبكة والعنوان الصحيحين');return redirect(url_for('owner_investors'))
    init_investor_admin()
    with db() as conn,conn.cursor() as c:
        c.execute('UPDATE owner_settings SET rain_network=%s,rain_deposit_address=%s,updated_at=NOW() WHERE id=1',(network or None,address or None))
    flash('حُفظت بيانات رين للإدارة فقط؛ لم يتم تفعيل أي تحصيل')
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
    if not dt:return '—'
    from zoneinfo import ZoneInfo
    return dt.astimezone(ZoneInfo('Asia/Kuwait')).strftime('%Y-%m-%d %H:%M:%S')

@app.route('/investors/requests')
def investor_requests_page():
    if not session.get('investor_id'):return redirect(url_for('investors'))
    init_approval_workflow()
    with db() as conn,conn.cursor(cursor_factory=RealDictCursor) as c:
        c.execute('SELECT id,label,description FROM owner_payment_methods WHERE enabled=TRUE ORDER BY id DESC');methods=c.fetchall()
        c.execute('SELECT kind,status,amount_usdt,investment_mode,pooled_amount_usdt,individual_amount_usdt,created_at,decided_at FROM investor_requests WHERE investor_id=%s ORDER BY id DESC LIMIT 100',(session['investor_id'],));rows=c.fetchall()
    html="""<!doctype html><html lang=ar dir=rtl><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>طلبات المستثمر</title>"""+APPROVAL_STYLE+"""<main><h1>طلبات الاشتراك والاستثمار</h1><p><a href='/investors'>العودة لصفحة المستثمر</a></p><article><strong>الطلبات للمراجعة فقط.</strong> لا ترسل أموالاً ولا توجد تعليمات تحويل أو دفع مفعّلة. الموافقة لا تثبت استلام المال ولا تبدأ تداولاً.</article>{% for m in get_flashed_messages() %}<article>{{m}}</article>{% endfor %}<article><h2>طلب مراجعة اشتراك</h2><form method=post action='/investors/requests/new'><input type=hidden name=csrf value='{{csrf}}'><input type=hidden name=kind value=SUBSCRIPTION><label>طريقة الدفع المفضلة (للتخطيط فقط)</label><select name=method_id><option value=''>لم أحدد</option>{% for m in methods %}<option value='{{m.id}}'>{{m.label}}</option>{% endfor %}</select><button>إرسال طلب الاشتراك للمراجعة</button></form></article><article><h2>طلب مراجعة مبلغ استثمار</h2><form method=post action='/investors/requests/new'><input type=hidden name=csrf value='{{csrf}}'><input type=hidden name=kind value=INVESTMENT><label>المبلغ المقترح (50 USDT على الأقل)</label><input name=amount type=number min=50 step=.01 required><label>طريقة الاستثمار</label><select name=investment_mode required><option value=POOLED>محفظة مشتركة — توزيع النتائج حسب الحصص</option><option value=INDIVIDUAL>صفقات مستقلة محاسبيًا — سجل مخصص لي</option><option value=BOTH>النظامان معًا — أوزع المبلغ بينهما</option></select><label>إذا اخترت النظامين: المبلغ للمحفظة المشتركة (USDT)</label><input name=pooled_amount type=number min=.01 step=.01 placeholder="مثال: 30"><label>المبلغ للصفقات الفردية (USDT)</label><input name=individual_amount type=number min=.01 step=.01 placeholder="مثال: 20"><small>عند اختيار النظامين يجب أن يساوي مجموع المبلغين إجمالي الاستثمار، بحد أدنى إجمالي 50 USDT.</small><p><small>الاختيار للمراجعة فقط، ولا يُفعّل تداولًا أو إيداعًا.</small></p><button>إرسال المبلغ للمراجعة</button></form></article><article><h2>طرق الدفع المقترحة</h2>{% for m in methods %}<p><b>{{m.label}}</b> — {{m.description}}</p>{% else %}<p>لم تُضف طرق بعد.</p>{% endfor %}<small>لا تظهر عناوين المحافظ أو بيانات التحويل هنا حتى يُجهز نظام دفع مرخص وآمن.</small></article><article><h2>سجل طلباتي — بتوقيت الكويت</h2><table><tr><th>النوع</th><th>المبلغ</th><th>النظام</th><th>الحالة</th><th>الإنشاء</th><th>القرار</th></tr>{% for r in rows %}<tr><td>{{r.kind}}</td><td>{{r.amount_usdt or '—'}}</td><td>{{'مشترك' if r.investment_mode=='POOLED' else 'مستقل محاسبيًا' if r.investment_mode=='INDIVIDUAL' else 'النظامان معًا' if r.investment_mode=='BOTH' else '—'}}{% if r.investment_mode=='BOTH' %} (مشترك {{r.pooled_amount_usdt}} / فردي {{r.individual_amount_usdt}}){% endif %}</td><td>{{r.status}}</td><td>{{kwt(r.created_at)}}</td><td>{{kwt(r.decided_at)}}</td></tr>{% endfor %}</table></article></main></html>"""
    return render_template_string(html,methods=methods,rows=rows,csrf=csrf(),kwt=kwt)

@app.route('/investors/requests/new',methods=['POST'])
def investor_request_new():
    if not session.get('investor_id'):return redirect(url_for('investors'))
    check_csrf();init_approval_workflow()
    kind=request.form.get('kind')
    if kind not in ('SUBSCRIPTION','INVESTMENT'):return 'نوع طلب غير صحيح',400
    amount=None;method_id=None;investment_mode=None;pooled_amount=None;individual_amount=None
    if kind=='INVESTMENT':
        try:amount=D(request.form.get('amount',''))
        except Exception:return 'مبلغ غير صالح',400
        if not amount.is_finite() or amount<50 or amount>D('100000000') or amount.as_tuple().exponent < -2:return 'المبلغ يجب أن يكون 50 على الأقل وبدقة سنت',400
        investment_mode=request.form.get('investment_mode')
        if investment_mode not in ('POOLED','INDIVIDUAL','BOTH'):return 'اختر طريقة الاستثمار',400
        if investment_mode=='BOTH':
            try:
                pooled_amount=D(request.form.get('pooled_amount',''))
                individual_amount=D(request.form.get('individual_amount',''))
            except Exception:return 'أدخل المبلغ المخصص لكل نظام',400
            for part in (pooled_amount,individual_amount):
                if not part.is_finite() or part<D('0.01') or part.as_tuple().exponent < -2:
                    return 'يجب إدخال مبلغ صحيح لكل نظام بدقة سنت',400
            if pooled_amount+individual_amount!=amount:
                return 'مجموع مبالغ النظامين لا يساوي إجمالي الاستثمار',400
    else:
        raw=request.form.get('method_id','')
        if raw:
            if not raw.isdecimal():return 'طريقة غير صحيحة',400
            method_id=int(raw)
            with db() as conn,conn.cursor() as c:
                c.execute('SELECT 1 FROM owner_payment_methods WHERE id=%s AND enabled=TRUE',(method_id,))
                if not c.fetchone():return 'طريقة غير متاحة',400
    with db() as conn,conn.cursor() as c:
        c.execute('INSERT INTO investor_requests(investor_id,kind,amount_usdt,method_id,investment_mode,pooled_amount_usdt,individual_amount_usdt) VALUES(%s,%s,%s,%s,%s,%s,%s)',(session['investor_id'],kind,amount,method_id,investment_mode,pooled_amount,individual_amount))
    flash('تم إرسال الطلب للمراجعة، ولا يُطلب منك تحويل أموال')
    return redirect(url_for('investor_requests_page'))

@app.route('/owner/requests')
@auth
def owner_requests_page():
    init_approval_workflow()
    with db() as conn,conn.cursor(cursor_factory=RealDictCursor) as c:
        c.execute('SELECT * FROM owner_payment_methods ORDER BY id DESC');methods=c.fetchall()
        c.execute('SELECT r.*,a.email,m.label AS method FROM investor_requests r JOIN investor_accounts a ON a.id=r.investor_id LEFT JOIN owner_payment_methods m ON m.id=r.method_id ORDER BY r.id DESC LIMIT 300');rows=c.fetchall()
    html="""<!doctype html><html lang=ar dir=rtl><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>موافقات المستثمرين</title>"""+APPROVAL_STYLE+"""<main><h1>🇰🇼 الموافقات وطرق الدفع</h1><p><a href='/owner/investors'>لوحة المستثمرين</a> | <a href="/owner/payouts">مستحقاتي ووجهة التحويل</a></p><article><strong>نظام موافقات مبدئية فقط.</strong> لا توجد تحويلات أو تفعيل استثمار أو اقتطاع أرباح. جميع الأوقات بتوقيت الكويت مع الثواني.</article>{% for msg in get_flashed_messages() %}<article>{{msg}}</article>{% endfor %}<article><h2>إضافة طريقة دفع للعرض</h2><form method=post action='/owner/methods/add'><input type=hidden name=csrf value='{{csrf}}'><input name=label maxlength=80 placeholder='اسم الطريقة مثل رين أو تحويل بنكي' required><input name=description maxlength=200 placeholder='وصف عام فقط — بدون بيانات تحويل'><button>إضافة</button></form><table><tr><th>الطريقة</th><th>الوصف</th><th>الحالة</th><th>تاريخ الإضافة</th><th>إجراء</th></tr>{% for m in methods %}<tr><td>{{m.label}}</td><td>{{m.description}}</td><td>{{'معروضة' if m.enabled else 'مخفية'}}</td><td>{{kwt(m.created_at)}}</td><td><form method=post action='/owner/methods/toggle'><input type=hidden name=csrf value='{{csrf}}'><input type=hidden name=id value='{{m.id}}'><button>{{'إخفاء' if m.enabled else 'إظهار'}}</button></form></td></tr>{% endfor %}</table></article><article><h2>طلبات المستثمرين</h2><table><tr><th>المستثمر</th><th>النوع</th><th>المبلغ USDT</th><th>نظام الاستثمار</th><th>الطريقة</th><th>الحالة</th><th>تاريخ الطلب</th><th>تاريخ القرار</th><th>القرار</th></tr>{% for r in rows %}<tr><td>{{r.email}}</td><td>{{r.kind}}</td><td>{{r.amount_usdt or '—'}}</td><td>{{'مشترك' if r.investment_mode=='POOLED' else 'مستقل محاسبيًا' if r.investment_mode=='INDIVIDUAL' else 'النظامان معًا' if r.investment_mode=='BOTH' else '—'}}{% if r.investment_mode=='BOTH' %} (مشترك {{r.pooled_amount_usdt}} / فردي {{r.individual_amount_usdt}}){% endif %}</td><td>{{r.method or '—'}}</td><td>{{r.status}}</td><td>{{kwt(r.created_at)}}</td><td>{{kwt(r.decided_at)}}</td><td>{% if r.status=='PENDING' %}<form method=post action='/owner/requests/decide'><input type=hidden name=csrf value='{{csrf}}'><input type=hidden name=id value='{{r.id}}'><button name=decision value=APPROVED>موافقة مبدئية</button><button name=decision value=REJECTED>رفض</button></form>{% endif %}</td></tr>{% endfor %}</table></article></main></html>"""
    return render_template_string(html,methods=methods,rows=rows,csrf=csrf(),kwt=kwt)

@app.route('/owner/methods/add',methods=['POST'])
@auth
def owner_method_add():
    check_csrf();init_approval_workflow()
    label=request.form.get('label','').strip();description=request.form.get('description','').strip()
    if not label or len(label)>80 or len(description)>200:return 'البيانات غير صحيحة',400
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
    if decision not in ('APPROVED','REJECTED'):return 'قرار غير صالح',400
    with db() as conn,conn.cursor() as c:
        c.execute("UPDATE investor_requests SET status=%s,decided_at=NOW() WHERE id=%s AND status='PENDING'",(decision,request.form.get('id')))
        if c.rowcount!=1:flash('الطلب غير موجود أو تم اتخاذ قرار بشأنه مسبقاً')
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

OWNER_PAYOUT_HTML="""<!doctype html><html lang=ar dir=rtl><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>المستحقات والتحويلات</title><style>body{font-family:system-ui;background:#101827;color:#fff;padding:18px}main{max-width:1000px;margin:auto}article{background:#203047;padding:18px;border-radius:12px;margin:14px 0}input,select,button{padding:11px;margin:5px;max-width:90%;border-radius:6px}table{width:100%;border-collapse:collapse}td,th{padding:8px;border-bottom:1px solid #607080;text-align:right}a{color:#8de1ff}small{color:#c8d3dd}</style><main><h1>🇰🇼 مستحقات صاحب البوت</h1><p><a href='/owner/investors'>إدارة المستثمرين</a> | <a href='/'>الرئيسية</a></p>{% for m in get_flashed_messages() %}<article>{{m}}</article>{% endfor %}<article><h2>حد التحويل المقترح: 100 USDT</h2><p>المستحقات المؤكدة والمسجلة: <b>{{balance}} USDT</b></p><p>المبالغ غير المتحقق منها لا تدخل في الحساب.</p><small>لا توجد تحويلات آلية أو طلبات سحب فعلية في هذه النسخة. لا يُنشأ طلب دفع إلا بعد إثبات المصدر والملكية والرسوم والموافقات النظامية.</small></article><article><h2>اختيار وجهة استلام مستحقاتك</h2><form method=post action='/owner/payout-settings'><input type=hidden name=csrf value='{{csrf}}'><label>الوجهة <select name=destination_type><option value=RAIN {% if settings.destination_type=='RAIN' %}selected{% endif %}>رين</option><option value=BANK {% if settings.destination_type=='BANK' %}selected{% endif %}>حساب بنكي</option></select></label><p>شبكة رين <input name=rain_network maxlength=50 value='{{settings.rain_network or ""}}' placeholder='الشبكة'></p><p>عنوان إيداع رين <input name=rain_address maxlength=256 value='{{settings.rain_address or ""}}' placeholder='العنوان'></p><p>اسم البنك <input name=bank_name maxlength=120 value='{{settings.bank_name or ""}}' placeholder='اسم البنك'></p><p>IBAN <input name=bank_iban maxlength=34 value='{{settings.bank_iban or ""}}' placeholder='رقم الحساب الدولي'></p><button>حفظ الوجهة فقط</button></form><small>هذه البيانات خاصة بالإدارة ولا تُرسل إلى Binance أو رين أو البنك. احفظها فقط على موقع HTTPS آمن. لا تدخل كلمة مرور أو رمز تحقق أو مفتاح محفظة.</small></article><article><h2>سجل المستحقات حسب المصدر</h2><table><tr><th>النوع</th><th>المبلغ</th><th>حالة التحقق</th><th>المرجع</th><th>الوقت (الكويت)</th></tr>{% for r in rows %}<tr><td>{{r.source_type}}</td><td>{{r.amount_usdt}}</td><td>{{r.verification_status}}</td><td>{{r.reference}}</td><td>{{kwt(r.created_at)}}</td></tr>{% endfor %}</table></article></main></html>"""

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
        flash('بيانات الوجهة غير صحيحة');return redirect(url_for('owner_payouts'))
    if kind=='RAIN' and (not network or not address):
        flash('أدخل عنوان رين والشبكة');return redirect(url_for('owner_payouts'))
    if kind=='BANK' and (not bank or not iban or not iban.isalnum()):
        flash('أدخل اسم البنك ورقم IBAN');return redirect(url_for('owner_payouts'))
    with db() as conn,conn.cursor() as c:
        c.execute('UPDATE owner_payout_settings SET destination_type=%s,rain_network=%s,rain_address=%s,bank_name=%s,bank_iban=%s,updated_at=NOW() WHERE id=1',(kind,network,address,bank,iban))
    flash('تم حفظ وجهة الاستلام. لم يتم إرسال أي أموال.');return redirect(url_for('owner_payouts'))

@app.route('/health')
def health(): return {'status':'ok','live_trading_enabled':LIVE}

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
        filtered=[]
        for candidate in pending:
            try:
                feasibility(candidate['symbol'],D(candidate['entry_reference']),D(candidate['stop_reference']),D(candidate['take_reference']))
                filtered.append(candidate)
            except Exception: pass
        pending=filtered
    except Exception as e: flash('Neon: '+str(e)[:160])
    try: bal=str(wallet())+' USDT';pos=positions()
    except Exception as e: flash('Binance: '+str(e)[:160])
    return render_template_string(PAGE,logged=True,csrf=csrf(),live=LIVE,balance=bal,positions=pos,pending=pending,recent=recent,prefs=preferences(),close_suggestions=close_suggestions,scan_state=scan_state)


@app.route('/settings/api',methods=['POST'])
@auth
def update_api_credentials():
    try:
        check_csrf()
        master=os.environ.get('CREDENTIALS_ENCRYPTION_KEY','')
        if not master: raise ValueError('أضف CREDENTIALS_ENCRYPTION_KEY إلى Render أولًا')
        f=Fernet(master.encode())
        key=request.form.get('api_key','').strip()
        secret=request.form.get('api_secret','').strip()
        if not key or not secret or len(key)>256 or len(secret)>256: raise ValueError('المفاتيح غير صالحة')
        # Test new credentials without replacing the active ones.
        params={'timestamp':int(time.time()*1000),'recvWindow':5000}
        q=urlencode(params)
        sig=hmac.new(secret.encode(),q.encode(),hashlib.sha256).hexdigest()
        r=requests.get(BASE+'/fapi/v2/account',params=q+'&signature='+sig,headers={'X-MBX-APIKEY':key},timeout=15)
        if not r.ok: raise ValueError('فشل اختبار Binance. تحقق من صلاحيات Futures وتقييد IP والبيئة (تجريبي/حقيقي).')
        init()
        with db() as conn,conn.cursor() as c:
            c.execute('''INSERT INTO bot_api_credentials(id,api_key_cipher,api_secret_cipher,updated_at)
                         VALUES(1,%s,%s,NOW()) ON CONFLICT(id) DO UPDATE
                         SET api_key_cipher=EXCLUDED.api_key_cipher,api_secret_cipher=EXCLUDED.api_secret_cipher,updated_at=NOW()''',
                      (psycopg2.Binary(f.encrypt(key.encode())),psycopg2.Binary(f.encrypt(secret.encode()))))
        flash('تم اختبار الاتصال وحفظ مفاتيح Binance الجديدة بشكل مشفر.')
    except (InvalidToken,ValueError) as e:
        flash(str(e)[:180])
    except Exception:
        logging.exception('Credential update failed (no secrets logged)')
        flash('تعذر حفظ المفاتيح. تحقق من إعدادات Neon ومفتاح التشفير.')
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
            raise ValueError('الإعدادات خارج الحدود المسموحة')
        init()
        with db() as conn,conn.cursor() as c:
            c.execute('UPDATE bot_preferences SET max_exposure_pct=%s,max_leverage=%s,target_roi_pct=%s,updated_at=NOW() WHERE id=1',(exposure,leverage,target))
        flash('تم حفظ حدود التحليل. قد لا يسمح Binance بالرافعة المطلوبة لكل رمز.')
    except Exception as e:
        flash('تعذر حفظ الإعدادات: '+str(e)[:150])
    return redirect(url_for('home'))

@app.route('/scan',methods=['POST'])
@auth
def scan():
    try:
        check_csrf();init();count=0
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
        flash(f'أُنشئت {count} فرص. التحليل ليس ضمانًا للربح.')
    except Exception as e: flash(str(e)[:180])
    return redirect(url_for('home'))

@app.route('/reject/<int:order_id>',methods=['POST'])
@auth
def reject(order_id):
    try:
        check_csrf()
        with db() as conn,conn.cursor() as c: c.execute("UPDATE bot_orders_v2 SET status='REJECTED' WHERE id=%s AND status='PENDING'",(order_id,))
        flash('تم رفض الفرصة')
    except Exception as e: flash(str(e)[:180])
    return redirect(url_for('home'))

@app.route('/approve/<int:order_id>',methods=['POST'])
@auth
def approve(order_id):
    try:
        check_csrf()
        if not LIVE: raise ValueError('التنفيذ الحقيقي غير مفعل')
        init()
        with db() as conn,conn.cursor(cursor_factory=RealDictCursor) as c:
            c.execute("SELECT COUNT(*) AS n FROM bot_orders_v2 WHERE created_at >= date_trunc('day',NOW() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC' AND status IN ('SUBMITTING','OPEN','CLOSING','CLOSED','ERROR')")
            if c.fetchone()['n']>=100: raise ValueError('بلغت الحد اليومي 100 محاولة فتح')
            c.execute("UPDATE bot_orders_v2 SET status='SUBMITTING' WHERE id=%s AND status='PENDING' RETURNING *",(order_id,))
            row=c.fetchone()
            if not row: raise ValueError('سبق تنفيذ أو رفض هذه الفرصة')
        try:
            order,protect,qty=entry_order(row)
            with db() as conn,conn.cursor() as c:
                c.execute("UPDATE bot_orders_v2 SET status='OPEN',order_id=%s,stop_algo_id=%s,quantity=%s,entry_price=%s,note='Stop placed' WHERE id=%s",(str(order.get('orderId')),str(protect.get('algoId')),qty,order.get('avgPrice','0'),order_id))
            flash('تم إرسال الفتح وتأكيد أمر وقف الخسارة. تحقق من Binance.')
        except Exception as e:
            with db() as conn,conn.cursor() as c: c.execute("UPDATE bot_orders_v2 SET status='REQUIRES_REVIEW',note=%s WHERE id=%s",(str(e)[:400],order_id))
            raise
    except Exception as e: flash('لم يكتمل فتح الصفقة: '+str(e)[:220])
    return redirect(url_for('home'))

@app.route('/close/<symbol>',methods=['POST'])
@auth
def close_position(symbol):
    try:
        check_csrf()
        if not LIVE: raise ValueError('التداول الحقيقي معطل')
        if symbol not in SYMBOLS: raise ValueError('رمز غير معتمد')
        ps=[p for p in positions() if p['symbol']==symbol]
        if not ps: raise ValueError('المركز مغلق بالفعل')
        p=ps[0]
        if p.get('positionSide') not in ('BOTH',None): raise ValueError('وضع Hedge غير مدعوم')
        amt=D(p['positionAmt'])
        order=signed('POST','/fapi/v1/order',{'symbol':symbol,'side':'SELL' if amt>0 else 'BUY',
            'type':'MARKET','quantity':strnum(abs(amt)),'reduceOnly':'true','newOrderRespType':'RESULT'})
        with db() as conn,conn.cursor(cursor_factory=RealDictCursor) as c:
            c.execute("SELECT id,stop_algo_id FROM bot_orders_v2 WHERE symbol=%s AND status='OPEN' ORDER BY id DESC LIMIT 1",(symbol,))
            tracked=c.fetchone()
        warning=''
        if tracked and tracked['stop_algo_id']:
            try: signed('DELETE','/fapi/v1/algoOrder',{'symbol':symbol,'algoId':tracked['stop_algo_id']})
            except Exception as e: warning=' تنبيه: لم نتأكد من إلغاء وقف الخسارة؛ راجع أوامر Binance: '+str(e)[:90]
        with db() as conn,conn.cursor() as c:
            c.execute("UPDATE bot_orders_v2 SET status='CLOSED',close_id=%s,note=%s WHERE symbol=%s AND status='OPEN'",(str(order.get('orderId')),'Manual close'+warning,symbol))
        flash('أُرسل أمر الإغلاق. راجع Binance للتأكد من التنفيذ.'+warning)
    except Exception as e: flash('تعذر الإغلاق: '+str(e)[:200])
    return redirect(url_for('home'))

if __name__=='__main__': app.run(host='0.0.0.0',port=int(os.environ.get('PORT','5000')))
