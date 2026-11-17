import json
import errno
import sqlite3
from decimal import Decimal, InvalidOperation
import signal
import threading
import time
from urllib.parse import urlsplit
from app import ASSET_FIELDS, usdt_balance, usdt_decimals, DATA, RemoteError, bnb, db, record_balance, rpc, settings, set_status, telegram
from sheets_sync import sync_once, export_balances
from prices import fetch_quote, usd
import withdrawals
import swaps
import operations
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

def retry_rpc_read(url, method, params, on_recovered=None):
    """One bounded retry for a transient RPC timeout; read-only methods only.

    Never retry writes, transaction submissions, or arbitrary RPC methods.
    """
    if method not in ('eth_chainId', 'eth_getBlockByNumber',
                      'eth_getBalance', 'eth_call'):
        return rpc(url, method, params)
    try:
        return rpc(url, method, params)
    except RemoteError as exc:
        if not any(marker in str(exc) for marker in ('Тайм-аут подключения к сервису', 'Ошибка соединения: TimeoutError')):
            raise
        if stop.is_set():
            raise
        stop.wait(0.25)
        if stop.is_set():
            raise
        result = rpc(url, method, params)
        if on_recovered is not None:
            on_recovered()
        return result


FINALIZED_CACHE_SECONDS = 5.0
FINALIZED_CACHE_MAX_USES = 8


class FinalizedBlockCache:
    """Short-lived, endpoint-local finalized height for balance reads.

    A bounded cache avoids fetching the same finalized block per asset.
    Invalidate and retry a *read* once if the provider rejects cached state.
    """

    def __init__(self, url, on_recovered=None):
        self.url = url
        self.on_recovered = on_recovered
        self.height = None
        self.timestamp = None
        self.fetched_at = 0.0
        self.uses = 0

    def invalidate(self):
        self.height = None
        self.timestamp = None
        self.uses = 0

    def get(self):
        now = time.monotonic()
        if (self.height is not None
                and now - self.fetched_at < FINALIZED_CACHE_SECONDS
                and self.uses < FINALIZED_CACHE_MAX_USES
                and abs(time.time() - self.timestamp) <= 180):
            self.uses += 1
            return self.height, True
        self.invalidate()
        block = retry_rpc_read(self.url, 'eth_getBlockByNumber', ['finalized', False], self.on_recovered)
        if not isinstance(block, dict):
            raise RemoteError('RPC не поддерживает finalized. Укажите другой RPC.')
        try:
            height = int(block['number'], 16)
            timestamp = int(block['timestamp'], 16)
        except (KeyError, TypeError, ValueError):
            raise RemoteError('RPC вернул некорректный финализированный блок.') from None
        if height < 0 or abs(time.time() - timestamp) > 180:
            raise RemoteError('RPC отстаёт более чем на 3 минуты или часы сервера неверны.')
        self.height = height
        self.timestamp = timestamp
        self.fetched_at = time.monotonic()
        self.uses = 1
        return height, False


def finalized_asset_sample(cache, w, asset, block_col):
    """Read at an explicit finalized height; retry once on cached-state failure."""
    for attempt in range(2):
        height, reused = cache.get()
        prior = w[block_col]
        if prior is not None and height < prior:
            # A previous endpoint (or manual check) advanced farther.
            # Refresh a reused height before failing over to the next RPC.
            if reused and attempt == 0:
                cache.invalidate()
                continue
            raise RemoteError('RPC отстаёт от ранее проверенного блока.')
        if prior is not None and height == prior:
            return height, None
        try:
            if asset == 'USDT':
                amount = usdt_balance(lambda url, method, params: retry_rpc_read(url, method, params, cache.on_recovered), cache.url, w['address'], hex(height))
            else:
                raw = retry_rpc_read(cache.url, 'eth_getBalance', [w['address'], hex(height)], cache.on_recovered)
                if not isinstance(raw, str) or not raw.startswith('0x'):
                    raise RemoteError('RPC вернул некорректный баланс.')
                amount = int(raw, 16)
            return height, amount
        except RemoteError:
            if reused and attempt == 0:
                cache.invalidate()
                continue
            raise
    raise RemoteError('Не удалось прочитать баланс с финализированного блока.')


