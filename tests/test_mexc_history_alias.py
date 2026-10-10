import unittest
from unittest.mock import patch, Mock
import mexc

class HistoryAliasTests(unittest.TestCase):
    def test_exact_native_bnb_alias(self):
        self.assertTrue(mexc.withdrawal_asset_matches({'coin':'BNB-BSC','network':'BNB Smart Chain(BEP20)'},'BSC'))
        self.assertTrue(mexc.withdrawal_asset_matches({'coin':'BNB'},'BSC'))

    def test_wrong_asset_or_network_rejected(self):
        for coin, network, selected in [('USDT-BSC','BSC','BSC'),('BNB-BSC','ETH','BSC'),('BNB-BSC','BSC','ETH'),('BNB-OTHER','BSC','BSC')]:
            self.assertFalse(mexc.withdrawal_asset_matches({'coin':coin,'network':network},selected))

    def check_history(self, address):
        d={'address':'0x'+'a'*40,'network':'BSC','withdraw_time':123,'withdraw_id':'withdraw-1'}
        job={'id':'job-1','state':'withdrawing','data':d}
        row={'id':'withdraw-1','coin':'BNB-BSC','network':'BNB Smart Chain(BEP20)',
             'address':address,'status':7,'txId':'0x123'}
        client=Mock()
        with patch.object(mexc,'get_job',return_value=job), patch.object(mexc,'Client',return_value=client), patch.object(mexc,'history',return_value=[row]), patch.object(mexc,'save') as save:
            mexc.reconcile('job-1')
            client.call.assert_not_called()
            return save.call_args.args

    def test_completed_alias_clears_error_without_transfer(self):
        args=self.check_history('0x'+'A'*40)
        self.assertEqual(args[1],'done')
        self.assertEqual(args[2]['txid'],'0x123')
        self.assertEqual(args[3],'')

    def test_wrong_recipient_still_blocks_completion(self):
        args=self.check_history('0x'+'b'*40)
        self.assertEqual(args[1],'withdrawing')
        self.assertIn('не совпали',args[3])
