import os, time, threading, secrets
from flask import Flask, request, redirect, url_for, session
import ccxt

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', secrets.token_hex(16))

ADMIN_EMAIL = "alabod213@gmail.com"
SECURITY_CODE = "96694489"
RAIN_BITCOIN_ADDRESS = 'bc1q5tga3rmaa6c5lfxtcngueewla22zqdfzr7dcw808zxwad895e42sx79pz0'
WATCHLIST = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'BNB/USDT', 'XRP/USDT']

exchange = ccxt.binance({
    'apiKey': os.environ.get('BINANCE_API_KEY'), 
    'secret': os.environ.get('BINANCE_API_SECRET'), 
    'enableRateLimit': True, 'options': {'defaultType': 'future'}
})

users_db = {}
total_subscription_earnings = 0.0

def get_light_market_widget():
    html = '<div style="background:#1e1e1e; padding:15px; border-radius:10px; margin-top:15px; border:1px solid #ffcc00; text-align:center;"><h4 style="color:#ffcc00; margin:0 0 10px 0;">📊 حركة الأسواق الحية الحالية</h4><div style="display:flex; justify-content:space-around; flex-wrap:wrap; gap:10px;">'
    try:
        for symbol in WATCHLIST:
            ticker = exchange.fetch_ticker(symbol)
            price = round(ticker['last'], 2)
            change = round(ticker['percentage'], 2) if ticker['percentage'] is not None else 0.0
            color = '#00ff00' if change >= 0 else '#ff3333'
            html += f'<div style="background:#111; padding:8px; border-radius:5px; font-size:12px;"><b>{symbol.replace("/USDT","")}</b><br><span style="color:#00ff00;">${price}</span><br><span style="color:{color}; font-weight:bold;">{change}%</span></div>'
    except:
        html += '<p style="color:#aaa; font-size:12px; margin:0;">🔄 جاري تحديث الأسعار الفورية من بايننس...</p>'
    html += '</div></div>'
    return html

@app.route('/')
def index():
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email').strip().lower()
        code = request.form.get('code')
        if email == ADMIN_EMAIL and code == SECURITY_CODE:
            session['logged_in'] = True; session['role'] = 'admin'
            return redirect(url_for('admin_panel'))
        if email in users_db and code == users_db[email]['password']:
            session['logged_in'] = True; session['role'] = 'user'; session['user_email'] = email
            return redirect(url_for('payment_page'))
        return '<script>alert("البيانات غير صحيحة!"); window.history.back();</script>'
    return f'<html lang="ar" dir="rtl"><body style="background:#0c0c0c; color:white; font-family:sans-serif; text-align:center; padding-top:60px;"><div style="background:#141414; max-width:400px; margin:0 auto; padding:30px; border-radius:15px; border:2px solid #ffcc00; text-align:right;"><h2>🔓 بوابة تسجيل الدخول</h2><form method="POST"><input type="email" name="email" placeholder="البريد الإلكتروني" required style="width:100%; padding:10px; margin-bottom:15px;"><br><input type="password" name="code" placeholder="كلمة السر أو كود الأمان" required style="width:100%; padding:10px; margin-bottom:20px;"><br><button type="submit" style="background:#ffcc00; color:black; width:100%; padding:12px; font-weight:bold; border:none; border-radius:5px; cursor:pointer; width:100%;">دخول آمن 🔐</button></form><p style="text-align:center; font-size:13px; margin-top:15px;"><a href="/register" style="color:#ffcc00; text-decoration:none;">سجل كمشترك جديد هنا 🚀</a> | <a href="/forgot-password" style="color:#aaa; text-decoration:none;">نسيت كلمة المرور؟ 🔍</a></p>{get_light_market_widget()}</div></body></html>'
