"""Адаптер Т-Инвестиций без SDK: фейковые объекты повторяют форму ответов контрактов 1.51."""

from __future__ import annotations

import contextlib
import os
from contextlib import asynccontextmanager
from decimal import Decimal
from enum import Enum
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Any

import pytest

from investbot.adapters.tinvest import mapper
from investbot.adapters.tinvest.broker import TInvestBroker
from investbot.adapters.tinvest.guard import ReadOnlyClientGuard, TradingAccessError, check_read_only
from investbot.core.models import AccessLevel, Account, AccountKind, AccountSource, AssetClass, CouponKind


class AccType(Enum):
    ACCOUNT_TYPE_TINKOFF = 1
    ACCOUNT_TYPE_TINKOFF_IIS = 2
    ACCOUNT_TYPE_DFA = 7
    ACCOUNT_TYPE_UNSPECIFIED_BANK = 99


class Access(Enum):
    ACCOUNT_ACCESS_LEVEL_FULL_ACCESS = 1
    ACCOUNT_ACCESS_LEVEL_READ_ONLY = 2
    ACCOUNT_ACCESS_LEVEL_NO_ACCESS = 3


def q(x: str) -> NS:
    d = Decimal(x)
    units = int(d)
    return NS(units=units, nano=int((d - units) * 1_000_000_000))


def m(x: str, cur: str = "rub") -> NS:
    v = q(x)
    v.currency = cur
    return v


def acc(id_: str, t: AccType, access: Access) -> NS:
    return NS(id=id_, name=f"Счёт {id_}", type=t, access_level=access, opened_date=None)


def test_quotation_and_money() -> None:
    assert mapper.quotation(NS(units=114, nano=250_000_000)) == Decimal("114.25")
    assert mapper.quotation(None) == 0
    assert mapper.money(m("12.5", "CNY")) == (Decimal("12.5"), "cny")


def test_map_account_kinds_and_access() -> None:
    a = mapper.map_account(acc("1", AccType.ACCOUNT_TYPE_TINKOFF_IIS, Access.ACCOUNT_ACCESS_LEVEL_READ_ONLY))
    assert a.kind is AccountKind.IIS and a.access is AccessLevel.READ_ONLY and a.source is AccountSource.TINVEST
    b = mapper.map_account(acc("2", AccType.ACCOUNT_TYPE_UNSPECIFIED_BANK, Access.ACCOUNT_ACCESS_LEVEL_NO_ACCESS))
    assert b.kind is AccountKind.OTHER and b.access is AccessLevel.NO_ACCESS


def _acc(kind: AccountKind, access: AccessLevel, id_: str = "x") -> Account:
    return Account(account_id=id_, name=id_, kind=kind, source=AccountSource.TINVEST, access=access)


def test_guard_rejects_full_access_anywhere() -> None:
    with pytest.raises(TradingAccessError):
        check_read_only("main", [_acc(AccountKind.BROKER, AccessLevel.READ_ONLY, "a"),
                                 _acc(AccountKind.IIS, AccessLevel.FULL, "b")])


def test_guard_rejects_unknown_level_on_invest_account() -> None:
    with pytest.raises(TradingAccessError):
        check_read_only("main", [_acc(AccountKind.BROKER, AccessLevel.UNKNOWN)])


def test_guard_ignores_bank_accounts_without_access() -> None:
    readable = check_read_only("main", [
        _acc(AccountKind.BROKER, AccessLevel.READ_ONLY, "a"),
        _acc(AccountKind.OTHER, AccessLevel.NO_ACCESS, "bank"),
        _acc(AccountKind.OTHER, AccessLevel.UNKNOWN, "bank2"),
        _acc(AccountKind.DFA, AccessLevel.NO_ACCESS, "dfa"),
    ])
    assert [a.account_id for a in readable] == ["a"]


@pytest.mark.parametrize("service", ["orders", "stop_orders", "sandbox"])
def test_client_guard_blocks_trading_services(service: str) -> None:
    guard = ReadOnlyClientGuard(NS(orders=object(), stop_orders=object(), sandbox=object(), users="ok"))
    with pytest.raises(PermissionError):
        getattr(guard, service)
    assert guard.users == "ok"


def test_map_bond_ofz_kinds_and_rating() -> None:
    base = dict(name="ОФЗ", currency="rub", isin="RU000", uid="u", class_code="TQOB", lot=1,
                nominal=m("1000"), maturity_date=None, for_qual_investor_flag=False)
    fixed = mapper.map_bond(NS(ticker="SU26238RMFS4", sector="government", floating_coupon_flag=False,
                               ratings=[], **base))
    floater = mapper.map_bond(NS(ticker="SU29014RMFS6", sector="government", floating_coupon_flag=True,
                                 ratings=[], **base))
    linker = mapper.map_bond(NS(ticker="SU52005RMFS4", sector="government", floating_coupon_flag=False,
                                ratings=[], **base))
    corp = mapper.map_bond(NS(ticker="RU000A0", sector="financial", floating_coupon_flag=False,
                              ratings=[NS(rating_level="ruAA"), NS(rating_level="A+(RU)")], **base))
    assert fixed.is_government and fixed.coupon_kind is CouponKind.FIXED
    assert floater.coupon_kind is CouponKind.FLOATING
    assert linker.coupon_kind is CouponKind.LINKER
    assert not corp.is_government and corp.rating == "A+"  # берётся худший рейтинг
    assert fixed.nominal == Decimal(1000)


