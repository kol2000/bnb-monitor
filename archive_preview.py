"""Conservative, read-only BSC activity preview powered by NodeReal.

Uses count-first RPC for activity detection; does not determine last-transaction date.
No wallet state changes, balance skips, transactions or Sheet updates.
Scan small batches manually: python archive_preview.py scan --requests 20
Review results in the dedicated web tab or run: python archive_preview.py list
Key: /data/nodereal-api-key (mode 0600; never in GitHub or API output).
"""
import argparse
import json
import os
import re
import socket
import time
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app import DATA, db, USDT_CONTRACT

API_BASE = 'https://bsc-mainnet.nodereal.io/v1/'
KINDS = ['external', 'internal', '20', '721', '1155']
DIRECTIONS = ('fromAddress', 'toAddress')
DAYS = 90
WINDOW = 100000  # NodeReal maximum difference between fromBlock and toBlock
FALLBACK_WINDOWS = (100000, 25000, 5000)  # Bound expensive retries; never 1000-by-default
INDEXER_LAG_BLOCKS = 50000  # Do not read blocks near head: observed blockNum not reached
MAX_TIP_AGE = 86400
ADDRESS = re.compile(r'^0x[0-9a-fA-F]{40}$')
HEX = re.compile(r'^0x[0-9a-fA-F]+$')


class IndexerError(Exception):
    pass


def schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS archive_activity_preview (
        wallet_id INTEGER PRIMARY KEY,
        address TEXT NOT NULL,
        checked_at REAL,
        last_activity REAL,
        decision TEXT NOT NULL DEFAULT 'scanning'
          CHECK(decision IN ('scanning','candidate','active','unknown')),
        detail TEXT NOT NULL DEFAULT '',
        cutoff_block INTEGER,
        tip_block INTEGER,
        tip_timestamp REAL,
        cursor_block INTEGER,
        direction INTEGER NOT NULL DEFAULT 0,
        requests INTEGER NOT NULL DEFAULT 0,
        window_blocks INTEGER NOT NULL DEFAULT 100000,
        scan_method TEXT NOT NULL DEFAULT 'transfers'
    )""")
    # Existing production SQLite tables were created before window_blocks.
    if 'window_blocks' not in {r['name'] for r in conn.execute(
            'PRAGMA table_info(archive_activity_preview)')}:
        conn.execute('ALTER TABLE archive_activity_preview '
                     'ADD COLUMN window_blocks INTEGER NOT NULL DEFAULT 100000')
    if 'scan_method' not in {r['name'] for r in conn.execute(
            'PRAGMA table_info(archive_activity_preview)')}:
        conn.execute("ALTER TABLE archive_activity_preview ADD COLUMN scan_method "
                     "TEXT NOT NULL DEFAULT 'transfers'")


def key_from_file():
    path = DATA / 'nodereal-api-key'
    try:
        key = path.read_text(encoding='ascii').strip()
    except OSError:
        raise IndexerError('NodeReal key not installed: /data/nodereal-api-key') from None
    if not re.fullmatch(r'[A-Za-z0-9_-]{8,256}', key):
        raise IndexerError('Invalid NodeReal key file')
    return key


def safe_api_error_message(message, key):
    """Redact credentials, URLs and addresses from a short remote diagnostic."""
    value = str(message).replace(key, '[API_KEY]')
    value = re.sub(r'https?://[^\s\x00-\x20]+', '[URL]', value, flags=re.I)
    value = re.sub(r'0x[0-9a-fA-F]{40,}', '[ADDRESS_OR_HEX]', value)
    value = re.sub(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}', '[EMAIL]', value)
    value = re.sub(r'(?<![A-Za-z0-9_])[A-Za-z0-9_-]{24,}(?![A-Za-z0-9_])', '[TOKEN]', value)
    value = re.sub(r'[\x00-\x1f\x7f]', ' ', value)
    return value[:180]


def rpc(method, params, *, opener=urlopen):
    if method not in ('eth_chainId','eth_blockNumber','eth_getBlockByNumber','nr_getAssetTransfers','nr_getAssetTransfersCount','eth_getTransactionCount','eth_getBalance','eth_call'):
        raise ValueError('Not a permitted read-only RPC method')
    key = key_from_file()
    request = Request(API_BASE + key,
                      data=json.dumps({'jsonrpc':'2.0','id':1,'method':method,
                                       'params':params}).encode(),
                      headers={'Content-Type':'application/json','Accept':'application/json',
                               'User-Agent':'BNB-Monitor/1.0'})
    # The only retry is a safe JSON-RPC READ. A timeout never retries transfers
    # or other write operations. Keep exception URLs/API-key bytes out of logs.
    for attempt in range(2):
        try:
            with opener(request, timeout=20) as res:
                data = res.read(600001)
            if len(data) > 600000:
                raise IndexerError('NodeReal oversized response')
            value = json.loads(data)
            if not isinstance(value, dict):
                raise IndexerError('NodeReal invalid JSON-RPC envelope')
            if value.get('error'):
                error = value['error']
                code = error.get('code') if isinstance(error, dict) else None
                raw_message = error.get('message', '') if isinstance(error, dict) else ''
                safe_message = safe_api_error_message(raw_message, key)
                raise IndexerError('NodeReal JSON-RPC code ' +
                                   str(code if isinstance(code, int) else 'unknown') +
                                   (': ' + safe_message if safe_message else ''))
            if 'result' not in value:
                raise IndexerError('NodeReal response has no result')
            return value['result']
        except HTTPError as exc:
            # Rate limits/permission problems should be visible, not blindly retried.
            raise IndexerError('NodeReal HTTP ' + str(exc.code)) from None
        except (TimeoutError, socket.timeout) as exc:
            if attempt == 0:
                time.sleep(2)
                continue
            raise IndexerError('NodeReal timed out on both read attempts') from None
        except URLError as exc:
            if isinstance(exc.reason, (TimeoutError, socket.timeout)) and attempt == 0:
                time.sleep(2)
                continue
            reason = ('timeout' if isinstance(exc.reason,(TimeoutError,socket.timeout))
                      else type(exc.reason).__name__)
            raise IndexerError('NodeReal connection error (' + reason + ')') from None
        except (OSError, ValueError, UnicodeError, TypeError):
            raise IndexerError('NodeReal invalid or incomplete response') from None


def block_data(number):
    result = rpc('eth_getBlockByNumber', [hex(number),False])
    if not isinstance(result, dict) or not HEX.fullmatch(str(result.get('number'))) or not HEX.fullmatch(str(result.get('timestamp'))):
        raise IndexerError('NodeReal invalid block')
    if int(result['number'],16) != number:
        raise IndexerError('NodeReal returned wrong block')
    return int(result['timestamp'],16)


def find_cutoff(tip, now):
    """Binary-search the first block at/after 90 days ago, based on actual timestamps."""
    target = now - DAYS * 86400
    low, high = 1, tip
    if block_data(tip) < target:
        raise IndexerError('Indexer-safe tip is older than 90-day cutoff')
    # Conservative in the presence of irregular timestamps: only preview.
    while low < high:
        mid = (low + high)//2
        if block_data(mid) < target:
            low = mid + 1
        else:
            high = mid
    return max(0,low-1)


def transfer_result(value, address, direction, low, high):
    """At most one latest transfer is needed to establish *activity*."""
    if not isinstance(value, dict) or not isinstance(value.get('transfers'),list):
        raise IndexerError('Malformed transfer result')
    transfers = value['transfers']
    if len(transfers) > 1 or (not transfers and value.get('pageKey')):
        raise IndexerError('Malformed or incomplete transfer page')
    if not transfers:
        return None
    t = transfers[0]
    if not isinstance(t,dict) or not HEX.fullmatch(str(t.get('blockNum'))):
        raise IndexerError('Malformed transfer block')
    height = int(t['blockNum'],16)
    if not low <= height <= high or str(t.get('category')) not in KINDS:
        raise IndexerError('Transfer outside requested range/categories')
    side = 'from' if direction == 'fromAddress' else 'to'
    if str(t.get(side,'')).lower() != address.lower():
        raise IndexerError('Transfer address did not match')
    raw_time = t.get('blockTimeStamp', t.get('blockTimestamp'))
    if isinstance(raw_time, int) and raw_time > 0:
        stamp = float(raw_time)
    elif isinstance(raw_time,str) and raw_time.isdecimal():
        stamp = float(raw_time)
    else:
        # One extra read only when activity exists and timestamp missing.
        stamp = float(block_data(height))
    if not 0 < stamp <= time.time()+300:
        raise IndexerError('Invalid transfer timestamp')
    return stamp


def safe_tip():
    if int(rpc('eth_chainId', []),16) != 56:
        raise IndexerError('NodeReal is not BSC mainnet')
    head = int(rpc('eth_blockNumber', []),16)
    tip = head - INDEXER_LAG_BLOCKS
    if tip <= 0:
        raise IndexerError('No safe indexed block available')
    stamp = block_data(tip)
    if not 0 <= time.time()-stamp <= MAX_TIP_AGE:
        raise IndexerError('Indexer tip too far behind')
    return tip, stamp


def start_wallet(conn, wallet, tip, stamp, cutoff):
    conn.execute("""INSERT INTO archive_activity_preview
        (wallet_id,address,checked_at,decision,detail,cutoff_block,
         tip_block,tip_timestamp,cursor_block,direction,requests,window_blocks,scan_method)
        VALUES(?,?,?,'scanning','Сканирование 90 дней',?,?,?,?,0,0,100000,'count')
        ON CONFLICT(wallet_id) DO UPDATE SET
        address=excluded.address,checked_at=excluded.checked_at,
        last_activity=NULL,decision='scanning',detail='Сканирование 90 дней',
        cutoff_block=excluded.cutoff_block,tip_block=excluded.tip_block,
        tip_timestamp=excluded.tip_timestamp,cursor_block=excluded.cursor_block,
        direction=0,requests=0,window_blocks=100000,scan_method='count'""",
        (wallet['id'],wallet['address'],time.time(),cutoff,tip,stamp,tip))


def count_result(value):
    """NodeReal returns a hexadecimal transfer count."""
    if not isinstance(value, str) or not HEX.fullmatch(value):
        raise IndexerError('NodeReal invalid transfer count')
    return int(value, 16)


def step(wallet, row):
    """Count-only activity preview with exact persisted range boundaries.

    A positive count proves an operation occurred in the checked block window.
    It does not establish the exact timestamp of the last transaction.
    """
    direction = DIRECTIONS[row['direction']]
    high = row['cursor_block']
    requested_window = row['window_blocks'] if 'window_blocks' in row.keys() else WINDOW
    if requested_window not in FALLBACK_WINDOWS:
        raise IndexerError('Invalid saved archive scan window')
    position = FALLBACK_WINDOWS.index(requested_window)
    for index in range(position, len(FALLBACK_WINDOWS)):
        current_window = FALLBACK_WINDOWS[index]
        low = max(row['cutoff_block'], high-current_window)
        params = {'category': KINDS, direction: wallet['address'],
                  'fromBlock':hex(low),'toBlock':hex(high)}
        try:
            result = rpc('nr_getAssetTransfersCount',[params])
            count = count_result(result)
            break
        except IndexerError as exc:
            if 'timed out on both read attempts' not in str(exc):
                raise
            if index == len(FALLBACK_WINDOWS)-1:
                raise IndexerError('NodeReal count API timed out even on 5000 blocks') from None
            next_window = FALLBACK_WINDOWS[index+1]
            with db() as conn:
                live = conn.execute('SELECT address FROM wallets WHERE id=?',
                                    (wallet['id'],)).fetchone()
                if not live or live['address'] != wallet['address']:
                    raise IndexerError('Wallet removed during scan')
                conn.execute('UPDATE archive_activity_preview SET window_blocks=?, '
                             'detail=? WHERE wallet_id=?',
                             (next_window,'NodeReal count timeout: reduced range to '+str(next_window),
                              wallet['id']))
    with db() as conn:
        live = conn.execute('SELECT address FROM wallets WHERE id=?',
                            (wallet['id'],)).fetchone()
        if not live or live['address'] != wallet['address']:
            return 'removed'
        if count > 0:
            conn.execute("""UPDATE archive_activity_preview SET
                decision='active',last_activity=NULL,
                detail=?,checked_at=?,requests=requests+1
                WHERE wallet_id=?""",
                ('Подтверждена активность: '+str(count)+
                 ' операций в диапазоне блоков '+str(low)+'–'+str(high)+
                 '; точная дата последней операции неизвестна',
                 time.time(),wallet['id']))
            return 'active'
        next_cursor = low-1
        next_direction = row['direction']
        if next_cursor < row['cutoff_block']:
            next_direction += 1
            next_cursor = row['tip_block']
        if next_direction == len(DIRECTIONS):
            if time.time()-row['tip_timestamp'] > MAX_TIP_AGE:
                decision, detail = 'unknown','Данные устарели за время сканирования; повторите позднее.'
            else:
                decision, detail = 'candidate','Предварительный кандидат: нет операций в проверенном диапазоне. Последние ~50000 блоков и возраст адреса НЕ проверены.'
            conn.execute("""UPDATE archive_activity_preview SET
                decision=?,detail=?,checked_at=?,requests=requests+1
                WHERE wallet_id=?""",
                (decision,detail,time.time(),wallet['id']))
            return decision
        conn.execute("""UPDATE archive_activity_preview SET
            cursor_block=?,direction=?,checked_at=?,requests=requests+1
            WHERE wallet_id=?""",
            (next_cursor,next_direction,time.time(),wallet['id']))
    return 'scanning'


def scan(limit=1, requests=20, sleeper=time.sleep, wallet_id=None, refresh_completed=False):
    """Bounded manual execution; unfinished wallets resume at their checkpoint."""
    if not 1 <= limit <= 10 or not 1 <= requests <= 100:
        raise ValueError('Use --limit 1..10 and --requests 1..100')
    if wallet_id is not None and (not isinstance(wallet_id, int) or wallet_id < 1):
        raise ValueError('Use --wallet-id with a positive integer')
    now = time.time()
    bounds = None

    def fresh_bounds():
        # Resuming a checkpoint needs no head/cutoff RPC calls. New wallets in
        # this bounded batch share one freshly verified 90-day boundary.
        nonlocal bounds
        if bounds is None:
            tip, stamp = safe_tip()
            bounds = (tip, stamp, find_cutoff(tip, time.time()))
        return bounds
    used = 0
    last_error = None
    completed = {'active':0,'candidate':0,'unknown':0,'failed':0}
    with db() as conn:
        schema(conn)
        # An explicit wallet ID bypasses the automatic queue. Existing partial
        # progress is still resumed; a completed wallet starts a fresh preview.
        if wallet_id is not None:
            wallets = [dict(r) for r in conn.execute("""
                SELECT w.id,w.address,p.decision,p.checked_at,
                       p.address AS scanned_address
                FROM wallets w LEFT JOIN archive_activity_preview p
                    ON w.id=p.wallet_id
                WHERE w.id=?""", (wallet_id,))]
            if not wallets:
                raise ValueError('Wallet ID not found')
        else:
            wallets = [dict(r) for r in conn.execute("""
                SELECT w.id,w.address,p.decision,p.checked_at,
                       p.address AS scanned_address
                FROM wallets w LEFT JOIN archive_activity_preview p
                    ON w.id=p.wallet_id
                WHERE p.wallet_id IS NULL OR p.decision='scanning'
                   OR (p.decision='unknown'
                       AND p.detail='Ошибка NodeReal; сканирование не подтверждено'
                       AND p.cursor_block IS NOT NULL
                       AND p.cutoff_block IS NOT NULL
                       AND p.tip_block IS NOT NULL
                       AND p.direction IN (0,1))
                   OR p.address!=w.address OR p.scan_method!='count'
                   OR (? AND p.decision IN ('active','candidate','unknown')
                       AND p.checked_at<?)
                ORDER BY
                    CASE WHEN p.decision='scanning' OR
                        (p.decision='unknown' AND
                         p.detail='Ошибка NodeReal; сканирование не подтверждено')
                        THEN 0 ELSE 1 END,
                    CASE WHEN p.decision='scanning' OR
                        (p.decision='unknown' AND
                         p.detail='Ошибка NodeReal; сканирование не подтверждено')
                        THEN COALESCE(p.requests,0) ELSE 0 END DESC,
                    CASE WHEN p.wallet_id IS NULL OR p.address!=w.address THEN 0 ELSE 1 END,
                    COALESCE(p.checked_at,0),w.id
                LIMIT ?""", (bool(refresh_completed),now-86400,limit))]

    for wallet in wallets:
        with db() as conn:
            schema(conn)
            row = conn.execute('SELECT * FROM archive_activity_preview WHERE wallet_id=?',
                               (wallet['id'],)).fetchone()
            if row is not None and row['scan_method'] != 'count':
                # Transfer-list absence was not independently validated. Recheck
                # from tip using the count API before making any candidate claim.
                tip, tip_stamp, cutoff = fresh_bounds()
                start_wallet(conn,wallet,tip,tip_stamp,cutoff)
                row = conn.execute('SELECT * FROM archive_activity_preview WHERE wallet_id=?',
                                   (wallet['id'],)).fetchone()
            resume_old_failure = (row is not None and row['address'] == wallet['address']
                and row['decision'] == 'unknown'
                and row['detail'] == 'Ошибка NodeReal; сканирование не подтверждено'
                and row['cursor_block'] is not None and row['cutoff_block'] is not None
                and row['tip_block'] is not None and row['direction'] in (0,1))
            if resume_old_failure:
                conn.execute('UPDATE archive_activity_preview SET decision=?,detail=? WHERE wallet_id=?',
                             ('scanning','Повторная попытка после ошибки NodeReal',wallet['id']))
            elif row is None or row['address'] != wallet['address'] or row['decision']!='scanning':
                tip, tip_stamp, cutoff = fresh_bounds()
                start_wallet(conn,wallet,tip,tip_stamp,cutoff)
                row = conn.execute('SELECT * FROM archive_activity_preview WHERE wallet_id=?',
                                   (wallet['id'],)).fetchone()
            row = dict(row)
        while used < requests:
            try:
                outcome = step(wallet,row)
            except (IndexerError,ValueError,TypeError,KeyError) as exc:
                last_error = str(exc) if isinstance(exc, IndexerError) else type(exc).__name__
                # Keep the cursor unchanged. No false candidate on an API error.
                with db() as conn:
                    conn.execute("""UPDATE archive_activity_preview SET
                        decision='scanning',detail='Пауза: ошибка NodeReal, можно продолжить с сохранённого блока',
                        checked_at=? WHERE wallet_id=?""",(time.time(),wallet['id']))
                completed['failed']+=1
                completed['unknown']+=1
                break
            used += 1
            if outcome != 'scanning':
                if outcome in completed:
                    completed[outcome] += 1
                break
            with db() as conn:
                row = dict(conn.execute('SELECT * FROM archive_activity_preview WHERE wallet_id=?',
                                        (wallet['id'],)).fetchone())
            sleeper(0.5)
        if used >= requests:
            break
    result = {'rpc_transfer_requests':used,'wallets_selected':len(wallets),**completed,
              'note':'preview only; no wallets archived'}
    if last_error is not None:
        result['error'] = last_error
    return result


def report():
    with db() as conn:
        schema(conn)
        return [dict(r) for r in conn.execute("""
            SELECT w.id,w.name,w.address,p.checked_at,p.last_activity,
                   p.decision,p.detail,p.requests,p.cursor_block,p.direction
            FROM wallets w JOIN archive_activity_preview p
                ON p.wallet_id=w.id AND p.address=w.address
            ORDER BY CASE p.decision WHEN 'candidate' THEN 0
                      WHEN 'unknown' THEN 1 WHEN 'scanning' THEN 2 ELSE 3 END,
                     p.last_activity,w.id
        """)]



def verification_schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS archive_candidate_verification (
        wallet_id INTEGER PRIMARY KEY, address TEXT NOT NULL,
        preview_tip INTEGER NOT NULL, checked_at REAL NOT NULL,
        status TEXT NOT NULL, detail TEXT NOT NULL, evidence TEXT NOT NULL
    )""")


