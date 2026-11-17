"""Offline safety and classification tests for archive activity preview."""
import time
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import test_monitor as base
import app
import archive_preview as preview


def item(at):
    return {'timestamp': datetime.fromtimestamp(at, timezone.utc).isoformat()}


class ArchivePreviewTests(unittest.TestCase):
    def setUp(self):
        self.fixture = base.MonitorTests()
        self.fixture.setUp()
        with app.db() as c:
            preview.schema(c)
            c.execute('DELETE FROM archive_activity_preview')
        self.fixture.wallet()

    def test_old_activity_candidate_after_three_complete_categories(self):
        past = time.time() - 91 * 86400
        calls = []
        def get(kind, address):
            calls.append(kind)
            return {'items': [item(past)] if kind == 'token-transfers' else [],
                    'next_page_params': None}
        decision, stamp, _ = preview.evaluate(base.A, get=get)
        self.assertEqual(decision, 'candidate')
        self.assertAlmostEqual(stamp, past)
        self.assertEqual(calls, list(preview.KINDS))

    def test_recent_internal_activity_prevents_candidate(self):
        def get(kind, address):
            ts = time.time() - (2 if kind == 'internal-transactions' else 120) * 86400
            return {'items': [item(ts)], 'next_page_params': None}
        self.assertEqual(preview.evaluate(base.A, get=get)[0], 'active')

    def test_no_transactions_is_unknown_not_candidate(self):
        get = lambda *_: {'items': [], 'next_page_params': None}
        self.assertEqual(preview.evaluate(base.A, get=get)[0], 'unknown')

    def test_missing_timestamp_and_wrong_order_fail_closed(self):
        with self.assertRaises(ValueError):
            preview.evaluate(base.A, get=lambda *_: {'items': [{}]})
        recent = time.time() - 86400
        old = time.time() - 120 * 86400
        with self.assertRaises(ValueError):
            preview.evaluate(base.A, get=lambda *_: {'items': [item(old), item(recent)]})

    def test_scan_error_does_not_archive_or_change_balances(self):
        with app.db() as c:
            before = dict(c.execute('SELECT * FROM wallets LIMIT 1').fetchone())
        def err(*_):
            raise RuntimeError('indexer unavailable')
        with patch.object(preview, 'fetch', side_effect=err):
            result = preview.scan(1, get=preview.fetch, sleeper=lambda _: None)
        self.assertEqual(result['unknown'], 1)
        self.assertEqual(result['failed'], 1)
        self.assertEqual(preview.report()[0]['decision'], 'unknown')
        with app.db() as c:
            after = dict(c.execute('SELECT * FROM wallets LIMIT 1').fetchone())
        self.assertEqual(before, after)

    def test_authenticated_preview_only(self):
        response = self.fixture.client.get('/api/archive-preview')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['mode'], 'preview-only')
        self.assertEqual(response.json['total'], 1)
        self.assertEqual(response.json['checked'], 0)
        self.assertEqual(app.app.test_client().get('/api/archive-preview').status_code, 401)

    def test_invalid_endpoint_and_address_rejected_before_network(self):
        with self.assertRaises(ValueError):
            preview.fetch('wrong-kind', base.A)
        with self.assertRaises(ValueError):
            preview.fetch('transactions', 'secret-phrase')
        with self.assertRaises(ValueError):
            preview.scan(100)


if __name__ == '__main__':
    unittest.main()
