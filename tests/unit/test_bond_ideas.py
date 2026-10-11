"""Аналитика облигаций (КБД, carry/roll-down) и генератор идей v1 по облигациям."""

from __future__ import annotations

from datetime import date, time, timedelta
from decimal import ROUND_FLOOR, Decimal
from itertools import pairwise
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from investbot.adapters.moex_iss.models import BondMarketRow, CurvePoint
from investbot.analytics.bonds import (
    Curve,
    bond_metrics,
    breakeven_bp,
    carry,
    d_mod,
    effective_from_nominal,
    expected_loss,
    g_spread,
    mad,
    mechanical_excess,
    median,
    pd_for_rating,
    price_for_yield,
    quantile,
    roll_down,
    years_from_days,
)
from investbot.core.models import AssetClass, CouponKind, InstrumentMeta
from investbot.ideas import bonds as ib
from investbot.ideas.bonds import generate_bond_ideas, screen_bond
from investbot.ideas.config import IdeasConfig, load_ideas_config
from investbot.ideas.models import V1_NOTE, EntryIdea, IdeaCandidate
from investbot.rebalance.ips import IPS, BucketTarget, Limits
from investbot.rebalance.models import CostModel

D = Decimal
ROOT = Path(__file__).parent.parent.parent
AS_OF = date(2026, 10, 10)
R_F = D("0.13")
CURVE = Curve.from_knots(
    [
        (D("0.25"), D("0.12")),
        (D(1), D("0.13")),
        (D(2), D("0.14")),
        (D(3), D("0.145")),
        (D(5), D("0.15")),
        (D(10), D("0.155")),
    ]
)
PD = {
    "AAA": D("0.0005"),
    "AA+": D("0.001"),
    "AA": D("0.002"),
    "AA-": D("0.003"),
    "A+": D("0.004"),
    "A": D("0.006"),
    "A-": D("0.009"),
    "BBB+": D("0.013"),
}
CFG = IdeasConfig.model_validate(
    {
        "version": 1,
        "pd_annual": PD,
        "benchmarks": {
            "ofz_fixed": [
                {"index": "RUGBITR1Y", "max_duration_years": 1},
                {"index": "RUGBITR3Y", "max_duration_years": 3},
                {"index": "RUGBITR10Y"},
            ],
            "corp_ig": [{"index": "RUCBTRNS"}],
        },
    }
)
NO_GROUPS = CFG.model_copy(update={"min_group_size": 1000})
IPS_ = IPS(
    version=1,
    buckets={
        "ofz_fixed": BucketTarget(target=D("0.3"), band_abs=D("0.05")),
        "corp_ig": BucketTarget(target=D("0.3"), band_abs=D("0.05")),
        "money_market": BucketTarget(target=D("0.4")),
    },
)
CHEAP = CostModel(broker_fee_rate=D("0.0005"), half_spread_rate=D("0.0005"))  # круговые издержки 0,2%


def q(x: Decimal, places: int) -> Decimal:
    return x.quantize(D(1).scaleb(-places))


def row(
    secid: str,
    *,
    y: str | None = "16",
    days: int | None = 730,
    price: str | None = "99",
    board: str = "TQCB",
    mat: date | None = date(2030, 6, 1),
    offer: date | None = None,
    call: date | None = None,
    trades: int | None = 50,
    val: int = 5_000_000,
    face: str = "SUR",
) -> BondMarketRow:
    return BondMarketRow(
        secid=secid,
        board=board,
        shortname=secid,
        isin=secid,
        face_value=D(1000),
        face_unit=face,
        coupon_percent=D(15),
        coupon_value=D("37.4"),
        next_coupon=None,
        accrued_int=D(0),
        mat_date=mat,
        offer_date=offer,
        buyback_date=None,
        call_option_date=call,
        put_option_date=None,
        lot_size=1,
        list_level=2,
        sec_type="6",
        price_pct=None if price is None else D(price),
        effective_yield=None if y is None else D(y),
        yield_to_offer=None,
        yield_date=offer or mat,
        duration_days=None if days is None else D(days),
        gspread_bp=None,
        zspread_bp=None,
        val_today=D(val),
        num_trades=trades,
        trading_status="T",
    )


