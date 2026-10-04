import json
import sqlite3
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import unittest
import test_monitor
import app as mod
import worker
import sheets_sync as sheets


def word(n):
    return '0x' + format(n, '064x')

class USDTTests(unittest.TestCase):
    def setUp(self):
        self.base = test_monitor.MonitorTests()
        self.base.setUp()
        self.post = self.base.post
        self.client = self.base.client
        self.post('settings', {'usdt_enabled': True, 'usdt_threshold': '0.01'})
        self.wid = self.base.wallet()
        with mod.db() as c: c.execute('DELETE FROM email_outbox')

    def wallet(self):
        with mod.db() as c: return dict(c.execute('SELECT * FROM wallets WHERE id=?', (self.wid,)).fetchone())

    def rpc(self, url, method, params):
        if method == 'eth_chainId': return '0x38'
        if method == 'eth_getBlockByNumber': return {'number': '0x100', 'timestamp': hex(int(time.time()))}
        if method == 'eth_getBalance': return '0x7'
        if method == 'eth_call':
            self.assertEqual(params[0]['to'], mod.USDT_CONTRACT)
            if params[0]['data'] == '0x313ce567': return word(18)
            self.assertEqual(params[0]['data'], '0x70a08231' + test_monitor.A[2:].zfill(64))
            self.assertEqual(params[1], '0x100')
            return word(123456789012345678901)
        raise AssertionError(method)

    def test_worker_and_manual_exact_balances(self):
        with patch('worker.rpc', side_effect=self.rpc), patch.object(worker.stop, 'wait'):
            worker.check_once()
        w = self.wallet()
        self.assertEqual(w['balance'], '7')
        self.assertEqual(w['usdt_balance'], '123456789012345678901')
        self.assertFalse(self.base.events())
        with patch('app.rpc', side_effect=self.rpc):
            self.assertEqual(self.post(f'wallets/{self.wid}/check', {}).status_code, 200)
        state = self.client.get('/api/state').json
        self.assertEqual(state['total_usdt'], '123.456789012345678901')
        self.assertEqual(state['wallets'][0]['usdt'], state['total_usdt'])

    def test_token_failure_preserves_bnb_and_old_usdt(self):
        mod.record_balance(self.wid, 99, 1, mod.settings(), 'USDT')
        def failing(url, method, params):
            if method == 'eth_call': return '0x'
            return self.rpc(url, method, params)
        with patch('worker.rpc', side_effect=failing), patch.object(worker.stop, 'wait'):
            worker.check_once()
        w = self.wallet()
        self.assertEqual(w['balance'], '7')
        self.assertIsNone(w['error'])
        self.assertEqual(w['usdt_balance'], '99')
        self.assertEqual(w['usdt_block'], 1)
        self.assertTrue(w['usdt_error'])

    def test_events_same_block_threshold_and_fallback(self):
        self.post('settings', {'telegram_enabled': True, 'telegram_token': '123:'+'x'*24, 'telegram_chat':'123',
              'email_enabled': True, 'smtp_host':'smtp.mail.ru', 'smtp_port':465, 'smtp_security':'ssl',
              'smtp_username':'a@example.org', 'smtp_password':'test', 'email_from':'a@example.org', 'email_to':'b@example.org'})
        cfg = mod.settings()
        for asset in ['BNB', 'USDT']:
            mod.record_balance(self.wid, 0, 10, cfg, asset)
            mod.record_balance(self.wid, 10**18, 11, cfg, asset)
            mod.record_balance(self.wid, 10**18, 11, cfg, asset)
        events = self.base.events()
        self.assertEqual([e['asset'] for e in events], ['BNB', 'USDT'])
        self.assertIn('+1 USDT', worker.notification_text(events[1]))
        self.assertNotIn('BNB', worker.notification_text(events[1]))
        mod.record_balance(self.wid, 10**18+1, 12, cfg, 'USDT')
        self.assertEqual(self.base.events()[-1]['delivery'], 'off')
        mod.record_balance(self.wid, 0, 13, cfg, 'USDT')
        self.assertEqual(self.base.events()[-1]['delivery'], 'off')
        with mod.db() as c: c.execute("UPDATE events SET attempts=3 WHERE asset='USDT' AND delivery='pending'")
        with patch('worker.send_email') as send, patch.object(worker.stop, 'wait'):
            worker.deliver_email()
        send.assert_called_once()
        self.assertIn('USDT', send.call_args.args[1])
        self.assertEqual(self.base.events()[1]['delivery'], 'cancelled')

    def test_decimals_validation_and_old_block(self):
        for raw in ['0x', word(6), None]:
            with self.assertRaises(mod.RemoteError): mod.usdt_decimals(lambda *args: raw, 'https://example.org')
        cfg = mod.settings()
        mod.record_balance(self.wid, 2, 20, cfg, 'USDT')
        mod.record_balance(self.wid, 1, 19, cfg, 'USDT')
        self.assertEqual(self.wallet()['usdt_balance'], '2')
        self.assertEqual(self.post('settings', {'usdt_threshold':'NaN'}).status_code, 400)
        self.assertEqual(self.post('settings', {'usdt_enabled':'yes'}).status_code, 400)

    def test_export_both_assets_and_missing_token_header(self):
        cfg = {'sheets_enabled': True, 'sheets_id':'a'*30, 'sheets_tab':'bnb кошельки'}
        with mod.db() as c:
            for k,v in cfg.items(): c.execute('UPDATE settings SET value=? WHERE key=?', (json.dumps(v), k))
        for asset in ['BNB','USDT']: mod.record_balance(self.wid, 10**18, 10, mod.settings(), asset)
        snap = {'valueRanges':[{'values':[['email','адрес','private header','остаток BNB (BSC)','остаток USDT (BSC)']]}, {'values':[['адрес'],[test_monitor.A]]}]}
        with patch('sheets_sync.google_credentials', return_value=SimpleNamespace(token='test')), patch('sheets_sync.export_request', side_effect=[snap,snap,{'totalUpdatedCells':2}]) as request:
            sheets.export_balances(0)
        self.assertEqual([r['range'] for r in request.call_args.args[3]['data']], ["'bnb кошельки'!D2", "'bnb кошельки'!E2"])
        snap['valueRanges'][0]['values'][0].pop()
        with patch('sheets_sync.google_credentials', return_value=SimpleNamespace(token='test')), patch('sheets_sync.export_request', side_effect=[snap,snap,{'totalUpdatedCells':1}]) as request:
            sheets.export_balances(0)
        self.assertEqual(len(request.call_args.args[3]['data']), 1)
        self.assertIn('USDT', self.client.get('/api/state').json['status']['sheets_export_error'])

    def test_legacy_migration_preserves_event_ids_and_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            dbpath = Path(tmp)/'monitor.db'
            with sqlite3.connect(dbpath) as c:
                c.executescript('''CREATE TABLE events (
                    id INTEGER PRIMARY KEY, wallet_id INTEGER NOT NULL, address TEXT NOT NULL,
                    name TEXT NOT NULL, old TEXT NOT NULL, new TEXT NOT NULL, delta TEXT NOT NULL,
                    block INTEGER NOT NULL, created REAL NOT NULL, delivery TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0,
                    delivery_error TEXT, chat TEXT NOT NULL DEFAULT '', UNIQUE(wallet_id, block));
                    INSERT INTO events VALUES(42,1,'address','name','0','1','1',100,1,'pending',9,1000,'offline','123');''')
            with patch.object(mod, 'DB', dbpath):
                mod.init_db()
                with mod.db() as c:
                    c.execute("INSERT INTO email_outbox(event_id,recipient,sender,message_id) VALUES(42,'a','b','saved-id')")
                mod.init_db()
                with mod.db() as c:
                    e = dict(c.execute('SELECT * FROM events').fetchone())
                    self.assertEqual((e['id'], e['asset'], e['attempts'], e['delivery']), (42,'BNB',9,'pending'))
                    self.assertEqual(c.execute('SELECT message_id FROM email_outbox').fetchone()[0], 'saved-id')
                    c.execute("INSERT INTO events(wallet_id,address,name,old,new,delta,block,created,delivery,asset) VALUES(1,'address','name','0','2','2',100,2,'off','USDT')")
                    self.assertEqual(c.execute('SELECT COUNT(*) FROM events').fetchone()[0], 2)
