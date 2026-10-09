"""Перенос ручного портфеля из YAML (config/manual_portfolio.yaml) в БД."""

from __future__ import annotations

import asyncio
from pathlib import Path

from investbot.data.manual_repo import DbManualStore
from investbot.portfolio.manual import ManualPortfolio, load_manual


def _read(path: Path) -> ManualPortfolio:
    # Отсутствующий файл — ошибка, а не «пустой портфель»: иначе replace=True молча стёр бы БД.
    if not path.is_file():
        raise FileNotFoundError(f"нет файла ручного портфеля: {path}")
    return load_manual(path)


async def import_yaml(path: Path, store: DbManualStore, *, replace: bool = False) -> int:
    """Импортировать счета из YAML одной транзакцией. Возвращает число импортированных счетов.

    Если в БД уже есть ручные счета — `ManualStoreNotEmpty`, кроме явного `replace=True`
    (тогда содержимое БД целиком заменяется содержимым файла).
    """
    portfolio = await asyncio.to_thread(_read, path)
    return await store.replace_all(portfolio, require_empty=not replace)
