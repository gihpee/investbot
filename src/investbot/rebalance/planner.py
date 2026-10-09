"""Недельный план ребалансировки и калькулятор пополнения (§10.2, §10.7), этап MVP-1a.

Порядок: оценка → триггеры → сначала кэш → продажи бакетов выше полосы до точки возврата → покупки внутри
бакетов → округление до лотов (lots.py) → HardLimits (limits.py) → проверка выгоды → план. Снятые лимитами и
проверкой выгоды сделки запрещаются, и задача решается заново (кардинальность понижается).

Соглашения MVP-1a:
- NAV = позиции + весь рублёвый кэш + прочее (валюта, ЦФА) + пополнение; рублёвый кэш — в денежном рынке.
- Точки возврата: target + f·(high − target) и target − f·(target − low), где [low, high] = BucketTarget.bounds(),
  f = return_to_band_fraction. Для бакетов только с cap верхняя точка — target + f·(cap − target).
- Свободный кэш (сверх резерва) и выручка делятся между бакетами с недовесом пропорционально недовесу к цели;
  денежный рынок участвует в делёжке своим недовесом, остаток остаётся кэшем.
- Резерв кэша закреплён за счетами с наибольшим кэшем; при любой покупке суммарный кэш после сделок ≥ резерва,
  поэтому выручка от продаж сначала восполняет резерв. Сделки только в рублях на счетах TRADABLE_KINDS.
- Недельный оборот: продажи ≤ cap и продажи + покупки ≤ cap + свободный кэш сверх резерва (с пополнением):
  размещение денежного потока оборотом не считается, иначе лимит душил бы /invest (§10.7).
- Налог — по средней цене (FIFO и ЛДВ — следующий шаг), только для TaxModel.taxed_kinds; убыток → 0.
Вне MVP-1a: валютные сделки и fx_convert, стресс-режим, лимиты сектора/выпуска/ВДО/ликвидности, продажи по
лимиту эмитента, транши крупных покупок, оферты, парковка остатка в фонд денежного рынка.
Тексты notes/deferred/unfunded/violations уходят в Telegram: в них только доли, без рублёвых сумм.
"""

from __future__ import annotations

import time
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import timedelta
from decimal import ROUND_HALF_EVEN, Decimal

from investbot.allocation.models import bucket_title
from investbot.core.models import AccountKind, AssetClass, InstrumentMeta
from investbot.core.money import ONE, ZERO, pct, pp, round_money, safe_div
from investbot.portfolio.taxlots import TaxLot, estimate_sale
from investbot.rebalance.drift import BucketDrift, compute_drift
from investbot.rebalance.ips import IPS
from investbot.rebalance.limits import Breach, LimitContext, Trade, cash_after, check_hard_limits, values_after
from investbot.rebalance.lots import TIME_LIMIT_S, Leg, LotProblem, Window, solve_lots
from investbot.rebalance.models import (
    BucketState,
    CostModel,
    Holding,
    PlanInput,
    RebalanceAction,
    RebalancePlan,
    Side,
    TaxModel,
    Verdict,
)

CASH_BUCKET = "money_market"  # рублёвый кэш учитывается в денежном рынке (как в portfolio/valuation.py)
FX_BUCKETS = ("fx_cash",)  # валютные покупки и fx_convert — MVP-1b
TRADABLE_KINDS = frozenset({AccountKind.BROKER, AccountKind.IIS, AccountKind.OTHER})
SELL_STATUSES = ("above", "cap_breach")
VALID_DAYS = 7
SURPLUS_MIN_TRADES = 3
MAX_SOLVES = 30
TIME_BUDGET_S = 20.0  # на все пересчёты после снятия сделок; одно решение MILP — не дольше TIME_LIMIT_S
W_QUANT = Decimal("0.000001")
DEFAULT_TICK = Decimal("0.01")
# Тай-брейки порядка сделок, доля оборота: малы рядом со штрафом отклонения (1 за рубль) и налогом.
TAXED_PREF = 0.01  # сначала продаём на ИИС, потом на брокерском
RANK_PREF = 1e-4  # затем — более крупные позиции
BASE_PREF = 1e-4  # «бесплатных» сделок нет даже при нулевом тарифе
AWAY_PREF = 1e-4  # покупка — предпочтительно на счёт пополнения, затем туда, где бумага уже есть

_COSTS = CostModel()
_TAX = TaxModel()


def plan_rebalance(inp: PlanInput, ips: IPS, costs: CostModel = _COSTS, tax: TaxModel = _TAX) -> RebalancePlan:
    """Недельный план (§10.2). Детерминирован: одинаковый вход — одинаковый план."""
    if inp.cash_in < 0:
        raise ValueError("пополнение не может быть отрицательным")
    return _Planner(inp, ips, costs, tax).run()


def plan_invest(inp: PlanInput, ips: IPS, costs: CostModel = _COSTS, tax: TaxModel = _TAX) -> RebalancePlan:
    """/invest N (§10.7): тот же движок с пополнением cash_in и запретом продаж."""
    return plan_rebalance(inp.model_copy(update={"allow_sells": False}), ips, costs, tax)


