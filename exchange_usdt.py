"""Explicit, durable USDT/BSC withdrawals. Reconciliation never dispatches money."""
import fcntl
import json
import time
import uuid
from contextlib import contextmanager
from decimal import Decimal
import binance
from exchange_clients import Client, ExchangeError, dec, fmt, one
from exchanges import address, round_step

VENUES = ('binance', 'gate', 'bybit')


def schema(c):
    c.execute("CREATE TABLE IF NOT EXISTS usdt_withdrawals(id TEXT PRIMARY KEY,venue TEXT NOT NULL,created REAL NOT NULL,state TEXT NOT NULL,data TEXT NOT NULL,error TEXT NOT NULL DEFAULT '')")
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS usdt_one_active ON usdt_withdrawals(venue) WHERE state='sending'")


def check_usdt_idle(c, venue):
    if c.execute("SELECT 1 FROM usdt_withdrawals WHERE venue=? AND state='sending'", (venue,)).fetchone():
        raise ExchangeError('Сначала завершите или сверьте предыдущий вывод USDT этой биржи.')


def check_idle(c, venue):
    check_usdt_idle(c, venue)
    if venue == 'binance':
        active = c.execute("SELECT 1 FROM binance_jobs WHERE state IN ('buying','bought','withdrawing')").fetchone()
    else:
        active = c.execute("SELECT 1 FROM exchange_jobs WHERE venue=? AND state IN ('ordering','bought','withdrawing')", (venue,)).fetchone()
    if active:
        raise ExchangeError('Сначала завершите предыдущую покупку, продажу или вывод этой биржи.')


@contextmanager
def locked(venue):
    from app import DATA
    if venue not in VENUES:
        raise ExchangeError('Неизвестная биржа.')
    # Same lock as existing BNB workflows, including their background workers.
    with (DATA / (venue + '.lock')).open('a') as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ExchangeError('Запрос биржи уже выполняется. Подождите.') from None
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


class Adapter:
    def __init__(self, venue):
        self.venue = venue
        self.client = binance.Client() if venue == 'binance' else Client(venue)

    def quote(self, target, source):
        c = self.client
        if self.venue != 'binance':
            c.address_check(target, coin='USDT')
            n = c.network(coin='USDT')
            balances = c.funding_balances() if source == 'FUND' else c.balances()
            return n, dec(balances.get('USDT', 0))
        binance.ensure_withdrawal_supported(c)
        a = binance.account(c)
        if a.get('canWithdraw') is not True:
            raise ExchangeError('Вывод Binance недоступен для аккаунта.')
        coin = one([r for r in c.call('/sapi/v1/capital/config/getall') if r.get('coin') == 'USDT'])
        n = one([r for r in coin['networkList'] if r.get('network') == 'BSC'])
        if n.get('withdrawEnable') is not True or n.get('busy') or n.get('withdrawTag'):
            raise ExchangeError('Вывод USDT/BSC на Binance недоступен или требует тег.')
        return dict(network='BSC', fee=fmt(dec(n['withdrawFee'])), minimum=fmt(dec(n['withdrawMin'])),
                    maximum=fmt(dec(n['withdrawMax'])), step=fmt(binance.withdrawal_step(n.get('withdrawIntegerMultiple')))), binance.free(a, 'USDT')

    def send(self, ident, d):
        if self.venue == 'binance':
            r = self.client.call('/sapi/v1/capital/withdraw/apply', dict(coin='USDT', network='BSC',
                walletType=0, withdrawOrderId=ident, address=d['address'], amount=d['amount']), method='POST')
            ident = r.get('id')
        else:
            ident = self.client.withdraw(ident, d['address'], d['amount'], coin='USDT', account_type=d['source'])
        if not ident or str(ident) in ('None', 'null'):
            raise ExchangeError('Биржа не вернула ID вывода. Нужна сверка.')
        return str(ident)

    def history(self, ident, d, supplied=None):
        wid = supplied or d.get('withdraw_id')
        if self.venue != 'binance':
            return self.client.history(ident=ident, withdraw_id=wid, start=d['sent_at'], coin='USDT')
        start = int((d['sent_at'] - 300) * 1000)
        p = dict(coin='USDT', withdrawOrderId=ident, startTime=start,
                 endTime=int(min(time.time(), d['sent_at'] + 86400) * 1000), limit=1000)
        if wid:
            p['idList'] = wid
        rows = self.client.call('/sapi/v1/capital/withdraw/history', p)
        return [dict(id=str(r['id']), request_id=r.get('withdrawOrderId'), coin=r['coin'], network=r['network'],
                     address=r['address'], amount=r['amount'], fee=r['transactionFee'], status=r['status'],
                     txid=r.get('txId', ''), done=r['status'] == 6, failed=r['status'] in (1, 3, 5)) for r in rows]


