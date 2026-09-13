import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time
from collections import defaultdict, deque
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from flask import Flask, jsonify, request, session, render_template
from werkzeug.security import generate_password_hash, check_password_hash
from prices import usd, SOURCE

DATA = Path(os.environ.get('DATA_DIR', '/data'))
DATA.mkdir(parents=True, exist_ok=True)
DB = DATA / 'monitor.db'
DEFAULTS = {'interval': 30, 'threshold': '0.00000001', 'telegram_enabled': False,
            'telegram_token': '', 'telegram_chat': '',
            'rpc_urls': ['https://bsc-dataseed-public.bnbchain.org'],
            'sheets_enabled': False, 'sheets_id': '', 'sheets_tab': 'Кошельки',
            'sheets_interval': 300}

@contextmanager
def db():
    conn = sqlite3.connect(DB, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def init_db():
    with db() as c:
        c.execute('PRAGMA journal_mode=WAL')
        c.executescript('''
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS wallets (
            id INTEGER PRIMARY KEY AUTOINCREMENT, address TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
            notify INTEGER NOT NULL DEFAULT 1, balance TEXT, block INTEGER,
            checked REAL, error TEXT);
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY, wallet_id INTEGER NOT NULL, address TEXT NOT NULL,
            name TEXT NOT NULL, old TEXT NOT NULL, new TEXT NOT NULL, delta TEXT NOT NULL,
            block INTEGER NOT NULL, created REAL NOT NULL, delivery TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0,
            delivery_error TEXT, chat TEXT NOT NULL DEFAULT '',
            UNIQUE(wallet_id, block));
        CREATE TABLE IF NOT EXISTS status (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS event_delivery ON events(delivery, retry_at);
        ''')
        for k, v in DEFAULTS.items():
            c.execute('INSERT OR IGNORE INTO settings VALUES (?,?)', (k, json.dumps(v)))
    os.chmod(DB, 0o600)

def settings():
    with db() as c:
        return {r['key']: json.loads(r['value']) for r in c.execute('SELECT * FROM settings')}

def set_status(**values):
    with db() as c:
        for k, v in values.items():
            c.execute('INSERT OR REPLACE INTO status VALUES (?,?)', (k, json.dumps(v)))

def bnb(wei):
    n = int(wei)
    sign = '-' if n < 0 else ''
    n = abs(n)
    whole, part = divmod(n, 10**18)
    return sign + str(whole) + (('.' + f'{part:018d}'.rstrip('0')) if part else '')

def threshold_wei(value):
    try:
        n = Decimal(str(value))
        if not n.is_finite() or n < 0 or n > 10**9 or n.as_tuple().exponent < -18:
            raise ValueError()
        return int(n * 10**18)
    except (InvalidOperation, ValueError):
        raise ValueError('Порог: положительное число или 0, максимум 18 знаков после точки.')

class RemoteError(Exception):
    pass

def post_json(url, payload):
    # Never return raw network exceptions: URLs may contain API keys or bot tokens.
    try:
        req = Request(url, data=json.dumps(payload).encode(),
                      headers={'Content-Type': 'application/json', 'User-Agent': 'BNB-Monitor/1.0'})
        with urlopen(req, timeout=12) as r:
            raw = r.read(2_000_001)
        if len(raw) > 2_000_000:
            raise ValueError()
        return json.loads(raw)
    except Exception:
        raise RemoteError('Сервис недоступен, ограничил запросы или вернул неверный ответ.') from None

def rpc(url, method, params):
    r = post_json(url, {'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params})
    if not isinstance(r, dict) or r.get('error') or 'result' not in r:
        raise RemoteError('Ошибка RPC. Проверьте endpoint, лимит и поддержку finalized.')
    return r['result']

def telegram(s, text, chat=None):
    if not re.fullmatch(r'\d+:[A-Za-z0-9_-]{20,}', s['telegram_token']):
        raise RemoteError('Токен Telegram не настроен.')
    if not s['telegram_chat']:
        raise RemoteError('Chat ID не настроен.')
    r = post_json('https://api.telegram.org/bot' + s['telegram_token'] + '/sendMessage',
                  {'chat_id': chat or s['telegram_chat'], 'text': text,
                   'link_preview_options': {'is_disabled': True}})
    if not isinstance(r, dict) or not r.get('ok'):
        raise RemoteError('Telegram отклонил сообщение. Проверьте токен, Chat ID и /start.')

def record_balance(wallet_id, amount, block, cfg):
    if not isinstance(amount, int) or amount < 0 or amount >= 2**256:
        raise RemoteError('Некорректный баланс RPC.')
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        w = c.execute('SELECT * FROM wallets WHERE id=?', (wallet_id,)).fetchone()
        if not w or (w['block'] is not None and block <= w['block']):
            return
        now = time.time()
        if w['balance'] is not None and int(w['balance']) != amount:
            delta = amount - int(w['balance'])
            send = delta > 0 and delta >= threshold_wei(cfg['threshold']) and w['notify'] and cfg['telegram_enabled']
            c.execute('''INSERT INTO events(wallet_id,address,name,old,new,delta,block,created,delivery,chat)
                         VALUES (?,?,?,?,?,?,?,?,?,?)''',
                      (w['id'], w['address'], w['name'], w['balance'], str(amount), str(delta),
                       block, now, 'pending' if send else 'off', cfg['telegram_chat'] if send else ''))
        c.execute('UPDATE wallets SET balance=?,block=?,checked=?,error=NULL WHERE id=?',
                  (str(amount), block, now, wallet_id))

init_db()
app = Flask(__name__)
password = os.environ.get('ADMIN_PASSWORD', '')
secret = os.environ.get('SECRET_KEY', '')
if len(password) < 12 or len(secret) < 32:
    raise RuntimeError('Сначала выполните python3 setup.py: нужны ADMIN_PASSWORD и SECRET_KEY.')
PASSWORD_HASH = generate_password_hash(password)
app.config.update(SECRET_KEY=secret, MAX_CONTENT_LENGTH=65536,
                  SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Strict',
                  SESSION_COOKIE_SECURE=os.environ.get('COOKIE_SECURE') == '1',
                  PERMANENT_SESSION_LIFETIME=43200)
attempts = defaultdict(deque)
attempt_lock = threading.Lock()

@app.before_request
def protect():
    if request.path.startswith('/api/'):
        if request.method != 'GET':
            token = request.headers.get('X-CSRF-Token', '')
            if not token or not hmac.compare_digest(token, session.get('csrf', '')):
                return jsonify(error='Сессия устарела. Обновите страницу.'), 403
        if request.path not in ('/api/session', '/api/login') and not session.get('auth'):
            return jsonify(error='Необходим вход.'), 401

@app.after_request
def headers(response):
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; form-action 'self'"
    return response

@app.errorhandler(ValueError)
def invalid(exc):
    return jsonify(error=str(exc)), 400

@app.errorhandler(RemoteError)
def remote_error(exc):
    return jsonify(error=str(exc)), 502

@app.get('/healthz')
def health():
    return {'ok': True}

@app.get('/')
def index():
    return render_template('index.html')

@app.get('/api/session')
def auth_state():
    if 'csrf' not in session:
        session['csrf'] = secrets.token_hex(32)
    return {'authenticated': bool(session.get('auth')), 'csrf': session['csrf']}

@app.post('/api/login')
def login():
    key = request.remote_addr
    with attempt_lock:
        now = time.time()
        for ip in list(attempts):
            if not attempts[ip] or attempts[ip][-1] < now - 300:
                del attempts[ip]
        q = attempts[key]
        while q and q[0] < now - 300:
            q.popleft()
        if len(q) >= 10:
            return jsonify(error='Слишком много попыток. Подождите 5 минут.'), 429
        q.append(now)
    supplied = request.get_json().get('password', '')
    if not isinstance(supplied, str) or not check_password_hash(PASSWORD_HASH, supplied):
        return jsonify(error='Неверный пароль.'), 401
    with attempt_lock:
        attempts.pop(key, None)
    session.clear()
    session.update(auth=True, csrf=secrets.token_hex(32))
    session.permanent = True
    return {'csrf': session['csrf']}

@app.post('/api/logout')
def logout():
    session.clear()
    return {'ok': True}

@app.get('/api/state')
def state():
    cfg = settings()
    cfg['telegram_token_set'] = bool(cfg.pop('telegram_token'))
    cfg['google_key_set'] = (DATA / 'google-service-account.json').is_file()
    with db() as c:
        wallets = [dict(r) for r in c.execute('SELECT * FROM wallets ORDER BY id DESC')]
        events = [dict(r) for r in c.execute('SELECT * FROM events ORDER BY id DESC LIMIT 200')]
        status = {r['key']: json.loads(r['value']) for r in c.execute('SELECT * FROM status')}
    price = status.get('bnb_usd')
    price_at = status.get('price_at')
    price_stale = not price_at or time.time() - price_at > 900
    for w in wallets:
        w['bnb'] = bnb(w['balance']) if w['balance'] is not None else None
        w['usd'] = usd(w['balance'], price)
    for e in events:
        e.update(delta_bnb=bnb(e['delta']), new_bnb=bnb(e['new']))
        e.pop('chat', None)
    total = sum(int(w['balance']) for w in wallets if w['balance'] is not None)
    return {'wallets': wallets, 'events': events, 'settings': cfg, 'status': status, 'total': bnb(total),
            'total_usd': usd(total, price), 'price': {'bnb_usd': price, 'updated': price_at,
            'stale': price_stale, 'error': status.get('price_error'), 'source': SOURCE}}

@app.post('/api/wallets')
def add_wallets():
    data = request.get_json()
    rows = data.get('wallets', [])
    if not isinstance(rows, list) or not 1 <= len(rows) <= 200:
        raise ValueError('Добавьте от 1 до 200 адресов за один раз.')
    valid = []
    for r in rows:
        address = str(r.get('address', '')).strip()
        name = str(r.get('name', '')).strip()[:80]
        if not re.fullmatch(r'0x[0-9a-fA-F]{40}', address):
            raise ValueError('Неверный адрес BSC: нужен 0x и 40 шестнадцатеричных символов.')
        valid.append((address.lower(), name or address[:8]))
    added = 0
    with db() as c:
        if c.execute('SELECT COUNT(*) FROM wallets').fetchone()[0] + len(valid) > 500:
            raise ValueError('Лимит этой версии — 500 адресов. Для большого списка нужен RPC с достаточной квотой.')
        for row in valid:
            added += c.execute('INSERT OR IGNORE INTO wallets(address,name) VALUES (?,?)', row).rowcount
    return {'added': added}

@app.post('/api/wallets/<int:wid>')
def edit_wallet(wid):
    data = request.get_json()
    with db() as c:
        if 'name' in data:
            name = str(data['name']).strip()
            if not 1 <= len(name) <= 80:
                raise ValueError('Название: 1–80 символов.')
            c.execute('UPDATE wallets SET name=? WHERE id=?', (name, wid))
        if 'notify' in data:
            if type(data['notify']) is not bool:
                raise ValueError('Неверное значение уведомлений.')
            c.execute('UPDATE wallets SET notify=? WHERE id=?', (int(data['notify']), wid))
    return {'ok': True}

@app.delete('/api/wallets/<int:wid>')
def delete_wallet(wid):
    with db() as c:
        c.execute("UPDATE events SET delivery='cancelled' WHERE wallet_id=? AND delivery='pending'", (wid,))
        c.execute('DELETE FROM wallets WHERE id=?', (wid,))
    return {'ok': True}

@app.post('/api/settings')
def save_settings():
    data = request.get_json()
    cfg = settings()
    try:
        interval = int(data.get('interval', cfg['interval']))
    except (ValueError, TypeError):
        raise ValueError('Интервал должен быть целым числом.')
    if not 10 <= interval <= 3600:
        raise ValueError('Интервал: от 10 до 3600 секунд.')
    threshold = str(data.get('threshold', cfg['threshold']))
    threshold_wei(threshold)
    urls = data.get('rpc_urls', cfg['rpc_urls'])
    if not isinstance(urls, list) or not 1 <= len(urls) <= 5:
        raise ValueError('Нужно от 1 до 5 RPC-адресов.')
    for url in urls:
        if not isinstance(url, str) or len(url) > 2048:
            raise ValueError('Некорректный RPC URL.')
        parsed = urlparse(url)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.fragment:
            raise ValueError('RPC должен начинаться с https://, без логина и фрагмента.')
    enabled = data.get('telegram_enabled', cfg['telegram_enabled'])
    if type(enabled) is not bool:
        raise ValueError('Неверное значение Telegram.')
    token = str(data.get('telegram_token', '')).strip() or cfg['telegram_token']
    if data.get('clear_token'):
        token = ''
    chat = str(data.get('telegram_chat', cfg['telegram_chat'])).strip()
    if token and not re.fullmatch(r'\d+:[A-Za-z0-9_-]{20,}', token):
        raise ValueError('Неверный формат токена Telegram.')
    if chat and not re.fullmatch(r'-?\d{1,20}', chat):
        raise ValueError('Chat ID должен быть числом, для группы может начинаться с минуса.')
    if enabled and (not token or not chat):
        raise ValueError('Для Telegram нужны токен и Chat ID.')
    sheets_enabled = data.get('sheets_enabled', cfg['sheets_enabled'])
    if type(sheets_enabled) is not bool:
        raise ValueError('Неверное значение синхронизации Google.')
    sheets_id = str(data.get('sheets_id', cfg['sheets_id'])).strip()
    if sheets_id.startswith('https://docs.google.com/spreadsheets/d/'):
        sheets_id = sheets_id.split('/d/', 1)[1].split('/', 1)[0].split('?', 1)[0].split('#', 1)[0]
    if sheets_id and not re.fullmatch(r'[A-Za-z0-9_-]{20,200}', sheets_id):
        raise ValueError('Введите ID или ссылку на Google Таблицу.')
    sheets_tab = str(data.get('sheets_tab', cfg['sheets_tab'])).strip()
    if not 1 <= len(sheets_tab) <= 100 or any(ord(ch) < 32 for ch in sheets_tab):
        raise ValueError('Введите название вкладки (1–100 символов).')
    try:
        sheets_interval = int(data.get('sheets_interval', cfg['sheets_interval']))
    except (ValueError, TypeError):
        raise ValueError('Интервал Google должен быть целым числом.')
    if not 60 <= sheets_interval <= 86400:
        raise ValueError('Интервал Google: от 60 до 86400 секунд.')
    if sheets_enabled and (not sheets_id or not (DATA / 'google-service-account.json').is_file()):
        raise ValueError('Укажите таблицу и сначала установите ключ через configure_google.py на VM.')
    cfg.update(interval=interval, threshold=threshold, rpc_urls=urls, telegram_enabled=enabled,
               telegram_token=token, telegram_chat=chat, sheets_enabled=sheets_enabled,
               sheets_id=sheets_id, sheets_tab=sheets_tab, sheets_interval=sheets_interval)
    with db() as c:
        for k, v in cfg.items():
            c.execute('UPDATE settings SET value=? WHERE key=?', (json.dumps(v), k))
        if not enabled:
            c.execute("UPDATE events SET delivery='cancelled' WHERE delivery='pending'")
    return {'ok': True}

@app.post('/api/sheets/sync')
def request_sync():
    if not settings()['sheets_enabled']:
        raise ValueError('Сначала включите Google Таблицы и сохраните настройки.')
    set_status(sheets_requested=True)
    return {'ok': True}

@app.post('/api/telegram/test')
def test_telegram():
    telegram(settings(), '✅ BNB Monitor: тестовое уведомление. Соединение работает.')
    return {'ok': True}

@app.post('/api/check')
def request_check():
    set_status(check_requested=True)
    return {'ok': True}
