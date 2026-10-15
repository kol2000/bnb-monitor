"""Read-only seed audit and explicitly confirmed Google reconciliation."""
import json
import re
import secrets
import time


def get(c, key, default=None):
    row = c.execute('SELECT value FROM status WHERE key=?', (key,)).fetchone()
    return json.loads(row['value']) if row else default


def put(c, key, value):
    c.execute('INSERT OR REPLACE INTO status VALUES (?,?)', (key, json.dumps(value)))


def source(cfg):
    return {k: cfg[k] for k in ('sheets_enabled', 'sheets_id', 'sheets_tab')}


def addresses(rows):
    from app import RemoteError
    if not isinstance(rows, list) or len(rows) >= 5000:
        raise RemoteError('Неполный список Google. Сверка отменена.')
    found = set()
    for i, row in enumerate(rows):
        if not isinstance(row, list):
            raise RemoteError('Неверная строка Google. Сверка отменена.')
        address = str(row[1]).strip().lower() if len(row) > 1 else ''
        if not address:
            if row and str(row[0]).strip():
                raise RemoteError('Есть имя без адреса. Заполните адрес или очистите строку перед сверкой.')
            continue
        if i == 0 and address in ('address', 'адрес', 'bsc-адрес', 'wallet address'):
            continue
        if not re.fullmatch(r'0x[0-9a-f]{40}', address):
            raise RemoteError('В Google есть неверные адреса. Сверка отменена.')
        found.add(address)
    return sorted(found)


def snapshot(c):
    return [dict(r) for r in c.execute('SELECT id,address,name FROM wallets ORDER BY id')]


def preview():
    from app import db, settings, RemoteError
    from sheets_sync import fetch_rows
    cfg = settings()
    if not cfg['sheets_enabled']:
        raise RemoteError('Сначала включите и сохраните Google Таблицу.')
    present = addresses(fetch_rows(cfg))
    with db() as c:
        wallets = snapshot(c)
        p = dict(token=secrets.token_hex(24), created=time.time(), source=source(cfg),
                 addresses=present, wallets=wallets,
                 removed=[w for w in wallets if w['address'].lower() not in present])
        put(c, 'reconcile_preview', p)
    return {'token':p['token'], 'removed':p['removed'], 'count':len(present), 'expires':p['created']+300}


def confirm(token):
    from app import db, settings, RemoteError
    from sheets_sync import fetch_rows
    with db() as c:
        p = get(c, 'reconcile_preview')
    if not p or not isinstance(token, str) or not secrets.compare_digest(p['token'], token) or time.time()-p['created']>300:
        raise RemoteError('Предпросмотр истёк. Выполните сверку заново.')
    cfg = settings()
    if source(cfg) != p['source'] or addresses(fetch_rows(cfg)) != p['addresses']:
        raise RemoteError('Таблица или настройки изменились. Выполните предпросмотр заново.')
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        current = {r['key']:json.loads(r['value']) for r in c.execute('SELECT * FROM settings')}
        if source(current) != p['source'] or get(c,'reconcile_preview') != p or snapshot(c) != p['wallets']:
            raise RemoteError('Список приложения изменился. Выполните предпросмотр заново.')
        for w in p['removed']:
            if c.execute("SELECT 1 FROM withdrawals WHERE wallet_id=? AND state IN ('pending1','pending2','blocked')", (w['id'],)).fetchone():
                raise RemoteError('Есть незавершённый перевод удаляемого кошелька. Сначала проверьте его статус.')
        for w in p['removed']:
            c.execute("UPDATE events SET delivery='cancelled' WHERE wallet_id=? AND delivery='pending'", (w['id'],))
            c.execute("UPDATE email_outbox SET delivery='cancelled' WHERE delivery='pending' AND event_id IN (SELECT id FROM events WHERE wallet_id=?)", (w['id'],))
            c.execute('DELETE FROM wallets WHERE id=?', (w['id'],))
        put(c, 'sheets_reconciled_at', time.time())
        put(c, 'sheets_removed', len(p['removed']))
        c.execute("DELETE FROM status WHERE key='reconcile_preview'")
    return {'removed':len(p['removed'])}


def start_audit():
    from app import db, settings, RemoteError
    cfg = settings()
    if not cfg['sheets_enabled']:
        raise RemoteError('Включите Google Таблицу перед проверкой.')
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        existing = get(c, 'seed_audit', {})
        if existing.get('state') == 'running':
            return public_audit(existing)
        a = dict(state='running', created=time.time(), source=source(cfg),
                 wallets=snapshot(c), results=[])
        put(c, 'seed_audit', a)
    return public_audit(a)


def public_audit(a):
    return {k:a.get(k) for k in ('state','created','updated','results')} | {'total':len(a.get('wallets', []))}


def audit_once():
    from app import db, settings
    from withdrawals import accounts_for
    with db() as c:
        a = get(c, 'seed_audit', {})
    if a.get('state') != 'running':
        return
    cfg = settings()
    if source(cfg) != a['source']:
        a['state'] = 'cancelled'
    elif len(a['results']) >= len(a['wallets']):
        a['state'] = 'done'
    else:
        w = a['wallets'][len(a['results'])]
        result = 'Адрес соответствует сид-фразе'
        ok = True
        try:
            pair = accounts_for(cfg, w['address'])
            del pair
        except ValueError:
            ok, result = False, 'Не совпадает адрес либо отсутствует единственная корректная строка B:C'
        except Exception:
            # Never persist an exception string: mnemonic libraries may include words.
            ok, result = False, 'Проверка не завершена: проверьте доступ к Google и корректность сид-фразы'
        a['results'].append(dict(address=w['address'], name=w['name'], ok=ok,
                                 result=result, checked=time.time(), path="m/44'/60'/0'/0/0"))
        if len(a['results']) == len(a['wallets']):
            a['state'] = 'done'
    a['updated'] = time.time()
    with db() as c:
        put(c, 'seed_audit', a)


def run(stop):
    while not stop.is_set():
        try:
            audit_once()
        except Exception:
            pass
        stop.wait(4)


def install(app):
    from flask import request
    from app import db
    @app.post('/api/sheets/reconcile-preview')
    def maintenance_preview():
        return preview()
    @app.post('/api/sheets/reconcile-confirm')
    def maintenance_confirm():
        return confirm((request.get_json() or {}).get('token'))
    @app.post('/api/seed-audit')
    def maintenance_audit_start():
        return start_audit()
    @app.get('/api/seed-audit')
    def maintenance_audit_status():
        with db() as c:
            return public_audit(get(c, 'seed_audit', {}))
