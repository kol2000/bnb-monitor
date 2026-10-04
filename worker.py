import json
from decimal import Decimal, InvalidOperation
import signal
import threading
import time
from app import ASSET_FIELDS, usdt_balance, usdt_decimals, DATA, RemoteError, bnb, db, record_balance, rpc, settings, set_status, telegram
from sheets_sync import sync_once, export_balances
from prices import fetch_quote, usd
from email_notifications import send_email, EmailError

stop = threading.Event()
# Serialize channel selection and network delivery so the two loops cannot
# deliver the same event concurrently. The worker process already holds flock.
delivery_lock = threading.RLock()
EMAIL_FALLBACK_ATTEMPTS = 3

def serialized_delivery(fn):
    from functools import wraps
    @wraps(fn)
    def wrapped(*args, **kwargs):
        with delivery_lock:
            return fn(*args, **kwargs)
    return wrapped

def heartbeat():
    (DATA / 'heartbeat').touch()
    set_status(worker_seen=time.time())

def refresh_price():
    try:
        price, timestamp = fetch_quote()
        if timestamp > time.time() + 300 or time.time() - timestamp > 900:
            raise ValueError('Stale quote')
        set_status(bnb_usd=price, price_at=timestamp, price_error=None)
    except Exception:
        # Preserve the previous quote and its original timestamp on any failure.
        set_status(price_error='Не удалось обновить курс CoinMarketCap.')

def check_once():
    cfg = settings()
    set_status(check_requested=False, running=True, cycle_started=time.time())
    try:
        with db() as c:
            wallets = [dict(r) for r in c.execute('SELECT * FROM wallets ORDER BY id')]
        if not wallets:
            set_status(last_cycle=time.time(), error=None, wallet_count=0)
            return
        assets = ['BNB', 'USDT'] if cfg['usdt_enabled'] else ['BNB']
        remaining = {(w['id'], asset): w for w in wallets for asset in assets}
        asset_errors = {}

        last_error = 'RPC недоступен.'
        used = None
        for index, url in enumerate(cfg['rpc_urls']):
            if stop.is_set():
                break
            try:
                heartbeat()
                if int(rpc(url, 'eth_chainId', []), 16) != 56:
                    raise RemoteError('RPC подключён не к BSC mainnet (chain ID 56).')
                token_verified = False
                for (wid, asset), w in list(remaining.items()):
                    balance_col, block_col, checked_col, error_col = ASSET_FIELDS[asset]
                    if stop.is_set():
                        break
                    heartbeat()
                    try:
                        if asset == 'USDT' and not token_verified:
                            usdt_decimals(rpc, url)
                            token_verified = True
                        # Public RPCs may discard old state during a long wallet sweep.
                        # Keep the balance tied to an explicit, freshly finalized height.
                        block = rpc(url, 'eth_getBlockByNumber', ['finalized', False])
                        if not isinstance(block, dict):
                            raise RemoteError('RPC не поддерживает finalized. Укажите другой RPC.')
                        height = int(block['number'], 16)
                        if abs(time.time() - int(block['timestamp'], 16)) > 180:
                            raise RemoteError('RPC отстаёт более чем на 3 минуты или часы сервера неверны.')
                        if w[block_col] is not None and height < w[block_col]:
                            raise RemoteError('RPC отстаёт от ранее проверенного блока.')
                        if w[block_col] is not None and height == w[block_col]:
                            with db() as c:
                                c.execute(f'UPDATE wallets SET {checked_col}=?,{error_col}=NULL WHERE id=?', (time.time(), wid))
                        else:
                            if asset == 'USDT':
                                amount = usdt_balance(rpc, url, w['address'], hex(height))
                            else:
                                raw = rpc(url, 'eth_getBalance', [w['address'], hex(height)])
                                if not isinstance(raw, str) or not raw.startswith('0x'):
                                    raise RemoteError('RPC вернул некорректный баланс.')
                                amount = int(raw, 16)
                            record_balance(wid, amount, height, cfg, asset)
                        remaining.pop((wid, asset))
                        used = index + 1
                    except (RemoteError, ValueError, KeyError, TypeError) as exc:
                        last_error = str(exc) if isinstance(exc, RemoteError) else 'Некорректный ответ RPC.'
                        asset_errors[(wid, asset)] = last_error
                    # Pace public RPC requests: one second plus response time per wallet.
                    stop.wait(1.0)
            except (RemoteError, ValueError, KeyError, TypeError) as exc:
                last_error = str(exc) if isinstance(exc, RemoteError) else 'Некорректный ответ RPC.'
            if not remaining:
                break
        # SIGTERM during a deployment is not a failed RPC check. Preserve the
        # unvisited wallets and the last completed cycle; finally clears running.
        if stop.is_set():
            return
        if remaining:
            with db() as c:
                for wid, asset in remaining:
                    error_col = ASSET_FIELDS[asset][3]
                    c.execute(f'UPDATE wallets SET {error_col}=? WHERE id=?',
                              (asset_errors.get((wid, asset), last_error), wid))
        set_status(last_cycle=time.time(), error=('; '.join(sorted({asset for _, asset in remaining})) + ': ' + last_error) if remaining else None,
                   rpc_index=used, wallet_count=len(wallets), failed=len(remaining))
    finally:
        set_status(running=False)

