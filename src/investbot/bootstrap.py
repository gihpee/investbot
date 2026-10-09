"""Composition root: здесь (и только здесь) адаптеры связываются с сервисами."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime

import structlog

from investbot.adapters.moex_iss import IssClient
from investbot.adapters.tinvest.broker import TInvestBroker, open_broker
from investbot.core.models import InstrumentMeta
from investbot.journal.store import Journal
from investbot.portfolio.manual import load_manual
from investbot.rebalance.models import CostModel, Quote, TaxModel
from investbot.services.allocation import AllocationService
from investbot.services.calendar import CalendarService
from investbot.services.portfolio import PortfolioService
from investbot.services.proxies import ProxyStore
from investbot.services.rebalance import QuoteProvider, RebalanceService
from investbot.services.status import SystemStatus
from investbot.settings import Settings

log = structlog.get_logger(__name__)


# основные режимы торгов MOEX: при нескольких инструментах с одним тикером берётся бумага основного режима
PRIMARY_BOARDS = ("TQBR", "TQTF", "TQOB", "TQCB", "TQIR", "TQPI")


def pick_by_ticker(meta: Mapping[str, InstrumentMeta], ticker: str) -> InstrumentMeta | None:
    found = [m for m in meta.values() if m.ticker == ticker]
    found.sort(key=lambda m: PRIMARY_BOARDS.index(m.class_code) if m.class_code in PRIMARY_BOARDS else 99)
    return found[0] if found else None


def broker_quote_provider(broker: TInvestBroker) -> QuoteProvider:
    """Котировки носителей бакетов: справочник по тикеру + GetLastPrices. Только рублёвые бумаги (MVP-1a)."""

    async def quotes(wanted: Mapping[str, str]) -> dict[str, Quote]:
        meta = await broker.instrument_meta()
        picked = {t: m for t in wanted if (m := pick_by_ticker(meta, t)) and m.tinvest_uid and m.currency == "rub"}
        prices = await broker.last_prices([m.tinvest_uid for m in picked.values() if m.tinvest_uid])
        return {
            t: Quote(instrument=m, bucket=wanted[t], price=prices[m.tinvest_uid])
            for t, m in picked.items()
            if m.tinvest_uid in prices
        }

    return quotes


@dataclass
class Services:
    settings: Settings
    portfolio: PortfolioService
    allocation: AllocationService
    rebalance: RebalanceService
    calendar: CalendarService
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
        journal = Journal(settings.data_dir / "journal.jsonl")
        portfolio = PortfolioService(broker, settings.manual_portfolio_path, settings.ips_path)
        allocation = AllocationService(
            portfolio=portfolio,
            proxies=ProxyStore(settings.data_dir / "proxies", iss.index_closes),
            config_path=settings.config_dir / "allocation.yaml",
            answers_path=settings.data_dir / "profile.yaml",
            ips_path=settings.ips_path,
            journal=journal,
        )
        rebalance = RebalanceService(
            portfolio=portfolio,
            quotes=broker_quote_provider(broker) if broker is not None else None,
            journal=journal,
            costs=CostModel(broker_fee_rate=settings.broker_fee_rate),
            tax=TaxModel(rate=settings.tax_rate),
        )
        calendar = CalendarService(portfolio, broker, iss.bondization)
        yield Services(settings=settings, portfolio=portfolio, allocation=allocation, rebalance=rebalance,
                       calendar=calendar, broker=broker)
