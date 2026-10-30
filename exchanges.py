"""Durable Gate/Bybit spot operations; every money action requires a preview."""
import fcntl
import json
import re
import time
import uuid
from contextlib import contextmanager
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from exchange_clients import Client, ExchangeError, dec, fmt

VENUES={'gate':'Gate','bybit':'Bybit'}
ACTIVE=('ordering','bought','withdrawing')

def venue(value):
    if value not in VENUES:raise ExchangeError('Неизвестная биржа.')
    return value

def schema(c):
    c.execute("CREATE TABLE IF NOT EXISTS exchange_jobs(id TEXT PRIMARY KEY,venue TEXT NOT NULL,created REAL NOT NULL,state TEXT NOT NULL,data TEXT NOT NULL,error TEXT NOT NULL DEFAULT '')")
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS exchange_one_active ON exchange_jobs(venue) WHERE state IN ('ordering','bought','withdrawing')")

@contextmanager
def locked(v):
    from app import DATA
    venue(v)
    with (DATA/(v+'.lock')).open('a') as f:
        try:fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise ExchangeError('Запрос биржи уже выполняется.') from None
        try:yield
        finally:fcntl.flock(f,fcntl.LOCK_UN)

def job(v,ident):
    from app import db
    with db() as c:r=c.execute('SELECT * FROM exchange_jobs WHERE id=? AND venue=?',(ident,v)).fetchone()
    if not r:raise ExchangeError('Операция не найдена.')
    return dict(r)|{'data':json.loads(r['data'])}

def save(j,state=None,error=''):
    from app import db
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        if state=='done' and j['state']!='done' and j['data']['action']=='buy':
            from operations import queue_balance
            queue_balance(c,j['data']['address'])
        c.execute('UPDATE exchange_jobs SET state=?,data=?,error=? WHERE id=? AND venue=?',
            (state or j['state'],json.dumps(j['data']),error,j['id'],j['venue']))
    return job(j['venue'],j['id'])

def insert(v,data):
    from app import db
    ident=uuid.uuid4().hex[:28]
    with db() as c:c.execute('INSERT INTO exchange_jobs(id,venue,created,state,data) VALUES (?,?,?,?,?)',(ident,v,time.time(),'preview',json.dumps(data)))
    return job(v,ident)

def round_step(n,step,up=False):
    step=dec(step)
    if not Decimal('1e-18')<=step<=1:raise ExchangeError('Неверный шаг количества.')
    return (n/step).to_integral_value(rounding=ROUND_UP if up else ROUND_DOWN)*step

def address(value):
    if not isinstance(value,str) or not re.fullmatch('0x[0-9a-fA-F]{40}',value) or not int(value[2:],16):
        raise ExchangeError('Введите полный адрес BSC (0x…).')
    return value # Bybit address book is case sensitive.

def funding_needed(v, client, available, required):
    shortage=max(Decimal(0),required-dec(available))
    if not shortage:return '0'
    if v!='bybit':raise ExchangeError('Недостаточно свободных USDT с резервом комиссии.')
    amount=round_step(shortage,'0.000001',True)
    if dec(client.funding_balances().get('USDT',0))<amount:
        raise ExchangeError('Недостаточно USDT на торговом аккаунте и финансировании с резервом комиссии.')
    return fmt(amount)

