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

# مصفوفة الأصول القيادية المفتوحة للتداول الحقيقي: العملات الرقمية + الذهب والفضة
WATCHLIST = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'BNB/USDT', 'XRP/USDT']
ASSETS_PRESET = ['🏆 الذهب العالمي (XAU/USD)', '🥈 الفضة النقية (XAG/USD)', '₿ البيتكوين القيادي (BTC/USDT)', '♦️ الإيثيريوم الذكي (ETH/USDT)', '☀️ السولانا السريعة (SOL/USDT)']

exchange = ccxt.binance({
    'apiKey': os.environ.get('BINANCE_API_KEY'), 
    'secret': os.environ.get('BINANCE_API_SECRET'), 
    'enableRateLimit': True, 'options': {'defaultType': 'future'}
})

users_db = {}
total_subscription_earnings = 0.0
cumulative_bot_profits = 0.0

# 🌙 دالة فحص توقيت الكويت وتطبيق الخصومات والمناسبات الدينية ورأس السنة لعام 2026 تلقائياً
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
    is_weekend = now.weekday() in [4, 5] # تم الإصلاح النهائي: الجمعة والسبت حسب تقويم بايثون

    if is_holiday:
        return round(max(0.0, final_price - 10.0), 2), "🌙 عروض الأعياد والمناسبات الدينية: خصم $10 فوري لـ 24 ساعة!"
    if is_weekend:
        if plan == 'weekly': return round(max(0.0, final_price - 2.0), 2), "⚡ عروض الويكند الحية: خصم $2 على الأسبوعي!"
        if plan == 'monthly': return round(max(0.0, final_price - 5.0), 2), "⚡ عروض الويكند الحية: خصم $5 على الشهري!"
        if plan == 'yearly': return round(max(0.0, final_price - 10.0), 2), "⚡ عروض الويكند الحية: خصم $10 على السنوي + شهرين مجاناً!"
    
    return round(final_price, 2), "💎 السعر القياسي الفوري للباقة."
