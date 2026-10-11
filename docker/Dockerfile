# syntax=docker/dockerfile:1.7
# Образ бота (docs/DEPLOY.md).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

# SDK Т-Инвестиций: из vendor/wheels, если wheel положен заранее, иначе из GitLab-индекса Т-Банка
# (нужен сертификат Минцифры — передаётся секретом сборки ca_bundle, в образ не попадает)
COPY vendor/wheels/ /app/vendor/wheels/
RUN --mount=type=secret,id=ca_bundle,required=false \
    if ls /app/vendor/wheels/*.whl >/dev/null 2>&1; then \
        uv pip install --python /app/.venv/bin/python /app/vendor/wheels/*.whl; \
    elif [ -s /run/secrets/ca_bundle ]; then \
        SSL_CERT_FILE=/run/secrets/ca_bundle uv pip install --python /app/.venv/bin/python \
            --index-url https://opensource.tbank.ru/api/v4/projects/238/packages/pypi/simple \
            --extra-index-url https://pypi.org/simple --index-strategy unsafe-best-match "t-tech-investments==1.51.0"; \
    else echo "SDK Т-Инвестиций не установлен: нет vendor/wheels и ca_bundle" >&2; fi

COPY src/ ./src/
COPY config/ ./config/
COPY migrations/ ./migrations/
COPY prompts/ ./prompts/
COPY alembic.ini ./
COPY docker/entrypoint.sh /app/entrypoint.sh
RUN uv sync --frozen --no-dev

RUN useradd --create-home --uid 10001 investbot && mkdir -p /app/data/local && chown -R investbot /app/data
USER investbot
ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["bot"]
