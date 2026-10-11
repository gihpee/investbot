"""Бумажное исполнение каждой рекомендации (§15.4) и scorecard (§15.5).

Что регистрируется (идемпотентно по `rec_key`):
- каждое действие плана ребалансировки — `rebalance_action`, ключ "plan:<plan_id>:<seq>";
- каждый кандидат топ-20 (§12.2 п. 8): показанный — `entry_idea`, непоказанный — `idea_candidate`,
  ключ "idea:<idea_id>". Исход считается для **всех** кандидатов: так оценка не страдает survivorship.

Исход на горизонте h недель (settle):
    start  — первый уровень TR-ряда в день as_of или позже (не дальше MAX_GAP_DAYS);
    end    — последний уровень не позже as_of + h недель (не раньше чем за MAX_GAP_DAYS до этой даты);
    ret    = TR(end) / TR(start) − 1, bench_ret — так же по ряду бенчмарка класса;
    excess = ret − bench_ret для покупки и bench_ret − ret для продажи (избежать падения — успех).
ret и bench_ret хранятся как есть (доходности рынка), знак стороны — только в excess.

Упрощения v1 (документированы в отчёте /journal одной строкой):
- исполнение по TR-уровню (закрытию) в день as_of или первый торговый день после него — без симуляции лимитной
  зоны, fill rate, спреда и комиссий; инвалидации по цене и тезису не моделируются (выход только по горизонту);
- налоги не учитываются;
- исходы считаются по всем горизонтам из `horizons`, а не только по собственному горизонту рекомендации
  (он хранится в `horizon_weeks` для справки);
- горизонт, приходящийся на будний день, рассчитывается со следующего дня: закрытие дня может быть ещё
  не опубликовано (EOD); при паузе торгов или делистинге исход откладывается до появления данных.
"""

from __future__ import annotations

import statistics
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from statistics import NormalDist
from typing import Any, Literal, Protocol, cast

import structlog
from pydantic import Field
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from investbot.core.models import Strict
from investbot.data.db import paper_outcome, paper_trade, read_snapshot, write_tx
from investbot.ideas.models import IdeaCandidate
from investbot.rebalance.models import RebalancePlan

log = structlog.get_logger(__name__)

PaperKind = Literal["rebalance_action", "entry_idea", "idea_candidate"]
KindFilter = Literal["rebalance_action", "entry_idea", "idea_candidate", "candidates"]
KIND_GROUPS: dict[str, tuple[str, ...]] = {
    "rebalance_action": ("rebalance_action",),
    "entry_idea": ("entry_idea",),
    "idea_candidate": ("idea_candidate",),
    "candidates": ("entry_idea", "idea_candidate"),  # все кандидаты топ-20, показанные и нет
}

INDEX = "index"  # asset_class для рядов индексов (бенчмарки)
DEFAULT_HORIZONS = (1, 4, 8, 12, 26)
JOURNAL_HORIZONS = (4, 12)
REBALANCE_HORIZON_WEEKS = 12  # у действия ребалансировки нет своего горизонта; ориентир — квартал
MAX_GAP_DAYS = 7  # насколько уровень может отстоять от as_of / даты горизонта (выходные, праздники, нет сделок)
MIN_N = 10  # меньше исходов — «мало данных»
CI_LEVEL = Decimal("0.90")
_Q = Decimal("1e-10")  # точность Money: одинаковое округление в SQLite и Postgres

# Бенчмарки классов (§15.5) для действий ребалансировки: бакет → индекс полной доходности MOEX.
# Денежный рынок, золото и ЦФА без бенчмарка: сравнение LQDT с LQDT ничего не говорит о качестве совета,
# индекса золота и «КС + спред» для ЦФА в TR-источнике нет. Такие сделки регистрируются, но не оцениваются.
BUCKET_BENCHMARKS: dict[str, str] = {
    "equity_ru": "MCFTR",
    "ofz_fixed": "RGBITR",
    "ofz_float_linker": "RUFLGBITR",  # линкеры тоже здесь: отдельного TR-индекса линкеров в ISS нет
    "corp_ig": "RUCBTRNS",
    "corp_hy": "RUCBHYTR",
}
# Класс бумаги по бакету, если вызывающий не передал точный (у RebalanceAction его нет).
BUCKET_ASSET_CLASS: dict[str, str] = {
    "equity_ru": "equity",
    "ofz_fixed": "bond",
    "ofz_float_linker": "bond",
    "corp_ig": "bond",
    "corp_hy": "bond",
    "money_market": "fund",
    "gold": "fund",
    "dfa": "dfa",
}
# Бенчмарки, которые не индексы (например, LQDT для денежного рынка, если его передать явно).
BENCHMARK_ASSET_CLASS: dict[str, str] = {"LQDT": "fund"}

