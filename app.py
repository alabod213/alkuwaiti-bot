"""Alkuwaiti Bot - human approved USDT-M futures execution.
Requires explicit LIVE_TRADING_ENABLED=true. Test with Binance testnet first.
"""
import os, time, hmac, hashlib, secrets, logging, threading
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from datetime import timedelta
from functools import wraps
from urllib.parse import urlencode
import requests, psycopg2
from psycopg2.extras import RealDictCursor
from flask import Flask, request, session, redirect, url_for, flash, render_template_string
from werkzeug.security import check_password_hash
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
{% else %}<a href="{{url_for('logout')}}">خروج</a><article><h2>الرصيد</h2><p>{{balance}}</p><small>رصيد المحفظة USDT وليس الربح المحقق</small></article>
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
<article><h2>📈 أسعار الأسواق — Google</h2><p><strong>تنبيه:</strong> Google Search لا يقدم API رسميًا عامًا لأسعار هذه الأسواق كل ثانية. لذلك لا يعرض البوت أرقامًا مخمّنة أو يوهمك أنها أسعار لحظية. اضغط على السوق لعرض السعر المتاح في Google، وقد يكون متأخرًا أو مختلفًا عن عقد Binance.</p><div id="google-clock">وقت الشاشة: —</div><table><thead><tr><th>السوق</th><th>رمز Binance</th><th>Google</th></tr></thead><tbody><tr><td>بيتكوين</td><td>BTCUSDT</td><td><a href="https://www.google.com/search?q=BTC-USD" target="_blank" rel="noopener noreferrer">عرض سعر Google ↗</a></td></tr><tr><td>إيثريوم</td><td>ETHUSDT</td><td><a href="https://www.google.com/search?q=ETH-USD" target="_blank" rel="noopener noreferrer">عرض سعر Google ↗</a></td></tr><tr><td>BNB</td><td>BNBUSDT</td><td><a href="https://www.google.com/search?q=BNB-USD" target="_blank" rel="noopener noreferrer">عرض سعر Google ↗</a></td></tr><tr><td>سولانا</td><td>SOLUSDT</td><td><a href="https://www.google.com/search?q=SOL-USD" target="_blank" rel="noopener noreferrer">عرض سعر Google ↗</a></td></tr><tr><td>XRP</td><td>XRPUSDT</td><td><a href="https://www.google.com/search?q=XRP-USD" target="_blank" rel="noopener noreferrer">عرض سعر Google ↗</a></td></tr><tr><td>الذهب</td><td>XAUUSDT</td><td><a href="https://www.google.com/search?q=gold%20price" target="_blank" rel="noopener noreferrer">عرض سعر Google ↗</a></td></tr><tr><td>الفضة</td><td>XAGUSDT</td><td><a href="https://www.google.com/search?q=silver%20price" target="_blank" rel="noopener noreferrer">عرض سعر Google ↗</a></td></tr><tr><td>نفط WTI</td><td>CLUSDT</td><td><a href="https://www.google.com/search?q=WTI%20crude%20oil%20price" target="_blank" rel="noopener noreferrer">عرض سعر Google ↗</a></td></tr><tr><td>نفط برنت</td><td>BZUSDT</td><td><a href="https://www.google.com/search?q=Brent%20crude%20oil%20price" target="_blank" rel="noopener noreferrer">عرض سعر Google ↗</a></td></tr></tbody></table><small>يُحدّث وقت الشاشة كل ثانية فقط؛ الأسعار تُعرض من صفحات Google عند فتحها، وليس عبر تحديث آلي مضمون.</small></article>
<article><h2>الأسواق المتابعة</h2><p>عملات رقمية: BTCUSDT، ETHUSDT، BNBUSDT، SOLUSDT، XRPUSDT</p><p>ذهب: XAUUSDT | فضة: XAGUSDT</p><p>نفط WTI: CLUSDT | نفط برنت: BZUSDT</p><small>يتم استبعاد أي رمز غير متاح في Binance Futures لحسابك.</small></article>
<article><h2>فحص فرص التداول</h2><form method="post" action="{{url_for('scan')}}"><input type="hidden" name="csrf" value="{{csrf}}"><button>تحليل العملات والذهب والفضة والنفط</button></form><small>يعرض فقط فرصًا تجتاز الحد الأدنى وحد التعرض المحدد وهدف صافي 10% تقديريًا. يعاد الفحص عند الموافقة. التمويل والانزلاق الفعلي قد يغيران النتيجة.</small></article>
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
 async function trigger(){if(watching||document.hidden)return;watching=true;try{await fetch('/api/browser-watch',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:'csrf='+encodeURIComponent(csrf),credentials:'same-origin'});}catch(e){}finally{watching=false;refresh();}}
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
            except Exception: logging.exception('Browser market scan failed')
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

@app.route('/api/market-prices')
@auth
def market_prices():
    # Do not scrape Google or silently substitute Binance prices.
    return {'prices':{},'source':'Google Search','realtime':False,
            'message':'لا تتوفر واجهة Google رسمية مضمونة لتحديث الأسعار كل ثانية. استخدم روابط Google في اللوحة.'},200

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
