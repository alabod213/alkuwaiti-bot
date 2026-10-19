import os, time, threading, pytz, secrets
from flask import Flask, request, redirect, url_for, session
import ccxt

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'kuwaiti_bot_secure_key')

ADMIN_EMAIL = "alabod213@gmail.com"
SECURITY_CODE = "96694489"
RAIN_BITCOIN_ADDRESS = 'bc1q5tga3rmaa6c5lfxtcngueewla22zqdfzr7dcw808zxwad895e42sx79pz0'

# قائمة منسقة لأقوى وأكثر 30 عملة تداولاً في بايننس لضمان سرعة الخادم الفائقة وعدم الانهيار
TOP_30_MARKETS = [
    'BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'BNB/USDT', 'XRP/USDT',
    'ADA/USDT', 'DOGE/USDT', 'DOT/USDT', 'MATIC/USDT', 'SHIB/USDT',
    'AVAX/USDT', 'TRX/USDT', 'LINK/USDT', 'UNI/USDT', 'LTC/USDT',
    'BCH/USDT', 'ATOM/USDT', 'XLM/USDT', 'FIL/USDT', 'ETC/USDT',
    'NEAR/USDT', 'APT/USDT', 'OP/USDT', 'ARB/USDT', 'INJ/USDT',
    'TIA/USDT', 'SUI/USDT', 'ORDI/USDT', 'RUNE/USDT', 'IMX/USDT'
]

exchange = ccxt.binance({
    'apiKey': os.environ.get('BINANCE_API_KEY'), 
    'secret': os.environ.get('BINANCE_API_SECRET'), 
    'enableRateLimit': True, 'options': {'defaultType': 'future'}
})

users_db = {}
total_subscription_earnings = 0.0

# محرك جلب سريع ومحمي مخصص لـ 30 عملة قيادية مع ميزة البحث السريع
def get_binance_markets_widget():
    html = '''
    <div style="background:#1e1e1e; padding:20px; border-radius:12px; margin-top:20px; border:1px solid #ffcc00; text-align:right;">
        <h3 style="color:#ffcc00; margin:0 0 10px 0; text-align:center;">🌐 رادار حركة العملات القيادية الحية في بايننس</h3>
        <p style="color:#aaa; font-size:13px; text-align:center; margin-bottom:15px;">البحث الفوري الذكي بالثانية داخل أقوى 30 عملة استثمارية:</p>
        
        <input type="text" id="cryptoSearch" onkeyup="searchCrypto()" placeholder="🔍 اكتب اسم العملة هنا... (مثال: SOL , DOGE)" 
            style="width:100%; padding:12px; border-radius:8px; border:1px solid #444; background:#111; color:white; font-size:14px; margin-bottom:15px; box-sizing: border-box;">
        
        <div style="max-height: 300px; overflow-y: auto; border: 1px solid #222; border-radius: 8px;">
            <table id="cryptoTable" width="100%" style="border-collapse:collapse; text-align:center; font-size:14px;">
                <thead style="background:#222; color:#ffcc00; position: sticky; top: 0;">
                    <tr>
                        <th style="padding:10px;">العملة</th>
                        <th>السعر الفوري</th>
                        <th>تغير 24 ساعة</th>
                    </tr>
                </thead>
                <tbody>
    '''
    try:
        for symbol in TOP_30_MARKETS:
            ticker = exchange.fetch_ticker(symbol)
            price = round(ticker['last'], 2) if ticker['last'] >= 1 else round(ticker['last'], 4)
            change = round(ticker['percentage'], 2) if ticker['percentage'] is not None else 0.0
            color = '#00ff00' if change >= 0 else '#ff3333'
            sign = '+' if change >= 0 else ''
            
            html += f'''
            <tr style="background:#141414; border-bottom:1px solid #222;">
                <td style="padding:10px; font-weight:bold; color:white;">{symbol.replace('/USDT','')} <span style="color:#666; font-size:11px;">/USDT</span></td>
                <td style="color:#00ff00; font-family:monospace;">${price}</td>
                <td style="color:{color}; font-weight:bold; font-family:monospace;">{sign}{change}%</td>
            </tr>
            '''
    except Exception as e:
        html += '<tr><td colspan="3" style="padding:20px; color:#aaa;">جاري جلب نبض السوق المباشر...</td></tr>'
        
    html += '''
                </tbody>
            </table>
        </div>
    </div>
    
    <script>
    function searchCrypto() {
        var input, filter, table, tr, td, i, txtValue;
        input = document.getElementById("cryptoSearch");
        filter = input.value.toUpperCase();
        table = document.getElementById("cryptoTable");
        tr = table.getElementsByTagName("tr");
        
        for (i = 1; i < tr.length; i++) {
            td = tr[i].getElementsByTagName("td");
            if (td[0]) {
                txtValue = td[0].textContent || td[0].innerText;
                if (txtValue.toUpperCase().indexOf(filter) > -1) {
                    tr[i].style.display = "";
                } else {
                    tr[i].style.display = "none";
                }
            }       
        }
    }
    </script>
    '''
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
            session['logged_in'] = True
            session['role'] = 'admin'
            return redirect(url_for('admin_panel'))
        if email in users_db and code == users_db[email]['password']:
            session['logged_in'] = True
            session['role'] = 'user'
            session['user_email'] = email
            return redirect(url_for('payment_page'))
        return '<script>alert("بيانات الدخول غير صحيحة!"); window.history.back();</script>'
    
    crypto_widget = get_binance_markets_widget()
    return f'<html lang="ar" dir="rtl"><body style="background:#0c0c0c; color:white; font-family:sans-serif; text-align:center; padding-top:40px; padding-left:15px; padding-right:15px;"><div style="background:#141414; max-width:480px; margin:0 auto; padding:30px; border-radius:15px; border:2px solid #ffcc00; text-align:right;"><h2>🔓 بوابة تسجيل الدخول</h2><form method="POST"><input type="email" name="email" placeholder="البريد الإلكتروني" required style="width:100%; padding:10px; margin-bottom:15px;"><br><input type="password" name="code" placeholder="كلمة السر أو كود الأمان" required style="width:100%; padding:10px; margin-bottom:20px;"><br><button type="submit" style="background:#ffcc00; color:black; width:100%; padding:12px; font-weight:bold; border:none; border-radius:5px; cursor:pointer; width:100%;">دخول آمن 🔐</button></form><p style="text-align:center; font-size:13px; margin-top:15px;"><a href="/register" style="color:#ffcc00; text-decoration:none;">سجل كمشترك جديد هنا 🚀</a> | <a href="/forgot-password" style="color:#aaa; text-decoration:none;">نسيت كلمة المرور؟ 🔍</a></p></div><div style="max-width:480px; margin:0 auto;">{crypto_widget}</div></body></html>'

