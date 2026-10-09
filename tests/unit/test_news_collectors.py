"""Коллекторы новостей: RSS/Atom на фикстурах (сеть подменяет respx), здоровье источника, T-Invest News без SDK."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import Enum, IntEnum
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Any

import httpx
import pytest
import respx
from pydantic import ValidationError

from investbot.adapters.rss import CollectorError, RssCollector, parse_feed, update_health
from investbot.adapters.tinvest import mapper
from investbot.adapters.tinvest.broker import TInvestBroker
from investbot.news.models import RawNewsItem, SourceHealth
from investbot.news.sources import SourceConfig, load_sources
from investbot.news.text import canonical_url, clip, normalize_ws, strip_html

ROOT = Path(__file__).resolve().parents[2]
FIX = ROOT / "tests" / "fixtures" / "rss"
NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
FEED_URL = "https://news.example.ru/rss"
RSS = {"content-type": "application/rss+xml; charset=utf-8"}


def source(key: str = "test", url: str = FEED_URL, **kw: Any) -> SourceConfig:
    fields: dict[str, Any] = {"title": "Тест", "kind": "rss", "poll_sec": 600, "priority": "P0", "q_src": "0.9"}
    return SourceConfig(**{**fields, "key": key, "url": url, **kw})


def feed(name: str) -> bytes:
    return (FIX / name).read_bytes()


def rss(n: int, prefix: str = "id") -> bytes:
    items = "".join(
        f"<item><title>Новость {i}</title><link>https://news.example.ru/{prefix}/{i}</link>"
        f"<guid isPermaLink=\"false\">{prefix}-{i}</guid></item>"
        for i in range(n)
    )
    head = '<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>Лента</title>'
    return f"{head}{items}</channel></rss>".encode()


@pytest.fixture
def router() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=True) as r:
        yield r


@pytest.fixture
def delays() -> list[float]:
    return []


@pytest.fixture
async def collector(delays: list[float]) -> AsyncIterator[RssCollector]:
    async def fake_sleep(seconds: float) -> None:
        delays.append(seconds)

    async with RssCollector(sleep=fake_sleep) as c:
        yield c


# --- Текст и URL ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://News.Example.ru/economy/1001?utm_source=rss&utm_medium=feed&yclid=123#top",
         "https://news.example.ru/economy/1001"),
        ("https://news.example.ru/a/1002?from=rss&id=7&fbclid=abc#comments", "https://news.example.ru/a/1002?id=7"),
        ("HTTPS://WWW.Example.RU:443/Path/A?q=%D0%B0+b&UTM_Campaign=x", "https://www.example.ru/Path/A?q=%D0%B0+b"),
        ("http://example.ru:8080", "http://example.ru:8080/"),
        ("https://user:pw@Host.ru/x", "https://host.ru/x"),
        ("  https://a.ru/x?flag&utm_x=1  ", "https://a.ru/x?flag"),
    ],
)
def test_canonical_url(raw: str, expected: str) -> None:
    assert canonical_url(raw) == expected


def test_canonical_url_leaves_relative_and_garbage() -> None:
    assert canonical_url("/news/1?utm_source=x") == "/news/1?utm_source=x"
    assert canonical_url(" не ссылка ") == "не ссылка"


def test_strip_html_and_whitespace() -> None:
    html = '<p>Чистая прибыль&nbsp;выросла &mdash; <a href="x">подробнее</a></p><script>alert(1)</script><br/>Ещё'
    assert strip_html(html) == "Чистая прибыль выросла — подробнее Ещё"
    assert strip_html("<p>один</p><p>два</p>") == "один два"
    assert strip_html("<style>.x{}</style>текст") == "текст"
    assert strip_html("&amp;lt;b&amp;gt;") == "&lt;b&gt;"  # сущности раскрываются ровно один раз
    assert strip_html("a < b") == "a < b"
    assert strip_html("") == ""
    assert normalize_ws("a ​b \n\t c­") == "a b c"


def test_clip() -> None:
    assert clip("abcdef", 4) == "abc…" and len(clip("x" * 2000, 1000)) == 1000
    assert clip("abc", 4) == "abc"
    assert clip("abc", 0) == ""


# --- Реестр источников ----------------------------------------------------------------------------------


def test_sources_yaml_loads() -> None:
    sources = {s.key: s for s in load_sources(ROOT / "config" / "sources.yaml")}
    p0 = {k for k, s in sources.items() if s.priority == "P0" and s.enabled}
    assert {"tinvest_news", "moex_news", "cbr_press", "cbr_events", "interfax", "tass", "vedomosti_investments",
            "finam_conews", "smartlab_news", "smartlab_disclosure"} <= p0
    assert sources["tinvest_news"].kind == "tinvest_news" and not sources["tinvest_news"].tos_ml_allowed
    assert sources["tass"].category_filter == ("Экономика и бизнес",)
    assert sources["finmarket_main"].encoding == "windows-1251"
    assert not sources["rbc"].enabled
    assert all(s.license_tag == "headline_link_only" for s in sources.values())
    assert all(s.url for s in sources.values() if s.kind == "rss")
    # ADR-12: ни Telegram, ни Пульса, ни форума Smart-lab
    for s in sources.values():
        text = f"{s.key} {s.url}".lower()
        assert not any(bad in text for bad in ("t.me", "telegram", "pulse", "tbank.ru/api/invest-gw", "/forum/"))


def test_source_config_validation(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        SourceConfig(key="x", title="X", kind="rss", poll_sec=600, priority="P0", q_src=Decimal("0.5"))
    with pytest.raises(ValidationError):
        source(encoding="no-such-codec")
    with pytest.raises(ValidationError):
        source(q_src=Decimal("1.5"))
    with pytest.raises(ValidationError):
        source(url="ftp://example.ru/feed")
    dup = tmp_path / "sources.yaml"
    entry = "{key: a, title: A, kind: rss, url: 'https://a.ru/rss', poll_sec: 600, priority: P0, q_src: 0.9}"
    dup.write_text(f"version: 1\nsources:\n  - {entry}\n  - {entry}\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="повторяющиеся"):
        load_sources(dup)


# --- RSS/Atom -------------------------------------------------------------------------------------------


async def test_rss20_cdata_html_mapping(router: respx.MockRouter, collector: RssCollector) -> None:
    router.get(FEED_URL).mock(return_value=httpx.Response(200, content=feed("rss20_cdata.xml"), headers=RSS))
    items = await collector.fetch(source(), NOW)

    by_id = {i.external_id: i for i in items}
    assert len(items) == 5  # шесть элементов, дубль tass-1001 отброшен
    first = by_id["tass-1001"]
    assert first.title == "Сбербанк «отчитался» за сентябрь"
    assert first.url == "https://news.example.ru/economy/1001"
    assert first.lead == "Чистая прибыль выросла — подробнее Вторая строка"  # script вырезан
    assert first.published_at == datetime(2026, 10, 8, 16, 30, tzinfo=UTC)  # 19:30 МСК
    assert first.categories == ("Экономика и бизнес",)
    assert first.source == "test" and first.fetched_at == NOW
    assert first.instrument_tickers == () and first.priority is None

    no_guid = by_id["https://news.example.ru/economy/1002?id=7"]  # без guid — канонический URL
    assert no_guid.url == no_guid.external_id
    assert no_guid.published_at is None
    assert no_guid.lead == "Индекс RGBI снизился & доходности выросли."
    assert by_id["tass-1006"].url is None  # javascript: не становится ссылкой


async def test_category_filter_is_case_and_space_insensitive(router: respx.MockRouter, collector: RssCollector) -> None:
    router.get(FEED_URL).mock(return_value=httpx.Response(200, content=feed("rss20_cdata.xml"), headers=RSS))
    items = await collector.fetch(source(category_filter=("Экономика и бизнес",)), NOW)
    # «Спорт» и элемент без категорий отброшены; «  экономика   и бизнес » совпало
    assert [i.external_id for i in items] == ["tass-1001", "https://news.example.ru/economy/1002?id=7", "tass-1006"]


async def test_atom_mapping(router: respx.MockRouter, collector: RssCollector) -> None:
    url = "https://atom.example.ru/feed.atom"
    router.get(url).mock(
        return_value=httpx.Response(200, content=feed("atom.xml"), headers={"content-type": "application/atom+xml"})
    )
    a, b, c = await collector.fetch(source(url=url), NOW)

    assert a.external_id == "urn:uuid:1225c695-cfb8-4ebb-aaaa-80da344efa6a"
    assert a.title == "Банк России сохранил ставку"
    assert a.url == "https://atom.example.ru/news/42?page=2"
    assert a.lead == "Совет директоров «сохранил» ключевую ставку"
    assert a.published_at == datetime(2026, 10, 9, 7, 15, tzinfo=UTC)  # published важнее updated
    assert a.categories == ("Денежно-кредитная политика",)

    assert b.url == "https://atom.example.ru/news/43"  # относительная ссылка разрешена от адреса ленты
    assert b.lead == "Текст только в content"
    assert b.published_at == datetime(2026, 10, 9, 6, 30, tzinfo=UTC)  # только updated
    assert c.published_at is None


async def test_windows_1251_encoding_override(router: respx.MockRouter, collector: RssCollector) -> None:
    # Сервер не называет кодировку ни в заголовке, ни в прологе: без override вышла бы windows-1252-кракозябра
    router.get(FEED_URL).mock(
        return_value=httpx.Response(200, content=feed("cp1251.xml"), headers={"content-type": "application/rss+xml"})
    )
    items = await collector.fetch(source(encoding="windows-1251"), NOW)
    assert [i.title for i in items] == ["Минфин разместил ОФЗ на 50 млрд рублей", "Курс юаня на Мосбирже"]
    assert items[0].lead == "Спрос превысил предложение в 1,5 раза."
    assert items[0].external_id == "https://www.finmarket.ru/news/6500001"
    assert items[0].published_at == datetime(2026, 10, 9, 9, 0, tzinfo=UTC)


async def test_broken_feed_raises_without_retry(
    router: respx.MockRouter, collector: RssCollector, delays: list[float]
) -> None:
    router.get(FEED_URL).mock(
        return_value=httpx.Response(200, content=feed("broken.xml"), headers={"content-type": "text/html"})
    )
    with pytest.raises(CollectorError) as e:
        await collector.fetch(source(), NOW)
    assert e.value.source == "test"
    assert delays == []


def test_truncated_feed_before_first_item_raises() -> None:
    data = '<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>Обрыв'.encode()
    with pytest.raises(CollectorError):
        parse_feed(source(), data, content_type="application/rss+xml", now=NOW)


def test_valid_empty_feed_is_empty_not_error() -> None:
    # text/xml без charset + кириллица → bozo (CharacterEncodingOverride), но лента разобрана: это «пусто»
    assert parse_feed(source(), rss(0), content_type="text/xml", now=NOW) == []


def test_parse_feed_requires_aware_now() -> None:
    with pytest.raises(ValueError, match="tz-aware"):
        parse_feed(source(), rss(1), now=datetime(2026, 10, 9, 12, 0))


async def test_http_error_is_not_retried(
    router: respx.MockRouter, collector: RssCollector, delays: list[float]
) -> None:
    route = router.get(FEED_URL).mock(return_value=httpx.Response(404))
    with pytest.raises(CollectorError) as e:
        await collector.fetch(source(), NOW)
    assert e.value.status == 404 and route.call_count == 1 and delays == []


async def test_5xx_is_retried(router: respx.MockRouter, collector: RssCollector, delays: list[float]) -> None:
    route = router.get(FEED_URL)
    route.side_effect = [httpx.Response(503), httpx.Response(502), httpx.Response(200, content=rss(2), headers=RSS)]
    items = await collector.fetch(source(), NOW)
    assert len(items) == 2 and route.call_count == 3 and delays == [1.0, 2.0]


async def test_5xx_after_retries_raises(router: respx.MockRouter, collector: RssCollector) -> None:
    router.get(FEED_URL).mock(return_value=httpx.Response(503))
    with pytest.raises(CollectorError) as e:
        await collector.fetch(source(), NOW)
    assert e.value.status == 503


async def test_transport_errors_exhaust_retries(
    router: respx.MockRouter, collector: RssCollector, delays: list[float]
) -> None:
    route = router.get(FEED_URL).mock(side_effect=httpx.ConnectError("нет маршрута"))
    with pytest.raises(CollectorError, match="недоступен"):
        await collector.fetch(source(), NOW)
    assert route.call_count == 3 and delays == [1.0, 2.0]


async def test_conditional_get_304(router: respx.MockRouter, collector: RssCollector) -> None:
    route = router.get(FEED_URL)
    validators = {"etag": '"v1"', "last-modified": "Fri, 09 Oct 2026 08:00:00 GMT"}
    route.side_effect = [httpx.Response(200, content=rss(3), headers={**RSS, **validators}), httpx.Response(304)]
    src = source()

    first = await collector.poll(src, NOW)
    assert len(first.items) == 3 and not first.not_modified
    assert "if-none-match" not in route.calls[0].request.headers

    second = await collector.poll(src, NOW + timedelta(minutes=10))
    assert second.items == [] and second.not_modified and second.status == 304
    sent = route.calls[1].request.headers
    assert sent["if-none-match"] == '"v1"' and sent["if-modified-since"] == validators["last-modified"]

    # Выбор: 304 — успешный опрос, который не считается пустым ответом (валидаторы — только от непустых)
    prev = SourceHealth(source="test", last_ok_at=NOW, consecutive_empty=0)
    health = update_health(prev, second.items, {"id-0"}, NOW + timedelta(minutes=10), not_modified=True)
    assert health.consecutive_empty == 0 and health.last_ok_at == NOW + timedelta(minutes=10)
    assert health.last_new_items == 0 and not health.overrun


async def test_validators_of_empty_feed_are_not_kept(router: respx.MockRouter, collector: RssCollector) -> None:
    route = router.get(FEED_URL)
    route.side_effect = [
        httpx.Response(200, content=rss(0), headers={**RSS, "etag": '"empty"'}),
        httpx.Response(200, content=rss(0), headers={**RSS, "etag": '"empty"'}),
    ]
    assert await collector.fetch(source(), NOW) == []
    assert await collector.fetch(source(), NOW) == []
    assert "if-none-match" not in route.calls[1].request.headers  # иначе 304 спрятал бы пустую ленту


async def test_fetch_rejects_non_rss_source(collector: RssCollector) -> None:
    tinvest = SourceConfig(key="tinvest_news", title="T", kind="tinvest_news", poll_sec=900, priority="P0",
                           q_src=Decimal("0.8"))
    with pytest.raises(ValueError, match="не RSS"):
        await collector.fetch(tinvest, NOW)


# --- Здоровье источника: «поломка» и «пробитие» ----------------------------------------------------------


def items_of(n: int, prefix: str = "id") -> list[RawNewsItem]:
    return [
        RawNewsItem(source="test", external_id=f"{prefix}-{i}", url=None, title=f"t{i}", published_at=None,
                    fetched_at=NOW)
        for i in range(n)
    ]


def test_health_empty_streak_and_reset() -> None:
    h = SourceHealth(source="test")
    for k in range(1, 4):
        h = update_health(h, [], set(), NOW + timedelta(minutes=k))
        assert h.consecutive_empty == k
    assert h.last_ok_at == NOW + timedelta(minutes=3) and h.last_new_items == 0
    h = update_health(h, items_of(2), set(), NOW + timedelta(minutes=4))
    assert h.consecutive_empty == 0 and h.last_new_items == 2


def test_health_errors_then_recovery() -> None:
    h = SourceHealth(source="test", last_ok_at=NOW, consecutive_empty=2)
    h = update_health(h, [], set(), NOW + timedelta(minutes=10), error="HTTP 503")
    h = update_health(h, [], set(), NOW + timedelta(minutes=20), error="  таймаут\n ")
    assert h.consecutive_errors == 2 and h.last_error == "таймаут"
    assert h.last_error_at == NOW + timedelta(minutes=20) and h.last_ok_at == NOW
    assert h.consecutive_empty == 2  # ошибка не считается пустым ответом
    h = update_health(h, items_of(1), set(), NOW + timedelta(minutes=30))
    assert h.consecutive_errors == 0 and h.consecutive_empty == 0
    assert h.last_error == "таймаут"  # последняя ошибка остаётся в истории


def test_health_overrun() -> None:
    prev = SourceHealth(source="test", last_ok_at=NOW)
    later = NOW + timedelta(minutes=10)
    assert update_health(prev, items_of(10), set(), later).overrun  # все 10 новые — лента «пробита»
    assert not update_health(prev, items_of(9), set(), later).overrun  # мало элементов для вывода
    assert not update_health(prev, items_of(10), {"id-9"}, later).overrun  # есть известный — стык найден
    first_poll = update_health(SourceHealth(source="test"), items_of(25), set(), later)
    assert not first_poll.overrun and first_poll.last_new_items == 25  # на первом опросе сравнивать не с чем
    after_error = update_health(prev, [], set(), later, error="x")
    assert not after_error.overrun and after_error.last_new_items == 0


# --- T-Invest News (фейковые объекты SDK) ---------------------------------------------------------------


class Priority(IntEnum):
    NEWS_PRIORITY_HIGH = 2


def test_map_news_item_nested_instrument() -> None:
    n = NS(
        id="n-1", source="Интерфакс", title="<b>Сбербанк</b> &laquo;рекомендовал&raquo; дивиденды",
        summary="<p>Совет директоров рекомендовал выплатить дивиденды.</p>", content="Полный текст",
        instrument_id=[NS(instrument=NS(ticker="SBER", uid="u1")), NS(instrument=NS(ticker="SBERP")),
                       NS(instrument=NS(ticker="SBER"))],
        priority=True, ts=datetime(2026, 10, 9, 7, 0, tzinfo=UTC),
    )
    item = mapper.map_news_item(n, NOW)
    assert item.source == "tinvest_news" and item.external_id == "n-1"
    assert item.title == "Сбербанк «рекомендовал» дивиденды"
    assert item.lead == "Совет директоров рекомендовал выплатить дивиденды."
    assert item.instrument_tickers == ("SBER", "SBERP")
    assert item.priority == 1
    assert item.published_at == datetime(2026, 10, 9, 7, 0, tzinfo=UTC)
    assert item.fetched_at == NOW and item.url is None
    assert item.categories == ("source:Интерфакс",)


def test_map_news_item_flat_refs_and_fallbacks() -> None:
    n = NS(
        id="", title="", content="x" * 1500,
        instrument_id=[NS(ticker="GAZP", class_code="TQBR"), "uid-only"],
        priority=Priority.NEWS_PRIORITY_HIGH, ts=datetime(2026, 10, 9, 10, 0),  # naive из SDK = UTC
        url="https://Example.ru/a?utm_source=tinvest",
    )
    item = mapper.map_news_item(n, NOW)
    assert len(item.lead) == 1000 and item.title == item.lead[:500][:-1] + "…"
    assert item.instrument_tickers == ("GAZP",)
    assert item.priority == 2
    assert item.published_at == datetime(2026, 10, 9, 10, 0, tzinfo=UTC)
    assert item.external_id.startswith("sha1:")
    assert item.url == "https://example.ru/a"
    assert item.categories == ()


def test_map_news_item_empty_timestamp() -> None:
    n = NS(id="n-2", title="Без даты", ts=datetime(1970, 1, 1, tzinfo=UTC), instrument_id=None, priority=None)
    item = mapper.map_news_item(n, NOW)
    assert item.published_at is None and item.priority is None and item.lead == ""


class AccType(Enum):
    ACCOUNT_TYPE_TINKOFF = 1


class Access(Enum):
    ACCOUNT_ACCESS_LEVEL_READ_ONLY = 2


class NewsSchemas:
    class AccountStatus:
        ACCOUNT_STATUS_OPEN = 2

    @staticmethod
    def GetAccountsRequest(**kw: Any) -> NS:
        return NS(**kw)

    @staticmethod
    def NewsRequest(**kw: Any) -> NS:
        return NS(**kw)


async def test_news_page_reads_cursor_pages() -> None:
    requests: list[NS] = []
    pages = {
        None: NS(items=[NS(id="a", source="ПРАЙМ", title="Первая", ts=NOW, priority=0,
                           instrument_id=[NS(instrument=NS(ticker="LKOH"))])],
                 has_next=True, next_cursor="c2"),
        "c2": NS(items=[NS(id="b", title="Вторая", ts=NOW, instrument_id=[])], has_next=False, next_cursor="c3"),
    }

    async def get_accounts(request: Any) -> NS:
        return NS(accounts=[NS(id="A1", name="Брокерский", type=AccType.ACCOUNT_TYPE_TINKOFF,
                               access_level=Access.ACCOUNT_ACCESS_LEVEL_READ_ONLY, opened_date=None)])

    async def news(request: NS) -> NS:
        requests.append(request)
        return pages[getattr(request, "cursor", None)]

    services = NS(
        users=NS(get_accounts=get_accounts),
        instruments=NS(news=news),
        orders=NS(post_order=lambda **_: pytest.fail("торговый вызов")),
    )

    @asynccontextmanager
    async def factory(token: str) -> AsyncIterator[NS]:
        yield services

    async with TInvestBroker({"main": "t.fake"}, factory, NewsSchemas) as broker:
        first, cursor = await broker.news_page(None)
        assert cursor == "c2"
        second, cursor = await broker.news_page(cursor, limit=50)
        assert cursor is None  # has_next=False: курсор ответа не возвращается
        with pytest.raises(ValueError):
            await broker.news_page(None, limit=5000)

    assert [vars(r) for r in requests] == [{"limit": 200}, {"limit": 50, "cursor": "c2"}]
    assert [i.external_id for i in first + second] == ["a", "b"]
    assert first[0].instrument_tickers == ("LKOH",) and first[0].priority == 0
    assert all(i.source == "tinvest_news" and i.fetched_at.tzinfo is not None for i in first + second)
