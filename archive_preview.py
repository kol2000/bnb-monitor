"""Conservative, read-only BSC activity preview powered by NodeReal.

No wallet state changes, balance skips, transactions or Sheet updates.
Scan small batches manually: python archive_preview.py scan --requests 20
Review results in the dedicated web tab or run: python archive_preview.py list
Key: /data/nodereal-api-key (mode 0600; never in GitHub or API output).
"""
import argparse
import json
import os
import re
import time
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app import DATA, db

API_BASE = 'https://bsc-mainnet.nodereal.io/v1/'
KINDS = ['external', 'internal', '20', '721', '1155']
DIRECTIONS = ('fromAddress', 'toAddress')
DAYS = 90
WINDOW = 100000  # NodeReal maximum difference between fromBlock and toBlock
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
        requests INTEGER NOT NULL DEFAULT 0
    )""")


def key_from_file():
    path = DATA / 'nodereal-api-key'
    try:
        key = path.read_text(encoding='ascii').strip()
    except OSError:
        raise IndexerError('NodeReal key not installed: /data/nodereal-api-key') from None
    if not re.fullmatch(r'[A-Za-z0-9_-]{8,256}', key):
        raise IndexerError('Invalid NodeReal key file')
    return key


def rpc(method, params, *, opener=urlopen):
    if method not in ('eth_chainId','eth_blockNumber','eth_getBlockByNumber','nr_getAssetTransfers'):
        raise ValueError('Not a permitted read-only RPC method')
    key = key_from_file()
    request = Request(API_BASE + key,
                      data=json.dumps({'jsonrpc':'2.0','id':1,'method':method,
                                       'params':params}).encode(),
                      headers={'Content-Type':'application/json','Accept':'application/json',
                               'User-Agent':'BNB-Monitor/1.0'})
    try:
        with opener(request, timeout=20) as res:
            data = res.read(600001)
        if len(data) > 600000:
            raise ValueError('oversized payload')
        value = json.loads(data)
        if not isinstance(value, dict) or 'result' not in value or value.get('error'):
            raise ValueError('API error')
        return value['result']
    except (HTTPError, URLError, TimeoutError, OSError, ValueError,
            UnicodeError, TypeError) as exc:
        # Never expose secret URL from urllib exceptions or raw responses.
        raise IndexerError('NodeReal request failed ('+type(exc).__name__+
                           '); check API quota / connection / indexer lag') from None


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
         tip_block,tip_timestamp,cursor_block,direction,requests)
        VALUES(?,?,?,'scanning','Сканирование 90 дней',?,?,?,?,0,0)
        ON CONFLICT(wallet_id) DO UPDATE SET
        address=excluded.address,checked_at=excluded.checked_at,
        last_activity=NULL,decision='scanning',detail='Сканирование 90 дней',
        cutoff_block=excluded.cutoff_block,tip_block=excluded.tip_block,
        tip_timestamp=excluded.tip_timestamp,cursor_block=excluded.cursor_block,
        direction=0,requests=0""",
        (wallet['id'],wallet['address'],time.time(),cutoff,tip,stamp,tip))


def step(wallet, row):
    """One transfer API call, followed by a checkpoint in SQLite."""
    direction = DIRECTIONS[row['direction']]
    high = row['cursor_block']
    low = max(row['cutoff_block'], high-WINDOW)
    params = {'category': KINDS, direction: wallet['address'],
              'fromBlock':hex(low),'toBlock':hex(high),
              'order':'desc','maxCount':'0x1','excludeZeroValue':False}
    result = rpc('nr_getAssetTransfers',[params])
    activity = transfer_result(result,wallet['address'],direction,low,high)
    with db() as conn:
        live = conn.execute('SELECT address FROM wallets WHERE id=?',
                            (wallet['id'],)).fetchone()
        if not live or live['address'] != wallet['address']:
            return 'removed'
        if activity is not None:
            conn.execute("""UPDATE archive_activity_preview SET
                decision='active',last_activity=?,detail='Операция за последние 90 дней',
                checked_at=?,requests=requests+1 WHERE wallet_id=?""",
                (activity,time.time(),wallet['id']))
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


def scan(limit=1, requests=20, sleeper=time.sleep):
    """Bounded manual execution; unfinished wallets resume at their checkpoint."""
    if not 1 <= limit <= 10 or not 1 <= requests <= 100:
        raise ValueError('Use --limit 1..10 and --requests 1..100')
    tip, tip_stamp = safe_tip()
    now = time.time()
    cutoff = find_cutoff(tip,now)
    used = 0
    completed = {'active':0,'candidate':0,'unknown':0,'failed':0}
    with db() as conn:
        schema(conn)
        wallets = [dict(r) for r in conn.execute("""
            SELECT w.id,w.address,p.decision,p.checked_at,p.address AS scanned_address
            FROM wallets w LEFT JOIN archive_activity_preview p ON w.id=p.wallet_id
            WHERE p.wallet_id IS NULL OR p.decision='scanning'
               OR p.address!=w.address
               OR (p.decision IN ('active','candidate','unknown')
                   AND p.checked_at<?)
            ORDER BY CASE WHEN p.decision='scanning' THEN 0 ELSE 1 END,
                     COALESCE(p.checked_at,0),w.id LIMIT ?""",
            (now-86400,limit))]
    for wallet in wallets:
        with db() as conn:
            schema(conn)
            row = conn.execute('SELECT * FROM archive_activity_preview WHERE wallet_id=?',
                               (wallet['id'],)).fetchone()
            if row is None or row['address'] != wallet['address'] or row['decision']!='scanning':
                start_wallet(conn,wallet,tip,tip_stamp,cutoff)
                row = conn.execute('SELECT * FROM archive_activity_preview WHERE wallet_id=?',
                                   (wallet['id'],)).fetchone()
            row = dict(row)
        while used < requests:
            try:
                outcome = step(wallet,row)
            except (IndexerError,ValueError,TypeError,KeyError):
                # Keep the cursor unchanged. No false candidate on an API error.
                with db() as conn:
                    conn.execute("""UPDATE archive_activity_preview SET
                        decision='unknown',detail='Ошибка NodeReal; сканирование не подтверждено',
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
    return {'rpc_transfer_requests':used,'wallets_selected':len(wallets),**completed,
            'note':'preview only; no wallets archived'}


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


def main():
    parser = argparse.ArgumentParser(description='NodeReal BSC archive activity preview')
    parser.add_argument('action',choices=('scan','list'))
    parser.add_argument('--limit',type=int,default=1)
    parser.add_argument('--requests',type=int,default=20)
    args=parser.parse_args()
    if args.action == 'scan':
        print(json.dumps(scan(args.limit,args.requests),ensure_ascii=False))
    else:
        for row in report():
            when = (datetime.fromtimestamp(row['last_activity'],timezone.utc).isoformat()
                    if row['last_activity'] else 'unknown')
            print(f"{row['id']}: {row['name'][:50]} | {row['decision']} | "+
                  f"last={when} | requests={row['requests']}")


if __name__=='__main__':
    main()
