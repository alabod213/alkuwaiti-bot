import os, time, threading, secrets
from datetime import datetime
import pytz
from flask import Flask, request, redirect, url_for, session
import ccxt

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', secrets.token_hex(16))

ADMIN_EMAIL = "alabod213@gmail.com"
SECURITY_CODE = "96694489"
RAIN_BITCOIN_ADDRESS = 'bc1q5tga3rmaa6c5lfxtcngueewla22zqdfzr7dcw808zxwad895e42sx79pz0'
WATCHLIST = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'BNB/USDT', 'XRP/USDT']

exchange = ccxt.binance({
    'apiKey': os.environ.get('BINANCE_API_KEY'), 
    'secret': os.environ.get('BINANCE_API_SECRET'), 
    'enableRateLimit': True, 'options': {'defaultType': 'future'}
})

# قاعدة بيانات وهمية لتتبع عدد فترات التجديد لكل مستخدم لمنحه الأسابيع والأشهر المجانية تلقائياً
users_db = {}
total_subscription_earnings = 0.0

# 🌙 دالة الذكاء الاصطناعي لفحص توقيت الكويت وتطبيق الخصومات والمناسبات الدينية ورأس السنة لعام 2026 تلقائياً
def calculate_dynamic_subscription_fee(plan, email):
    kuwait_tz = pytz.timezone('Asia/Kuwait')
    now = datetime.now(kuwait_tz)
    
    # أسعار القيمة الافتراضية للباقات
    base_prices = {'weekly': 19.99, 'monthly': 49.99, 'yearly': 99.99}
    final_price = base_prices.get(plan, 49.99)
    
    # جلب سجل اشتراكات المستخدم لمعرفة الفترات المجانية
    user_record = users_db.get(email, {})
    past_payments = user_record.get('completed_payments_count', 0)

    # 1️⃣ تفعيل نظام الولاء (الفترات المجانية)
    if plan == 'weekly' and past_payments > 0 and (past_payments % 3 == 0):
        return 0.0, "🎁 عرض الولاء: الأسبوع الرابع مجاني بالكامل!"
    if plan == 'monthly' and past_payments > 0 and (past_payments % 3 == 0):
        return 0.0, "🎁 عرض الولاء: الشهر الرابع مجاني بالكامل!"

    # لستة المناسبات الدينية والوطنية ورأس السنة لعام 2026 في الكويت (صيغة: الشهر، اليوم)
    HOLIDAYS_2026 = [
        (1, 1),   # رأس السنة الميلادية
        (2, 25),  # اليوم الوطني الكويتي
        (2, 26),  # يوم التحرير الكويتي
        (3, 20),  # عيد الفطر المبارك (توقيتي فلكي لعام 2026)
        (5, 27),  # عيد الأضحى المبارك (توقيتي فلكي لعام 2026)
        (6, 16),  # رأس السنة الهجرية الجديدة
    ]
    
    is_holiday = (now.month, now.day) in HOLIDAYS_2026
    is_weekend = now.weekday() in [4, 5] # يوم الجمعة والسبت حسب تقويم بايثون

    # 2️⃣ تفعيل عروض الأعياد والمناسبات الدينية (خصم 10 دولارات لمدة 24 ساعة)
    if is_holiday:
        final_price = max(0.0, final_price - 10.0)
        note = "🌙 عروض الأعياد والمناسبات الدينية السعيدة: تم تطبيق خصم $10 فوري لـ 24 ساعة!"
        if plan == 'yearly':
            note += " + 🎁 مدمج مع الخطة شهرين مجاناً!"
        return round(final_price, 2), note

    # 3️⃣ تفعيل عروض نهاية الأسبوع (الجمعة والسبت التلقائي)
    if is_weekend:
        if plan == 'weekly':
            final_price = max(0.0, final_price - 2.0)
            note = "⚡ عروض الويكند الحية: تم تطبيق خصم $2 على الباقة الأسبوعية!"
        elif plan == 'monthly':
            final_price = max(0.0, final_price - 5.0)
            note = "⚡ عروض الويكند الحية: تم تطبيق خصم $5 على الباقة الشهرية!"
        elif plan == 'yearly':
            final_price = max(0.0, final_price - 10.0)
            note = "⚡ عروض الويكند الحية: تم تطبيق خصم $10 على الباقة السنوية + 🎁 شهرين مجاناً!"
        return round(final_price, 2), note

    # الحالة الافتراضية في الأيام العادية
    note = "💎 السعر القياسي الفوري للباقة."
    if plan == 'yearly':
        note = "💎 باقة التوفير السنوية الشاملة: تتضمن شهرين مجاناً بالكامل!"
    return round(final_price, 2), note
