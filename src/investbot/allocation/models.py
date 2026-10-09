"""Модели конструктора аллокации (ARCHITECTURE §10.8).

Соглашения о знаках (везде, где число — доходность портфеля):
- доходность отрицательна при убытке: `stress`, `max_drawdown_hist`, `cvar95_weekly` обычно ≤ 0;
- лимиты риска — положительные доли убытка: `RiskProfile.max_drawdown = 0.20` означает «не хуже −20%».
Доли бакетов — доли единицы (0.05 = 5%), п.п. в правках — тоже доли (0.05 = 5 п.п.).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Literal

import numpy as np
from numpy.typing import NDArray
from pydantic import Field, model_validator

from investbot.core.models import Strict
from investbot.core.money import ONE, ZERO
from investbot.rebalance.ips import KNOWN_BUCKETS

BUCKET_TITLES: dict[str, str] = {
    "equity_ru": "Акции РФ",
    "ofz_fixed": "ОФЗ с фиксированным купоном",
    "ofz_float_linker": "ОФЗ-флоатеры и линкеры",
    "corp_ig": "Корпоративные облигации IG",
    "corp_hy": "ВДО",
    "money_market": "Денежный рынок",
    "gold": "Золото",
    "dfa": "ЦФА",
    "fx_cash": "Валютный кэш",
}


def bucket_title(bucket: str) -> str:
    return BUCKET_TITLES.get(bucket, bucket)


class AllocationInfeasible(ValueError):
    """Ограничения анкеты несовместимы: ни одна аллокация им не удовлетворяет. Сообщение — по-русски."""


def _check_known(names: set[str], field: str) -> None:
    unknown = names - set(KNOWN_BUCKETS)
    if unknown:
        raise ValueError(f"{field}: неизвестные бакеты {sorted(unknown)}; допустимы: {', '.join(KNOWN_BUCKETS)}")


def _check_shares(values: dict[str, Decimal], field: str) -> None:
    bad = {k: v for k, v in values.items() if not ZERO <= v <= ONE}
    if bad:
        raise ValueError(f"{field}: доли должны быть в [0, 1]: {bad}")


class RiskProfile(Strict):
    """Ограничения из анкеты IPS, общие для всех вариантов."""

    horizon_years: int = Field(ge=0)
    max_drawdown: Decimal = Field(gt=0, le=1, description="допустимая просадка, положительная доля (0.20 = −20%)")
    needs_income: bool = False
    bucket_min: dict[str, Decimal] = Field(default_factory=dict)
    bucket_max: dict[str, Decimal] = Field(default_factory=dict)
    excluded: tuple[str, ...] = ()
    fixed: dict[str, Decimal] = Field(
        default_factory=dict, description="бакеты с долей-параметром анкеты (например, dfa); не оптимизируются"
    )
    growth_buckets: tuple[str, ...] = ("equity_ru", "corp_ig", "corp_hy")

    @model_validator(mode="after")
    def _check(self) -> RiskProfile:
        for field, names in (
            ("bucket_min", set(self.bucket_min)),
            ("bucket_max", set(self.bucket_max)),
            ("excluded", set(self.excluded)),
            ("fixed", set(self.fixed)),
            ("growth_buckets", set(self.growth_buckets)),
        ):
            _check_known(names, field)
        _check_shares(self.bucket_min, "bucket_min")
        _check_shares(self.bucket_max, "bucket_max")
        _check_shares(self.fixed, "fixed")
        for name, low in self.bucket_min.items():
            high = self.bucket_max.get(name, ONE)
            if low > high:
                raise ValueError(f"{name}: минимум {low} больше максимума {high}")
        clash = {b for b in self.excluded if self.fixed.get(b, ZERO) > 0}
        if clash:
            raise ValueError(f"бакеты одновременно исключены и зафиксированы с ненулевой долей: {sorted(clash)}")
        if sum(self.fixed.values(), ZERO) > ONE:
            raise ValueError("сумма фиксированных долей больше 100%")
        return self


@dataclass(frozen=True, eq=False)
class ReturnsData:
    """Недельные простые доходности прокси-рядов бакетов.

    `returns[t, i]` — доходность бакета `buckets[i]` за неделю, закончившуюся `dates[t]`.
    Ряды должны быть выровнены вызывающим кодом (общий диапазон дат, без пропусков).
    """

    buckets: tuple[str, ...]
    dates: tuple[date, ...]
    returns: NDArray[np.float64]

    def __post_init__(self) -> None:
        arr = np.array(self.returns, dtype=np.float64)
        if arr.ndim != 2:
            raise ValueError(f"returns должен быть матрицей T×N, получено измерений: {arr.ndim}")
        if arr.shape != (len(self.dates), len(self.buckets)):
            raise ValueError(f"returns {arr.shape} не совпадает с (дат={len(self.dates)}, бакетов={len(self.buckets)})")
        if not self.buckets:
            raise ValueError("нет бакетов")
        if len(set(self.buckets)) != len(self.buckets):
            raise ValueError("бакеты повторяются")
        if len(self.dates) < 2:
            raise ValueError("нужно не меньше двух недель истории")
        if any(b <= a for a, b in zip(self.dates, self.dates[1:], strict=False)):
            raise ValueError("даты должны строго возрастать")
        if not np.all(np.isfinite(arr)) or np.any(arr <= -1):
            raise ValueError("доходности должны быть конечными и больше −100%")
        arr.setflags(write=False)
        object.__setattr__(self, "returns", arr)

    def column(self, bucket: str) -> NDArray[np.float64]:
        return self.returns[:, self.buckets.index(bucket)]


class StressScenario(Strict):
    """Стресс-сценарий.

    historical — окно [start, end]: накопленная доходность бакета за недели, закончившиеся в окне
    (купить и держать с начала окна, поэтому доходность портфеля линейна по долям).
    shock — гипотетические мгновенные шоки по бакетам; бакеты без шока считаются неизменными (0).
    """

    name: str = Field(min_length=1)
    kind: Literal["historical", "shock"]
    start: date | None = None
    end: date | None = None
    shocks: dict[str, Decimal] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check(self) -> StressScenario:
        if self.kind == "historical":
            if self.start is None or self.end is None:
                raise ValueError(f"{self.name}: историческому сценарию нужны start и end")
            if self.start > self.end:
                raise ValueError(f"{self.name}: start позже end")
            if self.shocks:
                raise ValueError(f"{self.name}: у исторического сценария не бывает shocks")
        else:
            if not self.shocks:
                raise ValueError(f"{self.name}: шоковому сценарию нужны shocks")
            if self.start is not None or self.end is not None:
                raise ValueError(f"{self.name}: у шокового сценария не бывает окна дат")
            _check_known(set(self.shocks), f"{self.name}.shocks")
            if any(v <= -1 for v in self.shocks.values()):
                raise ValueError(f"{self.name}: шок не может быть хуже −100%")
        return self


class VariantMetrics(Strict):
    """Метрики на истории, не прогноз. Знаки — см. докстринг модуля."""

    vol_annual: Decimal  # σ недельной доходности × √52
    max_drawdown_hist: Decimal  # ≤ 0: худшее падение от пика портфеля с еженедельной ребалансировкой
    cvar95_weekly: Decimal  # средняя доходность худших 5% недель (≤ 0 при убытке)
    stress: dict[str, Decimal]  # доходность портфеля по сценарию (только сценарии, покрытые данными)
    current_yield: Decimal | None  # Σ w·текущая доходность бакета; не прогноз
    turnover_from_current: Decimal  # ½Σ|w − w0|


class AllocationVariant(Strict):
    code: Literal["A", "B", "C"]
    title: str
    weights: dict[str, Decimal]
    metrics: VariantMetrics
    violations: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()


EditOp = Literal["shift_bucket", "less_risk", "more_risk", "exclude", "set_weight"]


class AllocationEdit(Strict):
    """Правка черновика. `value` — п.п. как доля (0.05 = 5 п.п.); для set_weight — сама доля."""

    op: EditOp
    bucket: str | None = None
    value: Decimal | None = None

    @model_validator(mode="after")
    def _check(self) -> AllocationEdit:
        if self.bucket is not None:
            _check_known({self.bucket}, "bucket")
        needs_bucket = self.op in ("shift_bucket", "exclude", "set_weight")
        if needs_bucket and self.bucket is None:
            raise ValueError(f"{self.op}: нужен bucket")
        if self.op in ("shift_bucket", "set_weight") and self.value is None:
            raise ValueError(f"{self.op}: нужен value")
        if self.value is not None:
            if self.op == "set_weight" and not ZERO <= self.value <= ONE:
                raise ValueError("set_weight: доля должна быть в [0, 1]")
            if self.op in ("less_risk", "more_risk") and not ZERO < self.value <= ONE:
                raise ValueError(f"{self.op}: сдвиг должен быть в (0, 1]")
            if self.op == "shift_bucket" and not (self.value != 0 and abs(self.value) <= ONE):
                raise ValueError("shift_bucket: сдвиг должен быть ненулевым и не больше 100 п.п.")
        return self


class DraftDiff(Strict):
    before: dict[str, Decimal]
    after: dict[str, Decimal]
    metrics_before: VariantMetrics
    metrics_after: VariantMetrics
    violations: tuple[str, ...] = ()