def notification_usd(delta):
    """Use the cached quote at delivery time; missing USD must not block BNB alerts."""
    with db() as c:
        rows = c.execute("SELECT key,value FROM status WHERE key IN ('bnb_usd','price_at')").fetchall()
    try:
        quote = {r['key']: json.loads(r['value']) for r in rows}
        price = Decimal(str(quote.get('bnb_usd')))
        timestamp = float(quote.get('price_at'))
        now = time.time()
        if not price.is_finite() or price <= 0 or not 0 < timestamp <= now + 300:
            raise ValueError('Invalid cached quote')
        amount = usd(delta, str(price))
        stale = ', курс устарел' if now - timestamp > 900 else ''
        return f" (≈ {amount}{stale})"
    except (ValueError, TypeError, InvalidOperation):
        return ' (USD: курс недоступен)'

def notification_text(e, html_format=False):
    asset = e.get('asset', 'BNB')
    valuation = notification_usd(e['delta']) if asset == 'BNB' else ''
    prefix = (f"🟢 Увеличение баланса {asset} · событие #{e['id']}\n"
              f"{e['name']}\n{e['address']}\n\n"
              f"Изменение: +{bnb(e['delta'])} {asset}{valuation}\n")
    balance = f"Баланс: {bnb(e['new'])} {asset}"
    suffix = f"\n\nБлок: {e['block']}\nhttps://bscscan.com/address/{e['address']}"
    if html_format:
        from html import escape
        return escape(prefix) + '<b>' + escape(balance) + '</b>' + escape(suffix)
    return prefix + balance + suffix

@serialized_delivery
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
        cfg = settings()
        if not cfg['telegram_enabled']:
            break
        with db() as c:
            w = c.execute('SELECT notify FROM wallets WHERE id=?', (e['wallet_id'],)).fetchone()
            current = c.execute('SELECT delivery FROM events WHERE id=?', (e['id'],)).fetchone()
            if not current or current['delivery'] != 'pending':
                continue
            if not w or not w['notify'] or cfg['telegram_chat'] != e['chat']:
                c.execute("UPDATE events SET delivery='cancelled' WHERE id=?", (e['id'],))
                continue
        text = notification_text(e, html_format=True)
        try:
            telegram(cfg, text, e['chat'], html_format=True)
            with db() as c:
                c.execute("UPDATE events SET delivery='sent',delivery_error=NULL,retry_at=0 WHERE id=?", (e['id'],))
                c.execute("UPDATE email_outbox SET delivery='suppressed',delivery_error=NULL,retry_at=0 WHERE event_id=? AND delivery='pending'", (e['id'],))
        except RemoteError as exc:
            with db() as c:
                c.execute('UPDATE events SET attempts=attempts+1,retry_at=?,delivery_error=? WHERE id=?',
                          (time.time() + min(3600, 30 * 2**min(e['attempts'], 7)), str(exc), e['id']))
        stop.wait(1.1)

