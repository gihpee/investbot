"""Append-only журнал с хэш-цепочкой (ADR-13), временная реализация на JSONL до появления Postgres.

Каждая запись содержит хэш предыдущей; `verify_chain` находит любую правку задним числом.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

GENESIS = "0" * 64


def _default(o: Any) -> Any:
    if isinstance(o, Decimal):
        return str(o)
    if isinstance(o, datetime):
        return o.isoformat()
    raise TypeError(f"не сериализуется: {type(o).__name__}")


def _hash(prev: str, body: dict[str, Any]) -> str:
    raw = json.dumps(body, sort_keys=True, ensure_ascii=False, default=_default)
    return hashlib.sha256((prev + raw).encode("utf-8")).hexdigest()


class JournalCorrupted(RuntimeError):
    pass


class JournalLike(Protocol):
    """Append-only журнал: файловый (JSONL) или в БД (journal_record с триггерами)."""

    async def append(self, kind: str, payload: dict[str, Any], author: str = "system") -> dict[str, Any]: ...

    async def records(self) -> list[dict[str, Any]]: ...

    async def verify_chain(self) -> int: ...


class Journal:
    """Файловая реализация (JSONL) — для локального запуска; в проде — journal.db_store.DbJournal."""

    def __init__(self, path: Path) -> None:
        self._path = path

    async def records(self) -> list[dict[str, Any]]:
        return list(self._iter())

    def _iter(self) -> Iterator[dict[str, Any]]:
        if not self._path.exists():
            return
        with self._path.open(encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    yield json.loads(line)

    def _last_hash(self) -> str:
        last = GENESIS
        for r in self._iter():
            last = r["hash"]
        return last

    async def append(self, kind: str, payload: dict[str, Any], author: str = "system") -> dict[str, Any]:
        prev = self._last_hash()
        body = {"ts": datetime.now(UTC).isoformat(), "kind": kind, "author": author, "payload": payload}
        record = {**body, "prev": prev, "hash": _hash(prev, body)}
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=_default) + "\n")
        return record

    async def verify_chain(self) -> int:
        prev = GENESIS
        n = 0
        for n, r in enumerate(self._iter(), 1):
            body = {k: r[k] for k in ("ts", "kind", "author", "payload")}
            if r["prev"] != prev or r["hash"] != _hash(prev, body):
                raise JournalCorrupted(f"запись №{n} ({r.get('kind')}) изменена или вставлена вне цепочки")
            prev = r["hash"]
        return n
