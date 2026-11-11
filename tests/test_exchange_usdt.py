"""Offline tests. No exchange keys, orders, or withdrawals are used."""
import os
import tempfile
import time
import json
import unittest
from decimal import Decimal
from unittest.mock import patch
_tmp=tempfile.TemporaryDirectory()
os.environ.setdefault('DATA_DIR',_tmp.name)
os.environ.setdefault('ADMIN_PASSWORD','test-password-only-123')
os.environ.setdefault('SECRET_KEY','test-secret-'*5)
import app
import exchange_usdt as u
import exchange_clients as clients
import exchanges
import binance
A='0x'+'1'*40

class Fake:
 def __init__(self):
  self.fee='0.15';self.available=Decimal('100');self.posts=[];self.timeout=False;self.rows=None;self.allowed=True;self.maximum='1000';self.step='0.000001'
 def quote(self,target,source):
  if not self.allowed:raise ValueError('Whitelist')
  return dict(network='BSC',fee=self.fee,minimum='1',maximum=self.maximum,step=self.step),self.available
 def send(self,ident,d):
  self.posts.append((ident,dict(d)))
  if self.timeout:raise ValueError('Timeout')
  return 'withdraw1'
 def history(self,ident,d,supplied):
  return self.rows if self.rows is not None else [dict(id='withdraw1',request_id=ident,coin='USDT',network='BSC',address=A,amount=d['amount'],created=str(int(d['sent_at']*1000)),status='success',done=True,failed=False,txid='')]

