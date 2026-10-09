"""Движок ребалансировки MVP-1a: триггеры, продажи до точки возврата, cash-first, лоты, HardLimits, выгода."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

import pytest

from investbot.core.models import AccountKind, AssetClass, CouponKind, FundFocus, InstrumentMeta
from investbot.rebalance import lots as lots_mod
from investbot.rebalance.ips import IPS, BucketTarget, Limits, Rules
from investbot.rebalance.limits import LimitContext, Trade, check_hard_limits
from investbot.rebalance.lots import Leg, LotProblem, Window, greedy_lots, solve_milp
from investbot.rebalance.models import CashBalanceRub, CostModel, Holding, PlanInput, Quote, RebalancePlan
from investbot.rebalance.planner import plan_invest, plan_rebalance

D = Decimal
AS_OF = date(2026, 10, 9)
BRK, IIS = AccountKind.BROKER, AccountKind.IIS


def meta(ticker: str, cls: AssetClass, **kw: object) -> InstrumentMeta:
    return InstrumentMeta(ticker=ticker, asset_class=cls, **kw)  # type: ignore[arg-type]


TMOS = meta("TMOS", AssetClass.FUND, fund_focus=FundFocus.EQUITY, min_price_increment=D("0.01"))
SBER = meta("SBER", AssetClass.EQUITY, lot=10, issuer="sber", min_price_increment=D("0.01"))
OFZ = meta("OFZ26238", AssetClass.BOND, is_government=True, coupon_kind=CouponKind.FIXED, nominal=D(1000),
           min_price_increment=D("0.01"))
CORP = meta("CORP1", AssetClass.BOND, rating="AA", nominal=D(1000))
LQDT = meta("LQDT", AssetClass.FUND, fund_focus=FundFocus.MONEY_MARKET)
HYB = meta("HYB1", AssetClass.BOND, rating="BB", nominal=D(1000))
GOLD = meta("GOLD", AssetClass.FUND, fund_focus=FundFocus.GOLD)

BANDED = {"band_abs": D("0.05"), "band_rel": D("0.25")}


def make_ips(buckets: dict[str, BucketTarget] | None = None, carriers: dict[str, str] | None = None,
             limits: Limits | None = None, rules: Rules | None = None) -> IPS:
    default = {
        "equity_ru": BucketTarget(target=D("0.40"), **BANDED),
        "ofz_fixed": BucketTarget(target=D("0.40"), **BANDED),
        "money_market": BucketTarget(target=D("0.15"), band_abs=D("0.03"), band_rel=D("0.30")),
        "corp_hy": BucketTarget(target=D(0), cap=D("0.03")),
        "dfa": BucketTarget(target=D("0.05"), cap=D("0.08")),
    }
    return IPS(version=3, buckets=buckets or default, carriers=carriers or {},
               limits=limits or Limits(max_weekly_turnover=D(1)), rules=rules or Rules())


def hold(m: InstrumentMeta, bucket: str, qty: int, price: str | int, account: str = "brk",
         kind: AccountKind = BRK, **kw: object) -> Holding:
    return Holding(account_id=account, account_kind=kind, instrument=m, bucket=bucket, qty=D(qty),
                   price=D(price), **kw)  # type: ignore[arg-type]


def cash(amount: int, account: str = "brk", kind: AccountKind = BRK) -> CashBalanceRub:
    return CashBalanceRub(account_id=account, account_kind=kind, amount=D(amount))


def plan_input(*holdings: Holding, cash_rows: tuple[CashBalanceRub, ...] = (), dfa: int = 50_000,
               **kw: object) -> PlanInput:
    return PlanInput(holdings=holdings, cash=cash_rows, other_value_rub=D(dfa), other_bucket_values={"dfa": D(dfa)},
                     as_of=AS_OF, **kw)  # type: ignore[arg-type]


def bucket(plan: RebalancePlan, name: str) -> tuple[Decimal, Decimal, str, str]:
    b = next(x for x in plan.buckets if x.bucket == name)
    return b.weight_before, b.weight_after, b.status_before, b.status_after


def sold(plan: RebalancePlan) -> dict[tuple[str, str], int]:
    out: dict[tuple[str, str], int] = defaultdict(int)
    for a in plan.actions:
        if a.side == "sell":
            out[(a.account_id, a.ticker)] += a.qty
    return dict(out)


def cash_after(plan: RebalancePlan, inp: PlanInput) -> dict[str, Decimal]:
    out: dict[str, Decimal] = defaultdict(lambda: D(0))
    for c in inp.cash:
        out[c.account_id] += c.amount
    if inp.cash_in and inp.cash_in_account:
        out[inp.cash_in_account] += inp.cash_in
    for a in plan.actions:
        out[a.account_id] += (a.est_amount_rub if a.side == "sell" else -a.est_amount_rub) - a.est_fee_rub
    return dict(out)


# NAV = 1 000 000: акции 52% (выше полосы 35–45%), ОФЗ 30% (ниже 35–45%), денежный рынок 13%, ЦФА 5%
def above_band_input(**kw: object) -> PlanInput:
    return plan_input(
        hold(TMOS, "equity_ru", 104_000, 5, avg_price=D(4)),
        hold(OFZ, "ofz_fixed", 300, 1000),
        hold(LQDT, "money_market", 80_000, "1.5"),
        cash_rows=(cash(10_000),), **kw,
    )


def test_no_trigger_no_action_names_closest_bucket() -> None:
    inp = plan_input(
        hold(TMOS, "equity_ru", 86_200, 5), hold(OFZ, "ofz_fixed", 400, 950), hold(LQDT, "money_market", 86_000, "1.5"),
        cash_rows=(cash(10_000),),
    )
    plan = plan_rebalance(inp, make_ips())
    assert plan.verdict == "no_action" and plan.actions == ()
    assert plan.nav_rub == D(1_000_000)
    assert "ближе всего к границе — Акции РФ: +3,1 п.п. при полосе ±5 п.п." in plan.notes[0]
    assert plan.drift_before == plan.drift_after
    assert [b.bucket for b in plan.buckets] == ["equity_ru", "ofz_fixed", "money_market", "corp_hy", "dfa"]


def test_above_band_sells_to_return_point_and_funds_underweight() -> None:
    inp = above_band_input()
    plan = plan_rebalance(inp, make_ips())
    assert plan.verdict == "rebalance"
    assert set(sold(plan)) == {("brk", "TMOS")}
    _, w_eq, before, _ = bucket(plan, "equity_ru")
    assert before == "above"
    assert abs(w_eq - D("0.44375")) < D("0.001")  # точка возврата 40% + 0,875·5 п.п., а не цель 40%
    buys = [a for a in plan.actions if a.side == "buy"]
    assert buys and {a.ticker for a in buys} == {"OFZ26238"}
    assert all("CASHFLOW_FIRST" not in a.reason_codes for a in buys)  # куплено на выручку, свободного кэша не было
    assert "BAND_RETURN:ofz_fixed" in buys[0].reason_codes
    sell = next(a for a in plan.actions if a.side == "sell")
    assert sell.reason_codes == ("BAND_RETURN:equity_ru",) and sell.seq == 1
    assert sell.est_tax_rub == round(sell.qty * D(1) * D("0.13"), 2)  # (5 − 4) × qty × 13%
    assert any("налог ориентировочный" in n for n in plan.notes)
    assert sell.valid_until == AS_OF + timedelta(days=7)
    assert sell.limit_zone == (D("4.98"), D("5.02"))  # ± max(2 шага, ½ спреда)
    assert plan.drift_after < plan.drift_before
    assert min(cash_after(plan, inp).values()) >= 0
    assert sum(cash_after(plan, inp).values()) >= D(10_000)


@pytest.mark.parametrize("broker_avg", [D(3), D(6)])  # прибыль и убыток на брокерском счёте
def test_iis_sold_before_broker(broker_avg: Decimal) -> None:
    inp = plan_input(
        hold(TMOS, "equity_ru", 60_000, 5, avg_price=broker_avg),
        hold(TMOS, "equity_ru", 44_000, 5, account="iis", kind=IIS, avg_price=D(3)),
        hold(OFZ, "ofz_fixed", 300, 1000),
        hold(LQDT, "money_market", 80_000, "1.5"),
        cash_rows=(cash(10_000), cash(0, "iis", IIS)),
    )
    plan = plan_rebalance(inp, make_ips())
    assert set(sold(plan)) == {("iis", "TMOS")}
    assert all(a.est_tax_rub == 0 for a in plan.actions if a.side == "sell")
    assert plan.tax_rub == 0


def test_unsellable_and_blocked_never_sold() -> None:
    no_sell = SBER.model_copy(update={"sell_available": False})
    inp = plan_input(
        hold(no_sell, "equity_ru", 1000, 300),  # 300 000, продажа запрещена
        hold(TMOS, "equity_ru", 44_000, 5, blocked_qty=D(40_000)),  # продать можно только 4 000 шт.
        hold(OFZ, "ofz_fixed", 300, 1000),
        hold(LQDT, "money_market", 80_000, "1.5"),
        cash_rows=(cash(10_000),),
    )
    plan = plan_rebalance(inp, make_ips())
    assert set(sold(plan)) <= {("brk", "TMOS")}
    assert sold(plan).get(("brk", "TMOS"), 0) <= 4000


def test_nothing_sellable_is_blocked() -> None:
    no_sell = SBER.model_copy(update={"sell_available": False})
    inp = plan_input(
        hold(no_sell, "equity_ru", 1000, 300),
        hold(TMOS, "equity_ru", 44_000, 5, blocked_qty=D(44_000)),
        hold(OFZ, "ofz_fixed", 300, 1000),
        hold(LQDT, "money_market", 80_000, "1.5"),
        cash_rows=(cash(10_000),),
    )
    plan = plan_rebalance(inp, make_ips())
    assert plan.verdict == "blocked" and plan.actions == ()
    assert any("Акции РФ" in v and "недоступны к продаже" in v for v in plan.violations)


def test_cash_first_without_sells_is_cashflow_only() -> None:
    inp = plan_input(
        hold(TMOS, "equity_ru", 82_000, 5), hold(OFZ, "ofz_fixed", 380, 1000),
        hold(LQDT, "money_market", 66_000, "1.5"),
        cash_rows=(cash(61_000),),
    )
    plan = plan_rebalance(inp, make_ips())
    assert plan.verdict == "cashflow_only"
    assert all(a.side == "buy" for a in plan.actions)
    assert {a.ticker for a in plan.actions} == {"OFZ26238"}  # акции выше цели — им кэш не положен
    assert all({"CASHFLOW_FIRST", "CASH_SURPLUS", "UNDERWEIGHT:ofz_fixed"} <= set(a.reason_codes)
               for a in plan.actions)
    assert sum(cash_after(plan, inp).values()) >= D(10_000)
    # недовес ЦФА (cash_first: false) деньги не получает, но упоминается
    assert all(a.bucket != "dfa" for a in plan.actions)


def gold_ips(carriers: dict[str, str] | None = None) -> IPS:
    return make_ips({
        "equity_ru": BucketTarget(target=D("0.40"), **BANDED),
        "ofz_fixed": BucketTarget(target=D("0.35"), **BANDED),
        "money_market": BucketTarget(target=D("0.15"), band_abs=D("0.03")),
        "gold": BucketTarget(target=D("0.05"), band_abs=D("0.02"), band_rel=D("0.5")),
        "dfa": BucketTarget(target=D("0.05"), cap=D("0.08")),
    }, carriers=carriers)


def gold_input(**kw: object) -> PlanInput:
    return plan_input(hold(TMOS, "equity_ru", 80_000, 5), hold(OFZ, "ofz_fixed", 350, 1000),
                      hold(LQDT, "money_market", 50_000, 2), cash_rows=(cash(100_000),), **kw)


def test_unfunded_bucket_reported() -> None:
    plan = plan_rebalance(gold_input(), gold_ips())
    assert any(u.startswith("Золото: недовес") and "носител" in u for u in plan.unfunded)
    assert all(a.bucket != "gold" for a in plan.actions)

    no_quote = plan_rebalance(gold_input(), gold_ips({"gold": "GOLD"}))
    assert any("нет котировки носителя GOLD" in u for u in no_quote.unfunded)


def test_carrier_funds_empty_bucket() -> None:
    quote = Quote(instrument=GOLD, bucket="gold", price=D("2.5"))
    plan = plan_rebalance(gold_input(quotes={"GOLD": quote}), gold_ips({"gold": "GOLD"}))
    assert plan.unfunded == ()
    gold = [a for a in plan.actions if a.ticker == "GOLD"]
    assert gold and gold[0].side == "buy" and "BAND_RETURN:gold" in gold[0].reason_codes
    _, w_after, before, after = bucket(plan, "gold")
    assert before == "below" and after == "ok" and w_after <= D("0.05") + D("0.001")


def test_min_trade_and_turnover_respected() -> None:
    limits = Limits(min_trade_rub=D(20_000), max_weekly_turnover=D("0.03"))
    inp = above_band_input()
    plan = plan_rebalance(inp, make_ips(limits=limits))
    assert plan.actions
    assert all(a.est_amount_rub >= D(20_000) for a in plan.actions)
    sells = sum((a.est_amount_rub for a in plan.actions if a.side == "sell"), D(0))
    assert sells <= D(30_000) and plan.turnover_rub <= D(30_000)  # свободного кэша нет: всё — оборот
    assert any("недельного лимита оборота" in n for n in plan.notes)
    _, _, _, after = bucket(plan, "equity_ru")
    assert after == "above"  # остаток — в следующих неделях


def test_plan_invest_distributes_cash_in_by_lots_without_sells() -> None:
    # NAV до пополнения 900 000 ровно по целям; пополнение 100 000 → недовес 40 000 в акциях и ОФЗ
    inp = plan_input(
        hold(TMOS, "equity_ru", 72_000, 5), hold(OFZ, "ofz_fixed", 360, 1000),
        hold(LQDT, "money_market", 100_000, "1.25"),
        cash_rows=(cash(10_000), cash(0, "iis", IIS)), dfa=45_000, cash_in=D(100_000), cash_in_account="brk",
    )
    plan = plan_invest(inp, make_ips(limits=Limits()))  # лимит оборота 10% не мешает размещению пополнения
    assert plan.verdict == "cashflow_only"
    assert plan.actions and all(a.side == "buy" and a.account_id == "brk" for a in plan.actions)
    by_ticker = {a.ticker: a.est_amount_rub for a in plan.actions}
    assert set(by_ticker) == {"TMOS", "OFZ26238"}
    assert all(D(39_000) <= v <= D(40_000) for v in by_ticker.values())
    assert all(a.qty == a.lots * (1 if a.ticker != "SBER" else 10) for a in plan.actions)
    left = cash_after(plan, inp)["brk"]
    assert left >= D(10_000) + D(15_000)  # резерв и доля денежного рынка остаются кэшем
    assert any("нераспределённый остаток" in n for n in plan.notes)
    assert any(n.startswith("ЦФА: недовес") for n in plan.notes)


def test_plan_invest_never_sells_even_above_band() -> None:
    inp = above_band_input(cash_in=D(50_000), cash_in_account="brk")
    plan = plan_invest(inp, make_ips())
    assert plan.actions and all(a.side == "buy" for a in plan.actions)
    assert any("продажи в этом расчёте отключены" in n for n in plan.notes)


def test_cap_only_bucket_breach_sells_to_return_point() -> None:
    inp = plan_input(
        hold(TMOS, "equity_ru", 80_000, 5), hold(OFZ, "ofz_fixed", 380, 1000), hold(HYB, "corp_hy", 50, 1000),
        hold(LQDT, "money_market", 73_334, "1.5"), cash_rows=(cash(10_000),),
    )
    plan = plan_rebalance(inp, make_ips())
    hy = [a for a in plan.actions if a.ticker == "HYB1"]
    assert hy and hy[0].side == "sell" and hy[0].reason_codes == ("CAP_BREACH:corp_hy",)
    _, w_after, before, after = bucket(plan, "corp_hy")
    assert before == "cap_breach" and after == "ok"
    assert D("0.02") < w_after <= D("0.02625") + D("0.0005")  # 0 + 0,875 × (3% − 0), а не до нуля


def test_benefit_check_defers_tiny_expensive_trades() -> None:
    ips = make_ips({
        "equity_ru": BucketTarget(target=D("0.40"), **BANDED),
        "ofz_fixed": BucketTarget(target=D("0.30"), **BANDED),
        "corp_ig": BucketTarget(target=D("0.30"), **BANDED),
    })
    inp = plan_input(hold(TMOS, "equity_ru", 92_000, 5), hold(OFZ, "ofz_fixed", 270, 1000),
                     hold(CORP, "corp_ig", 260, 1000), cash_rows=(cash(10_000),), dfa=0)
    cheap = plan_rebalance(inp, ips)
    assert any(a.side == "buy" for a in cheap.actions) and cheap.deferred == ()

    expensive = CostModel(fee_overrides={"OFZ26238": D("0.05"), "CORP1": D("0.05")})
    plan = plan_rebalance(inp, ips, expensive)
    assert plan.verdict == "rebalance"
    assert all(a.side == "sell" for a in plan.actions)  # продажа снимает нарушение полосы — всегда оправдана
    assert any("OFZ26238" in x for x in plan.deferred) and any("CORP1" in x for x in plan.deferred)


def test_greedy_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lots_mod, "solve_milp", lambda _p: None)
    inp = above_band_input()
    plan = plan_rebalance(inp, make_ips())
    assert any("жадной эвристикой" in n for n in plan.notes)
    assert plan.verdict == "rebalance"
    assert set(sold(plan)) == {("brk", "TMOS")}
    _, w_eq, _, _ = bucket(plan, "equity_ru")
    assert w_eq >= D("0.4437")  # округление вниз: не продаём дальше точки возврата
    assert min(cash_after(plan, inp).values()) >= 0
    assert all(a.est_amount_rub >= D(3000) for a in plan.actions)


def test_excluded_issuer_not_bought_and_issuer_cap() -> None:
    inp = plan_input(hold(SBER, "equity_ru", 1000, 300), hold(OFZ, "ofz_fixed", 400, 1000),
                     hold(LQDT, "money_market", 100_000, 1), cash_rows=(cash(150_000),))
    excluded = plan_rebalance(inp, make_ips(limits=Limits(exclude_issuers=("sber",), max_weekly_turnover=D(1))))
    assert all(a.ticker != "SBER" for a in excluded.actions)
    assert any(u.startswith("Акции РФ") for u in excluded.unfunded)

    capped = plan_rebalance(inp, make_ips(limits=Limits(max_issuer_total=D("0.32"), max_weekly_turnover=D(1))))
    bought = sum((a.est_amount_rub for a in capped.actions if a.ticker == "SBER"), D(0))
    assert 0 < bought <= D("0.32") * D(1_000_000) - D(300_000)


def test_deterministic() -> None:
    inp = above_band_input()
    assert plan_rebalance(inp, make_ips()) == plan_rebalance(inp, make_ips())


def test_negative_cash_in_rejected() -> None:
    with pytest.raises(ValueError, match="пополнение"):
        plan_rebalance(above_band_input(cash_in=D(-1)), make_ips())


# --- HardLimits и округление напрямую ---------------------------------------------------------------------

def limit_ctx(**kw: object) -> LimitContext:
    base: dict[str, object] = {
        "nav": D(100_000), "cash": {"a": D(20_000)}, "reserve": D(5_000), "min_trade": D(1_000),
        "turnover_cap": D(10_000), "bucket_values": {"equity_ru": D(40_000), "corp_hy": D(1_000)},
        "bucket_caps": {"equity_ru": D("0.45"), "corp_hy": D("0.03")}, "issuer_values": {"sber": D(9_000)},
        "max_issuer": D("0.10"), "exclude_issuers": frozenset({"bad"}),
    }
    base.update(kw)
    return LimitContext(**base)  # type: ignore[arg-type]


def trade(key: int, ticker: str, amount: int, side: str = "buy", bucket: str = "equity_ru",
          issuer: str | None = None) -> Trade:
    return Trade(key=key, account_id="a", ticker=ticker, bucket=bucket, issuer=issuer, side=side,  # type: ignore[arg-type]
                 amount=D(amount), fee=D(0))


def test_hard_limits_detect_each_breach() -> None:
    assert check_hard_limits([trade(0, "OK", 2_000)], limit_ctx()) == []
    msgs = {b.message.split(":")[1].strip(): b for b in check_hard_limits([
        trade(0, "TINY", 500),
        trade(1, "BAD", 2_000, issuer="bad"),
        trade(2, "SBER", 2_000, issuer="sber"),
        trade(3, "HY", 3_000, bucket="corp_hy"),
    ], limit_ctx())}
    assert set(msgs) == {"меньше минимальной суммы сделки", "эмитент исключён в IPS",
                         "эмитент превысил бы лимит 10,0% портфеля", "доля превысила бы верхнюю границу 3,0%"}
    assert msgs["эмитент исключён в IPS"].scope == "ticker"
    cash_breach = check_hard_limits([trade(0, "BIG", 19_000, bucket="ofz")], limit_ctx(turnover_cap=D(100_000)))
    assert [b.message for b in cash_breach] == ["снята покупка BIG: кэш опустился бы ниже резерва"]
    overdraft = check_hard_limits([trade(0, "BIG", 25_000, bucket="ofz")], limit_ctx(turnover_cap=D(100_000)))
    assert "не хватает денег на счёте" in overdraft[0].message
    turnover = check_hard_limits([trade(0, "S", 11_000, side="sell"), trade(1, "B", 2_000)], limit_ctx())
    assert [b.keys for b in turnover] == [(0,)]
    flow = check_hard_limits([trade(0, "B", 12_000, bucket="ofz")], limit_ctx(cash_flow=D(15_000)))
    assert flow == []  # размещение свободного кэша — не оборот


def lot_problem(**kw: object) -> LotProblem:
    base: dict[str, object] = {
        "legs": (Leg("A", "a", "equity_ru", "sell", 1000.0, 50, 0.004),
                 Leg("B", "a", "ofz_fixed", "buy", 900.0, 100, 0.004)),
        "bucket_values": {"equity_ru": 60_000.0, "ofz_fixed": 30_000.0, "money_market": 10_000.0},
        "windows": {"equity_ru": Window(40_000.0, 45_000.0, 2.0), "ofz_fixed": Window(45_000.0, 50_000.0)},
        "caps": {"ofz_fixed": 55_000.0}, "buy_targets": {"B": 15_000.0}, "sell_needs": {"equity_ru": 15_000.0},
        "cash": {"a": 10_000.0}, "cash_floor": {"a": 5_000.0}, "reserve": 5_000.0, "min_trade": 1_000.0,
        "turnover_cap": 100_000.0, "issuer_room": {},
    }
    base.update(kw)
    return LotProblem(**base)  # type: ignore[arg-type]


def test_milp_and_greedy_round_to_lots_within_cash() -> None:
    for x in (solve_milp(lot_problem()).lots, greedy_lots(lot_problem())):  # type: ignore[union-attr]
        sell, buy = x
        assert 15 <= sell <= 20  # до окна [40k; 45k] после продажи
        cash_left = 10_000 + sell * 1000 * 0.996 - buy * 900 * 1.004
        assert cash_left >= 5_000 and buy > 0
    banned = solve_milp(lot_problem(banned=frozenset({1})))
    assert banned is not None and banned.lots[1] == 0
