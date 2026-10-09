"""Сквозной сценарий новостей: сбор → дедуп → связывание → архив (SQLite) → фон → блок отчёта."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from investbot.adapters.rss.collector import CollectorError, FetchResult
from investbot.channels.telegram.news import news_block, news_message
from investbot.core.models import AssetClass, InstrumentMeta
from investbot.data.db import make_engine, metadata
from investbot.data.news_repo import NewsRepo
from investbot.news.linking import AliasIndex
from investbot.news.models import RawNewsItem
from investbot.news.sources import SourceConfig
from investbot.services.news import NewsService, is_due

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)

SBER = InstrumentMeta(ticker="SBER", name="Сбербанк", asset_class=AssetClass.EQUITY, isin="RU0009029540")
GAZP = InstrumentMeta(ticker="GAZP", name="Газпром", asset_class=AssetClass.EQUITY, isin="RU0007661625")


def src(key: str, poll: int = 600) -> SourceConfig:
    return SourceConfig(key=key, title=key, kind="rss", url=f"https://{key}.example/rss", poll_sec=poll,
                        priority="P0", q_src=1)


def item(source: str, ext: str, title: str, minutes_ago: int = 5, tickers: tuple[str, ...] = ()) -> RawNewsItem:
    return RawNewsItem(source=source, external_id=ext, url=f"https://{source}.example/{ext}", title=title,
                       lead=title + ". Подробности в материале.", published_at=NOW - timedelta(minutes=minutes_ago),
                       fetched_at=NOW, instrument_tickers=tickers)


class FakeRss:
    def __init__(self, feeds: dict[str, list[RawNewsItem] | Exception]) -> None:
        self.feeds = feeds
        self.calls: list[str] = []

    async def poll(self, source: SourceConfig, now: datetime) -> FetchResult:
        self.calls.append(source.key)
        feed = self.feeds[source.key]
        if isinstance(feed, Exception):
            raise feed
        return FetchResult(items=list(feed))


@pytest.fixture
async def repo(tmp_path: Path) -> NewsRepo:
    engine = make_engine(f"sqlite+aiosqlite:///{tmp_path / 'n.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    return NewsRepo(engine)


async def aliases() -> AliasIndex:
    return AliasIndex.build([SBER, GAZP])


async def test_collect_dedup_link_and_background(repo: NewsRepo) -> None:
    rss = FakeRss({
        "interfax": [item("interfax", "i1", "Сбербанк увеличил чистую прибыль по МСФО за девять месяцев на 12%"),
                     item("interfax", "i2", "Погода в Москве: ожидается дождь")],
        "tass": [item("tass", "t1", "Сбербанк увеличил чистую прибыль по МСФО за девять месяцев на 12 процентов")],
        "broken": CollectorError("HTTP 503", source="broken", status=503),
    })
    svc = NewsService(repo, [src("interfax"), src("tass"), src("broken")], rss, None, aliases, now=lambda: NOW)
    inserted = await svc.collect_due()
    assert inserted == {"interfax": 2, "tass": 1}
    health = {h.source: h for h, _ in await svc.health()}
    assert health["broken"].consecutive_errors == 1 and health["interfax"].last_ok_at == NOW

    # повторный вызов сразу же: срок опроса не подошёл — коллектор не дёргается
    rss.calls.clear()
    assert await svc.collect_due() == {}
    assert rss.calls == []

    [bg] = await svc.background(["SBER"])
    assert bg.stories_7d == 1  # один сюжет из двух источников
    assert bg.headlines[0].story_size == 2
    text = news_block([bg])
    assert text is not None and "SBER" in text and "+1 ист." in text


async def test_tinvest_news_pages_until_nothing_new(repo: NewsRepo) -> None:
    pages = {
        None: ([item("tinvest_news", "n1", "Газпром: совет директоров", tickers=("GAZP",)),
                item("tinvest_news", "n2", "Новость без бумаг")], "c1"),
        "c1": ([item("tinvest_news", "n0", "Старая новость про Газпром", 120, ("GAZP",))], "c2"),
        "c2": ([item("tinvest_news", "n0", "Старая новость про Газпром", 120, ("GAZP",))], "c3"),
    }
    calls: list[str | None] = []

    async def tinvest(cursor: str | None, limit: int) -> tuple[list[RawNewsItem], str | None]:
        calls.append(cursor)
        return pages[cursor]

    svc = NewsService(repo, [], None, tinvest, aliases, now=lambda: NOW)
    assert await svc.collect_tinvest() == 3
    assert calls == [None, "c1", "c2"]  # третья страница ничего нового не дала — остановились
    [bg] = await svc.background(["GAZP"])
    assert bg.stories_7d == 2
    assert "GAZP" in news_message([bg], "GAZP")


def test_is_due_respects_poll_and_overrun() -> None:
    from investbot.news.models import SourceHealth

    s = src("x", poll=600)
    assert is_due(s, SourceHealth(source="x"), NOW)
    assert not is_due(s, SourceHealth(source="x", last_ok_at=NOW - timedelta(seconds=400)), NOW)
    assert is_due(s, SourceHealth(source="x", last_ok_at=NOW - timedelta(seconds=400), overrun=True), NOW)
