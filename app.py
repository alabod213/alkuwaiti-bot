@app.route('/payment', methods=['GET', 'POST'])
def payment_page():
    if not session.get('logged_in') or session.get('role') != 'user': return redirect(url_for('login'))
    email = session.get('user_email'); u_info = users_db.get(email, {})
    if not u_info: return redirect(url_for('login'))
    if u_info.get('status') == 'نشط ومتابع ومحمي بالكامل 🛡️': return redirect(url_for('user_dashboard'))
    
    required_fee, discount_note = calculate_dynamic_subscription_fee(u_info.get('plan'), email)
    
    if request.method == 'POST':
        global total_subscription_earnings
        total_subscription_earnings += float(required_fee)
        u_info['status'] = 'تم تحويل رسوم Rain - بانتظار مراجعة وتفعيل المشرف عبد الله ⏳'
        return '<script>alert("تم إرسال إشعار التحويل للمشرف! سيفتح حسابك فور تأكيد الدفع."); window.location.href="/user-dashboard";</script>'
        
    return f'<html lang="ar" dir="rtl"><body style="background:#0c0c0c; color:white; font-family:sans-serif; padding-top:60px;"><div style="max-width:420px; margin:0 auto; background:#141414; padding:30px; border-radius:15px; border:2px solid #ffcc00; text-align:right;"><h2>💳 بوابة تحويل رسوم الاشتراك (محفظة Rain)</h2><p style="color:#00ff00; font-weight:bold;">المبلغ المطلوب: ${required_fee} USD</p><p style="color:#ffcc00; font-size:13px;">📌 {discount_note}</p><div style="background:#222; padding:12px; border-radius:5px; text-align:center; font-family:monospace; color:#ffcc00; word-break:break-all;">{RAIN_BITCOIN_ADDRESS}</div><br><form method="POST"><button type="submit" style="background:#00ff00; color:black; width:100%; padding:14px; font-weight:bold; border:none; border-radius:5px; cursor:pointer;">لقد قمت بالتحويل، أرسل الطلب للمشرف 🚀</button></form></div></body></html>'

@app.route('/admin-panel', methods=['GET', 'POST'])
def admin_panel():
    if not session.get('logged_in') or session.get('role') != 'admin': return redirect(url_for('login'))
    if request.method == 'POST':
        action = request.form.get('action'); target_email = request.form.get('user_email')
        if target_email in users_db:
            if action == 'approve_fees':
                users_db[target_email]['is_approved'] = True
                users_db[target_email]['status'] = 'تم تأكيد رسوم رين - بانتظار إيداع رأس مال بايننس 💰'
            elif action == 'approve_investment':
                users_db[target_email]['status'] = 'نشط ومتابع ومحمي بالكامل 🛡️'
                users_db[target_email]['completed_payments_count'] += 1
        return redirect(url_for('admin_panel'))
    try:
        balance = exchange.fetch_balance()
        free_usdt = round(balance['free'].get('USDT', 0), 2)
    except: free_usdt = "0.00 (محفظة الفيوتشرز فارغة)"
    rows = ""
    for k, v in users_db.items():
        btn_html = ""
        if 'بانتظار دفع الاشتراك' in v['status'] or 'تم تحويل رسوم Rain' in v['status']:
            btn_html = f'<form method="POST" style="display:inline;"><input type="hidden" name="user_email" value="{k}"><input type="hidden" name="action" value="approve_fees"><button type="submit" style="background:#00ff00; color:black; padding:5px 10px; font-weight:bold; border:none; border-radius:4px; cursor:pointer;">✓ تأكيد رسوم Rain</button></form>'
        elif 'بانتظار إيداع رأس مال بايننس' in v['status'] or 'تم إرسال رأس المال' in v['status']:
            btn_html = f'<form method="POST" style="display:inline;"><input type="hidden" name="user_email" value="{k}"><input type="hidden" name="action" value="approve_investment"><button type="submit" style="background:#00bfff; color:black; padding:5px 10px; font-weight:bold; border:none; border-radius:4px; cursor:pointer;">✓ تأكيد إيداع بايننس</button></form>'
        rows += f"<tr style='background:#222; border-bottom:1px solid #333;'><td>{v['username']}</td><td>{k}</td><td>{v['plan']}</td><td>${v['amount']}</td><td style='color:#ffcc00;'>{v['status']}</td><td>{btn_html}</td></tr>"
    if not users_db: rows = "<tr><td colspan='6' style='color:#666; padding:15px;'>لا يوجد مستثمرون حالياً.</td></tr>"
    return f'<html lang="ar" dir="rtl"><body style="background:#0c0c0c; color:white; font-family:sans-serif; padding:20px;"><div style="max-width:950px; margin:0 auto; background:#141414; padding:25px; border-radius:15px; border:2px solid #ffcc00;"><h2>👑 لوحة الإشراف العليا - المشرف الغالي عبد الله</h2><p>رصيدك في بايننس: <b style="color:#00ff00;">${free_usdt} USDT</b></p><p>أرباح رسوم رين: <b style="color:#ffcc00;">${total_subscription_earnings:.2f} USD</b></p><hr style="border-color:#333;"><h3>👥 إدارة وتفعيل المستثمرين:</h3><table width="100%" border="1" cellpadding="10" style="text-align:center; border-collapse:collapse; border-color:#333;"><thead style="background:#222; color:#ffcc00;"><th>الاسم</th><th>البريد</th><th>الباقة</th><th>رأس المال</th><th>الحالة والترخيص</th><th>الإجراء المالي</th></thead><tbody>{rows}</tbody></table><br><a href="/logout" style="color:#ff3333; font-weight:bold; text-decoration:none;">↩️ خروج آمن</a></div></body></html>'

