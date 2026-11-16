"""Offline regression tests for explicit recovery from a historic Binance 401/-1002.

No test can send actual funds: Binance Client is mocked.
"""
import json
import time
from unittest.mock import patch

import app
import binance as m
from test_binance import BinanceTests


class LegacyDeniedWithdrawalTests(BinanceTests):
    def legacy_denied(self):
        job = self.bought()
        m.withdraw_preview(job['id'])
        original_call = self.fake.call
        def denied(path, *args, **kwargs):
            if path.endswith('/withdraw/apply'):
                raise m.BinanceError('Binance HTTP 401 · код Binance -1002. Запрос автоматически не повторяется.')
            if path.endswith('/apiRestrictions'):
                return {'enableWithdrawals': True, 'ipRestrict': True}
            if path.endswith('/history'):
                return []
            return original_call(path, *args, **kwargs)
        patcher = patch.object(self.fake, 'call', side_effect=denied)
        patcher.start()
        self.addCleanup(patcher.stop)
        result = m.withdraw_confirm(job['id'])
        assert result['state'] == 'withdrawing'
        return job['id']

    def test_recovery_does_not_dispatch_and_requires_new_confirmation(self):
        ident = self.legacy_denied()
        before = sum(x[0].endswith('/withdraw/apply') for x in self.fake.calls)
        recovered = m.recover_rejected_withdrawal(ident)
        self.assertEqual(recovered['state'], 'bought')
        self.assertNotIn('plan_time', recovered['data'])
        self.assertIn('rejected_withdrawal_recovery', recovered['data'])
        self.assertEqual(sum(x[0].endswith('/withdraw/apply') for x in self.fake.calls), before)
        with self.assertRaises(m.BinanceError):
            m.withdraw_confirm(ident)
        new_preview = m.withdraw_preview(ident)
        self.assertEqual(new_preview['state'], 'bought')
        self.assertEqual(new_preview['data']['withdraw_plan']['network'], 'BSC')

    def test_recovery_refuses_original_found_in_history(self):
        ident = self.legacy_denied()
        original = self.fake.call
        def history_found(path, *args, **kwargs):
            if path.endswith('/history'):
                return [{'coin': 'BNB', 'network': 'BSC', 'address': '0x'+'1'*40,
                         'withdrawOrderId': ident, 'status': 4}]
            return original(path, *args, **kwargs)
        with patch.object(self.fake, 'call', side_effect=history_found):
            with self.assertRaises(m.BinanceError):
                m.recover_rejected_withdrawal(ident)
        self.assertEqual(m.get_job(ident)['state'], 'withdrawing')

    def test_recovery_refuses_insufficient_balance(self):
        ident = self.legacy_denied()
        original = self.fake.call
        def emptied(path, *args, **kwargs):
            if path.endswith('/account') and not path.endswith('/apiRestrictions'):
                return {'canWithdraw': True, 'balances': [{'asset':'BNB','free':'0','locked':'0'}]}
            return original(path, *args, **kwargs)
        with patch.object(self.fake, 'call', side_effect=emptied):
            with self.assertRaises(m.BinanceError):
                m.recover_rejected_withdrawal(ident)
        self.assertEqual(m.get_job(ident)['state'], 'withdrawing')

    def test_recovery_refuses_missing_api_permissions(self):
        ident = self.legacy_denied()
        original = self.fake.call
        def forbidden(path, *args, **kwargs):
            if path.endswith('/apiRestrictions'):
                return {'enableWithdrawals': False, 'ipRestrict': True}
            return original(path, *args, **kwargs)
        with patch.object(self.fake, 'call', side_effect=forbidden):
            with self.assertRaises(m.BinanceError):
                m.recover_rejected_withdrawal(ident)

    def test_recovery_refuses_other_unknown_submission_error(self):
        ident = self.legacy_denied()
        with app.db() as c:
            r = c.execute('SELECT data FROM binance_jobs WHERE id=?', (ident,)).fetchone()
            data = json.loads(r['data'])
            data['withdraw_submit_error'] = 'Нет достоверного ответа Binance.'
            c.execute('UPDATE binance_jobs SET data=? WHERE id=?', (json.dumps(data),ident))
        with self.assertRaises(m.BinanceError):
            m.recover_rejected_withdrawal(ident)
        self.assertEqual(m.get_job(ident)['state'], 'withdrawing')

    def test_recovery_refuses_stale_rejected_withdrawal(self):
        ident = self.legacy_denied()
        with app.db() as c:
            r = c.execute('SELECT data FROM binance_jobs WHERE id=?', (ident,)).fetchone()
            data = json.loads(r['data'])
            data['withdraw_time'] = time.time() - 8*86400
            c.execute('UPDATE binance_jobs SET data=? WHERE id=?', (json.dumps(data),ident))
        with self.assertRaises(m.BinanceError):
            m.recover_rejected_withdrawal(ident)
