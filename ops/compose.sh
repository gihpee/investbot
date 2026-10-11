#!/usr/bin/env bash
# Обёртка над docker compose: файл docker/compose.yml и переменные из .env в корне проекта.
set -euo pipefail
cd "$(dirname "$0")/.."
exec docker compose --env-file .env -f docker/compose.yml "$@"
