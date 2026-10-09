"""Ряды индексов-прокси для бакетов (§10.8): дневные закрытия → недельные доходности.

Кэш на диске (CSV), чтобы не дёргать ISS при каждом показе вариантов и работать при его недоступности.
"""

from __future__ import annotations

import csv
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import numpy as np
import structlog

log = structlog.get_logger(__name__)

# Индексы полной доходности MOEX (секции markets/index в ISS). Бакеты без индекса задаются в профиле как fixed.
DEFAULT_PROXIES: dict[str, str] = {
    "equity_ru": "MCFTR",
    "ofz_fixed": "RGBITR",
    "corp_ig": "RUCBTRNS",
}
# Денежный рынок моделируется синтетически: доходность ≈ ставка/52 без волатильности (synthetic_rate_series).

Closes = list[tuple[date, Decimal]]
Fetch = Callable[[str, date, date], Awaitable[Closes]]


def weekly_closes(closes: Sequence[tuple[date, Decimal]]) -> dict[date, Decimal]:
    """Последнее закрытие каждой ISO-недели, ключ — пятница этой недели."""
    out: dict[date, Decimal] = {}
    for d, c in sorted(closes):
        friday = d + timedelta(days=4 - d.weekday())
        out[friday] = c
    return out


def weekly_returns(series: Mapping[str, Sequence[tuple[date, Decimal]]]) -> tuple[list[date], list[str], np.ndarray]:
    """Выравнивает ряды по общим неделям и считает простые доходности.

    Недели без торгов (например, 28.02–24.03.2022) выпадают из пересечения — доходность через разрыв
    считается одним шагом, как требует §10.8.
    """
    weekly = {k: weekly_closes(v) for k, v in series.items() if v}
    if not weekly:
        return [], [], np.empty((0, 0))
    common = sorted(set.intersection(*(set(w) for w in weekly.values())))
    buckets = list(weekly)
    prices = np.array([[float(weekly[b][d]) for b in buckets] for d in common])
    if len(common) < 2:
        return [], buckets, np.empty((0, len(buckets)))
    rets = prices[1:] / prices[:-1] - 1.0
    return common[1:], buckets, rets


def synthetic_rate_series(dates: Sequence[date], annual_rate: float) -> np.ndarray:
    """Недельные доходности инструмента денежного рынка при постоянной ставке (упрощение до истории КС)."""
    return np.full(len(dates), (1.0 + annual_rate) ** (1 / 52) - 1.0)


class ProxyStore:
    def __init__(self, cache_dir: Path, fetch: Fetch | None) -> None:
        self._dir = cache_dir
        self._fetch = fetch

    def _path(self, secid: str) -> Path:
        return self._dir / f"{secid}.csv"

    def _read(self, secid: str) -> Closes:
        p = self._path(secid)
        if not p.exists():
            return []
        with p.open(encoding="utf-8") as f:
            return [(date.fromisoformat(r[0]), Decimal(r[1])) for r in csv.reader(f) if r]

    def _write(self, secid: str, rows: Closes) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        with self._path(secid).open("w", encoding="utf-8", newline="") as f:
            csv.writer(f).writerows((d.isoformat(), str(c)) for d, c in rows)

    async def closes(self, secid: str, frm: date, to: date) -> Closes:
        cached = self._read(secid)
        last = max((d for d, _ in cached), default=None)
        if self._fetch is not None and (last is None or last < to - timedelta(days=3)):
            start = frm if last is None else last + timedelta(days=1)
            try:
                fresh = await self._fetch(secid, start, to)
                merged = dict(cached)
                merged.update(fresh)
                cached = sorted(merged.items())
                self._write(secid, cached)
            except Exception as e:
                log.warning("proxies.fetch_failed", secid=secid, error=type(e).__name__)
        return [(d, c) for d, c in cached if frm <= d <= to]

    async def bucket_series(self, proxies: Mapping[str, str], frm: date, to: date) -> dict[str, Closes]:
        return {bucket: await self.closes(secid, frm, to) for bucket, secid in proxies.items()}
