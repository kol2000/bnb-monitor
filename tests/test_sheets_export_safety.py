"""Offline regression tests for bounded Sheets exports and unknown write outcomes."""
import io
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import URLError

import sheets_sync as m


class ExportSafetyTests(unittest.TestCase):
    def setUp(self):
        self.cfg = {'sheets_enabled': True, 'sheets_tab': 'bnb кошельки',
                    'sheets_id': 'fake-spreadsheet-id', 'usdt_enabled': False}
        self.changes = []
        self.writes = []
        self.snapshot = {
            'valueRanges': [
                {'values': [['name', 'address', 'other', 'остаток BNB (BSC)']]},
                {'values': [['0x' + 'a'*40]]}
            ]
        }
        self.data = [{'range': "'bnb кошельки'!D" + str(i+2), 'values': [[float(i)]]}
                     for i in range(205)]

    @contextmanager
    def fake_db(self):
        class Cursor:
            def execute(self, *args, **kwargs):
                return [{'address': '0x'+'a'*40, 'balance': '1'}]
        yield Cursor()

    def status(self, **changes):
        self.changes.append(changes)

    def patch_export(self, callback):
        return (
            patch.object(m, 'db', self.fake_db),
            patch.object(m, 'settings', return_value=self.cfg),
            patch.object(m, 'google_credentials', return_value=SimpleNamespace(token='fake')),
            patch.object(m, 'balance_updates', return_value=self.data),
            patch.object(m, 'export_request', side_effect=callback),
            patch.object(m, 'set_status', side_effect=self.status),
        )

    def test_100_cell_batches_and_success_status(self):
        def request(cfg, token, suffix, payload=None):
            if payload is None:
                return self.snapshot
            self.writes.append(payload['data'])
            return {'totalUpdatedCells': len(payload['data'])}
        from contextlib import ExitStack
        with ExitStack() as stack:
            for p in self.patch_export(request):
                stack.enter_context(p)
            m.export_balances(1)
        self.assertEqual(list(map(len, self.writes)), [100, 100, 5])
        self.assertEqual([s['sheets_export_last_count'] for s in self.changes
                          if 'sheets_export_last_count' in s], [0, 100, 200, 205])
        self.assertTrue(any(s.get('sheets_export_count') == 205 and
                            s.get('sheets_export_error', 'missing') is None for s in self.changes))

    def test_unknown_write_verified_without_second_write(self):
        def request(cfg, token, suffix, payload=None):
            if payload is None:
                return self.snapshot
            self.writes.append(payload['data'])
            raise m.WriteOutcomeUnknown('таймаут')
        from contextlib import ExitStack
        with ExitStack() as stack:
            for p in self.patch_export(request):
                stack.enter_context(p)
            confirmed = stack.enter_context(patch.object(m, 'confirm_written_values', return_value=True))
            m.export_balances(1)
        self.assertEqual(len(self.writes), 3)
        self.assertEqual(confirmed.call_count, 3)
        self.assertTrue(any(s.get('sheets_export_count') == 205 for s in self.changes))

    def test_unknown_write_not_confirmed_stops_without_resending(self):
        def request(cfg, token, suffix, payload=None):
            if payload is None:
                return self.snapshot
            self.writes.append(payload['data'])
            raise m.WriteOutcomeUnknown('тайм-аут записи')
        from contextlib import ExitStack
        with ExitStack() as stack:
            for p in self.patch_export(request):
                stack.enter_context(p)
            stack.enter_context(patch.object(m, 'confirm_written_values', return_value=False))
            m.export_balances(1)
        self.assertEqual(len(self.writes), 1)
        self.assertTrue(any('повторная отправка заблокирована' in str(s.get('sheets_export_error', ''))
                            for s in self.changes))
        self.assertFalse(any('sheets_export_success' in s for s in self.changes))

    def test_write_timeout_is_60_seconds_and_single_attempt(self):
        with patch.object(m, 'urlopen', side_effect=URLError(TimeoutError('timeout'))) as send:
            with self.assertRaises(m.WriteOutcomeUnknown):
                m.export_request(self.cfg, 'fake', 'values:batchUpdate', {'data': []})
        self.assertEqual(send.call_count, 1)
        self.assertEqual(send.call_args.kwargs['timeout'], 60)

    def test_read_timeout_remains_20_seconds(self):
        with patch.object(m, 'urlopen', return_value=io.BytesIO(b'{}')) as send:
            m.export_request(self.cfg, 'fake', 'values/A1')
        self.assertEqual(send.call_args.kwargs['timeout'], 20)

    def test_readback_requires_exact_numeric_values(self):
        requested = [{'range': "'bnb кошельки'!D2", 'values': [[1.2]]},
                     {'range': "'bnb кошельки'!D3", 'values': [[0.0]]}]
        with patch.object(m, 'export_request', return_value={
            'valueRanges': [{'values': [[1.2]]}, {'values': [[0]]}]
        }):
            self.assertTrue(m.confirm_written_values(self.cfg, 'fake', requested))
        with patch.object(m, 'export_request', return_value={
            'valueRanges': [{'values': [[1.2]]}, {'values': [[0.01]]}]
        }):
            self.assertFalse(m.confirm_written_values(self.cfg, 'fake', requested))
        with patch.object(m, 'export_request', return_value={
            'valueRanges': [{'values': [[1.2]]}, {}]
        }):
            self.assertFalse(m.confirm_written_values(self.cfg, 'fake', requested))


if __name__ == '__main__':
    unittest.main()
