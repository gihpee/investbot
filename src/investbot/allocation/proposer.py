"""Генератор вариантов стратегической аллокации A/B/C (ARCHITECTURE §10.8).

Без прогнозов доходности: варианты строятся из риска (ковариация Ледуа–Вульфа), исторических стресс-окон,
гипотетических шоков и ограничений анкеты. Всё детерминированно.

Вселенная вариантов — бакеты из `data.buckets` без `excluded` (оптимизируются) плюс `fixed` (доля из анкеты,
не оптимизируется). Фиксированный бакет с историей в `data` участвует в риске как обычный. Без истории
(типично ЦФА, §7.5) его доходность в исторических метриках и окнах принята за 0 (риск не моделируется,
об этом пишется в notes), а в шоковых сценариях берётся заданный шок. Бакеты текущего портфеля вне
вселенной (нет истории, исключены) в варианты не попадают; их доля перераспределяется.

Общие ограничения: Σw = 1, w ≥ 0, bucket_min/max, excluded = 0, fixed точно, и для каждого сценария
доходность портфеля ≥ −max_drawdown. Вариант B стресс-ограничение не навязывает: нарушение пишется в violations.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

import numpy as np
from numpy.typing import NDArray
from scipy.optimize import Bounds, LinearConstraint, linprog, milp

from investbot.allocation.config import DEFAULT_LAMBDA, GRID_STEP
from investbot.allocation.models import (
    AllocationInfeasible,
    AllocationVariant,
    ReturnsData,
    RiskProfile,
    StressScenario,
    VariantMetrics,
    bucket_title,
)
from investbot.allocation.risk import (
    CVAR_BETA,
    WEEKS_PER_YEAR,
    FloatArray,
    IntArray,
    ScenarioTable,
    cvar_return,
    erc_weights,
    history_matrix,
    largest_remainder,
    ledoit_wolf,
    max_drawdown,
    risk_contributions,
)
from investbot.core.money import ONE, ZERO, pct

__all__ = ["AllocationInfeasible", "check_violations", "fmt_pct", "metrics", "propose"]

TITLES = {"A": "Ближе к текущему", "B": "Равный вклад риска", "C": "Под допустимую просадку"}
CASH_VOL_ANNUAL = 0.01  # бакеты тише 1% годовых считаются «кэшем» в варианте B
FINE_STEP = Decimal("0.0001")  # запасная сетка, если 0,5% несовместима с границами
_EPS = 1e-9
_STRESS_TOL = Decimal("0.000001")
_SHARE_TOL = Decimal("0.0000001")


def fmt_pct(x: Decimal | float, places: int = 1) -> str:
    """0.1234 → «12,3%», −0.2 → «−20,0%» (типографский минус)."""
    return pct(Decimal(str(x)), places).replace("-", "−")


def _dec(x: float, places: int = 6) -> Decimal:
    d = Decimal(f"{x:.{places}f}")
    return d.copy_abs() if d == 0 else d


# --- постановка ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Problem:
    opt: tuple[str, ...]
    fixed: tuple[str, ...]
    fixed_w: FloatArray
    lb: FloatArray
    ub: FloatArray
    total: float  # 1 − Σ fixed
    r_opt: FloatArray  # T×n недельные доходности оптимизируемых бакетов
    r_fix: FloatArray  # T×m фиксированных (без истории — нули)
    s_opt: FloatArray  # k×n доходности бакетов по сценариям
    s_fix: FloatArray  # k×m
    scen_names: tuple[str, ...]
    max_dd: float
    max_cvar: float | None
    growth: FloatArray  # 0/1 по оптимизируемым
    current_ref: FloatArray  # текущие доли на оптимизируемых, нормированные к total

    @property
    def n(self) -> int:
        return len(self.opt)

    @property
    def stress_rhs(self) -> FloatArray:
        """Правая часть −S·w ≤ max_dd + S_fix·f."""
        out: FloatArray = self.max_dd + self.s_fix @ self.fixed_w
        return out


def _build_problem(
    data: ReturnsData,
    current_weights: Mapping[str, Decimal],
    profile: RiskProfile,
    table: ScenarioTable,
    max_cvar: Decimal | None,
) -> tuple[_Problem, list[str]]:
    excluded = set(profile.excluded)
    fixed = tuple(b for b in profile.fixed if b not in excluded)  # исключённый фиксированный — только с долей 0
    opt = tuple(b for b in data.buckets if b not in excluded and b not in profile.fixed)
    total_dec = ONE - sum((profile.fixed[b] for b in fixed), ZERO)

    problems: list[str] = []
    for b, low in profile.bucket_min.items():
        if low > 0 and b not in opt and b not in fixed:
            why = "исключён анкетой" if b in excluded else "нет истории доходностей"
            problems.append(f"минимум {bucket_title(b)} {fmt_pct(low)} задан, но бакет {why}")
    for b in fixed:
        low, high = profile.bucket_min.get(b, ZERO), profile.bucket_max.get(b, ONE)
        if not low <= profile.fixed[b] <= high:
            problems.append(
                f"доля {bucket_title(b)} зафиксирована на {fmt_pct(profile.fixed[b])}, "
                f"а анкета требует {fmt_pct(low)}–{fmt_pct(high)}"
            )
    if not opt:
        problems.append("нет бакетов с историей доходностей, между которыми можно распределить долю")
    if problems:
        raise AllocationInfeasible("Ограничения анкеты несовместимы: " + "; ".join(problems) + ".")

    negative = {b: w for b, w in current_weights.items() if w < 0}
    if negative:
        raise ValueError(f"текущие доли не могут быть отрицательными: {negative}")

    total = float(total_dec)
    notes: list[str] = []
    cur = np.array([float(current_weights.get(b, ZERO)) for b in opt], dtype=np.float64)
    if cur.sum() > 0:
        ref = cur / cur.sum() * total
    else:
        ref = np.full(len(opt), total / max(len(opt), 1))
        notes.append("Текущий портфель пуст: за точку отсчёта варианта A взяты равные доли.")
    outside = {b: w for b, w in current_weights.items() if w > 0 and b not in opt and b not in fixed}
    if outside:
        listed = ", ".join(f"{bucket_title(b)} — сейчас {fmt_pct(w)}" for b, w in outside.items())
        notes.append(f"Не входят в варианты (исключены или нет истории доходностей): {listed}.")

    all_buckets = opt + fixed
    r_all = history_matrix(data, all_buckets)
    s_all = table.matrix(all_buckets)
    n = len(opt)
    problem = _Problem(
        opt=opt,
        fixed=fixed,
        fixed_w=np.array([float(profile.fixed[b]) for b in fixed], dtype=np.float64),
        lb=np.array([float(profile.bucket_min.get(b, ZERO)) for b in opt], dtype=np.float64),
        ub=np.array([float(min(profile.bucket_max.get(b, ONE), ONE)) for b in opt], dtype=np.float64),
        total=total,
        r_opt=r_all[:, :n],
        r_fix=r_all[:, n:],
        s_opt=s_all[:, :n],
        s_fix=s_all[:, n:],
        scen_names=tuple(table.names),
        max_dd=float(profile.max_drawdown),
        max_cvar=None if max_cvar is None else float(max_cvar),
        growth=np.array([1.0 if b in profile.growth_buckets else 0.0 for b in opt], dtype=np.float64),
        current_ref=ref,
    )
    return problem, notes


@dataclass(frozen=True)
class _Lp:
    """Ограничения над x = [w (n), extra (n_extra), α, z (T) — блок CVaR, если включён]."""

    a_ub: FloatArray
    b_ub: FloatArray
    a_eq: FloatArray
    b_eq: FloatArray
    lo: FloatArray
    hi: FloatArray

    @property
    def width(self) -> int:
        return int(self.a_eq.shape[1])

    def bounds(self) -> list[tuple[float | None, float | None]]:
        return [
            (None if np.isinf(a) else float(a), None if np.isinf(b) else float(b))
            for a, b in zip(self.lo, self.hi, strict=True)
        ]


def _constraints(p: _Problem, n_extra: int, with_stress: bool, with_cvar: bool) -> _Lp:
    n, t = p.n, p.r_opt.shape[0]
    n_cvar = 1 + t if with_cvar else 0
    width = n + n_extra + n_cvar
    rows: list[FloatArray] = []
    rhs: list[FloatArray] = []
    k = len(p.scen_names)
    if with_stress and k:
        rows.append(np.hstack([-p.s_opt, np.zeros((k, n_extra + n_cvar))]))
        rhs.append(p.stress_rhs)
    if with_cvar:
        if p.max_cvar is None:
            raise ValueError("CVaR-ограничение без лимита")
        # Рокафеллар–Урясев: z_t ≥ L_t − α, L_t = −(r_t·w + r_fix,t·f); α + Σz/((1−β)T) ≤ лимит.
        rows.append(np.hstack([-p.r_opt, np.zeros((t, n_extra)), -np.ones((t, 1)), -np.eye(t)]))
        rhs.append(p.r_fix @ p.fixed_w)
        last = np.zeros((1, width))
        last[0, n + n_extra] = 1.0
        last[0, n + n_extra + 1 :] = 1.0 / ((1 - CVAR_BETA) * t)
        rows.append(last)
        rhs.append(np.array([p.max_cvar]))
    a_ub = np.vstack(rows) if rows else np.zeros((0, width))
    b_ub = np.concatenate(rhs) if rhs else np.zeros(0)
    a_eq = np.zeros((1, width))
    a_eq[0, :n] = 1.0
    lo = np.concatenate([p.lb, np.zeros(n_extra), [-np.inf] if with_cvar else [], np.zeros(t if with_cvar else 0)])
    hi = np.concatenate([p.ub, np.full(n_extra, np.inf), np.full(n_cvar, np.inf)])
    return _Lp(a_ub, b_ub, a_eq, np.array([p.total]), lo, hi)


def _with_l1_rows(lp: _Lp, n: int, anchor: FloatArray, w_scale: float = 1.0) -> _Lp:
    """Добавляет d ≥ |w − anchor| для блока extra = d (первые n после w)."""
    width = lp.width
    up = np.zeros((n, width))
    up[:, :n] = w_scale * np.eye(n)
    up[:, n : 2 * n] = -np.eye(n)
    down = np.zeros((n, width))
    down[:, :n] = -w_scale * np.eye(n)
    down[:, n : 2 * n] = -np.eye(n)
    return _Lp(
        np.vstack([lp.a_ub, up, down]), np.concatenate([lp.b_ub, anchor, -anchor]), lp.a_eq, lp.b_eq, lp.lo, lp.hi
    )


def _linprog(cost: FloatArray, lp: _Lp) -> FloatArray | None:
    res = linprog(
        cost,
        A_ub=lp.a_ub if lp.a_ub.shape[0] else None,
        b_ub=lp.b_ub if lp.a_ub.shape[0] else None,
        A_eq=lp.a_eq,
        b_eq=lp.b_eq,
        bounds=lp.bounds(),
        method="highs",
    )
    if res.status != 0:
        return None
    return np.asarray(res.x, dtype=np.float64)


# --- диагностика несовместимости ----------------------------------------------------------------------


def _greedy_best(s: FloatArray, lb: FloatArray, ub: FloatArray, total: float) -> tuple[float, float, FloatArray]:
    """Максимум s·w на {lb ≤ w ≤ ub, Σw = total}: заливка от лучших бакетов. → (максимум, s граничного, w)."""
    w = lb.copy()
    rest = total - float(lb.sum())
    order = np.argsort(-s, kind="stable")
    marginal = float(s[order[0]])
    for i in order:
        if rest <= _EPS:
            break
        add = min(float(ub[i] - w[i]), rest)
        if add > 0:
            w[i] += add
            rest -= add
            marginal = float(s[i])
    return float(s @ w), marginal, w


def _diagnose(p: _Problem) -> str:
    suffix = " (за вычетом фиксированных долей)" if p.fixed else ""
    if p.lb.sum() > p.total + _EPS:
        return f"Сумма минимумов бакетов {fmt_pct(float(p.lb.sum()))} больше доступной доли {fmt_pct(p.total)}{suffix}."
    if p.ub.sum() < p.total - _EPS:
        return (
            f"Сумма максимумов бакетов {fmt_pct(float(p.ub.sum()))} меньше доступной доли {fmt_pct(p.total)}{suffix}: "
            "ослабьте потолки или исключения."
        )
    limit = fmt_pct(-p.max_dd)
    for j, name in enumerate(p.scen_names):
        fixed_part = float(p.s_fix[j] @ p.fixed_w)
        best, marginal, w = _greedy_best(p.s_opt[j], p.lb, p.ub, p.total)
        if best + fixed_part >= -p.max_dd - _EPS:
            continue
        mins = [
            f"{bucket_title(b)} ≥ {fmt_pct(float(p.lb[i]))}"
            for i, b in enumerate(p.opt)
            if p.lb[i] > 0 and p.s_opt[j, i] < marginal - _EPS
        ]
        caps = [
            f"{bucket_title(b)} ≤ {fmt_pct(float(p.ub[i]))}"
            for i, b in enumerate(p.opt)
            if p.ub[i] < p.total and w[i] >= p.ub[i] - _EPS and p.s_opt[j, i] > marginal + _EPS
        ]
        reasons = []
        if mins:
            reasons.append("минимумы " + ", ".join(mins))
        if caps:
            reasons.append("потолки " + ", ".join(caps))
        hurt = [bucket_title(b) for i, b in enumerate(p.fixed) if p.s_fix[j, i] < 0 and p.fixed_w[i] > 0]
        if hurt:
            reasons.append("фиксированные доли " + ", ".join(hurt))
        if not reasons:
            reasons.append("даже лучший доступный бакет теряет больше допуска")
        return (
            f"Допустимая просадка недостижима при заданных минимумах бакетов: в сценарии «{name}» "
            f"лучший достижимый результат {fmt_pct(best + fixed_part)} при допуске {limit}. "
            f"Мешают: {'; '.join(reasons)}."
        )
    names = ", ".join(f"«{n}»" for n in p.scen_names)
    return (
        "Допустимая просадка недостижима при заданных минимумах бакетов одновременно во всех стресс-сценариях "
        f"({names}): по отдельности каждый выполним, вместе — нет."
    )


# --- варианты -----------------------------------------------------------------------------------------


def _solve_a(p: _Problem) -> FloatArray:
    """A: min Σ|w − w_тек| при всех ограничениях (LP, d — вспомогательные)."""
    lp = _with_l1_rows(_constraints(p, p.n, with_stress=True, with_cvar=False), p.n, p.current_ref)
    cost = np.concatenate([np.zeros(p.n), np.ones(p.n)])
    x = _linprog(cost, lp)
    if x is None:
        raise AllocationInfeasible(_diagnose(p))
    return x[: p.n]


def _covariance(p: _Problem, data: ReturnsData) -> tuple[FloatArray, float]:
    """LW-ковариация по бакетам с историей; строки и столбцы бакетов без истории — нули."""
    all_buckets = p.opt + p.fixed
    idx = [i for i, b in enumerate(all_buckets) if b in data.buckets]
    cov = np.zeros((len(all_buckets), len(all_buckets)))
    shrunk, delta = ledoit_wolf(history_matrix(data, [all_buckets[i] for i in idx]))
    cov[np.ix_(idx, idx)] = shrunk
    return cov, delta


@dataclass(frozen=True)
class _ErcResult:
    w: FloatArray
    converged: bool
    equalized: NDArray[np.bool_]  # по оптимизируемым: участвует ли бакет в выравнивании вкладов
    cash: NDArray[np.bool_]  # «кэш» (σ < CASH_VOL_ANNUAL): доля по минимуму анкеты
    cash_extra: float  # сколько сверх минимумов досталось «кэшу» из-за потолков остальных


def _solve_b(p: _Problem, cov: FloatArray) -> _ErcResult:
    """B: равный вклад риска (SLSQP) с границами анкеты, без стресс-ограничения.

    Почти безрисковые бакеты (σ < CASH_VOL_ANNUAL, денежный рынок) не выравниваются: иначе ERC отдаёт им
    почти весь портфель. Они получают минимум анкеты, а если потолки остальных не вмещают долю, —
    остаток пропорционально запасу до максимума. Бакеты с потолком 0 в выравнивании не участвуют.
    """
    n = p.n
    vol = np.sqrt(np.var(p.r_opt, axis=0, ddof=1) * WEEKS_PER_YEAR)
    active = p.ub > _EPS
    cash = active & (vol < CASH_VOL_ANNUAL)
    if not (active & ~cash).any():
        cash = np.zeros(n, dtype=np.bool_)
    risky = active & ~cash
    lb_c, ub_c = p.lb[cash], p.ub[cash]
    total_r = max(min(p.total - float(lb_c.sum()), float(p.ub[risky].sum())), float(p.lb[risky].sum()))
    extra = max(p.total - total_r - float(lb_c.sum()), 0.0)
    room = ub_c - lb_c
    w = np.zeros(n)
    w[cash] = lb_c + (extra * room / room.sum() if extra > _EPS and room.sum() > 0 else 0.0)
    order = np.concatenate([np.flatnonzero(risky), np.flatnonzero(~risky), np.arange(n, n + len(p.fixed))])
    w[risky], ok = erc_weights(
        cov[np.ix_(order, order)], p.lb[risky], p.ub[risky], total_r, np.concatenate([w[~risky], p.fixed_w])
    )
    return _ErcResult(w, ok, risky, cash, extra)


def _solve_c(p: _Problem, w_b: FloatArray, lam: float) -> FloatArray:
    """C: max Σw_growth − λ·Σ|w − w_B| при всех ограничениях (+ CVaR95, если задан)."""
    with_cvar = p.max_cvar is not None
    lp = _with_l1_rows(_constraints(p, p.n, with_stress=True, with_cvar=with_cvar), p.n, w_b)
    cost = np.concatenate([-p.growth, np.full(p.n, lam), np.zeros(lp.width - 2 * p.n)])
    x = _linprog(cost, lp)
    if x is None:
        if with_cvar and p.max_cvar is not None:
            raise AllocationInfeasible(
                f"Недельный CVaR95 не хуже {fmt_pct(-p.max_cvar)} недостижим вместе с ограничениями анкеты: "
                "ослабьте лимит CVaR или минимумы рискованных бакетов."
            )
        raise AllocationInfeasible(_diagnose(p))
    return x[: p.n]


# --- округление ---------------------------------------------------------------------------------------


def _feasible(p: _Problem, w: FloatArray, with_stress: bool, with_cvar: bool, tol: float = 1e-9) -> bool:
    if with_stress and p.scen_names and np.any(-p.s_opt @ w > p.stress_rhs + tol):
        return False
    if with_cvar and p.max_cvar is not None:
        port = p.r_opt @ w + p.r_fix @ p.fixed_w
        if -cvar_return(port) > p.max_cvar + tol:
            return False
    return True


def _on_grid(k: IntArray, step: Decimal | float) -> FloatArray:
    return np.asarray(k, dtype=np.float64) * float(step)


def _grid(p: _Problem, step: float) -> tuple[IntArray, IntArray, int] | None:
    lo = np.ceil(p.lb / step - 1e-9).astype(np.int64)
    hi = np.floor(p.ub / step + 1e-9).astype(np.int64)
    units = int(np.floor(p.total / step + 1e-9))
    if np.any(lo > hi) or int(lo.sum()) > units or int(hi.sum()) < units:
        return None
    return lo, hi, units


def _milp_round(
    p: _Problem, w: FloatArray, grid: tuple[IntArray, IntArray, int], step: float, with_stress: bool, with_cvar: bool
) -> IntArray | None:
    """Ближайшая (L1) точка сетки, удовлетворяющая ограничениям: w = step·k, k целые."""
    lo_u, hi_u, units = grid
    n = p.n
    lp = _with_l1_rows(_constraints(p, n, with_stress, with_cvar), n, w, w_scale=step)
    a_ub = lp.a_ub.copy()
    a_ub[: a_ub.shape[0] - 2 * n, :n] *= step  # стресс и CVaR — по w = step·k; строки L1 уже масштабированы
    a_eq = lp.a_eq.copy()
    lo = lp.lo.copy()
    hi = lp.hi.copy()
    lo[:n], hi[:n] = lo_u, hi_u
    cost = np.concatenate([np.zeros(n), np.ones(n), np.zeros(lp.width - 2 * n)])
    integrality = np.concatenate([np.ones(n), np.zeros(lp.width - n)])
    res = milp(
        cost,
        integrality=integrality,
        bounds=Bounds(lo, hi),
        constraints=[LinearConstraint(a_ub, -np.inf, lp.b_ub), LinearConstraint(a_eq, units, units)],
        options={"time_limit": 30},
    )
    if res.x is None:
        return None
    k: IntArray = np.round(np.asarray(res.x[:n], dtype=np.float64)).astype(np.int64)
    return k if _feasible(p, _on_grid(k, step), with_stress, with_cvar) else None


@dataclass(frozen=True)
class _Rounded:
    k: IntArray  # доли оптимизируемых бакетов в шагах сетки
    step: Decimal
    residue_at: int | None  # куда добавить остаток 1 − Σfixed − Σk·step (если фиксированные доли не на сетке)
    note: str | None


def _round(p: _Problem, w: FloatArray, step: Decimal, with_stress: bool, with_cvar: bool) -> _Rounded:
    """Наибольшие остатки на сетке `step`; если это ломает стресс/CVaR — MILP к ближайшей допустимой точке."""

    def place(k: IntArray, st: Decimal) -> tuple[FloatArray, int | None]:
        base = _on_grid(k, st)
        residue = p.total - float(base.sum())
        if abs(residue) < 1e-12:
            return base, None
        room = [i for i in np.argsort(-base, kind="stable") if 0 <= base[i] + residue <= p.ub[i] + 1e-12]
        for i in room:
            cand = base.copy()
            cand[i] += residue
            if _feasible(p, cand, with_stress, with_cvar):
                return cand, int(i)
        i = int(room[0]) if room else int(np.argmax(base))
        cand = base.copy()
        cand[i] += residue
        return cand, i

    for st in (step, FINE_STEP):
        grid = _grid(p, float(st))
        if grid is None:
            continue
        k = largest_remainder(w, grid[0], grid[1], grid[2], float(st))
        wk, at = place(k, st)
        if not _feasible(p, wk, with_stress, with_cvar):
            fixed_k = _milp_round(p, w, grid, float(st), with_stress, with_cvar)
            if fixed_k is not None:
                k = fixed_k
                wk, at = place(k, st)
        if _feasible(p, wk, with_stress, with_cvar):
            note = None
            if st != step:
                note = f"Доли округлены с шагом {fmt_pct(st, 2)}: сетка {fmt_pct(step)} несовместима с ограничениями."
            return _Rounded(k, st, at, note)
    grid = _grid(p, float(FINE_STEP))
    if grid is None:
        raise AllocationInfeasible("Границы бакетов несовместимы даже с шагом 0,01%.")
    k = largest_remainder(w, grid[0], grid[1], grid[2], float(FINE_STEP))
    return _Rounded(k, FINE_STEP, place(k, FINE_STEP)[1], "Округление долей нарушает ограничения на сотые доли п.п.")


def _to_weights(p: _Problem, profile: RiskProfile, r: _Rounded) -> dict[str, Decimal]:
    out = {b: Decimal(int(r.k[i])) * r.step for i, b in enumerate(p.opt)}
    out.update({b: profile.fixed[b] for b in p.fixed})
    if r.residue_at is not None:
        out[p.opt[r.residue_at]] += ONE - sum(out.values(), ZERO)
    return out


# --- метрики и проверки -------------------------------------------------------------------------------


def _turnover(weights: Mapping[str, Decimal], current: Mapping[str, Decimal]) -> Decimal:
    total = sum(current.values(), ZERO)
    if total <= 0:
        return ZERO
    keys = set(weights) | set(current)
    diff = sum((abs(weights.get(b, ZERO) - current.get(b, ZERO) / total) for b in keys), ZERO)
    return (diff / 2).quantize(Decimal("0.000001"))


def metrics(
    weights: Mapping[str, Decimal],
    data: ReturnsData,
    scenarios: Sequence[StressScenario],
    current_weights: Mapping[str, Decimal],
    bucket_yields: Mapping[str, Decimal] | None = None,
    *,
    table: ScenarioTable | None = None,
) -> VariantMetrics:
    """Метрики портфеля с постоянными долями (еженедельная ребалансировка) на истории, не прогноз.

    Бакет без истории в `data` даёт 0 в исторических рядах; сценарий, не покрытый данными, пропускается.
    current_yield: бакеты без переданной доходности дают 0.
    """
    buckets = list(weights)
    w = np.array([float(weights[b]) for b in buckets], dtype=np.float64)
    port = history_matrix(data, buckets) @ w
    tbl = table if table is not None else ScenarioTable(data, scenarios)
    stress_vals = tbl.matrix(buckets) @ w
    current_yield = None
    if bucket_yields is not None:
        current_yield = sum((weights[b] * bucket_yields.get(b, ZERO) for b in buckets), ZERO).quantize(
            Decimal("0.000001")
        )
    return VariantMetrics(
        vol_annual=_dec(float(np.std(port, ddof=1)) * float(np.sqrt(WEEKS_PER_YEAR))),
        max_drawdown_hist=_dec(max_drawdown(port)),
        cvar95_weekly=_dec(cvar_return(port)),
        stress={name: _dec(float(v)) for name, v in zip(tbl.names, stress_vals, strict=True)},
        current_yield=current_yield,
        turnover_from_current=_turnover(weights, current_weights),
    )


def _pair(a: Decimal, b: Decimal) -> tuple[str, str]:
    """Форматирует два числа так, чтобы они различались в тексте (1 или 2 знака)."""
    sa, sb = fmt_pct(a), fmt_pct(b)
    return (sa, sb) if sa != sb else (fmt_pct(a, 2), fmt_pct(b, 2))


def check_violations(
    weights: Mapping[str, Decimal], profile: RiskProfile, stress: Mapping[str, Decimal]
) -> tuple[str, ...]:
    """Нарушения анкеты человеческим языком: сумма, исключения, фиксированные доли, границы, стресс."""
    out: list[str] = []
    total = sum(weights.values(), ZERO)
    if abs(total - ONE) > _SHARE_TOL:
        out.append(f"Сумма долей {fmt_pct(total, 2)} вместо 100%.")
    for b in dict.fromkeys([*weights, *profile.bucket_min, *profile.fixed]):
        w = weights.get(b, ZERO)
        title = bucket_title(b)
        if b in profile.excluded and w > _SHARE_TOL:
            out.append(f"{title}: класс исключён анкетой, но доля {fmt_pct(w)}.")
        if b in profile.fixed and abs(w - profile.fixed[b]) > _SHARE_TOL:
            out.append(f"{title}: доля {fmt_pct(w)} отличается от заданной в анкете {fmt_pct(profile.fixed[b])}.")
        low = profile.bucket_min.get(b)
        if low is not None and w < low - _SHARE_TOL:
            sw, sl = _pair(w, low)
            out.append(f"{title}: {sw} ниже минимума анкеты {sl}.")
        high = profile.bucket_max.get(b)
        if high is not None and w > high + _SHARE_TOL:
            sw, sh = _pair(w, high)
            out.append(f"{title}: {sw} выше максимума анкеты {sh}.")
    limit = -profile.max_drawdown
    for name, r in stress.items():
        if r < limit - _STRESS_TOL:
            sr, sl = _pair(r, limit)
            out.append(f"Стресс «{name}»: {sr} хуже допустимой просадки {sl}.")
    return tuple(out)


# --- точка входа --------------------------------------------------------------------------------------


def propose(
    data: ReturnsData,
    current_weights: Mapping[str, Decimal],
    profile: RiskProfile,
    scenarios: Sequence[StressScenario],
    bucket_yields: Mapping[str, Decimal] | None = None,
    *,
    max_cvar95_weekly: Decimal | None = None,
    diversification_lambda: Decimal = DEFAULT_LAMBDA,
    grid_step: Decimal = GRID_STEP,
) -> list[AllocationVariant]:
    """Три варианта A/B/C. Бросает AllocationInfeasible, если ограничения анкеты несовместимы.

    `max_cvar95_weekly` — положительная доля убытка (0.05 = средняя доходность худших 5% недель не хуже −5%),
    применяется только к варианту C.
    """
    if max_cvar95_weekly is not None and not ZERO < max_cvar95_weekly <= ONE:
        raise ValueError("max_cvar95_weekly — положительная доля убытка в (0, 1]")
    table = ScenarioTable(data, scenarios)
    p, common = _build_problem(data, current_weights, profile, table, max_cvar95_weekly)
    w_a = _solve_a(p)
    cov, delta = _covariance(p, data)
    erc = _solve_b(p, cov)
    w_b = erc.w
    w_c = _solve_c(p, w_b, float(diversification_lambda))

    common = common + [f"Сценарий «{name}» пропущен: данные не покрывают окно." for name in table.skipped]
    no_history = [b for b in p.fixed if b not in data.buckets]
    if no_history:
        listed = ", ".join(f"{bucket_title(b)} {fmt_pct(profile.fixed[b])}" for b in no_history)
        common.append(
            f"{listed}: доля задана анкетой; истории цен нет, в исторических метриках и окнах их доходность "
            "принята за 0, в шоковых сценариях — по заданному шоку."
        )
    common.append("Волатильность, просадка, CVaR и стресс посчитаны на истории прокси-рядов — это не прогноз.")

    rc = risk_contributions(np.concatenate([w_b, p.fixed_w]), cov)[: p.n][erc.equalized]
    rc_share = rc / rc.sum() if rc.sum() > 0 else rc
    shrink = f"{delta:.2f}".replace(".", ",")
    specific: dict[str, list[str]] = {
        "A": [],
        "B": [f"Ковариация Ледуа–Вульфа по недельным доходностям, сжатие к диагонали {shrink}."],
        "C": [],
    }
    if erc.cash.any():
        cash_names = ", ".join(bucket_title(b) for i, b in enumerate(p.opt) if erc.cash[i])
        tail = ", плюс остаток, который не вместили потолки остальных" if erc.cash_extra > _EPS else ""
        specific["B"].append(
            f"{cash_names}: почти без колебаний, в выравнивание вкладов риска не входит — "
            f"доля по минимуму анкеты{tail}."
        )
    if len(rc_share) > 1 and float(rc_share.max() - rc_share.min()) > 0.02:
        specific["B"].append(
            "Границы анкеты не дают полностью выровнять вклады риска: "
            f"от {fmt_pct(float(rc_share.min()))} до {fmt_pct(float(rc_share.max()))} на бакет."
        )
    if not erc.converged:
        specific["B"].append("Оптимизатор ERC не сошёлся полностью: вклады риска выровнены приближённо.")
    if p.max_cvar is not None:
        specific["C"].append(f"Недельный CVaR95 ограничен уровнем {fmt_pct(-p.max_cvar)}.")
    if not p.growth.any():
        specific["C"].append("Во вселенной нет растущих бакетов: C — ближайший к B вариант в рамках ограничений.")

    variants: list[AllocationVariant] = []
    plan: tuple[tuple[Literal["A", "B", "C"], FloatArray, bool, bool], ...] = (
        ("A", w_a, True, False),
        ("B", w_b, False, False),
        ("C", w_c, True, p.max_cvar is not None),
    )
    for code, w_raw, with_stress, with_cvar in plan:
        rounded = _round(p, w_raw, grid_step, with_stress, with_cvar)
        weights = _to_weights(p, profile, rounded)
        m = metrics(weights, data, scenarios, current_weights, bucket_yields, table=table)
        notes = list(specific[code])
        if rounded.note:
            notes.append(rounded.note)
        if code == "A":
            if float(np.abs(w_a - p.current_ref).sum()) < 1e-7:
                notes.insert(0, "Текущие доли уже в рамках анкеты: отличие только от округления.")
            notes.insert(0, f"Оборот перехода от текущего портфеля: {fmt_pct(m.turnover_from_current)}.")
        if code == "C":
            growth_c = sum((weights[b] for b in p.opt if b in profile.growth_buckets), ZERO)
            growth_b = sum((variants[1].weights[b] for b in p.opt if b in profile.growth_buckets), ZERO)
            notes.insert(0, f"Доля растущих бакетов: {fmt_pct(growth_c)} (в варианте B — {fmt_pct(growth_b)}).")
            binding = [n for n, r in m.stress.items() if r <= -profile.max_drawdown + Decimal("0.0025")]
            if binding:
                listed = ", ".join(f"«{n}»" for n in binding)
                notes.append(f"Упирается в допустимую просадку в сценариях: {listed}.")
        if profile.needs_income:
            if m.current_yield is not None:
                cy = fmt_pct(m.current_yield)
                notes.append(f"Текущий денежный поток: {cy} годовых по текущим доходностям, не прогноз.")
            else:
                notes.append("Нужен денежный поток, но текущие доходности бакетов не переданы.")
        variants.append(
            AllocationVariant(
                code=code,
                title=TITLES[code],
                weights=weights,
                metrics=m,
                violations=check_violations(weights, profile, m.stress),
                notes=tuple(notes + common),
            )
        )
    return variants
