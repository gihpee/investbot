"""Сборка новой версии IPS из утверждённых долей (конструктор аллокации, §10.8) и запись в YAML."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from investbot.core.money import ZERO
from investbot.rebalance.ips import IPS, BucketTarget

# Потолок для бакетов с нулевой целью: уже купленное не заставляем продавать немедленно.
ZERO_TARGET_CAPS = {"corp_hy": Decimal("0.03"), "fx_cash": Decimal("0.05")}
DEFAULT_ZERO_CAP = Decimal("0.02")
DFA_CAP_HEADROOM = Decimal("0.03")


def default_band(bucket: str, target: Decimal) -> BucketTarget:
    if target == ZERO:
        return BucketTarget(target=ZERO, cap=ZERO_TARGET_CAPS.get(bucket, DEFAULT_ZERO_CAP))
    if bucket == "dfa":
        # ЦФА держатся до погашения и не продаются ради ребалансировки: только потолок
        return BucketTarget(target=target, cap=min(Decimal(1), target + DFA_CAP_HEADROOM))
    band_abs = Decimal("0.05") if target >= Decimal("0.20") else Decimal("0.04") if target >= Decimal("0.10") \
        else Decimal("0.03")
    band_rel = Decimal("0.50") if bucket == "gold" else Decimal("0.25")
    return BucketTarget(target=target, band_abs=band_abs, band_rel=band_rel)


def build_ips(weights: Mapping[str, Decimal], previous: IPS | None) -> IPS:
    """Новая версия: доли и полосы — из утверждённого варианта, лимиты и правила — из прошлой версии."""
    buckets = {b: default_band(b, w) for b, w in weights.items()}
    return IPS(
        version=(previous.version + 1) if previous else 1,
        buckets=buckets,
        instrument_buckets=previous.instrument_buckets if previous else {},
        carriers=previous.carriers if previous else {},
        limits=previous.limits if previous else IPS.model_fields["limits"].default,
        rules=previous.rules if previous else IPS.model_fields["rules"].default,
    )


def _plain(o: Any) -> Any:
    if isinstance(o, Decimal):
        return float(o)
    if isinstance(o, dict):
        return {k: _plain(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_plain(v) for v in o]
    return o


def save_ips(ips: IPS, path: Path) -> None:
    data = _plain(ips.model_dump(exclude_none=True))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        "# Создано конструктором аллокации. Правка вручную допустима; версия увеличивается при утверждении.\n"
        + yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    tmp.replace(path)
