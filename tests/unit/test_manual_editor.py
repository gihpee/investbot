from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from investbot.core.models import AccountKind, AssetClass
from investbot.journal.store import Journal
from investbot.portfolio.manual import ManualPortfolioError, ManualPosition
from investbot.portfolio.manual_store import YamlManualStore
from investbot.services.manual import ManualEditor, slugify

D = Decimal


def editor(tmp_path: Path, broker_ids: set[str] | None = None) -> tuple[ManualEditor, Journal, YamlManualStore]:
    store, journal = YamlManualStore(tmp_path / "manual.yaml"), Journal(tmp_path / "j.jsonl")
    return ManualEditor(store, journal, broker_ids), journal, store


def test_slugify_transliterates() -> None:
    assert slugify("ЦФА Альфа-Банк") == "cfa_alfa_bank"
    assert slugify("!!!") == "account"


async def test_create_account_add_dfa_card_cash_and_delete(tmp_path: Path) -> None:
    ed, journal, store = editor(tmp_path)
    acc = await ed.create_account("ЦФА Атомайз", AccountKind.DFA)
    dup = await ed.create_account("ЦФА Атомайз", AccountKind.DFA)
    assert acc.id == "cfa_atomaiz" and dup.id == "cfa_atomaiz_2"
    card = ManualPosition(ticker="ЦФА-1", name="Эмитент Б", asset_class=AssetClass.DFA, qty=D(3), nominal=D(10000))
    warnings = await ed.save_position(acc.id, card)
    assert any("оператор" in w for w in warnings) and any("погашения" in w for w in warnings)
    await ed.set_cash(acc.id, "RUB", D(1500))
    mp = await store.load()
    [a] = [x for x in mp.accounts if x.id == acc.id]
    assert a.positions[0].ticker == "ЦФА-1" and a.cash == {"rub": D(1500)}
    # повторное сохранение с тем же тикером заменяет позицию
    await ed.save_position(acc.id, card.model_copy(update={"qty": D(5), "operator": "Атомайз"}))
    assert (await store.load()).accounts[0].positions[0].qty == D(5)
    await ed.delete_position(acc.id, "ЦФА-1")
    await ed.delete_account(dup.id)
    mp = await store.load()
    assert [x.id for x in mp.accounts] == [acc.id] and not mp.accounts[0].positions
    kinds = [r["kind"] for r in await journal.records()]
    assert kinds.count("manual_position") == 3 and await journal.verify_chain() == len(kinds)
    # в журнал не попадают количества и цены
    assert all("qty" not in r["payload"] and "price" not in r["payload"] for r in await journal.records())


async def test_rejects_manual_positions_on_broker_accounts(tmp_path: Path) -> None:
    ed, _, _ = editor(tmp_path, {"2000123"})
    with pytest.raises(ManualPortfolioError):
        await ed.save_position("2000123", ManualPosition(ticker="X", asset_class=AssetClass.EQUITY, qty=D(1)))
    with pytest.raises(ManualPortfolioError):
        await ed.create_account("Брокер", AccountKind.BROKER, tinvest_account_id="2000123")
    acc = await ed.create_account("Другой брокер", AccountKind.BROKER)
    with pytest.raises(ManualPortfolioError):
        await ed.set_cash(acc.id, "rub", D(-1))
