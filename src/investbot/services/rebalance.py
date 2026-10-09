"""Сервис плана ребалансировки: снимок портфеля + IPS → вход движка → план → предрегистрация в журнале.

Каждый план записывается в журнал ДО отправки пользователю (ADR-13), решения по действиям — отдельными записями.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Awaitable, Callable, Mapping
from datetime import date
from decimal import Decimal

import structlog

from investbot.core.models import AccountKind, AssetClass, PortfolioSnapshot, Strict
from investbot.core.money import ZERO
from investbot.journal.store import Journal
from investbot.portfolio.buckets import classify
from investbot.rebalance.ips import IPS
from investbot.rebalance.models import CashBalanceRub, CostModel, Holding, PlanInput, Quote, RebalancePlan, TaxModel
from investbot.rebalance.planner import plan_invest, plan_rebalance
from investbot.services.portfolio import PortfolioOverview, PortfolioService

log = structlog.get_logger(__name__)

# тикеры → котировки (носители бакетов и бумаги, которых нет в портфеле); bucket — для какого бакета нужен тикер
QuoteProvider = Callable[[Mapping[str, str]], Awaitable[dict[str, Quote]]]

Decision = str  # "accept" | "postpone" | "skip"
DECISIONS = ("accept", "postpone", "skip")


class PreparedPlan(Strict):
    plan_id: str  # первые 12 символов хэша записи журнала
    overview: PortfolioOverview
    plan: RebalancePlan
    account_names: dict[str, str]


def build_plan_input(
    snap: PortfolioSnapshot,
    fx: Mapping[str, Decimal],
    ips: IPS,
    as_of: date,
    quotes: Mapping[str, Quote] | None = None,
    cash_in: Decimal = ZERO,
    cash_in_account: str | None = None,
    allow_sells: bool = True,
) -> PlanInput:
    """В сделках MVP-1a участвуют только рублёвые бумаги с ценой; остальное (валюта, ЦФА, без цены) — в NAV."""
    kinds = {a.account_id: a.kind for a in snap.accounts}
    holdings: list[Holding] = []
    other: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for p in snap.positions:
        bucket = classify(p.instrument, ips.rules.ig_min_rating, ips.instrument_buckets)
        tradable = (
            p.instrument.currency == "rub"
            and p.price is not None
            and p.instrument.asset_class is not AssetClass.DFA
            and p.source != "dfa_card"
        )
        if tradable and p.price is not None:
            holdings.append(Holding(
                account_id=p.account_id, account_kind=kinds.get(p.account_id, AccountKind.OTHER),
                instrument=p.instrument, bucket=bucket, qty=p.qty, price=p.price, accrued=p.accrued,
                avg_price=p.avg_price, blocked_qty=p.blocked_qty,
            ))
        elif p.value is not None:
            rate = fx.get(p.instrument.currency.lower())
            if rate is not None:
                other[bucket] += p.value * rate
    cash: list[CashBalanceRub] = []
    for c in snap.cash:
        if c.currency.lower() == "rub":
            cash.append(CashBalanceRub(account_id=c.account_id, account_kind=kinds.get(c.account_id, AccountKind.OTHER),
                                       amount=c.amount))
        elif (rate := fx.get(c.currency.lower())) is not None:
            other["fx_cash"] += c.amount * rate
    return PlanInput(
        holdings=tuple(holdings), cash=tuple(cash),
        other_value_rub=sum(other.values(), ZERO), other_bucket_values=dict(other),
        quotes=dict(quotes or {}), carriers=dict(ips.carriers),
        cash_in=cash_in, cash_in_account=cash_in_account, allow_sells=allow_sells, as_of=as_of,
    )


def resolve_account(snap: PortfolioSnapshot, hint: str | None) -> str | None:
    if not hint:
        return None
    h = hint.lower()
    for a in snap.accounts:
        if h == a.account_id.lower() or h in a.name.lower() or h == a.kind.value:
            return a.account_id
    raise LookupError(f"счёт «{hint}» не найден; доступны: {', '.join(a.name for a in snap.accounts)}")


class RebalanceService:
    def __init__(self, portfolio: PortfolioService, quotes: QuoteProvider | None, journal: Journal,
                 costs: CostModel, tax: TaxModel) -> None:
        self._portfolio = portfolio
        self._quotes = quotes
        self._journal = journal
        self._costs = costs
        self._tax = tax

    async def _carrier_quotes(self, ips: IPS) -> dict[str, Quote]:
        if self._quotes is None or not ips.carriers:
            return {}
        try:
            return await self._quotes({ticker: bucket for bucket, ticker in ips.carriers.items()})
        except Exception as e:
            log.warning("rebalance.quotes_failed", error=type(e).__name__)
            return {}

    async def _prepare(self, as_of: date, cash_in: Decimal, account_hint: str | None,
                       allow_sells: bool, kind: str) -> PreparedPlan:
        snap, fx = await self._portfolio.snapshot()
        cash_in_account = resolve_account(snap, account_hint)
        overview = self._portfolio.overview_from(snap, fx)
        ips = overview.ips
        if ips is None:
            raise LookupError("целевая аллокация не утверждена — сначала /allocation")
        quotes = await self._carrier_quotes(ips)
        inp = build_plan_input(snap, fx, ips, as_of, quotes, cash_in, cash_in_account, allow_sells)
        costs, tax = self._costs, self._tax
        plan = plan_invest(inp, ips, costs, tax) if kind == "invest" else plan_rebalance(inp, ips, costs, tax)
        record = self._journal.append(kind, {"plan": plan.model_dump(mode="json"), "ips_version": ips.version})
        return PreparedPlan(
            plan_id=record["hash"][:12], overview=overview, plan=plan,
            account_names={a.account_id: a.name for a in snap.accounts},
        )

    async def weekly_plan(self, as_of: date) -> PreparedPlan:
        return await self._prepare(as_of, ZERO, None, True, "rebalance_plan")

    async def invest(self, amount: Decimal, account_hint: str | None, as_of: date) -> PreparedPlan:
        """Пополнение: account_hint — id или часть названия счёта («иис»); по умолчанию — счёт с наибольшим кэшем."""
        return await self._prepare(as_of, amount, account_hint, False, "invest")

    def record_decision(self, plan_id: str, seq: int | None, decision: Decision) -> None:
        if decision not in DECISIONS:
            raise ValueError(f"неизвестное решение: {decision}")
        self._journal.append("decision", {"plan_id": plan_id, "seq": seq, "decision": decision}, author="user")
