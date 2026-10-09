"""Параметры конструктора аллокации по умолчанию: config/allocation.yaml."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field, field_validator

from investbot.allocation.models import StressScenario
from investbot.core.models import Strict
from investbot.rebalance.ips import KNOWN_BUCKETS

DEFAULT_LAMBDA = Decimal("0.1")
GRID_STEP = Decimal("0.005")


class AllocationConfig(Strict):
    version: int = Field(ge=1)
    growth_buckets: tuple[str, ...] = ("equity_ru", "corp_ig", "corp_hy")
    diversification_lambda: Decimal = Field(default=DEFAULT_LAMBDA, ge=0)
    grid_step: Decimal = Field(default=GRID_STEP, gt=0, le=Decimal("0.05"))
    max_cvar95_weekly: Decimal | None = Field(default=None, gt=0, le=1)
    scenarios: tuple[StressScenario, ...]

    @field_validator("growth_buckets")
    @classmethod
    def _known(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        unknown = set(v) - set(KNOWN_BUCKETS)
        if unknown:
            raise ValueError(f"growth_buckets: неизвестные бакеты {sorted(unknown)}")
        return v


def load_allocation_config(path: Path) -> AllocationConfig:
    raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    return AllocationConfig.model_validate(raw)
