"""Бумажное исполнение рекомендаций (§15.4): регистрация, settle, scorecard, TR из ISS и рендер /journal.

БД — SQLite (схема миграциями alembic) и Postgres (если задан INVESTBOT_TEST_PG_URL), как в test_db_storage.
TR-ряды — детерминированная подделка; ISS подменяется respx (сети нет).
"""

from __future__ import annotations

import asyncio
import math
import os
import statistics
from collections.abc import AsyncIterator, Callable, Iterator
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from alembic import command
from alembic.config import Config
from scipy.stats import t as student_t
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from investbot.adapters.moex_iss.client import IssClient
from investbot.adapters.moex_iss.total_return import IssTotalReturn
from investbot.channels.telegram.journal import build_journal_router, scorecard_message
from investbot.core.models import AssetClass
from investbot.data.db import make_engine, paper_trade
from investbot.ideas.models import EntryIdea, IdeaCandidate
from investbot.journal.paper import (
    MIN_N,
    PaperBroker,
    Scorecard,
    compute_scorecard,
    mean_ci,
    window_return,
)
from investbot.rebalance.models import RebalanceAction, RebalancePlan

D = Decimal
ROOT = Path(__file__).resolve().parents[2]
PG_URL = os.environ.get("INVESTBOT_TEST_PG_URL")

AS_OF = date(2026, 6, 1)  # понедельник
TODAY = date(2026, 8, 1)  # суббота: наступили горизонты 1, 4 и 8 недель (до 27.07), 12 — ещё нет


def alembic_config(url: str) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