@app.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    if request.method == 'POST':
        email = request.form.get('email').strip().lower()
        if email == ADMIN_EMAIL:
            return f'<script>alert("أهلاً بالمشرف عبد الله، كود أمانك الثابت هو: {SECURITY_CODE}"); window.location.href="/login";</script>'
        if email in users_db:
            user_pass = users_db[email]['password']
            return f'<html lang="ar" dir="rtl"><body style="background:#0c0c0c; color:white; font-family:sans-serif; text-align:center; padding-top:100px;"><div style="background:#141414; width:380px; margin:0 auto; padding:30px; border-radius:15px; border:1px solid #00bfff; text-align:right;"><h3>🔑 استعادة الحساب الموثق</h3><p>كلمة المرور الخاصة بك هي: <b style="color:#00ff00; font-size:18px;">{user_pass}</b></p><br><a href="/login" style="background:#00bfff; color:black; padding:10px 20px; text-decoration:none; font-weight:bold; border-radius:5px; display:block; text-align:center;">العودة لتسجيل الدخول 🔓</a></div></body></html>'
        return '<script>alert("هذا البريد الإلكتروني غير مسجل لدينا!"); window.history.back();</script>'
    return '<html lang="ar" dir="rtl"><body style="background:#0c0c0c; color:white; font-family:sans-serif; text-align:center; padding-top:120px;"><div style="background:#141414; width:350px; margin:0 auto; padding:30px; border-radius:15px; border:1px solid #ffcc00; text-align:right;"><h2>🔍 استعادة كلمة المرور</h2><form method="POST"><input type="email" name="email" placeholder="بريدك الإلكتروني الحقيقي" required style="width:100%; padding:10px; margin-bottom:20px;"><br><button type="submit" style="background:#ffcc00; color:black; width:100%; padding:12px; font-weight:bold; border:none; border-radius:5px; cursor:pointer;">التحقق واستعادة الحساب 🛡️</button></form></div></body></html>'

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username')
        email = request.form.get('email').strip().lower()
        password = request.form.get('password')
        plan = request.form.get('plan')
        amount = float(request.form.get('amount', 0))
        
        if email in users_db or email == ADMIN_EMAIL: 
            return '<script>alert("مسجل مسبقاً!"); window.history.back();</script>'
            
        # حفظ الحساب وتوجيهه الآمن
        users_db[email] = {'username': username, 'password': password, 'plan': plan, 'amount': amount, 'status': 'بانتظار دفع الاشتراك ⏳'}
        session['logged_in'] = True
        session['role'] = 'user'
        session['user_email'] = email
        return redirect(url_for('payment_page'))
        
    crypto_widget = get_binance_markets_widget()
