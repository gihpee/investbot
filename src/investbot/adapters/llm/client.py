"""Тонкий асинхронный клиент OpenAI-совместимого Chat Completions (Cloud.ru FM, Yandex AI Studio; §9.7, ADR-09).

Без SDK: httpx, повторы на 429/5xx и сетевых ошибках с паузами (Retry-After учитывается), типизированные ошибки.
Ключ не попадает ни в логи, ни в тексты исключений, ни в repr. `tools` и `response_format` в одном запросе
не передаются: Yandex AI Studio их не совмещает (отчёт 10, §1.1), поэтому запрет общий для всех провайдеров.
"""

from __future__ import annotations

import asyncio
import json
import ssl
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Self

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

BACKOFF = (1.0, 3.0)  # паузы между тремя попытками
MAX_RETRY_AFTER = 30.0  # сервер просит ждать дольше — сдаёмся сразу, роутер перейдёт к следующему провайдеру
_RETRYABLE = frozenset({408, 409, 429})


class LLMError(RuntimeError):
    """Базовая ошибка вызова LLM. Текст не содержит ключей и содержимого запроса."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class LLMHTTPError(LLMError):
    """4xx без повтора: неверный запрос, снятая модель (Yandex отвечает 400), превышен контекст."""


class LLMAuthError(LLMHTTPError):
    """401/403: ключ недействителен или нет прав."""


class LLMUnavailable(LLMError):
    """429/5xx/сеть после всех повторов или слишком долгий Retry-After."""


class LLMResponseError(LLMError):
    """200, но тело не похоже на ответ Chat Completions."""


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str  # JSON-строка аргументов, как вернула модель


@dataclass(frozen=True)
class ChatResult:
    content: str | None
    tool_calls: tuple[ToolCall, ...]
    input_tokens: int  # 0, если провайдер не прислал usage
    output_tokens: int  # у reasoning-моделей включает скрытые рассуждения
    raw_finish_reason: str | None


class _Wire(BaseModel):
    model_config = ConfigDict(extra="ignore")


class _Function(_Wire):
    name: str = ""
    arguments: str | dict[str, Any] | None = None


class _ToolCallIn(_Wire):
    id: str | None = None
    function: _Function


class _Message(_Wire):
    content: str | list[Any] | None = None
    tool_calls: list[_ToolCallIn] | None = None
    function_call: _Function | None = None  # устаревший формат functions (GigaChat через прокси)


class _Choice(_Wire):
    message: _Message
    finish_reason: str | None = None


class _Usage(_Wire):
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


class _Completion(_Wire):
    choices: list[_Choice] = Field(min_length=1)
    usage: _Usage | None = None


def _ssl(verify: str | Path | bool) -> ssl.SSLContext | bool:
    """Строка — путь к bundle корней (в образе с Russian Trusted Root CA), как у клиента ISS."""
    if isinstance(verify, bool):
        return verify
    if Path(verify).is_dir():
        return ssl.create_default_context(capath=str(verify))
    return ssl.create_default_context(cafile=str(verify))


def _retryable(status: int) -> bool:
    return status in _RETRYABLE or status >= 500


def retry_after_seconds(resp: httpx.Response, now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> float | None:
    """Retry-After в секундах или HTTP-дата; мусор — None."""
    raw = resp.headers.get("retry-after")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        at = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if at.tzinfo is None:
        at = at.replace(tzinfo=UTC)
    return max(0.0, (at - now()).total_seconds())


def _error_detail(resp: httpx.Response) -> str:
    """Короткое описание ошибки из тела (`error.message`), без эха запроса."""
    try:
        body = resp.json()
    except ValueError:
        return ""
    err = body.get("error") if isinstance(body, dict) else None
    msg = err.get("message") if isinstance(err, dict) else err if isinstance(err, str) else None
    return f": {str(msg)[:200]}" if msg else ""


def _text(content: str | list[Any] | None) -> str | None:
    if content is None or isinstance(content, str):
        return content
    parts = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") in (None, "text")]
    return "".join(str(p) for p in parts)


def _arguments(fn: _Function) -> str:
    if fn.arguments is None:
        return "{}"
    return fn.arguments if isinstance(fn.arguments, str) else json.dumps(fn.arguments, ensure_ascii=False)


def parse_completion(payload: Any) -> ChatResult:
    try:
        completion = _Completion.model_validate(payload)
    except ValidationError as exc:
        raise LLMResponseError(f"неожиданный формат ответа Chat Completions: {exc.error_count()} ошибок") from exc
    choice = completion.choices[0]
    msg = choice.message
    calls = [
        ToolCall(id=tc.id or f"call_{i}", name=tc.function.name, arguments=_arguments(tc.function))
        for i, tc in enumerate(msg.tool_calls or [])
    ]
    if not calls and msg.function_call is not None:
        calls = [ToolCall(id="call_0", name=msg.function_call.name, arguments=_arguments(msg.function_call))]
    usage = completion.usage or _Usage()
    return ChatResult(
        content=_text(msg.content),
        tool_calls=tuple(calls),
        input_tokens=usage.prompt_tokens or 0,
        output_tokens=usage.completion_tokens or 0,
        raw_finish_reason=choice.finish_reason,
    )


class _Gate:
    """Ограничение параллелизма (Yandex: 10 одновременных) и частоты (Cloud.ru: 20 rps) запросов."""

    def __init__(self, rps: float | None, concurrency: int | None, sleep: Callable[[float], Awaitable[object]]) -> None:
        self._sem = asyncio.Semaphore(concurrency) if concurrency else None
        self._interval = 1.0 / rps if rps else 0.0
        self._lock = asyncio.Lock()
        self._next = 0.0
        self._sleep = sleep

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        if self._sem is not None:
            await self._sem.acquire()
        try:
            if self._interval:
                async with self._lock:
                    wait = self._next - time.monotonic()
                    if wait > 0:
                        await self._sleep(wait)
                    self._next = max(time.monotonic(), self._next) + self._interval
            yield
        finally:
            if self._sem is not None:
                self._sem.release()


class OpenAICompatClient:
    """POST {base_url}/chat/completions. Ретраи — только здесь (у роутера своя цепочка провайдеров)."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout: float = 120.0,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
        model_template: str = "{model}",
        auth_scheme: str = "Bearer",
        rps: float | None = None,
        max_concurrency: int | None = None,
        backoff: Sequence[float] = BACKOFF,
        max_retry_after: float = MAX_RETRY_AFTER,
        verify: str | Path | bool = True,
    ) -> None:
        if not api_key:
            raise ValueError("api_key пуст")
        if model_template.count("{model}") != 1:
            raise ValueError("model_template: ровно одно вхождение {model}")
        self._sleep = sleep
        self._model_template = model_template
        self._backoff = tuple(backoff)
        self._max_retry_after = max_retry_after
        self._gate = _Gate(rps, max_concurrency, sleep)
        self.host = httpx.URL(base_url).host
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            transport=transport,
            verify=_ssl(verify),
            headers={
                **dict(headers or {}),
                "Authorization": f"{auth_scheme} {api_key}",
                "User-Agent": "investbot/0.1",
            },
        )

    def __repr__(self) -> str:  # без ключа
        return f"OpenAICompatClient(host={self.host!r})"

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    def wire_model(self, model: str) -> str:
        """Имя модели в запросе: у Yandex — URI gpt://<folder>/<model>."""
        return self._model_template.replace("{model}", model)

    async def chat(
        self,
        model: str,
        messages: Sequence[Mapping[str, Any]],
        *,
        max_tokens: int,
        temperature: float,
        response_format: Mapping[str, Any] | None = None,
        tools: Sequence[Mapping[str, Any]] | None = None,
        tool_choice: str | Mapping[str, Any] | None = None,
    ) -> ChatResult:
        if tools and response_format:
            raise ValueError("tools и response_format в одном запросе не передаются (Yandex AI Studio их не совмещает)")
        body: dict[str, Any] = {
            "model": self.wire_model(model),
            "messages": [dict(m) for m in messages],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if response_format:
            body["response_format"] = dict(response_format)
        if tools:
            body["tools"] = [dict(t) for t in tools]
            if tool_choice is not None:
                body["tool_choice"] = tool_choice
        return parse_completion(await self._post("/chat/completions", body))

    async def _post(self, path: str, body: Mapping[str, Any]) -> Any:
        for delay in (*self._backoff, None):
            try:
                async with self._gate.slot():
                    resp = await self._http.post(path, json=body)
            except httpx.TransportError as exc:
                if delay is None:
                    raise LLMUnavailable(f"{self.host}: сетевая ошибка {type(exc).__name__}") from exc
                wait = delay
            else:
                if resp.is_success:
                    try:
                        return resp.json()
                    except ValueError as exc:
                        raise LLMResponseError(f"{self.host}: ответ не JSON", status=resp.status_code) from exc
                status = resp.status_code
                detail = _error_detail(resp)
                if status in (401, 403):
                    raise LLMAuthError(f"{self.host}: {status} — проверьте ключ и права{detail}", status=status)
                if not _retryable(status):
                    raise LLMHTTPError(f"{self.host}: HTTP {status}{detail}", status=status)
                if delay is None:
                    raise LLMUnavailable(f"{self.host}: HTTP {status} после повторов{detail}", status=status)
                hint = retry_after_seconds(resp)
                if hint is not None and hint > self._max_retry_after:
                    raise LLMUnavailable(f"{self.host}: HTTP {status}, Retry-After {hint:.0f} с", status=status)
                wait = max(delay, hint or 0.0)
            await self._sleep(wait)
        raise AssertionError("unreachable")