def check_once():
    cfg = settings()
    started = time.time()
    set_status(check_requested=False, running=True, cycle_started=started, cycle_done=0, cycle_total=0)
    try:
        with db() as c:
            wallets = [dict(r) for r in c.execute('SELECT * FROM wallets ORDER BY id')]
        if not wallets:
            set_status(last_full_cycle=time.time(), last_cycle=time.time(), error=None, wallet_count=0)
            return
        assets = ['BNB', 'USDT'] if cfg['usdt_enabled'] else ['BNB']
        remaining = {(w['id'], asset): w for w in wallets for asset in assets}
        asset_errors = {}
        # Per-cycle counts: asset attempts, successful checks, failures and
        # read timeouts recovered before changing endpoints.
        rpc_stats = [{'rpc': index + 1, 'host': urlsplit(url).hostname or 'unknown',
                      'attempts': 0, 'success': 0, 'errors': 0, 'retry_recovered': 0,
                      'failover_recovered': 0, 'skipped': False}
                     for index, url in enumerate(cfg['rpc_urls'])]
        set_status(rpc_cycle_stats=rpc_stats)

        last_error = 'RPC недоступен.'
        used = None
        with db() as c:
            r = c.execute("SELECT value FROM status WHERE key='rpc_health'").fetchone()
            rpc_health = json.loads(r['value']) if r else {}
        for index, url in enumerate(cfg['rpc_urls']):
            if stop.is_set():
                break
            import hashlib
            key = hashlib.sha256(url.encode()).hexdigest()[:16]
            health = rpc_health.get(key, {})
            if health.get('until', 0) > time.time():
                rpc_stats[index]['skipped'] = True
                continue
            consecutive = 0
            provider_stats = rpc_stats[index]
            def recovered():
                provider_stats['retry_recovered'] += 1
            try:
                heartbeat()
                if int(retry_rpc_read(url, 'eth_chainId', [], recovered), 16) != 56:
                    raise RemoteError('RPC подключён не к BSC mainnet (chain ID 56).')
                token_verified = False
                finalized_cache = FinalizedBlockCache(url, recovered)
                for (wid, asset), w in list(remaining.items()):
                    balance_col, block_col, checked_col, error_col = ASSET_FIELDS[asset]
                    if stop.is_set():
                        break
                    heartbeat()
                    provider_stats['attempts'] += 1
                    try:
                        if asset == 'USDT' and not token_verified:
                            usdt_decimals(lambda endpoint, method, params: retry_rpc_read(endpoint, method, params, recovered), url)
                            token_verified = True
                        height, amount = finalized_asset_sample(finalized_cache, w, asset, block_col)
                        if amount is None:
                            with db() as c:
                                c.execute(f'UPDATE wallets SET {checked_col}=?,{error_col}=NULL WHERE id=?', (time.time(), wid))
                        else:
                            record_balance(wid, amount, height, cfg, asset)
                        consecutive = 0
                        rpc_health[key] = {'failures': 0, 'until': 0}
                        remaining.pop((wid, asset))
                        provider_stats['success'] += 1
                        if (wid, asset) in asset_errors:
                            provider_stats['failover_recovered'] += 1
                            asset_errors.pop((wid, asset), None)
                        used = index + 1
                        set_status(cycle_done=len(wallets)*len(assets)-len(remaining), cycle_total=len(wallets)*len(assets))
                    except (RemoteError, ValueError, KeyError, TypeError) as exc:
                        last_error = str(exc) if isinstance(exc, RemoteError) else 'Некорректный ответ RPC.'
                        asset_errors[(wid, asset)] = last_error
                        provider_stats['errors'] += 1
                        consecutive += 1
                        if consecutive >= 3:
                            failures = min(health.get('failures', 0) + 1, 5)
                            rpc_health[key] = {'failures': failures, 'until': time.time()+min(300, 15*2**failures)}
                            break
                    # Keep backoff on consecutive RPC failures; successful requests
                    # use a configurable delay to balance speed and public-RPC quotas.
                    delay = cfg['rpc_success_delay'] if consecutive == 0 else min(8.0, 2.0 ** consecutive)
                    stop.wait(delay)
            except (RemoteError, ValueError, KeyError, TypeError) as exc:
                last_error = str(exc) if isinstance(exc, RemoteError) else 'Некорректный ответ RPC.'
                failures = min(health.get('failures', 0) + 1, 5)
                rpc_health[key] = {'failures': failures, 'until': time.time()+min(300,15*2**failures)}
            set_status(rpc_cycle_stats=rpc_stats, rpc_health=rpc_health, cycle_done=len(wallets)*len(assets)-len(remaining), cycle_total=len(wallets)*len(assets))
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
        if not remaining:
            set_status(last_full_cycle=time.time())
        set_status(rpc_cycle_stats=rpc_stats, last_cycle=time.time(), error=('; '.join(sorted({asset for _, asset in remaining})) + ': ' + last_error) if remaining else None,
                   rpc_index=used, wallet_count=len(wallets), failed=len(remaining))
    finally:
        set_status(running=False, last_cycle_duration=time.time()-started)

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
    if cfg.get('telegram_digest'):
        old = []
        with db() as c:
            for e in events:
                w = c.execute('SELECT notify FROM wallets WHERE id=?', (e['wallet_id'],)).fetchone()
                if e['created'] < time.time()-3600 and w and w['notify'] and e['chat'] == cfg['telegram_chat']:
                    old.append(e)
        if len(old) >= 3:
            from html import escape
            text = '<b>Сводка накопившихся изменений баланса</b>\n' + '\n'.join(
                f"#{e['id']} · {escape(e['name'][:50])}: +{bnb(e['delta'])} {e['asset']}" for e in old)
            text += '\nПодробности и текущие балансы — в BNB Monitor.'
            ids = [e['id'] for e in old]
            try:
                telegram(cfg, text, cfg['telegram_chat'], html_format=True)
                with db() as c:
                    c.executemany("UPDATE events SET delivery='sent',delivery_error=NULL,retry_at=0 WHERE id=? AND delivery='pending'", [(i,) for i in ids])
                    c.executemany("UPDATE email_outbox SET delivery='suppressed',delivery_error=NULL,retry_at=0 WHERE event_id=? AND delivery='pending'", [(i,) for i in ids])
                set_status(telegram_digest_last={'at': time.time(), 'events': ids})
            except RemoteError as exc:
                with db() as c:
                    for e in old:
                        c.execute('UPDATE events SET attempts=attempts+1,retry_at=?,delivery_error=? WHERE id=?',
                                  (time.time()+min(3600,30*2**min(e['attempts'],7)), str(exc), e['id']))
            events = [e for e in events if e['id'] not in ids]
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

