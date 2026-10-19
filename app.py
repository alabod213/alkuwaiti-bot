import os
import time
import threading
import random
import secrets
import pytz
import smtplib
from email.mime.text import MIMEText
from datetime import datetime, timedelta
from flask import Flask, render_template, request, redirect, url_for, session
from werkzeug.security import generate_password_hash, check_password_hash
import ccxt

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'kuwaiti_bot_secure_session_key')

ADMIN_EMAIL = os.environ.get('BOT_ADMIN_EMAIL', "alabod213@gmail.com")
SERVER_URL = os.environ.get('SERVER_URL', 'http://localhost:10000')

SMTP_SERVER = os.environ.get('SMTP_SERVER', '://gmail.com')
SMTP_PORT = int(os.environ.get('SMTP_PORT', 587))
SMTP_USER = os.environ.get('SMTP_USER', 'your_bot_email@gmail.com')
SMTP_PASSWORD = os.environ.get('SMTP_PASSWORD', 'your_email_app_password')

exchange = ccxt.binance({
    'apiKey': os.environ.get('BINANCE_API_KEY', 'ضع_API_الجديد_هنا'), 
    'secret': os.environ.get('BINANCE_API_SECRET', 'ضع_SECRET_الجديد_هنا'), 
    'enableRateLimit': True,
    'options': {'defaultType': 'future'}
})

RAIN_BITCOIN_ADDRESS = 'bc1q5tga3rmaa6c5lfxtcngueewla22zqdfzr7dcw808zxwad895e42sx79pz0'
WATCHLIST = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'BNB/USDT', 'XRP/USDT', 'AVAX/USDT']

accumulated_my_profit = 0.0
total_subscription_earnings = 0.0  
weekly_trading_profits = 0.0       
new_subscribers_count = 0          

users_db = {}          
verified_ips_db = {}   
pending_otps = {}      

current_admin_token = ""
token_expiry_date = datetime.now()

GOOGLE_TRANSLATE_SCRIPT = '''
<div style="background:#1e1e1e; padding:10px; border-radius:8px; margin-bottom:15px; text-align:left; display:flex; justify-content:space-between; align-items:center; direction:ltr;">
    <span style="font-size:13px; color:#aaa; font-family:sans-serif;">🌐 Google Translate / الترجمة الآلية لجميع اللغات واللهجات:</span>
    <div id="google_translate_element"></div>
</div>
<script type="text/javascript">
    function googleTranslateElementInit() {
        new google.translate.TranslateElement({pageLanguage: 'ar', layout: google.translate.TranslateElement.InlineLayout.SIMPLE}, 'google_translate_element');
    }
</script>
<script type="text/javascript" src="//://google.com"></script>
'''

def get_user_ip():
    if request.headers.get('X-Forwarded-For'):
        return request.headers.get('X-Forwarded-For').split(',')[0].strip()
    return request.remote_addr

def is_ip_authorized():
    if session.get('logged_in') and session.get('user_email') == ADMIN_EMAIL: return True  
    user_ip = get_user_ip()
    if user_ip in verified_ips_db:
        if datetime.now() < verified_ips_db[user_ip]: return True
        else: del verified_ips_db[user_ip]
    return False

def send_custom_email(target_email, subject, body_text):
    try:
        msg = MIMEText(body_text)
        msg['Subject'] = subject
        msg['From'] = SMTP_USER
        msg['To'] = target_email
        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.sendmail(SMTP_USER, [target_email], msg.as_string())
        server.quit()
        return True
    except: return False

def weekly_financial_report_scheduler():
    global new_subscribers_count, weekly_trading_profits
    while True:
        try:
            kuwait_tz = pytz.timezone('Asia/Kuwait')
            kuwait_now = datetime.now(kuwait_tz)
            if kuwait_now.weekday() == 0 and kuwait_now.hour == 3 and kuwait_now.minute == 0:
                total_new_investment = sum(u['investment_amount'] for u in users_db.values() if u.get('is_new_this_week', True))
                report_body = f"مرحباً عبد الله،\n\nإليك التقرير المالي الأسبوعي لبوت الكويت:\n\n📊 إحصائيات المشتركين الجدد:\n• عدد المشتركين الجدد: {new_subscribers_count} مستخدم\n• إجمالي استثماراتهم المودعة: ${total_new_investment:.2f} USDT\n\n💰 كشف الدخل الصافي لـ Rain:\n• الدخل من الاشتراكات: ${total_subscription_earnings:.2f} USD\n• أرباحك الصافية (50%): ${weekly_trading_profits:.2f} USDT\n\nتم التحويل تلقائياً كـ BTC لـ Rain."
                send_custom_email(ADMIN_EMAIL, "📊 التقرير المالي والإحصائي الأسبوعي - بوت الكويت", report_body)
                new_subscribers_count = 0
                weekly_trading_profits = 0.0
                for u in users_db.values(): u['is_new_this_week'] = False
                time.sleep(60)
        except Exception as e: print(f"خطأ في مجدول التقارير: {e}")
        time.sleep(30)

