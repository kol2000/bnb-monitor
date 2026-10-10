import unittest
from unittest.mock import patch
from decimal import Decimal
from test_mexc import Exchange
import app
import mexc as m

class SellExchange(Exchange):
    def __init__(self):
        super().__init__()
        self.balance='0.064000005992576067'
    def call(self,path,params=None,method='GET',signed=True):
        r=super().call(path,params,method,signed)
        if path.endswith('/account'):
            r['balances'][1]['free']=self.balance
        elif path.endswith('/exchangeInfo'):
            r['symbols'][0].update(baseAssetPrecision=3,baseSizePrecision='0.001')
        elif path.endswith('/order') and method=='GET':
            r.update(side='SELL',executedQty='0.064',cummulativeQuoteQty='32')
        elif path.endswith('/myTrades'):
            r[0].update(qty='0.064',commission='0.032',commissionAsset='USDT')
        return r

class SellTests(unittest.TestCase):
    def setUp(self):
        with app.db() as c:c.execute('DELETE FROM mexc_jobs')
        self.fake=SellExchange()
        p=patch.object(m,'Client',return_value=self.fake);p.start();self.addCleanup(p.stop)
    def test_free_balance_rounded_down(self):
        j=m.sell_preview();d=j['data']
        self.assertEqual(d['quantity'],'0.064')
        self.assertEqual(Decimal(d['dust']),Decimal('0.000000005992576067'))
        self.assertFalse(any(c[2]=='POST' for c in self.fake.calls))
    def test_market_sell_and_net_proceeds(self):
        j=m.sell_preview();m.sell_confirm(j['id']);result=m.reconcile(j['id'])
        posts=[c for c in self.fake.calls if c[2]=='POST']
        self.assertEqual(len(posts),1)
        self.assertEqual(posts[0][1]['side'],'SELL')
        self.assertEqual(posts[0][1]['quantity'],'0.064')
        self.assertNotIn('quoteOrderQty',posts[0][1])
        self.assertEqual(result['state'],'done')
        self.assertEqual(result['data']['received_usdt'],'31.968')
    def test_timeout_no_repeat(self):
        j=m.sell_preview();self.fake.fail_buy=True;m.sell_confirm(j['id'])
        with self.assertRaises(m.MexcError):m.sell_confirm(j['id'])
        self.assertEqual(m.reconcile(j['id'])['state'],'done')
        self.assertEqual(sum(c[2]=='POST' for c in self.fake.calls),1)
    def test_dust_below_minimum(self):
        self.fake.balance='0.00001'
        with self.assertRaises(m.MexcError):m.sell_preview()
    def test_balance_changes_require_reconfirmation(self):
        j=m.sell_preview();self.fake.balance='0.1'
        with self.assertRaises(m.MexcError):m.sell_confirm(j['id'])
        self.assertFalse(any(c[2]=='POST' for c in self.fake.calls))
    def test_buy_endpoint_rejects_sell_preview(self):
        j=m.sell_preview()
        with self.assertRaises(m.MexcError):m.buy_confirm(j['id'])
    def test_active_job_blocks_sale(self):
        j=m.sell_preview();m.sell_confirm(j['id'])
        with self.assertRaises(m.MexcError):m.sell_preview()
    def test_partial_sale_stays_pending(self):
        j=m.sell_preview();m.sell_confirm(j['id']);self.fake.partial=True
        self.assertEqual(m.reconcile(j['id'])['state'],'buying')