def telegram_loop():
    while not stop.is_set():
        try:
            deliver()
        except Exception:
            set_status(telegram_error='Ошибка очереди Telegram. Проверьте worker.')
        stop.wait(2)


def email_loop():
    # Separate from RPC sweeps; channel delivery shares a lock.
    while not stop.is_set():
        try:
            deliver_email()
        except Exception:
            set_status(email_error='Ошибка очереди email. Проверьте диск и перезапустите worker.')
        stop.wait(2)

def monitoring_error(stage, exc):
    # Never expose exception text: it can contain URLs, tokens or SQL values.
    if isinstance(exc, OSError) and exc.errno == errno.ENOSPC:
        cause = 'на диске нет свободного места'
    elif isinstance(exc, PermissionError):
        cause = 'нет прав доступа к файлу или каталогу'
    elif isinstance(exc, sqlite3.Error):
        code = getattr(exc, 'sqlite_errorcode', 0) or 0
        cause = {sqlite3.SQLITE_FULL: 'нет места для записи в базу данных',
                 sqlite3.SQLITE_BUSY: 'база данных занята',
                 sqlite3.SQLITE_LOCKED: 'база данных заблокирована',
                 sqlite3.SQLITE_READONLY: 'база данных доступна только для чтения',
                 sqlite3.SQLITE_CORRUPT: 'повреждена база данных'}.get(
                     code & 255, 'ошибка доступа или записи в базу данных')
    elif isinstance(exc, (TimeoutError, ConnectionError)):
        cause = 'тайм-аут или ошибка сетевого соединения'
    else:
        cause = 'внутренняя ошибка обработки данных (' + type(exc).__name__ + ')'
    return f'{stage}: {cause}. Цикл прерван; фоновый процесс повторит проверку автоматически.'

