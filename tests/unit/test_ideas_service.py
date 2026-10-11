"""Сервис идей: MOEX (облигации, кривая) + справочник брокера + IPS → лента, журнал и бумажное исполнение."""

from __future__ import annotations

from datetime import date, time
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace as NS

from investbot.channels.telegram.ideas import idea_card, ideas_block, ideas_message
from investbot.core.models import InstrumentMeta
from investbot.ideas.models import IdeaCandidate
from investbot.journal.store import Journal
from investbot.rebalance.models import CostModel
from investbot.services.ideas import IdeasService, latest_curve_points
from investbot.services.portfolio import PortfolioService

from .test_bond_ideas import meta, ofz, row

D = Decimal
AS_OF = date(2026, 10, 10)
ROOT = Path(__file__).resolve().parents[2]

IPS_YAML = """version: 1
buckets:
  ofz_fixed: {target: 0.40, band_abs: 0.05}
  corp_ig: {target: 0.40, band_abs: 0.05}
  money_market: {target: 0.20, band_abs: 0.05}
"""
MANUAL_YAML = "accounts:\n  - id: cash\n    name: Кэш\n    cash: {rub: 100000}\n"


def curve_points() -> list[NS]:
    pts = [(0.25, 12.0), (1, 13.0), (2, 14.0), (3, 14.5), (5, 15.0), (10, 15.5)]
    old = [NS(tradedate=AS_OF, tradetime=time(10, 0), period=D(str(p)), value=D("99")) for p, _ in pts]
    new = [NS(tradedate=AS_OF, tradetime=time(18, 0), period=D(str(p)), value=D(str(v))) for p, v in pts]
    return old + new


class FakePaper:
    def __init__(self) -> None:
        self.calls: list[list[IdeaCandidate]] = []

    async def register_ideas(self, candidates: list[IdeaCandidate]) -> int:
        self.calls.append(list(candidates))
        return len(candidates)


def make(tmp_path: Path, with_meta: bool = True) -> tuple[IdeasService, Journal, FakePaper]:
    (tmp_path / "ips.yaml").write_text(IPS_YAML, encoding="utf-8")
    (tmp_path / "manual.yaml").write_text(MANUAL_YAML, encoding="utf-8")
    pairs = [ofz("SU26250RMFS1", y="15.5", days=1100)] + [
        (row(f"RU000A10{i:04d}", y=str(17 + i / 10), days=600 + 30 * i), meta(f"RU000A10{i:04d}")) for i in range(8)
    ]
    rows_by_board = {b: [r for r, _ in pairs if r.board == b] for b in ("TQOB", "TQCB")}

    async def bonds_market(board: str) -> list[object]:
        return list(rows_by_board[board])

    async def zcyc() -> list[NS]:
        return curve_points()

    async def bond_meta() -> dict[str, InstrumentMeta]:
        return {f"uid-{m.isin}": m for _, m in pairs} if with_meta else {}

    journal, paper = Journal(tmp_path / "j.jsonl"), FakePaper()
    portfolio = PortfolioService(None, tmp_path / "manual.yaml", tmp_path / "ips.yaml")
    svc = IdeasService(portfolio, bonds_market, zcyc, bond_meta, None, journal, paper,
                       CostModel(broker_fee_rate=D("0.0005"), half_spread_rate=D("0.0005")),
                       ROOT / "config" / "ideas.yaml", key_rate=D("0.13"))
    return svc, journal, paper


def test_latest_curve_points_takes_last_intraday_set() -> None:
    pts = latest_curve_points(curve_points())
    assert len(pts) == 6 and all(p.tradetime == time(18, 0) for p in pts)


async def test_weekly_publication_registers_once(tmp_path: Path) -> None:
    svc, journal, paper = make(tmp_path)
    res = await svc.weekly(AS_OF)
    assert res.published and res.ideas, res.warnings
    assert 1 <= len(res.ideas) <= 5 and len({i.issuer for i in res.ideas}) == len(res.ideas)
    assert sum(1 for c in res.candidates if c.shown) == len(res.ideas)
    kinds = [r["kind"] for r in await journal.records()]
    assert kinds[0] == "idea_candidates" and kinds.count("entry_idea") == len(res.ideas)
    again = await svc.weekly(AS_OF)
    assert again is res and len(paper.calls) == 1  # повтор отчёта в тот же день не публикует заново
    latest = await svc.latest(date(2026, 10, 12))
    assert latest is res

    block = ideas_block(res.ideas)
    assert block is not None and "Идеи недели" in block
    text, kb = ideas_message(res.ideas)
    assert kb is not None and "carry + roll-down" in text
    card = idea_card(res.ideas[0])
    assert "Против" in card and "Идея отменяется" in card


async def test_preview_without_publication_and_missing_meta(tmp_path: Path) -> None:
    svc, journal, paper = make(tmp_path, with_meta=False)
    res = await svc.latest(AS_OF)
    assert not res.published and not res.ideas
    assert any("справочника" in w for w in res.warnings) and any("предварительный" in w for w in res.warnings)
    assert await journal.records() == [] and paper.calls == []
