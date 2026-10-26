"""V57 controls on the original V38 application. LIVE is opt-in and starts paused."""
import os, time, hmac, logging
from decimal import Decimal
from functools import wraps
from flask import request, session, redirect, url_for, flash, render_template_string, jsonify
from psycopg2.extras import RealDictCursor
from live_engine import execute, account_snapshot, daily_pnl, TradingError

D=Decimal

def install(app, db, init, auth, csrf, check_csrf, proposal, symbols):
    def upgrade():
        init()
        with db() as conn, conn.cursor() as c:
            c.execute('ALTER TABLE bot_preferences ADD COLUMN IF NOT EXISTS max_total_exposure_pct NUMERIC NOT NULL DEFAULT 10')
            c.execute('ALTER TABLE bot_preferences ADD COLUMN IF NOT EXISTS live_leverage INTEGER NOT NULL DEFAULT 20')
            c.execute('ALTER TABLE bot_preferences ADD COLUMN IF NOT EXISTS max_daily_trades INTEGER NOT NULL DEFAULT 100')
            c.execute('ALTER TABLE bot_preferences ADD COLUMN IF NOT EXISTS daily_target_pct NUMERIC NOT NULL DEFAULT 20')
            c.execute('ALTER TABLE bot_preferences ALTER COLUMN is_paused SET DEFAULT TRUE')
            c.execute('''CREATE TABLE IF NOT EXISTS bot_live_audit (
                id BIGSERIAL PRIMARY KEY, order_id BIGINT UNIQUE NOT NULL,
                exchange_order_id TEXT, stop_algo_id TEXT, status TEXT NOT NULL,
                detail TEXT, created_at TIMESTAMPTZ DEFAULT NOW())''')
    def state():
        upgrade()
        with db() as conn,conn.cursor(cursor_factory=RealDictCursor) as c:
            c.execute('SELECT * FROM bot_preferences WHERE id=1')
            return c.fetchone()
    def enabled():
        return os.environ.get('LIVE_TRADING_ENABLED','false').lower()=='true' and os.environ.get('EXECUTION_AUDITED','false').lower()=='true'
    @app.route('/live/settings', methods=['GET','POST'])
    @auth
    def live_settings():
        if request.method=='POST':
            try:
                check_csrf()
                exposure=D(request.form.get('exposure','10'))
                leverage=int(request.form.get('leverage','20'))
                cap=int(request.form.get('daily_cap','100'))
                target=D(request.form.get('target','20'))
                if not (D('0')<exposure<=D('10') and 1<=leverage<=20 and 1<=cap<=100 and D('0')<=target<=100):
                    raise ValueError('الإعدادات خارج الحدود المسموحة')
                upgrade()
                with db() as conn,conn.cursor() as c:
                    c.execute('UPDATE bot_preferences SET max_total_exposure_pct=%s,live_leverage=%s,max_daily_trades=%s,daily_target_pct=%s,updated_at=NOW() WHERE id=1',(exposure,leverage,cap,target))
                flash('تم حفظ إعدادات التداول')
            except Exception as e: flash('تعذر الحفظ: '+str(e)[:150])
            return redirect(url_for('live_settings'))
        p=state(); pnl=None; equity=None; used=None; err=None
        if os.environ.get('BINANCE_API_KEY') and os.environ.get('BINANCE_API_SECRET'):
            try:
                _,equity,used,_=account_snapshot()
                pnl=daily_pnl()
            except Exception as e: err=str(e)[:200]
        return render_template_string('''<!doctype html><html lang="ar" dir="rtl"><meta name="viewport" content="width=device-width,initial-scale=1"><meta charset="utf-8"><title>V57 LIVE</title><body style="background:#101827;color:white;font:17px system-ui;padding:20px"><h1>🇰🇼 إعدادات التداول الحقيقي V57 + V38</h1><p><a style="color:skyblue" href="/">الرئيسية</a></p><p>حالة التداول الحقيقي: <b>{{'مفعّل' if live else 'معطّل من Render'}}</b> | {{'متوقف' if p.is_paused else 'مسموح بفتح صفقات'}}</p><p>الرصيد: {{equity if equity is not none else 'غير متاح'}} USDT | التعرض المفتوح: {{used if used is not none else 'غير متاح'}} USDT</p><p>صافي الربح/الخسارة المحقق اليوم (UTC): {{pnl if pnl is not none else 'غير متاح'}} USDT{% if pnl is not none and equity %} ({{(pnl/equity*100)|round(2)}}% من الرصيد الحالي){% endif %}</p><p>{{err or ''}}</p><form method="post"><input type="hidden" name="csrf" value="{{token}}"><p>سقف إجمالي التعرض % (أقصى 10): <input name="exposure" type="number" step="0.1" min="0.1" max="10" value="{{p.max_total_exposure_pct}}"></p><p>الرافعة (1–20): <input name="leverage" type="number" min="1" max="20" value="{{p.live_leverage}}"></p><p>الحد اليومي (1–100): <input name="daily_cap" type="number" min="1" max="100" value="{{p.max_daily_trades}}"></p><p>هدف الربح اليومي % (ليس ضمانًا): <input name="target" type="number" min="0" max="100" step="0.1" value="{{p.daily_target_pct}}"></p><button>حفظ الإعدادات</button></form><form method="post" action="/toggle-bot"><input type="hidden" name="csrf" value="{{token}}"><input type="hidden" name="action" value="{{'resume' if p.is_paused else 'stop'}}"><button>{{'استئناف' if p.is_paused else 'إيقاف فتح صفقات جديدة'}}</button></form><p>الإيقاف لا يغلق المراكز القائمة. 10% حد للقيمة الاسمية الإجمالية وليس للهامش.</p></body></html>''',p=p,token=csrf(),live=enabled(),equity=equity,used=used,pnl=pnl,err=err)
    @app.route('/live/approve/<int:order_id>',methods=['POST'])
    @auth
    def live_approve(order_id):
        try:
            check_csrf()
            if not enabled(): raise ValueError('التداول الحقيقي غير مفعل أو لم يتم اعتماد اختبارات التنفيذ')
            upgrade()
            # Database transaction serializes approvals even across multiple Render workers.
            with db() as conn,conn.cursor(cursor_factory=RealDictCursor) as c:
                c.execute('SELECT pg_advisory_xact_lock(570010)')
                c.execute('SELECT * FROM bot_preferences WHERE id=1 FOR UPDATE')
                p=c.fetchone()
                if p['is_paused']: raise ValueError('البوت متوقف')
                c.execute("SELECT COUNT(*) AS n FROM bot_orders_v2 WHERE created_at >= date_trunc('day',NOW() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC' AND status IN ('SUBMITTING','OPEN','CLOSING','CLOSED','REQUIRES_REVIEW')")
                if c.fetchone()['n']>=min(int(p['max_daily_trades']),100): raise ValueError('بلغت الحد اليومي')
                c.execute("UPDATE bot_orders_v2 SET status='SUBMITTING' WHERE id=%s AND status='PENDING' RETURNING *",(order_id,))
                row=c.fetchone()
                if not row: raise ValueError('الفرصة منفذة أو ملغاة')
                # Do not release the transaction lock before the exchange confirms outcome.
                try:
                    result=execute({'symbol':row['symbol'],'direction':row['direction'],'stop':row['stop_reference']},min(D('10'),D(p['max_total_exposure_pct'])),int(p['live_leverage']))
                    c.execute("UPDATE bot_orders_v2 SET status='OPEN',order_id=%s,stop_algo_id=%s,quantity=%s,note='LIVE entry + stop' WHERE id=%s",(str(result['order_id']),str(result['stop_id']),result['quantity'],order_id))
                    c.execute("INSERT INTO bot_live_audit(order_id,exchange_order_id,stop_algo_id,status) VALUES(%s,%s,%s,'OPEN') ON CONFLICT(order_id) DO NOTHING",(order_id,str(result['order_id']),str(result['stop_id'])))
                except Exception as exc:
                    c.execute("UPDATE bot_orders_v2 SET status='REQUIRES_REVIEW',note=%s WHERE id=%s",(str(exc)[:400],order_id))
                    c.execute("INSERT INTO bot_live_audit(order_id,status,detail) VALUES(%s,'REQUIRES_REVIEW',%s) ON CONFLICT(order_id) DO NOTHING",(order_id,str(exc)[:400]))
                    flash('يحتاج مراجعة Binance: '+str(exc)[:180]); return redirect(url_for('live_settings'))
            flash('تم إرسال أمر LIVE مع وقف خسارة؛ تحقق من Binance')
        except Exception as e:
            logging.exception('Live approval failure')
            flash('لم تنفذ الصفقة: '+str(e)[:180])
        return redirect(url_for('live_settings'))
    @app.route('/cron/v57-scan',methods=['POST'])
    def cron_v57_scan():
        token=os.environ.get('CRON_SECRET','')
        if not token or not hmac.compare_digest(request.headers.get('X-Cron-Secret',''),token): return jsonify(error='unauthorized'),401
        try:
            p=state()
            if p['is_paused']: return jsonify(status='PAUSED')
            # Scan only: never auto-place real orders from an unauthenticated/background request.
            count=0
            for sym in symbols[:2]:
                try:
                    idea=proposal(sym)
                    if not idea: continue
                    direction,entry,stop,take=idea
                    with db() as conn,conn.cursor() as c:
                        c.execute("SELECT 1 FROM bot_orders_v2 WHERE symbol=%s AND status IN ('PENDING','SUBMITTING','OPEN','REQUIRES_REVIEW') LIMIT 1",(sym,))
                        if c.fetchone(): continue
                        c.execute('INSERT INTO bot_orders_v2(symbol,direction,entry_reference,stop_reference,take_reference) VALUES(%s,%s,%s,%s,%s)',(sym,direction,entry,stop,take));count+=1
                except Exception: logging.exception('Scan failed for %s',sym)
            return jsonify(status='OK',opportunities=count)
        except Exception as e: return jsonify(status='ERROR',detail=str(e)[:150]),503
    # Disable legacy approval path: its exposure calculation is not the aggregate V57 cap.
    def legacy_approval_disabled(order_id):
        return redirect(url_for('live_settings'))
    app.view_functions['approve']=auth(legacy_approval_disabled)
