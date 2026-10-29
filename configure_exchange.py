"""Install Gate/Bybit HMAC credentials locally on the VM using hidden input."""
import getpass
import json
import os
from pathlib import Path
import subprocess
import sys
import warnings


def main():
    if len(sys.argv) not in (2,3) or sys.argv[1] not in ('gate','bybit') or (len(sys.argv)==3 and sys.argv[2]!='--check'):
        raise SystemExit('Использование: python3 configure_exchange.py gate|bybit [--check]')
    venue=sys.argv[1]
    os.chdir(Path(__file__).resolve().parent)
    command=(['sudo'] if os.geteuid()!=0 else [])+['docker','compose','exec','-T','web','python','-c']
    if len(sys.argv)==3:
        code='''import sys
from exchange_clients import Client
c=Client(sys.argv[1])
for title,fn in [('Доступный баланс',c.balances),('BNB/BSC',c.network),('Рынок BNB/USDT',c.market),('История',lambda:{'записей':len(c.history())})]:
 try: print(title,fn())
 except Exception as e: print(title,type(e).__name__,str(e) if isinstance(e,ValueError) else 'Неожиданный формат ответа API')
'''
        raise SystemExit(subprocess.run(command+[code,venue]).returncode)
    if not sys.stdin.isatty():raise SystemExit('Запустите в интерактивном терминале VM.')
    warnings.simplefilter('error',getpass.GetPassWarning)
    try:
        key=getpass.getpass(venue+' API Key (ввод скрыт): ').strip()
        secret=getpass.getpass(venue+' Secret (ввод скрыт): ').strip()
    except (getpass.GetPassWarning,KeyboardInterrupt,EOFError):raise SystemExit('Отменено. Ключи не изменены.') from None
    if not all(8<=len(v)<=512 and v.isascii() and not any(ch.isspace() for ch in v) for v in (key,secret)):
        raise SystemExit('Неверный формат HMAC-ключа.')
    code='''import sys,json,os,tempfile
from app import DATA,db
from exchanges import locked
v=sys.argv[1]
assert v in ('gate','bybit')
raw=sys.stdin.buffer.read(4096)
json.loads(raw)
with locked(v):
 with db() as c:
  if c.execute("SELECT 1 FROM exchange_jobs WHERE venue=? AND state IN ('ordering','bought','withdrawing')",(v,)).fetchone():
   raise SystemExit('Есть незавершённые операции. Сначала сверьте их.')
 fd,tmp=tempfile.mkstemp(dir=DATA,prefix='.'+v+'-key-')
 try:
  with os.fdopen(fd,'wb') as f:
   f.write(raw); f.flush(); os.fsync(f.fileno())
  os.replace(tmp,DATA/(v+'-api.json'))
 finally:
  if os.path.exists(tmp):os.unlink(tmp)
'''
    result=subprocess.run(command+[code,venue],input=json.dumps({'api_key':key,'api_secret':secret}).encode(),capture_output=True)
    if result.returncode:raise SystemExit('Ключи не изменены. Проверьте контейнер web и завершите активные операции этой биржи.')
    print('Ключи '+venue+' сохранены с правами 600. Теперь: python3 configure_exchange.py '+venue+' --check')

if __name__=='__main__':main()