@pytest.fixture(params=["sqlite", "postgres"])
async def engine(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    if request.param == "sqlite":
        url = f"sqlite+aiosqlite:///{tmp_path / 't.db'}"
        await asyncio.to_thread(command.upgrade, alembic_config(url), "head")  # env.py сам зовёт asyncio.run
        eng = make_engine(url)
    else:
        if not PG_URL:
            pytest.skip("INVESTBOT_TEST_PG_URL не задан")
        eng = make_engine(PG_URL)
        async with eng.begin() as conn:
            await conn.execute(text("TRUNCATE paper_outcome, paper_trade RESTART IDENTITY"))
    yield eng
    await eng.dispose()


# ---------- детерминированный источник TR ----------

def weekdays(frm: date, to: date) -> Iterator[date]:
    d = frm
    while d <= to:
        if d.weekday() < 5:
            yield d
        d += timedelta(days=1)


def steps(*points: tuple[date, int | str], until: date = date(2026, 12, 31)) -> list[tuple[date, Decimal]]:
    """Ступенчатый ряд по будням: значение действует с даты до следующей точки."""
    out = []
    for d in weekdays(points[0][0], until):
        value = next(v for start, v in reversed(points) if start <= d)
        out.append((d, D(value)))
    return out


START = date(2026, 5, 25)
OWN = steps((START, 100), (date(2026, 6, 8), 110), (date(2026, 6, 29), 120), (date(2026, 7, 27), 90),
            (date(2026, 8, 24), 150))
BENCH = steps((START, 100), (date(2026, 6, 8), 104), (date(2026, 6, 29), 102), (date(2026, 7, 27), 99),
              (date(2026, 8, 24), 125))
FLAT = steps((START, 100))


class FakeTR:
    def __init__(self, series: dict[tuple[str, str], list[tuple[date, Decimal]]],
                 default: Callable[[str], list[tuple[date, Decimal]]] | None = None) -> None:
        self.series = series
        self.default = default
        self.calls: list[tuple[str, str, date, date]] = []

    async def tr_levels(self, ticker: str, asset_class: str, frm: date, to: date) -> list[tuple[date, Decimal]]:
        self.calls.append((ticker, asset_class, frm, to))
        if ticker == "BROKEN":
            raise ConnectionError("ISS недоступен")
        rows = self.series.get((ticker, asset_class))
        if rows is None:
            rows = self.default(ticker) if self.default else []
        return [(d, v) for d, v in rows if frm <= d <= to]


def fake_tr() -> FakeTR:
    return FakeTR({
        ("SBER", "equity"): OWN,
        ("MCFTR", "index"): BENCH,
        ("RGBITR", "index"): FLAT,
        ("DELIST", "equity"): [(d, v) for d, v in OWN if d <= date(2026, 6, 10)],
    })


def action(seq: int, ticker: str, side: str = "buy", bucket: str = "equity_ru",
           price: str = "300.5") -> RebalanceAction:
    p = D(price)
    return RebalanceAction(
        seq=seq, account_id=f"acc{seq}", ticker=ticker, name=ticker, bucket=bucket, side=side,
        lots=1, qty=10, price=p, limit_zone=(p, p), valid_until=AS_OF + timedelta(days=5),
        est_amount_rub=p * 10, est_fee_rub=D(1), est_tax_rub=D(0), reason_codes=("band",),
        w_bucket_before=D("0.3"), w_bucket_after=D("0.31"),
    )


def plan(*actions: RebalanceAction, as_of: date = AS_OF) -> RebalancePlan:
    return RebalancePlan(
        as_of=as_of, ips_version=1, verdict="rebalance", nav_rub=D(1_000_000), actions=actions, buckets=(),
        drift_before=D("0.05"), drift_after=D("0.01"), turnover_rub=D(0), fees_rub=D(0), tax_rub=D(0),
    )


def idea(ticker: str, *, shown: bool, rank: int = 1, as_of: date = AS_OF, arm: str = "bond_carry_roll",
         benchmark: str = "RGBITR", horizon: int = 12) -> IdeaCandidate:
    return IdeaCandidate(rank=rank, shown=shown, idea=EntryIdea(
        idea_id=f"{arm}:{ticker}:{as_of.isoformat()}", as_of=as_of, asset_class=AssetClass.BOND, ticker=ticker,
        name=ticker, bucket="corp_ig", source_arm=arm, score=D("0.01"), price=D("98.75"), price_unit="pct_nominal",
        entry_low=D("98.5"), entry_high=D("99"), valid_until=as_of + timedelta(weeks=2), horizon_weeks=horizon,
        benchmark=benchmark, invalidation=("спред выше p90",), reasons=("carry",), risks=("кредитный риск",),
    ))


async def trades(engine: AsyncEngine) -> list[dict[str, Any]]:
    async with engine.connect() as conn:
        rows = (await conn.execute(select(paper_trade).order_by(paper_trade.c.id))).all()
    return [dict(r._mapping) for r in rows]


# ---------- регистрация ----------

async def test_register_plan_is_idempotent(engine: AsyncEngine) -> None:
    broker = PaperBroker(engine, fake_tr())
    p = plan(action(1, "SBER"), action(2, "SU26238RMFS4", "sell", bucket="ofz_fixed", price="581.23"),
             action(3, "LQDT", bucket="money_market", price="1.8765"))
    assert await broker.register_plan("abc123def456", p) == 3
    assert await broker.register_plan("abc123def456", p) == 0  # повтор — ни одной новой строки
    assert await broker.register_plan("abc123def456", plan(action(1, "SBER"), action(4, "GAZP"))) == 1

    rows = await trades(engine)
    assert [r["rec_key"] for r in rows] == ["plan:abc123def456:1", "plan:abc123def456:2", "plan:abc123def456:3",
                                            "plan:abc123def456:4"]
    sber, ofz, lqdt, _ = rows
    assert sber["kind"] == "rebalance_action" and sber["shown"] is True and sber["source_arm"] is None
    assert (sber["asset_class"], sber["benchmark"], sber["side"]) == ("equity", "MCFTR", "buy")
    assert (ofz["asset_class"], ofz["benchmark"], ofz["side"]) == ("bond", "RGBITR", "sell")
    assert ofz["entry_price"] == D("581.23") and ofz["price_unit"] == "rub" and ofz["as_of"] == AS_OF
    assert (lqdt["asset_class"], lqdt["benchmark"]) == ("fund", None)  # у денежного рынка бенчмарка нет
    assert sber["horizon_weeks"] == 12


async def test_register_plan_overrides(engine: AsyncEngine) -> None:
    broker = PaperBroker(engine, fake_tr())
    n = await broker.register_plan("p", plan(action(1, "TMOS"), action(2, "LQDT", bucket="money_market")),
                                   benchmarks={"money_market": "LQDT"}, asset_classes={"TMOS": "fund"},
                                   horizon_weeks=4)
    assert n == 2
    tmos, lqdt = await trades(engine)
    assert (tmos["asset_class"], tmos["benchmark"], tmos["horizon_weeks"]) == ("fund", "MCFTR", 4)
    assert lqdt["benchmark"] == "LQDT"


async def test_register_ideas_kinds_and_idempotency(engine: AsyncEngine) -> None:
    broker = PaperBroker(engine, fake_tr())
    cands = [idea("RU000A1", shown=True, rank=1), idea("RU000A2", shown=False, rank=2)]
    assert await broker.register_ideas(cands) == 2
    assert await broker.register_ideas(cands) == 0
    assert await broker.register_ideas([*cands, idea("RU000A3", shown=False, rank=3)]) == 1
    shown, hidden, _ = await trades(engine)
    assert shown["rec_key"] == "idea:bond_carry_roll:RU000A1:2026-06-01"
    assert (shown["kind"], shown["shown"]) == ("entry_idea", True)
    assert (hidden["kind"], hidden["shown"]) == ("idea_candidate", False)
    assert shown["source_arm"] == "bond_carry_roll" and shown["asset_class"] == "bond"
    assert (shown["entry_price"], shown["price_unit"], shown["benchmark"]) == (D("98.75"), "pct_nominal", "RGBITR")
    assert shown["side"] == "buy" and shown["horizon_weeks"] == 12
    with pytest.raises(ValueError, match="rec_key"):
        await broker.register_ideas([idea("X", shown=True), idea("X", shown=False)])


# ---------- settle ----------

def by_key(outcomes: list[dict[str, Any]]) -> dict[tuple[str, int], dict[str, Any]]:
    return {(o["rec_key"], o["horizon_weeks"]): o for o in outcomes}


async def test_settle_buy_and_sell_sign(engine: AsyncEngine) -> None:
    tr = fake_tr()
    broker = PaperBroker(engine, tr)
    await broker.register_plan("p", plan(action(1, "SBER"), action(2, "SBER", "sell")))
    report = await broker.settle(TODAY)
    assert (report.settled, report.skipped) == (6, 0)

    out = by_key(await broker.outcomes())
    expected = {1: (D("0.1"), D("0.04")), 4: (D("0.2"), D("0.02")), 8: (D("-0.1"), D("-0.01"))}
    for h, (ret, bench) in expected.items():
        buy, sell = out[("plan:p:1", h)], out[("plan:p:2", h)]
        assert (buy["ret"], buy["bench_ret"]) == (ret, bench)  # доходности рынка хранятся как есть
        assert (sell["ret"], sell["bench_ret"]) == (ret, bench)
        assert buy["excess"] == ret - bench
        assert sell["excess"] == bench - ret  # продажа: избежать падения — успех
        assert buy["settled_on"] == TODAY
    assert out[("plan:p:2", 8)]["excess"] == D("0.09")
    # ряды запрошены от as_of рекомендации и не дальше «сегодня», один раз на (бумага, as_of)
    assert sorted(tr.calls) == [("MCFTR", "index", AS_OF, date(2026, 7, 27)),
                                ("SBER", "equity", AS_OF, date(2026, 7, 27))]


async def test_settle_horizons_missing_data_and_no_duplicates(engine: AsyncEngine) -> None:
    broker = PaperBroker(engine, fake_tr())
    await broker.register_plan("p", plan(
        action(1, "SBER"), action(2, "LQDT", bucket="money_market"), action(3, "DELIST"), action(4, "BROKEN")))

    first = await broker.settle(TODAY)
    # SBER: 3 горизонта; LQDT: без бенчмарка; DELIST: торги прекратились 10.06 — только 1 неделя; BROKEN: ошибка
    assert first.settled == 4
    assert first.reasons == {"no_benchmark": 3, "no_data": 2, "source_error": 3}
    assert first.skipped == 8
    out = by_key(await broker.outcomes())
    assert set(out) == {("plan:p:1", 1), ("plan:p:1", 4), ("plan:p:1", 8), ("plan:p:3", 1)}

    again = await broker.settle(TODAY)
    assert again.settled == 0 and again.skipped == 8  # ничего не задвоилось, пропуски повторяются
    assert len(await broker.outcomes()) == 4

    later = await broker.settle(date(2026, 8, 25))  # вторник: наступил горизонт 12 недель (24.08)
    assert later.settled == 1
    h12 = by_key(await broker.outcomes(horizon=12))[("plan:p:1", 12)]
    assert (h12["ret"], h12["bench_ret"], h12["excess"]) == (D("0.5"), D("0.25"), D("0.25"))


async def test_settle_waits_for_eod_on_weekday_horizon(engine: AsyncEngine) -> None:
    broker = PaperBroker(engine, fake_tr())
    await broker.register_plan("p", plan(action(1, "SBER")))
    assert (await broker.settle(date(2026, 6, 8))).settled == 0  # понедельник горизонта: закрытия ещё может не быть
    assert (await broker.settle(date(2026, 6, 9))).settled == 1
    assert (await broker.settle(date(2026, 6, 9), horizons=(1, 4))).settled == 0


async def test_settle_rejects_bad_horizons(engine: AsyncEngine) -> None:
    with pytest.raises(ValueError):
        await PaperBroker(engine, fake_tr()).settle(TODAY, horizons=(0, 4))


def test_window_return_gaps() -> None:
    lv = [(date(2026, 6, 1), D(100)), (date(2026, 6, 5), D(105)), (date(2026, 6, 20), D(110))]
    assert window_return(lv, date(2026, 6, 1), date(2026, 6, 8)) == D("0.05")
    assert window_return(lv, date(2026, 5, 28), date(2026, 6, 8)) == D("0.05")  # старт в ближайший день после
    assert window_return(lv, date(2026, 6, 1), date(2026, 6, 15)) is None  # конец окна старше 7 дней
    assert window_return(lv, date(2026, 6, 6), date(2026, 6, 25)) is None  # начало на 14 дней позже as_of
    assert window_return(lv[:1], date(2026, 6, 1), date(2026, 6, 3)) is None  # один уровень
    assert window_return([], date(2026, 6, 1), date(2026, 6, 8)) is None


# ---------- scorecard ----------

def test_compute_scorecard_math() -> None:
    xs = [D(x) for x in ("0.01", "0.02", "-0.01", "0.03", "0", "0.05", "-0.02", "0.04", "0.01", "-0.03")]
    card = compute_scorecard(xs, horizon_weeks=4, kind="entry_idea")
    assert card.n == 10 and card.registered == 10 and card.enough
    assert card.hit_rate == D("0.6")  # ноль — не попадание
    assert card.mean_excess == D("0.01")
    assert card.median_excess == D("0.01")
    assert card.worst_excess == D("-0.03")
    fx = [float(x) for x in xs]
    half = student_t.ppf(0.95, 9) * statistics.stdev(fx) / math.sqrt(10)
    assert card.ci_low is not None and card.ci_high is not None
    assert math.isclose(float(card.ci_low), 0.01 - half, abs_tol=1e-12)
    assert math.isclose(float(card.ci_high), 0.01 + half, abs_tol=1e-12)
    assert card.ci_low < 0 < card.ci_high and card.verdict == "unconfirmed"

    good = compute_scorecard([D("0.01")] * 5 + [D("0.02")] * 5, horizon_weeks=4)
    assert good.verdict == "positive" and good.ci_low is not None and good.ci_low > 0
    bad = compute_scorecard([D("-0.01")] * 6 + [D("-0.03")] * 6, horizon_weeks=4)
    assert bad.verdict == "negative" and bad.worst_excess == D("-0.03")


def test_scorecard_minimum_n() -> None:
    few = compute_scorecard([D("0.05")] * (MIN_N - 1), horizon_weeks=12, registered=30)
    assert few.n == MIN_N - 1 and few.registered == 30 and not few.enough
    assert few.verdict == "insufficient" and few.mean_excess == D("0.05")
    empty = compute_scorecard([], horizon_weeks=4)
    assert empty.n == 0 and empty.hit_rate is None and empty.mean_excess is None and empty.verdict == "insufficient"
    one = compute_scorecard([D("0.01")], horizon_weeks=4)
    assert one.ci_low is None and one.median_excess == D("0.01")
    assert mean_ci([D(1)]) is None


async def test_scorecard_from_db_per_kind_and_arm(engine: AsyncEngine) -> None:
    # кандидаты B0…B11: уровень 100 → 95…106 с 08.06, бенчмарк ровный → excess (i − 5)%
    def level(ticker: str) -> list[tuple[date, Decimal]]:
        i = int(ticker[1:])
        return steps((START, 100), (date(2026, 6, 8), 95 + i))

    broker = PaperBroker(engine, FakeTR({**fake_tr().series}, default=level))
    cands = [idea(f"B{i}", shown=i < 4, rank=i + 1) for i in range(12)]
    cands += [idea("E1", shown=True, arm="eq_dividend", benchmark="RGBITR")]
    await broker.register_ideas(cands)
    await broker.register_plan("p", plan(action(1, "SBER")))
    await broker.settle(date(2026, 6, 9), horizons=(1,))

    allc = await broker.scorecard(kind="candidates", source_arm="bond_carry_roll", horizon=1)
    assert (allc.n, allc.registered) == (12, 12)
    assert allc.hit_rate == D("0.5") and allc.mean_excess == D("0.005") and allc.median_excess == D("0.005")
    assert allc.worst_excess == D("-0.05") and allc.verdict == "unconfirmed"

    shown = await broker.scorecard(kind="entry_idea", horizon=1)
    assert (shown.n, shown.registered, shown.verdict) == (5, 5, "insufficient")  # B0…B3 и E1
    assert (await broker.scorecard(kind="idea_candidate", horizon=1)).n == 8
    rebal = await broker.scorecard(kind="rebalance_action", horizon=1)
    assert (rebal.n, rebal.registered) == (1, 1)
    assert (await broker.scorecard(horizon=1)).n == 14
    assert (await broker.scorecard(horizon=4)).n == 0
    with pytest.raises(ValueError):
        await broker.scorecard(kind="bogus")

    cards = await broker.scorecards((1, 4))
    groups = [(c.kind, c.source_arm, c.horizon_weeks) for c in cards]
    assert groups == [("rebalance_action", None, 1), ("rebalance_action", None, 4),
                      ("entry_idea", None, 1), ("entry_idea", None, 4),
                      ("candidates", None, 1), ("candidates", None, 4),
                      ("candidates", "bond_carry_roll", 1), ("candidates", "bond_carry_roll", 4),
                      ("candidates", "eq_dividend", 1), ("candidates", "eq_dividend", 4)]
    assert cards[4].n == 13


# ---------- TR из ISS ----------

BASE = "https://iss.moex.com/iss"
BOND = "RU000A10TST1"
HIST_COLS = ["BOARDID", "TRADEDATE", "SECID", "CLOSE", "LEGALCLOSEPRICE", "ACCINT", "YIELDCLOSE", "DURATION",
             "FACEVALUE", "FACEUNIT"]
COUPON_COLS = ["isin", "name", "issuevalue", "coupondate", "recorddate", "startdate", "initialfacevalue",
               "facevalue", "faceunit", "value", "valueprc", "value_rub", "secid", "primary_boardid"]
AMORT_COLS = ["isin", "name", "issuevalue", "amortdate", "facevalue", "initialfacevalue", "faceunit", "valueprc",
              "value", "value_rub", "data_source", "secid", "primary_boardid"]
OFFER_COLS = ["isin", "name", "issuevalue", "offerdate", "offerdatestart", "offerdateend", "facevalue", "faceunit",
              "price", "value", "agent", "offertype", "secid", "primary_boardid"]


def security_json(secid: str, board: str, market: str) -> dict[str, Any]:
    return {
        "description": {"columns": ["name", "title", "value", "type", "sort_order", "is_hidden", "precision"],
                        "data": [["SECID", "Код", secid, "string", 1, 0, None]]},
        "boards": {"columns": ["secid", "boardid", "market", "engine", "is_primary"],
                   "data": [[secid, "SPOB", market, "stock", 0], [secid, board, market, "stock", 1]]},
    }


def history_json(rows: list[list[Any]], cols: list[str] = HIST_COLS) -> dict[str, Any]:
    return {"history": {"columns": cols, "data": rows},
            "history.cursor": {"columns": ["INDEX", "TOTAL", "PAGESIZE"], "data": [[0, len(rows), 100]]}}


def history_by_dates(rows: list[list[Any]], cols: list[str] = HIST_COLS) -> Callable[[httpx.Request], httpx.Response]:
    """Как ISS: отдаёт только строки с TRADEDATE в [from, till]."""
    i = cols.index("TRADEDATE")

    def respond(request: httpx.Request) -> httpx.Response:
        frm, till = request.url.params["from"], request.url.params["till"]
        return httpx.Response(200, json=history_json([r for r in rows if frm <= r[i] <= till], cols))

    return respond


def bond_row(d: str, close: float | None, accint: float, face: int) -> list[Any]:
    return ["TQCB", d, BOND, close, close, accint, 15.1, 600, face, "SUR"]


def coupon(pay: str, record: str | None, value: float | None) -> list[Any]:
    return [BOND, "Пример", 10**9, pay, record, "2026-03-05", 1000, 1000, "RUB", value, 16.4, value, BOND, "TQCB"]


def bondization_json(coupons: list[list[Any]]) -> dict[str, Any]:
    return {"coupons": {"columns": COUPON_COLS, "data": coupons},
            "amortizations": {"columns": AMORT_COLS, "data": [
                [BOND, "Пример", 10**9, "2026-09-08", 1000, 1000, "RUB", 50, 500, 500, "amortization", BOND, "TQCB"]]},
            "offers": {"columns": OFFER_COLS, "data": []}}


EMPTY_BONDIZATION = {"coupons": {"columns": COUPON_COLS, "data": []},
                     "amortizations": {"columns": AMORT_COLS, "data": []},
                     "offers": {"columns": OFFER_COLS, "data": []}}


def mock_bondization(iss: respx.MockRouter, secid: str, payload: dict[str, Any]) -> respx.Route:
    """Первая страница — данные, вторая — пустая (клиент листает до пустой страницы)."""
    return iss.get(f"/securities/{secid}/bondization.json").mock(side_effect=[
        httpx.Response(200, json=payload), httpx.Response(200, json=EMPTY_BONDIZATION)])


@pytest.fixture
def iss() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=BASE, assert_all_called=True) as router:
        yield router