class Workflow(unittest.TestCase):
 def setUp(self):
  with app.db() as c:
   for table in ('usdt_withdrawals','binance_jobs','exchange_jobs'):c.execute('DELETE FROM '+table)
  self.f=Fake();p=patch.object(u,'Adapter',return_value=self.f);p.start();self.addCleanup(p.stop)
 def tearDown(self):
  with app.db() as c:
   for table in ('usdt_withdrawals','binance_jobs','exchange_jobs'):c.execute('DELETE FROM '+table)
 def preview(self,v='bybit',**kwargs):return u.preview(v,dict(address=A,amount='10',**kwargs))
 def test_preview_read_only(self):
  for v in u.VENUES:
   j=self.preview(v);self.assertEqual(j['data']['reserve'],'10.15');self.assertEqual(j['state'],'preview')
  self.assertEqual(self.f.posts,[])
 def test_maximum_reserves_fee(self):
  j=self.preview(maximum=True);self.assertEqual(Decimal(j['data']['amount']),Decimal('99.85'))
 def test_limit_caps_maximum(self):
  self.f.maximum='50';self.assertEqual(Decimal(self.preview(maximum=True)['data']['amount']),50)
 def test_invalid_inputs(self):
  for b in ({'amount':'NaN'},{'amount':'Infinity'},{'amount':'-1'},{'amount':'0'},{'amount':'0.5'}, {'amount':'100'},{'amount':'10.0000001'},{'address':'x'},{'address':'0x'+'0'*40},{'network':'ETH'},{'source':'EARN'}):
   with self.subTest(b=b),self.assertRaises(ValueError):u.preview('bybit',dict(address=A,amount='10')|b)
 def test_confirm_once_all_venues(self):
  for v in u.VENUES:
   j=self.preview(v);u.confirm(v,j['id'])
   with self.assertRaises(ValueError):u.confirm(v,j['id'])
   self.assertEqual(u.reconcile(v,j['id'])['state'],'done')
  self.assertEqual(len(self.f.posts),3)
 def test_timeout_never_reposts(self):
  j=self.preview();self.f.timeout=True;r=u.confirm('bybit',j['id']);self.assertEqual(r['state'],'sending')
  self.assertIn('Timeout',r['error'])
  for _ in range(3):u.reconcile('bybit',j['id'])
  with self.assertRaises(ValueError):u.confirm('bybit',j['id'])
  self.assertEqual(len(self.f.posts),1)
  r=u.reconcile('bybit',j['id'],'withdraw1');self.assertEqual(r['state'],'done')
 def test_gate_and_binance_lost_response_recover_by_client_id(self):
  self.f.timeout=True
  for v in ('binance','gate'):
   j=self.preview(v);u.confirm(v,j['id']);self.assertEqual(u.reconcile(v,j['id'])['state'],'done')
  self.assertEqual(len(self.f.posts),2)
 def test_whitelist_recheck(self):
  j=self.preview();self.f.allowed=False
  with self.assertRaises(ValueError):u.confirm('bybit',j['id'])
  self.assertFalse(self.f.posts)
 def test_changed_fee(self):
  j=self.preview();self.f.fee='1'
  with self.assertRaises(ValueError):u.confirm('bybit',j['id'])
  self.assertFalse(self.f.posts)
 def test_changed_balance(self):
  j=self.preview();self.f.available=Decimal(5)
  with self.assertRaises(ValueError):u.confirm('bybit',j['id'])
  self.assertFalse(self.f.posts)
 def test_more_balance_never_increases_confirmed_max(self):
  j=self.preview(maximum=True);self.f.available=Decimal('200');u.confirm('bybit',j['id'])
  self.assertEqual(Decimal(self.f.posts[0][1]['amount']),Decimal('99.85'))
 def test_stale_preview(self):
  j=self.preview()
  with app.db() as c:c.execute('UPDATE usdt_withdrawals SET created=? WHERE id=?',(time.time()-181,j['id']))
  with self.assertRaises(ValueError):u.confirm('bybit',j['id'])
  self.assertFalse(self.f.posts)
 def test_parallel_previews_only_one_sends(self):
  a=self.preview();b=self.preview();u.confirm('bybit',a['id'])
  with self.assertRaises(ValueError):u.confirm('bybit',b['id'])
  self.assertEqual(len(self.f.posts),1)
 def test_existing_bnb_blocks_usdt(self):
  with app.db() as c:c.execute("INSERT INTO exchange_jobs VALUES('bnb','bybit',?,'withdrawing','{}','')",(time.time(),))
  with self.assertRaises(ValueError):self.preview()
 def test_usdt_blocks_bnb_reservation(self):
  j=self.preview();u.confirm('bybit',j['id'])
  b=exchanges.insert('bybit',{})
  with self.assertRaises(ValueError):exchanges.reserve(b,'preview','ordering')
 def test_wrong_history_cannot_complete(self):
  for field,value in [('coin','BNB'),('network','ETH'),('address','0x'+'2'*40),('amount','9'),('created','0'),('id','other')]:
   with self.subTest(field=field):
    self.setUp();j=self.preview();u.confirm('bybit',j['id']);d=u.job('bybit',j['id'])['data'];self.f.rows=self.f.history(j['id'],d,None);self.f.rows[0][field]=value
    self.assertEqual(u.reconcile('bybit',j['id'])['state'],'sending')
 def test_empty_history_not_failure_or_retry(self):
  j=self.preview();u.confirm('bybit',j['id']);self.f.rows=[]
  r=u.reconcile('bybit',j['id']);self.assertEqual(r['state'],'sending');self.assertTrue(r['error']);self.assertEqual(len(self.f.posts),1)
 def test_confirmed_rejection_closes(self):
  j=self.preview();u.confirm('bybit',j['id']);d=u.job('bybit',j['id'])['data'];self.f.rows=self.f.history(j['id'],d,None);self.f.rows[0].update(done=False,failed=True,status='Reject')
  self.assertEqual(u.reconcile('bybit',j['id'])['state'],'failed')
 def test_requests_auth_and_csrf(self):
  c=app.app.test_client();self.assertEqual(c.get('/api/usdt-withdrawals/bybit').status_code,401)
  with c.session_transaction() as s:s['auth']=True;s['csrf']='test-token'
  self.assertEqual(c.post('/api/usdt-withdrawals/bybit/preview',json={}).status_code,403)
  r=c.post('/api/usdt-withdrawals/bybit/preview',json={'address':A,'amount':'10'},headers={'X-CSRF-Token':'test-token'});self.assertEqual(r.status_code,200)
  ident=r.json['id'];r=c.post('/api/usdt-withdrawals/bybit/'+ident+'/confirm',json={},headers={'X-CSRF-Token':'test-token'});self.assertEqual(r.status_code,200)
  self.assertEqual(c.get('/api/usdt-withdrawals/bybit').json['items'][0]['id'],ident)
 def test_template_has_three_forms(self):
  html=app.app.test_client().get('/').get_data(as_text=True)
  self.assertEqual(html.count('class="card usdt-form"'),3);self.assertIn('/static/exchange-usdt.js',html)

