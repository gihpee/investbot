"""Численные примитивы конструктора аллокации: ковариация, риск-вклады, просадка, CVaR, стресс-окна.

Внутри — numpy/float; Decimal только на границах API (proposer.py, edits.py).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, timedelta

import numpy as np
from numpy.typing import NDArray
from scipy.optimize import minimize

from investbot.allocation.models import ReturnsData, StressScenario

FloatArray = NDArray[np.float64]
IntArray = NDArray[np.int64]

WEEKS_PER_YEAR = 52
CVAR_BETA = 0.95
_WEEK = timedelta(days=7)


def ledoit_wolf(returns: FloatArray) -> tuple[FloatArray, float]:
    """Ledoit–Wolf (2004): сжатие выборочной ковариации к μ·I, μ = tr(S)/p.

    Возвращает (Σ*, δ), где Σ* = δ·μ·I + (1 − δ)·S, δ ∈ [0, 1]. S — с делением на T (как в статье).
    """
    x = returns - returns.mean(axis=0)
    t, p = x.shape
    s = x.T @ x / t
    mu = float(np.trace(s)) / p
    d2 = float(np.sum((s - mu * np.eye(p)) ** 2)) / p
    if d2 <= 0:
        return s, 0.0
    b2_bar = (float(np.sum(np.sum(x**2, axis=1) ** 2)) - t * float(np.sum(s**2))) / (t * t * p)
    b2 = min(max(b2_bar, 0.0), d2)
    delta = b2 / d2
    return delta * mu * np.eye(p) + (1 - delta) * s, delta


def risk_contributions(w: FloatArray, cov: FloatArray) -> FloatArray:
    """Доли вклада в дисперсию: w_i·(Σw)_i / wᵀΣw (сумма = 1)."""
    rc = w * (cov @ w)
    total = float(rc.sum())
    return rc / total if total > 0 else np.zeros_like(w)


def project_box_sum(x: FloatArray, lb: FloatArray, ub: FloatArray, total: float) -> FloatArray:
    """Евклидова проекция на {lb ≤ w ≤ ub, Σw = total} (бисекция по сдвигу τ)."""
    lo, hi = float(np.min(lb - x)) - 1.0, float(np.max(ub - x)) + 1.0
    for _ in range(200):
        tau = (lo + hi) / 2
        if float(np.clip(x + tau, lb, ub).sum()) < total:
            lo = tau
        else:
            hi = tau
    out: FloatArray = np.clip(x + (lo + hi) / 2, lb, ub)
    return out


def erc_weights(
    cov: FloatArray, lb: FloatArray, ub: FloatArray, total: float, fixed: FloatArray | None = None
) -> tuple[FloatArray, bool]:
    """Равный вклад риска (ERC) с границами долей: SLSQP по Σ(p_i − 1/n)², p_i — доли вкладов.

    `cov` — ковариация (n + m)×(n + m): первые n бакетов оптимизируются, последние m — фиксированные доли
    `fixed` (их риск учитывается во взаимных членах, но их вклады не выравниваются).
    Возвращает (веса n, сошёлся ли оптимизатор). При активных границах точное равенство недостижимо.
    """
    n = len(lb)
    fx = np.zeros(0) if fixed is None else fixed
    if n == 1:
        return np.array([total]), True
    inv_vol = 1.0 / np.sqrt(np.maximum(np.diag(cov)[:n], 1e-18))
    x0 = project_box_sum(inv_vol / inv_vol.sum() * total, lb, ub, total)

    def parts(w: FloatArray) -> tuple[FloatArray, FloatArray, float]:
        full = np.concatenate([w, fx])
        sw = cov @ full
        rc = w * sw[:n]
        # ∂rc_i/∂w_k = δ_ik·(Σw)_i + w_i·Σ_ik
        jac = np.diag(sw[:n]) + w[:, None] * cov[:n, :n]
        return rc, jac, float(rc.sum())

    def objective(w: FloatArray) -> tuple[float, FloatArray]:
        rc, jac, s = parts(w)
        if s <= 0:
            return 0.0, np.zeros(n)
        p = rc / s
        dev = p - 1.0 / n
        ds = jac.sum(axis=0)
        dp = (jac - p[:, None] * ds[None, :]) / s
        scale = 1e4  # SLSQP чувствителен к масштабу целевой функции
        return scale * float(dev @ dev), scale * 2 * (dev @ dp)

    res = minimize(
        objective,
        x0,
        jac=True,
        method="SLSQP",
        bounds=list(zip(lb.tolist(), ub.tolist(), strict=True)),
        constraints=[{"type": "eq", "fun": lambda w: float(w.sum()) - total, "jac": lambda w: np.ones(n)}],
        options={"ftol": 1e-12, "maxiter": 1000},
    )
    w = project_box_sum(np.asarray(res.x, dtype=np.float64), lb, ub, total)
    # status 8 (линейный поиск не улучшает) — штатный финал на активных границах
    return w, bool(res.success) or int(res.status) == 8


def max_drawdown(port_returns: FloatArray) -> float:
    """Максимальная просадка ряда доходностей, ≤ 0 (−0.35 = падение на 35% от пика)."""
    wealth = np.concatenate([[1.0], np.cumprod(1.0 + port_returns)])
    peak = np.maximum.accumulate(wealth)
    return min(float(np.min(wealth / peak - 1.0)), 0.0)


def cvar_return(port_returns: FloatArray, beta: float = CVAR_BETA) -> float:
    """CVaR_β в знаке доходности: средняя доходность худших (1 − β) наблюдений (дробный хвост — с весом).

    Совпадает с минимумом функции Рокафеллара–Урясева на эмпирическом распределении, как в LP варианта C.
    """
    losses = np.sort(-port_returns)[::-1]
    k = (1 - beta) * len(losses)
    whole = int(np.floor(k + 1e-12))
    tail = float(losses[:whole].sum())
    if whole < len(losses) and k - whole > 1e-12:
        tail += (k - whole) * float(losses[whole])
    return -tail / k


def _window(scenario: StressScenario) -> tuple[date, date]:
    if scenario.start is None or scenario.end is None:
        raise ValueError(f"{scenario.name}: у сценария нет окна дат")
    return scenario.start, scenario.end


def window_covered(data: ReturnsData, scenario: StressScenario) -> bool:
    """Окно покрыто данными с точностью до недели с обеих сторон."""
    start, end = _window(scenario)
    return data.dates[0] <= start + _WEEK and data.dates[-1] >= end - _WEEK


def window_returns(data: ReturnsData, scenario: StressScenario) -> FloatArray:
    """Накопленная доходность каждого бакета за недели, закончившиеся в [start, end]."""
    start, end = _window(scenario)
    mask = np.array([start <= d <= end for d in data.dates])
    if not mask.any():
        return np.zeros(len(data.buckets))
    out: FloatArray = np.prod(1.0 + data.returns[mask], axis=0) - 1.0
    return out


class ScenarioTable:
    """Доходности бакетов по сценариям, покрытым данными; непокрытые — в `skipped`."""

    def __init__(self, data: ReturnsData, scenarios: Sequence[StressScenario]) -> None:
        names = [s.name for s in scenarios]
        if len(set(names)) != len(names):
            raise ValueError("имена стресс-сценариев должны быть уникальны")
        self.names: list[str] = []
        self.skipped: list[str] = []
        self._rows: list[dict[str, float]] = []
        for sc in scenarios:
            if sc.kind == "historical":
                if not window_covered(data, sc):
                    self.skipped.append(sc.name)
                    continue
                cum = window_returns(data, sc)
                row = {b: float(cum[i]) for i, b in enumerate(data.buckets)}
            else:
                row = {b: float(v) for b, v in sc.shocks.items()}
            self.names.append(sc.name)
            self._rows.append(row)

    def matrix(self, buckets: Sequence[str]) -> FloatArray:
        """k×n: доходность бакета в сценарии; бакет без истории или без шока — 0."""
        return np.array([[row.get(b, 0.0) for b in buckets] for row in self._rows], dtype=np.float64).reshape(
            len(self._rows), len(buckets)
        )


def history_matrix(data: ReturnsData, buckets: Sequence[str]) -> FloatArray:
    """T×n доходностей для бакетов; бакет без истории (например, ЦФА) — нули."""
    cols = [data.column(b) if b in data.buckets else np.zeros(len(data.dates)) for b in buckets]
    return np.column_stack(cols) if cols else np.zeros((len(data.dates), 0))


def largest_remainder(w: FloatArray, lo_units: IntArray, hi_units: IntArray, units: int, step: float) -> IntArray:
    """Округление долей до сетки `step` методом наибольших остатков: Σ = units, lo ≤ k ≤ hi.

    Требует Σlo ≤ units ≤ Σhi. Ничьи — в пользу меньшего индекса (детерминированно).
    """
    raw = w / step
    k = np.clip(np.floor(raw + 1e-9).astype(np.int64), lo_units, hi_units)
    rem = raw - k
    diff = units - int(k.sum())
    while diff > 0:
        room = np.where(k < hi_units, rem, -np.inf)
        i = int(np.argmax(room))
        if not np.isfinite(room[i]):
            raise ValueError("сетка: сумма максимумов меньше целевой суммы")
        k[i] += 1
        rem[i] -= 1
        diff -= 1
    while diff < 0:
        room = np.where(k > lo_units, rem, np.inf)
        i = int(np.argmin(room))
        if not np.isfinite(room[i]):
            raise ValueError("сетка: сумма минимумов больше целевой суммы")
        k[i] -= 1
        rem[i] += 1
        diff += 1
    return k