def meta(
    secid: str,
    *,
    rating: str | None = "ruAA",
    issuer: str | None = "auto",
    gov: bool = False,
    kind: CouponKind = CouponKind.FIXED,
    qual: bool = False,
    buy: bool = True,
) -> InstrumentMeta:
    return InstrumentMeta(
        ticker=secid,
        name=f"Бумага {secid}",
        asset_class=AssetClass.BOND,
        isin=secid,
        is_government=gov,
        coupon_kind=kind,
        rating=rating,
        issuer=f"Эмитент {secid}" if issuer == "auto" else issuer,
        for_qual_only=qual,
        buy_available=buy,
    )


def ofz(secid: str, **kw: object) -> tuple[BondMarketRow, InstrumentMeta]:
    return row(secid, board="TQOB", **kw), meta(secid, rating=None, issuer=None, gov=True)  # type: ignore[arg-type]


def run(
    pairs: list[tuple[BondMarketRow, InstrumentMeta]],
    *,
    cfg: IdeasConfig = CFG,
    costs: CostModel = CHEAP,
    qualified: bool = False,
    underweight: set[str] | None = None,
    held: dict[str, Decimal] | None = None,
    ips: IPS = IPS_,
) -> tuple[list[EntryIdea], list[IdeaCandidate]]:
    rows = [r for r, _ in pairs]
    metas = {m.isin: m for _, m in pairs if m.isin}
    return generate_bond_ideas(
        rows,
        metas,
        CURVE,
        R_F,
        ips,
        costs,
        AS_OF,
        cfg,
        qualified=qualified,
        underweight_buckets=underweight or set(),
        held_issuers=held,
    )


# --- КБД -----------------------------------------------------------------------------------------------------


def test_curve_interpolation_and_flat_extrapolation() -> None:
    c = Curve.from_knots([(D(3), D("0.14")), (D(1), D("0.10"))])  # порядок узлов не важен
    assert c.tenors == (D(1), D(3))
    assert c(D(1)) == D("0.10") and c(D(3)) == D("0.14")
    assert c(D(2)) == D("0.12")
    assert c(D("1.5")) == D("0.11")
    assert c(D("0.5")) == D("0.10") and c(D(0)) == D("0.10")  # плоско слева
    assert c(D(30)) == D("0.14")  # плоско справа


def test_curve_from_iss_points_in_percent() -> None:
    pts = [
        CurvePoint(tradedate=AS_OF, tradetime=time(18, 39), period=D(2), value=D("14.31")),
        CurvePoint(tradedate=AS_OF, tradetime=time(18, 39), period=D("0.25"), value=D("13.35")),
    ]
    c = Curve.from_points(pts)
    assert c.tenors == (D("0.25"), D(2))
    assert c.yields == (D("0.1335"), D("0.1431"))


@pytest.mark.parametrize(
    "knots",
    [
        [(D(1), D("0.1"))],  # одна точка
        [(D(1), D("0.1")), (D(1), D("0.11"))],  # дубль срока
        [(D(-1), D("0.1")), (D(1), D("0.11"))],  # отрицательный срок
    ],
)
def test_curve_rejects_bad_knots(knots: list[tuple[Decimal, Decimal]]) -> None:
    with pytest.raises(ValueError):
        Curve.from_knots(knots)


@given(
    st.lists(st.integers(1, 3000), min_size=2, max_size=8, unique=True),
    st.lists(st.integers(500, 2500), min_size=8, max_size=8),
)
def test_curve_monotone_and_bounded_between_knots(tenors_c: list[int], ys_bp: list[int]) -> None:
    tenors = sorted(D(t) / 100 for t in tenors_c)
    ys = sorted(D(y) / 10000 for y in ys_bp[: len(tenors)])
    c = Curve.from_knots(zip(tenors, ys, strict=True))
    vals = [c(D(i) / 10) for i in range(0, 320)]
    assert all(b >= a for a, b in pairwise(vals))  # монотонные узлы → монотонная кривая, без «перелётов»
    assert all(ys[0] <= v <= ys[-1] for v in vals)


# --- формулы ---------------------------------------------------------------------------------------------------


