"""Вопросы аналитику: /ask и свободный текст (§13.2). Подключается последним роутером — забирает только текст,
который не нужен мастерам и обсуждению аллокации."""

from __future__ import annotations

from typing import Protocol

from aiogram import F, Router
from aiogram.enums import ChatAction
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.types import Message

from investbot.channels.telegram.handlers import (
    BTN_ALLOCATION,
    BTN_CALENDAR,
    BTN_IDEAS,
    BTN_MANUAL,
    BTN_NEWS,
    BTN_PORTFOLIO,
    BTN_STATUS,
)
from investbot.services.qa_types import QaResult

MENU_TEXTS = {BTN_ALLOCATION, BTN_CALENDAR, BTN_IDEAS, BTN_MANUAL, BTN_NEWS, BTN_PORTFOLIO, BTN_STATUS}
MAX_QUESTION = 1000


class QaServiceLike(Protocol):
    @property
    def available(self) -> bool: ...

    async def ask(self, question: str, max_attempts: int = 2) -> QaResult: ...


def build_qa_router(service: QaServiceLike) -> Router:
    router = Router(name="qa")

    async def answer(message: Message, question: str) -> None:
        question = question.strip()[:MAX_QUESTION]
        if not question:
            await message.answer("Задайте вопрос: <code>/ask почему бот предлагает продать акции?</code>")
            return
        if not service.available:
            await message.answer("Вопросы аналитику работают с подключённой LLM — добавьте ключ провайдера "
                                 "(см. .env.example). Пока доступны команды: /portfolio, /report, /ideas, /calendar.")
            return
        if message.bot is not None:
            await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
        res = await service.ask(question)
        await message.answer(res.html if res.html else f"Не получилось ответить: {res.reason}. "
                                                       "Попробуйте /portfolio или /report.")

    @router.message(Command("ask"))
    async def ask(message: Message, command: CommandObject) -> None:
        await answer(message, command.args or "")

    @router.message(StateFilter(None), F.text, ~F.text.startswith("/"), ~F.text.in_(MENU_TEXTS))
    async def free_text(message: Message) -> None:
        await answer(message, message.text or "")

    return router
