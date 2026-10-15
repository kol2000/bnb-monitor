"""Manually confirmed two-leg BNB sweeps. Never stores mnemonics/private keys.

Both exact transactions are signed and journalled BEFORE the first broadcast.
Retries rebroadcast identical bytes, never allocate a replacement nonce.
"""
import json
import re
import secrets
import threading
import time
from urllib.parse import quote

from eth_account import Account
from eth_utils import to_checksum_address

DEFAULTS = {'withdraw_destination': '0x2ce6195ccdf030fb7fd8d674995728ddcac2148b'}
PATHS = ("m/44'/60'/0'/0/0", "m/44'/60'/0'/0/1")
GAS = 21000
MAX_GAS_PRICE = 3_000_000_000
lock = threading.Lock()


def schema(c):
    c.execute('''CREATE TABLE IF NOT EXISTS withdrawals (
        id TEXT PRIMARY KEY, wallet_id INTEGER NOT NULL, created REAL NOT NULL,
        state TEXT NOT NULL, plan TEXT NOT NULL, raw1 TEXT, raw2 TEXT,
        hash1 TEXT, hash2 TEXT, error TEXT, updated REAL NOT NULL,
        attempted1 INTEGER NOT NULL DEFAULT 0, attempted2 INTEGER NOT NULL DEFAULT 0)''')
    # Only one transfer route at a time, across web requests and worker restarts.
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS withdrawal_active ON withdrawals((1)) WHERE state IN ('pending1','pending2','blocked')")


def destination(value):
    if not isinstance(value, str) or not re.fullmatch(r'0x[0-9a-fA-F]{40}', value.strip()):
        raise ValueError('Адрес биржи: 0x и 40 шестнадцатеричных символов.')
    value = value.strip().lower()
    if value in ('0x' + '0' * 40, '0x' + '0' * 36 + 'dead'):
        raise ValueError('Нельзя использовать нулевой адрес или адрес сжигания.')
    return value


def accounts_for(cfg, address):
    """Read B for row lookup, then only that row's B:C. No bulk seed download."""
    from app import RemoteError
    from sheets_sync import google_credentials, export_request
    if not cfg.get('sheets_enabled'):
        raise ValueError('Для вывода нужен кошелёк из подключённой Google Таблицы.')
    token = google_credentials().token
    tab = cfg['sheets_tab'].replace("'", "''")
    def read(cell_range):
        suffix = 'values/' + quote(f"'{tab}'!{cell_range}", safe='') + '?valueRenderOption=UNFORMATTED_VALUE'
        return export_request(cfg, token, suffix).get('values', [])
    rows = read('B1:B5000')
    matches = [i + 1 for i, row in enumerate(rows) if row and str(row[0]).strip().lower() == address]
    if len(rows) >= 5000 or len(matches) != 1:
        raise ValueError('Нужна ровно одна строка с этим адресом в столбце B.')
    row = matches[0]
    values = read(f'B{row}:C{row}')
    if (len(values) != 1 or len(values[0]) != 2
            or str(values[0][0]).strip().lower() != address):
        raise ValueError('Строка Google изменилась или в C отсутствует сид-фраза. Повторите расчёт.')
    try:
        Account.enable_unaudited_hdwallet_features()
        mnemonic = ' '.join(str(values[0][1]).split())
        pair = tuple(Account.from_mnemonic(mnemonic, account_path=path) for path in PATHS)
    except Exception:
        # Library errors can contain the mnemonic. Never propagate them.
        raise RemoteError('Сид-фраза в столбце C не прошла проверку.') from None
    finally:
        values.clear()
    if pair[0].address.lower() != address:
        raise ValueError('Адрес из B не совпадает с первым аккаунтом сид-фразы. Вывод остановлен.')
    return pair


def node(url):
    from app import rpc, RemoteError
    def call(method, params):
        return rpc(url, method, params)
    if int(call('eth_chainId', []), 16) != 56:
        raise RemoteError('Вывод разрешён только в BSC mainnet (56).')
    head = call('eth_getBlockByNumber', ['latest', False])
    if abs(time.time() - int(head['timestamp'], 16)) > 90:
        raise RemoteError('RPC отстаёт или часы сервера неверны.')
    return call