def run():
    # One worker per database; prevent accidental duplicate notification workers.
    import fcntl
    with (DATA / 'worker.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        threading.Thread(target=operations.run, args=(stop,), name='operation-notices', daemon=True).start()
        threading.Thread(target=operations.diagnostics_loop, args=(stop,), name='connection-diagnostics', daemon=True).start()
        email_thread = threading.Thread(target=email_loop, name='email-delivery', daemon=True)
        email_thread.start()
        threading.Thread(target=telegram_loop, name='telegram-delivery', daemon=True).start()
        threading.Thread(target=swaps.run, args=(stop,), name='wallet-swaps', daemon=True).start()
        threading.Thread(target=withdrawals.run, args=(stop,), name='bnb-withdrawals', daemon=True).start()
        import binance, maintenance, exchanges, exchange_usdt
        threading.Thread(target=exchange_usdt.run, args=(stop,), name='usdt-withdrawals', daemon=True).start()
        threading.Thread(target=exchanges.run, args=(stop,), name='exchange-status', daemon=True).start()
        threading.Thread(target=binance.run, args=(stop,), name='binance-status', daemon=True).start()
        threading.Thread(target=maintenance.run, args=(stop,), name='seed-audit', daemon=True).start()
        next_check = 0
        next_sync = 0
        next_price = 0
        previous_source = None
        while not stop.is_set():
            heartbeat()
            set_status(worker_seen=time.time())
            stage = 'Чтение настроек мониторинга'
            try:
                cfg = settings()
                if time.time() >= next_price:
                    stage = 'Обновление курса BNB (CoinMarketCap)'
                    refresh_price()
                    next_price = time.time() + 300
                    heartbeat()
                stage = 'Подготовка синхронизации Google Таблицы'
                source = (cfg['sheets_enabled'], cfg['sheets_id'], cfg['sheets_tab'], cfg['sheets_interval'])
                if source != previous_source:
                    next_sync = 0
                    previous_source = source
                with db() as c:
                    sync_request = c.execute("SELECT value FROM status WHERE key='sheets_requested'").fetchone()
                if cfg['sheets_enabled'] and (time.time() >= next_sync or (sync_request and sync_request['value'] == 'true')):
                    stage = 'Импорт кошельков из Google Таблицы'
                    sync_once()
                    next_sync = time.time() + cfg['sheets_interval']
                    heartbeat()
                stage = 'Чтение очереди проверки балансов'
                with db() as c:
                    row = c.execute("SELECT value FROM status WHERE key='check_requested'").fetchone()
                if time.time() >= next_check or (row and row['value'] == 'true'):
                    cycle_started = time.time()
                    stage = 'Проверка балансов BNB / USDT в сети BSC'
                    check_once()
                    if not stop.is_set():
                        stage = 'Запись балансов в Google Таблицу'
                        export_balances(cycle_started)
                    next_check = time.time() + settings()['interval']
            except Exception as exc:
                set_status(error=monitoring_error(stage, exc), running=False)
                next_check = time.time() + 30
            stop.wait(2)

if __name__ == '__main__':
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    run()

