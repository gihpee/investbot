"""IPS — целевая аллокация, утверждённая пользователем (ARCHITECTURE §10.1, §10.8).

Полоса бакета: действует более узкая из абсолютной (п.п.) и относительной (доля от цели).
Бакеты только с `cap` (без полос) срабатывают лишь при превышении потолка.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field, model_validator

from investbot.core.models import Strict
from investbot.core.money import ONE, ZERO

TOLERANCE = Decimal("0.0001")

# Бакеты, которые понимает классификатор (portfolio/buckets.py). Пользователь может не задавать все.
KNOWN_BUCKETS = (
    "equity_ru",
    "ofz_fixed",
    "ofz_float_linker",
    "corp_ig",
    "corp_hy",
    "money_market",
    "gold",
    "dfa",
    "fx_cash",
)


class BucketTarget(Strict):
    target: Decimal = Field(ge=0, le=1)
    band_abs: Decimal | None = Field(default=None, gt=0)
    band_rel: Decimal | None = Field(default=None, gt=0)
    cap: Decimal | None = Field(default=None, ge=0, le=1)

    def band(self) -> Decimal | None:
        candidates = []
        if self.band_abs is not None:
            candidates.append(self.band_abs)
        if self.band_rel is not None and self.target > 0:
            candidates.append(self.band_rel * self.target)
        return min(candidates) if candidates else None

    def bounds(self) -> tuple[Decimal, Decimal]:
        """Допустимый коридор доли бакета."""
        band = self.band()
        if band is None:
            upper = self.cap if self.cap is not None else ONE
            return ZERO, upper
        low = max(ZERO, self.target - band)
        high = min(ONE, self.target + band)
        if self.cap is not None:
            high = min(high, self.cap)
        return low, high


class Limits(Strict):
    max_issuer_total: Decimal = Decimal("0.10")
    max_sector: Decimal = Decimal("0.25")
    min_trade_rub: Decimal = Decimal(3000)
    max_weekly_turnover: Decimal = Decimal("0.10")
    cash_reserve_rub: Decimal = Decimal(10000)
    exclude_issuers: tuple[str, ...] = ()


class Rules(Strict):
    return_to_band_fraction: Decimal = Decimal("0.875")
    cashflow_first: bool = True
    ig_min_rating: str = "A-"


class IPS(Strict):
    version: int = Field(ge=0)
    buckets: dict[str, BucketTarget]
    instrument_buckets: dict[str, str] = Field(
        default_factory=dict, description="ручное отнесение тикера к бакету, перекрывает классификатор"
    )
    limits: Limits = Limits()
    rules: Rules = Rules()

    @model_validator(mode="after")
    def _check(self) -> IPS:
        unknown = set(self.buckets) - set(KNOWN_BUCKETS)
        if unknown:
            raise ValueError(f"неизвестные бакеты: {sorted(unknown)}; допустимы: {', '.join(KNOWN_BUCKETS)}")
        total = sum((b.target for b in self.buckets.values()), ZERO)
        if abs(total - ONE) > TOLERANCE:
            raise ValueError(f"сумма целевых долей = {total}, должна быть 1")
        for name, b in self.buckets.items():
            if b.cap is not None and b.target > b.cap:
                raise ValueError(f"{name}: цель {b.target} выше потолка {b.cap}")
        bad = {t: b for t, b in self.instrument_buckets.items() if b not in KNOWN_BUCKETS}
        if bad:
            raise ValueError(f"instrument_buckets ссылается на неизвестные бакеты: {bad}")
        return self


def load_ips(path: Path) -> IPS:
    raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    return IPS.model_validate(raw)
