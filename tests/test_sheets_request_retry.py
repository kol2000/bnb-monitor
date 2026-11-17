"""Offline regression tests for Google Sheets GET retries and write safety."""
import io
import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

import app
import sheets_sync as m


class SheetsRequestRetryTests(unittest.TestCase):
    def setUp(self):
        self.cfg = {'sheets_id': 'fake-spreadsheet-id'}
        self.delay = patch.object(m.time, 'sleep')
        self.sleep = self.delay.start()
        self.addCleanup(self.delay.stop)

    def test_read_recovers_from_transient_dns_error(self):
        with patch.object(m, 'urlopen', side_effect=[
            URLError(OSError('DNS')), io.BytesIO(b'{"values":[["ok"]]}')
        ]) as send:
            self.assertEqual(m.export_request(self.cfg, 'fake-token', 'values/A1'), {'values': [['ok']]})
            self.assertEqual(send.call_count, 2)
            self.assertEqual(self.sleep.call_count, 1)

    def test_read_retries_rate_limit_then_succeeds(self):
        err = HTTPError('https://sheets.googleapis.com', 429, 'Too many', {}, None)
        with patch.object(m, 'urlopen', side_effect=[
            err, io.BytesIO(b'{"values":[]}')
        ]) as send:
            self.assertEqual(m.export_request(self.cfg, 'fake-token', 'values/A1'), {'values': []})
            self.assertEqual(send.call_count, 2)

    def test_read_does_not_retry_permission_failure(self):
        err = HTTPError('https://sheets.googleapis.com', 403, 'Forbidden', {}, None)
        with patch.object(m, 'urlopen', side_effect=err) as send:
            with self.assertRaisesRegex(app.RemoteError, 'чтения'):
                m.export_request(self.cfg, 'fake-token', 'values/A1')
            self.assertEqual(send.call_count, 1)

    def test_transient_read_failure_is_bounded_and_clear(self):
        with patch.object(m, 'urlopen', side_effect=URLError(OSError('DNS'))) as send:
            with self.assertRaisesRegex(app.RemoteError, 'DNS'):
                m.export_request(self.cfg, 'fake-token', 'values/A1')
            self.assertEqual(send.call_count, 3)
            self.assertEqual(self.sleep.call_count, 2)

    def test_write_never_retried_after_ambiguous_error(self):
        with patch.object(m, 'urlopen', side_effect=URLError(OSError('connection reset'))) as send:
            with self.assertRaisesRegex(app.RemoteError, 'записать'):
                m.export_request(self.cfg, 'fake-token', 'values:batchUpdate',
                                 {'valueInputOption': 'RAW', 'data': []})
            self.assertEqual(send.call_count, 1)
            self.sleep.assert_not_called()

    def test_read_json_error_not_retried(self):
        with patch.object(m, 'urlopen', return_value=io.BytesIO(b'not-json')) as send:
            with self.assertRaisesRegex(app.RemoteError, 'JSON'):
                m.export_request(self.cfg, 'fake-token', 'values/A1')
            self.assertEqual(send.call_count, 1)