def plan(v,body):
    client=Client(v); action=body.get('action','buy')
    if action not in ('buy','sell'):raise ExchangeError('Неизвестная операция.')
    a=client.balances(); m=client.market(); price=dec(m['price']); rate=dec(m['fee'])
    if not price or rate>=Decimal('0.1'):raise ExchangeError('Не удалось проверить цену/комиссию.')
    if action=='sell':
        available=dec(a.get('BNB',0))
        # Reserve potential BNB trading fee instead of assuming the fee asset.
        amount=round_step(available/(1+rate),m['base_step'])
        quote_amount=amount*price
        if amount<=0 or amount>dec(m['max_base']):raise ExchangeError('Количество вне лимитов рыночной продажи.')
        data=dict(action=action,quantity=fmt(amount),available=fmt(available),dust=fmt(available-amount),estimate=fmt(quote_amount),market=m)
    else:
        target=address(str(body.get('address','')).strip())
        client.address_check(target) # BEFORE purchase, not after spending USDT.
        n=client.network(); amount=dec(body.get('amount'))
        if amount<=0 or amount.as_tuple().exponent < -2:raise ExchangeError('Сумма USDT: больше нуля, максимум 2 знака.')
        goal=body.get('goal','wallet')
        if goal not in ('trade','wallet'):raise ExchangeError('Неверная цель расчёта.')
        reserve=2*dec(n['fee']) if goal=='wallet' else Decimal(0)
        total=round_step((amount+reserve*price)/(1-rate),m['quote_step'],True)
        # Reserve fee also if charged in USDT. Never borrow to fill a market order.
        funding=funding_needed(v,client,a.get('USDT',0),total*(1+rate))
        received=total*(1-rate)/price
        if received-dec(n['fee'])<dec(n['minimum']):raise ExchangeError('Сумма покупки меньше минимума последующего вывода.')
        quote_amount=total
        data=dict(action=action,goal=goal,funding_amount=funding,amount=fmt(amount),total=fmt(total),estimate=fmt(received),
            recipient_estimate=fmt(max(Decimal(0),received-2*dec(n['fee']))),address=target,network=n,market=m)
    if quote_amount<dec(m['minimum']) or quote_amount>dec(m['max_quote']):raise ExchangeError('Стоимость вне лимитов рыночного ордера.')
    return data

def preview(v,body):return insert(v,plan(v,body))

def reserve(j,old,state):
    from app import db
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        if old=='preview' and c.execute("SELECT 1 FROM exchange_jobs WHERE venue=? AND state IN ('ordering','bought','withdrawing')",(j['venue'],)).fetchone():
            raise ExchangeError('Сначала завершите предыдущую операцию этой биржи.')
        if not c.execute('UPDATE exchange_jobs SET state=?,data=?,error=? WHERE id=? AND venue=? AND state=?',
                (state,json.dumps(j['data']),'',j['id'],j['venue'],old)).rowcount:
            raise ExchangeError('Операция уже подтверждена.')
    j['state']=state

def confirm(v,ident):
    j=job(v,ident); d=j['data']
    if j['state']!='preview' or time.time()-j['created']>120:raise ExchangeError('Расчёт истёк или уже подтверждён.')
    fresh=plan(v,d)
    keys=('quantity','available') if d['action']=='sell' else ('network','address')
    if any(fresh[k]!=d[k] for k in keys) or fresh['market']['fee']!=d['market']['fee']:
        raise ExchangeError('Баланс, цена или комиссия изменились. Пересчитайте операцию.')
    if d['action']=='buy':
        funds=Client(v).balances()
        needed=funding_needed(v,Client(v),funds.get('USDT',0),dec(d['total'])*(1+dec(d['market']['fee'])))
        if dec(needed)>dec(d.get('funding_amount','0')):
            raise ExchangeError('Нужна большая сумма перевода с финансирования. Пересчитайте покупку.')
        d['funding_amount']=needed
        if not dec(fresh['market']['minimum']) <= dec(d['total']) <= dec(fresh['market']['max_quote']):
            raise ExchangeError('Изменились торговые лимиты. Пересчитайте покупку.')
    if d['action']=='buy' and dec(d.get('funding_amount','0')):
        d['funding_id']=str(uuid.uuid4());d['funding_pending']=True
        reserve(j,'preview','ordering')
        try:
            Client(v).fund_trading(d['funding_id'],d['funding_amount'])
            return save(j)
        except Exception as e:
            return save(j,error=str(e) if isinstance(e,ValueError) else 'Перевод не подтверждён. Проверяем статус; повтор запрещён.')
    reserve(j,'preview','ordering')
    try:
        d['order_id']=Client(v).order(ident,d['action'],d['quantity'] if d['action']=='sell' else d['total'])
        return save(j)
    except Exception as e:
        return save(j,error=str(e) if isinstance(e,ValueError) else 'Нет подтверждения ордера. Повторная отправка заблокирована.')