import os, time, threading, secrets
from datetime import datetime
import pytz
from flask import Flask, request, redirect, url_for, session
import ccxt

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', secrets.token_hex(16))

ADMIN_EMAIL = "alabod213@gmail.com"
SECURITY_CODE = "96694489"
RAIN_BITCOIN_ADDRESS = 'bc1q5tga3rmaa6c5lfxtcngueewla22zqdfzr7dcw808zxwad895e42sx79pz0'
WATCHLIST = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'BNB/USDT', 'XRP/USDT']

exchange = ccxt.binance({
    'apiKey': os.environ.get('BINANCE_API_KEY'), 
    'secret': os.environ.get('BINANCE_API_SECRET'), 
    'enableRateLimit': True, 'options': {'defaultType': 'future'}
})

# قاعدة بيانات وهمية لتتبع عدد فترات التجديد لكل مستخدم لمنحه الأسابيع والأشهر المجانية تلقائياً
users_db = {}
total_subscription_earnings = 0.0

# 🌙 دالة الذكاء الاصطناعي لفحص توقيت الكويت وتطبيق الخصومات والمناسبات الدينية ورأس السنة لعام 2026 تلقائياً
def calculate_dynamic_subscription_fee(plan, email):
    kuwait_tz = pytz.timezone('Asia/Kuwait')
    now = datetime.now(kuwait_tz)
    
    # أسعار القيمة الافتراضية للباقات
    base_prices = {'weekly': 19.99, 'monthly': 49.99, 'yearly': 99.99}
    final_price = base_prices.get(plan, 49.99)
    
    # جلب سجل اشتراكات المستخدم لمعرفة الفترات المجانية
    user_record = users_db.get(email, {})
    past_payments = user_record.get('completed_payments_count', 0)

    # 1️⃣ تفعيل نظام الولاء (الفترات المجانية)
    if plan == 'weekly' and past_payments > 0 and (past_payments % 3 == 0):
        return 0.0, "🎁 عرض الولاء: الأسبوع الرابع مجاني بالكامل!"
    if plan == 'monthly' and past_payments > 0 and (past_payments % 3 == 0):
        return 0.0, "🎁 عرض الولاء: الشهر الرابع مجاني بالكامل!"

    # لستة المناسبات الدينية والوطنية ورأس السنة لعام 2026 في الكويت (صيغة: الشهر، اليوم)
    HOLIDAYS_2026 = [
        (1, 1),   # رأس السنة الميلادية
        (2, 25),  # اليوم الوطني الكويتي
        (2, 26),  # يوم التحرير الكويتي
        (3, 20),  # عيد الفطر المبارك (توقيتي فلكي لعام 2026)
        (5, 27),  # عيد الأضحى المبارك (توقيتي فلكي لعام 2026)
        (6, 16),  # رأس السنة الهجرية الجديدة
    ]
    
    is_holiday = (now.month, now.day) in HOLIDAYS_2026
    is_weekend = now.weekday() in [4, 5] # يوم الجمعة والسبت حسب تقويم بايثون

    # 2️⃣ تفعيل عروض الأعياد والمناسبات الدينية (خصم 10 دولارات لمدة 24 ساعة)
    if is_holiday:
        final_price = max(0.0, final_price - 10.0)
        note = "🌙 عروض الأعياد والمناسبات الدينية السعيدة: تم تطبيق خصم $10 فوري لـ 24 ساعة!"
        if plan == 'yearly':
            note += " + 🎁 مدمج مع الخطة شهرين مجاناً!"
        return round(final_price, 2), note

    # 3️⃣ تفعيل عروض نهاية الأسبوع (الجمعة والسبت التلقائي)
    if is_weekend:
        if plan == 'weekly':
            final_price = max(0.0, final_price - 2.0)
            note = "⚡ عروض الويكند الحية: تم تطبيق خصم $2 على الباقة الأسبوعية!"
        elif plan == 'monthly':
            final_price = max(0.0, final_price - 5.0)
            note = "⚡ عروض الويكند الحية: تم تطبيق خصم $5 على الباقة الشهرية!"
        elif plan == 'yearly':
            final_price = max(0.0, final_price - 10.0)
            note = "⚡ عروض الويكند الحية: تم تطبيق خصم $10 على الباقة السنوية + 🎁 شهرين مجاناً!"
        return round(final_price, 2), note

    # الحالة الافتراضية في الأيام العادية
    note = "💎 السعر القياسي الفوري للباقة."
    if plan == 'yearly':
        note = "💎 باقة التوفير السنوية الشاملة: تتضمن شهرين مجاناً بالكامل!"
    return round(final_price, 2), note
