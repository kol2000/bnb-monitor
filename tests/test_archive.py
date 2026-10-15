"""Offline archive scenarios: fake Google sheets and fake history, no real seeds/API."""
import copy
import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch, MagicMock
import test_monitor as base
import wallet_archive as a
from sheets_guard import sheet_guard

app=base.mod
A=base.A;B=base.B
OLD=int(time.time()-45*86400)

def props(title,i):
    return {'title':title,'sheetId':i,'sheetType':'GRID','gridProperties':{'rowCount':20,'columnCount':6}}
def snapshot():
    return {a.ACTIVE_TAB:{'props':props(a.ACTIVE_TAB,1),'rows':[
        ['email','адрес','кодовая сид фраза','остаток BNB (BSC)','ссылка','остаток USDT (BSC)'],
        ['One',A,'FAKE SECRET A',0,'link-a',0],['Two',B,'FAKE SECRET B',0,'link-b',0]]},
        a.ARCHIVE_TAB:{'props':props(a.ARCHIVE_TAB,2),'rows':[
        ['email','адрес','кодовая сид фраза','остаток BNB (BSC)']]}}

class HistoryTests(unittest.TestCase):
    def record(self,stamp=OLD):
        return {'status':'1','message':'OK','result':[{'timeStamp':str(stamp),'hash':'0x'+'1'*64,'from':A,'to':B}]}
    def test_latest_across_native_internal_tokens_nfts(self):
        values=[self.record(OLD),self.record(OLD+1),self.record(OLD+4),self.record(OLD+2),self.record(OLD+3)]
        with patch.object(a,'explorer_request',side_effect=values) as request,patch.object(a.time,'sleep'):
            self.assertEqual(a.last_activity(A,'fake'),OLD+4)
        self.assertEqual([call.args[1] for call in request.call_args_list],list(a.ACTIONS))
    def test_api_failure_is_never_inactivity(self):
        bad=[{}, {'status':'0','message':'NOTOK','result':'Max rate limit reached'},
             {'status':'0','message':'NOTOK','result':[]},self.record(int(time.time()+3600))]
        wrong=self.record();wrong['result'][0]['from']='0x'+'c'*40;wrong['result'][0]['to']='0x'+'d'*40;bad.append(wrong)
        for response in bad:
            with self.assertRaises(app.RemoteError):a.parse_history(response,A)
    def test_no_transactions_is_distinct(self):
        self.assertIsNone(a.parse_history({'status':'0','message':'No transactions found','result':[]},A))
    def test_requests_use_bsc_latest_one(self):
        from urllib.parse import parse_qs,urlparse
        opener=MagicMock()
        opener.open.return_value.__enter__.return_value.read.return_value=json.dumps(self.record()).encode()
        with patch.object(a,'build_opener',return_value=opener):
            a.explorer_request(A,'txlist','FAKE_KEY_12345')
        req=opener.open.call_args.args[0];url=urlparse(req.full_url);q=parse_qs(url.query)
        self.assertEqual(url.hostname,'api.etherscan.io')
        self.assertEqual(q['chainid'],['56']);self.assertEqual(q['sort'],['desc'])
        self.assertEqual(q['offset'],['1']);self.assertEqual(q['address'],[A])
        with patch.object(a,'build_opener',side_effect=Exception('FAKE_KEY_12345')):
            try:a.explorer_request(A,'txlist','FAKE_KEY_12345')
            except Exception as e:self.assertNotIn('FAKE_KEY_12345',str(e))

