import os
import time
import threading
import secrets
import json
import sqlite3
from pathlib import Path
from datetime import datetime, timedelta, timezone
import calendar
import hmac
import re
import pytz
from flask import Flask, request, redirect, url_for, session, abort
from markupsafe import escape
from werkzeug.security import generate_password_hash, check_password_hash
import ccxt
import ema_paper

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "alabod213@gmail.com").strip().lower()
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")
WATCHLIST = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "PAXG/USDT", "BNB/USDT"]

exchange = None
if os.environ.get("BINANCE_API_KEY") and os.environ.get("BINANCE_API_SECRET"):
    exchange = ccxt.binance({
        "apiKey": os.environ["BINANCE_API_KEY"],
        "secret": os.environ["BINANCE_API_SECRET"],
        "enableRateLimit": True,
        "options": {"defaultType": "spot", "adjustForTimeSync": True, "recvWindow": 60000},
    })

# Persistent SQLite storage. Override DB_PATH to choose a writable persistent volume.
DB_PATH = os.environ.get("DB_PATH", str(Path(__file__).with_name("app_data.sqlite3")))
users_db = {}
total_subscription_earnings = 0.0
cumulative_bot_profits = 0.0
db_lock = threading.RLock()


def get_db_connection():
    connection = sqlite3.connect(DB_PATH, timeout=30)
    connection.execute("PRAGMA busy_timeout = 30000")
    connection.row_factory = sqlite3.Row
    return connection


