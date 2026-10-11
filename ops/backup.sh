#!/usr/bin/env bash
# Ежедневный бэкап БД бота: pg_dump из контейнера → data/backups, хранение 14 дней.
# Cron (на сервере, от пользователя с доступом к docker):  15 4 * * * /opt/investbot/ops/backup.sh
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p data/backups
stamp=$(date +%Y%m%d-%H%M)
ops/compose.sh exec -T postgres pg_dump -U investbot -d investbot -Fc > "data/backups/investbot-${stamp}.dump"
find data/backups -name 'investbot-*.dump' -mtime +14 -delete
echo "ok: data/backups/investbot-${stamp}.dump"
