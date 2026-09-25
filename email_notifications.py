"""SMTP transport. Credentials never appear in errors or messages."""
import re
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

DEFAULTS = dict(email_enabled=False, smtp_host='smtp.mail.ru', smtp_port=465,
                smtp_security='ssl', smtp_username='', smtp_password='',
                email_from='', email_to='')

class EmailError(Exception):
    pass

def validate_settings(data, current):
    cfg = {k: current.get(k, v) for k, v in DEFAULTS.items()}
    for key in DEFAULTS:
        if key in data and key != 'smtp_password':
            cfg[key] = data[key]
    if type(cfg['email_enabled']) is not bool:
        raise ValueError('Неверное значение email.')
    for key in ('smtp_host', 'smtp_username', 'email_from', 'email_to', 'smtp_security'):
        if not isinstance(cfg[key], str) or len(cfg[key]) > 254 or any(ord(c) < 32 for c in cfg[key]):
            raise ValueError('Некорректные настройки SMTP.')
        cfg[key] = cfg[key].strip()
    try:
        cfg['smtp_port'] = int(cfg['smtp_port'])
    except (ValueError, TypeError):
        raise ValueError('Порт SMTP должен быть числом.')
    if not 1 <= cfg['smtp_port'] <= 65535 or cfg['smtp_security'] not in ('ssl', 'starttls'):
        raise ValueError('Укажите порт SMTP и SSL/TLS или STARTTLS.')
    if cfg['smtp_host'] and not re.fullmatch(r'[A-Za-z0-9.-]+', cfg['smtp_host']):
        raise ValueError('SMTP-сервер: только имя хоста, без https:// и порта.')
    for key in ('email_from', 'email_to'):
        value = cfg[key]
        if value and not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+", value):
            raise ValueError('Укажите один корректный email отправителя и получателя.')
    password = data.get('smtp_password', '')
    if not isinstance(password, str) or len(password) > 1024 or '\r' in password or '\n' in password:
        raise ValueError('Некорректный пароль приложения.')
    if password:
        cfg['smtp_password'] = password
    if data.get('clear_smtp_password'):
        cfg['smtp_password'] = ''
    if cfg['email_enabled'] and any(not cfg[k] for k in
            ('smtp_host', 'smtp_username', 'smtp_password', 'email_from', 'email_to')):
        raise ValueError('Для email заполните SMTP, логин, пароль, отправителя и получателя.')
    return cfg

def send_email(cfg, text, message_id=None):
    if any(not cfg.get(k) for k in ('smtp_host', 'smtp_username', 'smtp_password', 'email_from', 'email_to')):
        raise EmailError('Сначала сохраните все настройки email.')
    msg = EmailMessage()
    msg['Subject'] = 'BNB Monitor — уведомление'
    msg['From'] = cfg['email_from']
    msg['To'] = cfg['email_to']
    msg['Date'] = formatdate(localtime=False)
    msg['Message-ID'] = message_id or make_msgid()
    msg.set_content(text)
    context = ssl.create_default_context()
    try:
        if cfg['smtp_security'] == 'ssl':
            smtp = smtplib.SMTP_SSL(cfg['smtp_host'], cfg['smtp_port'], timeout=12, context=context)
        else:
            smtp = smtplib.SMTP(cfg['smtp_host'], cfg['smtp_port'], timeout=12)
        try:
            if cfg['smtp_security'] == 'starttls':
                smtp.ehlo()
                smtp.starttls(context=context)
                smtp.ehlo()
            smtp.login(cfg['smtp_username'], cfg['smtp_password'])
            smtp.send_message(msg)
        finally:
            smtp.close()
    except smtplib.SMTPAuthenticationError:
        raise EmailError('SMTP отклонил вход. Проверьте логин и пароль приложения.') from None
    except Exception:
        raise EmailError('Письмо не отправлено: проверьте доступ к SMTP, порт, TLS и адреса.') from None
