"""Оценка портфеля в рублях и доли по бакетам."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from decimal import Decimal

from investbot.core.models import PortfolioSnapshot, Position, Strict
from investbot.core.money import ZERO, safe_div
from investbot.portfolio.buckets import UNCLASSIFIED, classify


class PositionView(Strict):
    account_id: str
    ticker: str
    name: str
    bucket: str
    value_rub: Decimal | None
    weight: Decimal
    source: str


class PortfolioValuation(Strict):
    total_rub: Decimal
    bucket_values: dict[str, Decimal]
    bucket_weights: dict[str, Decimal]
    positions: tuple[PositionView, ...]
    unpriced: tuple[str, ...]  # позиции без цены — в стоимость не вошли
    unclassified: tuple[str, ...]  # тикеры без бакета


def to_rub(amount: Decimal, currency: str, fx: Mapping[str, Decimal]) -> Decimal | None:
    cur = currency.lower()
    if cur == "rub":
        return amount
    rate = fx.get(cur)
    return amount * rate if rate is not None else None


def _position_rub(p: Position, fx: Mapping[str, Decimal]) -> Decimal | None:
    v = p.value
    return None if v is None else to_rub(v, p.instrument.currency, fx)


def value_portfolio(
    snap: PortfolioSnapshot,
    fx: Mapping[str, Decimal],
    ig_min: str = "A-",
    overrides: Mapping[str, str] | None = None,
) -> PortfolioValuation:
    values: dict[str, Decimal] = defaultdict(lambda: ZERO)
    rows: list[tuple[Position, str, Decimal | None]] = []
    unpriced: list[str] = []
    unclassified: list[str] = []

    for p in snap.positions:
        bucket = classify(p.instrument, ig_min, overrides)
        rub = _position_rub(p, fx)
        if rub is None:
            unpriced.append(p.instrument.ticker)
        else:
            values[bucket] += rub
        if bucket == UNCLASSIFIED:
            unclassified.append(p.instrument.ticker)
        rows.append((p, bucket, rub))

    for c in snap.cash:
        rub = to_rub(c.amount, c.currency, fx)
        if rub is None:
            unpriced.append(f"cash:{c.currency}")
            continue
        values["money_market" if c.currency.lower() == "rub" else "fx_cash"] += rub

    total = sum(values.values(), ZERO)
    weights = {k: safe_div(v, total) for k, v in values.items()}
    views = tuple(
        PositionView(
            account_id=p.account_id,
            ticker=p.instrument.ticker,
            name=p.instrument.name,
            bucket=bucket,
            value_rub=rub,
            weight=safe_div(rub, total) if rub is not None else ZERO,
            source=p.source,
        )
        for p, bucket, rub in sorted(rows, key=lambda r: -(r[2] or ZERO))
    )
    return PortfolioValuation(
        total_rub=total,
        bucket_values=dict(values),
        bucket_weights=weights,
        positions=views,
        unpriced=tuple(unpriced),
        unclassified=tuple(sorted(set(unclassified))),
    )
