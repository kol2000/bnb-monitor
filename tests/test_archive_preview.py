"""Offline NodeReal archive-preview tests: no live network, no wallet mutation."""
import json
import time
import unittest
from unittest.mock import patch

import test_monitor as base
import app
import archive_preview as preview


class ArchivePreviewTests(unittest.TestCase):
    def setUp(self):
        self.fixture=base.MonitorTests()
        self.fixture.setUp()
        with app.db() as c:
            preview.schema(c)
            c.execute('DELETE FROM archive_activity_preview')
        self.fixture.wallet()

    def test_transfer_response_validates_address_block_and_category(self):
        params={'transfers':[{'blockNum':'0x65','category':'20',
                              'to':base.A,'blockTimeStamp':int(time.time())}]}
        self.assertIsInstance(preview.transfer_result(params,base.A,'toAddress',100,200),float)
        with self.assertRaises(preview.IndexerError):
            preview.transfer_result(params,base.A,'fromAddress',100,200)
        with self.assertRaises(preview.IndexerError):
            preview.transfer_result(params,base.A,'toAddress',200,300)

    def test_api_omission_is_not_a_valid_empty_page(self):
        for response in ({}, {'transfers':[], 'pageKey':'has-more'},
                         {'transfers':None}):
            with self.assertRaises(preview.IndexerError):
                preview.transfer_result(response,base.A,'fromAddress',100,200)

    def test_steps_checkpoint_both_directions_without_archiving_wallet(self):
        with app.db() as c:
            before=dict(c.execute('SELECT * FROM wallets').fetchone())
            preview.start_wallet(c,before,tip=100,stamp=time.time(),cutoff=90)
            row=dict(c.execute('SELECT * FROM archive_activity_preview').fetchone())
        with patch.object(preview,'rpc',return_value={'transfers':[]}) as send:
            result=preview.step(before,row)
            self.assertEqual(result,'scanning')
            with app.db() as c:
                row=dict(c.execute('SELECT * FROM archive_activity_preview').fetchone())
            self.assertEqual(row['direction'],1)
            self.assertEqual(row['cursor_block'],100)
            result=preview.step(before,row)
        self.assertEqual(result,'candidate')
        self.assertEqual(send.call_count,2)
        with app.db() as c:
            after=dict(c.execute('SELECT * FROM wallets').fetchone())
        self.assertEqual(before,after)

    def test_activity_stops_early_and_marks_active(self):
        with app.db() as c:
            before=dict(c.execute('SELECT * FROM wallets').fetchone())
            preview.start_wallet(c,before,tip=100,stamp=time.time(),cutoff=90)
            row=dict(c.execute('SELECT * FROM archive_activity_preview').fetchone())
        data={'transfers':[{'blockNum':'0x61','category':'external',
                            'from':base.A,'blockTimeStamp':int(time.time())}]}
        with patch.object(preview,'rpc',return_value=data):
            result=preview.step(before,row)
        self.assertEqual(result,'active')
        self.assertEqual(preview.report()[0]['decision'],'active')

    def test_scan_limit_and_persisted_progress(self):
        with patch.object(preview,'safe_tip',return_value=(210000,time.time())),\
             patch.object(preview,'find_cutoff',return_value=1),\
             patch.object(preview,'rpc',return_value={'transfers':[]}):
            r=preview.scan(limit=1,requests=1,sleeper=lambda _:None)
            self.assertEqual(r['rpc_transfer_requests'],1)
            x=preview.report()[0]
            self.assertEqual(x['decision'],'scanning')
            self.assertEqual(x['cursor_block'],109999)
            r=preview.scan(limit=1,requests=1,sleeper=lambda _:None)
            self.assertEqual(r['rpc_transfer_requests'],1)
            x=preview.report()[0]
            self.assertEqual(x['cursor_block'],9998)
            self.assertEqual(x['requests'],2)

    def test_scan_failure_never_marks_candidate(self):
        with patch.object(preview,'safe_tip',return_value=(100,time.time())),\
             patch.object(preview,'find_cutoff',return_value=90),\
             patch.object(preview,'rpc',side_effect=preview.IndexerError('blocked')):
            r=preview.scan(limit=1,requests=1,sleeper=lambda _:None)
        self.assertEqual(r['failed'],1)
        self.assertEqual(preview.report()[0]['decision'],'scanning')

    def test_timeout_retried_once_without_exposing_key(self):
        with patch.object(preview,'key_from_file',return_value='private-secret'):
            calls=[]
            class Response:
                def __enter__(self): return self
                def __exit__(self,*_): pass
                def read(self, _): return b'{"jsonrpc":"2.0","id":1,"result":"0x38"}'
            def network(req,timeout):
                calls.append(timeout)
                if len(calls)==1: raise TimeoutError('private-secret')
                return Response()
            with patch.object(preview.time,'sleep') as delay:
                result=preview.rpc('eth_chainId',[],opener=network)
            self.assertEqual(result,'0x38')
            self.assertEqual(calls,[20,20])
            delay.assert_called_once_with(2)

    def test_http_rate_limit_not_retried_and_key_not_disclosed(self):
        from urllib.error import HTTPError
        with patch.object(preview,'key_from_file',return_value='private-secret'):
            calls=[]
            def network(req,timeout):
                calls.append(1)
                raise HTTPError(req.full_url,429,'private-secret',None,None)
            with self.assertRaises(preview.IndexerError) as err:
                preview.rpc('eth_chainId',[],opener=network)
            self.assertEqual(len(calls),1)
            self.assertIn('HTTP 429',str(err.exception))
            self.assertNotIn('private-secret',str(err.exception))

    def test_resume_previous_unknown_failure_without_restart(self):
        with app.db() as c:
            wallet=dict(c.execute('SELECT * FROM wallets').fetchone())
            preview.start_wallet(c,wallet,tip=200000,stamp=time.time(),cutoff=1)
            c.execute("""UPDATE archive_activity_preview SET decision='unknown',
                detail='Ошибка NodeReal; сканирование не подтверждено',
                cursor_block=150000,direction=0,requests=68 WHERE wallet_id=?""",
                (wallet['id'],))
        bounds=[]
        def fake_rpc(method,params):
            if method=='nr_getAssetTransfers':
                bounds.append(params[0]['toBlock'])
                return {'transfers':[]}
            raise AssertionError(method)
        with patch.object(preview,'safe_tip',return_value=(210000,time.time())),\
             patch.object(preview,'find_cutoff',return_value=1),\
             patch.object(preview,'rpc',side_effect=fake_rpc):
            result=preview.scan(limit=1,requests=1,sleeper=lambda _:None)
        self.assertEqual(result['rpc_transfer_requests'],1)
        self.assertEqual(bounds,[hex(150000)])
        row=preview.report()[0]
        self.assertEqual(row['requests'],69)
        self.assertEqual(row['decision'],'scanning')
        self.assertEqual(row['cursor_block'],49999)

    def test_rpc_error_message_is_redacted_but_explanatory(self):
        secret='super-private-api-key'
        address='0x'+'a'*40
        message='Invalid fromBlock for '+address+' token '+secret+' at https://host.example/v1/'+secret
        class Response:
            def __enter__(self): return self
            def __exit__(self,*args): pass
            def read(self,*args):
                return json.dumps({'jsonrpc':'2.0','id':1,
                    'error':{'code':-32602,'message':message}}).encode()
        with patch.object(preview,'key_from_file',return_value=secret):
            with self.assertRaises(preview.IndexerError) as thrown:
                preview.rpc('nr_getAssetTransfers',[{}],opener=lambda *a,**kw:Response())
        output=str(thrown.exception)
        self.assertIn('-32602',output)
        self.assertIn('Invalid fromBlock',output)
        self.assertNotIn(secret,output)
        self.assertNotIn(address,output)
        self.assertNotIn('https://host.example',output)

    def test_scan_failure_exposes_safe_error_and_keeps_cursor(self):
        with patch.object(preview,'safe_tip',return_value=(100,time.time())),\
             patch.object(preview,'find_cutoff',return_value=90),\
             patch.object(preview,'rpc',side_effect=preview.IndexerError(
                 'NodeReal JSON-RPC code -32602: Invalid params')):
            report=preview.scan(limit=1,requests=1,sleeper=lambda _:None)
        self.assertEqual(report['failed'],1)
        self.assertIn('Invalid params',report['error'])
        self.assertEqual(preview.report()[0]['decision'],'scanning')
        self.assertEqual(preview.report()[0]['requests'],0)

    def test_explicit_wallet_id_preserves_existing_cursor(self):
        with app.db() as c:
            wallet=dict(c.execute('SELECT * FROM wallets').fetchone())
            preview.start_wallet(c,wallet,tip=200000,stamp=time.time(),cutoff=1)
            c.execute("""UPDATE archive_activity_preview SET
                cursor_block=150000, requests=68 WHERE wallet_id=?""",
                (wallet['id'],))
        seen=[]
        def fake_rpc(method,params):
            seen.append(params[0]['toBlock'])
            return {'transfers':[]}
        with patch.object(preview,'safe_tip',return_value=(210000,time.time())),\
             patch.object(preview,'find_cutoff',return_value=1),\
             patch.object(preview,'rpc',side_effect=fake_rpc):
            result=preview.scan(requests=1,wallet_id=wallet['id'],sleeper=lambda _:None)
        self.assertEqual(result['rpc_transfer_requests'],1)
        self.assertEqual(seen,[hex(150000)])
        with app.db() as c:
            saved=c.execute('SELECT requests,cursor_block FROM archive_activity_preview WHERE wallet_id=?',
                            (wallet['id'],)).fetchone()
        self.assertEqual(saved['requests'],69)
        self.assertEqual(saved['cursor_block'],49999)

    def test_explicit_unknown_wallet_rejected(self):
        with patch.object(preview,'safe_tip',return_value=(210000,time.time())),\
             patch.object(preview,'find_cutoff',return_value=1):
            with self.assertRaisesRegex(ValueError,'Wallet ID not found'):
                preview.scan(wallet_id=999999)
        with self.assertRaises(ValueError):
            preview.scan(wallet_id=0)

    def test_wallet_removed_during_scan_cannot_be_recreated(self):
        with app.db() as c:
            original=dict(c.execute('SELECT * FROM wallets').fetchone())
            preview.start_wallet(c,original,tip=100,stamp=time.time(),cutoff=90)
            row=dict(c.execute('SELECT * FROM archive_activity_preview').fetchone())
            c.execute('DELETE FROM wallets WHERE id=?',(original['id'],))
        with patch.object(preview,'rpc',return_value={'transfers':[]}):
            self.assertEqual(preview.step(original,row),'removed')
        self.assertEqual(preview.report(),[])

    def test_authenticated_preview_only(self):
        r=self.fixture.client.get('/api/archive-preview')
        self.assertEqual(r.status_code,200)
        self.assertEqual(r.json['mode'],'preview-only')
        self.assertEqual(r.json['total'],1)
        self.assertEqual(r.json['checked'],0)
        self.assertEqual(app.app.test_client().get('/api/archive-preview').status_code,401)

    def test_no_secret_in_network_errors_and_read_only_methods(self):
        with self.assertRaises(ValueError):
            preview.rpc('eth_sendRawTransaction',['secret'])
        with patch.object(preview,'key_from_file',return_value='secret-example'):
            def bad(*args,**kwargs):
                raise OSError('https://bsc-mainnet.nodereal.io/v1/secret-example')
            with self.assertRaises(preview.IndexerError) as thrown:
                preview.rpc('eth_chainId',[],opener=bad)
        self.assertNotIn('secret-example',str(thrown.exception))

    def test_invalid_request_budget(self):
        for count in (0,101):
            with self.assertRaises(ValueError):
                preview.scan(requests=count)


if __name__=='__main__':
    unittest.main()
