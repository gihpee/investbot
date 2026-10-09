from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from investbot.journal.store import Journal, JournalCorrupted


async def test_chain_appends_and_verifies(tmp_path: Path) -> None:
    j = Journal(tmp_path / "journal.jsonl")
    await j.append("ips_proposal", {"variants": ["A", "B", "C"]})
    await j.append("ips_version", {"version": 1, "weights": {"equity_ru": Decimal("0.4")}}, author="user")
    assert await j.verify_chain() == 2


async def test_tampering_is_detected(tmp_path: Path) -> None:
    path = tmp_path / "journal.jsonl"
    j = Journal(path)
    await j.append("ips_version", {"version": 1})
    await j.append("ips_version", {"version": 2})
    lines = path.read_text(encoding="utf-8").splitlines()
    rec = json.loads(lines[0])
    rec["payload"]["version"] = 99
    lines[0] = json.dumps(rec, ensure_ascii=False)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(JournalCorrupted):
        await j.verify_chain()
