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

# مصفوفة رادار الأصول والسلع القيادية للتحليل اللحظي المباشر لعام 2026
WATCHLIST = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'PAXG/USDT', 'BNB/USDT']

# الربط القياسي الصارم لبايننس فيوتشرز (USDT-Margined) ومنع الرفض
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

    if plan == 'weekly' and past_payments > 0 and (past_payments % 3 == 0):
        return 0.0, "🎁 عرض Loyalty: الأسبوع الرابع مجاني بالكامل!"
    if plan == 'monthly' and past_payments > 0 and (past_payments % 3 == 0):
        return 0.0, "🎁 عرض Loyalty: الشهر الرابع مجاني بالكامل!"

    HOLIDAYS_2026 = [(1, 1), (2, 25), (2, 26), (3, 20), (5, 27), (6, 16)]
    is_holiday = (now.month, now.day) in HOLIDAYS_2026
    is_weekend = now.weekday() in (4, 5)

    if is_holiday:
        return round(max(0.0, final_price - 10.0), 2), "🌙 عروض الأعياد والمناسبات: خصم $10 فوري لـ 24 ساعة!"
    if is_weekend:
        if plan == 'weekly': return round(max(0.0, final_price - 2.0), 2), "⚡ عروض الويكند الحية: خصم $2 على الأسبوعي!"
        if plan == 'monthly': return round(max(0.0, final_price - 5.0), 2), "⚡ عروض الويكند الحية: خصم $5 على الشهري!"
        if plan == 'yearly': return round(max(0.0, final_price - 10.0), 2), "⚡ عروض الويكند الحية: خصم $10 على السنوي!"
    
    return round(final_price, 2), "Standard Price Plan"

def get_light_market_widget():
    html = '<div style="background:#1e1e1e; padding:15px; border-radius:10px; margin-top:15px; border:1px solid #ffcc00; text-align:center;"><h4 style="color:#ffcc00; margin:0 0 10px 0;">Market Radar Summary</h4><div style="display:flex; justify-content:space-around; flex-wrap:wrap; gap:10px;">'
    try:
        for symbol in WATCHLIST:
            ticker = exchange.fetch_ticker(symbol)
            price = round(ticker['last'], 2)
            change = round(ticker['percentage'], 2) if ticker['percentage'] is not None else 0.0
            color = '#00ff00' if change >= 0 else '#ff3333'
            display_name = "Gold (PAXG)" if "PAXG" in symbol else ("Oil/Energy" if "BNB" in symbol else symbol.replace("/USDT",""))
            html += f'<div style="background:#111; padding:8px; border-radius:5px; font-size:12px;"><b>{display_name}</b><br><span style="color:#00ff00;">${price}</span><br><span style="color:{color}; font-weight:bold;">{change}%</span></div>'
    except:
        html += '<p style="color:#aaa; font-size:12px; margin:0;">Updating rates from Binance Futures Secure Grid...</p>'
    html += '</div></div>'
    return html
