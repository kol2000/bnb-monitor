# Проверка версии 1.1

- 24 автоматических теста Python: PASS (15 мониторинга и 9 Google-синхронизации).
- Google-синхронизация проверена на подставных ответах: сопоставление A/B,
  переименование без потери баланса, повторы, конфликт имён, пустая таблица,
  сетевой сбой, лимит импорта и смена настроек во время запроса.
- Живой Google API не проверен: сервисный аккаунт ещё не настроен на VM.
- Проверка синтаксиса JavaScript `node --check`: PASS.
- Компиляция модулей Python: PASS.
- Разбор compose.yaml и наличие web/worker: PASS.
- Запуск Gunicorn и загрузка приложения: PASS.
- Публичный BSC RPC ответил chain ID 56 и завершённым блоком при отдельных
  запросах. Полный живой цикл из этой среды подтвердить не удалось: один из
  последующих запросов завершился сетевой ошибкой. Рабочий цикл покрыт mock-тестами.
- Реальные сообщения Telegram не отправлялись: бот пользователя не настроен.
- Docker-сборка не запускалась: Docker отсутствует в среде разработки.
- Браузерная визуальная проверка не завершена: загрузка Chromium завершалась
  тайм-аутом. Это ограничение проверки, а не подтверждение корректности в браузере.

Приёмка на VM: `docker compose up -d --build`, проверить оба контейнера,
войти в панель, добавить один публичный адрес, дождаться баланса и проверить
его по BscScan, настроить Telegram и нажать тестовую отправку. Не используйте
монитор как единственный источник информации о финансовых операциях.


## Семь улучшений обслуживания — 15.10.2026

- 141 Python-тест: вся существующая проверка плюс автоматическая сверка MEXC без повторных POST, задержка повторов при ошибке, повторное получение баланса, предпросмотр удаления и защита от устаревшего подтверждения, отмена уведомлений при сохранении истории, блокировка удаления при активном переводе, авторизация/CSRF, отсутствие секретов в результатах аудита.
- Резервное копирование/обновление проверены с имитацией Docker/Git: остановка писателей до копии, запуск после ошибки копирования, запрет обновления без копии, возврат образа при неудаче, сохранение актуальной базы при откате, Git от пользователя при запуске через sudo.
- `node tests/test_wallet_ui.js`: поиск без учёта регистра, фильтры, давность данных и сохранение баланса при ошибке. Синтаксис трёх изменённых JS-файлов и Python-модулей проверен.
- Реальная VM, Google Таблица, ключи и средства при разработке не использовались. Docker-обновление и фактические сетевые запросы нужно подтвердить после установки на VM.

## Binance integration — 2026-10-21

- 172 offline Python tests pass, including the existing MEXC suite.
- New tests cover Binance HMAC header/signature, market buy/sell, +0.10 USDT, actual withdrawal increment semantics, BSC-only validation, Travel Rule blocking, separate exchange state, non-replay after ambiguous purchase/withdrawal responses, and exchange-address migration.
- Node wallet UI and Binance UI checks pass: disabled sale threshold, UTC history formatting, balance rendering, tab placement, collapsed audit markup. JS syntax and unique HTML IDs checked.
- Full browser visual check unavailable in this environment: Chromium was absent and the download returned an invalid archive. This is not counted as a passing browser test.
- No real API keys, trades, withdrawals, or Google Sheet changes were used. Account-specific Binance connectivity and permissions must be checked on the VM with `python3 configure_binance.py --check`.
