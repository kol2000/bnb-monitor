import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import test_monitor as base
import archive_auto as a


class AutomaticArchiveTests(unittest.TestCase):
    def setUp(self):
        self.fixture = base.MonitorTests(); self.fixture.setUp()
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.patches = [patch.object(a, 'DATA', root), patch.object(a, 'PLAN', root/'plan.json'),
                        patch.object(a, 'JOURNAL', root/'move.json'),
                        patch.object(a, 'settings', return_value={'sheets_enabled': True,
                          'sheets_tab': a.move.SOURCE, 'sheets_id': 'test'})]
        for p in self.patches: p.start()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(lambda: [p.stop() for p in reversed(self.patches)])

    def test_dust_limits_and_status_are_both_required(self):
        e={'bnb_wei': str(a.move.MAX_BNB), 'usdt_units': str(a.move.MAX_USDT)}
        self.assertTrue(a.eligible('has_balance', e))
        for status in ('unknown', 'active', 'age_unknown'):
            self.assertFalse(a.eligible(status, e))
        self.assertFalse(a.eligible('has_balance', {**e, 'bnb_wei': str(a.move.MAX_BNB+1)}))
        self.assertFalse(a.eligible('has_balance', {**e, 'usdt_units': str(a.move.MAX_USDT+1)}))
        self.assertFalse(a.eligible('verified_snapshot', {}))
        self.assertFalse(a.eligible('has_balance', {'bnb_wei': None}))

    def test_pending_plan_resumes_without_scanning(self):
        a.atomic(a.PLAN, {'selected': {'1': 'address'}})
        with patch.object(a.preview, 'scan') as scan:
            self.assertEqual(a.prepare(), 10)
            scan.assert_not_called()

    def test_pending_journal_resumes_without_scanning(self):
        a.atomic(a.JOURNAL, {'state': 'submitting'})
        with patch.object(a.preview, 'scan') as scan:
            self.assertEqual(a.prepare(), 10)
            scan.assert_not_called()

    def test_completed_move_keeps_audit_and_clears_plan(self):
        a.atomic(a.JOURNAL, {'job': 'abc', 'state': 'done', 'cleanup': 'done'})
        a.atomic(a.PLAN, {})
        with patch.object(a.move, 'main') as main, patch.object(a.move, 'JOURNAL'), patch.object(a.move, 'APPROVED'):
            a.apply()
            main.assert_called_once_with(allow_empty=True)
        self.assertFalse(a.PLAN.exists())
        self.assertFalse(a.JOURNAL.exists())
        self.assertTrue((a.DATA/'archive-auto-done-abc.json').exists())

    def test_ambiguous_move_stays_pending(self):
        a.atomic(a.JOURNAL, {'job': 'abc', 'state': 'submitting'})
        with patch.object(a.move, 'main'), patch.object(a.move, 'JOURNAL'), patch.object(a.move, 'APPROVED'):
            with self.assertRaises(a.RemoteError): a.apply()
        self.assertTrue(a.JOURNAL.exists())

    def test_changed_sheet_blocks_new_move(self):
        a.atomic(a.PLAN, {'spreadsheet': 'other', 'selected': {}})
        with patch.object(a.move, 'main') as main, patch.object(a.move, 'JOURNAL'):
            with self.assertRaises(a.RemoteError): a.apply()
            main.assert_not_called()

    def test_no_longer_eligible_candidates_do_not_block_queue(self):
        a.atomic(a.PLAN, {'spreadsheet': 'test', 'selected': {'1': 'address'}})
        with patch.object(a.move, 'main'), patch.object(a.move, 'JOURNAL'), patch.object(a.move, 'APPROVED'):
            a.apply()
        self.assertFalse(a.PLAN.exists())

    def test_queue_scans_new_wallet_without_moving(self):
        self.fixture.wallet()
        with a.db() as c:
            a.preview.schema(c)
            c.execute('DELETE FROM archive_activity_preview')
        with patch.object(a.preview, 'scan', return_value={'rpc_transfer_requests':100,'failed':0}) as scan:
            self.assertEqual(a.prepare(), 0)
            self.assertEqual(scan.call_args.kwargs['requests'], 100)
        self.assertFalse(a.PLAN.exists())

    def test_verified_candidate_creates_plan_without_sheet_write(self):
        self.fixture.wallet()
        with a.db() as c:
            a.preview.schema(c)
            c.execute('DELETE FROM archive_activity_preview')
            row=dict(c.execute('SELECT * FROM wallets LIMIT 1').fetchone())
            a.preview.start_wallet(c,row,tip=100,stamp=1,cutoff=90)
            c.execute("UPDATE archive_activity_preview SET decision='candidate'")
        with patch.object(a.preview, 'verify_candidate', return_value=('has_balance','',{'bnb_wei':'1','usdt_units':'0'})), patch.object(a.move, 'Sheets') as sheets:
            self.assertEqual(a.prepare(), 10)
            sheets.assert_not_called()
        self.assertEqual(json.loads(a.PLAN.read_text())['selected'],{str(row['id']):row['address'].lower()})
