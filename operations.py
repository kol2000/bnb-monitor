"""Read-only diagnostics, priority balance refresh and durable operation notices."""
import ipaddress
import json
import re
import os
import time
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler
from urllib.error import HTTPError, URLError
from email.utils import make_msgid


def schema(c):
    c.executescript('''
    CREATE TABLE IF NOT EXISTS priority_balances (
        address TEXT PRIMARY KEY, min_block INTEGER NOT NULL, requested REAL NOT NULL,
        retry_at REAL NOT NULL DEFAULT 0, error TEXT);
    CREATE TABLE IF NOT EXISTS operation_notices (
        id TEXT PRIMARY KEY, created REAL NOT NULL, text TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'pending', channel TEXT, error TEXT);
    ''')


def status_value(key, default=None):
    from app import db
    with db() as c:
        row = c.execute('SELECT value FROM status WHERE key=?', (key,)).fetchone()
    return json.loads(row['value']) if row else default


def queue_balance(c, address, height=0):
    if not re.fullmatch(r'0x[0-9a-fA-F]{40}', address):
        return
    c.execute('''INSERT INTO priority_balances(address,min_block,requested) VALUES (?,?,?)
        ON CONFLICT(address) DO UPDATE SET min_block=MAX(min_block,excluded.min_block),
        requested=excluded.requested,retry_at=0,error=NULL''', (address.lower(), height, time.time()))


def priority_once():
    from app import db, settings, rpc, RemoteError, record_balance, usdt_balance, usdt_decimals
    cfg = settings()
    with db() as c:
        jobs = [dict(r) for r in c.execute('SELECT * FROM priority_balances WHERE retry_at<=? ORDER BY requested LIMIT 4', (time.time(),))]
    for job in jobs:
        with db() as c:
            wallet = c.execute('SELECT * FROM wallets WHERE lower(address)=?', (job['address'],)).fetchone()
        if not wallet:
            with db() as c:
                c.execute('DELETE FROM priority_balances WHERE address=? AND requested=?', (job['address'],job['requested']))
            continue
        error = 'RPC недоступен для приоритетной проверки.'
        for url in cfg['rpc_urls']:
            try:
                if int(rpc(url,'eth_chainId',[]),16) != 56:
                    raise RemoteError('RPC не в сети BSC.')
                block = rpc(url,'eth_getBlockByNumber',['finalized',False])
                height = int(block['number'],16)
                if height < max(job['min_block'], wallet['block'] or 0, wallet['usdt_block'] or 0):
                    raise RemoteError('Ожидаем блок не старше подтверждённого перевода.')
                if abs(time.time()-int(block['timestamp'],16))>180:
                    raise RemoteError('RPC отстаёт более чем на 3 минуты.')
                value = int(rpc(url,'eth_getBalance',[job['address'],hex(height)]),16)
                record_balance(wallet['id'],value,height,cfg)
                if cfg['usdt_enabled']:
                    usdt_decimals(rpc,url,hex(height))
                    value = usdt_balance(rpc,url,job['address'],hex(height))
                    record_balance(wallet['id'],value,height,cfg,'USDT')
                with db() as c:
                    c.execute('DELETE FROM priority_balances WHERE address=? AND requested=?', (job['address'],job['requested']))
                break
            except Exception as exc:
                error = str(exc) if isinstance(exc,RemoteError) else 'Некорректный ответ RPC при обновлении баланса.'
        else:
            with db() as c:
                c.execute('UPDATE priority_balances SET retry_at=?,error=? WHERE address=? AND requested=?',
                          (time.time()+30,error,job['address'],job['requested']))


def notice(c, ident, text):
    c.execute('INSERT OR IGNORE INTO operation_notices(id,created,text) VALUES (?,?,?)', (ident,time.time(),text))


