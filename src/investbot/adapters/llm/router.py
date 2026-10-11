"""LLM-роутер по цепочкам ролей (ARCHITECTURE §9.6, §9.7, ADR-09). Реализует `llm_roles.base.JsonLLM`.

Для каждой роли — цепочка `provider:model` из config/llm.yaml. Вызов идёт по цепочке, пропуская:
- провайдеров без ключа в окружении (их клиента нет в `clients`);
- модели с `data_in_ru = false`, если в контексте портфель (G8);
- провайдеров с открытым circuit breaker: 3 ошибки за 5 минут — пауза 10 минут; после паузы один
  пробный вызов, его ошибка снова открывает breaker сразу (G11);
- провайдеров, у которых вызов превысил бы месячный лимит: потрачено в этом месяце (по `llm_call`)
  + вызовы в полёте + оценка этого вызова (вход — символы / 4, выход — max_tokens × 3 у reasoning) > monthly;
- для ролей Q&A (`qa_roles`) — провайдеров, чья месячная квота вопросов исчерпана (L2 для Q&A).

Деградация: когда первый доступный провайдер роли потратил ≥ 90% месячного лимита, `role_degraded(role)`
истинно, и роутер пробует только самую дешёвую (по оценке этого вызова) допустимую модель цепочки — без
дорогих резервов.

JSON (G2): в системное сообщение добавляется JSON-схема выхода, `response_format={"type": "json_object"}` —
где поддерживается. JSON вырезается из ответа (code fences, текст вокруг, `<think>`), проверяется pydantic.
Невалидно — один ретрай на той же модели с текстом ошибки, затем следующая модель цепочки, иначе None.

Инструменты (Q&A): `run_tools` передаёт `tools` без `response_format` (Yandex их не совмещает), выполняет
вызовы через `handler` по одному (GigaChat делает один вызов функции за ответ) и останавливается, когда модель
вызывает `final_tool` с аргументами, валидными для `output`.

Каждый HTTP-запрос пишется в `llm_call` (токены, ₽ по ценам модели, успех/ошибка) — см. ledger.py.
"""

from __future__ import annotations

import json
import re
from collections import deque
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import structlog
from pydantic import BaseModel, ValidationError

from investbot.adapters.llm.client import ChatResult, LLMError, OpenAICompatClient, ToolCall
from investbot.adapters.llm.config import BreakerSpec, ChainEntry, LLMConfig
from investbot.adapters.llm.ledger import CallLedger, CallRecord, month_start

log = structlog.get_logger(__name__)

JSON_OBJECT: Mapping[str, str] = {"type": "json_object"}
MAX_TOOL_STEPS = 20
MAX_PROMPT_ID_LEN = 64  # llm_call.prompt_id String(64)
MAX_JSON_STARTS = 64  # сколько открывающих скобок пробовать в одном ответе
_TOOL_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_FENCE = re.compile(r"```[ \t]*(?:json|JSON)?[ \t]*\n?(.*?)```", re.DOTALL)
_THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)

JSON_INSTRUCTION = (
    "Ответь только одним JSON-объектом, без пояснений и без markdown. "
    "Объект должен соответствовать JSON-схеме (лишние поля запрещены):\n{schema}"
)
RETRY_INSTRUCTION = "Ответ не прошёл проверку: {error}. Верни исправленный JSON-объект целиком, без пояснений."
FINAL_TOOL_INSTRUCTION = (
    "Пользуйся инструментами, чтобы получить факты. Финальный ответ оформи одним вызовом инструмента "
    "{name}; текст вне этого вызова ответом не считается."
)
NO_TOOL_NUDGE = "Ответ не принят: вызови инструмент {name} с итоговым ответом."
TOOL_SCHEMA_ERROR = "Ошибка проверки аргументов: {error}. Вызови {name} ещё раз с исправленными аргументами."

ToolHandler = Callable[[str, dict[str, Any]], Awaitable[str]]


class OutputError(ValueError):
    """В ответе нет JSON-объекта или он не проходит схему. Текст безопасно показывать модели."""


def utcnow() -> datetime:
    return datetime.now(UTC)


# --- Разбор ответа -------------------------------------------------------------------------------------------


def iter_json_objects(text: str | None) -> Iterator[dict[str, Any]]:
    """JSON-объекты верхнего уровня из ответа: сначала внутри ``` ```, затем во всём тексте (проза вокруг)."""
    if not text:
        return
    body = _THINK.sub("", text)
    decoder = json.JSONDecoder()
    for cand in [*(m.group(1) for m in _FENCE.finditer(body)), body]:
        pos, starts = 0, 0
        while (i := cand.find("{", pos)) != -1 and starts < MAX_JSON_STARTS:
            starts += 1
            try:
                obj, end = decoder.raw_decode(cand, i)
            except json.JSONDecodeError:
                pos = i + 1
                continue
            if isinstance(obj, dict):
                yield obj
            pos = end  # вложенные объекты уже найденного не перебираем


