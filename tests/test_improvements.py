"""No network, real mnemonic, purchase, transfer or wallet deletion."""
import json
import time
import unittest
from unittest.mock import patch
import test_monitor as base
import maintenance as mt
import mexc
from test_mexc import Exchange

app=base.mod

class ReconciliationTests(unittest.TestCase):
    post=base.MonitorTests.post
    wallet=base.MonitorTests.wallet
    def setUp(self):
        base.MonitorTests.setUp(self)
        with app.db() as c:
            c.execute('DELETE FROM withdrawals')
            c.execute('DELETE FROM email_outbox')
            c.execute("UPDATE settings SET value='true' WHERE key='sheets_enabled'")
        self.wid=self.wallet()
        p=patch('sheets_sync.fetch_rows',return_value=[['email','адрес'],['Other',base.B]])
        self.rows=p.start();self.addCleanup(p.stop)
    def test_preview_does_not_delete_confirmation_one_use(self):
        p=self.post('sheets/reconcile-preview',{}).json
        self.assertEqual(len(p['removed']),1)
        self.assertEqual(len(self.client.get('/api/state').json['wallets']),1)
        self.assertEqual(self.post('sheets/reconcile-confirm',{'token':p['token']}).json['removed'],1)
        self.assertGreaterEqual(self.post('sheets/reconcile-confirm',{'token':p['token']}).status_code,400)
    def test_changed_sheet_or_wallet_rejects(self):
        p=mt.preview();self.rows.return_value=[['Name',base.A]]
        with self.assertRaises(app.RemoteError):mt.confirm(p['token'])
        self.rows.return_value=[['Other',base.B]]
        p=mt.preview()
        with app.db() as c:c.execute('UPDATE wallets SET name=?',('Renamed',))
        with self.assertRaises(app.RemoteError):mt.confirm(p['token'])
        self.assertEqual(len(self.client.get('/api/state').json['wallets']),1)
    def test_bad_rows_abort_and_empty_sheet_explicit(self):
        for rows in ([['N','bad']], [['Name']], [None]):
            self.rows.return_value=rows
            with self.assertRaises(app.RemoteError):mt.preview()
        self.rows.return_value=[]
        p=mt.preview();self.assertEqual(len(p['removed']),1)
        mt.confirm(p['token'])
    def test_active_transfer_blocks(self):
        with app.db() as c:
            c.execute("INSERT INTO withdrawals(id,wallet_id,created,state,plan,updated) VALUES ('test',?,0,'pending1','{}',0)",(self.wid,))
        p=mt.preview()
        with self.assertRaises(app.RemoteError):mt.confirm(p['token'])
    def test_notifications_cancelled_history_preserved(self):
        cfg=app.settings();app.record_balance(self.wid,1,1,cfg);app.record_balance(self.wid,2,2,cfg)
        with app.db() as c:
            eid=c.execute('SELECT id FROM events').fetchone()[0]
            c.execute("UPDATE events SET delivery='pending'")
            c.execute("INSERT INTO email_outbox(event_id,recipient,sender,delivery,message_id) VALUES (?, 'to','from','pending','test')",(eid,))
        p=mt.preview();mt.confirm(p['token'])
        with app.db() as c:
            self.assertEqual(c.execute('SELECT delivery FROM events').fetchone()[0],'cancelled')
            self.assertEqual(c.execute('SELECT delivery FROM email_outbox').fetchone()[0],'cancelled')
    def test_audit_never_exposes_exception_secrets(self):
        a=mt.start_audit();self.assertEqual(a['total'],1)
        with patch('withdrawals.accounts_for',side_effect=Exception('FAKE_SECRET_WORDS')):
            mt.audit_once()
        result=self.client.get('/api/seed-audit').json
        self.assertEqual(result['state'],'done')
        self.assertFalse(result['results'][0]['ok'])
        self.assertNotIn('FAKE_SECRET_WORDS',json.dumps(self.client.get('/api/state').json))
    def test_audit_success_and_source_change(self):
        mt.start_audit()
        with patch('withdrawals.accounts_for',return_value=(object(),object())) as call:
            mt.audit_once();call.assert_called_once()
        self.assertTrue(self.client.get('/api/seed-audit').json['results'][0]['ok'])
        mt.start_audit()
        with app.db() as c:c.execute("UPDATE settings SET value='false' WHERE key='sheets_enabled'")
        with patch('withdrawals.accounts_for') as call:
            mt.audit_once();call.assert_not_called()
        self.assertEqual(self.client.get('/api/seed-audit').json['state'],'cancelled')
    def test_auth_csrf(self):
        self.assertEqual(app.app.test_client().get('/api/seed-audit').status_code,401)
        self.assertEqual(self.client.post('/api/sheets/reconcile-confirm',json={}).status_code,403)

class AutoMexcTests(unittest.TestCase):
    def setUp(self):
        with app.db() as c:
            c.execute('DELETE FROM mexc_jobs');c.execute("DELETE FROM status WHERE key LIKE 'mexc_%'")
        self.fake=Exchange();p=patch('mexc.Client',return_value=self.fake);p.start();self.addCleanup(p.stop)
    def buy(self):
        j=mexc.buy_preview({'amount':'50','network':'BSC','address':'0x'+'1'*40})
        mexc.buy_confirm(j['id']);self.fake.calls.clear();return j
    def test_poll_reconciles_and_refreshes_without_posts(self):
        j=self.buy();mexc.poll_once()
        self.assertEqual(mexc.get_job(j['id'])['state'],'bought')
        self.assertTrue(all(c[2]=='GET' for c in self.fake.calls))
        with app.db() as c:self.assertTrue(mt.get(c,'mexc_balances')['balances'])
    def test_timeout_backoff_and_no_resubmit(self):
        j=self.buy()
        with patch.object(self.fake,'call',side_effect=mexc.MexcError('timeout')) as call:
            mexc.poll_once();count=call.call_count;mexc.poll_once()
            self.assertEqual(call.call_count,count)
        self.assertEqual(mexc.get_job(j['id'])['state'],'buying')
        with app.db() as c:self.assertGreater(mt.get(c,'mexc_poll')['next_at'],time.time()+30)
    def test_account_failure_retried_without_trade(self):
        j=self.buy();mexc.reconcile(j['id']);self.fake.calls.clear()
        with patch('mexc.account',side_effect=mexc.MexcError('timeout')):mexc.poll_once()
        with app.db() as c:
            p=mt.get(c,'mexc_poll');self.assertNotIn('balances_for',p)
            p['next_at']=0;mt.put(c,'mexc_poll',p)
        mexc.poll_once()
        self.assertTrue(all(c[2]=='GET' for c in self.fake.calls))
        with app.db() as c:self.assertTrue(mt.get(c,'mexc_balances'))
