"""Вопросы аналитику: инструменты → факты → ответ плейсхолдерами → валидатор; в боте — последний роутер."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any

from aiogram import Dispatcher
from pydantic import BaseModel

from investbot.channels.telegram.qa import build_qa_router
from investbot.services.portfolio import PortfolioService
from investbot.services.qa import QaAnswer, QaDeps, QaService

from .test_telegram_edit import Conversation, FakeBot
from .test_weekly_report import IPS_YAML, FakeBroker


class ScriptedRouter:
    """Подменяет LLMRouter.run_tools: вызывает инструменты по сценарию и отдаёт заранее заданные ответы."""

    def __init__(self, tool_calls: list[tuple[str, dict[str, Any]]], answers: list[str | None]) -> None:
        self.tool_calls = tool_calls
        self.answers = answers
        self.seen: list[dict[str, Any]] = []
        self.messages: list[Sequence[Any]] = []

    async def run_tools(self, role: str, messages: Sequence[Any], tools: Sequence[Any],
                        handler: Callable[[str, dict[str, Any]], Awaitable[str]], *, final_tool: str,
                        output: type[BaseModel], has_portfolio_ctx: bool, prompt_id: str, **_: Any) -> Any:
        assert role == "qa" and final_tool == "submit_answer" and has_portfolio_ctx
        assert {t.name for t in tools} >= {"get_portfolio_view", "get_position", "get_last_plan"}
        self.messages.append(messages)
        for name, args in self.tool_calls:
            self.seen.append(json.loads(await handler(name, args)))
        text = self.answers.pop(0)
        return None if text is None else output.model_validate({"text": text, "used_fact_ids": []})


def make(tmp_path: Path, router: Any) -> QaService:
    (tmp_path / "ips.yaml").write_text(IPS_YAML, encoding="utf-8")
    portfolio = PortfolioService(FakeBroker(), tmp_path / "manual.yaml", tmp_path / "ips.yaml")
    return QaService(router, QaDeps(overview=portfolio.overview))


async def test_answer_rendered_from_tool_facts(tmp_path: Path) -> None:
    router = ScriptedRouter([("get_portfolio_view", {}), ("get_position", {"ticker": "aaaa"})],
                            ["Доля {{i:b:equity_ru}} — {{f:pf:w:equity_ru}} при цели {{f:pf:target:equity_ru}}; "
                             "{{i:I:AAAA}} занимает {{f:pos:AAAA:w}} портфеля."])
    res = await make(tmp_path, router).ask("сколько у меня акций?")
    assert res.html is not None and "58,0%" in res.html and "40,0%" in res.html and "Акции РФ" in res.html
    facts = router.seen[0]["fact_values"]
    assert "pf:w:equity_ru" in facts and not any(k.endswith(":rub") for k in facts)  # приватность: без рублей
    assert "<untrusted_document" in router.messages[0][1]["content"]


async def test_raw_numbers_are_rejected_then_rewritten(tmp_path: Path) -> None:
    router = ScriptedRouter([("get_portfolio_view", {})],
                            ["Акций у вас 58%, это много.", "Акции: {{f:pf:w:equity_ru}}."])
    res = await make(tmp_path, router).ask("сколько акций?")
    assert res.html is not None and res.attempts == 2
    retry = router.messages[1][-1]["content"]
    assert "отклонён" in retry


async def test_gives_up_without_verified_numbers_or_llm(tmp_path: Path) -> None:
    router = ScriptedRouter([], ["Гарантированно вырастет на 10%", "Всё ещё 10%"])
    res = await make(tmp_path, router).ask("что будет?")
    assert res.html is None and "проверенными" in res.reason
    off = await make(tmp_path, None).ask("что будет?")
    assert off.html is None and "LLM не подключена" in off.reason


async def test_bot_routes_free_text_to_qa_but_not_menu_or_commands(tmp_path: Path) -> None:
    router = ScriptedRouter([("get_portfolio_view", {})], ["Акции: {{f:pf:w:equity_ru}}."])
    c = Conversation.__new__(Conversation)
    c.bot, c.dp, c.n, c.transcript = FakeBot(), Dispatcher(), 0, []
    c.dp.include_router(build_qa_router(make(tmp_path, router)))
    assert "58,0%" in await c.say("сколько у меня акций?")
    assert await c.say("📊 Портфель") == "" and await c.say("/unknown") == ""
    assert "Задайте вопрос" in await c.say("/ask")


def test_answer_schema_limits() -> None:
    assert QaAnswer.model_json_schema()["properties"]["text"]["maxLength"] == 1500