def test_bond_price_in_percent_is_normalized_with_warning() -> None:
    meta = mapper.map_bond(NS(ticker="SU26238RMFS4", name="ОФЗ", currency="rub", isin=None, uid="u",
                              class_code="TQOB", lot=1, nominal=m("1000"), maturity_date=None,
                              sector="government", floating_coupon_flag=False, ratings=[]))
    price, warn = mapper.normalize_bond_price(Decimal("61.5"), meta)
    assert price == Decimal("615") and warn
    price, warn = mapper.normalize_bond_price(Decimal("615.0"), meta)
    assert price == Decimal("615.0") and warn is None


def test_fx_from_portfolio_currency_positions() -> None:
    positions = [
        NS(instrument_type="currency", ticker="CNYRUB_TOM", current_price=m("11.2")),
        NS(instrument_type="currency", ticker="RUB000UTSTOM", current_price=m("1")),
        NS(instrument_type="share", ticker="AAAA", current_price=m("100")),
    ]
    assert mapper.fx_from_portfolio(positions) == {"rub": Decimal(1), "cny": Decimal("11.2")}


# --- TInvestBroker на фейковом SDK ---------------------------------------------------------------

class FakeSchemas:
    class AccountStatus:
        ACCOUNT_STATUS_OPEN = 2

    class InstrumentStatus:
        INSTRUMENT_STATUS_ALL = 2

    class PortfolioRequest:
        class CurrencyRequest:
            RUB = 0

        def __init__(self, **kw: Any) -> None:
            self.__dict__.update(kw)

    @staticmethod
    def GetAccountsRequest(**kw: Any) -> NS:
        return NS(**kw)

    @staticmethod
    def InstrumentsRequest(**kw: Any) -> NS:
        return NS(**kw)

    @staticmethod
    def PositionsRequest(**kw: Any) -> NS:
        return NS(**kw)

    @staticmethod
    def GetInfoRequest(**kw: Any) -> NS:
        return NS(**kw)


def fake_services(access: Access) -> NS:
    async def get_accounts(request: Any) -> NS:
        return NS(accounts=[acc("A1", AccType.ACCOUNT_TYPE_TINKOFF, access),
                            acc("bank", AccType.ACCOUNT_TYPE_UNSPECIFIED_BANK, Access.ACCOUNT_ACCESS_LEVEL_NO_ACCESS)])

    share = NS(ticker="AAAA", name="Эмитент А", currency="rub", isin="RU1", uid="uid-a", class_code="TQBR", lot=10)

    async def shares(request: Any) -> NS:
        return NS(instruments=[share])

    async def empty(request: Any) -> NS:
        return NS(instruments=[])

    async def get_portfolio(request: Any) -> NS:
        return NS(
            positions=[
                NS(instrument_uid="uid-a", ticker="AAAA", instrument_type="share", class_code="TQBR",
                   quantity=q("20"), current_price=m("150"), current_nkd=None, average_position_price=m("120"),
                   blocked_lots=q("0")),
                NS(instrument_uid="uid-cny", ticker="CNYRUB_TOM", instrument_type="currency", class_code="CETS",
                   quantity=q("100"), current_price=m("11"), current_nkd=None, average_position_price=None,
                   blocked_lots=None),
            ],
            total_amount_portfolio=m("5100"),
            total_amount_dfa=m("0"),
        )

    async def get_positions(request: Any) -> NS:
        return NS(money=[m("2000"), m("100", "cny")])

    return NS(
        users=NS(get_accounts=get_accounts),
        instruments=NS(shares=shares, bonds=empty, etfs=empty),
        operations=NS(get_portfolio=get_portfolio, get_positions=get_positions),
        orders=NS(post_order=lambda **_: pytest.fail("торговый вызов")),
    )


def factory_for(access: Access) -> Any:
    @asynccontextmanager
    async def factory(token: str) -> Any:
        yield fake_services(access)

    return factory


