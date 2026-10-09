"""Порты к внешним источникам. Методов заявок здесь нет и не будет (ADR-05)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Protocol

from investbot.calendar.models import CalendarEvent
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


class EventsPort(Protocol):
    """События по бумагам и последние цены (только чтение). Реализация — adapters.tinvest.TInvestBroker.

    `uid` — `instrument_uid` Т-Инвестиций (`InstrumentMeta.tinvest_uid`). Границы `frm`/`to` включительны.
    """

    async def bond_events(self, uid: str, frm: date, to: date) -> list[CalendarEvent]:
        """Купоны, амортизации, оферты и погашение облигации с датой события в [frm, to]."""
        ...

    async def dividends(self, uid: str, frm: date, to: date) -> list[CalendarEvent]:
        """Дивиденды с датой фиксации реестра в [frm, to] (фильтр GetDividends — по record_date)."""
        ...

    async def last_prices(self, uids: list[str]) -> dict[str, Decimal]:
        """Последняя биржевая цена за 1 шт. в валюте инструмента (облигации — из % номинала в валюту).

        Бумаги без цены (нет сделок) в ответ не попадают.
        """
        ...
