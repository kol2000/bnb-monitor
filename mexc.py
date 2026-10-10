"""MEXC spot: durable, single-dispatch orders. Secrets stay in /data."""
import fcntl
from functools import wraps
import hashlib
import hmac
import json
import re
import time
import uuid
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from urllib.parse import urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError

BASE = 'https://api.mexc.com'
ACTIVE = ('buying', 'bought', 'withdrawing')
MAX_RESPONSE_BYTES = 32 * 1024 * 1024

class MexcError(ValueError):
    pass

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None

def dec(value):
    try:
        n = Decimal(str(value))
        if not n.is_finite() or n < 0 or n > Decimal('1e18'):
            raise ValueError()
        return n
    except (InvalidOperation, ValueError):
        raise MexcError('Неверное числовое значение MEXC.') from None

def fmt(n):
    return format(n, 'f')

class Client:
    def __init__(self):
        from app import DATA
        try:
            p = DATA / 'mexc-api.json'
            if p.stat().st_mode & 0o077:
                raise ValueError()
            keys = json.loads(p.read_text())
            self.key, self.secret = keys['api_key'], keys['api_secret']
            if not self.key or not self.secret:
                raise ValueError()
        except Exception:
            raise MexcError('Проверьте /data/mexc-api.json: ключи и права 600.') from None

    def call(self, path, params=None, method='GET', signed=True):
        params = dict(params or {})
        headers = {'Content-Type': 'application/json', 'Accept': 'application/json'}
        if signed:
            params.update(timestamp=int(time.time()*1000), recvWindow=10000)
            headers['X-MEXC-APIKEY'] = self.key
        query = urlencode(params)
        if signed:
            query += '&signature=' + hmac.new(self.secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        req = Request(BASE + path + '?' + query, method=method, headers=headers,
                      data=b'' if method == 'POST' else None)
        try:
            with build_opener(NoRedirect()).open(req, timeout=15) as res:
                raw = res.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise MexcError('Ответ MEXC превышает лимит 32 МиБ. Данные не обработаны.')
                try:
                    data = json.loads(raw)
                except (ValueError, UnicodeError):
                    raise MexcError('MEXC вернул неполный или некорректный JSON (%d байт).' % len(raw)) from None
            if isinstance(data, dict) and data.get('code') not in (None, 0, 200):
                code = data.get('code')
                raise MexcError('MEXC отклонил запрос (код %s).' % (code if isinstance(code, int) else 'API'))
            return data
        except MexcError:
            raise
        except HTTPError as e:
            try:
                error_body = json.loads(e.read(65536))
                code = error_body.get('code') if isinstance(error_body, dict) else None
            except Exception:
                code = None
            detail = ' · код MEXC %s' % code if isinstance(code, int) else ''
            if code == 700013:
                detail += ' · неверный Content-Type'
            raise MexcError('MEXC HTTP %s%s. Запрос автоматически не повторяется.' % (e.code, detail)) from None
        except Exception:
            raise MexcError('Нет достоверного ответа MEXC. Операция автоматически не повторяется.') from None

def schema(c):
    c.execute('''CREATE TABLE IF NOT EXISTS mexc_jobs (
        id TEXT PRIMARY KEY, created REAL NOT NULL, state TEXT NOT NULL,
        data TEXT NOT NULL, error TEXT NOT NULL DEFAULT '')''')
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS mexc_one_active ON mexc_jobs((1)) WHERE state IN ('buying','bought','withdrawing')")

def get_job(ident):
    from app import db
    with db() as c:
        r = c.execute('SELECT * FROM mexc_jobs WHERE id=?', (ident,)).fetchone()
    if not r:
        raise MexcError('Операция не найдена.')
    return dict(r) | {'data': json.loads(r['data'])}

def save(ident, state, data, error=''):
    from app import db
    with db() as c:
        c.execute('UPDATE mexc_jobs SET state=?,data=?,error=? WHERE id=?',
                  (state, json.dumps(data), error, ident))

def networks(client):
    coins = client.call('/api/v3/capital/config/getall')
    if not isinstance(coins, list):
        raise MexcError('MEXC не вернул список сетей.')
    for coin in coins:
        if coin.get('coin') == 'BNB':
            return [n for n in coin.get('networkList', []) if n.get('netWork')]
    return []

def network(client, name):
    ns = [n for n in networks(client) if n['netWork'] == name and n.get('withdrawEnable') is True]
    if len(ns) != 1:
        raise MexcError('Вывод BNB в выбранной сети недоступен.')
    return ns[0]

def account(client):
    a = client.call('/api/v3/account')
    if not isinstance(a, dict) or not isinstance(a.get('balances'), list):
        raise MexcError('MEXC не вернул баланс аккаунта.')
    return a

def free(a, asset):
    return next((dec(b['free']) for b in a['balances'] if b['asset'] == asset), Decimal(0))

def history(client, start=None):
    end = int(time.time()*1000)
    begin = max(0, end-7*86400000+1000) if start is None else int(start*1000)-60000
    end = min(end, begin+7*86400000-1000)
    rows = client.call('/api/v3/capital/withdraw/history', {'coin':'BNB','startTime':begin,'endTime':end,'limit':1000})
    if not isinstance(rows, list):
        raise MexcError('MEXC не вернул историю выводов.')
    return rows

def buy_preview(body):
    from app import db
    amount = dec(body.get('amount'))
    if amount <= 0 or amount.as_tuple().exponent < -2:
        raise MexcError('Введите сумму USDT больше нуля, максимум 2 знака после запятой.')
    address = str(body.get('address', '')).strip()
    # EVM BNB destinations only; no silent interpretation of other address formats.
    if not re.fullmatch(r'0x[0-9a-fA-F]{40}', address) or int(address[2:],16) == 0:
        raise MexcError('Введите полный EVM-адрес получателя BNB (0x…).')
    name = str(body.get('network',''))
    memo = str(body.get('memo','')).strip()
    if len(memo) > 128:
        raise MexcError('Слишком длинный memo.')
    client = Client()
    net = network(client, name)
    a = account(client)
    total = amount + Decimal('0.10')
    if a.get('canTrade') is not True or a.get('canWithdraw') is not True:
        raise MexcError('Аккаунту недоступны торговля или вывод.')
    allowed = client.call('/api/v3/selfSymbols')
    if 'BNBUSDT' not in allowed.get('data', []):
        raise MexcError('Торговля BNBUSDT через API недоступна для этого ключа.')
    info = client.call('/api/v3/exchangeInfo', {'symbol':'BNBUSDT'}, signed=False)
    symbols = info.get('symbols', [info] if info.get('symbol') == 'BNBUSDT' else [])
    if len(symbols) != 1 or 'MARKET' not in symbols[0].get('orderTypes', []):
        raise MexcError('MARKET для BNBUSDT недоступен.')
    if free(a,'USDT') < total:
        raise MexcError('Недостаточно свободных USDT для суммы с добавкой 0,10.')
    # Server validates all additional venue filters atomically on order placement.
    s = symbols[0]
    if s.get('quoteOrderQtyMarketAllowed') is False or s.get('isSpotTradingAllowed') is False or str(s.get('tradeSideType','1')) not in ('1','2'):
        raise MexcError('Покупка BNB по рынку сейчас недоступна.')
    maximum = dec(s.get('maxQuoteAmountMarket') or s.get('maxQuoteAmount') or '0')
    if maximum and total > maximum:
        raise MexcError('Сумма превышает лимит рыночного ордера.')
    minimum = dec(s.get('quoteAmountPrecisionMarket') or s.get('quoteAmountPrecision') or '0')
    for f in s.get('filters', []):
        if f.get('filterType') in ('MIN_NOTIONAL','NOTIONAL'):
            minimum = max(minimum, dec(f.get('minNotional','0')))
    if total < minimum:
        raise MexcError('Минимальная покупка: %s USDT.' % fmt(minimum))
    price = dec(client.call('/api/v3/ticker/price', {'symbol':'BNBUSDT'}, signed=False)['price'])
    if not price:
        raise MexcError('Нет цены BNB.')
    fee = dec(net['withdrawFee'])
    if total/price <= dec(net['withdrawMin']) + fee:
        raise MexcError('Ориентировочного BNB недостаточно для минимума вывода и комиссии.')
    ident = uuid.uuid4().hex
    data = dict(amount=fmt(amount), total=fmt(total), address=address, network=name, memo=memo,
                estimate=fmt(total/price), fee=fmt(fee), minimum=str(net['withdrawMin']))
    with db() as c:
        c.execute('INSERT INTO mexc_jobs(id,created,state,data) VALUES (?,?,?,?)',
                  (ident,time.time(),'preview',json.dumps(data)))
    return get_job(ident)

def buy_confirm(ident):
    from app import db
    j = get_job(ident)
    if j['state'] != 'preview' or time.time()-j['created'] > 180:
        raise MexcError('Расчёт истёк или уже подтверждён. Обновите список операций.')
    d = j['data']
    client = Client()
    # Recheck available balance before reserving the single dispatch.
    if free(account(client),'USDT') < dec(d['total']):
        raise MexcError('Недостаточно свободных USDT.')
    network(client, d['network'])
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        if c.execute("SELECT 1 FROM mexc_jobs WHERE state IN ('buying','bought','withdrawing')").fetchone():
            raise MexcError('Сначала завершите текущую операцию MEXC.')
        if not c.execute("UPDATE mexc_jobs SET state='buying' WHERE id=? AND state='preview'",(ident,)).rowcount:
            raise MexcError('Покупка уже подтверждена.')
    try:
        result = client.call('/api/v3/order', {'symbol':'BNBUSDT','side':'BUY','type':'MARKET',
                    'quoteOrderQty':d['total'],'newClientOrderId':ident}, method='POST')
        if not result.get('orderId'):
            raise MexcError('Нет ID ордера. Нужна сверка с MEXC.')
        d['order_id'] = str(result['orderId'])
        save(ident,'buying',d)
    except MexcError as e:
        save(ident,'buying',d,str(e))
    return get_job(ident)

def withdrawal_asset_matches(row, expected_network):
    # MEXC history can name native BNB on BSC as BNB-BSC.
    # Accept only the observed alias for the selected chain, not arbitrary suffixes.
    if row.get('coin') == 'BNB':
        return True
    return (expected_network == 'BSC' and row.get('coin') == 'BNB-BSC'
            and row.get('network') in ('BSC', 'BNB Smart Chain(BEP20)'))

def reconcile(ident):
    j = get_job(ident)
    d, state = j['data'], j['state']
    if state not in ('buying','withdrawing'):
        return j
    client = Client()
    try:
        if state == 'buying':
            order = client.call('/api/v3/order', {'symbol':'BNBUSDT','origClientOrderId':ident})
            if order.get('clientOrderId') != ident or order.get('side') != 'BUY' or order.get('symbol') != 'BNBUSDT':
                raise MexcError('Ордер не соответствует запросу. Требуется ручная сверка.')
            d['order_id'] = str(order['orderId'])
            d['order_status'] = order['status']
            if order['status'] not in ('FILLED','CANCELED','PARTIALLY_CANCELED','EXPIRED','REJECTED'):
                save(ident,state,d)
                return get_job(ident)
            qty = dec(order['executedQty'])
            if qty == 0:
                save(ident,'closed',d,'Ордер завершён без покупки.')
                return get_job(ident)
            trades = client.call('/api/v3/myTrades', {'symbol':'BNBUSDT','orderId':d['order_id'],'limit':100})
            if not isinstance(trades,list) or any(str(t.get('orderId')) != d['order_id'] for t in trades):
                raise MexcError('Неверный список сделок ордера.')
            if sum((dec(t['qty']) for t in trades),Decimal(0)) != qty:
                raise MexcError('Получены не все исполнения ордера. Вывод пока заблокирован.')
            if len({str(t['id']) for t in trades}) != len(trades):
                raise MexcError('Повторяющиеся исполнения. Нужна сверка.')
            fee = sum((dec(t['commission']) for t in trades if t['commissionAsset']=='BNB'),Decimal(0))
            if fee >= qty:
                raise MexcError('Неверная комиссия покупки.')
            d.update(bought=fmt(qty-fee), spent=str(order['cummulativeQuoteQty']), bnb_trade_fee=fmt(fee))
            save(ident,'bought',d)
        else:
            found = [r for r in history(client,d['withdraw_time']) if
                     r.get('withdrawOrderId') == ident or (d.get('withdraw_id') and str(r.get('id')) == d['withdraw_id'])]
            if len(found) != 1:
                raise MexcError('Вывод ещё не найден в истории. Повторная отправка заблокирована; проверьте MEXC.')
            r = found[0]
            if str(r.get('address','')).lower() != d['address'].lower() or not withdrawal_asset_matches(r, d['network']):
                raise MexcError('Данные вывода не совпали. Нужна ручная сверка.')
            d.update(withdraw_id=str(r['id']),withdraw_status=r['status'],txid=r.get('txId') or r.get('transHash') or '')
            state = 'done' if int(r['status']) == 7 else 'closed' if int(r['status']) in (8,9) else state
            save(ident,state,d,'Вывод отклонён/отменён на MEXC.' if state=='closed' else '')
    except MexcError as e:
        save(ident,state,d,str(e))
    return get_job(ident)

def withdrawal_step(value):
    # MEXC returns decimal-place counts (e.g. "18"), not 18 whole BNB.
    # Also accept explicit fractional increments for compatible API responses.
    if value is None or isinstance(value, bool):
        raise MexcError('MEXC не сообщил точность вывода.')
    text = str(value).strip()
    if re.fullmatch(r'\d+', text):
        digits = int(text)
        if digits > 18:
            raise MexcError('Неподдерживаемая точность вывода MEXC.')
        return Decimal(1).scaleb(-digits)
    step = dec(text)
    if not Decimal('1e-18') <= step < 1:
        raise MexcError('Неверная точность вывода MEXC.')
    return step

def withdrawal_plan(client,d):
    n = network(client,d['network'])
    a = account(client)
    if a.get('canWithdraw') is not True:
        raise MexcError('Вывод на аккаунте недоступен.')
    fee = dec(n['withdrawFee'])
    # Reserve fee in addition to amount: never spend pre-existing BNB.
    # Venue may deduct fee from amount; exact receipt is reported by MEXC history.
    step = withdrawal_step(n.get('withdrawIntegerMultiple'))
    if step <= 0:
        raise MexcError('Неизвестна точность вывода.')
    available = min(dec(d['bought']),free(a,'BNB'))
    amount = ((available-fee)/step).to_integral_value(rounding=ROUND_DOWN)*step
    if amount <= 0 or amount < dec(n['withdrawMin']) or (n.get('withdrawMax') and amount > dec(n['withdrawMax'])):
        raise MexcError('Купленного/свободного BNB недостаточно либо превышен лимит вывода.')
    return {'amount':fmt(amount),'fee':fmt(fee),'network':d['network'],'address':d['address'],'memo':d['memo']}

def withdraw_preview(ident):
    j = get_job(ident)
    if j['state'] != 'bought':
        raise MexcError('Покупка ещё не подтверждена биржей.')
    d = j['data']
    d['withdraw_plan'] = withdrawal_plan(Client(),d)
    d['plan_time'] = time.time()
    save(ident,'bought',d)
    return get_job(ident)

def withdraw_confirm(ident):
    from app import db
    j = get_job(ident)
    d = j['data']
    if j['state'] != 'bought' or time.time()-d.get('plan_time',0) > 180:
        raise MexcError('Нужен свежий расчёт вывода.')
    client = Client()
    plan = withdrawal_plan(client,d)
    if plan != d.get('withdraw_plan'):
        raise MexcError('Баланс или комиссия изменились. Пересчитайте вывод.')
    d['withdraw_time'] = time.time()
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        if not c.execute("UPDATE mexc_jobs SET state='withdrawing',data=?,error='' WHERE id=? AND state='bought'",
                         (json.dumps(d),ident)).rowcount:
            raise MexcError('Вывод уже подтверждён.')
    try:
        params = dict(coin='BNB',withdrawOrderId=ident,netWork=d['network'],address=d['address'],amount=plan['amount'])
        if d['memo']:
            params['memo']=d['memo']
        result = client.call('/api/v3/capital/withdraw',params,method='POST')
        if not result.get('id'):
            raise MexcError('Нет ID вывода. Нужна сверка с биржей.')
        d['withdraw_id']=str(result['id'])
        save(ident,'withdrawing',d)
    except MexcError as e:
        save(ident,'withdrawing',d,str(e))
    return get_job(ident)

def install(app):
    from app import db
    from flask import request
    @app.get('/api/mexc/jobs')
    def mexc_jobs():
        with db() as c:
            rows=c.execute("SELECT id FROM mexc_jobs WHERE state!='preview' ORDER BY created DESC LIMIT 50").fetchall()
        return {'items':[get_job(r['id']) for r in rows]}

    @app.post('/api/mexc/refresh')
    def mexc_refresh():
        client=Client()
        a=account(client)
        ns=networks(client)
        rows=history(client)
        return {'updated':time.time(),'balances':[b for b in a['balances'] if b['asset'] in ('BNB','USDT')],
                'networks':[{k:n.get(k) for k in ('netWork','name','withdrawEnable','withdrawFee','withdrawMin','withdrawTips')} for n in ns],
                'withdrawals':[{k:r.get(k) for k in ('id','amount','address','network','status','txId','applyTime','transactionFee')} for r in rows]}

    @app.post('/api/mexc/preview')
    def mexc_preview():
        return buy_preview(request.get_json() or {})

    @app.post('/api/mexc/<ident>/buy')
    def mexc_buy(ident):
        return buy_confirm(ident)

    @app.post('/api/mexc/<ident>/check')
    def mexc_check(ident):
        return reconcile(ident)

    @app.post('/api/mexc/<ident>/withdraw-preview')
    def mexc_withdraw_preview(ident):
        return withdraw_preview(ident)

    @app.post('/api/mexc/<ident>/withdraw')
    def mexc_withdraw(ident):
        return withdraw_confirm(ident)

    @app.post('/api/mexc/<ident>/keep')
    def mexc_keep(ident):
        # An executed purchase can be kept on exchange without withdrawing.
        with db() as c:
            if not c.execute("UPDATE mexc_jobs SET state='closed' WHERE id=? AND state='bought'",(ident,)).rowcount:
                raise MexcError('Операция не находится в состоянии куплено.')
        return {'ok':True}

    # Cross-process lock also covers reconciliation, preventing stale state writes
    # during a dispatch or a second browser session. No external request on GET.
    def serialized(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            from app import DATA
            with (DATA / 'mexc.lock').open('a') as handle:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise MexcError('Запрос MEXC уже выполняется. Подождите.') from None
                try:
                    return fn(*args, **kwargs)
                finally:
                    fcntl.flock(handle, fcntl.LOCK_UN)
        return wrapped
    for name, fn in list(app.view_functions.items()):
        if name.startswith('mexc_') and name != 'mexc_jobs':
            app.view_functions[name] = serialized(fn)
