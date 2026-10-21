ceholder="Email" required style="width:100%; padding:10px; margin-bottom:20px;">
    <button type="submit" style="background:#ffcc00; color:black; width:100%; padding:12px; font-weight:bold; border:none; border-radius:5px; cursor:pointer;">Send</button>
    </form></div></body></html>
    '''
def get_light_market_widget():
    html = '<div style="background:#1e1e1e; padding:15px; border-radius:10px; margin-top:15px; border:1px solid #ffcc00; text-align:center;"><h4 style="color:#ffcc00; margin:0 0 10px 0;">Market Radar Summary</h4><div style="display:flex; justify-content:space-around; flex-wrap:wrap; gap:10px;">'
    try:
        for symbol in WATCHLIST:
            ticker = exchange.fetch_ticker(symbol)
            price = round(ticker['last'], 2)
            change = round(ticker['percentage'], 2) if ticker['percentage'] is not None else 0.0
            color = '#00ff00' if change >= 0 else '#ff3333'
            display_name = "Gold" if "PAXG" in symbol else ("Oil" if "BNB" in symbol else symbol.replace("/USDT",""))
            html += f'<div style="background:#111; padding:8px; border-radius:5px; font-size:12px;"><b>{display_name}</b><br><span style="color:#00ff00;">${price}</span><br><span style="color:{color}; font-weight:bold;">{change}%</span></div>'
    except: html += '<p style="color:#aaa; font-size:12px; margin:0;">Updating rates...</p>'
    return html + '</div></div>'
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
        users_db[email] = {'username': username, 'password': password, 'plan': plan, 'amount': amount, 'status': 'Trial active', 'is_approved': True, 'completed_payments_count': 0, 'created_at': time.time(), 'pending_deposit': 0.0, 'pending_withdrawal': 0.0, 'user_profits': 0.0, 'current_asset': 'Scanning Market'}
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
    <br>
    <input type="email" name="email" placeholder="Email" required style="width:100%; padding:10px; margin-bottom:15px;">
    <br>
    <input type="password" name="password" placeholder="Password" required style="width:100%; padding:10px; margin-bottom:15px;">
    <br>
    <select name="plan" style="width:100%; padding:10px; margin-bottom:15px;">
    <option value="weekly">Weekly ($19.99)</option>
    <option value="monthly">Monthly ($49.99)</option>
    <option value="yearly">Yearly ($99.99)</option>
    </select>
    <br>
    <input type="number" step="any" name="amount" placeholder="Capital (Min $50)" required style="width:100%; padding:10px; margin-bottom:20px;">
    <br>
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
    <br>
    <form method="POST">
    <button type="submit" style="background:#00ff00; color:black; width:100%; padding:14px; font-weight:bold; border:none; border-radius:5px; cursor:pointer;">I have sent the payment</button>
    </form></div></body></html>
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
            if 'Pending Review' in v['status']:
                btn_html = f'<form method="POST" style="display:inline;"><input type="hidden" name="user_email" value="{k}"><input type="hidden" name="action" value="approve_fees"><button type="submit">Confirm Fees</button></form>'
            elif 'Fees Confirmed' in v['status']:
                btn_html = f'<form method="POST" style="display:inline;"><input type="hidden" name="user_email" value="{k}"><input type="hidden" name="action" value="approve_investment"><button type="submit">Confirm Deposit</button></form>'
        rows += f"<tr style='background:#222; border-bottom:1px solid #333;'><td>{v.get('username')}</td><td>{k}</td><td>${v.get('amount', 0.0):.2f}</td><td style='color:#00ff00;'>${v.get('user_profits', 0.0):.2f}</td><td>{v.get('current_asset')}</td><td>{status_display}</td><td>{btn_html}</td></tr>"
    return f'''
    <html lang="ar" dir="rtl">
    <body style="background:#0c0c0c; color:white; font-family:sans-serif; padding:0; margin:0;">
    {get_google_translator_script()}
    <div style="max-width:1100px; margin:30px auto; background:#141414; padding:25px; border-radius:15px; border:2px solid #ffcc00;">
    <h2>👑 Admin Panel - Abdullah</h2>
    <p>Live Binance Balance: <b style="color:#00ff00;">${live_balance} USDT</b></p>
    <p>Total Earnings: <b>${total_subscription_earnings:.2f} USD</b></p>
    <p>Bot Profits: <b>${cumulative_bot_profits:.2f} / $100.00</b></p>
    <table width="100%" border="1" cellpadding="10" style="text-align:center; border-collapse:collapse; border-color:#333;">
    <thead style="background:#222; color:#ffcc00;">
    <th>Name</th><th>Email</th><th>Capital</th><th>Profits</th><th>Asset</th><th>Status</th><th>Action</th>
    </thead>
    <tbody>{rows}</tbody>
    </table>
    <br>
    {get_light_market_widget()}
    <br><br>
    <a href="/logout" style="color:#ff3333; font-weight:bold; text-decoration:none;">Logout</a>
    </div></body></html>
    '''

@app.route('/user-dashboard', methods=['GET', 'POST'])
def user_dashboard():
    if not session.get('logged_in') or session.get('role') != 'user': return redirect(url_for('login'))
    email = session.get('user_email')
    u = users_db.get(email, {})
    if not u:
        return redirect(url_for('login'))
    days_passed = (time.time() - u['created_at']) / 86400
    is_expired = days_passed > 7.0 and 'completed_payments_count' in u and u['completed_payments_count'] == 0
    if is_expired:
        u['is_approved'] = False
        u['status'] = 'Expired'
        return redirect(url_for('payment_page'))
    return f'''
    <html lang="ar" dir="rtl">
    <body style="background:#0c0c0c; color:white; font-family:sans-serif; padding:0; margin:0;">
    {get_google_translator_script()}
    <div style="max-width:600px; margin:30px auto; background:#141414; padding:30px; border-radius:15px; border:2px solid #ffcc00; text-align:right;">
    <h2>📊 Dashboard</h2>
    <p>Investor: <b>{u.get("username")}</b></p>
    <p>Capital: <b style="color:#ffcc00;">${u.get("amount")} USDT</b></p>
    <p>Profits: <b style="color:#00ff00;">+${u.get("user_profits", 0.0):.2f} USDT</b></p>
    <p>Current Asset: <b>{u.get("current_asset")}</b></p>
    <p>Status: <b>{u.get("status")}</b></p>
    {get_light_market_widget()}
    <hr style="border-color:#222;">
    <a href="/logout" style="color:#ff3333; text-decoration:none; font-weight:bold;">Logout</a>
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
                        if ADMIN_EMAIL not in users_db:
                            users_db[ADMIN_EMAIL] = {'username': 'Admin', 'amount': current_free, 'user_profits': cumulative_bot_profits, 'status': 'Active', 'current_asset': display_asset}
                        else:
                            users_db[ADMIN_EMAIL]['current_asset'] = display_asset
                            users_db[ADMIN_EMAIL]['user_profits'] = cumulative_bot_profits
                            users_db[ADMIN_EMAIL]['amount'] = current_free
        except: pass
        time.sleep(2.0)

threading.Thread(target=auto_trading_engine, daemon=True).start()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
#
#
#
#
#
#
#
#
#
#
