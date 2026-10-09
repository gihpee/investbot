"""Ручной портфель в БД: `ManualStore` поверх manual_account / manual_position / manual_cash.

Семантика та же, что у `YamlManualStore` (portfolio/manual_store.py):
- `upsert_account` у существующего счёта меняет только имя, тип и привязку — позиции и кэш не трогаются;
  новый счёт сохраняется целиком (с позициями и кэшем) и встаёт в конец списка;
- `upsert_position` заменяет позицию с тем же тикером и ставит её в конец списка счёта (как в YAML);
- `set_cash` с нулём удаляет строку, валюта приводится к нижнему регистру;
- операции с позициями и кэшем несуществующего счёта — `KeyError`.
Удаление счёта удаляет его позиции и кэш явно (в SQLite без PRAGMA foreign_keys каскад FK не срабатывает).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from sqlalchemy import ColumnElement, delete, exists, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from investbot.core.models import AccountKind
from investbot.data.db import manual_account, manual_cash, manual_position, read_snapshot, write_tx
from investbot.portfolio.manual import ManualAccount, ManualPortfolio, ManualPosition

_POSITION_FIELDS = tuple(ManualPosition.model_fields)


class ManualStoreNotEmpty(RuntimeError):
    pass


def _position_row(account_id: str, position: ManualPosition, sort_order: int, now: datetime) -> dict[str, Any]:
    # Все поля модели → одноимённые колонки. Новое поле без колонки уронит insert, а не потеряется молча.
    row = {k: (v.value if isinstance(v, Enum) else v) for k, v in position.model_dump().items()}
    return {**row, "account_id": account_id, "sort_order": sort_order, "updated_at": now}


def _check_unique(accounts: Iterable[ManualAccount]) -> None:
    seen: set[str] = set()
    for a in accounts:
        if a.id in seen:
            raise ValueError(f"ручной счёт {a.id} встречается дважды")
        seen.add(a.id)
        tickers = [p.ticker for p in a.positions]
        dup = {t for t in tickers if tickers.count(t) > 1}
        if dup:
            raise ValueError(f"счёт {a.id}: позиции с одинаковым тикером {sorted(dup)} (ключ позиции — счёт + тикер)")


class DbManualStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def load(self) -> ManualPortfolio:
        async with read_snapshot(self._engine) as conn:
            accounts = (await conn.execute(
                select(manual_account).order_by(manual_account.c.sort_order, manual_account.c.id))).mappings().all()
            positions = (await conn.execute(select(manual_position).order_by(
                manual_position.c.account_id, manual_position.c.sort_order, manual_position.c.ticker,
            ))).mappings().all()
            cash = (await conn.execute(
                select(manual_cash).order_by(manual_cash.c.account_id, manual_cash.c.currency))).mappings().all()
        by_account: dict[str, list[ManualPosition]] = defaultdict(list)
        for r in positions:
            by_account[r["account_id"]].append(ManualPosition.model_validate({k: r[k] for k in _POSITION_FIELDS}))
        cash_by: dict[str, dict[str, Decimal]] = defaultdict(dict)
        for r in cash:
            cash_by[r["account_id"]][r["currency"]] = r["amount"]
        return ManualPortfolio(accounts=tuple(
            ManualAccount(
                id=r["id"], name=r["name"], kind=AccountKind(r["kind"]), tinvest_account_id=r["tinvest_account_id"],
                positions=tuple(by_account[r["id"]]), cash=cash_by[r["id"]],
            )
            for r in accounts
        ))

    async def upsert_account(self, account: ManualAccount) -> None:
        async with write_tx(self._engine) as conn:
            res = await conn.execute(update(manual_account).where(manual_account.c.id == account.id).values(
                name=account.name, kind=account.kind.value, tinvest_account_id=account.tinvest_account_id))
            if res.rowcount == 0:
                _check_unique([account])
                order = await self._next_order(conn, manual_account.c.sort_order)
                await self._insert_account(conn, account, order, datetime.now(UTC))

    async def delete_account(self, account_id: str) -> None:
        async with write_tx(self._engine) as conn:
            await conn.execute(delete(manual_position).where(manual_position.c.account_id == account_id))
            await conn.execute(delete(manual_cash).where(manual_cash.c.account_id == account_id))
            await conn.execute(delete(manual_account).where(manual_account.c.id == account_id))

    async def upsert_position(self, account_id: str, position: ManualPosition) -> None:
        async with write_tx(self._engine) as conn:
            await self._require_account(conn, account_id)
            await conn.execute(delete(manual_position).where(
                manual_position.c.account_id == account_id, manual_position.c.ticker == position.ticker))
            order = await self._next_order(conn, manual_position.c.sort_order,
                                           manual_position.c.account_id == account_id)
            await conn.execute(insert(manual_position).values(
                _position_row(account_id, position, order, datetime.now(UTC))))

    async def delete_position(self, account_id: str, ticker: str) -> None:
        async with write_tx(self._engine) as conn:
            await self._require_account(conn, account_id)
            await conn.execute(delete(manual_position).where(
                manual_position.c.account_id == account_id, manual_position.c.ticker == ticker))

    async def set_cash(self, account_id: str, currency: str, amount: Decimal) -> None:
        cur = currency.lower()
        async with write_tx(self._engine) as conn:
            await self._require_account(conn, account_id)
            await conn.execute(delete(manual_cash).where(
                manual_cash.c.account_id == account_id, manual_cash.c.currency == cur))
            if amount != 0:
                await conn.execute(insert(manual_cash).values(account_id=account_id, currency=cur, amount=amount))

    async def replace_all(self, portfolio: ManualPortfolio, *, require_empty: bool = False) -> int:
        """Заменить весь ручной портфель одной транзакцией (импорт из YAML). Возвращает число счетов.

        `require_empty=True` — отказ (`ManualStoreNotEmpty`), если в БД уже есть счета.
        """
        _check_unique(portfolio.accounts)
        now = datetime.now(UTC)
        async with write_tx(self._engine) as conn:
            if require_empty and (await conn.execute(select(exists().select_from(manual_account)))).scalar_one():
                raise ManualStoreNotEmpty("в БД уже есть ручные счета — импорт отменён (нужна явная замена)")
            await conn.execute(delete(manual_position))
            await conn.execute(delete(manual_cash))
            await conn.execute(delete(manual_account))
            for i, account in enumerate(portfolio.accounts):
                await self._insert_account(conn, account, i, now)
        return len(portfolio.accounts)

    @staticmethod
    async def _insert_account(conn: AsyncConnection, account: ManualAccount, order: int, now: datetime) -> None:
        await conn.execute(insert(manual_account).values(
            id=account.id, name=account.name, kind=account.kind.value,
            tinvest_account_id=account.tinvest_account_id, sort_order=order,
        ))
        if account.positions:
            await conn.execute(insert(manual_position), [
                _position_row(account.id, p, j, now) for j, p in enumerate(account.positions)
            ])
        if account.cash:
            await conn.execute(insert(manual_cash), [
                {"account_id": account.id, "currency": cur, "amount": amt} for cur, amt in account.cash.items()
            ])

    @staticmethod
    async def _require_account(conn: AsyncConnection, account_id: str) -> None:
        found = (await conn.execute(select(manual_account.c.id).where(manual_account.c.id == account_id))).first()
        if found is None:
            raise KeyError(f"ручной счёт {account_id} не найден")

    @staticmethod
    async def _next_order(conn: AsyncConnection, column: Any, *where: ColumnElement[bool]) -> int:
        current = (await conn.execute(select(func.max(column)).where(*where))).scalar_one_or_none()
        return 0 if current is None else int(current) + 1
