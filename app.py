import os, time, threading, pytz, secrets
from flask import Flask, request, redirect, url_for, session
import ccxt

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'kuwaiti_bot_secure_key')

ADMIN_EMAIL = "alabod213@gmail.com"
SECURITY_CODE = "96694489"
RAIN_BITCOIN_ADDRESS = 'bc1q5tga3rmaa6c5lfxtcngueewla22zqdfzr7dcw808zxwad895e42sx79pz0'

exchange = ccxt.binance({
    'apiKey': os.environ.get('BINANCE_API_KEY'), 
    'secret': os.environ.get('BINANCE_API_SECRET'), 
    'enableRateLimit': True, 'options': {'defaultType': 'future'}
})

users_db = {}
total_subscription_earnings = 0.0

@app.route('/')
def index():
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email').strip().lower()
        code = request.form.get('code')
        if email == ADMIN_EMAIL and code == SECURITY_CODE:
            session['logged_in'] = True
            session['role'] = 'admin'
            return redirect(url_for('admin_panel'))
        if email in users_db and code == users_db[email]['password']:
            session['logged_in'] = True
            session['role'] = 'user'
            session['user_email'] = email
            return redirect(url_for('user_dashboard'))
        return '<script>alert("البيانات خاطئة!"); window.history.back();</script>'
    
    return '<html lang="ar" dir="rtl"><body style="background:#0c0c0c; color:white; font-family:sans-serif; text-align:center; padding-top:100px;"><div style="background:#141414; width:350px; margin:0 auto; padding:30px; border-radius:15px; border:2px solid #ffcc00; text-align:right;"><h2>🔓 بوابة تسجيل الدخول</h2><form method="POST"><input type="email" name="email" placeholder="البريد الإلكتروني" required style="width:100%; padding:10px; margin-bottom:15px;"><br><input type="password" name="code" placeholder="كلمة السر أو كود الأمان" required style="width:100%; padding:10px; margin-bottom:20px;"><br><button type="submit" style="background:#ffcc00; color:black; width:100%; padding:12px; font-weight:bold; border:none; border-radius:5px; cursor:pointer;">دخول آمن 🔐</button></form><p style="text-align:center;"><a href="/register" style="color:#ffcc00; text-decoration:none;">سجل كمشترك جديد هنا 🚀</a></p></div></body></html>'

@app.route('/register', methods=['GET', 'POST'])
def register():
    global total_subscription_earnings
    if request.method == 'POST':
        username = request.form.get('username')
        email = request.form.get('email').strip().lower()
        password = request.form.get('password')
        plan = request.form.get('plan')
        amount = float(request.form.get('amount', 0))
        
        if email in users_db or email == ADMIN_EMAIL:
            return '<script>alert("مسجل مسبقاً!"); window.history.back();</script>'
            
        users_db[email] = {'username': username, 'password': password, 'plan': plan, 'amount': amount, 'status': 'نشط ومتابع'}
        total_subscription_earnings += 49.99
        session['logged_in'] = True
        session['role'] = 'user'
        session['user_email'] = email
        return redirect(url_for('user_dashboard'))
        
    return '<html lang="ar" dir="rtl"><body style="background:#0d0d0d; color:white; font-family:sans-serif; padding:20px;"><div style="max-width:400px; margin:0 auto; background:#141414; padding:30px; border-radius:15px; border:1px solid #ffcc00; text-align:right;"><h2>🤖 حساب مستثمر جديد</h2><form method="POST"><input type="text" name="username" placeholder="الاسم الكامل" required style="width:100%; padding:10px; margin-bottom:15px;"><br><input type="email" name="email" placeholder="البريد الإلكتروني" required style="width:100%; padding:10px; margin-bottom:15px;"><br><input type="password" name="password" placeholder="اختر كلمة السر" required style="width:100%; padding:10px; margin-bottom:15px;"><br><select name="plan" style="width:100%; padding:10px; margin-bottom:15px;"><option value="weekly">أسبوعي</option><option value="monthly">شهري</option><option value="yearly">سنوي</option></select><br><input type="number" name="amount" placeholder="رأس المال ($)" required style="width:100%; padding:10px; margin-bottom:20px;"><br><button type="submit" style="background:#ffcc00; color:black; width:100%; padding:12px; font-weight:bold; border:none; border-radius:5px; cursor:pointer;">إنشاء الحساب والربط 🚀</button></form></div></body></html>'

@app.route('/admin-panel')
def admin_panel():
    if not session.get('logged_in') or session.get('role') != 'admin': return redirect(url_for('login'))
    try:
        balance = exchange.fetch_balance()
        free_usdt = round(balance['free'].get('USDT', 0), 2)
    except: free_usdt = "خطأ في الاتصال ببايننس"
    
    rows = ""
    for k, v in users_db.items():
        rows += f"<tr style='background:#222;'><td>{v['username']}</td><td>{k}</td><td>{v['plan']}</td><td>${v['amount']}</td><td>{v['status']}</td></tr>"
    if not users_db: rows = "<tr><td colspan='5' style='color:#666; padding:15px;'>لا يوجد مستثمرون حالياً.</td></tr>"
    
    return f'<html lang="ar" dir="rtl"><body style="background:#0c0c0c; color:white; font-family:sans-serif; padding:20px;"><div style="max-width:800px; margin:0 auto; background:#141414; padding:25px; border-radius:15px; border:1px solid #ffcc00;"><h2>🇰🇼 لوحة المشرف عبد الله</h2><p>رصيد بايننس الحالي: <b style="color:#00ff00;">${free_usdt} USDT</b></p><p>أرباح الاشتراكات المجمعة: <b>${total_subscription_earnings} USD</b></p><hr><table width="100%" border="1" cellpadding="8" style="text-align:center; border-collapse:collapse;"><thead><tr style="color:#ffcc00;"><th>الاسم</th><th>البريد</th><th>الباقة</th><th>رأس المال</th><th>الحالة</th></tr></thead><tbody>{rows}</tbody></table><br><a href="/logout" style="color:red;">خروج</a></div></body></html>'

@app.route('/user-dashboard')
def user_dashboard():
    if not session.get('logged_in') or session.get('role') != 'user': return redirect(url_for('login'))
    u = users_db.get(session.get('user_email'), {})
    return f'<html lang="ar" dir="rtl"><body style="background:#0c0c0c; color:white; font-family:sans-serif; padding:40px; text-align:center;"><div style="max-width:400px; margin:0 auto; background:#141414; padding:30px; border-radius:12px; border:1px solid #ffcc00; text-align:right;"><h2>📊 لوحة المستثمر</h2><p>مرحباً: <b>{u.get("username")}</b></p><p>رأس مالك المتابع بأمان: <b style="color:#00ff00;">${u.get("amount")} USDT</b></p><p>الحالة: <b style="color:#00bfff;">{u.get("status")}</b></p><hr><a href="/logout" style="color:red;">خروج</a></div></body></html>'

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
