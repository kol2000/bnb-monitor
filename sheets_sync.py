"""Google Sheets wallet import and balance export."""
import json
import re
import time
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from app import DATA, RemoteError, db, set_status, settings, bnb

def google_credentials(write=False):
    from google.auth.transport.requests import Request as AuthRequest
    from google.oauth2 import service_account
    try:
        info = json.loads((DATA / 'google-service-account.json').read_text())
        if (info.get('type') != 'service_account'
                or info.get('token_uri') != 'https://oauth2.googleapis.com/token'
                or info.get('universe_domain', 'googleapis.com') != 'googleapis.com'):
            raise ValueError()
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=['https://www.googleapis.com/auth/spreadsheets' if write else
                          'https://www.googleapis.com/auth/spreadsheets.readonly'])
        transport = AuthRequest()
        def bounded_request(*args, **kwargs):
            kwargs['timeout'] = 15
            return transport(*args, **kwargs)
        creds.refresh(bounded_request)
    except Exception:
        raise RemoteError('Не удалось авторизоваться в Google. Проверьте ключ сервисного аккаунта и интернет.') from None
    return creds

def fetch_rows(cfg):
    creds = google_credentials()
    tab = cfg['sheets_tab'].replace("'", "''")
    cell_range = quote(f"'{tab}'!A1:B5000", safe='')
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{cfg['sheets_id']}/values/{cell_range}?valueRenderOption=FORMATTED_VALUE"
    try:
        req = Request(url, headers={'Authorization': 'Bearer ' + creds.token})
        with urlopen(req, timeout=20) as response:
            raw = response.read(2_000_001)
        if len(raw) > 2_000_000:
            raise RemoteError('Ответ Google слишком большой. Используйте отдельную вкладку с адресами.')
        result = json.loads(raw)
        if not isinstance(result, dict) or not isinstance(result.get('range'), str) or not result['range']:
            raise ValueError('Missing Google ValueRange')
        rows = result.get('values', [])
        if not isinstance(rows, list):
            raise ValueError()
        if len(rows) >= 5000:
            raise RemoteError('Достигнут предел 5000 строк. Используйте отдельную вкладку короче 5000 строк.')
        return rows
    except HTTPError as exc:
        if exc.code in (401, 403):
            raise RemoteError('Google отказал в доступе. Включите Sheets API и дайте сервисному аккаунту доступ «Читатель».') from None
        if exc.code in (400, 404):
            raise RemoteError('Google не нашёл таблицу или вкладку, либо нет доступа. Проверьте ID, имя вкладки и права.') from None
        raise RemoteError('Ошибка Google API. Возможен лимит запросов; синхронизация повторится позже.') from None
    except RemoteError:
        raise
    except Exception:
        raise RemoteError('Не удалось прочитать Google Таблицу. Сохранённые кошельки продолжают проверяться.') from None

def apply_rows(rows, source=None):
    # Normalize and validate before touching the database. Conflicts fail atomically.
    if not isinstance(rows, list) or len(rows) >= 5000:
        raise RemoteError('Неполный или некорректный список Google. Кошельки сохранены.')
    unique = {}
    invalid = duplicates = 0
    for index, row in enumerate(rows):
        if not isinstance(row, list):
            invalid += 1
            continue
        address = str(row[1]).strip().lower() if len(row) > 1 else ''
        if not address:
            continue
        if not re.fullmatch(r'0x[0-9a-f]{40}', address):
            # Recognize a header only on the first row, never arbitrary invalid data.
            if source is not None and index == 0 and address in ('address', 'адрес', 'bsc-адрес', 'wallet address'):
                continue
            invalid += 1
            continue
        name = str(row[0]).strip()[:80] if row else ''
        name = name or address[:8]
        if address in unique:
            if unique[address] != name:
                raise RemoteError('Один адрес имеет разные имена в таблице. Исправьте дубликаты; изменения не применены.')
            duplicates += 1
        unique[address] = name
    if not unique and source is None:
        raise RemoteError('В столбце B не найдено корректных BSC-адресов. Список приложения сохранён.')
    added = renamed = 0
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        now = time.time()
        reconcile = False
        if source is not None:
            current = {r['key']: json.loads(r['value']) for r in c.execute('SELECT * FROM settings')}
            if any(current[k] != source[k] for k in ('sheets_enabled', 'sheets_id', 'sheets_tab')):
                raise RemoteError('Настройки Google изменились во время синхронизации. Повторите её.')
            last = c.execute("SELECT value FROM status WHERE key='sheets_reconciled_at'").fetchone()
            reconcile = last is None or now - json.loads(last['value']) >= 86400
            if reconcile and invalid:
                raise RemoteError('Ежедневная сверка отменена: в столбце B есть неверные адреса. Исправьте их; кошельки сохранены.')
        existing = {r['address']: r for r in c.execute('SELECT id,address,name FROM wallets')}
        if (len(unique) if reconcile else len(set(existing) | set(unique))) > 500:
            raise RemoteError('После импорта будет больше 500 адресов. Список не изменён.')
        if reconcile:
            removed = [w['id'] for address, w in existing.items() if address not in unique]
            for wid in removed:
                c.execute("UPDATE events SET delivery='cancelled' WHERE wallet_id=? AND delivery='pending'", (wid,))
                c.execute('DELETE FROM wallets WHERE id=?', (wid,))
            for key, value in {'sheets_reconciled_at': now, 'sheets_removed': len(removed)}.items():
                c.execute('INSERT OR REPLACE INTO status VALUES (?,?)', (key, json.dumps(value)))
        for address, name in unique.items():
            if address not in existing:
                c.execute('INSERT INTO wallets(address,name) VALUES (?,?)', (address, name))
                added += 1
            elif existing[address]['name'] != name:
                c.execute('UPDATE wallets SET name=? WHERE address=?', (name, address))
                renamed += 1
    return {'added': added, 'renamed': renamed, 'invalid': invalid,
            'duplicates': duplicates, 'valid': len(unique)}