async def no_sleep(_: float) -> None:
    return None


async def test_iss_bond_tr_with_coupon_and_amortization(iss: respx.MockRouter) -> None:
    iss.get(f"/securities/{BOND}.json").mock(
        return_value=httpx.Response(200, json=security_json(BOND, "TQCB", "bonds")))
    hist = iss.get(f"/history/engines/stock/markets/bonds/boards/TQCB/securities/{BOND}.json").mock(
        side_effect=history_by_dates([
            bond_row("2026-09-01", 99.5, 40.0, 1000),
            bond_row("2026-09-02", 99.6, 40.3, 1000),
            bond_row("2026-09-03", 99.7, 0.0, 1000),  # дата фиксации: НКД обнулён, купон уже наш
            bond_row("2026-09-04", None, 0.4, 1000),  # сделок не было
            bond_row("2026-09-07", 99.8, 1.3, 1000),
            bond_row("2026-09-08", 99.8, 0.7, 500),  # амортизация 50% номинала
        ]))
    mock_bondization(iss, BOND, bondization_json([
        coupon("2026-06-04", "2026-06-03", 44.0),  # до окна
        coupon("2026-09-04", "2026-09-03", 41.0),
        coupon("2026-12-04", "2026-12-03", None),  # будущий купон флоатера: вне окна, ряд не обрывает
    ]))

    async with IssClient(sleep=no_sleep) as client:
        tr = IssTotalReturn(client)
        levels = await tr.tr_levels(BOND, "bond", date(2026, 9, 1), date(2026, 9, 8))
        assert levels == [
            (date(2026, 9, 1), D("1035.0")),
            (date(2026, 9, 2), D("1036.3")),
            (date(2026, 9, 3), D("1038.0")),
            (date(2026, 9, 7), D("1040.3")),
            (date(2026, 9, 8), D("1040.7")),  # 499 + 0,7 НКД + 41 купон + 500 погашенного номинала
        ]
        # окно, начатое после купона: купон в уровни не входит (накопление — от первой даты ряда)
        later = await tr.tr_levels(BOND, "bond", date(2026, 9, 7), date(2026, 9, 8))
        assert later[0][1] == D("998") + D("1.3")
    params = hist.calls[0].request.url.params
    assert params["from"] == "2026-09-01" and params["till"] == "2026-09-08"
    assert window_return(levels, date(2026, 9, 1), date(2026, 9, 8)) == D("1040.7") / D("1035.0") - 1