Levels = list[tuple[date, Decimal]]


class TotalReturnSource(Protocol):
    """Ряд уровней полной доходности бумаги или индекса (для индексов — просто закрытия).

    Уровни сравнимы только внутри одного вызова: у облигаций выплаченные купоны накапливаются с первой даты
    ряда. Поэтому settle запрашивает ряд от as_of каждой рекомендации. Пустой список — данных нет.
    """

    async def tr_levels(self, ticker: str, asset_class: str, frm: date, to: date) -> Levels: ...


class SettleReport(Strict):
    settled: int = 0
    skipped: int = 0  # срок наступил, но исход не посчитан (нет данных/бенчмарка) — повтор при следующем settle
    reasons: dict[str, int] = Field(default_factory=dict)  # no_benchmark | source_error | no_data


Verdict = Literal["insufficient", "unconfirmed", "positive", "negative"]
VERDICT_TEXT: dict[str, str] = {
    "insufficient": "мало данных",
    "unconfirmed": "статистически не подтверждено",
    "positive": "лучше бенчмарка (ДИ выше нуля)",
    "negative": "хуже бенчмарка (ДИ ниже нуля)",
}


class Scorecard(Strict):
    """Качество рекомендаций группы на одном горизонте. Доли: 0.012 = 1,2 п.п. избыточной доходности."""

    kind: KindFilter | None = None  # None — все рекомендации
    source_arm: str | None = None
    horizon_weeks: int
    registered: int = 0  # всего рекомендаций в группе (с исходом и без)
    n: int = 0  # исходов на этом горизонте
    hit_rate: Decimal | None = None  # доля excess > 0
    mean_excess: Decimal | None = None
    ci_low: Decimal | None = None  # ДИ среднего (t-распределение), при n ≥ 2
    ci_high: Decimal | None = None
    ci_level: Decimal = CI_LEVEL
    median_excess: Decimal | None = None
    worst_excess: Decimal | None = None
    verdict: Verdict = "insufficient"

    @property
    def enough(self) -> bool:
        return self.n >= MIN_N


def _t_quantile(p: float, df: int) -> float:
    """Квантиль t-распределения; без scipy — нормальное приближение (шире не станет, только уже при малых n)."""
    try:
        from scipy.stats import t as student_t
    except ImportError:  # pragma: no cover — scipy в зависимостях, ветка на случай урезанной сборки
        return NormalDist().inv_cdf(p)
    return float(student_t.ppf(p, df))


def mean_ci(values: Sequence[Decimal], level: Decimal = CI_LEVEL) -> tuple[Decimal, Decimal] | None:
    """Двусторонний ДИ среднего: m ± t_{(1+level)/2, n−1} · s / √n (s — выборочное СКО). None при n < 2."""
    n = len(values)
    if n < 2:
        return None
    m = sum(values, Decimal(0)) / n
    var = sum(((v - m) ** 2 for v in values), Decimal(0)) / (n - 1)
    q = Decimal(repr(_t_quantile(float((1 + level) / 2), n - 1)))
    half = q * var.sqrt() / Decimal(n).sqrt()
    return m - half, m + half


