"""Live RPC statistics are persisted mid-cycle, not only at RPC exit."""
import json
import time
import unittest
from unittest.mock import patch

from test_monitor import MonitorTests
import app
import worker


class RpcLiveStatsTests(MonitorTests):
    def test_live_provider_stats_every_ten_attempts(self):
        addresses = ['0x' + format(n, '040x') for n in range(1, 13)]
        response = self.post('wallets', {'wallets': [
            {'address': address, 'name': f'Wallet {n}'}
            for n, address in enumerate(addresses)
        ]})
        self.assertEqual(response.status_code, 200)
        snapshots = []
        original = worker.set_status

        def capture(**kwargs):
            if 'rpc_cycle_stats' in kwargs:
                snapshots.append([dict(v) for v in kwargs['rpc_cycle_stats']])
            return original(**kwargs)

        def fake_rpc(url, method, params):
            if method == 'eth_chainId':
                return '0x38'
            if method == 'eth_getBlockByNumber':
                return {'number': '0x10', 'timestamp': hex(int(time.time()))}
            if method == 'eth_getBalance':
                return '0x10'
            raise AssertionError(method)

        with patch.object(worker, 'rpc', side_effect=fake_rpc), \\
             patch.object(worker, 'set_status', side_effect=capture), \\
             patch.object(worker.stop, 'wait'):
            worker.check_once()

        values = [(snap[0]['attempts'], snap[0]['success']) for snap in snapshots]
        self.assertIn((10, 10), values)
        self.assertIn((12, 12), values)
        with app.db() as c:
            row = c.execute("SELECT value FROM status WHERE key='rpc_cycle_stats'").fetchone()
        self.assertEqual(json.loads(row['value'])[0]['success'], 12)
