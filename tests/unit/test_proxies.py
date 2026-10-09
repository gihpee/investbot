from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import numpy as np

from investbot.services.proxies import ProxyStore, synthetic_rate_series, weekly_closes, weekly_returns

D = Decimal


def daily(start: date, days: int, start_price: float, step: float) -> list[tuple[date, Decimal]]:
    out, p, d = [], start_price, start
    while len(out) < days:
        if d.weekday() < 5:
            out.append((d, D(str(round(p, 4)))))
            p *= 1 + step
        d += timedelta(days=1)
    return out


def test_weekly_closes_take_last_day_of_week() -> None:
    rows = [(date(2026, 10, 5), D(1)), (date(2026, 10, 8), D(2)), (date(2026, 10, 12), D(3))]
    assert weekly_closes(rows) == {date(2026, 10, 9): D(2), date(2026, 10, 16): D(3)}


def test_weekly_returns_align_on_common_weeks_and_skip_gaps() -> None:
    a = daily(date(2022, 1, 3), 60, 100, 0.001)
    b = daily(date(2022, 1, 3), 60, 50, -0.001)
    gap = [r for r in a if not (date(2022, 2, 28) <= r[0] <= date(2022, 3, 18))]  # торги остановлены
    dates, buckets, rets = weekly_returns({"equity_ru": gap, "ofz_fixed": b})
    assert buckets == ["equity_ru", "ofz_fixed"]
    assert rets.shape == (len(dates), 2)
    assert date(2022, 3, 4) not in dates  # неделя без торгов выпала из пересечения
    assert np.all(rets[:, 0] > 0) and np.all(rets[:, 1] < 0)


def test_synthetic_money_market_compounds_to_rate() -> None:
    r = synthetic_rate_series([date(2026, 1, 2)] * 52, 0.16)
    assert abs(float(np.prod(1 + r)) - 1.16) < 1e-9 and np.std(r) == 0


async def test_store_uses_cache_when_fetch_fails(tmp_path: Path) -> None:
    calls: list[tuple[str, date, date]] = []

    async def ok(secid: str, frm: date, to: date) -> list[tuple[date, Decimal]]:
        calls.append((secid, frm, to))
        return [(date(2026, 10, 1), D(100)), (date(2026, 10, 2), D(101))]

    async def broken(secid: str, frm: date, to: date) -> list[tuple[date, Decimal]]:
        raise TimeoutError

    await ProxyStore(tmp_path, ok).closes("MCFTR", date(2026, 1, 1), date(2026, 10, 2))
    rows = await ProxyStore(tmp_path, broken).closes("MCFTR", date(2026, 1, 1), date(2026, 10, 9))
    assert rows == [(date(2026, 10, 1), D(100)), (date(2026, 10, 2), D(101))]
    assert len(calls) == 1