def init_database():
    db_file = Path(DB_PATH)
    db_file.parent.mkdir(parents=True, exist_ok=True)
    with get_db_connection() as connection:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute(
            """CREATE TABLE IF NOT EXISTS users (
                email TEXT PRIMARY KEY,
                data TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        connection.commit()
    with get_db_connection() as connection:
        records = connection.execute("SELECT email, data FROM users").fetchall()
    with db_lock:
        users_db.clear()
        for record in records:
            try:
                users_db[record["email"]] = json.loads(record["data"])
            except (json.JSONDecodeError, TypeError):
                app.logger.exception("Skipping invalid stored user record")


def save_user(email, user_record):
    serialized = json.dumps(user_record, ensure_ascii=False, separators=(",", ":"))
    with get_db_connection() as connection:
        connection.execute(
            """INSERT INTO users(email, data, updated_at)
               VALUES (?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(email) DO UPDATE SET
                 data=excluded.data, updated_at=CURRENT_TIMESTAMP""",
            (email, serialized),
        )
        connection.commit()


def delete_user(email):
    with get_db_connection() as connection:
        connection.execute("DELETE FROM users WHERE email = ?", (email,))
        connection.commit()



def utc_now():
    return datetime.now(timezone.utc)

def parse_utc(value):
    if not value:
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result if result.tzinfo else result.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None

def add_calendar_months(value, months):
    index = value.year * 12 + value.month - 1 + months
    year, month = divmod(index, 12)
    month += 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)

def subscription_status(user):
    now = utc_now()
    paid_until = parse_utc(user.get("subscription_until"))
    trial_start = parse_utc(user.get("trial_started_at"))
    if not trial_start:
        created = user.get("created_at")
        if created:
            trial_start = datetime.fromtimestamp(float(created), timezone.utc)
    trial_end = trial_start + timedelta(days=7) if trial_start else None
    if paid_until and paid_until > now:
        return "اشتراك مدفوع", paid_until
    if trial_end and trial_end > now:
        return "تجربة مجانية", trial_end
    return "منتهي - يلزم دفع الاشتراك", None

def activate_subscription(user, plan, units, paid_at):
    if plan not in ("weekly", "monthly", "yearly") or units not in (1, 3, 12):
        raise ValueError("Invalid plan")
    if (plan, units) not in (("weekly", 1), ("weekly", 3), ("monthly", 1), ("monthly", 3), ("yearly", 1)):
        raise ValueError("Invalid subscription package")
    existing = parse_utc(user.get("subscription_until"))
    start = max(filter(None, (existing, paid_at)))
    if plan == "weekly":
        end = start + timedelta(weeks=units + (1 if units == 3 else 0))
    elif plan == "monthly":
        end = add_calendar_months(start, units + (1 if units == 3 else 0))
    else:
        end = add_calendar_months(start, 14)
    user["subscription_until"] = end.isoformat()
    user["plan"] = plan
    user["last_subscription_paid_at"] = paid_at.isoformat()
    user["completed_payments_count"] = int(user.get("completed_payments_count", 0)) + 1
    return end

def init_subscription_payments():
    with get_db_connection() as connection:
        connection.execute("""CREATE TABLE IF NOT EXISTS subscription_payments(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL,
            plan TEXT NOT NULL,
            units INTEGER NOT NULL,
            reference TEXT NOT NULL,
            requested_at TEXT NOT NULL,
            confirmed_at TEXT,
            status TEXT NOT NULL DEFAULT 'pending'
        )""")
        connection.commit()


TERMS_VERSION = "2026-10-21-v1"
RAIN_BTC_ADDRESS = os.environ.get("RAIN_BTC_ADDRESS", "bc1q5tga3rmaa6c5lfxtcngueewla22zqdfzr7dcw808zxwad895e42sx79pz0").strip()
BTC_ADDRESS_RE = re.compile(r"^(bc1[ac-hj-np-z02-9]{11,71}|[13][a-km-zA-HJ-NP-Z1-9]{25,34})$")

def terms_text():
    return """<section style='border:1px solid #e0b94e;padding:16px;margin:14px 0;line-height:1.9'>
    <h3>اتفاقية المخاطر والتداول وإدارة الأرباح</h3>
    <p>1. خلال الفترة التجريبية المجانية (7 أيام)، لا تُفعّل أي صفقات حقيقية أو تحويلات تلقائية.</p>
    <p>2. في حال تفعيل التداول الحقيقي لاحقاً بتفويض منفصل، قد يستخدم البوت عقوداً آجلة برافعة 10× إلى 20×.
    الحد الأقصى لإجمالي <b>القيمة الاسمية</b> للمراكز والأوامر المفتوحة هو 10% من رأس مال الحساب، وليس 10% كهامش لكل صفقة.</p>
    <p>3. التداول بالرافعة عالي المخاطر؛ يمكن خسارة رأس المال، وقد يحدث تصفية أو انزلاق سعري.
    لا توجد أرباح مضمونة. الحد الاسمي 10% ليس ضماناً لحد الخسارة.</p>
    <p>4. وفق اتفاق مستقل وواضح لتقاسم الأرباح، تكون حصة مشغل البوت المقترحة 50% من
    <b>صافي الأرباح المحققة الموجبة</b> بعد الرسوم والخسائر المرحلة المستحقة، وليست نصف رأس المال أو نصف قيمة الصفقة.
    يُعرض كشف تفصيلي للمستثمر قبل استحقاق أي تحويل.</p>
    <p>5. يتحمل المستثمر مخاطر السوق والخسائر الاستثمارية، لكن هذا النص لا يعفي المشغل من
    المسؤولية التي يقررها القانون، أو من الاحتيال أو الإهمال الجسيم أو مخالفة التفويض.
    يجب مراجعة الاتفاق مع مختص قانوني والامتثال للتراخيص واللوائح المحلية.</p>
    <p>6. إرسال BTC إلى عنوان محفظة مشغل البوت يُعد تحويل أموال إلى طرف آخر، وقد لا يمكن استرجاعه.
    يجب التحقق من العنوان وشبكة Bitcoin (BTC) ومصدر الملكية والرسوم، ولا يعد الإرسال موافقة
    تلقائية على إدارة الأموال أو خصم الأرباح.</p>
    <p>7. لا يُسمح بسحب أو نقل أموال المستثمر إلى حساب المشغل إلا بتفويض منفصل موثق
    ووفق شروط الاتفاق والقانون. الموافقة هنا تثبت الاطلاع على المخاطر ولا تُنشئ صلاحية سحب.</p>
    </section>"""

def ensure_funding_tables():
    with get_db_connection() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS terms_acceptances(
            id INTEGER PRIMARY KEY AUTOINCREMENT,email TEXT NOT NULL,version TEXT NOT NULL,
            accepted_at TEXT NOT NULL,ip_address TEXT,user_agent TEXT)""")
        con.execute("""CREATE TABLE IF NOT EXISTS btc_deposit_claims(
            id INTEGER PRIMARY KEY AUTOINCREMENT,email TEXT NOT NULL,txid TEXT NOT NULL,
            claimed_btc TEXT NOT NULL,submitted_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', UNIQUE(email,txid))""")
        con.commit()

init_database()
init_subscription_payments()
ensure_funding_tables()
with get_db_connection() as _paper_conn:
    ema_paper.setup(_paper_conn)


@app.route('/paper-trading', methods=['GET', 'POST'])
def paper_trading():
    if not session.get('logged_in'):
        return redirect(url_for('login'))
    if session.get('role') == 'user':
        with db_lock:
            subscriber = dict(users_db.get(session.get('user_email'), {}))
        if subscription_status(subscriber)[1] is None:
            return redirect(url_for('subscription'))
    owner = session.get('user_email') if session.get('role') == 'user' else ADMIN_EMAIL
    message = ''
    if request.method == 'POST':
        # This endpoint deliberately has NO live trading or real withdrawal actions.
        if request.form.get('action') != 'step':
            return 'Unsupported action', 400
        try:
            public = ccxt.binance({'enableRateLimit': True, 'options': {'defaultType': 'spot'}})
            # Exclude the still-forming candle to prevent look-ahead / unstable signals.
            candles = public.fetch_ohlcv('BTC/USDT', timeframe='1h', limit=120)[:-1]
            with get_db_connection() as connection:
                message = ema_paper.run_paper_step(connection, owner, candles)
        except Exception:
            app.logger.exception('Paper trading step failed')
            message = 'Could not update simulation. Check market data availability.'
    with get_db_connection() as connection:
        row = ema_paper.account(connection, owner)
        events = connection.execute('SELECT * FROM paper_events WHERE owner=? ORDER BY id DESC LIMIT 20', (owner,)).fetchall()
    def fmt(value):
        return f'{float(value):,.6f}'
    table_rows = ''.join(f"<tr><td>{escape(e['at'])}</td><td>{escape(e['kind'])}</td><td>{fmt(e['price'])}</td><td>{fmt(e['qty'])}</td><td>{fmt(e['pnl'])}</td></tr>" for e in events)
    html = f"""<div style='max-width:900px;margin:30px auto;padding:25px;background:#141414;border:2px solid #ffcc00'>
    <h2>EMA 20/50 — SPOT PAPER TRADING ONLY</h2><p><b>No real orders or withdrawals.</b> BTC/USDT, 1h closed candles; maximum allocation 10%, stop-loss trigger 2%, daily drawdown guard 3% (not guaranteed).</p>
    <p>Simulated cash: {fmt(row['cash'])} USDT | BTC: {fmt(row['asset'])} | Realized P/L: {fmt(row['realized'])} USDT</p>
    <p>Reserved simulated withdrawal: {fmt(row['reserved'])} USDT | Simulated fees: {fmt(row['fees'])} USDT</p>
    <p>Inflation, network fees, slippage, and exchange precision constraints are NOT included in simulated P/L.</p>
    <p>{escape(message)}</p><form method='post'><input type='hidden' name='action' value='step'><button type='submit'>Run one paper step</button></form>
    <table border='1' cellpadding='8'><tr><th>UTC time</th><th>Signal</th><th>Price</th><th>Quantity</th><th>Net realized P/L</th></tr>{table_rows}</table>
    <p><a href='/user-dashboard'>User dashboard</a> | <a href='/admin-panel'>Admin panel</a></p></div>"""
    return page(html)



def calculate_dynamic_subscription_fee(plan, email):
    now = datetime.now(pytz.timezone("Asia/Kuwait"))
    prices = {"weekly": 19.99, "monthly": 49.99, "yearly": 99.99}
    if plan not in prices:
        raise ValueError("Invalid subscription plan")
    with db_lock:
        payments = users_db.get(email, {}).get("completed_payments_count", 0)
    if plan in {"weekly", "monthly"} and payments > 0 and payments % 3 == 0:
        return 0.0, "Discount Active"
    if (now.month, now.day) in {(1, 1), (2, 25), (2, 26), (3, 20), (5, 27), (6, 16)}:
        return round(max(0.0, prices[plan] - 10), 2), "Holiday Offer"
    if now.weekday() in (4, 5):
        discount = {"weekly": 2, "monthly": 5, "yearly": 10}[plan]
        return round(max(0.0, prices[plan] - discount), 2), "Weekend Offer"
    return prices[plan], "Standard Price Plan"


def get_header_tools():
    now = datetime.now(pytz.timezone("Asia/Kuwait")).strftime("%Y-%m-%d %H:%M:%S")
    return f'''
    <div style="background:#141414;padding:15px;border-bottom:1px solid #333;display:flex;justify-content:space-between;flex-wrap:wrap;">
      <b style="color:#ffcc00;">Time (Kuwait): <span id="live-clock" style="color:white;">{now}</span></b>
      <div id="google_translate_element"></div>
    </div>
    <script>
    function googleTranslateElementInit() {{
      new google.translate.TranslateElement({{pageLanguage:'ar'}}, 'google_translate_element');
    }}
    setInterval(function() {{
      var el=document.getElementById('live-clock');
      if(el) el.textContent=new Intl.DateTimeFormat('en-CA', {{
        timeZone:'Asia/Kuwait',year:'numeric',month:'2-digit',day:'2-digit',
        hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false
      }}).format(new Date());
    }},1000);
    </script>
    <script src="https://translate.google.com/translate_a/element.js?cb=googleTranslateElementInit"></script>
    '''


def get_light_market_widget():
    html = '<div style="background:#1e1e1e;padding:15px;border-radius:10px;margin-top:15px;border:1px solid #ffcc00;text-align:center;">'
    html += '<h4 style="color:#ffcc00;">Market Radar Summary</h4><div style="display:flex;justify-content:space-around;flex-wrap:wrap;gap:10px;">'
    try:
        if exchange is None:
            raise RuntimeError("Exchange API is not configured")
        for symbol in WATCHLIST:
            ticker = exchange.fetch_ticker(symbol)
            if ticker.get("last") is None:
                continue
            price = float(ticker["last"])
            change = float(ticker.get("percentage") or 0.0)
            color = "#00ff00" if change >= 0 else "#ff3333"
            name = "Gold" if "PAXG" in symbol else symbol.split("/")[0]
            html += f'<div style="background:#111;padding:8px;border-radius:5px;font-size:12px;"><b>{escape(name)}</b><br><span style="color:#00ff00;">${price:.2f}</span><br><span style="color:{color};">{change:.2f}%</span></div>'
    except Exception:
        html += '<p style="color:#aaa;font-size:12px;">Market data temporarily unavailable.</p>'
    return html + "</div></div>"


def page(content):
    return f'''<!doctype html><html lang="ar" dir="rtl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Trading Dashboard</title></head><body style="background:#0c0c0c;color:white;font-family:sans-serif;margin:0;padding:0;">{get_header_tools()}{content}</body></html>'''


@app.route("/")
def index():
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("code") or ""
        if ADMIN_PASSWORD and email == ADMIN_EMAIL and secrets.compare_digest(password, ADMIN_PASSWORD):
            session.clear()
            session.update(logged_in=True, role="admin")
            return redirect(url_for("admin_panel"))
        with db_lock:
            user = dict(users_db.get(email, {}))
        stored = user.get("password_hash")
        valid_password = False
        if stored:
            try:
                valid_password = check_password_hash(stored, password)
            except (ValueError, TypeError):
                valid_password = False
        # Upgrade legacy plaintext records once, then remove the plaintext field.
        elif user.get("password"):
            legacy_password = user.get("password", "")
            valid_password = secrets.compare_digest(password, legacy_password)
            if valid_password:
                user["password_hash"] = generate_password_hash(password)
                user.pop("password", None)
                with db_lock:
                    users_db[email] = user
                save_user(email, user)
        if user and valid_password:
            session.clear()
            session.update(logged_in=True, role="user", user_email=email)
            return redirect(url_for("user_dashboard"))
        return '<script>alert("Invalid login details");window.location.href="/login";</script>'
    content = '''<div style="background:#141414;max-width:400px;margin:60px auto;padding:30px;border-radius:15px;border:2px solid #ffcc00;text-align:right;"><h2>Login</h2><form method="POST"><input type="email" name="email" placeholder="Email" required autocomplete="username" style="width:100%;padding:10px;margin-bottom:15px;box-sizing:border-box;"><input type="password" name="code" placeholder="Password" required autocomplete="current-password" style="width:100%;padding:10px;margin-bottom:20px;box-sizing:border-box;"><button type="submit" style="background:#ffcc00;width:100%;padding:12px;border:0;">Enter</button></form><a href="/register">Register</a> | <a href="/forgot-password">Forgot Password</a>'''
    return page(content + get_light_market_widget() + "</div>")


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        # Never reveal passwords or the administrator password/code.
        return '<script>alert("If the account exists, secure recovery instructions will be provided.");window.location.href="/login";</script>'
    return page('''<div style="background:#141414;max-width:350px;margin:50px auto;padding:30px;border:2px solid #ffcc00;"><h2>Password Recovery</h2><form method="POST"><input type="email" name="email" placeholder="Email" required style="width:100%;padding:10px;box-sizing:border-box;"><button type="submit">Send</button></form></div>''')


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        plan = request.form.get("plan") or ""
        agreed = request.form.get("accept_terms") == "yes"
        version = request.form.get("terms_version")
        try:
            amount = float(request.form.get("amount", "0"))
        except (TypeError, ValueError):
            amount = 0.0
        if not agreed or version != TERMS_VERSION:
            return page("<h3>يجب قراءة الشروط والموافقة عليها لإكمال التسجيل.</h3><a href='/register'>العودة</a>"), 400
        if not username or len(username) > 100 or "@" not in email or len(email) > 254:
            return '<script>alert("Enter valid name and email");window.location.href="/register";</script>'
        if len(password) < 8:
            return '<script>alert("Password must be at least 8 characters");window.location.href="/register";</script>'
        if plan not in {"weekly", "monthly", "yearly"}:
            return '<script>alert("Invalid plan");window.location.href="/register";</script>'
        if amount < 50 or amount == float("inf") or amount != amount:
            return '<script>alert("Minimum capital is $50");window.location.href="/register";</script>'
        with db_lock:
            if email in users_db or email == ADMIN_EMAIL:
                return '<script>alert("Email already registered");window.location.href="/register";</script>'
            users_db[email] = {
                "username": username,
                "password_hash": generate_password_hash(password),
                "plan": plan,
                "amount": amount,
                "status": "Trial active",
                "is_approved": True,
                "completed_payments_count": 0,
                "created_at": time.time(),
                "trial_started_at": utc_now().isoformat(),
                "subscription_until": None,
                "user_profits": 0.0,
                "user_losses": 0.0,
                "current_asset": "Scanning Market",
                "terms_version": TERMS_VERSION,
                "terms_accepted_at": utc_now().isoformat(),
            }
            saved_user = dict(users_db[email])
        save_user(email, saved_user)
        with get_db_connection() as con:
            con.execute("INSERT INTO terms_acceptances(email,version,accepted_at,ip_address,user_agent) VALUES(?,?,?,?,?)",
                (email, TERMS_VERSION, saved_user["terms_accepted_at"], request.remote_addr,
                 (request.headers.get("User-Agent") or "")[:300]))
            con.commit()
        session.clear()
        session.update(logged_in=True, role="user", user_email=email)
        return redirect(url_for("btc_funding"))
    content = f'''<div style="max-width:400px;margin:40px auto;background:#141414;padding:30px;border-radius:15px;border:2px solid #ffcc00;text-align:right;"><h2>Register</h2><form method="POST"><input type="text" name="username" placeholder="Name" required maxlength="100" style="width:100%;padding:10px;margin-bottom:15px;box-sizing:border-box;"><input type="email" name="email" placeholder="Email" required style="width:100%;padding:10px;margin-bottom:15px;box-sizing:border-box;"><input type="password" name="password" placeholder="Password (8 characters minimum)" required minlength="8" style="width:100%;padding:10px;margin-bottom:15px;box-sizing:border-box;"><select name="plan" required style="width:100%;padding:10px;margin-bottom:15px;"><option value="weekly">Weekly ($19.99)</option><option value="monthly">Monthly ($49.99)</option><option value="yearly">Yearly ($99.99)</option></select><input type="number" min="50" step="0.01" name="amount" placeholder="Capital (Min $50)" required style="width:100%;padding:10px;margin-bottom:20px;box-sizing:border-box;">{terms_text()}<label style="display:block;margin:14px 0"><input type="checkbox" name="accept_terms" value="yes" required> قرأت الشروط والمخاطر وأوافق عليها</label><input type="hidden" name="terms_version" value="{TERMS_VERSION}"><button type="submit">أوافق وأتابع</button></form>'''
    return page(content + get_light_market_widget() + "</div>")


@app.route("/admin-panel")
def admin_panel():
    if not session.get("logged_in") or session.get("role") != "admin":
        return redirect(url_for("login"))
    rows = []
    with db_lock:
        for email, user in users_db.items():
            rows.append(f"<tr><td>{escape(str(user.get('username', '')))}</td><td>{escape(email)}</td><td>${float(user.get('amount', 0.0)):.2f}</td><td>{escape(str(user.get('status', 'Unknown')))}</td></tr>")
    content = f'''<div style="max-width:1100px;margin:30px auto;background:#141414;padding:25px;border:2px solid #ffcc00;"><h2>Admin Panel</h2><p>Reported bot profit counter (not verified): ${cumulative_bot_profits:.2f} USDT</p><table width="100%" border="1" cellpadding="10" style="text-align:center;border-collapse:collapse;"><thead><tr><th>Name</th><th>Email</th><th>Capital</th><th>Status</th></tr></thead><tbody>{''.join(rows)}</tbody></table>{get_light_market_widget()}<p><a href="/paper-trading">EMA Paper Trading</a> | <a href="/subscription-admin">طلبات الاشتراك</a> | <a href="/logout">Logout</a></p></div>'''
    return page(content)


@app.route("/user-dashboard")
def user_dashboard():
    if not session.get("logged_in") or session.get("role") != "user":
        return redirect(url_for("login"))
    email = session.get("user_email")
    with db_lock:
        user = dict(users_db.get(email, {}))
    if not user:
        session.clear()
        return redirect(url_for("login"))
    subscription_label, subscription_end = subscription_status(user)
    subscription_html = f"<p>الاشتراك: {escape(subscription_label)} | ينتهي: {escape(subscription_end.isoformat() if subscription_end else chr(8212))}</p><p><a href='/subscription'>إدارة الاشتراك</a> | <a href='/btc-funding'>إيداع Bitcoin</a></p>"
    content = f'''<div style="max-width:600px;margin:30px auto;background:#141414;padding:30px;border:2px solid #ffcc00;text-align:right;"><h2>Dashboard</h2><p>Investor: <b>{escape(str(user.get("username", "")))}</b></p><p>Capital: <b>${float(user.get("amount", 0.0)):.2f} USDT</b></p><p>Status: <b>{escape(str(user.get("status", "Unknown")))}</b></p>{subscription_html}{get_light_market_widget()}<hr><a href="/paper-trading">EMA Paper Trading</a> | <a href="/logout">Logout</a></div>'''
    return page(content)




@app.route("/btc-funding", methods=["GET", "POST"])
def btc_funding():
    if not session.get("logged_in") or session.get("role") != "user":
        return redirect(url_for("login"))
    email = session["user_email"]
    with db_lock:
        user = dict(users_db.get(email, {}))
    if user.get("terms_version") != TERMS_VERSION:
        return page("<p>يجب قبول النسخة الحالية من الشروط قبل متابعة إجراءات التمويل.</p>"), 403
    if not RAIN_BTC_ADDRESS or not BTC_ADDRESS_RE.fullmatch(RAIN_BTC_ADDRESS):
        return page("<h3>صفحة التمويل غير مفعّلة: يجب على المشغل إعداد عنوان BTC صحيح والتحقق منه أولاً.</h3><a href='/user-dashboard'>العودة</a>"), 503
    message = ""
    if request.method == "POST":
        txid = (request.form.get("txid") or "").strip().lower()
        claimed = (request.form.get("btc_amount") or "").strip()
        from decimal import Decimal, InvalidOperation
        try:
            btc = Decimal(claimed)
            valid = btc.is_finite() and btc > 0 and btc <= Decimal("21000000")
        except InvalidOperation:
            valid = False
        if not re.fullmatch(r"[a-f0-9]{64}", txid) or not valid:
            return page("<h3>رقم المعاملة أو المبلغ غير صالح.</h3><a href='/btc-funding'>العودة</a>"), 400
        try:
            with get_db_connection() as con:
                con.execute("INSERT INTO btc_deposit_claims(email,txid,claimed_btc,submitted_at) VALUES(?,?,?,?)",
                            (email, txid, str(btc), utc_now().isoformat()))
                con.commit()
            message = "تم تسجيل بلاغ الإيداع للمراجعة؛ لم يتم إثبات وصول الأموال بعد."
        except sqlite3.IntegrityError:
            message = "هذه المعاملة مسجلة مسبقاً لهذا الحساب."
    address = escape(RAIN_BTC_ADDRESS)
    return page(f"""<div style='max-width:650px;margin:30px auto;padding:24px;background:#161616;line-height:1.9'>
      <h2>إرسال Bitcoin (BTC) إلى محفظة Rain</h2><p>{escape(message)}</p>
      <p><b>تحذير:</b> هذا عنوان محفظة يحدده المشغل، وليس حساب حفظ مستقل باسم المستثمر.
      إرسال الأموال إليه قد يؤدي لفقدان السيطرة عليها. لا ترسل أموالاً قبل التحقق من
      هوية المستلم، والتراخيص والتفويضات والاتفاق المكتوب.</p>
      <p>الشبكة: <b>Bitcoin mainnet (BTC)</b> فقط. تأكد من دعم الإيداع لدى Rain وأن العنوان
      يخص الجهة المقصودة. لا تستخدم شبكة BEP20 أو ERC20.</p>
      <p style='overflow-wrap:anywhere'>العنوان: <code dir='ltr'>{address}</code></p>
      <p>لا يُفعّل التداول الحقيقي تلقائياً عند إرسال الأموال.</p>
      <form method='POST'><label>رقم معاملة BTC (TXID)</label><input name='txid' required minlength='64' maxlength='64'>
      <label>المبلغ BTC</label><input name='btc_amount' required inputmode='decimal' placeholder='0.001'>
      <button type='submit'>تسجيل التحويل للمراجعة</button></form>
      <p><a href='/user-dashboard'>لوحة التحكم</a></p></div>""")

@app.route("/subscription", methods=["GET", "POST"])
def subscription():
    if not session.get("logged_in") or session.get("role") != "user":
        return redirect(url_for("login"))
    email = session["user_email"]
    packages = {"week": ("weekly", 1), "three_weeks": ("weekly", 3),
                "month": ("monthly", 1), "three_months": ("monthly", 3),
                "year": ("yearly", 1)}
    if request.method == "POST":
        key = request.form.get("package")
        reference = (request.form.get("reference") or "").strip()
        if key not in packages or not (4 <= len(reference) <= 120):
            abort(400)
        plan, units = packages[key]
        with get_db_connection() as connection:
            connection.execute(
                "INSERT INTO subscription_payments(email,plan,units,reference,requested_at) VALUES(?,?,?,?,?)",
                (email, plan, units, reference, utc_now().isoformat()))
            connection.commit()
        return page("<h2>تم تسجيل طلب الدفع للمراجعة. لا يتفعّل الاشتراك إلا بعد التحقق من وصول الدفع.</h2><a href='/user-dashboard'>العودة</a>")
    with db_lock:
        user = dict(users_db.get(email, {}))
    status, end = subscription_status(user)
    options = [("week", "أسبوع واحد"), ("three_weeks", "3 أسابيع + الرابع مجاناً"),
               ("month", "شهر واحد"), ("three_months", "3 أشهر + الرابع مجاناً"),
               ("year", "سنة + شهران مجاناً")]
    choices = "".join(f"<option value='{key}'>{escape(label)}</option>" for key, label in options)
    return page(f"""<div style="max-width:620px;margin:40px auto;padding:24px;background:#171717">
      <h2>الاشتراكات</h2><p>7 أيام مجانية لمرة واحدة عند التسجيل، ثم يتطلب الاستمرار اشتراكاً مؤكداً.</p>
      <p>الحالة: {escape(status)}</p>
      <p>انتهاء الصلاحية: {escape(end.isoformat() if end else '—')}</p>
      <p>الدفع لا يُخصم آلياً. سجّل مرجع التحويل بعد الدفع ليتم التحقق منه.</p>
      <form method="POST"><select name="package">{choices}</select>
      <input name="reference" minlength="4" maxlength="120" required placeholder="رقم مرجع الدفع">
      <button type="submit">إرسال طلب التفعيل</button></form>
      <p><a href="/user-dashboard">العودة</a></p></div>""")

@app.route("/subscription-admin", methods=["GET", "POST"])
def subscription_admin():
    if not session.get("logged_in") or session.get("role") != "admin":
        return redirect(url_for("login"))
    if request.method == "POST":
        try:
            payment_id = int(request.form.get("payment_id", "0"))
        except ValueError:
            abort(400)
        # Admin must independently verify funds arrived before approving.
        with db_lock:
            with get_db_connection() as connection:
                record = connection.execute(
                    "SELECT * FROM subscription_payments WHERE id=? AND status='pending'",
                    (payment_id,)).fetchone()
                if record is None:
                    abort(404)
                email = record["email"]
                user = dict(users_db.get(email, {}))
                if not user:
                    abort(404)
                now = utc_now()
                activate_subscription(user, record["plan"], record["units"], now)
                save_user(email, user)
                users_db[email] = user
                connection.execute(
                    "UPDATE subscription_payments SET status='confirmed', confirmed_at=? WHERE id=? AND status='pending'",
                    (now.isoformat(), payment_id))
                connection.commit()
        return redirect(url_for("subscription_admin"))
    with get_db_connection() as connection:
        pending = connection.execute(
            "SELECT * FROM subscription_payments WHERE status='pending' ORDER BY id").fetchall()
    rows = "".join(
        f"<tr><td>{r['id']}</td><td>{escape(r['email'])}</td><td>{escape(r['plan'])}</td>"
        f"<td>{r['units']}</td><td>{escape(r['reference'])}</td><td>"
        f"<form method='POST'><input type='hidden' name='payment_id' value='{r['id']}'>"
        f"<button type='submit'>تأكيد بعد التحقق من الدفع</button></form></td></tr>"
        for r in pending)
    return page("<h2>طلبات الاشتراك المعلقة</h2><p>تحقق من وصول الدفع فعلياً قبل التأكيد.</p>"
                "<table border='1'><tr><th>ID</th><th>البريد</th><th>الخطة</th><th>العدد</th>"
                "<th>المرجع</th><th>الإجراء</th></tr>" + rows + "</table><a href='/admin-panel'>العودة</a>")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


def auto_trading_engine():
    # Read balance only; do not place real orders or withdraw funds automatically.
    while True:
        try:
            if exchange is not None:
                balance = exchange.fetch_balance()
                free = float((balance.get("free") or {}).get("USDT") or 0.0)
                with db_lock:
                    admin_record = dict(users_db.get(ADMIN_EMAIL, {}))
                    if admin_record:
                        admin_record["exchange_balance"] = free
                        users_db[ADMIN_EMAIL] = admin_record
                if admin_record:
                    save_user(ADMIN_EMAIL, admin_record)
        except Exception:
            app.logger.exception("Could not refresh exchange balance")
        time.sleep(15)


if __name__ == "__main__":
    if os.environ.get("AUTO_TRADING_ENABLED", "false").lower() == "true":
        threading.Thread(target=auto_trading_engine, daemon=True).start()
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port, debug=False)