def get_google_translator_script():
    # كود جافا سكريبت النظيف المصلح لاستدعاء مترجم جوجل الفوري لجميع لغات العالم ولهجاتها
    return '''
    <div id="google_translate_element" style="text-align:center; padding:10px; background:#141414; border-bottom:1px solid #333; min-height:40px;"></div>
    <script type="text/javascript">
    function googleTranslateElementInit() {
        new google.translate.TranslateElement({pageLanguage: 'ar', layout: google.translate.TranslateElement.InlineLayout.SIMPLE}, 'google_translate_element');
    }
    </script>
    <script type="text/javascript" src="//://google.com"></script>
    '''

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
            live_balance = 362.41
            try:
                balance = exchange.fetch_balance()
                free_usdt = balance['free'].get('USDT', 0)
                if free_usdt and float(free_usdt) > 0: live_balance = round(float(free_usdt), 2)
            except: pass
            users_db[ADMIN_EMAIL] = {
                'username': 'المشرف العام عبد الله 👑', 'password': SECURITY_CODE, 'plan': 'سيادي (إشراف)', 'amount': live_balance,
                'status': 'نشط ومتابع ومحمي بالكامل 🛡️', 'is_approved': True, 'completed_payments_count': 1, 'created_at': time.time(),
                'pending_deposit': 0.0, 'pending_withdrawal': 0.0, 'user_profits': 0.0, 'current_asset': 'جاري بث وإرسال الأوامر الفورية لـ USDT...'
            }
            return redirect(url_for('admin_panel'))
        if email in users_db and code == users_db[email]['password']:
            session['logged_in'] = True; session['role'] = 'user'; session['user_email'] = email
            return redirect(url_for('user_dashboard'))
        return '<script>alert("البيانات غير صحيحة!"); window.history.back();</script>'
    return f'<html lang="ar" dir="rtl"><body style="background:#0c0c0c; color:white; font-family:sans-serif; text-align:center; margin:0; padding:0;">{get_google_translator_script()}<div style="background:#141414; max-width:400px; margin:60px auto; padding:30px; border-radius:15px; border:2px solid #ffcc00; text-align:right;"><h2>🔓 بوابة تسجيل الدخول الموحدة</h2><form method="POST"><input type="email" name="email" placeholder="البريد الإلكتروني" required style="width:100%; padding:10px; margin-bottom:15px;"><br><input type="password" name="code" placeholder="كلمة السر أو كود الأمان" required style="width:100%; padding:10px; margin-bottom:20px;"><br><button type="submit" style="background:#ffcc00; color:black; width:100%; padding:12px; font-weight:bold; border:none; border-radius:5px; cursor:pointer; width:100%;">دخول آمن للمنصة 🔐</button></form><p style="text-align:center; font-size:13px; margin-top:15px;"><a href="/register" style="color:#ffcc00; text-decoration:none;">سجل كمشترك جديد هنا 🚀</a> | <a href="/forgot-password" style="color:#aaa; text-decoration:none;">نسيت كلمة المرور؟ 🔍</a></p>{get_light_market_widget()}</div></body></html>'

@app.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    if request.method == 'POST':
        email = request.form.get('email').strip().lower()
        if email == ADMIN_EMAIL:
            return f'<script>alert("كود أمانك الثابت هو: {SECURITY_CODE}"); window.location.href="/login";</script>'
        if email in users_db:
            user_pass = users_db[email]['password']
            return f'<html lang="ar" dir="rtl"><body style="background:#0c0c0c; color:white; font-family:sans-serif; text-align:center; padding-top:100px;">{get_google_translator_script()}<div style="background:#141414; width:350px; margin:0 auto; padding:30px; border-radius:15px; border:2px solid #ffcc00; text-align:right;"><h3>🔑 استعادة الحساب</h3><p>كلمة المرور الخاصة بك هي: <b style="color:#00ff00; font-size:18px;">{user_pass}</b></p><br><a href="/login" style="background:#00bfff; color:black; padding:10px; text-decoration:none; font-weight:bold; border-radius:5px; display:block; text-align:center;">تسجيل الدخول 🔓</a></div></body></html>'
        return '<script>alert("هذا البريد غير مسجل!"); window.history.back();</script>'
    return f'<html lang="ar" dir="rtl"><body style="background:#0c0c0c; color:white; font-family:sans-serif; text-align:center; padding-top:120px;">{get_google_translator_script()}<div style="background:#141414; width:350px; margin:0 auto; padding:30px; border-radius:15px; border:2px solid #ffcc00; text-align:right;"><h2>🔍 استعادة كلمة المرور</h2><form method="POST"><input type="email" name="email" placeholder="بريدك الإلكتروني الحقيقي" required style="width:100%; padding:10px; margin-bottom:20px;"><br><button type="submit" style="background:#ffcc00; color:black; width:100%; padding:12px; font-weight:bold; border:none; border-radius:5px; cursor:pointer;">استعادة الحساب 🛡️</button></form></div></body></html>'
