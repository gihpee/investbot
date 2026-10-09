"""Деградация (§4.5): брокер недоступен → снимок из кэша без продаж; старый снимок или нет цен → план отложен."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from investbot.channels.telegram.report import weekly_report_message
from investbot.core.models import Account, BrokerPortfolio, UserInfo
from investbot.journal.store import Journal
from investbot.ops.degradation import GateResult, combine
from investbot.rebalance.models import CostModel, TaxModel
from investbot.services.portfolio import BrokerSnapshotCache, PortfolioService
from investbot.services.rebalance import RebalanceService

from .test_weekly_report import IPS_YAML, TODAY, FakeBroker

D = Decimal


class DownBroker:
    async def accounts(self) -> list[Account]:
        raise ConnectionError("T-Invest down")

    async def user_info(self) -> UserInfo:
        raise ConnectionError

    async def portfolio(self, account_id: str) -> BrokerPortfolio:
        raise ConnectionError


async def _cache_from_good_broker(tmp_path: Path, age: timedelta) -> BrokerSnapshotCache:
    cache = BrokerSnapshotCache(tmp_path / "snap")
    good = FakeBroker()
    accounts = await good.accounts()
    cache.save(datetime.now(UTC) - age, accounts, [await good.portfolio(a.account_id) for a in accounts])
    return cache


def service(tmp_path: Path, broker: object, cache: BrokerSnapshotCache | None) -> RebalanceService:
    (tmp_path / "ips.yaml").write_text(IPS_YAML, encoding="utf-8")
    portfolio = PortfolioService(broker, tmp_path / "manual.yaml", tmp_path / "ips.yaml", cache=cache)  # type: ignore[arg-type]
    return RebalanceService(portfolio, None, Journal(tmp_path / "j.jsonl"), CostModel(), TaxModel())


async def test_broker_down_uses_fresh_cache_without_sells(tmp_path: Path) -> None:
    svc = service(tmp_path, DownBroker(), await _cache_from_good_broker(tmp_path, timedelta(days=2)))
    prep = await svc.weekly_plan(TODAY)
    assert "broker_stale" in prep.degradation.flags and prep.degradation.level == 3
    assert prep.plan is not None and all(a.side == "buy" for a in prep.plan.actions)
    text = weekly_report_message(prep)
    assert "продажи не предлагаются" in text


async def test_old_cache_postpones_plan_with_partial_report(tmp_path: Path) -> None:
    svc = service(tmp_path, DownBroker(), await _cache_from_good_broker(tmp_path, timedelta(days=9)))
    prep = await svc.weekly_plan(TODAY)
    assert prep.plan is None and "broker_too_old" in prep.degradation.flags
    text = weekly_report_message(prep, events_html="<b>События</b>")
    assert "план отложен" in text and "События" in text and "Доли по классам" in text


async def test_broker_down_without_cache_raises(tmp_path: Path) -> None:
    svc = service(tmp_path, DownBroker(), BrokerSnapshotCache(tmp_path / "empty"))
    try:
        await svc.weekly_plan(TODAY)
    except ConnectionError:
        return
    raise AssertionError("ожидалась ошибка без кэша")


async def test_healthy_broker_refreshes_cache(tmp_path: Path) -> None:
    cache = BrokerSnapshotCache(tmp_path / "snap")
    svc = service(tmp_path, FakeBroker(), cache)
    prep = await svc.weekly_plan(TODAY)
    assert prep.degradation.level == 0 and cache.load() is not None


def test_flags_combine_levels_and_banners() -> None:
    deg = combine(["broker_stale"], [GateResult(name="G-news", ok=False, message="лента X простаивает"),
                                     GateResult(name="G-fresh", ok=True)], datetime(2026, 10, 9, 18, tzinfo=UTC))
    assert set(deg.flags) == {"broker_stale", "data_fallback"} and deg.level == 3
    assert not deg.allow_sells and deg.trade_list_allowed
    banners = " ".join(deg.banners())
    assert "21:00 МСК" in banners and "лента X простаивает" in banners
    crit = combine([], [GateResult(name="G-fresh", ok=False, critical=True, message="нет цены у A")])
    assert crit.level == 4 and not crit.trade_list_allowed and "План отложен" in crit.banners()[0]
