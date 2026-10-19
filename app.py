import os, time, threading, random, secrets, pytz, smtplib
from email.mime.text import MIMEText
from datetime import datetime, timedelta
from flask import Flask, request, redirect, url_for, session
import ccxt

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'kuwaiti_bot_secure_key')

ADMIN_EMAIL = "alabod213@gmail.com"
SERVER_URL = "https://onrender.com"

# جعل البوت يقرأ بيانات البريد من السيرفر بأمان ودقة وبدون أخطاء نقل
SMTP_USER = os.environ.get('SMTP_USER', 'alabod213@gmail.com')
SMTP_PASSWORD = os.environ.get('SMTP_PASSWORD', 'gdxeeeonfhdcsqao')
RAIN_ADDRESS = 'bc1q5tga3rmaa6c5lfxtcngueewla22zqdfzr7dcw808zxwad895e42sx79pz0'

exchange = ccxt.binance({
    'apiKey': os.environ.get('BINANCE_API_KEY'), 
    'secret': os.environ.get('BINANCE_API_SECRET'), 
    'enableRateLimit': True, 'options': {'defaultType': 'future'}
})

users_db = {}
verified_ips = {}
current_admin_token = ""
token_expiry = datetime.now()

def send_email(to, subject, body):
    try:
        msg = MIMEText(body)
        msg['Subject'] = subject
        msg['From'] = SMTP_USER
        msg['To'] = to
        server = smtplib.SMTP("://gmail.com", 587)
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.sendmail(SMTP_USER, [to], msg.as_string())
        server.quit()
        print("✅ تم إرسال الإيميل بنجاح واختراق الشبكة الموثوقة!")
    except Exception as e:
        print(f"⚠️ فشل إرسال الإيميل بسبب خطأ في الرموز بالسيرفر: {e}")

@app.route('/')
def index(): return "<h2>🤖 بوت الكويت يعمل بنجاح ومستعد للاستثمار!</h2>"

@app.route('/secure-admin-entry/<token>')
def secure_admin_entry(token):
    global current_admin_token
    if token == current_admin_token and datetime.now() < token_expiry:
        session['logged_in'] = True
        verified_ips[request.remote_addr] = datetime.now() + timedelta(days=365)
        return "<h2>🎯 مرحباً عبد الله، تم الدخول بأمان وتفويض جهازك.</h2>"
    return "رابط منتهي الصلاحية!", 403

def admin_token_scheduler():
    global current_admin_token, token_expiry
    # إرسال الرابط الأول فوراً عند تشغيل السيرفر دون انتظار الـ 24 ساعة
    time.sleep(10)
    try:
        current_admin_token = secrets.token_hex(16)
        token_expiry = datetime.now() + timedelta(days=1)
        link = f"{SERVER_URL}/secure-admin-entry/{current_admin_token}"
        send_email(ADMIN_EMAIL, "🔑 رابط الإدارة اليومي - بوت الكويت", f"رابط الدخول لليوم:\n{link}")
    except: pass

    while True:
        try:
            current_admin_token = secrets.token_hex(16)
            token_expiry = datetime.now() + timedelta(days=1)
            link = f"{SERVER_URL}/secure-admin-entry/{current_admin_token}"
            send_email(ADMIN_EMAIL, "🔑 رابط الإدارة اليومي - بوت الكويت", f"رابط الدخول لليوم:\n{link}")
        except: pass
        time.sleep(86400)

def auto_trading_engine():
    while True:
        try:
            balance = exchange.fetch_balance()
            free_balance = balance['free'].get('USDT', 0)
            if free_balance > 20:
                ticker = exchange.fetch_ticker('BTC/USDT')
                amount = (free_balance * 0.05) / ticker['last']
                amount_fmt = exchange.amount_to_precision('BTC/USDT', amount)
                if float(amount_fmt) > 0:
                    exchange.create_market_order('BTC/USDT', 'buy', amount_fmt)
        except: pass
        time.sleep(20)

threading.Thread(target=auto_trading_engine, daemon=True).start()
threading.Thread(target=admin_token_scheduler, daemon=True).start()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
