from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace as NS
from typing import Any

from aiogram.types import Chat, Message, User

from investbot.channels.telegram.handlers import status_message
from investbot.channels.telegram.middlewares import OwnerOnlyMiddleware
from investbot.channels.telegram.render import portfolio_message
from investbot.core.models import PortfolioSnapshot
from investbot.portfolio.valuation import PortfolioValuation, PositionView
from investbot.rebalance.drift import compute_drift
from investbot.rebalance.ips import IPS, BucketTarget
from investbot.services.portfolio import PortfolioOverview
from investbot.services.status import SystemStatus

D = Decimal


def overview(with_ips: bool) -> PortfolioOverview:
    weights = {"equity_ru": D("0.47"), "ofz_fixed": D("0.43"), "money_market": D("0.10")}
    ips = IPS(version=2, buckets={
        "equity_ru": BucketTarget(target=D("0.40"), band_abs=D("0.05")),
        "ofz_fixed": BucketTarget(target=D("0.45"), band_abs=D("0.05")),
        "money_market": BucketTarget(target=D("0.15"), band_abs=D("0.05")),
    }) if with_ips else None
    valuation = PortfolioValuation(
        total_rub=D("1234567.89"), bucket_values={k: v * 1000 for k, v in weights.items()},
        bucket_weights=weights,
        positions=(PositionView(account_id="a", ticker="AAAA", name="Эмитент А", bucket="equity_ru",
                                value_rub=D("580246.91"), weight=D("0.47"), source="broker"),),
        unpriced=(), unclassified=(),
    )
    snap = PortfolioSnapshot(taken_at=datetime.now(UTC), accounts=(), positions=(), cash=())
    return PortfolioOverview(snapshot=snap, valuation=valuation, ips=ips,
                             drift=tuple(compute_drift(weights, ips)) if ips else (), warnings=("тест <x>",))


def test_weights_mode_has_no_ruble_amounts() -> None:
    text = portfolio_message(overview(with_ips=True), "weights")
    assert "₽" not in text.replace("Денежный рынок и ₽", "")
    assert "1234567" not in text and "580246" not in text
    assert "47,0%" in text and "🔺" in text
    assert "&lt;x&gt;" in text  # предупреждения экранируются


def test_full_mode_shows_total() -> None:
    assert "1 234 568 ₽" in portfolio_message(overview(with_ips=True), "full")


def test_monitoring_mode_without_ips() -> None:
    text = portfolio_message(overview(with_ips=False), "weights")
    assert "только мониторинг" in text and "/allocation" in text


def test_status_message_lists_problems() -> None:
    st = SystemStatus(checked_at=datetime.now(UTC), deployment_mode="personal", broker_connected=False,
                      broker_accounts=0, ips_version=None, manual_accounts=1, privacy_mode="weights",
                      problems=("токен не задан",))
    text = status_message(st)
    assert "не подключены" in text and "токен не задан" in text and "не утверждён" in text


def message(user_id: int) -> Message:
    return Message(message_id=1, date=datetime.now(UTC), chat=Chat(id=user_id, type="private"),
                   from_user=User(id=user_id, is_bot=False, first_name="x"), text="/start")


async def test_owner_only_middleware_drops_strangers() -> None:
    called: list[Any] = []

    async def handler(event: Any, data: dict[str, Any]) -> str:
        called.append(event)
        return "ok"

    mw = OwnerOnlyMiddleware(owner_id=42)
    assert await mw(handler, message(42), {"event_from_user": NS(id=42)}) == "ok"
    assert await mw(handler, message(7), {"event_from_user": NS(id=7)}) is None
    assert len(called) == 1


def test_reply_keyboard_buttons_match_routers() -> None:
    from investbot.channels.telegram import edit, handlers, news

    assert handlers.BTN_MANUAL == edit.BTN_MANUAL and handlers.BTN_NEWS == news.BTN_NEWS
    texts = {b.text for row in handlers.MAIN_KEYBOARD.keyboard for b in row}
    assert {handlers.BTN_MANUAL, handlers.BTN_NEWS, handlers.BTN_PORTFOLIO, handlers.BTN_STATUS} <= texts
