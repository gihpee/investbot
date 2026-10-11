# Образ бота. SDK Т-Инвестиций ставится из vendor/wheels (wheel скачивается заранее, см. README).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY vendor/wheels/ /app/vendor/wheels/
RUN if ls /app/vendor/wheels/*.whl >/dev/null 2>&1; then \
        uv pip install --python /app/.venv/bin/python /app/vendor/wheels/*.whl; fi

COPY src/ ./src/
COPY config/ ./config/
COPY migrations/ ./migrations/
COPY prompts/ ./prompts/
COPY alembic.ini ./
RUN uv sync --frozen --no-dev

RUN useradd --create-home --uid 10001 investbot && mkdir -p /app/data/local && chown -R investbot /app/data
USER investbot
ENTRYPOINT ["/app/.venv/bin/investbot"]
CMD ["bot"]
