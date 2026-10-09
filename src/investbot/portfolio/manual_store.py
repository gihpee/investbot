"""Хранилище ручного портфеля: протокол и YAML-реализация (локальный запуск). В проде — БД (data/manual_repo.py)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

import yaml

from investbot.portfolio.manual import ManualAccount, ManualPortfolio, ManualPosition, load_manual


class ManualStore(Protocol):
    async def load(self) -> ManualPortfolio: ...

    async def upsert_account(self, account: ManualAccount) -> None:
        """Создать или обновить счёт (имя, тип, привязка к смарт-счёту). Позиции и кэш не трогаются."""
        ...

    async def delete_account(self, account_id: str) -> None: ...

    async def upsert_position(self, account_id: str, position: ManualPosition) -> None:
        """Ключ позиции — (счёт, тикер)."""
        ...

    async def delete_position(self, account_id: str, ticker: str) -> None: ...

    async def set_cash(self, account_id: str, currency: str, amount: Decimal) -> None: ...


def _plain(o: Any) -> Any:
    if isinstance(o, Decimal):
        return str(o)
    if isinstance(o, dict):
        return {k: _plain(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_plain(v) for v in o]
    return o


class YamlManualStore:
    """Файл config/manual_portfolio.yaml: читается при каждом снимке, правки бота перезаписывают его целиком."""

    def __init__(self, path: Path) -> None:
        self.path = path

    async def load(self) -> ManualPortfolio:
        return load_manual(self.path)

    def _save(self, mp: ManualPortfolio) -> None:
        data = _plain(mp.model_dump(mode="json", exclude_defaults=True))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text("# Ручной портфель. Правится ботом (/edit) или вручную.\n"
                       + yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
        tmp.replace(self.path)

    def _replace_account(self, mp: ManualPortfolio, account: ManualAccount) -> ManualPortfolio:
        accounts = [a for a in mp.accounts if a.id != account.id]
        idx = next((i for i, a in enumerate(mp.accounts) if a.id == account.id), len(accounts))
        accounts.insert(idx, account)
        return ManualPortfolio(accounts=tuple(accounts))

    def _account(self, mp: ManualPortfolio, account_id: str) -> ManualAccount:
        acc = next((a for a in mp.accounts if a.id == account_id), None)
        if acc is None:
            raise KeyError(f"ручной счёт {account_id} не найден")
        return acc

    async def upsert_account(self, account: ManualAccount) -> None:
        mp = load_manual(self.path)
        existing = next((a for a in mp.accounts if a.id == account.id), None)
        merged = account if existing is None else account.model_copy(
            update={"positions": existing.positions, "cash": existing.cash})
        self._save(self._replace_account(mp, merged))

    async def delete_account(self, account_id: str) -> None:
        mp = load_manual(self.path)
        self._save(ManualPortfolio(accounts=tuple(a for a in mp.accounts if a.id != account_id)))

    async def upsert_position(self, account_id: str, position: ManualPosition) -> None:
        mp = load_manual(self.path)
        acc = self._account(mp, account_id)
        positions = [p for p in acc.positions if p.ticker != position.ticker] + [position]
        self._save(self._replace_account(mp, acc.model_copy(update={"positions": tuple(positions)})))

    async def delete_position(self, account_id: str, ticker: str) -> None:
        mp = load_manual(self.path)
        acc = self._account(mp, account_id)
        positions = tuple(p for p in acc.positions if p.ticker != ticker)
        self._save(self._replace_account(mp, acc.model_copy(update={"positions": positions})))

    async def set_cash(self, account_id: str, currency: str, amount: Decimal) -> None:
        mp = load_manual(self.path)
        acc = self._account(mp, account_id)
        cash = {k: v for k, v in acc.cash.items() if k != currency.lower()}
        if amount != 0:
            cash[currency.lower()] = amount
        self._save(self._replace_account(mp, acc.model_copy(update={"cash": cash})))