def extract_json(text: str | None) -> dict[str, Any]:
    for obj in iter_json_objects(text):
        return obj
    raise OutputError("в ответе нет JSON-объекта")


def describe_validation(exc: ValidationError, limit: int = 5) -> str:
    """Ошибки pydantic без входных значений (в журнал не попадают фрагменты ответа)."""
    errors = exc.errors(include_url=False, include_input=False)
    parts = [f"{'.'.join(str(p) for p in e['loc']) or '<объект>'}: {e['msg']}" for e in errors[:limit]]
    more = f" (и ещё {len(errors) - limit})" if len(errors) > limit else ""
    return "; ".join(parts) + more


def parse_output[T: BaseModel](text: str | None, output: type[T]) -> T:
    """Первый JSON-объект ответа, проходящий схему `output`; иначе OutputError с ошибкой первого объекта."""
    first: OutputError | None = None
    for obj in iter_json_objects(text):
        try:
            return output.model_validate(obj)
        except ValidationError as exc:
            first = first or OutputError(describe_validation(exc))
    raise first or OutputError("в ответе нет JSON-объекта")


def json_instruction(output: type[BaseModel]) -> str:
    schema = json.dumps(output.model_json_schema(), ensure_ascii=False, separators=(",", ":"))
    return JSON_INSTRUCTION.format(schema=schema)


def with_system(messages: Sequence[Mapping[str, Any]], addendum: str) -> list[dict[str, Any]]:
    """Копия сообщений с дополнением к первому системному (или новым системным в начале)."""
    out = [dict(m) for m in messages]
    if out and out[0].get("role") == "system":
        out[0]["content"] = f"{out[0].get('content') or ''}\n\n{addendum}"
    else:
        out.insert(0, {"role": "system", "content": addendum})
    return out


def _chars(items: Sequence[Mapping[str, Any]] | None) -> int:
    return sum(len(json.dumps(m, ensure_ascii=False)) for m in items or ())


# --- Инструменты ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolSpec:
    """Read-only инструмент для Q&A (§9.3): имя, описание, JSON-схема аргументов."""

    name: str
    description: str
    parameters: Mapping[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {}})

    def __post_init__(self) -> None:
        if not _TOOL_NAME.fullmatch(self.name):
            raise ValueError(f"имя инструмента {self.name!r}: латиница, цифры, _ и -, до 64 символов")

    @classmethod
    def from_model(cls, name: str, description: str, model: type[BaseModel]) -> ToolSpec:
        return cls(name=name, description=description, parameters=model.model_json_schema())

    def to_openai(self) -> dict[str, Any]:
        return {"type": "function",
                "function": {"name": self.name, "description": self.description, "parameters": dict(self.parameters)}}


def _assistant_message(res: ChatResult) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": res.content or None,
        "tool_calls": [{"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments}}
                       for c in res.tool_calls],
    }


def _tool_message(call: ToolCall, content: str) -> dict[str, Any]:
    return {"role": "tool", "tool_call_id": call.id, "content": content}


# --- Circuit breaker -----------------------------------------------------------------------------------------


class CircuitBreaker:
    """`errors` ошибок за `window_s` → открыт на `cooldown_s`; затем пробный вызов (half-open):
    успех закрывает, ошибка сразу открывает снова."""

    def __init__(self, spec: BreakerSpec) -> None:
        self._spec = spec
        self._errors: deque[datetime] = deque()
        self._open_until: datetime | None = None
        self._half_open = False

    def is_open(self, now: datetime) -> bool:
        if self._open_until is None:
            return False
        if now < self._open_until:
            return True
        self._open_until = None
        self._half_open = True
        return False

    def record_failure(self, now: datetime) -> None:
        if self._open_until is not None and now < self._open_until:
            return  # запрос в полёте завершился после открытия
        if self._half_open:
            self._trip(now)
            return
        self._errors.append(now)
        window = timedelta(seconds=self._spec.window_s)
        while self._errors and now - self._errors[0] > window:
            self._errors.popleft()
        if len(self._errors) >= self._spec.errors:
            self._trip(now)

    def record_success(self) -> None:
        self._half_open = False

    def _trip(self, now: datetime) -> None:
        self._open_until = now + timedelta(seconds=self._spec.cooldown_s)
        self._errors.clear()
        self._half_open = False


