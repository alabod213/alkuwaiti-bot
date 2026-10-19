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
