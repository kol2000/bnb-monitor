import unittest
from unittest.mock import patch
import test_monitor as base
from app import db
import archive_budget as b
import archive_auto as a

class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.fixture=base.MonitorTests(); self.fixture.setUp()
        with db() as c:
            c.execute('DROP TABLE IF EXISTS archive_cu_usage')
            c.execute('DROP TABLE IF EXISTS archive_cu_state')

    def test_reservation_persists_and_paces_without_double_charge(self):
        self.assertEqual(b.reserve('nr_getAssetTransfersCount',1000),0)
        self.assertGreater(b.reserve('nr_getAssetTransfersCount',1000),1)
        with db() as c:
            self.assertEqual(c.execute('SELECT SUM(cost) FROM archive_cu_usage').fetchone()[0],150)
        self.assertEqual(b.reserve('nr_getAssetTransfersCount',1002),0)

    def test_initial_daily_cap_and_automatic_resume(self):
        b.reserve('eth_call',1000)
        with db() as c: c.execute('INSERT INTO archive_cu_usage VALUES(?,?)',(1000,54850))
        with self.assertRaises(b.BudgetPause): b.reserve('eth_call',1002)
        self.assertEqual(b.reserve('eth_call',1000+86401),0)

    def test_rolling_cap_even_after_initial_period(self):
        b.reserve('eth_call',1000)
        now=1000+32*86400
        with db() as c: c.execute('INSERT INTO archive_cu_usage VALUES(?,?)',(now-2*86400,6000000))
        with self.assertRaises(b.BudgetPause): b.reserve('eth_call',now)

    def test_retries_also_pass_guard(self):
        import archive_preview as p
        from urllib.error import URLError
        with patch.object(p,'key_from_file',return_value='test'),patch.object(p,'RPC_GUARD') as guard,patch.object(p.time,'sleep'):
            with self.assertRaises(p.IndexerError):
                p.rpc('eth_call',[],opener=lambda *a,**kw: (_ for _ in ()).throw(TimeoutError()))
            self.assertEqual(guard.call_count,2)

    def test_checkpoint_never_advances_on_active_or_unknown(self):
        with patch.object(a.preview,'verification_block') as block:
            for status in ('active','unknown'):
                a.advance_checkpoint({},status,{})
            block.assert_not_called()

    def test_checkpoint_preserves_overlap_and_requires_same_block(self):
        self.fixture.wallet()
        with db() as c:
            a.preview.schema(c)
            c.execute('DELETE FROM archive_activity_preview')
            wallet=dict(c.execute('SELECT * FROM wallets LIMIT 1').fetchone())
            a.preview.start_wallet(c,wallet,tip=100,stamp=1,cutoff=90)
            c.execute("UPDATE archive_activity_preview SET decision='candidate'")
            row=dict(c.execute('SELECT * FROM archive_activity_preview').fetchone())
        e={'head':100000,'head_timestamp':123,'head_hash':'hash'}
        with patch.object(a.preview,'verification_block',return_value=(123,'different')):
            a.advance_checkpoint(row,'age_unknown',e)
        with db() as c: self.assertEqual(c.execute('SELECT tip_block FROM archive_activity_preview').fetchone()[0],100)
        with patch.object(a.preview,'verification_block',return_value=(123,'hash')):
            a.advance_checkpoint(row,'age_unknown',e)
        with db() as c:
            r=c.execute('SELECT tip_block,cutoff_block FROM archive_activity_preview').fetchone()
            self.assertEqual(tuple(r),(50000,90))
