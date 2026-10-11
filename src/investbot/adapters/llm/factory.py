"""Сборка LLM-роутера из config/llm.yaml и окружения (ключи — только из окружения, §9.7)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime
from pathlib import Path

import httpx
import structlog
from sqlalchemy.ext.asyncio import AsyncEngine

from investbot.adapters.llm.client import OpenAICompatClient
from investbot.adapters.llm.config import LLMConfig, load_llm_config
from investbot.adapters.llm.ledger import CallLedger
from investbot.adapters.llm.router import LLMRouter, utcnow

log = structlog.get_logger(__name__)


def build_clients(
    cfg: LLMConfig,
    env: Mapping[str, str],
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
    verify: str | Path | bool = True,
) -> dict[str, OpenAICompatClient]:
    """Клиенты провайдеров, для которых в окружении заданы ключ и все нужные переменные; остальные отсутствуют."""
    clients: dict[str, OpenAICompatClient] = {}
    for name, spec in cfg.providers.items():
        missing = [var for var in spec.required_envs() if not env.get(var, "").strip()]
        if missing:
            # имена переменных, не значения
            log.info("llm.provider_absent", provider=name, missing=missing)
            continue
        clients[name] = OpenAICompatClient(
            spec.base_url,
            env[spec.api_key_env].strip(),
            headers={header: env[var].strip() for header, var in spec.header_envs.items()},
            timeout=spec.timeout_s,
            transport=transport,
            sleep=sleep,
            model_template=spec.resolve_template(env),
            auth_scheme=spec.auth_scheme,
            rps=spec.rps,
            max_concurrency=spec.max_concurrency,
            verify=verify,
        )
    return clients


def build_router(
    cfg_path: Path | str,
    settings_env: Mapping[str, str],
    engine: AsyncEngine,
    *,
    clock: Callable[[], datetime] = utcnow,
    transport: httpx.AsyncBaseTransport | None = None,
    sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
    verify: str | Path | bool = True,
) -> LLMRouter | None:
    """Роутер или None, если ни у одного провайдера нет ключа (LLM-слой выключен, роли уходят в шаблоны, L2).

    `settings_env` — обычно `os.environ` (в Docker .env попадает туда через env_file); `verify` — путь к bundle
    корней с Russian Trusted Root CA (`Settings.ca_bundle`), если он нужен.
    """
    cfg = load_llm_config(cfg_path)
    clients = build_clients(cfg, settings_env, transport=transport, sleep=sleep, verify=verify)
    if not clients:
        log.warning("llm.disabled", reason="no_api_keys")
        return None
    if cfg.primary_provider not in clients:
        log.warning("llm.primary_absent", primary=cfg.primary_provider, available=sorted(clients))
    return LLMRouter(cfg, clients, CallLedger(engine), clock)
