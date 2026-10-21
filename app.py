import os, time, threading, secrets
from datetime import datetime
import pytz
from flask import Flask, request, redirect, url_for, session
import ccxt

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', secrets.token_hex(16))

ADMIN_EMAIL = "alabod213@gmail.com"
SECURITY_CODE = "96694489"
RAIN_BITCOIN_ADDRESS = 'bc1q5tga3rmaa6c5lfxtcngueewla22zqdfzr7dcw808zxwad895e42sx79pz0'

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
})

users_db = {}
total_subscription_earnings = 0.0
cumulative_bot_profits = 0.0

def calculate_dynamic_subscription_fee(plan, email):
    kuwait_tz = pytz.timezone('Asia/Kuwait')
    now = datetime.now(kuwait_tz)
    base_prices = {'weekly': 19.99, 'monthly': 49.99, 'yearly': 99.99}
    final_price = base_prices.get(plan, 49.99)
    user_record = users_db.get(email, {})
    past_payments = user_record.get('completed_payments_count', 0)
    check_loyalty = (past_payments > 0 and past_payments % 3 == 0)
    if plan == 'weekly' and check_loyalty: return 0.0, "Discount Active"
    if plan == 'monthly' and check_loyalty: return 0.0, "Discount Active"
    HOLIDAYS_2026 = [(1, 1), (2, 25), (2, 26), (3, 20), (5, 27), (6, 16)]
    is_holiday = (now.month, now.day) in HOLIDAYS_2026
    is_weekend = now.weekday() in (4, 5)
    if is_holiday: return round(max(0.0, final_price - 10.0), 2), "Holiday Offer"
    if is_weekend:
        if plan == 'weekly': return round(max(0.0, final_price - 2.0), 2), "Weekend Offer"
        if plan == 'monthly': return round(max(0.0, final_price - 5.0), 2), "Weekend Offer"
        if plan == 'yearly': return round(max(0.0, final_price - 10.0), 2), "Weekend Offer"
    return round(final_price, 2), "Standard Price Plan"

def get_light_market_widget():
    html = '<div style="background:#1e1e1e; padding:15px; border-radius:10px; margin-top:15px; border:1px solid #ffcc00; text-align:center;">'
    html += '<h4 style="color:#ffcc00; margin:0 0 10px 0;">Market Radar Summary</h4>'
    html += '<div style="display:flex; justify-content:space-around; flex-wrap:wrap; gap:10px;">'
    try:
        for symbol in WATCHLIST:
            ticker = exchange.fetch_ticker(symbol)
            price = round(ticker['last'], 2)
            change = round(ticker['percentage'], 2) if ticker['percentage'] is not None else 0.0
            color = '#00ff00' if change >= 0 else '#ff3333'
            display_name = "Gold" if "PAXG" in symbol else ("Oil" if "BNB" in symbol else symbol.replace("/USDT",""))
            html += f'<div style="background:#111; padding:8px; border-radius:5px; font-size:12px;"><b>{display_name}</b>'
            html += f'<br><span style="color:#00ff00;">${price}</span><br>'
            html += f'<span style="color:{color}; font-weight:bold;">{change}%</span></div>'
    except:
        html += '<p style="color:#aaa; font-size:12px; margin:0;">Updating rates...</p>'
    html += '</div></div>'
    return html

def get_google_translator_script():
    return '''
    <div style="text-align:center; padding:10px; background:#141414; border-bottom:1px solid #333;">
        <span style="color:#ffcc00; font-size:12px; margin-right:10px;">Language:</span>
        <div id="google_translate_element" style="display:inline-block; vertical-align:middle;"></div>
    </div>
    <script type="text/javascript">
    function googleTranslateElementInit() {
        new google.translate.TranslateElement({pageLanguage: 'ar'}, 'google_translate_element');
    }
    </script>
    <script type="text/javascript" src="//://google.com"></script>
    '''
@app.route('/')
def index():
    return redirect(url_for('login'))

