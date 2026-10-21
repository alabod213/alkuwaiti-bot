
import os
import threading
import time
from decimal import Decimal, ROUND_DOWN
from functools import wraps

import ccxt
from flask import (
    Flask, request, redirect, session,
    render_template_string, url_for
)
from werkzeug.security import check_password_hash

app = Flask(__name__)
app.secret_key = os.environ["SECRET_KEY"]

ADMIN_EMAIL = os.environ["ADMIN_EMAIL"]
ADMIN_PASSWORD_HASH = os.environ["ADMIN_PASSWORD_HASH"]

API_KEY = os.getenv("BINANCE_API_KEY", "")
API_SECRET = os.getenv("BINANCE_API_SECRET", "")

SYMBOLS = [
    "BTC/USDT:USDT",
    "ETH/USDT:USDT",
    "BNB/USDT:USDT",
    "SOL/USDT:USDT",
    "XRP/USDT:USDT",
]

MAX_EXPOSURE = Decimal("0.10")
SCAN_SECONDS = 300

state = {
    "running": False,
    "status": "متوقف",
    "last_signal": None,
}

lock = threading.Lock()


def exchange():
    if not API_KEY or not API_SECRET:
        raise RuntimeError("مفاتيح Binance غير موجودة")

    ex = ccxt.binanceusdm({
        "apiKey": API_KEY,
        "secret": API_SECRET,
        "enableRateLimit": True,
        "options": {"defaultType": "future"},
    })

    # هذه النسخة للفحص التجريبي فقط.
    ex.set_sandbox_mode(True)
    return ex