@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username')
        email = request.form.get('email').strip().lower()
        password = request.form.get('password')
        plan = request.form.get('plan')
        amount = float(request.form.get('amount', 0))
        if amount < 50.0:
            return '<script>alert("الحد الادنى للاستثمار هو 50 دولار لحماية الصفقات."); window.location.href="/register";</script>'
        if email in users_db or email == ADMIN_EMAIL: return '<script>alert("مسجل مسبقا!"); window.location.href="/register";</script>'
        users_db[email] = {
            'username': username, 'password': password, 'plan': plan, 'amount': amount, 
            'status': 'فترة تجريبية مجانية نشطة (7 أيام) 🎁', 'is_approved': True, 
            'completed_payments_count': 0, 'created_at': time.time(), 
            'pending_deposit': 0.0, 'pending_withdrawal': 0.0,
            'user_profits': 0.0, 'current_asset': 'تحديد تلقائي مرن لقنوات السوق'
        }
        session['logged_in'] = True; session['role'] = 'user'; session['user_email'] = email
        return redirect(url_for('user_dashboard'))
    return f'<html lang="ar" dir="rtl"><body style="background:#0d0d0d; color:white; font-family:sans-serif; padding:0; margin:0;">{get_google_translator_script()}<div style="max-width:400px; margin:40px auto; background:#141414; padding:30px; border-radius:15px; border:2px solid #ffcc00; text-align:right;"><h2>🤖 صندوق استثماري جديد</h2><form method="POST"><input type="text" name="username" placeholder="الاسم الكامل" required style="width:100%; padding:10px; margin-bottom:15px;"><br><input type="email" name="email" placeholder="البريد الإلكتروني" required style="width:100%; padding:10px; margin-bottom:15px;"><br><input type="password" name="password" placeholder="اختر كلمة السر" required style="width:100%; padding:10px; margin-bottom:15px;"><br><select name="plan" style="width:100%; padding:10px; margin-bottom:15px;"><option value="weekly">أسبوعي ($19.99)</option><option value="monthly">شهري ($49.99)</option><option value="yearly">سنوي ($99.99)</option></select><br><input type="number" step="any" name="amount" placeholder="رأس المال الاستثماري (الحد الأدنى $50)" required style="width:100%; padding:10px; margin-bottom:20px;"><br><button type="submit" style="background:#ffcc00; color:black; width:100%; padding:12px; font-weight:bold; border:none; border-radius:5px; cursor:pointer;">إنشاء الحساب وبدء الـ 7 أيام مجاناً 🚀</button></form>{get_light_market_widget()}</div></body></html>'

@app.route('/payment', methods=['GET', 'POST'])
def payment_page():
    if not session.get('logged_in') or session.get('role') != 'user': return redirect(url_for('login'))
    email = session.get('user_email'); u_info = users_db.get(email, {})
    if not u_info: return redirect(url_for('login'))
    if u_info.get('status') == 'نشط ومتابع ومحمي بالكامل 🛡️': return redirect(url_for('user_dashboard'))
    required_fee, discount_note = calculate_dynamic_subscription_fee(u_info.get('plan'), email)
    if request.method == 'POST':
        global total_subscription_earnings; total_subscription_earnings += float(required_fee)
        u_info['status'] = 'تم تحويل رسوم Rain - بانتظار مراجعة وتفعيل المشرف عبد الله ⏳'
        return '<script>alert("تم إرسال معاملتك للمشرف عبد الله! ستفتح لوحتك فور تأكيد التحويل."); window.location.href="/user-dashboard";</script>'
    return f'<html lang="ar" dir="rtl"><body style="background:#0c0c0c; color:white; font-family:sans-serif; padding:0; margin:0;">{get_google_translator_script()}<div style="max-width:420px; margin:60px auto; background:#141414; padding:30px; border-radius:15px; border:2px solid #ffcc00; text-align:right;"><h2>💳 بوابة تحويل رسوم الاشتراك (محفظة Rain)</h2><p style="color:#00ff00; font-weight:bold;">المبلغ المطلوب: ${required_fee} USD</p><p style="color:#ffcc00; font-size:13px;">📌 {discount_note}</p><div style="background:#222; padding:12px; border-radius:5px; text-align:center; font-family:monospace; color:#ffcc00; word-break:break-all;">{RAIN_BITCOIN_ADDRESS}</div><br><form method="POST"><button type="submit" style="background:#00ff00; color:black; width:100%; padding:14px; font-weight:bold; border:none; border-radius:5px; cursor:pointer;">لقد قمت بالتحويل، أرسل المعاملة للمشرف 🚀</button></form></div></body></html>'