# --- Роутер --------------------------------------------------------------------------------------------------


@dataclass
class _Request:
    """Один логический вызов роли: нумерует HTTP-запросы для колонки role (`qa`, `qa#2`, …)."""

    role: str
    prompt_id: str
    calls: int = 0

    def next_label(self) -> str:
        self.calls += 1
        return self.role if self.calls == 1 else f"{self.role}#{self.calls}"


@dataclass(frozen=True)
class _Attempt:
    result: ChatResult
    label: str
    input_tokens: int
    output_tokens: int
    cost: Decimal


class LLMRouter:
    def __init__(
        self,
        cfg: LLMConfig,
        clients: Mapping[str, OpenAICompatClient],
        ledger: CallLedger,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        if unknown := sorted(set(clients) - set(cfg.providers)):
            raise ValueError(f"клиенты для необъявленных провайдеров: {unknown}")
        self.cfg = cfg
        self._clients = dict(clients)
        self._ledger = ledger
        self._clock = clock
        self._breakers = {name: CircuitBreaker(cfg.breaker) for name in self._clients}
        self._reserved = {name: Decimal(0) for name in self._clients}  # оценки вызовов в полёте

    @property
    def providers(self) -> tuple[str, ...]:
        """Провайдеры с ключом в окружении."""
        return tuple(self._clients)

    def breaker_open(self, provider: str) -> bool:
        breaker = self._breakers.get(provider)
        return breaker is not None and breaker.is_open(self._clock())

    async def aclose(self) -> None:
        for client in self._clients.values():
            await client.aclose()

    # --- бюджеты ---

    async def monthly_spent(self, provider: str) -> Decimal:
        """₽ по провайдеру с начала календарного месяца (Europe/Moscow)."""
        return await self._ledger.monthly_spent(self._clock(), provider=provider)

    async def qa_calls_this_month(self) -> int:
        """Вопросы Q&A (роли из qa_roles) за месяц по всем провайдерам: первые запросы логических вызовов."""
        return await self._ledger.count_since(month_start(self._clock()), roles=self.cfg.qa_roles)

    def role_provider(self, role: str) -> str | None:
        """Первый провайдер цепочки роли, для которого есть ключ."""
        return next((e.provider for e in self._chain(role) if e.provider in self._clients), None)

    async def role_degraded(self, role: str) -> bool:
        """≥ degrade_at (90%) месячного лимита у первого доступного провайдера роли → только дешёвая модель."""
        provider = self.role_provider(role)
        if provider is None:
            return False
        limit = self.cfg.budgets[provider].monthly * self.cfg.degrade_at
        return await self.monthly_spent(provider) >= limit

    # --- JsonLLM ---

    async def complete_json[T: BaseModel](
        self,
        role: str,
        messages: Sequence[Mapping[str, Any]],
        output: type[T],
        *,
        has_portfolio_ctx: bool,
        prompt_id: str,
        max_tokens: int = 2000,
    ) -> T | None:
        req = self._request(role, prompt_id)
        base = with_system(messages, json_instruction(output))
        plan = await self._plan(req, has_portfolio_ctx=has_portfolio_ctx, need_tools=False,
                                input_chars=_chars(base), max_tokens=max_tokens)
        for entry in plan:
            json_mode = self.cfg.providers[entry.provider].json_mode_supported(entry.model)
            convo = base
            for _ in range(2):  # G2: один ретрай на той же модели
                att = await self._call(req, entry, convo, max_tokens=max_tokens,
                                       response_format=JSON_OBJECT if json_mode else None)
                if att is None:
                    break
                try:
                    parsed = parse_output(att.result.content, output)
                except OutputError as exc:
                    await self._record(req, entry, att, ok=False, error=f"schema: {exc}")
                    convo = [*convo, {"role": "assistant", "content": att.result.content or ""},
                             {"role": "user", "content": RETRY_INSTRUCTION.format(error=exc)}]
                    continue
                await self._record(req, entry, att, ok=True)
                return parsed
        log.warning("llm.role_failed", role=role, prompt_id=prompt_id, requests=req.calls)
        return None

    async def run_tools[T: BaseModel](
        self,
        role: str,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[ToolSpec],
        handler: ToolHandler,
        *,
        final_tool: str,
        output: type[T],
        has_portfolio_ctx: bool,
        prompt_id: str,
        max_steps: int = 6,
        max_tokens: int = 2000,
    ) -> T | None:
        """Цикл с инструментами (Q&A): ответ — вызов `final_tool` с аргументами по схеме `output`.

        Если `final_tool` нет среди `tools`, он добавляется со схемой `output`. Инструменты выполняются
        последовательно; ошибка инструмента возвращается модели текстом. Ответ текстом без вызова, если в нём
        валидный JSON по схеме, тоже принимается. Неверный формат (нет вызова или аргументы не по схеме) —
        один ретрай на модели, затем следующая модель цепочки; `max_steps` запросов на модель.
        """
        if not 1 <= max_steps <= MAX_TOOL_STEPS:
            raise ValueError(f"max_steps в [1, {MAX_TOOL_STEPS}]")
        specs = list(tools)
        names = [t.name for t in specs]
        if len(set(names)) != len(names):
            raise ValueError("имена инструментов повторяются")
        if final_tool not in names:
            doc = (output.__doc__ or "").strip().splitlines()
            specs.append(ToolSpec.from_model(final_tool, doc[0] if doc else f"Итоговый ответ ({output.__name__})",
                                             output))
        payload = [t.to_openai() for t in specs]
        known = {t.name for t in specs}
        req = self._request(role, prompt_id)
        base = with_system(messages, FINAL_TOOL_INSTRUCTION.format(name=final_tool))
        plan = await self._plan(req, has_portfolio_ctx=has_portfolio_ctx, need_tools=True,
                                input_chars=_chars(base) + _chars(payload), max_tokens=max_tokens)
        for entry in plan:
            convo = list(base)
            format_retry_used = False
            for _ in range(max_steps):
                att = await self._call(req, entry, convo, max_tokens=max_tokens, tools=payload)
                if att is None:
                    break
                res = att.result
                format_error: str | None = None
                if not res.tool_calls:
                    try:
                        answer = parse_output(res.content, output)
                    except OutputError:
                        format_error = "no_tool_call"
                        convo = [*convo, {"role": "assistant", "content": res.content or ""},
                                 {"role": "user", "content": NO_TOOL_NUDGE.format(name=final_tool)}]
                    else:
                        await self._record(req, entry, att, ok=True)
                        return answer
                else:
                    convo.append(_assistant_message(res))
                    for call in res.tool_calls:
                        if call.name != final_tool:
                            convo.append(_tool_message(call, await self._exec(handler, call, known, role)))
                            continue
                        try:
                            answer = parse_output(call.arguments, output)
                        except OutputError as exc:
                            format_error = f"schema: {exc}"
                            convo.append(_tool_message(call, TOOL_SCHEMA_ERROR.format(error=exc, name=final_tool)))
                        else:
                            await self._record(req, entry, att, ok=True)
                            return answer
                await self._record(req, entry, att, ok=format_error is None, error=format_error)
                if format_error is not None:
                    if format_retry_used:
                        break
                    format_retry_used = True
            else:
                log.warning("llm.tool_steps_exhausted", role=role, provider=entry.provider, model=entry.model)
        log.warning("llm.role_failed", role=role, prompt_id=prompt_id, requests=req.calls)
        return None

    # --- внутреннее ---

    def _chain(self, role: str) -> tuple[ChainEntry, ...]:
        chain = self.cfg.roles.get(role)
        if chain is None:
            raise ValueError(f"роль {role!r} не объявлена в config/llm.yaml")
        return chain

    def _request(self, role: str, prompt_id: str) -> _Request:
        self._chain(role)
        if not prompt_id or len(prompt_id) > MAX_PROMPT_ID_LEN:
            raise ValueError(f"prompt_id: 1–{MAX_PROMPT_ID_LEN} символов")
        return _Request(role=role, prompt_id=prompt_id)

    async def _plan(self, req: _Request, *, has_portfolio_ctx: bool, need_tools: bool, input_chars: int,
                    max_tokens: int) -> list[ChainEntry]:
        out: list[ChainEntry] = []
        for entry in self._chain(req.role):
            if entry.provider not in self._clients:
                continue  # нет ключа в окружении
            if has_portfolio_ctx and not self.cfg.model(entry).data_in_ru:
                self._skip(req, entry, "residency")  # G8: портфель только моделям с данными в РФ
                continue
            if need_tools and not self.cfg.providers[entry.provider].tools_supported(entry.model):
                self._skip(req, entry, "no_tools")
                continue
            out.append(entry)
        if out and await self.role_degraded(req.role):
            # только самая дешёвая из допустимых сейчас (breaker, бюджет, квота) — без дорогих резервов
            ranked = sorted(out, key=lambda e: self.cfg.estimate_cost(e, input_chars, max_tokens))
            for entry in ranked:
                if await self._admit(req, entry, self.cfg.estimate_cost(entry, input_chars, max_tokens)):
                    log.info("llm.degraded", role=req.role, provider=entry.provider, model=entry.model)
                    return [entry]
            return []
        return out

    def _skip(self, req: _Request, entry: ChainEntry, reason: str) -> None:
        log.info("llm.skip", role=req.role, provider=entry.provider, model=entry.model, reason=reason)

    async def _admit(self, req: _Request, entry: ChainEntry, estimate: Decimal) -> bool:
        provider = entry.provider
        if self._breakers[provider].is_open(self._clock()):
            self._skip(req, entry, "circuit_open")
            return False
        budget = self.cfg.budgets[provider]
        if req.calls == 0 and req.role in self.cfg.qa_roles and \
                await self.qa_calls_this_month() >= budget.qa_monthly_quota:
            self._skip(req, entry, "qa_quota")
            return False
        if await self.monthly_spent(provider) + self._reserved[provider] + estimate > budget.monthly:
            self._skip(req, entry, "budget")
            return False
        return True

    async def _call(
        self,
        req: _Request,
        entry: ChainEntry,
        convo: Sequence[Mapping[str, Any]],
        *,
        max_tokens: int,
        response_format: Mapping[str, Any] | None = None,
        tools: Sequence[Mapping[str, Any]] | None = None,
    ) -> _Attempt | None:
        """Один HTTP-запрос с проверками допуска. None — пропуск или ошибка провайдера (уже в журнале)."""
        provider = entry.provider
        chars = _chars(convo) + _chars(tools)
        estimate = self.cfg.estimate_cost(entry, chars, max_tokens)
        if not await self._admit(req, entry, estimate):
            return None
        label = req.next_label()
        self._reserved[provider] += estimate
        try:
            res = await self._clients[provider].chat(
                entry.model, convo,
                max_tokens=self.cfg.output_cap(entry, max_tokens),
                temperature=self.cfg.temperature(req.role),
                response_format=response_format,
                tools=tools,
                tool_choice="auto" if tools else None,
            )
        except LLMError as exc:
            self._breakers[provider].record_failure(self._clock())
            log.warning("llm.call_failed", role=req.role, provider=provider, model=entry.model,
                        error=type(exc).__name__, status=exc.status)
            await self._ledger.record(CallRecord(
                at=self._clock(), role=label, provider=provider, model=entry.model, prompt_id=req.prompt_id,
                input_tokens=0, output_tokens=0, cost_rub=Decimal(0), ok=False,
                error=f"{type(exc).__name__}: {exc}",
            ))
            return None
        finally:
            self._reserved[provider] -= estimate
        self._breakers[provider].record_success()
        in_tok, out_tok = res.input_tokens, res.output_tokens
        if in_tok == 0 and out_tok == 0:  # нет usage — в бюджет идёт верхняя оценка, а не ноль
            in_tok = self.cfg.estimate_tokens(chars)
            out_tok = self.cfg.output_cap(entry, max_tokens)
        cost = self.cfg.model(entry).cost(in_tok, out_tok)
        return _Attempt(result=res, label=label, input_tokens=in_tok, output_tokens=out_tok, cost=cost)

    async def _record(self, req: _Request, entry: ChainEntry, att: _Attempt, *, ok: bool,
                      error: str | None = None) -> None:
        await self._ledger.record(CallRecord(
            at=self._clock(), role=att.label, provider=entry.provider, model=entry.model, prompt_id=req.prompt_id,
            input_tokens=att.input_tokens, output_tokens=att.output_tokens, cost_rub=att.cost, ok=ok, error=error,
        ))

    async def _exec(self, handler: ToolHandler, call: ToolCall, known: set[str], role: str) -> str:
        if call.name not in known:
            return f"Ошибка: инструмента {call.name!r} нет. Доступны: {', '.join(sorted(known))}"
        try:
            args = json.loads(call.arguments or "{}")
        except json.JSONDecodeError:
            return "Ошибка: аргументы — не JSON"
        if not isinstance(args, dict):
            return "Ошибка: аргументы — не JSON-объект"
        try:
            return await handler(call.name, args)
        except Exception as exc:  # ошибка read-only инструмента возвращается модели, цикл продолжается
            log.warning("llm.tool_failed", role=role, tool=call.name, error=type(exc).__name__)
            return f"Ошибка инструмента {call.name}: {str(exc)[:300]}"


if TYPE_CHECKING:
    from investbot.llm_roles.base import JsonLLM

    def _conforms(router: LLMRouter) -> JsonLLM:  # mypy: LLMRouter реализует контракт JsonLLM
        return router
