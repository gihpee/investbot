"""Бот отвечает только владельцу (ADR-20, deployment_mode=personal)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject, User

log = structlog.get_logger(__name__)

BOOTSTRAP_TEXT = (
    "Бот ещё не привязан к владельцу.\n"
    "Ваш Telegram ID: <code>{user_id}</code>\n\n"
    "Впишите на сервере в <code>.env</code> строку\n"
    "<code>INVESTBOT_OWNER_TELEGRAM_ID={user_id}</code>\n"
    "и перезапустите бота. После этого он будет отвечать только вам."
)


class OwnerOnlyMiddleware(BaseMiddleware):
    def __init__(self, owner_id: int | None) -> None:
        self.owner_id = owner_id

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user: User | None = data.get("event_from_user")
        if user is None:
            return None
        if self.owner_id is None:
            # режим первичной настройки: единственное, что делает бот, — сообщает ID собеседнику
            if isinstance(event, Message):
                await event.answer(BOOTSTRAP_TEXT.format(user_id=user.id))
            return None
        if user.id != self.owner_id:
            log.warning("telegram.foreign_user_ignored", user_id=user.id)
            return None
        return await handler(event, data)
