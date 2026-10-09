"""Контракт движка ребалансировки (§10.2–10.6). Этап MVP-1a: только рублёвые инструменты.

Валютные позиции и валютный кэш входят в NAV и показываются, но в сделках не участвуют (fx_convert — MVP-1b).
Налог оценивается по средней цене позиции (FIFO по операциям — следующий шаг), с пометкой «ориентировочно».
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import Field

from investbot.core.models import AccountKind, InstrumentMeta, Strict
from investbot.core.money import ZERO


class Holding(Strict):
    """Позиция, приведённая к виду, нужному движку. Цена и НКД — за 1 шт. в рублях."""

    account_id: str
    account_kind: AccountKind
    instrument: InstrumentMeta
    bucket: str
    qty: Decimal
    price: Decimal
    accrued: Decimal = ZERO
    avg_price: Decimal | None = None  # себестоимость за 1 шт.; None — налог не оценивается
    blocked_qty: Decimal = ZERO

    @property
    def unit_value(self) -> Decimal:
        return self.price + self.accrued

    @property
    def value(self) -> Decimal:
        return self.qty * self.unit_value

    @property
    def sellable_qty(self) -> Decimal:
        if not self.instrument.sell_available:
            return ZERO
        return max(ZERO, self.qty - self.blocked_qty)


class Quote(Strict):
    """Котировка инструмента для покупки (носитель бакета или докупка позиции)."""

    instrument: InstrumentMeta
    bucket: str
    price: Decimal  # за 1 шт. в рублях
    accrued: Decimal = ZERO

    @property
    def unit_value(self) -> Decimal:
        return self.price + self.accrued


class CashBalanceRub(Strict):
    account_id: str
    account_kind: AccountKind
    amount: Decimal  # доступно с учётом T+1


class CostModel(Strict):
    """Издержки сделки как доля оборота. Значения — параметры тарифа пользователя (config/tariffs.yaml)."""

    broker_fee_rate: Decimal = Decimal("0.003")  # «Инвестор» 0,3% — ориентир, сверять со страницей тарифа
    half_spread_rate: Decimal = Decimal("0.001")
    fee_overrides: dict[str, Decimal] = Field(default_factory=dict, description="тикер → ставка (фонды УК брокера)")

    def rate(self, ticker: str) -> Decimal:
        return self.fee_overrides.get(ticker, self.broker_fee_rate) + self.half_spread_rate


class TaxModel(Strict):
    rate: Decimal = Decimal("0.13")
    taxed_kinds: tuple[AccountKind, ...] = (AccountKind.BROKER,)  # продажа внутри ИИС текущего налога не создаёт


class PlanInput(Strict):
    holdings: tuple[Holding, ...]
    cash: tuple[CashBalanceRub, ...]
    other_value_rub: Decimal = ZERO  # валюта, ЦФА и прочее вне сделок MVP-1a: входит в NAV и доли бакетов
    other_bucket_values: dict[str, Decimal] = Field(default_factory=dict)  # те же суммы по бакетам
    quotes: dict[str, Quote] = Field(default_factory=dict)  # тикер → котировка носителей и докупаемых бумаг
    carriers: dict[str, str] = Field(default_factory=dict)  # бакет → тикер носителя по умолчанию
    cash_in: Decimal = ZERO  # пополнение (для /invest и плана с пополнением), зачисляется на cash_in_account
    cash_in_account: str | None = None
    allow_sells: bool = True
    as_of: date


Side = Literal["buy", "sell"]


class RebalanceAction(Strict):
    seq: int
    account_id: str
    ticker: str
    name: str
    bucket: str
    side: Side
    lots: int = Field(gt=0)
    qty: int
    price: Decimal
    limit_zone: tuple[Decimal, Decimal]
    valid_until: date
    est_amount_rub: Decimal
    est_fee_rub: Decimal
    est_tax_rub: Decimal
    reason_codes: tuple[str, ...]
    w_bucket_before: Decimal
    w_bucket_after: Decimal


Verdict = Literal["rebalance", "cashflow_only", "no_action", "blocked"]


class BucketState(Strict):
    bucket: str
    weight_before: Decimal
    weight_after: Decimal
    target: Decimal | None
    low: Decimal | None
    high: Decimal | None
    status_before: str
    status_after: str


class RebalancePlan(Strict):
    as_of: date
    ips_version: int
    verdict: Verdict
    nav_rub: Decimal
    actions: tuple[RebalanceAction, ...]
    buckets: tuple[BucketState, ...]
    drift_before: Decimal  # Σ|w − цель| / 2 по бакетам IPS
    drift_after: Decimal
    turnover_rub: Decimal
    fees_rub: Decimal
    tax_rub: Decimal
    deferred: tuple[str, ...] = ()  # «при следующем пополнении»: не прошли проверку выгоды
    unfunded: tuple[str, ...] = ()  # бакеты с недовесом без носителя и без позиций — нужен выбор пользователя
    notes: tuple[str, ...] = ()
    violations: tuple[str, ...] = ()
