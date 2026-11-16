"""Offline regression tests for manual Binance withdrawal resolution.

Run: python -m unittest discover -s tests -p 'test_binance_manual_resolution.py'
No real Binance requests, purchases, or withdrawals.
"""
import json
import time
import unittest
from unittest.mock import patch

from test_binance import BinanceTests
import app
import binance as m


class ManualResolutionTests(BinanceTests):
    MANUAL_ID = 'a' * 32

    def pending(self):
        j = self.bought()
        m.withdraw_preview(j['id'])
        m.withdraw_confirm(j['id'])
        self.assertEqual(m.get_job(j['id'])['state'], 'withdrawing')
        with app.db() as c:
            row = c.execute('SELECT data FROM binance_jobs WHERE id=?', (j['id'],)).fetchone()
            d = json.loads(row['data'])
            d.pop('withdraw_id', None)
            c.execute('UPDATE binance_jobs SET data=? WHERE id=?', (json.dumps(d), j['id']))
        return j['id']

    def manual(self, ident, **overrides):
        from datetime import datetime, timezone
        original = m.get_job(ident)['data']
        row = dict(id=self.MANUAL_ID, withdrawOrderId=None,
                   address=original['address'], coin='BNB', network='BSC',
                   status=6, txId='0x' + '1' * 64,
                   applyTime=datetime.fromtimestamp(time.time(), timezone.utc).strftime('%Y-%m-%d %H:%M:%S'))
        row.update(overrides)
        return row

    def test_closes_only_after_verified_manual_withdrawal(self):
        ident = self.pending()
        manual = self.manual(ident)
        with patch.object(m, 'history', return_value=[manual]), patch.object(m, 'account', return_value={
            'balances': [{'asset': 'BNB', 'free': '0', 'locked': '0'}]
        }):
            result = m.resolve_manual_withdrawal(ident, self.MANUAL_ID)
        self.assertEqual(result['state'], 'closed')
        self.assertEqual(result['data']['manual_resolution']['manual_withdraw_id'], self.MANUAL_ID)
        self.assertFalse(any(method == 'POST' for _, _, method in self.fake.calls[-2:]))
        with self.assertRaises(m.BinanceError):
            m.resolve_manual_withdrawal(ident, self.MANUAL_ID)

    def test_rejects_original_request_in_history(self):
        ident = self.pending()
        original = self.manual(ident, id='b' * 32, withdrawOrderId=ident)
        with patch.object(m, 'history', return_value=[original, self.manual(ident)]):
            with self.assertRaises(m.BinanceError):
                m.resolve_manual_withdrawal(ident, self.MANUAL_ID)
        self.assertEqual(m.get_job(ident)['state'], 'withdrawing')

    def test_rejects_wrong_address_and_old_manual_withdrawal(self):
        ident = self.pending()
        from datetime import datetime, timezone
        old = datetime.fromtimestamp(m.get_job(ident)['data']['withdraw_time'] - 3600, timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        for manual in [self.manual(ident, address='0x' + '2' * 40), self.manual(ident, applyTime=old)]:
            with self.subTest(manual=manual), patch.object(m, 'history', return_value=[manual]):
                with self.assertRaises(m.BinanceError):
                    m.resolve_manual_withdrawal(ident, self.MANUAL_ID)
        self.assertEqual(m.get_job(ident)['state'], 'withdrawing')

    def test_rejects_nonzero_spot_balance(self):
        ident = self.pending()
        with patch.object(m, 'history', return_value=[self.manual(ident)]), patch.object(m, 'account', return_value={
            'balances': [{'asset': 'BNB', 'free': '0.1', 'locked': '0'}]
        }):
            with self.assertRaises(m.BinanceError):
                m.resolve_manual_withdrawal(ident, self.MANUAL_ID)
        self.assertEqual(m.get_job(ident)['state'], 'withdrawing')

    def test_rejects_history_failure(self):
        ident = self.pending()
        with patch.object(m, 'history', side_effect=m.BinanceError('Unavailable')):
            with self.assertRaises(m.BinanceError):
                m.resolve_manual_withdrawal(ident, self.MANUAL_ID)
        self.assertEqual(m.get_job(ident)['state'], 'withdrawing')


if __name__ == '__main__':
    unittest.main()
