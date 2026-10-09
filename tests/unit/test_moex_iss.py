"""Клиент MOEX ISS на записанных вручную ответах (сети нет, ISS подменяется respx)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import date, time
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import respx

from investbot.adapters.moex_iss.client import IssClient, IssError, IssSubscriptionRequired, parse_block

FIX = Path(__file__).parent.parent / "fixtures" / "iss"
BASE = "https://iss.moex.com/iss"
JSON = {"content-type": "application/json; charset=utf-8"}


def ok(name: str) -> httpx.Response:
    return httpx.Response(200, content=(FIX / name).read_bytes(), headers=JSON)


@pytest.fixture
def iss() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=BASE, assert_all_called=True) as router:
        yield router


@pytest.fixture
def delays() -> list[float]:
    return []


@pytest.fixture
async def client(delays: list[float]) -> AsyncIterator[IssClient]:
    async def fake_sleep(seconds: float) -> None:
        delays.append(seconds)

    async with IssClient(sleep=fake_sleep) as c:
        yield c


def test_parse_block() -> None:
    payload = {"history": {"columns": ["TRADEDATE", "CLOSE"], "data": [["2026-10-06", 1.5], ["2026-10-07", None]]}}
    assert parse_block(payload, "history") == [
        {"TRADEDATE": "2026-10-06", "CLOSE": 1.5},
        {"TRADEDATE": "2026-10-07", "CLOSE": None},
    ]
    assert parse_block({"history": {"columns": ["A"], "data": []}}, "history") == []
    with pytest.raises(IssError):
        parse_block(payload, "marketdata")
    with pytest.raises(IssError):
        parse_block({"history": {"columns": ["A", "B"], "data": [[1]]}}, "history")


async def test_share_history_paginates_by_cursor(iss: respx.MockRouter, client: IssClient) -> None:
    path = "/history/engines/stock/markets/shares/boards/TQBR/securities/SBER.json"
    p1 = iss.get(path, params__contains={"start": "0"}).mock(return_value=ok("history_shares_SBER_p1.json"))
    p2 = iss.get(path, params__contains={"start": "2"}).mock(return_value=ok("history_shares_SBER_p2.json"))

    bars = await client.share_history("SBER", date(2026, 10, 5), date(2026, 10, 7))

    assert [b.tradedate for b in bars] == [date(2026, 10, 5), date(2026, 10, 6), date(2026, 10, 7)]
    first = bars[0]
    assert first.close == Decimal("302.44")
    assert first.waprice == Decimal("301.73")
    assert first.volume == 32729510
    assert first.value == Decimal("9875432100.5")
    # день без сделок: цены null, а не 0
    assert bars[2].open is None and bars[2].close is None and bars[2].waprice is None
    assert bars[2].volume == 0
    params = p1.calls.last.request.url.params
    assert params["iss.meta"] == "off"
    assert params["from"] == "2026-10-05" and params["till"] == "2026-10-07"
    assert p1.call_count == 1 and p2.call_count == 1


async def test_index_history(iss: respx.MockRouter, client: IssClient) -> None:
    route = iss.get("/history/engines/stock/markets/index/securities/MCFTR.json").mock(
        return_value=ok("history_index_MCFTR.json")
    )
    frm, to = date(2026, 10, 5), date(2026, 10, 6)

    closes = await client.index_history("MCFTR", frm, to)
    assert [(c.tradedate, c.close) for c in closes] == [
        (date(2026, 10, 5), Decimal("7123.45")),
        (date(2026, 10, 6), Decimal("7151.07")),
    ]
    assert await client.index_closes("MCFTR", frm, to) == [(c.tradedate, c.close) for c in closes]
    assert route.call_count == 2  # одна страница: курсор INDEX + PAGESIZE >= TOTAL


async def test_bond_history_duration_and_nulls(iss: respx.MockRouter, client: IssClient) -> None:
    iss.get("/history/engines/stock/markets/bonds/boards/TQOB/securities/SU26238RMFS4.json").mock(
        return_value=ok("history_bonds_SU26238RMFS4.json")
    )

    traded, idle = await client.bond_history("SU26238RMFS4", date(2026, 10, 6), date(2026, 10, 7))

    assert traded.close == Decimal("55.4")
    assert traded.yieldclose == Decimal("15.12")
    assert traded.accint == Decimal("20.41")
    assert traded.facevalue == Decimal(1000)
    assert traded.duration_days == Decimal(2920)
    assert traded.duration_years == Decimal(8)
    assert idle.close is None and idle.yieldclose is None
    assert idle.duration_days is None and idle.duration_years is None
    assert idle.accint == Decimal("20.6")


async def test_bondization_pages_until_empty(iss: respx.MockRouter, client: IssClient) -> None:
    path = "/securities/RU000A10FLT1/bondization.json"
    pages = {
        "0": "bondization_RU000A10FLT1_p1.json",
        "3": "bondization_RU000A10FLT1_p2.json",
        "5": "bondization_empty.json",
    }
    routes = [
        iss.get(path, params__contains={"start": start}).mock(return_value=ok(name)) for start, name in pages.items()
    ]

    bz = await client.bondization("RU000A10FLT1")

    assert all(r.call_count == 1 for r in routes)
    assert all(r.calls.last.request.url.params["limit"] == "100" for r in routes)
    assert [c.coupondate for c in bz.coupons] == [
        date(2026, 6, 11),
        date(2026, 9, 10),
        date(2026, 12, 10),
        date(2027, 3, 11),
        date(2027, 6, 10),
    ]
    assert bz.coupons[0].value == Decimal("44.88") and bz.coupons[0].valueprc == Decimal(18)
    assert bz.coupons[0].recorddate == date(2026, 6, 10) and bz.coupons[0].startdate == date(2026, 3, 12)
    # будущие купоны флоатера — null
    assert bz.coupons[-1].value is None and bz.coupons[-1].valueprc is None
    assert [(a.amortdate, a.value, a.facevalue) for a in bz.amortizations] == [
        (date(2027, 6, 10), Decimal(500), Decimal(1000)),
        (date(2027, 12, 9), Decimal(500), Decimal(500)),
    ]
    assert len(bz.offers) == 1
    assert bz.offers[0].offerdate == date(2027, 3, 11)
    assert bz.offers[0].offertype == "Оферта (Put)"
    assert bz.offers[0].price == Decimal(100)


async def test_zcyc(iss: respx.MockRouter, client: IssClient) -> None:
    route = iss.get("/engines/stock/zcyc.json").mock(return_value=ok("zcyc_yearyields.json"))

    curve = await client.zcyc()
    assert len(curve) == 11
    assert curve[0].period == Decimal("0.25") and curve[0].value == Decimal("13.35")
    assert curve[3].period == Decimal(1) and curve[3].value == Decimal("13.72")
    assert curve[0].tradedate == date(2026, 10, 9) and curve[0].tradetime == time(18, 39, 52)
    assert "date" not in route.calls.last.request.url.params

    await client.zcyc(date(2026, 10, 8))
    assert route.calls.last.request.url.params["date"] == "2026-10-08"


async def test_security_primary_board(iss: respx.MockRouter, client: IssClient) -> None:
    iss.get("/securities/SBER.json").mock(return_value=ok("security_SBER.json"))

    info = await client.security("SBER")

    assert (info.boardid, info.market, info.engine) == ("TQBR", "shares", "stock")
    assert info.description["ISIN"] == "RU0009029540"
    assert info.description["TYPE"] == "common_share"
    assert info.description["LISTLEVEL"] == "1"


async def test_security_not_found(iss: respx.MockRouter, client: IssClient) -> None:
    iss.get("/securities/NOPE.json").mock(return_value=ok("security_unknown.json"))
    with pytest.raises(IssError):
        await client.security("NOPE")


async def test_subscription_html_detected(iss: respx.MockRouter, client: IssClient) -> None:
    html = (FIX / "subscription_required.html").read_bytes()
    iss.get("/securities/SBER.json").mock(
        return_value=httpx.Response(200, content=html, headers={"content-type": "text/html; charset=utf-8"})
    )
    with pytest.raises(IssSubscriptionRequired):
        await client.security("SBER")


async def test_non_json_body_is_subscription(iss: respx.MockRouter, client: IssClient) -> None:
    iss.get("/engines/stock/zcyc.json").mock(
        return_value=httpx.Response(200, content=b"<p>only subscribers</p>", headers=JSON)
    )
    with pytest.raises(IssSubscriptionRequired):
        await client.zcyc()


async def test_retry_on_503_then_success(iss: respx.MockRouter, client: IssClient, delays: list[float]) -> None:
    route = iss.get("/engines/stock/zcyc.json").mock(side_effect=[httpx.Response(503), ok("zcyc_yearyields.json")])

    curve = await client.zcyc()

    assert len(curve) == 11
    assert route.call_count == 2
    assert delays == [0.5]


async def test_retry_exhausted_on_transport_error(
    iss: respx.MockRouter, client: IssClient, delays: list[float]
) -> None:
    route = iss.get("/engines/stock/zcyc.json").mock(side_effect=httpx.ConnectError("tls timeout"))

    with pytest.raises(IssError) as err:
        await client.zcyc()

    assert not isinstance(err.value, IssSubscriptionRequired)
    assert route.call_count == 3
    assert delays == [0.5, 1.0]


async def test_persistent_5xx_raises(iss: respx.MockRouter, client: IssClient, delays: list[float]) -> None:
    route = iss.get("/engines/stock/zcyc.json").mock(return_value=httpx.Response(502))
    with pytest.raises(IssError) as err:
        await client.zcyc()
    assert err.value.status == 502
    assert route.call_count == 3
    assert delays == [0.5, 1.0]


async def test_404_is_iss_error_without_retry(iss: respx.MockRouter, client: IssClient, delays: list[float]) -> None:
    route = iss.get("/securities/SBER.json").mock(
        return_value=httpx.Response(404, content=b"<html>Not Found</html>", headers={"content-type": "text/html"})
    )
    with pytest.raises(IssError) as err:
        await client.security("SBER")
    assert not isinstance(err.value, IssSubscriptionRequired)
    assert err.value.status == 404
    assert route.call_count == 1
    assert delays == []