import os, time, threading, secrets
from datetime import datetime
import pytz
from flask import Flask, request, redirect, url_for, session
import ccxt

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', secrets.token_hex(16))

ADMIN_EMAIL = "alabod213@gmail.com"
SECURITY_CODE = "96694489"
RAIN_BITCOIN_ADDRESS = 'bc1q5tga3rmaa6c5lfxtcngueewla22zqdfzr7dcw808zxwad895e42sx79pz0'
WATCHLIST = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'BNB/USDT', 'XRP/USDT']

exchange = ccxt.binance({
    'apiKey': os.environ.get('BINANCE_API_KEY'), 
    'secret': os.environ.get('BINANCE_API_SECRET'), 
    'enableRateLimit': True, 'options': {'defaultType': 'future'}
})

# قاعدة بيانات وهمية لتتبع عدد فترات التجديد لكل مستخدم لمنحه الأسابيع والأشهر المجانية تلقائياً
users_db = {}
total_subscription_earnings = 0.0

# 🌙 دالة الذكاء الاصطناعي لفحص توقيت الكويت وتطبيق الخصومات والمناسبات الدينية ورأس السنة لعام 2026 تلقائياً
def calculate_dynamic_subscription_fee(plan, email):
    kuwait_tz = pytz.timezone('Asia/Kuwait')
    now = datetime.now(kuwait_tz)
    
    # أسعار القيمة الافتراضية للباقات
    base_prices = {'weekly': 19.99, 'monthly': 49.99, 'yearly': 99.99}
    final_price = base_prices.get(plan, 49.99)
    
    # جلب سجل اشتراكات المستخدم لمعرفة الفترات المجانية
    user_record = users_db.get(email, {})
    past_payments = user_record.get('completed_payments_count', 0)

    # 1️⃣ تفعيل نظام الولاء (الفترات المجانية)
    if plan == 'weekly' and past_payments > 0 and (past_payments % 3 == 0):
        return 0.0, "🎁 عرض الولاء: الأسبوع الرابع مجاني بالكامل!"
    if plan == 'monthly' and past_payments > 0 and (past_payments % 3 == 0):
        return 0.0, "🎁 عرض الولاء: الشهر الرابع مجاني بالكامل!"

    # لستة المناسبات الدينية والوطنية ورأس السنة لعام 2026 في الكويت (صيغة: الشهر، اليوم)
    HOLIDAYS_2026 = [
        (1, 1),   # رأس السنة الميلادية
        (2, 25),  # اليوم الوطني الكويتي
        (2, 26),  # يوم التحرير الكويتي
        (3, 20),  # عيد الفطر المبارك (توقيتي فلكي لعام 2026)
        (5, 27),  # عيد الأضحى المبارك (توقيتي فلكي لعام 2026)
        (6, 16),  # رأس السنة الهجرية الجديدة
    ]
    
    is_holiday = (now.month, now.day) in HOLIDAYS_2026
    is_weekend = now.weekday() in [4, 5] # يوم الجمعة والسبت حسب تقويم بايثون

    # 2️⃣ تفعيل عروض الأعياد والمناسبات الدينية (خصم 10 دولارات لمدة 24 ساعة)
    if is_holiday:
        final_price = max(0.0, final_price - 10.0)
        note = "🌙 عروض الأعياد والمناسبات الدينية السعيدة: تم تطبيق خصم $10 فوري لـ 24 ساعة!"
        if plan == 'yearly':
            note += " + 🎁 مدمج مع الخطة شهرين مجاناً!"
        return round(final_price, 2), note

    # 3️⃣ تفعيل عروض نهاية الأسبوع (الجمعة والسبت التلقائي)
    if is_weekend:
        if plan == 'weekly':
            final_price = max(0.0, final_price - 2.0)
            note = "⚡ عروض الويكند الحية: تم تطبيق خصم $2 على الباقة الأسبوعية!"
        elif plan == 'monthly':
            final_price = max(0.0, final_price - 5.0)
            note = "⚡ عروض الويكند الحية: تم تطبيق خصم $5 على الباقة الشهرية!"
        elif plan == 'yearly':
            final_price = max(0.0, final_price - 10.0)
            note = "⚡ عروض الويكند الحية: تم تطبيق خصم $10 على الباقة السنوية + 🎁 شهرين مجاناً!"
        return round(final_price, 2), note

    # الحالة الافتراضية في الأيام العادية
    note = "💎 السعر القياسي الفوري للباقة."
    if plan == 'yearly':
        note = "💎 باقة التوفير السنوية الشاملة: تتضمن شهرين مجاناً بالكامل!"
    return round(final_price, 2), note