def compute_scorecard(
    excesses: Sequence[Decimal],
    *,
    horizon_weeks: int,
    kind: KindFilter | None = None,
    source_arm: str | None = None,
    registered: int | None = None,
    level: Decimal = CI_LEVEL,
) -> Scorecard:
    """Чистая функция: метрики по списку избыточных доходностей (уже с учётом стороны)."""
    n = len(excesses)
    base = Scorecard(kind=kind, source_arm=source_arm, horizon_weeks=horizon_weeks,
                     registered=n if registered is None else registered, n=n, ci_level=level)
    if n == 0:
        return base
    mean = sum(excesses, Decimal(0)) / n
    ci = mean_ci(excesses, level)
    verdict: Verdict = "insufficient"
    if n >= MIN_N and ci is not None:
        verdict = "positive" if ci[0] > 0 else "negative" if ci[1] < 0 else "unconfirmed"
    return base.model_copy(update={
        "hit_rate": Decimal(sum(1 for x in excesses if x > 0)) / n,
        "mean_excess": mean,
        "ci_low": ci[0] if ci else None,
        "ci_high": ci[1] if ci else None,
        "median_excess": statistics.median(excesses),
        "worst_excess": min(excesses),
        "verdict": verdict,
    })


def window_return(levels: Sequence[tuple[date, Decimal]], start: date, target: date,
                  max_gap: int = MAX_GAP_DAYS) -> Decimal | None:
    """TR(end)/TR(start) − 1; None, если у начала или конца окна нет уровня в пределах max_gap дней."""
    gap = timedelta(days=max_gap)
    first = next(((d, v) for d, v in levels if start <= d <= target), None)
    last = next(((d, v) for d, v in reversed(levels) if start <= d <= target), None)
    if first is None or last is None or first[0] - start > gap or target - last[0] > gap:
        return None
    if last[0] <= first[0] or first[1] <= 0:
        return None
    return last[1] / first[1] - 1


def _q(x: Decimal) -> Decimal:
    return x.quantize(_Q)


def _eod_ready(target: date, today: date) -> bool:
    """Закрытие будничного дня горизонта может быть ещё не опубликовано — считаем со следующего дня."""
    return target < today or (target == today and target.weekday() >= 5)


def benchmark_class(benchmark: str) -> str:
    return BENCHMARK_ASSET_CLASS.get(benchmark, INDEX)


@dataclass(frozen=True, slots=True)
class _Due:
    row: Any  # строка paper_trade
    h: int
    target: date

    @property
    def bench(self) -> str | None:
        return cast(str | None, self.row.benchmark)


