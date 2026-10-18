import os
import json
import random
import smtplib
import threading
import time
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import pytz
from flask import Flask, render_template_string, jsonify, request, session

app = Flask(__name__)
app.secret_key = os.urandom(24)

DATA_FILE = "secure_settings.json"
OWNER_EXEMPT_CODE = "96694489"

SMTP_SERVER = "://gmail.com"
SMTP_PORT = 587
SENDER_EMAIL = "your_system_email@gmail.com" 
SENDER_PASSWORD = "xxxx xxxx xxxx xxxx"
import ccxt
import threading
import time

# ربط الحساب الحقيقي لبايننس بالمفاتيح المؤمنة
exchange = ccxt.binance({
    'apiKey': 'V0l1KbmIggXAoNjOtCapBT2FX0aGUx4EdsgySrRNOjROVAraZ0SZpFXEPjvRT0Cy',
    'secret': 'Spp4vhqManMIj9DcmQh1ATaXQXBCuPOs9pwRAs63TjxVQV83KTK7qu9hOJAqDndS',
    'enableRateLimit': True,
    'options': {
        'defaultType': 'future'  # تفعيل نظام العقود الآجلة تلقائياً
    }
})

# عنوان محفظة رين الخاصة بك لاستلاف أرباح البتكوين
RAIN_BITCOIN_ADDRESS = 'bc1q5tga3rmaa6c5lfxtcngueewla22zqdfzr7dcw808zxwad895e42sx79pz0'

# 2. خوارزمية استراتيجية القنص الذكي (Scalping Engine)
def auto_trading_engine():
    symbol = 'BTC/USDT'  # العملة الأساسية للمراقبة
    leverage = 5  # رافعة مالية آمنة لتخطي حد الـ 10 دولارات لبايننس
    
    try:
        exchange.set_leverage(leverage, symbol)
    except Exception as e:
        print(f"Error setting leverage: {e}")

    while True:
        try:
            # قراءة الرصيد المتاح في محفظة العقود الآجلة
            balance = exchange.fetch_balance()
            free_balance = balance['free']['USDT']
            
            if free_balance < 10:
                print("الرصيد في محفظة الفيوتشر أقل من الحد الأدنى")
                time.sleep(60)
                continue

            # حساب قيمة الصفقة (10% من رأس المال المتاح)
            order_size_usdt = free_balance * 0.10
            ticker = exchange.fetch_ticker(symbol)
            current_price = ticker['last']
            
            # فحص المراكز المفتوحة لتجنب التكرار
            positions = exchange.fetch_positions(symbols=[symbol])
            has_position = False
            for pos in positions:
                if float(pos['contracts']) > 0:
                    has_position = True
                    pnl = float(pos['unrealizedProfit'])
                    if pnl > (order_size_usdt * 0.05):  # هدف الربح 5%
                        exchange.create_market_order(symbol, 'sell', pos['contracts'], {'reduceOnly': True})
                        process_profits_to_rain(pnl * 0.50)

            if not has_position:
                amount_to_buy = order_size_usdt / current_price
                exchange.create_market_order(symbol, 'buy', amount_to_buy)

            time.sleep(60)
            
        except Exception as e:
            time.sleep(10)

# 3. محرك تحويل الأرباح الآلي إلى رين (Rain)
def process_profits_to_rain(profit_usdt):
    try:
        if profit_usdt <= 0:
            return
        exchange.transfer('USDT', profit_usdt, 'future', 'spot')
        time.sleep(2)
        btc_ticker = exchange.fetch_ticker('BTC/USDT')
        btc_amount = profit_usdt / btc_ticker['last']
        exchange.create_market_order('BTC/USDT', 'buy', btc_amount, {'tdMode': 'cash'})
        time.sleep(2)
        exchange.withdraw('BTC', btc_amount, RAIN_BITCOIN_ADDRESS)
    except Exception as e:
        print(f"Error transferring: {e}")

# 4. تشغيل محرك التداول في الخلفية بشكل مستقل ودائم
threading.Thread(target=auto_trading_engine, daemon=True).start()

default_settings = {
    "app_name": "بوت الكويتي",
    "app_description": "المنصة الذكية الأولى للتداول الخوارزمي والمراجحة اللحظية في العالم والخليج واختيار أفضل الصفقات المربحة.",
    "logo_url": "https://flaticon.com",
    "theme_color": "#00cc66",
    "owner_email": "alabod213@gmail.com"
}

def load_secure_settings():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return default_settings

def save_secure_settings(settings):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False, indent=4)

def send_security_alert(target_email, details):
    try:
        msg = MIMEMultipart()
        msg["From"] = SENDER_EMAIL
        msg["To"] = target_email
        msg["Subject"] = "🚨 تحذير أمني: محاولة تعديل خارجي محظورة في البوت"
        
        body = f"<html><body><h2>🛡️ تم صد محاولة تحديث غير مصرحة</h2><p><strong>تفاصيل العملية:</strong> {details}</p></body></html>"
        msg.attach(MIMEText(body, "html"))
        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
        server.starttls()
        server.login(SENDER_EMAIL, SENDER_PASSWORD)
        server.sendmail(SENDER_EMAIL, target_email, msg.as_string())
        server.quit()
    except Exception as e:
        print(f"❌ فشل إرسال التحذير: {e}")