@app.route('/user-dashboard', methods=['GET', 'POST'])
def user_dashboard():
    if not session.get('logged_in') or session.get('role') != 'user': return redirect(url_for('login'))
    email = session.get('user_email'); u = users_db.get(email, {})
    if not u: return redirect(url_for('login'))
    
    days_passed = (time.time() - u['created_at']) / 86400
    if days_passed > 7.0 and '7 أيام مجانية' in u['status']:
        u['is_approved'] = False
        u['status'] = 'انتهت الفترة التجريبية (7 أيام) - بانتظار دفع الاشتراك ⏳'
        return redirect(url_for('payment_page'))
        
    if u['is_approved'] and 'بانتظار إيداع رأس مال بايننس' in u['status']:
        if request.method == 'POST':
            coin = request.form.get('coin_type', 'USDT')
            u['status'] = f'تم إرسال رأس المال بـ ({coin}) - قيد تفعيل التداول التلقائي من المشرف ⏳'
            return '<script>alert("تم إرسال الطلب لعبد الله!"); window.location.href="/user-dashboard";</script>'
        return f'<html lang="ar" dir="rtl"><body style="background:#0c0c0c; color:white; font-family:sans-serif; padding-top:40px;"><div style="max-width:480px; margin:0 auto; background:#141414; padding:35px; border-radius:15px; border:2px solid #00bfff; text-align:right;"><h2>💰 خطوتك الأخيرة: إيداع رأس المال الاستثماري في بايننس</h2><p style="color:#aaa;">اختر العملة التي ترغب بتحويل رأس مال تداولك بها إلى حساب بايننس العام:</p><form method="POST"><select name="coin_type" style="width:100%; padding:12px; background:#222; color:white; border:1px solid #444; border-radius:5px; margin-bottom:20px; font-weight:bold;"><option value="USDT">دولار رقمي (USDT)</option><option value="BTC">بيتكوين (BTC)</option><option value="ETH">إيثيريوم (ETH)</option></select><p style="color:#ffcc00; font-size:14px;">📌 يرجى مراجعة عبد الله للحصول على عنوان الإيداع والشبكة المدعومة فور تحويل المبلغ.</p><button type="submit" style="background:#00bfff; color:black; width:100%; padding:14px; font-weight:bold; border:none; border-radius:5px; cursor:pointer;">لقد قمت بتحويل رأس المال، ابدأ التداول 🚀</button></form></div></body></html>'

    if not u['is_approved']: return redirect(url_for('payment_page'))

    return f'<html lang="ar" dir="rtl"><body style="background:#0c0c0c; color:white; font-family:sans-serif; padding:20px; text-align:center;"><div style="max-width:550px; margin:0 auto; background:#141414; padding:30px; border-radius:15px; border:1px solid #ffcc00; text-align:right;"><h2>📊 لوحة المستثمر المباشرة - الصندوق السيادي لعام 2026</h2><p>مرحباً بك: <b style="color:#ffcc00;">{u.get("username")}</b></p><p>رأس المال المربوط بالتداول: <b style="color:#00ff00;">${u.get("amount")} USDT</b></p><p>حالة التداول وجهازك: <b style="color:#00bfff;">{u.get("status")}</b></p><div style="background:#1e1e1e; padding:15px; border-radius:10px; margin-top:20px; border:1px solid #ffcc00; text-align:center;"><h4 style="color:#ffcc00; margin:0 0 10px 0;">🎯 رادار صفقات الأصول القيادية الحية المفتوحة للبوت</h4><table width="100%" style="font-size:13px; text-align:center; color:#fff;"><tr style="background:#222; color:#ffcc00;"><th>الأصل الاستثماري</th><th>سعر العقد الحالي</th><th>نبض اتجاه الصفقة</th></tr><tr><td style="padding:6px; font-weight:bold;">🏆 الذهب العالمي (XAU/USD)</td><td style="color:#00ff00;">$2,045.20</td><td style="color:#00ff00; font-weight:bold;">شراء (Long) 📈</td></tr><tr><td style="padding:6px; font-weight:bold;">🛢️ النفط الخام / البترول (WTI)</td><td style="color:#ff3333;">$78.45</td><td style="color:#00ff00; font-weight:bold;">شراء (Long) 📈</td></tr><tr><td style="padding:6px; font-weight:bold;">₿ البيتكوين القيادي (BTC/USDT)</td><td style="color:#00ff00;">$82,016.50</td><td style="color:#ff3333; font-weight:bold;">بيع (Short) 📉</td></tr></table></div>{get_light_market_widget()}<hr style="border-color:#222;"><a href="/logout" style="color:#ff3333; text-decoration:none; font-weight:bold;">↩️ تسجيل خروج آمن</a></div></body></html>'

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
        users_db[email] = {'username': username, 'password': password, 'plan': plan, 'amount': amount, 'status': 'فترة تجريبية مجانية نشطة (7 أيام) 🎁', 'is_approved': True, 'completed_payments_count': 0, 'created_at': time.time(), 'pending_deposit': 0.0, 'pending_withdrawal': 0.0}
        session['logged_in'] = True; session['role'] = 'user'; session['user_email'] = email
        return redirect(url_for('user_dashboard'))
    return f'<html lang="ar" dir="rtl"><body style="background:#0d0d0d; color:white; font-family:sans-serif; padding:20px; padding-top:40px;"><div style="max-width:400px; margin:0 auto; background:#141414; padding:30px; border-radius:15px; border:1px solid #ffcc00; text-align:right;"><h2>🤖 حساب مستثمر جديد</h2><form method="POST"><input type="text" name="username" placeholder="الاسم الكامل" required style="width:100%; padding:10px; margin-bottom:15px;"><br><input type="email" name="email" placeholder="البريد الإلكتروني" required style="width:100%; padding:10px; margin-bottom:15px;"><br><input type="password" name="password" placeholder="اختر كلمة السر" required style="width:100%; padding:10px; margin-bottom:15px;"><br><select name="plan" style="width:100%; padding:10px; margin-bottom:15px;"><option value="weekly">أسبوعي ($19.99)</option><option value="monthly">شهري ($49.99)</option><option value="yearly">سنوي ($99.99)</option></select><br><input type="number" name="amount" placeholder="رأس مال محفظتك ($)" required style="width:100%; padding:10px; margin-bottom:20px;"><br><button type="submit" style="background:#ffcc00; color:black; width:100%; padding:12px; font-weight:bold; border:none; border-radius:5px; cursor:pointer;">إنشاء الحساب والبدء بـ 7 أيام مجانية 🚀</button></form>{get_light_market_widget()}</div></body></html>'
