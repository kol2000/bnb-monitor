"""Offline tests only. This published development mnemonic holds no real funds."""
import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch

tmp = tempfile.TemporaryDirectory()
os.environ.setdefault('DATA_DIR', tmp.name)
os.environ.setdefault('ADMIN_PASSWORD', 'test-password-only-123')
os.environ.setdefault('SECRET_KEY', 'test-secret-' * 5)
import app
import withdrawals as wd
from eth_account import Account

Account.enable_unaudited_hdwallet_features()
MNEMONIC = 'test test test test test test test test test test test junk'
PAIR = tuple(Account.from_mnemonic(MNEMONIC, account_path=p) for p in wd.PATHS)
SOURCE, MIDDLE = (a.address.lower() for a in PAIR)
TARGET = wd.DEFAULTS['withdraw_destination']


class FakeRPC:
    def __init__(self):
        self.balances = {SOURCE: 10**17, MIDDLE: 10**16}
        self.nonces = {SOURCE: 3, MIDDLE: 7}
        self.pending = {}
        self.receipts = {}
        self.sent = []
        self.fail_ack = False
        self.chain = 56
        self.code = {}
        self.final = 100
        self.gas = 100000000
    def __call__(self, method, params):
        if method == 'eth_chainId': return hex(self.chain)
        if method == 'eth_getBlockByNumber':
            return {'timestamp': hex(int(time.time())), 'number': hex(self.final), 'hash': '0x'+'ab'*32}
        if method == 'eth_getCode': return self.code.get(params[0], '0x')
        if method == 'eth_getBalance': return hex(self.balances[params[0]])
        if method == 'eth_getTransactionCount':
            return hex(self.pending.get(params[0], self.nonces[params[0]]) if params[1] == 'pending' else self.nonces[params[0]])
        if method == 'eth_gasPrice': return hex(self.gas)
        if method == 'eth_estimateGas': return hex(wd.GAS)
        if method == 'eth_getTransactionReceipt': return self.receipts.get(params[0])
        if method == 'eth_sendRawTransaction':
            from eth_utils import keccak
            self.sent.append(params[0])
            if self.fail_ack: raise app.RemoteError('Lost ACK')
            return '0x'+keccak(bytes.fromhex(params[0][2:])).hex()
        raise AssertionError(method)


