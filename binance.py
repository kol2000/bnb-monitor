"""Binance spot: durable, single-dispatch orders. Secrets stay in /data."""
import fcntl
from functools import wraps
import hashlib
import hmac
import json
import re
import time
import uuid
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_UP
from urllib.parse import urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler
from urllib.error import HTTPError

BASE = 'https://api.binance.com'
ACTIVE = ('buying', 'bought', 'withdrawing')
MAX_RESPONSE_BYTES = 32 * 1024 * 1024

class BinanceError(ValueError):
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
        raise BinanceError('Неверное числовое значение Binance.') from None

def fmt(n):
    return format(n, 'f')

class Client:
    def __init__(self):
        from app import DATA
        try:
            p = DATA / 'binance-api.json'
            if p.stat().st_mode & 0o077:
                raise ValueError()
            keys = json.loads(p.read_text())
            self.key, self.secret = keys['api_key'], keys['api_secret']
            if not self.key or not self.secret:
                raise ValueError()
        except Exception:
            raise BinanceError('Проверьте /data/binance-api.json: ключи и права 600.') from None

    def call(self, path, params=None, method='GET', signed=True):
        params = dict(params or {})
        headers = {'Content-Type': 'application/x-www-form-urlencoded', 'Accept': 'application/json'}
        if signed:
            params.update(timestamp=int(time.time()*1000), recvWindow=10000)
            headers['X-MBX-APIKEY'] = self.key
        query = urlencode(params)
        if signed:
            query += '&signature=' + hmac.new(self.secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        req = Request(BASE + path + '?' + query, method=method, headers=headers,
                      data=b'' if method == 'POST' else None)
        try:
            with build_opener(ProxyHandler({}), NoRedirect()).open(req, timeout=15) as res:
                raw = res.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise BinanceError('Ответ Binance превышает лимит 32 МиБ. Данные не обработаны.')
                try:
                    data = json.loads(raw)
                except (ValueError, UnicodeError):
                    raise BinanceError('Binance вернул неполный или некорректный JSON (%d байт).' % len(raw)) from None
            if isinstance(data, dict) and data.get('code') not in (None, 0, 200):
                code = data.get('code')
                raise BinanceError('Binance отклонил запрос (код %s).' % (code if isinstance(code, int) else 'API'))
            return data
        except BinanceError:
            raise
        except HTTPError as e:
            try:
                error_body = json.loads(e.read(65536))
                code = error_body.get('code') if isinstance(error_body, dict) else None
            except Exception:
                code = None
            detail = ' · код Binance %s' % code if isinstance(code, int) else ''
            if code == 700013:
                detail += ' · неверный Content-Type'
            raise BinanceError('Binance HTTP %s%s. Запрос автоматически не повторяется.' % (e.code, detail)) from None
        except Exception:
            raise BinanceError('Нет достоверного ответа Binance. Операция автоматически не повторяется.') from None

def schema(c):
    c.execute('''CREATE TABLE IF NOT EXISTS binance_jobs (
        id TEXT PRIMARY KEY, created REAL NOT NULL, state TEXT NOT NULL,
        data TEXT NOT NULL, error TEXT NOT NULL DEFAULT '')''')
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS binance_one_active ON binance_jobs((1)) WHERE state IN ('buying','bought','withdrawing')")

def poll_once():
    """Read-only exchange reconciliation. Share the web dispatch lock."""
    from app import DATA, db, set_status
    with (DATA / 'binance.lock').open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        with db() as c:
            cached = c.execute("SELECT value FROM status WHERE key='binance_poll'").fetchone()
            schedule = json.loads(cached['value']) if cached else {}
            rows = c.execute("SELECT id,state FROM binance_jobs WHERE state IN ('buying','withdrawing')").fetchall()
        now = time.time()
        active = [[r['id'], r['state']] for r in rows]
        if now < schedule.get('next_at', 0) and active == schedule.get('active'):
            return
        failures = 0
        for row in rows:
            try:
                result = reconcile(row['id'])  # GET order / fills / history only
                failures += bool(result['error'])
            except Exception:
                failures += 1
        with db() as c:
            current = [tuple(r) for r in c.execute("SELECT id,state FROM binance_jobs WHERE state!='preview' ORDER BY created DESC LIMIT 50")]
        # Refresh after any state change, including a manually reconciled operation.
        current = [list(r) for r in current]
        if current and (current != schedule.get('balances_for') or now-schedule.get('balance_at',0)>=60):
            try:
                a = account(Client())
                set_status(binance_balances={'updated': time.time(), 'balances':
                    [b for b in a['balances'] if b['asset'] in ('BNB', 'USDT')]}, binance_balance_error=None)
                schedule['balances_for'] = current
                schedule['balance_at'] = time.time()
            except Exception:
                failures += 1
                set_status(binance_balance_error='Не удалось обновить баланс Binance. Повторим автоматически.')
        schedule['failures'] = min(schedule.get('failures', 0) + 1, 4) if failures else 0
        schedule['checked'] = now
        schedule['active'] = active
        schedule['next_at'] = time.time() + min(300, 20 * 2 ** schedule['failures'])
        set_status(binance_poll=schedule)


def run(stop):
    while not stop.is_set():
        try:
            poll_once()
        except Exception:
            pass  # No credentials or DB unavailable: never dispatch a replacement order.
        stop.wait(5)


def get_job(ident):
    from app import db
    with db() as c:
        r = c.execute('SELECT * FROM binance_jobs WHERE id=?', (ident,)).fetchone()
    if not r:
        raise BinanceError('Операция не найдена.')
    return dict(r) | {'data': json.loads(r['data'])}

def save(ident, state, data, error=''):
    from app import db
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        before = c.execute('SELECT state FROM binance_jobs WHERE id=?',(ident,)).fetchone()
        data['updated'] = time.time()
        if before and before['state'] != 'done' and state == 'done' and data.get('action') != 'sell':
            from operations import queue_balance
            queue_balance(c, data['address'])
        c.execute('UPDATE binance_jobs SET state=?,data=?,error=? WHERE id=?',
                  (state, json.dumps(data), error, ident))

def networks(client):
    coins = client.call('/sapi/v1/capital/config/getall')
    if not isinstance(coins, list):
        raise BinanceError('Binance не вернул список сетей.')
    for coin in coins:
        if coin.get('coin') == 'BNB':
            return [n for n in coin.get('networkList', []) if n.get('network') == 'BSC']
    return []

def network(client, name):
    if name != 'BSC':
        raise BinanceError('В этой версии вывод Binance поддерживает только сеть BSC.')
    ns = [n for n in networks(client) if n['network'] == name and n.get('withdrawEnable') is True and not n.get('busy') and not n.get('withdrawTag')]
    if len(ns) != 1:
        raise BinanceError('Вывод BNB в выбранной сети недоступен.')
    return ns[0]

def account(client):
    a = client.call('/api/v3/account')
    if not isinstance(a, dict) or not isinstance(a.get('balances'), list):
        raise BinanceError('Binance не вернул баланс аккаунта.')
    return a

def free(a, asset):
    return next((dec(b['free']) for b in a['balances'] if b['asset'] == asset), Decimal(0))

def history(client, start=None):
    end = int(time.time()*1000)
    begin = max(0, end-7*86400000+1000) if start is None else int(start*1000)-60000
    end = min(end, begin+7*86400000-1000)
    rows = client.call('/sapi/v1/capital/withdraw/history', {'coin':'BNB','startTime':begin,'endTime':end,'limit':1000})
    if not isinstance(rows, list):
        raise BinanceError('Binance не вернул историю выводов.')
    return rows

def buy_commission(client, amount, price, available_bnb):
    """Estimate net BNB acquisition using the account's current symbol commissions."""
    data = client.call('/api/v3/account/commission', {'symbol':'BNBUSDT'})
    try:
        if data['symbol'] != 'BNBUSDT':
            raise ValueError()
        rates = [dec(data[k]['taker']) + dec(data[k]['buyer'])
                 for k in ('standardCommission','taxCommission','specialCommission')]
        discount = data['discount']
        factor = dec(discount['discount'])
        if factor > 1 or any(type(discount[k]) is not bool for k in ('enabledForAccount','enabledForSymbol')):
            raise ValueError()
        full = sum(rates)
        if full >= 1:
            raise ValueError()
        # Conservatively require existing BNB to cover the undiscounted fee.
        use_discount = (discount['enabledForAccount'] and discount['enabledForSymbol']
                        and discount['discountAsset'] == 'BNB'
                        and available_bnb >= amount / (1-full) * full / price)
        rate = rates[0] * (factor if use_discount else 1) + rates[1] + rates[2]
        if rate >= 1:
            raise ValueError()
        return rate, bool(use_discount)
    except (KeyError, TypeError, ValueError):
        raise BinanceError('Не удалось достоверно рассчитать комиссию Binance. Покупка не выполнялась.') from None


def buy_preview(body):
    from app import db
    amount = dec(body.get('amount'))
    if amount <= 0 or amount.as_tuple().exponent < -2:
        raise BinanceError('Введите сумму USDT больше нуля, максимум 2 знака после запятой.')
    goal = body.get('goal','trade')
    if goal not in ('trade','wallet'):
        raise BinanceError('Выберите цель: после покупки или на кошельке.')
    address = str(body.get('address', '')).strip()
    # EVM BNB destinations only; no silent interpretation of other address formats.
    if not re.fullmatch(r'0x[0-9a-fA-F]{40}', address) or int(address[2:],16) == 0:
        raise BinanceError('Введите полный EVM-адрес получателя BNB (0x…).')
    name = str(body.get('network',''))
    memo = str(body.get('memo','')).strip()
    if len(memo) > 128:
        raise BinanceError('Слишком длинный memo.')
    client = Client()
    net = network(client, name)
    ensure_withdrawal_supported(client)
    if memo:
        raise BinanceError('Для BSC memo не используется. Очистите поле.')
    a = account(client)
    price = dec(client.call('/api/v3/ticker/price', {'symbol':'BNBUSDT'}, signed=False)['price'])
    if not price:
        raise BinanceError('Нет цены BNB.')
    fee = dec(net['withdrawFee'])
    step = withdrawal_step(net.get('withdrawIntegerMultiple'))
    target_bnb = (amount/price/step).to_integral_value(rounding=ROUND_UP)*step
    # Match the conservative withdrawal plan: reserve one fee on exchange,
    # allow another if the venue deducts its fee from the submitted amount.
    commission_base = (target_bnb + 2*fee)*price if goal == 'wallet' else amount
    rate, discounted = buy_commission(client, commission_base, price, free(a,'BNB'))
    if a.get('canTrade') is not True or a.get('canWithdraw') is not True:
        raise BinanceError('Аккаунту недоступны торговля или вывод.')
    info = client.call('/api/v3/exchangeInfo', {'symbol':'BNBUSDT'}, signed=False)
    symbols = info.get('symbols', [info] if info.get('symbol') == 'BNBUSDT' else [])
    if len(symbols) != 1 or 'MARKET' not in symbols[0].get('orderTypes', []):
        raise BinanceError('MARKET для BNBUSDT недоступен.')
    precision = symbols[0].get('quoteAssetPrecision', 8)
    if type(precision) is not int or not 0 <= precision <= 18:
        raise BinanceError('Некорректная точность USDT.')
    total = (commission_base / (1-rate)).quantize(Decimal(1).scaleb(-precision), rounding=ROUND_UP)
    if free(a,'USDT') < total:
        raise BinanceError('Недостаточно свободных USDT для суммы с учётом комиссии.')
    # Server validates all additional venue filters atomically on order placement.
    s = symbols[0]
    if s.get('quoteOrderQtyMarketAllowed') is False or s.get('isSpotTradingAllowed') is False or s.get('status') != 'TRADING':
        raise BinanceError('Покупка BNB по рынку сейчас недоступна.')
    maximum = dec(s.get('maxQuoteAmountMarket') or s.get('maxQuoteAmount') or '0')
    if maximum and total > maximum:
        raise BinanceError('Сумма превышает лимит рыночного ордера.')
    minimum = dec(s.get('quoteAmountPrecisionMarket') or s.get('quoteAmountPrecision') or '0')
    for f in s.get('filters', []):
        if f.get('filterType') in ('MIN_NOTIONAL','NOTIONAL'):
            if f.get('applyToMarket',f.get('applyMinToMarket',True)):
                minimum = max(minimum, dec(f.get('minNotional','0')))
            cap = dec(f.get('maxNotional') or '0')
            if cap and f.get('applyMaxToMarket',False) and total > cap:
                raise BinanceError('Сумма превышает максимальную стоимость рыночного ордера.')
    if total < minimum:
        raise BinanceError('Минимальная покупка: %s USDT.' % fmt(minimum))
    fee = dec(net['withdrawFee'])
    if total*(1-rate)/price <= dec(net['withdrawMin']) + fee:
        raise BinanceError('Ориентировочного BNB недостаточно для минимума вывода и комиссии.')
    net_estimate = total*(1-rate)/price
    withdrawal_estimate = ((net_estimate-fee)/step).to_integral_value(rounding=ROUND_DOWN)*step
    ident = uuid.uuid4().hex
    data = dict(goal=goal, commission_base=fmt(commission_base), withdrawal_step=fmt(step),
                reserve_estimate=fmt(net_estimate-withdrawal_estimate),
                withdrawal_estimate=fmt(withdrawal_estimate),
                recipient_low=fmt(max(Decimal(0),withdrawal_estimate-fee)),
                recipient_high=fmt(withdrawal_estimate),
                recipient_usdt_low=fmt(max(Decimal(0),withdrawal_estimate-fee)*price),
                target_bnb=fmt(amount/price), amount=fmt(amount), total=fmt(total), address=address, network=name, memo=memo,
                estimate=fmt(total/price), net_estimate=fmt(total*(1-rate)/price),
                commission_rate=fmt(rate), commission_percent=fmt(rate*100),
                commission_extra=fmt(total-commission_base), total_extra=fmt(total-amount), commission_discount=discounted,
                commission_price=fmt(price), fee=fmt(fee), minimum=str(net['withdrawMin']))
    with db() as c:
        c.execute('INSERT INTO binance_jobs(id,created,state,data) VALUES (?,?,?,?)',
                  (ident,time.time(),'preview',json.dumps(data)))
    return get_job(ident)

def buy_confirm(ident):
    from app import db
    j = get_job(ident)
    if j['state'] != 'preview' or j['data'].get('action') == 'sell' or time.time()-j['created'] > 180:
        raise BinanceError('Расчёт истёк или уже подтверждён. Обновите список операций.')
    d = j['data']
    client = Client()
    # Recheck available balance before reserving the single dispatch.
    a = account(client)
    if 'commission_rate' not in d:
        raise BinanceError('Пересчитайте покупку: изменилась схема учёта комиссии.')
    rate, discounted = buy_commission(client, dec(d.get('commission_base',d['amount'])), dec(d['commission_price']), free(a,'BNB'))
    if rate != dec(d['commission_rate']) or discounted != d['commission_discount']:
        raise BinanceError('Комиссия Binance изменилась. Рассчитайте покупку заново.')
    if free(a,'USDT') < dec(d['total']):
        raise BinanceError('Недостаточно свободных USDT.')
    fresh_network = network(client, d['network'])
    if d.get('goal') == 'wallet' and (dec(fresh_network['withdrawFee']) != dec(d['fee'])
            or withdrawal_step(fresh_network.get('withdrawIntegerMultiple')) != dec(d['withdrawal_step'])):
        raise BinanceError('Комиссия или шаг вывода изменились. Рассчитайте покупку заново.')
    ensure_withdrawal_supported(client)
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        if c.execute("SELECT 1 FROM binance_jobs WHERE state IN ('buying','bought','withdrawing')").fetchone():
            raise BinanceError('Сначала завершите текущую операцию Binance.')
        if not c.execute("UPDATE binance_jobs SET state='buying' WHERE id=? AND state='preview'",(ident,)).rowcount:
            raise BinanceError('Покупка уже подтверждена.')
    try:
        result = client.call('/api/v3/order', {'symbol':'BNBUSDT','side':'BUY','type':'MARKET',
                    'quoteOrderQty':d['total'],'newClientOrderId':ident}, method='POST')
        if not result.get('orderId'):
            raise BinanceError('Нет ID ордера. Нужна сверка с Binance.')
        d['order_id'] = str(result['orderId'])
        save(ident,'buying',d)
    except BinanceError as e:
        save(ident,'buying',d,str(e))
    return get_job(ident)

def withdrawal_asset_matches(row, expected_network):
    return row.get('coin') == 'BNB' and row.get('network') == expected_network


def ensure_withdrawal_supported(client):
    requirements = client.call('/sapi/v1/localentity/questionnaire-requirements')
    country = requirements.get('questionnaireCountryCode') if isinstance(requirements, dict) else requirements
    if country != 'NIL':
        raise BinanceError('Binance требует Travel Rule или не подтвердил его отсутствие. Вывод через приложение остановлен; используйте интерфейс Binance.')


def reconcile(ident):
    j = get_job(ident)
    d, state = j['data'], j['state']
    if state not in ('buying','withdrawing'):
        return j
    client = Client()
    try:
        if state == 'buying':
            order = client.call('/api/v3/order', {'symbol':'BNBUSDT','origClientOrderId':ident})
            if order.get('clientOrderId') != ident or order.get('side') != ('SELL' if d.get('action') == 'sell' else 'BUY') or order.get('symbol') != 'BNBUSDT':
                raise BinanceError('Ордер не соответствует запросу. Требуется ручная сверка.')
            d['order_id'] = str(order['orderId'])
            d['order_status'] = order['status']
            if order['status'] not in ('FILLED','CANCELED','PARTIALLY_CANCELED','EXPIRED','REJECTED','EXPIRED_IN_MATCH'):
                save(ident,state,d)
                return get_job(ident)
            qty = dec(order['executedQty'])
            if qty == 0:
                save(ident,'closed',d,'Ордер завершён без покупки.')
                return get_job(ident)
            trades = client.call('/api/v3/myTrades', {'symbol':'BNBUSDT','orderId':d['order_id'],'limit':1000})
            if not isinstance(trades,list) or any(str(t.get('orderId')) != d['order_id'] for t in trades):
                raise BinanceError('Неверный список сделок ордера.')
            if sum((dec(t['qty']) for t in trades),Decimal(0)) != qty:
                raise BinanceError('Получены не все исполнения ордера. Вывод пока заблокирован.')
            if len({str(t['id']) for t in trades}) != len(trades):
                raise BinanceError('Повторяющиеся исполнения. Нужна сверка.')
            quote = dec(order['cummulativeQuoteQty'])
            fees = {}
            for trade in trades:
                asset = str(trade['commissionAsset'])
                fees[asset] = fmt(dec(fees.get(asset,0)) + dec(trade['commission']))
            d.update(gross_bnb=fmt(qty), actual_quote=fmt(quote), average_price=fmt(quote/qty), trade_fees=fees)
            if d.get('action') == 'sell':
                quote_fee = sum((dec(t['commission']) for t in trades if t['commissionAsset']=='USDT'), Decimal(0))
                quote = dec(order['cummulativeQuoteQty'])
                if quote_fee > quote:
                    raise BinanceError('Неверная комиссия продажи.')
                d.update(sold=fmt(qty), received_usdt=fmt(quote-quote_fee))
                save(ident,'done',d)
                return get_job(ident)
            fee = sum((dec(t['commission']) for t in trades if t['commissionAsset']=='BNB'),Decimal(0))
            if fee >= qty:
                raise BinanceError('Неверная комиссия покупки.')
            d.update(bought=fmt(qty-fee), spent=str(order['cummulativeQuoteQty']), bnb_trade_fee=fmt(fee))
            save(ident,'bought',d)
        else:
            found = [r for r in history(client,d['withdraw_time']) if
                     r.get('withdrawOrderId') == ident or (d.get('withdraw_id') and str(r.get('id')) == d['withdraw_id'])]
            if len(found) != 1:
                raise BinanceError('Вывод ещё не найден в истории. Повторная отправка заблокирована; проверьте Binance.')
            r = found[0]
            if str(r.get('address','')).lower() != d['address'].lower() or not withdrawal_asset_matches(r, d['network']):
                raise BinanceError('Данные вывода не совпали. Нужна ручная сверка.')
            d.update(withdraw_id=str(r['id']),withdraw_status=r['status'],txid=r.get('txId') or r.get('transHash') or '')
            for source, target in [('amount','withdraw_actual_amount'),('transactionFee','withdraw_actual_fee')]:
                if r.get(source) is not None:
                    d[target] = fmt(dec(r[source]))
            state = 'done' if int(r['status']) == 6 else 'closed' if int(r['status']) in (1,3,5) else state
            save(ident,state,d,'Вывод отклонён/отменён на Binance.' if state=='closed' else '')
    except BinanceError as e:
        save(ident,state,d,str(e))
    return get_job(ident)

def withdrawal_step(value):
    # Binance supplies an increment, NOT a count of decimal places.
    if value is None or isinstance(value, bool):
        raise BinanceError('Binance не сообщил точность вывода.')
    step = dec(value)
    if not Decimal('1e-18') <= step <= 1:
        raise BinanceError('Неверная точность вывода Binance.')
    return step


def withdrawal_plan(client,d):
    ensure_withdrawal_supported(client)
    n = network(client,d['network'])
    if d.get('memo') or n.get('withdrawTag') or n.get('busy'):
        raise BinanceError('Сеть требует тег или временно занята. Вывод остановлен.')
    a = account(client)
    if a.get('canWithdraw') is not True:
        raise BinanceError('Вывод на аккаунте недоступен.')
    fee = dec(n['withdrawFee'])
    # Reserve fee in addition to amount: never spend pre-existing BNB.
    # Venue may deduct fee from amount; exact receipt is reported by Binance history.
    step = withdrawal_step(n.get('withdrawIntegerMultiple'))
    if step <= 0:
        raise BinanceError('Неизвестна точность вывода.')
    available = min(dec(d['bought']),free(a,'BNB'))
    amount = ((available-fee)/step).to_integral_value(rounding=ROUND_DOWN)*step
    if amount <= 0 or amount < dec(n['withdrawMin']) or (n.get('withdrawMax') and amount > dec(n['withdrawMax'])):
        raise BinanceError('Купленного/свободного BNB недостаточно либо превышен лимит вывода.')
    return {'amount':fmt(amount),'fee':fmt(fee),'network':d['network'],'address':d['address'],'memo':d['memo']}

def withdraw_preview(ident):
    j = get_job(ident)
    if j['state'] != 'bought':
        raise BinanceError('Покупка ещё не подтверждена биржей.')
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
        raise BinanceError('Нужен свежий расчёт вывода.')
    client = Client()
    plan = withdrawal_plan(client,d)
    if plan != d.get('withdraw_plan'):
        raise BinanceError('Баланс или комиссия изменились. Пересчитайте вывод.')
    d['withdraw_time'] = time.time()
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        if not c.execute("UPDATE binance_jobs SET state='withdrawing',data=?,error='' WHERE id=? AND state='bought'",
                         (json.dumps(d),ident)).rowcount:
            raise BinanceError('Вывод уже подтверждён.')
    try:
        params = dict(coin='BNB',walletType=0,withdrawOrderId=ident,network=d['network'],address=d['address'],amount=plan['amount'])
        if d['memo']:
            params['addressTag']=d['memo']
        result = client.call('/sapi/v1/capital/withdraw/apply',params,method='POST')
        if not result.get('id'):
            raise BinanceError('Нет ID вывода. Нужна сверка с биржей.')
        d['withdraw_id']=str(result['id'])
        save(ident,'withdrawing',d)
    except BinanceError as e:
        save(ident,'withdrawing',d,str(e))
    return get_job(ident)

def sell_plan(client):
    a = account(client)
    if a.get('canTrade') is not True:
        raise BinanceError('Торговля на аккаунте недоступна.')
    available = free(a, 'BNB')
    if available <= 0:
        raise BinanceError('Нет свободного BNB для продажи.')
    info = client.call('/api/v3/exchangeInfo', {'symbol':'BNBUSDT'}, signed=False)
    symbols = info.get('symbols', [info] if info.get('symbol') == 'BNBUSDT' else [])
    if len(symbols) != 1:
        raise BinanceError('Не получены правила BNBUSDT.')
    s = symbols[0]
    if ('MARKET' not in s.get('orderTypes', []) or s.get('isSpotTradingAllowed') is False
            or s.get('status') != 'TRADING'):
        raise BinanceError('Рыночная продажа BNBUSDT недоступна.')
    precision = s.get('baseAssetPrecision')
    if isinstance(precision, bool) or not str(precision).isdigit() or not 0 <= int(precision) <= 18:
        raise BinanceError('Не получена точность количества BNB.')
    step = Decimal(1).scaleb(-int(precision))
    minimum = dec(s.get('baseSizePrecision') or '0')
    maximum = Decimal('1e18')
    minimum_quote = dec(s.get('quoteAmountPrecisionMarket') or s.get('quoteAmountPrecision') or '0')
    for f in s.get('filters', []):
        if f.get('filterType') in ('LOT_SIZE','MARKET_LOT_SIZE'):
            increment = dec(f.get('stepSize') or '0')
            if increment:
                step = max(step, increment)
            minimum = max(minimum, dec(f.get('minQty') or '0'))
            limit = dec(f.get('maxQty') or '0')
            if limit:
                maximum = min(maximum, limit)
        if f.get('filterType') in ('MIN_NOTIONAL','NOTIONAL'):
            if f.get('applyToMarket',f.get('applyMinToMarket',True)):
                minimum_quote = max(minimum_quote, dec(f.get('minNotional') or '0'))
    quantity = (available/step).to_integral_value(rounding=ROUND_DOWN)*step
    price = dec(client.call('/api/v3/ticker/price', {'symbol':'BNBUSDT'}, signed=False)['price'])
    estimate = quantity*price
    max_quote = dec(s.get('maxQuoteAmountMarket') or s.get('maxQuoteAmount') or '0')
    for f in s.get('filters',[]):
        if f.get('filterType') == 'NOTIONAL' and f.get('applyMaxToMarket',False):
            cap=dec(f.get('maxNotional') or '0')
            if cap and estimate > cap:
                raise BinanceError('Весь баланс превышает максимальную стоимость одного ордера.')
    if quantity <= 0 or quantity < minimum or estimate < minimum_quote:
        raise BinanceError('Баланс BNB меньше минимального рыночного ордера Binance.')
    if quantity > maximum or (max_quote and estimate > max_quote):
        raise BinanceError('Весь баланс превышает лимит одного ордера Binance. Продажа не отправлена.')
    return dict(action='sell', quantity=fmt(quantity), available=fmt(available),
                dust=fmt(available-quantity), estimate=fmt(estimate))

def sell_preview():
    from app import db
    with db() as c:
        if c.execute("SELECT 1 FROM binance_jobs WHERE state IN ('buying','bought','withdrawing')").fetchone():
            raise BinanceError('Сначала завершите текущую операцию или оставьте купленный BNB на бирже.')
    data = sell_plan(Client())
    ident = uuid.uuid4().hex
    with db() as c:
        c.execute('INSERT INTO binance_jobs(id,created,state,data) VALUES (?,?,?,?)',
                  (ident,time.time(),'preview',json.dumps(data)))
    return get_job(ident)

def sell_confirm(ident):
    from app import db
    j = get_job(ident)
    d = j['data']
    if j['state'] != 'preview' or d.get('action') != 'sell' or time.time()-j['created'] > 180:
        raise BinanceError('Расчёт продажи истёк или уже подтверждён.')
    client = Client()
    current = sell_plan(client)
    if current['quantity'] != d['quantity'] or current['available'] != d['available']:
        raise BinanceError('Баланс изменился. Рассчитайте продажу заново.')
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        if c.execute("SELECT 1 FROM binance_jobs WHERE state IN ('buying','bought','withdrawing')").fetchone():
            raise BinanceError('Сначала завершите текущую операцию Binance.')
        if not c.execute("UPDATE binance_jobs SET state='buying' WHERE id=? AND state='preview'",(ident,)).rowcount:
            raise BinanceError('Продажа уже подтверждена.')
    try:
        result = client.call('/api/v3/order', {'symbol':'BNBUSDT','side':'SELL','type':'MARKET',
                    'quantity':d['quantity'],'newClientOrderId':ident}, method='POST')
        if not result.get('orderId'):
            raise BinanceError('Нет ID ордера продажи. Нужна сверка с Binance.')
        d['order_id'] = str(result['orderId'])
        save(ident,'buying',d)
    except BinanceError as e:
        save(ident,'buying',d,str(e))
    return get_job(ident)


def install(app):
    from app import db
    from flask import request
    @app.get('/api/binance/jobs')
    def binance_jobs():
        with db() as c:
            rows=c.execute("SELECT id FROM binance_jobs WHERE state!='preview' ORDER BY created DESC LIMIT 50").fetchall()
        with db() as c:
            cache = {r['key']: json.loads(r['value']) for r in c.execute(
                "SELECT * FROM status WHERE key IN ('binance_balances','binance_balance_error','binance_poll')")}
        return {'items':[get_job(r['id']) for r in rows], 'cache':cache}

    @app.post('/api/binance/refresh')
    def binance_refresh():
        client=Client()
        a=account(client)
        warnings=[]
        try:
            ns=networks(client)
        except BinanceError as e:
            ns=[]; warnings.append('Сети: '+str(e))
        try:
            rows=history(client)
        except BinanceError as e:
            rows=[]; warnings.append('История: '+str(e))
        from app import set_status
        set_status(binance_balances={'updated':time.time(),'balances':[b for b in a['balances'] if b['asset'] in ('BNB','USDT')]},binance_balance_error=None)
        return {'warnings':warnings,'updated':time.time(),'balances':[b for b in a['balances'] if b['asset'] in ('BNB','USDT')],
                'networks':[{k:n.get(k) for k in ('network','name','withdrawEnable','withdrawFee','withdrawMin','withdrawTips')} for n in ns],
                'withdrawals':[{k:r.get(k) for k in ('id','amount','address','network','status','txId','applyTime','transactionFee')} for r in rows]}

    @app.post('/api/binance/sell-preview')
    def binance_sell_preview():
        return sell_preview()

    @app.post('/api/binance/<ident>/sell')
    def binance_sell(ident):
        return sell_confirm(ident)

    @app.post('/api/binance/preview')
    def binance_preview():
        return buy_preview(request.get_json() or {})

    @app.post('/api/binance/<ident>/buy')
    def binance_buy(ident):
        return buy_confirm(ident)

    @app.post('/api/binance/<ident>/check')
    def binance_check(ident):
        return reconcile(ident)

    @app.post('/api/binance/<ident>/withdraw-preview')
    def binance_withdraw_preview(ident):
        return withdraw_preview(ident)

    @app.post('/api/binance/<ident>/withdraw')
    def binance_withdraw(ident):
        return withdraw_confirm(ident)

    @app.post('/api/binance/<ident>/keep')
    def binance_keep(ident):
        # An executed purchase can be kept on exchange without withdrawing.
        with db() as c:
            if not c.execute("UPDATE binance_jobs SET state='closed' WHERE id=? AND state='bought'",(ident,)).rowcount:
                raise BinanceError('Операция не находится в состоянии куплено.')
        return {'ok':True}

    # Cross-process lock also covers reconciliation, preventing stale state writes
    # during a dispatch or a second browser session. No external request on GET.
    def serialized(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            from app import DATA
            with (DATA / 'binance.lock').open('a') as handle:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise BinanceError('Запрос Binance уже выполняется. Подождите.') from None
                try:
                    return fn(*args, **kwargs)
                finally:
                    fcntl.flock(handle, fcntl.LOCK_UN)
        return wrapped
    for name, fn in list(app.view_functions.items()):
        if name.startswith('binance_') and name != 'binance_jobs':
            app.view_functions[name] = serialized(fn)
