"""Реализация BrokerReadPort и EventsPort поверх официального SDK `t-tech-investments` (только чтение).

SDK импортируется лениво: остальной код и тесты работают без него. Установка SDK — README.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from datetime import UTC, date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any

import structlog

from investbot.adapters.tinvest import mapper
from investbot.adapters.tinvest.guard import ReadOnlyClientGuard, TradingAccessError, check_read_only
from investbot.adapters.tinvest.ratelimit import ServiceLimits
from investbot.calendar.models import CalendarEvent
from investbot.core.models import Account, AccountKind, BrokerPortfolio, InstrumentMeta, UserInfo
from investbot.news.models import RawNewsItem
from investbot.portfolio.taxlots import TradeOp

log = structlog.get_logger(__name__)

ClientFactory = Callable[[str], AbstractAsyncContextManager[Any]]

NEWS_PAGE_LIMIT = 200  # как в примере SDK из отчёта 01
NEWS_MAX_LIMIT = 1000  # limit по умолчанию у сервера; больше не запрашиваем


class SdkNotInstalled(RuntimeError):
    pass


def sdk_client_factory(app_name: str, ca_bundle: Path | None = None) -> ClientFactory:
    """Фабрика клиентов SDK. Проверка версии SDK по сети отключена (телеметрия, отчёт 01).

    Клиент `t_tech.invest.grpc.AsyncClient` не инициализирует error hub SDK (Sentry на error-hub.tbank.ru) — это делает
    только устаревший `t_tech.invest.AsyncClient`, поэтому отчёты об ошибках из бота не отправляются.
    """
    if ca_bundle is not None:
        # gRPC берёт корневые сертификаты из этого файла: системные + Russian Trusted Root CA
        os.environ.setdefault("GRPC_DEFAULT_SSL_ROOTS_FILE_PATH", str(ca_bundle))
        # Свой bundle — единственный источник корней: иначе SDK подставляет встроенный certs/RussianTrustedRootCA.pem,
        # а в wheel 1.51.0 с GitHub там кроме корня Минцифры самоподписанный TINKOFFBANK-ROOTCA (в исходниках его нет)
        os.environ["SSL_TBANK_VERIFY"] = "false"
    # Системный резолвер вместо встроенного c-ares: тот ходит к DNS-серверам напрямую и молча зависает в сетях,
    # где прямые DNS-запросы закрыты (VPN, корпоративные сети), — канал gRPC не поднимается без внятной ошибки
    os.environ.setdefault("GRPC_DNS_RESOLVER", "native")
    try:
        from t_tech.invest.grpc import AsyncClient
    except ImportError as e:  # pragma: no cover - зависит от окружения
        raise SdkNotInstalled("SDK t-tech-investments не установлен, см. README «SDK Т-Инвестиций»") from e

    def factory(token: str) -> AbstractAsyncContextManager[Any]:
        return AsyncClient(token, app_name=app_name, check_sdk_version=False)  # type: ignore[no-any-return]

    return factory


def _schemas() -> Any:
    from t_tech.invest.grpc import schemas

    return schemas


MAX_OPERATION_PAGES = 50  # 50 × 1000 операций — с запасом для личного счёта


class TInvestBroker:
    """Пул клиентов: один read-only токен на счёт или один общий; вызов по account_id уходит клиенту его токена."""

    def __init__(self, tokens: dict[str, str], factory: ClientFactory, schemas: Any | None = None) -> None:
        if not tokens:
            raise ValueError("не задан ни один токен T-Invest")
        self._tokens = tokens
        self._factory = factory
        self._schemas = schemas
        self._stack = AsyncExitStack()
        self._by_account: dict[str, ReadOnlyClientGuard] = {}
        self._clients: list[ReadOnlyClientGuard] = []
        self._accounts: list[Account] = []
        self._meta: dict[str, InstrumentMeta] | None = None
        self._limits = ServiceLimits()

    @property
    def s(self) -> Any:
        if self._schemas is None:
            self._schemas = _schemas()
        return self._schemas

    async def __aenter__(self) -> TInvestBroker:
        try:
            for alias, token in self._tokens.items():
                services = await self._stack.enter_async_context(self._factory(token))
                client = ReadOnlyClientGuard(services)
                accounts = await self._raw_accounts(client)
                readable = check_read_only(alias, accounts)  # fail-closed
                self._clients.append(client)
                for acc in readable:
                    self._by_account.setdefault(acc.account_id, client)
                    if all(a.account_id != acc.account_id for a in self._accounts):
                        self._accounts.append(acc)
                log.info("tinvest.token_ok", alias=alias, accounts=len(readable))
        except BaseException:
            await self._stack.aclose()
            raise
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._stack.aclose()

    async def _raw_accounts(self, client: ReadOnlyClientGuard) -> list[Account]:
        await self._limits.take("users")
        resp = await client.users.get_accounts(
            request=self.s.GetAccountsRequest(status=self.s.AccountStatus.ACCOUNT_STATUS_OPEN)
        )
        return [mapper.map_account(a) for a in resp.accounts]

    def _client(self, account_id: str) -> ReadOnlyClientGuard:
        try:
            return self._by_account[account_id]
        except KeyError:
            raise TradingAccessError(f"счёт {account_id} недоступен ни одному read-only токену") from None

    async def accounts(self) -> list[Account]:
        return list(self._accounts)

    async def recheck_read_only(self) -> None:
        """Ежедневная повторная проверка: токен могли перевыпустить с другими правами."""
        for i, client in enumerate(self._clients):
            check_read_only(f"#{i}", await self._raw_accounts(client))

    async def user_info(self) -> UserInfo:
        client = self._clients[0]
        await self._limits.take("users")
        info = await client.users.get_info(request=self.s.GetInfoRequest())
        return UserInfo(
            qualified=bool(info.qual_status),
            qualified_for=tuple(str(x) for x in (info.qualified_for_work_with or ())),
            tariff=str(info.tariff) if info.tariff else None,
        )

    async def instrument_meta(self) -> dict[str, InstrumentMeta]:
        """Справочники акций, облигаций и фондов (3 запроса; кешируются на время жизни объекта)."""
        if self._meta is None:
            client = self._clients[0]
            req = self.s.InstrumentsRequest(instrument_status=self.s.InstrumentStatus.INSTRUMENT_STATUS_ALL)
            meta: dict[str, InstrumentMeta] = {}
            for method, fn in (("shares", mapper.map_share), ("bonds", mapper.map_bond), ("etfs", mapper.map_etf)):
                await self._limits.take("instruments")
                resp = await getattr(client.instruments, method)(request=req)
                for x in resp.instruments:
                    meta[str(x.uid)] = fn(x)
            self._meta = meta
        return self._meta

    async def trade_ops(self, account_id: str, since: datetime) -> list[TradeOp]:
        """Сделки и переводы бумаг из GetOperationsByCursor (только исполненные) — для FIFO-лотов."""
        s = self.s
        client = self._client(account_id)
        meta = await self.instrument_meta()
        types = [t for name in mapper.TRADE_OP_KINDS if (t := getattr(s.OperationType, name, None)) is not None]
        state = getattr(getattr(s, "OperationState", None), "OPERATION_STATE_EXECUTED", None)
        out: list[TradeOp] = []
        cursor = ""
        for _ in range(MAX_OPERATION_PAGES):
            await self._limits.take("operations")
            kwargs: dict[str, Any] = {"account_id": account_id, "from_": since, "to": datetime.now(UTC),
                                      "cursor": cursor, "limit": 1000, "operation_types": types}
            if state is not None:
                kwargs["state"] = state
            r = await client.operations.get_operations_by_cursor(request=s.GetOperationsByCursorRequest(**kwargs))
            for op in r.items:
                if (t := mapper.map_trade_op(account_id, op, meta)) is not None:
                    out.append(t)
            if not r.has_next:
                break
            cursor = r.next_cursor
        return out

    async def portfolio(self, account_id: str) -> BrokerPortfolio:
        acc = next((a for a in self._accounts if a.account_id == account_id), None)
        if acc is not None and acc.kind is AccountKind.INVEST_BOX:
            # GetPortfolio для Инвесткопилки не работает (отчёт 01) — ведётся как ручной счёт
            return BrokerPortfolio(account_id=account_id, positions=(), cash=())
        client = self._client(account_id)
        meta = await self.instrument_meta()
        await self._limits.take("operations")
        p = await client.operations.get_portfolio(
            request=self.s.PortfolioRequest(
                account_id=account_id, currency=self.s.PortfolioRequest.CurrencyRequest.RUB
            )
        )
        await self._limits.take("operations")
        pos = await client.operations.get_positions(request=self.s.PositionsRequest(account_id=account_id))

        positions = []
        for raw in p.positions:
            position, warning = mapper.map_portfolio_position(account_id, raw, meta)
            if warning:
                log.warning("tinvest.bond_price_units", detail=warning)
            if position is not None:
                positions.append(position)
        total, _ = mapper.money(p.total_amount_portfolio)
        dfa, _ = mapper.money(getattr(p, "total_amount_dfa", None))
        return BrokerPortfolio(
            account_id=account_id,
            positions=tuple(positions),
            cash=tuple(mapper.map_cash(account_id, pos.money)),
            total_rub=total,
            total_dfa_rub=dfa or None,
            fx_rates=mapper.fx_from_portfolio(p.positions),
        )

    # --- EventsPort: события по бумагам и последние цены (InstrumentsService, MarketDataService) -----------

    async def _names(self, uid: str) -> tuple[str, str, date | None]:
        meta = (await self.instrument_meta()).get(uid)
        if meta is None:
            return uid, "", None
        return meta.ticker, meta.name, meta.maturity

    async def bond_events(self, uid: str, frm: date, to: date) -> list[CalendarEvent]:
        """GetBondEvents по типам CPN, CALL, MTY — отдельный запрос на тип (3 запроса на бумагу).

        Возвращает ли запрос без `type` (EVENT_TYPE_UNSPECIFIED) все типы, отчёт 01 не подтверждает,
        поэтому тип задаётся явно, как в примере SDK.
        """
        client = self._clients[0]
        ticker, name, maturity = await self._names(uid)
        event_types = self.s.GetBondEventsRequest.EventType
        out: list[CalendarEvent] = []
        for type_name in mapper.BOND_EVENT_TYPES:
            await self._limits.take("instruments")
            resp = await client.instruments.get_bond_events(
                request=self.s.GetBondEventsRequest(
                    instrument_id=uid, from_=_ts_from(frm), to=_ts_to(to), type=getattr(event_types, type_name)
                )
            )
            for raw in resp.events or ():
                event = mapper.map_bond_event(raw, ticker=ticker, name=name, type_hint=type_name, maturity=maturity)
                if event is not None:
                    out.append(event)
        return out

    async def dividends(self, uid: str, frm: date, to: date) -> list[CalendarEvent]:
        """GetDividends: сервер фильтрует по `record_date`."""
        client = self._clients[0]
        ticker, name, _ = await self._names(uid)
        await self._limits.take("instruments")
        resp = await client.instruments.get_dividends(
            request=self.s.GetDividendsRequest(instrument_id=uid, from_=_ts_from(frm), to=_ts_to(to))
        )
        out: list[CalendarEvent] = []
        for raw in resp.dividends or ():
            event = mapper.map_dividend(raw, ticker=ticker, name=name)
            if event is not None:
                out.append(event)
        return out

    async def last_prices(self, uids: list[str]) -> dict[str, Decimal]:
        """GetLastPrices пакетами по `LAST_PRICES_BATCH` uid; цены облигаций переводятся из % номинала в валюту."""
        unique = list(dict.fromkeys(uids))
        if not unique:
            return {}
        client = self._clients[0]
        meta = await self.instrument_meta()
        # LastPriceType.LAST_PRICE_EXCHANGE — только биржевые цены; если в SDK нет перечисления, параметр
        # не передаётся (сервер отдаёт цену по умолчанию)
        price_type = getattr(getattr(self.s, "LastPriceType", None), "LAST_PRICE_EXCHANGE", None)
        extra = {"last_price_type": price_type} if price_type is not None else {}
        out: dict[str, Decimal] = {}
        for i in range(0, len(unique), LAST_PRICES_BATCH):
            await self._limits.take("market_data")
            resp = await client.market_data.get_last_prices(
                request=self.s.GetLastPricesRequest(instrument_id=unique[i : i + LAST_PRICES_BATCH], **extra)
            )
            out.update(mapper.map_last_prices(resp.last_prices, meta))
        return out

    # --- Лента новостей InstrumentsService.News (с 1.49, §5.7 и §8.2 п. 1) --------------------------------

    async def news_page(
        self, cursor: str | None, limit: int = NEWS_PAGE_LIMIT
    ) -> tuple[list[RawNewsItem], str | None]:
        """Одна страница `News(NewsRequest(limit, cursor))`: элементы и курсор следующей страницы.

        Фильтров по тикеру и дате у метода нет — только курсор. Курсор следующей страницы возвращается, если
        `has_next`, иначе None. Направление курсора (к более старым или к более новым новостям) и глубина ленты
        в отчёте 01 не описаны — [проверить] в спайке Ф0; хранение курсора в БД — у вызывающего (§8.2).
        Только чтение: InstrumentsService, бюджет сервиса `instruments`.
        """
        if not 1 <= limit <= NEWS_MAX_LIMIT:
            raise ValueError(f"limit News должен быть в 1..{NEWS_MAX_LIMIT}, получено {limit}")
        client = self._clients[0]
        params: dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        await self._limits.take("instruments")
        resp = await client.instruments.news(request=self.s.NewsRequest(**params))
        now = datetime.now(UTC)
        items = [mapper.map_news_item(n, now) for n in getattr(resp, "items", None) or ()]
        next_cursor = str(getattr(resp, "next_cursor", "") or "") if getattr(resp, "has_next", False) else ""
        return items, next_cursor or None


LAST_PRICES_BATCH = 100  # лимит числа инструментов в одном GetLastPrices не документирован — берём с запасом


def _ts_from(d: date) -> datetime:
    return datetime.combine(d, time.min, tzinfo=UTC)


def _ts_to(d: date) -> datetime:
    return datetime.combine(d, time(23, 59, 59), tzinfo=UTC)


@asynccontextmanager
async def open_broker(tokens: dict[str, str], app_name: str, ca_bundle: Path | None) -> AsyncIterator[TInvestBroker]:
    async with TInvestBroker(tokens, sdk_client_factory(app_name, ca_bundle)) as broker:
        yield broker
