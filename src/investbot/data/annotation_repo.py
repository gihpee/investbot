"""Репозиторий LLM-разметки новостей (news_annotation, §8.2 п. 6–7): очередь на разметку, запись, выборка для тона."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Collection, Iterable
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, NamedTuple

from sqlalchemy import and_, insert, select
from sqlalchemy.ext.asyncio import AsyncEngine

from investbot.data.db import news_annotation, news_item, news_link, write_tx
from investbot.data.news_repo import _utc
from investbot.news.tone import ToneItem

if TYPE_CHECKING:
    from investbot.llm_roles.tagger import NewsAnnotationOut

_CHUNK = 500


class PendingNews(NamedTuple):
    news_id: int
    title: str
    lead: str
    source: str
    candidates: list[tuple[str, str | None]]  # все бумаги, связанные с новостью; название — None (его знает сервис)


class AnnotationRepo:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def pending(self, tickers: Iterable[str], since: datetime, limit: int = 50, *,
                      sources: Collection[str] | None = None) -> list[PendingNews]:
        """Новости с `first_seen_at ≥ since`, связанные хотя бы с одной из `tickers` и ещё не размеченные для неё;
        новые сверху. `sources` — только из этих источников (тексты, которые можно отдавать в LLM); None — все.

        Кандидаты — все связи новости, не только запрошенные бумаги: разметка пишется сразу для всех, а по списку
        кандидатов в промпте не видно, какие бумаги интересуют пользователя (G8)."""
        wanted = sorted(set(tickers))
        if not wanted or limit <= 0 or (sources is not None and not sources):
            return []
        na = news_annotation
        unannotated = (
            select(news_link.c.news_id)
            .outerjoin(na, and_(na.c.news_id == news_link.c.news_id, na.c.ticker == news_link.c.ticker))
            .where(news_link.c.ticker.in_(wanted), na.c.news_id.is_(None))
            .distinct()
        )
        conds: list[Any] = [news_item.c.id.in_(unannotated), news_item.c.first_seen_at >= since]
        if sources is not None:
            conds.append(news_item.c.source.in_(sorted(set(sources))))
        async with self._engine.connect() as conn:
            rows = (await conn.execute(
                select(news_item.c.id, news_item.c.title, news_item.c.lead, news_item.c.source)
                .where(and_(*conds))
                .order_by(news_item.c.first_seen_at.desc(), news_item.c.id.desc())
                .limit(limit))).all()
            ids = [int(r[0]) for r in rows]
            links: dict[int, list[str]] = defaultdict(list)
            for i in range(0, len(ids), _CHUNK):
                for nid, ticker in await conn.execute(
                        select(news_link.c.news_id, news_link.c.ticker)
                        .where(news_link.c.news_id.in_(ids[i:i + _CHUNK]))
                        .order_by(news_link.c.news_id, news_link.c.confidence.desc(), news_link.c.ticker)):
                    links[int(nid)].append(str(ticker))
        return [PendingNews(news_id=int(r[0]), title=str(r[1]), lead=str(r[2] or ""), source=str(r[3]),
                            candidates=[(t, None) for t in links[int(r[0])]]) for r in rows]

    async def save(self, news_id: int, out: NewsAnnotationOut, model: str, prompt_id: str, *,
                   at: datetime | None = None) -> int:
        """Одна строка на сущность разметки. Связанные с новостью бумаги, которых нет в `entities`, получают строку
        с relevance 0 и тоном 0 (событие `other`), чтобы не уходить на повторную разметку. Уже размеченные пары
        (news_id, ticker) не перезаписываются — повторный вызов ничего не меняет. Возвращает число новых строк."""
        at = at or datetime.now(UTC)
        zero = Decimal(0)
        rows: dict[str, tuple[Decimal, Decimal, str]] = {
            e.ticker: (e.tone, e.relevance, out.event_type) for e in out.entities
        }
        na = news_annotation
        async with write_tx(self._engine) as conn:
            linked = [str(r[0]) for r in await conn.execute(
                select(news_link.c.ticker).where(news_link.c.news_id == news_id))]
            for t in linked:
                rows.setdefault(t, (zero, zero, "other"))
            existing = {str(r[0]) for r in await conn.execute(select(na.c.ticker).where(na.c.news_id == news_id))}
            values = [
                {"news_id": news_id, "ticker": t, "event_type": ev, "tone": tone, "relevance": rel,
                 "confidence": out.confidence, "is_ad": out.is_ad, "is_rumor": out.is_rumor, "summary": out.summary,
                 "model": model, "prompt_id": prompt_id, "annotated_at": at}
                for t, (tone, rel, ev) in rows.items() if t not in existing
            ]
            if values:
                await conn.execute(insert(na), values)
        return len(values)

    async def tone_items(self, tickers: Iterable[str], since: datetime) -> dict[str, list[ToneItem]]:
        """Разметка по бумагам для новостей с `first_seen_at ≥ since`, новые сверху."""
        wanted = sorted(set(tickers))
        if not wanted:
            return {}
        na = news_annotation
        out: dict[str, list[ToneItem]] = defaultdict(list)
        async with self._engine.connect() as conn:
            rows = (await conn.execute(
                select(na.c.news_id, na.c.ticker, na.c.tone, na.c.relevance, na.c.confidence, na.c.event_type,
                       na.c.is_ad, na.c.is_rumor, na.c.summary, news_item.c.story_id, news_item.c.source,
                       news_item.c.published_at, news_item.c.first_seen_at)
                .join(news_item, news_item.c.id == na.c.news_id)
                .where(and_(na.c.ticker.in_(wanted), news_item.c.first_seen_at >= since))
                .order_by(news_item.c.first_seen_at.desc(), news_item.c.id.desc(), na.c.ticker))).mappings().all()
        for r in rows:
            seen = _utc(r["first_seen_at"]) or since
            out[str(r["ticker"])].append(ToneItem(
                news_id=int(r["news_id"]), story_id=int(r["story_id"]), at=_utc(r["published_at"]) or seen,
                source=str(r["source"]), tone=r["tone"], relevance=r["relevance"], confidence=r["confidence"],
                event_type=str(r["event_type"]), is_ad=bool(r["is_ad"]), is_rumor=bool(r["is_rumor"]),
                summary=str(r["summary"]),
            ))
        return dict(out)
