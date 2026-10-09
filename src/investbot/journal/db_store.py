"""Журнал в БД (ADR-13): таблица journal_record, append-only за счёт триггеров (миграция 0002).

Хэш считается той же функцией, что у файлового журнала (`store._hash`), по телу {ts, kind, author, payload};
ts хранится строкой ровно в том виде, в каком вошёл в хэш, payload — JSON. Поэтому одинаковые записи дают
одинаковые хэши, а JSONL-журнал переносится в БД как есть, без пересчёта цепочки (`import_file_journal`).

Добавление сериализовано: в Postgres — `pg_advisory_xact_lock` (работает и на пустой таблице, и без права UPDATE,
которое отбирает REVOKE), в SQLite — `BEGIN IMMEDIATE`. Уникальность `prev` в схеме не даёт цепочке ветвиться.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterable, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import exists, func, insert, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from investbot.data.db import journal_record, write_tx
from investbot.journal.store import GENESIS, Journal, JournalCorrupted, _default, _hash

_PG_LOCK_KEY = 0x6A6F75726E616C  # «journal»: ключ advisory-блокировки добавления в журнал
_BODY = ("ts", "kind", "author", "payload")


class JournalNotEmpty(RuntimeError):
    pass


def _dumps(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, default=_default)


def check_chain(records: Iterable[Mapping[str, Any]]) -> int:
    """Проверка хэш-цепочки по списку записей (как `Journal.verify_chain`). Возвращает число записей."""
    prev = GENESIS
    n = 0
    for n, r in enumerate(records, 1):
        body = {k: r[k] for k in _BODY}
        if r["prev"] != prev or r["hash"] != _hash(prev, body):
            raise JournalCorrupted(f"запись №{n} ({r.get('kind')}) изменена или вставлена вне цепочки")
        prev = r["hash"]
    return n


class DbJournal:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    @asynccontextmanager
    async def _append_tx(self) -> AsyncIterator[AsyncConnection]:
        async with write_tx(self._engine) as conn:
            if conn.dialect.name == "postgresql":
                await conn.execute(select(func.pg_advisory_xact_lock(_PG_LOCK_KEY)))
            yield conn

    @staticmethod
    async def _last_hash(conn: AsyncConnection) -> str:
        last = (await conn.execute(
            select(journal_record.c.hash).order_by(journal_record.c.seq.desc()).limit(1))).scalar_one_or_none()
        return GENESIS if last is None else str(last)

    async def append(self, kind: str, payload: dict[str, Any], author: str = "system") -> dict[str, Any]:
        stored = _dumps(payload)
        normalized = json.loads(stored)
        # В БД лежит JSON; хэш по прочитанному обратно payload должен совпасть с хэшем по исходному,
        # иначе verify_chain потом не сойдётся (так бывает только с нестроковыми ключами словарей).
        if _hash(GENESIS, {"payload": payload}) != _hash(GENESIS, {"payload": normalized}):
            raise ValueError(f"журнал: payload записи {kind} меняется при JSON-сериализации (нестроковые ключи?)")
        async with self._append_tx() as conn:
            prev = await self._last_hash(conn)
            body = {"ts": datetime.now(UTC).isoformat(), "kind": kind, "author": author, "payload": normalized}
            record = {**body, "prev": prev, "hash": _hash(prev, body)}
            await conn.execute(insert(journal_record).values(
                ts=body["ts"], kind=kind, author=author, payload=stored, prev=prev, hash=record["hash"]))
        return record

    @staticmethod
    async def _select_records(conn: AsyncConnection) -> list[dict[str, Any]]:
        rows = (await conn.execute(select(
            journal_record.c.ts, journal_record.c.kind, journal_record.c.author, journal_record.c.payload,
            journal_record.c.prev, journal_record.c.hash,
        ).order_by(journal_record.c.seq))).all()
        # тот же набор и порядок ключей, что у строк JSONL-журнала
        return [
            {"ts": ts, "kind": kind, "author": author, "payload": json.loads(payload), "prev": prev, "hash": h}
            for ts, kind, author, payload, prev, h in rows
        ]

    async def records(self) -> list[dict[str, Any]]:
        async with self._engine.connect() as conn:
            return await self._select_records(conn)

    async def verify_chain(self) -> int:
        return check_chain(await self.records())

    async def _import_records(self, records: list[dict[str, Any]]) -> int:
        async with self._append_tx() as conn:
            if (await conn.execute(select(exists().select_from(journal_record)))).scalar_one():
                raise JournalNotEmpty("журнал в БД не пуст — импорт файла отменён")
            if records:
                await conn.execute(insert(journal_record), [
                    {"ts": r["ts"], "kind": r["kind"], "author": r["author"], "payload": _dumps(r["payload"]),
                     "prev": r["prev"], "hash": r["hash"]}
                    for r in records
                ])
            check_chain(await self._select_records(conn))  # до COMMIT: при расхождении импорт откатится
        return len(records)


async def import_file_journal(src: Journal, dst: DbJournal) -> int:
    """Перенести JSONL-журнал в БД как есть (те же ts и хэши). Возвращает число записей.

    Цепочка источника проверяется на том самом снимке, который копируется; непустой журнал в БД — отказ.
    """
    records = await src.records()
    check_chain(records)
    return await dst._import_records(records)
