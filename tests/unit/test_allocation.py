"""Конструктор аллокации (§10.8): варианты A/B/C, правки, метрики. Синтетические данные с фиксированным seed."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal as D
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from investbot.allocation.config import load_allocation_config
from investbot.allocation.edits import apply_edit
from investbot.allocation.models import (
    AllocationEdit,
    AllocationVariant,
    DraftDiff,
    ReturnsData,
    RiskProfile,
    StressScenario,
)
from investbot.allocation.proposer import AllocationInfeasible, metrics, propose
from investbot.allocation.risk import (
    cvar_return,
    erc_weights,
    largest_remainder,
    ledoit_wolf,
    max_drawdown,
    risk_contributions,
)

ROOT = Path(__file__).resolve().parents[2]
BUCKETS = ("equity_ru", "ofz_fixed", "corp_ig", "money_market", "gold")
GROWTH = ("equity_ru", "corp_ig", "corp_hy")
GRID = D("0.005")
# накопленные доходности бакетов в окнах кризисов (порядок BUCKETS)
CRASHES = {
    (date(2014, 12, 1), date(2014, 12, 31)): (-0.25, -0.10, -0.05, 0.004, 0.15),
    (date(2022, 2, 1), date(2022, 3, 31)): (-0.40, -0.12, -0.08, 0.01, 0.10),
}
FEB22 = "Шок февраль–март 2022"
RATE_SHOCK = "Ставка ЦБ +5 п.п."


def make_data(seed: int = 7, weeks: int = 780) -> ReturnsData:
    rng = np.random.default_rng(seed)
    dates = tuple(date(2010, 1, 8) + timedelta(weeks=i) for i in range(weeks))
    mu = np.array([0.002, 0.0012, 0.0015, 0.0013, 0.0015])
    vol = np.array([0.035, 0.010, 0.007, 0.0004, 0.025])
    corr = np.array(
        [[1, 0.4, 0.4, 0, -0.1], [0.4, 1, 0.7, 0, 0], [0.4, 0.7, 1, 0, 0], [0, 0, 0, 1, 0], [-0.1, 0, 0, 0, 1]]
    )
    r = mu + (rng.standard_normal((weeks, 5)) @ np.linalg.cholesky(corr).T) * vol
    for (start, end), total in CRASHES.items():
        mask = np.array([start <= d <= end for d in dates])
        r[mask] = (1 + np.array(total)) ** (1 / mask.sum()) - 1
    return ReturnsData(BUCKETS, dates, r)


DATA = make_data()
SCENARIOS = load_allocation_config(ROOT / "config" / "allocation.yaml").scenarios

FEASIBLE = {
    "equity_ru": D("0.30"),
    "ofz_fixed": D("0.25"),
    "corp_ig": D("0.20"),
    "money_market": D("0.15"),
    "gold": D("0.06"),
    "dfa": D("0.04"),
}
RISKY = {
    "equity_ru": D("0.50"),
    "ofz_fixed": D("0.20"),
    "corp_ig": D("0.15"),
    "money_market": D("0.05"),
    "gold": D("0.06"),
    "dfa": D("0.04"),
}
YIELDS = {"equity_ru": D("0.08"), "ofz_fixed": D("0.15"), "corp_ig": D("0.17"), "money_market": D("0.16")}


def make_profile(**kw: Any) -> RiskProfile:
    base: dict[str, Any] = {
        "horizon_years": 10,
        "max_drawdown": D("0.20"),
        "bucket_min": {"money_market": D("0.05")},
        "bucket_max": {"equity_ru": D("0.60"), "corp_ig": D("0.40"), "gold": D("0.15")},
        "excluded": ("corp_hy",),
        "fixed": {"dfa": D("0.04")},
    }
    base.update(kw)
    return RiskProfile(**base)


PROFILE = make_profile()


def by_code(variants: list[AllocationVariant]) -> dict[str, AllocationVariant]:
    return {v.code: v for v in variants}


def l1(a: dict[str, D], b: dict[str, D]) -> D:
    return sum((abs(a.get(k, D(0)) - b.get(k, D(0))) for k in set(a) | set(b)), D(0))


def growth_share(w: dict[str, D]) -> D:
    return sum((v for k, v in w.items() if k in GROWTH), D(0))


@pytest.fixture(scope="module")
def variants() -> dict[str, AllocationVariant]:
    return by_code(propose(DATA, RISKY, PROFILE, SCENARIOS, YIELDS))


# --- модели -------------------------------------------------------------------------------------------


def test_returns_data_validates_shapes() -> None:
    dates = (date(2020, 1, 3), date(2020, 1, 10))
    with pytest.raises(ValueError, match="не совпадает"):
        ReturnsData(("equity_ru",), dates, np.zeros((2, 2)))
    with pytest.raises(ValueError, match="возрастать"):
        ReturnsData(("equity_ru",), (dates[1], dates[0]), np.zeros((2, 1)))
    with pytest.raises(ValueError, match="конечными"):
        ReturnsData(("equity_ru",), dates, np.array([[0.01], [np.nan]]))
    data = ReturnsData(("equity_ru",), dates, np.zeros((2, 1)))
    assert not data.returns.flags.writeable


def test_profile_and_edit_validation() -> None:
    with pytest.raises(ValueError, match="минимум"):
        make_profile(bucket_min={"gold": D("0.2")}, bucket_max={"gold": D("0.1")})
    with pytest.raises(ValueError, match="неизвестные"):
        make_profile(excluded=("crypto",))
    with pytest.raises(ValueError, match="исключены и зафиксированы"):
        make_profile(excluded=("dfa",))
    with pytest.raises(ValueError, match="нужен bucket"):
        AllocationEdit(op="exclude")
    with pytest.raises(ValueError, match="start и end"):
        StressScenario(name="x", kind="historical", start=date(2020, 1, 1))


# --- численные примитивы ------------------------------------------------------------------------------


def test_ledoit_wolf_matches_definition() -> None:
    rng = np.random.default_rng(1)
    x = rng.standard_normal((30, 8)) * np.linspace(0.5, 2, 8)
    cov, delta = ledoit_wolf(x)
    # прямой расчёт по Ledoit–Wolf (2004) с нормированной нормой Фробениуса ‖A‖² = tr(AAᵀ)/p
    xc = x - x.mean(axis=0)
    t, p = xc.shape
    s = xc.T @ xc / t
    mu = np.trace(s) / p
    d2 = np.sum((s - mu * np.eye(p)) ** 2) / p
    b2 = min(sum(np.sum((np.outer(r, r) - s) ** 2) / p for r in xc) / t**2, d2)
    assert delta == pytest.approx(b2 / d2)
    assert 0 < delta < 1
    np.testing.assert_allclose(cov, b2 / d2 * mu * np.eye(p) + (1 - b2 / d2) * s)
    assert np.all(np.linalg.eigvalsh(cov) > 0)


def test_drawdown_and_cvar_conventions() -> None:
    assert max_drawdown(np.array([0.1, -0.5, 0.2])) == pytest.approx(-0.5)
    assert max_drawdown(np.array([0.01, 0.02])) == 0.0
    r = np.linspace(-0.10, 0.09, 20)  # худшие 5% из 20 — одно наблюдение
    assert cvar_return(r) == pytest.approx(-0.10)
    r40 = np.arange(40) / 100 - 0.2  # 5% от 40 = 2 худших: −0.20 и −0.19
    assert cvar_return(r40) == pytest.approx(-0.195)


def test_erc_equalizes_risk_contributions_before_rounding() -> None:
    cov, _ = ledoit_wolf(DATA.returns[:, [0, 1, 2, 4]])
    w, ok = erc_weights(cov, np.zeros(4), np.ones(4), 0.91)
    assert ok
    assert w.sum() == pytest.approx(0.91)
    np.testing.assert_allclose(risk_contributions(w, cov), 0.25, atol=1e-4)


@given(
    raw=st.lists(st.floats(0.0, 1.0), min_size=2, max_size=8).filter(lambda xs: sum(xs) > 0.01),
    step=st.sampled_from([0.005, 0.01, 0.0001]),
)
def test_largest_remainder_preserves_sum_and_bounds(raw: list[float], step: float) -> None:
    w = np.array(raw) / sum(raw)
    units = round(1 / step)
    lo = np.zeros(len(w), dtype=np.int64)
    hi = np.full(len(w), units, dtype=np.int64)
    k = largest_remainder(w, lo, hi, units, step)
    assert int(k.sum()) == units
    assert np.all(k >= 0)
    assert np.all(np.abs(k * step - w) < step + 1e-12)


# --- варианты -----------------------------------------------------------------------------------------


@pytest.mark.parametrize("current", [FEASIBLE, RISKY])
def test_variants_respect_all_hard_constraints(current: dict[str, D]) -> None:
    variants = propose(DATA, current, PROFILE, SCENARIOS, YIELDS)
    assert [v.code for v in variants] == ["A", "B", "C"]
    for v in variants:
        assert sum(v.weights.values()) == 1
        assert all(w >= 0 for w in v.weights.values())
        assert "corp_hy" not in v.weights or v.weights["corp_hy"] == 0
        assert v.weights["dfa"] == D("0.04")
        assert v.weights["money_market"] >= D("0.05")
        for b, cap in PROFILE.bucket_max.items():
            assert v.weights.get(b, D(0)) <= cap
        for b, w in v.weights.items():
            if b != "dfa":
                assert w % GRID == 0, (v.code, b, w)


def test_a_equals_current_when_current_is_feasible() -> None:
    a = by_code(propose(DATA, FEASIBLE, PROFILE, SCENARIOS))["A"]
    assert a.weights == FEASIBLE
    assert a.metrics.turnover_from_current == 0
    assert a.violations == ()
    assert any("уже в рамках анкеты" in n for n in a.notes)


def test_a_moves_minimally_when_current_breaks_stress_limit(variants: dict[str, AllocationVariant]) -> None:
    current = metrics(RISKY, DATA, SCENARIOS, RISKY)
    assert current.stress[FEB22] < -PROFILE.max_drawdown  # текущий портфель нарушает допустимую просадку
    a = variants["A"]
    assert a.violations == ()
    # двигается ровно до границы стресса (с точностью до шага сетки), не дальше
    assert -PROFILE.max_drawdown <= a.metrics.stress[FEB22] <= -PROFILE.max_drawdown + D("0.005")
    # и с меньшим оборотом, чем любой другой допустимый вариант или наивное «срезать акции в кэш»
    naive = dict(RISKY, equity_ru=D("0.40"), money_market=D("0.15"))
    assert check_feasible(naive)
    for other in (variants["B"].weights, variants["C"].weights, naive):
        assert l1(a.weights, RISKY) <= l1(other, RISKY)
    assert a.metrics.turnover_from_current == l1(a.weights, RISKY) / 2


def check_feasible(w: dict[str, D]) -> bool:
    m = metrics(w, DATA, SCENARIOS, w)
    return all(r >= -PROFILE.max_drawdown for r in m.stress.values())


def test_stress_constraint_holds_in_a_and_c(variants: dict[str, AllocationVariant]) -> None:
    for code in ("A", "C"):
        v = variants[code]
        assert v.violations == ()
        assert v.metrics.stress, "должны быть посчитанные сценарии"
        assert all(r >= -PROFILE.max_drawdown for r in v.metrics.stress.values()), (code, v.metrics.stress)


def test_c_growth_at_least_b_when_b_feasible(variants: dict[str, AllocationVariant]) -> None:
    b, c = variants["B"], variants["C"]
    assert b.violations == ()
    assert growth_share(c.weights) >= growth_share(b.weights)
    assert any("Доля растущих бакетов" in n for n in c.notes)


def test_b_risk_contributions_equal_among_risky_buckets() -> None:
    loose = make_profile(bucket_max={})
    b = by_code(propose(DATA, FEASIBLE, loose, SCENARIOS))["B"]
    # денежный рынок (σ < 1% годовых) получает минимум и в выравнивании не участвует
    assert b.weights["money_market"] == D("0.05")
    # ERC по LW-ковариации всей вселенной: рисковые (equity, ofz, corp, gold) + «фиксированные» mm и dfa
    lw, _ = ledoit_wolf(DATA.returns)
    order = [0, 1, 2, 4, 3]
    cov = np.zeros((6, 6))
    cov[:5, :5] = lw[np.ix_(order, order)]
    w_star, ok = erc_weights(cov, np.zeros(4), np.ones(4), 0.91, fixed=np.array([0.05, 0.04]))
    assert ok
    rc = risk_contributions(np.concatenate([w_star, [0.05, 0.04]]), cov)[:4]
    np.testing.assert_allclose(rc / rc.sum(), 0.25, atol=1e-3)
    rounded = np.array([float(b.weights[x]) for x in ("equity_ru", "ofz_fixed", "corp_ig", "gold")])
    assert np.all(np.abs(rounded - w_star) <= float(GRID))


def test_b_stress_violation_is_reported_not_hidden() -> None:
    tight = make_profile(max_drawdown=D("0.10"), bucket_max={})
    v = by_code(propose(DATA, FEASIBLE, tight, SCENARIOS))
    b = v["B"]
    assert b.metrics.stress[RATE_SHOCK] < -tight.max_drawdown
    assert any(RATE_SHOCK in x and "хуже допустимой просадки" in x for x in b.violations)
    for code in ("A", "C"):
        assert v[code].violations == ()
        assert all(r >= -tight.max_drawdown for r in v[code].metrics.stress.values())


def test_c_respects_weekly_cvar_limit(variants: dict[str, AllocationVariant]) -> None:
    free_c = variants["C"]
    limit = D("0.025")
    assert free_c.metrics.cvar95_weekly < -limit  # без лимита C рискованнее
    c = by_code(propose(DATA, RISKY, PROFILE, SCENARIOS, max_cvar95_weekly=limit))["C"]
    assert c.metrics.cvar95_weekly >= -limit
    assert c.violations == ()
    assert growth_share(c.weights) <= growth_share(free_c.weights)
    assert any("CVaR95" in n for n in c.notes)


def test_infeasible_drawdown_names_binding_minimum() -> None:
    bad = make_profile(bucket_min={"equity_ru": D("0.55"), "money_market": D("0.05")})
    with pytest.raises(AllocationInfeasible) as exc:
        propose(DATA, RISKY, bad, SCENARIOS)
    msg = str(exc.value)
    assert "допустимая просадка недостижима при заданных минимумах бакетов" in msg.lower()
    assert "Акции РФ ≥ 55,0%" in msg


@pytest.mark.parametrize(
    ("kw", "cvar", "needle"),
    [
        ({"bucket_min": {"equity_ru": D("0.6"), "ofz_fixed": D("0.4")}}, None, "Сумма минимумов"),
        ({"bucket_min": {"corp_hy": D("0.05")}}, None, "исключён анкетой"),
        ({"bucket_min": {"fx_cash": D("0.05")}}, None, "нет истории"),
        ({"fixed": {"dfa": D("0.10")}, "bucket_max": {"dfa": D("0.07")}}, None, "зафиксирована"),
        ({"bucket_min": {"equity_ru": D("0.3"), "money_market": D("0.05")}}, D("0.005"), "CVaR95"),
    ],
)
def test_infeasible_profiles_raise(kw: dict[str, Any], cvar: D | None, needle: str) -> None:
    with pytest.raises(AllocationInfeasible, match=needle):
        propose(DATA, RISKY, make_profile(**kw), SCENARIOS, max_cvar95_weekly=cvar)


def test_notes_cover_skipped_scenarios_fixed_buckets_and_outside_holdings() -> None:
    current = dict(FEASIBLE, fx_cash=D("0.03"))
    variants = propose(DATA, current, make_profile(needs_income=True), SCENARIOS, YIELDS)
    for v in variants:
        assert "Кризис осени 2008" not in v.metrics.stress
        notes = " ".join(v.notes)
        assert "«Кризис осени 2008» пропущен" in notes
        assert "ЦФА 4,0%" in notes
        assert "Валютный кэш" in notes
        assert "Текущий денежный поток" in notes
        assert "fx_cash" not in v.weights


def test_empty_portfolio_and_determinism() -> None:
    first = propose(DATA, {}, PROFILE, SCENARIOS)
    second = propose(DATA, {}, PROFILE, SCENARIOS)
    assert [v.weights for v in first] == [v.weights for v in second]
    assert any("Текущий портфель пуст" in n for n in first[0].notes)
    assert first[0].metrics.turnover_from_current == 0


def test_metrics_sanity() -> None:
    m = metrics(FEASIBLE, DATA, SCENARIOS, RISKY, YIELDS)
    assert m.vol_annual > 0
    assert D(-1) < m.max_drawdown_hist <= 0
    assert m.cvar95_weekly < 0
    assert set(m.stress) == {"Рублёвый кризис, декабрь 2014", FEB22, RATE_SHOCK}
    assert m.stress[FEB22] == pytest.approx(D("-0.1585"), abs=D("1e-6"))  # Σ w·R по окну, dfa без истории = 0
    assert m.turnover_from_current == D("0.2")
    assert m.current_yield == sum((FEASIBLE[b] * y for b, y in YIELDS.items()), D(0))  # gold и dfa без доходности → 0
    assert metrics(FEASIBLE, DATA, SCENARIOS, FEASIBLE).turnover_from_current == 0


def test_config_defaults() -> None:
    cfg = load_allocation_config(ROOT / "config" / "allocation.yaml")
    assert cfg.diversification_lambda == D("0.1")
    assert cfg.growth_buckets == GROWTH
    kinds = {s.name: s.kind for s in cfg.scenarios}
    assert kinds[RATE_SHOCK] == "shock"
    assert sum(k == "historical" for k in kinds.values()) == 3
    shock = next(s for s in cfg.scenarios if s.kind == "shock")
    assert shock.shocks["ofz_fixed"] < 0


# --- правки -------------------------------------------------------------------------------------------


def edit(weights: dict[str, D], **kw: Any) -> DraftDiff:
    return apply_edit(weights, AllocationEdit(**kw), PROFILE, DATA, SCENARIOS, YIELDS)


def test_shift_bucket_takes_proportionally_from_others() -> None:
    diff = edit(FEASIBLE, op="shift_bucket", bucket="equity_ru", value=D("0.05"))
    after = diff.after
    assert sum(after.values()) == 1
    assert after["equity_ru"] == D("0.35")
    assert after["dfa"] == D("0.04")
    assert after["ofz_fixed"] / after["corp_ig"] == pytest.approx(D("1.25"), abs=D("0.001"))
    assert diff.violations == ()
    assert diff.metrics_before.turnover_from_current == 0
    assert diff.metrics_after.turnover_from_current == D("0.05")


def test_shift_bucket_is_clipped_and_reported() -> None:
    diff = edit(FEASIBLE, op="shift_bucket", bucket="equity_ru", value=D("0.40"))
    assert diff.after["equity_ru"] == D("0.60")
    assert any("сдвиг +40,0 п.п. ограничен границами анкеты, применено +30,0 п.п." in v for v in diff.violations)
    back = edit(FEASIBLE, op="shift_bucket", bucket="money_market", value=D("-0.15"))
    assert back.after["money_market"] == D("0.05")  # не ниже минимума анкеты
    fixed = edit(FEASIBLE, op="shift_bucket", bucket="dfa", value=D("0.02"))
    assert fixed.after == FEASIBLE
    assert any("зафиксирована" in v for v in fixed.violations)


def test_more_risk_reports_stress_violation() -> None:
    diff = edit(FEASIBLE, op="more_risk", value=D("0.25"))
    assert sum(diff.after.values()) == 1
    assert growth_share(diff.after) == growth_share(FEASIBLE) + D("0.25")
    assert diff.metrics_after.stress[FEB22] < -PROFILE.max_drawdown
    assert any(FEB22 in v for v in diff.violations)


def test_less_risk_moves_default_step_to_defensive() -> None:
    diff = edit(FEASIBLE, op="less_risk")
    assert growth_share(diff.after) == growth_share(FEASIBLE) - D("0.05")
    assert diff.violations == ()
    assert diff.metrics_after.vol_annual < diff.metrics_before.vol_annual


def test_exclude_redistributes_and_reports_caps() -> None:
    diff = edit(FEASIBLE, op="exclude", bucket="gold")
    assert diff.after["gold"] == 0
    assert sum(diff.after.values()) == 1
    near_caps = {
        "equity_ru": D("0.60"),
        "corp_ig": D("0.30"),
        "money_market": D("0.06"),
        "dfa": D("0.04"),
    }
    capped = apply_edit(
        near_caps,
        AllocationEdit(op="exclude", bucket="corp_ig"),
        make_profile(bucket_max={"equity_ru": D("0.60"), "money_market": D("0.20")}),
        DATA,
        SCENARIOS,
    )
    assert sum(capped.after.values()) == 1
    assert any("выше максимума анкеты" in v for v in capped.violations)


def test_set_weight_is_exact_and_flags_bounds() -> None:
    diff = edit(FEASIBLE, op="set_weight", bucket="equity_ru", value=D("0.65"))
    assert diff.after["equity_ru"] == D("0.65")
    assert diff.after["dfa"] == D("0.04")
    assert sum(diff.after.values()) == 1
    assert any("Акции РФ: 65,0% выше максимума анкеты 60,0%" in v for v in diff.violations)


_shares = st.lists(st.integers(0, 40), min_size=5, max_size=5).filter(lambda xs: sum(xs) > 0)
_edits = st.one_of(
    st.builds(
        AllocationEdit,
        op=st.just("shift_bucket"),
        bucket=st.sampled_from(BUCKETS),
        value=st.sampled_from([D("-0.1"), D("-0.05"), D("0.005"), D("0.05"), D("0.2")]),
    ),
    st.builds(AllocationEdit, op=st.sampled_from(["less_risk", "more_risk"]), value=st.sampled_from([None, D("0.1")])),
    st.builds(AllocationEdit, op=st.just("exclude"), bucket=st.sampled_from(BUCKETS)),
    st.builds(
        AllocationEdit,
        op=st.just("set_weight"),
        bucket=st.sampled_from(BUCKETS),
        value=st.sampled_from([D(0), D("0.123"), D("0.5"), D("0.96")]),
    ),
)


@settings(max_examples=60, deadline=None)
@given(shares=_shares, e=_edits)
def test_edits_keep_sum_and_never_go_negative(shares: list[int], e: AllocationEdit) -> None:
    raw = [D(x) for x in shares]
    total = sum(raw)
    weights = {b: (x / total * D("0.96")).quantize(D("0.0001")) for b, x in zip(BUCKETS, raw, strict=True)}
    largest = max(weights, key=lambda b: weights[b])
    weights[largest] += D("0.96") - sum(weights.values())
    weights["dfa"] = D("0.04")
    diff = apply_edit(weights, e, PROFILE, DATA, SCENARIOS)
    assert sum(diff.after.values()) == 1
    assert all(v >= 0 for v in diff.after.values())
    assert diff.after["dfa"] == D("0.04")
    if e.op == "set_weight" and e.bucket is not None and e.value is not None:
        assert diff.after[e.bucket] == e.value
