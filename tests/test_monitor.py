import os
import tempfile
import time
import unittest
from unittest.mock import patch

temp = tempfile.TemporaryDirectory()
os.environ.update(DATA_DIR=temp.name, ADMIN_PASSWORD='test-password-only-123', SECRET_KEY='test-secret-' * 5)
import app as mod
import worker

A = '0x' + 'a'*40
B = '0x' + 'b'*40

class MonitorTests(unittest.TestCase):
    def setUp(self):
        with mod.db() as c:
            for table in ('wallets', 'events', 'status', 'settings'):
                c.execute('DELETE FROM ' + table)
        mod.init_db()
        self.client = mod.app.test_client()
        self.csrf = self.client.get('/api/session').json['csrf']
        r = self.post('login', {'password': 'test-password-only-123'})
        self.csrf = r.json['csrf']
        worker.stop.clear()

    def post(self, path, data):
        return self.client.post('/api/' + path, json=data, headers={'X-CSRF-Token': self.csrf})

    def wallet(self):
        self.assertEqual(self.post('wallets', {'wallets': [{'address': A, 'name': 'Основной'}]}).status_code, 200)
        with mod.db() as c:
            return c.execute('SELECT id FROM wallets').fetchone()[0]

    def events(self):
        with mod.db() as c:
            return [dict(r) for r in c.execute('SELECT * FROM events ORDER BY id')]

    def test_auth_and_csrf(self):
        other=mod.app.test_client()
        self.assertEqual(other.get('/api/state').status_code,401)
        self.assertEqual(self.client.post('/api/check',json={}).status_code,403)
        self.assertEqual(self.client.get('/').status_code,200)
        self.assertIn("frame-ancestors 'none'", self.client.get('/').headers['Content-Security-Policy'])

    def test_initial_increase_decrease_and_dedup(self):
        wid=self.wallet(); cfg=mod.settings()
        mod.record_balance(wid,10**18,100,cfg)
        self.assertEqual(len(self.events()),0)
        mod.record_balance(wid,10**18+1,101,cfg)
        mod.record_balance(wid,10**18+2,101,cfg)
        mod.record_balance(wid,0,99,cfg)
        mod.record_balance(wid,0,102,cfg)
        e=self.events()
        self.assertEqual(len(e),2)
        self.assertEqual(e[0]['delta'],'1')
        self.assertEqual(e[1]['delta'],str(-(10**18+1)))

    def test_exact_arithmetic(self):
        self.assertEqual(mod.bnb(10**18+1),'1.000000000000000001')
        self.assertEqual(mod.bnb(-1),'-0.000000000000000001')
        self.assertEqual(mod.threshold_wei('0.000000000000000001'),1)
        for invalid in ('NaN','Infinity','-1','0.0000000000000000001'):
            with self.assertRaises(ValueError):mod.threshold_wei(invalid)

    def test_address_validation_duplicate_and_atomic_import(self):
        self.wallet()
        self.assertEqual(self.post('wallets',{'wallets':[{'address':A.upper().replace('0X','0x')}]}).json['added'],0)
        self.assertEqual(self.post('wallets',{'wallets':[{'address':B},{'address':'bad'}]}).status_code,400)
        self.assertEqual(len(self.client.get('/api/state').json['wallets']),1)

    def test_restart_persistence(self):
        wid=self.wallet();mod.record_balance(wid,123,100,mod.settings())
        mod.init_db()
        mod.record_balance(wid,125,101,mod.settings())
        self.assertEqual(self.events()[0]['delta'],'2')

    def configure_telegram(self):
        r=self.post('settings',{'telegram_token':'123456:'+'a'*30,'telegram_chat':'12345','telegram_enabled':True,'threshold':'0'})
        self.assertEqual(r.status_code,200)
        return mod.settings()

    def test_queue_retry_and_delivery(self):
        cfg=self.configure_telegram();wid=self.wallet()
        mod.record_balance(wid,0,100,cfg);mod.record_balance(wid,99,101,cfg)
        with patch('worker.telegram',side_effect=mod.RemoteError('Ошибка')), patch.object(worker.stop,'wait'):
            worker.deliver()
        self.assertEqual(self.events()[0]['delivery'],'pending')
        self.assertEqual(self.events()[0]['attempts'],1)
        with mod.db() as c:c.execute('UPDATE events SET retry_at=0')
        with patch('worker.telegram') as send, patch.object(worker.stop,'wait'):
            worker.deliver();worker.deliver()
            self.assertEqual(send.call_count,1)
        self.assertEqual(self.events()[0]['delivery'],'sent')

    def test_delivery_includes_usd_change_and_preserves_bnb(self):
        cases = [
            ('fresh', '1000', 0, 276320000000000, '(≈ $0.28)'),
            ('dust', '1000', 0, 1, '(≈ < $0.01)'),
            ('missing', None, None, 276320000000000, '(USD: курс недоступен)'),
            ('stale', '1000', 901, 276320000000000, '(≈ $0.28, курс устарел)'),
            ('invalid', 'NaN', 0, 276320000000000, '(USD: курс недоступен)'),
            ('no timestamp', '1000', None, 276320000000000, '(USD: курс недоступен)'),
        ]
        for label, price, age, delta, expected in cases:
            with self.subTest(label=label):
                self.setUp()
                cfg = self.configure_telegram()
                wid = self.wallet()
                mod.record_balance(wid, 10**18, 100, cfg)
                mod.record_balance(wid, 10**18 + delta, 101, cfg)
                now = time.time()
                mod.set_status(bnb_usd=price, price_at=now - age if age is not None else None)
                with patch('worker.time.time', return_value=now), patch('worker.telegram') as send, patch.object(worker.stop, 'wait'):
                    worker.deliver()
                send.assert_called_once()
                text = send.call_args.args[1]
                self.assertIn(f"Изменение: +{mod.bnb(delta)} BNB {expected}\n", text)
                self.assertIn(f"Баланс: {mod.bnb(10**18 + delta)} BNB\n", text)
                self.assertEqual(self.events()[0]['delivery'], 'sent')

    def test_disable_cancels_queue_and_token_is_hidden(self):
        cfg=self.configure_telegram();wid=self.wallet()
        mod.record_balance(wid,0,100,cfg);mod.record_balance(wid,1,101,cfg)
        data=self.client.get('/api/state').json
        self.assertNotIn('telegram_token',data['settings'])
        self.assertTrue(data['settings']['telegram_token_set'])
        self.post('settings',{'telegram_enabled':False})
        self.assertEqual(self.events()[0]['delivery'],'cancelled')

    def test_worker_wrong_chain_preserves_balance(self):
        wid=self.wallet();mod.record_balance(wid,777,100,mod.settings())
        with patch('worker.rpc',return_value='0x1'):worker.check_once()
        w=self.client.get('/api/state').json['wallets'][0]
        self.assertEqual(w['balance'],'777')
        self.assertIn('не к BSC',w['error'])
        self.assertEqual(self.events(),[])

    def test_worker_success(self):
        self.wallet()
        def rpc(url,method,params):
            if method=='eth_chainId':return '0x38'
            if method=='eth_getBlockByNumber':return {'number':'0x100','timestamp':hex(int(time.time()))}
            self.assertEqual(params[1],'0x100')
            return hex(10**18+1)
        with patch('worker.rpc',side_effect=rpc), patch.object(worker.stop,'wait'):worker.check_once()
        self.assertEqual(self.client.get('/api/state').json['wallets'][0]['bnb'],'1.000000000000000001')

    def test_shutdown_does_not_mark_unvisited_wallets_as_rpc_failures(self):
        self.wallet()
        self.post('wallets', {'wallets': [{'address': B, 'name': 'Second'}]})
        mod.set_status(last_cycle=123, error=None)
        def rpc(url, method, params):
            if method == 'eth_chainId': return '0x38'
            if method == 'eth_getBlockByNumber':
                return {'number': '0x100', 'timestamp': hex(int(time.time()))}
            return '0x7'
        with patch('worker.rpc', side_effect=rpc), patch.object(worker.stop, 'wait', side_effect=lambda _: worker.stop.set()):
            worker.check_once()
        state = self.client.get('/api/state').json
        self.assertTrue(all(w['error'] is None for w in state['wallets']))
        self.assertEqual(sum(w['balance'] is not None for w in state['wallets']), 1)
        self.assertEqual(state['status']['last_cycle'], 123)
        self.assertIsNone(state['status']['error'])
        self.assertFalse(state['status']['running'])

    def test_sweep_survives_provider_discarding_old_state(self):
        self.wallet()
        self.post('wallets', {'wallets': [{'address': B, 'name': 'Second'}]})
        current_height = 256
        def rpc(url, method, params):
            if method == 'eth_chainId':
                return '0x38'
            if method == 'eth_getBlockByNumber':
                return {'number': hex(current_height), 'timestamp': hex(int(time.time()))}
            if int(params[1], 16) < current_height:
                raise mod.RemoteError('not supported')
            return '0x7'
        def advance(_seconds):
            nonlocal current_height
            current_height += 100
        with patch('worker.rpc', side_effect=rpc), patch.object(worker.stop, 'wait', side_effect=advance):
            worker.check_once()
        with mod.db() as c:
            rows = c.execute('SELECT balance,block,error FROM wallets ORDER BY id').fetchall()
        self.assertEqual([(r['balance'], r['block'], r['error']) for r in rows],
                         [('7', 256, None), ('7', 356, None)])
        self.assertEqual(self.events(), [])

    def test_fallback_and_stale_provider(self):
        self.wallet();self.post('settings',{'rpc_urls':['https://one.example','https://two.example']})
        def rpc(url,method,params):
            if url=='https://one.example':raise mod.RemoteError('Недоступен')
            if method=='eth_chainId':return '0x38'
            if method=='eth_getBlockByNumber':return {'number':'0x100','timestamp':hex(int(time.time()))}
            return '0x1'
        with patch('worker.rpc',side_effect=rpc), patch.object(worker.stop,'wait'):worker.check_once()
        self.assertEqual(self.client.get('/api/state').json['wallets'][0]['balance'],'1')

    def test_manual_check_reads_same_block_and_clears_error(self):
        wid = self.wallet()
        mod.record_balance(wid, 7, 256, mod.settings())
        with mod.db() as c:
            c.execute("UPDATE wallets SET checked=1,error='old failure'")
        with patch('app.rpc', side_effect=['0x38', {'number': '0x100', 'timestamp': hex(int(time.time()))}, '0x7']) as call:
            self.assertEqual(self.post(f'wallets/{wid}/check', {}).status_code, 200)
        self.assertEqual(call.call_args.args[1], 'eth_getBalance')
        w = self.client.get('/api/state').json['wallets'][0]
        self.assertGreater(w['checked'], 1)
        self.assertIsNone(w['error'])
        self.assertEqual(self.events(), [])

    def test_manual_check_fallback_preserves_notification_rules(self):
        cfg = self.configure_telegram()
        wid = self.wallet()
        mod.record_balance(wid, 1, 100, cfg)
        self.post('settings', {'rpc_urls': ['https://one.example', 'https://two.example']})
        with patch('app.rpc', side_effect=[mod.RemoteError('Unavailable'), '0x38',
                   {'number': '0x101', 'timestamp': hex(int(time.time()))}, '0x9']):
            self.assertEqual(self.post(f'wallets/{wid}/check', {}).status_code, 200)
        self.assertEqual(self.events()[0]['delta'], '8')
        self.assertEqual(self.events()[0]['delivery'], 'pending')
        # An older in-flight worker result cannot undo the manual refresh.
        mod.record_balance(wid, 3, 101, cfg)
        self.assertEqual(self.client.get('/api/state').json['wallets'][0]['balance'], '9')
        self.assertEqual(len(self.events()), 1)

    def test_manual_check_failure_and_lock_cleanup(self):
        wid = self.wallet()
        mod.record_balance(wid, 777, 100, mod.settings())
        with patch('app.rpc', return_value='0x1'):
            self.assertEqual(self.post(f'wallets/{wid}/check', {}).status_code, 502)
        self.assertFalse(mod.manual_check_lock.locked())
        self.assertEqual(self.client.get('/api/state').json['wallets'][0]['balance'], '777')
        self.assertEqual(self.events(), [])

    def test_manual_check_auth_missing_wallet_and_busy(self):
        wid = self.wallet()
        path = f'/api/wallets/{wid}/check'
        self.assertEqual(self.client.post(path, json={}).status_code, 403)
        other = mod.app.test_client()
        token = other.get('/api/session').json['csrf']
        self.assertEqual(other.post(path, json={}, headers={'X-CSRF-Token': token}).status_code, 401)
        with patch('app.rpc') as call:
            self.assertEqual(self.post(f'wallets/{wid+1}/check', {}).status_code, 404)
            with mod.manual_check_lock:
                self.assertEqual(self.post(f'wallets/{wid}/check', {}).status_code, 409)
            call.assert_not_called()

    def test_manual_check_does_not_overwrite_newer_concurrent_result(self):
        wid = self.wallet()
        cfg = mod.settings()
        mod.record_balance(wid, 1, 100, cfg)
        def rpc(url, method, params):
            if method == 'eth_chainId': return '0x38'
            if method == 'eth_getBlockByNumber':
                return {'number': '0x101', 'timestamp': hex(int(time.time()))}
            mod.record_balance(wid, 9, 258, cfg)
            return '0x7'
        with patch('app.rpc', side_effect=rpc):
            self.assertEqual(self.post(f'wallets/{wid}/check', {}).status_code, 200)
        self.assertEqual(self.client.get('/api/state').json['wallets'][0]['balance'], '9')
        self.assertEqual(len(self.events()), 1)

    def test_settings_validation(self):
        for data in ({'interval':1},{'rpc_urls':['http://example.com']},{'threshold':'NaN'},{'telegram_enabled':True}):
            self.assertEqual(self.post('settings',data).status_code,400)

    def test_threshold_and_first_balance_do_not_notify(self):
        self.configure_telegram(); self.post('settings',{'threshold':'0.1'})
        cfg=mod.settings();wid=self.wallet()
        mod.record_balance(wid,10**18,100,cfg)
        self.assertEqual(self.events(),[])
        mod.record_balance(wid,10**18+1,101,cfg)
        self.assertEqual(self.events()[0]['delivery'],'off')
        mod.record_balance(wid,11*10**17+1,102,cfg)
        self.assertEqual(self.events()[1]['delivery'],'pending')

    def test_stale_block_is_rejected(self):
        self.wallet()
        def rpc(url,method,params):
            if method=='eth_chainId':return '0x38'
            return {'number':'0x100','timestamp':hex(int(time.time())-1000)}
        with patch('worker.rpc',side_effect=rpc):worker.check_once()
        w=self.client.get('/api/state').json['wallets'][0]
        self.assertIsNone(w['balance'])
        self.assertIn('отстаёт',w['error'])

    def test_missing_finalized_is_rejected(self):
        self.wallet()
        with patch('worker.rpc',side_effect=['0x38',None]):worker.check_once()
        w=self.client.get('/api/state').json['wallets'][0]
        self.assertIsNone(w['balance'])
        self.assertIn('finalized',w['error'])

    def test_delete_keeps_history_cancels_delivery_and_does_not_reuse_id(self):
        cfg=self.configure_telegram();wid=self.wallet()
        mod.record_balance(wid,0,100,cfg);mod.record_balance(wid,1,101,cfg)
        self.assertEqual(self.client.delete('/api/wallets/'+str(wid),headers={'X-CSRF-Token':self.csrf}).status_code,200)
        self.assertEqual(self.events()[0]['delivery'],'cancelled')
        self.assertGreater(self.wallet(),wid)

if __name__=='__main__':unittest.main()
