"""Сборка LLM-роутера из config/llm.yaml и окружения (ключи — только из окружения, §9.7)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import httpx
import structlog
from sqlalchemy.ext.asyncio import AsyncEngine

from investbot.adapters.llm.client import OpenAICompatClient
from investbot.adapters.llm.config import (
    ChainEntry,
    LLMConfig,
    ModelSpec,
    ProviderBudget,
    ProviderSpec,
    load_llm_config,
)
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
            proxy=env.get(PROXY_ENV, "").strip() or None,
        )
    return clients


PROXY_ENV = "INVESTBOT_LLM_PROXY"  # http(s):// или socks5:// — если провайдер недоступен напрямую с сервера
CUSTOM = "custom"
TAGGER_ROLES = ("tagger", "tagger_universe")


def _flag(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name, "").strip().lower()
    return default if not raw else raw in ("1", "true", "yes", "да", "on")


def _dec(env: Mapping[str, str], name: str, default: str) -> Decimal:
    raw = env.get(name, "").strip().replace(",", ".")
    return Decimal(raw or default)


def custom_config(env: Mapping[str, str], base: LLMConfig) -> LLMConfig | None:
    """Свой OpenAI-совместимый провайдер из окружения — заменяет провайдеров из config/llm.yaml.

    INVESTBOT_LLM_BASE_URL, INVESTBOT_LLM_API_KEY, INVESTBOT_LLM_MODEL — обязательны;
    INVESTBOT_LLM_MODEL_FAST — модель для разметки новостей (по умолчанию та же);
    INVESTBOT_LLM_DATA_IN_RU — провайдер хранит и обрабатывает данные в РФ (по умолчанию нет);
    INVESTBOT_LLM_ALLOW_PORTFOLIO_ABROAD — явное согласие отправлять доли портфеля провайдеру вне РФ (по умолчанию нет);
    INVESTBOT_LLM_PRICE_IN / _OUT — ₽ за 1M токенов (для бюджета), INVESTBOT_LLM_MONTHLY_RUB — месячный лимит;
    INVESTBOT_LLM_QA_QUOTA — вопросов в месяц;
    INVESTBOT_LLM_SUPPORTS_TOOLS / _JSON_MODE / _REASONING — возможности модели.
    """
    url, model = env.get("INVESTBOT_LLM_BASE_URL", "").strip(), env.get("INVESTBOT_LLM_MODEL", "").strip()
    if not (url and model and env.get("INVESTBOT_LLM_API_KEY", "").strip()):
        return None
    fast = env.get("INVESTBOT_LLM_MODEL_FAST", "").strip() or model
    in_ru = _flag(env, "INVESTBOT_LLM_DATA_IN_RU", False)
    abroad_ok = _flag(env, "INVESTBOT_LLM_ALLOW_PORTFOLIO_ABROAD", False)
    if not in_ru and abroad_ok:
        log.warning("llm.portfolio_abroad_allowed")  # осознанное решение пользователя: доли портфеля уходят за рубеж
    spec = {
        "data_in_ru": in_ru or abroad_ok,
        "reasoning": _flag(env, "INVESTBOT_LLM_REASONING", False),
        "price_in_rub_per_m": _dec(env, "INVESTBOT_LLM_PRICE_IN", "300"),
        "price_out_rub_per_m": _dec(env, "INVESTBOT_LLM_PRICE_OUT", "1200"),
    }
    models = {model: ModelSpec(**spec)}
    if fast != model:
        models[fast] = ModelSpec(**spec)
    provider = ProviderSpec(
        base_url=url, api_key_env="INVESTBOT_LLM_API_KEY", timeout_s=float(_dec(env, "INVESTBOT_LLM_TIMEOUT", "120")),
        supports_tools=_flag(env, "INVESTBOT_LLM_SUPPORTS_TOOLS", True),
        supports_response_format_json=_flag(env, "INVESTBOT_LLM_JSON_MODE", True),
        models=models,
    )
    monthly = _dec(env, "INVESTBOT_LLM_MONTHLY_RUB", "3000")
    roles = {role: (ChainEntry(provider=CUSTOM, model=fast if role in TAGGER_ROLES else model),)
             for role in base.roles}
    return base.model_copy(update={
        "primary_provider": CUSTOM,
        "providers": {CUSTOM: provider},
        "budgets": {CUSTOM: ProviderBudget(monthly=monthly, per_run=monthly / 10,
                                           qa_monthly_quota=int(_dec(env, "INVESTBOT_LLM_QA_QUOTA", "100")))},
        "roles": roles,
    })


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
    cfg = custom_config(settings_env, cfg) or cfg
    clients = build_clients(cfg, settings_env, transport=transport, sleep=sleep, verify=verify)
    if not clients:
        log.warning("llm.disabled", reason="no_api_keys")
        return None
    if cfg.primary_provider not in clients:
        log.warning("llm.primary_absent", primary=cfg.primary_provider, available=sorted(clients))
    return LLMRouter(cfg, clients, CallLedger(engine), clock)
