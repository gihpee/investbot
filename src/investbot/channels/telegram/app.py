"""Запуск бота: long polling (вебхук не нужен — бот за NAT/туннелем)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.types import BotCommand, BotCommandScopeChat

from investbot.channels.telegram.handlers import ServicesLike, build_router
from investbot.channels.telegram.middlewares import OwnerOnlyMiddleware

COMMANDS = [
    BotCommand(command="portfolio", description="Портфель и отклонения от целей"),
    BotCommand(command="report", description="План ребалансировки сейчас"),
    BotCommand(command="invest", description="Распределить пополнение: /invest 50000"),
    BotCommand(command="calendar", description="Купоны, оферты, погашения, дивиденды"),
    BotCommand(command="news", description="Новостной фон по позициям: /news или /news SBER"),
    BotCommand(command="allocation", description="Конструктор аллокации"),
    BotCommand(command="status", description="Состояние системы"),
]

Startup = Callable[[Bot, Dispatcher], Awaitable[None]]


def build_dispatcher(services: ServicesLike, owner_id: int | None, privacy_mode: str,
                     extra_routers: tuple[Router, ...] = ()) -> Dispatcher:
    dp = Dispatcher()
    guard = OwnerOnlyMiddleware(owner_id)
    dp.message.outer_middleware(guard)
    dp.callback_query.outer_middleware(guard)
    dp.include_router(build_router(services, privacy_mode))
    for r in extra_routers:
        dp.include_router(r)
    return dp


async def run_bot(token: str, services: ServicesLike, owner_id: int | None, privacy_mode: str,
                  proxy: str | None = None, extra_routers: tuple[Router, ...] = (),
                  on_startup: Startup | None = None) -> None:
    session = AiohttpSession(proxy=proxy) if proxy else None
    bot = Bot(token, session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML,
                                                                   protect_content=True))
    dp = build_dispatcher(services, owner_id, privacy_mode, extra_routers)
    if owner_id is not None:
        # команды видны только в чате владельца
        await bot.set_my_commands(COMMANDS, scope=BotCommandScopeChat(chat_id=owner_id))
    if on_startup is not None:
        await on_startup(bot, dp)
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()
