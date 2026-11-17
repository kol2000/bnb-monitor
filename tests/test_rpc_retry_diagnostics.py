"""Offline regression tests for read-only RPC recovery and provider counters."""
import time
import unittest
from unittest.mock import patch

import test_monitor as base
import app
import worker


class RpcRetryDiagnosticTests(unittest.TestCase):
    def test_timeout_is_retried_once_and_counted(self):
        recoveries = []
        with patch('worker.rpc', side_effect=[
            app.RemoteError('Тайм-аут подключения к сервису.'),
            '0x38'
        ]) as send, patch.object(worker.stop, 'wait') as wait:
            value = worker.retry_rpc_read('https://example.org', 'eth_chainId', [],
                                          lambda: recoveries.append(True))
        self.assertEqual(value, '0x38')
        self.assertEqual(send.call_count, 2)
        self.assertEqual(recoveries, [True])
        wait.assert_called_once_with(0.25)

    def test_two_timeouts_stop_after_one_retry(self):
        with patch('worker.rpc', side_effect=app.RemoteError('Тайм-аут подключения к сервису.')) as send, \
             patch.object(worker.stop, 'wait'):
            with self.assertRaises(app.RemoteError):
                worker.retry_rpc_read('https://example.org', 'eth_getBalance',
                                      ['0x'+'0'*40, 'latest'])
        self.assertEqual(send.call_count, 2)

    def test_non_timeout_is_not_retried(self):
        with patch('worker.rpc', side_effect=app.RemoteError('Сервис вернул HTTP 429.')) as send:
            with self.assertRaises(app.RemoteError):
                worker.retry_rpc_read('https://example.org', 'eth_getBalance', [])
        send.assert_called_once()

    def test_write_method_is_never_retried(self):
        with patch('worker.rpc', side_effect=app.RemoteError('Тайм-аут подключения к сервису.')) as send:
            with self.assertRaises(app.RemoteError):
                worker.retry_rpc_read('https://example.org', 'eth_sendRawTransaction', ['fake'])
        send.assert_called_once()

    def test_provider_counters_with_second_rpc_recovery(self):
        instance = base.MonitorTests()
        instance.setUp()
        self.addCleanup(instance.tearDown) if hasattr(instance, 'tearDown') else None
        instance.wallet()
        instance.post('settings', {'rpc_urls': ['https://one.example', 'https://two.example']})
        state = {'primary_calls': 0}
        def rpc(url, method, params):
            if method == 'eth_chainId':
                return '0x38'
            if method == 'eth_getBlockByNumber':
                return {'number': '0x100', 'timestamp': hex(int(time.time()))}
            if method == 'eth_getBalance':
                if url == 'https://one.example':
                    state['primary_calls'] += 1
                    raise app.RemoteError('Тайм-аут подключения к сервису.')
                return '0x1'
            raise AssertionError(method)
        with patch('worker.rpc', side_effect=rpc), patch.object(worker.stop, 'wait'):
            worker.check_once()
        with app.db() as db:
            row = db.execute("SELECT value FROM status WHERE key='rpc_cycle_stats'").fetchone()
            import json
            stats = json.loads(row['value'])
        self.assertEqual(state['primary_calls'], 2)
        self.assertEqual(stats[0]['attempts'], 1)
        self.assertEqual(stats[0]['errors'], 1)
        self.assertEqual(stats[0]['success'], 0)
        self.assertEqual(stats[1]['attempts'], 1)
        self.assertEqual(stats[1]['success'], 1)
        self.assertEqual(stats[1]['failover_recovered'], 1)
        self.assertEqual(instance.client.get('/api/state').json['status']['failed'], 0)


if __name__ == '__main__':
    unittest.main()