class Adapters(unittest.TestCase):
 def client(self,v):
  c=object.__new__(clients.Client);c.venue=v;return c
 def test_withdraw_asset_source_and_request_id(self):
  for v in ('gate','bybit'):
   c=self.client(v)
   with patch.object(c,'request',return_value={'id':'123'}) as r:
    self.assertEqual(c.withdraw('req1',A,'10',coin='USDT',account_type='FUND'),'123')
    p=r.call_args.args[1];self.assertEqual(p['currency' if v=='gate' else 'coin'],'USDT')
    if v=='bybit':self.assertEqual(p['accountType'],'FUND');self.assertEqual(p['feeType'],0);self.assertEqual(p['forceChain'],1)
 def test_history_queries_usdt(self):
  for v in ('gate','bybit'):
   c=self.client(v)
   with patch.object(c,'request',return_value=[] if v=='gate' else {'rows':[]}) as r:
    c.history(coin='USDT');self.assertEqual(r.call_args.args[1]['currency' if v=='gate' else 'coin'],'USDT')
 def test_usdt_whitelist_not_bnb(self):
  c=self.client('bybit');row=dict(address=A,coin='BNB',chain='BSC',status=0,verified=1)
  with patch.object(c,'request',return_value={'rows':[row]}):
   with self.assertRaises(ValueError):c.address_check(A,coin='USDT')
   row['coin']='USDT';c.address_check(A,coin='USDT')
 def test_bybit_usdt_network_and_disabled_message(self):
  c=self.client('bybit');chain=dict(chain='BSC',chainWithdraw='1',withdrawFee='0.15',withdrawPercentageFee='0',withdrawMin='1',withdrawMax='-1',minAccuracy='6')
  with patch.object(c,'request',return_value={'rows':[{'coin':'USDT','chains':[chain]}]}) as r:
   n=c.network('USDT');self.assertEqual(n['fee'],'0.15');self.assertIsNone(n['maximum']);self.assertEqual(n['step'],'0.000001');self.assertEqual(r.call_args.args[1]['coin'],'USDT')
   chain['chainWithdraw']='0'
   with self.assertRaisesRegex(ValueError,'USDT/BSC'):c.network('USDT')
 def test_gate_usdt_network(self):
  c=self.client('gate');chains=[dict(chain='BSC',is_disabled=0,is_withdraw_disabled=0,is_tag=0,decimal='6')];fees=[dict(currency='USDT',withdraw_fix_on_chains={'BSC':'0.2'},withdraw_percent='0',withdraw_amount_mini='1',withdraw_eachtime_limit='1000',withdraw_day_limit_remain='500')]
  with patch.object(c,'request',side_effect=[chains,fees]) as r:
   n=c.network('USDT');self.assertEqual(n['fee'],'0.2');self.assertEqual(n['maximum'],'500')
   for call in r.call_args_list:self.assertEqual(call.args[1]['currency'],'USDT')
 def test_bybit_funding_quote(self):
  c=self.client('bybit');a=object.__new__(u.Adapter);a.venue='bybit';a.client=c
  with patch.object(c,'network',return_value={'fee':'1'}) as n,patch.object(c,'address_check') as w,patch.object(c,'funding_balances',return_value={'USDT':'15'}) as f,patch.object(c,'balances') as b:
   self.assertEqual(a.quote(A,'FUND')[1],Decimal(15));n.assert_called_with(coin='USDT');w.assert_called_with(A,coin='USDT');b.assert_not_called()
 def test_binance_quote_uses_usdt_bsc(self):
  from unittest.mock import Mock
  a=object.__new__(u.Adapter);a.venue='binance';a.client=Mock()
  n=dict(network='BSC',withdrawEnable=True,withdrawFee='0.1',withdrawMin='1',withdrawMax='10000',withdrawIntegerMultiple='0.000001')
  a.client.call.side_effect=[{'questionnaireCountryCode':'NIL'},{'canWithdraw':True,'balances':[{'asset':'USDT','free':'100','locked':'0'}]},[{'coin':'BNB','networkList':[]},{'coin':'USDT','networkList':[n]}]]
  network,balance=a.quote(A,'UTA');self.assertEqual(balance,100);self.assertEqual(network['fee'],'0.1');self.assertEqual(network['step'],'0.000001')
 def test_embedded_key_install_scripts_compile(self):
  import ast
  from pathlib import Path
  for filename in ('configure_binance.py','configure_exchange.py'):
   tree=ast.parse(Path(filename).read_text())
   for node in ast.walk(tree):
    if isinstance(node,ast.Constant) and isinstance(node.value,str) and ('from app import DATA' in node.value or 'from binance import Client' in node.value):compile(node.value,filename+' embedded','exec')
 def test_binance_withdraw_usdt(self):
  a=object.__new__(u.Adapter);a.venue='binance'
  from unittest.mock import Mock
  a.client=Mock();a.client.call.return_value={'id':'123'}
  a.send('req',dict(address=A,amount='10',source='UTA'))
  p=a.client.call.call_args.args[1];self.assertEqual(p['coin'],'USDT');self.assertEqual(p['walletType'],0);self.assertEqual(p['withdrawOrderId'],'req')

if __name__=='__main__':unittest.main()