def collect_notices():
    """Observe new transitions; never flood the user with all historical transfers."""
    from app import db, bnb
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        row = c.execute("SELECT value FROM status WHERE key='operation_notice_start'").fetchone()
        if not row:
            c.execute("INSERT INTO status VALUES ('operation_notice_start',?)", (json.dumps(time.time()),))
            return
        start = json.loads(row['value'])
        for r in c.execute("SELECT * FROM withdrawals WHERE updated>=? AND state IN ('done','pending1','pending2','blocked')",(start,)).fetchall():
            p = json.loads(r['plan'])
            w = c.execute('SELECT name FROM wallets WHERE id=?',(r['wallet_id'],)).fetchone()
            name = w['name'] if w else p['source']
            if r['state']=='done':
                amount = bnb(p['value1'] if p.get('legs',2)==1 else p['value2'])
                tx = r['hash1'] if p.get('legs',2)==1 else r['hash2']
                link = '\nhttps://bscscan.com/tx/'+tx if tx and re.fullmatch(r'0x[0-9a-fA-F]{64}',tx) else ''
                notice(c,'wallet:'+r['id']+':done',f"✅ Перевод BNB завершён\n{name}\n{amount} BNB\nПолучатель: {p['destination']}"+link)
            elif time.time()-r['created']>=900:
                notice(c,'wallet:'+r['id']+':delay',f"⏳ Перевод BNB ещё не завершён\n{name}\nПолучатель: {p['destination']}\nПрошло более 15 минут. Статус — во вкладке «Переводы».")
        for r in c.execute("SELECT * FROM binance_jobs WHERE state IN ('done','withdrawing')").fetchall():
            p = json.loads(r['data'])
            if p.get('updated',r['created']) < start or p.get('action')=='sell':
                continue
            if r['state']=='done':
                tx = p.get('txid','')
                link = '\nhttps://bscscan.com/tx/'+tx if re.fullmatch(r'0x[0-9a-fA-F]{64}',tx) else ''
                wallet = c.execute('SELECT name FROM wallets WHERE lower(address)=?',(p.get('address','').lower(),)).fetchone()
                name = wallet['name'] if wallet else 'Внешний кошелёк'
                notice(c,'binance:'+r['id']+':done',f"✅ Binance: вывод завершён\n{name}\nПолучатель: {p.get('address','')}\nСумма в истории Binance: {p.get('withdraw_actual_amount','—')} BNB\nКомиссия: {p.get('withdraw_actual_fee','—')} BNB"+link)
            elif time.time()-p.get('withdraw_time',time.time())>=900:
                notice(c,'binance:'+r['id']+':delay',f"⏳ Binance: вывод обрабатывается более 15 минут\nПолучатель: {p.get('address','')}\nПодробности — во вкладке «Биржа Binance».")
        # A queued delay notice is obsolete once completion is known.
        c.execute("UPDATE operation_notices SET state='cancelled' WHERE state='pending' AND id LIKE '%:delay' AND substr(id,1,length(id)-5)||'done' IN (SELECT id FROM operation_notices)")


def deliver_notice():
    from app import db, settings, telegram
    from email_notifications import send_email
    cfg = settings()
    channel = 'telegram' if cfg['telegram_enabled'] else 'email' if cfg.get('email_enabled') else None
    if not channel:
        return
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        r = c.execute("SELECT * FROM operation_notices WHERE state='pending' ORDER BY created LIMIT 1").fetchone()
        if not r:
            return
        c.execute("UPDATE operation_notices SET state='sending',channel=? WHERE id=?",(channel,r['id']))
    try:
        if channel=='telegram':
            telegram(cfg,r['text'])
        else:
            send_email(cfg,r['text'],make_msgid())
        state,error='sent',None
    except Exception:
        # Network timeout can occur AFTER delivery. No automatic duplicate or cross-channel send.
        state,error='unknown','Доставка не подтверждена. Автоповтор отключён, чтобы не дублировать сообщение.'
    with db() as c:
        c.execute('UPDATE operation_notices SET state=?,error=? WHERE id=?',(state,error,r['id']))


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):
        return None


def check_ip():
    from app import set_status
    opener = build_opener(ProxyHandler({}),NoRedirect())
    with opener.open('https://api.ipify.org',timeout=10) as r:
        ip = str(ipaddress.IPv4Address(r.read(64).decode().strip()))
    previous = status_value('outbound_ip',{})
    expected = previous.get('expected') or ip
    set_status(outbound_ip={'current':ip,'expected':expected,'checked':time.time(),
                            'changed':ip!=expected,'error':None})
    return ip


def safe_error(exc):
    from app import RemoteError
    from binance import BinanceError
    if isinstance(exc,HTTPError): return f'HTTP {exc.code}'
    if isinstance(exc,TimeoutError): return 'Тайм-аут подключения.'
    if isinstance(exc,URLError): return 'Ошибка соединения: '+type(exc.reason).__name__
    if isinstance(exc,(RemoteError,BinanceError)): return str(exc)
    return 'Некорректный ответ или ошибка соединения: '+type(exc).__name__


def probe(name, fn):
    from app import set_status
    before = status_value('connection_'+name,{})
    at = time.time()
    try:
        detail = fn()
        result = {'checked':at,'success':time.time(),'ok':True,'detail':detail,'error':None}
    except Exception as exc:
        result = {'checked':at,'success':before.get('success'),'ok':False,'error':safe_error(exc)}
    set_status(**{'connection_'+name:result})