class PaperBroker:
    """Регистрация рекомендаций, бумажный settlement и scorecard. Ничего не торгует (ADR-05)."""

    def __init__(self, engine: AsyncEngine, tr: TotalReturnSource) -> None:
        self._engine = engine
        self._tr = tr

    # ---------- регистрация ----------

    async def _register(self, rows: Sequence[dict[str, Any]]) -> int:
        if not rows:
            return 0
        keys = [r["rec_key"] for r in rows]
        if len(set(keys)) != len(keys):
            raise ValueError("paper: повторяющиеся rec_key в одной регистрации")
        async with write_tx(self._engine) as conn:
            existing = set((await conn.execute(
                select(paper_trade.c.rec_key).where(paper_trade.c.rec_key.in_(keys)))).scalars())
            new = [r for r in rows if r["rec_key"] not in existing]
            if new:
                await conn.execute(insert(paper_trade), new)
        return len(new)

    async def register_plan(
        self,
        plan_id: str,
        plan: RebalancePlan,
        *,
        benchmarks: Mapping[str, str | None] | None = None,
        asset_classes: Mapping[str, str] | None = None,
        horizon_weeks: int = REBALANCE_HORIZON_WEEKS,
    ) -> int:
        """По сделке на каждое действие плана. Возвращает число новых записей (повторный вызов — 0).

        benchmarks — бакет → бенчмарк (поверх BUCKET_BENCHMARKS; None снимает бенчмарк);
        asset_classes — тикер → класс бумаги (иначе класс выводится из бакета).
        """
        bench_map: dict[str, str | None] = {**BUCKET_BENCHMARKS, **(benchmarks or {})}
        now = datetime.now(UTC)
        rows = [{
            "rec_key": f"plan:{plan_id}:{a.seq}",
            "kind": "rebalance_action",
            "source_arm": None,
            "shown": True,
            "ticker": a.ticker,
            "asset_class": (asset_classes or {}).get(a.ticker) or BUCKET_ASSET_CLASS.get(a.bucket, "other"),
            "side": a.side,
            "as_of": plan.as_of,
            "entry_price": a.price,
            "price_unit": "rub",
            "benchmark": bench_map.get(a.bucket),
            "horizon_weeks": horizon_weeks,
            "created_at": now,
        } for a in plan.actions]
        return await self._register(rows)

    async def register_ideas(self, ideas: Sequence[IdeaCandidate]) -> int:
        """Все кандидаты топ-20: показанные — entry_idea, остальные — idea_candidate. Идемпотентно по idea_id."""
        now = datetime.now(UTC)
        rows = [{
            "rec_key": f"idea:{c.idea.idea_id}",
            "kind": "entry_idea" if c.shown else "idea_candidate",
            "source_arm": c.idea.source_arm,
            "shown": c.shown,
            "ticker": c.idea.ticker,
            "asset_class": c.idea.asset_class.value,
            "side": "buy",
            "as_of": c.idea.as_of,
            "entry_price": c.idea.price,
            "price_unit": c.idea.price_unit,
            "benchmark": c.idea.benchmark or None,
            "horizon_weeks": c.idea.horizon_weeks,
            "created_at": now,
        } for c in ideas]
        return await self._register(rows)

    # ---------- settlement ----------

    async def _levels(self, ticker: str, asset_class: str, frm: date, to: date) -> Levels | None:
        try:
            return sorted(await self._tr.tr_levels(ticker, asset_class, frm, to))
        except Exception as e:  # источник недоступен — исход посчитаем при следующем settle
            log.warning("paper.tr_failed", ticker=ticker, asset_class=asset_class, error=type(e).__name__)
            return None

    async def settle(self, today: date, horizons: Iterable[int] = DEFAULT_HORIZONS) -> SettleReport:
        """Исходы для всех сделок, у которых наступил горизонт и ещё нет исхода. Повторный вызов ничего не дублирует."""
        hs = sorted(set(horizons))
        if not hs or hs[0] <= 0:
            raise ValueError("горизонты — положительные числа недель")
        async with read_snapshot(self._engine) as conn:
            trades = (await conn.execute(select(paper_trade).where(
                paper_trade.c.as_of <= today - timedelta(weeks=hs[0])).order_by(paper_trade.c.id))).all()
            done = {(r.paper_trade_id, r.horizon_weeks) for r in (await conn.execute(
                select(paper_outcome.c.paper_trade_id, paper_outcome.c.horizon_weeks))).all()}

        due: list[_Due] = []
        for t in trades:
            for h in hs:
                target = t.as_of + timedelta(weeks=h)
                if (t.id, h) not in done and _eod_ready(target, today):
                    due.append(_Due(t, h, target))

        reasons: Counter[str] = Counter()
        # ряды запрашиваются от as_of каждой рекомендации (см. TotalReturnSource), один раз на (бумага, as_of)
        need: dict[tuple[str, str, date], date] = {}
        for d in due:
            if d.bench is None:
                continue
            t = d.row
            for key in ((t.ticker, t.asset_class, t.as_of), (d.bench, benchmark_class(d.bench), t.as_of)):
                need[key] = max(need.get(key, d.target), d.target)
        series: dict[tuple[str, str, date], Levels | None] = {}
        for (ticker, cls, frm), to in need.items():
            series[(ticker, cls, frm)] = await self._levels(ticker, cls, frm, min(to, today))

        outcomes: list[dict[str, Any]] = []
        for d in due:
            t = d.row
            if d.bench is None:
                reasons["no_benchmark"] += 1
                continue
            own = series[(t.ticker, t.asset_class, t.as_of)]
            bench = series[(d.bench, benchmark_class(d.bench), t.as_of)]
            if own is None or bench is None:
                reasons["source_error"] += 1
                continue
            ret = window_return(own, t.as_of, d.target)
            bench_ret = window_return(bench, t.as_of, d.target)
            if ret is None or bench_ret is None:
                reasons["no_data"] += 1
                continue
            ret, bench_ret = _q(ret), _q(bench_ret)
            excess = ret - bench_ret if t.side == "buy" else bench_ret - ret
            outcomes.append({"paper_trade_id": t.id, "horizon_weeks": d.h, "settled_on": today,
                             "ret": ret, "bench_ret": bench_ret, "excess": excess})

        settled = 0
        if outcomes:
            async with write_tx(self._engine) as conn:
                ids = sorted({o["paper_trade_id"] for o in outcomes})
                exists = {(r.paper_trade_id, r.horizon_weeks) for r in (await conn.execute(
                    select(paper_outcome.c.paper_trade_id, paper_outcome.c.horizon_weeks).where(
                        paper_outcome.c.paper_trade_id.in_(ids)))).all()}
                fresh = [o for o in outcomes if (o["paper_trade_id"], o["horizon_weeks"]) not in exists]
                if fresh:
                    await conn.execute(insert(paper_outcome), fresh)
                settled = len(fresh)
        report = SettleReport(settled=settled, skipped=sum(reasons.values()), reasons=dict(reasons))
        log.info("paper.settled", settled=report.settled, skipped=report.skipped, reasons=report.reasons)
        return report

    # ---------- scorecard ----------

    @staticmethod
    async def _load(conn: AsyncConnection) -> tuple[list[Any], dict[tuple[int, int], Decimal]]:
        trades = list((await conn.execute(select(
            paper_trade.c.id, paper_trade.c.kind, paper_trade.c.source_arm))).all())
        outcomes = {(r.paper_trade_id, r.horizon_weeks): r.excess for r in (await conn.execute(
            select(paper_outcome.c.paper_trade_id, paper_outcome.c.horizon_weeks, paper_outcome.c.excess))).all()}
        return trades, outcomes

    @staticmethod
    def _card(trades: Sequence[Any], outcomes: Mapping[tuple[int, int], Decimal], kind: KindFilter | None,
              source_arm: str | None, horizon: int) -> Scorecard:
        kinds = KIND_GROUPS[kind] if kind is not None else None
        group = [t for t in trades if (kinds is None or t.kind in kinds)
                 and (source_arm is None or t.source_arm == source_arm)]
        excesses = [outcomes[(t.id, horizon)] for t in group if (t.id, horizon) in outcomes]
        return compute_scorecard(excesses, horizon_weeks=horizon, kind=kind, source_arm=source_arm,
                                 registered=len(group))

    async def scorecard(self, kind: KindFilter | None = None, source_arm: str | None = None,
                        horizon: int = 4) -> Scorecard:
        if kind is not None and kind not in KIND_GROUPS:
            raise ValueError(f"неизвестный вид рекомендаций: {kind}")
        async with read_snapshot(self._engine) as conn:
            trades, outcomes = await self._load(conn)
        return self._card(trades, outcomes, kind, source_arm, horizon)

    async def scorecards(self, horizons: Sequence[int] = JOURNAL_HORIZONS) -> list[Scorecard]:
        """Набор для /journal: действия ребалансировки, показанные идеи, все кандидаты; затем по рукам-источникам."""
        async with read_snapshot(self._engine) as conn:
            trades, outcomes = await self._load(conn)
        groups: list[tuple[KindFilter | None, str | None]] = [
            ("rebalance_action", None), ("entry_idea", None), ("candidates", None)]
        arms = sorted({t.source_arm for t in trades if t.source_arm is not None})
        groups += [("candidates", arm) for arm in arms]
        return [self._card(trades, outcomes, k, a, h) for k, a in groups for h in horizons]

    async def outcomes(self, horizon: int | None = None) -> list[dict[str, Any]]:
        """Исходы с атрибутами сделки — для отладки и выгрузки (без сумм в рублях)."""
        q = (select(paper_trade.c.rec_key, paper_trade.c.kind, paper_trade.c.side, paper_trade.c.ticker,
                    paper_trade.c.benchmark, paper_trade.c.as_of, paper_outcome.c.horizon_weeks,
                    paper_outcome.c.settled_on, paper_outcome.c.ret, paper_outcome.c.bench_ret,
                    paper_outcome.c.excess)
             .join(paper_outcome, paper_outcome.c.paper_trade_id == paper_trade.c.id)
             .order_by(paper_trade.c.id, paper_outcome.c.horizon_weeks))
        if horizon is not None:
            q = q.where(paper_outcome.c.horizon_weeks == horizon)
        async with read_snapshot(self._engine) as conn:
            return [dict(r._mapping) for r in (await conn.execute(q)).all()]
