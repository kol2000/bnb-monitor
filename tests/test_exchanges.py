"""Offline exchange tests: no credentials, real trading or transfers."""
import os,tempfile,time,json,unittest,hashlib,hmac
from decimal import Decimal
from unittest.mock import patch
_tmp=tempfile.TemporaryDirectory()
os.environ.setdefault('DATA_DIR',_tmp.name)
os.environ.setdefault('ADMIN_PASSWORD','test-password-only-123')
os.environ.setdefault('SECRET_KEY','test-secret-'*5)
import app
import exchanges as ex
import exchange_clients as clients
import withdrawals as wd
ADDRESS='0x'+'1'*40

class Fake:
 def __init__(self):
  self.posts=[];self.allowed=True;self.timeout=False;self.partial=False;self.fee='0.00001';self.price='600';self.rate='0.001';self.free='1000';self.mismatch=False
 def balances(self):return {'BNB':'2','USDT':self.free}
 def network(self):return dict(network='BSC',fee=self.fee,minimum='0.0005',maximum='100',step='0.000001')
 def market(self):return dict(price=self.price,fee=self.rate,base_step='0.00001',quote_step='0.01',minimum='5',max_quote='10000',max_base='100')
 def address_check(self,a):
  if not self.allowed:raise clients.ExchangeError('Whitelist')
 def order(self,i,s,a):
  self.posts.append(('order',i,s,a))
  if self.timeout:raise clients.ExchangeError('Timeout')
  return 'order1'
 def order_status(self,*a):return dict(id='order1',terminal=not self.partial,quantity='0.1',quote='60',fees={'BNB':'0.0001'},status='Filled')
 def withdraw(self,i,a,n):
  self.posts.append(('withdraw',i,a,n));self.wtime=time.time()
  if self.timeout:raise clients.ExchangeError('Timeout')
  return 'withdraw1'
 def history(self,ident=None,withdraw_id=None):
  n=self.posts[-1][-1]
  return [dict(id='withdraw1',request_id=ident,coin='BNB',network='ETH' if self.mismatch else 'BSC',address=ADDRESS,amount=n,
   status='success',txid='0x'+'a'*64,created=str(int(self.wtime*1000)),done=True,failed=False)]

