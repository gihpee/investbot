"""Порты к внешним источникам. Методов заявок здесь нет и не будет (ADR-05)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Protocol

from investbot.core.models import Account, BrokerPortfolio, UserInfo


class BrokerReadPort(Protocol):
    """Брокер только на чтение. Реализация — adapters.tinvest.TInvestBroker."""

    async def accounts(self) -> list[Account]: ...

    async def user_info(self) -> UserInfo: ...

    async def portfolio(self, account_id: str) -> BrokerPortfolio: ...


class FxRates(Protocol):
    """Курсы валют к рублю на дату."""

    async def rates(self, on: date | None = None) -> dict[str, Decimal]: ...


class IndexHistory(Protocol):
    """Недельные или дневные ряды полной доходности индексов-прокси бакетов (MCFTR, RGBITR, ...)."""

    async def index_closes(self, secid: str, frm: date, to: date) -> list[tuple[date, Decimal]]: ...
