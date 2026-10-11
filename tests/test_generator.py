"""Offline tests: temporary wallets, mocked Google, no funds."""
import os,tempfile,unittest,json,stat
from unittest.mock import patch
_tmp=tempfile.TemporaryDirectory()
os.environ.setdefault('DATA_DIR',_tmp.name)
os.environ.setdefault('ADMIN_PASSWORD','test-password-only-123')
os.environ.setdefault('SECRET_KEY','test-secret-'*5)
import app
import wallet_generator as g
from eth_account import Account
class GeneratorTests(unittest.TestCase):
 def setUp(self):
  with app.db() as c:c.execute('DELETE FROM generated_batches')
  for p in app.DATA.glob('generated-*'):p.unlink()
  self.cfg={'sheets_id':'test','sheets_tab':'bnb кошельки','sheet_id':7}
  self.records=[{'address':'0x'+'1'*40,'seed':'fake seed used only in mocked test'}]
  self.written=[];self.fail=False;self.wrong=False;self.posts=0
  for name,value in [('prepare_cfg',lambda count:(self.cfg.copy(),'test-token')),('create_records',lambda count:self.records),('request_google',self.google),('read_range',self.read)]:
   p=patch.object(g,name,side_effect=value);p.start();self.addCleanup(p.stop)
 def read(self,cfg,token,ran):return [['адрес']]+[[r['address']] for r in self.written]
 def google(self,cfg,token,suffix,payload=None):
  if suffix==':batchUpdate':
   self.posts+=1;self.written=list(self.records)
   if self.fail:raise app.RemoteError('no response')
   return {}
  if suffix.startswith('?fields='):return {'sheets':[{'properties':{'sheetId':7,'title':'bnb кошельки'}}]}
  return {'valueRanges':[{'values':[[r['address'],'wrong' if self.wrong else r['seed']]]} for r in self.written]}
 def test_append_and_idempotence_and_no_seed_in_public(self):
  result=g.generate('a'*32,1)
  self.assertEqual(result['state'],'done');self.assertNotIn(self.records[0]['seed'],json.dumps(result))
  self.assertFalse(g.stage_path('a'*32).exists())
  self.assertEqual(g.generate('a'*32,1)['state'],'done');self.assertEqual(self.posts,1)
 def test_timeout_keeps_seed_private_then_check_never_appends(self):
  self.fail=True
  result=g.generate('b'*32,1);self.assertEqual(result['state'],'uncertain')
  self.assertEqual(stat.S_IMODE(g.stage_path('b'*32).stat().st_mode),0o600)
  self.assertEqual(g.generate('b'*32,1)['state'],'uncertain');self.assertEqual(self.posts,1)
  with self.assertRaises(ValueError):g.generate('c'*32,1)
  with patch('sheets_sync.google_credentials') as cred:
   cred.return_value.token='test-token';self.assertEqual(g.verify('b'*32)['state'],'done')
  self.assertEqual(self.posts,1)
 def test_wrong_seed_never_marks_confirmed(self):
  self.wrong=True;self.assertEqual(g.generate('c'*32,1)['state'],'uncertain')
  self.assertTrue(g.stage_path('c'*32).exists())
 def test_count_and_id_validation(self):
  for n in [0,101,True,'2',1.5]:
   with self.assertRaises(ValueError):g.generate('a'*32,n)
  with self.assertRaises(ValueError):g.generate('../bad',1)
  self.assertEqual(self.posts,0)
 def test_append_payload_targets_only_new_b_c(self):
  a=g.append_payload(7,self.records)['requests'][0]['appendCells']
  self.assertEqual(a['sheetId'],7);self.assertEqual(a['rows'][0]['values'][0],{})
  self.assertEqual(len(a['rows'][0]['values']),3)
  self.assertEqual(a['rows'][0]['values'][1]['userEnteredValue']['stringValue'],self.records[0]['address'])
 def test_api_auth_csrf_and_seed_redaction(self):
  client=app.app.test_client()
  self.assertEqual(client.get('/api/generator').status_code,401)
  with client.session_transaction() as s:s.update(auth=True,csrf='test')
  self.assertEqual(client.post('/api/generator',json={'id':'d'*32,'count':1}).status_code,403)
  r=client.post('/api/generator',json={'id':'d'*32,'count':1},headers={'X-CSRF-Token':'test'})
  self.assertEqual(r.status_code,200);self.assertNotIn(self.records[0]['seed'],r.get_data(as_text=True))
class RealGenerationTests(unittest.TestCase):
 def test_derivation_and_unique_twelve_word_seeds(self):
  records=g.create_records(3)
  self.assertEqual(len({r['address'] for r in records}),3)
  for r in records:
   self.assertEqual(len(r['seed'].split()),12)
   self.assertEqual(Account.from_mnemonic(r['seed'],account_path=g.PATH).address,r['address'])
if __name__=='__main__':unittest.main()
