"""Read recent updates from your bot to find your chat ID; no token is saved."""
import getpass
import json
import re
from urllib.request import urlopen

print('Сначала откройте вашего бота в Telegram и отправьте ему /start.')
token = getpass.getpass('Токен бота от BotFather (ввод скрыт): ').strip()
if not re.fullmatch(r'\d+:[A-Za-z0-9_-]{20,}', token):
    raise SystemExit('Неверный формат токена.')
try:
    with urlopen('https://api.telegram.org/bot' + token + '/getUpdates', timeout=15) as r:
        data = json.load(r)
    if not data.get('ok'):
        raise ValueError()
except Exception:
    raise SystemExit('Не удалось получить сообщения. Проверьте токен и доступ к Telegram. Не используйте бота с активным webhook.') from None
seen = set()
for update in data.get('result', []):
    message = update.get('message', {})
    chat = message.get('chat', {})
    if chat.get('id') and chat['id'] not in seen:
        seen.add(chat['id'])
        print('Chat ID:', chat['id'], '|', chat.get('type', ''), '|',
              chat.get('title') or chat.get('first_name', ''), '|', chat.get('username', ''))
if not seen:
    print('Сообщений нет. Отправьте боту новое сообщение и повторите. Рекомендуется отдельный бот только для мониторинга.')
else:
    print('Выберите именно ваш личный чат и скопируйте его ID в настройки панели.')
