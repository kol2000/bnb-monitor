"""Read-only BSC activity assessment: preview only, never archive or move wallets.

Usage inside container: python archive_preview.py scan --limit 10
                        python archive_preview.py list

Each successful assessment checks normal transactions, token transfers and
internal transactions. Incomplete responses remain UNKNOWN, never candidates.
"""
import argparse
import json
import re
import sqlite3
import time
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app import db

API = 'https://blockscout.routescan.io/56/api/v2/addresses'
KINDS = ('transactions', 'token-transfers', 'internal-transactions')
INACTIVE_SECONDS = 90 * 86400
ADDRESS = re.compile(r'^0x[0-9a-fA-F]{40}$')


def schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS archive_activity_preview (
        wallet_id INTEGER PRIMARY KEY,
        address TEXT NOT NULL,
        checked_at REAL NOT NULL,
        last_activity REAL,
        decision TEXT NOT NULL CHECK(decision IN ('candidate','active','unknown')),
        detail TEXT NOT NULL
    )""")


def fetch(endpoint, address, *, opener=urlopen):
    """One bounded public indexer GET; never embed sensitive data in errors."""
    if endpoint not in KINDS or not ADDRESS.fullmatch(address):
        raise ValueError('Invalid indexer query.')
    req = Request(f'{API}/{address}/{endpoint}',
                  headers={'Accept': 'application/json', 'User-Agent': 'BNB-Monitor/1.0'})
    try:
        with opener(req, timeout=15) as resp:
            body = resp.read(600_001)
        if len(body) > 600_000:
            raise ValueError('Oversized indexer response')
        result = json.loads(body)
        if not isinstance(result, dict) or not isinstance(result.get('items'), list):
            raise ValueError('Unexpected indexer response')
        if len(result['items']) > 1000:
            raise ValueError('Oversized indexer page')
        return result
    except (HTTPError, URLError, TimeoutError, OSError, ValueError,
            UnicodeError, json.JSONDecodeError) as exc:
        # Do not copy external response bodies, request URL or tokens into DB.
        raise RuntimeError('Indexer unavailable or returned an invalid response (' +
                           type(exc).__name__ + ')') from None


def timestamp(item):
    if not isinstance(item, dict):
        raise ValueError('Invalid activity item')
    raw = item.get('timestamp')
    if not isinstance(raw, str):
        raise ValueError('Missing activity timestamp')
    parsed = datetime.fromisoformat(raw.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('Activity timestamp missing timezone')
    value = parsed.timestamp()
    if not 0 < value <= time.time() + 300:
        raise ValueError('Activity timestamp out of range')
    return value


def evaluate(address, *, get=fetch, now=None):
    """Conservatively classify wallet only when all indexer categories succeed."""
    if not ADDRESS.fullmatch(address):
        raise ValueError('Invalid BSC address')
    now = time.time() if now is None else now
    latest = []
    for kind in KINDS:
        page = get(kind, address)
        if not isinstance(page, dict) or not isinstance(page.get('items'), list):
            raise ValueError('Malformed indexer page')
        times = [timestamp(item) for item in page['items']]
        # Blockscout API v2 lists latest entries first. Refuse to infer an old
        # last activity when the first page isn't ordered newest-first.
        if times != sorted(times, reverse=True):
            raise ValueError('Indexer activity order is uncertain')
        if page.get('next_page_params') and not times:
            raise ValueError('Missing paged activity records')
        if times:
            latest.append(max(times))
    if not latest:
        return 'unknown', None, 'Нет операций в индексаторе; возраст адреса неизвестен.'
    last = max(latest)
    return ('candidate' if now - last >= INACTIVE_SECONDS else 'active',
            last, 'История обычных, внутренних и токен-транзакций.')


def scan(limit=10, *, get=fetch, sleeper=time.sleep):
    if not 1 <= limit <= 50:
        raise ValueError('Batch limit must be from 1 to 50')
    with db() as c:
        schema(c)
        wallets = [dict(r) for r in c.execute("""
            SELECT w.id,w.address FROM wallets w
            LEFT JOIN archive_activity_preview p ON p.wallet_id=w.id
            WHERE p.wallet_id IS NULL OR p.checked_at < ?
            ORDER BY COALESCE(p.checked_at, 0),w.id LIMIT ?
        """, (time.time() - 86400, limit))]
    counts = {'candidate': 0, 'active': 0, 'unknown': 0, 'failed': 0}
    for w in wallets:
        try:
            decision, last, detail = evaluate(w['address'], get=get)
        except (ValueError, RuntimeError, KeyError, TypeError) as exc:
            # Fail closed; never classify a failed indexer request as inactive.
            decision, last = 'unknown', None
            detail = 'Не удалось подтвердить историю: ' + type(exc).__name__
            counts['failed'] += 1
        with db() as c:
            schema(c)
            # A wallet could be removed/reimported by Sheets while scanning.
            exists = c.execute('SELECT address FROM wallets WHERE id=?', (w['id'],)).fetchone()
            if exists and exists['address'] == w['address']:
                c.execute("""INSERT INTO archive_activity_preview
                    (wallet_id,address,checked_at,last_activity,decision,detail)
                    VALUES (?,?,?,?,?,?)
                    ON CONFLICT(wallet_id) DO UPDATE SET
                    address=excluded.address,checked_at=excluded.checked_at,
                    last_activity=excluded.last_activity,
                    decision=excluded.decision,detail=excluded.detail""",
                    (w['id'], w['address'], time.time(), last, decision, detail))
                counts[decision] += 1
        sleeper(0.5)
    return {'scanned': len(wallets), **counts}


def report():
    with db() as c:
        schema(c)
        return [dict(r) for r in c.execute("""
            SELECT w.id,w.name,w.address,p.checked_at,p.last_activity,
                   p.decision,p.detail
            FROM wallets w JOIN archive_activity_preview p
              ON p.wallet_id=w.id AND p.address=w.address
            ORDER BY CASE p.decision WHEN 'candidate' THEN 0
                        WHEN 'unknown' THEN 1 ELSE 2 END,
                     p.last_activity,w.id
        """)]


def main():
    parser = argparse.ArgumentParser(description='BSC archive candidate preview (no archiving)')
    parser.add_argument('action', choices=('scan', 'list'))
    parser.add_argument('--limit', type=int, default=10)
    args = parser.parse_args()
    if args.action == 'scan':
        print(json.dumps(scan(args.limit), ensure_ascii=False))
    else:
        for row in report():
            print(f"{row['id']}: {row['name'][:50]} | {row['decision']} | "+
                  (datetime.fromtimestamp(row['last_activity'],timezone.utc).isoformat()
                   if row['last_activity'] else 'unknown'))


if __name__ == '__main__':
    main()
