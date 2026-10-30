import ast
import errno
import sqlite3
import unittest
from pathlib import Path

# Isolate the pure formatter without importing the application/database.
tree = ast.parse(Path('worker.py').read_text())
node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'monitoring_error')
scope = {'errno': errno, 'sqlite3': sqlite3}
exec(compile(ast.Module(body=[node], type_ignores=[]), 'worker.py', 'exec'), scope)
format_error = scope['monitoring_error']

class MonitoringErrors(unittest.TestCase):
    def test_stage_and_no_secret(self):
        text = format_error('Запись балансов в Google Таблицу', ValueError('secret-token'))
        self.assertIn('Запись балансов в Google Таблицу', text)
        self.assertIn('ValueError', text)
        self.assertNotIn('secret-token', text)
        self.assertNotIn('диск', text)

    def test_actual_disk_full(self):
        self.assertIn('нет свободного места', format_error('Проверка', OSError(errno.ENOSPC, 'secret')))

    def test_database_busy(self):
        exc = sqlite3.OperationalError('secret')
        exc.sqlite_errorcode = sqlite3.SQLITE_BUSY
        self.assertIn('база данных занята', format_error('Проверка', exc))

    def test_permission_and_timeout(self):
        self.assertIn('нет прав', format_error('Проверка', PermissionError('secret')))
        self.assertIn('тайм-аут', format_error('Проверка', TimeoutError('secret')))
