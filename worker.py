import signal
import threading
import time
from app import DATA, RemoteError, bnb, db, record_balance, rpc, settings, set_status, telegram
from sheets_sync import sync_once

stop = threading.Event()

def heartbeat():
    (DATA / 'heartbeat').touch()
    set_status(worker_seen=time.time())

def check_once():
    cfg = settings()
    set_status(check_requested=False, running=True, cycle_started=time.time())
    try:
        with db() as c:
            wallets = [dict(r) for r in c.execute('SELECT * FROM wallets ORDER BY id')]
        if not wallets:
            set_status(last_cycle=time.time(), error=None, wallet_count=0)
            return
        remaining = {w['id']: w for w in wallets}
        last_error = 'RPC недоступен.'
        used = None
        for index, url in enumerate(cfg['rpc_urls']):
            if stop.is_set():
                break
            try:
                heartbeat()
                if int(rpc(url, 'eth_chainId', []), 16) != 56:
                    raise RemoteError('RPC подключён не к BSC mainnet (chain ID 56).')
                block = rpc(url, 'eth_getBlockByNumber', ['finalized', False])
                if not isinstance(block, dict):
                    raise RemoteError('RPC не поддерживает finalized. Укажите другой RPC.')
                height = int(block['number'], 16)
                if abs(time.time() - int(block['timestamp'], 16)) > 180:
                    raise RemoteError('RPC отстаёт более чем на 3 минуты или часы сервера неверны.')
                for wid, w in list(remaining.items()):
                    if stop.is_set():
                        break
                    heartbeat()
                    try:
                        if w['block'] is not None and height < w['block']:
                            raise RemoteError('RPC отстаёт от ранее проверенного блока.')
                        if w['block'] is not None and height == w['block']:
                            with db() as c:
                                c.execute('UPDATE wallets SET checked=?,error=NULL WHERE id=?', (time.time(), wid))
                        else:
                            raw = rpc(url, 'eth_getBalance', [w['address'], hex(height)])
                            if not isinstance(raw, str) or not raw.startswith('0x'):
                                raise RemoteError('RPC вернул некорректный баланс.')
                            record_balance(wid, int(raw, 16), height, cfg)
                        remaining.pop(wid)
                        used = index + 1
                    except (RemoteError, ValueError, KeyError, TypeError) as exc:
                        last_error = str(exc) if isinstance(exc, RemoteError) else 'Некорректный ответ RPC.'
                    stop.wait(0.1)
            except (RemoteError, ValueError, KeyError, TypeError) as exc:
                last_error = str(exc) if isinstance(exc, RemoteError) else 'Некорректный ответ RPC.'
            if not remaining:
                break
        if remaining:
            with db() as c:
                for wid in remaining:
                    c.execute('UPDATE wallets SET error=? WHERE id=?', (last_error, wid))
        set_status(last_cycle=time.time(), error=last_error if remaining else None,
                   rpc_index=used, wallet_count=len(wallets), failed=len(remaining))
    finally:
        set_status(running=False)

def deliver():
    cfg = settings()
    if not cfg['telegram_enabled']:
        return
    with db() as c:
        events = [dict(r) for r in c.execute("SELECT * FROM events WHERE delivery='pending' AND retry_at<=? ORDER BY id LIMIT 20", (time.time(),))]
    for e in events:
        if stop.is_set():
            break
        heartbeat()
        with db() as c:
            w = c.execute('SELECT notify FROM wallets WHERE id=?', (e['wallet_id'],)).fetchone()
            current = c.execute('SELECT delivery FROM events WHERE id=?', (e['id'],)).fetchone()
            if not current or current['delivery'] != 'pending':
                continue
            if not w or not w['notify'] or cfg['telegram_chat'] != e['chat']:
                c.execute("UPDATE events SET delivery='cancelled' WHERE id=?", (e['id'],))
                continue
        text = (f"🟢 Увеличение баланса BNB · событие #{e['id']}\n"
                f"{e['name']}\n{e['address']}\n\n"
                f"Изменение: +{bnb(e['delta'])} BNB\nБаланс: {bnb(e['new'])} BNB\n"
                f"Блок: {e['block']}\nhttps://bscscan.com/address/{e['address']}\n\n"
                'Это разница балансов между проверками, не сумма отдельной транзакции.')
        try:
            telegram(cfg, text, e['chat'])
            with db() as c:
                c.execute("UPDATE events SET delivery='sent',delivery_error=NULL WHERE id=?", (e['id'],))
        except RemoteError as exc:
            with db() as c:
                c.execute('UPDATE events SET attempts=attempts+1,retry_at=?,delivery_error=? WHERE id=?',
                          (time.time() + min(3600, 30 * 2**min(e['attempts'], 7)), str(exc), e['id']))
        stop.wait(1.1)

def run():
    # One worker per database; prevent accidental duplicate notification workers.
    import fcntl
    with (DATA / 'worker.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        next_check = 0
        next_sync = 0
        previous_source = None
        while not stop.is_set():
            heartbeat()
            set_status(worker_seen=time.time())
            try:
                cfg = settings()
                source = (cfg['sheets_enabled'], cfg['sheets_id'], cfg['sheets_tab'], cfg['sheets_interval'])
                if source != previous_source:
                    next_sync = 0
                    previous_source = source
                with db() as c:
                    sync_request = c.execute("SELECT value FROM status WHERE key='sheets_requested'").fetchone()
                if cfg['sheets_enabled'] and (time.time() >= next_sync or (sync_request and sync_request['value'] == 'true')):
                    sync_once()
                    next_sync = time.time() + cfg['sheets_interval']
                    heartbeat()
                with db() as c:
                    row = c.execute("SELECT value FROM status WHERE key='check_requested'").fetchone()
                if time.time() >= next_check or (row and row['value'] == 'true'):
                    check_once()
                    next_check = time.time() + settings()['interval']
                deliver()
            except Exception:
                # Do not log exceptions containing credentials or network URLs.
                set_status(error='Внутренняя ошибка мониторинга. Проверьте диск и перезапустите worker.', running=False)
                next_check = time.time() + 30
            stop.wait(2)

if __name__ == '__main__':
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    run()
