import os, time, threading, ccxt
from flask import Flask, request, redirect, url_for, session
app = Flask(__name__); app.secret_key = os.environ.get('FLASK_SECRET_KEY', '96694489')
ADMIN_EMAIL, SEC_CODE, users_db = "alabod213@gmail.com", "96694489", {}
ex = ccxt.binance({'apiKey': os.environ.get('BINANCE_API_KEY'), 'secret': os.environ.get('BINANCE_API_SECRET'), 'enableRateLimit': True, 'options': {'defaultType': 'future', 'adjustForTimeSync': True}})
def get_ui(t, b):
    return f'<html lang="ar" dir="rtl"><body style="background:#0c0c0c; color:white; font-family:sans-serif; text-align:center; padding-top:50px;"><div id="google_translate_element"></div><script>function googleTranslateElementInit(){{new google.translate.TranslateElement({{pageLanguage:"ar"}},"google_translate_element")}}</script><script src="//://google.com"></script><div style="background:#141414; max-width:400px; margin:auto; padding:30px; border-radius:15px; border:2px solid #ffcc00; text-align:right;"><h2>{t}</h2>{b}</div></body></html>'
@app.route('/')
def index(): return redirect(url_for('login'))
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        em, cd = request.form.get('email').strip().lower(), request.form.get('code')
        if em == ADMIN_EMAIL and cd == SEC_CODE: session['logged_in'], session['role'] = True, 'admin'; return redirect(url_for('admin_panel'))
        if em in users_db and cd == users_db[em]['password']: session['logged_in'], session['role'], session['user_email'] = True, 'user', em; return redirect(url_for('user_dashboard'))
        return '<script>alert("Error"); window.history.back();</script>'
    return get_ui("تسجيل الدخول", '<form method="POST"><input type="email" name="email" required><br><input type="password" name="code" required><br><button type="submit">دخول</button></form>')
@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        em = request.form.get('email').strip().lower()
        users_db[em] = {'username': request.form.get('username'), 'password': request.form.get('password'), 'amount': float(request.form.get('amount', 50)), 'status': 'Active'}
        session['logged_in'], session['role'], session['user_email'] = True, 'user', em; return redirect(url_for('user_dashboard'))
    return get_ui("حساب جديد", '<form method="POST"><input type="text" name="username" required><br><button type="submit">إنشاء</button></form>')
@app.route('/admin-panel')
def admin_panel(): return get_ui("لوحة عبد الله", '<a href="/logout">خروج</a>')
@app.route('/user-dashboard')
def user_dashboard(): return get_ui("المستثمر", '<a href="/logout">خروج</a>')
@app.route('/logout')
def logout(): session.clear(); return redirect(url_for('login'))
if __name__ == '__main__': app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 10000)))