def reconcile(v,ident,withdraw_id=None):
    j=job(v,ident); d=j['data']; client=Client(v)
    try:
        if j['state']=='ordering' and d.get('funding_pending'):
            status=client.funding_status(d['funding_id'],d['funding_amount'])
            if status=='FAILED':return save(j,'closed','Внутренний перевод отклонён. Покупка не отправлялась.')
            if status!='SUCCESS':return save(j,error='Ожидается подтверждение перевода USDT на торговый аккаунт.')
            # A delayed transfer must not trigger a stale market purchase.
            if time.time()-j['created']>120:
                return save(j,'closed','USDT переведены на торговый аккаунт. Расчёт истёк: создайте новую покупку.')
            client.address_check(d['address'])
            m=client.market()
            if m['fee']!=d['market']['fee'] or not dec(m['minimum'])<=dec(d['total'])<=dec(m['max_quote']):
                return save(j,'closed','USDT переведены. Торговые условия изменились: пересчитайте покупку.')
            if dec(client.balances().get('USDT',0))<dec(d['total'])*(1+dec(m['fee'])):
                return save(j,error='Перевод подтверждён; ожидается доступный торговый баланс.')
            d['funding_pending']=False;d['funding_completed']=True
            reserve(j,'ordering','ordering') # Persist BEFORE the only order submission.
            d['order_id']=client.order(ident,'buy',d['total'])
            return save(j)
        if j['state']=='ordering':
            r=client.order_status(ident,d['action'],d.get('order_id')); d['execution']=r; d['order_id']=r['id']
            if not r['terminal']:return save(j)
            qty=dec(r['quantity'])
            if not qty:return save(j,'closed','Ордер завершён без исполнения.')
            if d['action']=='sell':return save(j,'done')
            fee=dec(r['fees'].get('BNB',0))
            if fee>=qty:raise ExchangeError('Некорректная комиссия исполнения.')
            d['bought']=fmt(qty-fee)
            return save(j,'bought')
        if j['state']=='withdrawing':
            supplied=withdraw_id or d.get('withdraw_id')
            if v=='bybit' and not supplied:
                raise ExchangeError('Ответ Bybit потерян. Укажите ID вывода из истории Bybit для сверки. Не создавайте повторный вывод.')
            if supplied and not re.fullmatch('[A-Za-z0-9_-]{1,128}',supplied):raise ExchangeError('Неверный ID вывода.')
            rows=client.history(ident,supplied)
            rows=[r for r in rows if (r['id']==supplied if supplied else r.get('request_id')==ident)]
            if len(rows)!=1:raise ExchangeError('Вывод ещё не найден. Повторная отправка заблокирована.')
            r=rows[0]
            if r['coin']!='BNB' or r['network']!='BSC' or r['address'].lower()!=d['address'].lower() or dec(r['amount'])!=dec(d['withdraw_plan']['amount']):
                raise ExchangeError('Вывод не совпал по адресу, сети, монете или сумме.')
            if v=='bybit' and abs(int(r['created'])/1000-d['withdraw_time'])>300:
                raise ExchangeError('Время вывода не совпало с запросом.')
            d['withdraw_id']=r['id']; d['withdrawal']=r
            return save(j,'done' if r['done'] else 'closed' if r['failed'] else 'withdrawing')
        return j
    except Exception as e:
        return save(j,error=str(e) if isinstance(e,ValueError) else 'Не удалось проверить ответ биржи. Операция остаётся заблокированной.')

def withdrawal_plan(v,d):
    client=Client(v); client.address_check(d['address']); n=client.network(); a=client.balances()
    available=min(dec(d['bought']),dec(a.get('BNB',0)))
    amount=round_step(available-dec(n['fee']),n['step'])
    if amount<=0 or amount<dec(n['minimum']) or (n['maximum'] is not None and amount>dec(n['maximum'])):raise ExchangeError('Недостаточно BNB для вывода или превышен лимит.')
    return dict(amount=fmt(amount),fee=n['fee'],network='BSC',address=d['address'],step=n['step'])

def withdraw_preview(v,ident):
    j=job(v,ident)
    if j['state']!='bought':raise ExchangeError('Покупка ещё не подтверждена.')
    j['data']['withdraw_plan']=withdrawal_plan(v,j['data']); j['data']['plan_time']=time.time()
    return save(j)

