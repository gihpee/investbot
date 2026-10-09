"""Сервис плана ребалансировки: снимок портфеля + IPS → вход движка → план → предрегистрация в журнале.

Каждый план записывается в журнал ДО отправки пользователю (ADR-13), решения по действиям — отдельными записями.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, date, datetime
from decimal import Decimal

import structlog

from investbot.core.models import AccountKind, AssetClass, PortfolioSnapshot, Strict
from investbot.core.money import ZERO
from investbot.data.quality import gate_fresh, gate_portfolio
from investbot.journal.store import JournalLike
from investbot.ops.degradation import Degradation, GateResult, combine
from investbot.portfolio.buckets import classify
from investbot.portfolio.taxlots import TaxLot, TradeOp, build_lots, reconcile
from investbot.rebalance.ips import IPS
from investbot.rebalance.models import CashBalanceRub, CostModel, Holding, PlanInput, Quote, RebalancePlan, TaxModel
from investbot.rebalance.planner import plan_invest, plan_rebalance
from investbot.services.portfolio import PortfolioOverview, PortfolioService

log = structlog.get_logger(__name__)

# тикеры → котировки (носители бакетов и бумаги, которых нет в портфеле); bucket — для какого бакета нужен тикер
QuoteProvider = Callable[[Mapping[str, str]], Awaitable[dict[str, Quote]]]
TradeOpsProvider = Callable[[str, datetime], Awaitable[list[TradeOp]]]  # (счёт, с какого момента) → сделки
HISTORY_SINCE = datetime(2014, 1, 1, tzinfo=UTC)  # ЛДВ действует для бумаг, купленных с 2014 года
TAXED_KINDS = (AccountKind.BROKER,)

Decision = str  # "accept" | "postpone" | "skip"
DECISIONS = ("accept", "postpone", "skip")


GatesProvider = Callable[[], Awaitable[list[GateResult]]]  # внешние гейты (G-news и т. п.)


class PreparedPlan(Strict):
    plan_id: str  # первые 12 символов хэша записи журнала
    overview: PortfolioOverview
    plan: RebalancePlan | None  # None — план отложен (L4): уходит частичный отчёт
    account_names: dict[str, str]
    notes: tuple[str, ...] = ()  # примечания сервиса (сверка лотов и т. п.), показываются в отчёте
    degradation: Degradation = Degradation()


def build_plan_input(
    snap: PortfolioSnapshot,
    fx: Mapping[str, Decimal],
    ips: IPS,
    as_of: date,
    quotes: Mapping[str, Quote] | None = None,
    cash_in: Decimal = ZERO,
    cash_in_account: str | None = None,
    allow_sells: bool = True,
    lots: Mapping[tuple[str, str], tuple[TaxLot, ...]] | None = None,
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
                tax_lots=(lots or {}).get((p.account_id, p.instrument.ticker), ()),
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
    def __init__(self, portfolio: PortfolioService, quotes: QuoteProvider | None, journal: JournalLike,
                 costs: CostModel, tax: TaxModel, trade_ops: TradeOpsProvider | None = None,
                 extra_gates: GatesProvider | None = None) -> None:
        self._portfolio = portfolio
        self._extra_gates = extra_gates
        self._quotes = quotes
        self._trade_ops = trade_ops
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

    async def _tax_lots(self, snap: PortfolioSnapshot) -> tuple[dict[tuple[str, str], tuple[TaxLot, ...]], list[str]]:
        """FIFO-лоты по брокерским счетам Т-Инвестиций, сверенные с количеством в портфеле."""
        if self._trade_ops is None:
            return {}, []
        kinds = {a.account_id: a for a in snap.accounts}
        out: dict[tuple[str, str], tuple[TaxLot, ...]] = {}
        notes: list[str] = []
        accounts = sorted({p.account_id for p in snap.positions if p.source == "broker"
                           and (a := kinds.get(p.account_id)) is not None and a.kind in TAXED_KINDS})
        for account_id in accounts:
            try:
                built = build_lots(await self._trade_ops(account_id, HISTORY_SINCE))
            except Exception as e:
                log.warning("rebalance.trade_ops_failed", error=type(e).__name__)
                notes.append("история операций недоступна — налог оценён по средней цене")
                continue
            for p in snap.positions:
                if p.account_id != account_id:
                    continue
                res = built.get((account_id, p.instrument.ticker))
                if res is None:
                    continue
                if (diff := reconcile(res, p.qty)) is not None:
                    notes.append(f"{p.instrument.ticker}: {diff}")
                    continue
                out[(account_id, p.instrument.ticker)] = res.lots
        return out, notes

    async def _prepare(self, as_of: date, cash_in: Decimal, account_hint: str | None,
                       allow_sells: bool, kind: str) -> PreparedPlan:
        snap, fx = await self._portfolio.snapshot()
        cash_in_account = resolve_account(snap, account_hint)
        overview = self._portfolio.overview_from(snap, fx)
        ips = overview.ips
        if ips is None:
            raise LookupError("целевая аллокация не утверждена — сначала /allocation")
        lots, lot_notes = await self._tax_lots(snap) if allow_sells else ({}, [])
        hint = {p.instrument.ticker: p.qty * p.avg_price for p in snap.positions
                if p.price is None and p.avg_price is not None}
        gates = [gate_fresh(overview.valuation, hint), gate_portfolio(lot_notes)]
        if self._extra_gates is not None:
            try:
                gates.extend(await self._extra_gates())
            except Exception as e:
                log.warning("rebalance.gates_failed", error=type(e).__name__)
        deg = combine(snap.flags, gates, snap.broker_as_of)
        plan: RebalancePlan | None = None
        if deg.trade_list_allowed:
            quotes = await self._carrier_quotes(ips)
            inp = build_plan_input(snap, fx, ips, as_of, quotes, cash_in, cash_in_account,
                                   allow_sells and deg.allow_sells, lots)
            costs, tax = self._costs, self._tax
            plan = plan_invest(inp, ips, costs, tax) if kind == "invest" else plan_rebalance(inp, ips, costs, tax)
        record = await self._journal.append(kind, {
            "plan": plan.model_dump(mode="json") if plan else None, "ips_version": ips.version,
            "degradation": {"flags": list(deg.flags), "gates": [g.model_dump() for g in deg.gates if not g.ok]},
        })
        return PreparedPlan(
            plan_id=record["hash"][:12], overview=overview, plan=plan,
            account_names={a.account_id: a.name for a in snap.accounts},
            notes=tuple(lot_notes), degradation=deg,
        )

    async def weekly_plan(self, as_of: date) -> PreparedPlan:
        return await self._prepare(as_of, ZERO, None, True, "rebalance_plan")

    async def invest(self, amount: Decimal, account_hint: str | None, as_of: date) -> PreparedPlan:
        """Пополнение: account_hint — id или часть названия счёта («иис»); по умолчанию — счёт с наибольшим кэшем."""
        return await self._prepare(as_of, amount, account_hint, False, "invest")

    async def record_decision(self, plan_id: str, seq: int | None, decision: Decision) -> None:
        if decision not in DECISIONS:
            raise ValueError(f"неизвестное решение: {decision}")
        await self._journal.append("decision", {"plan_id": plan_id, "seq": seq, "decision": decision}, author="user")
