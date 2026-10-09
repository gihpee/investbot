"""Репозиторий архива новостей, здоровья источников и курсоров."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import and_, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from investbot.data.db import kv_state, news_item, news_link, source_health
from investbot.news.models import NewsLink, RawNewsItem, SourceHealth, StoredNews


def _utc(dt: datetime | None) -> datetime | None:
    """SQLite отдаёт naive datetime: считаем его UTC (пишем всегда в UTC)."""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


class NewsRepo:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def known_ids(self, source: str, external_ids: Iterable[str]) -> set[str]:
        ids = list(set(external_ids))
        if not ids:
            return set()
        found: set[str] = set()
        async with self._engine.connect() as conn:
            for i in range(0, len(ids), 500):
                rows = await conn.execute(select(news_item.c.external_id).where(
                    and_(news_item.c.source == source, news_item.c.external_id.in_(ids[i:i + 500]))))
                found.update(r[0] for r in rows)
        return found

    async def recent_for_index(self, since: datetime) -> list[tuple[int, int, str, str]]:
        """(id, story_id, title, lead) за окно дедупликации — для StoryIndex."""
        async with self._engine.connect() as conn:
            rows = await conn.execute(
                select(news_item.c.id, news_item.c.story_id, news_item.c.title, news_item.c.lead)
                .where(news_item.c.first_seen_at >= since).order_by(news_item.c.id))
            return [(int(r[0]), int(r[1]), str(r[2]), str(r[3])) for r in rows]

    async def insert(self, item: RawNewsItem, sha1: str, story_id: int | None, first_seen_at: datetime,
                     links: Sequence[NewsLink]) -> int:
        async with self._engine.begin() as conn:
            res = await conn.execute(insert(news_item).values(
                source=item.source, external_id=item.external_id, url=item.url, title=item.title, lead=item.lead,
                published_at=item.published_at, first_seen_at=first_seen_at, sha1=sha1, story_id=story_id or 0,
                priority=item.priority,
            ))
            pk = res.inserted_primary_key
            if pk is None:
                raise RuntimeError("не удалось получить id новости")
            news_id = int(pk[0])
            if story_id is None:  # первая новость сюжета: сюжет = её id
                await conn.execute(update(news_item).where(news_item.c.id == news_id).values(story_id=news_id))
            if links:
                await conn.execute(insert(news_link), [
                    {"news_id": news_id, "ticker": lk.ticker, "method": lk.method, "confidence": lk.confidence}
                    for lk in links
                ])
        return news_id

    async def items_for_tickers(self, tickers: Iterable[str], since: datetime) -> dict[str, list[StoredNews]]:
        wanted = sorted(set(tickers))
        out: dict[str, list[StoredNews]] = defaultdict(list)
        if not wanted:
            return out
        async with self._engine.connect() as conn:
            rows = (await conn.execute(
                select(news_item, news_link.c.ticker, news_link.c.method, news_link.c.confidence)
                .join(news_link, news_link.c.news_id == news_item.c.id)
                .where(and_(news_link.c.ticker.in_(wanted), news_item.c.first_seen_at >= since))
                .order_by(news_item.c.first_seen_at.desc(), news_item.c.id.desc()))).mappings().all()
        for r in rows:
            out[r["ticker"]].append(StoredNews(
                id=int(r["id"]), source=r["source"], external_id=r["external_id"], url=r["url"], title=r["title"],
                published_at=_utc(r["published_at"]), first_seen_at=_utc(r["first_seen_at"]) or since,
                sha1=r["sha1"], story_id=int(r["story_id"]),
                links=(NewsLink(ticker=r["ticker"], method=r["method"], confidence=Decimal(str(r["confidence"]))),),
            ))
        return out

    async def archive_start(self) -> datetime | None:
        """Момент первой записи архива — начало истории для buzz_z (а не первое упоминание бумаги)."""
        async with self._engine.connect() as conn:
            return _utc((await conn.execute(select(func.min(news_item.c.first_seen_at)))).scalar_one_or_none())

    async def count_since(self, since: datetime) -> int:
        async with self._engine.connect() as conn:
            return int((await conn.execute(
                select(func.count()).select_from(news_item).where(news_item.c.first_seen_at >= since))).scalar_one())

    async def get_health(self, source: str) -> SourceHealth:
        async with self._engine.connect() as conn:
            r = (await conn.execute(select(source_health).where(source_health.c.source == source))).mappings().first()
        if r is None:
            return SourceHealth(source=source)
        return SourceHealth(
            source=source, last_ok_at=_utc(r["last_ok_at"]), last_error_at=_utc(r["last_error_at"]),
            last_error=r["last_error"], consecutive_empty=r["consecutive_empty"],
            consecutive_errors=r["consecutive_errors"], last_new_items=r["last_new_items"], overrun=bool(r["overrun"]),
        )

    async def all_health(self) -> list[SourceHealth]:
        async with self._engine.connect() as conn:
            keys = [r[0] for r in await conn.execute(select(source_health.c.source).order_by(source_health.c.source))]
        return [await self.get_health(k) for k in keys]

    async def save_health(self, h: SourceHealth) -> None:
        values = {
            "last_ok_at": h.last_ok_at, "last_error_at": h.last_error_at, "last_error": h.last_error,
            "consecutive_empty": h.consecutive_empty, "consecutive_errors": h.consecutive_errors,
            "last_new_items": h.last_new_items, "overrun": int(h.overrun),
        }
        async with self._engine.begin() as conn:
            res = await conn.execute(update(source_health).where(source_health.c.source == h.source).values(**values))
            if res.rowcount == 0:
                await conn.execute(insert(source_health).values(source=h.source, **values))

    async def get_state(self, key: str) -> str | None:
        async with self._engine.connect() as conn:
            r = (await conn.execute(select(kv_state.c.value).where(kv_state.c.key == key))).first()
        return None if r is None else str(r[0])

    async def set_state(self, key: str, value: str) -> None:
        now = datetime.now(UTC)
        async with self._engine.begin() as conn:
            res = await conn.execute(update(kv_state).where(kv_state.c.key == key).values(value=value, updated_at=now))
            if res.rowcount == 0:
                await conn.execute(insert(kv_state).values(key=key, value=value, updated_at=now))