def test_metric_formulas_hand_computed() -> None:
    c = Curve.from_knots([(D(1), D("0.10")), (D(3), D("0.14"))])
    assert years_from_days(D(730)) == D(2)
    assert d_mod(D("2.32"), D("0.16")) == D(2)
    assert g_spread(D("0.15"), c, D(2)) == D("0.03")
    assert carry(D("0.16"), D("0.14"), D("0.5")) == D("0.01")
    # G(2,5) − G(2) = 0,13 − 0,12 = 0,01; × D_mod 2
    assert roll_down(D(2), c, D("2.5"), D("0.5")) == D("0.02")
    assert roll_down(D(2), c, D("0.3"), D("0.5")) == 0  # D − h < 0 → короткий конец плоский
    assert breakeven_bp(D("0.01"), D("0.02"), D(2)) == D(150)
    assert mechanical_excess(D("0.01"), D("0.02"), D("0.001"), D("0.002")) == D("0.027")
    assert price_for_yield(D(100), D(2), D("0.01")) == D(98)
    with pytest.raises(ValueError):
        breakeven_bp(D("0.01"), D(0), D(0))


def test_expected_loss_and_pd_lookup() -> None:
    table = {"AA-": D("0.003"), "A": D("0.006")}
    assert pd_for_rating("ruAA-", table) == D("0.003")
    assert pd_for_rating("A(RU)", table) == D("0.006")
    assert pd_for_rating("BBB", table) is None
    assert pd_for_rating(None, table) is None
    assert expected_loss("AA-(RU)", D("0.5"), table, D("0.65")) == D("0.000975")
    with pytest.raises(ValueError):
        expected_loss("BBB", D("0.5"), table, D("0.65"))
    with pytest.raises(ValueError):
        expected_loss(None, D("0.5"), table, D("0.65"))


def test_effective_rate_conversion() -> None:
    assert effective_from_nominal(D("0.12"), 1) == D("0.12")
    assert effective_from_nominal(D("0.10"), 2) == D("0.1025")
    # RUONIA 14% простых с ежедневным начислением ≈ 15,02% эффективных
    assert abs(effective_from_nominal(D("0.14"), 365) - D("0.150237")) < D("0.00001")
    with pytest.raises(ValueError):
        effective_from_nominal(D("0.1"), 0)


def test_robust_stats() -> None:
    assert median([D(3), D(1), D(2)]) == D(2)
    assert median([D(1), D(2), D(3), D(4)]) == D("2.5")
    assert mad([D(1), D(2), D(3), D(4), D(100)]) == D(1)
    assert quantile([D(1), D(2), D(3), D(4), D(5)], D("0.9")) == D("4.6")  # как numpy linear
    assert quantile([D(7)], D("0.9")) == D(7)
    with pytest.raises(ValueError):
        median([])
    with pytest.raises(ValueError):
        quantile([D(1)], D("1.5"))


def test_bond_metrics_combines_components() -> None:
    h = D("0.5")
    m = bond_metrics(
        y=D("0.16"), d_mac=D(2), r_f=R_F, curve=CURVE, h=h, pd_annual=D("0.002"), lgd=D("0.65"), cost=D("0.002")
    )
    dm = D(2) / D("1.16")
    assert m.d_mod == dm
    assert m.g_curve == D("0.14") and m.g_spread == D("0.02")
    assert m.carry == D("0.015")
    assert m.roll == dm * (D("0.14") - D("0.135"))
    assert m.el == D("0.00065")
    assert m.alpha_net == m.carry + m.roll - m.el - D("0.002")
    assert m.breakeven_bp == (m.carry + m.roll) / dm * 10000


# --- вселенная -------------------------------------------------------------------------------------------------


def screen(r: BondMarketRow, m: InstrumentMeta | None, **kw: object) -> str | None:
    cfg = kw.pop("cfg", CFG)
    ips = kw.pop("ips", IPS_)
    return screen_bond(r, m, AS_OF, cfg, ips, qualified=bool(kw.pop("qualified", False)), **kw)  # type: ignore[arg-type]


def test_screen_accepts_plain_ig_and_ofz() -> None:
    assert screen(row("C1"), meta("C1")) is None
    assert screen(*ofz("SU1")) is None
    # ОФЗ узнаётся и по флагу справочника, если режим не TQOB
    assert screen(row("SU2", board="TQCB"), meta("SU2", rating=None, gov=True)) is None


def test_screen_qualified_only() -> None:
    r, m = row("Q1"), meta("Q1", qual=True)
    assert screen(r, m) == ib.QUAL_ONLY
    assert screen(r, m, qualified=True) is None


