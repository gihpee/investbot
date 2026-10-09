"""Свойства движка ребалансировки (§10.3, property-тесты): деньги сходятся, лоты целые, лимиты соблюдены."""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal

from hypothesis import assume, given, settings
from hypothesis import strategies as st

from investbot.core.models import AccountKind, AssetClass, CouponKind, FundFocus, InstrumentMeta
from investbot.rebalance.drift import compute_drift
from investbot.rebalance.ips import IPS, BucketTarget, Limits
from investbot.rebalance.models import CashBalanceRub, Holding, PlanInput, Quote, RebalancePlan, TaxModel
from investbot.rebalance.planner import plan_rebalance

D = Decimal
AS_OF = date(2026, 10, 9)
BRK, IIS = AccountKind.BROKER, AccountKind.IIS
ACCOUNTS = (("brk", BRK), ("iis", IIS))

BUCKETS = {
    "equity_ru": BucketTarget(target=D("0.40"), band_abs=D("0.05"), band_rel=D("0.25")),
    "ofz_fixed": BucketTarget(target=D("0.30"), band_abs=D("0.05"), band_rel=D("0.25")),
    "money_market": BucketTarget(target=D("0.15"), band_abs=D("0.03"), band_rel=D("0.30")),
    "gold": BucketTarget(target=D("0.05"), band_abs=D("0.02"), band_rel=D("0.50")),
    "corp_hy": BucketTarget(target=D(0), cap=D("0.03")),
    "dfa": BucketTarget(target=D("0.10"), cap=D("0.12")),
}


def meta(ticker: str, cls: AssetClass, **kw: object) -> InstrumentMeta:
    return InstrumentMeta(ticker=ticker, asset_class=cls, **kw)  # type: ignore[arg-type]


# (инструмент, бакет, цена, НКД, максимум лотов)
UNIVERSE = (
    (meta("TMOS", AssetClass.FUND, fund_focus=FundFocus.EQUITY), "equity_ru", D(5), D(0), 80_000),
    (meta("SBER", AssetClass.EQUITY, lot=10, issuer="sber"), "equity_ru", D("301.5"), D(0), 100),
    (meta("OFZ1", AssetClass.BOND, is_government=True, coupon_kind=CouponKind.FIXED, nominal=D(1000),
          min_price_increment=D("0.01")), "ofz_fixed", D(950), D("12.3"), 400),
    (meta("LQDT", AssetClass.FUND, fund_focus=FundFocus.MONEY_MARKET), "money_market", D("1.5"), D(0), 100_000),
    (meta("GOLD", AssetClass.FUND, fund_focus=FundFocus.GOLD), "gold", D(12), D(0), 5_000),
    (meta("HYB1", AssetClass.BOND, rating="BB", nominal=D(1000)), "corp_hy", D(1000), D(5), 60),
)
GOLD_QUOTE = Quote(instrument=UNIVERSE[4][0], bucket="gold", price=D(12))


@st.composite
def plan_inputs(draw: st.DrawFn) -> PlanInput:
    holdings: list[Holding] = []
    for m, bucket, price, accrued, max_lots in UNIVERSE:
        inst = m.model_copy(update={
            "sell_available": draw(st.sampled_from([True, True, True, False])),
            "buy_available": draw(st.sampled_from([True, True, False])),
            "for_iis": draw(st.sampled_from([True, True, False])),
        })
        for account, kind in ACCOUNTS:
            if not draw(st.booleans()):
                continue
            qty = draw(st.integers(0, max_lots)) * inst.lot
            holdings.append(Holding(
                account_id=account, account_kind=kind, instrument=inst, bucket=bucket, qty=D(qty), price=price,
                accrued=accrued, blocked_qty=D(draw(st.integers(0, qty)) if draw(st.booleans()) else 0),
                avg_price=draw(st.sampled_from([None, price / 2, price * D("1.2")])),
            ))
    cash = tuple(CashBalanceRub(account_id=a, account_kind=k, amount=D(draw(st.integers(0, 300)) * 1000))
                 for a, k in ACCOUNTS)
    dfa = D(draw(st.integers(0, 200)) * 1000)
    return PlanInput(
        holdings=tuple(holdings), cash=cash, other_value_rub=dfa, other_bucket_values={"dfa": dfa},
        quotes={"GOLD": GOLD_QUOTE} if draw(st.booleans()) else {},
        carriers={"gold": "GOLD"},
        cash_in=D(draw(st.sampled_from([0, 0, 25_000, 150_000]))),
        cash_in_account=draw(st.sampled_from([None, "brk", "iis"])),
        allow_sells=draw(st.booleans()), as_of=AS_OF,
    )


