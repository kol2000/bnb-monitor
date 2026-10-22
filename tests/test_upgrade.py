"""Offline regression tests; fixtures use a public development mnemonic only."""
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import test_monitor as base
import test_withdrawals as fixture
from test_withdrawals import SOURCE, MIDDLE, TARGET, PAIR
import withdrawals as wd
import maintenance as mt
import worker
from backup_crypto import transform
app = base.mod

class AccountTwoTests(unittest.TestCase):
    setUp = fixture.WithdrawTests.setUp
    row = fixture.WithdrawTests.row
    receipt = fixture.WithdrawTests.receipt
    def test_direct_signs_only_account_two_and_finishes(self):
        with app.db() as c:
            c.execute('UPDATE wallets SET address=? WHERE id=?',(MIDDLE,self.wid))
        p=wd.preview(self.wid)
        self.assertEqual(p['legs'],1)
        self.assertEqual(p['source'],MIDDLE)
        wd.confirm(p['id'])
        row=self.row(p['id'])
        self.assertIsNone(row['raw2']);self.assertIsNone(row['hash2'])
        from eth_account import Account
        from eth_account._utils.legacy_transactions import Transaction
        self.assertEqual(Account.recover_transaction(row['raw1']).lower(),MIDDLE)
        tx=Transaction.from_bytes(bytes.fromhex(row['raw1'][2:])).as_dict()
        self.assertEqual('0x'+tx['to'].hex(),TARGET)
        self.assertEqual(tx['value'], self.rpc.balances[MIDDLE]-wd.GAS*self.rpc.gas)
        wd.tick();self.assertEqual(len(self.rpc.sent),1)
        self.receipt(row['hash1']);wd.tick()
        self.assertEqual(self.row(p['id'])['state'],'done')
        wd.tick();self.assertEqual(len(self.rpc.sent),1)

    def test_direct_lost_ack_never_resigns_or_sends_second_leg(self):
        with app.db() as c:c.execute('UPDATE wallets SET address=? WHERE id=?',(MIDDLE,self.wid))
        p=wd.preview(self.wid);wd.confirm(p['id'])
        self.rpc.fail_ack=True
        wd.tick();wd.tick()
        self.assertEqual(len(set(self.rpc.sent)),1)
        self.assertIsNone(self.row(p['id'])['raw2'])

class DashboardTests(unittest.TestCase):
    setUp = base.MonitorTests.setUp
    wallet = base.MonitorTests.wallet
    post = base.MonitorTests.post
    def test_health_and_detail_do_not_expose_keys(self):
        wid=self.wallet()
        cfg=app.settings();app.record_balance(wid,100,1,cfg)
        with app.db() as c:c.execute('UPDATE wallets SET checked=?', (time.time()-10000,))
        s=self.client.get('/api/state').json
        self.assertEqual(s['health']['stale'],1)
        d=self.client.get(f'/api/wallets/{wid}').json
        self.assertEqual(d['wallet']['address'],base.A)
        self.assertNotIn('raw1',json.dumps(d));self.assertNotIn('telegram_token',json.dumps(d))
        self.assertEqual(app.app.test_client().get(f'/api/wallets/{wid}').status_code,401)

    def test_destination_requires_matching_confirmation(self):
        target='0x'+'4'*40
        self.assertEqual(self.post('settings',{'withdraw_binance_destination':target}).status_code,400)
        self.assertEqual(self.post('settings',{'withdraw_binance_destination':target,'confirm_destination':target}).status_code,200)

    def test_reconcile_adds_renames_removes_atomically(self):
        wid=self.wallet()
        with app.db() as c:c.execute("UPDATE settings SET value='true' WHERE key='sheets_enabled'")
        with patch('sheets_sync.fetch_rows',return_value=[['Renamed',base.A],['Added',base.B]]):
            p=mt.preview();self.assertEqual(len(p['added']),1);self.assertEqual(len(p['renamed']),1)
            result=mt.confirm(p['token'])
            self.assertEqual(result,{'added':1,'renamed':1,'removed':0})
        with app.db() as c:self.assertEqual(c.execute('SELECT name FROM wallets WHERE id=?',(wid,)).fetchone()[0],'Renamed')

    def test_digest_failure_preserves_queue_then_ack_marks_all(self):
        wid=self.wallet()
        cfg=app.settings()
        cfg.update(telegram_enabled=True,telegram_digest=True,telegram_chat='123',threshold='0')
        with app.db() as c:
            for k,v in cfg.items():c.execute('UPDATE settings SET value=? WHERE key=?',(json.dumps(v),k))
        for i in range(5):app.record_balance(wid,i+1,i+1,cfg)
        with app.db() as c:c.execute('UPDATE events SET created=?',(time.time()-7200,))
        with patch('worker.telegram',side_effect=app.RemoteError('timeout')) as send:
            worker.deliver();self.assertEqual(send.call_count,1)
        with app.db() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM events WHERE delivery='pending'").fetchone()[0],4)
            c.execute('UPDATE events SET retry_at=0')
        with patch('worker.telegram') as send:
            worker.deliver();self.assertEqual(send.call_count,1)
        with app.db() as c:self.assertEqual(c.execute("SELECT count(*) FROM events WHERE delivery='sent'").fetchone()[0],4)