async def test_iss_bond_tr_stops_before_unknown_coupon(iss: respx.MockRouter) -> None:
    iss.get(f"/securities/{BOND}.json").mock(return_value=httpx.Response(404))
    # карточки нет → режим по умолчанию для корпоративной облигации (TQCB)
    iss.get(f"/history/engines/stock/markets/bonds/boards/TQCB/securities/{BOND}.json").mock(
        return_value=httpx.Response(200, json=history_json([
            bond_row("2026-09-01", 100.0, 10.0, 1000),
            bond_row("2026-09-02", 100.1, 10.2, 1000),
            bond_row("2026-09-03", 100.2, 0.0, 1000),
        ])))
    mock_bondization(iss, BOND, bondization_json([coupon("2026-09-04", "2026-09-03", None)]))
    async with IssClient(sleep=no_sleep) as client:
        levels = await IssTotalReturn(client).tr_levels(BOND, "bond", date(2026, 9, 1), date(2026, 9, 3))
    assert levels == [(date(2026, 9, 1), D("1010.0")), (date(2026, 9, 2), D("1011.2"))]


async def test_iss_index_share_and_unsupported(iss: respx.MockRouter) -> None:
    iss.get("/history/engines/stock/markets/index/securities/MCFTR.json").mock(
        return_value=httpx.Response(200, json=history_json(
            [["2026-09-02", 7010.5], ["2026-09-01", 7000.0], ["2026-09-03", None]], cols=["TRADEDATE", "CLOSE"])))
    iss.get("/securities/TMOS.json").mock(
        return_value=httpx.Response(200, json=security_json("TMOS", "TQTF", "shares")))
    share_cols = ["TRADEDATE", "OPEN", "HIGH", "LOW", "CLOSE", "VOLUME", "VALUE", "WAPRICE"]
    iss.get("/history/engines/stock/markets/shares/boards/TQTF/securities/TMOS.json").mock(
        return_value=httpx.Response(200, json=history_json(
            [["2026-09-01", 7.1, 7.2, 7.0, 7.15, 10, 71.5, 7.1], ["2026-09-02", None, None, None, None, 0, 0, None]],
            cols=share_cols)))
    async with IssClient(sleep=no_sleep) as client:
        tr = IssTotalReturn(client)
        frm, to = date(2026, 9, 1), date(2026, 9, 3)
        assert await tr.tr_levels("MCFTR", "index", frm, to) == [
            (date(2026, 9, 1), D("7000.0")), (date(2026, 9, 2), D("7010.5"))]
        assert await tr.tr_levels("TMOS", "fund", frm, to) == [(date(2026, 9, 1), D("7.15"))]
        assert await tr.tr_levels("ЦФА-1", "dfa", frm, to) == []
        assert await tr.tr_levels("MCFTR", "index", to, frm) == []