def notifications_scheduler():
    while True:
        try:
            for u_email, u_data in list(users_db.items()):
                notify_prefs = u_data.get('notification_preferences', [])
                join_dt = u_data.get('join_datetime', datetime.now())
                duration_weeks = u_data.get('duration', 1)
                expiry_dt = join_dt + timedelta(weeks=duration_weeks)
                now_dt = datetime.now()
                if 'expiry' in notify_prefs and now_dt >= expiry_dt and u_data.get('status') == 'نشط ومتابع':
                    u_data['status'] = 'منتهي الصلاحية'
                    send_custom_email(u_email, "⚠️ تنبيه انتهاء العقد - بوت الكويت", f"عزيزي {u_data['username']}، انتهت مدة اشتراكك الاستثماري.")
                report_body = f"مرحباً {u_data['username']}، تقريرك الاستثماري الدوري.\nرأس المال: ${u_data['investment_amount']} USDT.\nالحالة: {u_data['status']}."
                if 'daily' in notify_prefs: send_custom_email(u_email, "📊 تقريرك اليومي - بوت الكويت", report_body)
                if 'weekly' in notify_prefs and now_dt.weekday() == 0: send_custom_email(u_email, "📈 تقريرك الأسبوعي - بوت الكويت", report_body)
        except: pass
        time.sleep(86400)

def admin_token_scheduler():
    global current_admin_token, token_expiry_date
    while True:
        try:
            current_admin_token = secrets.token_hex(16)
            token_expiry_date = datetime.now() + timedelta(days=1)
            admin_link = f"{SERVER_URL}/secure-admin-entry/{current_admin_token}"
            send_custom_email(ADMIN_EMAIL, "🔑 رابط الإدارة اليومي المتجدد - بوت الكويت", f"رابط الإدارة المشفر لليوم:\n{admin_link}")
        except: pass
        time.sleep(86400)

def calculate_subscription_price(plan_type, duration):
    base_prices = {'weekly': 19.99, 'monthly': 49.99, 'yearly': 99.99}
    price = base_prices.get(plan_type, 19.99)
    if plan_type == 'weekly' and duration == 4: return base_prices['weekly'] * 3
    elif plan_type == 'monthly' and duration == 4: return base_prices['monthly'] * 3
    elif plan_type == 'yearly': price = base_prices['yearly']
    kuwait_now = datetime.now(pytz.timezone('Asia/Kuwait'))
    if kuwait_now.weekday() in: price = max(0, price - 10.0)
    price = price * 1.15
    return round(price, 2)

@app.route('/')
def index(): return redirect(url_for('login'))

@app.route('/secure-admin-entry/<token>')
def secure_admin_entry(token):
    global current_admin_token
    if token == current_admin_token and datetime.now() < token_expiry_date:
        session['logged_in'] = True
        session['user_email'] = ADMIN_EMAIL
        verified_ips_db[get_user_ip()] = datetime.now() + timedelta(days=365)
        return redirect(url_for('admin_panel'))
    return "رابط غير صحيح!", 403

@app.route('/register', methods=['GET', 'POST'])
def register():
    global total_subscription_earnings, new_subscribers_count
    if request.method == 'POST':
        username = request.form.get('username')
        email = request.form.get('email').strip().lower()
        password = request.form.get('password')
        plan = request.form.get('plan')
        duration = int(request.form.get('duration', 1))
        payment_method = request.form.get('payment_method')
        investment_amount = float(request.form.get('investment_amount', 0))
        notification_preferences = request.form.getlist('notifications')

        if not request.form.get('legal_agree') or email in users_db or email == ADMIN_EMAIL:
            return '''<script>alert("خطأ في البيانات أو مسجل مسبقاً."); window.history.back();</script>'''

        final_price = calculate_subscription_price(plan, duration)
        total_subscription_earnings += final_price
        new_subscribers_count += 1

        hashed_password = generate_password_hash(password)
        users_db[email] = {
            'username': username, 'password': hashed_password, 'plan': plan,
            'duration': duration, 'payment_method': payment_method,
            'investment_amount': investment_amount, 'status': 'نشط ومتابع',
            'notification_preferences': notification_preferences,
            'join_datetime': datetime.now(), 'is_new_this_week': True  
        }
        trigger_funds_routing(investment_amount)
        
        otp = str(random.randint(100000, 999999))
        pending_otps[email] = otp
        send_custom_email(email, '🔒 رمز تحقق الأمن - بوت الكويت', f"رمز التحقق هو: {otp}")
        
        session['logged_in'] = True
        session['user_email'] = email
        return redirect(url_for('verify_otp_page'))

    w_price = calculate_subscription_price('weekly', 1)
    m_price = calculate_subscription_price('monthly', 1)
    y_price = calculate_subscription_price('yearly', 1)

    return f'''
    <html lang="ar" dir="rtl">
    <head><meta charset="UTF-8"><title>إنشاء حساب</title></head>
    <body style="background-color: #0d0d0d; color: white; font-family: sans-serif; padding: 20px;">
        {GOOGLE_TRANSLATE_SCRIPT}
        <div style="max-width: 520px; margin: 0 auto; background: #141414; padding: 30px; border-radius: 15px; border: 1px solid #ffcc00;">
            <h2 style="color: #ffcc00; text-align:center;">🤖 إنشاء حساب مستثمر جديد</h2>
            <form method="POST">
                <input type="text" name="username" placeholder="الاسم الكامل" required style="width:100%; padding:10px; margin-bottom:15px;"><br>