@app.route('/payment', methods=['GET', 'POST'])
def payment_page():
    if not session.get('logged_in') or session.get('role') != 'user': return redirect(url_for('login'))
    email = session.get('user_email'); u_info = users_db.get(email, {})
    if not u_info: return redirect(url_for('login'))
    if u_info.get('status') == 'نشط ومتابع ومحمي بالكامل 🛡️': return redirect(url_for('user_dashboard'))
    
    required_fee, discount_note = calculate_dynamic_subscription_fee(u_info.get('plan'), email)
    
    if request.method == 'POST':
        global total_subscription_earnings
        total_subscription_earnings += float(required_fee)
        u_info['status'] = 'تم تحويل رسوم Rain - بانتظار مراجعة وتفعيل المشرف عبد الله ⏳'
        return '<script>alert("تم إرسال إشعار التحويل للمشرف! سيفتح حسابك فور تأكيد الدفع."); window.location.href="/user-dashboard";</script>'
        
    return f'<html lang="ar" dir="rtl"><body style="background:#0c0c0c; color:white; font-family:sans-serif; padding-top:60px;"><div style="max-width:420px; margin:0 auto; background:#141414; padding:30px; border-radius:15px; border:2px solid #ffcc00; text-align:right;"><h2>💳 بوابة تحويل رسوم الاشتراك (محفظة Rain)</h2><p style="color:#00ff00; font-weight:bold;">المبلغ المطلوب: ${required_fee} USD</p><p style="color:#ffcc00; font-size:13px;">📌 {discount_note}</p><div style="background:#222; padding:12px; border-radius:5px; text-align:center; font-family:monospace; color:#ffcc00; word-break:break-all;">{RAIN_BITCOIN_ADDRESS}</div><br><form method="POST"><button type="submit" style="background:#00ff00; color:black; width:100%; padding:14px; font-weight:bold; border:none; border-radius:5px; cursor:pointer;">لقد قمت بالتحويل، أرسل الطلب للمشرف 🚀</button></form></div></body></html>'

