"""Аналитика облигаций (ARCHITECTURE §7.2): кривая КБД и механические метрики «carry + roll-down − потери».

Модуль чистый: без сети, БД и часов; вся арифметика — Decimal (детерминированно и воспроизводимо).

Единицы — одни и те же во всех функциях:
- доходности и ставки — доли годовых, **эффективные** (годовая капитализация): 0.15 = 15%.
  ISS EFFECTIVEYIELD и КБД `yearyields` уже эффективные. Простые ставки (RUONIA, ключевая) переводятся
  через `effective_from_nominal` — иначе carry к денежному рынку занижается на ~1 п.п. при ставке 14%;
- сроки, дюрации и горизонт `h` — годы; ISS DURATION приходит в днях (Маколея) → `years_from_days` (база 365);
- `carry`, `roll_down`, `expected_loss`, `mechanical_excess`, издержки — доли цены **за горизонт h** (не годовые);
- спреды — доли; в базисных пунктах — только то, что явно названо `*_bp` (1 б.п. = 0.0001).

Упрощения MVP (документированы, проверить при переходе к v2):
- КБД строится по опубликованным точкам ISS `yearyields` (11 сроков 0,25…20 лет) с линейной интерполяцией,
  а не пересчётом параметров Нельсона–Сигеля (B1–B3, T1, G1–G9). Между узлами расхождение с параметрической
  G(t) — единицы б.п. на крутых участках кривой;
- срок на кривой для бумаги — дюрация Маколея (бескупонный эквивалент; так же считает ISS GSPREADBP),
  ценовая чувствительность — модифицированная дюрация `D_mod = D_mac / (1 + y)`;
- формулы первого порядка, как в §7.2: carry `(y − r_f)·h` вместо `(1+y)^h − (1+r_f)^h`
  (при y = 16%, r_f = 14%, h = 0,5 линейная формула завышает carry на ≈ 0,07 п.п. — сознательное упрощение
  ради совпадения с формулами документации); roll-down по текущей D_mod; ΔP ≈ −D_mod·Δy·P без выпуклости;
- кривая и спред на горизонте предполагаются неизменными (механический прогноз, не прогноз ставок).
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from itertools import pairwise
from typing import Protocol

from investbot.core.money import ONE, ZERO
from investbot.portfolio.buckets import RATING_ORDINAL, rating_ordinal

DAYS_PER_YEAR = Decimal(365)
BP = Decimal(10000)
HUNDRED = Decimal(100)
_RATING_LABEL = {v: k for k, v in RATING_ORDINAL.items()}


class CurvePointLike(Protocol):
    """Точка КБД: adapters.moex_iss.models.CurvePoint и любые объекты с теми же полями."""

    @property
    def period(self) -> Decimal: ...  # срок, лет

    @property
    def value(self) -> Decimal: ...  # доходность, % годовых (эффективная)


@dataclass(frozen=True)
class Curve:
    """КБД `G(t)`: срок в годах → эффективная доходность (доля).

    Линейная интерполяция между узлами (монотонно-безопасна: значение между соседними узлами всегда лежит
    между их доходностями, без «перелётов», как у кубических сплайнов) и плоская экстраполяция за краями.
    """

    tenors: tuple[Decimal, ...]
    yields: tuple[Decimal, ...]

    def __post_init__(self) -> None:
        if len(self.tenors) != len(self.yields):
            raise ValueError("КБД: число сроков и доходностей не совпадает")
        if len(self.tenors) < 2:
            raise ValueError("КБД: нужно хотя бы две точки")
        if self.tenors[0] < 0:
            raise ValueError("КБД: срок не может быть отрицательным")
        if any(b <= a for a, b in pairwise(self.tenors)):
            raise ValueError("КБД: сроки должны строго возрастать (дубли сроков — несколько снимков в одном наборе?)")

    @classmethod
    def from_knots(cls, knots: Iterable[tuple[Decimal, Decimal]]) -> Curve:
        """Узлы (срок в годах, доходность-доля) в любом порядке."""
        ordered = sorted(knots, key=lambda k: k[0])
        return cls(tuple(t for t, _ in ordered), tuple(y for _, y in ordered))

    @classmethod
    def from_points(cls, points: Iterable[CurvePointLike]) -> Curve:
        """Точки ISS `yearyields` (доходность в %) → кривая в долях."""
        return cls.from_knots((p.period, p.value / HUNDRED) for p in points)

    def __call__(self, t: Decimal) -> Decimal:
        ts, ys = self.tenors, self.yields
        if t <= ts[0]:
            return ys[0]
        if t >= ts[-1]:
            return ys[-1]
        i = bisect_right(ts, t)  # ts[i-1] <= t < ts[i]
        t0, t1, y0, y1 = ts[i - 1], ts[i], ys[i - 1], ys[i]
        return y0 + (y1 - y0) * (t - t0) / (t1 - t0)


# --- единицы ---------------------------------------------------------------------------------------------


def years_from_days(days: Decimal) -> Decimal:
    """ISS DURATION (дни) → годы, база 365."""
    return days / DAYS_PER_YEAR


def to_bp(x: Decimal) -> Decimal:
    return x * BP


def from_bp(bp: Decimal) -> Decimal:
    return bp / BP


def effective_from_nominal(rate: Decimal, periods_per_year: int) -> Decimal:
    """Простая годовая ставка с капитализацией m раз в год → эффективная: (1 + r/m)^m − 1.

    RUONIA и доходность фонда денежного рынка — ежедневное начисление: m = 365.
    """
    if periods_per_year < 1:
        raise ValueError("periods_per_year должен быть ≥ 1")
    m = Decimal(periods_per_year)
    return (ONE + rate / m) ** periods_per_year - ONE


# --- метрики бумаги ---------------------------------------------------------------------------------------


def d_mod(d_mac: Decimal, y: Decimal) -> Decimal:
    """Модифицированная дюрация (годы) из дюрации Маколея (годы) и эффективной доходности (доля)."""
    return d_mac / (ONE + y)


def g_spread(y: Decimal, curve: Curve, tenor: Decimal) -> Decimal:
    """G-спред (доля): `y − G(tenor)`, tenor — дюрация Маколея в годах."""
    return y - curve(tenor)


def carry(y: Decimal, r_f: Decimal, h: Decimal) -> Decimal:
    """Carry к денежному рынку за горизонт h лет (доля цены): `(y − r_f)·h`."""
    return (y - r_f) * h


def roll_down(d_mod: Decimal, curve: Curve, tenor: Decimal, h: Decimal) -> Decimal:
    """Roll-down за h лет (доля цены): `D_mod·(G(D) − G(D − h))` при неизменных кривой и спреде.

    Срок `D − h` ограничен снизу нулём (короткий конец кривой плоский), растущая кривая даёт > 0.
    """
    return d_mod * (curve(tenor) - curve(max(tenor - h, ZERO)))


def pd_for_rating(rating: str | None, pd_table: Mapping[str, Decimal]) -> Decimal | None:
    """Годовая PD по таблице, ключи — канонические буквенные рейтинги ("AA-"). «ruAA-», «AA-(RU)» распознаются."""
    ordinal = rating_ordinal(rating)
    if ordinal is None:
        return None
    return pd_table.get(_RATING_LABEL[ordinal])


def expected_loss(rating: str | None, h: Decimal, pd_table: Mapping[str, Decimal], lgd: Decimal) -> Decimal:
    """Ожидаемые кредитные потери за h лет (доля номинала): `PD(рейтинг)·LGD·h`.

    PD и LGD — допущения из конфига. Для ОФЗ функция не вызывается (суверенный риск принят равным нулю).
    """
    pd = pd_for_rating(rating, pd_table)
    if pd is None:
        raise ValueError(f"нет PD для рейтинга {rating!r}")
    return pd * lgd * h


def breakeven_bp(carry: Decimal, roll: Decimal, d_mod: Decimal) -> Decimal:
    """На сколько б.п. может вырасти доходность за горизонт, прежде чем бумага проиграет денежному рынку:
    `(carry + roll) / D_mod`."""
    if d_mod <= 0:
        raise ValueError("D_mod должна быть положительной")
    return to_bp((carry + roll) / d_mod)


def mechanical_excess(carry: Decimal, roll: Decimal, el: Decimal, cost: Decimal) -> Decimal:
    """Механическая ожидаемая избыточная доходность за горизонт (доля): `carry + roll − EL − c`.

    `c` — издержки за весь горизонт (вход и выход), а не в год.
    """
    return carry + roll - el - cost


def price_for_yield(price: Decimal, d_mod: Decimal, dy: Decimal) -> Decimal:
    """Цена после сдвига доходности на dy (доля): `P·(1 − D_mod·dy)` — первый порядок, без выпуклости.

    Для чистой цены в % номинала: при росте доходности на 1 п.п. ошибка ≈ ½·C·dy² (единицы б.п. цены)
    плюс доля НКД в грязной цене. Для зоны входа в пределах ±100 б.п. этого достаточно.
    """
    return price * (ONE - d_mod * dy)


@dataclass(frozen=True)
class BondMetrics:
    """Метрики одной бумаги на горизонт h. Доли, кроме явно названных *_bp и лет."""

    y: Decimal
    d_mac: Decimal
    d_mod: Decimal
    g_curve: Decimal  # G(D_mac)
    g_spread: Decimal
    carry: Decimal
    roll: Decimal
    el: Decimal
    cost: Decimal
    alpha_net: Decimal  # mechanical_excess
    breakeven_bp: Decimal


def bond_metrics(
    *,
    y: Decimal,
    d_mac: Decimal,
    r_f: Decimal,
    curve: Curve,
    h: Decimal,
    pd_annual: Decimal,
    lgd: Decimal,
    cost: Decimal,
) -> BondMetrics:
    """Все метрики §7.2 разом. `pd_annual` — годовая PD (0 для ОФЗ), `cost` — круговые издержки за горизонт."""
    dm = d_mod(d_mac, y)
    c = carry(y, r_f, h)
    r = roll_down(dm, curve, d_mac, h)
    el = pd_annual * lgd * h
    return BondMetrics(
        y=y,
        d_mac=d_mac,
        d_mod=dm,
        g_curve=curve(d_mac),
        g_spread=g_spread(y, curve, d_mac),
        carry=c,
        roll=r,
        el=el,
        cost=cost,
        alpha_net=mechanical_excess(c, r, el, cost),
        breakeven_bp=breakeven_bp(c, r, dm),
    )


# --- робастная статистика групп «рейтинг × дюрация» --------------------------------------------------------


def median(xs: Sequence[Decimal]) -> Decimal:
    if not xs:
        raise ValueError("медиана пустой выборки")
    s = sorted(xs)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2


def mad(xs: Sequence[Decimal]) -> Decimal:
    """Медианное абсолютное отклонение без масштабного множителя 1,4826: `median(|x − median(x)|)`."""
    m = median(xs)
    return median([abs(x - m) for x in xs])


def quantile(xs: Sequence[Decimal], q: Decimal) -> Decimal:
    """Квантиль с линейной интерполяцией между порядковыми статистиками (как numpy `linear`)."""
    if not xs:
        raise ValueError("квантиль пустой выборки")
    if not ZERO <= q <= ONE:
        raise ValueError("q должен быть в [0, 1]")
    s = sorted(xs)
    pos = (len(s) - 1) * q
    lo = int(pos)  # pos ≥ 0, int() — пол
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)
