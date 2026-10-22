import os, sqlite3, secrets, json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from functools import wraps
from pathlib import Path

import ccxt
from cryptography.fernet import Fernet
from flask import Flask, request, session, redirect, url_for, render_template_string, abort, flash
from werkzeug.security import check_password_hash

app = Flask(__name__)
app.secret_key = os.environ['SECRET_KEY']
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SECURE=True, SESSION_COOKIE_SAMESITE='Lax')
ADMIN_EMAIL = os.environ['ADMIN_EMAIL'].strip().lower()
ADMIN_HASH = os.environ['ADMIN_PASSWORD_HASH']
DB_PATH = os.getenv('DATABASE_PATH', '/var/data/alkuwaiti.db')
PERSISTENT = os.getenv('PERSISTENT_STORAGE_READY') == 'yes'
FEE_RATE = Decimal(os.getenv('PAPER_PROFIT_FEE_RATE', '0.20'))
if not Decimal('0') <= FEE_RATE <= Decimal('1'):
    raise RuntimeError('Invalid PAPER_PROFIT_FEE_RATE')

TRANSLATIONS = {
 'ar': {'title':'البوت الكويتي', 'home':'الرئيسية', 'login':'دخول المدير', 'logout':'خروج', 'investors':'المستثمرون', 'markets':'الأسواق', 'api':'مفاتيح Binance', 'balance':'رصيد الفيوتشر', 'paper':'تجريبي فقط', 'save':'حفظ', 'name':'الاسم', 'email':'البريد', 'password':'كلمة المرور', 'add':'إضافة مستثمر', 'wallets':'المحافظ', 'trades':'الصفقات', 'fee':'العمولة', 'no':'لا توجد بيانات', 'admin':'لوحة المدير'},
 'en': {'title':'Al Kuwaiti Bot', 'home':'Home', 'login':'Admin login', 'logout':'Logout', 'investors':'Investors', 'markets':'Markets', 'api':'Binance API keys', 'balance':'Futures balance', 'paper':'Paper trading only', 'save':'Save', 'name':'Name', 'email':'Email', 'password':'Password', 'add':'Add investor', 'wallets':'Wallets', 'trades':'Trades', 'fee':'Fee', 'no':'No data', 'admin':'Admin dashboard'}
}

def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')

