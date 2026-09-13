"""Interactive local configuration; never send passwords to anybody."""
import getpass
import ipaddress
import os
from pathlib import Path
import secrets

os.chdir(Path(__file__).resolve().parent)
path = Path('.env')
if path.exists():
    raise SystemExit('.env уже существует. Для изменения откройте его: nano .env')
print('Настройка BNB Monitor. Пароль вводится скрыто.')
while True:
    password = getpass.getpass('Пароль панели (от 12 символов): ')
    if len(password) < 12 or any(c in password for c in "'\n\r"):
        print('Нужно минимум 12 символов; без одинарной кавычки и переноса строки.')
        continue
    if password == getpass.getpass('Повторите пароль: '):
        break
    print('Пароли не совпали.')
while True:
    bind = input('Локальный IPv4 этой VM (например 192.168.1.130): ').strip()
    try:
        ip = ipaddress.IPv4Address(bind)
        if not ip.is_private or ip.is_unspecified or ip.is_multicast:
            raise ValueError()
        break
    except ValueError:
        print('Введите адрес VM в локальной сети, не внешний адрес и не 0.0.0.0.')
with path.open('x', encoding='utf-8') as out:
    os.chmod(path, 0o600)
    out.write(f"ADMIN_PASSWORD='{password}'\nSECRET_KEY={secrets.token_hex(32)}\nBIND_IP={bind}\nPORT=8080\nCOOKIE_SECURE=0\n")
print(f'Готово. Запустите: sudo docker compose up -d --build\nПанель: http://{bind}:8080')
