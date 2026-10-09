"""Дрейф портфеля относительно IPS: какие бакеты вышли из коридора."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Literal

from investbot.core.models import Strict
from investbot.core.money import ZERO
from investbot.rebalance.ips import IPS

DriftStatus = Literal["ok", "below", "above", "cap_breach", "not_in_ips"]


class BucketDrift(Strict):
    bucket: str
    weight: Decimal
    target: Decimal | None
    low: Decimal | None
    high: Decimal | None
    deviation: Decimal  # weight − target (для бакетов вне IPS — сама доля)
    status: DriftStatus


def compute_drift(weights: Mapping[str, Decimal], ips: IPS) -> list[BucketDrift]:
    """Сравнивает доли бакетов с целями. Сортировка: сначала нарушения, затем по |отклонению|."""
    out: list[BucketDrift] = []
    for name, bt in ips.buckets.items():
        w = weights.get(name, ZERO)
        low, high = bt.bounds()
        status: DriftStatus = "ok"
        if bt.cap is not None and w > bt.cap:
            status = "cap_breach"
        elif w < low:
            status = "below"
        elif w > high:
            status = "above"
        out.append(
            BucketDrift(bucket=name, weight=w, target=bt.target, low=low, high=high,
                        deviation=w - bt.target, status=status)
        )
    for name, w in weights.items():
        if name not in ips.buckets and w > 0:
            out.append(
                BucketDrift(bucket=name, weight=w, target=None, low=None, high=None,
                            deviation=w, status="not_in_ips")
            )
    out.sort(key=lambda d: (d.status == "ok", -abs(d.deviation)))
    return out


def needs_rebalance(drift: list[BucketDrift]) -> bool:
    return any(d.status != "ok" for d in drift)
