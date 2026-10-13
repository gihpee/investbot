# Образ бота (docs/DEPLOY.md).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
# Зависимости + SDK Т-Инвестиций (extra tinvest): wheel 1.51.0 с GitHub Т-Банка по фиксированному коммиту,
# uv проверяет его sha256 из uv.lock
RUN uv sync --frozen --no-dev --extra tinvest --no-install-project

COPY src/ ./src/
COPY config/ ./config/
COPY migrations/ ./migrations/
COPY prompts/ ./prompts/
COPY alembic.ini ./
COPY docker/entrypoint.sh /app/entrypoint.sh
RUN uv sync --frozen --no-dev --extra tinvest

RUN useradd --create-home --uid 10001 investbot && mkdir -p /app/data/local && chown -R investbot /app/data
USER investbot
ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["bot"]
