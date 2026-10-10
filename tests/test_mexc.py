"""Offline tests: no real keys, trades, or transfers."""
import os,tempfile,time,unittest
from unittest.mock import patch
from decimal import Decimal
_tmp=tempfile.TemporaryDirectory()
os.environ.setdefault('DATA_DIR',_tmp.name)
os.environ.setdefault('ADMIN_PASSWORD','test-password-only-123')
os.environ.setdefault('SECRET_KEY','test-secret-'*5)
import app
import mexc as m
class Exchange:
 def __init__(self):
  self.calls=[];self.fail_buy=False;self.fail_withdraw=False;self.partial=False;self.incomplete=False;self.fee='0.001';self.ident=None
 def call(self,path,params=None,method='GET',signed=True):
  p=params or {};self.calls.append((path,p.copy(),method))
  if path.endswith('/account'):return {'canTrade':True,'canWithdraw':True,'balances':[{'asset':'USDT','free':'100','locked':'0'},{'asset':'BNB','free':'10','locked':'0'}]}
  if path.endswith('/getall'):return [{'coin':'BNB','networkList':[{'netWork':'BSC','withdrawEnable':True,'withdrawFee':self.fee,'withdrawMin':'0.001','withdrawMax':'100','withdrawIntegerMultiple':'0.0001'}]}]
  if path.endswith('/selfSymbols'):return {'data':['BNBUSDT']}
  if path.endswith('/exchangeInfo'):return {'symbols':[{'orderTypes':['MARKET'],'quoteAmountPrecisionMarket':'1'}]}
  if path.endswith('/ticker/price'):return {'price':'500'}
  if path.endswith('/order'):
   if method=='POST':
    self.ident=p['newClientOrderId']
    if self.fail_buy:raise m.MexcError('Timeout')
    return {'orderId':'123'}
   return {'orderId':'123','clientOrderId':self.ident,'symbol':'BNBUSDT','side':'BUY','status':'PARTIALLY_FILLED' if self.partial else 'FILLED','executedQty':'0.1','cummulativeQuoteQty':'50.10'}
  if path.endswith('/myTrades'):return [{'id':'t1','orderId':'123','qty':'0.05' if self.incomplete else '0.1','commission':'0.0001','commissionAsset':'BNB'}]
  if path.endswith('/withdraw'):
   if self.fail_withdraw:raise m.MexcError('Timeout')
   return {'id':'w1'}
  if path.endswith('/history'):return [{'id':'w1','withdrawOrderId':self.ident,'address':'0x'+'1'*40,'coin':'BNB','status':7,'txId':'tx'}]
  raise AssertionError(path)
class MexcTests(unittest.TestCase):
 def setUp(self):
  with app.db() as c:c.execute('DELETE FROM mexc_jobs')
  self.fake=Exchange();p=patch.object(m,'Client',return_value=self.fake);p.start();self.addCleanup(p.stop)
 def preview(self):return m.buy_preview({'amount':'50','network':'BSC','address':'0x'+'1'*40})
 def bought(self):
  j=self.preview();m.buy_confirm(j['id']);return m.reconcile(j['id'])
 def test_exact_addition_and_no_post_on_preview(self):
  j=self.preview();self.assertEqual(j['data']['total'],'50.10');self.assertFalse(any(c[2]=='POST' for c in self.fake.calls))
 def test_timeout_never_duplicates_buy_and_can_reconcile(self):
  j=self.preview();self.fake.fail_buy=True;m.buy_confirm(j['id'])
  with self.assertRaises(m.MexcError):m.buy_confirm(j['id'])
  self.assertEqual(m.reconcile(j['id'])['state'],'bought');self.assertEqual(sum(c[2]=='POST' for c in self.fake.calls),1)
 def test_fee_and_only_purchase_withdrawable(self):
  j=self.bought();self.assertEqual(j['data']['bought'],'0.0999');p=m.withdraw_preview(j['id'])['data']['withdraw_plan'];self.assertEqual(p['amount'],'0.0989')
  self.assertLessEqual(Decimal(p['amount'])+Decimal(p['fee']),Decimal('0.0999'))
 def test_partial_order_waits(self):
  self.fake.partial=True;j=self.preview();m.buy_confirm(j['id']);self.assertEqual(m.reconcile(j['id'])['state'],'buying')
  with self.assertRaises(m.MexcError):m.withdraw_preview(j['id'])
 def test_missing_fills_block(self):
  self.fake.incomplete=True;j=self.preview();m.buy_confirm(j['id']);self.assertEqual(m.reconcile(j['id'])['state'],'buying')
 def test_changed_fee_invalidates_confirmation(self):
  j=self.bought();m.withdraw_preview(j['id']);self.fake.fee='0.002'
  with self.assertRaises(m.MexcError):m.withdraw_confirm(j['id'])
  self.assertFalse(any(c[0].endswith('/withdraw') for c in self.fake.calls))
 def test_timeout_never_duplicates_withdraw(self):
  j=self.bought();m.withdraw_preview(j['id']);self.fake.fail_withdraw=True;m.withdraw_confirm(j['id'])
  with self.assertRaises(m.MexcError):m.withdraw_confirm(j['id'])
  self.assertEqual(m.reconcile(j['id'])['state'],'done');self.assertEqual(sum(c[0].endswith('/withdraw') for c in self.fake.calls),1)
 def test_one_active_purchase(self):
  a=self.preview();b=self.preview();m.buy_confirm(a['id'])
  with self.assertRaises(m.MexcError):m.buy_confirm(b['id'])
 def test_expired_preview(self):
  j=self.preview()
  with app.db() as c:c.execute('UPDATE mexc_jobs SET created=? WHERE id=?',(time.time()-200,j['id']))
  with self.assertRaises(m.MexcError):m.buy_confirm(j['id'])
 def test_invalid_amounts(self):
  for v in ['NaN','Infinity','-1','0','0.001']:
   with self.assertRaises(m.MexcError):m.buy_preview({'amount':v})
 def test_auth_csrf(self):
  c=app.app.test_client();self.assertEqual(c.get('/api/mexc/jobs').status_code,401);self.assertEqual(c.post('/api/mexc/refresh',json={}).status_code,403)
 def test_exact_payload(self):
  j=self.bought();m.withdraw_preview(j['id']);m.withdraw_confirm(j['id']);p=[c for c in self.fake.calls if c[2]=='POST'];self.assertEqual(p[0][1]['quoteOrderQty'],'50.10');self.assertEqual(p[1][1]['netWork'],'BSC');self.assertEqual(p[1][1]['address'],'0x'+'1'*40)
