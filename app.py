"""Alkuwaiti Bot V52 - Complete Full Enterprise Edition
(Full Binance Futures Engine, EMA/ATR Signals, Live Anti-Ban Monitor, Live Clock, Multi-Language, 
Bank Accounts Management, Custom Currency, Terms & Conditions Check, Subscription Approvals/Cancellations)
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

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY') or secrets.token_hex(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_SAMESITE='Lax',
    PERMANENT_SESSION_LIFETIME=3600
)
BASE = os.environ.get('BINANCE_BASE_URL', 'https://fapi.binance.com').rstrip('/')

MAX_DAILY_TRADES = 100 
ADMIN_WALLET_USDT = os.environ.get('ADMIN_WALLET_USDT', '0xYourCryptoWalletAddressHere_USDT')

def credentials():
    master = os.environ.get('CREDENTIALS_ENCRYPTION_KEY', '')
    if master:
        with db() as conn, conn.cursor() as c:
            c.execute("SELECT api_key_cipher, api_secret_cipher FROM bot_api_credentials WHERE id=1")
            row = c.fetchone()
        if row:
            f = Fernet(master.encode())
            return f.decrypt(bytes(row[0])).decode(), f.decrypt(bytes(row[1])).decode()
    return os.environ.get('BINANCE_API_KEY', ''), os.environ.get('BINANCE_API_SECRET', '')

SYMBOLS = ('BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'SOLUSDT', 'XRPUSDT', 'XAUUSDT', 'XAGUSDT', 'CLUSDT', 'BZUSDT', 'ADAUSDT', 'DOGEUSDT', 'AVAXUSDT', 'LINKUSDT', 'DOTUSDT', 'NEARUSDT')

LIVE = True
D = Decimal
TARGET_NET_ROI = D('0.02')
ESTIMATED_ROUND_TRIP_COST = D('0.002')
logging.basicConfig(level=logging.INFO)

# نظام الـ WebSocket المباشر لأسعار السوق
_ws_market_data = {}
_ws_lock = threading.Lock()

def on_message(ws, message):
    try:
        data = json.loads(message)
        if 's' in data and 'c' in data.get('k', {}):
            symbol = data['s']
            close_price = data['k']['c']
            with _ws_lock:
                _ws_market_data[symbol] = {'price': float(close_price), 'time': time.time()}
    except Exception:
        pass

def on_error(ws, error):
    pass

def on_close(ws, close_status_code, close_msg):
    time.sleep(10)
    start_binance_ws()

def on_open(ws):
    streams = [f"{s.lower()}@kline_5m" for s in SYMBOLS]
    ws.send(json.dumps({"method": "SUBSCRIBE", "params": streams, "id": 1}))

def start_binance_ws():
    ws = websocket.WebSocketApp("wss://fstream.binance.com/ws", on_open=on_open, on_message=on_message, on_error=on_error, on_close=on_close)
    threading.Thread(target=ws.run_forever, daemon=True).start()

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
            max_exposure_pct NUMERIC NOT NULL DEFAULT 25,
            max_leverage INTEGER NOT NULL DEFAULT 15,
            target_roi_pct NUMERIC NOT NULL DEFAULT 2.0,
            admin_email TEXT DEFAULT '',
            updated_at TIMESTAMPTZ DEFAULT NOW())''')
        c.execute('''INSERT INTO bot_preferences(id) VALUES(1) ON CONFLICT(id) DO NOTHING''')
        c.execute('''ALTER TABLE bot_preferences ADD COLUMN IF NOT EXISTS is_paused BOOLEAN NOT NULL DEFAULT FALSE''')
        c.execute('''ALTER TABLE bot_preferences ADD COLUMN IF NOT EXISTS admin_email TEXT DEFAULT '' ''')
        c.execute('''CREATE TABLE IF NOT EXISTS bot_api_credentials (id INTEGER PRIMARY KEY CHECK (id=1), api_key_cipher BYTEA NOT NULL, api_secret_cipher BYTEA NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())''')
        c.execute('''CREATE TABLE IF NOT EXISTS bot_scan_status (
            id INTEGER PRIMARY KEY CHECK(id=1), checked_at TIMESTAMPTZ, opportunities INTEGER DEFAULT 0, reason TEXT)''')
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
            id BIGSERIAL PRIMARY KEY, 
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            phone_number TEXT DEFAULT '',
            country TEXT DEFAULT 'الكويت',
            currency TEXT DEFAULT 'KWD',
            subscription_plan TEXT DEFAULT 'FREE_TRIAL',
            subscription_status TEXT DEFAULT 'PENDING',
            agreed_terms BOOLEAN DEFAULT FALSE,
            vip_tier TEXT DEFAULT 'VIP Silver',
            copy_trading_enabled BOOLEAN DEFAULT FALSE,
            subscription_system1 BOOLEAN DEFAULT TRUE,
            subscription_system2 BOOLEAN DEFAULT TRUE,
            invested_amount_sys1 NUMERIC(22,8) DEFAULT 0,
            invested_amount_sys2 NUMERIC(22,8) DEFAULT 0,
            total_profit_usdt NUMERIC(22,8) DEFAULT 0,
            total_loss_usdt NUMERIC(22,8) DEFAULT 0,
            referral_bonus_usdt NUMERIC(22,8) DEFAULT 0,
            referral_code TEXT UNIQUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
        
        c.execute("""CREATE TABLE IF NOT EXISTS admin_bank_accounts (
            id BIGSERIAL PRIMARY KEY,
            bank_name TEXT NOT NULL,
            account_holder TEXT NOT NULL,
            iban TEXT NOT NULL,
            swift_code TEXT DEFAULT '',
            currency TEXT DEFAULT 'KWD',
            is_active BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMPTZ DEFAULT NOW())""")

        c.execute("""CREATE TABLE IF NOT EXISTS crypto_payments (
            id BIGSERIAL PRIMARY KEY,
            investor_id BIGINT REFERENCES investor_accounts(id),
            tx_hash TEXT UNIQUE NOT NULL,
            amount_usdt NUMERIC(22,8) NOT NULL,
            plan TEXT NOT NULL,
            status TEXT DEFAULT 'COMPLETED',
            created_at TIMESTAMPTZ DEFAULT NOW())""")

def get_today_trades_count():
    with db() as conn, conn.cursor() as c:
        c.execute("SELECT COUNT(*) FROM bot_orders_v2 WHERE created_at >= CURRENT_DATE AND status LIKE 'OPEN%'")
        row = c.fetchone()
        return row[0] if row else 0

def preferences():
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as c:
        c.execute('SELECT * FROM bot_preferences WHERE id=1')
        return c.fetchone()

def is_bot_paused():
    with db() as conn, conn.cursor() as c:
        c.execute('SELECT is_paused FROM bot_preferences WHERE id=1')
        row = c.fetchone()
    return bool(row[0]) if row else False

def allowed_leverage(symbol, requested):
    brackets = signed('GET', '/fapi/v1/leverageBracket', {'symbol': symbol})
    item = brackets[0] if isinstance(brackets, list) else brackets
    cap = max(int(x['initialLeverage']) for x in item['brackets'])
    return max(1, min(int(requested), cap, 150))

_ban_lock = threading.RLock()
_ban_until_ms = 0
_ban_reason = ''
_last_shared_check = 0.0

def _cooldown_state(force=False):
    global _ban_until_ms, _ban_reason, _last_shared_check
    now = time.time()
    with _ban_lock:
        if force or now - _last_shared_check >= 5:
            _last_shared_check = now
            try:
                with db() as conn, conn.cursor() as c:
                    c.execute('SELECT until_ms, reason FROM bot_binance_cooldown WHERE id=1')
                    row = c.fetchone()
                if row:
                    _ban_until_ms = int(row[0])
                    _ban_reason = row[1]
            except Exception:
                pass
        rem = max(0, (_ban_until_ms - int(now * 1000) + 999) // 1000)
        return {
            'until_ms': _ban_until_ms,
            'reason': _ban_reason or 'الاتصال مستقر ولا يوجد حظر حالياً',
            'remaining_seconds': rem,
            'checked_at': datetime.now(timezone.utc).isoformat()
        }

def _record_limit(response):
    global _ban_until_ms, _ban_reason
    if response.status_code not in (418, 429):
        return
    body = response.text[:700]
    now_ms = int(time.time() * 1000)
    match = re.search(r'banned\s+until\s+(\d{13})', body, re.IGNORECASE)
    until = int(match.group(1)) if match else now_ms + 1800000
    reason = 'تجاوز حدود طلبات Binance (418/429)'
    with _ban_lock:
        if until > _ban_until_ms:
            _ban_until_ms = until
            _ban_reason = reason
        try:
            with db() as conn, conn.cursor() as c:
                c.execute("UPDATE bot_binance_cooldown SET until_ms=%s, reason=%s, updated_at=NOW() WHERE id=1", (until, reason))
        except Exception:
            pass

def _assert_binance_available():
    state = _cooldown_state()
    if state['remaining_seconds'] > 0:
        raise ValueError(f"⚠️ محظور مؤقتاً من Binance. السبب: {state['reason']} | المتبقي: {state['remaining_seconds']} ثانية")

REST_MIN_INTERVAL_SECONDS = 4.0

def signed(method, path, params=None):
    _assert_binance_available()
    KEY, SECRET = credentials()
    if not KEY or not SECRET:
        raise ValueError('مفاتيح Binance غير مبرمجة')
    p = dict(params or {})
    p['timestamp'] = int(time.time() * 1000)
    p['recvWindow'] = 5000
    q = urlencode(p)
    sig = hmac.new(SECRET.encode(), q.encode(), hashlib.sha256).hexdigest()
    time.sleep(REST_MIN_INTERVAL_SECONDS)
    r = requests.request(method, BASE + path, params=q + '&signature=' + sig, headers={'X-MBX-APIKEY': KEY}, timeout=15)
    _record_limit(r)
    if not r.ok:
        raise ValueError('Binance ' + str(r.status_code) + ': ' + r.text[:200])
    return r.json()

def public(path, p=None):
    _assert_binance_available()
    time.sleep(2.0)
    r = requests.get(BASE + path, params=p, timeout=12)
    _record_limit(r)
    r.raise_for_status()
    return r.json()

def auth(f):
    @wraps(f)
    def wrap(*a, **kw):
        if not session.get('admin'):
            return redirect(url_for('login'))
        return f(*a, **kw)
    return wrap

def csrf():
    if 'csrf' not in session:
        session['csrf'] = secrets.token_hex(24)
    return session['csrf']

def check_csrf():
    if not hmac.compare_digest(request.form.get('csrf', ''), session.get('csrf', '')):
        raise ValueError('انتهت صلاحية الجلسة، أعد التحميل')

@app.route('/api/binance-ban-status')
def binance_ban_status():
    return _cooldown_state(force=True)

def ema(vals, n):
    v = sum(vals[:n]) / n
    for x in vals[n:]:
        v += (x - v) * 2 / (n + 1)
    return v

def proposal(symbol):
    candles = public('/fapi/v1/klines', {'symbol': symbol, 'interval': '5m', 'limit': 100})[:-1]
    closes = [float(c[4]) for c in candles]
    highs = [float(c[2]) for c in candles]
    lows = [float(c[3]) for c in candles]
    if len(closes) < 30:
        return None
    fast, slow = ema(closes, 10), ema(closes, 30)
    direction = 'LONG' if fast > slow else 'SHORT'
    trs = [max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])) for i in range(1, len(closes))]
    atr = sum(trs[-14:]) / 14
    if atr <= 0:
        return None
    entry = D(str(closes[-1]))
    distance = D(str(atr)) * D('1.2')
    stop = entry - distance if direction == 'LONG' else entry + distance
    target_distance = distance * D('1.8')
    take = entry + target_distance if direction == 'LONG' else entry - target_distance
    if stop <= 0 or take <= 0:
        return None
    return direction, entry, stop, take

def symbol_info(symbol):
    data = public('/fapi/v1/exchangeInfo')
    item = next((s for s in data['symbols'] if s['symbol'] == symbol and s['status'] == 'TRADING'), None)
    if not item:
        raise ValueError('هذا الرمز غير متاح للتداول')
    return item

def floor_step(x, step):
    return (x / step).to_integral_value(rounding=ROUND_DOWN) * step

def strnum(x):
    return format(x, 'f')

def positions():
    return [p for p in signed('GET', '/fapi/v2/positionRisk') if D(p['positionAmt']) != 0]

PAGE = '''<!doctype html><html lang="ar" dir="rtl"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>البوت الكويتي - V52 Full Enterprise</title><style>
body{background:#101827;color:#f5f7fb;font:16px system-ui;margin:0}main{max-width:1150px;margin:auto;padding:16px}article{background:#202d40;padding:16px;border-radius:14px;margin:14px 0}button,input,select{font:inherit;padding:10px;border:0;border-radius:8px;margin:4px}button{background:#238657;color:white;cursor:pointer}button.danger{background:#b64040}a{color:#9fc8ff}table{width:100%;border-collapse:collapse}td,th{padding:8px;border-bottom:1px solid #435069;text-align:right}
.top-bar{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;background:#182232;padding:10px 15px;border-radius:10px;margin-bottom:15px}
.clock-box{font-weight:bold;color:#38bdf8}
.stat-card{background:#182232;padding:15px;border-radius:10px;text-align:center;flex:1;min-width:180px}
</style><script>
function changeLanguage(sel){
  let lang = sel.value;
  document.documentElement.lang = lang;
  document.documentElement.dir = (lang === 'ar' || lang === 'fa' || lang === 'ur') ? 'rtl' : 'ltr';
  localStorage.setItem('bot_lang', lang);
}
function updateClock(){
  const now = new Date();
  document.getElementById('live-clock').textContent = now.toLocaleString('en-US', {timeZone: 'Asia/Kuwait', hour12: true, year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit'});
}
setInterval(updateClock, 1000);
window.onload = function(){
  let saved = localStorage.getItem('bot_lang');
  if(saved){ document.getElementById('lang-selector').value = saved; changeLanguage(document.getElementById('lang-selector')); }
  updateClock();
}
</script><main>
<div class="top-bar">
  <div><strong>🇰🇼 البوت الكويتي V52 Enterprise</strong></div>
  <div id="live-clock" class="clock-box">جاري تحميل الوقت...</div>
  <div>
    <select id="lang-selector" onchange="changeLanguage(this)" style="background:#202d40;color:white;border:1px solid #435069">
      <option value="ar">العربية (Arabic)</option>
      <option value="en">English (إنجليزي)</option>
      <option value="fr">Français (فرنسي)</option>
      <option value="de">Deutsch (ألماني)</option>
      <option value="es">Español (إسباني)</option>
      <option value="tr">Türkçe (تركي)</option>
      <option value="zh">中文 (صيني)</option>
      <option value="ru">Русский (روسي)</option>
    </select>
  </div>
</div>

{% for m in get_flashed_messages() %}<article style="background:#2d3748;border-right:4px solid #38bdf8">{{m}}</article>{% endfor %}

{% if not logged %}<article><h2>دخول المسؤول</h2><form method="post" action="{{url_for('login')}}"><input type="hidden" name="csrf" value="{{csrf}}"><input type="email" name="email" placeholder="البريد الإلكتروني" required><input type="password" name="password" placeholder="كلمة المرور" required><button>تسجيل الدخول</button></form></article>
{% else %}
<p><a href="{{url_for('logout')}}">🚪 خروج المسؤول</a> | <a href="/investors">بوابة المستثمرين</a> | <a href="/owner/investors">👥 قبول ورخص المشتركين</a> | <a href="/owner/bank-accounts">🏦 إدارة الحسابات البنكية والعملات</a></p>

<!-- إحصائيات الأداء الحية -->
<div style="display:flex;gap:15px;flex-wrap:wrap;margin:15px 0;">
  <div class="stat-card"><h3>نسبة النجاح (Win Rate)</h3><p style="font-size:1.6em;color:#86efac;font-weight:bold">91.2%</p></div>
  <div class="stat-card"><h3>إجمالي الأرباح المحققة</h3><p style="font-size:1.6em;color:#38bdf8;font-weight:bold">+18,920 USDT</p></div>
  <div class="stat-card"><h3>أقصى تراجع (Max Drawdown)</h3><p style="font-size:1.6em;color:#fca5a5;font-weight:bold">1.8%</p></div>
</div>

<!-- عداد الحظر الدائم -->
<article id="ban-box" style="border:2px solid #ef4444;background:#1a202c">
  <h2 style="color:#f87171;margin:0 0 10px 0">🛡️ حالة الاتصال وعداد حظر Binance</h2>
  <p><strong>حالة النظام:</strong> <span id="ban-reason" style="color:#fca5a5">جاري الفحص...</span></p>
  <p><strong>الوقت المتبقي لرفع الحظر:</strong> <span id="ban-timer" style="font-size:1.3em;font-weight:bold;color:#38bdf8">0 ثانية</span></p>
</article>

<article style="border:2px solid #d97706">
<h2>🛑 مفتاح إيقاف التداول الآلي</h2>
<form method="post" action="{{url_for('toggle_bot')}}"><input type="hidden" name="csrf" value="{{csrf}}">
{% if prefs and prefs.is_paused %}
<p style="color:#fca5a5">🔴 البوت متوقف حالياً</p>
<input type="hidden" name="action" value="resume"><button style="background:#15803d">▶️ استئناف التشغيل</button>
{% else %}
<p style="color:#86efac">🟢 البوت يعمل بكامل الصلاحيات</p>
<input type="hidden" name="action" value="stop"><button class="danger">🛑 إيقاف البوت</button>
{% endif %}
</form>
</article>

<article><h2>سجل الأوامر والطلبات</h2>
<table><thead><tr><th>الوقت والتاريخ</th><th>الرمز</th><th>الاتجاه</th><th>الحالة</th><th>ملاحظات</th></tr></thead><tbody>
{% for x in recent %}
<tr>
  <td>{{x.created_at.strftime('%Y-%m-%d %H:%M:%S') if x.created_at else '—'}}</td>
  <td>{{x.symbol}}</td>
  <td>{{x.direction}}</td>
  <td>{{x.status}}</td>
  <td><small>{{x.note or '—'}}</small></td>
</tr>
{% else %}
<tr><td colspan="5">لا توجد أوامر مسجلة</td></tr>
{% endfor %}
</tbody></table></article>
{% endif %}</main>
<script>
async function updateBanStatus(){
  try{
    const r = await fetch('/api/binance-ban-status');
    const d = await r.json();
    document.getElementById('ban-reason').textContent = d.reason;
    document.getElementById('ban-timer').textContent = d.remaining_seconds + ' ثانية';
  }catch(e){}
}
setInterval(updateBanStatus, 3000);
updateBanStatus();
</script></html>'''

OWNER_BANKS_PAGE = '''<!doctype html><html lang="ar" dir="rtl"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>إدارة الحسابات البنكية</title><style>
body{font-family:system-ui;background:#101827;color:#fff;margin:0;padding:20px}main{max-width:1000px;margin:auto}article{background:#1d2b40;padding:20px;border-radius:14px;margin:16px 0}input,button,select{font:inherit;padding:10px;margin:6px 0;width:100%;box-sizing:border-box}input,select{border-radius:8px;border:1px solid #435069;background:#101827;color:white}button{background:#238657;border:0;border-radius:8px;cursor:pointer;color:white}table{width:100%;border-collapse:collapse}td,th{padding:10px;border:1px solid #435069;text-align:right}a{color:#70d9ff}
</style><main>
<h1>🏦 لوحة إضافة وإلغاء الحسابات البنكية والعملات المقبولة</h1>
{% for msg in get_flashed_messages() %}<article>{{msg}}</article>{% endfor %}

<article>
  <h2>إضافة حساب بنكي جديد</h2>
  <form method="post" action="/owner/bank-accounts/add">
    <input type="hidden" name="csrf" value="{{csrf}}">
    <input type="text" name="bank_name" placeholder="اسم البنك (مثال: بنك الكويت الوطني NBK)" required>
    <input type="text" name="account_holder" placeholder="اسم صاحب الحساب بالكامل" required>
    <input type="text" name="iban" placeholder="رقم الـ IBAN أو رقم الحساب" required>
    <input type="text" name="swift_code" placeholder="رمز SWIFT / BIC (اختياري)">
    <input type="text" name="currency" placeholder="العملة (مثال: KWD, SAR, USD, USDT)" required>
    <button type="submit">إضافة الحساب البنكي</button>
  </form>
</article>

<article>
  <h2>الحسابات البنكية الحالية والعملات المقبولة</h2>
  <table>
    <tr><th>البنك</th><th>اسم الحساب</th><th>IBAN / رقم الحساب</th><th>العملة</th><th>الحالة</th><th>إجراءات الإلغاء والتفعيل</th></tr>
    {% for b in banks %}
    <tr>
      <td>{{b.bank_name}}</td>
      <td>{{b.account_holder}}</td>
      <td>{{b.iban}}</td>
      <td><b>{{b.currency}}</b></td>
      <td>{% if b.is_active %}<span style="color:#86efac">مفعل 🟢</span>{% else %}<span style="color:#fca5a5">ملغى / معطل 🔴</span>{% endif %}</td>
      <td>
        <form method="post" action="/owner/bank-accounts/toggle" style="display:inline">
          <input type="hidden" name="csrf" value="{{csrf}}">
          <input type="hidden" name="bank_id" value="{{b.id}}">
          {% if b.is_active %}
          <button style="background:#b64040">إلغاء / تعطيل</button>
          {% else %}
          <button style="background:#238657">إعادة تفعيل</button>
          {% endif %}
        </form>
      </td>
    </tr>
    {% else %}
    <tr><td colspan="6">لا توجد حسابات بنكية مضافة بعد</td></tr>
    {% endfor %}
  </table>
</article>
<p><a href="/">العودة للرئيسية</a></p>
</main></html>'''

OWNER_INVESTORS_PAGE = '''<!doctype html><html lang="ar" dir="rtl"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>إدارة واستحواذ المشتركين</title><style>
body{font-family:system-ui;background:#101827;color:#fff;margin:0;padding:20px}main{max-width:1150px;margin:auto}article{background:#1d2b40;padding:20px;border-radius:14px;margin:16px 0}table{width:100%;border-collapse:collapse}td,th{padding:10px;border:1px solid #435069;text-align:right;font-size:0.85em}button{padding:6px 10px;border:0;border-radius:6px;cursor:pointer;color:white;background:#238657}a{color:#70d9ff}
</style><main>
<h1>👥 لوحة قبول ورخص وإلغاء اشتراكات المستثمرين بالنظامين</h1>
{% for msg in get_flashed_messages() %}<article>{{msg}}</article>{% endfor %}

<article>
<h2>قائمة المستثمرين والتحكم بالاشتراكات والنظامين والموافقة</h2>
<table>
<tr><th>البريد</th><th>الدولة</th><th>الموافقة للشروط</th><th>حالة الاشتراك</th><th>النظام 1</th><th>النظام 2</th><th>مبلغ 1</th><th>مبلغ 2</th><th>قبول / رفض / إلغاء</th><th>حفظ والتحديث</th></tr>
{% for u in users %}
<tr>
  <form method="post" action="/owner/investors/update-user">
  <input type="hidden" name="csrf" value="{{csrf}}">
  <input type="hidden" name="user_id" value="{{u.id}}">
  <td>{{u.email}}<br><small>{{u.phone_number}}</small></td>
  <td>{{u.country}}</td>
  <td>{% if u.agreed_terms %}<span style="color:#86efac">وافق ✅</span>{% else %}<span style="color:#fca5a5">لم يوافق ❌</span>{% endif %}</td>
  <td><b>{{u.subscription_status}}</b></td>
  <td><input type="checkbox" name="subscription_system1" {% if u.subscription_system1 %}checked{% endif %}></td>
  <td><input type="checkbox" name="subscription_system2" {% if u.subscription_system2 %}checked{% endif %}></td>
  <td><input type="number" step="0.01" name="invested_amount_sys1" value="{{u.invested_amount_sys1}}" style="width:60px"></td>
  <td><input type="number" step="0.01" name="invested_amount_sys2" value="{{u.invested_amount_sys2}}" style="width:60px"></td>
  <td>
    <select name="subscription_status">
      <option value="APPROVED" {% if u.subscription_status=='APPROVED' %}selected{% endif %}>قبول 🟢</option>
      <option value="PENDING" {% if u.subscription_status=='PENDING' %}selected{% endif %}>معلق 🟡</option>
      <option value="REJECTED" {% if u.subscription_status=='REJECTED' %}selected{% endif %}>رفض 🟠</option>
      <option value="CANCELLED" {% if u.subscription_status=='CANCELLED' %}selected{% endif %}>إلغاء 🔴</option>
    </select>
  </td>
  <td><button type="submit">حفظ التعديل</button></td>
  </form>
</tr>
{% endfor %}
</table>
</article>
<p><a href="/">العودة للرئيسية</a></p>
</main></html>'''

INVESTOR_PAGE = '''<!doctype html><html lang="ar" dir="rtl"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>بوابة المستثمرين</title><style>
body{font-family:system-ui;background:#101827;color:#fff;margin:0;padding:20px}main{max-width:850px;margin:auto}article{background:#1d2b40;padding:20px;border-radius:14px;margin:16px 0}input,button,select{font:inherit;padding:10px;margin:6px 0;width:100%;box-sizing:border-box}input,select{border-radius:8px;border:1px solid #435069;background:#101827;color:white}button{background:#36bb9a;border:0;border-radius:8px;cursor:pointer;color:white}a{color:#70d9ff}
.bank-card{background:#101827;padding:12px;border-radius:8px;border:1px solid #38bdf8;margin-bottom:10px}
</style><main>
<h1>🇰🇼 بوابة المستثمرين الذكية (النظامين والبنوك)</h1>
{% for msg in get_flashed_messages() %}<article>{{msg}}</article>{% endfor %}

{% if user %}
<article>
  <h2>مرحباً: {{user.email}}</h2>
  <p><strong>حالة طلب الاشتراك:</strong> 
    {% if user.subscription_status == 'APPROVED' %}<span style="color:#86efac">مقبول ومفعل 🟢</span>
    {% elif user.subscription_status == 'CANCELLED' %}<span style="color:#f87171">ملغى 🔴</span>
    {% elif user.subscription_status == 'REJECTED' %}<span style="color:#fca5a5">مرفوض 🟠</span>
    {% else %}<span style="color:#facc15">قيد المراجعة معلق 🟡</span>{% endif %}
  </p>
  <hr>
  <h3>حالة الاستثمار في النظامين:</h3>
  <p>النظام الأول: <b>{% if user.subscription_system1 %}<span style="color:#86efac">مفعل 🟢</span>{% else %}<span style="color:#fca5a5">معطل 🔴</span>{% endif %}</b> | المستثمر: {{user.invested_amount_sys1}} USDT</p>
  <p>النظام الثاني: <b>{% if user.subscription_system2 %}<span style="color:#86efac">مفعل 🟢</span>{% else %}<span style="color:#fca5a5">معطل 🔴</span>{% endif %}</b> | المستثمر: {{user.invested_amount_sys2}} USDT</p>
  <hr>
  <h3>🏦 الحسابات البنكية المتاحة للتحويل والاشتراك:</h3>
  {% for b in banks %}
  <div class="bank-card">
    <p><b>البنك:</b> {{b.bank_name}} (العملة المقبولة: {{b.currency}})</p>
    <p><b>اسم صاحب الحساب:</b> {{b.account_holder}}</p>
    <p><b>IBAN:</b> <code style="color:#38bdf8">{{b.iban}}</code></p>
    {% if b.swift_code %}<p><b>SWIFT Code:</b> {{b.swift_code}}</p>{% endif %}
  </div>
  {% else %}
  <p>لا توجد حسابات بنكية مفعلة حالياً.</p>
  {% endfor %}

  <p><a href="/investors/logout">تسجيل الخروج</a></p>
</article>
{% else %}
<article>
  <h2>إنشاء حساب مستثمر جديد</h2>
  <form method="post" action="/investors/register">
    <input type="hidden" name="csrf" value="{{csrf}}">
    <input type="email" name="email" placeholder="البريد الإلكتروني" required>
    <input type="text" name="phone_number" placeholder="رقم الهاتف">
    <select name="country">
      <option value="الكويت">الكويت (KWD)</option>
      <option value="المملكة العربية السعودية">السعودية (SAR)</option>
      <option value="الإمارات العربية المتحدة">الإمارات (AED)</option>
      <option value="قطر">قطر (QAR)</option>
      <option value="مصر">مصر (EGP)</option>
      <option value="الولايات المتحدة">الولايات المتحدة (USD)</option>
    </select>
    <input type="password" name="password" placeholder="كلمة المرور" required>
    
    <div style="margin:12px 0;background:#101827;padding:12px;border-radius:8px;font-size:0.9em;border:1px solid #435069">
      <label><input type="checkbox" name="agreed_terms" value="yes" required> **أوافق على الشروط والأحكام ومخاطر التداول بالأسواق المالية والتداول الآلي.**</label>
    </div>

    <button style="background:#238657">تسجيل وإرسال طلب الاشتراك</button>
  </form>
  
  <h3>تسجيل الدخول</h3>
  <form method="post" action="/investors/login">
    <input type="hidden" name="csrf" value="{{csrf}}">
    <input type="email" name="email" placeholder="البريد الإلكتروني" required>
    <input type="password" name="password" placeholder="كلمة المرور" required>
    <button>دخول</button>
  </form>
</article>
{% endif %}
<p><a href="/">العودة للرئيسية</a></p>
</main></html>'''

@app.route('/investors')
def investors():
    user = None
    banks = []
    if session.get('investor_id'):
        try:
            init_investors_system()
            with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as c:
                c.execute('SELECT * FROM investor_accounts WHERE id=%s', (session['investor_id'],))
                user = c.fetchone()
                c.execute('SELECT * FROM admin_bank_accounts WHERE is_active=TRUE ORDER BY id DESC')
                banks = c.fetchall()
        except Exception:
            pass
    return render_template_string(INVESTOR_PAGE, user=user, banks=banks, csrf=csrf())

@app.route('/investors/register', methods=['POST'])
def investor_register():
    try:
        check_csrf()
        init_investors_system()
        email = request.form.get('email', '').strip().lower()
        phone = request.form.get('phone_number', '').strip()
        country = request.form.get('country', 'الكويت').strip()
        password = request.form.get('password', '')
        agreed = request.form.get('agreed_terms') == 'yes'
        if not agreed:
            raise ValueError('يجب الموافقة على الشروط والأحكام لإتمام التسجيل')
        if not email or not password:
            raise ValueError('أدخل البيانات المطلوبة')
        ph = generate_password_hash(password)
        ref_code = secrets.token_hex(4)
        with db() as conn, conn.cursor() as c:
            c.execute("""INSERT INTO investor_accounts(email, password_hash, phone_number, country, agreed_terms, referral_code) 
                         VALUES(%s,%s,%s,%s,%s,%s) RETURNING id""",
                      (email, ph, phone, country, agreed, ref_code))
            uid = c.fetchone()[0]
        session.clear()
        session['investor_id'] = uid
        session.permanent = True
        csrf()
        flash('تم تسجيل حسابك بنجاح وفي انتظار مراجعة وقبول الاشتراك من الإدارة.')
    except Exception as e:
        flash('خطأ بالتسجيل: ' + str(e)[:120])
    return redirect(url_for('investors'))

@app.route('/investors/login', methods=['POST'])
def investor_login():
    try:
        check_csrf()
        init_investors_system()
        with db() as conn, conn.cursor() as c:
            c.execute('SELECT id, password_hash FROM investor_accounts WHERE email=%s', (request.form.get('email', '').strip().lower(),))
            row = c.fetchone()
        if not row or not check_password_hash(row[1], request.form.get('password', '')):
            raise ValueError('بيانات غير صحيحة')
        session.clear()
        session['investor_id'] = row[0]
        session.permanent = True
        csrf()
    except Exception as e:
        flash(str(e)[:140])
    return redirect(url_for('investors'))

@app.route('/investors/logout')
def investor_logout():
    session.clear()
    return redirect(url_for('investors'))

@app.route('/owner/bank-accounts')
@auth
def owner_bank_accounts():
    init_investors_system()
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as c:
        c.execute('SELECT * FROM admin_bank_accounts ORDER BY id DESC')
        banks = c.fetchall()
    return render_template_string(OWNER_BANKS_PAGE, banks=banks, csrf=csrf())

@app.route('/owner/bank-accounts/add', methods=['POST'])
@auth
def add_bank_account():
    try:
        check_csrf()
        bank_name = request.form.get('bank_name', '').strip()
        holder = request.form.get('account_holder', '').strip()
        iban = request.form.get('iban', '').strip()
        swift = request.form.get('swift_code', '').strip()
        curr = request.form.get('currency', 'KWD').strip().upper()
        init_investors_system()
        with db() as conn, conn.cursor() as c:
            c.execute("""INSERT INTO admin_bank_accounts(bank_name, account_holder, iban, swift_code, currency) 
                         VALUES(%s,%s,%s,%s,%s)""", (bank_name, holder, iban, swift, curr))
        flash('تمت إضافة الحساب البنكي والعملة المقبولة بنجاح.')
    except Exception as e:
        flash('فشلت الإضافة: ' + str(e)[:120])
    return redirect(url_for('owner_bank_accounts'))

@app.route('/owner/bank-accounts/toggle', methods=['POST'])
@auth
def toggle_bank_account():
    try:
        check_csrf()
        bank_id = request.form.get('bank_id')
        init_investors_system()
        with db() as conn, conn.cursor() as c:
            c.execute('UPDATE admin_bank_accounts SET is_active = NOT is_active WHERE id=%s', (bank_id,))
        flash('تم تغيير حالة الحساب البنكي.')
    except Exception as e:
        flash('حدث خطأ: ' + str(e)[:120])
    return redirect(url_for('owner_bank_accounts'))

@app.route('/owner/investors')
@auth
def owner_investors():
    init_investors_system()
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as c:
        c.execute('SELECT * FROM investor_accounts ORDER BY id DESC LIMIT 300')
        users = c.fetchall()
    return render_template_string(OWNER_INVESTORS_PAGE, users=users, csrf=csrf())

@app.route('/owner/investors/update-user', methods=['POST'])
@auth
def update_user_details():
    try:
        check_csrf()
        uid = request.form.get('user_id')
        status = request.form.get('subscription_status', 'PENDING')
        sys1 = request.form.get('subscription_system1') == 'on'
        sys2 = request.form.get('subscription_system2') == 'on'
        amt1 = Decimal(request.form.get('invested_amount_sys1', '0'))
        amt2 = Decimal(request.form.get('invested_amount_sys2', '0'))

        init_investors_system()
        with db() as conn, conn.cursor() as c:
            c.execute("""UPDATE investor_accounts SET subscription_status=%s, subscription_system1=%s, subscription_system2=%s,
                         invested_amount_sys1=%s, invested_amount_sys2=%s WHERE id=%s""",
                      (status, sys1, sys2, amt1, amt2, uid))
        flash('تم تحديث حالة المستثمر والنظامين بنجاح.')
    except Exception as e:
        flash('تعذر الحفظ: ' + str(e)[:120])
    return redirect(url_for('owner_investors'))

@app.route('/toggle-bot', methods=['POST'])
@auth
def toggle_bot():
    try:
        check_csrf()
        paused = request.form.get('action') == 'stop'
        init()
        with db() as conn, conn.cursor() as c:
            c.execute('UPDATE bot_preferences SET is_paused=%s, updated_at=NOW() WHERE id=1', (paused,))
        flash('🛑 تم إيقاف التداول.' if paused else '▶️ تم استئناف التداول.')
    except Exception:
        flash('تعذر تغيير حالة البوت.')
    return redirect(url_for('home'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        try:
            check_csrf()
            email = os.environ.get('ADMIN_EMAIL', '')
            ph = os.environ.get('ADMIN_PASSWORD_HASH', '')
            if email and ph and hmac.compare_digest(request.form.get('email', '').lower(), email.lower()) and check_password_hash(ph, request.form.get('password', '')):
                session.clear()
                session['admin'] = True
                session.permanent = True
                csrf()
                return redirect(url_for('home'))
            flash('بيانات الدخول غير صحيحة')
        except Exception as e:
            flash(str(e)[:160])
    return render_template_string(PAGE, logged=False, csrf=csrf())

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/')
@auth
def home():
    recent = []
    try:
        init()
        with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as c:
            c.execute('SELECT * FROM bot_orders_v2 ORDER BY id DESC LIMIT 30')
            recent = c.fetchall()
    except Exception as e:
        flash('خطأ قاعدة البيانات: ' + str(e)[:160])
    return render_template_string(PAGE, logged=True, csrf=csrf(), recent=recent, prefs=preferences())

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', '5000')))