async def test_broker_reads_portfolio_read_only() -> None:
    async with TInvestBroker({"main": "t.fake"}, factory_for(Access.ACCOUNT_ACCESS_LEVEL_READ_ONLY),
                             FakeSchemas) as broker:
        accounts = await broker.accounts()
        assert [a.account_id for a in accounts] == ["A1"]
        p = await broker.portfolio("A1")
    assert len(p.positions) == 1  # валютная позиция ушла в кэш, а не в бумаги
    pos = p.positions[0]
    assert pos.instrument.asset_class is AssetClass.EQUITY and pos.instrument.lot == 10
    assert pos.qty == 20 and pos.price == 150 and pos.value == 3000
    assert {c.currency: c.amount for c in p.cash} == {"rub": Decimal(2000), "cny": Decimal(100)}
    assert p.fx_rates["cny"] == Decimal(11)


async def test_broker_refuses_full_access_token() -> None:
    with pytest.raises(TradingAccessError):
        async with TInvestBroker({"main": "t.fake"}, factory_for(Access.ACCOUNT_ACCESS_LEVEL_FULL_ACCESS),
                                 FakeSchemas):
            pass


class OpType(Enum):
    OPERATION_TYPE_BUY = 15
    OPERATION_TYPE_SELL = 22
    OPERATION_TYPE_COUPON = 23
    OPERATION_TYPE_INPUT_SECURITIES = 9


def test_map_trade_op_kinds_qty_commission() -> None:
    from datetime import UTC, datetime

    meta = {"uid-a": mapper.map_share(NS(ticker="AAAA", name="А", currency="rub", isin=None, uid="uid-a",
                                         class_code="TQBR", lot=10))}
    when = datetime(2023, 5, 1, 10, tzinfo=UTC)
    buy = mapper.map_trade_op("B", NS(type=OpType.OPERATION_TYPE_BUY, quantity=20, date=when, instrument_uid="uid-a",
                                      price=m("150"), commission=m("-4.5")), meta)
    assert buy is not None and buy.kind == "buy" and buy.ticker == "AAAA" and buy.qty == 20
    assert buy.price == Decimal(150) and buy.commission == Decimal("4.5")
    transfer = mapper.map_trade_op("B", NS(type=OpType.OPERATION_TYPE_INPUT_SECURITIES, quantity=5, date=when,
                                           instrument_uid="uid-a", price=m("0"), commission=None), meta)
    assert transfer is not None and transfer.kind == "transfer_in" and transfer.price is None
    coupon = mapper.map_trade_op("B", NS(type=OpType.OPERATION_TYPE_COUPON, quantity=0, date=when,
                                         instrument_uid="uid-a", price=m("0"), commission=None), meta)
    assert coupon is None


async def test_broker_trade_ops_pages_through_cursor() -> None:
    from datetime import UTC, datetime

    when = datetime(2023, 5, 1, 10, tzinfo=UTC)
    pages = {
        "": NS(items=[NS(type=OpType.OPERATION_TYPE_BUY, quantity=10, date=when, instrument_uid="uid-a",
                         price=m("100"), commission=m("-1"))], has_next=True, next_cursor="c1"),
        "c1": NS(items=[NS(type=OpType.OPERATION_TYPE_SELL, quantity=5, date=when, instrument_uid="uid-a",
                           price=m("120"), commission=m("-1"))], has_next=False, next_cursor=""),
    }

    class OpsSchemas(FakeSchemas):
        OperationType = OpType

        @staticmethod
        def GetOperationsByCursorRequest(**kw: Any) -> NS:
            return NS(**kw)

    services = fake_services(Access.ACCOUNT_ACCESS_LEVEL_READ_ONLY)

    async def get_operations_by_cursor(request: Any) -> NS:
        assert OpType.OPERATION_TYPE_COUPON not in request.operation_types
        return pages[request.cursor]

    services.operations.get_operations_by_cursor = get_operations_by_cursor

    @asynccontextmanager
    async def factory(token: str) -> Any:
        yield services

    async with TInvestBroker({"main": "t.fake"}, factory, OpsSchemas) as broker:
        ops = await broker.trade_ops("A1", datetime(2014, 1, 1, tzinfo=UTC))
    assert [(o.kind, o.qty, o.ticker) for o in ops] == [("buy", 10, "AAAA"), ("sell", 5, "AAAA")]


def test_sdk_uses_only_our_ca_bundle(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # встроенный в SDK certs/RussianTrustedRootCA.pem не используется: корни — только из нашего bundle
    from investbot.adapters.tinvest.broker import SdkNotInstalled, sdk_client_factory

    bundle = tmp_path / "ca-bundle.pem"
    monkeypatch.delenv("GRPC_DEFAULT_SSL_ROOTS_FILE_PATH", raising=False)
    monkeypatch.setenv("SSL_TBANK_VERIFY", "true")
    with contextlib.suppress(SdkNotInstalled):
        sdk_client_factory("investbot", bundle)
    assert os.environ["GRPC_DEFAULT_SSL_ROOTS_FILE_PATH"] == str(bundle)
    assert os.environ["SSL_TBANK_VERIFY"] == "false"
