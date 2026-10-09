"""DQ-гейты (§4.5), реализованные на текущем этапе: G-fresh (по оценке портфеля), G-news, G-portfolio."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta
from decimal import Decimal

from investbot.core.money import pct, safe_div
from investbot.news.models import SourceHealth
from investbot.ops.degradation import GateResult
from investbot.portfolio.valuation import PortfolioValuation

UNPRICED_CRITICAL_SHARE = Decimal("0.02")
NEWS_IDLE_MAX = timedelta(minutes=60)


def gate_fresh(valuation: PortfolioValuation, position_values_hint: dict[str, Decimal] | None = None) -> GateResult:
    """Бумаги без цены. Критично, если их оценочная доля > 2% (по подсказке стоимости, например по себестоимости),
    а если подсказки нет — если таких бумаг больше одной при портфеле меньше чем из 20 позиций."""
    if not valuation.unpriced:
        return GateResult(name="G-fresh", ok=True)
    names = ", ".join(valuation.unpriced)
    if position_values_hint:
        missing = sum((position_values_hint.get(t, Decimal(0)) for t in valuation.unpriced), Decimal(0))
        share = safe_div(missing, valuation.total_rub + missing)
        critical = share > UNPRICED_CRITICAL_SHARE
        return GateResult(name="G-fresh", ok=False, critical=critical,
                          message=f"нет цены у {names} (≈{pct(share)} портфеля)")
    critical = len(valuation.unpriced) > 1 and len(valuation.positions) < 20
    return GateResult(name="G-fresh", ok=False, critical=critical, message=f"нет цены у {names}")


def gate_news(health: Iterable[SourceHealth], p0_sources: Sequence[str], now: datetime) -> GateResult:
    stale = [h.source for h in health if h.source in p0_sources
             and (h.last_ok_at is None or now - h.last_ok_at > NEWS_IDLE_MAX)]
    if not stale:
        return GateResult(name="G-news", ok=True)
    return GateResult(name="G-news", ok=False, message=f"новостные ленты простаивают: {', '.join(sorted(stale))}")


def gate_portfolio(lot_notes: Sequence[str]) -> GateResult:
    if not lot_notes:
        return GateResult(name="G-portfolio", ok=True)
    return GateResult(name="G-portfolio", ok=False,
                      message="лоты по операциям расходятся с портфелем — налог частично оценён по средней цене")