limits_st = st.builds(
    Limits,
    min_trade_rub=st.sampled_from([D(1000), D(3000), D(10_000)]),
    max_weekly_turnover=st.sampled_from([D("0.02"), D("0.10"), D(1)]),
    cash_reserve_rub=st.sampled_from([D(0), D(10_000), D(50_000)]),
    max_issuer_total=st.sampled_from([D("0.05"), D("0.10"), D(1)]),
)


def make_ips(limits: Limits) -> IPS:
    return IPS(version=1, buckets=BUCKETS, carriers={"gold": "GOLD"}, limits=limits)


def cash_in_account(inp: PlanInput) -> str:
    if inp.cash_in_account is not None:
        return inp.cash_in_account
    cash = {c.account_id: c.amount for c in inp.cash}
    return max(sorted(cash), key=lambda a: cash[a])


def check_invariants(inp: PlanInput, ips: IPS, plan: RebalancePlan) -> None:
    holdings = {(h.account_id, h.instrument.ticker): h for h in inp.holdings}
    metas = {h.instrument.ticker: h.instrument for h in inp.holdings} | {
        q.instrument.ticker: q.instrument for q in inp.quotes.values()}
    kinds = dict(ACCOUNTS)
    sells = [a for a in plan.actions if a.side == "sell"]
    buys = [a for a in plan.actions if a.side == "buy"]

    # лоты целые, количество = лоты × лот, не меньше минимальной сделки
    for a in plan.actions:
        assert isinstance(a.lots, int) and a.lots >= 1
        assert a.qty == a.lots * metas[a.ticker].lot
        assert a.est_amount_rub >= ips.limits.min_trade_rub
    # продаж сверх доступного нет; при запрете продаж — ни одной; на ИИС — без налога
    sold: dict[tuple[str, str], int] = defaultdict(int)
    for a in sells:
        sold[(a.account_id, a.ticker)] += a.qty
        if kinds[a.account_id] not in TaxModel().taxed_kinds:
            assert a.est_tax_rub == 0
    for key, qty in sold.items():
        h = holdings[key]
        assert h.instrument.sell_available and qty <= h.sellable_qty
    if not inp.allow_sells:
        assert not sells
    # покупки — только доступных бумаг, на ИИС — только for_iis; ЦФА автоматически не покупаются
    for a in buys:
        m = metas[a.ticker]
        assert m.buy_available and (kinds[a.account_id] is not IIS or m.for_iis)
        assert a.bucket != "dfa"
    # деньги сходятся по каждому счёту, резерв при покупках сохраняется
    cash = {c.account_id: c.amount for c in inp.cash}
    if inp.cash_in > 0:
        cash[cash_in_account(inp)] += inp.cash_in
    free = max(D(0), sum(cash.values(), D(0)) - ips.limits.cash_reserve_rub)
    for a in plan.actions:
        cash[a.account_id] += (a.est_amount_rub if a.side == "sell" else -a.est_amount_rub) - a.est_fee_rub
    assert all(v >= 0 for v in cash.values())
    if buys:
        assert sum(cash.values(), D(0)) >= ips.limits.cash_reserve_rub
    # недельный оборот: продажи ≤ лимита, всё вместе — не больше лимита плюс размещаемый свободный кэш
    nav = (sum((h.value for h in inp.holdings), D(0)) + sum((c.amount for c in inp.cash), D(0))
           + inp.other_value_rub + inp.cash_in)
    cap = ips.limits.max_weekly_turnover * nav
    assert sum((a.est_amount_rub for a in sells), D(0)) <= cap
    assert plan.turnover_rub <= cap + free
    # покупки не выводят бакет за верх полосы или потолок
    states = {b.bucket: b for b in plan.buckets}
    for b in {a.bucket for a in buys}:
        s = states[b]
        assert s.high is not None and s.weight_after <= s.high + D("0.000001")
    # сводка согласована с действиями
    assert plan.verdict == ("rebalance" if sells else "cashflow_only" if buys else plan.verdict)
    if not plan.actions:
        assert plan.verdict in ("no_action", "blocked")
    assert plan.turnover_rub == sum((a.est_amount_rub for a in plan.actions), D(0))
    assert [a.seq for a in plan.actions] == list(range(1, len(plan.actions) + 1))
    assert {b.bucket for b in plan.buckets} >= set(ips.buckets)