@serialized_delivery
def deliver_email():
    cfg = settings()
    with db() as c:
        # Also reconcile the existing queue after upgrading, regardless of retry_at.
        c.execute("""UPDATE email_outbox SET delivery='suppressed',delivery_error=NULL,retry_at=0
            WHERE delivery='pending' AND event_id IN
            (SELECT id FROM events WHERE delivery='sent')""")
        events = [dict(r) for r in c.execute("""SELECT e.*, m.recipient, m.sender,
            m.message_id, m.attempts AS email_attempts FROM email_outbox m
            JOIN events e ON e.id=m.event_id
            WHERE m.delivery='pending' AND m.retry_at<=?
            AND e.delivery!='sent'
            AND (?=0 OR e.delivery='off' OR (e.delivery='pending' AND e.attempts>=?))
            ORDER BY e.id LIMIT 20""", (time.time(), int(cfg['telegram_enabled']), EMAIL_FALLBACK_ATTEMPTS))]
    for e in events:
        if stop.is_set():
            return
        cfg = settings()
        with db() as c:
            current = c.execute('SELECT delivery FROM email_outbox WHERE event_id=?', (e['id'],)).fetchone()
            event = c.execute('SELECT delivery,attempts FROM events WHERE id=?', (e['id'],)).fetchone()
            if not event or event['delivery'] == 'sent':
                continue
            if cfg['telegram_enabled'] and event['delivery'] != 'off':
                if event['delivery'] != 'pending' or event['attempts'] < EMAIL_FALLBACK_ATTEMPTS:
                    continue
            w = c.execute('SELECT notify FROM wallets WHERE id=?', (e['wallet_id'],)).fetchone()
            if not current or current['delivery'] != 'pending':
                continue
            if (not cfg['email_enabled'] or not w or not w['notify']
                    or cfg['email_to'] != e['recipient'] or cfg['email_from'] != e['sender']):
                c.execute("UPDATE email_outbox SET delivery='cancelled' WHERE event_id=?", (e['id'],))
                continue
        try:
            send_email(cfg, notification_text(e), e['message_id'])
            with db() as c:
                c.execute("UPDATE email_outbox SET delivery='sent',delivery_error=NULL,retry_at=0 WHERE event_id=?", (e['id'],))
                c.execute("UPDATE events SET delivery='cancelled',delivery_error=NULL,retry_at=0 WHERE id=? AND delivery='pending'", (e['id'],))
            set_status(email_last_sent=time.time(), email_error=None)
        except EmailError as exc:
            with db() as c:
                c.execute("""UPDATE email_outbox SET attempts=attempts+1,retry_at=?,delivery_error=?
                             WHERE event_id=? AND delivery='pending'""",
                          (time.time() + min(3600, 30 * 2**min(e['email_attempts'], 7)), str(exc), e['id']))
            set_status(email_error=str(exc))
        stop.wait(1.1)

    with db() as c:
        pending = c.execute("SELECT 1 FROM email_outbox WHERE delivery='pending' LIMIT 1").fetchone()
    if not pending:
        set_status(email_error=None)

def email_loop():
    # Separate from RPC sweeps; channel delivery shares a lock.
    while not stop.is_set():
        try:
            deliver_email()
        except Exception:
            set_status(email_error='Ошибка очереди email. Проверьте диск и перезапустите worker.')
        stop.wait(2)

def run():
    # One worker per database; prevent accidental duplicate notification workers.
    import fcntl
    with (DATA / 'worker.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        email_thread = threading.Thread(target=email_loop, name='email-delivery', daemon=True)
        email_thread.start()
        next_check = 0
        next_sync = 0
        next_price = 0
        previous_source = None
        while not stop.is_set():
            heartbeat()
            set_status(worker_seen=time.time())
            try:
                cfg = settings()
                if time.time() >= next_price:
                    refresh_price()
                    next_price = time.time() + 300
                    heartbeat()
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
                    cycle_started = time.time()
                    check_once()
                    if not stop.is_set():
                        export_balances(cycle_started)
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



