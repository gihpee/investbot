"""Ручной портфель и журнал в БД: SQLite (схема — миграциями alembic, с триггерами) и Postgres (если задан URL)."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

import investbot.journal.db_store as db_store_mod
import investbot.journal.store as store_mod
from investbot.core.models import AccountKind, AssetClass, CouponKind, FundFocus
from investbot.data.db import make_engine
from investbot.data.manual_import import import_yaml
from investbot.data.manual_repo import DbManualStore, ManualStoreNotEmpty
from investbot.journal.db_store import DbJournal, JournalNotEmpty, import_file_journal
from investbot.journal.store import Journal, JournalCorrupted
from investbot.portfolio.manual import ManualAccount, ManualPortfolio, ManualPosition, load_manual
from investbot.portfolio.manual_store import YamlManualStore

D = Decimal
ROOT = Path(__file__).resolve().parents[2]
PG_URL = os.environ.get("INVESTBOT_TEST_PG_URL")  # postgresql+asyncpg://… с применёнными миграциями


def alembic_config(url: str) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


@pytest.fixture(params=["sqlite", "postgres"])
async def engine(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    if request.param == "sqlite":
        url = f"sqlite+aiosqlite:///{tmp_path / 't.db'}"
        # env.py сам зовёт asyncio.run — поэтому в отдельном потоке
        await asyncio.to_thread(command.upgrade, alembic_config(url), "head")
        eng = make_engine(url)
    else:
        if not PG_URL:
            pytest.skip("INVESTBOT_TEST_PG_URL не задан")
        eng = make_engine(PG_URL)
        async with eng.begin() as conn:
            # TRUNCATE журнала запрещён триггером — в тестовой БД снимаем его на время очистки (в той же транзакции)
            await conn.execute(text("ALTER TABLE journal_record DISABLE TRIGGER USER"))
            await conn.execute(text(
                "TRUNCATE journal_record, manual_position, manual_cash, manual_account RESTART IDENTITY"))
            await conn.execute(text("ALTER TABLE journal_record ENABLE TRIGGER USER"))
    yield eng
    await eng.dispose()


# ---------- ручной портфель ----------

# карточка, где каждое необязательное поле отличается от значения по умолчанию
FULL_CARD = ManualPosition(
    ticker="ЦФА-АТМ-1", name="Эмитент Б, флоатер КС+2%", asset_class=AssetClass.DFA, qty=D("5.5"),
    price=D("10012.34"), price_pct=D("100.1234"), nominal=D(10000), accrued=D("12.3456789012"), currency="usd",
    price_date=date(2026, 10, 1), is_government=True, coupon_kind=CouponKind.FLOATING, rating="ruA+",
    fund_focus=FundFocus.FIXED_INCOME, operator="Атомайз", maturity=date(2027, 6, 30),
)


def sample() -> ManualPortfolio:
    return ManualPortfolio(accounts=(
        ManualAccount(id="dfa_tbank", name="Смарт-счёт ЦФА (Т-Банк)", kind=AccountKind.DFA,
                      tinvest_account_id="2000999", positions=(FULL_CARD,), cash={"rub": D("1500.5")}),
        ManualAccount(id="other", name="Другой брокер", kind=AccountKind.BROKER, positions=(
            ManualPosition(ticker="SU26238RMFS4", name="ОФЗ 26238", asset_class=AssetClass.BOND, qty=D(10),
                           price_pct=D("58.123"), nominal=D(1000), accrued=D("3.21"), is_government=True,
                           coupon_kind=CouponKind.FIXED, price_date=date(2026, 10, 9), maturity=date(2041, 5, 15)),
            ManualPosition(ticker="TMON", asset_class=AssetClass.FUND, qty=D("123.456"), price=D("130.1"),
                           fund_focus=FundFocus.MONEY_MARKET),
            # 28 значащих цифр: SQLite через float их бы потерял
            ManualPosition(ticker="AAPL", asset_class=AssetClass.EQUITY, qty=D(2),
                           price=D("123456789012345678.1234567891"), currency="usd"),
        ), cash={"usd": D("0.01"), "rub": D(15000)}),
        ManualAccount(id="empty", name="Пустой", kind=AccountKind.OTHER),
    ))


def test_full_card_covers_every_field() -> None:
    for name, field in ManualPosition.model_fields.items():
        if not field.is_required():
            assert getattr(FULL_CARD, name) != field.get_default(call_default_factory=True), name


async def test_manual_portfolio_roundtrip(engine: AsyncEngine) -> None:
    store, mp = DbManualStore(engine), sample()
    assert await store.replace_all(mp) == 3
    loaded = await store.load()
    assert loaded == mp
    assert loaded.model_dump() == mp.model_dump()
    assert [a.id for a in loaded.accounts] == ["dfa_tbank", "other", "empty"]
    assert [p.ticker for p in loaded.accounts[1].positions] == ["SU26238RMFS4", "TMON", "AAPL"]
    assert str(loaded.accounts[1].positions[2].price) == "123456789012345678.1234567891"
    assert str(loaded.accounts[1].positions[0].qty) == "10"  # без хвоста .0000000000
    assert loaded.accounts[0].positions[0] == FULL_CARD
    assert loaded.positions()[0].source == "dfa_card"
    assert await DbManualStore(engine).load() == mp


async def test_same_semantics_as_yaml_store(engine: AsyncEngine, tmp_path: Path) -> None:
    db, ym = DbManualStore(engine), YamlManualStore(tmp_path / "manual.yaml")
    bond = sample().accounts[1].positions[0]
    ops: list[tuple[str, tuple[Any, ...]]] = [
        # новый счёт сохраняется целиком, вместе с позициями и кэшем
        ("upsert_account", (ManualAccount(id="a", name="A", positions=(FULL_CARD,), cash={"rub": D(1)}),)),
        ("upsert_account", (ManualAccount(id="b", name="B", kind=AccountKind.IIS),)),
        # существующий: меняются имя/тип/привязка, позиции и кэш — нет, место в списке — то же
        ("upsert_account", (ManualAccount(id="a", name="A2", kind=AccountKind.DFA, tinvest_account_id="1"),)),
        ("upsert_position", ("a", bond)),
        # замена позиции по тикеру — уходит в конец списка (как в YAML)
        ("upsert_position", ("a", FULL_CARD.model_copy(update={"qty": D(7), "operator": None}))),
        ("set_cash", ("a", "USD", D("2.5"))),
        ("set_cash", ("a", "rub", D(0))),  # ноль удаляет строку
        ("upsert_position", ("b", bond)),
        ("delete_position", ("a", bond.ticker)),
        ("delete_position", ("a", "нет-такого")),
        ("upsert_account", (ManualAccount(id="c", name="C"),)),
        ("delete_account", ("b",)),
        ("delete_account", ("нет-такого",)),
        ("upsert_account", (ManualAccount(id="b", name="B снова"),)),  # пересоздан — в конце
    ]
    for name, args in ops:
        await getattr(ym, name)(*args)
        await getattr(db, name)(*args)
        assert await db.load() == await ym.load(), name
    final = await db.load()
    assert [a.id for a in final.accounts] == ["a", "c", "b"]
    assert final.accounts[0].name == "A2" and final.accounts[0].cash == {"usd": D("2.5")}
    assert [p.qty for p in final.accounts[0].positions] == [D(7)]
    for store in (db, ym):
        with pytest.raises(KeyError):
            await store.upsert_position("missing", bond)
        with pytest.raises(KeyError):
            await store.set_cash("missing", "rub", D(1))
        with pytest.raises(KeyError):
            await store.delete_position("missing", bond.ticker)


async def _child_rows(engine: AsyncEngine, account_id: str) -> tuple[int, int]:
    async with engine.connect() as conn:
        pos = (await conn.execute(text("SELECT count(*) FROM manual_position WHERE account_id = :a"),
                                  {"a": account_id})).scalar_one()
        cash = (await conn.execute(text("SELECT count(*) FROM manual_cash WHERE account_id = :a"),
                                   {"a": account_id})).scalar_one()
    return int(pos), int(cash)


async def test_delete_account_cascades(engine: AsyncEngine) -> None:
    store = DbManualStore(engine)
    await store.replace_all(sample())
    assert await _child_rows(engine, "other") == (3, 2)
    await store.delete_account("other")
    assert await _child_rows(engine, "other") == (0, 0)
    assert [a.id for a in (await store.load()).accounts] == ["dfa_tbank", "empty"]
    # каскад объявлен и в схеме: прямой DELETE счёта уносит позиции и кэш
    async with engine.begin() as conn:
        if conn.dialect.name == "sqlite":
            await conn.exec_driver_sql("PRAGMA foreign_keys = ON")
        await conn.execute(text("DELETE FROM manual_account WHERE id = 'dfa_tbank'"))
    assert await _child_rows(engine, "dfa_tbank") == (0, 0)


async def test_replace_all_guards(engine: AsyncEngine) -> None:
    store = DbManualStore(engine)
    await store.replace_all(sample(), require_empty=True)
    with pytest.raises(ManualStoreNotEmpty):
        await store.replace_all(ManualPortfolio(), require_empty=True)
    acc = ManualAccount(id="x", name="X", positions=(FULL_CARD, FULL_CARD))
    with pytest.raises(ValueError, match="одинаковым тикером"):
        await store.replace_all(ManualPortfolio(accounts=(acc,)))
    with pytest.raises(ValueError, match="дважды"):
        await store.replace_all(ManualPortfolio(accounts=(ManualAccount(id="x", name="1"),
                                                          ManualAccount(id="x", name="2"))))
    assert await store.load() == sample()  # неудачные замены ничего не тронули
    assert await store.replace_all(ManualPortfolio()) == 0
    assert await store.load() == ManualPortfolio()


async def test_import_yaml(engine: AsyncEngine, tmp_path: Path) -> None:
    path = tmp_path / "manual_portfolio.yaml"
    shutil.copy(ROOT / "config" / "manual_portfolio.example.yaml", path)
    store = DbManualStore(engine)
    assert await import_yaml(path, store) == 2
    assert await store.load() == load_manual(path)
    with pytest.raises(ManualStoreNotEmpty):
        await import_yaml(path, store)
    await store.upsert_account(ManualAccount(id="extra", name="Лишний"))
    assert await import_yaml(path, store, replace=True) == 2
    assert await store.load() == load_manual(path)
    with pytest.raises(FileNotFoundError):
        await import_yaml(tmp_path / "nope.yaml", store, replace=True)
    assert await store.load() == load_manual(path)


# ---------- журнал ----------

def frozen_clock() -> type[datetime]:
    state = {"t": datetime(2026, 10, 9, 12, 0, tzinfo=UTC)}

    class Frozen(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:
            state["t"] += timedelta(seconds=1, microseconds=123)
            return state["t"]

    return Frozen


ENTRIES: list[tuple[str, dict[str, Any], str]] = [
    ("ips_proposal", {"variants": ["A", "B", "C"], "weights": {"equity_ru": D("0.40"), "bonds": D("0.6")}}, "system"),
    ("manual_position", {"op": "upsert", "ticker": "ЦФА-1", "qty": D("5.50"), "t": (1, 2),
                         "nested": [1, 2.5, None, True, {"б": "в", "a": [D("1e-7")]}]}, "user"),
    ("empty", {}, "system"),
]


async def test_db_journal_hashes_equal_file_journal(engine: AsyncEngine, tmp_path: Path,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    # одинаковые часы у обоих журналов → одинаковые ts → одинаковые хэши
    monkeypatch.setattr(store_mod, "datetime", frozen_clock())
    monkeypatch.setattr(db_store_mod, "datetime", frozen_clock())
    fj, dj = Journal(tmp_path / "journal.jsonl"), DbJournal(engine)
    for kind, payload, author in ENTRIES:
        a = await fj.append(kind, payload, author=author)
        b = await dj.append(kind, payload, author=author)
        assert (a["ts"], a["prev"], a["hash"]) == (b["ts"], b["prev"], b["hash"])
    file_records, db_records = await fj.records(), await dj.records()
    assert db_records == file_records
    assert [list(r) for r in db_records] == [list(r) for r in file_records]  # и порядок ключей тот же
    assert await dj.verify_chain() == await fj.verify_chain() == len(ENTRIES)


async def test_db_journal_payload_rules(engine: AsyncEngine) -> None:
    j = DbJournal(engine)
    at = datetime(2026, 10, 9, 12, 30, tzinfo=UTC)
    rec = await j.append("ips_version", {"at": at, "w": D("0.1")}, author="user")
    assert rec["payload"] == {"at": at.isoformat(), "w": "0.1"}
    assert (await j.records())[0] == rec
    # нестроковые ключи меняют порядок при JSON-сериализации → хэш не воспроизвести → отказ сразу
    with pytest.raises(ValueError, match="нестроковые"):
        await j.append("bad", {"x": {2: "a", 10: "b"}})
    assert await j.verify_chain() == 1


async def test_db_journal_concurrent_appends_keep_one_chain(engine: AsyncEngine) -> None:
    j = DbJournal(engine)
    await asyncio.gather(*(j.append("tick", {"i": i}) for i in range(8)))
    assert await j.verify_chain() == 8
    assert sorted(r["payload"]["i"] for r in await j.records()) == list(range(8))


async def test_journal_record_is_append_only(engine: AsyncEngine) -> None:
    j = DbJournal(engine)
    await j.append("ips_version", {"version": 1})
    statements = ["UPDATE journal_record SET kind = 'x'", "DELETE FROM journal_record"]
    if engine.dialect.name == "postgresql":
        statements.append("TRUNCATE journal_record")
    for sql in statements:
        with pytest.raises(DBAPIError, match="append-only"):
            async with engine.begin() as conn:
                await conn.execute(text(sql))
    assert await j.verify_chain() == 1


async def _tamper(engine: AsyncEngine, sql: str) -> None:
    """Правка в обход триггера (как это сделал бы владелец БД) — её должен поймать verify_chain."""
    async with engine.begin() as conn:
        if conn.dialect.name == "sqlite":
            await conn.execute(text("DROP TRIGGER journal_record_no_update"))
            await conn.execute(text(sql))
        else:
            await conn.execute(text("ALTER TABLE journal_record DISABLE TRIGGER USER"))
            await conn.execute(text(sql))
            await conn.execute(text("ALTER TABLE journal_record ENABLE TRIGGER USER"))


async def test_verify_chain_detects_tampering(engine: AsyncEngine) -> None:
    j = DbJournal(engine)
    await j.append("ips_version", {"version": 1})
    await j.append("ips_version", {"version": 2})
    assert await j.verify_chain() == 2
    await _tamper(engine, "UPDATE journal_record SET payload = '{\"version\": 99}' WHERE seq = 1")
    with pytest.raises(JournalCorrupted, match="№1"):
        await j.verify_chain()


async def test_verify_chain_detects_foreign_insert(engine: AsyncEngine) -> None:
    j = DbJournal(engine)
    await j.append("ips_version", {"version": 1})
    async with engine.begin() as conn:
        await conn.execute(text(
            "INSERT INTO journal_record (ts, kind, author, payload, prev, hash) "
            "VALUES (:ts, 'forged', 'x', '{}', :prev, :hash)"),
            {"ts": "2026-10-09T00:00:00+00:00", "prev": "f" * 64, "hash": "e" * 64})
    with pytest.raises(JournalCorrupted, match="№2"):
        await j.verify_chain()


async def test_import_file_journal(engine: AsyncEngine, tmp_path: Path) -> None:
    fj = Journal(tmp_path / "journal.jsonl")
    for kind, payload, author in ENTRIES:
        await fj.append(kind, payload, author=author)
    dj = DbJournal(engine)
    assert await import_file_journal(fj, dj) == len(ENTRIES)
    assert await dj.records() == await fj.records()
    nxt = await dj.append("after_import", {"ok": True})
    assert nxt["prev"] == (await fj.records())[-1]["hash"]
    assert await dj.verify_chain() == len(ENTRIES) + 1
    with pytest.raises(JournalNotEmpty):
        await import_file_journal(fj, dj)


async def test_import_file_journal_refuses_corrupted_source(engine: AsyncEngine, tmp_path: Path) -> None:
    path = tmp_path / "journal.jsonl"
    fj = Journal(path)
    await fj.append("ips_version", {"version": 1})
    await fj.append("ips_version", {"version": 2})
    lines = path.read_text(encoding="utf-8").splitlines()
    rec = json.loads(lines[1])
    rec["payload"]["version"] = 3
    lines[1] = json.dumps(rec, ensure_ascii=False)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    dj = DbJournal(engine)
    with pytest.raises(JournalCorrupted):
        await import_file_journal(fj, dj)
    assert await dj.records() == []
    assert await import_file_journal(Journal(tmp_path / "absent.jsonl"), dj) == 0


# ---------- миграции ----------

def _schema(url: str) -> tuple[set[str], set[str]]:
    con = sqlite3.connect(url.removeprefix("sqlite+aiosqlite:///"))
    try:
        rows = con.execute("SELECT type, name FROM sqlite_master WHERE type IN ('table', 'trigger')").fetchall()
    finally:
        con.close()
    return {n for t, n in rows if t == "table"}, {n for t, n in rows if t == "trigger"}


async def test_migration_0002_downgrade_and_upgrade_sqlite(tmp_path: Path) -> None:
    url = f"sqlite+aiosqlite:///{tmp_path / 'm.db'}"
    cfg = alembic_config(url)
    await asyncio.to_thread(command.upgrade, cfg, "head")
    tables, triggers = _schema(url)
    assert {"manual_account", "manual_position", "manual_cash", "journal_record"} <= tables
    assert triggers == {"journal_record_no_update", "journal_record_no_delete"}
    await asyncio.to_thread(command.downgrade, cfg, "0001")
    tables, triggers = _schema(url)
    assert not tables & {"manual_account", "manual_position", "manual_cash", "journal_record"} and not triggers
    assert "news_item" in tables
    await asyncio.to_thread(command.upgrade, cfg, "head")
    eng = make_engine(url)
    try:
        async with eng.connect() as conn:
            names = await conn.run_sync(lambda c: set(inspect(c).get_table_names()))
        assert "journal_record" in names
    finally:
        await eng.dispose()