class EncryptionTests(unittest.TestCase):
    def test_round_trip_and_tamper_never_publish_plaintext(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);key=p/'key';key.write_bytes(b'x'*32);key.chmod(0o600)
            src=p/'source';src.write_bytes(b'sensitive-data'*100000)
            enc=p/'encrypted';out=p/'plain'
            transform(src,enc,key)
            self.assertNotIn(b'sensitive-data',enc.read_bytes())
            transform(enc,out,key,True);self.assertEqual(src.read_bytes(),out.read_bytes())
            out.unlink();data=bytearray(enc.read_bytes());data[100]^=1;enc.write_bytes(data)
            with self.assertRaises(ValueError):transform(enc,out,key,True)
            self.assertFalse(out.exists());self.assertFalse(list(p.glob('.backup-*')))

class RelatedSeedTests(unittest.TestCase):
    def test_blank_account_two_uses_verified_current_source_only(self):
        cfg={'sheets_enabled':True,'sheets_id':'test','sheets_tab':'bnb кошельки'}
        audit={'source':cfg,'results':[{'address':SOURCE,'related':MIDDLE,'account_number':1,'ok':True}]}
        app.set_status(seed_audit=audit)
        rows=[{'values':[[SOURCE],[MIDDLE]]},{'values':[[MIDDLE]]},
              {'values':[[SOURCE],[MIDDLE]]},{'values':[[SOURCE,fixture.MNEMONIC]]}]
        with patch('sheets_sync.google_credentials'),patch('sheets_sync.export_request',side_effect=rows) as req:
            pair=wd.accounts_for(cfg,MIDDLE)
            self.assertEqual(pair[1].address.lower(),MIDDLE)
            self.assertEqual(req.call_count,4)
        audit['source']={**cfg,'sheets_id':'changed'};app.set_status(seed_audit=audit)
        with patch('sheets_sync.google_credentials'),patch('sheets_sync.export_request',side_effect=rows[:2]):
            with self.assertRaises(ValueError):wd.accounts_for(cfg,MIDDLE)

class BinanceAutomaticTests(unittest.TestCase):
    def test_auto_poll_is_get_only_and_failure_does_not_redispatch(self):
        import binance
        from test_binance import Exchange
        fake=Exchange()
        with app.db() as c:
            c.execute('DELETE FROM binance_jobs')
            c.execute("DELETE FROM status WHERE key LIKE 'binance_%'")
        with patch('binance.Client',return_value=fake):
            j=binance.buy_preview({'amount':'50','network':'BSC','address':'0x'+'1'*40})
            binance.buy_confirm(j['id']);fake.calls.clear()
            binance.poll_once()
            self.assertEqual(binance.get_job(j['id'])['state'],'bought')
            self.assertTrue(all(c[2]=='GET' for c in fake.calls))

class RpcBackoffTests(unittest.TestCase):
    setUp=base.MonitorTests.setUp
    wallet=base.MonitorTests.wallet
    post=base.MonitorTests.post
    def test_failed_endpoint_stops_after_three_and_uses_fallback(self):
        with app.db() as c:
            for i in range(6):c.execute('INSERT INTO wallets(address,name) VALUES (?,?)',('0x'+f'{i+1:040x}','Test'))
            c.execute("UPDATE settings SET value=? WHERE key='rpc_urls'",(json.dumps(['https://first','https://second']),))
        calls=[]
        def rpc(url,method,params):
            calls.append((url,method))
            if method=='eth_chainId':return '0x38'
            if url=='https://first':raise app.RemoteError('temporary')
            if method=='eth_getBlockByNumber':return {'number':'0x10','timestamp':hex(int(time.time()))}
            return '0x1'
        with patch('worker.rpc',side_effect=rpc),patch.object(worker.stop,'wait'):
            worker.check_once()
        self.assertEqual(sum(u=='https://first' and m=='eth_getBlockByNumber' for u,m in calls),3)
        with app.db() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM wallets WHERE balance IS NOT NULL').fetchone()[0],6)