@app.route('/manifest.json')
def manifest():
    current_settings = load_secure_settings()
    manifest_data = {
        "short_name": current_settings["app_name"],
        "name": f"منصة {current_settings['app_name']} للذكاء الاصطناعي",
        "icons": [{"src": current_settings["logo_url"], "type": "image/png", "sizes": "512x512"}],
        "start_url": "/",
        "background_color": "#121212",
        "theme_color": current_settings["theme_color"],
        "display": "standalone",
        "orientation": "portrait"
    }
    return jsonify(manifest_data)

@app.route("/")
def index():
    current_settings = load_secure_settings()
    google_time = datetime.now(pytz.utc).strftime("%H:%M:%S || %Y-%m-%d")
    return render_template_string("""
    <!DOCTYPE html>
    <html dir="rtl" lang="ar">
    <head><title>منصة {{ settings['app_name'] }}</title><meta name="viewport" content="width=device-width, initial-scale=1.0"><link rel="manifest" href="/manifest.json"></head>
    <body style="font-family: Arial, sans-serif; text-align: center; background: #121212; color: white; padding: 40px 10px;">
        <img src="{{ settings['logo_url'] }}" style="width: 100px; height: 100px; border-radius: 12px; object-fit: contain;">
        <h1 style="color: {{ settings['theme_color'] }};">🤖 {{ settings['app_name'] }}</h1>
        <p style="color: #aaa;">{{ settings['app_description'] }}</p>
        <div style="background: #222; display: inline-block; padding: 8px 15px; border-radius: 20px; font-size: 14px; margin-top: 10px; border: 1px solid #333;">
            🌐 توقيت قوقل العالمي الموحد (UTC): <span style="color: #ffcc00; font-weight: bold;">{{ google_time }}</span>
        </div>
        <br><br>
        <a href="/admin-panel" style="color: #444; text-decoration: none; font-size: 13px;">⚙️ الإدارة والتحديث السري (خاص بالمالك)</a>
    </body>
    </html>
    """, settings=current_settings, google_time=google_time)

@app.route("/admin-panel", methods=["GET"])
def admin_panel():
    current_settings = load_secure_settings()
    return render_template_string("""
    <!DOCTYPE html>
    <html dir="rtl" lang="ar">
    <head><title>تحديث إعدادات البوت</title></head>
    <body style="font-family: Arial; background: #121212; color: white; padding-top: 50px; text-align: center;">
        <h2>⚙️ لوحة استقبال التحديثات من طرف المالك حصراً</h2>
        <div style="background: #1e1e1e; padding: 30px; display: inline-block; border-radius: 8px; text-align: right; width: 360px; border: 1px solid #00cc66;">
            <form method="POST" action="/secure-receive-update">
                <label>🔐 أدخل كود الأمان الرقمي الخاص بك للتحديث:</label><br>
                <input type="password" name="security_key" required style="width:100%; padding:8px; margin:10px 0; background:#000; color:#ffcc00; font-weight:bold; text-align:center;"><br>
                
                <label>تعديل اسم البوت:</label><br>
                <input type="text" name="name" value="{{ settings['app_name'] }}" required style="width:100%; padding:8px; margin:10px 0;"><br>
                
                <label>تعديل رابط اللوقو الجديد (URL):</label><br>
                <input type="url" name="logo" value="{{ settings['logo_url'] }}" required style="width:100%; padding:8px; margin:10px 0; direction:ltr;"><br>
                
                <label>تعديل البريد الإلكتروني للمالك:</label><br>
                <input type="email" name="owner_email" value="{{ settings['owner_email'] }}" required style="width:100%; padding:8px; margin:10px 0; direction:ltr;"><br>
                
                <button type="submit" style="width:100%; padding:12px; background:#00cc66; color:black; font-weight:bold; border:none; border-radius:4px; cursor:pointer;">إرسال وحفظ التحديث للسيرفر</button>
            </form>
        </div>
    </body>
    </html>
    """, settings=current_settings)

@app.route("/secure-receive-update", methods=["POST"])
def secure_receive_update():
    security_key = request.form.get("security_key")
    current_settings = load_secure_settings()
    
    if security_key == OWNER_EXEMPT_CODE:
        current_settings["app_name"] = request.form.get("name")
        current_settings["logo_url"] = request.form.get("logo")
        current_settings["owner_email"] = request.form.get("owner_email")
        
        save_secure_settings(current_settings)
        return "<h3>✅ تم تحديث النظام بنجاح من طرفك!</h3><a href='/'>شاهد هويتك الجديدة على الآيفون</a>"
    else:
        alert_details = f"محاولة تعديل اسم البوت إلى ({request.form.get('name')}) باستخدام كود خاطئ."
        threading.Thread(target=send_security_alert, args=(current_settings["owner_email"], alert_details)).start()
        return "<h3>❌ خطأ أمني جسيم!</h3>"

if __name__ == "__main__":
    app.run(port=5000, debug=False)
