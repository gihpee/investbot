"""Календарь событий: маппинг SDK, сервис с частичными сбоями источников, рендер для Telegram."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from enum import Enum
from types import SimpleNamespace as NS
from typing import Any

import pytest

from investbot.adapters.moex_iss.models import Amortization, Bondization, Coupon, Offer
from investbot.adapters.tinvest import mapper
from investbot.adapters.tinvest.broker import TInvestBroker
from investbot.calendar.models import (
    PUT_OFFER_LOOKAHEAD,
    CalendarEvent,
    CalendarResult,
    EventKind,
    classify_offer,
    dedupe_and_sort,
    in_horizon,
)
from investbot.channels.telegram.calendar import (
    MESSAGE_LIMIT,
    build_calendar_router,
    calendar_block,
    calendar_message,
    event_line,
    fmt_amount,
    plural,
)
from investbot.core.models import AssetClass, InstrumentMeta, PortfolioSnapshot, Position, PositionSource
from investbot.services.calendar import CalendarService, events_from_bondization

D = Decimal
TODAY = date(2026, 10, 9)  # пятница
EMPTY_TS = datetime(1970, 1, 1, tzinfo=UTC)  # пустая дата protobuf


def q(x: str) -> NS:
    d = Decimal(x)
    units = int(d)
    return NS(units=units, nano=int((d - units) * 1_000_000_000))


def m(x: str, cur: str = "rub") -> NS:
    v = q(x)
    v.currency = cur
    return v


def ts(d: date) -> datetime:
    return datetime.combine(d, time.min, tzinfo=UTC)


def day(n: int) -> date:
    return TODAY + timedelta(days=n)


class EvType(Enum):
    EVENT_TYPE_UNSPECIFIED = 0
    EVENT_TYPE_CPN = 1
    EVENT_TYPE_CALL = 2
    EVENT_TYPE_MTY = 3
    EVENT_TYPE_CONV = 4


def bond_event(when: date, t: EvType | None, pay: str = "0", value: str = "0", **kw: Any) -> NS:
    fields: dict[str, Any] = dict(
        event_date=ts(when), pay_one_bond=m(pay), value=q(value), note="", operation_type="", execution="",
        default_date=EMPTY_TS, real_pay_date=ts(when), coupon_interest_rate=q("0"),
    )
    if t is not None:
        fields["event_type"] = t
    fields.update(kw)
    return NS(**fields)


def ev(when: date, kind: EventKind, ticker: str = "AAAA", **kw: Any) -> CalendarEvent:
    return CalendarEvent(date=when, kind=kind, ticker=ticker, source=kw.pop("source", "tinvest"), **kw)


# --- маппинг GetBondEvents / GetDividends / GetLastPrices ----------------------------------------------------

def test_map_coupon_with_amount_and_unannounced() -> None:
    e = mapper.map_bond_event(bond_event(day(6), EvType.EVENT_TYPE_CPN, pay="35.4"), ticker="B", name="Выпуск")
    assert e is not None and e.kind is EventKind.COUPON and e.date == day(6)
    assert e.amount_per_unit == D("35.4") and e.currency == "rub" and e.source == "tinvest" and e.note == ""

    floater = mapper.map_bond_event(bond_event(day(6), EvType.EVENT_TYPE_CPN), ticker="B", name="")
    assert floater is not None and floater.amount_per_unit is None and "не объявлен" in floater.note


def test_map_offer_put_call_and_unknown_direction() -> None:
    put = mapper.map_bond_event(bond_event(day(20), EvType.EVENT_TYPE_CALL, value="100", note="Оферта (Put)"),
                                ticker="B", name="")
    call = mapper.map_bond_event(bond_event(day(20), EvType.EVENT_TYPE_CALL, operation_type="Call-опцион"),
                                 ticker="B", name="")
    unknown = mapper.map_bond_event(bond_event(day(20), EvType.EVENT_TYPE_CALL), ticker="B", name="")
    assert put is not None and put.kind is EventKind.PUT_OFFER and "цена выкупа 100% номинала" in put.note
    assert call is not None and call.kind is EventKind.CALL_OFFER
    # направление не указано — консервативно put (окно пропустить дороже), с пометкой
    assert unknown is not None and unknown.kind is EventKind.PUT_OFFER and "put или call" in unknown.note


def test_map_redemption_amortization_vs_maturity() -> None:
    maturity = day(300)
    partial = mapper.map_bond_event(bond_event(day(10), EvType.EVENT_TYPE_MTY, pay="500", value="50"),
                                    ticker="B", name="", maturity=maturity)
    early = mapper.map_bond_event(bond_event(day(10), EvType.EVENT_TYPE_MTY, pay="250"),
                                  ticker="B", name="", maturity=maturity)
    final = mapper.map_bond_event(bond_event(maturity, EvType.EVENT_TYPE_MTY, pay="1000", value="100"),
                                  ticker="B", name="", maturity=maturity)
    assert partial is not None and partial.kind is EventKind.AMORTIZATION and partial.amount_per_unit == 500
    assert early is not None and early.kind is EventKind.AMORTIZATION  # раньше даты погашения выпуска
    assert final is not None and final.kind is EventKind.MATURITY


def test_map_bond_event_type_hint_skips_and_default() -> None:
    # нет поля event_type в ответе — берётся тип из запроса
    hinted = mapper.map_bond_event(bond_event(day(3), None, pay="10"), ticker="B", name="",
                                   type_hint="EVENT_TYPE_CPN")
    assert hinted is not None and hinted.kind is EventKind.COUPON
    unspecified = mapper.map_bond_event(bond_event(day(3), EvType.EVENT_TYPE_UNSPECIFIED, pay="10"),
                                        ticker="B", name="", type_hint="EVENT_TYPE_CPN")
    assert unspecified is not None and unspecified.kind is EventKind.COUPON
    assert mapper.map_bond_event(bond_event(day(3), EvType.EVENT_TYPE_CONV), ticker="B", name="") is None
    assert mapper.map_bond_event(bond_event(day(3), EvType.EVENT_TYPE_CPN, event_date=EMPTY_TS),
                                 ticker="B", name="") is None
    defaulted = mapper.map_bond_event(
        bond_event(day(3), EvType.EVENT_TYPE_CPN, pay="10", default_date=ts(day(3)), real_pay_date=ts(day(5))),
        ticker="B", name="")
    assert defaulted is not None and defaulted.note.startswith("дефолт по выплате")
    assert "фактическая выплата 14.10.2026" in defaulted.note


def test_map_dividend() -> None:
    raw = NS(dividend_net=m("34.84"), record_date=ts(day(11)), last_buy_date=ts(day(10)),
             payment_date=ts(day(27)), declared_date=ts(day(-30)), dividend_type="Regular Cash")
    e = mapper.map_dividend(raw, ticker="SBER", name="Сбербанк")
    assert e is not None and e.kind is EventKind.DIVIDEND
    assert (e.date, e.last_buy_date, e.amount_per_unit) == (day(11), day(10), D("34.84"))
    assert e.note == "выплата 05.11.2026"

    cancelled = mapper.map_dividend(NS(dividend_net=m("0"), record_date=ts(day(11)), last_buy_date=EMPTY_TS,
                                       payment_date=EMPTY_TS, dividend_type="Cancelled"), ticker="X", name="")
    assert cancelled is not None and "отменён" in cancelled.note and cancelled.amount_per_unit is None
    assert cancelled.last_buy_date is None
    no_dates = NS(dividend_net=m("1"), record_date=EMPTY_TS, last_buy_date=None, payment_date=None)
    assert mapper.map_dividend(no_dates, ticker="X", name="") is None


def test_map_last_prices_converts_bonds_from_percent() -> None:
    meta = {
        "uid-b": InstrumentMeta(ticker="B", asset_class=AssetClass.BOND, nominal=D(1000)),
        "uid-s": InstrumentMeta(ticker="S", asset_class=AssetClass.EQUITY),
    }
    prices = mapper.map_last_prices([
        NS(instrument_uid="uid-b", price=q("98.5")),
        NS(instrument_uid="uid-s", price=q("150.25")),
        NS(instrument_uid="uid-zero", price=q("0")),
    ], meta)
    assert prices == {"uid-b": D("985"), "uid-s": D("150.25")}


# --- TInvestBroker как EventsPort на фейковом SDK ------------------------------------------------------------

class FakeSchemas:
    class AccountStatus:
        ACCOUNT_STATUS_OPEN = 2

    class InstrumentStatus:
        INSTRUMENT_STATUS_ALL = 2

    class LastPriceType(Enum):
        LAST_PRICE_EXCHANGE = 1

    class GetBondEventsRequest:
        EventType = EvType

        def __init__(self, **kw: Any) -> None:
            self.__dict__.update(kw)

    @staticmethod
    def GetAccountsRequest(**kw: Any) -> NS:
        return NS(**kw)

    @staticmethod
    def InstrumentsRequest(**kw: Any) -> NS:
        return NS(**kw)

    @staticmethod
    def GetDividendsRequest(**kw: Any) -> NS:
        return NS(**kw)

    @staticmethod
    def GetLastPricesRequest(**kw: Any) -> NS:
        return NS(**kw)


class AccType(Enum):
    ACCOUNT_TYPE_TINKOFF = 1


class Access(Enum):
    ACCOUNT_ACCESS_LEVEL_READ_ONLY = 2


def fake_sdk(calls: list[tuple[str, Any]]) -> NS:
    async def get_accounts(request: Any) -> NS:
        return NS(accounts=[NS(id="A1", name="Брокерский", type=AccType.ACCOUNT_TYPE_TINKOFF,
                               access_level=Access.ACCOUNT_ACCESS_LEVEL_READ_ONLY, opened_date=None)])

    share = NS(ticker="AAAA", name="Эмитент А", currency="rub", isin="RU1", uid="uid-s", class_code="TQBR", lot=10)
    bond = NS(ticker="RU000A10FLT1", name="ПАО Пример БО-01", currency="rub", isin="RU000A10FLT1", uid="uid-b",
              class_code="TQCB", lot=1, nominal=m("1000"), maturity_date=ts(day(400)), sector="financial",
              floating_coupon_flag=False, ratings=[])

    async def shares(request: Any) -> NS:
        return NS(instruments=[share])

    async def bonds(request: Any) -> NS:
        return NS(instruments=[bond])

    async def etfs(request: Any) -> NS:
        return NS(instruments=[])

    by_type = {
        EvType.EVENT_TYPE_CPN: [bond_event(day(6), EvType.EVENT_TYPE_CPN, pay="44.88")],
        EvType.EVENT_TYPE_CALL: [bond_event(day(20), EvType.EVENT_TYPE_CALL, value="100", note="Put")],
        EvType.EVENT_TYPE_MTY: [bond_event(day(25), EvType.EVENT_TYPE_MTY, pay="500", value="50")],
    }

    async def get_bond_events(request: Any) -> NS:
        calls.append(("get_bond_events", request))
        return NS(events=by_type[request.type])

    async def get_dividends(request: Any) -> NS:
        calls.append(("get_dividends", request))
        return NS(dividends=[NS(dividend_net=m("12.5"), record_date=ts(day(5)), last_buy_date=ts(day(4)),
                                payment_date=ts(day(20)), dividend_type="Regular Cash")])

    async def get_last_prices(request: Any) -> NS:
        calls.append(("get_last_prices", request))
        return NS(last_prices=[NS(instrument_uid=u, price=q("99")) for u in request.instrument_id])

    def trading(**_: Any) -> None:
        pytest.fail("торговый вызов")

    return NS(
        users=NS(get_accounts=get_accounts),
        instruments=NS(shares=shares, bonds=bonds, etfs=etfs, get_bond_events=get_bond_events,
                       get_dividends=get_dividends),
        market_data=NS(get_last_prices=get_last_prices),
        orders=NS(post_order=trading),
    )


@pytest.fixture
def sdk_calls() -> list[tuple[str, Any]]:
    return []


@pytest.fixture
def factory(sdk_calls: list[tuple[str, Any]]) -> Any:
    @asynccontextmanager
    async def make(token: str) -> Any:
        yield fake_sdk(sdk_calls)

    return make


async def test_broker_bond_events_one_request_per_type(factory: Any, sdk_calls: list[tuple[str, Any]]) -> None:
    async with TInvestBroker({"main": "t.fake"}, factory, FakeSchemas) as broker:
        events = await broker.bond_events("uid-b", TODAY, day(30))
    reqs = [r for name, r in sdk_calls if name == "get_bond_events"]
    assert [r.type for r in reqs] == [EvType.EVENT_TYPE_CPN, EvType.EVENT_TYPE_CALL, EvType.EVENT_TYPE_MTY]
    assert all(r.instrument_id == "uid-b" for r in reqs)
    assert reqs[0].from_ == datetime(2026, 10, 9, tzinfo=UTC)
    assert reqs[0].to == datetime(2026, 11, 8, 23, 59, 59, tzinfo=UTC)
    assert [(e.kind, e.ticker) for e in events] == [
        (EventKind.COUPON, "RU000A10FLT1"), (EventKind.PUT_OFFER, "RU000A10FLT1"),
        (EventKind.AMORTIZATION, "RU000A10FLT1"),
    ]
    assert events[0].name == "ПАО Пример БО-01" and events[0].amount_per_unit == D("44.88")


async def test_broker_dividends_and_last_prices(factory: Any, sdk_calls: list[tuple[str, Any]]) -> None:
    async with TInvestBroker({"main": "t.fake"}, factory, FakeSchemas) as broker:
        divs = await broker.dividends("uid-s", TODAY, day(30))
        prices = await broker.last_prices(["uid-b", "uid-s", "uid-b"])
        assert await broker.last_prices([]) == {}
    assert [(d.ticker, d.date, d.last_buy_date, d.amount_per_unit) for d in divs] == [
        ("AAAA", day(5), day(4), D("12.5"))
    ]
    lp = [r for name, r in sdk_calls if name == "get_last_prices"]
    assert len(lp) == 1 and lp[0].instrument_id == ["uid-b", "uid-s"]
    assert lp[0].last_price_type is FakeSchemas.LastPriceType.LAST_PRICE_EXCHANGE
    assert prices == {"uid-b": D("990"), "uid-s": D("99")}  # облигация: 99% от номинала 1000


# --- доменные правила: оферты, горизонт, дедупликация -------------------------------------------------------

def test_classify_offer() -> None:
    assert classify_offer("Оферта (Put)") is EventKind.PUT_OFFER
    assert classify_offer("Безотзывная оферта") is EventKind.PUT_OFFER
    assert classify_offer("Оферта (Call)") is EventKind.CALL_OFFER
    assert classify_offer("Колл-опцион") is EventKind.CALL_OFFER
    assert classify_offer("Оферта") is None


def test_in_horizon_put_offer_lookahead_and_dividend_last_buy() -> None:
    end = day(30)
    assert in_horizon(ev(end, EventKind.COUPON), TODAY, 30)
    assert not in_horizon(ev(end + timedelta(days=1), EventKind.COUPON), TODAY, 30)
    assert not in_horizon(ev(day(-1), EventKind.COUPON), TODAY, 30)
    # put-оферта за горизонтом без окна — показывается заранее
    assert in_horizon(ev(end + PUT_OFFER_LOOKAHEAD, EventKind.PUT_OFFER), TODAY, 30)
    assert not in_horizon(ev(end + PUT_OFFER_LOOKAHEAD + timedelta(days=1), EventKind.PUT_OFFER), TODAY, 30)
    # окно известно: решает начало окна
    assert in_horizon(ev(day(40), EventKind.PUT_OFFER, window_start=day(29), window_end=day(35)), TODAY, 30)
    assert not in_horizon(ev(day(40), EventKind.PUT_OFFER, window_start=day(31), window_end=day(35)), TODAY, 30)
    # отсечка сразу за горизонтом, но купить нужно в нём
    assert in_horizon(ev(day(31), EventKind.DIVIDEND, last_buy_date=day(30)), TODAY, 30)


def test_dedupe_merges_sources_and_sorts() -> None:
    events = [
        ev(day(5), EventKind.COUPON, "BBB", source="iss", amount_per_unit=D("10")),
        ev(day(5), EventKind.COUPON, "bbb", source="tinvest"),  # без суммы: проигрывает записи с суммой
        ev(day(2), EventKind.COUPON, "AAAA", amount_per_unit=D("1")),
        ev(day(2), EventKind.PUT_OFFER, "ZZZ"),
        ev(day(9), EventKind.PUT_OFFER, "CCC", source="tinvest"),
        ev(day(9), EventKind.PUT_OFFER, "CCC", source="iss", window_start=day(3), window_end=day(7)),
    ]
    out = dedupe_and_sort(events)
    assert [(e.date, e.kind, e.ticker) for e in out] == [
        (day(2), EventKind.PUT_OFFER, "ZZZ"),  # в пределах дня — сначала требующие действий
        (day(2), EventKind.COUPON, "AAAA"),
        (day(5), EventKind.COUPON, "BBB"),
        (day(9), EventKind.PUT_OFFER, "CCC"),
    ]
    assert out[2].source == "iss" and out[2].amount_per_unit == 10
    assert out[3].source == "tinvest" and (out[3].window_start, out[3].window_end) == (day(3), day(7))


def test_events_from_bondization_fixture_shape() -> None:
    bz = Bondization(
        secid="RU000A10FLT1",
        coupons=(
            Coupon(coupondate=day(5), recorddate=day(4), startdate=day(-86), value=D("42.38"), valueprc=D(17),
                   facevalue=D(1000)),
            Coupon(coupondate=day(96), recorddate=None, startdate=day(5), value=None, valueprc=None,
                   facevalue=D(1000)),
        ),
        amortizations=(
            Amortization(amortdate=date(2027, 12, 9), value=D(500), valueprc=D(50), facevalue=D(500)),
            Amortization(amortdate=date(2027, 6, 10), value=D(500), valueprc=D(50), facevalue=D(1000)),
        ),
        offers=(Offer(offerdate=date(2027, 3, 11), offertype="Оферта (Put)", price=D(100)),),
    )
    out = events_from_bondization(bz, ticker="RU000A10FLT1", name="Пример", currency="rub")
    kinds = [(e.date, e.kind) for e in out]
    assert (day(5), EventKind.COUPON) in kinds
    assert (date(2027, 6, 10), EventKind.AMORTIZATION) in kinds
    assert (date(2027, 12, 9), EventKind.MATURITY) in kinds
    offer = next(e for e in out if e.kind is EventKind.PUT_OFFER)
    assert offer.source == "iss" and "100% номинала" in offer.note and offer.window_start is None
    floater = next(e for e in out if e.date == day(96))
    assert floater.amount_per_unit is None and "не объявлен" in floater.note

    # окно подачи берётся, если модель ISS его отдаёт (колонки offerdatestart/offerdateend)
    duck = NS(coupons=(), amortizations=(), offers=(
        NS(offerdate=day(20), offertype="Оферта (Put)", price=None, offerdatestart=day(10), offerdateend=day(15)),
    ))
    (with_window,) = events_from_bondization(duck, ticker="X", name="")
    assert (with_window.window_start, with_window.window_end) == (day(10), day(15))


# --- сервис: источники, дедупликация, частичные сбои --------------------------------------------------------

def meta(ticker: str, cls: AssetClass, uid: str | None = None, **kw: Any) -> InstrumentMeta:
    return InstrumentMeta(ticker=ticker, name=kw.pop("name", f"Бумага {ticker}"), asset_class=cls,
                          tinvest_uid=uid, **kw)


def pos(account: str, m_: InstrumentMeta, source: PositionSource = "broker") -> Position:
    return Position(account_id=account, instrument=m_, qty=D(10), price=D(100), source=source)


class FakePortfolio:
    def __init__(self, positions: list[Position], fail: bool = False) -> None:
        self.positions = positions
        self.fail = fail

    async def snapshot(self) -> tuple[PortfolioSnapshot, dict[str, Decimal]]:
        if self.fail:
            raise RuntimeError("снимок недоступен")
        return PortfolioSnapshot(taken_at=datetime.now(UTC), accounts=(), positions=tuple(self.positions),
                                 cash=()), {"rub": D(1)}


class FakeEvents:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, date, date]] = []

    async def bond_events(self, uid: str, frm: date, to: date) -> list[CalendarEvent]:
        self.calls.append(("bond", uid, frm, to))
        if uid == "uid-x":
            raise RuntimeError("UNAVAILABLE")
        raw = dict(ticker=uid, source="tinvest")  # адаптер не знает тикер — сервис подставит тикер позиции
        return [
            CalendarEvent(date=day(6), kind=EventKind.COUPON, amount_per_unit=D("35.4"), **raw),
            CalendarEvent(date=day(6), kind=EventKind.COUPON, amount_per_unit=D("35.4"), **raw),  # дубль API
            CalendarEvent(date=day(40), kind=EventKind.PUT_OFFER, **raw),  # за горизонтом, но в запасе
            CalendarEvent(date=day(80), kind=EventKind.COUPON, amount_per_unit=D("35.4"), **raw),
        ]

    async def dividends(self, uid: str, frm: date, to: date) -> list[CalendarEvent]:
        self.calls.append(("div", uid, frm, to))
        return [CalendarEvent(date=day(11), kind=EventKind.DIVIDEND, ticker=uid, amount_per_unit=D("34.84"),
                              last_buy_date=day(10), source="tinvest")]

    async def last_prices(self, uids: list[str]) -> dict[str, Decimal]:
        return {}


class FakeIss:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def __call__(self, secid: str) -> Bondization:
        self.calls.append(secid)
        if secid == "RU000ABROKEN":
            raise RuntimeError("503")
        return Bondization(
            secid=secid,
            coupons=(Coupon(coupondate=day(12), recorddate=None, startdate=None, value=D("36.9"), valueprc=None,
                            facevalue=D(1000)),),
            amortizations=(Amortization(amortdate=day(700), value=D(1000), valueprc=D(100), facevalue=D(1000)),),
        )


def portfolio_positions() -> list[Position]:
    bond = meta("RU000A10FLT1", AssetClass.BOND, "uid-b", name="ПАО Пример БО-01")
    return [
        pos("A1", bond),
        pos("A2", bond),  # та же бумага на втором счёте — один запрос
        pos("A1", meta("SBER", AssetClass.EQUITY, "uid-s", name="Сбербанк")),
        pos("A1", meta("RU000AXXX", AssetClass.BOND, "uid-x")),  # T-Invest упал → резерв ISS
        pos("M1", meta("SU26238RMFS4", AssetClass.BOND, name="ОФЗ 26238"), "manual"),
        pos("M1", meta("RU000ABROKEN", AssetClass.BOND, maturity=day(20)), "manual"),  # ISS упал → погашение
        pos("M1", meta("MANL", AssetClass.EQUITY), "manual"),
        pos("D1", meta("DFA-1", AssetClass.DFA, maturity=day(15), nominal=D(1000)), "dfa_card"),
        pos("D1", meta("DFA-2", AssetClass.DFA, maturity=day(200), nominal=D(1000)), "dfa_card"),
        pos("D1", meta("DFA-3", AssetClass.DFA), "dfa_card"),
    ]


async def test_service_collects_all_sources_and_survives_failures() -> None:
    events, iss = FakeEvents(), FakeIss()
    service = CalendarService(FakePortfolio(portfolio_positions()), events, iss)
    result = await service.upcoming(TODAY, 30)

    assert result.today == TODAY and result.days == 30
    assert [c[:2] for c in events.calls if c[0] == "bond"] == [("bond", "uid-b"), ("bond", "uid-x")]
    assert all(c[2] == TODAY and c[3] == day(30) + PUT_OFFER_LOOKAHEAD for c in events.calls)
    assert sorted(iss.calls) == ["RU000ABROKEN", "RU000AXXX", "SU26238RMFS4"]

    got = [(e.date, e.kind, e.ticker, e.source) for e in result.events]
    assert got == [
        (day(6), EventKind.COUPON, "RU000A10FLT1", "tinvest"),
        (day(11), EventKind.DIVIDEND, "SBER", "tinvest"),
        (day(12), EventKind.COUPON, "RU000AXXX", "iss"),
        (day(12), EventKind.COUPON, "SU26238RMFS4", "iss"),
        (day(15), EventKind.DFA_MATURITY, "DFA-1", "manual"),
        (day(20), EventKind.MATURITY, "RU000ABROKEN", "manual"),
        (day(40), EventKind.PUT_OFFER, "RU000A10FLT1", "tinvest"),
    ]
    assert result.events == tuple(sorted(result.events, key=lambda e: e.date))
    assert result.events[0].name == "ПАО Пример БО-01"  # тикер и имя — из позиции
    assert result.events[4].amount_per_unit == D(1000)

    warnings = "\n".join(result.warnings)
    assert "RU000AXXX: Т-Инвестиции не отдали события облигации (RuntimeError) — показаны данные MOEX ISS" in warnings
    assert "RU000ABROKEN: MOEX ISS не отдал купоны и оферты (RuntimeError)" in warnings
    assert "ЦФА DFA-3: в карточке не указана дата погашения" in warnings
    assert "MANL" in warnings and "дивиденды не отслеживаются" in warnings


async def test_service_without_iss_and_with_failing_dividends() -> None:
    class BrokenDividends(FakeEvents):
        async def dividends(self, uid: str, frm: date, to: date) -> list[CalendarEvent]:
            raise TimeoutError

    positions = [
        pos("A1", meta("SBER", AssetClass.EQUITY, "uid-s")),
        pos("M1", meta("SU26238RMFS4", AssetClass.BOND, maturity=day(3)), "manual"),
        pos("A1", meta("RU000AXXX", AssetClass.BOND, "uid-x", maturity=day(4))),
    ]
    result = await CalendarService(FakePortfolio(positions), BrokenDividends(), None).upcoming(TODAY, 7)
    assert [(e.ticker, e.kind, e.source) for e in result.events] == [
        ("SU26238RMFS4", EventKind.MATURITY, "manual"),
        ("RU000AXXX", EventKind.MATURITY, "tinvest"),
    ]
    assert any("SBER: Т-Инвестиции не отдали дивиденды (TimeoutError)" in w for w in result.warnings)
    assert any("SU26238RMFS4: купоны и оферты не отслеживаются" in w for w in result.warnings)


async def test_service_propagates_snapshot_failure() -> None:
    with pytest.raises(RuntimeError, match="снимок"):
        await CalendarService(FakePortfolio([], fail=True), FakeEvents(), FakeIss()).upcoming(TODAY)


# --- рендер ---------------------------------------------------------------------------------------------------

def test_formatting_helpers() -> None:
    assert fmt_amount(D("35.4"), "rub") == "35,40 ₽"
    assert fmt_amount(D("1000"), "cny") == "1 000,00 ¥"
    assert fmt_amount(D("0.0042"), "usd") == "0,0042 $"
    assert [plural(n, "событие", "события", "событий") for n in (1, 2, 5, 11, 21, 104)] == [
        "событие", "события", "событий", "событий", "событие", "события"]


def test_calendar_message_lines_and_escaping() -> None:
    events = (
        ev(TODAY, EventKind.COUPON, "SU26238RMFS4", name="ОФЗ <26238>", amount_per_unit=D("35.4")),
        ev(day(2), EventKind.DIVIDEND, "SBER", name="Сбербанк", amount_per_unit=D("34.84"),
           last_buy_date=day(1)),
        ev(day(9), EventKind.PUT_OFFER, "RU000A10FLT1", window_start=day(3), window_end=day(7),
           note="цена выкупа 100% номинала", source="iss"),
        ev(day(20), EventKind.PUT_OFFER, "RU000AYYY"),
        ev(day(12), EventKind.CALL_OFFER, "RU000AZZZ"),
        ev(day(15), EventKind.DFA_MATURITY, "DFA-1", amount_per_unit=D(1000), source="manual"),
        ev(day(25), EventKind.AMORTIZATION, "RU000AAMRT", amount_per_unit=D(500)),
    )
    result = CalendarResult(today=TODAY, days=30, events=events, warnings=("ISS <503> & timeout",))
    text = calendar_message(result)
    assert "<b>Пт, 09.10</b> · сегодня" in text and "<b>Сб, 10.10</b> · завтра" not in text
    assert "💰 <b>SU26238RMFS4</b> · ОФЗ &lt;26238&gt; — купон 35,40 ₽ на облигацию" in text
    assert "💵 <b>SBER</b> · Сбербанк — дивиденд 34,84 ₽ на бумагу, отсечка · купить до 10.10 включительно" in text
    assert "⚠️ <b>RU000A10FLT1</b> — оферта: подать заявку в окне 12.10–16.10 <i>(цена выкупа" in text
    assert "оферта: подать заявку в окне до даты оферты" in text
    assert "📞 <b>RU000AZZZ</b> — колл-оферта" in text and "🪙 <b>DFA-1</b>" in text and "🧩" in text
    assert "⚠️ Требуют действий: 2 (оферты)" in text
    assert "ISS &lt;503&gt; &amp; timeout" in text
    assert "Не инвестиционная рекомендация" in text
    assert "<26238>" not in text


def test_calendar_message_dividend_last_buy_passed_and_empty() -> None:
    passed = ev(day(1), EventKind.DIVIDEND, "SBER", last_buy_date=day(-1))
    text = calendar_message(CalendarResult(today=TODAY, days=30, events=(passed,)))
    assert "последний день покупки прошёл (08.10)" in text and "купить до" not in text
    empty = calendar_message(CalendarResult(today=TODAY, days=30))
    assert "Событий в ближайшие 30 дней нет." in empty


def test_calendar_message_is_capped_with_more_counter() -> None:
    events = tuple(
        ev(day(i % 30), EventKind.COUPON, f"RU000A{i:06d}", name="Очень длинное название выпуска " * 5,
           amount_per_unit=D("12.34"), note="примечание " * 30)
        for i in range(300)
    )
    warnings = tuple(f"источник {i} недоступен" for i in range(12))
    text = calendar_message(CalendarResult(today=TODAY, days=30, events=dedupe_and_sort(events),
                                           warnings=warnings))
    assert len(text) <= MESSAGE_LIMIT < 4096
    assert "…и ещё" in text and "событий" in text.split("…и ещё")[1].splitlines()[0]
    assert "• …и ещё 7" in text  # предупреждений не больше пяти
    assert text.rstrip().endswith("</i>")  # дисклеймер не обрезан


def test_calendar_block_seven_days_compact() -> None:
    events = [
        ev(day(1), EventKind.COUPON, "B", amount_per_unit=D("10")),
        ev(day(1), EventKind.COUPON, "B", amount_per_unit=D("10")),  # дубль
        ev(day(7), EventKind.MATURITY, "C"),
        ev(day(8), EventKind.COUPON, "D"),  # за горизонтом недели
        ev(day(-1), EventKind.COUPON, "E"),  # прошло
    ]
    block = calendar_block(events, TODAY)
    assert block.startswith("<b>🗓 События на 7 дней</b>")
    assert block.count("<b>B</b>") == 1 and "<b>C</b>" in block
    assert "<b>D</b>" not in block and "<b>E</b>" not in block
    assert "\n\n" not in block and "Не инвестиционная" not in block
    assert calendar_block([], TODAY).endswith("Событий по бумагам портфеля нет.")
    many = [ev(day(i % 7), EventKind.COUPON, f"T{i}") for i in range(200)]
    capped = calendar_block(many, TODAY, limit=600)
    assert len(capped) <= 600 and "…и ещё" in capped


def test_event_line_has_no_quantities() -> None:
    line = event_line(ev(day(3), EventKind.COUPON, "B", amount_per_unit=D("35.4")), TODAY)
    assert "шт" not in line and "лот" not in line and "на облигацию" in line


# --- роутер ---------------------------------------------------------------------------------------------------

class FakeMessage:
    def __init__(self) -> None:
        self.answers: list[str] = []

    async def answer(self, text: str, **_: Any) -> None:
        self.answers.append(text)


async def test_router_answers_calendar_and_reports_failure() -> None:
    seen: list[tuple[date, int]] = []

    class OkService:
        async def upcoming(self, today: date, days: int = 30) -> CalendarResult:
            seen.append((today, days))
            return CalendarResult(today=today, days=days,
                                  events=(ev(today, EventKind.COUPON, "B", amount_per_unit=D(1)),))

    class FailingService:
        async def upcoming(self, today: date, days: int = 30) -> CalendarResult:
            raise ConnectionError("down")

    router = build_calendar_router(OkService(), today=lambda: TODAY)
    handler = router.message.handlers[0].callback
    msg = FakeMessage()
    await handler(msg)
    assert seen == [(TODAY, 30)] and "<b>B</b>" in msg.answers[0]

    failing = build_calendar_router(FailingService(), today=lambda: TODAY).message.handlers[0].callback
    msg = FakeMessage()
    await failing(msg)
    assert msg.answers == ["Не удалось собрать календарь: ConnectionError. Подробности — /status"]