def job(venue, ident):
    from app import db
    with db() as c:
        r = c.execute('SELECT * FROM usdt_withdrawals WHERE venue=? AND id=?', (venue, ident)).fetchone()
    if not r:
        raise ExchangeError('Операция USDT не найдена.')
    return dict(r) | {'data': json.loads(r['data'])}


def save(j, state=None, error=''):
    from app import db
    with db() as c:
        if state == 'done' and j['state'] != 'done':
            from operations import queue_balance
            queue_balance(c, j['data']['address'])
        c.execute('UPDATE usdt_withdrawals SET state=?,data=?,error=? WHERE id=?',
                  (state or j['state'], json.dumps(j['data']), error, j['id']))
    return job(j['venue'], j['id'])


def plan(venue, body):
    if venue not in VENUES or body.get('network', 'BSC') != 'BSC':
        raise ExchangeError('Поддерживается вывод USDT в сети BSC.')
    target = address(str(body.get('address', '')).strip())
    source = body.get('source', 'FUND' if venue == 'bybit' else 'UTA')
    if source not in (('FUND', 'UTA') if venue == 'bybit' else ('UTA',)):
        raise ExchangeError('Неверный аккаунт списания.')
    n, available = Adapter(venue).quote(target, source)
    fee = dec(n['fee'])
    if n['network'] != 'BSC':
        raise ExchangeError('Биржа вернула другую сеть.')
    maximum = round_step(max(Decimal(0), available - fee), n['step'])
    if n['maximum'] is not None:
        maximum = min(maximum, round_step(dec(n['maximum']), n['step']))
    amount = maximum if body.get('maximum') is True else dec(body.get('amount'))
    if amount != round_step(amount, n['step']):
        raise ExchangeError('Шаг суммы USDT для этой биржи: ' + n['step'])
    if amount <= 0 or amount < dec(n['minimum']) or amount > maximum:
        raise ExchangeError('Сумма вне лимитов или недостаточно USDT с резервом комиссии. Минимум: '+n['minimum']+'; доступно к выводу: '+fmt(maximum)+' USDT.')
    return dict(address=target, network='BSC', source=source, amount=fmt(amount), fee=fmt(fee),
                reserve=fmt(amount + fee), available=fmt(available), minimum=n['minimum'], step=n['step'])


def preview(venue, body):
    from app import db
    with db() as c:
        check_idle(c, venue)
    d = plan(venue, body)
    ident = uuid.uuid4().hex[:28]
    with db() as c:
        c.execute('INSERT INTO usdt_withdrawals(id,venue,created,state,data) VALUES(?,?,?,?,?)',
                  (ident, venue, time.time(), 'preview', json.dumps(d)))
    return job(venue, ident)


def confirm(venue, ident):
    from app import db
    j = job(venue, ident)
    d = j['data']
    if j['state'] != 'preview' or time.time() - j['created'] > 180:
        raise ExchangeError('Расчёт истёк или уже подтверждён. Повторная отправка запрещена.')
    fresh = plan(venue, d)  # Fresh balance, limits and whitelist. Amount cannot increase.
    if any(fresh[k] != d[k] for k in ('amount', 'fee', 'reserve', 'address', 'network', 'source')):
        raise ExchangeError('Комиссия изменилась. Рассчитайте вывод заново.')
    d['sent_at'] = time.time()
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        check_idle(c, venue)
        if not c.execute("UPDATE usdt_withdrawals SET state='sending',data=? WHERE id=? AND state='preview'",
                         (json.dumps(d), ident)).rowcount:
            raise ExchangeError('Операция уже подтверждена.')
    j['state'] = 'sending'
    try:
        d['withdraw_id'] = Adapter(venue).send(ident, d)
        return save(j)
    except Exception as e:
        return save(j, error=(str(e) if isinstance(e, ValueError) else 'Ответ биржи потерян.') + ' Повторный вывод не отправляется; выполните сверку.')


