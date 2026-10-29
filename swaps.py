"""BSC-only PancakeSwap V2 exchange and confirmed USDT transfers.

No external calldata is accepted. Signed bytes are durable before broadcast;
unknown outcomes keep the global wallet-operation slot reserved.
"""
import json
import secrets
import time
from eth_abi import encode, decode
from eth_utils import keccak, to_checksum_address
import withdrawals as wd

ROUTER = '0x10ed43c718714eb63d5aa57b78b54704e256024e'
FACTORY = '0xca143ce32fe78f1f7019d7d551a6402fc5350c73'
WBNB = '0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c'
USDT = '0x55d398326f99059ff775485246999027b3197955'
PATH = [WBNB, USDT]
TOKEN_GAS = 150000  # conservative future budget, re-estimated when funded
SWAP_GAS = 400000
SLIPPAGE_BPS = 50
ACTIVE = "('running','blocked')"


def schema(c):
    c.execute('''CREATE TABLE IF NOT EXISTS swaps (
      id TEXT PRIMARY KEY, wallet_id INTEGER NOT NULL, kind TEXT NOT NULL,
      state TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
      plan TEXT NOT NULL, step INTEGER NOT NULL DEFAULT 0,
      raw TEXT, hash TEXT, attempted INTEGER NOT NULL DEFAULT 0,
      hashes TEXT NOT NULL DEFAULT '[]', error TEXT, received TEXT)''')
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS swap_active ON swaps((1)) WHERE state IN ('running','blocked')")


def active(c):
    return c.execute(f'SELECT 1 FROM swaps WHERE state IN {ACTIVE}').fetchone() is not None


def assert_idle(c):
    if active(c) or c.execute("SELECT 1 FROM withdrawals WHERE state IN ('pending1','pending2','blocked')").fetchone():
        raise ValueError('Сначала завершите текущий обмен или перевод. Проверьте журнал операций.')


def calldata(signature, types=(), values=()):
    return '0x' + (keccak(text=signature)[:4] + encode(types, values)).hex()


def read(call, target, signature, types=(), values=(), outputs=('uint256',)):
    value = call('eth_call', [{'to': target, 'data': calldata(signature, types, values)}, 'latest'])
    try:
        return decode(outputs, bytes.fromhex(value[2:]))
    except Exception:
        from app import RemoteError
        raise RemoteError('Контракт вернул некорректный ответ.') from None


def token_balance(call, address):
    return read(call, USDT, 'balanceOf(address)', ['address'], [address])[0]


def verify_contracts(call, swap=False):
    if call('eth_getCode', [USDT, 'latest']) == '0x' or read(call, USDT, 'decimals()', outputs=['uint8'])[0] != 18:
        raise ValueError('Не удалось проверить контракт USDT в BSC.')
    if swap:
        if call('eth_getCode', [ROUTER, 'latest']) == '0x':
            raise ValueError('Контракт PancakeSwap недоступен.')
        for method, expected in [('WETH()', WBNB), ('factory()', FACTORY)]:
            if read(call, ROUTER, method, outputs=['address'])[0].lower() != expected:
                raise ValueError('Проверка контракта PancakeSwap не пройдена.')


def gas_price(call):
    price = int(call('eth_gasPrice', []), 16)
    if not 0 < price <= wd.MAX_GAS_PRICE:
        raise ValueError('Цена газа вне разрешённого диапазона (до 3 Gwei).')
    return price


def reserve(price, middle_balance):
    """50% price cushion, fixed upper gas budgets for both future token sends."""
    buffered = (price * 3 + 1) // 2
    middle_need = TOKEN_GAS * buffered
    topup = max(0, middle_need - middle_balance)
    source_need = TOKEN_GAS * buffered + topup + (31500 * buffered if topup else 0)
    return source_need, middle_need, topup


def amounts_out(call, value):
    amounts = read(call, ROUTER, 'getAmountsOut(uint256,address[])',
                   ['uint256', 'address[]'], [value, PATH], ['uint256[]'])[0]
    if len(amounts) != 2 or amounts[0] != value or amounts[1] <= 0:
        raise ValueError('Нет корректной котировки BNB → USDT.')
    return amounts[1]