@dataclass(frozen=True)
class _Route:
    """Бумага, через которую бакет получает деньги: докупаемая позиция или носитель."""

    instrument: InstrumentMeta
    price: Decimal
    accrued: Decimal
    weight: Decimal

    @property
    def lot_value(self) -> Decimal:
        return (self.price + self.accrued) * self.instrument.lot


@dataclass(frozen=True)
class _LegInfo:
    leg: Leg
    instrument: InstrumentMeta
    price: Decimal
    accrued: Decimal
    avg_price: Decimal | None
    account_kind: AccountKind
    tax_lots: tuple[TaxLot, ...] = ()


@dataclass(frozen=True)
class _Act:
    idx: int
    info: _LegInfo
    lots: int
    qty: int
    amount: Decimal
    fee: Decimal
    tax: Decimal

    @property
    def side(self) -> Side:
        return self.info.leg.side

    @property
    def bucket(self) -> str:
        return self.info.leg.bucket

    @property
    def ticker(self) -> str:
        return self.info.leg.ticker

    @property
    def account_id(self) -> str:
        return self.info.leg.account_id

    def trade(self) -> Trade:
        return Trade(key=self.idx, account_id=self.account_id, ticker=self.ticker, bucket=self.bucket,
                     issuer=self.info.instrument.issuer, side=self.side, amount=self.amount, fee=self.fee)


@dataclass
class _Desired:
    """Непрерывный план до округления: суммы по бакетам и окна стоимостей для MILP."""

    sold: dict[str, Decimal] = field(default_factory=dict)
    buys: dict[str, Decimal] = field(default_factory=dict)
    mm_sell: Decimal = ZERO  # продажа фондов денежного рынка, если он выше полосы, а кэша не хватает
    routes: dict[str, list[_Route]] = field(default_factory=dict)
    windows: dict[str, Window] = field(default_factory=dict)

    @property
    def turnover(self) -> Decimal:
        return sum(self.sold.values(), ZERO) + sum(self.buys.values(), ZERO) + self.mm_sell


def _weights(values: Mapping[str, Decimal], nav: Decimal) -> dict[str, Decimal]:
    return {b: safe_div(v, nav) for b, v in values.items()}


def _q(x: Decimal) -> Decimal:
    return x.quantize(W_QUANT, rounding=ROUND_HALF_EVEN)


def _num(x: Decimal) -> str:
    return f"{x.normalize():f}".replace(".", ",")


def _add_unique(items: list[str], text: str) -> None:
    if text not in items:
        items.append(text)


def _tick(meta: InstrumentMeta) -> Decimal:
    inc = meta.min_price_increment
    if inc is None or inc <= 0:
        return DEFAULT_TICK
    if meta.asset_class is AssetClass.BOND and meta.nominal:
        return inc * meta.nominal / 100  # шаг облигаций в T-Invest — в % номинала, цена движка — в рублях
    return inc


def _zone(price: Decimal, meta: InstrumentMeta, half_spread: Decimal) -> tuple[Decimal, Decimal]:
    """last ± max(2 шага, ½ спреда), по шагу цены. Облигации: зона в % номинала и доходности — следующий шаг."""
    tick = _tick(meta)
    delta = max(2 * tick, price * half_spread)

    def snap(x: Decimal) -> Decimal:
        return (x / tick).quantize(ONE, rounding=ROUND_HALF_EVEN) * tick

    return max(snap(price - delta), tick), snap(price + delta)


