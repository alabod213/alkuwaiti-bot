import os, time, threading, pytz, secrets
from flask import Flask, request, redirect, url_for, session
import ccxt

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', secrets.token_hex(32))

ADMIN_EMAIL = "alabod213@gmail.com"
SECURITY_CODE = "96694489"
RAIN_BITCOIN_ADDRESS = 'bc1q5tga3rmaa6c5lfxtcngueewla22zqdfzr7dcw808zxwad895e42sx79pz0'

exchange = ccxt.binance({
    'apiKey': os.environ.get('BINANCE_API_KEY'), 
    'secret': os.environ.get('BINANCE_API_SECRET'), 
    'enableRateLimit': True, 'options': {'defaultType': 'future'}
})

users_db = {}
total_subscription_earnings = 0.0

def get_ultimate_binance_widget():
    return '''
    <div style="background:#1e1e1e; padding:20px; border-radius:12px; margin-top:20px; border:1px solid #ffcc00; text-align:right;">
        <h3 style="color:#ffcc00; margin:0 0 10px 0; text-align:center;">🌐 رادار منصة بايننس العالمية الفوري (جميع العملات)</h3>
        <p style="color:#aaa; font-size:13px; text-align:center; margin-bottom:15px;">محرك بحث فوري متصل مباشرة بالسوق السحابي التوافقي لعام 2026:</p>
        <input type="text" id="cryptoSearch" onkeyup="searchCrypto()" placeholder="🔍 ابحث عن أي عملة متوفرة في بايننس... (مثال: BTC , SOL , XRP)" style="width:100%; padding:12px; border-radius:8px; border:1px solid #444; background:#111; color:white; font-size:14px; margin-bottom:15px; box-sizing: border-box;">
        <div style="max-height: 350px; overflow-y: auto; border: 1px solid #222; border-radius: 8px;">
            <table id="cryptoTable" width="100%" style="border-collapse:collapse; text-align:center; font-size:14px;">
                <thead style="background:#222; color:#ffcc00; position: sticky; top: 0; z-index:10;">
                    <tr><th style="padding:10px;">الزوج الاستثماري</th><th>السعر الفوري</th></tr>
                </thead>
                <tbody id="market-data-rows">
                    <tr><td colspan="2" style="padding:20px; color:#ffcc00;">🔄 جاري جلب وتهيئة مئات العملات الحية من بايننس...</td></tr>
                </tbody>
            </table>
        </div>
    </div>
    <script>
    async function loadBinanceTickerData() {
        try {
            // التصحيح التاريخي: الاتصال بالرابط البرمجي الرسمي لبايننس لجلب الأسعار الفورية كـ JSON بدلاً من الموقع العام
            const response = await fetch('https://binance.com');
            const data = await response.json();
            const tbody = document.getElementById('market-data-rows');
            let rowsHtml = '';
            const usdtPairs = data.filter(ticker => ticker.symbol.endsWith('USDT')).sort((a, b) => a.symbol.localeCompare(b.symbol));
            usdtPairs.forEach(coin => {
                const cleanName = coin.symbol.replace('USDT', '');
                const rawPrice = parseFloat(coin.price);
                const formattedPrice = rawPrice >= 1 ? rawPrice.toFixed(2) : rawPrice.toFixed(5);
                rowsHtml += `<tr style="background:#141414; border-bottom:1px solid #222;">
                    <td style="padding:10px; font-weight:bold; color:white;">${cleanName} <span style="color:#555; font-size:11px;">/USDT</span></td>
                    <td style="color:#00ff00; font-family:monospace; font-weight:bold;">$${formattedPrice}</td>
                </tr>`;
            });
            tbody.innerHTML = rowsHtml;
        } catch (error) {
            document.getElementById('market-data-rows').innerHTML = '<tr><td colspan="2" style="color:#ff3333; padding:20px;">⚠️ عذراً، حدث خطأ أثناء تحديث رادار العملات الحية.</td></tr>';
        }
    }
    loadBinanceTickerData();
    setInterval(loadBinanceTickerData, 5000);
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
                if (txtValue.toUpperCase().indexOf(filter) > -1) { tr[i].style.display = ""; } else { tr[i].style.display = "none"; }
            }       
        }
    }
    </script>
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
            return redirect(url_for('admin_panel'))
        if email in users_db and code == users_db[email]['password']:
            session['logged_in'] = True; session['role'] = 'user'; session['user_email'] = email
            return redirect(url_for('payment_page'))
        return '<script>alert("بيانات الدخول غير صحيحة!"); window.history.back();</script>'
    
    crypto_widget = get_ultimate_binance_widget()
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
        if email in users_db or email == ADMIN_EMAIL: return '<script>alert("مسجل مسبقاً!"); window.history.back();</script>'
        users_db[email] = {'username': username, 'password': password, 'plan': plan, 'amount': amount, 'status': 'بانتظار دفع الاشتراك ⏳'}
        session['logged_in'] = True; session['role'] = 'user'; session['user_email'] = email
        return redirect(url_for('payment_page'))
    crypto_widget = get_ultimate_binance_widget()
