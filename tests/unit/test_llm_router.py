"""LLM-роутер, клиент, журнал и конфиг (ARCHITECTURE §9.6, §9.7). Сети нет: провайдеры подменяются respx."""

from __future__ import annotations

import copy
import json
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import httpx
import pytest
import respx
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.ext.asyncio import AsyncEngine

from investbot.adapters.llm.client import (
    LLMAuthError,
    LLMHTTPError,
    LLMResponseError,
    LLMUnavailable,
    OpenAICompatClient,
    retry_after_seconds,
)
from investbot.adapters.llm.config import BreakerSpec, ChainEntry, LLMConfig, load_llm_config
from investbot.adapters.llm.factory import build_clients, build_router
from investbot.adapters.llm.ledger import CallLedger, CallRecord, month_start
from investbot.adapters.llm.router import (
    CircuitBreaker,
    LLMRouter,
    OutputError,
    ToolSpec,
    extract_json,
    parse_output,
)
from investbot.data.db import make_engine, metadata

ROOT = Path(__file__).resolve().parents[2]
CLOUD = "https://cloud.test/v1"
YA = "https://ya.test/v1"
CLOUD_CHAT = f"{CLOUD}/chat/completions"
YA_CHAT = f"{YA}/chat/completions"
ENV = {"CLOUDRU_API_KEY": "ck-secret", "YC_API_KEY": "yk-secret", "YC_FOLDER_ID": "b1gfolder"}
NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)


class Verdict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    label: Literal["ok", "block"]
    score: float = Field(ge=0, le=1)


