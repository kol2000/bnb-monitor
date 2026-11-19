#!/usr/bin/env bash
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo 'Запустите через sudo.'; exit 1; }
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
project=$(pwd -P)
# Unit paths are deliberately restricted to avoid shell/unit escaping ambiguity.
[[ $project =~ ^/[a-zA-Z0-9_./-]+$ ]] || { echo 'Путь проекта содержит неподдерживаемые символы.'; exit 1; }
cat > /etc/systemd/system/bnb-archive-auto.service <<EOF
[Unit]
Description=BNB monitor automatic wallet archive
Requires=docker.service
After=docker.service network-online.target
Wants=network-online.target
[Service]
Type=oneshot
WorkingDirectory=$project
ExecStart=/bin/bash $project/run-archive-auto.sh
TimeoutStartSec=infinity
TimeoutStopSec=120
EOF
cat > /etc/systemd/system/bnb-archive-auto.timer <<'EOF'
[Unit]
Description=Resume BNB wallet archive checks automatically
[Timer]
OnBootSec=5min
OnUnitInactiveSec=5min
Unit=bnb-archive-auto.service
[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload
systemctl enable --now bnb-archive-auto.timer
systemctl start --no-block bnb-archive-auto.service
echo 'Автоархив включён. Журнал: journalctl -u bnb-archive-auto.service -n 30 --no-pager'
