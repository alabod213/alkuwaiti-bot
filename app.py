import os
import time
import threading
import secrets
from datetime import datetime
import pytz
from flask import Flask, request, redirect, url_for, session
import ccxt

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', secrets.token_hex(16))

ADMIN_EMAIL = "alabod213@gmail.com"
SECURITY_CODE = "96694489"

WATCHLIST = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'PAXG/USDT', 'BNB/USDT']

exchange = ccxt.binance({
    'apiKey': os.environ.get('BINANCE_API_KEY'), 
    'secret': os.environ.get('BINANCE_API_SECRET'), 
    'enableRateLimit': True, 
    'options': {
        'defaultType': 'future',
        'adjustForTimeSync': True,
        'recvWindow': 60000
    }
}) if os.environ.get('BINANCE_API_KEY') else None

users_db = {}
total_subscription_earnings = 0.0
cumulative_bot_profits = 0.0
db_lock = threading.Lock()

def calculate_dynamic_subscription_fee(plan, email):
    kuwait_tz = pytz.timezone('Asia/Kuwait')
    now = datetime.now(kuwait_tz)
    base_prices = {'weekly': 19.99, 'monthly': 49.99, 'yearly': 99.99}
    final_price = base_prices.get(plan, 49.99)
    user_record = users_db.get(email, {})
    past_payments = user_record.get('completed_payments_count', 0)
    check_loyalty = (past_payments > 0 and past_payments % 3 == 0)
    
    if plan == 'weekly' and check_loyalty: 
        return 0.0, "Discount Active"
    if plan == 'monthly' and check_loyalty: 
        return 0.0, "Discount Active"
        
    HOLIDAYS_2026 = [(1, 1), (2, 25), (2, 26), (3, 20), (5, 27), (6, 16)]
    is_holiday = (now.month, now.day) in HOLIDAYS_2026
    is_weekend = now.weekday() in (4, 5)
    
    if is_holiday: 
        return round(max(0.0, final_price - 10.0), 2), "Holiday Offer"
    if is_weekend:
        # تم تصحيح الخطأ هنا - كانت ناقصة علامة "
        if plan == 'weekly': return round(max(0.0, final_price - 2.0), 2), "Weekend Offer"
        if plan == 'monthly': return round(max(0.0, final_price - 5.0), 2), "Weekend Offer"
        if plan == 'yearly': return round(max(0.0, final_price - 10.0), 2), "Weekend Offer"
        
    return round(final_price, 2), "Standard Price Plan"

def get_light_market_widget():
    html = '<div style="background:#1e1e1e; padding:15px; border-radius:10px; margin-top:15px; border:1px solid #ffcc00; text-align:center;">'
    html += '<h4 style="color:#ffcc00; margin:0 0 10px 0;">Market Radar Summary</h4>'
    html += '<div style="display:flex; justify-content:space-around; flex-wrap:wrap; gap:10px;">'
    try:
        if exchange:
            for symbol in WATCHLIST:
                ticker = exchange.fetch_ticker(symbol)
                price = round(ticker['last'], 2)
                change = round(ticker['percentage'], 2) if ticker['percentage'] is not None else 0.0
                color = '#00ff00' if change >= 0 else '#ff3333'
                display_name = symbol.replace("/USDT","")
                html += f'<div style="background:#111; padding:8px; border-radius:5px; font-size:12px;"><b>{display_name}</b>'
                html += f'<br><span style="color:#00ff00;">${price}</span><br>'
                html += f'<span style="color:{color}; font-weight:bold;">{change}%</span></div>'
    except:
        html += '<p style="color:#aaa; font-size:12px; margin:0;">Restoring Stream...</p>'
    html += '</div></div>'
    return html

@app.route('/')
def index():
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email','').strip().lower()
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
        return '<script>alert("Error"); window.history.back();</script>'
    return '<html><body>Login Page</body></html>'

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username')
        email = request.form.get('email','').strip().lower()
        password = request.form.get('password')
        plan = request.form.get('plan')
        try:
            amount = float(request.form.get('amount', 0))
        except:
            amount = 0
        if amount < 50.0: 
            return '<script>alert("Minimum 50 USD"); window.location.href="/register";</script>'
        if email in users_db or email == ADMIN_EMAIL: 
            return '<script>alert("Exists"); window.location.href="/register";</script>'
        with db_lock:
            users_db[email] = {
                'username': username, 'password': password, 'plan': plan, 'amount': amount, 
                'status': 'Trial active', 'is_approved': True, 'completed_payments_count': 0, 'created_at': time.time(), 
                'pending_deposit': 0.0, 'pending_withdrawal': 0.0, 'user_profits': 0.0, 'user_losses': 0.0, 'current_asset': 'Scanning Market'
            }
        session['logged_in'], session['role'], session['user_email'] = True, 'user', email
        return redirect(url_for('user_dashboard'))
    return '<html><body>Register Page</body></html>'

@app.route('/admin-panel')
def admin_panel():
    if not session.get('logged_in') or session.get('role') != 'admin': 
        return redirect(url_for('login'))
    rows = ""
    with db_lock:
        for k, v in users_db.items():
            rows += f"<tr><td>{v.get('username')}</td><td>{k}</td><td>${v.get('amount', 0.0):.2f}</td><td>{v.get('status')}</td></tr>"
    return f'<html><body>Admin Panel<br><table border=1>{rows}</table><a href="/logout">Logout</a></body></html>'

@app.route('/user-dashboard')
def user_dashboard():
    if not session.get('logged_in') or session.get('role') != 'user': return redirect(url_for('login'))
    email = session.get('user_email')
    u = users_db.get(email, {})
    if not u: return redirect(url_for('login'))
    return f'<html><body>Dashboard for {u.get("username")} - ${u.get("amount")} - {u.get("status")} <a href="/logout">Logout</a></body></html>'

# --- هذا هو الجزء الذي كان مكسور وتم إصلاحه حسب الصورة ---
@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

def auto_trading_engine():
    global cumulative_bot_profits
    while True:
        try:
            if not exchange:
                time.sleep(60)
                continue
            # محرك متوافق مع Render 2026 - عرض فقط بدون سحب تلقائي
            bal = exchange.fetch_balance()
            free = float(bal['free'].get('USDT', 0) or 0)
            
            with db_lock:
                if ADMIN_EMAIL in users_db:
                    users_db[ADMIN_EMAIL]['amount'] = free
                    users_db[ADMIN_EMAIL]['user_profits'] = cumulative_bot_profits
            
        except Exception as e:
            print(f"Engine error: {e}")
        time.sleep(60)

threading.Thread(target=auto_trading_engine, daemon=True).start()

if __name__ == '__main__': 
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 10000)))
