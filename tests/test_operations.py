"""Offline checks: confirmed transfers, durable notices and redacted diagnostics."""
import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
_tmp=tempfile.TemporaryDirectory()
os.environ.setdefault('DATA_DIR',_tmp.name)
os.environ.setdefault('ADMIN_PASSWORD','test-password-only-123')
os.environ.setdefault('SECRET_KEY','test-secret-'*5)
import app
import operations as op
import withdrawals
import binance

ADDRESS='0x'+'1'*40
TARGET='0x'+'2'*40

class OperationTests(unittest.TestCase):
    def setUp(self):
        from pathlib import Path
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        p=patch.object(app,'DB',Path(tmp.name)/'monitor.db');p.start();self.addCleanup(p.stop)
        app.init_db()
        with app.db() as c:
            for table in ('priority_balances','operation_notices','withdrawals','binance_jobs','wallets'):
                c.execute('DELETE FROM '+table)
            c.execute("DELETE FROM status WHERE key LIKE 'connection_%' OR key IN ('operation_notice_start','outbound_ip')")
            self.wallet=c.execute('INSERT INTO wallets(address,name,balance,block) VALUES (?,?,?,?)',
                                  (ADDRESS,'Example','1',100)).lastrowid
        self.cfg=dict(app.settings(),telegram_enabled=True,email_enabled=True,usdt_enabled=False)
    def row(self,state='pending1',age=1000):
        with app.db() as c:
            c.execute('INSERT INTO withdrawals(id,wallet_id,created,state,plan,updated,hash1) VALUES (?,?,?,?,?,?,?)',
                ('one',self.wallet,time.time()-age,state,json.dumps(dict(source=ADDRESS,middle=TARGET,destination=TARGET,legs=1,value1='10')),time.time(),'0x'+'a'*64))
    def test_state_and_priority_queue_are_atomic(self):
        self.row()
        withdrawals.update('one',refresh=[(ADDRESS,120),(TARGET,120)],state='done')
        with app.db() as c:
            rows=c.execute('SELECT * FROM priority_balances').fetchall()
            self.assertEqual(len(rows),2);self.assertEqual(rows[0]['min_block'],120)
            self.assertEqual(c.execute("SELECT state FROM withdrawals WHERE id='one'").fetchone()[0],'done')
    def test_deduplicate_priority_keep_newest_height(self):
        with app.db() as c:
            op.queue_balance(c,ADDRESS,123);op.queue_balance(c,ADDRESS,110)
            self.assertEqual(c.execute('SELECT min_block FROM priority_balances').fetchone()[0],123)
    def test_priority_updates_balance_without_full_scan(self):
        with app.db() as c:op.queue_balance(c,ADDRESS,120)
        def rpc(url,method,params):
            return {'eth_chainId':'0x38','eth_getBlockByNumber':{'number':'0x78','timestamp':hex(int(time.time()))},'eth_getBalance':'0x0'}[method]
        with patch('app.settings',return_value=self.cfg),patch('app.rpc',side_effect=rpc):op.priority_once()
        with app.db() as c:
            w=c.execute('SELECT * FROM wallets').fetchone();self.assertEqual(w['balance'],'0');self.assertEqual(w['block'],120)
            self.assertEqual(c.execute('SELECT count(*) FROM priority_balances').fetchone()[0],0)
    def test_old_rpc_block_never_clears_queue(self):
        with app.db() as c:op.queue_balance(c,ADDRESS,120)
        def rpc(url,method,params):
            return '0x38' if method=='eth_chainId' else {'number':'0x64','timestamp':hex(int(time.time()))}
        with patch('app.settings',return_value=self.cfg),patch('app.rpc',side_effect=rpc):op.priority_once()
        with app.db() as c:
            self.assertGreater(c.execute('SELECT retry_at FROM priority_balances').fetchone()[0],time.time())
            self.assertEqual(c.execute('SELECT balance FROM wallets').fetchone()[0],'1')
    def test_notices_one_delay_and_one_completion(self):
        op.collect_notices();self.row();op.collect_notices();op.collect_notices()
        with app.db() as c:self.assertEqual(c.execute('SELECT count(*) FROM operation_notices').fetchone()[0],1)
        withdrawals.update('one',state='done');op.collect_notices();op.collect_notices()
        with app.db() as c:
            states=dict(c.execute('SELECT id,state FROM operation_notices').fetchall())
        self.assertEqual(states,{'wallet:one:delay':'cancelled','wallet:one:done':'pending'})
    def test_upgrade_does_not_send_historical_completion(self):
        self.row('done');op.collect_notices();op.collect_notices()
        with app.db() as c:self.assertEqual(c.execute('SELECT count(*) FROM operation_notices').fetchone()[0],0)
    def test_unknown_delivery_does_not_repeat_or_fallback(self):
        with app.db() as c:op.notice(c,'test','Message')
        with patch('app.settings',return_value=self.cfg),patch('app.telegram',side_effect=TimeoutError) as tg,patch('email_notifications.send_email') as email:
            op.deliver_notice();op.deliver_notice();self.assertEqual(tg.call_count,1);email.assert_not_called()
        with app.db() as c:self.assertEqual(c.execute('SELECT state FROM operation_notices').fetchone()[0],'unknown')
    def test_success_not_resent(self):
        with app.db() as c:op.notice(c,'test','Message')
        with patch('app.settings',return_value=self.cfg),patch('app.telegram') as tg:
            op.deliver_notice();op.deliver_notice();self.assertEqual(tg.call_count,1)
    def test_diagnostic_keeps_last_success_and_no_secret(self):
        op.probe('rpc',lambda:'BSC OK');old=op.status_value('connection_rpc')['success']
        def fail():raise HTTPError('https://secret-token',429,'secret',None,None)
        op.probe('rpc',fail);result=op.status_value('connection_rpc')
        self.assertEqual(result['success'],old);self.assertEqual(result['error'],'HTTP 429');self.assertFalse(result['ok'])
    def test_ip_baseline_is_not_automatically_changed(self):
        class Response:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def read(self,n):return b'91.135.222.58'
        with patch('operations.build_opener') as opener:
            opener.return_value.open.return_value=Response();op.check_ip()
            app.set_status(outbound_ip={'current':'91.135.222.58','expected':'77.221.143.151'})
            op.check_ip()
        self.assertTrue(op.status_value('outbound_ip')['changed'])
        self.assertEqual(op.status_value('outbound_ip')['expected'],'77.221.143.151')
    def test_endpoints_require_login_and_csrf(self):
        client=app.app.test_client()
        self.assertEqual(client.get('/api/operations').status_code,401)
        self.assertEqual(client.post('/api/diagnostics',json={}).status_code,403)
    def test_completed_binance_queues_only_once(self):
        data={'address':ADDRESS}
        with app.db() as c:c.execute('INSERT INTO binance_jobs VALUES (?,?,?,?,?)',('x',time.time(),'withdrawing',json.dumps(data),''))
        binance.save('x','done',data)
        with app.db() as c:first=c.execute('SELECT requested FROM priority_balances').fetchone()[0]
        binance.save('x','done',data)
        with app.db() as c:self.assertEqual(c.execute('SELECT requested FROM priority_balances').fetchone()[0],first)