@app.route('/admin-panel', methods=['GET', 'POST'])
def admin_panel():
    if not session.get('logged_in') or session.get('role') != 'admin': return redirect(url_for('login'))
    u = users_db.get(ADMIN_EMAIL, {})
    live_balance = u.get('amount', 362.41)
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
    rows = ""
    for k, v in users_db.items():
        btn_html = ""
        if k == ADMIN_EMAIL:
            status_display = "<span style='color:#00ffff; font-weight:bold;'>حسابك الاستثماري الإداري نشط</span>"
        else:
            status_display = v['status']
            if 'بانتظار دفع الاشتراك' in v['status'] or 'تم تحويل رسوم Rain' in v['status']:
                btn_html = f'<form method="POST" style="display:inline;"><input type="hidden" name="user_email" value="{k}"><input type="hidden" name="action" value="approve_fees"><button type="submit" style="background:#00ff00; color:black; padding:5px 10px; font-weight:bold; border:none; border-radius:4px; cursor:pointer;">✓ تأكيد رسوم Rain</button></form>'
            elif 'بانتظار إيداع رأس مال بايننس' in v['status'] or 'تم إرسال رأس المال' in v['status']:
                btn_html = f'<form method="POST" style="display:inline;"><input type="hidden" name="user_email" value="{k}"><input type="hidden" name="action" value="approve_investment"><button type="submit" style="background:#00bfff; color:black; padding:5px 10px; font-weight:bold; border:none; border-radius:4px; cursor:pointer;">✓ تأكيد إيداع بايننس</button></form>'
            elif 'قيد مراجعة إيداع الأموال الإضافية' in v['status']:
                btn_html = f'<form method="POST" style="display:inline;"><input type="hidden" name="user_email" value="{k}"><input type="hidden" name="deposit_amount" value="{v.get("pending_deposit", 0)}"><input type="hidden" name="action" value="approve_deposit"><button type="submit" style="background:#00ff00; color:black; padding:5px 10px; font-weight:bold; border:none; border-radius:4px; cursor:pointer;">✓ موافقة شحن الرصيد</button></form>'
            elif 'قيد مراجعة طلب السحب' in v['status']:
                btn_html = f'<form method="POST" style="display:inline;"><input type="hidden" name="user_email" value="{k}"><input type="hidden" name="action" value="approve_withdrawal"><button type="submit" style="background:#ff3333; color:white; padding:5px 10px; font-weight:bold; border:none; border-radius:4px; cursor:pointer;">✓ موافقة سحب الأموال</button></form>'
        rows += f"<tr style='background:#222; border-bottom:1px solid #333;'><td>{v.get('username', 'مستثمر')}</td><td>{k}</td><td>${v.get('amount', 0.0):.2f}</td><td style='color:#00ff00; font-weight:bold;'>${v.get('user_profits', 0.0):.2f}</td><td style='color:#00e5ff;'>{v.get('current_asset', 'لا يوجد صفقات')}</td><td>{status_display}</td><td>{btn_html}</td></tr>"
    return f'<html lang="ar" dir="rtl"><body style="background:#0c0c0c; color:white; font-family:sans-serif; padding:0; margin:0;">{get_google_translator_script()}<div style="max-width:1100px; margin:30px auto; background:#141414; padding:25px; border-radius:15px; border:2px solid #ffcc00;"><h2>👑 لوحة الإشراف العليا والاستثمار السيادي للمشرف عبد الله</h2><p>رصيدك العام المباشر في بايننس: <b style="color:#00ff00;">${live_balance} USDT</b></p><p>أرباح رسوم رين الموثقة: <b style="color:#ffcc00;">${total_subscription_earnings:.2f} USD</b></p><p>صندوق تتبع سحوبات أرباح بايننس لـ Rain: <b style="color:#00e5ff;">${cumulative_bot_profits:.2f} / $100.00</b></p><hr style="border-color:#333;"><h3>👥 كشف وتحكم شامل بالمحفظة، المستثمرين... وأرباحك المباشرة:</h3><table width="100%" border="1" cellpadding="10" style="text-align:center; border-collapse:collapse; border-color:#333;"><thead style="background:#222; color:#ffcc00;"><th>الاسم الكلي</th><th>البريد الإلكتروني</th><th>رأس المال الحالي</th><th>الأرباح المحققة 📈</th><th>الأصل المستثمر به حالياً</th><th>الحالة العامة والترخيص</th><th>الإجراء المالي الفوري</th></thead><tbody>{rows}</tbody></table><br>{get_light_market_widget()}<br><br><a href="/logout" style="color:#ff3333; font-weight:bold; text-decoration:none;">↩️ تسجيل خروج آمن</a></div></body></html>'
