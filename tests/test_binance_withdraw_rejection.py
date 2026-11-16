"""Only explicit request rejection unlocks a fresh withdrawal confirmation."""
from unittest.mock import patch
from test_binance import BinanceTests
import binance as m

class WithdrawalRejectionTests(BinanceTests):
 def test_explicit_auth_rejection_returns_to_bought_without_resend(self):
  j=self.bought();m.withdraw_preview(j['id']);call=self.fake.call
  def rejected(path,*args,**kwargs):
   if path.endswith('/withdraw/apply'):raise m.request_error(-2015,'Denied')
   return call(path,*args,**kwargs)
  with patch.object(self.fake,'call',side_effect=rejected) as send:
   r=m.withdraw_confirm(j['id'])
   self.assertEqual(r['state'],'bought')
   self.assertTrue(r['data']['withdraw_rejected'])
   self.assertNotIn('plan_time',r['data'])
   with self.assertRaises(m.BinanceError):m.withdraw_confirm(j['id'])
   self.assertEqual(sum(x.args[0].endswith('/withdraw/apply') for x in send.call_args_list),1)
 def test_unknown_error_retains_original_error_after_empty_history(self):
  j=self.bought();m.withdraw_preview(j['id']);self.fake.fail_withdraw=True
  m.withdraw_confirm(j['id'])
  with patch.object(m,'history',return_value=[]):r=m.reconcile(j['id'])
  self.assertEqual(r['state'],'withdrawing')
  self.assertEqual(r['data']['withdraw_submit_error'],'Timeout')
  with self.assertRaises(m.BinanceError):m.withdraw_confirm(j['id'])
 def test_only_allowlisted_request_errors_are_definite_rejections(self):
  for code in (-2014,-2015,-1021,-1022):self.assertIsInstance(m.request_error(code,'error'),m.BinanceRequestRejected)
  for code in (None,-1007,-1006,-1000,500,'-2015'):
   self.assertNotIsInstance(m.request_error(code,'error'),m.BinanceRequestRejected)