@app.route('/admin-panel', methods=['GET', 'POST'])
def admin_panel():
    if not session.get('logged_in') or session.get('role') != 'admin': return redirect(url_for('login'))
    if request.method == 'POST':
        action = request.form.get('action'); target_email = request.form.get('user_email')
        if target_email in users_db:
            if action == 'approve_fees':
                users_db[target_email]['is_approved'] = True
                users_db[target_email]['status'] = 'تم تأكيد رسوم رين - بانتظار إيداع رأس مال بايننس 💰'
            elif action == 'approve_investment':
                users_db[target_email]['status'] = 'نشط ومتابع ومحمي بالكامل 🛡️'
                users_db[target_email]['completed_payments_count'] += 1
            elif action == 'approve_deposit':
                dep_amt = float(request.form.get('deposit_amount', 0))
                users_db[target_email]['amount'] += dep_amt
                users_db[target_email]['status'] = 'نشط ومتابع ومحمي بالكامل 🛡️'
            elif action == 'approve_withdrawal':
                users_db[target_email]['amount'] -= users_db[target_email]['pending_withdrawal']
                users_db[target_email]['pending_withdrawal'] = 0.0
                users_db[target_email]['status'] = 'انتهت الفترة التجريبية (7 أيام) - بانتظار دفع الاشتراك ⏳'
        return redirect(url_for('admin_panel'))
    try:
        balance = exchange.fetch_balance()
        free_usdt = round(balance['free'].get('USDT', 0), 2)
    except: free_usdt = "0.00 (محفظة الفيوتشرز فارغة)"
    rows = ""
    for k, v in users_db.items():
        btn_html = ""
        if 'بانتظار دفع الاشتراك' in v['status'] or 'تم تحويل رسوم Rain' in v['status']:
            btn_html = f'<form method="POST" style="display:inline;"><input type="hidden" name="user_email" value="{k}"><input type="hidden" name="action" value="approve_fees"><button type="submit" style="background:#00ff00; color:black; padding:5px 10px; font-weight:bold; border:none; border-radius:4px; cursor:pointer;">✓ تأكيد رسوم Rain</button></form>'
        elif 'بانتظار إيداع رأس مال بايننس' in v['status'] or 'تم إرسال رأس المال' in v['status']:
            btn_html = f'<form method="POST" style="display:inline;"><input type="hidden" name="user_email" value="{k}"><input type="hidden" name="action" value="approve_investment"><button type="submit" style="background:#00bfff; color:black; padding:5px 10px; font-weight:bold; border:none; border-radius:4px; cursor:pointer;">✓ تأكيد إيداع بايننس</button></form>'
        elif 'قيد مراجعة إيداع الأموال الإضافية' in v['status']:
            btn_html = f'<form method="POST" style="display:inline;"><input type="hidden" name="user_email" value="{k}"><input type="hidden" name="deposit_amount" value="{v.get("pending_deposit", 0)}"><input type="hidden" name="action" value="approve_deposit"><button type="submit" style="background:#00ff00; color:black; padding:5px 10px; font-weight:bold; border:none; border-radius:4px; cursor:pointer;">✓ موافقة على شحن الرصيد</button></form>'
        elif 'قيد مراجعة طلب السحب' in v['status']:
            btn_html = f'<form method="POST" style="display:inline;"><input type="hidden" name="user_email" value="{k}"><input type="hidden" name="action" value="approve_withdrawal"><button type="submit" style="background:#ff3333; color:white; padding:5px 10px; font-weight:bold; border:none; border-radius:4px; cursor:pointer;">✓ موافقة على سحب الأموال</button></form>'
        rows += f"<tr style='background:#222; border-bottom:1px solid #333;'><td>{v['username']}</td><td>{k}</td><td>{v['plan']}</td><td>${v['amount']}</td><td style='color:#ffcc00;'>{v['status']}</td><td>{btn_html}</td></tr>"
    if not users_db: rows = "<tr><td colspan='6' style='color:#666; padding:15px;'>لا يوجد مستثمرون حالياً.</td></tr>"
    return f'<html lang="ar" dir="rtl"><body style="background:#0c0c0c; color:white; font-family:sans-serif; padding:20px;"><div style="max-width:950px; margin:0 auto; background:#141414; padding:25px; border-radius:15px; border:2px solid #ffcc00;"><h2>👑 لوحة الإشراف العليا - المشرف الغالي عبد الله</h2><p>رصيدك في بايننس: <b style="color:#00ff00;">${free_usdt} USDT</b></p><p>أرباح رسوم رين: <b style="color:#ffcc00;">${total_subscription_earnings:.2f} USD</b></p><hr style="border-color:#333;"><h3>👥 إدارة وتفعيل المستثمرين والعمليات المالية:</h3><table width="100%" border="1" cellpadding="10" style="text-align:center; border-collapse:collapse; border-color:#333;"><thead style="background:#222; color:#ffcc00;"><th>الاسم</th><th>البريد</th><th>الباقة</th><th>رأس المال الحالي</th><th>الحالة والترخيص</th><th>الإجراء المالي الفوري</th></thead><tbody>{rows}</tbody></table><br><a href="/logout" style="color:#ff3333; font-weight:bold; text-decoration:none;">↩️ خروج آمن</a></div></body></html>'