class _Planner:
    def __init__(self, inp: PlanInput, ips: IPS, costs: CostModel, tax: TaxModel) -> None:
        self.inp, self.ips, self.costs, self.tax = inp, ips, costs, tax
        self.limits, self.rules = ips.limits, ips.rules
        self.notes: list[str] = []
        self.issues: list[str] = []  # невозможные действия: при пустом плане это violations и вердикт blocked
        self.removed: list[str] = []
        self.deferred: list[str] = []
        self.unfunded: list[str] = []
        self.mentioned: set[str] = set()
        self.greedy = False
        self.timed_out = False
        self.surplus = False
        self.cash_in_account: str | None = None
        self.holdings = tuple(sorted(inp.holdings, key=lambda h: (h.bucket, h.account_id, h.instrument.ticker)))
        self.kinds, self.cash = self._accounts()
        all_cash = sum((c.amount for c in inp.cash), ZERO) + inp.cash_in
        self.nav = sum((h.value for h in inp.holdings), ZERO) + all_cash + inp.other_value_rub
        values: dict[str, Decimal] = defaultdict(lambda: ZERO)
        for h in inp.holdings:
            values[h.bucket] += h.value
        for b, v in inp.other_bucket_values.items():
            values[b] += v
        values[CASH_BUCKET] += all_cash
        self.values = dict(values)
        self.weights = _weights(self.values, self.nav)
        self.drift: dict[str, BucketDrift] = {d.bucket: d for d in compute_drift(self.weights, ips)}
        self.reserve = self.limits.cash_reserve_rub
        self.trad_cash = sum(self.cash.values(), ZERO)
        self.floors = self._floors()
        self.turnover_cap = self.limits.max_weekly_turnover * self.nav
        self.flow = max(ZERO, self.trad_cash - self.reserve)  # размещение этого кэша оборотом не считается
        issuers: dict[str, Decimal] = defaultdict(lambda: ZERO)
        for h in inp.holdings:
            if h.instrument.issuer is not None:
                issuers[h.instrument.issuer] += h.value
        self.issuer_values = dict(issuers)

    # --- вход ------------------------------------------------------------------------------------------

    def _accounts(self) -> tuple[dict[str, AccountKind], dict[str, Decimal]]:
        """Виды счетов и рублёвый кэш торгуемых счетов; пополнение — на указанный счёт или с наибольшим кэшем."""
        kinds: dict[str, AccountKind] = {}
        cash: dict[str, Decimal] = defaultdict(lambda: ZERO)
        for c in self.inp.cash:
            kinds[c.account_id] = c.account_kind
            cash[c.account_id] += c.amount
        for h in self.holdings:
            kinds.setdefault(h.account_id, h.account_kind)
        if self.inp.cash_in > 0:
            target = self.inp.cash_in_account
            if target is None:
                tradable = sorted(a for a, k in kinds.items() if k in TRADABLE_KINDS)
                target = max(tradable, key=lambda a: cash[a]) if tradable else "cash_in"
                if len(tradable) > 1:
                    self.notes.append("пополнение отнесено на счёт с наибольшим свободным кэшем; счёт можно указать "
                                      "явно, например: /invest 50000 иис")
            kinds.setdefault(target, AccountKind.BROKER)
            cash[target] += self.inp.cash_in
            self.cash_in_account = target
        return kinds, {a: cash[a] for a in sorted(kinds) if kinds[a] in TRADABLE_KINDS}

    def _floors(self) -> dict[str, Decimal]:
        """Резерв закрепляется за счетами с наибольшим кэшем."""
        left, out = self.reserve, {}
        for a in sorted(self.cash, key=lambda a: (-self.cash[a], a)):
            take = min(max(self.cash[a], ZERO), left)
            out[a] = take
            left -= take
        return out

    def _tradable(self, h: Holding) -> bool:
        return (h.account_kind in TRADABLE_KINDS and h.instrument.currency == "rub"
                and h.instrument.asset_class is not AssetClass.DFA and h.unit_value > 0)

    def _buyable(self, meta: InstrumentMeta) -> bool:
        return (meta.buy_available and meta.currency == "rub" and meta.asset_class is not AssetClass.DFA
                and (meta.issuer is None or meta.issuer not in self.limits.exclude_issuers))

    def _sellable_lots(self, h: Holding) -> int:
        return int(h.sellable_qty // h.instrument.lot) if self._tradable(h) else 0

    def _sellable_value(self, bucket: str) -> Decimal:
        return sum((self._sellable_lots(h) * h.instrument.lot * h.unit_value
                    for h in self.holdings if h.bucket == bucket), ZERO)

    def _fee_rate(self, ticker: str) -> Decimal:
        return self.costs.fee_overrides.get(ticker, self.costs.broker_fee_rate)

    def _tax(self, price: Decimal, avg: Decimal | None, kind: AccountKind, qty: Decimal,
             lots: tuple[TaxLot, ...] = ()) -> Decimal:
        if kind not in self.tax.taxed_kinds:
            return ZERO
        if lots:
            return estimate_sale(lots, qty, price, self.inp.as_of, self.tax.rate).tax
        if avg is None:
            return ZERO
        return max(ZERO, price - avg) * qty * self.tax.rate

    def _mention(self, bucket: str, text: str) -> None:
        self.mentioned.add(bucket)
        _add_unique(self.notes, text)

    # --- триггеры --------------------------------------------------------------------------------------

    def _triggers(self) -> list[str]:
        out = []
        for b, bt in self.ips.buckets.items():
            d = self.drift[b]
            if d.status == "cap_breach":
                out.append(f"{bucket_title(b)} выше потолка {pct(bt.cap or ZERO)}: {pct(d.weight)}")
            elif d.status in ("above", "below"):
                side = "выше" if d.status == "above" else "ниже"
                out.append(f"{bucket_title(b)} {side} полосы: {pp(d.deviation)} при полосе ±{self._band_pp(b)}")
        existing = sum((c.amount for c in self.inp.cash if c.account_kind in TRADABLE_KINDS), ZERO)
        if existing > self.reserve + SURPLUS_MIN_TRADES * self.limits.min_trade_rub:
            self.surplus = True
            out.append("свободный кэш сверх резерва")
        if self.inp.cash_in > 0:
            out.append("пополнение")
        return out

    def _band_pp(self, bucket: str) -> str:
        band = self.ips.buckets[bucket].band()
        return f"{_num(band * 100)} п.п." if band is not None else "—"

    def _calm_note(self) -> str:
        best: tuple[Decimal, str] | None = None
        for b, bt in self.ips.buckets.items():
            w = self.weights.get(b, ZERO)
            low, high = bt.bounds()
            if bt.band() is not None:
                dist = min(w - low, high - w)
                text = f"{bucket_title(b)}: {pp(w - bt.target)} при полосе ±{self._band_pp(b)}"
            elif bt.cap is not None:
                dist = bt.cap - w
                text = f"{bucket_title(b)}: {pct(w)} при потолке {pct(bt.cap)}"
            else:
                continue
            if best is None or dist < best[0]:
                best = (dist, text)
        note = "Все бакеты в пределах полос, излишка свободного кэша нет — действий не требуется"
        return f"{note}; ближе всего к границе — {best[1]}" if best else note

    def _note_issuers(self) -> None:
        cap = self.limits.max_issuer_total * self.nav
        for issuer in sorted(self.issuer_values):
            if self.issuer_values[issuer] > cap:
                tickers = ", ".join(sorted({h.instrument.ticker for h in self.holdings
                                            if h.instrument.issuer == issuer}))
                self.notes.append(
                    f"эмитент бумаг {tickers}: {pct(safe_div(self.issuer_values[issuer], self.nav))} портфеля при "
                    f"лимите {pct(self.limits.max_issuer_total)} — новые покупки заблокированы; продажа по лимиту "
                    "эмитента — следующий шаг")

    # --- непрерывный план ------------------------------------------------------------------------------

    def _desired(self) -> _Desired:
        d = _Desired()
        nav, f = self.nav, self.rules.return_to_band_fraction
        for b, bt in self.ips.buckets.items():
            status = self.drift[b].status
            if b == CASH_BUCKET or status not in SELL_STATUSES:
                continue
            edge = "потолка" if status == "cap_breach" else "полосы"
            if not self.inp.allow_sells:
                self._mention(b, f"{bucket_title(b)}: выше {edge}, но продажи в этом расчёте отключены — "
                                 "вернётся в недельном плане")
                continue
            low, high = bt.bounds()
            sellable = self._sellable_value(b)
            if sellable <= 0:
                self._explain_unsellable(b, edge)
                continue
            sale = min(self.values.get(b, ZERO) - (bt.target + f * (high - bt.target)) * nav, sellable)
            d.sold[b] = sale
            d.windows[b] = Window(lo=float((bt.target - f * (bt.target - low)) * nav),
                                  hi=float(self.values.get(b, ZERO) - sale), kappa=2.0)

        proceeds = sum(d.sold.values(), ZERO)
        free = max(ZERO, self.trad_cash + proceeds - self.reserve)
        gaps: dict[str, Decimal] = {}
        for b, bt in self.ips.buckets.items():
            if b == CASH_BUCKET or b in d.sold or self.drift[b].status in SELL_STATUSES:
                continue
            gap = bt.target * nav - self.values.get(b, ZERO)
            if gap <= 0:
                continue
            title, worth = bucket_title(b), gap >= self.limits.min_trade_rub
            if b in self.rules.no_cash_first_buckets:
                if worth:
                    self._mention(b, f"{title}: недовес {pp(gap / nav)} — к пополнению при подходящем размещении, "
                                     "автоматически деньги сюда не направляются")
                continue
            if b in FX_BUCKETS:
                if worth:
                    self._mention(b, f"{title}: недовес {pp(gap / nav)} — валютные покупки появятся в MVP-1b")
                continue
            routes = self._routes(b, gap)
            if routes:
                gaps[b], d.routes[b] = gap, routes

        need = sum(gaps.values(), ZERO)
        mm = self.ips.buckets.get(CASH_BUCKET)
        v_mm = self.values.get(CASH_BUCKET, ZERO) + proceeds
        mm_share = max(ZERO, mm.target * nav - (v_mm - free)) if mm is not None else ZERO
        budget = free
        if mm is not None and self.inp.allow_sells and self.drift[CASH_BUCKET].status in SELL_STATUSES:
            _, high = mm.bounds()
            want = min(need, v_mm - (mm.target + f * (high - mm.target)) * nav)
            if want > free:
                d.mm_sell = min(want - free, self._sellable_value(CASH_BUCKET))
                budget = free + d.mm_sell
        total = need + mm_share
        alpha = min(ONE, budget / total) if total > 0 else ZERO
        for b, gap in gaps.items():
            if alpha * gap <= 0:
                continue
            bt, v = self.ips.buckets[b], self.values.get(b, ZERO)
            d.buys[b] = alpha * gap
            d.windows[b] = Window(lo=float(v + d.buys[b]), hi=float(max(v + d.buys[b], bt.target * nav)),
                                  kappa=2.0 if self.drift[b].status == "below" else 1.0)
        if mm is not None:
            d.windows[CASH_BUCKET] = Window(lo=float(v_mm - sum(d.buys.values(), ZERO)),
                                            kappa=1.0 if self.drift[CASH_BUCKET].status == "ok" else 2.0)
        return d

    def _explain_unsellable(self, bucket: str, edge: str) -> None:
        title = bucket_title(bucket)
        if any(h.bucket == bucket and self._tradable(h) and h.qty > 0 for h in self.holdings):
            self.mentioned.add(bucket)
            _add_unique(self.issues, f"{title}: выше {edge}, но позиции бакета недоступны к продаже (запрет продажи, "
                                     "блокировка или остаток меньше минимальной заявки)")
        else:
            self._mention(bucket, f"{title}: выше {edge}, но продать в бакете нечего в рамках MVP-1a (ЦФА держатся до "
                                  "погашения, валютные позиции — MVP-1b); новые покупки сюда не предлагаются")

    def _routes(self, bucket: str, gap: Decimal) -> list[_Route]:
        """Докупка позиций бакета пропорционально их стоимости, иначе носитель; без них — unfunded."""
        groups: dict[str, list[Holding]] = defaultdict(list)
        for h in self.holdings:
            if h.bucket == bucket and self._buyable(h.instrument) and h.unit_value > 0:
                groups[h.instrument.ticker].append(h)
        if groups:
            return [_Route(hs[0].instrument, hs[0].price, hs[0].accrued, sum((h.value for h in hs), ZERO))
                    for _, hs in sorted(groups.items())]
        ticker = self.inp.carriers.get(bucket) or self.ips.carriers.get(bucket)
        quote = self.inp.quotes.get(ticker) if ticker else None
        if ticker is None:
            reason = ("нет ни бумаг бакета, доступных к покупке, ни носителя по умолчанию — выберите носитель "
                      "(например, фонд этого класса)")
        elif quote is None:
            reason = f"нет котировки носителя {ticker} — покупка отложена"
        elif not self._buyable(quote.instrument) or quote.unit_value <= 0:
            reason = f"носитель {ticker} сейчас недоступен к покупке — выберите другой"
        else:
            return [_Route(quote.instrument, quote.price, quote.accrued, ONE)]
        if gap >= self.limits.min_trade_rub:
            self.mentioned.add(bucket)
            self.unfunded.append(f"{bucket_title(bucket)}: недовес {pp(gap / self.nav)}, {reason}")
        return []

    # --- округление до лотов ---------------------------------------------------------------------------

    def _legs(self, d: _Desired) -> list[_LegInfo]:
        infos: list[_LegInfo] = []
        proceeds_cap: dict[str, Decimal] = defaultdict(lambda: ZERO)
        for b in [*d.sold, *([CASH_BUCKET] if d.mm_sell > 0 else [])]:
            hs = [h for h in self.holdings if h.bucket == b and self._sellable_lots(h) > 0]
            order = sorted(range(len(hs)), key=lambda i: (-hs[i].value, hs[i].account_id, hs[i].instrument.ticker))
            for rank, i in enumerate(order):
                h = hs[i]
                lot = h.instrument.lot
                n = self._sellable_lots(h)
                taxed = h.account_kind in self.tax.taxed_kinds
                leg = Leg(ticker=h.instrument.ticker, account_id=h.account_id, bucket=b, side="sell",
                          lot_value=float(h.unit_value * lot), max_lots=n,
                          cost_rate=float(self.costs.rate(h.instrument.ticker)),
                          tax_per_lot=float(self._tax(h.price, h.avg_price, h.account_kind, Decimal(lot),
                                                      h.tax_lots)),
                          pref=BASE_PREF + (TAXED_PREF if taxed else 0.0) + RANK_PREF * rank,
                          issuer=h.instrument.issuer)
                infos.append(_LegInfo(leg, h.instrument, h.price, h.accrued, h.avg_price, h.account_kind,
                                      h.tax_lots))
                proceeds_cap[h.account_id] += n * lot * h.unit_value
        held = {(h.account_id, h.instrument.ticker) for h in self.holdings}
        for b, amount in d.buys.items():
            before = len(infos)
            for r in d.routes[b]:
                meta = r.instrument
                for a in self.cash:
                    if self.kinds[a] is AccountKind.IIS and not meta.for_iis:
                        continue
                    room = min(self.cash[a] + proceeds_cap[a], self.turnover_cap + self.flow)
                    n = int(room // r.lot_value) if room > 0 else 0
                    if n <= 0:
                        continue
                    away = (0.0 if a == self.cash_in_account else 2 * AWAY_PREF) + (
                        0.0 if (a, meta.ticker) in held else AWAY_PREF)
                    leg = Leg(ticker=meta.ticker, account_id=a, bucket=b, side="buy", lot_value=float(r.lot_value),
                              max_lots=n, cost_rate=float(self.costs.rate(meta.ticker)), pref=BASE_PREF + away,
                              issuer=meta.issuer)
                    infos.append(_LegInfo(leg, meta, r.price, r.accrued, None, self.kinds[a]))
            if len(infos) == before and amount >= self.limits.min_trade_rub:
                self._mention(b, f"{bucket_title(b)}: недовес {pp(amount / self.nav)}, но на счетах со свободными "
                                 "деньгами бумаги бакета купить нельзя (например, недоступны для ИИС) или минимальная "
                                 "заявка дороже доступной суммы")
        return infos

    def _problem(self, infos: Sequence[_LegInfo], d: _Desired) -> LotProblem:
        legs = tuple(i.leg for i in infos)
        max_fee = max((self._fee_rate(g.ticker) for g in legs), default=ZERO)
        nav_floor = self.nav - max_fee * (self.turnover_cap + self.flow)  # NAV после комиссий не ниже этого
        targets: dict[str, float] = {}
        for b, amount in d.buys.items():
            routes = d.routes[b]
            total = sum((r.weight for r in routes), ZERO)
            for r in routes:
                share = safe_div(r.weight, total) if total > 0 else ONE / len(routes)
                targets[r.instrument.ticker] = float(amount * share)
        sell_needs = {b: float(x) for b, x in d.sold.items()}
        if d.mm_sell > 0:
            sell_needs[CASH_BUCKET] = float(d.mm_sell)
        cap = self.limits.max_issuer_total * nav_floor
        issuers = sorted({g.issuer for g in legs if g.side == "buy" and g.issuer is not None})
        return LotProblem(
            legs=legs,
            bucket_values={b: float(v) for b, v in self.values.items()},
            windows=d.windows,
            caps={b: float(self.ips.buckets[b].bounds()[1] * nav_floor) for b in d.buys},
            buy_targets=targets,
            sell_needs=sell_needs,
            cash={a: float(c) for a, c in self.cash.items()},
            cash_floor={a: float(c) for a, c in self.floors.items()},
            reserve=float(self.reserve),
            min_trade=float(self.limits.min_trade_rub),
            turnover_cap=float(self.turnover_cap),
            issuer_room={i: float(max(ZERO, cap - self.issuer_values.get(i, ZERO))) for i in issuers},
            cash_bucket=CASH_BUCKET,
            cash_flow=float(self.flow),
        )

    def _limit_ctx(self) -> LimitContext:
        return LimitContext(
            nav=self.nav, cash=self.cash, reserve=self.reserve, min_trade=self.limits.min_trade_rub,
            turnover_cap=self.turnover_cap, bucket_values=self.values,
            bucket_caps={b: bt.bounds()[1] for b, bt in self.ips.buckets.items() if b != CASH_BUCKET},
            issuer_values=self.issuer_values, max_issuer=self.limits.max_issuer_total,
            exclude_issuers=frozenset(self.limits.exclude_issuers), cash_bucket=CASH_BUCKET, cash_flow=self.flow,
        )

    def _acts(self, infos: Sequence[_LegInfo], lots: Sequence[int]) -> list[_Act]:
        out = []
        for k, n in enumerate(lots):
            if n <= 0:
                continue
            info = infos[k]
            qty = n * info.instrument.lot
            amount = round_money(qty * (info.price + info.accrued))
            fee = round_money(amount * self._fee_rate(info.leg.ticker))
            tax = ZERO
            if info.leg.side == "sell":
                tax = round_money(self._tax(info.price, info.avg_price, info.account_kind, Decimal(qty),
                                            info.tax_lots))
            out.append(_Act(k, info, n, qty, amount, fee, tax))
        return out

    def _ban(self, infos: Sequence[_LegInfo], breaches: Sequence[Breach], banned: set[int]) -> None:
        for br in breaches:
            _add_unique(self.removed, br.message)
            if br.scope == "leg":
                banned.update(br.keys)
                continue
            hit = {(infos[k].leg.ticker, infos[k].leg.side) for k in br.keys}
            banned.update(k for k, i in enumerate(infos) if (i.leg.ticker, i.leg.side) in hit)

    def _optimize(self, infos: Sequence[_LegInfo], d: _Desired) -> list[_Act]:
        problem, ctx = self._problem(infos, d), self._limit_ctx()
        banned: set[int] = set()
        acts: list[_Act] = []
        deadline = time.monotonic() + TIME_BUDGET_S
        for _ in range(MAX_SOLVES):
            left = deadline - time.monotonic()
            if left <= 0:
                self.timed_out = True
                break
            sol = solve_lots(replace(problem, banned=frozenset(banned), time_limit=min(TIME_LIMIT_S, max(1.0, left))))
            self.greedy = self.greedy or sol.method == "greedy"
            self.timed_out = self.timed_out or sol.status == "time_limit"
            acts = self._acts(infos, sol.lots)
            breaches = check_hard_limits([a.trade() for a in acts], ctx)
            if breaches:
                self._ban(infos, breaches, banned)
                continue
            weak = self._unprofitable(acts)
            if not weak:
                return acts
            for a, text in weak:
                _add_unique(self.deferred, text)
                banned.update(k for k, i in enumerate(infos) if (i.leg.ticker, i.leg.side) == (a.ticker, a.side))
        # не сошлось за MAX_SOLVES решений или время вышло: снимаем нарушителей без пересчёта (сокращение плана
        # всегда допустимо: снятие покупки только освобождает кэш, оборот и лимиты)
        while breaches := check_hard_limits([a.trade() for a in acts], ctx):
            drop = {k for br in breaches for k in br.keys}
            for br in breaches:
                _add_unique(self.removed, br.message)
            acts = [a for a in acts if a.idx not in drop]
        weak_ids = set()
        for a, text in self._unprofitable(acts):
            _add_unique(self.deferred, text)
            weak_ids.add(a.idx)
        return [a for a in acts if a.idx not in weak_ids]

    # --- проверка выгоды (§10.2, п. 12) ----------------------------------------------------------------

    def _deviation(self, acts: Sequence[_Act]) -> Decimal:
        """Σ|w − цель| по бакетам IPS после сделок."""
        vals = values_after([a.trade() for a in acts], self.values, CASH_BUCKET)
        nav = self.nav - sum((a.fee for a in acts), ZERO)
        return sum((abs(safe_div(vals.get(b, ZERO), nav) - bt.target) for b, bt in self.ips.buckets.items()), ZERO)

    def _unprofitable(self, acts: Sequence[_Act]) -> list[tuple[_Act, str]]:
        """Покупки, чья выгода не окупает издержки. Всегда оправданы: продажи (снимают нарушение полосы или потолка),
        покупки в бакет ниже полосы и размещение излишка кэша или пополнения."""
        if not acts or self.surplus or self.inp.cash_in > 0:
            return []
        base = self._deviation(acts)
        k = self.rules.benefit_over_cost_min
        out = []
        for a in acts:
            if a.side == "sell" or self.drift[a.bucket].status == "below":
                continue
            gain = self._deviation([x for x in acts if x is not a]) - base
            benefit = gain * self.nav * self.rules.risk_penalty_rate
            cost = a.fee + a.amount * self.costs.half_spread_rate + a.tax
            if benefit < k * cost:
                out.append((a, f"купить {a.ticker} ({bucket_title(a.bucket)}): выгода от снижения отклонения ниже "
                               f"{_num(k)}× издержек"))
        return out

    # --- план ------------------------------------------------------------------------------------------

    def run(self) -> RebalancePlan:
        if self.nav <= 0:
            self.notes.append("портфель пуст — планировать нечего")
            return self._finish([], None)
        triggers = self._triggers()
        if not triggers:
            self.notes.insert(0, self._calm_note())
            return self._finish([], None)
        self.notes.insert(0, "Поводы для плана: " + "; ".join(triggers))
        self._note_issuers()
        d = self._desired()
        infos = self._legs(d)
        acts = self._optimize(infos, d) if infos else []
        if self.timed_out:
            self.notes.append("оптимизатор упёрся в лимит времени — план допустимый, но может быть не лучшим")
        if self.greedy:
            self.notes.append("MILP-округление не дало решения — план построен жадной эвристикой (бакеты от "
                              "наибольшего дрейфа, количества округлены вниз)")
        if sum(d.sold.values(), d.mm_sell) > self.turnover_cap or d.turnover > self.turnover_cap + self.flow:
            self.notes.append(f"нужные сделки больше недельного лимита оборота ({pct(self.limits.max_weekly_turnover)} "
                              "портфеля): план разбит на несколько недель, остаток — в следующих отчётах")
        return self._finish(acts, d)

    def _actions(self, acts: Sequence[_Act], w_after: Mapping[str, Decimal]) -> tuple[RebalanceAction, ...]:
        spent: dict[str, Decimal] = defaultdict(lambda: ZERO)
        for a in acts:
            if a.side == "buy":
                spent[a.account_id] += a.amount + a.fee
        funded = {a: spent[a] <= self.cash.get(a, ZERO) - self.floors.get(a, ZERO) for a in spent}
        valid_until = self.inp.as_of + timedelta(days=VALID_DAYS)
        out = []
        ordered = sorted(acts, key=lambda a: (a.side != "sell", a.account_id, -a.amount, a.ticker))
        for seq, a in enumerate(ordered, start=1):
            b, status = a.bucket, self.drift[a.bucket].status
            if a.side == "sell":
                codes = [f"CAP_BREACH:{b}" if status == "cap_breach" else f"BAND_RETURN:{b}"]
            else:
                codes = []
                if funded[a.account_id]:
                    codes.append("CASHFLOW_FIRST")
                    if self.surplus:
                        codes.append("CASH_SURPLUS")
                codes.append(f"UNDERWEIGHT:{b}")
                if status == "below":
                    codes.append(f"BAND_RETURN:{b}")
            meta = a.info.instrument
            out.append(RebalanceAction(
                seq=seq, account_id=a.account_id, ticker=a.ticker, name=meta.name or a.ticker, bucket=b, side=a.side,
                lots=a.lots, qty=a.qty, price=a.info.price,
                limit_zone=_zone(a.info.price, meta, self.costs.half_spread_rate), valid_until=valid_until,
                est_amount_rub=a.amount, est_fee_rub=a.fee, est_tax_rub=a.tax, reason_codes=tuple(codes),
                w_bucket_before=_q(self.weights.get(b, ZERO)), w_bucket_after=_q(w_after.get(b, ZERO)),
            ))
        return tuple(out)

    def _states(self, w_after: Mapping[str, Decimal], after: Mapping[str, BucketDrift]) -> tuple[BucketState, ...]:
        out = []
        for b, bt in self.ips.buckets.items():
            low, high = bt.bounds()
            out.append(BucketState(bucket=b, weight_before=_q(self.weights.get(b, ZERO)),
                                   weight_after=_q(w_after.get(b, ZERO)), target=bt.target, low=low, high=high,
                                   status_before=self.drift[b].status, status_after=after[b].status))
        extra = sorted(b for b in {*self.weights, *w_after} if b not in self.ips.buckets
                       and (self.weights.get(b, ZERO) != 0 or w_after.get(b, ZERO) != 0))
        for b in extra:
            out.append(BucketState(bucket=b, weight_before=_q(self.weights.get(b, ZERO)),
                                   weight_after=_q(w_after.get(b, ZERO)), target=None, low=None, high=None,
                                   status_before="not_in_ips", status_after="not_in_ips"))
        return tuple(out)

    def _drift_sum(self, w: Mapping[str, Decimal]) -> Decimal:
        return _q(sum((abs(w.get(b, ZERO) - bt.target) for b, bt in self.ips.buckets.items()), ZERO) / 2)

    def _explain_rest(self, acts: Sequence[_Act], after: Mapping[str, BucketDrift], d: _Desired) -> None:
        for b in self.ips.buckets:
            x = after[b]
            if x.status == "ok" or b in self.mentioned:
                continue
            title, corridor = bucket_title(b), f"{pct(x.low or ZERO)}–{pct(x.high or ZERO)}"
            if x.status == "below":
                self.notes.append(f"{title}: и после плана ниже полосы ({pct(x.weight)} при коридоре {corridor}) — "
                                  "свободных денег не хватает, а продажи предлагаются только из бакетов выше полосы; "
                                  "закроется пополнением")
            else:
                edge = "потолка" if x.status == "cap_breach" else "полосы"
                self.notes.append(f"{title}: и после плана выше {edge} ({pct(x.weight)}) — мешают минимальная сумма "
                                  "сделки, недельный лимит оборота, кратность заявок или нехватка бумаг для покупки; "
                                  "остаток — в следующих неделях")
        if (self.surplus or self.inp.cash_in > 0) and not d.buys and not any(a.side == "buy" for a in acts):
            self.notes.append("свободные деньги остаются кэшем (денежный рынок): бакетов с недовесом, куда их можно "
                              "направить, нет")

    def _finish(self, acts: Sequence[_Act], d: _Desired | None) -> RebalancePlan:
        trades = [a.trade() for a in acts]
        fees = sum((a.fee for a in acts), ZERO)
        nav_after = self.nav - fees
        w_after = _weights(values_after(trades, self.values, CASH_BUCKET), nav_after)
        after = {x.bucket: x for x in compute_drift(w_after, self.ips)}
        sells = [a for a in acts if a.side == "sell"]
        verdict: Verdict = ("rebalance" if sells else "cashflow_only" if acts
                            else "blocked" if self.issues or self.removed else "no_action")
        notes = list(self.notes)
        violations: tuple[str, ...] = ()
        if verdict == "blocked":
            violations = (*self.issues, *self.removed)
        else:
            notes.extend([*self.issues, *self.removed])
        taxed_sells = [a for a in sells if a.info.account_kind in self.tax.taxed_kinds]
        if any(a.info.tax_lots for a in taxed_sells):
            notes.append("налог ориентировочный: по FIFO-лотам из операций с учётом ЛДВ (лимит ЛДВ не проверялся), "
                         "не налоговая консультация")
        if any(not a.info.tax_lots and a.info.avg_price is not None for a in taxed_sells):
            notes.append("налог ориентировочный, по средней цене: лоты по операциям не загружены или не сошлись "
                         "с портфелем")
        if any(not a.info.tax_lots and a.info.avg_price is None for a in taxed_sells):
            notes.append("по части продаж себестоимость неизвестна — налог не оценён")
        for a in taxed_sells:
            if not a.info.tax_lots:
                continue
            t = estimate_sale(a.info.tax_lots, Decimal(a.qty), a.info.price, self.inp.as_of, self.tax.rate)
            if t.near_ldv_qty > 0 and t.days_to_ldv_min is not None:
                notes.append(f"{a.info.leg.ticker}: часть продаваемых лотов получит ЛДВ через {t.days_to_ldv_min} дн. "
                             "— если продажа не срочная, её можно отложить и не платить налог с этой части")
            if t.unknown_cost_qty > 0:
                notes.append(f"{a.info.leg.ticker}: у части лотов неизвестна себестоимость — налог занижен")
        if d is not None:
            before = len(self.notes)
            self._explain_rest(acts, after, d)
            notes.extend(self.notes[before:])
        if self.inp.cash_in > 0:
            left = sum(cash_after(trades, self.cash).values(), ZERO) - self.reserve
            if left > 0:
                notes.append(f"нераспределённый остаток около {pct(safe_div(left, self.nav))} портфеля остаётся "
                             "кэшем (денежный рынок)")
        return RebalancePlan(
            as_of=self.inp.as_of, ips_version=self.ips.version, verdict=verdict, nav_rub=round_money(self.nav),
            actions=self._actions(acts, w_after), buckets=self._states(w_after, after),
            drift_before=self._drift_sum(self.weights), drift_after=self._drift_sum(w_after),
            turnover_rub=sum((a.amount for a in acts), ZERO), fees_rub=fees, tax_rub=sum((a.tax for a in acts), ZERO),
            deferred=tuple(self.deferred), unfunded=tuple(self.unfunded), notes=tuple(notes),
            violations=violations,
        )
