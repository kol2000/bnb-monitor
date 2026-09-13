"""Install a local service-account key into the Docker volume, never into git."""
import json
import os
from pathlib import Path
import subprocess
import sys

os.chdir(Path(__file__).resolve().parent)
path = Path(sys.argv[1]).expanduser() if len(sys.argv) == 2 else Path(input('Полный путь к JSON-ключу Google: ').strip()).expanduser()
try:
    raw = path.read_bytes()
    info = json.loads(raw)
    if (len(raw) > 65536 or info.get('type') != 'service_account'
            or not info.get('private_key') or not info.get('client_email')
            or info.get('token_uri') != 'https://oauth2.googleapis.com/token'
            or info.get('universe_domain', 'googleapis.com') != 'googleapis.com'):
        raise ValueError()
except Exception:
    raise SystemExit('Не удалось прочитать корректный JSON-ключ сервисного аккаунта Google.') from None
code = '''import os,sys,tempfile
from pathlib import Path
p=Path('/data/google-service-account.json')
fd,tmp=tempfile.mkstemp(dir='/data',prefix='.google-key-')
try:
    with os.fdopen(fd,'wb') as f:
        f.write(sys.stdin.buffer.read())
    os.replace(tmp,p)
finally:
    if os.path.exists(tmp): os.unlink(tmp)
'''
cmd = (['sudo'] if os.geteuid() != 0 else []) + ['docker','compose','exec','-T','web','python','-c',code]
result = subprocess.run(cmd, input=raw)
if result.returncode:
    raise SystemExit('Ключ не установлен. Сначала запустите контейнеры: sudo docker compose up -d --build')
print('Ключ установлен в том Docker с правами 600. Дайте доступ «Читатель» адресу:')
print(info['client_email'])
print('Затем в панели укажите ссылку на таблицу и название вкладки, включите Google и сохраните.')