@app.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    if request.method == 'POST':
        email = request.form.get('email').strip().lower()
        if email == ADMIN_EMAIL:
            return f'<script>alert("أهلاً بالمشرف عبد الله، كود أمانك الثابت هو: {SECURITY_CODE}"); window.location.href="/login";</script>'
        if email in users_db:
            user_pass = users_db[email]['password']
            return f'<html lang="ar" dir="rtl"><body style="background:#0c0c0c; color:white; font-family:sans-serif; text-align:center; padding-top:100px;"><div style="background:#141414; width:350px; margin:0 auto; padding:30px; border-radius:15px; border:1px solid #00bfff; text-align:right;"><h3>🔑 استعادة الحساب</h3><p>كلمة المرور الخاصة بك هي: <b style="color:#00ff00; font-size:18px;">{user_pass}</b></p><br><a href="/login" style="background:#00bfff; color:black; padding:10px; text-decoration:none; font-weight:bold; border-radius:5px; display:block; text-align:center;">تسجيل الدخول 🔓</a></div></body></html>'
        return '<script>alert("هذا البريد غير مسجل!"); window.history.back();</script>'
    return '<html lang="ar" dir="rtl"><body style="background:#0c0c0c; color:white; font-family:sans-serif; text-align:center; padding-top:120px;"><div style="background:#141414; width:350px; margin:0 auto; padding:30px; border-radius:15px; border:1px solid #ffcc00; text-align:right;"><h2>🔍 استعادة كلمة المرور</h2><form method="POST"><input type="email" name="email" placeholder="بريدك الإلكتروني الحقيقي" required style="width:100%; padding:10px; margin-bottom:20px;"><br><button type="submit" style="background:#ffcc00; color:black; width:100%; padding:12px; font-weight:bold; border:none; border-radius:5px; cursor:pointer;">استعادة الحساب 🛡️</button></form></div></body></html>'

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username')
        email = request.form.get('email').strip().lower()
        password = request.form.get('password')
        plan = request.form.get('plan')
        amount = float(request.form.get('amount', 0))
        if email in users_db or email == ADMIN_EMAIL: return '<script>alert("مسجل مسبقاً!"); window.history.back();</script>'
        users_db[email] = {'username': username, 'password': password, 'plan': plan, 'amount': amount, 'status': 'بانتظار دفع الاشتراك ⏳'}
        session['logged_in'] = True; session['role'] = 'user'; session['user_email'] = email
        return redirect(url_for('payment_page'))
    return f'<html lang="ar" dir="rtl"><body style="background:#0d0d0d; color:white; font-family:sans-serif; padding:20px; padding-top:40px;"><div style="max-width:400px; margin:0 auto; background:#141414; padding:30px; border-radius:15px; border:1px solid #ffcc00; text-align:right;"><h2>🤖 حساب مستثمر جديد</h2><form method="POST"><input type="text" name="username" placeholder="الاسم الكامل" required style="width:100%; padding:10px; margin-bottom:15px;"><br><input type="email" name="email" placeholder="البريد الإلكتروني" required style="width:100%; padding:10px; margin-bottom:15px;"><br><input type="password" name="password" placeholder="اختر كلمة السر" required style="width:100%; padding:10px; margin-bottom:15px;"><br><select name="plan" style="width:100%; padding:10px; margin-bottom:15px;"><option value="weekly">أسبوعي ($19.99)</option><option value="monthly">شهري ($49.99)</option><option value="yearly">سنوي ($99.99)</option></select><br><input type="number" name="amount" placeholder="رأس مال محفظتك ($)" required style="width:100%; padding:10px; margin-bottom:20px;"><br><button type="submit" style="background:#ffcc00; color:black; width:100%; padding:12px; font-weight:bold; border:none; border-radius:5px; cursor:pointer;">إنشاء الحساب والدفع 🚀</button></form>{get_light_market_widget()}</div></body></html>'

@app.route('/payment', methods=['GET', 'POST'])
def payment_page():
    if not session.get('logged_in') or session.get('role') != 'user': return redirect(url_for('login'))
    email = session.get('user_email'); u_info = users_db.get(email, {})
    if not u_info: return redirect(url_for('login'))
    if u_info.get('status') == 'نشط ومتابع ومحمي': return redirect(url_for('user_dashboard'))
    prices = {'weekly': '19.99', 'monthly': '49.99', 'yearly': '99.99'}; required_fee = prices.get(u_info.get('plan'), '49.99')
    if request.method == 'POST':
        global total_subscription_earnings; total_subscription_earnings += float(required_fee); u_info['status'] = 'نشط ومتابع ومحمي'
        return '<script>alert("تم التفعيل والربط بنجاح 🚀"); window.location.href="/user-dashboard";</script>'
    return f'<html lang="ar" dir="rtl"><body style="background:#0c0c0c; color:white; font-family:sans-serif; padding-top:60px;"><div style="max-width:400px; margin:0 auto; background:#141414; padding:30px; border-radius:15px; border:2px solid #ffcc00; text-align:right;"><h2>💳 بوابة تحويل رسوم الاشتراك</h2><p style="color:#00ff00;">المبلغ المطلوب: {required_fee} USD بالبيتكوين.</p><div style="background:#222; padding:12px; border-radius:5px; text-align:center; font-family:monospace; color:#ffcc00; word-break:break-all;">{RAIN_BITCOIN_ADDRESS}</div><br><form method="POST"><button type="submit" style="background:#00ff00; color:black; width:100%; padding:14px; font-weight:bold; border:none; border-radius:5px; cursor:pointer;">لقد قمت بالتحويل، فعّل حسابي الآن 🚀</button></form></div></body></html>'

