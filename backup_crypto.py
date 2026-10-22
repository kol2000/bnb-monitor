"""Streaming AES-256-GCM backups; authenticate before publishing decrypted output."""
import os
from pathlib import Path
import sys
import tempfile
from Crypto.Cipher import AES

MAGIC = b'BNBBAK01'
CHUNK = 1024 * 1024

def transform(source, target, keyfile, decrypt=False):
    source, target, keyfile = map(Path, (source, target, keyfile))
    if target.exists():
        raise ValueError('Output already exists')
    key = keyfile.read_bytes()
    if len(key) != 32 or keyfile.stat().st_mode & 0o077:
        raise ValueError('Invalid key or permissions')
    fd, temporary = tempfile.mkstemp(dir=target.parent, prefix='.backup-')
    try:
        with source.open('rb') as src, os.fdopen(fd, 'wb') as out:
            if decrypt:
                if src.read(8) != MAGIC:
                    raise ValueError('Unknown backup format')
                nonce = src.read(12)
                remaining = source.stat().st_size - 36
                if remaining < 0:
                    raise ValueError('Truncated backup')
            else:
                nonce = os.urandom(12)
                out.write(MAGIC + nonce)
                remaining = source.stat().st_size
            cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
            cipher.update(MAGIC)
            while remaining:
                data = src.read(min(CHUNK, remaining))
                if not data:
                    raise ValueError('Truncated backup')
                out.write(cipher.decrypt(data) if decrypt else cipher.encrypt(data))
                remaining -= len(data)
            if decrypt:
                cipher.verify(src.read(16))
            else:
                out.write(cipher.digest())
            out.flush()
            os.fsync(out.fileno())
        # Do not overwrite any existing output, even if created during encryption.
        os.link(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)

if __name__ == '__main__':
    try:
        mode, key, folder = sys.argv[1:]
        folder = Path(folder)
        for name in ('data.tar.gz', '.env'):
            if mode == 'encrypt':
                transform(folder/name, folder/(name+'.enc'), key)
                (folder/name).unlink()
            elif mode == 'decrypt':
                transform(folder/(name+'.enc'), folder/name, key, True)
            else:
                raise ValueError('Unknown mode')
    except Exception:
        print('Ошибка шифрования/проверки резервной копии. Проверьте ключ, права и свободное место.', file=sys.stderr)
        raise SystemExit(1)
