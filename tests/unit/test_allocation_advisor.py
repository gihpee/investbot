"""AllocationAdvisor: свободный текст → правки черновика; в боте — только при открытом черновике."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

from aiogram import Dispatcher
from pydantic import BaseModel

from investbot.channels.telegram.allocation import build_allocation_router
from investbot.llm_roles.allocation_advisor import AllocationAdvisor
from investbot.services.questionnaire import ProfileAnswers

from .test_allocation_service import ANSWERS, TODAY, service
from .test_telegram_edit import Conversation, FakeBot

D = Decimal
TITLES = {"equity_ru": "Акции РФ", "ofz_fixed": "ОФЗ"}


class FakeLLM:
    def __init__(self, outputs: list[dict[str, Any] | None]) -> None:
        self.outputs = outputs
        self.calls: list[dict[str, Any]] = []

    async def complete_json(self, role: str, messages: list[dict[str, str]], output: type[BaseModel], *,
                            has_portfolio_ctx: bool, prompt_id: str, max_tokens: int = 2000) -> Any:
        self.calls.append({"role": role, "messages": messages, "ctx": has_portfolio_ctx})
        out = self.outputs.pop(0)
        return None if out is None else output.model_validate(out)


async def test_interpret_filters_and_strips_digits() -> None:
    llm = FakeLLM([{"edits": [{"op": "shift_bucket", "bucket": "equity_ru", "value": "-0.05"},
                              {"op": "set_weight", "bucket": "crypto", "value": "0.1"},
                              {"op": "whatif"}],
                    "intent": "уменьшить акции на 5 п.п."}])
    res = await AllocationAdvisor(llm).interpret("поменьше акций", {"equity_ru": D("0.4"), "ofz_fixed": D("0.6")},
                                                 TITLES, ["equity_ru", "ofz_fixed"])
    assert res is not None and [e.op for e in res.edits] == ["shift_bucket"] and res.edits[0].value == D("-0.05")
    assert res.whatif and res.intent == "" and any("crypto" in r for r in res.rejected)
    call = llm.calls[0]
    assert call["role"] == "allocation_advisor" and call["ctx"] is True
    assert "<untrusted_document" in call["messages"][1]["content"]


async def test_interpret_returns_none_when_llm_unavailable() -> None:
    assert await AllocationAdvisor(FakeLLM([None])).interpret("x", {}, TITLES, []) is None


async def test_bot_discussion_applies_edits_only_with_open_draft(tmp_path: Path) -> None:
    svc = service(tmp_path)
    await svc.save_answers(ProfileAnswers.from_codes(ANSWERS))
    llm = FakeLLM([
        {"edits": [{"op": "less_risk", "value": "0.05"}], "intent": "снизить риск черновика"},
        {"edits": [], "intent": "", "clarify": "какой класс уменьшить?"},
    ])
    c = Conversation.__new__(Conversation)
    c.bot, c.dp, c.n, c.transcript = FakeBot(), Dispatcher(), 0, []
    c.dp.include_router(build_allocation_router(svc, today=TODAY, advisor=AllocationAdvisor(llm)))

    assert await c.say("поменьше риска") == ""  # черновика нет — текст не перехватывается
    assert "Варианты аллокации" in await c.say("/allocation")
    assert "Черновик" in await c.press("al:v:C")
    reply = await c.say("хочу поменьше риска")
    assert "Понял так: снизить риск черновика" in reply and "п.п." in reply
    assert "какой класс уменьшить" in await c.say("переложи куда-нибудь")
    assert await c.say("💡 Идеи") == ""  # кнопки меню не перехватываются
    assert len(llm.calls) == 2
