"""Offline swaps and token-route tests. Public test mnemonic; no live RPC or sends."""
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from eth_abi import encode, decode
from eth_utils import keccak
from eth_account import Account
from eth_account._utils.legacy_transactions import Transaction

_tmp=tempfile.TemporaryDirectory()
os.environ.setdefault('DATA_DIR',_tmp.name)
os.environ.setdefault('ADMIN_PASSWORD','test-password-only-123')
os.environ.setdefault('SECRET_KEY','test-secret-'*5)
import app
import swaps as sw
import withdrawals as wd
Account.enable_unaudited_hdwallet_features()
PAIR=tuple(Account.from_mnemonic('test test test test test test test test test test test junk',account_path=p) for p in wd.PATHS)
SOURCE,MIDDLE=(a.address.lower() for a in PAIR)
TARGET='0x'+'2'*40

class Node:
    def __init__(self):
        self.balances={SOURCE:10**17,MIDDLE:0}
        self.tokens={SOURCE:0,MIDDLE:0}
        self.nonces={SOURCE:0,MIDDLE:0}
        self.receipts={};self.sent=[];self.fail=False;self.price=100000000
        self.rate=600;self.bad_factory=False;self.estimate=80000;self.chain=56
    def __call__(self,method,params):
        if method=='eth_chainId':return hex(self.chain)
        if method=='eth_getBlockByNumber':return {'number':'0x64','hash':'0x'+'a'*64,'timestamp':hex(int(time.time()))}
        if method=='eth_getCode':return '0x1234' if params[0] in (sw.USDT,sw.ROUTER) else '0x'
        if method=='eth_getTransactionCount':return hex(self.nonces[params[0]])
        if method=='eth_getBalance':return hex(self.balances[params[0]])
        if method=='eth_gasPrice':return hex(self.price)
        if method=='eth_estimateGas':return hex(21000 if params[0].get('data','0x')=='0x' else self.estimate)
        if method=='eth_call':
            data=params[0]['data'];sig=data[:10];args=bytes.fromhex(data[10:])
            if sig==sw.calldata('decimals()'):return '0x'+encode(['uint8'],[18]).hex()
            if sig==sw.calldata('WETH()'):return '0x'+encode(['address'],[sw.WBNB]).hex()
            if sig==sw.calldata('factory()'):return '0x'+encode(['address'],[TARGET if self.bad_factory else sw.FACTORY]).hex()
            if sig=='0x70a08231':
                address=decode(['address'],args)[0];return '0x'+encode(['uint256'],[self.tokens.get(address,0)]).hex()
            if sig=='0xd06ca61f':
                amount,path=decode(['uint256','address[]'],args)
                return '0x'+encode(['uint256[]'],[[amount,amount*self.rate]]).hex()
            raise AssertionError(data)
        if method=='eth_getTransactionReceipt':return self.receipts.get(params[0])
        if method=='eth_sendRawTransaction':
            self.sent.append(params[0])
            if self.fail:raise app.RemoteError('lost acknowledgement')
            return '0x'+keccak(bytes.fromhex(params[0][2:])).hex()
        raise AssertionError(method)
    def mine(self,row,status=1,token_amount=None,recipient=None):
        tx=Transaction.from_bytes(bytes.fromhex(row['raw'][2:])).as_dict()
        source=Account.recover_transaction(row['raw']).lower()
        target='0x'+tx['to'].hex()
        self.nonces[source]+=1
        self.balances[source]-=tx['value']+tx['gas']*tx['gasPrice']
        if target in self.balances:self.balances[target]+=tx['value']
        logs=[]
        if token_amount is not None:
            self.tokens[recipient]=self.tokens.get(recipient,0)+token_amount
            if target==sw.USDT:self.tokens[source]-=token_amount
            logs=[dict(address=sw.USDT,topics=['0x'+keccak(text='Transfer(address,address,uint256)').hex(),'0x'+'0'*64,'0x'+recipient[2:].zfill(64)],data=hex(token_amount))]
        self.receipts[row['hash']]={'transactionHash':row['hash'],'blockNumber':'0x64','blockHash':'0x'+'a'*64,'status':hex(status),'logs':logs}

class SwapTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        p=patch.object(app,'DB',Path(tmp.name)/'monitor.db');p.start();self.addCleanup(p.stop);app.init_db()
        self.node=Node()
        self.cfg=dict(app.settings(),sheets_enabled=True,sheets_id='test-sheet',sheets_tab='bnb кошельки',rpc_urls=['https://test.invalid'],withdraw_exchange='Binance',withdraw_destination=TARGET,withdraw_binance_destination=TARGET)
        with app.db() as c:
            self.wid=c.execute('INSERT INTO wallets(address,name) VALUES (?,?)',(SOURCE,'Test')).lastrowid
            for k,v in self.cfg.items():c.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',(k,json.dumps(v)))
        for p in (patch('withdrawals.accounts_for',return_value=PAIR),patch('app.rpc',side_effect=lambda u,m,p:self.node(m,p))):
            p.start();self.addCleanup(p.stop)
    def row(self,ident):
        with app.db() as c:return dict(c.execute('SELECT * FROM swaps WHERE id=?',(ident,)).fetchone())
    def ready(self,kind='swap'):
        p=sw.preview(self.wid,kind,TARGET);sw.confirm(p['id']);return self.row(p['id'])
    def test_preview_reserves_both_accounts_and_does_not_send(self):
        p=sw.preview(self.wid,'swap');r=self.row(p['id']);plan=json.loads(r['plan'])
        self.assertGreater(int(plan['reserve']),int(plan['middle_reserve']))
        self.assertEqual(int(plan['amount'])+int(plan['retained'])+int(plan['fee']),10**17)
        self.assertEqual(plan['destination'],SOURCE);self.assertEqual(self.node.sent,[])
        self.assertNotIn('test test',r['plan']);self.assertNotIn('url',p)
    def test_signed_swap_calldata_chain_amount_minimum_and_destination(self):
        r=self.ready();p=json.loads(r['plan']);tx=Transaction.from_bytes(bytes.fromhex(r['raw'][2:])).as_dict()
        self.assertEqual(Account.recover_transaction(r['raw']).lower(),SOURCE)
        self.assertEqual('0x'+tx['to'].hex(),sw.ROUTER);self.assertEqual((tx['v']-35)//2,56)
        minimum,path,to,deadline=decode(['uint256','address[]','address','uint256'],tx['data'][4:])
        self.assertEqual(to,SOURCE);self.assertEqual(list(path),sw.PATH)
        self.assertEqual(minimum,int(p['minimum']));self.assertEqual(tx['value'],int(p['amount']))
        self.assertEqual(deadline,p['deadline']);self.assertEqual(self.node.sent,[])
    def test_timeout_reuses_same_signed_bytes_and_blocks_new_operation(self):
        r=self.ready();self.node.fail=True;sw.tick();sw.tick()
        self.assertEqual(self.node.sent,[r['raw'],r['raw']]);self.assertEqual(self.row(r['id'])['attempted'],1)
        self.assertEqual(sw.confirm(r['id'])['id'],r['id'])
        with self.assertRaises(ValueError):sw.preview(self.wid,'swap')
        with self.assertRaises(ValueError):wd.preview(self.wid)
    def test_finality_success_logs_and_balance_queue(self):
        r=self.ready();p=json.loads(r['plan']);sw.tick();self.node.mine(r,token_amount=int(p['expected']),recipient=SOURCE);sw.tick()
        done=self.row(r['id']);self.assertEqual(done['state'],'done');self.assertEqual(done['received'],p['expected'])
        with app.db() as c:self.assertEqual(c.execute('SELECT count(*) FROM priority_balances').fetchone()[0],2)
    def test_receipt_without_transfer_log_never_reports_success(self):
        r=self.ready();sw.tick();self.node.mine(r);sw.tick();self.assertEqual(self.row(r['id'])['state'],'blocked')
    def test_revert_not_retried(self):
        r=self.ready();sw.tick();self.node.mine(r,status=0);sw.tick();sw.tick()
        self.assertEqual(self.row(r['id'])['state'],'failed');self.assertEqual(len(self.node.sent),1)
    def test_expired_preview_and_changed_balance_rejected(self):
        p=sw.preview(self.wid,'swap')
        with app.db() as c:c.execute('UPDATE swaps SET created=0 WHERE id=?',(p['id'],))
        with self.assertRaises(ValueError):sw.confirm(p['id'])
        p=sw.preview(self.wid,'swap');self.node.balances[SOURCE]-=1
        with self.assertRaises(ValueError):sw.confirm(p['id'])
    def test_changed_price_or_nonce_rejected(self):
        p=sw.preview(self.wid,'swap');self.node.rate=500
        with self.assertRaises(ValueError):sw.confirm(p['id'])
        self.node.rate=600;self.node.nonces[MIDDLE]=2
        with self.assertRaises(ValueError):sw.confirm(p['id'])
    def test_wrong_contract_chain_and_insufficient_gas(self):
        self.node.bad_factory=True
        with self.assertRaises(ValueError):sw.preview(self.wid,'swap')
        self.node.bad_factory=False;self.node.chain=1
        with self.assertRaises(app.RemoteError):sw.preview(self.wid,'swap')
        self.node.chain=56;self.node.balances[SOURCE]=100
        with self.assertRaises(ValueError):sw.preview(self.wid,'swap')
    def test_usdt_route_three_steps_and_preserves_existing_middle_tokens(self):
        self.node.tokens[SOURCE]=5*10**18;self.node.tokens[MIDDLE]=2*10**18
        r=self.ready('usdt');p=json.loads(r['plan']);self.assertEqual(len(p['steps']),3)
        for i in range(3):
            r=self.row(r['id'])
            if not r['raw']:sw.tick();r=self.row(r['id'])
            self.assertEqual(r['step'],i)
            tx=Transaction.from_bytes(bytes.fromhex(r['raw'][2:])).as_dict()
            self.assertEqual(Account.recover_transaction(r['raw']).lower(),SOURCE if i<2 else MIDDLE)
            sw.tick()
            self.node.mine(r,token_amount=5*10**18 if i else None,recipient=MIDDLE if i==1 else TARGET)
            sw.tick()
        self.assertEqual(self.row(r['id'])['state'],'done');self.assertEqual(self.node.tokens[MIDDLE],2*10**18)
        self.assertEqual(self.node.tokens[TARGET],5*10**18);self.assertEqual(len(self.node.sent),3)
    def test_funded_middle_skips_topup(self):
        self.node.balances[MIDDLE]=10**16;self.node.tokens[SOURCE]=10**18
        p=sw.preview(self.wid,'usdt',TARGET);plan=json.loads(self.row(p['id'])['plan'])
        self.assertEqual(plan['topup'],'0');self.assertEqual(len(plan['steps']),2)
    def test_bnb_sweep_does_not_remove_gas_for_usdt(self):
        self.node.tokens[SOURCE]=1
        with self.assertRaisesRegex(ValueError,'USDT'):wd.preview(self.wid)
        self.node.tokens[SOURCE]=0;self.node.tokens[MIDDLE]=1
        with self.assertRaisesRegex(ValueError,'USDT'):wd.preview(self.wid)
    def test_no_cancel_after_ambiguous_send(self):
        r=self.ready();self.node.fail=True;sw.tick()
        with app.db() as c:c.execute("UPDATE swaps SET state='blocked' WHERE id=?",(r['id'],))
        self.assertFalse(sw.public(self.row(r['id']))['can_cancel'])
    def test_unknown_nonce_never_resigns(self):
        r=self.ready();self.node.nonces[SOURCE]+=1;sw.tick();sw.tick()
        self.assertEqual(self.node.sent,[]);self.assertEqual(self.row(r['id'])['raw'],r['raw'])
        self.assertEqual(self.row(r['id'])['state'],'running')
    def test_account_two_cannot_be_swapped(self):
        with app.db() as c:c.execute('UPDATE wallets SET address=? WHERE id=?',(MIDDLE,self.wid))
        with self.assertRaisesRegex(ValueError,'аккаунт 1'):sw.preview(self.wid,'swap')
    def test_reserve_decrease_before_send_blocks(self):
        r=self.ready();self.node.balances[SOURCE]-=10**16;sw.tick()
        self.assertEqual(self.row(r['id'])['state'],'blocked');self.assertEqual(self.node.sent,[])
    def test_token_calldata_only_transfers_agreed_amount_to_agreed_accounts(self):
        self.node.tokens[SOURCE]=10**18
        p=sw.preview(self.wid,'usdt',TARGET);plan=json.loads(self.row(p['id'])['plan'])
        for step,target in zip(plan['steps'][-2:],(MIDDLE,TARGET)):
            self.assertEqual(step['to'],sw.USDT)
            self.assertTrue(step['data'].startswith('0xa9059cbb'))
            address,amount=decode(['address','uint256'],bytes.fromhex(step['data'][10:]))
            self.assertEqual(address,target);self.assertEqual(amount,10**18)
    def test_expired_before_first_send_blocks_without_attempt(self):
        r=self.ready()
        with patch('swaps.time.time',return_value=time.time()+400):sw.tick()
        r=self.row(r['id']);self.assertEqual(r['state'],'blocked')
        self.assertTrue(sw.public(r)['can_cancel']);self.assertEqual(self.node.sent,[])
    def test_price_increase_pauses_next_step_instead_of_using_reserve(self):
        self.node.tokens[SOURCE]=10**18
        r=self.ready('usdt');sw.tick();self.node.mine(r);sw.tick()
        self.node.price*=2;sw.tick()
        r=self.row(r['id']);self.assertEqual(r['state'],'blocked')
        self.assertFalse(sw.public(r)['can_cancel']);self.assertEqual(len(self.node.sent),1)
    def test_bnb_confirmation_cannot_race_with_swap(self):
        p=wd.preview(self.wid);self.ready()
        with self.assertRaises(ValueError):wd.confirm(p['id'])
    def test_existing_bnb_operation_blocks_swap(self):
        p=wd.preview(self.wid);wd.confirm(p['id'])
        with self.assertRaises(ValueError):sw.preview(self.wid,'swap')
    def test_no_intermediate_transfer_log_stops_next_leg(self):
        self.node.tokens[SOURCE]=10**18;self.node.balances[MIDDLE]=10**16
        r=self.ready('usdt');sw.tick();self.node.mine(r);sw.tick();sw.tick()
        self.assertEqual(self.row(r['id'])['state'],'blocked');self.assertEqual(len(self.node.sent),1)

if __name__=='__main__':unittest.main()
