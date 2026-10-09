"""Новостной фон без LLM (§8.3, Ф2a): /news и блок в недельном отчёте.

Показываются только заголовок, время, источник и ссылка — тексты СМИ не пересылаются. Тона пока нет (Ф3).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime
from decimal import Decimal
from html import escape
from typing import Protocol
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import LinkPreviewOptions, Message

from investbot.news.models import Headline, TickerNewsBackground

MSK = ZoneInfo("Europe/Moscow")
BTN_NEWS = "📰 Новости"
SPIKE_Z = Decimal(2)
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


def headline_html(h: Headline) -> str:
    at = h.at.astimezone(MSK).strftime("%d.%m %H:%M")
    title = escape(h.title)
    link = f'<a href="{escape(h.url, quote=True)}">{title}</a>' if h.url else title
    more = f" · +{h.story_size - 1} ист." if h.story_size > 1 else ""
    return f"{at} {link} <i>({escape(h.source)}{more})</i>"


def _spike(b: TickerNewsBackground) -> str:
    if b.buzz_z is not None and b.buzz_z >= SPIKE_Z:
        return f" 🔥 всплеск внимания (z={str(b.buzz_z.quantize(Decimal('0.1'))).replace('.', ',')})"
    return ""


def _summary(b: TickerNewsBackground) -> str:
    return f"<b>{escape(b.ticker)}</b>: {b.stories_7d} сюж. за 7 дней, {b.stories_24h} за сутки{_spike(b)}"


def _cap(lines: list[str], limit: int) -> str:
    out: list[str] = []
    size = 0
    for i, line in enumerate(lines):
        if size + len(line) + 1 > limit:
            out.append(f"…и ещё {len(lines) - i} строк")
            break
        out.append(line)
        size += len(line) + 1
    return "\n".join(out)


def news_block(backgrounds: Sequence[TickerNewsBackground], top: int = 5, per_ticker: int = 2,
               limit: int = 1500) -> str | None:
    """Блок «Новостной фон» для недельного отчёта: бумаги с наибольшим числом сюжетов и всплесками."""
    active = [b for b in backgrounds if b.stories_7d > 0]
    if not active:
        return None
    # сначала всплески внимания (по z), затем бумаги с наибольшим числом сюжетов
    active.sort(key=lambda b: (-(b.buzz_z or 0) if (b.buzz_z or 0) >= SPIKE_Z else 0, -b.stories_7d, b.ticker))
    lines = ["<b>📰 Новостной фон по позициям</b> <i>(без оценки тона)</i>"]
    for b in active[:top]:
        lines.append(_summary(b))
        lines.extend(f"  • {headline_html(h)}" for h in b.headlines[:per_ticker])
    quiet = len(backgrounds) - len(active)
    if quiet:
        lines.append(f"<i>Без новостей за неделю: {quiet} бумаг.</i>")
    return _cap(lines, limit)


def news_message(backgrounds: Sequence[TickerNewsBackground], ticker: str | None = None, limit: int = 4000) -> str:
    if ticker is not None:
        b = next((x for x in backgrounds if x.ticker == ticker), None)
        if b is None or not b.headlines:
            return f"По {escape(ticker)} новостей за неделю в архиве нет."
        lines = [_summary(b), ""] + [f"• {headline_html(h)}" for h in b.headlines]
        return _cap(lines, limit)
    block = news_block(backgrounds, top=10, per_ticker=3, limit=limit)
    return block or "По бумагам портфеля новостей за неделю в архиве нет. Архив копится с момента запуска бота."


class NewsServiceLike(Protocol):
    async def background(self, tickers: Sequence[str], now: datetime | None = None) -> list[TickerNewsBackground]: ...


def build_news_router(service: NewsServiceLike, tickers: Callable[[], Awaitable[list[str]]]) -> Router:
    """tickers — тикеры портфеля (и watchlist), по которым строится фон."""
    router = Router(name="news")

    @router.message(Command("news"))
    @router.message(F.text == BTN_NEWS)
    async def news(message: Message, command: CommandObject | None = None) -> None:
        ticker = ((command.args or "").strip().upper() or None) if command is not None else None
        wanted = [ticker] if ticker else await tickers()
        if not wanted:
            await message.answer("В портфеле пока нет бумаг — новостной фон строить не по чему.")
            return
        backgrounds = await service.background(wanted)
        await message.answer(news_message(backgrounds, ticker), link_preview_options=NO_PREVIEW)

    return router
