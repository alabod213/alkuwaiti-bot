import os, time, threading, pytz, secrets, random
from flask import Flask, request, redirect, url_for, session
import ccxt

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'kuwaiti_bot_secure_key')

# إعدادات المشرف عبد الله الثابتة والأمنية
ADMIN_EMAIL = "alabod213@gmail.com"
SECURITY_CODE = "96694489"
RAIN_BITCOIN_ADDRESS = 'bc1q5tga3rmaa6c5lfxtcngueewla22zqdfzr7dcw808zxwad895e42sx79pz0'
WATCHLIST = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'BNB/USDT', 'XRP/USDT', 'AVAX/USDT']

# ربط بايننس الآمن عبر خادم الاستضافة
exchange = ccxt.binance({
    'apiKey': os.environ.get('BINANCE_API_KEY'), 
    'secret': os.environ.get('BINANCE_API_SECRET'), 
    'enableRateLimit': True, 'options': {'defaultType': 'future'}
})

# قواعد البيانات الحية للمشتركين والنظام المالي
users_db = {}
weekly_trading_profits = 0.0
total_subscription_earnings = 0.0
new_subscribers_count = 0

GOOGLE_TRANSLATE_SCRIPT = '''
<div style="background:#1e1e1e; padding:10px; border-radius:8px; margin-bottom:15px; text-align:left; display:flex; justify-content:space-between; align-items:center; direction:ltr;">
    <span style="font-size:13px; color:#aaa; font-family:sans-serif;">🌐 Google Translate / الترجمة الآلية لجميع اللغات:</span>
    <div id="google_translate_element"></div>
</div>
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

# 🔓 صفحة تسجيل الدخول الموحدة (تدعم دخولك وتدعم دخول المشتركين)
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email').strip().lower()
        code = request.form.get('code')
        
        # 1. تحقق دخول المشرف عبد الله
        if email == ADMIN_EMAIL and code == SECURITY_CODE:
            session['logged_in'] = True
            session['role'] = 'admin'
            return redirect(url_for('admin_panel'))
            
        # 2. تحقق دخول المشتركين العاديين
        if email in users_db and code == users_db[email]['password_plain']:
            session['logged_in'] = True
            session['role'] = 'user'
            session['user_email'] = email
            return redirect(url_for('user_dashboard'))
            
        return '''<script>alert("بيانات الدخول غير صحيحة!"); window.history.back();</script>'''

    return f'''
    <html lang="ar" dir="rtl">
    <head><meta charset="UTF-8"><title>تسجيل الدخول - نظام الكويت</title></head>
    <body style="background-color: #0c0c0c; color: white; font-family: sans-serif; text-align: center; padding-top: 80px;">
        {GOOGLE_TRANSLATE_SCRIPT}
        <div style="background: #141414; width: 360px; margin: 0 auto; padding: 30px; border-radius: 15px; border: 2px solid #ffcc00; text-align:right;">
            <h2 style="color: #ffcc00; text-align:center;">🔓 بوابة تسجيل الدخول</h2>
            <form method="POST">
                <input type="email" name="email" placeholder="البريد الإلكتروني" required style="width:100%; padding:11px; margin-bottom:15px; background:#222; color:white; border:1px solid #444;"><br>
                <input type="password" name="code" placeholder="كلمة السر أو كود الأمان" required style="width:100%; padding:11px; margin-bottom:20px; background:#222; color:white; border:1px solid #444;"><br>
                <button type="submit" style="background:#ffcc00; color:black; width:100%; padding:12px; font-weight:bold; border:none; border-radius:5px; cursor:pointer;">دخول آمن 🔐</button>
            </form>
            <p style="text-align:center; margin-top:15px; font-size:13px;"><a href="/register" style="color:#ffcc00; text-decoration:none;">ليس لديك حساب؟ سجل كمشترك جديد هنا 🚀</a></p>
        </div>
    </body>
    </html>
    '''

# 🚀 صفحة تسجيل المشتركين الجدد بجميع لغات العالم واختيار تفضيلات الرسائل
@app.route('/register', methods=['GET', 'POST'])
def register():
    global new_subscribers_count, total_subscription_earnings
    if request.method == 'POST':
        username = request.form.get('username')
        email = request.form.get('email').strip().lower()
        password = request.form.get('password')
        plan = request.form.get('plan')
        duration = int(request.form.get('duration', 1))
        investment_amount = float(request.form.get('investment_amount', 0))
        notification_preferences = request.form.getlist('notifications')

        if email in users_db or email == ADMIN_EMAIL:
            return '''<script>alert("هذا البريد الإلكتروني مسجل مسبقاً!"); window.history.back();</script>'''

        # احتساب سعر الاشتراك الافتراضي وإضافته لعداد أرباحك
        base_prices = {'weekly': 19.99, 'monthly': 49.99, 'yearly': 99.99}
        sub_price = base_prices.get(plan, 19.99) * duration
        total_subscription_earnings += sub_price
        new_subscribers_count += 1

        # حفظ بيانات المستثمر الجديد في النظام
        users_db[email] = {
            'username': username, 'password_plain': password, 'plan': plan,
            'duration': duration, 'investment_amount': investment_amount,
            'status': 'نشط ومتابع ومحمي', 'notification_preferences': notification_preferences,
            'join_date': datetime.now().strftime('%Y-%m-%d %H:%M'), 'is_new_this_week': True
        }
        
        session['logged_in'] = True
        session['role'] = 'user'
        session['user_email'] = email
        return redirect(url_for('user_dashboard'))

    return f'''
    <html lang="ar" dir="rtl">
    <head><meta charset="UTF-8"><title>إنشاء حساب مستثمر جديد</title></head>
    <body style="background-color: #0d0d0d; color: white; font-family: sans-serif; padding: 20px;">
        {GOOGLE_TRANSLATE_SCRIPT}
        <div style="max-width: 500px; margin: 0 auto; background: #141414; padding: 30px; border-radius: 15px; border: 1px solid #ffcc00; text-align:right;">
            <h2 style="color: #ffcc00; text-align:center;">🤖 إنشاء حساب مستثمر جديد</h2>
            <form method="POST">
                <input type="text" name="username" placeholder="الاسم الكامل" required style="width:100%; padding:10px; margin-bottom:15px; background:#222; color:white; border:1px solid #444;"><br>
                <input type="email" name="email" placeholder="البريد الإلكتروني" required style="width:100%; padding:10px; margin-bottom:15px; background:#222; color:white; border:1px solid #444;"><br>
                <input type="password" name="password" placeholder="اختر كلمة السر الخاصة بك" required style="width:100%; padding:10px; margin-bottom:15px; background:#222; color:white; border:1px solid #444;"><br>
                
                <label>باقة الاستثمار التلقائي:</label>
                <select name="plan" style="width:100%; padding:10px; margin-bottom:15px; background:#222; color:white; border:1px solid #444;"><option value="weekly">أسبوعي</option><option value="monthly">شهري</option><option value="yearly">سنوي</option></select><br>
                
                <input type="number" name="duration" placeholder="عدد باقات العقد (مثال: 1)" value="1" min="1" required style="width:100%; padding:10px; margin-bottom:15px; background:#222; color:white; border:1px solid #444;"><br>
                <input type="number" name="investment_amount" placeholder="مبلغ رأس المال المراد استثماره ($)" required style="width:100%; padding:10px; margin-bottom:20px; background:#222; color:white; border:1px solid #444;"><br>
                
                <label style="color:#00bfff; font-weight:bold;">✉️ رغبتك وتفضيلاتك لتلقي تقارير الرسائل الدورية:</label><br>
                <input type="checkbox" name="notifications" value="daily" checked> أريد تقرير يومي مبسط على شاشتي<br>
                <input type="checkbox" name="notifications" value="weekly"> أريد تقرير أسبوعي شامل<br>
                <input type="checkbox" name="notifications" value="expiry" checked> تنبيهي فور انتهاء اشتراكي مباشرة<br><br>
                
                <button type="submit" style="background:#ffcc00; color:black; width:100%; padding:12px; font-weight:bold; border:none; border-radius:5px; cursor:pointer;">إنشاء الحساب وبدء الربط 🚀</button>
            </form>
            <p style="text-align:center;"><a href="/login" style="color:#ffcc00; text-decoration:none;">لديك حساب بالفعل؟ سجل دخولك</a></p>
        </div>
    </body>
    </html>
    '''

# 👑 لوحة التحكم الكبرى المخصصة للمشرف عبد الله (تستعرض إحصائيات بايننس والمشتركين والتقارير المالية)
@app.route('/admin-panel')
def admin_panel():
    if not session.get('logged_in') or session.get('role') != 'admin': return redirect(url_for('login'))
    try:
        balance = exchange.fetch_balance()
        free_usdt = round(balance['free'].get('USDT', 0), 2)
    except: free_usdt = "خطأ في الاتصال ببايننس"

    # جلب صفوف المشتركين المسجلين لعرضهم أمام عبد الله
    users_rows = ""
    for u_email, u_data in users_db.items():
        users_rows += f'''
        <tr style="background:#222; border-bottom:1px solid #333;">
            <td style="padding:10px;">{u_data['username']}</td>
            <td style="padding:10px;">{u_email}</td>
            <td style="padding:10px; color:#ffcc00;">{u_data['plan']}</td>
            <td style="padding:10px; color:#00ff00;">${u_data['investment_amount']}</td>
            <td style="padding:10px;">{u_data['join_date']}</td>
            <td style="padding:10px; color:#00bfff;">{u_data['status']}</td>
        </tr>
        '''
    if not users_db:
        users_rows = '<tr><td colspan="6" style="padding:15px; color:#666;">لا يوجد مستثمرون مسجلون حالياً.</td></tr>'

    return f'''
    <html lang="ar" dir="rtl">
    <head><meta charset="UTF-8"><title>لوحة إشراف عبد الله</title></head>
