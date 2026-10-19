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

# إعدادات المشرف عبد الله الثابتة
ADMIN_EMAIL = os.environ.get('BOT_ADMIN_EMAIL', "alabod213@gmail.com")
SERVER_URL = os.environ.get('SERVER_URL', 'http://localhost:10000')

# إعدادات خادم البريد الإلكتروني (SMTP) لضمان وصول الرسائل للمشتركين وعبد الله
SMTP_SERVER = os.environ.get('SMTP_SERVER', '://gmail.com')
SMTP_PORT = int(os.environ.get('SMTP_PORT', 587))
SMTP_USER = os.environ.get('SMTP_USER', 'your_bot_email@gmail.com')
SMTP_PASSWORD = os.environ.get('SMTP_PASSWORD', 'your_email_app_password')

# ربط بايننس الآمن
exchange = ccxt.binance({
    'apiKey': os.environ.get('BINANCE_API_KEY', 'ضع_API_الجديد_هنا'), 
    'secret': os.environ.get('BINANCE_API_SECRET', 'ضع_SECRET_الجديد_هنا'), 
    'enableRateLimit': True,
    'options': {'defaultType': 'future'}
})

RAIN_BITCOIN_ADDRESS = 'bc1q5tga3rmaa6c5lfxtcngueewla22zqdfzr7dcw808zxwad895e42sx79pz0'
RAIN_USDT_ADDRESS = os.environ.get('RAIN_USDT_ADDRESS', 'ضع_عنوان_محفظة_USDT_الخاص_بك_في_رين_هنا')
WATCHLIST = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'BNB/USDT', 'XRP/USDT', 'AVAX/USDT']

# متغيرات النظام المالية والأمنية الحية
accumulated_my_profit = 0.0
total_subscription_earnings = 0.0  # إجمالي الدخل التراكمي المحقق من رسوم الاشتراكات
weekly_trading_profits = 0.0       # إجمالي أرباح التداول المحققة خلال الأسبوع الحالي
new_subscribers_count = 0          # عداد المشتركين المستجدين خلال الأسبوع

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
        return request.headers.get('X-Forwarded-For').split(',').strip()
    return request.remote_addr

def is_ip_authorized():
    if session.get('logged_in') and session.get('user_email') == ADMIN_EMAIL:
        return True  
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
    except:
        return False

# 🕒 محرك حساب وإرسال التقرير المالي المالي الأسبوعي المجدول في تمام الساعة 3 فجراً بتوقيت الكويت
def weekly_financial_report_scheduler():
    global new_subscribers_count, weekly_trading_profits
    while True:
        try:
            # التحقق من التوقيت المحلي لدولة الكويت
            kuwait_tz = pytz.timezone('Asia/Kuwait')
            kuwait_now = datetime.now(kuwait_tz)
            
            # الشرط: يوم الاثنين (weekday == 0) في تمام الساعة 3 فجراً (hour == 3 و minute == 0)
            if kuwait_now.weekday() == 0 and kuwait_now.hour == 3 and kuwait_now.minute == 0:
                
                # حساب إجمالي حجم رؤوس أموال الاستثمارات الحالية للمشتركين الجدد
                total_new_investment = sum(u['investment_amount'] for u in users_db.values() if u.get('is_new_this_week', True))
                
                # صياغة التقرير المالي المبسط المخصص للمشرف عبد الله
                report_body = f"""
                مرحباً عبد الله،
                
                إليك التقرير المالي والإحصائي الأسبوعي لبوت الكويت (مجدول تلقائياً الساعة 3:00 صباحاً):
                
                📊 إحصائيات المشتركين الجدد والاستثمارات:
                • عدد المشتركين الجدد هذا الأسبوع: {new_subscribers_count} مستخدم
                • إجمالي رؤوس أموال استثماراتهم المودعة في بايننس: ${total_new_investment:.2f} USDT
                
                💰 كشف الحساب والدخل الصافي:
                • الدخل الصافي المحقق من رسوم الاشتراكات: ${total_subscription_earnings:.2f} USD (تم تحويلها لـ Rain)
                • أرباحك الصافية المحققة من صفقات التداول (نسبة الـ 50%): ${weekly_trading_profits:.2f} USDT
                
                دمت بأمان،
                نظام التقارير المالية الآلي - بوت الكويت.
                """
                
                # إرسال التقرير فوراً لبريد عبد الله
                send_custom_email(ADMIN_EMAIL, "📊 التقرير المالي والإحصائي الأسبوعي - بوت الكويت", report_body)
                
                # تصفير العدادات الأسبوعية لبدء دورة حسابية جديدة للأسبوع القادم
                new_subscribers_count = 0
                weekly_trading_profits = 0.0
                for u in users_db.values():
                    u['is_new_this_week'] = False
                    
                time.sleep(60) # تلافي تكرار الإرسال في نفس الدقيقة
        except Exception as e:
            print(f"خطأ في مجدول التقارير الأسبوعية: {e}")
        time.sleep(30) # الفحص الدوري للتوقيت كل 30 ثانية

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
                
                report_body = f"مرحباً {u_data['username']}، تقريرك الاستثماري المخصص من بوت الكويت.\nرأس المال: ${u_data['investment_amount']} USDT.\nالحالة: {u_data['status']}."
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
def index():
    return redirect(url_for('login'))

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
        
        # إضافة المدخول لعداد الاشتراكات التراكمي وزيادة عدد المشتركين الجدد للأسبوع
