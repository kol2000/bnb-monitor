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
  self.cfg={'sheets_id':'test','sheets_tab':'bnb кошельки','sheet_id':7,'row_count':826,'target_rows':[53]}
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
  payload=g.append_payload(7,self.records,[53],826)
  self.assertEqual(len(payload['requests']),1)
  a=payload['requests'][0]['updateCells']
  self.assertEqual(a['range'],{'sheetId':7,'startRowIndex':52,'endRowIndex':53,'startColumnIndex':1,'endColumnIndex':3})
  self.assertEqual(a['fields'],'userEnteredValue')
  self.assertEqual(len(a['rows'][0]['values']),2)
  self.assertEqual(a['rows'][0]['values'][0]['userEnteredValue']['stringValue'],self.records[0]['address'])
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
class PlacementTests(unittest.TestCase):
 def select(self,identities,seeds,count,row_count=826):
  from urllib.parse import parse_qs
  cfg={'sheets_tab':'bnb кошельки','row_count':row_count}
  def google(cfg,token,suffix):
   ranges=parse_qs(suffix.split('?')[1])['ranges']
   return {'valueRanges':[{'values':[[seeds[int(r.split('!C')[1])]]]} if int(r.split('!C')[1]) in seeds else {} for r in ranges]}
  with patch.object(g,'read_range',return_value=identities),patch.object(g,'request_google',side_effect=google):
   return g.vacant_rows(cfg,'test',count)
 def test_dynamic_end_and_formatted_tail(self):
  identities=[['name','address']]+[['name','address'] for _ in range(51)]
  self.assertEqual(self.select(identities,{},9),list(range(53,62)))
  self.assertEqual(self.select(identities[:10],{},2),[11,12])
 def test_reuses_gaps_and_preserves_partial_identities(self):
  self.assertEqual(self.select([['header'],['name'],[],[],['','address'],[],['=IF(TRUE,"","")']],{3:'occupied seed'},3),[4,6,8])
 def test_never_places_on_existing_generated_addresses_at_bottom(self):
  identities=[['header']]+[['wallet']]*51+[[]]*765+[['','generated address']]*9
  self.assertEqual(self.select(identities,{},9),list(range(53,62)))
 def test_grows_only_when_no_empty_rows_remain(self):
  self.assertEqual(self.select([['header'],['wallet']],{},2,row_count=2),[3,4])
  p=g.append_payload(7,[{'address':'a','seed':'s'},{'address':'b','seed':'t'}],[3,4],2)
  self.assertEqual(p['requests'][0],{'appendDimension':{'sheetId':7,'dimension':'ROWS','length':2}})
 def test_limit_without_silent_overwrite(self):
  with self.assertRaises(ValueError):self.select([['occupied']]*4999,{},1,row_count=4999)
if __name__=='__main__':unittest.main()
