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
            self.assertEqual(x['cursor_block'],110000)
            r=preview.scan(limit=1,requests=1,sleeper=lambda _:None)
            self.assertEqual(r['rpc_transfer_requests'],1)
            x=preview.report()[0]
            self.assertEqual(x['cursor_block'],10000)
            self.assertEqual(x['requests'],2)

    def test_scan_failure_never_marks_candidate(self):
        with patch.object(preview,'safe_tip',return_value=(100,time.time())),\
             patch.object(preview,'find_cutoff',return_value=90),\
             patch.object(preview,'rpc',side_effect=preview.IndexerError('blocked')):
            r=preview.scan(limit=1,requests=1,sleeper=lambda _:None)
        self.assertEqual(r['failed'],1)
        self.assertEqual(preview.report()[0]['decision'],'unknown')

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
