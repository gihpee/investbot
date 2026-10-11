"""IssClient.bonds_market на записанном вручную ответе ISS (сети нет, ISS подменяется respx)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import respx

from investbot.adapters.moex_iss.client import IssClient, IssError
from investbot.adapters.moex_iss.models import BondMarketRow

FIX = Path(__file__).parent.parent / "fixtures" / "iss"
BASE = "https://iss.moex.com/iss"
JSON = {"content-type": "application/json; charset=utf-8"}
PATH = "/engines/stock/markets/bonds/boards/TQCB/securities.json"


def ok(name: str) -> httpx.Response:
    return httpx.Response(200, content=(FIX / name).read_bytes(), headers=JSON)


@pytest.fixture
def iss() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=BASE, assert_all_called=True) as router:
        yield router


@pytest.fixture
async def client() -> AsyncIterator[IssClient]:
    async def no_sleep(_: float) -> None:
        return None

    async with IssClient(sleep=no_sleep) as c:
        yield c


async def _rows(iss: respx.MockRouter, client: IssClient) -> dict[str, BondMarketRow]:
    iss.get(PATH).mock(return_value=ok("bonds_TQCB.json"))
    return {r.secid: r for r in await client.bonds_market("TQCB")}


async def test_bonds_market_request_and_join(iss: respx.MockRouter, client: IssClient) -> None:
    route = iss.get(PATH).mock(return_value=ok("bonds_TQCB.json"))

    rows = await client.bonds_market("TQCB")

    params = route.calls.last.request.url.params
    assert params["iss.only"] == "securities,marketdata,marketdata_yields"
    assert params["iss.meta"] == "off"
    assert [r.secid for r in rows] == ["RU000A10AAA1", "RU000A10FLT1", "RU000A10USD1", "RU000A10NOM1"]
    assert all(r.board == "TQCB" for r in rows)


async def test_fixed_coupon_row(iss: respx.MockRouter, client: IssClient) -> None:
    row = (await _rows(iss, client))["RU000A10AAA1"]

    assert row.shortname == "Альфа 1Р1" and row.isin == "RU000A10AAA1"
    assert row.face_value == Decimal(1000) and row.face_unit == "rub"  # SUR → rub
    assert row.coupon_percent == Decimal("16.8") and row.coupon_value == Decimal("41.88")
    assert row.next_coupon == date(2026, 11, 12)
    assert row.accrued_int == Decimal("26.15")
    assert row.mat_date == date(2029, 8, 9)
    assert row.offer_date == date(2027, 8, 12) and row.put_option_date == date(2027, 8, 12)
    assert row.buyback_date is None  # «0000-00-00»
    assert row.call_option_date is None
    assert row.lot_size == 1 and row.list_level == 2 and row.sec_type == "6"
    # PRICE из marketdata_yields приоритетнее LAST
    assert row.price_pct == Decimal("98.88")
    assert row.effective_yield == Decimal("17.8123")
    assert row.yield_to_offer == Decimal("17.8123")
    assert row.yield_date == date(2027, 8, 12)
    # DURATION из marketdata_yields (287), а не из marketdata (280)
    assert row.duration_days == Decimal(287)
    assert row.duration_years == Decimal(287) / Decimal(365)
    assert row.gspread_bp == Decimal(262) and row.zspread_bp == Decimal(245)
    assert row.val_today == Decimal("35123456.7") and row.num_trades == 412
    assert row.trading_status == "T"


async def test_floater_zeros_become_none(iss: respx.MockRouter, client: IssClient) -> None:
    row = (await _rows(iss, client))["RU000A10FLT1"]

    assert row.effective_yield is None
    assert row.duration_days is None and row.duration_years is None
    assert row.yield_to_offer is None
    assert row.gspread_bp is None and row.zspread_bp is None  # нули спредов без доходности — тоже «нет значения»
    assert row.yield_date is None and row.offer_date is None
    assert row.price_pct == Decimal("100.15")  # PRICE = 0 → LAST
    assert row.num_trades == 96


async def test_foreign_face_and_price_fallbacks(iss: respx.MockRouter, client: IssClient) -> None:
    rows = await _rows(iss, client)

    usd = rows["RU000A10USD1"]
    assert usd.face_unit == "usd"
    assert usd.price_pct == Decimal("96.4")  # PRICE = 0, LAST = null → PREVPRICE
    assert usd.effective_yield is None and usd.duration_days is None
    assert usd.num_trades == 0 and usd.val_today == Decimal(0)

    quiet = rows["RU000A10NOM1"]  # нет строки marketdata
    assert quiet.price_pct == Decimal("101.2")
    assert quiet.num_trades is None and quiet.val_today is None and quiet.trading_status is None
    assert quiet.effective_yield == Decimal("14.37")
    assert quiet.gspread_bp == Decimal(-3) and quiet.zspread_bp == Decimal(-12)
    assert quiet.duration_days == Decimal(520)


async def test_missing_block_is_error(iss: respx.MockRouter, client: IssClient) -> None:
    body = b'{"securities": {"columns": ["SECID"], "data": [["X"]]}, "marketdata": {"columns": [], "data": []}}'
    iss.get(PATH).mock(return_value=httpx.Response(200, content=body, headers=JSON))
    with pytest.raises(IssError):
        await client.bonds_market("TQCB")


async def test_bad_value_is_iss_error(iss: respx.MockRouter, client: IssClient) -> None:
    body = (
        b'{"securities": {"columns": ["SECID", "MATDATE"], "data": [["X", "not-a-date"]]},'
        b' "marketdata": {"columns": ["SECID"], "data": []},'
        b' "marketdata_yields": {"columns": ["SECID"], "data": []}}'
    )
    iss.get(PATH).mock(return_value=httpx.Response(200, content=body, headers=JSON))
    with pytest.raises(IssError):
        await client.bonds_market("TQCB")


async def test_board_in_path(iss: respx.MockRouter, client: IssClient) -> None:
    body = (
        b'{"securities": {"columns": ["SECID", "BOARDID", "FACEUNIT"], "data": [["SU26238RMFS4", "TQOB", "SUR"]]},'
        b' "marketdata": {"columns": ["SECID"], "data": []},'
        b' "marketdata_yields": {"columns": ["SECID"], "data": []}}'
    )
    iss.get("/engines/stock/markets/bonds/boards/TQOB/securities.json").mock(
        return_value=httpx.Response(200, content=body, headers=JSON)
    )
    (row,) = await client.bonds_market("TQOB")
    assert row.board == "TQOB" and row.face_unit == "rub"
    assert row.price_pct is None and row.mat_date is None and row.effective_yield is None
