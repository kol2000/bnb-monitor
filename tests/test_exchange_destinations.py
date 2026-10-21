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
    def test_only_binance_and_no_old_address(self):
        for data in [{'withdraw_exchange':'MEXC'}, {'withdraw_binance_destination':w.MEXC_DESTINATION}]:
            with self.assertRaises(ValueError):w.destination_settings(data,w.DEFAULTS)
        cfg=w.destination_settings({'withdraw_binance_destination':'0x'+'2'*40},w.DEFAULTS)
        self.assertEqual(cfg['withdraw_exchange'],'Binance')
        self.assertEqual(cfg['withdraw_destination'],'0x'+'2'*40)
    def test_retirement_preserves_old_fields_and_never_copies_mexc(self):
        for target in ['', '0x'+'2'*40]:
            c=sqlite3.connect(':memory:');c.execute('CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT)')
            for k,v in dict(w.DEFAULTS,withdraw_binance_destination=target).items():
                c.execute('INSERT INTO settings VALUES (?,?)',(k,json.dumps(v)))
            w.activate_binance(c)
            cfg={k:json.loads(v) for k,v in c.execute('SELECT * FROM settings')}
            self.assertEqual(cfg['withdraw_destination'],target)
            self.assertEqual(cfg['withdraw_exchange'],'Binance')
            self.assertEqual(cfg['withdraw_mexc_destination'],w.MEXC_DESTINATION)
            c.close()
    def test_unconfigured_binance_allows_unrelated_settings(self):
        self.assertEqual(w.destination_settings({},w.DEFAULTS)['withdraw_destination'],'')