def test_screen_floaters_linkers_and_missing_yield() -> None:
    assert screen(row("F1"), meta("F1", kind=CouponKind.FLOATING)) == ib.FLOATING
    assert screen(row("L1"), meta("L1", kind=CouponKind.LINKER)) == ib.FLOATING
    # тип купона неизвестен, но ISS не дал доходность (флоатер или ночной ноль)
    assert screen(row("U1", y=None), meta("U1", kind=CouponKind.UNKNOWN)) == ib.NO_YIELD
    assert screen(row("U2"), meta("U2", kind=CouponKind.UNKNOWN)) is None


def test_screen_rating_floor() -> None:
    assert screen(row("H1"), meta("H1", rating="ruBBB+")) == ib.NOT_IG
    assert screen(row("H2"), meta("H2", rating=None)) == ib.NOT_IG  # без рейтинга — ВДО
    assert screen(row("H3"), meta("H3", rating="A-(RU)")) is None  # ровно порог IPS
    strict = IPS_.model_copy(update={"rules": IPS_.rules.model_copy(update={"ig_min_rating": "AA-"})})
    assert screen(row("H3"), meta("H3", rating="A-(RU)"), ips=strict) == ib.NOT_IG


def test_screen_events() -> None:
    assert screen(row("E1", offer=AS_OF + timedelta(days=10)), meta("E1")) == ib.NEAR_EVENT
    assert screen(row("E2", offer=AS_OF + timedelta(days=14)), meta("E2")) == ib.NEAR_EVENT  # 10 рабочих дней
    assert screen(row("E3", call=AS_OF + timedelta(days=5)), meta("E3")) == ib.NEAR_EVENT
    assert screen(row("E4", mat=AS_OF + timedelta(days=7), days=200), meta("E4")) == ib.NEAR_EVENT
    assert screen(row("E5", offer=AS_OF + timedelta(days=400)), meta("E5")) is None
    assert screen(row("E6", mat=date(2020, 1, 1)), meta("E6")) == ib.NO_MATURITY
    assert screen(row("E7", mat=None), meta("E7")) == ib.NO_MATURITY  # бессрочные — мимо


def test_screen_liquidity_duration_and_data() -> None:
    assert screen(row("L1", trades=3), meta("L1")) == ib.ILLIQUID
    assert screen(row("L2", val=1_000_000), meta("L2")) == ib.ILLIQUID
    assert screen(row("L3", trades=None), meta("L3")) == ib.ILLIQUID
    assert screen(row("D1", days=100), meta("D1")) == ib.DURATION_BOUNDS
    assert screen(row("D2", days=9 * 365), meta("D2")) == ib.DURATION_BOUNDS
    assert screen(row("D3", days=None), meta("D3")) == ib.NO_DURATION
    assert screen(row("P1", price=None), meta("P1")) == ib.NO_PRICE
    assert screen(row("X1", face="USD"), meta("X1")) == ib.FX_FACE
    assert screen(row("M1"), None) == ib.NO_META


def test_screen_access_issuers_and_buckets() -> None:
    assert screen(row("B1"), meta("B1", buy=False)) == ib.NOT_BUYABLE
    excl = IPS_.model_copy(update={"limits": Limits(exclude_issuers=("Эмитент B2",))})
    assert screen(row("B2"), meta("B2"), ips=excl) == ib.EXCLUDED_ISSUER
    assert screen(row("B3"), meta("B3"), held_issuers={"Эмитент B3": D("0.10")}) == ib.ISSUER_LIMIT
    assert screen(row("B3"), meta("B3"), held_issuers={"Эмитент B3": D("0.05")}) is None
    # к ОФЗ лимит эмитента не применяется
    assert screen(*ofz("SU3"), held_issuers={ib.SOVEREIGN_ISSUER: D("0.5")}) is None
    half = BucketTarget(target=D("0.5"))
    no_corp = IPS(version=1, buckets={"ofz_fixed": half, "money_market": half})
    assert screen(row("B4"), meta("B4"), ips=no_corp) == ib.BUCKET_CLOSED
    override = IPS_.model_copy(update={"instrument_buckets": {"B5": "corp_hy"}})
    assert screen(row("B5"), meta("B5"), ips=override) == ib.BUCKET
    no_pd = CFG.model_copy(update={"pd_annual": {"AAA": D("0.0005")}})
    assert screen(row("B6"), meta("B6"), cfg=no_pd) == ib.NO_PD


# --- генератор -------------------------------------------------------------------------------------------------


