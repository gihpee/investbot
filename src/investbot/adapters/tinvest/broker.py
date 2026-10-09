"""Реализация BrokerReadPort поверх официального SDK `t-tech-investments` (только чтение).

SDK импортируется лениво: остальной код и тесты работают без него. Установка SDK — README.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Any

import structlog

from investbot.adapters.tinvest import mapper
from investbot.adapters.tinvest.guard import ReadOnlyClientGuard, TradingAccessError, check_read_only
from investbot.adapters.tinvest.ratelimit import ServiceLimits
from investbot.core.models import Account, AccountKind, BrokerPortfolio, InstrumentMeta, UserInfo

log = structlog.get_logger(__name__)

ClientFactory = Callable[[str], AbstractAsyncContextManager[Any]]


class SdkNotInstalled(RuntimeError):
    pass


def sdk_client_factory(app_name: str, ca_bundle: Path | None = None) -> ClientFactory:
    """Фабрика клиентов SDK. Проверка версии SDK по сети отключена (телеметрия, отчёт 01)."""
    if ca_bundle is not None:
        # gRPC берёт корневые сертификаты из этого файла: системные + Russian Trusted Root CA
        os.environ.setdefault("GRPC_DEFAULT_SSL_ROOTS_FILE_PATH", str(ca_bundle))
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


@asynccontextmanager
async def open_broker(tokens: dict[str, str], app_name: str, ca_bundle: Path | None) -> AsyncIterator[TInvestBroker]:
    async with TInvestBroker(tokens, sdk_client_factory(app_name, ca_bundle)) as broker:
        yield broker
