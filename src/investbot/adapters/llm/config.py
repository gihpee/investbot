"""Конфигурация LLM-роутера: config/llm.yaml (ARCHITECTURE §9.6 G2/G8/G10/G11, §9.7, ADR-09).

В конфиге только имена переменных окружения с ключами (`api_key_env`), сами ключи — в окружении.
id моделей — точные, без плавающих алиасов (G10). Цены — ₽ за 1M токенов с НДС; сверять с `GET /v1/models`
и страницей тарифа ежемесячно. Цепочки ролей — строки `provider:model`, каждая ссылается на объявленные
провайдер и модель (проверяется при загрузке).
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import Field, field_validator, model_validator

from investbot.core.models import Strict

MILLION = Decimal(1_000_000)
ENV_NAME = r"^[A-Z][A-Z0-9_]*$"
_ENV_REF = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}")
_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
# Длины колонок llm_call: role String(32) (с суффиксом шага «#N»), provider String(32), model String(128)
MAX_ROLE_LEN = 24
MAX_PROVIDER_LEN = 32
MAX_MODEL_LEN = 128


class ModelSpec(Strict):
    """Модель провайдера. Резидентность — на уровне модели, а не провайдера (G8, отчёт 10 §1.3)."""

    data_in_ru: bool  # без значения по умолчанию: резидентность задаётся явно (fail-closed)
    reasoning: bool = False  # скрытые токены рассуждений: выход × reasoning_output_factor в оценке и лимите
    price_in_rub_per_m: Decimal = Field(ge=0)
    price_out_rub_per_m: Decimal = Field(ge=0)
    # None — как у провайдера; у Cloud.ru function calling и structured output различаются по моделям
    supports_tools: bool | None = None
    supports_response_format_json: bool | None = None

    def cost(self, input_tokens: int, output_tokens: int) -> Decimal:
        """Стоимость вызова по фактическим токенам (выход reasoning-моделей уже включает рассуждения)."""
        return (Decimal(input_tokens) * self.price_in_rub_per_m
                + Decimal(output_tokens) * self.price_out_rub_per_m) / MILLION


class ProviderSpec(Strict):
    """OpenAI-совместимый эндпоинт (Chat Completions)."""

    base_url: str = Field(pattern=r"^https?://\S+$")
    api_key_env: str = Field(pattern=ENV_NAME)  # имя переменной окружения, НЕ ключ
    # Bearer — как у OpenAI-клиента (Cloud.ru, OpenAI-совместимый API Yandex); Api-Key — нативный API Yandex
    auth_scheme: Literal["Bearer", "Api-Key"] = "Bearer"
    header_envs: dict[str, str] = Field(default_factory=dict)  # заголовок → имя переменной окружения со значением
    # Имя модели в запросе: {model} — id из конфига, ${ENV} — из окружения (Yandex: gpt://${YC_FOLDER_ID}/{model})
    model_template: str = "{model}"
    rps: float | None = Field(default=None, gt=0)
    max_concurrency: int | None = Field(default=None, ge=1)
    timeout_s: float = Field(default=120, gt=0)
    supports_response_format_json: bool = True  # response_format={"type": "json_object"}
    supports_tools: bool = True
    models: dict[str, ModelSpec] = Field(min_length=1)

    @field_validator("header_envs")
    @classmethod
    def _headers(cls, v: dict[str, str]) -> dict[str, str]:
        for header, env in v.items():
            if not re.fullmatch(r"[A-Za-z0-9-]+", header) or header.lower() == "authorization":
                raise ValueError(f"header_envs: недопустимый заголовок {header!r}")
            if not re.fullmatch(ENV_NAME, env):
                raise ValueError(f"header_envs: {env!r} — не имя переменной окружения")
        return v

    @field_validator("model_template")
    @classmethod
    def _template(cls, v: str) -> str:
        if v.count("{model}") != 1:
            raise ValueError("model_template: ровно одно вхождение {model}")
        rest = _ENV_REF.sub("", v.replace("{model}", ""))
        if "{" in rest or "}" in rest or "$" in rest:
            raise ValueError("model_template: допустимы только {model} и ${ENV_NAME}")
        return v

    @field_validator("models")
    @classmethod
    def _model_ids(cls, v: dict[str, ModelSpec]) -> dict[str, ModelSpec]:
        for model_id in v:
            if not model_id or len(model_id) > MAX_MODEL_LEN or ":" in model_id or model_id != model_id.strip():
                raise ValueError(f"models: недопустимый id модели {model_id!r}")
        return v

    def required_envs(self) -> list[str]:
        """Все переменные окружения, без которых провайдер не подключается."""
        names = [self.api_key_env, *self.header_envs.values(), *_ENV_REF.findall(self.model_template)]
        return list(dict.fromkeys(names))

    def resolve_template(self, env: Mapping[str, str]) -> str:
        return _ENV_REF.sub(lambda m: env[m.group(1)].strip(), self.model_template)

    def tools_supported(self, model: str) -> bool:
        flag = self.models[model].supports_tools
        return self.supports_tools if flag is None else flag

    def json_mode_supported(self, model: str) -> bool:
        flag = self.models[model].supports_response_format_json
        return self.supports_response_format_json if flag is None else flag


class ProviderBudget(Strict):
    """Бюджеты провайдера в ₽ (G11). Тест конфигурации: оценка × 1,2 ≤ лимита."""

    monthly: Decimal = Field(gt=0)  # месячный лимит; роутер не делает вызов, который его превысит
    per_run: Decimal = Field(gt=0)  # недельный прогон (Writer + RiskCritic + IdeaExplainer); контролирует вызывающий
    qa_monthly_quota: int = Field(ge=0)  # вопросов Q&A в месяц (роли из qa_roles)


class BreakerSpec(Strict):
    """Circuit breaker провайдера: `errors` ошибок за `window_s` секунд — пауза `cooldown_s` (G11)."""

    errors: int = Field(default=3, ge=1)
    window_s: int = Field(default=300, ge=1)
    cooldown_s: int = Field(default=600, ge=1)


class ChainEntry(Strict):
    provider: str
    model: str

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.model}"


def parse_chain_entry(raw: object) -> ChainEntry:
    """`"cloudru:ai-sage/GigaChat3-10B-A1.8B"` → ChainEntry. Двоеточие отделяет провайдера (первое вхождение)."""
    if isinstance(raw, ChainEntry):
        return raw
    if not isinstance(raw, str) or ":" not in raw:
        raise ValueError(f"элемент цепочки {raw!r}: ожидается строка provider:model")
    provider, model = raw.split(":", 1)
    if not provider.strip() or not model.strip():
        raise ValueError(f"элемент цепочки {raw!r}: пустой провайдер или модель")
    return ChainEntry(provider=provider.strip(), model=model.strip())


class LLMConfig(Strict):
    version: int = Field(ge=1)
    primary_provider: str  # cloudru или yandex — по итогам спайка Ф0
    budgets: dict[str, ProviderBudget]
    providers: dict[str, ProviderSpec] = Field(min_length=1)
    roles: dict[str, tuple[ChainEntry, ...]] = Field(min_length=1)
    qa_roles: tuple[str, ...] = ("qa", "allocation_advisor")  # общая месячная квота вопросов
    degrade_at: Decimal = Field(default=Decimal("0.9"), gt=0, le=1)  # доля месячного лимита → дешёвая модель
    reasoning_output_factor: int = Field(default=3, ge=1)  # скрытые рассуждения: выход × 3 (§9.7)
    chars_per_token: int = Field(default=4, ge=1)  # грубая оценка входа: символы / 4
    breaker: BreakerSpec = Field(default_factory=BreakerSpec)
    default_temperature: float = Field(default=0.2, ge=0, le=2)
    temperatures: dict[str, float] = Field(default_factory=dict)

    @field_validator("roles", mode="before")
    @classmethod
    def _parse_roles(cls, v: Any) -> Any:
        if not isinstance(v, Mapping):
            return v
        out: dict[Any, Any] = {}
        for role, chain in v.items():
            if not isinstance(chain, list | tuple) or not chain:
                raise ValueError(f"roles.{role}: непустой список provider:model")
            out[role] = tuple(parse_chain_entry(e) for e in chain)
        return out

    @model_validator(mode="after")
    def _check(self) -> LLMConfig:
        for name in self.providers:
            if not _NAME.fullmatch(name) or len(name) > MAX_PROVIDER_LEN:
                raise ValueError(f"providers: недопустимое имя провайдера {name!r}")
        if self.primary_provider not in self.providers:
            raise ValueError(f"primary_provider {self.primary_provider!r} не объявлен в providers")
        if missing := sorted(set(self.providers) - set(self.budgets)):
            raise ValueError(f"budgets: нет бюджета для провайдеров {missing}")
        if unknown := sorted(set(self.budgets) - set(self.providers)):
            raise ValueError(f"budgets: неизвестные провайдеры {unknown}")
        for role, chain in self.roles.items():
            if not _NAME.fullmatch(role) or len(role) > MAX_ROLE_LEN:
                raise ValueError(f"roles: недопустимое имя роли {role!r} (a-z0-9_, ≤ {MAX_ROLE_LEN})")
            seen: set[str] = set()
            for entry in chain:
                provider = self.providers.get(entry.provider)
                if provider is None:
                    raise ValueError(f"roles.{role}: провайдер {entry.provider!r} не объявлен ({entry.key})")
                if entry.model not in provider.models:
                    raise ValueError(f"roles.{role}: модель {entry.model!r} не объявлена у {entry.provider!r}")
                if entry.key in seen:
                    raise ValueError(f"roles.{role}: {entry.key} повторяется")
                seen.add(entry.key)
        if unknown := sorted(set(self.qa_roles) - set(self.roles)):
            raise ValueError(f"qa_roles: роли {unknown} не объявлены в roles")
        if unknown := sorted(set(self.temperatures) - set(self.roles)):
            raise ValueError(f"temperatures: роли {unknown} не объявлены в roles")
        if any(not 0 <= t <= 2 for t in self.temperatures.values()):
            raise ValueError("temperatures: значения в [0, 2]")
        return self

    def model(self, entry: ChainEntry) -> ModelSpec:
        return self.providers[entry.provider].models[entry.model]

    def temperature(self, role: str) -> float:
        return self.temperatures.get(role, self.default_temperature)

    def output_cap(self, entry: ChainEntry, max_tokens: int) -> int:
        """Лимит выхода в запросе: у reasoning-моделей рассуждения входят в max_tokens, поэтому × factor."""
        return max_tokens * self.reasoning_output_factor if self.model(entry).reasoning else max_tokens

    def estimate_tokens(self, chars: int) -> int:
        return math.ceil(chars / self.chars_per_token)

    def estimate_cost(self, entry: ChainEntry, input_chars: int, max_tokens: int) -> Decimal:
        """Верхняя оценка вызова для бюджета: вход по длине текста, выход — по лимиту (× factor у reasoning)."""
        return self.model(entry).cost(self.estimate_tokens(input_chars), self.output_cap(entry, max_tokens))


def load_llm_config(path: Path | str) -> LLMConfig:
    raw: Any = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return LLMConfig.model_validate(raw)