def test_single_ofz_idea_hand_computed() -> None:
    ideas, cands = run([ofz("SU26240", y="16", days=730, price="99")], cfg=NO_GROUPS)

    assert len(ideas) == 1 and len(cands) == 1 and cands[0].shown and cands[0].rank == 1
    idea = ideas[0]
    h = D(182) / D(365)  # 26 недель
    dm = D(2) / D("1.16")
    carry_ = (D("0.16") - R_F) * h
    roll_ = dm * (D("0.14") - (D("0.13") + D("0.01") * (D(2) - h - 1)))  # G(2) − G(2 − h), линейно между 1 и 2 г.
    alpha = carry_ + roll_ - D("0.002")
    assert idea.metrics["carry"] == q(carry_, 6) == D("0.014959")
    assert idea.metrics["roll"] == q(roll_, 6) == D("0.008597")
    assert idea.metrics["el"] == 0 and idea.metrics["pd"] == 0
    assert idea.metrics["cost"] == D("0.002")
    assert idea.score == idea.metrics["alpha_net"] == q(alpha, 6) == D("0.021556")
    assert idea.metrics["breakeven_bp"] == q((carry_ + roll_) / dm * 10000, 1) == D("136.6")
    assert idea.metrics["g_spread_bp"] == D("200.0")
    assert idea.metrics["d_mod"] == q(dm, 4)
    assert idea.idea_id == "bond_carry_roll:SU26240:2026-10-10"
    assert idea.asset_class == AssetClass.BOND and idea.bucket == "ofz_fixed"
    assert idea.issuer == ib.SOVEREIGN_ISSUER
    assert idea.price == D(99) and idea.price_unit == "pct_nominal"
    assert (idea.entry_low, idea.entry_high) == (D("98.90"), D(99))  # группы нет → по рынку
    assert idea.target_yield is None
    assert idea.valid_until == date(2026, 10, 31) and idea.horizon_weeks == 26
    assert idea.benchmark == "RUGBITR3Y"
    assert idea.note == V1_NOTE
    assert idea.risks and idea.reasons and idea.invalidation
    assert any("+100 б.п. → цена ≈ −1,7%" in r and "+300 б.п. → цена ≈ −5,2%" in r for r in idea.risks)
    # без группы — только отмена по breakeven: max(y, —) + 136,6 б.п. = 17,37%
    assert idea.invalidation == ("Доходность выше 17,37%: запас breakeven 137 б.п. от уровня входа исчерпан",)


def test_universe_mixed_only_eligible_tickers() -> None:
    pairs = [
        (row("OK1"), meta("OK1")),
        (row("FLT"), meta("FLT", kind=CouponKind.FLOATING)),
        (row("HY1", y="25"), meta("HY1", rating="ruBB")),
        (row("QUAL", y="18"), meta("QUAL", qual=True)),
        (row("OFR", offer=AS_OF + timedelta(days=12)), meta("OFR")),
        (row("THIN", trades=1), meta("THIN")),
        ofz("SU1"),
    ]
    _, cands = run(pairs, cfg=NO_GROUPS)
    assert {c.idea.ticker for c in cands} == {"OK1", "SU1"}
    ideas_q, _ = run(pairs, cfg=NO_GROUPS, qualified=True)
    assert "QUAL" in {i.ticker for i in ideas_q}


def test_cost_barrier_depends_on_tariff() -> None:
    # carry 0,4986% + roll 0,6084% − EL 0,0648% за 26 нед. = 1,0422% до издержек; с 0,8% остаётся 0,24% < 0,3%
    weak = [(row("W1", y="14", days=400), meta("W1"))]
    assert run(weak, cfg=NO_GROUPS)[1][0].idea.score == D("0.008422")  # издержки 0,2% — проходит
    investor = CostModel(broker_fee_rate=D("0.003"), half_spread_rate=D("0.001"))  # круговые 0,8%
    assert run(weak, cfg=NO_GROUPS, costs=investor) == ([], [])


def test_diversity_one_per_issuer() -> None:
    pairs = [
        (row("A1", y="17"), meta("A1", issuer="Альфа")),
        (row("A2", y="16.5"), meta("A2", issuer="Альфа")),
        (row("B1", y="16"), meta("B1", issuer="Бета")),
    ]
    ideas, cands = run(pairs, cfg=NO_GROUPS)
    assert [i.ticker for i in ideas] == ["A1", "B1"]
    assert [(c.idea.ticker, c.rank, c.shown) for c in cands] == [("A1", 1, True), ("A2", 2, False), ("B1", 3, True)]


