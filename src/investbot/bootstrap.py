"""Composition root: здесь (и только здесь) адаптеры связываются с сервисами."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import structlog

from investbot.adapters.moex_iss import IssClient
from investbot.adapters.rss.collector import RssCollector
from investbot.adapters.tinvest.broker import TInvestBroker, open_broker
from investbot.core.models import AssetClass, InstrumentMeta
from investbot.data.db import make_engine
from investbot.data.manual_repo import DbManualStore
from investbot.data.news_repo import NewsRepo
from investbot.data.quality import gate_news
from investbot.journal.db_store import DbJournal
from investbot.journal.store import Journal, JournalLike
from investbot.news.linking import AliasIndex, load_aliases
from investbot.news.sources import load_sources
from investbot.ops.degradation import GateResult
from investbot.portfolio.manual_store import ManualStore, YamlManualStore
from investbot.rebalance.models import CostModel, Quote, TaxModel
from investbot.services.allocation import AllocationService
from investbot.services.calendar import CalendarService
from investbot.services.manual import ManualEditor
from investbot.services.news import AliasProvider, NewsService
from investbot.services.portfolio import BrokerSnapshotCache, PortfolioService
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


ALIAS_TTL = timedelta(hours=6)


def alias_provider(settings: Settings, portfolio: PortfolioService, broker: TInvestBroker | None) -> AliasProvider:
    """Словарь алиасов: акции основного режима из справочника брокера + бумаги портфеля + config/aliases.yaml.
    Пересобирается не чаще раза в 6 часов."""
    cache: dict[str, tuple[datetime, AliasIndex]] = {}

    async def provide() -> AliasIndex:
        now = datetime.now(UTC)
        if "idx" in cache and now - cache["idx"][0] < ALIAS_TTL:
            return cache["idx"][1]
        instruments: dict[str, InstrumentMeta] = {}
        if broker is not None:
            try:
                for m in (await broker.instrument_meta()).values():
                    if m.asset_class is AssetClass.EQUITY and m.class_code == "TQBR":
                        instruments.setdefault(m.ticker, m)
            except Exception as e:
                log.warning("aliases.catalog_failed", error=type(e).__name__)
        try:
            snap, _ = await portfolio.snapshot()
            for p in snap.positions:
                instruments.setdefault(p.instrument.ticker, p.instrument)
        except Exception as e:
            log.warning("aliases.portfolio_failed", error=type(e).__name__)
        extra, ambiguous = load_aliases(settings.config_dir / "aliases.yaml")
        index = AliasIndex.build(instruments.values(), extra, ambiguous)
        cache["idx"] = (now, index)
        return index

    return provide


@dataclass
class Services:
    settings: Settings
    portfolio: PortfolioService
    allocation: AllocationService
    rebalance: RebalanceService
    calendar: CalendarService
    news: NewsService
    manual: ManualEditor
    broker: TInvestBroker | None

    async def held_tickers(self) -> list[str]:
        snap, _ = await self.portfolio.snapshot()
        return sorted({p.instrument.ticker for p in snap.positions})

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
            manual = len((await self.portfolio.manual.load()).accounts)
        except ValueError as e:
            manual = 0
            problems.append(f"ручной портфель не прочитан: {e}")
        try:
            broken = [h.source for h, is_broken in await self.news.health() if is_broken]
            if broken:
                problems.append(f"источники новостей не отвечают: {', '.join(broken)}")
        except Exception as e:
            problems.append(f"архив новостей недоступен: {type(e).__name__}")
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
        engine = make_engine(settings.db_url())
        stack.push_async_callback(engine.dispose)
        journal: JournalLike
        manual_store: ManualStore
        if settings.storage == "db":
            journal, manual_store = DbJournal(engine), DbManualStore(engine)
        else:
            journal = Journal(settings.data_dir / "journal.jsonl")
            manual_store = YamlManualStore(settings.manual_portfolio_path)
        portfolio = PortfolioService(broker, manual_store, settings.ips_path,
                                     cache=BrokerSnapshotCache(settings.data_dir / "snapshots"))
        editor = ManualEditor(manual_store, journal)
        if broker is not None:
            editor.set_broker_accounts({a.account_id for a in await broker.accounts()})
        allocation = AllocationService(
            portfolio=portfolio,
            proxies=ProxyStore(settings.data_dir / "proxies", iss.index_closes),
            config_path=settings.config_dir / "allocation.yaml",
            answers_path=settings.data_dir / "profile.yaml",
            ips_path=settings.ips_path,
            journal=journal,
        )
        news_holder: list[NewsService] = []

        async def extra_gates() -> list[GateResult]:
            if not news_holder:
                return []
            sources = load_sources(settings.config_dir / "sources.yaml")
            p0 = [x.key for x in sources if x.enabled and x.priority == "P0"]
            return [gate_news([h for h, _ in await news_holder[0].health()], p0, datetime.now(UTC))]

        rebalance = RebalanceService(
            portfolio=portfolio,
            quotes=broker_quote_provider(broker) if broker is not None else None,
            journal=journal,
            costs=CostModel(broker_fee_rate=settings.broker_fee_rate),
            tax=TaxModel(rate=settings.tax_rate),
            trade_ops=broker.trade_ops if broker is not None else None,
            extra_gates=extra_gates,
        )
        calendar = CalendarService(portfolio, broker, iss.bondization)
        rss = await stack.enter_async_context(
            RssCollector(verify=str(settings.ca_bundle) if settings.ca_bundle else True)
        )
        news = NewsService(
            repo=NewsRepo(engine),
            sources=load_sources(settings.config_dir / "sources.yaml"),
            rss=rss,
            tinvest_news=broker.news_page if broker is not None else None,
            aliases=alias_provider(settings, portfolio, broker),
        )
        news_holder.append(news)
        yield Services(settings=settings, portfolio=portfolio, allocation=allocation, rebalance=rebalance,
                       calendar=calendar, news=news, manual=editor, broker=broker)