class WithdrawTests(unittest.TestCase):
    def setUp(self):
        self.rpc = FakeRPC()
        with app.db() as c:
            c.execute('DELETE FROM withdrawals')
            c.execute('DELETE FROM wallets')
            self.wid = c.execute('INSERT INTO wallets(address,name,balance) VALUES (?,?,?)', (SOURCE, 'Test', str(10**17))).lastrowid
            for k, v in {'sheets_enabled': True, 'sheets_id': 'test-sheet', 'sheets_tab': 'bnb кошельки', 'withdraw_destination': TARGET}.items():
                c.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (k, json.dumps(v)))
        self.patches = [patch('withdrawals.accounts_for', return_value=PAIR), patch('app.rpc', side_effect=lambda u,m,p:self.rpc(m,p))]
        for p in self.patches: p.start()
        self.addCleanup(lambda: [p.stop() for p in self.patches])

    def ready(self):
        p = wd.preview(self.wid)
        wd.confirm(p['id'])
        return self.row(p['id'])

    def row(self, ident):
        with app.db() as c: return dict(c.execute('SELECT * FROM withdrawals WHERE id=?', (ident,)).fetchone())

    def receipt(self, h, status=1):
        self.rpc.receipts[h] = {'transactionHash': h, 'blockNumber': '0x64', 'blockHash': '0x'+'ab'*32, 'status': hex(status)}

    def test_plan_values_and_no_send_before_confirmation(self):
        p = wd.preview(self.wid)
        self.assertEqual(p['source'], SOURCE)
        self.assertEqual(p['middle'], MIDDLE)
        self.assertEqual(p['destination'], TARGET)
        self.assertEqual(p['value2'], app.bnb(11*10**16-2*wd.GAS*self.rpc.gas))
        self.assertEqual(self.rpc.sent, [])
        self.assertNotIn(MNEMONIC, json.dumps(self.row(p['id'])))

    def test_signed_transactions_and_idempotent_confirm(self):
        row = self.ready()
        self.assertEqual(Account.recover_transaction(row['raw1']).lower(), SOURCE)
        self.assertEqual(Account.recover_transaction(row['raw2']).lower(), MIDDLE)
        from eth_account._utils.legacy_transactions import Transaction
        for leg, target in ((1, MIDDLE), (2, TARGET)):
            tx = Transaction.from_bytes(bytes.fromhex(row[f'raw{leg}'][2:])).as_dict()
            self.assertEqual('0x'+tx['to'].hex(), target)
            self.assertEqual((tx['v']-35)//2, 56)
            self.assertEqual(tx['gas'], 21000)
        wd.confirm(row['id'])
        self.assertEqual(self.row(row['id'])['raw1'], row['raw1'])
        self.assertEqual(self.rpc.sent, [])
        self.assertNotIn('raw1', wd.public(row))
        self.assertNotIn('url', wd.public(row))

    def test_lost_ack_rebroadcasts_identical_transaction(self):
        row = self.ready()
        self.rpc.fail_ack = True
        wd.tick(); wd.tick()
        self.assertEqual(self.rpc.sent, [row['raw1'], row['raw1']])
        self.assertEqual(self.row(row['id'])['state'], 'pending1')
        self.assertEqual(self.row(row['id'])['attempted1'], 1)

    def test_both_legs_with_finality_and_existing_second_balance(self):
        row = self.ready(); plan = json.loads(row['plan'])
        wd.tick()
        self.assertEqual(self.rpc.sent, [row['raw1']])
        self.receipt(row['hash1']); self.rpc.final = 99
        wd.tick()
        self.assertEqual(self.row(row['id'])['state'], 'pending1')
        self.rpc.final = 100; wd.tick()
        self.assertEqual(self.row(row['id'])['state'], 'pending2')
        self.rpc.balances[MIDDLE] += int(plan['value1'])
        wd.tick()
        self.assertEqual(self.rpc.sent, [row['raw1'], row['raw2']])
        self.receipt(row['hash2']); wd.tick()
        self.assertEqual(self.row(row['id'])['state'], 'done')
        wd.tick(); self.assertEqual(len(self.rpc.sent), 2)

    def test_reverted_first_never_sends_second(self):
        row = self.ready(); self.receipt(row['hash1'], 0); wd.tick()
        self.assertEqual(self.row(row['id'])['state'], 'failed')
        self.assertEqual(self.rpc.sent, [])

    def test_stale_confirmation(self):
        p = wd.preview(self.wid)
        with app.db() as c: c.execute('UPDATE withdrawals SET created=0')
        with self.assertRaises(ValueError): wd.confirm(p['id'])
        self.assertEqual(self.rpc.sent, [])

    def test_changed_balance_before_confirmation(self):
        p = wd.preview(self.wid); self.rpc.balances[SOURCE] += 1
        with self.assertRaises(ValueError): wd.confirm(p['id'])

    def test_changed_destination_requires_new_preview(self):
        p = wd.preview(self.wid)
        with app.db() as c: c.execute('UPDATE settings SET value=? WHERE key=?', (json.dumps('0x'+'1'*40), 'withdraw_destination'))
        with self.assertRaises(ValueError): wd.confirm(p['id'])

    def test_destination_changes_do_not_retarget_signed_job(self):
        row = self.ready()
        with app.db() as c: c.execute('UPDATE settings SET value=? WHERE key=?', (json.dumps('0x'+'1'*40), 'withdraw_destination'))
        wd.tick()
        self.assertEqual(self.rpc.sent, [row['raw1']])
        self.assertEqual(wd.public(self.row(row['id']))['destination'], TARGET)

    def test_balance_changed_before_broadcast_blocks(self):
        row = self.ready(); self.rpc.balances[SOURCE] += 1; wd.tick()
        self.assertEqual(self.row(row['id'])['state'], 'blocked')
        self.assertEqual(self.rpc.sent, [])

    def test_active_job_prevents_second(self):
        p = wd.preview(self.wid); self.ready()
        with self.assertRaises(ValueError): wd.confirm(p['id'])
        with self.assertRaises(ValueError): wd.preview(self.wid)

    def test_insufficient_gas_and_pending_nonce(self):
        self.rpc.balances[SOURCE] = 1
        with self.assertRaises(ValueError): wd.preview(self.wid)
        self.rpc.balances[SOURCE] = 10**17; self.rpc.pending[SOURCE] = 4
        with self.assertRaises(ValueError): wd.preview(self.wid)

    def test_wrong_chain_and_contract_destination(self):
        self.rpc.chain = 1
        with self.assertRaises(app.RemoteError): wd.preview(self.wid)
        self.rpc.chain = 56; self.rpc.code[TARGET] = '0xab'
        with self.assertRaises(ValueError): wd.preview(self.wid)

    def test_nonce_advanced_without_receipt_never_resigns(self):
        row = self.ready(); self.rpc.nonces[SOURCE] += 1; wd.tick()
        self.assertEqual(self.row(row['id'])['state'], 'pending1')
        self.assertEqual(self.rpc.sent, [])

    def test_auth_and_csrf(self):
        client = app.app.test_client()
        self.assertEqual(client.get('/api/withdrawals').status_code, 401)
        self.assertEqual(client.post(f'/api/wallets/{self.wid}/withdraw/preview', json={}).status_code, 403)

    def test_cancel_only_before_first_attempt(self):
        row = self.ready(); self.rpc.balances[SOURCE] += 1; wd.tick()
        client = app.app.test_client()
        with client.session_transaction() as session:
            session['auth'] = True
            session['csrf'] = 'test-token'
        url = '/api/withdrawals/'+row['id']+'/cancel'
        self.assertTrue(wd.public(self.row(row['id']))['can_cancel'])
        self.assertEqual(client.post(url, json={}, headers={'X-CSRF-Token':'test-token'}).status_code, 200)
        self.assertIsNone(self.row(row['id'])['raw1'])
        row = self.ready(); wd.tick()
        wd.update(row['id'], state='blocked')
        url = '/api/withdrawals/'+row['id']+'/cancel'
        self.assertEqual(client.post(url, json={}, headers={'X-CSRF-Token':'test-token'}).status_code, 400)
        self.assertFalse(wd.public(self.row(row['id']))['can_cancel'])

    def test_changed_second_balance_pauses_without_sending(self):
        row = self.ready(); wd.tick(); self.receipt(row['hash1']); wd.tick()
        self.rpc.balances[MIDDLE] += int(json.loads(row['plan'])['value1']) + 1
        wd.tick()
        self.assertEqual(self.row(row['id'])['state'], 'blocked')
        self.assertFalse(wd.public(self.row(row['id']))['can_cancel'])
        self.assertEqual(self.rpc.sent, [row['raw1']])


class SeedMappingTests(unittest.TestCase):
    def test_only_target_seed_read_and_address_verified(self):
        cfg = {'sheets_enabled': True, 'sheets_id': 'test', 'sheets_tab': 'bnb кошельки'}
        with patch('sheets_sync.google_credentials') as creds, patch('sheets_sync.export_request') as req:
            creds.return_value.token = 'mock'
            req.side_effect = [{'values': [['адрес'], [SOURCE]]}, {'values': [[SOURCE, MNEMONIC]]}]
            pair = wd.accounts_for(cfg, SOURCE)
            self.assertEqual(pair[1].address.lower(), MIDDLE)
            self.assertIn('B1%3AB5000', req.call_args_list[0].args[2])
            self.assertIn('B2%3AC2', req.call_args_list[1].args[2])
            req.side_effect = [{'values': [[MIDDLE]]}, {'values': [[MIDDLE, MNEMONIC]]}]
            with self.assertRaisesRegex(ValueError, 'не совпадает'): wd.accounts_for(cfg, MIDDLE)

    def test_bad_seed_is_redacted(self):
        with patch('sheets_sync.google_credentials'), patch('sheets_sync.export_request') as req:
            req.side_effect = [{'values': [[SOURCE]]}, {'values': [[SOURCE, 'SECRET invalid words']]}]
            with self.assertRaises(app.RemoteError) as err:
                wd.accounts_for({'sheets_enabled': True, 'sheets_id': 'test', 'sheets_tab': 'test'}, SOURCE)
            self.assertNotIn('SECRET', str(err.exception))


if __name__ == '__main__': unittest.main()