def diagnostics():
    from app import settings, rpc, post_json, RemoteError
    import binance
    cfg = settings()
    def check_binance():
        binance.account(binance.Client())
        return 'Подписанный запрос баланса выполнен напрямую.'
    def check_telegram():
        token = cfg['telegram_token']
        if not re.fullmatch(r'\d+:[A-Za-z0-9_-]{20,}',token):
            raise RemoteError('Токен Telegram не настроен.')
        result = post_json('https://api.telegram.org/bot'+token+'/getMe',{})
        if not isinstance(result,dict) or result.get('ok') is not True:
            raise RemoteError('Telegram не подтвердил токен бота.')
        return ('Через заданный HTTP-прокси. ' if os.environ.get('TELEGRAM_PROXY_URL','').strip() else 'Напрямую. ') + 'Проверен getMe; сообщение не отправлялось.'
    def check_rpc():
        failures=[]
        for n,url in enumerate(cfg['rpc_urls'],1):
            try:
                if int(rpc(url,'eth_chainId',[]),16)!=56:
                    raise RemoteError('Неверная сеть.')
                block=rpc(url,'eth_getBlockByNumber',['finalized',False])
                if abs(time.time()-int(block['timestamp'],16))>180:
                    raise RemoteError('Устаревший finalized-блок.')
                raw=rpc(url,'eth_getBalance',['0x000000000000000000000000000000000000dead',block['number']])
                int(raw,16)
                return f'RPC №{n}: BSC, finalized и баланс доступны.' + (' Предыдущие: '+'; '.join(failures) if failures else '')
            except Exception as exc:
                failures.append(f'№{n}: '+safe_error(exc))
        raise RemoteError('; '.join(failures) or 'Нет RPC.')
    def check_google():
        if not cfg['sheets_enabled']:
            return 'Синхронизация отключена в настройках.'
        from sheets_sync import fetch_rows
        fetch_rows(cfg)  # A:B only; no private material or writes.
        return 'Активный лист доступен для чтения. Права записи здесь не проверяются.'
    for name,fn in [('binance',check_binance),('telegram',check_telegram),('rpc',check_rpc),('google',check_google)]:
        probe(name,fn)
    try:
        check_ip()
    except Exception as exc:
        from app import set_status
        value=status_value('outbound_ip',{})
        value.update(checked=time.time(),error=safe_error(exc))
        set_status(outbound_ip=value)


def run(stop):
    from app import db, set_status
    with db() as c:
        c.execute("UPDATE operation_notices SET state='unknown',error='Процесс прервался во время отправки; повтор не выполняется.' WHERE state='sending'")
    while not stop.is_set():
        try:
            priority_once()
            collect_notices()
            deliver_notice()
        except Exception:
            set_status(operations_error='Ошибка фоновой обработки операций. Проверьте состояние диска.')
        stop.wait(2)


def diagnostics_loop(stop):
    from app import set_status
    while not stop.is_set():
        requested=status_value('diagnostics_requested',False)
        last=status_value('diagnostics_at',0)
        if requested or time.time()-last>=300:
            set_status(diagnostics_requested=False,diagnostics_running=True,diagnostics_at=time.time())
            try: diagnostics()
            except Exception:
                set_status(diagnostics_error='Проверка прервана; повтор через 5 минут.')
            finally: set_status(diagnostics_running=False)
        stop.wait(2)


def install(app):
    from flask import request
    from app import set_status,db
    @app.get('/api/operations')
    def read():
        with db() as c:
            notices=[dict(r) for r in c.execute('SELECT id,created,state,channel,error,text FROM operation_notices ORDER BY created DESC LIMIT 30')]
            pending=[dict(r) for r in c.execute('SELECT address,requested,error FROM priority_balances ORDER BY requested LIMIT 20')]
        return {'connections':{name:status_value('connection_'+name,{}) for name in ('binance','telegram','rpc','google')},
                'ip':status_value('outbound_ip',{}),'backup':status_value('backup_report',{}),
                'running':status_value('diagnostics_running',False),'notices':notices,'priority':pending}
    @app.post('/api/diagnostics')
    def trigger():
        set_status(diagnostics_requested=True)
        return {'ok':True}
    @app.post('/api/ip/acknowledge')
    def acknowledge():
        ip=status_value('outbound_ip',{})
        supplied=(request.get_json() or {}).get('ip')
        if ip.get('error') or time.time()-ip.get('checked',0)>600 or supplied!=ip.get('current'):
            raise ValueError('Сначала выполните свежую проверку IP.')
        ip.update(expected=supplied,changed=False)
        set_status(outbound_ip=ip)
        return {'ok':True}
