"""Install HMAC credentials from a terminal, without shell arguments or logs."""
import getpass
import json
import os
from pathlib import Path
import subprocess
import sys
import warnings


def main():
    os.chdir(Path(__file__).resolve().parent)
    if sys.argv[1:] == ['--check']:
        code = """from binance import Client,account,networks,history,BinanceError
c=Client()
for name,fn in [
 ('Права ключа',lambda:{k:v for k,v in c.call('/sapi/v1/account/apiRestrictions').items() if k in ('enableReading','enableSpotAndMarginTrading','enableWithdrawals','ipRestrict')}),
 ('Спотовый баланс',lambda:[b for b in account(c)['balances'] if b['asset'] in ('BNB','USDT')]),
 ('Сеть BSC',lambda:[{k:n.get(k) for k in ('network','withdrawEnable','withdrawMin','withdrawFee')} for n in networks(c)]),
 ('История BNB',lambda:{'записей':len(history(c))}),
 ('Travel Rule',lambda:c.call('/sapi/v1/localentity/questionnaire-requirements'))]:
 try: print(name,fn())
 except BinanceError as e: print(name,str(e))
"""
        command = (['sudo'] if os.geteuid() != 0 else []) + ['docker','compose','exec','-T','web','python','-c',code]
        raise SystemExit(subprocess.run(command).returncode)
    if sys.argv[1:]:
        raise SystemExit('Использование: python3 configure_binance.py [--check]')
    if not sys.stdin.isatty():
        raise SystemExit('Запустите в интерактивном терминале VM.')
    warnings.simplefilter('error', getpass.GetPassWarning)
    try:
        key = getpass.getpass('Binance API Key (ввод скрыт): ').strip()
        secret = getpass.getpass('Binance Secret Key (ввод скрыт): ').strip()
    except (getpass.GetPassWarning, KeyboardInterrupt, EOFError):
        raise SystemExit('Ввод отменён; ключи не изменены.') from None
    if not all(16 <= len(v) <= 512 and v.isascii() and v.isalnum() for v in (key, secret)):
        raise SystemExit('Ожидаются полный API Key и Secret системного HMAC-ключа Binance.')
    code = '''import os,sys,tempfile,json
from pathlib import Path
from app import DATA
raw=sys.stdin.buffer.read(4096)
json.loads(raw)
fd,tmp=tempfile.mkstemp(dir=DATA,prefix='.binance-key-')
try:
    with os.fdopen(fd,'wb') as f:
        f.write(raw)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp,DATA/'binance-api.json')
finally:
    if os.path.exists(tmp): os.unlink(tmp)
'''
    command = (['sudo'] if os.geteuid() != 0 else []) + ['docker','compose','exec','-T','web','python','-c',code]
    result = subprocess.run(command,input=json.dumps({'api_key':key,'api_secret':secret}).encode(),capture_output=True)
    if result.returncode:
        raise SystemExit('Не удалось установить ключ. Проверьте, что контейнер web запущен. Секреты не выведены.')
    print('Ключи Binance сохранены в Docker с правами 600. Откройте вкладку «Биржа Binance» → «Проверить баланс и выводы».')


if __name__ == '__main__':
    main()
