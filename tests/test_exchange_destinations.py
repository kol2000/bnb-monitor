import json
import sqlite3
import unittest
import withdrawals as w

class Destinations(unittest.TestCase):
    def test_migration_preserves_custom_and_never_guesses_binance(self):
        for target, label in [(w.MEXC_DESTINATION,'MEXC'),('0x'+'1'*40,'Другая биржа')]:
            c=sqlite3.connect(':memory:')
            c.execute('CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT)')
            c.execute('INSERT INTO settings VALUES (?,?)',('withdraw_destination',json.dumps(target)))
            w.migrate_destinations(c)
            cfg={k:json.loads(v) for k,v in c.execute('SELECT * FROM settings')}
            self.assertEqual(cfg['withdraw_destination'],target)
            self.assertEqual(cfg['withdraw_exchange'],label)
            self.assertEqual(cfg['withdraw_binance_destination'],'')
            w.migrate_destinations(c)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM settings').fetchone()[0],len(w.DEFAULTS))
            c.close()
    def test_binance_requires_own_address(self):
        for value in ['',w.MEXC_DESTINATION]:
            with self.assertRaises(ValueError):w.destination_settings({'withdraw_exchange':'Binance','withdraw_binance_destination':value},w.DEFAULTS)
        cfg=w.destination_settings({'withdraw_exchange':'Binance','withdraw_binance_destination':'0x'+'2'*40},w.DEFAULTS)
        self.assertEqual(cfg['withdraw_destination'],'0x'+'2'*40)
        self.assertEqual(cfg['withdraw_mexc_destination'],w.MEXC_DESTINATION)
    def test_old_client_changes_dont_relabel_address(self):
        cfg=w.destination_settings({'withdraw_destination':'0x'+'3'*40},w.DEFAULTS)
        self.assertEqual(cfg['withdraw_exchange'],'Другая биржа')
        self.assertEqual(cfg['withdraw_destination'],'0x'+'3'*40)
