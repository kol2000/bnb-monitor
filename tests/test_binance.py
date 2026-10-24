"""Offline tests: no real keys, trades, or transfers."""
import os,tempfile,time,unittest
from unittest.mock import patch
from decimal import Decimal
_tmp=tempfile.TemporaryDirectory()
os.environ.setdefault('DATA_DIR',_tmp.name)
os.environ.setdefault('ADMIN_PASSWORD','test-password-only-123')
os.environ.setdefault('SECRET_KEY','test-secret-'*5)
import app
import binance as m
class Exchange:
 def __init__(self):
  self.calls=[];self.fail_buy=False;self.fail_withdraw=False;self.partial=False;self.incomplete=False;self.fee='0.001';self.ident=None
 def call(self,path,params=None,method='GET',signed=True):
  p=params or {};self.calls.append((path,p.copy(),method))
  if path.endswith('/questionnaire-requirements'):return {'questionnaireCountryCode':'NIL'}
  if path.endswith('/account/commission'):return {'symbol':'BNBUSDT','standardCommission':{'taker':'0.001','buyer':'0'},'taxCommission':{'taker':'0','buyer':'0'},'specialCommission':{'taker':'0','buyer':'0'},'discount':{'enabledForAccount':False,'enabledForSymbol':False,'discountAsset':'BNB','discount':'0.75'}}
  if path.endswith('/account'):return {'canTrade':True,'canWithdraw':True,'balances':[{'asset':'USDT','free':'100','locked':'0'},{'asset':'BNB','free':'10','locked':'0'}]}
  if path.endswith('/getall'):return [{'coin':'BNB','networkList':[{'network':'BSC','withdrawEnable':True,'withdrawFee':self.fee,'withdrawMin':'0.001','withdrawMax':'100','withdrawIntegerMultiple':'0.0001'}]}]
  if path.endswith('/selfSymbols'):return {'data':['BNBUSDT']}
  if path.endswith('/exchangeInfo'):return {'symbols':[{'status':'TRADING','orderTypes':['MARKET'],'quoteAmountPrecisionMarket':'1'}]}
  if path.endswith('/ticker/price'):return {'price':'500'}
  if path.endswith('/order'):
   if method=='POST':
    self.ident=p['newClientOrderId']
    if self.fail_buy:raise m.BinanceError('Timeout')
    return {'orderId':'123'}
   return {'orderId':'123','clientOrderId':self.ident,'symbol':'BNBUSDT','side':'BUY','status':'PARTIALLY_FILLED' if self.partial else 'FILLED','executedQty':'0.1','cummulativeQuoteQty':'50.05005006'}
  if path.endswith('/myTrades'):return [{'id':'t1','orderId':'123','qty':'0.05' if self.incomplete else '0.1','commission':'0.0001','commissionAsset':'BNB'}]
  if path.endswith('/withdraw/apply'):
   if self.fail_withdraw:raise m.BinanceError('Timeout')
   return {'id':'w1'}
  if path.endswith('/history'):return [{'id':'w1','withdrawOrderId':self.ident,'address':'0x'+'1'*40,'coin':'BNB','network':'BSC','status':6,'txId':'tx'}]
  raise AssertionError(path)
