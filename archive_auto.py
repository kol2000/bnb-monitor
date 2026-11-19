"""Scheduled archive batches; no fund transfers. Run through the host runner."""
import fcntl
import json
import os
import sys
import time
import uuid
from contextlib import contextmanager
import archive_approved as move
import archive_preview as preview
from archive_budget import guard, BudgetPause
from app import DATA, db, settings, RemoteError

PLAN = DATA / 'archive-auto-plan.json'
JOURNAL = DATA / 'archive-auto-move.json'
RECHECK = 7 * 86400


def atomic(path, data):
    tmp = path.with_suffix('.tmp')
    with tmp.open('w') as f:
        json.dump(data, f); f.flush(); os.fsync(f.fileno())
    os.replace(tmp, path)
    fd = os.open(path.parent, os.O_RDONLY)
    try: os.fsync(fd)
    finally: os.close(fd)


@contextmanager
def lock():
    with (DATA / 'archive-verification.lock').open('a') as f:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def eligible(status, evidence):
    try:
        return (status in ('verified_snapshot', 'has_balance') and
                0 <= int(evidence.get('bnb_wei', '-1')) <= move.MAX_BNB and
                0 <= int(evidence.get('usdt_units', '-1')) <= move.MAX_USDT)
    except (ValueError, TypeError):
        return False


def prepare():
    cfg = settings()
    if not cfg['sheets_enabled'] or cfg['sheets_tab'] != move.SOURCE:
        raise RemoteError('Автоархив: требуется синхронизация листа bnb кошельки.')
    with lock():
        # A lost response is reconciled before any new scan or move.
        if JOURNAL.exists() or PLAN.exists():
            return 10
        now = time.time()
        with db() as c:
            preview.schema(c)
            c.execute('CREATE TABLE IF NOT EXISTS archive_auto_attempts (wallet_id INTEGER PRIMARY KEY, address TEXT, checked REAL, result TEXT)')
            candidates = [dict(r) for r in c.execute('''SELECT p.* FROM archive_activity_preview p
                JOIN wallets w ON w.id=p.wallet_id AND w.address=p.address
                LEFT JOIN archive_auto_attempts a ON a.wallet_id=w.id AND a.address=w.address
                WHERE p.decision='candidate' AND COALESCE(a.checked,0)<?
                ORDER BY COALESCE(a.checked,0),w.id LIMIT 5''', (now-RECHECK,))]
        selected = {}
        for row in candidates:
            status, detail, evidence = preview.verify_candidate(row)
            with db() as c:
                c.execute('INSERT OR REPLACE INTO archive_auto_attempts VALUES(?,?,?,?)',
                          (row['wallet_id'], row['address'], time.time(), status))
            advance_checkpoint(row, status, evidence)
            if eligible(status, evidence):
                selected[str(row['wallet_id'])] = row['address'].lower()
        if selected:
            atomic(PLAN, {'spreadsheet': cfg['sheets_id'], 'selected': selected})
            print('Автоархив: подготовлено', len(selected), flush=True)
            return 10
        # Completed previews are refreshed weekly, partial scans resume first.
        with db() as c:
            row = c.execute('''SELECT w.id FROM wallets w
                LEFT JOIN archive_activity_preview p ON p.wallet_id=w.id
                WHERE p.wallet_id IS NULL OR p.address!=w.address OR p.decision='scanning'
                   OR p.scan_method!='count' OR (p.decision!='candidate' AND p.checked_at<?)
                ORDER BY CASE WHEN p.decision='scanning' THEN 0 ELSE 1 END,
                    COALESCE(p.checked_at,0),w.id LIMIT 1''', (now-RECHECK,)).fetchone()
        if row:
            result = preview.scan(wallet_id=row['id'], requests=100)
            # Do not emit upstream error text or credential-containing payloads.
            print('Автоархив: проверка', row['id'], 'шагов', result['rpc_transfer_requests'],
                  'ошибок', result['failed'], flush=True)
        else:
            print('Автоархив: очередь актуальна.', flush=True)
        return 0


def advance_checkpoint(row, status, evidence):
    # Only these verifier results prove a completely empty tail in both directions.
    # Retain a 50k-block overlap; revalidate the evidence block before persisting.
    if status not in ('age_unknown', 'verified_snapshot', 'has_balance'):
        return
    if not all(k in evidence for k in ('head', 'head_timestamp', 'head_hash')):
        return
    head = evidence['head']
    if preview.verification_block(head) != (evidence['head_timestamp'], evidence['head_hash']):
        return
    tip = max(row['tip_block'], head-preview.INDEXER_LAG_BLOCKS)
    with db() as c:
        c.execute("""UPDATE archive_activity_preview SET tip_block=?, checked_at=?
            WHERE wallet_id=? AND address=? AND tip_block=? AND decision='candidate'
            AND EXISTS(SELECT 1 FROM wallets w WHERE w.id=wallet_id AND w.address=archive_activity_preview.address)""",
            (tip,time.time(),row['wallet_id'],row['address'],row['tip_block']))


def apply():
    # Host has stopped web/worker; main rechecks every selected wallet and idle state.
    move.JOURNAL = JOURNAL
    if JOURNAL.exists():
        move.APPROVED = {}
    else:
        plan = json.loads(PLAN.read_text())
        if plan['spreadsheet'] != settings()['sheets_id']:
            raise RemoteError('Изменилась Google Таблица.')
        move.APPROVED = {int(k): v for k, v in plan['selected'].items()}
    move.main(allow_empty=True)
    if JOURNAL.exists():
        j = json.loads(JOURNAL.read_text())
        if j.get('state') != 'done' or j.get('cleanup') != 'done':
            raise RemoteError('Перенос требует сверки; следующая партия заблокирована.')
        # Preserve durable audit records, but allow the next independent batch.
        # Delete plan first: a crash leaves the completed journal to reconcile.
        PLAN.unlink(missing_ok=True)
        os.replace(JOURNAL, DATA / ('archive-auto-done-' + j['job'] + '.json'))
    else:
        PLAN.unlink(missing_ok=True)  # Every candidate failed the fresh pre-move check.
    print('Автоархив: партия завершена.', flush=True)


if __name__ == '__main__':
    os.umask(0o077)
    preview.RPC_GUARD = guard
    try:
        if sys.argv[1:] == ['prepare']:
            raise SystemExit(prepare())
        if sys.argv[1:] == ['apply']:
            apply()
        else:
            raise ValueError('Use prepare or apply')
    except BudgetPause as exc:
        print(str(exc), flush=True)
        raise SystemExit(0)
    except Exception as exc:
        print('Автоархив остановлен:', str(exc) if isinstance(exc, RemoteError) else type(exc).__name__, flush=True)
        raise SystemExit(1)