def verification_block(number):
    value = rpc('eth_getBlockByNumber', [hex(number), False])
    if (not isinstance(value, dict)
            or count_result(value.get('number')) != number
            or not re.fullmatch(r'0x[0-9a-fA-F]{64}', str(value.get('hash')))):
        raise IndexerError('Invalid verification block')
    stamp = count_result(value.get('timestamp'))
    return stamp, value['hash']


def verify_candidate(row, *, sleeper=time.sleep):
    """Read-only snapshot verification. Never authorizes a later automatic move.

    Historical nonce > 0 proves use before the cutoff (not key creation time).
    A zero nonce is NOT proof of a new address. Keep such addresses for review.
    """
    evidence = {}
    try:
        address = row['address']
        cutoff, old_tip = row['cutoff_block'], row['tip_block']
        if (row['decision'] != 'candidate' or row['scan_method'] != 'count'
                or not ADDRESS.fullmatch(address) or not isinstance(cutoff, int)
                or not isinstance(old_tip, int) or not 0 < cutoff <= old_tip):
            raise IndexerError('Invalid completed candidate checkpoint')
        if count_result(rpc('eth_chainId', [])) != 56:
            raise IndexerError('NodeReal is not BSC mainnet')
        head = count_result(rpc('eth_blockNumber', []))
        stamp, block_hash = verification_block(head)
        if head < old_tip or not 0 <= time.time() - stamp <= 300:
            raise IndexerError('Verification head is stale or behind preview')
        # Original cutoff is older than today's 90-day threshold. Reusing it
        # over-checks activity conservatively and does not leave a range gap.
        cutoff_stamp = block_data(cutoff)
        if not 0 < cutoff_stamp < time.time() - DAYS * 86400:
            raise IndexerError('Preview cutoff does not prove a 90-day interval')
        evidence.update(head=head, head_timestamp=stamp, head_hash=block_hash,
                        cutoff=cutoff, cutoff_timestamp=cutoff_stamp,
                        preview_tip=old_tip)
        # This also catches outgoing failed transactions not indexed as transfers.
        old_nonce = count_result(rpc('eth_getTransactionCount', [address, hex(cutoff)]))
        nonce = count_result(rpc('eth_getTransactionCount', [address, hex(head)]))
        evidence.update(old_nonce=old_nonce, head_nonce=nonce)
        if nonce < old_nonce:
            raise IndexerError('Inconsistent historical nonce')
        if nonce > old_nonce:
            return 'active', 'Есть исходящие транзакции после границы 90 дней.', evidence
        # Check every block since preview, including the formerly omitted tail.
        # Never move the target backwards to work around indexer lag.
        for direction in DIRECTIONS:
            low = old_tip + 1
            while low <= head:
                high = min(head, low + WINDOW)
                params = {'category': KINDS, direction: address,
                          'fromBlock': hex(low), 'toBlock': hex(high),
                          'excludeZeroValue': False}
                if count_result(rpc('nr_getAssetTransfersCount', [params])):
                    return 'active', 'Обнаружена активность после предварительного сканирования.', evidence
                low = high + 1
                sleeper(0.5)
        if old_nonce == 0:
            return 'age_unknown', ('Нет подтверждения использования адреса более 90 дней назад. '
                                   'Нулевой nonce не означает, что кошелёк новый.'), evidence
        bnb = count_result(rpc('eth_getBalance', [address, hex(head)]))
        raw = rpc('eth_call', [{'to': USDT_CONTRACT,
            'data': '0x70a08231' + address[2:].lower().zfill(64)}, hex(head)])
        if not isinstance(raw, str) or not re.fullmatch(r'0x[0-9a-fA-F]{64}', raw):
            raise IndexerError('Invalid USDT balance response')
        usdt = int(raw, 16)
        evidence.update(bnb_wei=str(bnb), usdt_units=str(usdt))
        if verification_block(head) != (stamp, block_hash):
            raise IndexerError('Verification block changed during scan')
        if bnb or usdt:
            return 'has_balance', 'Есть остаток BNB или USDT; оставлен для ручной проверки.', evidence
        return 'verified_snapshot', ('Активность не обнаружена до указанного блока; '
            'использование более 90 дней назад подтверждено, BNB и USDT равны нулю. '
            'Перед переносом нужна актуальная сверка.'), evidence
    except (IndexerError, ValueError, TypeError, KeyError) as exc:
        detail = str(exc) if isinstance(exc, IndexerError) else type(exc).__name__
        return 'unknown', 'Проверка не подтверждена: ' + detail, evidence


