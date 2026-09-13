import json
import time
import unittest
from unittest.mock import patch
import test_monitor
from test_monitor import mod, worker
from prices import parse_quote, usd

class PriceTests(unittest.TestCase):
    setUp = test_monitor.MonitorTests.setUp
    post = test_monitor.MonitorTests.post
    wallet = test_monitor.MonitorTests.wallet
    events = test_monitor.MonitorTests.events
    def test_parse_and_validate_asset(self):
        detail = {'id': 1839, 'symbol': 'BNB', 'statistics': {'price': '718.123456789'},
                  'latestUpdateTime': '2026-09-13T15:37:00.000Z'}
        def page():
            return '<script id="__NEXT_DATA__">'+json.dumps({'props': {'pageProps': {'detailRes': {'detail': detail}}}})+'</script>'
        self.assertEqual(parse_quote(page())[0], '718.123456789')
        detail['id'] = 1
        with self.assertRaises(ValueError): parse_quote(page())

    def test_usd_missing_zero_and_dust(self):
        self.assertIsNone(usd(None, '700'))
        self.assertIsNone(usd(1, None))
        self.assertEqual(usd(0, '700'), '$0.00')
        self.assertEqual(usd(1, '700'), '< $0.01')
        self.assertEqual(usd(2*10**18, '700.125'), '$1,400.25')

    def test_quote_failure_keeps_cache_and_balances(self):
        wid = self.wallet()
        mod.record_balance(wid, 10**18, 100, mod.settings())
        with patch('worker.fetch_quote', return_value=('700', time.time())):
            worker.refresh_price()
        with patch('worker.fetch_quote', side_effect=ValueError('unavailable')):
            worker.refresh_price()
        state = self.client.get('/api/state').json
        self.assertEqual(state['total_usd'], '$700.00')
        self.assertEqual(state['wallets'][0]['usd'], '$700.00')
        self.assertTrue(state['price']['error'])
        self.assertEqual(self.events(), [])
        mod.set_status(price_at=time.time()-3600)
        self.assertTrue(self.client.get('/api/state').json['price']['stale'])
