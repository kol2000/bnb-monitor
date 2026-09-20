import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import test_monitor as base
import sheets_sync as sheets

class ExportTests(unittest.TestCase):
    setUp = base.MonitorTests.setUp
    post = base.MonitorTests.post
    wallet = base.MonitorTests.wallet

    def test_mapping_reordered_duplicates_zero_and_unknown(self):
        data = sheets.balance_updates(['Имя', 'Адрес', ' остаток BNB (BSC) '],
            [['Адрес'], [base.B], [base.A], [base.A.upper()], ['invalid']],
            {base.A: '0', base.B: '1000000000000000000'}, 'bnb кошельки')
        self.assertEqual([x['range'] for x in data], ["'bnb кошельки'!C2", "'bnb кошельки'!C3", "'bnb кошельки'!C4"])
        self.assertEqual([x['values'] for x in data], [[[1.0]], [[0.0]], [[0.0]]])

    def test_missing_ambiguous_and_source_columns_rejected(self):
        for header in ([], ['остаток BNB (BSC)'], ['a','b','остаток BNB (BSC)','остаток BNB (BSC)']):
            with self.assertRaises(base.mod.RemoteError):
                sheets.balance_updates(header, [], {}, 'bnb кошельки')

    def test_export_and_changed_mapping_abort(self):
        wid = self.wallet()
        with base.mod.db() as c:
            c.execute("UPDATE settings SET value='true' WHERE key='sheets_enabled'")
            c.execute('UPDATE settings SET value=? WHERE key=?', ('"bnb кошельки"','sheets_tab'))
        started = time.time()
        base.mod.record_balance(wid, 10**18, 100, base.mod.settings())
        snap = {'valueRanges': [{'values': [['Имя','Адрес','остаток BNB (BSC)']]}, {'values': [['Адрес'],[base.A]]}]}
        with patch('sheets_sync.google_credentials', return_value=SimpleNamespace(token='fake')), patch('sheets_sync.export_request', side_effect=[snap,snap,{'totalUpdatedCells':1}]) as req:
            sheets.export_balances(started)
        self.assertEqual(req.call_args.args[3]['data'][0]['values'], [[1.0]])
        with patch('sheets_sync.google_credentials', return_value=SimpleNamespace(token='fake')), patch('sheets_sync.export_request', side_effect=[snap,{'valueRanges':[]}]) as req:
            sheets.export_balances(started)
        self.assertEqual(req.call_count, 2)
        with base.mod.db() as c: c.execute('UPDATE wallets SET error=?', ('RPC error',))
        with patch('sheets_sync.google_credentials') as auth:
            sheets.export_balances(started)
            auth.assert_not_called()

    def test_write_failure_does_not_change_balance(self):
        wid=self.wallet()
        with base.mod.db() as c:
            c.execute("UPDATE settings SET value='true' WHERE key='sheets_enabled'")
            c.execute('UPDATE settings SET value=? WHERE key=?', ('"bnb кошельки"','sheets_tab'))
        base.mod.record_balance(wid, 123, 100, base.mod.settings())
        with patch('sheets_sync.google_credentials', side_effect=base.mod.RemoteError('Нет прав')):
            sheets.export_balances(0)
        state=self.client.get('/api/state').json
        self.assertEqual(state['wallets'][0]['balance'], '123')
        self.assertEqual(state['status']['sheets_export_error'], 'Нет прав')
