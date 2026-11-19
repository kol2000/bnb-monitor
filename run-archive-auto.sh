#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
[[ $EUID -eq 0 ]] || { echo 'Запустите через sudo.'; exit 1; }
exec 9>/tmp/bnb-archive-scan.lock
flock -n 9 || exit 0
# Bound each batch. Normal monitoring remains running during the RPC scan.
rc=0
docker compose run --rm --no-deps -T web python archive_auto.py prepare || rc=$?
[[ $rc -eq 10 ]] || exit "$rc"
# Pending financial operations defer the move; the timer will retry later.
docker compose exec -T web python -c 'from app import db; from archive_approved import assert_idle; exec("with db() as c: assert_idle(c)")' || exit 0
restart_services() { docker compose start web worker; }
trap restart_services EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
python3 manage.py backup
docker compose stop web worker
docker compose run --rm --no-deps -T web python archive_auto.py apply
