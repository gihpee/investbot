"""Доменные модели новостного конвейера (§8.1–8.3, этап Ф1/Ф2a: архив и детерминированный фон без LLM).

Пользователю показываются только заголовок, время и ссылка (тексты СМИ не пересылаются, §1.3).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import Field

from investbot.core.models import Strict

LinkMethod = Literal["api", "alias_ticker", "alias_isin", "alias_name"]


class RawNewsItem(Strict):
    """Элемент, как его отдал коллектор, после нормализации текста и URL."""

    source: str  # ключ источника из config/sources.yaml: "interfax", "tinvest_news", "moex_sitenews", …
    external_id: str  # guid/id источника; если нет — канонический URL
    url: str | None
    title: str
    lead: str = ""  # анонс без HTML, ≤ 1000 символов — для дедупликации и связывания, пользователю не показывается
    published_at: datetime | None  # tz-aware UTC; None — источник не дал дату
    fetched_at: datetime  # tz-aware UTC, момент получения коллектором
    instrument_tickers: tuple[str, ...] = ()  # связи от источника (T-Invest News: instrument_id[]) — метод "api"
    priority: int | None = None
    categories: tuple[str, ...] = ()


class NewsLink(Strict):
    ticker: str
    method: LinkMethod
    confidence: Decimal = Field(ge=0, le=1)


class StoredNews(Strict):
    """Запись архива. `first_seen_at` — момент, когда мы впервые увидели сюжет (PIT-дисциплина)."""

    id: int
    source: str
    external_id: str
    url: str | None
    title: str
    published_at: datetime | None
    first_seen_at: datetime
    sha1: str
    story_id: int  # id первой новости сюжета (дубликаты из разных источников — один сюжет)
    links: tuple[NewsLink, ...] = ()


class SourceHealth(Strict):
    source: str
    last_ok_at: datetime | None = None
    last_error_at: datetime | None = None
    last_error: str | None = None
    consecutive_empty: int = 0
    consecutive_errors: int = 0
    last_new_items: int = 0
    overrun: bool = False  # «пробитие»: все элементы ответа новые — могли пропустить часть ленты


class Headline(Strict):
    title: str
    url: str | None
    source: str
    at: datetime  # published_at или first_seen_at
    story_size: int  # сколько источников написали об этом сюжете


class TickerNewsBackground(Strict):
    """Детерминированный новостной фон по бумаге за период: без тона и без LLM (§8.3, Ф2a)."""

    ticker: str
    stories_7d: int
    stories_24h: int
    buzz_z: Decimal | None  # z-оценка числа сюжетов за 24 ч к своей истории; None — история < 20 наблюдений
    headlines: tuple[Headline, ...]
