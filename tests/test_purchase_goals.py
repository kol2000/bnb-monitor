from decimal import Decimal
from unittest.mock import patch
from test_binance import Exchange
import app
import binance
import unittest

class PurchaseGoalTests(unittest.TestCase):
    def setUp(self):
        with app.db() as c:c.execute('DELETE FROM binance_jobs')
        self.fake=Exchange()
        p=patch.object(binance,'Client',return_value=self.fake);p.start();self.addCleanup(p.stop)
    def preview(self,goal='wallet'):
        return binance.buy_preview({'amount':'50','network':'BSC','address':'0x'+'1'*40,'goal':goal})
    def test_wallet_target_includes_fee_and_reserve(self):
        j=self.preview();d=j['data']
        self.assertGreater(Decimal(d['total']),Decimal('51'))
        self.assertGreaterEqual(Decimal(d['recipient_usdt_low']),Decimal('50'))
        self.assertEqual(d['fee'],'0.001')
        self.assertFalse(any(c[2]=='POST' for c in self.fake.calls))
    def test_trade_mode_keeps_target_after_trade(self):
        d=self.preview('trade')['data'];self.assertEqual(d['total'],'50.05005006')
        self.assertLess(Decimal(d['recipient_usdt_low']),Decimal('50'))
    def test_fee_change_blocks_wallet_goal_before_purchase(self):
        j=self.preview();self.fake.fee='0.002'
        with self.assertRaises(binance.BinanceError):binance.buy_confirm(j['id'])
        self.assertFalse(any(c[2]=='POST' for c in self.fake.calls))
    def test_unknown_goal_rejected(self):
        with self.assertRaises(binance.BinanceError):self.preview('invalid')
    def test_actual_fills_in_history(self):
        j=self.preview('trade');binance.buy_confirm(j['id']);d=binance.reconcile(j['id'])['data']
        self.assertEqual(d['trade_fees'],{'BNB':'0.0001'})
        self.assertEqual(d['average_price'],'500.5005006')
        self.assertEqual(d['gross_bnb'],'0.1')
        self.assertEqual(d['bought'],'0.0999')
