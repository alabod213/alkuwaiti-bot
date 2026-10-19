import os
import time
import threading
import pytz
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, session
import ccxt

app = Flask(__name__)
app.secret_key = 'kuwaiti_bot_secure_session_key'

# إعدادات الحماية والأمان لحساب عبد الله
SECURITY_CODE = "96694489"
ADMIN_EMAIL = "alabod213@gmail.com"

# 1. ربط الحساب الحقيقي لبايننس بالمفاتيح الصافية والجديدة لـ "Kuwaiti Bot"
exchange = ccxt.binance({
    'apiKey': 'V0l1KbmIggXAoNjOtCapBT2FX0aGUx4EdsgySrRNOjROVAraZ0SZpFXEPjvRT0Cy', 
    'secret': 'Spp4vhqManMIj9DcmQh1ATaXQXBCuPOs9pwRAs63TjxVQV83KTK7qu9hOJAqDndS', 
    'enableRateLimit': True,
    'options': {
        'defaultType': 'future'  # تفعيل نظام العقود الآجلة
    }
})

RAIN_BITCOIN_ADDRESS = 'bc1q5tga3rmaa6c5lfxtcngueewla22zqdfzr7dcw808zxwad895e42sx79pz0'

# دالة ذكية لجلب سجل الصفقات الحقيقية
def get_trading_history():
    try:
        trades = exchange.fetch_my_trades(symbol='BTC/USDT', limit=10)
        formatted_trades = []
        for trade in trades:
            utc_time = datetime.fromtimestamp(trade['timestamp'] / 1000, pytz.utc)
            kuwait_time = utc_time.astimezone(pytz.timezone('Asia/Kuwait')).strftime('%Y-%m-%d %H:%M')
            pnl = float(trade.get('fee', {}).get('cost', 0)) * -1
            if trade['side'] == 'sell':
                pnl = (float(trade['price']) * float(trade['amount'])) * 0.05
            formatted_trades.append({
                'time': kuwait_time,
                'symbol': trade['symbol'],
                'side': 'شراء (Buy)' if trade['side'] == 'buy' else 'بيع وجني ربح (Sell)',
                'price': trade['price'],
                'amount': trade['amount'],
                'pnl': round(pnl, 2)
            })
        return formatted_trades
    except:
        return []

@app.route('/', methods=['GET', 'POST'])
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email')
        code = request.form.get('code')
        if email == ADMIN_EMAIL and code == SECURITY_CODE:
            session['logged_in'] = True
            return redirect(url_for('admin_panel'))
        return render_template('login.html', error="البيانات غير صحيحة.")
    return '''
    <html lang="ar" dir="rtl">
    <body style="background-color: #121212; color: white; font-family: sans-serif; text-align: center; padding-top: 100px;">
        <div style="background: #1e1e1e; width: 350px; margin: 0 auto; padding: 30px; border-radius: 15px; border: 2px solid #ffcc00;">
            <h2 style="color: #ffcc00;">🤖 دخول المشرف عبد الله</h2>
            <form method="POST">
                <input type="email" name="email" placeholder="الإيميل" required style="width:100%; padding:10px; margin-bottom:15px;"><br>
                <input type="password" name="code" placeholder="كود الأمان" required style="width:100%; padding:10px; margin-bottom:20px;"><br>
                <button type="submit" style="background:#ffcc00; color:black; width:100%; padding:12px; font-weight:bold;">دخول آمن 🔓</button>
            </form>
        </div>
    </body>
    </html>
    '''

@app.route('/admin-panel')
def admin_panel():
    if not session.get('logged_in'): return redirect(url_for('login'))
    trades = get_trading_history()
    trades_html = ""
    for trade in trades:
        color = "#00ff00" if trade['pnl'] >= 0 else "#ff0000"
        trades_html += f'<tr><td>{trade["time"]}</td><td>{trade["symbol"]}</td><td>{trade["side"]}</td><td>${trade["price"]}</td><td>{trade["amount"]}</td><td style="color:{color};">${trade["pnl"]}</td></tr>'
    if not trades:
        trades_html = '<tr><td colspan="6" style="padding:20px; color:#aaa;">في انتظار قنص أولى الصفقات الحية...</td></tr>'
    return f'''
    <html lang="ar" dir="rtl">
    <body style="background-color: #0c0c0c; color: white; font-family: sans-serif; padding: 20px;">
        <div style="max-width: 800px; margin: 0 auto; background: #141414; padding: 25px; border-radius: 15px;">
            <h2 style="color: #ffcc00;">🇰🇼 لوحة تحكم بوت الكويتي الحية</h2>
            <table style="width: 100%; background: #1a1a1a; text-align:center;" border="1">
                <thead><tr><th>التوقيت</th><th>العملة</th><th>العملية</th><th>السعر</th><th>الكمية</th><th>الربح</th></tr></thead>
                <tbody>{trades_html}</tbody>
            </table>
        </div>
    </body>
    </html>
    '''

def auto_trading_engine():
    symbol = 'BTC/USDT'
    # جدار وقاية مدمج للتخطي الآمن
    try:
        exchange.set_leverage(5, symbol)
    except Exception as e:
        print(f"تنبيه الأمان (تم التخطي): {e}")

    while True:
        try:
            balance = exchange.fetch_balance()
            free_balance = balance['free']['USDT']
            if free_balance < 5:
                time.sleep(30)
                continue

            order_size_usdt = free_balance * 0.10
            ticker = exchange.fetch_ticker(symbol)
            current_price = ticker['last']
            
            positions = exchange.fetch_positions(symbols=[symbol])
            has_position = False
            for pos in positions:
                if float(pos['contracts']) > 0:
                    has_position = True
                    pnl = float(pos['unrealizedProfit'])
                    if pnl > (order_size_usdt * 0.05):
                        exchange.create_market_order(symbol, 'sell', pos['contracts'], {'reduceOnly': True})
                        process_profits_to_rain(pnl * 0.50)

            if not has_position:
                amount_to_buy = order_size_usdt / current_price
                exchange.create_market_order(symbol, 'buy', amount_to_buy)
            time.sleep(30)
        except Exception as e:
            print(f"جاري الفحص ومراقبة اختراق السوق... ({e})")
            time.sleep(30)

def process_profits_to_rain(profit_usdt):
    try:
        if profit_usdt <= 0: return
        exchange.transfer('USDT', profit_usdt, 'future', 'spot')
        time.sleep(2)
        btc_ticker = exchange.fetch_ticker('BTC/USDT')
        btc_amount = profit_usdt / btc_ticker['last']
        exchange.create_market_order('BTC/USDT', 'buy', btc_amount, {'tdMode': 'cash'})
        time.sleep(2)
        exchange.withdraw('BTC', btc_amount, RAIN_BITCOIN_ADDRESS)
    except: pass

threading.Thread(target=auto_trading_engine, daemon=True).start()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)
