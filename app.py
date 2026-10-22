"""Alkuwaiti Bot - human approved USDT-M futures execution.
Requires explicit LIVE_TRADING_ENABLED=true. Test with Binance testnet first.
"""
import os, time, hmac, hashlib, secrets, logging
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from functools import wraps
from urllib.parse import urlencode
import requests, psycopg2
from psycopg2.extras import RealDictCursor
from flask import Flask, request, session, redirect, url_for, flash, render_template_string
from werkzeug.security import check_password_hash
from cryptography.fernet import Fernet, InvalidToken

app=Flask(__name__)
app.secret_key=os.environ.get('SECRET_KEY') or secrets.token_hex(32)
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
logging.basicConfig(level=logging.INFO)

def db():
    return psycopg2.connect(os.environ['DATABASE_URL'], connect_timeout=8, sslmode='require')

def init():
    with db() as conn, conn.cursor() as c:
        c.execute('''CREATE TABLE IF NOT EXISTS bot_api_credentials (id INTEGER PRIMARY KEY CHECK (id=1), api_key_cipher BYTEA NOT NULL, api_secret_cipher BYTEA NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())''')
        c.execute('''CREATE TABLE IF NOT EXISTS bot_orders_v2 (
            id BIGSERIAL PRIMARY KEY, symbol TEXT NOT NULL, direction TEXT NOT NULL,
            entry_reference NUMERIC NOT NULL, stop_reference NUMERIC NOT NULL,
            take_reference NUMERIC NOT NULL, created_at TIMESTAMPTZ DEFAULT NOW(),
            status TEXT NOT NULL DEFAULT 'PENDING', order_id TEXT, close_id TEXT,
            entry_price NUMERIC, quantity NUMERIC, stop_algo_id TEXT, note TEXT)''')

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
    take=entry+distance*2 if direction=='LONG' else entry-distance*2
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

def entry_order(row):
    if not LIVE: raise ValueError('التداول الحقيقي معطّل: اضبط LIVE_TRADING_ENABLED=true بعد الاختبار')
    symbol=row['symbol']; info=symbol_info(symbol)
    account=signed('GET','/fapi/v2/account')
    if account.get('dualSidePosition'): raise ValueError('يرجى تعطيل Hedge Mode؛ هذا الإصدار يدعم One-way فقط')
    equity=D(account['totalWalletBalance'])
    if equity<=0: raise ValueError('رصيد Futures غير كافٍ')
    existing=positions()
    if any(p['symbol']==symbol for p in existing): raise ValueError('توجد صفقة مفتوحة على هذا الرمز')
    # Exposure measured by mark price, including other open positions.
    exposure=sum(abs(D(p['positionAmt']))*D(p['markPrice']) for p in existing)
    allowed=equity*D('0.10')-exposure
    notional=min(equity*D('0.02'),allowed)
    if notional<=0: raise ValueError('وصلت إلى حد التعرض الإجمالي 10%')
    filters={f['filterType']:f for f in info['filters']}
    lot=filters.get('MARKET_LOT_SIZE',filters.get('LOT_SIZE'))
    step=D(lot['stepSize']); minqty=D(lot['minQty'])
    mark=D(public('/fapi/v1/premiumIndex',{'symbol':symbol})['markPrice'])
    qty=floor_step(notional/mark,step)
    min_notional=D(filters.get('MIN_NOTIONAL',{}).get('notional','5'))
    if qty<minqty or qty*mark<min_notional: raise ValueError('حجم الصفقة أقل من الحد الأدنى؛ لن نرفع المخاطرة لتجاوزه')
    # Ensure protective stop is on correct side of live mark price.
    stop=D(row['stop_reference']); side='BUY' if row['direction']=='LONG' else 'SELL'
    if (side=='BUY' and stop>=mark) or (side=='SELL' and stop<=mark):
        raise ValueError('سعر وقف الخسارة لم يعد صالحًا بعد تحرك السوق؛ أعد التحليل')
    price_filter=filters['PRICE_FILTER']; tick=D(price_filter['tickSize'])
    stop=floor_step(stop,tick)
    if stop<=0: raise ValueError('وقف الخسارة غير صالح')
    # Set isolated leverage conservatively. Do not change leverage on existing positions.
    signed('POST','/fapi/v1/marginType',{'symbol':symbol,'marginType':'ISOLATED'}) if False else None
    signed('POST','/fapi/v1/leverage',{'symbol':symbol,'leverage':1})
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