class ArchiveTests(unittest.TestCase):
    post=base.MonitorTests.post
    wallet=base.MonitorTests.wallet
    def setUp(self):
        base.MonitorTests.setUp(self)
        with app.db() as c:
            for table in ('archive_jobs','generated_batches','withdrawals','mexc_jobs','email_outbox'):c.execute('DELETE FROM '+table)
            c.execute("UPDATE settings SET value='true' WHERE key='sheets_enabled'")
            c.execute("UPDATE settings SET value=? WHERE key='sheets_tab'",(json.dumps(a.ACTIVE_TAB),))
        self.wid=self.wallet()
        self.data=snapshot();self.markers=[];self.dispatches=[]
        self.patch('history_key',return_value='FAKE_KEY')
        self.patch('properties',side_effect=lambda cfg,token:({k:v['props'] for k,v in self.data.items()},self.markers))
        self.patch('read_values',side_effect=self.read)
        self.patch('sheet_snapshot',side_effect=lambda *args:copy.deepcopy(self.data))
        self.patch('last_activity',return_value=OLD)
        self.patch('dispatch_batch',side_effect=self.dispatch)
        p=patch('sheets_sync.google_credentials',return_value=SimpleNamespace(token='fake'));p.start();self.addCleanup(p.stop)
        self.addCleanup(self.clean)
    def clean(self):
        with app.db() as c:c.execute('DELETE FROM archive_jobs')
        a.key_path().unlink(missing_ok=True)
    def patch(self,name,**kw):
        p=patch.object(a,name,**kw);mock=p.start();self.addCleanup(p.stop);return mock
    def read(self,cfg,token,title,cell_range):
        rows=self.data[title]['rows']
        if cell_range.startswith('B1:'):return [[r[1]] if len(r)>1 else [] for r in rows]
        return [r[:2] for r in rows]
    def dispatch(self,cfg,token,payload):
        self.dispatches.append(payload)
        src=self.data[a.ACTIVE_TAB]['rows'];dst=self.data[a.ARCHIVE_TAB]['rows']
        for req in payload['requests']:
            if 'copyPaste' in req:
                cp=req['copyPaste'];s=cp['source'];d=cp['destination']
                while len(dst)<d['endRowIndex']:dst.append([])
                row=dst[d['startRowIndex']]
                while len(row)<d['endColumnIndex']:row.append('')
                row[d['startColumnIndex']:d['endColumnIndex']]=src[s['startRowIndex']][s['startColumnIndex']:s['endColumnIndex']]
            elif 'deleteDimension' in req:
                r=req['deleteDimension']['range'];del src[r['startIndex']:r['endIndex']]
            elif 'createDeveloperMetadata' in req:self.markers.append(req['createDeveloperMetadata']['developerMetadata'])
    def preview(self):
        j=a.start();a.tick();a.tick();return a.load(j['id'])
    def move(self):
        j=self.preview();a.enqueue(j['id'],[A]);a.tick();a.tick();return a.load(j['id'])
    def test_scan_does_not_write_and_no_history_not_candidate(self):
        j=a.start()
        with patch.object(a,'last_activity',return_value=None):a.tick()
        a.tick();j=a.load(j['id'])
        self.assertEqual(j['state'],'preview')
        self.assertEqual(j['data']['results'][0]['state'],'no_history')
        self.assertEqual(self.dispatches,[])
        self.assertNotIn('FAKE SECRET',json.dumps(a.public(j)))
    def test_explicit_selection_full_row_moved_and_monitor_removed(self):
        j=self.move();self.assertEqual(j['state'],'done')
        self.assertEqual(len(self.dispatches),1)
        self.assertEqual(self.data[a.ARCHIVE_TAB]['rows'][1][2],'FAKE SECRET A')
        self.assertEqual(self.data[a.ACTIVE_TAB]['rows'][1][1],B)
        with app.db() as c:self.assertIsNone(c.execute('SELECT id FROM wallets WHERE address=?',(A,)).fetchone())
        self.assertNotIn('FAKE SECRET',json.dumps(self.dispatches))
        self.assertNotIn('FAKE SECRET',self.client.get('/api/archive').get_data(as_text=True))
    def test_confirm_idempotent(self):
        j=self.preview();a.enqueue(j['id'],[A]);a.enqueue(j['id'],[A]);a.tick();a.tick()
        a.enqueue(j['id'],[A]);a.tick();self.assertEqual(len(self.dispatches),1)
    def test_new_activity_cancels_before_google_write(self):
        j=self.preview();a.enqueue(j['id'],[A])
        with patch.object(a,'last_activity',return_value=int(time.time())):a.tick()
        self.assertEqual(a.load(j['id'])['state'],'failed');self.assertFalse(self.dispatches)
    def test_pending_transfer_never_archived(self):
        j=self.preview();a.enqueue(j['id'],[A]);a.tick()
        with app.db() as c:c.execute("INSERT INTO withdrawals(id,wallet_id,created,state,plan,updated) VALUES('active',?,0,'pending1','{}',0)",(self.wid,))
        a.tick();self.assertEqual(a.load(j['id'])['state'],'failed');self.assertFalse(self.dispatches)
    def test_timeout_after_apply_resolves_via_atomic_marker(self):
        def ambiguous(*args):self.dispatch(*args);raise RuntimeError('timeout')
        with patch.object(a,'dispatch_batch',side_effect=ambiguous):j=self.move()
        self.assertEqual(j['state'],'done');self.assertEqual(len(self.dispatches),1)
    def test_timeout_before_apply_no_second_dispatch_and_writes_blocked(self):
        with patch.object(a,'dispatch_batch',side_effect=RuntimeError('timeout')) as send:j=self.move()
        self.assertEqual(send.call_count,1);self.assertEqual(j['state'],'uncertain')
        with self.assertRaises(app.RemoteError):
            with sheet_guard():pass
        self.post('archive/'+j['id']+'/check',{})
        with patch.object(a,'dispatch_batch') as send:a.tick();send.assert_not_called()
        self.assertEqual(a.load(j['id'])['state'],'uncertain')
    def test_rejection_does_not_block_future_scans(self):
        with patch.object(a,'dispatch_batch',side_effect=a.BatchRejected('Denied')):j=self.move()
        self.assertEqual(j['state'],'failed');self.assertFalse(self.dispatches)
    def test_changed_sheet_before_dispatch_aborts(self):
        j=self.preview();a.enqueue(j['id'],[A]);a.tick()
        changed=copy.deepcopy(self.data);changed[a.ACTIVE_TAB]['rows'][1][0]='Changed'
        with patch.object(a,'sheet_snapshot',side_effect=[copy.deepcopy(self.data),changed]):a.tick()
        self.assertEqual(a.load(j['id'])['state'],'failed');self.assertFalse(self.dispatches)
    def test_duplicate_archive_address_aborts(self):
        self.data[a.ARCHIVE_TAB]['rows'].append(['One',A,'fake'])
        j=self.move();self.assertEqual(j['state'],'failed');self.assertFalse(self.dispatches)
    def test_cancel_during_history_does_not_resurrect_scan(self):
        j=a.start()
        def cancel(*args):self.post('archive/'+j['id']+'/cancel',{});return OLD
        with patch.object(a,'last_activity',side_effect=cancel):a.tick()
        self.assertEqual(a.load(j['id'])['state'],'cancelled')
    def test_no_unknown_address_selection(self):
        j=self.preview()
        with self.assertRaises(ValueError):a.enqueue(j['id'],['0x'+'c'*40])
    def test_seed_key_never_in_public_api_and_permissions(self):
        self.post('archive/key',{'key':'FAKE_HISTORY_KEY_12345'})
        self.assertEqual(a.key_path().stat().st_mode&0o777,0o600)
        self.assertNotIn('FAKE_HISTORY_KEY',self.client.get('/api/archive').get_data(as_text=True))
        self.assertEqual(self.client.post('/api/archive/scan',json={}).status_code,403)
        self.assertEqual(app.app.test_client().get('/api/archive').status_code,401)
    def test_financial_intents_blocked_while_uncertain(self):
        with patch.object(a,'dispatch_batch',side_effect=RuntimeError('timeout')):self.move()
        with app.db() as c:
            with self.assertRaises(ValueError):a.assert_not_archiving(c,A)
            a.assert_not_archiving(c,B)
    def test_delete_rows_bottom_up_and_archive_after_last_used(self):
        snap=snapshot();snap[a.ARCHIVE_TAB]['rows'] += [['Old','0x'+'c'*40,'fake'],[],['Old2','0x'+'d'*40,'fake']]
        payload,_=a.build_move(snap,[B,A],'test')
        deletes=[r['deleteDimension']['range']['startIndex'] for r in payload['requests'] if 'deleteDimension' in r]
        self.assertEqual(deletes,[2,1])
        copies=[r['copyPaste'] for r in payload['requests'] if 'copyPaste' in r and r['copyPaste']['source']['startRowIndex']>0]
        self.assertEqual([r['destination']['startRowIndex'] for r in copies],[4,5])
    def test_incompatible_headers_abort(self):
        snap=snapshot();snap[a.ARCHIVE_TAB]['rows'][0][1]='Wrong column'
        with self.assertRaises(ValueError):a.build_move(snap,[A],'test')

    def test_history_preserved_pending_notifications_cancelled(self):
        cfg=app.settings();app.record_balance(self.wid,1,1,cfg);app.record_balance(self.wid,2,2,cfg)
        with app.db() as c:
            eid=c.execute('SELECT id FROM events').fetchone()[0]
            c.execute("UPDATE events SET delivery='pending'")
            c.execute("INSERT INTO email_outbox(event_id,recipient,sender,delivery,message_id) VALUES (?,'to','from','pending','test')",(eid,))
        self.move()
        with app.db() as c:
            self.assertEqual(c.execute('SELECT delivery FROM events').fetchone()[0],'cancelled')
            self.assertEqual(c.execute('SELECT delivery FROM email_outbox').fetchone()[0],'cancelled')
    def test_cancel_validating_before_write(self):
        j=self.preview();a.enqueue(j['id'],[A]);a.tick()
        self.assertEqual(self.post('archive/'+j['id']+'/cancel',{}).status_code,200)
        a.tick();self.assertEqual(a.load(j['id'])['state'],'cancelled');self.assertFalse(self.dispatches)
    def test_active_transaction_after_wallet_deleted_still_blocks(self):
        with app.db() as c:
            c.execute("INSERT INTO withdrawals(id,wallet_id,created,state,plan,updated) VALUES('active',?,0,'pending1',?,0)",(self.wid,json.dumps({'source':A})))
            c.execute('DELETE FROM wallets')
            self.assertTrue(a.pending(c,A))
