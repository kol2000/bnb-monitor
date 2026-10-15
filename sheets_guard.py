"""Serialize application-side sheet reads followed by writes across processes."""
import fcntl
from contextlib import contextmanager
from functools import wraps

@contextmanager
def sheet_guard(archive=False):
    from app import DATA, RemoteError, db
    with (DATA/'sheets-write.lock').open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            raise RemoteError('Google Таблица сейчас обновляется приложением. Повторите чуть позже.') from None
        try:
            if not archive:
                with db() as c:
                    if c.execute("SELECT 1 FROM archive_jobs WHERE state IN ('moving','uncertain')").fetchone():
                        raise RemoteError('Сначала проверьте результат переноса в архив; запись в Google временно приостановлена.')
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)

def serialized_sheets(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        with sheet_guard():
            return fn(*args, **kwargs)
    return wrapper
