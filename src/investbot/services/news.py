"""Сервис новостей (§8.1–8.3, этап Ф1/Ф2a): сбор → дедупликация → связывание → архив → новостной фон без LLM."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable, Sequence
from datetime import UTC, datetime, timedelta

import structlog

from investbot.adapters.rss.collector import CollectorError, FetchResult, RssCollector, update_health
from investbot.data.news_repo import NewsRepo
from investbot.news.background import build_background, msk_date
from investbot.news.dedup import StoryIndex, content_sha1
from investbot.news.linking import AliasIndex
from investbot.news.models import RawNewsItem, SourceHealth, TickerNewsBackground
from investbot.news.sources import SourceConfig

log = structlog.get_logger(__name__)

DEDUP_WINDOW = timedelta(days=7)
HISTORY_DAYS = 120
BROKEN_AFTER_EMPTY = 6  # подряд пустых ответов → источник считается сломанным (баннер в /status)
POLL_CONCURRENCY = 6
SOURCE_TIMEOUT_SEC = 45  # потолок на источник с учётом повторов: зависшая лента не задерживает остальные

TInvestNews = Callable[[str | None, int], Awaitable[tuple[list[RawNewsItem], str | None]]]
AliasProvider = Callable[[], Awaitable[AliasIndex]]


def utcnow() -> datetime:
    return datetime.now(UTC)


def is_due(src: SourceConfig, h: SourceHealth, now: datetime) -> bool:
    """Источник пора опрашивать: прошло poll_sec с последней попытки (при «пробитии» — вдвое чаще)."""
    last = max((t for t in (h.last_ok_at, h.last_error_at) if t is not None), default=None)
    if last is None:
        return True
    period = src.poll_sec / 2 if h.overrun else src.poll_sec
    return (now - last).total_seconds() >= period


class NewsService:
    def __init__(self, repo: NewsRepo, sources: Sequence[SourceConfig], rss: RssCollector | None,
                 tinvest_news: TInvestNews | None, aliases: AliasProvider,
                 now: Callable[[], datetime] = utcnow) -> None:
        self._repo = repo
        self._sources = [s for s in sources if s.enabled]
        self._rss = rss
        self._tinvest = tinvest_news
        self._aliases = aliases
        self._now = now

    async def _index(self, now: datetime) -> StoryIndex:
        index = StoryIndex()
        for news_id, story_id, title, lead in await self._repo.recent_for_index(now - DEDUP_WINDOW):
            index.add(news_id, story_id, title, lead)
        return index

    async def ingest(self, items: Sequence[RawNewsItem], aliases: AliasIndex, index: StoryIndex,
                     now: datetime) -> int:
        """Сохраняет новые элементы. Возвращает число вставленных."""
        if not items:
            return 0
        by_source: dict[str, list[RawNewsItem]] = {}
        for it in items:
            by_source.setdefault(it.source, []).append(it)
        inserted = 0
        for source, group in by_source.items():
            known = await self._repo.known_ids(source, (i.external_id for i in group))
            seen: set[str] = set()
            for it in sorted(group, key=lambda i: (i.published_at or now, i.external_id)):
                if it.external_id in known or it.external_id in seen:
                    continue
                seen.add(it.external_id)
                story = index.find_story(it.title, it.lead)
                news_id = await self._repo.insert(it, content_sha1(it.title, it.lead), story, now, aliases.link(it))
                index.add(news_id, story or news_id, it.title, it.lead)
                inserted += 1
        return inserted

    async def collect_due(self) -> dict[str, int]:
        """Опрашивает RSS-источники, у которых подошёл срок. Ошибка одного источника не мешает остальным."""
        if self._rss is None:
            return {}
        now = self._now()
        due = [(s, h) for s in self._sources if s.kind == "rss" for h in [await self._repo.get_health(s.key)]
               if is_due(s, h, now)]
        if not due:
            return {}
        aliases = await self._aliases()
        index = await self._index(now)
        result: dict[str, int] = {}
        sem = asyncio.Semaphore(POLL_CONCURRENCY)

        async def poll(src: SourceConfig) -> FetchResult | Exception:
            async with sem:
                try:
                    return await asyncio.wait_for(self._rss.poll(src, now), SOURCE_TIMEOUT_SEC)  # type: ignore[union-attr]
                except TimeoutError:
                    return CollectorError(f"таймаут {SOURCE_TIMEOUT_SEC} с", source=src.key)
                except CollectorError as e:
                    return e

        fetched = await asyncio.gather(*(poll(src) for src, _ in due))
        # вставка — последовательно: общий индекс сюжетов и одна транзакция на новость
        for (src, health), res in zip(due, fetched, strict=True):
            if isinstance(res, Exception):
                await self._repo.save_health(update_health(health, [], set(), now, str(res)))
                log.warning("news.source_failed", source=src.key, error=str(res))
                continue
            known = await self._repo.known_ids(src.key, (i.external_id for i in res.items))
            result[src.key] = await self.ingest(res.items, aliases, index, now)
            await self._repo.save_health(
                update_health(health, res.items, known, now, None, not_modified=res.not_modified))
        return result

    async def collect_tinvest(self, max_pages: int = 10, page_size: int = 200) -> int:
        """Лента News Т-Инвестиций: читаем от свежих страниц, пока страница приносит новое.

        Направление курсора в документации не описано, поэтому курсор между запусками не сохраняется:
        иначе при курсоре «в прошлое» бот уходил бы в историю и пропускал свежие новости.
        """
        if self._tinvest is None:
            return 0
        now = self._now()
        aliases = await self._aliases()
        index = await self._index(now)
        health = await self._repo.get_health("tinvest_news")
        total, all_items, cursor = 0, [], None
        try:
            for _ in range(max_pages):
                items, cursor = await self._tinvest(cursor, page_size)
                all_items.extend(items)
                inserted = await self.ingest(items, aliases, index, now)
                total += inserted
                if not cursor or not items or inserted == 0:
                    break
        except Exception as e:
            await self._repo.save_health(update_health(health, [], set(), now, f"{type(e).__name__}: {e}"))
            log.warning("news.tinvest_failed", error=type(e).__name__)
            return total
        await self._repo.save_health(update_health(health, all_items, set(), now, None))
        return total

    async def background(self, tickers: Iterable[str], now: datetime | None = None) -> list[TickerNewsBackground]:
        now = now or self._now()
        wanted = sorted(set(tickers))
        items = await self._repo.items_for_tickers(wanted, now - timedelta(days=HISTORY_DAYS + 1))
        start = await self._repo.archive_start()
        history_start = msk_date(start) if start is not None else None
        out = [build_background(t, items.get(t, []), now, history_days=HISTORY_DAYS, history_start=history_start)
               for t in wanted]
        return sorted(out, key=lambda b: (-b.stories_7d, b.ticker))

    async def health(self) -> list[tuple[SourceHealth, bool]]:
        """(здоровье, сломан ли источник) по всем источникам, которые уже опрашивались."""
        return [(h, h.consecutive_empty >= BROKEN_AFTER_EMPTY or h.consecutive_errors >= 3)
                for h in await self._repo.all_health()]
