"""Команды бота. Только чтение: никаких заявок, только рекомендации и состояние."""

from __future__ import annotations

from html import escape
from typing import Protocol

import structlog
from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup

from investbot.channels.telegram.render import portfolio_message
from investbot.services.portfolio import PortfolioService
from investbot.services.status import SystemStatus

log = structlog.get_logger(__name__)

BTN_PORTFOLIO = "📊 Портфель"
BTN_ALLOCATION = "🎯 Аллокация"
BTN_STATUS = "⚙️ Статус"
BTN_CALENDAR = "🗓 Календарь"
BTN_MANUAL = "✍️ Ручной портфель"  # обрабатывается роутером channels/telegram/edit.py
BTN_NEWS = "📰 Новости"  # обрабатывается роутером channels/telegram/news.py
BTN_IDEAS = "💡 Идеи"  # обрабатывается роутером channels/telegram/ideas.py

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=BTN_PORTFOLIO), KeyboardButton(text=BTN_IDEAS)],
              [KeyboardButton(text=BTN_ALLOCATION)],
              [KeyboardButton(text=BTN_CALENDAR), KeyboardButton(text=BTN_NEWS)],
              [KeyboardButton(text=BTN_MANUAL), KeyboardButton(text=BTN_STATUS)]],
    resize_keyboard=True,
)

WELCOME = (
    "Привет! Я личный аналитик вашего портфеля.\n\n"
    "• <b>Я не совершаю сделок</b>: токен Т-Инвестиций — только чтение, решения принимаете вы.\n"
    "• Показываю доли портфеля, отклонения от целевой аллокации и события по бумагам.\n"
    "• По умолчанию не пишу в Telegram рублёвые суммы и количества — только доли.\n\n"
    "Команды: /portfolio — портфель, /report — план ребалансировки, /invest 50000 — распределить пополнение, "
    "/calendar — купоны и дивиденды, /news — новостной фон, /allocation — конструктор аллокации, "
    "/edit — ручной портфель (другие брокеры, ЦФА), /status — состояние.\n"
    "Недельный отчёт приходит сам по субботам в 10:00 МСК."
)


class ServicesLike(Protocol):
    @property
    def portfolio(self) -> PortfolioService: ...

    async def status(self) -> SystemStatus: ...


def status_message(st: SystemStatus) -> str:
    broker = (f"подключены, счетов: {st.broker_accounts} (только чтение)" if st.broker_connected
              else "не подключены")
    lines = [
        "<b>⚙️ Состояние</b>",
        f"Режим: {st.deployment_mode} · приватность: {st.privacy_mode}",
        f"Т-Инвестиции: {broker}",
        f"Ручные счета: {st.manual_accounts}",
        f"IPS: {'версия ' + str(st.ips_version) if st.ips_version is not None else 'не утверждён'}",
    ]
    if st.problems:
        lines.append("")
        lines.append("<b>Проблемы</b>")
        lines.extend(f"• {escape(p)}" for p in st.problems)
    return "\n".join(lines)


def build_router(services: ServicesLike, privacy_mode: str) -> Router:
    router = Router(name="main")

    @router.message(CommandStart())
    async def start(message: Message) -> None:
        await message.answer(WELCOME, reply_markup=MAIN_KEYBOARD)

    @router.message(Command("portfolio"))
    @router.message(F.text == BTN_PORTFOLIO)
    async def portfolio(message: Message) -> None:
        try:
            ov = await services.portfolio.overview()
        except Exception as e:
            log.exception("portfolio.failed")
            await message.answer(f"Не удалось собрать портфель: {escape(type(e).__name__)}. Подробности — /status")
            return
        await message.answer(portfolio_message(ov, privacy_mode))

    @router.message(Command("status"))
    @router.message(F.text == BTN_STATUS)
    async def status(message: Message) -> None:
        await message.answer(status_message(await services.status()))

    return router