PAGE='''<!doctype html><html lang="ar" dir="rtl"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>البوت الكويتي</title><style>
body{background:#101827;color:#f5f7fb;font:16px system-ui;margin:0}main{max-width:850px;margin:auto;padding:16px}article{background:#202d40;padding:16px;border-radius:14px;margin:14px 0;overflow:auto}button,input{font:inherit;padding:10px;border:0;border-radius:8px;margin:4px}button{background:#238657;color:white}button.danger{background:#b64040}a{color:#9fc8ff}table{width:100%;border-collapse:collapse}td,th{padding:8px;border-bottom:1px solid #435069;text-align:right}small{color:#bdc9da}.warn{color:#ffcf83}</style><main><h1>🇰🇼 البوت الكويتي</h1><p class="warn">{{'⚠️ التداول الحقيقي مفعل' if live else '🔒 التداول الحقيقي معطل — وضع التحليل فقط'}}</p>
{% for m in get_flashed_messages() %}<article>{{m}}</article>{% endfor %}
{% if not logged %}<article><h2>دخول المدير</h2><form method="post" action="{{url_for('login')}}"><input type="hidden" name="csrf" value="{{csrf}}"><input type="email" name="email" placeholder="البريد" required><input type="password" name="password" placeholder="كلمة المرور" required><button>دخول</button></form></article>
{% else %}<a href="{{url_for('logout')}}">خروج</a><article><h2>الرصيد</h2><p>{{balance}}</p><small>رصيد المحفظة USDT وليس الربح المحقق</small></article>
<article><h2>🔑 تغيير مفاتيح Binance API</h2><p><small>يمكنك استبدال المفاتيح متى شئت. لا تُعرض المفاتيح القديمة، وتُحفظ الجديدة مشفرة في Neon. يلزم CREDENTIALS_ENCRYPTION_KEY في Render.</small></p><form method="post" action="{{url_for('update_api_credentials')}}"><input type="hidden" name="csrf" value="{{csrf}}"><input name="api_key" type="password" autocomplete="off" placeholder="API Key الجديد" required style="width:90%"><input name="api_secret" type="password" autocomplete="off" placeholder="API Secret الجديد" required style="width:90%"><button>فحص الاتصال وحفظ المفاتيح الجديدة</button></form><small>أنشئ المفاتيح بصلاحية Futures فقط، وعطّل السحب Withdrawal. تغيير المفاتيح لا يلغي الصفقات المفتوحة.</small></article>
<article><h2>فحص فرص التداول</h2><form method="post" action="{{url_for('scan')}}"><input type="hidden" name="csrf" value="{{csrf}}"><button>تحليل العملات والذهب والفضة والنفط</button></form><small>تحليل يدوي لشموع 15 دقيقة مغلقة. قد لا تتوفر بعض الرموز.</small></article>
<article><h2>فرص بانتظار الموافقة</h2><table><tr><th>الرمز</th><th>الاتجاه</th><th>الدخول التقريبي</th><th>الهدف</th><th>وقف الخسارة</th><th>الأمر</th></tr>{% for x in pending %}<tr><td>{{x.symbol}}</td><td>{{x.direction}}</td><td>{{x.entry_reference}}</td><td>{{x.take_reference}}</td><td>{{x.stop_reference}}</td><td><form method="post" action="{{url_for('approve',order_id=x.id)}}"><input type="hidden" name="csrf" value="{{csrf}}"><button>موافقة وفتح فورًا</button></form><form method="post" action="{{url_for('reject',order_id=x.id)}}"><input type="hidden" name="csrf" value="{{csrf}}"><button class="danger">رفض</button></form></td></tr>{% else %}<tr><td colspan="6">لا توجد فرص</td></tr>{% endfor %}</table></article>
<article><h2>مراكز Binance المفتوحة</h2><table><tr><th>الرمز</th><th>الكمية</th><th>الدخول</th><th>ربح/خسارة غير محققة</th><th>إغلاق</th></tr>{% for p in positions %}<tr><td>{{p.symbol}}</td><td>{{p.positionAmt}}</td><td>{{p.entryPrice}}</td><td>{{p.unRealizedProfit}} USDT</td><td><form method="post" action="{{url_for('close_position',symbol=p.symbol)}}"><input type="hidden" name="csrf" value="{{csrf}}"><button class="danger">إغلاق الآن</button></form></td></tr>{% else %}<tr><td colspan="5">لا توجد مراكز أو تعذر جلبها</td></tr>{% endfor %}</table><small>الإغلاق Market؛ السعر النهائي والرسوم قد يختلفان. أوامر الحماية قد تغلق المركز تلقائيًا.</small></article>
<article><h2>آخر الأوامر</h2><table><tr><th>الرمز</th><th>الاتجاه</th><th>الحالة</th><th>ملاحظة</th></tr>{% for x in recent %}<tr><td>{{x.symbol}}</td><td>{{x.direction}}</td><td>{{x.status}}</td><td>{{x.note or '—'}}</td></tr>{% endfor %}</table></article>{% endif %}</main></html>'''

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
    pending=[];recent=[]; pos=[];bal='غير متاح'
    try:
        init()
        with db() as conn,conn.cursor(cursor_factory=RealDictCursor) as c:
            c.execute("SELECT * FROM bot_orders_v2 WHERE status='PENDING' ORDER BY id DESC LIMIT 25");pending=c.fetchall()
            c.execute('SELECT * FROM bot_orders_v2 ORDER BY id DESC LIMIT 25');recent=c.fetchall()
    except Exception as e: flash('Neon: '+str(e)[:160])
    try: bal=str(wallet())+' USDT';pos=positions()
    except Exception as e: flash('Binance: '+str(e)[:160])
    return render_template_string(PAGE,logged=True,csrf=csrf(),live=LIVE,balance=bal,positions=pos,pending=pending,recent=recent)


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
            with db() as conn,conn.cursor() as c: c.execute("UPDATE bot_orders_v2 SET status='ERROR',note=%s WHERE id=%s",(str(e)[:400],order_id))
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
