#!/usr/bin/env bash
# Read-only verification. Shares the existing preview runner's host lock.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
exec 9>/tmp/bnb-archive-scan.lock
echo 'Ожидаем завершения другого сканирования…'
flock 9
echo 'Проверяем кандидатов; переноса в архив не будет.'
docker compose exec -T web python -u archive_preview.py verify
