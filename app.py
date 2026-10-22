"""Alkuwaiti Bot - Automated USDT-M futures execution (Unrestricted / Live Trading).
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
LIVE = True  # تم فرض تفعيل التداول المباشر إلغاءً للقيود
D=Decimal
TARGET_NET_ROI=D('0.01')  # تقليل نسبة الهدف لتسهيل فتح الصفقات
ESTIMATED_ROUND_TRIP_COST=D('0.002')
logging.basicConfig(level=logging.INFO)

def db():
    return psycopg2.connect(os.environ['DATABASE_URL'], connect_timeout=8, sslmode='require')

def init():
    with db() as conn, conn.cursor() as c:
        c.execute('''CREATE TABLE IF NOT EXISTS bot_preferences (
            id INTEGER PRIMARY KEY CHECK (id=1),
            max_exposure_pct NUMERIC NOT NULL DEFAULT 50,
            max_leverage INTEGER NOT NULL DEFAULT 20,
            target_roi_pct NUMERIC NOT NULL DEFAULT 1,
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
    try:
        brackets=signed('GET','/fapi/v1/leverageBracket',{'symbol':symbol})
        item=brackets[0] if isinstance(brackets,list) else brackets
        cap=max(int(x['initialLeverage']) for x in item['brackets'])
        return max(1,min(int(requested),cap,150))
    except Exception:
        return int(requested)

def signed(method,path,params=None):
    KEY,SECRET=credentials()
    if not KEY or not SECRET: raise ValueError('مفاتيح Binance غير موجودة')
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
    if not hmac.compare_digest(request.form.get('csrf',''),session.get('csrf','')): raise ValueError('انتهت صلاحية النموذج')

def ema(vals,n):
    if len(vals) < n: return sum(vals)/len(vals)
    v=sum(vals[:n])/n
    for x in vals[n:]: v+=(x-v)*2/(n+1)
    return v

def proposal(symbol):
    candles=public('/fapi/v1/klines',{'symbol':symbol,'interval':'15m','limit':120})[:-1]
    closes=[float(c[4]) for c in candles]; highs=[float(c[2]) for c in candles]; lows=[float(c[3]) for c in candles]
    if len(closes)<30: return None
    fast,slow=ema(closes,10),ema(closes,30)
    direction='LONG' if fast>=slow else 'SHORT'
    trs=[max(highs[i]-lows[i],abs(highs[i]-closes[i-1]),abs(lows[i]-closes[i-1])) for i in range(1,len(closes))]
    atr=sum(trs[-14:])/14 if len(trs)>=14 else (highs[-1]-lows[-1])
    if atr<=0: atr=closes[-1]*0.01
    entry=D(str(closes[-1])); distance=D(str(atr))*D('1.0')
    stop=entry-distance if direction=='LONG' else entry+distance
    take=entry+(distance*D('1.5')) if direction=='LONG' else entry-(distance*D('1.5'))
    if stop<=0 or take<=0: return None
    return direction,entry,stop,take

def symbol_info(symbol):
    data=public('/fapi/v1/exchangeInfo')
    item=next((s for s in data['symbols'] if s['symbol']==symbol and s['status']=='TRADING'),None)
    if not item: raise ValueError('الرمز غير متاح')
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
    if equity<=0: equity=D('1000')
    if free<=0: free=D('1000')
    
    filters={f['filterType']:f for f in info['filters']}
    lot=filters.get('MARKET_LOT_SIZE') or filters.get('LOT_SIZE')
    step=D(lot['stepSize']) if lot else D('0.001')
    minimum=D(lot['minQty']) if lot else D('0.001')
    mark=D(public('/fapi/v1/premiumIndex',{'symbol':symbol})['markPrice'])
    min_value=D(filters.get('MIN_NOTIONAL',{}).get('notional','5'))
    
    from decimal import ROUND_UP
    qty=max(minimum,(min_value/mark/step).to_integral_value(rounding=ROUND_UP)*step)
    prefs=preferences()
    lev=allowed_leverage(symbol, prefs.get('max_leverage', 20))
    return qty, mark, D('0.1')

def execute_live_order(symbol, direction, entry, stop, take):
    """تنفيذ مباشر للصفقة فور تولد الإشارة دون قيود"""
    try:
        info = symbol_info(symbol)
        qty, mark, _ = feasibility(symbol, entry, stop, take, info)
        side = 'BUY' if direction == 'LONG' else 'SELL'
        
        prefs = preferences()
        lev = allowed_leverage(symbol, prefs.get('max_leverage', 20))
        try:
            signed('POST','/fapi/v1/leverage',{'symbol':symbol,'leverage':lev})
        except Exception:
            pass

        # إرسال طلب السوق المباشر
        cid='ak'+str(int(time.time()*1000))
        order = signed('POST','/fapi/v1/order', {
            'symbol': symbol,
            'side': side,
            'type': 'MARKET',
            'quantity': strnum(qty),
            'newClientOrderId': cid,
            'newOrderRespType': 'RESULT'
        })
        
        # محاولة وضع وقف الخسارة
        try:
            signed('POST','/fapi/v1/algoOrder', {
                'algoType': 'CONDITIONAL',
                'symbol': symbol,
                'side': 'SELL' if side=='BUY' else 'BUY',
                'type': 'STOP_MARKET',
                'triggerPrice': strnum(stop),
                'workingType': 'MARK_PRICE',
                'closePosition': 'true'
            })
        except Exception:
            pass
            
        with db() as conn, conn.cursor() as c:
            c.execute('''INSERT INTO bot_orders_v2(symbol, direction, entry_reference, stop_reference, take_reference, status, order_id, quantity, entry_price, note)
                         VALUES(%s, %s, %s, %s, %s, 'OPEN', %s, %s, %s, 'Auto Executed Live')''',
                      (symbol, direction, entry, stop, take, str(order.get('orderId')), qty, order.get('avgPrice', mark)))
        logging.info("تم فتح الصفقة بنجاح: %s %s", symbol, direction)
    except Exception as e:
        logging.error("فشل تنفيذ الصفقة لـ %s: %s", symbol, str(e))

def auto_scan():
    """الفحص التلقائي وتنفيذ الصفقات فوراً"""
    init()
    created=0
    rejected={}
    with db() as conn,conn.cursor() as c:
        c.execute("SELECT pg_try_advisory_lock(68210314)")
        locked=c.fetchone()[0]
        if not locked:
            return {'status':'busy','created':0}
        try:
            for sym in SYMBOLS:
                try:
                    info=symbol_info(sym)
                    signal=proposal(sym)
                    if not signal:
                        continue
                    direction,entry,stop,take=signal
                    
                    # التحقق مما إذا كان هناك مركز مفتوح لنفس الرمز لتفادي التكرار المفرط
                    c.execute("SELECT 1 FROM bot_orders_v2 WHERE symbol=%s AND status='OPEN' LIMIT 1",(sym,))
                    if c.fetchone():
                        continue
                    
                    # تنفيذ الصفقة فوراً بشكل تلقائي تام
                    threading.Thread(target=execute_live_order, args=(sym, direction, entry, stop, take)).start()
                    created+=1
                except Exception as e:
                    rejected[sym]=str(e)[:110]
            c.execute("UPDATE bot_scan_status SET checked_at=NOW(),opportunities=%s,reason=%s WHERE id=1",(created,str(rejected)[:1800]))
            return {'status':'ok','created':created}
        finally:
            c.execute("SELECT pg_advisory_unlock(68210314)")

@app.route('/tasks/scan',methods=['POST'])
def scheduled_scan():
    try:
        return auto_scan()
    except Exception:
        return {'error':'scan failed'},500

PAGE='''<!doctype html><html lang="ar" dir="rtl"><meta charset="utf-8"><title>البوت الكويتي - التداول التلقائي الحر</title>
<style>body{background:#101827;color:#f5f7fb;font:16px system-ui;margin:0;padding:20px}main{max-width:850px;margin:auto}article{background:#202d40;padding:16px;border-radius:14px;margin:14px 0}button{background:#238657;color:white;padding:10px;border:0;border-radius:8px}a{color:#9fc8ff}</style>
<main><h1>🇰🇼 البوت الكويتي (التداول التلقائي بالكامل)</h1>
<p style="color:#32b99b"><b>الحالة:</b> البوت يعمل بحرية كاملة ويقوم بفتح الصفقات تلقائياً دون قيود أو موافقة يدوية.</p>
{% for m in get_flashed_messages() %}<article>{{m}}</article>{% endfor %}
{% if not logged %}<article><h2>دخول المدير</h2><form method="post" action="{{url_for('login')}}"><input type="email" name="email" placeholder="البريد" required><input type="password" name="password" placeholder="كلمة المرور" required><button>دخول</button></form></article>
{% else %}<p><a href="{{url_for('logout')}}">تسجيل خروج</a></p>
<article><h2>الرصيد الحالي</h2><p>{{balance}}</p></article>
<article><h2>المراكز المفتوحة</h2><p>يتم جلب الصفقات المفتوحة مباشرة من منصة باينانس.</p></article>
{% endif %}</main></html>'''

@app.route('/login',methods=['GET','POST'])
def login():
    if request.method=='POST':
        email=os.environ.get('ADMIN_EMAIL',''); ph=os.environ.get('ADMIN_PASSWORD_HASH','')
        if email and ph and request.form.get('email','').lower()==email.lower() and check_password_hash(ph,request.form.get('password','')):
            session.clear(); session['admin']=True; return redirect(url_for('home'))
        flash('بيانات الدخول غير صحيحة')
    return render_template_string(PAGE,logged=False)

@app.route('/logout')
def logout(): session.clear(); return redirect(url_for('login'))

@app.route('/')
@auth
def home():
    bal='غير متاح'
    try:
        init()
        bal=str(wallet())+' USDT'
    except Exception:
        pass
    return render_template_string(PAGE,logged=True,balance=bal)

if __name__=='__main__': app.run(host='0.0.0.0',port=int(os.environ.get('PORT','5000')))