class Workflow(unittest.TestCase):
 def setUp(self):
  with app.db() as c:c.execute('DELETE FROM exchange_jobs')
  self.f=Fake();p=patch.object(ex,'Client',return_value=self.f);p.start();self.addCleanup(p.stop)
 def preview(self,v='gate'):return ex.preview(v,dict(amount='50',address=ADDRESS,goal='wallet'))
 def bought(self,v='gate'):
  j=self.preview(v);ex.confirm(v,j['id']);return ex.reconcile(v,j['id'])
 def test_preview_readonly_and_commission(self):
  j=self.preview();d=j['data'];self.assertEqual(self.f.posts,[]);self.assertGreater(Decimal(d['total']),Decimal('50'));self.assertGreaterEqual(Decimal(d['recipient_estimate'])*600,50)
 def test_whitelist_blocks_before_spending(self):
  self.f.allowed=False
  with self.assertRaises(ValueError):self.preview('bybit')
  self.assertEqual(self.f.posts,[])
 def test_whitelist_rechecked_at_confirmation(self):
  j=self.preview('bybit');self.f.allowed=False
  with self.assertRaises(ValueError):ex.confirm('bybit',j['id'])
  self.assertEqual(self.f.posts,[])
 def test_order_timeout_cannot_duplicate(self):
  j=self.preview();self.f.timeout=True;ex.confirm('gate',j['id'])
  with self.assertRaises(ValueError):ex.confirm('gate',j['id'])
  self.assertEqual(ex.reconcile('gate',j['id'])['state'],'bought');self.assertEqual(len(self.f.posts),1)
 def test_partial_waits(self):
  self.f.partial=True;j=self.bought();self.assertEqual(j['state'],'ordering')
  with self.assertRaises(ValueError):ex.withdraw_preview('gate',j['id'])
 def test_partial_cancel_with_execution_withdrawable(self):
  j=self.bought();self.assertEqual(j['data']['bought'],'0.0999')
 def test_price_change_does_not_change_approved_spend(self):
  j=self.preview();self.f.price='601';ex.confirm('gate',j['id']);self.assertEqual(self.f.posts[0][-1],j['data']['total'])
 def test_changed_fee_blocks_confirmation(self):
  j=self.preview();self.f.rate='0.002'
  with self.assertRaises(ValueError):ex.confirm('gate',j['id'])
  self.assertEqual(self.f.posts,[])
 def test_one_active_per_venue(self):
  a=self.preview();b=self.preview();ex.confirm('gate',a['id'])
  with self.assertRaises(ValueError):ex.confirm('gate',b['id'])
  c=self.preview('bybit');ex.confirm('bybit',c['id']);self.assertEqual(len(self.f.posts),2)
 def test_cross_venue_id_rejected(self):
  j=self.preview()
  with self.assertRaises(ValueError):ex.confirm('bybit',j['id'])
 def test_only_purchased_amount_with_fee_reserved(self):
  j=self.bought();r=ex.withdraw_preview('gate',j['id']);p=r['data']['withdraw_plan'];self.assertEqual(p['amount'],'0.09989');self.assertLessEqual(Decimal(p['amount'])+Decimal(p['fee']),Decimal('0.0999'))
 def test_unlimited_chain_still_reserves_fee_and_checks_minimum(self):
  network=self.f.network();network['maximum']=None
  self.f.network=lambda:network
  plan=ex.withdrawal_plan('bybit',{'address':ADDRESS,'bought':'0.1'})
  self.assertEqual(plan['amount'],'0.09999')
  network['minimum']='0.2'
  with self.assertRaises(ValueError):ex.withdrawal_plan('bybit',{'address':ADDRESS,'bought':'0.1'})
  network['minimum']='0.0005';network['maximum']='0.01'
  with self.assertRaises(ValueError):ex.withdrawal_plan('bybit',{'address':ADDRESS,'bought':'0.1'})
  self.assertEqual(self.f.posts,[])
 def test_withdraw_timeout_not_resent(self):
  j=self.bought();ex.withdraw_preview('gate',j['id']);self.f.timeout=True;ex.withdraw_confirm('gate',j['id'])
  with self.assertRaises(ValueError):ex.withdraw_confirm('gate',j['id'])
  self.assertEqual(ex.reconcile('gate',j['id'])['state'],'done');self.assertEqual(len(self.f.posts),2)
 def test_bybit_timeout_requires_id(self):
  j=self.bought('bybit');ex.withdraw_preview('bybit',j['id']);self.f.timeout=True;ex.withdraw_confirm('bybit',j['id'])
  self.assertEqual(ex.reconcile('bybit',j['id'])['state'],'withdrawing')
  self.assertEqual(ex.reconcile('bybit',j['id'],'withdraw1')['state'],'done');self.assertEqual(len(self.f.posts),2)
 def test_wrong_history_blocks(self):
  j=self.bought();ex.withdraw_preview('gate',j['id']);ex.withdraw_confirm('gate',j['id']);self.f.mismatch=True
  self.assertEqual(ex.reconcile('gate',j['id'])['state'],'withdrawing')
 def test_fee_changed_before_withdraw(self):
  j=self.bought();ex.withdraw_preview('gate',j['id']);self.f.fee='0.0001'
  with self.assertRaises(ValueError):ex.withdraw_confirm('gate',j['id'])
  self.assertEqual(len(self.f.posts),1)
 def test_sale_reserves_fee_and_rounds(self):
  j=ex.preview('gate',{'action':'sell'});d=j['data'];self.assertLessEqual(Decimal(d['quantity'])*Decimal('1.001'),2)
  ex.confirm('gate',j['id']);self.assertEqual(ex.reconcile('gate',j['id'])['state'],'done')
 def test_expired_calculation(self):
  j=self.preview()
  with app.db() as c:c.execute('UPDATE exchange_jobs SET created=0')
  with self.assertRaises(ValueError):ex.confirm('gate',j['id'])
 def test_insufficient_balance_before_confirmation(self):
  j=self.preview();self.f.free='0'
  with self.assertRaises(ValueError):ex.confirm('gate',j['id'])
  self.assertEqual(self.f.posts,[])
 def test_auth_and_csrf(self):
  c=app.app.test_client()
  self.assertEqual(c.get('/api/exchanges/gate/jobs').status_code,401)
  self.assertEqual(c.post('/api/exchanges/gate/preview',json={}).status_code,403)
  self.assertEqual(c.post('/api/exchange-deposits',json={}).status_code,403)

