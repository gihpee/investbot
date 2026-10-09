"""Округление плана до лотов (§10.3): MILP через scipy.optimize.milp (HiGHS) и жадный фолбэк.

Постановка MVP-1a (упрощение §10.3), всё в тысячах рублей:
    x_k ∈ ℤ≥0 — лоты по ноге k (бумага × счёт × сторона), y_k ∈ {0,1} — индикатор сделки;
    dl_b, du_b ≥ 0 — выход стоимости бакета b после сделок за окно [lo_b, hi_b];
    e⁺_t, e⁻_t ≥ 0 — отклонение покупки тикера t от желаемой суммы (распределение внутри бакета).

    min  Σ_b κ_b·(dl_b + du_b) + κ_t·Σ_t (e⁺_t + e⁻_t)
       + Σ_k (c_k + pref_k)·lv_k·x_k + λ_tax·Σ_k tax_k·x_k
    s.t. v_b + Σ_k a_bk·x_k + dl_b ≥ lo_b;   v_b + Σ_k a_bk·x_k − du_b ≤ hi_b
         v_b + Σ_k a_bk·x_k ≤ cap_b                          (покупаемые бакеты не выходят за полосу/потолок)
         cash_a + Σ_{k∈a} q_k·x_k ≥ floor_a                  (кэш по счёту, без межсчётных переводов)
         Σ_a cash_a + Σ_k q_k·x_k ≥ reserve·y_k  ∀ покупка k (если кэша меньше резерва: сначала резерв)
         lv_k·x_k ≥ min_trade·y_k;  x_k ≤ max_lots_k·y_k
         Σ_{sell} lv_k·x_k ≤ turnover_cap;  Σ_k lv_k·x_k ≤ turnover_cap + cash_flow
         Σ_{k∈I, buy} lv_k·x_k ≤ room_I
         Σ_{k∈t, buy} lv_k·x_k − e⁺_t + e⁻_t = d_t
где a_bk = ±lv_k для ноги своего бакета и ∓lv_k для бакета кэша (рублёвый кэш — денежный рынок),
q_k = lv_k·(1 − c_k) для продажи и −lv_k·(1 + c_k) для покупки (консервативно: комиссия + ½ спреда).
Оборот: размещение свободного кэша сверх резерва и пополнения (cash_flow) оборотом не считается — иначе лимит
оборота душил бы /invest; продажи и покупки на выручку считаются с обеих сторон, как в §10.3.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

import numpy as np
from numpy.typing import NDArray
from scipy.optimize import Bounds, LinearConstraint, milp

from investbot.rebalance.models import Side

FloatArray = NDArray[np.float64]

SCALE = 1000.0  # решатель считает в тысячах рублей
TIME_LIMIT_S = 10.0
MIP_REL_GAP = 1e-6  # тай-брейки порядка сделок различимы, а хвост времени решения не растёт
KAPPA_TICKER = 0.1  # распределение покупки внутри бакета вторично к доле самого бакета
LAMBDA_TAX = 1.0
MARGIN_RUB = 1.0  # запас на допуски решателя в ограничениях-неравенствах


@dataclass(frozen=True)
class Leg:
    """Кандидат сделки: бумага × счёт × сторона. Суммы — в рублях."""

    ticker: str
    account_id: str
    bucket: str
    side: Side
    lot_value: float  # (цена + НКД) × лот
    max_lots: int
    cost_rate: float  # комиссия + ½ спреда, доля оборота
    tax_per_lot: float = 0.0
    pref: float = 0.0  # тай-брейк порядка сделок, доля оборота
    issuer: str | None = None

    @property
    def sign(self) -> float:
        return 1.0 if self.side == "buy" else -1.0

    @property
    def cash_per_lot(self) -> float:
        if self.side == "buy":
            return -self.lot_value * (1 + self.cost_rate)
        return self.lot_value * (1 - self.cost_rate)


@dataclass(frozen=True)
class Window:
    """Желаемая стоимость бакета после сделок: штраф κ за каждый рубль вне [lo, hi]."""

    lo: float
    hi: float = math.inf
    kappa: float = 1.0


@dataclass(frozen=True)
class LotProblem:
    legs: tuple[Leg, ...]
    bucket_values: Mapping[str, float]  # стоимость бакетов до сделок
    windows: Mapping[str, Window]
    caps: Mapping[str, float]  # жёсткий потолок стоимости покупаемых бакетов
    buy_targets: Mapping[str, float]  # тикер → желаемая сумма покупки
    sell_needs: Mapping[str, float]  # бакет → желаемая сумма продажи (порядок и объём для фолбэка)
    cash: Mapping[str, float]  # счёт → кэш до сделок
    cash_floor: Mapping[str, float]  # счёт → минимум кэша после сделок (доля резерва)
    reserve: float
    min_trade: float
    turnover_cap: float
    issuer_room: Mapping[str, float]
    cash_flow: float = 0.0  # свободный кэш сверх резерва + пополнение: его размещение не считается оборотом
    cash_bucket: str = "money_market"
    banned: frozenset[int] = frozenset()
    time_limit: float = TIME_LIMIT_S


@dataclass(frozen=True)
class LotSolution:
    lots: tuple[int, ...]
    method: Literal["milp", "greedy"]
    status: str = ""


def solve_lots(p: LotProblem) -> LotSolution:
    """MILP; если решатель не дал решения — жадная эвристика с округлением вниз."""
    res = solve_milp(p)
    if res is not None:
        return res
    return LotSolution(greedy_lots(p), "greedy")


def _effect(p: LotProblem, bucket: str) -> FloatArray:
    """Изменение стоимости бакета (в единицах решателя) на один лот каждой ноги."""
    out = np.zeros(len(p.legs))
    for k, g in enumerate(p.legs):
        if g.bucket == bucket:
            out[k] += g.sign * g.lot_value
        if bucket == p.cash_bucket:
            out[k] -= g.sign * g.lot_value
    return out / SCALE


def solve_milp(p: LotProblem) -> LotSolution | None:
    legs = p.legs
    n = len(legs)
    if n == 0:
        return LotSolution((), "milp", "empty")
    wins = sorted(p.windows)
    tickers = sorted(p.buy_targets)
    m, q = len(wins), len(tickers)
    nv = 2 * n + 2 * m + 2 * q
    i_dl, i_du, i_ep, i_em = 2 * n, 2 * n + m, 2 * n + 2 * m, 2 * n + 2 * m + q

    lv = np.array([g.lot_value for g in legs]) / SCALE
    ub_x = np.array([0.0 if k in p.banned else float(g.max_lots) for k, g in enumerate(legs)])
    cash_q = np.array([g.cash_per_lot for g in legs]) / SCALE
    margin = MARGIN_RUB / SCALE

    c = np.zeros(nv)
    c[:n] = lv * np.array([g.cost_rate + g.pref for g in legs]) + LAMBDA_TAX * np.array(
        [g.tax_per_lot for g in legs]) / SCALE
    lb = np.zeros(nv)
    ub = np.full(nv, np.inf)
    ub[:n] = ub_x
    ub[n:2 * n] = (ub_x > 0).astype(float)
    rows: list[FloatArray] = []
    lo: list[float] = []
    hi: list[float] = []

    def add(row: FloatArray, low: float, high: float) -> None:
        rows.append(row)
        lo.append(low)
        hi.append(high)

    for j, b in enumerate(wins):
        w = p.windows[b]
        v = p.bucket_values.get(b, 0.0) / SCALE
        c[i_dl + j] = c[i_du + j] = w.kappa
        eff = _effect(p, b)
        row = np.zeros(nv)
        row[:n] = eff
        row[i_dl + j] = 1.0
        add(row, w.lo / SCALE - v, np.inf)
        if math.isinf(w.hi):
            ub[i_du + j] = 0.0
        else:
            row = np.zeros(nv)
            row[:n] = eff
            row[i_du + j] = -1.0
            add(row, -np.inf, w.hi / SCALE - v)
    for b in sorted(p.caps):
        row = np.zeros(nv)
        row[:n] = _effect(p, b)
        add(row, -np.inf, max(0.0, (p.caps[b] - p.bucket_values.get(b, 0.0)) / SCALE - margin))

    accounts = sorted({g.account_id for g in legs})
    for a in accounts:
        row = np.zeros(nv)
        row[:n] = np.where([g.account_id == a for g in legs], cash_q, 0.0)
        slack = (p.cash.get(a, 0.0) - p.cash_floor.get(a, 0.0)) / SCALE
        add(row, -max(0.0, slack - margin), np.inf)
    total_cash = sum(p.cash.values())
    if sum(p.cash_floor.values()) < p.reserve - 1e-6:
        for k, g in enumerate(legs):
            if g.side == "buy":
                row = np.zeros(nv)
                row[:n] = cash_q
                row[n + k] = -(p.reserve + MARGIN_RUB) / SCALE
                add(row, -total_cash / SCALE, np.inf)

    for k in range(n):
        row = np.zeros(nv)
        row[k] = lv[k]
        row[n + k] = -p.min_trade / SCALE
        add(row, 0.0, np.inf)
        row = np.zeros(nv)
        row[k] = 1.0
        row[n + k] = -ub_x[k]
        add(row, -np.inf, 0.0)
    row = np.zeros(nv)
    row[:n] = np.where([g.side == "sell" for g in legs], lv, 0.0)
    add(row, -np.inf, max(0.0, p.turnover_cap / SCALE - margin))
    row = np.zeros(nv)
    row[:n] = lv
    add(row, -np.inf, max(0.0, (p.turnover_cap + p.cash_flow) / SCALE - margin))
    for issuer in sorted(p.issuer_room):
        mask = [g.side == "buy" and g.issuer == issuer for g in legs]
        if any(mask):
            row = np.zeros(nv)
            row[:n] = np.where(mask, lv, 0.0)
            add(row, -np.inf, max(0.0, p.issuer_room[issuer] / SCALE - margin))
    for t, ticker in enumerate(tickers):
        row = np.zeros(nv)
        row[:n] = np.where([g.side == "buy" and g.ticker == ticker for g in legs], lv, 0.0)
        row[i_ep + t] = -1.0
        row[i_em + t] = 1.0
        d = p.buy_targets[ticker] / SCALE
        add(row, d, d)
        c[i_ep + t] = c[i_em + t] = KAPPA_TICKER

    integrality = np.zeros(nv)
    integrality[:2 * n] = 1
    res = milp(
        c,
        integrality=integrality,
        bounds=Bounds(lb, ub),
        constraints=[LinearConstraint(np.vstack(rows), np.array(lo), np.array(hi))],
        options={"time_limit": p.time_limit, "mip_rel_gap": MIP_REL_GAP, "disp": False},
    )
    if res.x is None:
        return None
    x = np.clip(np.round(np.asarray(res.x[:n], dtype=np.float64)), 0.0, ub_x)
    return LotSolution(tuple(int(v) for v in x), "milp", "optimal" if res.status == 0 else "time_limit")


def greedy_lots(p: LotProblem) -> tuple[int, ...]:
    """Фолбэк §10.3: бакеты от наибольшего дрейфа, продажи до точки возврата, всё округляется вниз до лота."""
    legs = p.legs
    lots = [0] * len(legs)
    free = defaultdict(float, {a: p.cash.get(a, 0.0) - p.cash_floor.get(a, 0.0) for a in p.cash})
    total_cash = sum(p.cash.values())
    sell_turnover = p.turnover_cap - MARGIN_RUB
    turnover = p.turnover_cap + p.cash_flow - MARGIN_RUB
    room = dict(p.issuer_room)

    def usable(k: int) -> bool:
        return k not in p.banned and legs[k].max_lots > 0

    for bucket, need in sorted(p.sell_needs.items(), key=lambda kv: (-kv[1], kv[0])):
        cands = [k for k, g in enumerate(legs) if g.side == "sell" and g.bucket == bucket and usable(k)]
        cands.sort(key=lambda k: (legs[k].tax_per_lot / legs[k].lot_value + legs[k].cost_rate + legs[k].pref, k))
        for k in cands:
            g = legs[k]
            n = min(g.max_lots, math.floor(min(need, turnover, sell_turnover) / g.lot_value))
            if n <= 0 or n * g.lot_value < p.min_trade:
                continue
            lots[k] = n
            need -= n * g.lot_value
            turnover -= n * g.lot_value
            sell_turnover -= n * g.lot_value
            free[g.account_id] += n * g.cash_per_lot
            total_cash += n * g.cash_per_lot

    def shortfall(b: str) -> float:
        return p.windows[b].lo - p.bucket_values.get(b, 0.0) if b in p.windows else 0.0

    bought = sorted({g.bucket for g in legs if g.side == "buy"}, key=lambda b: (-shortfall(b), b))
    for bucket in bought:
        cap_room = p.caps.get(bucket, math.inf) - p.bucket_values.get(bucket, 0.0) - MARGIN_RUB
        tickers = sorted({g.ticker for g in legs if g.side == "buy" and g.bucket == bucket},
                         key=lambda t: (-p.buy_targets.get(t, 0.0), t))
        for ticker in tickers:
            want = p.buy_targets.get(ticker, 0.0)
            cands = [k for k, g in enumerate(legs) if g.side == "buy" and g.ticker == ticker and usable(k)]
            cands.sort(key=lambda k: (-free[legs[k].account_id], k))
            for k in cands:
                g = legs[k]
                unit = -g.cash_per_lot
                limit = min(want, cap_room, turnover, room.get(g.issuer, math.inf) if g.issuer else math.inf)
                cash_lim = min(free[g.account_id], total_cash - p.reserve) - MARGIN_RUB
                n = min(g.max_lots, math.floor(limit / g.lot_value), math.floor(cash_lim / unit))
                if n <= 0 or n * g.lot_value < p.min_trade:
                    continue
                lots[k] = n
                amount = n * g.lot_value
                want -= amount
                cap_room -= amount
                turnover -= amount
                if g.issuer and g.issuer in room:
                    room[g.issuer] -= amount
                free[g.account_id] -= n * unit
                total_cash -= n * unit
    return tuple(lots)
