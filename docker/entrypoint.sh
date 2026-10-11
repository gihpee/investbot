#!/bin/sh
# Точка входа контейнера: URL базы собирается из секрета с паролем (в .env пароля нет), затем команда investbot.
set -eu
if [ -n "${PG_PASSWORD_FILE:-}" ] && [ -f "$PG_PASSWORD_FILE" ] && [ -z "${INVESTBOT_DATABASE_URL:-}" ]; then
  INVESTBOT_DATABASE_URL="postgresql+asyncpg://investbot:$(cat "$PG_PASSWORD_FILE")@postgres/investbot"
  export INVESTBOT_DATABASE_URL
fi
exec /app/.venv/bin/investbot "$@"
