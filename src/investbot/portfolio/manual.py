"""Ручные счета: другие брокеры, ЦФА (смарт-счёт Т-Банка и другие операторы), режим без токена.

ADR-06: по счетам Т-Инвестиций с подключённым токеном количества берутся только из API,
ручная позиция на таком счёте — ошибка валидации (иначе двойной учёт).
Хранилище — YAML (`config/manual_portfolio.yaml`, не в git); с появлением Postgres переедет в БД.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field

from investbot.core.models import (
    Account,
    AccountKind,
    AccountSource,
    AssetClass,
    CashBalance,
    CouponKind,
    FundFocus,
    InstrumentMeta,
    Position,
    Strict,
)
from investbot.core.money import ZERO


class ManualPosition(Strict):
    ticker: str
    name: str = ""
    asset_class: AssetClass
    qty: Decimal = Field(gt=0)
    price: Decimal | None = Field(default=None, description="за 1 шт. в валюте; у облигаций — в валюте, не в %")
    price_pct: Decimal | None = Field(default=None, description="альтернатива для облигаций: % номинала")
    nominal: Decimal | None = None
    accrued: Decimal = ZERO
    currency: str = "rub"
    price_date: date | None = None
    # атрибуты для бакетов
    is_government: bool = False
    coupon_kind: CouponKind = CouponKind.UNKNOWN
    rating: str | None = None
    fund_focus: FundFocus | None = None
    # ЦФА
    operator: str | None = Field(default=None, description="оператор ИС (лимит 7176-У считается по оператору)")
    maturity: date | None = None

    def unit_price(self) -> Decimal | None:
        if self.price is not None:
            return self.price
        if self.price_pct is not None and self.nominal is not None:
            return self.price_pct * self.nominal / 100
        if self.asset_class is AssetClass.DFA and self.nominal is not None:
            return self.nominal  # ЦФА с фиксированными выплатами: модельная цена = номинал (§7.5)
        return None

    def to_position(self, account_id: str) -> Position:
        meta = InstrumentMeta(
            ticker=self.ticker, name=self.name or self.ticker, asset_class=self.asset_class,
            currency=self.currency.lower(), is_government=self.is_government, coupon_kind=self.coupon_kind,
            rating=self.rating, nominal=self.nominal, maturity=self.maturity, fund_focus=self.fund_focus,
        )
        as_of = datetime.combine(self.price_date, datetime.min.time(), UTC) if self.price_date else None
        return Position(
            account_id=account_id, instrument=meta, qty=self.qty, price=self.unit_price(),
            accrued=self.accrued, source="dfa_card" if self.asset_class is AssetClass.DFA else "manual",
            price_as_of=as_of,
        )


class ManualAccount(Strict):
    id: str
    name: str
    kind: AccountKind = AccountKind.BROKER
    tinvest_account_id: str | None = Field(
        default=None, description="заполняется только для смарт-счёта ЦФА Т-Банка: сумма сверяется с API"
    )
    positions: tuple[ManualPosition, ...] = ()
    cash: dict[str, Decimal] = Field(default_factory=dict)


class ManualPortfolio(Strict):
    accounts: tuple[ManualAccount, ...] = ()

    def domain_accounts(self) -> list[Account]:
        return [
            Account(account_id=a.id, name=a.name, kind=a.kind, source=AccountSource.MANUAL)
            for a in self.accounts
        ]

    def positions(self) -> list[Position]:
        return [p.to_position(a.id) for a in self.accounts for p in a.positions]

    def cash(self) -> list[CashBalance]:
        return [
            CashBalance(account_id=a.id, currency=cur.lower(), amount=amt)
            for a in self.accounts
            for cur, amt in a.cash.items()
            if amt != 0
        ]


class ManualPortfolioError(ValueError):
    pass


def validate_against_broker(manual: ManualPortfolio, broker_account_ids: set[str]) -> None:
    """Ручной счёт не может совпадать с брокерским счётом, который читается по токену (ADR-06).

    Исключение — смарт-счёт ЦФА: API не отдаёт его позиции, поэтому они ведутся карточками.
    """
    for a in manual.accounts:
        if a.id in broker_account_ids:
            raise ManualPortfolioError(
                f"счёт «{a.name}» ({a.id}) читается из API Т-Инвестиций — ручные позиции на нём запрещены"
            )
        if a.tinvest_account_id and a.kind is not AccountKind.DFA:
            raise ManualPortfolioError(
                f"счёт «{a.name}»: привязка к счёту Т-Инвестиций допустима только для смарт-счёта ЦФА"
            )


def load_manual(path: Path) -> ManualPortfolio:
    if not path.exists():
        return ManualPortfolio()
    raw: Any = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return ManualPortfolio.model_validate(raw)