# ---------- /journal ----------

def card(kind: str | None, h: int, xs: list[str], registered: int = 20, arm: str | None = None) -> Scorecard:
    return compute_scorecard([D(x) for x in xs], horizon_weeks=h, kind=kind, source_arm=arm,
                             registered=registered)


ENOUGH = ["0.01", "0.02", "-0.01", "0.03", "0", "0.05", "-0.02", "0.04", "0.01", "-0.03"]


def test_scorecard_message_render() -> None:
    cards = [
        card("rebalance_action", 4, ENOUGH), card("rebalance_action", 12, ["0.1"] * 3),
        card("entry_idea", 4, ["0.01"] * 5 + ["0.02"] * 5), card("entry_idea", 12, []),
        card("candidates", 4, ENOUGH), card("candidates", 12, ENOUGH),
        card("candidates", 4, ENOUGH, arm="bond_carry_roll"),
    ]
    text = scorecard_message(cards)
    assert "Действия ребалансировки" in text and "Показанные идеи" in text and "Все кандидаты топ-20" in text
    assert "облигации: carry + roll-down" in text
    assert "без учёта налогов" in text and "бенчмарка класса" in text
    assert "4 нед.: n=10 · попадания 60,0% · среднее +1,0 п.п." in text
    assert "ДИ 90%: −0,5…+2,5" in text
    assert "медиана +1,0 п.п. · худшее −3,0 п.п. — статистически не подтверждено" in text
    assert "12 нед.: мало данных (исходов 3, нужно от 10)" in text
    assert "12 нед.: мало данных (исходов 0, нужно от 10)" in text
    assert "лучше бенчмарка" in text
    assert "₽" not in text and "руб" not in text
    assert "Не инвестиционная рекомендация" in text
    assert text.index("Действия ребалансировки") < text.index("Показанные идеи") < text.index("Все кандидаты")


def test_scorecard_message_empty() -> None:
    text = scorecard_message([card("rebalance_action", 4, [], registered=0)])
    assert "Пока нечего оценивать" in text and "мало данных" not in text


class FakeMessage:
    def __init__(self) -> None:
        self.answers: list[str] = []

    async def answer(self, text: str, **_: Any) -> None:
        self.answers.append(text)


async def test_journal_router(engine: AsyncEngine) -> None:
    broker = PaperBroker(engine, fake_tr())
    await broker.register_plan("p", plan(action(1, "SBER")))
    await broker.settle(TODAY)
    handler = build_journal_router(broker).message.handlers[0].callback
    msg = FakeMessage()
    await handler(msg)
    assert "Действия ребалансировки</b> · всего 1" in msg.answers[0]
    assert "4 нед.: мало данных (исходов 1, нужно от 10)" in msg.answers[0]

    class Failing:
        async def scorecards(self, horizons: Any = (4, 12)) -> list[Scorecard]:
            raise ConnectionError("db down")

    failing = build_journal_router(Failing()).message.handlers[0].callback
    msg = FakeMessage()
    await failing(msg)
    assert msg.answers == ["Не удалось собрать scorecard: ConnectionError. Подробности — /status"]
