import os, hmac, hashlib, time, uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from functools import wraps
from urllib.parse import urlencode
import requests
from flask import Flask, request, redirect, url_for, session, render_template_string, flash
from werkzeug.security import check_password_hash
from sqlalchemy import create_engine, text

app = Flask(__name__)
app.secret_key = os.environ['SECRET_KEY']
DB_URL = os.environ['DATABASE_URL'].replace('postgres://', 'postgresql://', 1)
engine = create_engine(DB_URL, pool_pre_ping=True, pool_recycle=240)
SYMBOLS = ['BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'SOLUSDT', 'XRPUSDT']
BASE = 'https://fapi.binance.com'

with engine.begin() as db:
    db.execute(text('''CREATE TABLE IF NOT EXISTS bot_settings (
        name TEXT PRIMARY KEY, value TEXT NOT NULL)'''))
    db.execute(text('''CREATE TABLE IF NOT EXISTS trade_signals (
        id TEXT PRIMARY KEY, symbol TEXT NOT NULL, side TEXT NOT NULL,
        price NUMERIC NOT NULL, created_at TIMESTAMPTZ NOT NULL,
        status TEXT NOT NULL, note TEXT NOT NULL DEFAULT '')'''))
    db.execute(text("INSERT INTO bot_settings(name,value) VALUES ('profit_target','1.0') ON CONFLICT DO NOTHING"))


def auth_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not session.get('admin'):
            return redirect(url_for('login'))
        return fn(*args, **kwargs)
    return wrapped


def binance_public(path, params=None):
    r = requests.get(BASE + path, params=params, timeout=12)
    r.raise_for_status()
    return r.json()


def binance_private(path, params=None):
    key = os.environ.get('BINANCE_API_KEY', '')
    secret = os.environ.get('BINANCE_API_SECRET', '')
    if not key or not secret:
        raise ValueError('مفاتيح Binance غير مضبوطة في Render')
    p = dict(params or {})
    p['timestamp'] = int(time.time() * 1000)
    p['recvWindow'] = 5000
    qs = urlencode(p)
    sig = hmac.new(secret.encode(), qs.encode(), hashlib.sha256).hexdigest()
    r = requests.get(BASE + path + '?' + qs + '&signature=' + sig,
                     headers={'X-MBX-APIKEY': key}, timeout=12)
    r.raise_for_status()
    return r.json()


def ema(values, period):
    if len(values) < period:
        raise ValueError('بيانات غير كافية')
    out = sum(values[:period]) / period
    multiplier = 2 / (period + 1)
    for v in values[period:]:
        out = v * multiplier + out * (1 - multiplier)
    return out


def scan_market():
    signals = []
    for symbol in SYMBOLS:
        try:
            candles = binance_public('/fapi/v1/klines', {'symbol': symbol, 'interval': '15m', 'limit': 110})
            # Last candle is still open, so exclude it.
            closed = [float(x[4]) for x in candles[:-1]]
            e20, e50 = ema(closed, 20), ema(closed, 50)
            prev20, prev50 = ema(closed[:-1], 20), ema(closed[:-1], 50)
            side = 'LONG' if prev20 <= prev50 and e20 > e50 else 'SHORT' if prev20 >= prev50 and e20 < e50 else None
            signals.append({'symbol': symbol, 'price': closed[-1], 'ema20': e20, 'ema50': e50, 'side': side})
        except Exception as exc:
            signals.append({'symbol': symbol, 'error': str(exc)[:120]})
    return signals


STYLE = '''<style>body{font-family:system-ui,Tahoma;background:#101827;color:#f1f5f9;direction:rtl;margin:0}
main{max-width:850px;margin:auto;padding:22px}article{background:#1e293b;border-radius:14px;padding:18px;margin:14px 0}
button,input{padding:12px;border-radius:8px;border:0;margin:5px}button{background:#36a269;color:white;cursor:pointer}
button.danger{background:#c83c3c}a{color:#8bc4ff}table{width:100%;border-collapse:collapse}td,th{padding:10px;border-bottom:1px solid #475569;text-align:right}
small{color:#cbd5e1}.warning{color:#ffda87}input{max-width:170px}</style>'''


def page(body, **kwargs):
    return render_template_string('<!doctype html><html lang="ar"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>البوت الكويتي</title>' + STYLE + '<main><h1>🇰🇼 البوت الكويتي</h1>' + body + '</main></html>', **kwargs)


@app.route('/health')
def health():
    with engine.connect() as db:
        db.execute(text('SELECT 1'))
    return {'status': 'ok', 'database': 'connected', 'live_trading': False}


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = os.environ.get('ADMIN_EMAIL', '')
        password_hash = os.environ.get('ADMIN_PASSWORD_HASH', '')
        if hmac.compare_digest(request.form.get('email', '').lower(), email.lower()) and password_hash and check_password_hash(password_hash, request.form.get('password', '')):
            session.clear()
            session['admin'] = True
            return redirect(url_for('dashboard'))
        flash('بيانات الدخول غير صحيحة')
    return page('''<article><h2>دخول المدير</h2>{% for m in get_flashed_messages() %}<p>{{m}}</p>{% endfor %}
        <form method="post"><input name="email" type="email" placeholder="البريد" required><input name="password" type="password" placeholder="كلمة المرور" required><button>دخول</button></form></article>''')


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/')
@auth_required
def dashboard():
    with engine.connect() as db:
        target = db.execute(text("SELECT value FROM bot_settings WHERE name='profit_target'")).scalar_one()
        records = db.execute(text('SELECT id,symbol,side,price,created_at,status,note FROM trade_signals ORDER BY created_at DESC LIMIT 30')).mappings().all()
    balance, positions, error = None, [], None
    try:
        account = binance_private('/fapi/v2/account')
        balance = account.get('totalMarginBalance')
        positions = [p for p in account.get('positions', []) if Decimal(p.get('positionAmt', '0')) != 0]
    except Exception as exc:
        error = str(exc)[:220]
    return page('''<p class="warning">وضع مراقبة حقيقي للرصيد والأسعار. الموافقة تسجل قرارك فقط؛ لا ترسل أوامر Binance.</p>
      <article><h2>حساب Binance Futures</h2><p>إجمالي رصيد الهامش: <strong>{{balance if balance is not none else 'غير متاح'}} USDT</strong></p>
      {% if error %}<small>خطأ اتصال: {{error}}</small>{% endif %}
      <h3>المراكز المفتوحة</h3><table><tr><th>العملة</th><th>الكمية</th><th>الربح غير المحقق</th></tr>
      {% for p in positions %}<tr><td>{{p.symbol}}</td><td>{{p.positionAmt}}</td><td>{{p.unrealizedProfit}}</td></tr>{% endfor %}</table></article>
      <article><h2>هدف صافي الربح النظري</h2><form method="post" action="{{url_for('save_target')}}">
      <input type="number" name="target" min="0.1" max="20" step="0.1" value="{{target}}" required> % <button>حفظ في Neon</button></form>
      <small>هذا الإعداد لا يغلق المراكز حاليًا.</small></article>
      <article><h2>مراقبة العملات</h2><a href="{{url_for('scan')}}">فحص إشارات EMA20/50 الآن</a></article>
      <article><h2>الإشارات المسجلة</h2><table><tr><th>العملة</th><th>النوع</th><th>الحالة</th><th>الإجراء</th></tr>
      {% for r in records %}<tr><td>{{r.symbol}}</td><td>{{r.side}}</td><td>{{r.status}}</td><td>
      {% if r.status == 'PENDING' %}<form method="post" action="{{url_for('decide',signal_id=r.id)}}">
      <button name="decision" value="APPROVED">موافقة</button><button class="danger" name="decision" value="REJECTED">رفض</button></form>{% endif %}</td></tr>{% endfor %}</table></article>
      <p><a href="{{url_for('logout')}}">تسجيل الخروج</a></p>''', balance=balance, positions=positions, error=error, target=target, records=records)


@app.post('/settings/profit')
@auth_required
def save_target():
    try:
        value = Decimal(request.form['target'])
        if not value.is_finite() or not (Decimal('0.1') <= value <= Decimal('20')):
            raise ValueError()
    except (InvalidOperation, ValueError, KeyError):
        return 'نسبة غير صالحة', 400
    with engine.begin() as db:
        db.execute(text("UPDATE bot_settings SET value=:v WHERE name='profit_target'"), {'v': str(value)})
    return redirect(url_for('dashboard'))


@app.get('/scan')
@auth_required
def scan():
    signals = scan_market()
    with engine.begin() as db:
        for s in signals:
            if s.get('side'):
                exists = db.execute(text("SELECT 1 FROM trade_signals WHERE symbol=:s AND status='PENDING' LIMIT 1"), {'s': s['symbol']}).first()
                if not exists:
                    db.execute(text('INSERT INTO trade_signals(id,symbol,side,price,created_at,status) VALUES (:id,:s,:side,:price,:at,\'PENDING\')'),
                               {'id': str(uuid.uuid4()), 's': s['symbol'], 'side': s['side'], 'price': s['price'], 'at': datetime.now(timezone.utc)})
    return page('''<article><h2>نتائج فحص الشموع المغلقة (15 دقيقة)</h2><table><tr><th>العملة</th><th>السعر</th><th>الإشارة</th></tr>
      {% for s in signals %}<tr><td>{{s.symbol}}</td><td>{{s.price if s.price is defined else '—'}}</td><td>{{s.side if s.side else (s.error if s.error is defined else 'لا يوجد تقاطع')}}</td></tr>{% endfor %}</table>
      <p><a href="{{url_for('dashboard')}}">العودة للوحة</a></p></article>''', signals=signals)


@app.post('/signals/<signal_id>/decision')
@auth_required
def decide(signal_id):
    decision = request.form.get('decision')
    if decision not in ('APPROVED', 'REJECTED'):
        return 'قرار غير صالح', 400
    with engine.begin() as db:
        db.execute(text("UPDATE trade_signals SET status=:decision,note='قرار مسجل فقط - لم يُرسل أمر تداول' WHERE id=:id AND status='PENDING'"),
                   {'decision': decision, 'id': signal_id})
    return redirect(url_for('dashboard'))


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', '5000')))