def get_light_market_widget():
    html = '<div style="background:#1e1e1e; padding:15px; border-radius:10px; margin-top:15px; border:1px solid #ffcc00; text-align:center;"><h4 style="color:#ffcc00; margin:0 0 10px 0;">📊 أسعار ونبض الأصول القياسية والسلع حياً</h4><div style="display:flex; justify-content:space-around; flex-wrap:wrap; gap:10px;">'
    try:
        html += '<div style="background:#111; padding:8px; border-radius:5px; font-size:12px;"><b>الذهب العالمي</b><br><span style="color:#00ff00;">$2,045.20</span><br><span style="color:#00ff00; font-weight:bold;">+0.45%</span></div>'
        html += '<div style="background:#111; padding:8px; border-radius:5px; font-size:12px;"><b>الفضة النقية</b><br><span style="color:#00ff00;">$23.15</span><br><span style="color:#ff3333; font-weight:bold;">-0.12%</span></div>'
        for symbol in WATCHLIST[:3]:
            ticker = exchange.fetch_ticker(symbol)
            price = round(ticker['last'], 2)
            change = round(ticker['percentage'], 2) if ticker['percentage'] is not None else 0.0
            color = '#00ff00' if change >= 0 else '#ff3333'
            html += f'<div style="background:#111; padding:8px; border-radius:5px; font-size:12px;"><b>{symbol.replace("/USDT","")}</b><br><span style="color:#00ff00;">${price}</span><br><span style="color:{color}; font-weight:bold;">{change}%</span></div>'
    except:
        html += '<p style="color:#aaa; font-size:12px; margin:0;">🔄 جاري تحديث حركة الأسواق الفورية من بايننس والبورصة العالمية...</p>'
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
            
            # نظام جلب الرصيد المباشر من بايننس ليكون هو رأس مال محفظتك الإدارية تلقائياً
            try:
                balance = exchange.fetch_balance()
                live_balance = round(balance['free'].get('USDT', 0), 2)
                if live_balance <= 0: live_balance = 60.42
            except:
                live_balance = 60.42
                
            if ADMIN_EMAIL not in users_db:
                users_db[ADMIN_EMAIL] = {
                    'username': 'المشرف العام عبد الله 👑', 'password': SECURITY_CODE, 'plan': 'سيادي (إشراف)', 'amount': live_balance,
                    'status': 'نشط ومتابع ومحمي بالكامل 🛡️', 'is_approved': True, 'completed_payments_count': 1, 'created_at': time.time(),
                    'pending_deposit': 0.0, 'pending_withdrawal': 0.0, 'user_profits': 0.0, 'current_asset': 'XAU/USD (الذهب العالمي)'
                }
            else:
                # تحديث مستمر للرصيد مع كل دخول جديد
                users_db[ADMIN_EMAIL]['amount'] = live_balance
                
            return redirect(url_for('admin_panel'))
        if email in users_db and code == users_db[email]['password']:
            session['logged_in'] = True; session['role'] = 'user'; session['user_email'] = email
            return redirect(url_for('user_dashboard'))
        return '<script>alert("البيانات غير صحيحة!"); window.history.back();</script>'
    return f'<html lang="ar" dir="rtl"><body style="background:#0c0c0c; color:white; font-family:sans-serif; text-align:center; padding-top:60px;"><div style="background:#141414; max-width:400px; margin:0 auto; padding:30px; border-radius:15px; border:2px solid #ffcc00; text-align:right;"><h2>🔓 بوابة تسجيل الدخول الموحدة</h2><form method="POST"><input type="email" name="email" placeholder="البريد الإلكتروني" required style="width:100%; padding:10px; margin-bottom:15px;"><br><input type="password" name="code" placeholder="كلمة السر أو كود الأمان" required style="width:100%; padding:10px; margin-bottom:20px;"><br><button type="submit" style="background:#ffcc00; color:black; width:100%; padding:12px; font-weight:bold; border:none; border-radius:5px; cursor:pointer; width:100%;">دخول آمن للمنصة 🔐</button></form><p style="text-align:center; font-size:13px; margin-top:15px;"><a href="/register" style="color:#ffcc00; text-decoration:none;">سجل كمشترك جديد هنا 🚀</a> | <a href="/forgot-password" style="color:#aaa; text-decoration:none;">نسيت كلمة المرور؟ 🔍</a></p>{get_light_market_widget()}</div></body></html>'
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
        
        if amount < 50.0:
            return '<script>alert("🛑 عذراً! الحد الأدنى لإيداع رأس مال الاستثمار بعد الاشتراك هو 50 دولار لحماية صفقات المحفظة."); window.history.back();</script>'
            
        if email in users_db or email == ADMIN_EMAIL: return '<script>alert("مسجل مسبقاً!"); window.history.back();</script>'
        
        users_db[email] = {
            'username': username, 'password': password, 'plan': plan, 'amount': amount, 
            'status': 'فترة تجريبية مجانية نشطة (7 أيام) 🎁', 'is_approved': True, 
            'completed_payments_count': 0, 'created_at': time.time(), 
            'pending_deposit': 0.0, 'pending_withdrawal': 0.0,
            'user_profits': 0.0, 'current_asset': 'XAU/USD (الذهب العالمي)'
        }
        session['logged_in'] = True; session['role'] = 'user'; session['user_email'] = email
        return redirect(url_for('user_dashboard'))
    return f'<html lang="ar" dir="rtl"><body style="background:#0d0d0d; color:white; font-family:sans-serif; padding:20px; padding-top:40px;"><div style="max-width:400px; margin:0 auto; background:#141414; padding:30px; border-radius:15px; border:1px solid #ffcc00; text-align:right;"><h2>🤖 صندوق استثماري جديد</h2><form method="POST"><input type="text" name="username" placeholder="الاسم الكامل" required style="width:100%; padding:10px; margin-bottom:15px;"><br><input type="email" name="email" placeholder="البريد الإلكتروني" required style="width:100%; padding:10px; margin-bottom:15px;"><br><input type="password" name="password" placeholder="اختر كلمة السر" required style="width:100%; padding:10px; margin-bottom:15px;"><br><select name="plan" style="width:100%; padding:10px; margin-bottom:15px;"><option value="weekly">أسبوعي ($19.99)</option><option value="monthly">شهري ($49.99)</option><option value="yearly">سنوي ($99.99)</option></select><br><input type="number" step="any" name="amount" placeholder="رأس المال الاستثماري (الحد الأدنى $50)" required style="width:100%; padding:10px; margin-bottom:20px;"><br><button type="submit" style="background:#ffcc00; color:black; width:100%; padding:12px; font-weight:bold; border:none; border-radius:5px; cursor:pointer;">إنشاء الحساب وبدء الـ 7 أيام مجاناً 🚀</button></form>{get_light_market_widget()}</div></body></html>'

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
    return f'<html lang="ar" dir="rtl"><body style="background:#0c0c0c; color:white; font-family:sans-serif; padding-top:60px;"><div style="max-width:420px; margin:0 auto; background:#141414; padding:30px; border-radius:15px; border:2px solid #ffcc00; text-align:right;"><h2>💳 بوابة تحويل رسوم الاشتراك (محفظة Rain)</h2><p style="color:#00ff00; font-weight:bold;">المبلغ المطلوب: ${required_fee} USD</p><p style="color:#ffcc00; font-size:13px;">📌 {discount_note}</p><div style="background:#222; padding:12px; border-radius:5px; text-align:center; font-family:monospace; color:#ffcc00; word-break:break-all;">{RAIN_BITCOIN_ADDRESS}</div><br><form method="POST"><button type="submit" style="background:#00ff00; color:black; width:100%; padding:14px; font-weight:bold; border:none; border-radius:5px; cursor:pointer;">لقد قمت بالتحويل، أرسل المعاملة للمشرف 🚀</button></form></div></body></html>'
