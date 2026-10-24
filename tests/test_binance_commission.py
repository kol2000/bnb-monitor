from decimal import Decimal as D
from unittest.mock import patch
import test_binance as fixture
from test_binance import Exchange
import binance as m
import unittest

class CommissionTests(unittest.TestCase):
 def setUp(self): self.client=Exchange()
 def data(self): return self.client.call('/api/v3/account/commission')
 def rate(self,data,bnb='10'):
  with patch.object(self.client,'call',return_value=data):
   return m.buy_commission(self.client,D('50'),D('500'),D(bnb))
 def test_full_rate_and_buyer_tax_special(self):
  data=self.data();data['standardCommission']['buyer']='0.0001';data['taxCommission']['taker']='0.0002';data['specialCommission']['buyer']='0.0003'
  self.assertEqual(self.rate(data),(D('0.0016'),False))
 def test_discount_only_on_standard_and_sufficient_existing_bnb(self):
  data=self.data();data['discount'].update(enabledForAccount=True,enabledForSymbol=True);data['taxCommission']['taker']='0.0002'
  self.assertEqual(self.rate(data),(D('0.00095'),True))
  self.assertEqual(self.rate(data,'0'),(D('0.0012'),False))
 def test_zero_commission(self):
  data=self.data();data['standardCommission']['taker']='0'
  self.assertEqual(self.rate(data),(D(0),False))
 def test_incomplete_rates_stop_purchase(self):
  data=self.data();del data['specialCommission']
  with self.assertRaises(m.BinanceError):self.rate(data)
 def test_invalid_rate(self):
  data=self.data();data['standardCommission']['taker']='1'
  with self.assertRaises(m.BinanceError):self.rate(data)

class ConfirmationTests(unittest.TestCase):
 setUp=fixture.BinanceTests.setUp
 preview=fixture.BinanceTests.preview
 def test_changed_rate_does_not_place_order(self):
  job=self.preview();original=self.fake.call
  def changed(path,*args,**kwargs):
   value=original(path,*args,**kwargs)
   if path.endswith('/commission'):value['standardCommission']['taker']='0.002'
   return value
  self.fake.call=changed
  with self.assertRaises(m.BinanceError):m.buy_confirm(job['id'])
  self.assertFalse(any(c[2]=='POST' for c in self.fake.calls))
 def test_net_estimate_meets_target(self):
  d=self.preview()['data']
  self.assertGreaterEqual(D(d['net_estimate'])*D(d['commission_price']),D('50'))
  self.assertLess(D(d['total']),D('50.051'))