def idle_account(call, address):
    if call('eth_getCode', [address, 'latest']) != '0x':
        raise ValueError('Вывод поддерживает только обычные адреса без кода контракта.')
    latest = int(call('eth_getTransactionCount', [address, 'latest']), 16)
    pending = int(call('eth_getTransactionCount', [address, 'pending']), 16)
    if latest != pending:
        raise ValueError('У аккаунта есть неподтверждённая транзакция. Дождитесь её завершения.')
    balance = int(call('eth_getBalance', [address, 'latest']), 16)
    return balance, latest


def make_plan(cfg, source, middle, call):
    target = destination(cfg['withdraw_destination'])
    if len({source, middle, target}) != 3:
        raise ValueError('Первый, второй и биржевой адреса должны различаться.')
    if call('eth_getCode', [target, 'latest']) != '0x':
        raise ValueError('Биржевой адрес содержит код контракта. Такой вывод не поддерживается.')
    balance1, nonce1 = idle_account(call, source)
    balance2, nonce2 = idle_account(call, middle)
    gas_price = int(call('eth_gasPrice', []), 16)
    if not 0 < gas_price <= MAX_GAS_PRICE:
        raise ValueError('Цена газа вне допустимого диапазона (до 3 Gwei). Попробуйте позже.')
    fee = GAS * gas_price
    value1 = balance1 - fee
    value2 = balance2 + value1 - fee
    if value1 <= 0 or value2 <= 0:
        raise ValueError('Недостаточно BNB для двух переводов с комиссиями.')
    estimate = int(call('eth_estimateGas', [{'from': source, 'to': middle,
                   'value': hex(value1), 'gasPrice': hex(gas_price)}]), 16)
    if estimate != GAS:
        raise ValueError('Неожиданная комиссия перевода. Вывод остановлен.')
    return {'source': source, 'middle': middle, 'destination': target,
            'balance1': str(balance1), 'balance2': str(balance2),
            'nonce1': nonce1, 'nonce2': nonce2, 'gas_price': gas_price,
            'value1': str(value1), 'value2': str(value2), 'fee': str(fee),
            'sheets_enabled': True, 'sheets_id': cfg['sheets_id'], 'sheets_tab': cfg['sheets_tab']}


def public(row):
    from app import bnb
    p = json.loads(row['plan'])
    result = {key: row[key] for key in ('id', 'wallet_id', 'created', 'state', 'updated', 'hash1', 'hash2', 'error')}
    for key in ('source', 'middle', 'destination'):
        result[key] = p[key]
    for key in ('balance1', 'balance2', 'value1', 'value2', 'fee'):
        result[key] = bnb(p[key])
    result['can_cancel'] = row['state'] == 'blocked' and not row['attempted1']
    return result


def preview(wid):
    from app import db, settings, RemoteError
    cfg = settings()
    with db() as c:
        if c.execute("SELECT 1 FROM withdrawals WHERE state IN ('pending1','pending2','blocked')").fetchone():
            raise ValueError('Сначала завершите текущий вывод. Его статус показан в журнале.')
        wallet = c.execute('SELECT address FROM wallets WHERE id=?', (wid,)).fetchone()
    if not wallet:
        raise ValueError('Кошелёк не найден.')
    pair = accounts_for(cfg, wallet['address'])
    last_error = None
    for url in cfg['rpc_urls']:
        try:
            plan = make_plan(cfg, pair[0].address.lower(), pair[1].address.lower(), node(url))
            plan['url'] = url
            break
        except RemoteError as exc:
            last_error = exc
    else:
        raise last_error or RemoteError('Нет доступного RPC.')
    now = time.time()
    ident = secrets.token_hex(16)
    with db() as c:
        c.execute("INSERT INTO withdrawals(id,wallet_id,created,state,plan,updated) VALUES (?,?,?,'preview',?,?)",
                  (ident, wid, now, json.dumps(plan), now))
        row = c.execute('SELECT * FROM withdrawals WHERE id=?', (ident,)).fetchone()
    return public(row)


