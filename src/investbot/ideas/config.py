"""Параметры генератора идей v1 (облигации): config/ideas.yaml (ARCHITECTURE §12.1–12.3)."""

from __future__ import annotations

from decimal import Decimal
from itertools import pairwise
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field, model_validator

from investbot.core.models import Strict
from investbot.portfolio.buckets import RATING_ORDINAL
from investbot.rebalance.ips import KNOWN_BUCKETS


class BenchmarkRule(Strict):
    """Бенчмарк бакета; для ОФЗ — по дюрации: первое правило с D_mac ≤ max_duration_years (None — без границы)."""

    index: str
    max_duration_years: Decimal | None = Field(default=None, gt=0)


class IdeasConfig(Strict):
    version: int = Field(ge=1)
    # горизонт и срок жизни идеи
    horizon_weeks: int = Field(default=26, ge=1, le=52)  # горизонт = min(этого, срок до оферты/погашения)
    valid_days: int = Field(default=21, ge=1, le=60)  # 2–4 недели (§12.3)
    min_days_to_event: int = Field(default=14, ge=0)  # оферта/выкуп/погашение ближе — бумага исключается
    # вселенная
    min_duration_years: Decimal = Field(default=Decimal("0.5"), gt=0)  # дюрация Маколея
    max_duration_years: Decimal = Field(default=Decimal(8), gt=0)
    min_trades: int = Field(default=10, ge=0)  # NUMTRADES за одну сессию — прокси ликвидности
    min_val_today: Decimal = Field(default=Decimal(2_000_000), ge=0)  # VALTODAY, ₽ — прокси ADV20 (§7.2)
    # барьер издержек и план входа
    min_alpha_net: Decimal = Decimal("0.003")  # доля за горизонт, после круговых издержек и ожидаемых потерь
    target_mad_k: Decimal = Field(default=Decimal("0.5"), ge=0)  # m в y_target = G(D) + median + m·MAD
    min_group_size: int = Field(default=5, ge=2)  # меньше — групповой сигнал не считается
    spread_cancel_quantile: Decimal = Field(default=Decimal("0.9"), gt=0, le=1)  # отмена: спред выше p90 группы
    duration_bucket_edges: tuple[Decimal, ...] = (Decimal(1), Decimal(2), Decimal(3), Decimal(5))  # годы
    entry_buffer_pct: Decimal = Field(default=Decimal("0.1"), ge=0)  # ширина зоны входа, п. % номинала
    rate_shocks_bp: tuple[int, ...] = (100, 300)  # сценарии роста доходности для блока «против»
    # разнообразие (§12.2, п. 6)
    max_ideas: int = Field(default=5, ge=0)
    max_per_class: int = Field(default=3, ge=1)
    max_per_issuer: int = Field(default=1, ge=1)
    max_candidates: int = Field(default=20, ge=1)  # топ-N кандидатов в журнал; показанные выбираются из них
    # кредитный риск — ДОПУЩЕНИЯ
    lgd: Decimal = Field(default=Decimal("0.65"), ge=0, le=1)
    pd_annual: dict[str, Decimal]  # канонический рейтинг ("AA-") → годовая PD (доля)
    # бенчмарки по бакетам
    benchmarks: dict[str, tuple[BenchmarkRule, ...]] = Field(default_factory=dict)
    default_benchmark: str = "RGBITR"

    @model_validator(mode="after")
    def _check(self) -> IdeasConfig:
        if self.min_duration_years >= self.max_duration_years:
            raise ValueError("min_duration_years должен быть меньше max_duration_years")
        edges = self.duration_bucket_edges
        if any(e <= 0 for e in edges) or any(b <= a for a, b in pairwise(edges)):
            raise ValueError("duration_bucket_edges: положительные и строго возрастающие")
        unknown = set(self.pd_annual) - set(RATING_ORDINAL)
        if unknown:
            raise ValueError(f"pd_annual: нераспознанные рейтинги {sorted(unknown)}")
        if any(not Decimal(0) <= v <= Decimal(1) for v in self.pd_annual.values()):
            raise ValueError("pd_annual: PD — доля в [0, 1]")
        by_grade = sorted(self.pd_annual.items(), key=lambda kv: RATING_ORDINAL[kv[0]])
        if any(b[1] < a[1] for a, b in pairwise(by_grade)):
            raise ValueError("pd_annual: PD не должна убывать при ухудшении рейтинга")
        bad = set(self.benchmarks) - set(KNOWN_BUCKETS)
        if bad:
            raise ValueError(f"benchmarks: неизвестные бакеты {sorted(bad)}")
        if any(s <= 0 for s in self.rate_shocks_bp):
            raise ValueError("rate_shocks_bp: сдвиги положительные, в б.п.")
        return self

    def benchmark(self, bucket: str, d_mac: Decimal) -> str:
        for rule in self.benchmarks.get(bucket, ()):
            if rule.max_duration_years is None or d_mac <= rule.max_duration_years:
                return rule.index
        return self.default_benchmark


def load_ideas_config(path: Path) -> IdeasConfig:
    raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    return IdeasConfig.model_validate(raw)