def matches(venue, d, r):
    if r.get('coin') != 'USDT' or r.get('network') != 'BSC' or str(r.get('address', '')).lower() != d['address'].lower():
        return False
    amount = dec(r.get('amount'))
    # Binance/Gate history can report the amount net of the confirmed fixed fee.
    allowed = (dec(d['amount']),) if venue == 'bybit' else (dec(d['amount']), dec(d['amount']) - dec(d['fee']))
    if amount not in allowed or amount <= 0:
        return False
    if venue == 'bybit':
        return abs(float(r['created']) / 1000 - d['sent_at']) <= 300
    return True


def reconcile(venue, ident, supplied=None):
    from app import db
    j = job(venue, ident)
    if j['state'] != 'sending':
        return j
    d = j['data']; d['last_checked'] = time.time()
    try:
        if supplied and (not isinstance(supplied, str) or not supplied.isalnum() or len(supplied) > 128):
            raise ExchangeError('Некорректный ID вывода.')
        if supplied and d.get('withdraw_id') and supplied != d['withdraw_id']:
            raise ExchangeError('У операции уже другой ID вывода.')
        rows = Adapter(venue).history(ident, d, supplied)
        wid = supplied or d.get('withdraw_id')
        if venue == 'bybit' and not wid:
            candidates = [r for r in rows if matches(venue, d, r)]
            d['candidates'] = candidates
            return save(j, error='ID вывода не получен. Совпадений в истории: '+str(len(candidates))+'. Укажите ID из истории Bybit для сверки. Повторный вывод заблокирован.')
        rows = [r for r in rows if (r.get('id') == wid if wid else r.get('request_id') == ident)]
        r = one(rows)
        if not matches(venue, d, r):
            raise ExchangeError('Адрес, сеть, монета, сумма или время вывода не совпали.')
        if venue != 'bybit' and r.get('request_id') != ident:
            raise ExchangeError('ID заявки не совпал.')
        with db() as c:
            for other in c.execute('SELECT data FROM usdt_withdrawals WHERE venue=? AND id!=?', (venue, ident)):
                if json.loads(other['data']).get('withdraw_id') == r['id']:
                    raise ExchangeError('Этот ID уже связан с другой операцией.')
        d['withdraw_id'] = r['id']; d['withdrawal'] = r; d.pop('candidates', None)
        return save(j, 'done' if r['done'] else 'failed' if r['failed'] else 'sending')
    except Exception as e:
        return save(j, error=str(e) if isinstance(e, ValueError) else 'Не удалось сверить вывод USDT. Повторная отправка заблокирована.')


def run(stop):
    from app import db
    while not stop.is_set():
        for venue in VENUES:
            try:
                with locked(venue):
                    with db() as c:
                        rows = c.execute("SELECT id FROM usdt_withdrawals WHERE venue=? AND state='sending'", (venue,)).fetchall()
                    for r in rows:
                        reconcile(venue, r['id'])
            except Exception:
                pass
        stop.wait(30)


def install(app):
    from flask import request
    from app import db
    @app.get('/api/usdt-withdrawals/<venue>')
    def exchange_usdt_jobs(venue):
        if venue not in VENUES:
            raise ExchangeError('Неизвестная биржа.')
        with db() as c:
            rows = c.execute("SELECT id FROM usdt_withdrawals WHERE venue=? AND state!='preview' ORDER BY created DESC LIMIT 50", (venue,)).fetchall()
        return {'items': [job(venue, r['id']) for r in rows]}

    @app.post('/api/usdt-withdrawals/<venue>/preview')
    def exchange_usdt_preview(venue):
        with locked(venue):
            return preview(venue, request.get_json() or {})

    @app.post('/api/usdt-withdrawals/<venue>/<ident>/<action>')
    def exchange_usdt_action(venue, ident, action):
        with locked(venue):
            if action == 'confirm':
                return confirm(venue, ident)
            if action == 'check':
                return reconcile(venue, ident, (request.get_json() or {}).get('withdraw_id'))
            raise ExchangeError('Неизвестное действие.')