def swap_data(minimum, source, deadline):
    return calldata('swapExactETHForTokens(uint256,address[],address,uint256)',
                    ['uint256', 'address[]', 'address', 'uint256'],
                    [minimum, PATH, source, deadline])


def transfer_data(target, amount):
    return calldata('transfer(address,uint256)', ['address', 'uint256'], [target, amount])


def estimate(call, tx, cap):
    payload = {k: (hex(v) if isinstance(v, int) else v) for k,v in tx.items() if k != 'nonce'}
    used = int(call('eth_estimateGas', [payload]), 16)
    limit = (used * 3 + 1) // 2
    if used < 21000 or limit > cap:
        raise ValueError('Оценка газа превышает резерв. Операция остановлена.')
    return limit


def build_plan(cfg, source, middle, call, kind, target=''):
    if source == middle:
        raise ValueError('Обмен и этот маршрут USDT доступны только на аккаунте 1.')
    balance, nonce = wd.idle_account(call, source)
    middle_balance, middle_nonce = wd.idle_account(call, middle)
    verify_contracts(call, swap=kind == 'swap')
    price = gas_price(call)
    retained, middle_need, topup = reserve(price, middle_balance)
    p = dict(source=source, middle=middle, nonce=nonce, middle_nonce=middle_nonce,
             balance=str(balance), middle_balance=str(middle_balance), gas_price=price,
             reserve=str(retained), middle_reserve=str(middle_need), topup=str(topup),
             source_config={k:cfg[k] for k in ('sheets_enabled','sheets_id','sheets_tab')})
    if kind == 'swap':
        # Start with an upper gas budget, then quote using the simulated limit.
        limit = SWAP_GAS
        deadline = int(time.time()) + 300
        for _ in range(3):
            amount = balance - retained - limit * price
            if amount <= 0:
                raise ValueError('BNB недостаточно для обмена и резерва двух переводов USDT.')
            output = amounts_out(call, amount)
            minimum = output * (10000-SLIPPAGE_BPS) // 10000
            if minimum <= 0:
                raise ValueError('Сумма слишком мала для защищённого обмена.')
            data = swap_data(minimum, source, deadline)
            tx = {'from': source, 'to': ROUTER, 'value': amount, 'data': data, 'gasPrice': price}
            new_limit = estimate(call, tx, SWAP_GAS)
            if new_limit <= limit and limit != SWAP_GAS:
                break
            limit = new_limit
        else:
            raise ValueError('Оценка газа меняется. Повторите расчёт.')
        small = max(1, amount // 1000)
        reference = amounts_out(call, small)
        if output * small * 10000 < reference * amount * 9800:
            raise ValueError('Влияние на цену больше 2%. Обмен остановлен.')
        p.update(amount=str(amount), expected=str(output), minimum=str(minimum),
                 deadline=deadline, gas=limit, data=data, fee=str(limit*price),
                 retained=str(balance-amount-limit*price), destination=source)
    elif kind == 'usdt':
        target = wd.destination(target)
        if target in (source, middle) or call('eth_getCode', [target, 'latest']) != '0x':
            raise ValueError('Укажите отдельный депозитный адрес Binance USDT в сети BSC без кода контракта.')
        amount = token_balance(call, source)
        if amount <= 0:
            raise ValueError('На аккаунте 1 нет USDT.')
        if balance < retained:
            raise ValueError('Не хватает BNB на аккаунте 1 для резерва всего маршрута USDT.')
        # No auto-sale of tokens to buy gas and no sweep of pre-existing account-2 USDT.
        steps = []
        if topup:
            steps.append(dict(source=source, to=middle, value=str(topup), data='0x', nonce=nonce, cap=31500))
        steps.append(dict(source=source, to=USDT, value='0', data=transfer_data(middle, amount),
                          nonce=nonce+len(steps), cap=TOKEN_GAS))
        steps.append(dict(source=middle, to=USDT, value='0', data=transfer_data(target, amount),
                          nonce=middle_nonce, cap=TOKEN_GAS))
        p.update(amount=str(amount), destination=target, steps=steps)
    else:
        raise ValueError('Неизвестная операция.')
    return p


def public(row):
    from app import bnb
    p = json.loads(row['plan'])
    result = {k:row[k] for k in ('id','wallet_id','kind','state','created','updated','step','hash','error')}
    result.update({k:p[k] for k in ('source','middle','destination')})
    for key in ('amount','expected','minimum','fee','reserve','middle_reserve','topup','retained'):
        if key in p: result[key] = bnb(p[key])
    result['hashes'] = json.loads(row['hashes'])
    result['received'] = bnb(row['received']) if row['received'] is not None else None
    result['can_cancel'] = row['state']=='blocked' and row['step']==0 and not row['attempted']
    return result


def preview(wid, kind, target=''):
    from app import db, settings, RemoteError
    cfg = settings()
    with db() as c:
        assert_idle(c)
        wallet = c.execute('SELECT address FROM wallets WHERE id=?', (wid,)).fetchone()
    if not wallet: raise ValueError('Кошелёк не найден.')
    pair = wd.accounts_for(cfg, wallet['address'])
    source, middle = (a.address.lower() for a in pair)
    if source != wallet['address'].lower():
        raise ValueError('Выберите аккаунт 1 этого кошелька.')
    error = None
    for url in cfg['rpc_urls']:
        try:
            p = build_plan(cfg, source, middle, wd.node(url), kind, target)
            p['url'] = url
            break
        except RemoteError as exc: error = exc
    else: raise error or RemoteError('Нет доступного RPC.')
    now, ident = time.time(), secrets.token_hex(16)
    with db() as c:
        c.execute('INSERT INTO swaps(id,wallet_id,kind,state,created,updated,plan) VALUES (?,?,?,\'preview\',?,?,?)',
                  (ident,wid,kind,now,now,json.dumps(p)))
        return public(c.execute('SELECT * FROM swaps WHERE id=?',(ident,)).fetchone())


def checked_pair(cfg, p):
    if any(cfg[k]!=v for k,v in p['source_config'].items()):
        raise ValueError('Источник кошельков изменился. Операция приостановлена.')
    pair = wd.accounts_for(cfg,p['source'])
    if [a.address.lower() for a in pair] != [p['source'],p['middle']]:
        raise ValueError('Адреса аккаунтов изменились.')
    return pair


def signed_step(row, p, cfg, call):
    pair = checked_pair(cfg,p)
    if row['kind']=='swap':
        if time.time() > p['deadline']-30:
            raise ValueError('Котировка истекла. Отмените неподписанный обмен и рассчитайте заново.')
        if amounts_out(call,int(p['amount'])) < int(p['minimum']):
            raise ValueError('Курс вышел за допустимое проскальзывание 0.5%. Нужен новый расчёт.')
        s=dict(source=p['source'],to=ROUTER,value=p['amount'],data=p['data'],nonce=p['nonce'],cap=p['gas'])
        price=p['gas_price']
    else:
        s=p['steps'][row['step']]
        price=gas_price(call)
        if price > (p['gas_price']*3+1)//2:
            raise ValueError('Газ подорожал выше резерва. Дождитесь снижения и нажмите «Продолжить».')
        if s['to']==USDT and token_balance(call,s['source']) < int(p['amount']):
            raise ValueError('USDT на очередном аккаунте недостаточно для согласованного перевода.')
    balance, nonce=wd.idle_account(call,s['source'])
    if nonce!=s['nonce']:
        raise ValueError('Nonce изменился. Автоматическая переподпись запрещена.')
    if row['kind']=='swap' and balance!=int(p['balance']):
        raise ValueError('Баланс изменился. Рассчитайте обмен заново.')
    tx={'from':s['source'],'to':s['to'],'value':int(s['value']),'data':s['data'],'gasPrice':price}
    limit=estimate(call,tx,s['cap'])
    if row['kind']=='swap': limit=p['gas']
    if balance < int(s['value'])+limit*price:
        raise ValueError('BNB недостаточно для очередной транзакции.')
    account=pair[0] if s['source']==p['source'] else pair[1]
    tx.pop('from')
    tx.update(chainId=56,nonce=nonce,gas=limit,to=to_checksum_address(s['to']))
    signed=account.sign_transaction(tx)
    return '0x'+signed.raw_transaction.hex(), '0x'+signed.hash.hex()


def confirm(ident):
    from app import db, settings
    cfg=settings()
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        row=c.execute('SELECT * FROM swaps WHERE id=?',(ident,)).fetchone()
        if not row: raise ValueError('Расчёт не найден.')
        if row['state']!='preview': return public(row)
        assert_idle(c)
        if time.time()-row['created']>120: raise ValueError('Расчёт старше 2 минут. Рассчитайте заново.')
        p=json.loads(row['plan']); call=wd.node(p['url'])
        verify_contracts(call,row['kind']=='swap')
        # Both account nonces/balances must still match the approved reserve.
        for address,balance,nonce in [(p['source'],p['balance'],p['nonce']),(p['middle'],p['middle_balance'],p['middle_nonce'])]:
            if wd.idle_account(call,address)!=(int(balance),nonce):
                raise ValueError('Баланс или nonce изменился. Повторите расчёт.')
        if row['kind']=='usdt' and token_balance(call,p['source'])!=int(p['amount']):
            raise ValueError('Баланс USDT изменился. Повторите расчёт.')
        raw,txhash=signed_step(row,p,cfg,call)
        c.execute("UPDATE swaps SET state='running',raw=?,hash=?,updated=? WHERE id=?",(raw,txhash,time.time(),ident))
        return public(c.execute('SELECT * FROM swaps WHERE id=?',(ident,)).fetchone())


def update(c, ident, **values):
    values['updated']=time.time()
    c.execute('UPDATE swaps SET '+','.join(k+'=?' for k in values)+' WHERE id=?',(*values.values(),ident))


def received_usdt(receipt, recipient):
    topic='0x'+keccak(text='Transfer(address,address,uint256)').hex()
    total=0
    for log in receipt.get('logs',[]):
        topics=log.get('topics',[])
        if log.get('address','').lower()==USDT and len(topics)==3 and topics[0].lower()==topic and ('0x'+topics[2][-40:]).lower()==recipient:
            total+=int(log['data'],16)
    return total


def tick():
    from app import db, settings
    from operations import queue_balance
    with db() as c:
        row=c.execute("SELECT * FROM swaps WHERE state='running' LIMIT 1").fetchone()
    if not row: return
    ident=row['id']; p=json.loads(row['plan'])
    try:
        call=wd.node(p['url'])
        if not row['raw']:
            with db() as c:
                c.execute('BEGIN IMMEDIATE')
                fresh=c.execute('SELECT * FROM swaps WHERE id=?',(ident,)).fetchone()
                if fresh['state']!='running' or fresh['raw']: return
                raw,txhash=signed_step(fresh,p,settings(),call)
                update(c,ident,raw=raw,hash=txhash,attempted=0,error=None)
            return
        call,receipt,_=wd.receipt_node(p,row['hash'])
        if receipt=='waiting': return
        if receipt is not None:
            with db() as c:
                c.execute('BEGIN IMMEDIATE')
                fresh=c.execute('SELECT * FROM swaps WHERE id=?',(ident,)).fetchone()
                if fresh['state']!='running' or fresh['hash']!=row['hash']: return
                hashes=json.loads(row['hashes'])+[row['hash']]
                height=int(receipt['blockNumber'],16)
                for address in (p['source'],p['middle']): queue_balance(c,address,height)
                if int(receipt['status'],16)!=1:
                    update(c,ident,state='failed',hashes=json.dumps(hashes),error='Транзакция отклонена сетью; газ потрачен. Новая отправка отключена. Если предыдущие шаги завершены, средства остаются на аккаунте 2 — проверьте журнал.')
                    return
                last=row['kind']=='swap' or row['step']==len(p['steps'])-1
                token_step = row['kind']=='swap' or p['steps'][row['step']]['to']==USDT
                recipient = p['destination'] if last else p['middle']
                received=received_usdt(receipt,recipient) if token_step else None
                # Successful receipt alone does not prove the expected token transfer.
                if token_step and received < int(p['minimum'] if row['kind']=='swap' else p['amount']):
                    update(c,ident,state='blocked',error='Квитанция успешна, но получение USDT не подтверждено. Проверьте хеш; новая отправка запрещена.')
                    return
                update(c,ident,state='done' if last else 'running',hashes=json.dumps(hashes),
                       step=row['step']+1,raw=None,hash=None,attempted=0,error=None,received=str(received) if last else None)
            return
        s=({'source':p['source'],'nonce':p['nonce']} if row['kind']=='swap' else p['steps'][row['step']])
        latest=int(call('eth_getTransactionCount',[s['source'],'latest']),16)
        if latest!=s['nonce']:
            # Do not release slot or sign another nonce, even after timeout/deadline.
            raise RuntimeError('nonce unresolved')
        with db() as c:
            c.execute('BEGIN IMMEDIATE')
            fresh=c.execute('SELECT * FROM swaps WHERE id=?',(ident,)).fetchone()
            if fresh['state']!='running' or fresh['hash']!=row['hash']: return
            if not fresh['attempted']:
                if row['kind']=='swap' and time.time()>p['deadline']-15:
                    update(c,ident,state='blocked',error='Котировка истекла до отправки. Можно отменить и рассчитать заново.')
                    return
                # Ensure no external transaction appeared after signing.
                balance, nonce=wd.idle_account(call,s['source'])
                if row['kind']=='swap' and balance<int(p['amount'])+p['gas']*p['gas_price']+int(p['reserve']):
                    raise ValueError('Баланс уменьшился: резерв газа не обеспечен. Отправка остановлена.')
                if nonce!=s['nonce']: raise ValueError('Nonce изменился перед отправкой.')
            update(c,ident,attempted=1)
        returned=call('eth_sendRawTransaction',[row['raw']])
        if not isinstance(returned,str) or returned.lower()!=row['hash']: raise RuntimeError('hash mismatch')
        with db() as c: update(c,ident,error=None)
    except ValueError as exc:
        with db() as c: update(c,ident,state='blocked',error=str(exc))
    except Exception:
        with db() as c: update(c,ident,error='Нет достоверного ответа RPC. Проверяем сохранённый хеш; новую транзакцию не создаём.')


def run(stop):
    while not stop.is_set():
        try: tick()
        except Exception: pass
        stop.wait(10)


def install(app):
    from app import db
    from flask import request
    @app.post('/api/wallets/<int:wid>/swap/preview')
    def swap_preview(wid): return preview(wid,'swap')
    @app.post('/api/wallets/<int:wid>/usdt-transfer/preview')
    def usdt_preview(wid): return preview(wid,'usdt',(request.get_json() or {}).get('destination',''))
    @app.post('/api/swaps/<ident>/confirm')
    def swap_confirm(ident): return confirm(ident)
    @app.get('/api/swaps')
    def swap_list():
        with db() as c:
            rows=c.execute("SELECT * FROM swaps WHERE state!='preview' ORDER BY created DESC LIMIT 50").fetchall()
        return {'items':[public(r) for r in rows]}
    @app.post('/api/swaps/<ident>/resume')
    def swap_resume(ident):
        with db() as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute('SELECT * FROM swaps WHERE id=?',(ident,)).fetchone()
            if not row or row['state']!='blocked': raise ValueError('Операция не приостановлена.')
            # Preserve every signed byte/hash/nonce. Never create a replacement.
            update(c,ident,state='running',error=None)
        return {'ok':True}
    @app.post('/api/swaps/<ident>/cancel')
    def swap_cancel(ident):
        with db() as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute('SELECT * FROM swaps WHERE id=?',(ident,)).fetchone()
            if not row or row['state']!='blocked' or row['attempted'] or row['step']!=0:
                raise ValueError('Отмена недоступна: транзакция могла быть отправлена.')
            update(c,ident,state='cancelled',raw=None,hash=None,error=None)
        return {'ok':True}
