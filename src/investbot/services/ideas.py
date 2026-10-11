"""Сервис идей для входа (§12): данные MOEX + справочник брокера + IPS → идеи v1 по облигациям.

Публикация — раз в неделю вместе с отчётом (§12.2 п. 6): показанные идеи и весь топ-20 кандидатов
предрегистрируются в журнале и уходят в бумажное исполнение. `/ideas` между отчётами показывает
опубликованную ленту, а если её нет — предварительный расчёт без регистрации.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Awaitable, Callable, Sequence
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

import structlog

from investbot.analytics.bonds import Curve, effective_from_nominal
from investbot.core.models import AssetClass, InstrumentMeta, Strict
from investbot.ideas.bonds import generate_bond_ideas
from investbot.ideas.config import load_ideas_config
from investbot.ideas.models import EntryIdea, IdeaCandidate
from investbot.journal.store import JournalLike
from investbot.rebalance.models import CostModel
from investbot.services.portfolio import PortfolioOverview, PortfolioService

log = structlog.get_logger(__name__)

BOND_BOARDS = ("TQOB", "TQCB")  # ОФЗ и корпоративные облигации основного режима


class BondsMarket(Protocol):
    async def __call__(self, board: str) -> Sequence[Any]: ...


class PaperIdeas(Protocol):
    async def register_ideas(self, candidates: Sequence[IdeaCandidate]) -> int: ...


class IdeasResult(Strict):
    as_of: date
    ideas: tuple[EntryIdea, ...]
    candidates: tuple[IdeaCandidate, ...]
    warnings: tuple[str, ...] = ()
    published: bool = False


def latest_curve_points(points: Sequence[Any]) -> list[Any]:
    """ISS zcyc за дату может вернуть несколько внутридневных наборов — берём последний."""
    if not points:
        return []
    key = max((p.tradedate, p.tradetime) for p in points)
    return [p for p in points if (p.tradedate, p.tradetime) == key]


def held_issuer_shares(ov: PortfolioOverview) -> dict[str, Decimal]:
    """Доля портфеля по эмитенту (сквозной лимит §10.5) — по бумагам с известным эмитентом."""
    issuer_by_ticker = {p.instrument.ticker: p.instrument.issuer for p in ov.snapshot.positions if p.instrument.issuer}
    out: dict[str, Decimal] = defaultdict(lambda: Decimal(0))
    total = ov.valuation.total_rub
    if total <= 0:
        return {}
    for pv in ov.valuation.positions:
        issuer = issuer_by_ticker.get(pv.ticker)
        if issuer and pv.value_rub is not None:
            out[issuer] += pv.value_rub / total
    return dict(out)


class IdeasService:
    def __init__(self, portfolio: PortfolioService, bonds_market: BondsMarket,
                 zcyc: Callable[[], Awaitable[Sequence[Any]]],
                 bond_meta: Callable[[], Awaitable[dict[str, InstrumentMeta]]] | None,
                 qualified: Callable[[], Awaitable[bool]] | None,
                 journal: JournalLike, paper: PaperIdeas | None, costs: CostModel, config_path: Path,
                 key_rate: Decimal) -> None:
        self._portfolio = portfolio
        self._bonds_market = bonds_market
        self._zcyc = zcyc
        self._bond_meta = bond_meta
        self._qualified = qualified
        self._journal = journal
        self._paper = paper
        self._costs = costs
        self._config_path = config_path
        self._key_rate = key_rate
        self._published: IdeasResult | None = None

    async def _meta_by_isin(self) -> dict[str, InstrumentMeta]:
        if self._bond_meta is None:
            return {}
        return {m.isin: m for m in (await self._bond_meta()).values()
                if m.isin and m.asset_class is AssetClass.BOND}

    async def generate(self, as_of: date, publish: bool) -> IdeasResult:
        cfg = load_ideas_config(self._config_path)
        ov = await self._portfolio.overview()
        if ov.ips is None:
            raise LookupError("целевая аллокация не утверждена — сначала /allocation")
        warnings: list[str] = []
        rows: list[Any] = []
        for board in BOND_BOARDS:
            try:
                rows.extend(await self._bonds_market(board))
            except Exception as e:
                warnings.append(f"MOEX {board}: данные недоступны ({type(e).__name__})")
        points = latest_curve_points(await self._zcyc())
        if not points or not rows:
            warnings.append("нет данных рынка облигаций или кривой ОФЗ — идеи не построены")
            return IdeasResult(as_of=as_of, ideas=(), candidates=(), warnings=tuple(warnings))
        meta = await self._meta_by_isin()
        if not meta:
            warnings.append("нет справочника облигаций брокера (рейтинги, доступность) — идеи не построены")
            return IdeasResult(as_of=as_of, ideas=(), candidates=(), warnings=tuple(warnings))
        qualified = await self._qualified() if self._qualified is not None else False
        underweight = {d.bucket for d in ov.drift if d.target is not None and d.weight < d.target}
        ideas, candidates = generate_bond_ideas(
            rows, meta, Curve.from_points(points), effective_from_nominal(self._key_rate, 365), ov.ips,
            self._costs, as_of, cfg, qualified=qualified, underweight_buckets=underweight,
            held_issuers=held_issuer_shares(ov),
        )
        result = IdeasResult(as_of=as_of, ideas=tuple(ideas), candidates=tuple(candidates),
                             warnings=tuple(warnings), published=publish)
        if publish:
            await self._publish(result)
        return result

    async def _publish(self, result: IdeasResult) -> None:
        """Предрегистрация до отправки (§12.2 п. 8): показанные идеи и все кандидаты, затем бумажное исполнение."""
        await self._journal.append("idea_candidates", {
            "as_of": result.as_of.isoformat(),
            "candidates": [{"idea_id": c.idea.idea_id, "rank": c.rank, "shown": c.shown,
                            "score": c.idea.score, "arm": c.idea.source_arm} for c in result.candidates],
        })
        for idea in result.ideas:
            await self._journal.append("entry_idea", idea.model_dump(mode="json"))
        if self._paper is not None and result.candidates:
            try:
                await self._paper.register_ideas(result.candidates)
            except Exception as e:
                log.warning("ideas.paper_register_failed", error=type(e).__name__)
        self._published = result

    async def weekly(self, as_of: date) -> IdeasResult:
        """Публикация недели; повторный вызов в тот же день (повтор отчёта) не публикует заново."""
        if self._published is not None and self._published.as_of == as_of:
            return self._published
        return await self.generate(as_of, publish=True)

    async def latest(self, as_of: date) -> IdeasResult:
        """Опубликованная лента этой недели, иначе предварительный расчёт (без регистрации)."""
        if self._published is not None and (as_of - self._published.as_of).days < 7:
            return self._published
        preview = await self.generate(as_of, publish=False)
        return preview.model_copy(update={"warnings": (*preview.warnings, "предварительный расчёт: "
                                                       "лента публикуется с недельным отчётом")})