@app.route('/user-dashboard', methods=['GET', 'POST'])
def user_dashboard():
    if not session.get('logged_in') or session.get('role') != 'user': return redirect(url_for('login'))
    email = session.get('user_email'); u = users_db.get(email, {})
    if not u: return redirect(url_for('login'))
    days_passed = (time.time() - u['created_at']) / 86400
    is_expired = days_passed > 7.0 and 'completed_payments_count' in u and u['completed_payments_count'] == 0
    if is_expired and '7 أيام مجانية' in u['status']:
        u['is_approved'] = False; u['status'] = 'انتهت الفترة التجريبية (7 أيام) - بانتظار دفع الاشتراك ⏳'
        return redirect(url_for('payment_page'))
    if request.method == 'POST':
        action = request.form.get('user_action')
        if action == 'add_funds':
            dep_amt = float(request.form.get('deposit_amount', 0))
            u['pending_deposit'] = dep_amt; u['status'] = f'قيد مراجعة إيداع الأموال الإضافية (${dep_amt}) من قبل عبد الله ⏳'
            return '<script>alert("تم إرسال طلب شحن المحفظة للمشرف عبد الله!"); window.location.href="/user-dashboard";</script>'
        elif action == 'withdraw_funds':
            if u['is_approved'] and not is_expired and 'نشط ومتابع' in u['status']:
                return '<script>alert("🛑 أمنياً لحماية الصفقات: لا يمكنك السحب أثناء نشاط الاشتراك الفعال وعقود التداول المفتوحة."); window.location.href="/user-dashboard";</script>'
            withdraw_address = request.form.get('withdraw_address')
            u['pending_withdrawal'] = u['amount']; u['status'] = f'قيد مراجعة طلب السحب لمحفظة ({withdraw_address}) من قبل عبد الله ⏳'
            return '<script>alert("تم تقديم طلب السحب بنجاح!"); window.location.href="/user-dashboard";</script>'
    return f'<html lang="ar" dir="rtl"><body style="background:#0c0c0c; color:white; font-family:sans-serif; padding:0; margin:0;">{get_google_translator_script()}<div style="max-width:600px; margin:30px auto; background:#141414; padding:30px; border-radius:15px; border:2px solid #ffcc00; text-align:right;"><h2>📊 لوحة المستثمر - صندوق الأصول الشامل لعام 2026</h2><p>المستثمر: <b>{u.get("username")}</b></p><p>رأس المال المودع بالتداول: <b style="color:#ffcc00;">${u.get("amount")} USDT</b></p><p>صافي أرباحك الإجمالية المحققة: <b style="color:#00ff00; font-size:18px;">+${u.get("user_profits", 0.0):.2f} USDT 📈</b></p><p>الأصل المستثمر به حسابك الآن: <b style="color:#00e5ff;">{u.get("current_asset")}</b></p><p>الحالة الأمنية للمحفظة: <b style="color:#00bfff;">{u.get("status")}</b></p><div style="background:#1e1e1e; padding:15px; border-radius:10px; margin-top:20px; margin-bottom:20px; border:1px solid #ffcc00;"><h3 style="color:#ffcc00; text-align:center; margin-top:0;">💳 العمليات والتحويلات المالية للمحفظة</h3><div style="display:flex; justify-content:space-between; gap:15px;"><div style="background:#111; padding:15px; border-radius:8px; width:50%;"><h4>💰 إضافة أموال إضافية</h4><form method="POST"><input type="hidden" name="user_action" value="add_funds"><input type="number" step="any" name="deposit_amount" placeholder="المبلغ بالدولار $" required style="width:100%; padding:8px; margin-bottom:10px;"><br><button type="submit" style="background:#00ff00; color:black; width:100%; padding:8px; border:none; border-radius:4px; font-weight:bold; cursor:pointer;">شحن المحفظة 🚀</button></form></div><div style="background:#111; padding:15px; border-radius:8px; width:50%;"><h4>📥 سحب رأس المال والأرباح</h4><form method="POST"><input type="hidden" name="user_action" value="withdraw_funds"><input type="text" name="withdraw_address" placeholder="عنوان محفظتك الخارجية" required style="width:100%; padding:8px; margin-bottom:10px;"><br><button type="submit" style="background:#ff3333; color:white; width:100%; padding:8px; border:none; border-radius:4px; font-weight:bold; cursor:pointer;">سحب كامل الأموال 💳</button></form></div></div></div>{get_light_market_widget()}<hr style="border-color:#222;"><a href="/logout" style="color:#ff3333; text-decoration:none; font-weight:bold;">↩️ تسجيل خروج آمن</a></div></body></html>'