class Answer(BaseModel):
    """Итоговый ответ пользователю."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    text: str = Field(max_length=200)
    used_fact_ids: list[str]


VALID = '{"label": "ok", "score": 0.7}'
SYSTEM = [{"role": "system", "content": "Ты критик."}, {"role": "user", "content": "План: {{f:1}}"}]
POSITION_TOOL = ToolSpec("get_position", "Позиция портфеля",
                         {"type": "object", "properties": {"ticker": {"type": "string"}}, "required": ["ticker"]})


def base_cfg() -> dict[str, Any]:
    return {
        "version": 1,
        "primary_provider": "cloudru",
        "budgets": {
            "cloudru": {"monthly": 100, "per_run": 10, "qa_monthly_quota": 5},
            "yandex": {"monthly": 100, "per_run": 10, "qa_monthly_quota": 5},
        },
        "temperatures": {"tagger": 0},
        "providers": {
            "cloudru": {
                "base_url": CLOUD, "api_key_env": "CLOUDRU_API_KEY",
                "models": {
                    "big-r": {"data_in_ru": True, "reasoning": True,
                              "price_in_rub_per_m": 100, "price_out_rub_per_m": 300},
                    "small": {"data_in_ru": True, "price_in_rub_per_m": 10, "price_out_rub_per_m": 10},
                    "ext": {"data_in_ru": False, "price_in_rub_per_m": 1, "price_out_rub_per_m": 1},
                    "nofc": {"data_in_ru": True, "supports_tools": False, "price_in_rub_per_m": 1,
                             "price_out_rub_per_m": 1},
                },
            },
            "yandex": {
                "base_url": YA, "api_key_env": "YC_API_KEY", "header_envs": {"OpenAI-Project": "YC_FOLDER_ID"},
                "model_template": "gpt://${YC_FOLDER_ID}/{model}", "max_concurrency": 2,
                "models": {"alice": {"data_in_ru": True, "price_in_rub_per_m": 500, "price_out_rub_per_m": 1200}},
            },
        },
        "roles": {
            "writer": ["cloudru:big-r", "yandex:alice"],
            "digest": ["cloudru:big-r", "cloudru:small"],
            "tagger": ["cloudru:ext", "cloudru:small"],
            "qa": ["cloudru:nofc", "cloudru:big-r", "yandex:alice"],
            "allocation_advisor": ["cloudru:big-r"],
        },
    }


def completion(content: str | None = None, *, tool_calls: list[dict[str, Any]] | None = None,
               usage: tuple[int, int] | None = (1000, 500), finish: str = "stop") -> httpx.Response:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    body: dict[str, Any] = {"id": "x", "object": "chat.completion",
                            "choices": [{"index": 0, "message": message, "finish_reason": finish}]}
    if usage is not None:
        body["usage"] = {"prompt_tokens": usage[0], "completion_tokens": usage[1], "total_tokens": sum(usage)}
    return httpx.Response(200, json=body)


def call(name: str, args: dict[str, Any] | str, call_id: str = "c1") -> dict[str, Any]:
    arguments = args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}


def body(route: respx.Route, i: int = -1) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(route.calls[i].request.content)
    return payload


@dataclass
class Clock:
    now: datetime = NOW

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw: float) -> None:
        self.now += timedelta(**kw)


@pytest.fixture
async def engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    eng = make_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with eng.begin() as conn:
        await conn.run_sync(metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture
def ledger(engine: AsyncEngine) -> CallLedger:
    return CallLedger(engine)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def delays() -> list[float]:
    return []


@pytest.fixture
def mock() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as router:
        yield router


@pytest.fixture
def make_router(ledger: CallLedger, clock: Clock, delays: list[float]) -> Any:
    async def fake_sleep(seconds: float) -> None:
        delays.append(seconds)

    def make(cfg: dict[str, Any] | None = None, env: dict[str, str] | None = None) -> LLMRouter:
        config = LLMConfig.model_validate(cfg or base_cfg())
        clients = build_clients(config, ENV if env is None else env, sleep=fake_sleep)
        return LLMRouter(config, clients, ledger, clock)

    return make


async def rows(ledger: CallLedger) -> list[CallRecord]:
    return await ledger.calls_since(NOW - timedelta(days=400))


# --- разбор JSON ---------------------------------------------------------------------------------------------


def test_extract_json_fences_prose_and_think() -> None:
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('Вот ответ:\n```\n{"a": {"b": "}"}}\n```\nГотово.') == {"a": {"b": "}"}}
    assert extract_json('Конечно! {"a": [1, 2], "t": "{{f:12}}"} — это всё.') == {"a": [1, 2], "t": "{{f:12}}"}
    assert extract_json('<think>может {"a": 0}?</think>{"a": 2}') == {"a": 2}
    assert extract_json('Плейсхолдер {{f:1}} и {"a": 3}') == {"a": 3}
    for bad in ("", None, "нет json", "[1, 2]", '{"a": '):
        with pytest.raises(OutputError):
            extract_json(bad)


def test_parse_output_takes_first_valid_object() -> None:
    text = 'Пример схемы: {"label": "maybe"}. Ответ: {"label": "block", "score": 1}'
    assert parse_output(text, Verdict) == Verdict(label="block", score=1)
    with pytest.raises(OutputError, match="label"):
        parse_output('{"label": "maybe", "score": 0.5}', Verdict)
    with pytest.raises(OutputError, match=r"[Ee]xtra"):
        parse_output('{"label": "ok", "score": 0.5, "why": "x"}', Verdict)


# --- complete_json -------------------------------------------------------------------------------------------


async def test_complete_json_happy_path_and_cost(mock: respx.MockRouter, make_router: Any,
                                                 ledger: CallLedger) -> None:
    cloud = mock.post(CLOUD_CHAT).mock(return_value=completion(f"```json\n{VALID}\n```"))
    router = make_router()
    out = await router.complete_json("writer", SYSTEM, Verdict, has_portfolio_ctx=True, prompt_id="critic.v1")
    assert out == Verdict(label="ok", score=0.7)
    sent = body(cloud)
    assert sent["model"] == "big-r"
    assert sent["response_format"] == {"type": "json_object"}
    assert "tools" not in sent
    assert sent["max_tokens"] == 6000  # reasoning: лимит × 3
    assert sent["temperature"] == 0.2
    assert sent["messages"][0]["role"] == "system"
    assert sent["messages"][0]["content"].startswith("Ты критик.")
    assert '"label"' in sent["messages"][0]["content"]  # JSON-схема выхода
    assert cloud.calls[0].request.headers["authorization"] == "Bearer ck-secret"
    [rec] = await rows(ledger)
    assert (rec.role, rec.provider, rec.model, rec.prompt_id, rec.ok) == (
        "writer", "cloudru", "big-r", "critic.v1", True)
    assert (rec.input_tokens, rec.output_tokens) == (1000, 500)
    assert rec.cost_rub == Decimal("0.25")  # 1000 × 100 / 1M + 500 × 300 / 1M
    assert await router.monthly_spent("cloudru") == Decimal("0.25")
    assert await router.monthly_spent("yandex") == 0


async def test_schema_retry_once_then_next_provider(mock: respx.MockRouter, make_router: Any,
                                                    ledger: CallLedger) -> None:
    cloud = mock.post(CLOUD_CHAT).mock(side_effect=[
        completion('{"label": "ok", "score": 0.5, "extra": 1}'),
        completion("Извините, не могу."),
    ])
    ya = mock.post(YA_CHAT).mock(return_value=completion(VALID, usage=(2000, 100)))
    router = make_router()
    out = await router.complete_json("writer", SYSTEM, Verdict, has_portfolio_ctx=True, prompt_id="critic.v1")
    assert out == Verdict(label="ok", score=0.7)
    assert cloud.call_count == 2
    retry_msgs = body(cloud, 1)["messages"]
    assert retry_msgs[-2] == {"role": "assistant", "content": '{"label": "ok", "score": 0.5, "extra": 1}'}
    assert retry_msgs[-1]["role"] == "user" and "extra" in retry_msgs[-1]["content"]
    # Yandex: URI модели с каталогом, заголовок каталога, ретрай схемы не переносится на следующего провайдера
    sent = body(ya)
    assert sent["model"] == "gpt://b1gfolder/alice"
    assert sent["max_tokens"] == 2000
    assert len(sent["messages"]) == len(SYSTEM)
    assert ya.calls[0].request.headers["openai-project"] == "b1gfolder"
    assert ya.calls[0].request.headers["authorization"] == "Bearer yk-secret"
    recs = await rows(ledger)
    assert [(r.role, r.provider, r.ok) for r in recs] == [
        ("writer", "cloudru", False), ("writer#2", "cloudru", False), ("writer#3", "yandex", True)]
    assert all(r.error and r.error.startswith("schema:") for r in recs[:2])
    assert recs[2].cost_rub == Decimal("1.12")  # 2000 × 500 / 1M + 100 × 1200 / 1M
    assert await router.monthly_spent("cloudru") == Decimal("0.5")


async def test_all_providers_fail_returns_none(mock: respx.MockRouter, make_router: Any,
                                               ledger: CallLedger) -> None:
    cloud = mock.post(CLOUD_CHAT).mock(return_value=httpx.Response(400, json={"error": {"message": "model retired"}}))
    ya = mock.post(YA_CHAT).mock(return_value=completion("нет"))
    router = make_router()
    assert await router.complete_json("writer", SYSTEM, Verdict, has_portfolio_ctx=False, prompt_id="p") is None
    assert cloud.call_count == 1  # 4xx без повтора
    assert ya.call_count == 2
    recs = await rows(ledger)
    assert [r.ok for r in recs] == [False, False, False]
    assert recs[0].error is not None and "LLMHTTPError" in recs[0].error and "model retired" in recs[0].error
    assert recs[0].cost_rub == 0


async def test_residency_skips_foreign_models_with_portfolio(mock: respx.MockRouter, make_router: Any) -> None:
    cloud = mock.post(CLOUD_CHAT).mock(return_value=completion(VALID))
    router = make_router()
    await router.complete_json("tagger", SYSTEM, Verdict, has_portfolio_ctx=True, prompt_id="tagger.v1")
    assert body(cloud)["model"] == "small"  # ext (data_in_ru=false) пропущена
    assert body(cloud)["temperature"] == 0
    await router.complete_json("tagger", SYSTEM, Verdict, has_portfolio_ctx=False, prompt_id="tagger.v1")
    assert body(cloud)["model"] == "ext"  # публичный текст можно


async def test_unknown_role_and_bad_prompt_id(make_router: Any) -> None:
    router = make_router()
    with pytest.raises(ValueError, match="роль"):
        await router.complete_json("nope", SYSTEM, Verdict, has_portfolio_ctx=False, prompt_id="p")
    with pytest.raises(ValueError, match="prompt_id"):
        await router.complete_json("writer", SYSTEM, Verdict, has_portfolio_ctx=False, prompt_id="x" * 65)


# --- бюджет --------------------------------------------------------------------------------------------------


def test_month_start_is_moscow() -> None:
    assert month_start(NOW) == datetime(2026, 9, 30, 21, 0, tzinfo=UTC)
    assert month_start(datetime(2026, 9, 30, 21, 30, tzinfo=UTC)) == datetime(2026, 9, 30, 21, 0, tzinfo=UTC)
    assert month_start(datetime(2026, 9, 30, 20, 59, tzinfo=UTC)) == datetime(2026, 8, 31, 21, 0, tzinfo=UTC)
    with pytest.raises(ValueError):
        month_start(datetime(2026, 10, 1))  # naive запрещён


def spend(at: datetime, cost: str, provider: str = "cloudru", role: str = "writer") -> CallRecord:
    return CallRecord(at=at, role=role, provider=provider, model="big-r", prompt_id="p", input_tokens=1,
                      output_tokens=1, cost_rub=Decimal(cost), ok=True)


async def test_budget_skip_counts_only_moscow_month(mock: respx.MockRouter, make_router: Any,
                                                    ledger: CallLedger) -> None:
    await ledger.record(spend(datetime(2026, 9, 30, 20, 59, 59, tzinfo=UTC), "999"))  # 23:59:59 MSK, сентябрь
    await ledger.record(spend(datetime(2026, 9, 30, 21, 0, tzinfo=UTC), "99.99"))  # 00:00 MSK, октябрь
    cloud = mock.post(CLOUD_CHAT).mock(return_value=completion(VALID))
    ya = mock.post(YA_CHAT).mock(return_value=completion(VALID))
    router = make_router()
    assert await router.monthly_spent("cloudru") == Decimal("99.99")
    out = await router.complete_json("writer", SYSTEM, Verdict, has_portfolio_ctx=True, prompt_id="p")
    assert out is not None
    assert cloud.call_count == 0  # 99,99 + оценка > 100
    assert ya.call_count == 1


async def test_degraded_role_uses_only_cheapest_model(mock: respx.MockRouter, make_router: Any,
                                                      ledger: CallLedger) -> None:
    cloud = mock.post(CLOUD_CHAT).mock(return_value=completion("не JSON"))
    router = make_router()
    assert not await router.role_degraded("digest")
    await ledger.record(spend(NOW - timedelta(days=1), "90"))
    assert await router.role_degraded("digest")
    assert await router.complete_json("digest", SYSTEM, Verdict, has_portfolio_ctx=True, prompt_id="p") is None
    assert {body(cloud, i)["model"] for i in range(cloud.call_count)} == {"small"}  # big-r не пробуется
    assert cloud.call_count == 2  # ретрай схемы на дешёвой модели


async def test_qa_quota_counts_questions_not_steps(mock: respx.MockRouter, make_router: Any) -> None:
    cfg = base_cfg()
    cfg["budgets"]["cloudru"]["qa_monthly_quota"] = 1
    cfg["budgets"]["yandex"]["qa_monthly_quota"] = 1
    cloud = mock.post(CLOUD_CHAT).mock(side_effect=[
        completion(None, tool_calls=[call("get_position", {"ticker": "SBER"})]),
        completion(None, tool_calls=[call("submit_answer", {"text": "ok", "used_fact_ids": []}, "c2")]),
    ])
    router = make_router(cfg)

    async def handler(name: str, args: dict[str, Any]) -> str:
        return "{}"

    first = await router.run_tools("qa", SYSTEM, [POSITION_TOOL], handler, final_tool="submit_answer",
                                   output=Answer, has_portfolio_ctx=True, prompt_id="qa.v1")
    assert first is not None
    assert await router.qa_calls_this_month() == 1  # два запроса, один вопрос
    second = await router.run_tools("qa", SYSTEM, [POSITION_TOOL], handler, final_tool="submit_answer",
                                    output=Answer, has_portfolio_ctx=True, prompt_id="qa.v1")
    assert second is None
    assert cloud.call_count == 2


# --- circuit breaker -----------------------------------------------------------------------------------------


def test_breaker_window_and_half_open() -> None:
    br = CircuitBreaker(BreakerSpec())

    def at(minutes: int, seconds: int = 0) -> datetime:
        return NOW + timedelta(minutes=minutes, seconds=seconds)

    for t in (at(0), at(4), at(9, 1), at(9, 30)):  # не больше двух ошибок в любом окне 5 минут
        br.record_failure(t)
        assert not br.is_open(t)
    br.record_failure(at(10))  # третья за 5 минут (9:01, 9:30, 10:00)
    assert br.is_open(at(10, 1))
    assert br.is_open(at(19, 59))
    assert not br.is_open(at(20))  # пауза 10 минут прошла → пробный вызов
    br.record_failure(at(20, 1))  # ошибка пробного — сразу снова открыт
    assert br.is_open(at(25))
    assert not br.is_open(at(30, 1))
    br.record_success()
    br.record_failure(at(31))
    assert not br.is_open(at(31))  # после успешной пробы снова нужно 3 ошибки


async def test_breaker_opens_after_three_errors_and_recovers(mock: respx.MockRouter, make_router: Any,
                                                             clock: Clock, delays: list[float]) -> None:
    state = {"cloud_ok": False}

    def cloud_side_effect(request: httpx.Request) -> httpx.Response:
        return completion(VALID) if state["cloud_ok"] else httpx.Response(503)

    cloud = mock.post(CLOUD_CHAT).mock(side_effect=cloud_side_effect)
    ya = mock.post(YA_CHAT).mock(return_value=completion(VALID))
    router = make_router()

    async def ask() -> Verdict | None:
        return await router.complete_json("writer", SYSTEM, Verdict, has_portfolio_ctx=True, prompt_id="p")

    for _ in range(3):
        assert await ask() is not None  # Cloud.ru падает, отвечает Yandex
        clock.advance(minutes=1)
    assert cloud.call_count == 9  # 3 вызова × 3 попытки клиента
    assert delays == [1.0, 3.0] * 3
    assert router.breaker_open("cloudru")
    assert await ask() is not None
    assert cloud.call_count == 9  # breaker открыт — Cloud.ru пропущен
    clock.advance(minutes=10)
    assert await ask() is not None  # пробный вызов падает → снова открыт
    assert cloud.call_count == 12
    assert router.breaker_open("cloudru")
    clock.advance(minutes=10)
    state["cloud_ok"] = True
    ya_before = ya.call_count
    assert await ask() is not None
    assert cloud.call_count == 13 and ya.call_count == ya_before
    assert not router.breaker_open("cloudru")


# --- журнал --------------------------------------------------------------------------------------------------


async def test_ledger_cost_accounting(mock: respx.MockRouter, make_router: Any, ledger: CallLedger) -> None:
    mock.post(CLOUD_CHAT).mock(side_effect=[completion(VALID, usage=(1234, 567)), completion(VALID, usage=None)])
    router = make_router()
    await router.complete_json("writer", SYSTEM, Verdict, has_portfolio_ctx=True, prompt_id="p", max_tokens=100)
    await router.complete_json("writer", SYSTEM, Verdict, has_portfolio_ctx=True, prompt_id="p", max_tokens=100)
    first, second = await rows(ledger)
    assert first.cost_rub == Decimal("0.2935")  # 1234 × 100 / 1M + 567 × 300 / 1M
    # без usage — верхняя оценка: вход по символам / 4, выход = лимит × 3 (reasoning)
    assert second.output_tokens == 300 and second.input_tokens > 0
    assert second.cost_rub == (Decimal(second.input_tokens) * 100 + 300 * 300) / 1_000_000
    assert await router.monthly_spent("cloudru") == first.cost_rub + second.cost_rub
    assert await ledger.spent_since(NOW - timedelta(days=1)) == first.cost_rub + second.cost_rub
    assert await ledger.spent_since(NOW + timedelta(seconds=1)) == 0


# --- инструменты ---------------------------------------------------------------------------------------------


async def test_tool_loop_one_tool_then_final(mock: respx.MockRouter, make_router: Any, ledger: CallLedger) -> None:
    cloud = mock.post(CLOUD_CHAT).mock(side_effect=[
        completion(None, tool_calls=[call("get_position", {"ticker": "SBER"})], finish="tool_calls"),
        completion(None, tool_calls=[call("submit_answer", {"text": "Доля {{f:1}}", "used_fact_ids": ["1"]}, "c2")],
                   finish="tool_calls"),
    ])
    seen: list[tuple[str, dict[str, Any]]] = []

    async def handler(name: str, args: dict[str, Any]) -> str:
        seen.append((name, args))
        return '{"facts": [{"fact_id": "1", "value": "0.1"}]}'

    router = make_router()
    out = await router.run_tools("qa", SYSTEM, [POSITION_TOOL], handler, final_tool="submit_answer",
                                 output=Answer, has_portfolio_ctx=True, prompt_id="qa.v1")
    assert out == Answer(text="Доля {{f:1}}", used_fact_ids=["1"])
    assert seen == [("get_position", {"ticker": "SBER"})]
    first, second = body(cloud, 0), body(cloud, 1)
    assert first["model"] == "big-r"  # nofc (без function calling) пропущена
    for sent in (first, second):
        assert "response_format" not in sent  # tools и response_format не вместе
        assert [t["function"]["name"] for t in sent["tools"]] == ["get_position", "submit_answer"]
        assert sent["tool_choice"] == "auto"
    final_params = first["tools"][1]["function"]["parameters"]
    assert set(final_params["properties"]) == {"text", "used_fact_ids"}
    assert first["tools"][1]["function"]["description"] == "Итоговый ответ пользователю."
    assistant, tool_msg = second["messages"][-2:]
    assert assistant["role"] == "assistant" and assistant["tool_calls"][0]["id"] == "c1"
    assert tool_msg["role"] == "tool" and tool_msg["tool_call_id"] == "c1" and "fact_id" in tool_msg["content"]
    assert [(r.role, r.ok) for r in await rows(ledger)] == [("qa", True), ("qa#2", True)]


async def test_tool_loop_schema_retry_then_fallback_without_tools_support(
        mock: respx.MockRouter, make_router: Any, ledger: CallLedger) -> None:
    bad = completion(None, tool_calls=[call("submit_answer", {"text": "x"})])
    cloud = mock.post(CLOUD_CHAT).mock(side_effect=[bad, bad])
    # Yandex отвечает JSON текстом без вызова: принимается, раз проходит схему
    ya = mock.post(YA_CHAT).mock(return_value=completion('{"text": "ok", "used_fact_ids": []}'))
    router = make_router()

    async def handler(name: str, args: dict[str, Any]) -> str:
        raise AssertionError("инструменты не вызываются")

    out = await router.run_tools("qa", SYSTEM, [], handler, final_tool="submit_answer", output=Answer,
                                 has_portfolio_ctx=True, prompt_id="qa.v1")
    assert out == Answer(text="ok", used_fact_ids=[])
    assert cloud.call_count == 2
    retry_tool_msg = body(cloud, 1)["messages"][-1]
    assert retry_tool_msg["role"] == "tool" and "used_fact_ids" in retry_tool_msg["content"]
    sent = body(ya)
    assert "tools" in sent and "response_format" not in sent
    assert [(r.role, r.provider, r.ok) for r in await rows(ledger)] == [
        ("qa", "cloudru", False), ("qa#2", "cloudru", False), ("qa#3", "yandex", True)]


async def test_tool_errors_are_returned_to_model(mock: respx.MockRouter, make_router: Any) -> None:
    cloud = mock.post(CLOUD_CHAT).mock(side_effect=[
        completion(None, tool_calls=[call("get_position", {"ticker": "XXX"}), call("rm_rf", "{}", "c2")]),
        completion("Готово"),  # текст без вызова и без JSON → просьба вызвать submit_answer
        completion(None, tool_calls=[call("submit_answer", {"text": "нет данных", "used_fact_ids": []}, "c3")]),
    ])

    async def handler(name: str, args: dict[str, Any]) -> str:
        raise KeyError(args["ticker"])

    router = make_router()
    out = await router.run_tools("qa", SYSTEM, [POSITION_TOOL], handler, final_tool="submit_answer",
                                 output=Answer, has_portfolio_ctx=True, prompt_id="qa.v1")
    assert out is not None and out.text == "нет данных"
    msgs = body(cloud, 1)["messages"]
    assert msgs[-2]["content"].startswith("Ошибка инструмента get_position")
    assert msgs[-1]["content"].startswith("Ошибка: инструмента 'rm_rf' нет")
    nudge = body(cloud, 2)["messages"][-1]
    assert nudge["role"] == "user" and "submit_answer" in nudge["content"]


async def test_tool_loop_without_tool_capable_provider_returns_none(mock: respx.MockRouter, make_router: Any) -> None:
    cfg = base_cfg()
    cfg["roles"]["qa"] = ["cloudru:nofc"]
    cloud = mock.post(CLOUD_CHAT).mock(return_value=completion(VALID))
    router = make_router(cfg)

    async def handler(name: str, args: dict[str, Any]) -> str:
        return ""

    assert await router.run_tools("qa", SYSTEM, [], handler, final_tool="submit_answer", output=Answer,
                                  has_portfolio_ctx=False, prompt_id="qa.v1") is None
    assert cloud.call_count == 0
    with pytest.raises(ValueError, match="повторяются"):
        await router.run_tools("qa", SYSTEM, [POSITION_TOOL, POSITION_TOOL], handler, final_tool="submit_answer",
                               output=Answer, has_portfolio_ctx=False, prompt_id="qa.v1")


# --- клиент --------------------------------------------------------------------------------------------------


async def test_client_never_sends_tools_with_response_format() -> None:
    async with OpenAICompatClient(CLOUD, "k") as client:
        with pytest.raises(ValueError, match="response_format"):
            await client.chat("m", SYSTEM, max_tokens=10, temperature=0, response_format={"type": "json_object"},
                              tools=[POSITION_TOOL.to_openai()])


async def test_client_retries_and_typed_errors(mock: respx.MockRouter) -> None:
    delays: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        delays.append(seconds)

    route = mock.post(CLOUD_CHAT)
    async with OpenAICompatClient(CLOUD, "very-secret-key", sleep=fake_sleep) as client:
        assert "very-secret-key" not in repr(client)
        route.side_effect = [httpx.Response(429, headers={"Retry-After": "2.5"}), httpx.Response(502), completion("hi")]
        res = await client.chat("m", SYSTEM, max_tokens=10, temperature=0)
        assert (res.content, res.input_tokens, res.output_tokens, res.raw_finish_reason) == ("hi", 1000, 500, "stop")
        assert delays == [2.5, 3.0]

        delays.clear()
        route.side_effect = [httpx.Response(401, json={"error": {"message": "bad key"}})]
        with pytest.raises(LLMAuthError) as auth:
            await client.chat("m", SYSTEM, max_tokens=10, temperature=0)
        assert auth.value.status == 401 and "very-secret-key" not in str(auth.value)

        route.side_effect = [httpx.Response(429, headers={"Retry-After": "120"})]
        with pytest.raises(LLMUnavailable, match="Retry-After"):
            await client.chat("m", SYSTEM, max_tokens=10, temperature=0)
        assert delays == []

        route.side_effect = httpx.ConnectError("boom")
        with pytest.raises(LLMUnavailable, match="ConnectError"):
            await client.chat("m", SYSTEM, max_tokens=10, temperature=0)
        assert delays == [1.0, 3.0]

        route.side_effect = [httpx.Response(422, json={"error": "context too long"})]
        with pytest.raises(LLMHTTPError, match="context too long"):
            await client.chat("m", SYSTEM, max_tokens=10, temperature=0)

        route.side_effect = [httpx.Response(200, json={"choices": []})]
        with pytest.raises(LLMResponseError):
            await client.chat("m", SYSTEM, max_tokens=10, temperature=0)


async def test_client_parses_legacy_function_call_and_content_parts(mock: respx.MockRouter) -> None:
    mock.post(CLOUD_CHAT).mock(return_value=httpx.Response(200, json={"choices": [{"message": {
        "content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}],
        "function_call": {"name": "submit_answer", "arguments": {"text": "x"}},
    }, "finish_reason": "function_call"}]}))
    async with OpenAICompatClient(CLOUD, "k") as client:
        res = await client.chat("m", SYSTEM, max_tokens=10, temperature=0)
    assert res.content == "ab"
    calls = [(c.id, c.name, json.loads(c.arguments)) for c in res.tool_calls]
    assert calls == [("call_0", "submit_answer", {"text": "x"})]
    assert (res.input_tokens, res.output_tokens) == (0, 0)


async def test_client_rate_gate_spaces_requests(mock: respx.MockRouter) -> None:
    delays: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        delays.append(seconds)

    route = mock.post(CLOUD_CHAT).mock(return_value=completion("hi"))
    async with OpenAICompatClient(CLOUD, "k", sleep=fake_sleep, rps=10, max_concurrency=1) as client:
        for _ in range(3):
            await client.chat("m", SYSTEM, max_tokens=10, temperature=0)
    assert route.call_count == 3
    assert delays == [pytest.approx(0.1, abs=0.02), pytest.approx(0.2, abs=0.02)]  # 10 rps: шаг 0,1 с


def test_retry_after_http_date() -> None:
    resp = httpx.Response(429, headers={"Retry-After": "Thu, 15 Oct 2026 12:00:10 GMT"})
    assert retry_after_seconds(resp, now=lambda: NOW) == 10
    assert retry_after_seconds(httpx.Response(429, headers={"Retry-After": "soon"})) is None


# --- фабрика -------------------------------------------------------------------------------------------------


def test_missing_api_key_means_provider_absent() -> None:
    cfg = LLMConfig.model_validate(base_cfg())
    assert set(build_clients(cfg, ENV)) == {"cloudru", "yandex"}
    assert set(build_clients(cfg, {"CLOUDRU_API_KEY": "k"})) == {"cloudru"}
    assert set(build_clients(cfg, {"YC_API_KEY": "k", "CLOUDRU_API_KEY": "  "})) == set()  # без YC_FOLDER_ID
    assert set(build_clients(cfg, {"YC_API_KEY": "k", "YC_FOLDER_ID": "f"})) == {"yandex"}


async def test_build_router_from_shipped_config(engine: AsyncEngine, mock: respx.MockRouter) -> None:
    path = ROOT / "config" / "llm.yaml"
    assert build_router(path, {}, engine) is None
    router = build_router(path, {"YC_API_KEY": "k", "YC_FOLDER_ID": "b1g"}, engine)
    assert router is not None and router.providers == ("yandex",)
    ya = mock.post("https://llm.api.cloud.yandex.net/v1/chat/completions").mock(return_value=completion(VALID))
    out = await router.complete_json("critic", SYSTEM, Verdict, has_portfolio_ctx=True, prompt_id="critic.v1")
    assert out is not None
    assert body(ya)["model"] == "gpt://b1g/aliceai-llm"
    assert body(ya)["temperature"] == 0
    # разметка вселенной без Cloud.ru — только правила
    assert await router.complete_json("tagger_universe", SYSTEM, Verdict, has_portfolio_ctx=False,
                                      prompt_id="t") is None
    assert ya.call_count == 1
    await router.aclose()


# --- конфиг --------------------------------------------------------------------------------------------------


def _mutate(path: str, value: Any) -> dict[str, Any]:
    cfg = copy.deepcopy(base_cfg())
    *parents, last = path.split(".")
    node = cfg
    for p in parents:
        node = node[p]
    if value is _DELETE:
        del node[last]
    else:
        node[last] = value
    return cfg


_DELETE = object()


@pytest.mark.parametrize(("path", "value", "match"), [
    ("roles.writer", ["cloudru:big-r", "gigachat:max"], "не объявлен"),
    ("roles.writer", ["cloudru:gpt-5"], "не объявлена"),
    ("roles.writer", ["cloudru-big-r"], "provider:model"),
    ("roles.writer", [], "непустой"),
    ("roles.writer", ["cloudru:big-r", "cloudru:big-r"], "повторяется"),
    ("primary_provider", "openrouter", "primary_provider"),
    ("budgets.yandex", _DELETE, "нет бюджета"),
    ("roles.qa", _DELETE, "qa_roles"),
    ("temperatures", {"nope": 0}, "temperatures"),
    ("providers.cloudru.models.small.data_in_ru", _DELETE, "data_in_ru"),
    ("providers.cloudru.models.small.price_in_rub_per_m", -1, "price_in_rub_per_m"),
    ("providers.cloudru.api_key_env", "sk-live-123", "api_key_env"),
    ("providers.yandex.model_template", "gpt://${YC_FOLDER_ID}/", "model_template"),
    ("providers.yandex.header_envs", {"Authorization": "YC_API_KEY"}, "header_envs"),
    ("providers.cloudru.unknown_field", 1, "unknown_field"),
])
def test_config_validation_errors(path: str, value: Any, match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        LLMConfig.model_validate(_mutate(path, value))


# --- G11: бюджеты конфигурации против оценки §9.7 -----------------------------------------------------------

WEEKS_PER_MONTH = Decimal("4.3")
REPORT_RETRIES = Decimal("1.3")  # +30% на ретраи текстов
MARGIN = Decimal("1.2")
PORTFOLIO_ROLES = ("tagger", "writer", "critic", "idea_explainer", "qa", "allocation_advisor")


@dataclass(frozen=True)
class Load:
    role: str
    calls: Decimal  # в месяц, или в неделю для weekly
    tokens_in: int
    tokens_out: int
    retries: Decimal = Decimal(1)
    weekly: bool = False


def section_9_7_loads(qa_quota: int) -> list[Load]:
    """Объёмы установившегося режима Ф4 (§9.7, §8.4); AssetAnalyst и дайджесты — «как Writer»."""
    return [
        Load("tagger", Decimal(4500), 1400, 150),  # портфель и watchlist
        Load("writer", Decimal(450), 1400, 150),  # эскалация 10% разметки на сильную модель
        Load("tagger_universe", Decimal(15000), 1400, 150),  # вселенная и топ-20 (верх диапазона 12–15 тыс.)
        Load("writer", Decimal(4), 20000, 2000, REPORT_RETRIES, weekly=True),  # недельный отчёт
        Load("critic", Decimal(2), 6000, 1000, REPORT_RETRIES, weekly=True),
        Load("idea_explainer", Decimal(5), 6000, 1000, REPORT_RETRIES, weekly=True),
        Load("qa", Decimal(qa_quota), 20000, 4500),
        Load("writer", Decimal(60), 8000, 1000),  # AssetAnalyst (shadow)
        Load("writer", Decimal(30), 5000, 1500),  # дайджесты
    ]


def g11_estimate(cfg: LLMConfig, provider: str) -> tuple[Decimal, Decimal]:
    """(месяц, недельный прогон) в ₽, если `provider` — основной: каждая роль на первой своей модели у него."""
    month = weekly_run = Decimal(0)
    for load in section_9_7_loads(cfg.budgets[provider].qa_monthly_quota):
        entry: ChainEntry | None = next((e for e in cfg.roles.get(load.role, ()) if e.provider == provider), None)
        if entry is None:
            continue  # роли нет у провайдера — только правила или шаблон
        per_call = cfg.model(entry).cost(load.tokens_in, cfg.output_cap(entry, load.tokens_out)) * load.retries
        if load.weekly:
            weekly_run += per_call * load.calls
            month += per_call * load.calls * WEEKS_PER_MONTH
        else:
            month += per_call * load.calls
    return month, weekly_run


@pytest.mark.parametrize("provider", ["cloudru", "yandex"])
def test_g11_shipped_budgets_cover_estimates(provider: str) -> None:
    cfg = load_llm_config(ROOT / "config" / "llm.yaml")
    month, weekly_run = g11_estimate(cfg, provider)
    budget = cfg.budgets[provider]
    assert month * MARGIN <= budget.monthly, f"{provider}: месяц {month:.0f} ₽ × 1,2 > {budget.monthly}"
    assert weekly_run * MARGIN <= budget.per_run, f"{provider}: прогон {weekly_run:.0f} ₽ × 1,2 > {budget.per_run}"
    # порядок величин сходится с таблицей §9.7 (≈ 1 400 ₽ Cloud.ru, ≈ 3 130 ₽ Yandex)
    low, high = {"cloudru": (1200, 1600), "yandex": (2700, 3300)}[provider]
    assert low <= month <= high


def test_g11_fails_when_qa_primary_is_deepseek_pro() -> None:
    cfg = load_llm_config(ROOT / "config" / "llm.yaml")
    raw = cfg.model_dump(mode="json")
    raw["roles"] = {role: [e.key for e in chain] for role, chain in cfg.roles.items()}
    raw["roles"]["qa"] = ["cloudru:deepseek-ai/DeepSeek-V4-Pro", "cloudru:ai-sage/GigaChat3.5-432B-A28B-Reasoning"]
    month, _ = g11_estimate(LLMConfig.model_validate(raw), "cloudru")
    assert month * MARGIN > cfg.budgets["cloudru"].monthly  # §9.7: ≈ 2 520 ₽ × 1,2 > 2 500


def test_shipped_config_roles_and_residency() -> None:
    cfg = load_llm_config(ROOT / "config" / "llm.yaml")
    assert set(cfg.roles) >= {"tagger", "tagger_universe", "writer", "critic", "idea_explainer", "qa",
                              "allocation_advisor"}
    for role in PORTFOLIO_ROLES:  # роли с портфельным контекстом — только модели с данными в РФ
        assert all(cfg.model(e).data_in_ru for e in cfg.roles[role]), role
    assert not cfg.providers["cloudru"].models["deepseek-ai/DeepSeek-V4-Flash"].data_in_ru
    assert cfg.breaker == BreakerSpec(errors=3, window_s=300, cooldown_s=600)
    assert cfg.degrade_at == Decimal("0.9")


def test_custom_provider_from_env_replaces_yaml() -> None:
    from investbot.adapters.llm.factory import build_clients, custom_config

    base = load_llm_config(ROOT / "config" / "llm.yaml")
    env = {"INVESTBOT_LLM_BASE_URL": "https://llm.example.com/v1", "INVESTBOT_LLM_API_KEY": "sk-test",
           "INVESTBOT_LLM_MODEL": "big-model", "INVESTBOT_LLM_MODEL_FAST": "small-model",
           "INVESTBOT_LLM_MONTHLY_RUB": "1500", "INVESTBOT_LLM_PROXY": "socks5://127.0.0.1:1080"}
    cfg = custom_config(env, base)
    assert cfg is not None and cfg.primary_provider == "custom" and list(cfg.providers) == ["custom"]
    assert {e.model for e in cfg.roles["writer"]} == {"big-model"} and cfg.roles["tagger"][0].model == "small-model"
    # по умолчанию провайдер считается зарубежным: портфельный контекст ему не отправляется
    assert not cfg.providers["custom"].models["big-model"].data_in_ru
    assert cfg.budgets["custom"].monthly == 1500
    clients = build_clients(cfg, env)
    assert list(clients) == ["custom"] and clients["custom"].host == "llm.example.com"
    allowed = custom_config({**env, "INVESTBOT_LLM_ALLOW_PORTFOLIO_ABROAD": "true"}, base)
    assert allowed is not None and allowed.providers["custom"].models["big-model"].data_in_ru
    assert custom_config({"INVESTBOT_LLM_BASE_URL": "https://x/v1"}, base) is None
