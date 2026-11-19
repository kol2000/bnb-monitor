"""Offline final candidate checks; no live keys, transfers or Sheets access."""
import json
import time
import unittest
from unittest.mock import patch
import test_monitor as base
import app
import archive_preview as p


class VerificationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = base.MonitorTests()
        self.fixture.setUp()
        self.fixture.wallet()
        self.now = int(time.time())
        self.row = {'wallet_id':1, 'address':base.A, 'decision':'candidate',
                    'scan_method':'count','cutoff_block':100,'tip_block':200}
        self.old_nonce = 3
        self.new_nonce = 3
        self.bnb = 0
        self.usdt = 0
        self.count = 0
        self.calls = []

    def rpc(self, method, params):
        self.calls.append((method, params))
        if method == 'eth_chainId': return '0x38'
        if method == 'eth_blockNumber': return hex(250)
        if method == 'eth_getBlockByNumber':
            n = int(params[0],16)
            return {'number':hex(n), 'timestamp':hex(self.now if n == 250 else self.now-91*86400), 'hash':'0x'+'a'*64}
        if method == 'eth_getTransactionCount':
            return hex(self.old_nonce if params[1] == hex(100) else self.new_nonce)
        if method == 'nr_getAssetTransfersCount': return hex(self.count)
        if method == 'eth_getBalance': return hex(self.bnb)
        if method == 'eth_call': return '0x'+format(self.usdt,'064x')
        raise AssertionError(method)

    def verify(self):
        with patch.object(p,'rpc',side_effect=self.rpc):
            return p.verify_candidate(self.row,sleeper=lambda _:None)

    def test_old_empty_wallet_verified_and_tail_has_no_gap(self):
        status, _, evidence = self.verify()
        self.assertEqual(status,'verified_snapshot')
        self.assertEqual(evidence['head'],250)
        queries = [v[0] for m,v in self.calls if m == 'nr_getAssetTransfersCount']
        self.assertEqual(len(queries),2)
        for direction, q in zip(p.DIRECTIONS,queries):
            self.assertEqual(q[direction],base.A)
            self.assertEqual((q['fromBlock'],q['toBlock']),('0xc9','0xfa'))
            self.assertFalse(q['excludeZeroValue'])

    def test_outgoing_nonce_detects_failed_transactions(self):
        self.new_nonce = 4
        self.assertEqual(self.verify()[0],'active')

    def test_tail_activity_excludes_candidate(self):
        self.count = 1
        self.assertEqual(self.verify()[0],'active')

    def test_zero_nonce_does_not_assert_new_or_old_age(self):
        self.old_nonce = self.new_nonce = 0
        self.assertEqual(self.verify()[0],'age_unknown')

    def test_nonzero_balance_in_either_asset_blocks_verified_status(self):
        self.bnb = 1
        self.assertEqual(self.verify()[0],'has_balance')
        self.bnb = 0
        self.usdt = 1
        self.assertEqual(self.verify()[0],'has_balance')

    def test_missing_archive_state_or_indexer_lag_fails_closed(self):
        original = self.rpc
        for fail_method in ('eth_getTransactionCount','nr_getAssetTransfersCount'):
            def fail(method, params):
                if method == fail_method: raise p.IndexerError('blockNum not reached')
                return original(method,params)
            with patch.object(p,'rpc',side_effect=fail):
                self.assertEqual(p.verify_candidate(self.row,sleeper=lambda _:None)[0],'unknown')

    def test_malformed_usdt_response_is_not_zero(self):
        original = self.rpc
        with patch.object(p,'rpc',side_effect=lambda m,a: '0x' if m=='eth_call' else original(m,a)):
            self.assertEqual(p.verify_candidate(self.row,sleeper=lambda _:None)[0],'unknown')

    def test_reorg_fails_closed(self):
        with patch.object(p,'rpc',side_effect=self.rpc), patch.object(p,'verification_block',side_effect=[(self.now,'0x'+'a'*64),(self.now,'0x'+'b'*64)]):
            self.assertEqual(p.verify_candidate(self.row,sleeper=lambda _:None)[0],'unknown')

    def test_verification_preserves_wallet_and_preview(self):
        with app.db() as c:
            p.schema(c)
            wallet=dict(c.execute('SELECT * FROM wallets').fetchone())
            p.start_wallet(c,wallet,200,self.now,100)
            c.execute("UPDATE archive_activity_preview SET decision='candidate'")
            before=dict(c.execute('SELECT * FROM archive_activity_preview').fetchone())
        with patch.object(p,'rpc',side_effect=self.rpc):
            result=p.verify_candidates(sleeper=lambda _:None)
        self.assertEqual(result,{'verified_snapshot':1})
        with app.db() as c:
            self.assertEqual(dict(c.execute('SELECT * FROM wallets').fetchone()),wallet)
            self.assertEqual(dict(c.execute('SELECT * FROM archive_activity_preview').fetchone()),before)
        self.assertEqual(p.verification_report()[0]['status'],'verified_snapshot')

    def test_changed_wallet_during_request_cannot_be_verified(self):
        with app.db() as c:
            p.schema(c)
            wallet=dict(c.execute('SELECT * FROM wallets').fetchone())
            p.start_wallet(c,wallet,200,self.now,100)
            c.execute("UPDATE archive_activity_preview SET decision='candidate'")
        def mutate(row, **kw):
            with app.db() as c:
                c.execute("UPDATE archive_activity_preview SET decision='active'")
            return 'verified_snapshot','test',{}
        with patch.object(p,'verify_candidate',side_effect=mutate):
            self.assertEqual(p.verify_candidates(sleeper=lambda _:None),{'unknown':1})

if __name__=='__main__': unittest.main()