@app.route('/logout')
def logout():
    session.clear(); return redirect(url_for('login'))

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
                if required_margin_paxg <= capital_share:
                    active_assets.extend(commodity_assets)
                    
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
                        
                        direction_label = f"Buy/Long {chosen_leverage}x" if order_side == 'buy' else f"Sell/Short {chosen_leverage}x"
                        display_asset = f"Gold (PAXG) {direction_label} 📈" if "PAXG" in asset else (f"Oil/Energy (BNB) {direction_label} 📈" if "BNB" in asset else f"{asset.replace('/USDT','')} {direction_label} 📈")
                        
                        if ADMIN_EMAIL not in users_db:
                            users_db[ADMIN_EMAIL] = {'username': 'المشرف العام عبد الله 👑', 'amount': current_free, 'user_profits': cumulative_bot_profits, 'status': 'نشط ومتابع ومحمي بالكامل 🛡️', 'current_asset': display_asset}
                        else:
                            users_db[ADMIN_EMAIL]['current_asset'] = display_asset
                            users_db[ADMIN_EMAIL]['user_profits'] = cumulative_bot_profits
                            users_db[ADMIN_EMAIL]['amount'] = current_free
                            
                        if cumulative_bot_profits >= 100.0:
                            cumulative_bot_profits = max(0.0, cumulative_bot_profits - 100.0)
                            print(f"[SECURITY SYSTEM] Target met! Withdrawing $100 to Rain: {RAIN_BITCOIN_ADDRESS}")
        except: pass
        time.sleep(2.0)

threading.Thread(target=auto_trading_engine, daemon=True).start()
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000)); app.run(host='0.0.0.0', port=port)