def verify_candidates(*, sleeper=time.sleep):
    """Process all current candidates once, keeping original preview untouched."""
    import fcntl
    # Prevent two verification CLI processes from overwriting one another.
    with (DATA / 'archive-verification.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise IndexerError('Candidate verification is already running') from None
        with db() as conn:
            schema(conn)
            verification_schema(conn)
            rows = [dict(r) for r in conn.execute("""
                SELECT p.* FROM archive_activity_preview p JOIN wallets w
                ON w.id=p.wallet_id AND w.address=p.address
                WHERE p.decision='candidate' ORDER BY p.wallet_id""")]
            # Invalidate previous results before any network request on a rerun.
            for row in rows:
                conn.execute("""INSERT OR REPLACE INTO archive_candidate_verification
                    VALUES(?,?,?,?,?,?,?)""", (row['wallet_id'],row['address'],
                    row['tip_block'],time.time(),'pending','Ожидает проверки','{}'))
        totals = {}
        for index, row in enumerate(rows, 1):
            status, detail, evidence = verify_candidate(row, sleeper=sleeper)
            with db() as conn:
                current = conn.execute("""SELECT p.* FROM archive_activity_preview p
                    JOIN wallets w ON w.id=p.wallet_id AND w.address=p.address
                    WHERE p.wallet_id=?""", (row['wallet_id'],)).fetchone()
                if (not current or any(current[k] != row[k] for k in
                        ('address','decision','tip_block','cutoff_block','checked_at'))):
                    status, detail = 'unknown', 'Кошелёк или предварительная проверка изменились.'
                conn.execute("""UPDATE archive_candidate_verification
                    SET checked_at=?,status=?,detail=?,evidence=? WHERE wallet_id=?""",
                    (time.time(),status,detail,json.dumps(evidence),row['wallet_id']))
            totals[status] = totals.get(status, 0) + 1
            print(json.dumps({'processed':index,'total':len(rows),
                'wallet_id':row['wallet_id'],'status':status,'detail':detail,
                'totals':totals}, ensure_ascii=False), flush=True)
            sleeper(0.5)
        print('Проверка кандидатов завершена. Кошельки и Google Таблица не изменялись.', flush=True)
        return totals


def verification_report():
    with db() as conn:
        verification_schema(conn)
        return [dict(r) for r in conn.execute("""SELECT v.wallet_id,w.name,v.address,
            v.status,v.checked_at,v.detail,v.evidence FROM archive_candidate_verification v
            JOIN wallets w ON w.id=v.wallet_id AND w.address=v.address
            ORDER BY v.status,v.wallet_id""")]


def main():
    parser = argparse.ArgumentParser(description='NodeReal BSC archive activity preview')
    parser.add_argument('action',choices=('scan','list','verify','verification-report'))
    parser.add_argument('--limit',type=int,default=1)
    parser.add_argument('--requests',type=int,default=20)
    parser.add_argument('--wallet-id',type=int,default=None,
                        help='Select a specific existing wallet (does not archive it)')
    parser.add_argument('--refresh-completed',action='store_true',
                        help='Also recheck completed results older than one day; default finishes first pass')
    args=parser.parse_args()
    if args.action == 'verify':
        verify_candidates()
    elif args.action == 'verification-report':
        for row in verification_report():
            print(json.dumps(row, ensure_ascii=False))
    elif args.action == 'scan':
        print(json.dumps(scan(args.limit,args.requests,wallet_id=args.wallet_id,refresh_completed=args.refresh_completed),ensure_ascii=False))
    else:
        for row in report():
            when = (datetime.fromtimestamp(row['last_activity'],timezone.utc).isoformat()
                    if row['last_activity'] else 'unknown')
            print(f"{row['id']}: {row['name'][:50]} | {row['decision']} | "+
                  f"last={when} | requests={row['requests']}")


if __name__=='__main__':
    main()