def withdraw_confirm(v,ident):
    j=job(v,ident); d=j['data']
    if j['state']!='bought' or time.time()-d.get('plan_time',0)>120:raise ExchangeError('Нужен свежий расчёт вывода.')
    if withdrawal_plan(v,d)!=d['withdraw_plan']:raise ExchangeError('Баланс или комиссия изменились. Пересчитайте вывод.')
    d['withdraw_time']=time.time(); reserve(j,'bought','withdrawing')
    try:
        d['withdraw_id']=Client(v).withdraw(ident,d['address'],d['withdraw_plan']['amount'])
        return save(j)
    except Exception as e:
        return save(j,error=str(e) if isinstance(e,ValueError) else 'Ответ вывода не подтверждён. Повторная отправка заблокирована.')

def run(stop):
    from app import db
    while not stop.is_set():
        for v in VENUES:
            try:
                with locked(v):
                    with db() as c:rows=c.execute("SELECT id FROM exchange_jobs WHERE venue=? AND state IN ('ordering','withdrawing')",(v,)).fetchall()
                    for r in rows:reconcile(v,r['id'])
            except Exception:pass
        stop.wait(30)

def install(app):
    from flask import request
    from app import db
    @app.get('/api/exchanges/<v>/jobs')
    def exchanges_jobs(v):
        venue(v)
        with db() as c:rows=c.execute("SELECT id FROM exchange_jobs WHERE venue=? AND state!='preview' ORDER BY created DESC LIMIT 50",(v,)).fetchall()
        return {'items':[job(v,r['id']) for r in rows]}
    @app.post('/api/exchanges/<v>/refresh')
    def exchanges_refresh(v):
        with locked(v):
            client=Client(v); a=client.balances(); warnings=[]; funding=None
            if v=='bybit':
                try:funding=client.funding_balances()
                except ValueError as e:warnings.append('Финансирование: '+str(e))
            try:n=client.network()
            except ValueError as e:n=None; warnings.append(str(e))
            try:h=client.history()
            except ValueError as e:h=[]; warnings.append(str(e))
            return dict(balances=a,funding_balances=funding,network=n,history=h,warnings=warnings,updated=time.time())
    @app.post('/api/exchanges/<v>/preview')
    def exchanges_preview(v):
        with locked(v):return preview(v,request.get_json() or {})
    @app.post('/api/exchanges/<v>/<ident>/<action>')
    def exchanges_action(v,ident,action):
        with locked(v):
            if action=='confirm':return confirm(v,ident)
            if action=='check':return reconcile(v,ident,(request.get_json() or {}).get('withdraw_id'))
            if action=='withdraw-preview':return withdraw_preview(v,ident)
            if action=='withdraw':return withdraw_confirm(v,ident)
            if action=='keep':
                j=job(v,ident)
                if j['state']!='bought':raise ExchangeError('Можно закрыть только исполненную покупку.')
                return save(j,'closed')
            raise ExchangeError('Неизвестное действие.')

    @app.get('/api/exchange-deposits')
    def exchange_deposits():
        from app import settings
        from withdrawals import EXCHANGES
        cfg=settings()
        return {'items':[{'exchange':label,'bnb':cfg.get('withdraw_'+key+'_destination',''),
            'usdt':cfg.get('withdraw_'+key+'_usdt_destination','')} for label,key in EXCHANGES.items()]}

    @app.post('/api/exchange-deposits')
    def exchange_deposits_save():
        from withdrawals import EXCHANGES, destination, MEXC_DESTINATION
        data=request.get_json() or {}; label=data.get('exchange')
        if label not in EXCHANGES:raise ExchangeError('Выберите биржу.')
        values={}
        for asset in ('bnb','usdt'):
            target=str(data.get(asset,'')).strip()
            target=destination(target) if target else ''
            if target==MEXC_DESTINATION:raise ExchangeError('Прежний адрес MEXC больше не используется.')
            if data.get('confirm_'+asset)!=target:raise ExchangeError('Подтвердите полный адрес '+asset.upper())
            key='withdraw_'+EXCHANGES[label]+('_usdt' if asset=='usdt' else '')+'_destination'
            values[key]=target
        if label=='Binance':values['withdraw_destination']=values['withdraw_binance_destination']
        with db() as c:
            for k,v in values.items():c.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',(k,json.dumps(v)))
        return {'ok':True}
