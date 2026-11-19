#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
if [[ $EUID -ne 0 ]]; then echo 'Запустите через sudo.'; exit 1; fi
exec 9>/tmp/bnb-archive-scan.lock
flock 9
# Fail before downtime if financial work is pending.
docker compose exec -T web python -c 'from app import db; from archive_approved import assert_idle; c=db(); conn=c.__enter__(); assert_idle(conn); c.__exit__(None,None,None)'
python3 manage.py backup
restart_services() { docker compose start web worker; }
trap restart_services EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
docker compose stop web worker
echo 'Web и worker временно остановлены. Не редактируйте Google Таблицу до завершения.'
docker compose run --rm --no-deps -T web python archive_approved.py
echo 'Готово. Перенос проверен, службы запускаются.'