class BinanceTests(unittest.TestCase):
 def setUp(self):
  with app.db() as c:c.execute('DELETE FROM binance_jobs')
  self.fake=Exchange();p=patch.object(m,'Client',return_value=self.fake);p.start();self.addCleanup(p.stop)
 def preview(self):return m.buy_preview({'amount':'50','network':'BSC','address':'0x'+'1'*40})
 def bought(self):
  j=self.preview();m.buy_confirm(j['id']);return m.reconcile(j['id'])
 def test_exact_addition_and_no_post_on_preview(self):
  j=self.preview();self.assertEqual(j['data']['total'],'50.05005006');self.assertFalse(any(c[2]=='POST' for c in self.fake.calls))
 def test_timeout_never_duplicates_buy_and_can_reconcile(self):
  j=self.preview();self.fake.fail_buy=True;m.buy_confirm(j['id'])
  with self.assertRaises(m.BinanceError):m.buy_confirm(j['id'])
  self.assertEqual(m.reconcile(j['id'])['state'],'bought');self.assertEqual(sum(c[2]=='POST' for c in self.fake.calls),1)
 def test_fee_and_only_purchase_withdrawable(self):
  j=self.bought();self.assertEqual(j['data']['bought'],'0.0999');p=m.withdraw_preview(j['id'])['data']['withdraw_plan'];self.assertEqual(p['amount'],'0.0989')
  self.assertLessEqual(Decimal(p['amount'])+Decimal(p['fee']),Decimal('0.0999'))
 def test_partial_order_waits(self):
  self.fake.partial=True;j=self.preview();m.buy_confirm(j['id']);self.assertEqual(m.reconcile(j['id'])['state'],'buying')
  with self.assertRaises(m.BinanceError):m.withdraw_preview(j['id'])
 def test_missing_fills_block(self):
  self.fake.incomplete=True;j=self.preview();m.buy_confirm(j['id']);self.assertEqual(m.reconcile(j['id'])['state'],'buying')
 def test_changed_fee_invalidates_confirmation(self):
  j=self.bought();m.withdraw_preview(j['id']);self.fake.fee='0.002'
  with self.assertRaises(m.BinanceError):m.withdraw_confirm(j['id'])
  self.assertFalse(any(c[0].endswith('/withdraw/apply') for c in self.fake.calls))
 def test_timeout_never_duplicates_withdraw(self):
  j=self.bought();m.withdraw_preview(j['id']);self.fake.fail_withdraw=True;m.withdraw_confirm(j['id'])
  with self.assertRaises(m.BinanceError):m.withdraw_confirm(j['id'])
  self.assertEqual(m.reconcile(j['id'])['state'],'done');self.assertEqual(sum(c[0].endswith('/withdraw/apply') for c in self.fake.calls),1)
 def test_one_active_purchase(self):
  a=self.preview();b=self.preview();m.buy_confirm(a['id'])
  with self.assertRaises(m.BinanceError):m.buy_confirm(b['id'])
 def test_expired_preview(self):
  j=self.preview()
  with app.db() as c:c.execute('UPDATE binance_jobs SET created=? WHERE id=?',(time.time()-200,j['id']))
  with self.assertRaises(m.BinanceError):m.buy_confirm(j['id'])
 def test_invalid_amounts(self):
  for v in ['NaN','Infinity','-1','0','0.001']:
   with self.assertRaises(m.BinanceError):m.buy_preview({'amount':v})
 def test_auth_csrf(self):
  c=app.app.test_client();self.assertEqual(c.get('/api/binance/jobs').status_code,401);self.assertEqual(c.post('/api/binance/refresh',json={}).status_code,403)
 def test_exact_payload(self):
  j=self.bought();m.withdraw_preview(j['id']);m.withdraw_confirm(j['id']);p=[c for c in self.fake.calls if c[2]=='POST'];self.assertEqual(p[0][1]['quoteOrderQty'],'50.05005006');self.assertEqual(p[1][1]['network'],'BSC');self.assertEqual(p[1][1]['address'],'0x'+'1'*40)

 def test_binance_increment_is_not_decimal_count(self):
  self.assertEqual(m.withdrawal_step('1'),Decimal('1'))
  self.assertEqual(m.withdrawal_step('0.000001'),Decimal('0.000001'))
  for bad in ['18','0',None,True]:
   with self.assertRaises(m.BinanceError):m.withdrawal_step(bad)
 def test_travel_rule_blocks_before_purchase(self):
  original=self.fake.call
  self.fake.call=lambda path,*a,**kw: {'questionnaireCountryCode':'AE'} if path.endswith('questionnaire-requirements') else original(path,*a,**kw)
  with self.assertRaises(m.BinanceError):self.preview()
  self.assertFalse(any(c[2]=='POST' for c in self.fake.calls))
 def test_mexc_alias_and_wrong_network_not_accepted(self):
  self.assertFalse(m.withdrawal_asset_matches({'coin':'BNB-BSC','network':'BSC'},'BSC'))
  self.assertFalse(m.withdrawal_asset_matches({'coin':'BNB','network':'ETH'},'BSC'))
 def test_separate_mexc_state(self):
  import mexc,json
  with app.db() as c:c.execute("INSERT OR REPLACE INTO mexc_jobs(id,created,state,data) VALUES ('other',?,'buying','{}')",(time.time(),))
  try:
   j=self.preview();m.buy_confirm(j['id'])
   self.assertEqual(m.get_job(j['id'])['state'],'buying')
   self.assertEqual(mexc.get_job('other')['state'],'buying')
  finally:
   with app.db() as c:c.execute("DELETE FROM mexc_jobs WHERE id='other'")
 def test_bsc_only(self):
  with self.assertRaises(m.BinanceError):m.buy_preview({'amount':'50','network':'ETH','address':'0x'+'1'*40})
 def test_withdraw_uses_spot_and_binance_fields(self):
  j=self.bought();m.withdraw_preview(j['id']);m.withdraw_confirm(j['id'])
  path,p,method=[c for c in self.fake.calls if c[0].endswith('/withdraw/apply')][0]
  self.assertEqual(path,'/sapi/v1/capital/withdraw/apply');self.assertEqual(p['walletType'],0)
  self.assertNotIn('netWork',p);self.assertNotIn('memo',p)