def confirm(ident):
    from app import db, settings
    with db() as c:
        row = c.execute('SELECT * FROM withdrawals WHERE id=?', (ident,)).fetchone()
    if not row:
        raise ValueError('Расчёт не найден.')
    if row['state'] != 'preview':
        return public(row)  # Idempotent confirmation after an HTTP timeout.
    if time.time() - row['created'] > 180:
        raise ValueError('Расчёт старше 3 минут. Нажмите «Вывести» заново.')
    plan = json.loads(row['plan'])
    cfg = settings()
    if (cfg['withdraw_destination'].lower() != plan['destination']
            or any(cfg[k] != plan[k] for k in ('sheets_enabled', 'sheets_id', 'sheets_tab'))):
        raise ValueError('Настройки изменились. Повторите расчёт.')
    pair = accounts_for(cfg, plan['source'])
    if pair[1].address.lower() != plan['middle']:
        raise ValueError('Второй аккаунт изменился. Вывод остановлен.')
    call = node(plan['url'])
    # Recompute with the same agreed gas price; a changed balance/nonce cancels.
    fresh = make_plan(cfg, plan['source'], plan['middle'], call)
    for key in ('balance1', 'balance2', 'nonce1', 'nonce2', 'gas_price'):
        if fresh[key] != plan[key]:
            raise ValueError('Баланс, nonce или комиссия изменились. Повторите расчёт.')
    signed = []
    for i, account in enumerate(pair, 1):
        tx = {'chainId': 56, 'nonce': plan[f'nonce{i}'], 'gas': GAS,
              'gasPrice': plan['gas_price'], 'value': int(plan[f'value{i}']),
              'to': to_checksum_address(plan['middle'] if i == 1 else plan['destination'])}
        signed.append(account.sign_transaction(tx))
    # Atomic reservation, followed by durable signed bytes. No RPC sends in web.
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        current = c.execute('SELECT * FROM withdrawals WHERE id=?', (ident,)).fetchone()
        if current['state'] != 'preview':
            return public(current)
        if c.execute("SELECT 1 FROM withdrawals WHERE state IN ('pending1','pending2','blocked')").fetchone():
            raise ValueError('Другой вывод уже запущен.')
        c.execute("""UPDATE withdrawals SET state='pending1',raw1=?,raw2=?,hash1=?,hash2=?,updated=?
                     WHERE id=?""", ('0x'+signed[0].raw_transaction.hex(), '0x'+signed[1].raw_transaction.hex(),
                     '0x'+signed[0].hash.hex(), '0x'+signed[1].hash.hex(), time.time(), ident))
        current = c.execute('SELECT * FROM withdrawals WHERE id=?', (ident,)).fetchone()
    return public(current)


def update(ident, **values):
    from app import db
    values['updated'] = time.time()
    with db() as c:
        c.execute('UPDATE withdrawals SET '+','.join(k+'=?' for k in values)+' WHERE id=?',
                  (*values.values(), ident))


def finalized_receipt(call, txhash):
    receipt = call('eth_getTransactionReceipt', [txhash])
    if receipt is None:
        return None
    if receipt['transactionHash'].lower() != txhash:
        raise ValueError('RPC вернул квитанцию другой транзакции.')
    height = int(receipt['blockNumber'], 16)
    final = call('eth_getBlockByNumber', ['finalized', False])
    if height > int(final['number'], 16):
        return 'waiting'
    block = call('eth_getBlockByNumber', [receipt['blockNumber'], False])
    if block['hash'].lower() != receipt['blockHash'].lower():
        return 'waiting'
    return receipt


