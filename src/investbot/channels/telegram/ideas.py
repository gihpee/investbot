"""Лента идей для входа (§12, §13.2 /ideas): v1 — облигации, детерминированная метрика.

Идеи не пушатся (§13.7): лента открывается командой, в недельном отчёте — короткий блок «Идеи недели».
Обязательный блок «Против» и условия отмены — в каждой карточке.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from html import escape
from typing import Protocol

import structlog
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from investbot.channels.telegram.render import DISCLAIMER, title
from investbot.core.money import pct
from investbot.ideas.models import EntryIdea

log = structlog.get_logger(__name__)
BTN_IDEAS = "💡 Идеи"


class IdeasResultLike(Protocol):
    @property
    def ideas(self) -> Sequence[EntryIdea]: ...

    @property
    def warnings(self) -> Sequence[str]: ...


class IdeasServiceLike(Protocol):
    async def latest(self, as_of: date) -> IdeasResultLike: ...


def _num(x: Decimal, places: int = 2) -> str:
    return f"{x.quantize(Decimal(1).scaleb(-places))}".replace(".", ",")


def _price(idea: EntryIdea, x: Decimal) -> str:
    return f"{_num(x)}%" if idea.price_unit == "pct_nominal" else f"{_num(x)} ₽"


def idea_line(i: int, idea: EntryIdea) -> str:
    m = idea.metrics
    ytm = f"YTM {pct(m['ytm'])} · " if "ytm" in m else ""
    dur = f"D {_num(m['d_mod'], 1)} г. · " if "d_mod" in m else ""
    star = " ⭐ в недовес" if idea.for_underweight_bucket else ""
    return (f"{i}. <b>{escape(idea.name)}</b> ({escape(idea.ticker)}) · {escape(title(idea.bucket))}{star}\n"
            f"   {ytm}{dur}оценка {pct(idea.score, 2)} за {idea.horizon_weeks} нед. · "
            f"вход {_price(idea, idea.entry_low)}–{_price(idea, idea.entry_high)} до {idea.valid_until:%d.%m}")


def ideas_block(ideas: Sequence[EntryIdea], top: int = 3) -> str | None:
    """Короткий блок для недельного отчёта."""
    if not ideas:
        return None
    lines = ["<b>💡 Идеи недели</b> <i>(подробно — /ideas)</i>"]
    lines.extend(idea_line(i, x) for i, x in enumerate(ideas[:top], 1))
    lines.append(f"<i>{escape(ideas[0].note)}.</i>")
    return "\n".join(lines)


def ideas_message(ideas: Sequence[EntryIdea], warnings: Sequence[str] = ()) -> tuple[str, InlineKeyboardMarkup | None]:
    if not ideas:
        text = ("Подходящих идей сейчас нет: ни одна облигация не прошла фильтры (рейтинг, ликвидность, "
                "события, издержки).")
        if warnings:
            text += "\n" + "\n".join(f"• {escape(w)}" for w in warnings)
        return text, None
    lines = ["<b>💡 Идеи для входа</b> · облигации", f"<i>{escape(ideas[0].note)}.</i>", ""]
    lines.extend(idea_line(i, x) for i, x in enumerate(ideas, 1))
    if warnings:
        lines.append("")
        lines.extend(f"• {escape(w)}" for w in warnings)
    lines.append("")
    lines.append("Оценка — «carry + roll-down − ожидаемые кредитные потери − издержки» за горизонт, не прогноз. "
                 "Покупку совершаете вы в приложении брокера.")
    lines.append(DISCLAIMER)
    rows = [[InlineKeyboardButton(text=f"{i}. Подробнее", callback_data=f"id:card:{i - 1}")]
            for i in range(1, len(ideas) + 1)]
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


def idea_card(idea: EntryIdea) -> str:
    m = idea.metrics
    isin = f" · ISIN {escape(idea.isin)}" if idea.isin else ""
    lines = [f"<b>{escape(idea.name)}</b> ({escape(idea.ticker)}){isin}"]
    lines.append(f"Класс: {escape(title(idea.bucket))} · бенчмарк {escape(idea.benchmark)} · "
                 f"горизонт {idea.horizon_weeks} нед.")
    lines.append(f"Цена сейчас {_price(idea, idea.price)} · зона входа {_price(idea, idea.entry_low)}–"
                 f"{_price(idea, idea.entry_high)} до {idea.valid_until:%d.%m.%Y}")
    if idea.target_yield is not None:
        lines.append(f"Целевая доходность входа: {pct(idea.target_yield, 2)}")
    metric_names = {"ytm": "доходность к погашению/оферте", "d_mod": "модиф. дюрация, лет", "g_spread_bp":
                    "спред к ОФЗ, б.п.", "carry": "carry за горизонт", "roll": "roll-down за горизонт",
                    "el": "ожидаемые кредитные потери", "breakeven_bp": "запас по росту доходности, б.п."}
    shown = [(k, v) for k, v in m.items() if k in metric_names]
    if shown:
        lines.append("")
        lines.append("<b>Метрики</b>")
        for k, v in shown:
            val = _num(v, 1) if k in ("d_mod", "g_spread_bp", "breakeven_bp") else pct(v, 2)
            lines.append(f"• {metric_names[k]}: {val}")
    lines.append("")
    lines.append(f"<b>Оценка:</b> {pct(idea.score, 2)} за горизонт сверх денежного рынка, после издержек")
    if idea.reasons:
        lines.append("<b>За</b>")
        lines.extend(f"• {escape(r)}" for r in idea.reasons)
    lines.append("<b>Против</b>")
    lines.extend(f"• {escape(r)}" for r in idea.risks)
    lines.append("<b>Идея отменяется, если</b>")
    lines.extend(f"• {escape(r)}" for r in idea.invalidation)
    lines.append("")
    lines.append(f"<i>{escape(idea.note)}.</i>")
    lines.append(DISCLAIMER)
    return "\n".join(lines)


def build_ideas_router(service: IdeasServiceLike, today: date | None = None) -> Router:
    router = Router(name="ideas")
    cache: dict[str, Sequence[EntryIdea]] = {}

    @router.message(Command("ideas"))
    @router.message(F.text == BTN_IDEAS)
    async def ideas(message: Message) -> None:
        try:
            result = await service.latest(today or date.today())
        except Exception as e:
            log.exception("ideas.failed")
            await message.answer(f"Не удалось построить идеи: {escape(type(e).__name__)}. Подробности — /status")
            return
        cache["last"] = list(result.ideas)
        text, markup = ideas_message(result.ideas, result.warnings)
        await message.answer(text, reply_markup=markup)

    @router.callback_query(F.data.startswith("id:card:"))
    async def card(cb: CallbackQuery) -> None:
        idx = int((cb.data or "id:card:0").rsplit(":", 1)[1])
        items = cache.get("last", [])
        if idx >= len(items) or not isinstance(cb.message, Message):
            await cb.answer("Лента устарела — откройте /ideas заново", show_alert=True)
            return
        await cb.answer()
        await cb.message.answer(idea_card(items[idx]))

    return router