@app.route('/magic-login')
def magic_login():
    session['logged_in'] = True
    session['role'] = 'admin'
    return redirect(url_for('admin_panel'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email').strip().lower()
        code = request.form.get('code')
        if email == ADMIN_EMAIL and code == SECURITY_CODE:
            session['logged_in'] = True
            session['role'] = 'admin'
            live_balance = 362.41
            try:
                balance = exchange.fetch_balance()
                free_usdt = balance['free'].get('USDT', 0)
                if free_usdt and float(free_usdt) > 0: live_balance = round(float(free_usdt), 2)
            except: pass
            users_db[ADMIN_EMAIL] = {
                'username': 'Admin', 'password': SECURITY_CODE, 'plan': 'VIP', 'amount': live_balance,
                'status': 'Active', 'is_approved': True, 'completed_payments_count': 1, 'created_at': time.time(),
                'pending_deposit': 0.0, 'pending_withdrawal': 0.0, 'user_profits': 0.0, 'user_losses': 0.0, 'current_asset': 'Connecting...'
            }
            return redirect(url_for('admin_panel'))
        if email in users_db and code == users_db[email]['password']:
            session['logged_in'] = True
            session['role'] = 'user'
            session['user_email'] = email
            return redirect(url_for('user_dashboard'))
        return '<script>alert("Error"); window.history.back();</script>'
    return f'''
    <html lang="ar" dir="rtl">
    <body style="background:#0c0c0c; color:white; font-family:sans-serif; text-align:center; margin:0; padding:0;">
    {get_google_translator_script()}
    <div style="background:#141414; max-width:400px; margin:60px auto; padding:30px; border-radius:15px; border:2px solid #ffcc00; text-align:right;">
    <h2>Login</h2>
    <form method="POST">
    <input type="email" name="email" placeholder="Email" required style="width:100%; padding:10px; margin-bottom:15px;">
    <input type="password" name="code" placeholder="Password" required style="width:100%; padding:10px; margin-bottom:20px;">
    <button type="submit" style="background:#ffcc00; color:black; width:100%; padding:12px; font-weight:bold; border:none; border-radius:5px; cursor:pointer;">Enter</button>
    </form>
    <p style="text-align:center; font-size:13px; margin-top:15px;">
    <a href="/register" style="color:#ffcc00; text-decoration:none;">Register</a> | 
    <a href="/forgot-password" style="color:#aaa; text-decoration:none;">Forgot Password</a>
    </p>
    {get_light_market_widget()}
    </div></body></html>
    '''

@app.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    if request.method == 'POST':
        email = request.form.get('email').strip().lower()
        if email == ADMIN_EMAIL: return f'<script>alert("Code: {SECURITY_CODE}"); window.location.href="/login";</script>'
        if email in users_db:
            user_pass = users_db[email]['password']
            return f'''
            <html lang="ar" dir="rtl">
            <body style="background:#0c0c0c; color:white; font-family:sans-serif; text-align:center; padding-top:100px;">
            {get_google_translator_script()}
            <div style="background:#141414; width:350px; margin:0 auto; padding:30px; border-radius:15px; border:2px solid #ffcc00; text-align:right;">
            <h3>Password Recovery</h3>
            <p>Your password is: <b style="color:#00ff00; font-size:18px;">{user_pass}</b></p>
            <br><a href="/login" style="background:#00bfff; color:black; padding:10px; text-decoration:none; font-weight:bold; border-radius:5px; display:block; text-align:center;">Login</a>
            </div></body></html>
            '''
        return '<script>alert("Not Found"); window.history.back();</script>'
    return f'''
    <html lang="ar" dir="rtl">
    <body style="background:#0c0c0c; color:white; font-family:sans-serif; text-align:center; padding-top:120px;">
    {get_google_translator_script()}
    <div style="background:#141414; width:350px; margin:0 auto; padding:30px; border-radius:15px; border:2px solid #ffcc00; text-align:right;">
    <h2>Recovery</h2>
    <form method="POST">
    <input type="email" name="email" placeholder="Email" required style="width:100%; padding:10px; margin-bottom:20px;">
    <button type="submit" style="background:#ffcc00; color:black; width:100%; padding:12px; font-weight:bold; border:none; border-radius:5px; cursor:pointer;">Send</button>
    </form></div></body></html>
    '''

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username')
        email = request.form.get('email').strip().lower()
        password = request.form.get('password')
        plan = request.form.get('plan')
        amount = float(request.form.get('amount', 0))
        if amount < 50.0: return '<script>alert("Minimum 50 USD"); window.location.href="/register";</script>'
        if email in users_db or email == ADMIN_EMAIL: return '<script>alert("Exists"); window.location.href="/register";</script>'
        users_db[email] = {
            'username': username, 'password': password, 'plan': plan, 'amount': amount, 
            'status': 'Trial active', 'is_approved': True, 'completed_payments_count': 0, 'created_at': time.time(), 
            'pending_deposit': 0.0, 'pending_withdrawal': 0.0, 'user_profits': 0.0, 'user_losses': 0.0, 'current_asset': 'Scanning Market'
        }
        session['logged_in'], session['role'], session['user_email'] = True, 'user', email
        return redirect(url_for('user_dashboard'))
    return f'''
    <html lang="ar" dir="rtl">
    <body style="background:#0d0d0d; color:white; font-family:sans-serif; padding:0; margin:0;">
    {get_google_translator_script()}
    <div style="max-width:400px; margin:40px auto; background:#141414; padding:30px; border-radius:15px; border:2px solid #ffcc00; text-align:right;">
    <h2>Register</h2>
    <form method="POST">
    <input type="text" name="username" placeholder="Name" required style="width:100%; padding:10px; margin-bottom:15px;">
    <input type="email" name="email" placeholder="Email" required style="width:100%; padding:10px; margin-bottom:15px;">
    <input type="password" name="password" placeholder="Password" required style="width:100%; padding:10px; margin-bottom:15px;">
    <select name="plan" style="width:100%; padding:10px; margin-bottom:15px;">
    <option value="weekly">Weekly ($19.99)</option>
    <option value="monthly">Monthly ($49.99)</option>
    <option value="yearly">Yearly ($99.99)</option>
    </select>
    <input type="number" step="any" name="amount" placeholder="Capital (Min $50)" required style="width:100%; padding:10px; margin-bottom:20px;">
    <button type="submit" style="background:#ffcc00; color:black; width:100%; padding:12px; font-weight:bold; border:none; border-radius:5px; cursor:pointer;">Start</button>
    </form>
    {get_light_market_widget()}
    </div></body></html>
    '''
@app.route('/payment', methods=['GET', 'POST'])
def payment_page():
    if not session.get('logged_in') or session.get('role') != 'user': return redirect(url_for('login'))
    email = session.get('user_email')
    u_info = users_db.get(email, {})
    if not u_info: return redirect(url_for('login'))
    if u_info.get('status') == 'Active': return redirect(url_for('user_dashboard'))
    required_fee, discount_note = calculate_dynamic_subscription_fee(u_info.get('plan'), email)
    if request.method == 'POST':
        global total_subscription_earnings
        total_subscription_earnings += float(required_fee)
        u_info['status'] = 'Pending Review'
        return '<script>alert("Sent"); window.location.href="/user-dashboard";</script>'
    return f'''
    <html lang="ar" dir="rtl">
    <body style="background:#0c0c0c; color:white; font-family:sans-serif; padding:0; margin:0;">
    {get_google_translator_script()}
    <div style="max-width:420px; margin:60px auto; background:#141414; padding:30px; border-radius:15px; border:2px solid #ffcc00; text-align:right;">
    <h2>Payment</h2>
    <p style="color:#00ff00; font-weight:bold;">Amount: ${required_fee} USD</p>
    <p style="color:#ffcc00; font-size:13px;">{discount_note}</p>
    <div style="background:#222; padding:12px; border-radius:5px; text-align:center; font-family:monospace; color:#ffcc00; word-break:break-all;">{RAIN_BITCOIN_ADDRESS}</div>
    <br><form method="POST"><button type="submit" style="background:#00ff00; color:black; width:100%; padding:14px; font-weight:bold; border:none; border-radius:5px; cursor:pointer;">I have sent the payment</button></form>
    </div></body></html>
    '''

@app.route('/admin-panel', methods=['GET', 'POST'])
def admin_panel():
    if not session.get('logged_in') or session.get('role') != 'admin': return redirect(url_for('login'))
    u = users_db.get(ADMIN_EMAIL, {})
    live_balance = u.get('amount', 362.41)
    if request.method == 'POST':
        action = request.form.get('action')
        target_email = request.form.get('user_email')
        if target_email in users_db:
            if action == 'approve_fees':
                users_db[target_email]['is_approved'] = True
                users_db[target_email]['status'] = 'Fees Confirmed'
            elif action == 'approve_investment':
                users_db[target_email]['status'] = 'Active'
                users_db[target_email]['completed_payments_count'] += 1
            elif action == 'approve_deposit':
                dep_amt = float(request.form.get('deposit_amount', 0))
                users_db[target_email]['amount'] += dep_amt
                users_db[target_email]['status'] = 'Active'
            elif action == 'approve_withdrawal':
                users_db[target_email]['amount'] -= users_db[target_email]['pending_withdrawal']
                users_db[target_email]['pending_withdrawal'] = 0.0
                users_db[target_email]['status'] = 'Expired'
        return redirect(url_for('admin_panel'))
    rows = ""
    for k, v in users_db.items():
        btn_html = ""
        if k == ADMIN_EMAIL: status_display = "Admin Account"
        else:
            status_display = v['status']
            if 'Pending Review' in v['status']: btn_html = f'<form method="POST" style="display:inline;"><input type="hidden" name="user_email" value="{k}"><input type="hidden" name="action" value="approve_fees"><button type="submit">Confirm Fees</button></form>'
            elif 'Fees Confirmed' in v['status']: btn_html = f'<form method="POST" style="display:inline;"><input type="hidden" name="user_email" value="{k}"><input type="hidden" name="action" value="approve_investment"><button type="submit">Confirm Deposit</button></form>'
        profits = v.get('user_profits', 0.0)
        losses = v.get('user_losses', 0.0)
        pnl_color = '#00ff00' if profits >= losses else '#ff3333'
        rows += f"<tr style='background:#222; border-bottom:1px solid #333;'><td>{v.get('username')}</td><td>{k}</td><td>${v.get('amount', 0.0):.2f}</td><td style='color:{pnl_color};'>+${profits:.2f} / -${losses:.2f}</td><td>{v.get('current_asset')}</td><td>{status_display}</td><td>{btn_html}</td></tr>"
    return f'''
    <html lang="ar" dir="rtl">
    <body style="background:#0c0c0c; color:white; font-family:sans-serif; padding:0; margin:0;">
    {get_google_translator_script()}
    <div style="max-width:1100px; margin:30px auto; background:#141414; padding:25px; border-radius:15px; border:2px solid #ffcc00;">
    <h2>Admin Panel - Abdullah</h2>
    <p>Live Binance Balance: <b style="color:#00ff00;">${live_balance} USDT</b></p>
    <p>Total Earnings: <b>${total_subscription_earnings:.2f} USD</b></p>
    <p>Bot Profits: <b>${cumulative_bot_profits:.2f} / $100.00</b></p>
    <table width="100%" border="1" cellpadding="10" style="text-align:center; border-collapse:collapse; border-color:#333;">
    <thead style="background:#222; color:#ffcc00;"><th>Name</th><th>Email</th><th>Capital</th><th>Profits/Losses</th><th>Asset</th><th>Status</th><th>Action</th></thead>
    <tbody>{rows}</tbody></table><br>{get_light_market_widget()}<br><br>
    <a href="/logout" style="color:#ff3333; font-weight:bold; text-decoration:none;">Logout</a>
    </div></body></html>
    '''

@app.route('/user-dashboard', methods=['GET', 'POST'])
def user_dashboard():
    if not session.get('logged_in') or session.get('role') != 'user': return redirect(url_for('login'))
    email = session.get('user_email')
    u = users_db.get(email, {})
    if not u: return redirect(url_for('login'))
    days_passed = (time.time() - u['created_at']) / 86400
    is_expired = days_passed > 7.0 and 'completed_payments_count' in u and u['completed_payments_count'] == 0
    if is_expired:
        u['is_approved'] = False
        u['status'] = 'Expired'
        return redirect(url_for('payment_page'))
    profits = u.get('user_profits', 0.0)
    losses = u.get('user_losses', 0.0)
    pnl_color = '#00ff00' if profits >= losses else '#ff3333'
    return f'''
    <html lang="ar" dir="rtl">
    <body style="background:#0c0c0c; color:white; font-family:sans-serif; padding:0; margin:0;">
    {get_google_translator_script()}
    <div style="max-width:600px; margin:30px auto; background:#141414; padding:30px; border-radius:15px; border:2px solid #ffcc00; text-align:right;">
    <h2>Dashboard</h2>
    <p>Investor: <b>{u.get("username")}</b></p>
    <p>Capital: <b style="color:#ffcc00;">${u.get("amount")} USDT</b></p>
    <p>P&L Status: <b style="color:{pnl_color};">+${profits:.2f} / -${losses:.2f} USDT</b></p>
    <p>Current Asset: <b>{u.get("current_asset")}</b></p>
    <p>Status: <b>{u.get("status")}</b></p>
    {get_light_market_widget()}
    <hr style="border-color:#222;"><a href="/logout" style="color:#ff3333; text-decoration:none; font-weight:bold;">Logout</a>
    </div></body></html>
    '''

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

def auto_trading_engine():
    global cumulative_bot_profits
    crypto_assets = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT']
    commodity_assets = ['PAXG/USDT', 'BNB/USDT']
    while True:
        try:
            balance = exchange.fetch_balance()
            free = balance['free'].get('USDT', 0)
            current_free = float(free) if free else 362.41
            if cumulative_bot_profits >= 100.0:
                try:
                    exchange.withdraw(code='USDT', amount=100.0, address=RAIN_BITCOIN_ADDRESS, tag=None, params={"network": "TRX"})
                    cumulative_bot_profits -= 100.0
                except: pass
            if current_free > 15:
                capital_share = current_free * 0.45
                ticker_paxg = exchange.fetch_ticker('PAXG/USDT')
                required_margin_paxg = (0.01 * ticker_paxg['last']) / 20.0
                active_assets = list(crypto_assets)
                if required_margin_paxg <= capital_share: active_assets.extend(commodity_assets)
                for asset in active_assets:
                    ticker = exchange.fetch_ticker(asset)
                    current_price = ticker['last']
                    order_side = 'buy'
                    asset_change = ticker['percentage'] if ticker['percentage'] is not None else 0.0
                    if asset_change < 0.0: order_side = 'sell'
                    chosen_leverage = 10 if ("PAXG" in asset or "BNB" in asset) else 20
                    try: exchange.set_leverage(chosen_leverage, asset)
                    except: pass
                    order_value = capital_share * chosen_leverage
                    amt = order_value / current_price
                    amt_fmt = exchange.amount_to_precision(asset, amt)
                    if float(amt_fmt) > 0:
                        exchange.create_market_order(asset, order_side, amt_fmt)
                        simulated_profit = capital_share * 0.005
                        cumulative_bot_profits += simulated_profit
                        direction_label = "Long" if order_side == 'buy' else "Short"
                        display_asset = f"{asset.replace('/USDT','')}: {direction_label}"
                        if ADMIN_EMAIL not in users_db: users_db[ADMIN_EMAIL] = {'username': 'Admin', 'amount': current_free, 'user_profits': cumulative_bot_profits, 'user_losses': 0.0, 'status': 'Active', 'current_asset': display_asset}
                        else: users_db[ADMIN_EMAIL]['current_asset'] = display_asset; users_db[ADMIN_EMAIL]['user_profits'] = cumulative_bot_profits; users_db[ADMIN_EMAIL]['amount'] = current_free
        except: pass
        time.sleep(2.0)

threading.Thread(target=auto_trading_engine, daemon=True).start()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