class Adapter(unittest.TestCase):
 def client(self,v):
  c=object.__new__(clients.Client);c.venue=v;c.key='key';c.secret='secret';return c
 def test_bybit_address_pagination_and_case(self):
  c=self.client('bybit');calls=[]
  def request(path,p):
   calls.append(p)
   return {'rows':[],'nextPageCursor':'next'} if not p.get('cursor') else {'rows':[{'address':ADDRESS,'coin':'baseCoin','chain':'BSC','status':0,'verified':1,'tag':''}],'nextPageCursor':''}
  c.request=request;c.address_check(ADDRESS);self.assertEqual(len(calls),2)
 def test_bybit_address_unverified_blocked(self):
  c=self.client('bybit');c.request=lambda *a:{'rows':[{'address':ADDRESS,'coin':'BNB','chain':'BSC','status':0,'verified':0}],'nextPageCursor':''}
  with self.assertRaises(ValueError):c.address_check(ADDRESS)
 def test_bybit_address_24h_blocked(self):
  c=self.client('bybit');c.request=lambda *a:{'rows':[{'address':ADDRESS,'coin':'BNB','chain':'BSC','status':1,'verified':1}],'nextPageCursor':''}
  with self.assertRaises(ValueError):c.address_check(ADDRESS)
 def test_bybit_market_units_and_no_borrow(self):
  c=self.client('bybit');calls=[];c.request=lambda path,p,method:(calls.append(p) or {'orderId':'a'})
  c.order('abc','buy','50');c.order('def','sell','0.1')
  self.assertEqual(calls[0]['marketUnit'],'quoteCoin');self.assertEqual(calls[1]['marketUnit'],'baseCoin');self.assertEqual(calls[0]['isLeverage'],0)
 def test_bybit_withdraw_force_onchain_from_uta(self):
  c=self.client('bybit');calls=[];c.request=lambda path,p,method:(calls.append(p) or {'id':'a'})
  c.withdraw('abc',ADDRESS,'0.1');p=calls[0];self.assertEqual(p['accountType'],'UTA');self.assertEqual(p['feeType'],0);self.assertEqual(p['forceChain'],1);self.assertEqual(p['requestId'],'abc')
 def test_gate_market_buy_quote_and_spot_only(self):
  c=self.client('gate');calls=[];c.request=lambda path,p,method:(calls.append(p) or {'id':'a'})
  c.order('abc','buy','50');self.assertEqual(calls[0]['amount'],'50');self.assertFalse(calls[0]['auto_borrow']);self.assertEqual(calls[0]['account'],'spot')
 def test_signed_transports_exact_body_and_direct_proxy(self):
  for v in ['gate','bybit']:
   c=self.client(v);seen=[]
   class Response:
    def __enter__(self):return self
    def __exit__(self,*a):pass
    def read(self,*a):return b'{"id":"x"}' if v=='gate' else b'{"retCode":0,"result":{"id":"x"}}'
   class Opener:
    def open(self,req,timeout):seen.append(req);return Response()
   with patch.object(clients,'build_opener',return_value=Opener()) as opener,patch.object(clients.time,'time',return_value=1000):
    c.request('/test',{'amount':'50'},'POST')
   self.assertEqual(opener.call_args[0][0].proxies,{})
   req=seen[0];self.assertEqual(req.data,b'{"amount":"50"}')
   headers={k.lower():val for k,val in req.header_items()}
   if v=='gate':
    msg='POST\n/api/v4/test\n\n'+hashlib.sha512(req.data).hexdigest()+'\n1000'
    expected=hmac.new(b'secret',msg.encode(),hashlib.sha512).hexdigest();self.assertEqual(headers['sign'],expected)
   else:
    msg='1000000key10000'+req.data.decode();expected=hmac.new(b'secret',msg.encode(),hashlib.sha256).hexdigest();self.assertEqual(headers['x-bapi-sign'],expected)
 def test_unsafe_key_permissions(self):
  p=app.DATA/'gate-api.json';p.write_text('{"api_key":"key","api_secret":"secret"}');p.chmod(0o644)
  try:
   with self.assertRaises(ValueError):clients.Client('gate')
  finally:p.unlink()