@app.route('/user-dashboard', methods=['GET', 'POST'])
def user_dashboard():
    if not session.get('logged_in') or session.get('role') != 'user': return redirect(url_for('login'))
    email = session.get('user_email'); u = users_db.get(email, {})
    if not u: return redirect(url_for('login'))
    
    days_passed = (time.time() - u['created_at']) / 86400
    is_expired = days_passed > 7.0 and 'completed_payments_count' in u and u['completed_payments_count'] == 0
    
    if is_expired and '7 أيام مجانية' in u['status']:
        u['is_approved'] = False
        u['status'] = 'انتهت الفترة التجريبية (7 أيام) - بانتظار دفع الاشتراك ⏳'
        return redirect(url_for('payment_page'))
        
    if request.method == 'POST':
        action = request.form.get('user_action')
        if action == 'add_funds':
            dep_amt = float(request.form.get('deposit_amount', 0))
            u['pending_deposit'] = dep_amt
            u['status'] = f'قيد مراجعة إيداع الأموال الإضافية (${dep_amt}) من قبل عبد الله ⏳'
            return '<script>alert("تم إرسال طلب شحن المحفظة للمشرف عبد الله! سيتم إضافة المبلغ فور تأكيد تحويلك."); window.location.href="/user-dashboard";</script>'
        elif action == 'withdraw_funds':
            if u['is_approved'] and not is_expired and 'نشط ومتابع' in u['status']:
                return '<script>alert("🛑 أمنياً لحماية الصفقات: لا يمكنك السحب أثناء نشاط الاشتراك الفعال وعقود التداول المفتوحة. يرجى الانتظار حتى انتهاء الاشتراك لتجنب تصفير المحفظة."); window.location.href="/user-dashboard";</script>'
            withdraw_address = request.form.get('withdraw_address')
            u['pending_withdrawal'] = u['amount']
            u['status'] = f'قيد مراجعة طلب السحب لمحفظة ({withdraw_address}) من قبل عبد الله ⏳'
            return '<script>alert("تم تقديم طلب السحب بنجاح! سيقوم عبد الله بتحويل الأموال ومراجعة طلبك فوراً."); window.location.href="/user-dashboard";</script>'

    if u['is_approved'] and 'بانتظار إيداع رأس مال بايننس' in u['status']:
        if request.method == 'POST' and request.form.get('coin_type'):
            coin = request.form.get('coin_type', 'USDT')
            u['status'] = f'تم إرسال رأس المال بـ ({coin}) - قيد تفعيل التداول التلقائي من المشرف ⏳'
            return '<script>alert("تم إرسال الطلب لعبد الله!"); window.location.href="/user-dashboard";</script>'