@app.route('/admin-panel')
def admin_panel():
    if not session.get('logged_in') or session.get('role') != 'admin': return redirect(url_for('login'))
    try:
        balance = exchange.fetch_balance()
        free_usdt = round(balance['free'].get('USDT', 0), 2)
    except: free_usdt = "0.00 (حساب تجريبي / محفظة بايننس فارغة)"
    rows = ""
    for k, v in users_db.items():
        rows += f"<tr style='background:#222;'><td>{v['username']}</td><td>{k}</td><td>{v['plan']}</td><td>${v['amount']}</td><td>{v['status']}</td></tr>"
    if not users_db: rows = "<tr><td colspan='5' style='color:#666; padding:15px;'>لا يوجد مستثمرون حالياً.</td></tr>"
    return f'<html lang="ar" dir="rtl"><body style="background:#0c0c0c; color:white; font-family:sans-serif; padding:20px;"><div style="max-width:800px; margin:0 auto; background:#141414; padding:25px; border-radius:15px; border:1px solid #ffcc00;"><h2>👑 لوحة الإشراف العليا - المشرف عبد الله</h2><p>رصيدك الحالي في بايننس: <b style="color:#00ff00;">${free_usdt} USDT</b></p><p>أرباح الاشتراكات الكلية لـ Rain: <b style="color:#ffcc00;">{total_subscription_earnings:.2f} USD</b></p><hr><h3>👥 تتبع المستثمرين الجدد:</h3><table width="100%" border="1" cellpadding="8" style="text-align:center; border-collapse:collapse;"><thead style="background:#333; color:#ffcc00;"><th>الاسم</th><th>البريد</th><th>الباقة</th><th>رأس المال</th><th>الحالة والترخيص</th></thead><tbody>{rows}</tbody></table>{get_light_market_widget()}<br><div style="text-align:center;"><a href="/logout" style="color:red; text-decoration:none; font-weight:bold;">تسجيل الخروج الآمن ↩️</a></div></div></body></html>'

@app.route('/user-dashboard')
def user_dashboard():
    if not session.get('logged_in') or session.get('role') != 'user': return redirect(url_for('login'))
    u = users_db.get(session.get('user_email'), {})
    if not u or u.get('status') != 'نشط ومتابع ومحمي': return redirect(url_for('payment_page'))
    return f'<html lang="ar" dir="rtl"><body style="background:#0c0c0c; color:white; font-family:sans-serif; padding:20px; text-align:center;"><div style="max-width:450px; margin:0 auto; background:#141414; padding:30px; border-radius:12px; border:1px solid #ffcc00; text-align:right;"><h2>📊 لوحة المستثمر النشطة</h2><p>مرحباً: <b>{u.get("username")}</b></p><p>رأس مالك المتابع بالتداول: <b style="color:#00ff00;">${u.get("amount")} USDT</b></p><p>الحالة: <b style="color:#00bfff;">{u.get("status")}</b></p>{get_light_market_widget()}<hr><a href="/logout" style="color:red; text-decoration:none; font-weight:bold;">تسجيل الخروج</a></div></body></html>'

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

def auto_trading_engine():
    while True:
        try:
            balance = exchange.fetch_balance()
            free = balance['free'].get('USDT', 0)
            if free > 20:
                ticker = exchange.fetch_ticker('BTC/USDT')
                amt = (free * 0.05) / ticker['last']
                amt_fmt = exchange.amount_to_precision('BTC/USDT', amt)
                if float(amt_fmt) > 0: exchange.create_market_order('BTC/USDT', 'buy', amt_fmt)
        except: pass
        time.sleep(20)

threading.Thread(target=auto_trading_engine, daemon=True).start()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