@settings(max_examples=60, deadline=None)
@given(inp=plan_inputs(), limits=limits_st)
def test_plan_invariants(inp: PlanInput, limits: Limits) -> None:
    ips = make_ips(limits)
    check_invariants(inp, ips, plan_rebalance(inp, ips))


@settings(max_examples=15, deadline=None)
@given(inp=plan_inputs(), limits=limits_st)
def test_plan_is_deterministic(inp: PlanInput, limits: Limits) -> None:
    ips = make_ips(limits)
    assert plan_rebalance(inp, ips) == plan_rebalance(inp, ips)


@st.composite
def calm_inputs(draw: st.DrawFn) -> PlanInput:
    """Все бакеты внутри полос, свободного кэша не больше резерва + 3 минимальных сделки, пополнения нет."""
    nav = D(1_000_000)
    permille = {
        "equity_ru": draw(st.integers(365, 435)),
        "ofz_fixed": draw(st.integers(265, 335)),
        "gold": draw(st.integers(33, 67)),
        "corp_hy": draw(st.integers(0, 27)),
    }
    rest = 1000 - sum(permille.values())  # денежный рынок (12,5–17,5%) + ЦФА (0–11,8%)
    lo, hi = max(125, rest - 118), min(175, rest)
    assume(lo <= hi)
    permille["money_market"] = draw(st.integers(lo, hi))
    permille["dfa"] = rest - permille["money_market"]
    w = {b: D(x) / 1000 for b, x in permille.items()}
    cash = D(draw(st.integers(0, 19)) * 1000)
    price = {m.ticker: p + a for m, _, p, a, _ in UNIVERSE}
    by_bucket = {"equity_ru": UNIVERSE[0], "ofz_fixed": UNIVERSE[2], "money_market": UNIVERSE[3],
                 "gold": UNIVERSE[4], "corp_hy": UNIVERSE[5]}
    holdings = []
    for b, (m, _, p, a, _) in by_bucket.items():
        value = w[b] * nav - (cash if b == "money_market" else 0)
        account, kind = ACCOUNTS[draw(st.integers(0, 1))]
        holdings.append(Holding(account_id=account, account_kind=kind, instrument=m, bucket=b,
                                qty=(value / price[m.ticker]).to_integral_value(), price=p, accrued=a))
    dfa = w["dfa"] * nav
    inp = PlanInput(holdings=tuple(holdings), cash=(CashBalanceRub(account_id="brk", account_kind=BRK, amount=cash),),
                    other_value_rub=dfa, other_bucket_values={"dfa": dfa}, quotes={"GOLD": GOLD_QUOTE},
                    allow_sells=draw(st.booleans()), as_of=AS_OF)
    total = sum((h.value for h in holdings), D(0)) + cash + dfa
    values = defaultdict(lambda: D(0), {h.bucket: h.value for h in holdings})
    values["money_market"] += cash
    values["dfa"] += dfa
    assume(all(d.status == "ok" for d in compute_drift({b: v / total for b, v in values.items()},
                                                        make_ips(Limits())) if d.status != "not_in_ips"))
    return inp


@settings(max_examples=40, deadline=None)
@given(inp=calm_inputs())
def test_no_trades_without_triggers(inp: PlanInput) -> None:
    plan = plan_rebalance(inp, make_ips(Limits()))
    assert plan.verdict == "no_action" and plan.actions == ()
    assert plan.drift_after == plan.drift_before
    assert "ближе всего к границе" in plan.notes[0]
