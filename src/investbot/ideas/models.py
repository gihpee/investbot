"""Идеи для входа (§12): контракт между генератором, лентой в боте, журналом и бумажным исполнением.

v1 (Ф2b): только облигации, детерминированная метрика «carry + roll-down − ожидаемые потери − издержки».
Пометка для пользователя: «Детерминированная метрика, качество статистически не подтверждено».
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import Field

from investbot.core.models import AssetClass, Strict

PriceUnit = Literal["pct_nominal", "rub"]
V1_NOTE = "Детерминированная метрика, качество статистически не подтверждено"


class EntryIdea(Strict):
    idea_id: str  # стабильный ключ: "<arm>:<ticker>:<as_of>"
    as_of: date
    asset_class: AssetClass
    ticker: str  # для облигаций — SECID MOEX
    isin: str | None = None
    name: str
    issuer: str | None = None
    bucket: str  # бакет IPS, в который попадёт покупка
    source_arm: str  # "bond_carry_roll", позже "fund_carrier", "eq_dividend", …
    score: Decimal  # ожидаемая механическая избыточная доходность за горизонт (доля, 0.012 = 1,2%)
    metrics: dict[str, Decimal] = Field(default_factory=dict)  # ytm, d_mod, g_spread_bp, carry, roll, el, …
    price: Decimal  # текущая цена в единицах price_unit
    price_unit: PriceUnit
    entry_low: Decimal
    entry_high: Decimal
    target_yield: Decimal | None = None
    valid_until: date
    horizon_weeks: int
    benchmark: str  # "RGBITR", "RUCBTRNS", …
    invalidation: tuple[str, ...]  # условия отмены (правила в коде; здесь — формулировка для пользователя)
    reasons: tuple[str, ...]  # «за»: из чего сложилась оценка
    risks: tuple[str, ...]  # «против»: обязательный блок (§13.1)
    for_underweight_bucket: bool = False  # покупка приближает портфель к IPS
    note: str = V1_NOTE


class IdeaCandidate(Strict):
    """Кандидат из топ-20 (§12.2 п. 8): пишется в журнал вместе с флагом показа — для честной оценки."""

    idea: EntryIdea
    rank: int
    shown: bool
