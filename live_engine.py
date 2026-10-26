"""Binance USD-M LIVE executor. Total open notional <= 10% wallet equity.
One-way position mode required. No trade is entered without protective stop support.
"""
import os, time, hmac, hashlib, logging, threading
from decimal import Decimal, ROUND_DOWN
from urllib.parse import urlencode
import requests

D=Decimal
BASE='https://fapi.binance.com'
HTTP=requests.Session()
_LOCK=threading.RLock()
_last=0.0
_ban_until=0.0

class TradingError(RuntimeError): pass

def call(method,path,params=None,private=True):
 global _last,_ban_until
 with _LOCK:
  now=time.time()
  if now<_ban_until:raise TradingError('Binance cooldown: %d seconds'%(_ban_until-now))
  gap=3.5-(time.monotonic()-_last)
  if gap>0:time.sleep(gap)
  p=dict(params or {})
  headers={}
  if private:
   key=os.environ.get('BINANCE_API_KEY','');secret=os.environ.get('BINANCE_API_SECRET','')
   if not key or not secret:raise TradingError('Missing Binance API credentials')
   p.update(timestamp=int(time.time()*1000),recvWindow=5000)
   query=urlencode(p)
   p['signature']=hmac.new(secret.encode(),query.encode(),hashlib.sha256).hexdigest()
   headers['X-MBX-APIKEY']=key
  _last=time.monotonic()
  try:r=HTTP.request(method,BASE+path,params=p,headers=headers,timeout=15)
  except requests.RequestException as e:raise TradingError('Network error; check actual positions before retry') from e
  if r.status_code in (418,429):
   try:delay=max(120,min(86400,int(r.headers.get('Retry-After','300'))))
   except ValueError:delay=3600
   _ban_until=time.time()+delay
   raise TradingError('Binance rate limited; cooldown %ds'%delay)
  if not r.ok:raise TradingError('Binance HTTP %s: %s'%(r.status_code,r.text[:220]))
  return r.json()

def dec(x):return D(str(x))
def floor_step(value,step):return (value/step).to_integral_value(rounding=ROUND_DOWN)*step

def account_snapshot():
 acc=call('GET','/fapi/v2/account')
 equity=dec(acc['totalMarginBalance'])
 # Count ALL positions, including positions opened outside this bot.
 positions=[p for p in acc.get('positions',[]) if dec(p.get('positionAmt','0'))!=0]
 # Absolute notional includes long and short; avoids netting away risk.
 notional=sum((abs(dec(p.get('notional','0'))) for p in positions),D('0'))
 return acc,equity,notional,positions

def symbol_filters(symbol):
 info=call('GET','/fapi/v1/exchangeInfo',private=False)
 s=next((x for x in info['symbols'] if x['symbol']==symbol and x['status']=='TRADING'),None)
 if not s:raise TradingError('Symbol not available on Binance Futures')
 f={x['filterType']:x for x in s['filters']}
 return dec(f['LOT_SIZE']['stepSize']),dec(f['LOT_SIZE']['minQty']),dec(f['PRICE_FILTER']['tickSize']),dec(f.get('MIN_NOTIONAL',{}).get('notional','5'))

def daily_pnl():
 # Binance income history returns realized pnl; other commissions/funding are included separately.
 rows=call('GET','/fapi/v1/income',{'startTime':int((time.time()//86400)*86400*1000),'limit':1000})
 realized=sum((dec(x['income']) for x in rows if x.get('incomeType') in ('REALIZED_PNL','COMMISSION','FUNDING_FEE')),D('0'))
 return realized

def execute(proposal, max_total_exposure_pct=D('10'),leverage=20):
 """Execute one trade, fail closed if total exposure cap would be breached."""
 symbol=proposal['symbol'];direction=proposal['direction']
 if direction not in ('LONG','SHORT'):raise TradingError('Bad direction')
 if not (D('0')<max_total_exposure_pct<=D('10')):raise TradingError('Aggregate exposure cap cannot exceed 10%')
 if not 1<=int(leverage)<=20:raise TradingError('Leverage outside permitted 1..20')
 with _LOCK:
  # Ensure account uses one-way mode; reduceOnly exits assume one-way mode.
  mode=call('GET','/fapi/v1/positionSide/dual')
  if mode.get('dualSidePosition'):raise TradingError('Switch Binance account to One-way Mode before trading')
  acc,equity,used,positions=account_snapshot()
  if equity<=0:raise TradingError('No positive equity')
  if any(p['symbol']==symbol for p in positions):raise TradingError('Position already open on symbol')
  price=dec(call('GET','/fapi/v1/ticker/price',{'symbol':symbol},private=False)['price'])
  step,minqty,tick,minnotional=symbol_filters(symbol)
  budget=max(D('0'),equity*max_total_exposure_pct/D('100')-used)
  # Leave buffer for fees and price slippage; never intentionally cross 10%.
  budget=budget*D('0.97')
  qty=floor_step(budget/price,step)
  if qty<minqty or qty*price<minnotional:raise TradingError('Remaining total exposure is too small for Binance minimum order')
  # Margin is ~ notional/leverage, so 10% notional cap at 20x uses ~0.5% wallet margin.
  call('POST','/fapi/v1/leverage',{'symbol':symbol,'leverage':int(leverage)})
  side='BUY' if direction=='LONG' else 'SELL'
  exit_side='SELL' if side=='BUY' else 'BUY'
  stop=dec(proposal['stop'])
  stop=floor_step(stop,tick)
  if stop<=0 or (direction=='LONG' and stop>=price) or (direction=='SHORT' and stop<=price):
   raise TradingError('Invalid stop relative to current price')
  # MARKET entry then protective closePosition STOP_MARKET algo order.
  # Binance migrated conditional orders to /fapi/v1/algoOrder.
  order=call('POST','/fapi/v1/order',{'symbol':symbol,'side':side,'type':'MARKET','quantity':format(qty,'f'),'newOrderRespType':'RESULT'})
  try:
   protective=call('POST','/fapi/v1/algoOrder',{'algoType':'CONDITIONAL','symbol':symbol,'side':exit_side,'type':'STOP_MARKET','triggerPrice':format(stop,'f'),'closePosition':'true','workingType':'MARK_PRICE'})
  except Exception as stop_error:
   logging.exception('STOP FAILED; emergency market close for %s',symbol)
   try:
    # Re-read actual size before reducing; order may have partially filled.
    _,_,_,opened=account_snapshot()
    pos=next((p for p in opened if p['symbol']==symbol),None)
    if pos:
     actual=abs(dec(pos['positionAmt']))
     if actual>0:call('POST','/fapi/v1/order',{'symbol':symbol,'side':exit_side,'type':'MARKET','quantity':format(actual,'f'),'reduceOnly':'true'})
   except Exception:
    logging.exception('CRITICAL: emergency close failed; manual intervention required')
    raise TradingError('CRITICAL: stop failed AND emergency close failed. Inspect Binance immediately') from stop_error
   raise TradingError('Stop placement failed; emergency close sent; verify position') from stop_error
  return {'order_id':order.get('orderId'),'stop_id':protective.get('algoId'),'symbol':symbol,'quantity':str(qty),'notional_estimate':str(qty*price),'equity':str(equity),'prior_exposure':str(used)}
