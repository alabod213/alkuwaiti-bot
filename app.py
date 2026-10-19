import os, time, threading, pytz
from flask import Flask, request, redirect, url_for, session
import ccxt

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'kuwaiti_bot_secure_key')

ADMIN_EMAIL = "alabod213@gmail.com"
SECURITY_CODE = "96694489"

exchange = ccxt.binance({
    'apiKey': os.environ.get('BINANCE_API_KEY', 'V0l1KbmIggXAoNjOtCapBT2FX0aGUx4EdsgySrRNOjROVAraZ0SZpFXEPjvRT0Cy'), 
    'secret': os.environ.get('BINANCE_API_SECRET', 'Spp4vhqManMIj9DcmQh1ATaXQXBCuPOs9pwRAs63TjxVQV83KTK7qu9hOJAqDndS'), 
    'enableRateLimit': True, 'options': {'defaultType': 'future'}
})

RAIN_BITCOIN_ADDRESS = 'bc1q5tga3rmaa6c5lfxtcngueewla22zqdfzr7dcw808zxwad895e42sx79pz0'
WATCHLIST = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'BNB/USDT', 'XRP/USDT', 'AVAX/USDT']

GOOGLE_TRANSLATE_SCRIPT = '''
<div style="background:#1e1e1e; padding:10px; border-radius:8px; margin-bottom:15px; text-align:left; display:flex; justify-content:space-between; align-items:center; direction:ltr;">
    <span style="font-size:13px; color:#aaa; font-family:sans-serif;">🌐 Google Translate / الترجمة الآلية:</span>
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

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email').strip().lower()
        code = request.form.get('code')
        if email == ADMIN_EMAIL and code == SECURITY_CODE:
            session['logged_in'] = True
            return redirect(url_for('admin_panel'))
        return '''<script>alert("بيانات الدخول غير صحيحة!"); window.history.back();</script>'''

    return f'''
    <html lang="ar" dir="rtl">
    <head><meta charset="UTF-8"><title>تسجيل الدخول</title></head>
    <body style="background-color: #0c0c0c; color: white; font-family: sans-serif; text-align: center; padding-top: 100px;">
        {GOOGLE_TRANSLATE_SCRIPT}
        <div style="background: #141414; width: 360px; margin: 0 auto; padding: 30px; border-radius: 15px; border: 2px solid #ffcc00; text-align:right;">
            <h2 style="color: #ffcc00; text-align:center;">🤖 دخول المشرف عبد الله</h2>
            <form method="POST">
                <input type="email" name="email" placeholder="البريد الإلكتروني" required style="width:100%; padding:11px; margin-bottom:15px;"><br>
                <input type="password" name="code" placeholder="كود الأمان الموحد" required style="width:100%; padding:11px; margin-bottom:20px;"><br>
                <button type="submit" style="background:#ffcc00; color:black; width:100%; padding:12px; font-weight:bold; border:none; border-radius:5px; cursor:pointer; width:100%;">دخول مباشر للموقع 🔐</button>
            </form>
        </div>
    </body>
    </html>
    '''

@app.route('/admin-panel')
def admin_panel():
    if not session.get('logged_in'): return redirect(url_for('login'))
    try:
        balance = exchange.fetch_balance()
        free_usdt = round(balance['free'].get('USDT', 0), 2)
    except: free_usdt = "خطأ في الاتصال ببايننس"

    return f'''
    <html lang="ar" dir="rtl">
    <head><meta charset="UTF-8"><title>لوحة التحكم</title></head>
    <body style="background-color: #0c0c0c; color: white; font-family: sans-serif; padding: 20px;">
        {GOOGLE_TRANSLATE_SCRIPT}
        <div style="max-width: 900px; margin: 0 auto; background: #141414; padding: 30px; border-radius: 15px; border: 1px solid #222;">
            <h1 style="color:#ffcc00; text-align:center;">🇰🇼 لوحة تحكم بوت الكويت المطور</h1>
            <hr style="border-color:#333; margin-bottom:20px;">
            <div style="background:#1e1e1e; padding:20px; border-radius:10px; margin-bottom:20px; text-align:center;">
                <h3 style="color:#aaa; margin:0 0 10px 0;">رصيد بايننس الحالي المتاح للاستثمار</h3>
                <p style="font-size:32px; font-weight:bold; color:#00ff00; margin:0;">${free_usdt} USDT</p>
            </div>
            <div style="text-align:center; margin-top:30px;"><a href="/logout" style="color:#ff3333; font-weight:bold; text-decoration:none;">تسجيل الخروج الآمن ↩️</a></div>
        </div>
    </body>
    </html>
    '''

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

def auto_trading_engine():
    while True:
        try:
            balance = exchange.fetch_balance()
            free_balance = balance['free'].get('USDT', 0)
            if free_balance > 20:
                ticker = exchange.fetch_ticker('BTC/USDT')
                amount = (free_balance * 0.05) / ticker['last']
                amount_fmt = exchange.amount_to_precision('BTC/USDT', amount)
                if float(amount_fmt) > 0: exchange.create_market_order('BTC/USDT', 'buy', amount_fmt)
        except: pass
        time.sleep(20)

threading.Thread(target=auto_trading_engine, daemon=True).start()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