def login_required(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        if not session.get("admin"):
            return redirect(url_for("login"))
        return func(*args, **kwargs)
    return wrapper


def ema(values, period):
    alpha = Decimal(2) / Decimal(period + 1)
    result = Decimal(str(values[0]))

    for value in values[1:]:
        price = Decimal(str(value))
        result = alpha * price + (1 - alpha) * result

    return result


def trading_signal(ex, symbol):
    candles = ex.fetch_ohlcv(
        symbol, timeframe="1h", limit=120
    )

    # تجاهل الشمعة التي لم تغلق بعد
    candles = candles[:-1]

    if len(candles) < 60:
        return None

    closes = [c[4] for c in candles]

    fast_previous = ema(closes[:-1], 20)
    slow_previous = ema(closes[:-1], 50)

    fast_current = ema(closes, 20)
    slow_current = ema(closes, 50)

    if fast_previous <= slow_previous:
        if fast_current > slow_current:
            return "buy"

    if fast_previous >= slow_previous:
        if fast_current < slow_current:
            return "sell"

    return None


def risk_check(ex, symbol):
    balance = ex.fetch_balance()
    info = balance.get("info", {})

    equity = Decimal(
        str(info.get("totalMarginBalance", "0"))
    )

    if equity <= 0:
        raise RuntimeError("تعذر تحديد رأس المال")

    positions = ex.fetch_positions()

    exposure = sum(
        (
            abs(Decimal(str(p.get("notional") or 0)))
            for p in positions
        ),
        Decimal("0")
    )

    available = equity * MAX_EXPOSURE - exposure

    if available <= 0:
        return None

    # لا تفتح صفقة إذا توجد مراكز أو أوامر
    # تحتاج إلى مراجعة أو تسوية.
    if any(
        Decimal(str(p.get("contracts") or 0)) != 0
        for p in positions
    ):
        return None

    if ex.fetch_open_orders():
        return None

    ticker = ex.fetch_ticker(symbol)
    price = Decimal(str(ticker["last"]))

    if price <= 0:
        return None

    # حد تعرض اسمي، وليس هامش الرافعة
    notional = min(
        available,
        equity * Decimal("0.02")
    )

    quantity = Decimal(
        str(ex.amount_to_precision(
            symbol, float(notional / price)
        ))
    )

    if quantity <= 0:
        return None

    market = ex.market(symbol)
    filters = {
        f["filterType"]: f
        for f in market.get("info", {}).get(
            "filters", []
        )
    }

    min_filter = filters.get("MIN_NOTIONAL", {})
    minimum = Decimal(str(
        min_filter.get(
            "notional",
            min_filter.get("minNotional", "0")
        )
    ))

    if quantity * price < minimum:
        return None

    for filter_name in ("LOT_SIZE", "MARKET_LOT_SIZE"):
        rule = filters.get(filter_name, {})
        min_qty = Decimal(
            str(rule.get("minQty", "0"))
        )

        if quantity < min_qty:
            return None

    return {
        "symbol": symbol,
        "quantity": str(quantity),
        "notional": str(quantity * price),
        "equity": str(equity),
    }


def scan_once():
    ex = exchange()
    ex.load_markets()

    results = []

    for symbol in SYMBOLS:
        if not state["running"]:
            break

        try:
            signal = trading_signal(ex, symbol)

            if signal is None:
                continue

            plan = risk_check(ex, symbol)

            if plan is None:
                continue

            results.append({
                "side": signal,
                **plan,
            })

        except Exception:
            app.logger.exception(
                "فشل فحص %s", symbol
            )

    return results


def worker():
    while True:
        with lock:
            running = state["running"]

        if not running:
            return

        try:
            signals = scan_once()

            with lock:
                state["last_signal"] = signals
                state["status"] = (
                    "مراقبة تجريبية تعمل"
                )

        except Exception:
            app.logger.exception("فشل الفحص")

            with lock:
                state["status"] = "خطأ في الفحص"

        time.sleep(SCAN_SECONDS)


PAGE = """
<!doctype html>
<html lang="ar" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport"
      content="width=device-width,initial-scale=1">
<title>البوت الكويتي</title>
<style>
body {
    background:#101827;
    color:white;
    font-family:Arial;
    padding:24px;
}
main {
    max-width:600px;
    margin:auto;
}
button,input {
    padding:13px;
    margin:8px 0;
    width:100%;
    box-sizing:border-box;
}
button {
    background:#16a34a;
    color:white;
    border:0;
    border-radius:8px;
}
.stop {background:#dc2626}
.card {
    background:#1e293b;
    padding:20px;
    border-radius:12px;
}
pre {white-space:pre-wrap}
</style>
</head>
<body>
<main>
<div class="card">
<h1>البوت الكويتي 🇰🇼</h1>
<p>{{ status }}</p>
<p>الوضع: مراقبة تجريبية فقط</p>
{% if admin %}
<form action="/start" method="post">
<button>بدء المراقبة</button>
</form>
<form action="/stop" method="post">
<button class="stop">إيقاف المراقبة</button>
</form>
<a href="/check">فحص اتصال Binance</a>
<p>آخر إشارات:</p>
<pre>{{ signals }}</pre>
<a href="/logout">تسجيل الخروج</a>
{% else %}
<a href="/login">دخول المدير</a>
{% endif %}
</div>
</main>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(
        PAGE,
        status=state["status"],
        signals=state["last_signal"],
        admin=session.get("admin"),
    )


@app.route("/login", methods=["GET", "POST"])
def login():
    error = ""

    if request.method == "POST":
        email = request.form.get("email", "")
        password = request.form.get("password", "")

        if (
            email == ADMIN_EMAIL
            and check_password_hash(
                ADMIN_PASSWORD_HASH, password
            )
        ):
            session.clear()
            session["admin"] = True
            return redirect("/")

        error = "بيانات الدخول غير صحيحة"

    return render_template_string("""
    <html lang="ar" dir="rtl">
    <meta name="viewport"
          content="width=device-width,initial-scale=1">
    <h2>دخول المدير</h2>
    <p>{{ error }}</p>
    <form method="post">
      <input name="email" placeholder="البريد">
      <input name="password"
             type="password"
             placeholder="كلمة المرور">
      <button>دخول</button>
    </form>
    </html>
    """, error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


@app.route("/check")
@login_required
def check():
    try:
        ex = exchange()
        ex.load_markets()
        balance = ex.fetch_balance()

        return {
            "connected": True,
            "mode": "sandbox",
            "balance_available": bool(balance),
        }

    except Exception as exc:
        app.logger.exception("Binance connection")
        return {
            "connected": False,
            "error_type": type(exc).__name__,
        }, 503


@app.route("/start", methods=["POST"])
@login_required
def start():
    with lock:
        if state["running"]:
            return redirect("/")

        state["running"] = True
        state["status"] = "بدأت المراقبة"

    threading.Thread(
        target=worker,
        daemon=True
    ).start()

    return redirect("/")


@app.route("/stop", methods=["POST"])
@login_required
def stop():
    with lock:
        state["running"] = False
        state["status"] = "متوقف"

    return redirect("/")


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", "10000"))
    )
