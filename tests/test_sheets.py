import json
import unittest
from unittest.mock import patch
import test_monitor as base
import sheets_sync

mod = base.mod
A = base.A
B = base.B

class SheetsTests(unittest.TestCase):
    setUp = base.MonitorTests.setUp
    post = base.MonitorTests.post
    wallet = base.MonitorTests.wallet

    def test_import_and_rename_preserve_balance_and_notify(self):
        wid = self.wallet()
        mod.record_balance(wid, 123, 100, mod.settings())
        self.post('wallets/' + str(wid), {'notify': False})
        counts = sheets_sync.apply_rows([['New name', A], ['Second', B]])
        self.assertEqual(counts['added'], 1)
        self.assertEqual(counts['renamed'], 1)
        wallets = self.client.get('/api/state').json['wallets']
        old = next(w for w in wallets if w['id'] == wid)
        self.assertEqual(old['balance'], '123')
        self.assertEqual(old['notify'], 0)
        self.assertEqual(old['name'], 'New name')

    def test_duplicate_case_and_invalid_header(self):
        counts = sheets_sync.apply_rows([['Name', 'Address'], ['First', A], ['First', A[:2]+A[2:].upper()], [], ['Blank', '']])
        self.assertEqual(counts, dict(added=1, renamed=0, invalid=1, duplicates=1, valid=1))

    def test_conflicting_names_abort(self):
        with self.assertRaises(mod.RemoteError):
            sheets_sync.apply_rows([['First', A], ['Second', A]])
        self.assertEqual(self.client.get('/api/state').json['wallets'], [])

    def test_empty_sheet_and_removed_rows_preserve_wallets(self):
        self.wallet()
        with self.assertRaises(mod.RemoteError): sheets_sync.apply_rows([])
        sheets_sync.apply_rows([['Second', B]])
        self.assertEqual(len(self.client.get('/api/state').json['wallets']), 2)

    def test_google_settings_and_no_key_exposure(self):
        key = mod.DATA / 'google-service-account.json'
        key.write_text('{"test":"fake-key-only"}')
        try:
            r = self.post('settings', {'sheets_enabled': True, 'sheets_id': 'https://docs.google.com/spreadsheets/d/'+'x'*25+'/edit', 'sheets_tab': 'Кошельки'})
            self.assertEqual(r.status_code, 200)
            state = self.client.get('/api/state').json
            self.assertEqual(state['settings']['sheets_id'], 'x'*25)
            self.assertNotIn('fake-key-only', json.dumps(state))
            self.assertEqual(self.post('sheets/sync', {}).status_code, 200)
        finally:
            key.unlink()

    def test_failed_fetch_preserves_data(self):
        self.wallet()
        with mod.db() as c: c.execute("UPDATE settings SET value='true' WHERE key='sheets_enabled'")
        with patch('sheets_sync.fetch_rows', side_effect=mod.RemoteError('Google unavailable')):
            sheets_sync.sync_once()
        state = self.client.get('/api/state').json
        self.assertEqual(len(state['wallets']), 1)
        self.assertEqual(state['status']['sheets_error'], 'Google unavailable')

    def test_sync_is_idempotent(self):
        with mod.db() as c: c.execute("UPDATE settings SET value='true' WHERE key='sheets_enabled'")
        with patch('sheets_sync.fetch_rows', return_value=[['First', A]]):
            sheets_sync.sync_once()
            sheets_sync.sync_once()
        state = self.client.get('/api/state').json
        self.assertEqual(len(state['wallets']), 1)
        self.assertEqual(state['status']['sheets_counts']['added'], 0)
        self.assertTrue(state['status']['check_requested'])

    def test_limit_aborts_without_partial_import(self):
        rows = [['Wallet', '0x'+format(i,'040x')] for i in range(501)]
        with self.assertRaises(mod.RemoteError): sheets_sync.apply_rows(rows)
        self.assertEqual(self.client.get('/api/state').json['wallets'], [])

    def test_config_changed_during_fetch_discards_rows(self):
        with mod.db() as c: c.execute("UPDATE settings SET value='true' WHERE key='sheets_enabled'")
        def changed(cfg):
            with mod.db() as c: c.execute("UPDATE settings SET value='false' WHERE key='sheets_enabled'")
            return [['First', A]]
        with patch('sheets_sync.fetch_rows', side_effect=changed): sheets_sync.sync_once()
        self.assertEqual(self.client.get('/api/state').json['wallets'], [])

    def enable_sync(self):
        with mod.db() as c:
            c.execute("UPDATE settings SET value='true' WHERE key='sheets_enabled'")
        return mod.settings()

    def test_daily_removal_preserves_history_and_cancels_pending(self):
        wid = self.wallet()
        cfg = self.enable_sync()
        cfg.update(telegram_enabled=True, telegram_chat='123', threshold='0')
        mod.record_balance(wid, 0, 100, cfg)
        mod.record_balance(wid, 10, 101, cfg)
        sheets_sync.apply_rows([['Second', B]], source=mod.settings())
        state = self.client.get('/api/state').json
        self.assertEqual([w['address'] for w in state['wallets']], [B])
        self.assertEqual(state['events'][0]['delivery'], 'cancelled')
        self.assertEqual(state['status']['sheets_removed'], 1)

    def test_daily_interval_survives_reinitialization_and_manual_sync(self):
        cfg = self.enable_sync()
        with patch('sheets_sync.time.time', return_value=100000):
            sheets_sync.apply_rows([['First', A], ['Second', B]], source=cfg)
        mod.init_db()
        with patch('sheets_sync.time.time', return_value=186399), patch('sheets_sync.fetch_rows', return_value=[['Second', B]]):
            sheets_sync.sync_once()
        self.assertEqual(len(self.client.get('/api/state').json['wallets']), 2)
        with patch('sheets_sync.time.time', return_value=186400):
            sheets_sync.apply_rows([['Second', B]], source=cfg)
        self.assertEqual([w['address'] for w in self.client.get('/api/state').json['wallets']], [B])

    def test_empty_and_header_only_sheet_remove_last_wallet(self):
        cfg = self.enable_sync()
        for rows in ([], [['Имя', 'Адрес']]):
            self.wallet()
            with mod.db() as c:
                c.execute("DELETE FROM status WHERE key='sheets_reconciled_at'")
            sheets_sync.apply_rows(rows, source=cfg)
            self.assertEqual(self.client.get('/api/state').json['wallets'], [])

    def test_invalid_or_truncated_rows_never_remove_wallets(self):
        self.wallet()
        cfg = self.enable_sync()
        for rows in ([['Name', 'Address'], ['Typo', '0xBAD']], [None], {}, [[]]*5000):
            with self.assertRaises(mod.RemoteError):
                sheets_sync.apply_rows(rows, source=cfg)
            state = self.client.get('/api/state').json
            self.assertEqual(len(state['wallets']), 1)
            self.assertNotIn('sheets_reconciled_at', state['status'])

    def test_disabled_sync_does_not_remove(self):
        self.wallet()
        with patch('sheets_sync.fetch_rows') as fetch:
            sheets_sync.sync_once()
            fetch.assert_not_called()
        self.assertEqual(len(self.client.get('/api/state').json['wallets']), 1)