def test_diversity_per_class_and_total() -> None:
    corp = [(row(f"C{i}", y=str(17 - D(i) / 4)), meta(f"C{i}")) for i in range(5)]
    govs = [ofz("SU1", y="16.2"), ofz("SU2", y="16.1")]
    ideas, cands = run(corp + govs, cfg=NO_GROUPS)
    by_bucket = [i.bucket for i in ideas]
    assert by_bucket.count("corp_ig") == 3  # max_per_class
    assert by_bucket.count("ofz_fixed") == 1  # все ОФЗ — один эмитент
    assert len(cands) == 7 and [c.rank for c in cands] == list(range(1, 8))
    assert [c.idea for c in cands if c.shown] == ideas

    capped, _ = run(corp + govs, cfg=NO_GROUPS.model_copy(update={"max_ideas": 2}))
    assert [i.ticker for i in capped] == ["C0", "C1"]


def test_candidates_limited_to_top_n() -> None:
    corp = [(row(f"C{i}", y=str(17 - D(i) / 10)), meta(f"C{i}")) for i in range(8)]
    ideas, cands = run(corp, cfg=NO_GROUPS.model_copy(update={"max_candidates": 5}))
    assert [c.idea.ticker for c in cands] == ["C0", "C1", "C2", "C3", "C4"]
    assert [c.shown for c in cands] == [True, True, True, False, False]
    assert [i.ticker for i in ideas] == ["C0", "C1", "C2"]


def test_underweight_bucket_goes_first() -> None:
    pairs = [(row("CORP", y="17"), meta("CORP")), ofz("SU1", y="16")]
    plain, _ = run(pairs, cfg=NO_GROUPS)
    assert [i.ticker for i in plain] == ["CORP", "SU1"]
    assert not any(i.for_underweight_bucket for i in plain)

    ideas, cands = run(pairs, cfg=NO_GROUPS, underweight={"ofz_fixed"})
    assert [i.ticker for i in ideas] == ["SU1", "CORP"]
    assert cands[0].idea.ticker == "SU1" and cands[0].rank == 1
    assert ideas[0].for_underweight_bucket and not ideas[1].for_underweight_bucket
    assert any("ниже цели IPS" in r for r in ideas[0].reasons)
    assert ideas[0].score < ideas[1].score  # score не меняется, меняется только порядок


def _aa_group() -> list[tuple[BondMarketRow, InstrumentMeta]]:
    # D = 2 г. → G = 14%; спреды 100, 150, 200, 250, 300 б.п.: медиана 200, MAD 50, p90 280, цель 200 + 0,5·50 = 225
    ys = ["15", "15.5", "16", "16.5", "17"]
    prices = ["101", "100", "99", "99.37", "97"]
    return [(row(f"G{i}", y=y, price=p), meta(f"G{i}")) for i, (y, p) in enumerate(zip(ys, prices, strict=True))]


def test_entry_zone_from_group_target_yield() -> None:
    ideas, cands = run(_aa_group())
    by = {c.idea.ticker: c.idea for c in cands}

    assert "G4" not in by  # спред 300 б.п. выше p90 группы (280) — условие отмены уже выполнено
    limit = by["G2"]  # y = 16% < цели 16,25%
    assert limit.target_yield == D("0.162500")
    dm = D(2) / D("1.16")
    expected_high = (D(99) * (1 - dm * D("0.0025"))).quantize(D("0.01"), rounding=ROUND_FLOOR)
    assert expected_high == D("98.57")
    assert (limit.entry_low, limit.entry_high) == (D("98.47"), expected_high)
    assert limit.metrics["group_n"] == 5
    assert limit.metrics["group_median_bp"] == D("200.0") and limit.metrics["group_mad_bp"] == D("50.0")
    assert limit.metrics["group_p90_bp"] == D("280.0")
    assert limit.metrics["spread_z"] == D("0.00")
    assert any("ниже целевой 16,25%" in r for r in limit.risks)
    assert limit.invalidation[0] == "G-спред выше 280 б.п. — p90 группы AA × 2–3 г."
    # отмена по доходности — от уровня входа (цели): 16,25% + breakeven 136,6 б.п. (как у ОФЗ выше: EL в него не входит)
    assert limit.metrics["breakeven_bp"] == D("136.6")
    assert limit.invalidation[1] == "Доходность выше 17,62%: запас breakeven 137 б.п. от уровня входа исчерпан"
    assert "Понижение рейтинга ниже AA (порог IPS — A-)" in limit.invalidation[2]
    assert "Техдефолт" in limit.invalidation[3]

    market = by["G3"]  # y = 16,5% ≥ цели — по рынку
    assert (market.entry_low, market.entry_high) == (D("99.27"), D("99.37"))
    assert all(i.benchmark == "RUCBTRNS" for i in ideas)