def db():
    path = Path(DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    conn.executescript('''
      CREATE TABLE IF NOT EXISTS investors (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL, email TEXT, deposit_address TEXT,
        withdrawal_address TEXT, network TEXT,
        paper_balance TEXT NOT NULL DEFAULT '0',
        created_at TEXT NOT NULL
      );
      CREATE TABLE IF NOT EXISTS paper_trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        investor_id INTEGER NOT NULL REFERENCES investors(id),
        symbol TEXT NOT NULL, side TEXT NOT NULL,
        notional TEXT NOT NULL, pnl_gross TEXT NOT NULL,
        fee TEXT NOT NULL, pnl_net TEXT NOT NULL,
        created_at TEXT NOT NULL
      );
      CREATE TABLE IF NOT EXISTS secrets (
        name TEXT PRIMARY KEY, ciphertext BLOB NOT NULL
      );
    ''')
    conn.commit()
    return conn

def csrf():
    if 'csrf' not in session:
        session['csrf'] = secrets.token_urlsafe(32)
    return session['csrf']

def validate_csrf():
    if not secrets.compare_digest(session.get('csrf', ''), request.form.get('csrf', '')):
        abort(403)

def admin_only(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not session.get('admin'):
            return redirect(url_for('login'))
        return fn(*args, **kwargs)
    return wrapped

def money(value, *, nonnegative=False):
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or (nonnegative and amount < 0):
            raise ValueError()
        return amount.quantize(Decimal('0.00000001'))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError('Invalid amount')

def lang():
    return 'en' if session.get('lang') == 'en' else 'ar'

BASE = '''<!doctype html><html lang="{{ lang }}" dir="{{ 'rtl' if lang=='ar' else 'ltr' }}"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{ t.title }}</title><style>
body{background:#0f172a;color:#f8fafc;font:16px Arial,sans-serif;margin:0;padding:18px}
main{max-width:1000px;margin:auto}nav,.panel{background:#1e293b;padding:18px;border-radius:14px;margin-bottom:16px}
a{color:#93c5fd;margin:6px;display:inline-block}h1{margin:8px 0 16px}
input,select,button{padding:11px;margin:6px 0;box-sizing:border-box;max-width:100%;border-radius:7px}
input{width:100%;border:1px solid #64748b}button{background:#16a34a;color:white;border:0;cursor:pointer}
.danger{background:#b91c1c}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:12px}
small{color:#cbd5e1}table{width:100%;border-collapse:collapse}td,th{padding:9px;border-bottom:1px solid #475569;text-align:start}
.scroll{overflow-x:auto}.notice{color:#fbbf24}form{margin:10px 0}label{display:block;margin-top:8px}
</style></head><body><main><nav><strong>🇰🇼 {{ t.title }}</strong>
<a href="{{ url_for('home') }}">{{ t.home }}</a><a href="{{ url_for('markets') }}">{{ t.markets }}</a>
{% if session.get('admin') %}<a href="{{ url_for('dashboard') }}">{{ t.admin }}</a>
<a href="{{ url_for('investors') }}">{{ t.investors }}</a><a href="{{ url_for('api_settings') }}">{{ t.api }}</a>
<a href="{{ url_for('logout') }}">{{ t.logout }}</a>{% else %}<a href="{{ url_for('login') }}">{{ t.login }}</a>{% endif %}
<a href="{{ url_for('set_lang', code='ar') }}">العربية</a><a href="{{ url_for('set_lang', code='en') }}">English</a></nav>
{% for msg in get_flashed_messages() %}<div class="panel notice">{{ msg }}</div>{% endfor %}
{{ content|safe }}</main></body></html>'''

def page(template, **context):
    content = render_template_string(template, **context, t=TRANSLATIONS[lang()], csrf=csrf())
    return render_template_string(BASE, content=content, t=TRANSLATIONS[lang()], lang=lang())

@app.route('/language/<code>')
def set_lang(code):
    if code in TRANSLATIONS:
        session['lang'] = code
    return redirect(request.referrer if request.referrer and request.referrer.startswith(request.host_url) else url_for('home'))

@app.route('/')
def home():
    return page('''<section class="panel"><h1>🇰🇼 {{ t.title }}</h1><p>{{ t.paper }}</p>
<p>أسعار الأسواق عامة. إدارة المستثمرين والحساب الحقيقي للمدير فقط.</p>
<p class="notice">لا توجد أوامر تداول حقيقية في هذه النسخة.</p></section>''')

@app.route('/login', methods=['GET','POST'])
def login():
    if request.method == 'POST':
        validate_csrf()
        email = request.form.get('email','').strip().lower()
        password = request.form.get('password','')
        if secrets.compare_digest(email, ADMIN_EMAIL) and check_password_hash(ADMIN_HASH, password):
            session.clear()
            session['admin'] = True
            return redirect(url_for('dashboard'))
        flash('بيانات الدخول غير صحيحة')
    return page('''<div class="panel"><h2>{{ t.login }}</h2><form method="post">
<input type="hidden" name="csrf" value="{{ csrf }}"><label>{{ t.email }}</label><input name="email" required>
<label>{{ t.password }}</label><input name="password" type="password" required>
<button>{{ t.login }}</button></form></div>''')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('home'))

@app.route('/admin')
@admin_only
def dashboard():
    with db() as conn:
        count = conn.execute('SELECT count(*) FROM investors').fetchone()[0]
        trade_count = conn.execute('SELECT count(*) FROM paper_trades').fetchone()[0]
    return page('''<h1>{{ t.admin }}</h1><div class="grid"><div class="panel"><h3>{{ t.investors }}</h3>{{ count }}</div>
<div class="panel"><h3>{{ t.trades }}</h3>{{ trade_count }}</div>
<div class="panel"><h3>{{ t.balance }}</h3><p>اقرأ الرصيد من صفحة API بعد ربط المفاتيح.</p></div></div>
<p class="notice">{{ t.paper }} — لا يوجد محرك تداول حقيقي أو عامل خلفي.</p>''', count=count, trade_count=trade_count)

@app.route('/admin/investors', methods=['GET','POST'])
@admin_only
def investors():
    if request.method == 'POST':
        validate_csrf()
        name = request.form.get('name','').strip()[:120]
        email = request.form.get('email','').strip()[:200]
        if not name:
            abort(400)
        with db() as conn:
            conn.execute('INSERT INTO investors(name,email,created_at) VALUES(?,?,?)', (name,email,now()))
            conn.commit()
        flash('تمت إضافة المستثمر')
        return redirect(url_for('investors'))
    with db() as conn:
        rows = [dict(x) for x in conn.execute('SELECT * FROM investors ORDER BY id DESC')]
    return page('''<div class="panel"><h2>{{ t.investors }}</h2><form method="post">
<input type="hidden" name="csrf" value="{{ csrf }}"><label>{{ t.name }}</label><input name="name" required>
<label>{{ t.email }}</label><input name="email" type="email"><button>{{ t.add }}</button></form></div>
<div class="panel scroll"><table><tr><th>ID</th><th>{{ t.name }}</th><th>{{ t.email }}</th><th>رصيد تجريبي</th></tr>
{% for r in rows %}<tr><td><a href="{{ url_for('investor', investor_id=r.id) }}">{{ r.id }}</a></td>
<td>{{ r.name }}</td><td>{{ r.email }}</td><td>{{ r.paper_balance }} USDT</td></tr>
{% endfor %}</table></div>''', rows=rows)

@app.route('/admin/investors/<int:investor_id>', methods=['GET','POST'])
@admin_only
def investor(investor_id):
    with db() as conn:
        row = conn.execute('SELECT * FROM investors WHERE id=?', (investor_id,)).fetchone()
        if row is None:
            abort(404)
        if request.method == 'POST':
            validate_csrf()
            action = request.form.get('action')
            if action == 'wallet':
                conn.execute('''UPDATE investors SET deposit_address=?,withdrawal_address=?,network=? WHERE id=?''',
                             (request.form.get('deposit','').strip()[:250], request.form.get('withdrawal','').strip()[:250],
                              request.form.get('network','').strip()[:50], investor_id))
            elif action == 'paper_credit':
                if not PERSISTENT:
                    abort(503, 'Persistent database required')
                amount = money(request.form.get('amount'), nonnegative=True)
                if amount > Decimal('100000000'):
                    abort(400)
                conn.execute('UPDATE investors SET paper_balance=? WHERE id=?', (str(amount),investor_id))
            elif action == 'trade':
                if not PERSISTENT:
                    abort(503, 'Persistent database required')
                symbol = request.form.get('symbol','').strip().upper()[:30]
                side = request.form.get('side')
                if not symbol or side not in ('LONG','SHORT'):
                    abort(400)
                notional = money(request.form.get('notional'), nonnegative=True)
                pnl = money(request.form.get('pnl'))
                if notional <= 0 or notional > Decimal('100000000') or abs(pnl) > Decimal('100000000'):
                    abort(400)
                fee = (max(pnl,Decimal(0))*FEE_RATE).quantize(Decimal('0.00000001'))
                net = pnl-fee
                current = money(row['paper_balance'])
                if current+net < 0:
                    abort(400, 'Insufficient paper balance')
                conn.execute('''INSERT INTO paper_trades(investor_id,symbol,side,notional,pnl_gross,fee,pnl_net,created_at)
                                VALUES(?,?,?,?,?,?,?,?)''',
                             (investor_id,symbol,side,str(notional),str(pnl),str(fee),str(net),now()))
                conn.execute('UPDATE investors SET paper_balance=? WHERE id=?', (str(current+net),investor_id))
            else:
                abort(400)
            conn.commit()
            flash('تم الحفظ')
            return redirect(url_for('investor', investor_id=investor_id))
        trades = [dict(x) for x in conn.execute('SELECT * FROM paper_trades WHERE investor_id=? ORDER BY id DESC LIMIT 100', (investor_id,))]
    return page('''<h2>{{ r.name }}</h2><div class="grid"><div class="panel"><h3>الرصيد التجريبي</h3><h2>{{ r.paper_balance }} USDT</h2>
<small>قيمة إدارية تجريبية، وليست إيداعًا مؤكدًا.</small>
<form method="post"><input type="hidden" name="csrf" value="{{ csrf }}"><input type="hidden" name="action" value="paper_credit">
<label>تحديد الرصيد الافتراضي</label><input name="amount" type="number" min="0" step="any" required>
<button {{ 'disabled' if not persistent else '' }}>{{ t.save }}</button></form></div>
<div class="panel"><h3>{{ t.wallets }}</h3><form method="post"><input type="hidden" name="csrf" value="{{ csrf }}">
<input type="hidden" name="action" value="wallet"><label>شبكة التحويل</label><input name="network" value="{{ r.network or '' }}">
<label>عنوان الاستقبال منه</label><input name="deposit" value="{{ r.deposit_address or '' }}">
<label>عنوان التحويل إليه</label><input name="withdrawal" value="{{ r.withdrawal_address or '' }}">
<button>{{ t.save }}</button></form><small>هذه عناوين مرجعية فقط، ولا يتم إجراء تحويلات.</small></div></div>
<div class="panel"><h3>تسجيل صفقة تجريبية مغلقة يدويًا</h3><form method="post">
<input type="hidden" name="csrf" value="{{ csrf }}"><input type="hidden" name="action" value="trade">
<label>الرمز</label><input name="symbol" placeholder="BTC/USDT" required><label>الاتجاه</label>
<select name="side"><option>LONG</option><option>SHORT</option></select><label>القيمة الاسمية USDT</label>
<input name="notional" type="number" step="any" min="0.000001" required><label>الربح أو الخسارة قبل العمولة USDT</label>
<input name="pnl" type="number" step="any" required><button {{ 'disabled' if not persistent else '' }}>تسجيل الصفقة</button></form>
<small>عمولة المدير {{ fee_pct }}% من الأرباح الموجبة فقط، وليست من الخسائر. لا توجد صفقات آلية للمستثمرين بعد.</small></div>
<div class="panel scroll"><h3>{{ t.trades }}</h3><table><tr><th>التاريخ</th><th>العملة</th><th>الاتجاه</th><th>القيمة</th><th>الربح الإجمالي</th><th>{{ t.fee }}</th><th>الصافي</th></tr>
{% for tr in trades %}<tr><td>{{ tr.created_at }}</td><td>{{ tr.symbol }}</td><td>{{ tr.side }}</td><td>{{ tr.notional }}</td>
<td>{{ tr.pnl_gross }}</td><td>{{ tr.fee }}</td><td>{{ tr.pnl_net }}</td></tr>{% endfor %}</table></div>''', r=dict(row), trades=trades, persistent=PERSISTENT, fee_pct=FEE_RATE*100)

PUBLIC_SYMBOLS = ['BTC/USDT','ETH/USDT','BNB/USDT','SOL/USDT','XRP/USDT','PAXG/USDT','XAUT/USDT']
@app.route('/markets')
def markets():
    rows=[]
    error=None
    try:
        ex=ccxt.binance({'enableRateLimit':True,'timeout':10000})
        tickers=ex.fetch_tickers(PUBLIC_SYMBOLS)
        for symbol in PUBLIC_SYMBOLS:
            tick=tickers.get(symbol)
            if tick:
                rows.append({'symbol':symbol,'price':tick.get('last'),'change':tick.get('percentage')})
    except Exception:
        app.logger.exception('Public market data failed')
        error='تعذر جلب الأسعار الآن؛ حاول لاحقًا.'
    return page('''<div class="panel"><h2>{{ t.markets }}</h2><p>خمس عملات رئيسية مختارة، وليست ترتيبًا آليًا للأفضل.</p>
<p>الذهب الرقمي: PAXG و XAUT عند توفرهما. الفضة والنفط: لا تُعرض أسعار غير متحقق منها.</p>
{% if error %}<p class="notice">{{ error }}</p>{% endif %}<div class="scroll"><table><tr><th>الرمز</th><th>السعر USDT</th><th>التغير 24h %</th></tr>
{% for r in rows %}<tr><td>{{ r.symbol }}</td><td>{{ r.price }}</td><td>{{ r.change }}</td></tr>{% endfor %}</table></div>
<small>الأسعار عامة وقد تتأخر. ليست توصية استثمارية.</small></div>''', rows=rows, error=error)

def cipher():
    key=os.getenv('API_ENCRYPTION_KEY','')
    if not key:
        raise RuntimeError('API_ENCRYPTION_KEY not set')
    return Fernet(key.encode())

def stored_keys():
    with db() as conn:
        rows=conn.execute('SELECT name,ciphertext FROM secrets WHERE name IN (?,?)',('binance_key','binance_secret')).fetchall()
    items={r['name']:cipher().decrypt(r['ciphertext']).decode() for r in rows}
    if 'binance_key' in items and 'binance_secret' in items:
        return items['binance_key'],items['binance_secret']
    return None

@app.route('/admin/api', methods=['GET','POST'])
@admin_only
def api_settings():
    result=''
    if request.method=='POST':
        validate_csrf()
        if not check_password_hash(ADMIN_HASH,request.form.get('password','')):
            abort(403)
        action=request.form.get('action')
        if action=='save':
            if not PERSISTENT:
                abort(503,'Persistent storage not configured')
            key=request.form.get('key','').strip()
            secret=request.form.get('secret','').strip()
            if not key or not secret or len(key)>500 or len(secret)>500:
                abort(400)
            c=cipher()
            with db() as conn:
                conn.execute('INSERT OR REPLACE INTO secrets VALUES (?,?)',('binance_key',c.encrypt(key.encode())))
                conn.execute('INSERT OR REPLACE INTO secrets VALUES (?,?)',('binance_secret',c.encrypt(secret.encode())))
                conn.commit()
            result='تم حفظ المفاتيح مشفرة.'
        elif action=='delete':
            with db() as conn:
                conn.execute("DELETE FROM secrets WHERE name IN ('binance_key','binance_secret')")
                conn.commit()
            result='تم حذف المفاتيح من قاعدة البيانات.'
        elif action=='check':
            try:
                pair=stored_keys()
                if not pair:
                    raise RuntimeError('No saved keys')
                ex=ccxt.binanceusdm({'apiKey':pair[0],'secret':pair[1],'enableRateLimit':True,'timeout':12000})
                balance=ex.fetch_balance()
                info=balance.get('info',{})
                result='اتصال ناجح (قراءة فقط). إجمالي هامش الفيوتشر: '+str(info.get('totalMarginBalance','غير متاح'))+' USDT'
            except Exception:
                app.logger.exception('Binance read-only check failed')
                result='فشل فحص الاتصال. تحقق من صلاحيات المفاتيح وإعدادات Binance.'
        else:
            abort(400)
    return page('''<div class="panel"><h2>{{ t.api }}</h2><p class="notice">المفاتيح لا تُعرض بعد الحفظ. لا تُفعّل صلاحية السحب.</p>
{% if not persistent %}<p class="notice">التخزين الدائم غير مهيأ: لا يمكن حفظ مفاتيح حقيقية بأمان على هذه الخدمة.</p>{% endif %}
{% if result %}<p>{{ result }}</p>{% endif %}
<form method="post"><input type="hidden" name="csrf" value="{{ csrf }}"><input type="hidden" name="action" value="save">
<label>API Key</label><input type="password" name="key" autocomplete="off"><label>API Secret</label>
<input type="password" name="secret" autocomplete="off"><label>{{ t.password }}</label><input type="password" name="password" required>
<button {{ 'disabled' if not persistent else '' }}>{{ t.save }}</button></form>
<form method="post"><input type="hidden" name="csrf" value="{{ csrf }}"><input type="hidden" name="action" value="check">
<label>{{ t.password }}</label><input type="password" name="password" required><button>فحص رصيد Binance الحقيقي (قراءة فقط)</button></form>
<form method="post"><input type="hidden" name="csrf" value="{{ csrf }}"><input type="hidden" name="action" value="delete">
<label>{{ t.password }}</label><input type="password" name="password" required><button class="danger">حذف المفاتيح</button></form></div>''', result=result, persistent=PERSISTENT)

if __name__=='__main__':
    app.run(host='0.0.0.0',port=int(os.getenv('PORT','10000')))
