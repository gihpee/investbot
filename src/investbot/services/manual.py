"""Редактирование ручного портфеля (§13.2 /edit, §13.4 мастер ЦФА): проверки, журнал, единые правила.

Ручные счета — это бумаги у других брокеров, ЦФА (смарт-счёт Т-Банка и другие операторы) и режим без токена.
По счетам, которые читаются из API Т-Инвестиций, ручные позиции запрещены (ADR-06).
"""

from __future__ import annotations

import re
from decimal import Decimal

from investbot.core.models import AccountKind, AssetClass
from investbot.journal.store import JournalLike
from investbot.portfolio.manual import ManualAccount, ManualPortfolio, ManualPortfolioError, ManualPosition
from investbot.portfolio.manual_store import ManualStore

_SLUG = re.compile(r"[^a-z0-9]+")
_TRANSLIT = str.maketrans({
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z", "и": "i", "й": "i",
    "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f",
    "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
})


def slugify(name: str) -> str:
    return _SLUG.sub("_", name.lower().translate(_TRANSLIT)).strip("_")[:40] or "account"


class ManualEditor:
    def __init__(self, store: ManualStore, journal: JournalLike, broker_account_ids: set[str] | None = None) -> None:
        self._store = store
        self._journal = journal
        self._broker_ids = broker_account_ids or set()

    def set_broker_accounts(self, ids: set[str]) -> None:
        self._broker_ids = ids

    async def portfolio(self) -> ManualPortfolio:
        return await self._store.load()

    async def create_account(self, name: str, kind: AccountKind,
                             tinvest_account_id: str | None = None) -> ManualAccount:
        mp = await self._store.load()
        base = slugify(name)
        existing = {a.id for a in mp.accounts} | self._broker_ids
        account_id, n = base, 2
        while account_id in existing:
            account_id, n = f"{base}_{n}", n + 1
        if tinvest_account_id and kind is not AccountKind.DFA:
            raise ManualPortfolioError("привязка к счёту Т-Инвестиций допустима только для смарт-счёта ЦФА")
        account = ManualAccount(id=account_id, name=name.strip(), kind=kind, tinvest_account_id=tinvest_account_id)
        await self._store.upsert_account(account)
        await self._journal.append("manual_account", {"op": "create", "id": account_id, "kind": kind.value},
                                   author="user")
        return account

    async def delete_account(self, account_id: str) -> None:
        await self._store.delete_account(account_id)
        await self._journal.append("manual_account", {"op": "delete", "id": account_id}, author="user")

    async def save_position(self, account_id: str, position: ManualPosition) -> list[str]:
        """Добавить или заменить позицию. Возвращает предупреждения (не блокирующие)."""
        if account_id in self._broker_ids:
            raise ManualPortfolioError("этот счёт читается из API Т-Инвестиций — ручные позиции на нём запрещены")
        mp = await self._store.load()
        acc = next((a for a in mp.accounts if a.id == account_id), None)
        if acc is None:
            raise ManualPortfolioError(f"ручной счёт {account_id} не найден")
        warnings: list[str] = []
        if position.asset_class is AssetClass.DFA:
            if acc.kind is not AccountKind.DFA:
                warnings.append("ЦФА обычно ведутся на отдельном счёте типа «ЦФА» — так проще сверка и лимиты")
            if position.operator is None:
                warnings.append("не указан оператор ЦФА: лимит 600 тыс. ₽ в год для неквала считается по оператору")
            if position.maturity is None:
                warnings.append("не указана дата погашения — в календаре ЦФА не появится")
        if position.unit_price() is None:
            warnings.append("нет цены: позиция не войдёт в оценку портфеля")
        await self._store.upsert_position(account_id, position)
        await self._journal.append("manual_position", {
            "op": "upsert", "account": account_id, "ticker": position.ticker,
            "asset_class": position.asset_class.value, "source": "telegram",
        }, author="user")
        return warnings

    async def delete_position(self, account_id: str, ticker: str) -> None:
        await self._store.delete_position(account_id, ticker)
        await self._journal.append("manual_position", {"op": "delete", "account": account_id, "ticker": ticker},
                                   author="user")

    async def set_cash(self, account_id: str, currency: str, amount: Decimal) -> None:
        if amount < 0:
            raise ManualPortfolioError("остаток денег не может быть отрицательным")
        await self._store.set_cash(account_id, currency, amount)
        await self._journal.append("manual_cash", {"account": account_id, "currency": currency.lower()},
                                   author="user")