def test_group_statistics_ignore_investor_filters() -> None:
    group = _aa_group()
    group[0] = (group[0][0], meta("G0", qual=True))  # бумага для квалов остаётся в статистике группы
    _, cands = run(group)
    assert "G0" not in {c.idea.ticker for c in cands}
    assert cands[0].idea.metrics["group_n"] == 5


def test_offer_limits_horizon_and_risk_lines() -> None:
    cfg = NO_GROUPS.model_copy(update={"min_duration_years": D("0.1")})
    offer = AS_OF + timedelta(days=90)
    ideas, _ = run([(row("OF1", y="19", days=85, offer=offer), meta("OF1"))], cfg=cfg, held={"Эмитент OF1": D("0.04")})
    (idea,) = ideas
    assert idea.horizon_weeks == 12
    assert idea.metrics["h_years"] == q(D(90) / D(365), 4)
    assert any(r.startswith("Доходность 19,00% к оферте 08.01.2027") for r in idea.reasons)
    assert any("Оферта 08.01.2027" in r for r in idea.risks)
    assert any("Кредитный риск AA:" in r for r in idea.risks)
    assert any("Эмитент уже в портфеле: 4,0% NAV при лимите IPS 10%" in r for r in idea.risks)
    assert any("меньше 1000 бумаг" in r for r in idea.risks)


def test_deterministic_regardless_of_row_order() -> None:
    pairs = [
        *_aa_group(),
        *[(row(f"C{i}", y=str(16 + D(i) / 7), days=400 + 90 * i), meta(f"C{i}")) for i in range(6)],
        ofz("SU1", y="16.3", days=1500),
        ofz("SU2", y="15.9", days=900),
    ]
    first = run(pairs)
    for permuted in (pairs[::-1], pairs[5:] + pairs[:5], sorted(pairs, key=lambda p: p[0].secid, reverse=True)):
        assert run(permuted) == first
    assert first[0]  # набор не пустой — проверка не тривиальна


# --- конфиг ----------------------------------------------------------------------------------------------------


def test_repo_config_loads() -> None:
    cfg = load_ideas_config(ROOT / "config" / "ideas.yaml")
    assert cfg.horizon_weeks == 26 and cfg.valid_days == 21 and cfg.min_days_to_event == 14
    assert (cfg.max_ideas, cfg.max_per_class, cfg.max_per_issuer, cfg.max_candidates) == (5, 3, 1, 20)
    assert cfg.min_alpha_net == D("0.003") and cfg.target_mad_k == D("0.5") and cfg.lgd == D("0.65")
    assert cfg.pd_annual["A-"] == D("0.009") and set(cfg.pd_annual) >= {"AAA", "A-", "B-"}
    assert cfg.benchmark("ofz_fixed", D("0.7")) == "RUGBITR1Y"
    assert cfg.benchmark("ofz_fixed", D(4)) == "RUGBITR5Y"
    assert cfg.benchmark("ofz_fixed", D(7)) == "RUGBITR10Y"
    assert cfg.benchmark("corp_ig", D(2)) == "RUCBTRNS"
    assert cfg.benchmark("gold", D(2)) == "RGBITR"


@pytest.mark.parametrize(
    "patch",
    [
        {"pd_annual": {"AA": "0.01", "A": "0.005"}},  # PD убывает при ухудшении рейтинга
        {"pd_annual": {"XYZ": "0.01"}},
        {"duration_bucket_edges": [2, 1]},
        {"min_duration_years": 9},
        {"benchmarks": {"stocks": [{"index": "MCFTR"}]}},
        {"rate_shocks_bp": [0]},
        {"unknown_field": 1},
    ],
)
def test_config_validation(patch: dict[str, object]) -> None:
    raw: dict[str, object] = {"version": 1, "pd_annual": {"AA": "0.002"}}
    with pytest.raises(ValidationError):
        IdeasConfig.model_validate(raw | patch)
