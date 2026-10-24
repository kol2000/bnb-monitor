"""Restore encrypted files into a disposable directory and check SQLite, offline."""
import json
import os
from pathlib import Path, PurePosixPath
import sqlite3
import tarfile
import tempfile
from backup_crypto import transform


def verify(folder, key, temporary_root=None):
    folder = Path(folder)
    with tempfile.TemporaryDirectory(prefix='bnb-restore-', dir=temporary_root) as temp:
        root = Path(temp)
        for name in ('data.tar.gz', '.env'):
            transform(folder/(name+'.enc'), root/name, key, decrypt=True)
        # Never extract links, devices, duplicates or paths outside data/.
        with tarfile.open(root/'data.tar.gz', 'r:gz') as archive:
            members = archive.getmembers()
            seen = set()
            total = 0
            for m in members:
                path = PurePosixPath(m.name)
                if (path.is_absolute() or '..' in path.parts or not path.parts
                        or path.parts[0] != 'data' or m.name in seen
                        or not (m.isfile() or m.isdir())):
                    raise ValueError('Unsafe backup member')
                seen.add(m.name)
                total += m.size
            if total > 400*1024*1024:
                raise ValueError('Restore verification exceeds 400 MiB')
            archive.extractall(root, filter='data')
        database = root/'data'/'monitor.db'
        if not database.is_file():
            raise ValueError('Missing database')
        # Read/write only the disposable copy, so any journal can be recovered.
        with sqlite3.connect(database) as connection:
            results = connection.execute('PRAGMA integrity_check').fetchall()
            if results != [('ok',)]:
                raise ValueError('SQLite integrity check failed')
            tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {'wallets','events','settings','status'} <= tables:
                raise ValueError('Required database tables missing')
            count = connection.execute('SELECT count(*) FROM wallets').fetchone()[0]
        return {'integrity':'ok','wallets':count,'files':len(members)}


if __name__ == '__main__':
    os.umask(0o077)
    try:
        print(json.dumps(verify('/backup','/backup-encryption.key','/tmp')))
    except Exception:
        # No archive member names, credentials, SQL or plaintext contents in logs.
        print('Проверка восстановления не пройдена: ключ, архив или база некорректны.')
        raise SystemExit(1)
