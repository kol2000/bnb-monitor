import unittest
from decimal import Decimal
from unittest.mock import patch
import mexc

class PrecisionTests(unittest.TestCase):
    def test_decimal_places(self):
        for value, expected in [('18','1e-18'),('8','1e-8'),('0','1'),('1','0.1')]:
            self.assertEqual(mexc.withdrawal_step(value), Decimal(expected))

    def test_explicit_fractional_step(self):
        self.assertEqual(mexc.withdrawal_step('0.0001'), Decimal('0.0001'))

    def test_invalid_precision(self):
        for value in [None, True, '', '-1', '19', 'NaN', 'Infinity', '1e-30']:
            with self.assertRaises(mexc.MexcError):
                mexc.withdrawal_step(value)

    def test_reported_bsc_balance(self):
        net={'withdrawFee':'0.00001','withdrawMin':'0.0004',
             'withdrawMax':'2000','withdrawIntegerMultiple':'18'}
        account={'canWithdraw':True,'balances':[{'asset':'BNB','free':'0.064000005992576067'}]}
        data={'bought':'0.064','network':'BSC','address':'0x'+'1'*40,'memo':''}
        with patch.object(mexc,'network',return_value=net), patch.object(mexc,'account',return_value=account):
            plan=mexc.withdrawal_plan(None,data)
        self.assertEqual(Decimal(plan['amount']),Decimal('0.06399'))
        self.assertEqual(Decimal(plan['fee']),Decimal('0.00001'))
        self.assertEqual(Decimal(plan['amount'])+Decimal(plan['fee']),Decimal('0.064'))
