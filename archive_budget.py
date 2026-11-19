"""Conservative local CU budget for automatic archive RPC only."""
import time
from app import db

class BudgetPause(Exception):
    pass


def reserve(method, now=None):
    now = time.time() if now is None else now
    # Charge every attempt, including failures; ordinary RPC is overestimated.
    cost = 250 if method == 'nr_getAssetTransfers' else 150
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        c.execute('CREATE TABLE IF NOT EXISTS archive_cu_usage (stamp REAL, cost INTEGER)')
        c.execute('CREATE TABLE IF NOT EXISTS archive_cu_state (id INTEGER PRIMARY KEY, started REAL, next_request REAL)')
        c.execute('INSERT OR IGNORE INTO archive_cu_state VALUES(1,?,0)', (now,))
        state = c.execute('SELECT * FROM archive_cu_state WHERE id=1').fetchone()
        if now < state['started']:
            raise BudgetPause('Часы сервера переведены назад; расход CU приостановлен.')
        initial = now-state['started'] < 31*86400
        daily_limit = 55000 if initial else 180000
        window_limit = 1800000 if initial else 6000000
        c.execute('DELETE FROM archive_cu_usage WHERE stamp<?', (now-31*86400,))
        rolling, daily = c.execute('SELECT COALESCE(SUM(cost),0), COALESCE(SUM(CASE WHEN stamp>? THEN cost ELSE 0 END),0) FROM archive_cu_usage', (now-86400,)).fetchone()
        if rolling+cost > window_limit or daily+cost > daily_limit:
            raise BudgetPause('Пауза автоархива: достигнут бюджет CU; продолжение автоматически после освобождения бюджета.')
        if state['next_request'] > now:
            return state['next_request']-now
        c.execute('INSERT INTO archive_cu_usage VALUES(?,?)', (now,cost))
        c.execute('UPDATE archive_cu_state SET next_request=? WHERE id=1', (now+cost/150+0.15,))
    return 0


def guard(method):
    while True:
        delay = reserve(method)
        if not delay:
            return
        time.sleep(min(delay, 2))