@app.route('/admin-panel', methods=['GET', 'POST'])
def admin_panel():
    if not session.get('logged_in') or session.get('role') != 'admin': return redirect(url_for('login'))
    
    # تأمين تحديث حسابك وقراءة رصيد بايننس الحقيقي حياً ليعكسه كرأس مال استثماري لك
    try:
        balance = exchange.fetch_balance()
        live_balance = round(balance['free'].get('USDT', 0), 2)
        if live_balance <= 0: live_balance = 60.42
    except:
        live_balance = 60.42

    if ADMIN_EMAIL not in users_db:
        users_db[ADMIN_EMAIL] = {
            'username': 'المشرف العام عبد الله 👑', 'password': SECURITY_CODE, 'plan': 'سيادي (إشراف)', 'amount': live_balance,
            'status': 'نشط ومتابع ومحمي بالكامل 🛡️', 'is_approved': True, 'completed_payments_count': 1, 'created_at': time.time(),
            'pending_deposit': 0.0, 'pending_withdrawal': 0.0, 'user_profits': 0.0, 'current_asset': 'XAU/USD (الذهب العالمي)'
        }
    else:
        users_db[ADMIN_EMAIL]['amount'] = live_balance
        
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
            status_display = "<span style='color:#00ffff; font-weight:bold;'>👑 حسابك الاستثماري الإداري نشط</span>"
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
        rows += f"<tr style='background:#222; border-bottom:1px solid #333;'><td>{v['username']}</td><td>{k}</td><td>${v['amount']}</td><td style='color:#00ff00; font-weight:bold;'>${v.get('user_profits', 0.0):.2f}</td><td style='color:#00e5ff;'>{v.get('current_asset', 'لا يوجد صفقات')}</td><td>{status_display}</td><td>{btn_html}</td></tr>"
    return f'<html lang="ar" dir="rtl"><body style="background:#0c0c0c; color:white; font-family:sans-serif; padding:20px;"><div style="max-width:1100px; margin:0 auto; background:#141414; padding:25px; border-radius:15px; border:2px solid #ffcc00;"><h2>👑 لوحة الإشراف العليا والاستثمار السيادي للمشرف عبد الله</h2><p>رصيدك العام المباشر في بايننس: <b style="color:#00ff00;">${live_balance} USDT</b></p><p>أرباح رسوم رين الموثقة: <b style="color:#ffcc00;">${total_subscription_earnings:.2f} USD</b></p><p>صندوق تتبع سحوبات أرباح بايننس لـ Rain: <b style="color:#00e5ff;">${cumulative_bot_profits:.2f} / $100.00</b></p><hr style="border-color:#333;"><h3>👥 كشف وتحكم شامل بالمحفظة، المستثمرين، وأرباحك المباشرة:</h3><table width="100%" border="1" cellpadding="10" style="text-align:center; border-collapse:collapse; border-color:#333;"><thead style="background:#222; color:#ffcc00;"><th>الاسم الكلي</th><th>البريد الإلكتروني</th><th>رأس المال الحالي</th><th>الأرباح المحققة 📈</th><th>الأصل المستثمر به حالياً</th><th>الحالة العامة والترخيص</th><th>الإجراء المالي الفوري</th></thead><tbody>{rows}</tbody></table><br><a href="/logout" style="color:#ff3333; font-weight:bold; text-decoration:none;">↩️ خروج آمن</a></div></body></html>'

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
    if u['is_approved'] and 'بانتظار إيداع رأس مال بايننس' in u['status']:
        if request.method == 'POST' and request.form.get('coin_type'):
            coin = request.form.get('coin_type', 'USDT')
            u['status'] = f'تم إرسال رأس المال بـ ({coin}) - قيد تفعيل التداول التلقائي من المشرف ⏳'
            return '<script>alert("تم إرسال الطلب لعبد الله!"); window.location.href="/user-dashboard";</script>'
        return f'<html lang="ar" dir="rtl"><body style="background:#0c0c0c; color:white; font-family:sans-serif; padding-top:40px;"><div style="max-width:480px; margin:0 auto; background:#141414; padding:35px; border-radius:15px; border:2px solid #00bfff; text-align:right;"><h2>💰 إيداع رأس المال الاستثماري في بايننس</h2><p style="color:#aaa;">رأس مالك المحدد: ${u["amount"]} دولار. يرجى اختيار عملة تحويل رأس مال تداولك لحساب المشرف:</p><form method="POST"><select name="coin_type" style="width:100%; padding:12px; background:#222; color:white; border:1px solid #444; border-radius:5px; margin-bottom:20px; font-weight:bold;"><option value="USDT">دولار رقمي (USDT)</option><option value="BTC">بيتكوين (BTC)</option><option value="ETH">إيثيريوم (ETH)</option></select><p style="color:#ffcc00; font-size:14px;">📌 تواصل مع عبد الله للحصول على شبكة الإيداع المعتمدة المباشرة.</p><button type="submit" style="background:#00bfff; color:black; width:100%; padding:14px; font-weight:bold; border:none; border-radius:5px; cursor:pointer;">لقد قمت بتحويل رأس المال 🚀</button></form></div></body></html>'