class DepositSettings(unittest.TestCase):
 def test_separate_venues_and_assets(self):
  cfg=dict(wd.DEFAULTS,withdraw_gate_destination=ADDRESS,withdraw_bybit_destination='0x'+'2'*40,withdraw_gate_usdt_destination='0x'+'3'*40)
  self.assertEqual(wd.select_destination(cfg,'Gate')['withdraw_destination'],ADDRESS)
  self.assertEqual(wd.select_destination(cfg,'Gate','usdt')['withdraw_destination'],'0x'+'3'*40)
  self.assertEqual(wd.select_destination(cfg,'Bybit')['withdraw_destination'],'0x'+'2'*40)
  with self.assertRaises(ValueError):wd.select_destination(cfg,'MEXC')
  with self.assertRaises(ValueError):wd.select_destination(cfg,'Binance')

class ResponseFixtures(Adapter):
 def test_gate_network_fixture(self):
  c=self.client('gate')
  c.request=lambda path,*a,**k:([dict(chain='BSC',is_disabled=0,is_withdraw_disabled=0,is_tag=0,decimal='8')] if path.endswith('currency_chains') else [dict(currency='BNB',withdraw_fix_on_chains={'BSC':'0.0005'},withdraw_percent_on_chains={'BSC':'0%'},withdraw_amount_mini='0.001',withdraw_eachtime_limit='100',withdraw_day_limit_remain='99')])
  n=c.network();self.assertEqual(n['step'],'0.00000001');self.assertEqual(n['fee'],'0.0005');self.assertEqual(n['maximum'],'99')
 def test_bybit_network_fixture(self):
  c=self.client('bybit');c.request=lambda *a:{'rows':[{'coin':'BNB','chains':[dict(chain='BSC',chainWithdraw='1',withdrawFee='0.00001',withdrawPercentageFee='0',withdrawMin='0.0005',withdrawMax='200',minAccuracy='8')]}]}
  self.assertEqual(c.network()['step'],'0.00000001')
 def test_bybit_unlimited_network_and_invalid_limits(self):
  c=self.client('bybit')
  chain=dict(chain='BSC',chainWithdraw='1',withdrawFee='0.00001',withdrawPercentageFee='0',withdrawMin='0.0005',withdrawMax='-1',minAccuracy='8')
  c.request=lambda *a:{'rows':[{'coin':'BNB','chains':[chain]}]}
  self.assertIsNone(c.network()['maximum'])
  for value in ('-2', '', None, 'NaN', 'Infinity'):
   chain['withdrawMax']=value
   with self.subTest(value=value),self.assertRaises(ValueError):c.network()
  chain['withdrawMax']='-1';chain['withdrawFee']='-1'
  with self.assertRaises(ValueError):c.network()
 def test_missing_network_data_fails_readably(self):
  c=self.client('bybit');c.request=lambda *a:{'rows':[{'coin':'BNB','chains':[dict(chain='BSC',chainWithdraw='1')]}]}
  with self.assertRaisesRegex(ValueError,'неполные'):c.network()
 def test_bybit_funding_is_separate_readonly_balance(self):
  c=self.client('bybit');calls=[]
  def request(path,params):
   calls.append((path,params))
   return dict(accountType=params['accountType'],balance=[dict(coin='USDT',walletBalance='172.8519',transferBalance='170')])
  c.request=request
  self.assertEqual(c.funding_balances(),{'USDT':'170'})
  self.assertEqual(calls[0][1]['accountType'],'FUND')
  self.assertEqual(calls[0][1]['withBonus'],0)
  c.request=lambda *a:dict(accountType='UNIFIED',balance=[])
  with self.assertRaises(ValueError):c.funding_balances()
 def test_bybit_market_and_transferable_balance(self):
  c=self.client('bybit')
  def request(path,*a,**k):
   if path.endswith('coins-balance'):return dict(accountType='UNIFIED',balance=[dict(coin='BNB',walletBalance='2',transferBalance='1')])
   if path.endswith('instruments-info'):return {'list':[dict(symbol='BNBUSDT',status='Trading',lotSizeFilter=dict(basePrecision='0.00001',quotePrecision='0.01',minOrderAmt='5',maxOrderAmt='100000',maxMarketOrderQty='100'))]}
   if path.endswith('tickers'):return {'list':[dict(lastPrice='600')]}
   if path.endswith('fee-rate'):return {'list':[dict(takerFeeRate='0.001')]}
   raise AssertionError(path)
  c.request=request
  self.assertEqual(c.balances()['BNB'],'1');self.assertEqual(c.market()['fee'],'0.001')
 def test_bybit_execution_pagination_and_fees(self):
  c=self.client('bybit')
  def request(path,p):
   if path.endswith('history'):return {'list':[dict(orderId='1',orderLinkId='abc',symbol='BNBUSDT',side='Buy',orderType='Market',orderStatus='Filled',cumExecQty='0.1',cumExecValue='60')]}
   cursor=p.get('cursor')
   return {'list':[dict(orderId='1',execId='b' if cursor else 'a',execQty='0.05',feeCurrency='BNB',execFee='0.00005')],'nextPageCursor':'' if cursor else 'next'}
  c.request=request;r=c.order_status('abc','buy');self.assertTrue(r['terminal']);self.assertEqual(r['fees']['BNB'],'0.00010')
 def test_bybit_incomplete_execution_blocks(self):
  c=self.client('bybit')
  c.request=lambda path,p:({'list':[dict(orderId='1',orderLinkId='abc',symbol='BNBUSDT',side='Buy',orderType='Market',orderStatus='Filled',cumExecQty='0.1',cumExecValue='60')]} if path.endswith('history') else {'list':[],'nextPageCursor':''})
  with self.assertRaisesRegex(ValueError,'не все'):c.order_status('abc','buy')
 def test_gate_order_checks_custom_id(self):
  c=self.client('gate');r=dict(id='1',text='t-abc',currency_pair='BNB_USDT',side='buy',type='market',status='closed',filled_amount='0.1',filled_total='60',fee='0.0001',fee_currency='BNB')
  c.request=lambda *a:r
  self.assertEqual(c.order_status('abc','buy')['fees']['BNB'],'0.0001')
  with self.assertRaises(ValueError):c.order_status('wrong','buy')
 def test_gate_failed_onchain_stays_unresolved(self):
  c=self.client('gate');c.request=lambda *a:[dict(id='1',currency='BNB',chain='BSC',address=ADDRESS,amount='0.1',status='FAIL')]
  r=c.history()[0];self.assertFalse(r['done']);self.assertFalse(r['failed'])