def sync_once():
    cfg = settings()
    set_status(sheets_requested=False, sheets_attempt=time.time())
    if not cfg['sheets_enabled']:
        return
    try:
        rows = fetch_rows(cfg)
        current = settings()
        fields = ('sheets_enabled', 'sheets_id', 'sheets_tab')
        if any(current[k] != cfg[k] for k in fields):
            return  # Settings changed during the network request; discard stale response.
        counts = apply_rows(rows, source=cfg)
        set_status(sheets_success=time.time(), sheets_error=None, sheets_counts=counts,
                   check_requested=True)
    except RemoteError as exc:
        set_status(sheets_error=str(exc))
    except Exception:
        set_status(sheets_error='Ошибка синхронизации Google. Проверьте доступность диска и настройки.')

def export_request(cfg, token, suffix, payload=None):
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{cfg['sheets_id']}/{suffix}"
    req = Request(url, data=json.dumps(payload).encode() if payload is not None else None,
                  headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
    try:
        with urlopen(req, timeout=20) as response:
            raw = response.read(2_000_001)
        if len(raw) > 2_000_000:
            raise ValueError()
        return json.loads(raw)
    except HTTPError as exc:
        if exc.code in (401, 403):
            raise RemoteError('Для записи балансов дайте сервисному аккаунту Google доступ «Редактор».') from None
        raise RemoteError('Не удалось записать балансы: ошибка Google API.') from None
    except Exception:
        raise RemoteError('Не удалось обновить балансы в Google Таблице.') from None

def balance_updates(header, rows, balances, tab):
    normalize = lambda value: ' '.join(str(value).split()).casefold()
    matches = [i for i, value in enumerate(header) if normalize(value) == 'остаток bnb (bsc)']
    if len(matches) != 1 or matches[0] < 2:
        raise RemoteError('Нужен один столбец «остаток BNB (BSC)» в первой строке, после столбцов A и B.')
    number = matches[0] + 1
    column = ''
    while number:
        number, remainder = divmod(number - 1, 26)
        column = chr(65 + remainder) + column
    escaped = tab.replace("'", "''")
    updates = []
    for index, row in enumerate(rows, 1):
        address = str(row[0]).strip().lower() if row else ''
        if address in balances:
            # RAW numeric values work independently of the spreadsheet locale.
            updates.append({'range': f"'{escaped}'!{column}{index}",
                            'values': [[float(bnb(balances[address]))]]})
    return updates

def export_balances(cycle_started):
    cfg = settings()
    if not cfg['sheets_enabled']:
        return
    try:
        if cfg['sheets_tab'] != 'bnb кошельки':
            raise RemoteError('Запись балансов настроена только для листа «bnb кошельки».')
        with db() as c:
            balances = {r['address']: r['balance'] for r in c.execute(
                'SELECT address,balance FROM wallets WHERE balance IS NOT NULL AND error IS NULL AND checked>=?',
                (cycle_started,))}
        if not balances:
            return
        creds = google_credentials(write=True)
        tab = cfg['sheets_tab'].replace("'", "''")
        suffix = ('values:batchGet?ranges=' + quote(f"'{tab}'!A1:ZZ1", safe='')
                  + '&ranges=' + quote(f"'{tab}'!B1:B5000", safe='')
                  + '&valueRenderOption=FORMATTED_VALUE')
        snapshot = export_request(cfg, creds.token, suffix)
        ranges = snapshot['valueRanges']
        header = ranges[0].get('values', [[]])[0]
        rows = ranges[1].get('values', [])
        if len(rows) >= 5000:
            raise RemoteError('Запись отменена: достигнут предел строк.')
        data = balance_updates(header, rows, balances, cfg['sheets_tab'])
        if not data:
            return
        # Refresh the mapping just before writing; don't reuse the import's row order.
        if export_request(cfg, creds.token, suffix) != snapshot:
            raise RemoteError('Строки Google изменились во время записи. Повторим после следующего обхода.')
        current = settings()
        if any(current[k] != cfg[k] for k in ('sheets_enabled', 'sheets_id', 'sheets_tab')):
            return
        result = export_request(cfg, creds.token, 'values:batchUpdate',
                                {'valueInputOption': 'RAW', 'data': data})
        if result.get('totalUpdatedCells') != len(data):
            raise RemoteError('Google подтвердил не все обновления балансов.')
        set_status(sheets_export_success=time.time(), sheets_export_count=len(data), sheets_export_error=None)
    except RemoteError as exc:
        set_status(sheets_export_error=str(exc))
    except Exception:
        set_status(sheets_export_error='Не удалось записать балансы. Проверьте заголовок столбца и права Google.')
