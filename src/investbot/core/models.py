"""Доменные модели. Не зависят от SDK брокера, MOEX и Telegram (см. .importlinter)."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from investbot.core.money import ZERO


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AssetClass(StrEnum):
    EQUITY = "equity"
    BOND = "bond"
    FUND = "fund"
    DFA = "dfa"
    CURRENCY = "currency"
    OTHER = "other"


class AccountKind(StrEnum):
    BROKER = "broker"
    IIS = "iis"
    DFA = "dfa"  # смарт-счёт ЦФА: позиции API не отдаёт, только total_amount_dfa
    INVEST_BOX = "invest_box"  # Инвесткопилка: GetPortfolio не работает
    INVEST_FUND = "invest_fund"
    OTHER = "other"


class AccountSource(StrEnum):
    TINVEST = "tinvest"  # количества и кэш — только из брокерского снимка (ADR-06)
    MANUAL = "manual"  # другие брокеры, ЦФА других операторов, режим tinvest_sync: off


class AccessLevel(StrEnum):
    READ_ONLY = "read_only"
    FULL = "full"
    NO_ACCESS = "no_access"
    UNKNOWN = "unknown"


class Account(Strict):
    account_id: str
    name: str
    kind: AccountKind
    source: AccountSource
    access: AccessLevel = AccessLevel.READ_ONLY
    opened: date | None = None


class CouponKind(StrEnum):
    FIXED = "fixed"
    FLOATING = "floating"  # флоатеры (RUONIA, КС)
    LINKER = "linker"  # номинал индексируется на инфляцию
    UNKNOWN = "unknown"


class FundFocus(StrEnum):
    EQUITY = "equity"
    FIXED_INCOME = "fixed_income"
    MONEY_MARKET = "money_market"
    GOLD = "gold"
    MIXED = "mixed"
    OTHER = "other"


class InstrumentMeta(Strict):
    """Справочные атрибуты, нужные для отнесения бумаги к бакету IPS."""

    ticker: str
    name: str = ""
    asset_class: AssetClass
    currency: str = "rub"
    isin: str | None = None
    tinvest_uid: str | None = None
    class_code: str | None = None
    lot: int = 1
    # облигации
    is_government: bool = False  # ОФЗ
    coupon_kind: CouponKind = CouponKind.UNKNOWN
    rating: str | None = None  # минимальный из рейтингов агентств, например "AA-"
    nominal: Decimal | None = None
    maturity: date | None = None
    # фонды
    fund_focus: FundFocus | None = None
    # доступ
    for_qual_only: bool = False


PositionSource = Literal["broker", "manual", "dfa_card"]


class Position(Strict):
    account_id: str
    instrument: InstrumentMeta
    qty: Decimal
    price: Decimal | None  # за 1 шт. в валюте инструмента (у облигаций — в валюте, не в % номинала)
    accrued: Decimal = ZERO  # НКД на 1 шт.
    avg_price: Decimal | None = None
    blocked_qty: Decimal = ZERO
    source: PositionSource = "broker"
    price_as_of: datetime | None = None

    @property
    def value(self) -> Decimal | None:
        """Стоимость в валюте инструмента, включая НКД."""
        if self.price is None:
            return None
        return self.qty * (self.price + self.accrued)


class CashBalance(Strict):
    account_id: str
    currency: str
    amount: Decimal


class PortfolioSnapshot(Strict):
    taken_at: datetime
    accounts: tuple[Account, ...]
    positions: tuple[Position, ...]
    cash: tuple[CashBalance, ...]
    warnings: tuple[str, ...] = ()


class Fact(Strict):
    """Число или дата, на которые разрешено ссылаться LLM (ADR-02). Всё считается кодом."""

    fact_id: str
    value: Decimal | str | date
    unit: Literal["pct", "rub", "bp", "years", "pp", "count", "text", "date"]
    as_of: datetime
    source: str


class UserInfo(Strict):
    qualified: bool
    qualified_for: tuple[str, ...] = ()
    tariff: str | None = None


class BrokerPortfolio(Strict):
    account_id: str
    positions: tuple[Position, ...]
    cash: tuple[CashBalance, ...]
    total_rub: Decimal | None = None
    total_dfa_rub: Decimal | None = Field(default=None, description="сумма на смарт-счёте ЦФА")
    fx_rates: dict[str, Decimal] = Field(default_factory=dict, description="курсы к рублю из валютных позиций")