def tick():
    from app import db, RemoteError, set_status
    with db() as c:
        row = c.execute("SELECT * FROM withdrawals WHERE state IN ('pending1','pending2') LIMIT 1").fetchone()
    if not row:
        return
    ident = row['id']
    plan = json.loads(row['plan'])
    leg = 1 if row['state'] == 'pending1' else 2
    try:
        call = node(plan['url'])
        txhash = row[f'hash{leg}']
        receipt = finalized_receipt(call, txhash)
        if receipt == 'waiting':
            update(ident, error='Транзакция включена в блок; ожидаем финализацию.')
            return
        if receipt is not None:
            if int(receipt['status'], 16) != 1:
                update(ident, state='failed', error=f'Перевод {leg} отклонён сетью. Проверьте транзакцию по хешу.')
                return
            update(ident, state='pending2' if leg == 1 else 'done', error=None)
            set_status(check_requested=True)
            return
        # Absence of a receipt is NOT proof of failure. Re-use the same signed tx.
        source = plan['source'] if leg == 1 else plan['middle']
        target = plan['middle'] if leg == 1 else plan['destination']
        nonce = plan[f'nonce{leg}']
        latest_nonce = int(call('eth_getTransactionCount', [source, 'latest']), 16)
        if latest_nonce > nonce:
            # May be temporary RPC inconsistency: keep the slot reserved.
            update(ident, error='Nonce уже использован, но квитанция пока не найдена. Повторяем проверку без нового перевода.')
            return
        if latest_nonce != nonce:
            raise ValueError('Nonce не совпадает с расчётом. Требуется проверка маршрута.')
        if not row[f'attempted{leg}']:
            balance, pending_nonce = idle_account(call, source)
            expected = int(plan['balance1']) if leg == 1 else int(plan['balance2']) + int(plan['value1'])
            if balance != expected or pending_nonce != nonce:
                raise ValueError('Баланс или nonce изменился перед отправкой. Вывод приостановлен.')
            if call('eth_getCode', [target, 'latest']) != '0x':
                raise ValueError('Получатель теперь содержит код контракта. Вывод приостановлен.')
            estimate = int(call('eth_estimateGas', [{'from': source, 'to': target,
                           'value': hex(int(plan[f'value{leg}'])), 'gasPrice': hex(plan['gas_price'])}]), 16)
            if estimate != GAS:
                raise ValueError('Оценка газа изменилась. Вывод приостановлен.')
            # Persist BEFORE send, so loss of acknowledgement cannot trigger resigning.
            update(ident, **{f'attempted{leg}': 1})
        returned = call('eth_sendRawTransaction', [row[f'raw{leg}']])
        if not isinstance(returned, str) or returned.lower() != txhash:
            raise RemoteError('RPC не подтвердил хеш. Проверяем квитанцию; новую транзакцию не создаём.')
        update(ident, error=None)
    except ValueError as exc:
        update(ident, state='blocked', error=str(exc))
    except RemoteError:
        update(ident, error='RPC временно недоступен или отклонил повтор. Проверяем прежний хеш каждые 15 секунд.')
    except Exception:
        # Never expose provider URLs, signed payloads or cryptographic exceptions.
        update(ident, error='Не удалось проверить вывод. Маршрут сохранён, повторим проверку.')


def run(stop):
    while not stop.is_set():
        try:
            tick()
        except Exception:
            pass  # Journal remains authoritative, including when DB is temporarily busy.
        stop.wait(15)


def install(app):
    from app import db
    from flask import jsonify
    @app.post('/api/wallets/<int:wid>/withdraw/preview')
    def preview_route(wid):
        if not lock.acquire(blocking=False):
            return jsonify(error='Уже рассчитывается вывод. Подождите.'), 409
        try:
            return preview(wid)
        finally:
            lock.release()

    @app.post('/api/withdrawals/<ident>/confirm')
    def confirm_route(ident):
        if not lock.acquire(blocking=False):
            return jsonify(error='Уже готовится вывод. Подождите.'), 409
        try:
            return confirm(ident)
        finally:
            lock.release()

    @app.get('/api/withdrawals')
    def list_route():
        with db() as c:
            rows = c.execute("SELECT * FROM withdrawals WHERE state!='preview' ORDER BY created DESC LIMIT 30").fetchall()
        return {'items': [public(r) for r in rows]}

    @app.post('/api/withdrawals/<ident>/cancel')
    def cancel_route(ident):
        # Only blocked jobs are cancellable: worker cannot be broadcasting them.
        # Never offer cancellation after even an ambiguous first send attempt.
        with db() as c:
            c.execute('BEGIN IMMEDIATE')
            changed = c.execute("""UPDATE withdrawals SET state='cancelled',raw1=NULL,raw2=NULL,
                         error=NULL,updated=? WHERE id=? AND state='blocked' AND attempted1=0""",
                         (time.time(), ident)).rowcount
            if not changed:
                raise ValueError('Отмена недоступна: транзакция могла быть отправлена.')
        return {'ok': True}
