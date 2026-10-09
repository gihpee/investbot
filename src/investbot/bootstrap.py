"""Composition root: здесь (и только здесь) адаптеры связываются с сервисами."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime

import structlog

from investbot.adapters.moex_iss import IssClient
from investbot.adapters.tinvest.broker import TInvestBroker, open_broker
from investbot.journal.store import Journal
from investbot.portfolio.manual import load_manual
from investbot.services.allocation import AllocationService
from investbot.services.portfolio import PortfolioService
from investbot.services.proxies import ProxyStore
from investbot.services.status import SystemStatus
from investbot.settings import Settings

log = structlog.get_logger(__name__)


@dataclass
class Services:
    settings: Settings
    portfolio: PortfolioService
    allocation: AllocationService
    broker: TInvestBroker | None

    async def status(self) -> SystemStatus:
        problems: list[str] = []
        accounts = 0
        if self.broker is not None:
            try:
                await self.broker.recheck_read_only()
                accounts = len(await self.broker.accounts())
            except Exception as e:
                problems.append(f"Т-Инвестиции: {type(e).__name__}")
        elif self.settings.tinvest_sync:
            problems.append("токен Т-Инвестиций не задан — портфель только из ручного ввода")
        try:
            ips = self.portfolio.load_ips()
        except ValueError as e:
            ips = None
            problems.append(f"IPS не прочитан: {e}")
        try:
            manual = len(load_manual(self.settings.manual_portfolio_path).accounts)
        except ValueError as e:
            manual = 0
            problems.append(f"ручной портфель не прочитан: {e}")
        return SystemStatus(
            checked_at=datetime.now(UTC),
            deployment_mode=self.settings.deployment_mode,
            broker_connected=self.broker is not None and not problems,
            broker_accounts=accounts,
            ips_version=ips.version if ips else None,
            manual_accounts=manual,
            privacy_mode=self.settings.privacy_mode,
            problems=tuple(problems),
        )


@asynccontextmanager
async def build_services(settings: Settings) -> AsyncIterator[Services]:
    async with AsyncExitStack() as stack:
        broker: TInvestBroker | None = None
        tokens = settings.broker_tokens()
        if tokens:
            broker = await stack.enter_async_context(
                open_broker(tokens, settings.tinvest_app_name, settings.ca_bundle)
            )
        iss = await stack.enter_async_context(
            IssClient(verify=str(settings.ca_bundle) if settings.ca_bundle else True)
        )
        portfolio = PortfolioService(broker, settings.manual_portfolio_path, settings.ips_path)
        allocation = AllocationService(
            portfolio=portfolio,
            proxies=ProxyStore(settings.data_dir / "proxies", iss.index_closes),
            config_path=settings.config_dir / "allocation.yaml",
            answers_path=settings.data_dir / "profile.yaml",
            ips_path=settings.ips_path,
            journal=Journal(settings.data_dir / "journal.jsonl"),
        )
        yield Services(settings=settings, portfolio=portfolio, allocation=allocation, broker=broker)
