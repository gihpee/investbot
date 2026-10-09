from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import text

from investbot.data.db import make_engine, metadata
from investbot.data.news_repo import NewsRepo
from investbot.news.models import NewsLink, RawNewsItem, SourceHealth

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


PG_URL = os.environ.get("INVESTBOT_TEST_PG_URL")  # postgresql+asyncpg://… с применёнными миграциями


@pytest.fixture(params=["sqlite", "postgres"])
async def repo(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[NewsRepo]:
    if request.param == "sqlite":
        engine = make_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
        async with engine.begin() as conn:
            await conn.run_sync(metadata.create_all)
    else:
        if not PG_URL:
            pytest.skip("INVESTBOT_TEST_PG_URL не задан")
        engine = make_engine(PG_URL)
        async with engine.begin() as conn:
            await conn.execute(text("TRUNCATE news_link, news_item, source_health, kv_state RESTART IDENTITY CASCADE"))
    yield NewsRepo(engine)
    await engine.dispose()


def item(ext: str, title: str, source: str = "interfax") -> RawNewsItem:
    return RawNewsItem(source=source, external_id=ext, url=f"https://x/{ext}", title=title, lead="лид",
                       published_at=NOW - timedelta(minutes=5), fetched_at=NOW)


async def test_insert_assigns_story_and_links(repo: NewsRepo) -> None:
    first = await repo.insert(item("1", "Сбербанк отчитался"), "a" * 40, None, NOW,
                              [NewsLink(ticker="SBER", method="alias_name", confidence=Decimal("0.7"))])
    second = await repo.insert(item("2", "Сбербанк отчитался", "tass"), "a" * 40, first, NOW,
                               [NewsLink(ticker="SBER", method="api", confidence=Decimal(1))])
    assert await repo.known_ids("interfax", ["1", "3"]) == {"1"}
    idx = await repo.recent_for_index(NOW - timedelta(days=7))
    assert [(i, s) for i, s, _, _ in idx] == [(first, first), (second, first)]
    by = await repo.items_for_tickers(["SBER", "GAZP"], NOW - timedelta(days=1))
    assert [n.id for n in by["SBER"]] == [second, first]
    assert by["SBER"][0].links[0].method == "api"
    assert by["SBER"][0].first_seen_at.tzinfo is not None
    assert "GAZP" not in by


async def test_health_and_state_roundtrip(repo: NewsRepo) -> None:
    assert (await repo.get_health("tass")).consecutive_empty == 0
    await repo.save_health(SourceHealth(source="tass", last_ok_at=NOW, consecutive_empty=2, overrun=True))
    await repo.save_health(SourceHealth(source="tass", last_ok_at=NOW, consecutive_empty=3))
    h = await repo.get_health("tass")
    assert h.consecutive_empty == 3 and not h.overrun and h.last_ok_at == NOW
    assert [x.source for x in await repo.all_health()] == ["tass"]
    assert await repo.get_state("tinvest_news_cursor") is None
    await repo.set_state("tinvest_news_cursor", "abc")
    await repo.set_state("tinvest_news_cursor", "def")
    assert await repo.get_state("tinvest_news_cursor") == "def"
