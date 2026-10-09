"""Сервис конструктора аллокации (§10.8): анкета → варианты A/B/C → правки → утверждение IPS.

Предлагает бот, утверждает только пользователь: `approve` вызывается лишь по кнопке пользователя.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from pathlib import Path

import numpy as np
import structlog

from investbot.allocation.config import AllocationConfig, load_allocation_config
from investbot.allocation.edits import apply_edit
from investbot.allocation.models import (
    AllocationEdit,
    AllocationVariant,
    DraftDiff,
    ReturnsData,
    RiskProfile,
    VariantMetrics,
)
from investbot.allocation.proposer import propose
from investbot.core.money import pct
from investbot.journal.store import Journal
from investbot.rebalance.ips import IPS
from investbot.rebalance.ips_builder import build_ips, save_ips
from investbot.services.portfolio import PortfolioService
from investbot.services.proxies import DEFAULT_PROXIES, ProxyStore, synthetic_rate_series, weekly_returns
from investbot.services.questionnaire import ProfileAnswers, load_answers, save_answers

log = structlog.get_logger(__name__)

HISTORY_START = date(2014, 1, 1)  # чтобы покрыть рублёвый кризис декабря 2014
MONEY_MARKET_RATE = 0.14  # ключевая ставка на 09.10.2026 (docs/RESEARCH.md §8.1); упрощение до истории КС
HY_PROXY = "RUCBHYTR"  # индекс ВДО MOEX [проверить доступность ряда в ISS в Ф0]
HY_MAX = Decimal("0.05")
# Консервативный потолок акций для коротких горизонтов — параметр по умолчанию, пользователь может поменять правкой.
HORIZON_EQUITY_CAP = {1: Decimal("0.20"), 3: Decimal("0.50")}
NO_PROXY_BUCKETS = ("ofz_float_linker", "fx_cash")
# Потолок на один класс по умолчанию, чтобы варианты не сводились к одному бакету (денежный рынок — без потолка).
DEFAULT_BUCKET_MAX = Decimal("0.60")
CAPPED_BY_DEFAULT = ("equity_ru", "ofz_fixed", "corp_ig")  # в MVP нет прокси-рядов — в вариантах не участвуют


class NotEnoughHistory(RuntimeError):
    pass


def drawdown_warning(m: VariantMetrics, profile: RiskProfile) -> str | None:
    """Ограничение анкеты проверяется по стресс-сценариям; если на всей истории просадка глубже — говорим прямо."""
    if m.max_drawdown_hist < -profile.max_drawdown:
        return (f"На всей истории худшая просадка {pct(m.max_drawdown_hist)} глубже допустимой "
                f"−{pct(profile.max_drawdown)}: ограничение анкеты проверяется по стресс-сценариям, "
                "а не по каждой неделе истории.")
    return None


def to_risk_profile(a: ProfileAnswers, cfg: AllocationConfig) -> RiskProfile:
    excluded = list(NO_PROXY_BUCKETS)
    fixed: dict[str, Decimal] = {}
    bucket_max: dict[str, Decimal] = {}
    for bucket, share in (("dfa", a.dfa_share), ("gold", a.gold_share)):
        if share > 0:
            fixed[bucket] = share
        else:
            excluded.append(bucket)
    if a.allow_hy:
        bucket_max["corp_hy"] = HY_MAX
    else:
        excluded.append("corp_hy")
    for bucket in CAPPED_BY_DEFAULT:
        bucket_max[bucket] = DEFAULT_BUCKET_MAX
    cap = HORIZON_EQUITY_CAP.get(a.horizon_years)
    if cap is not None:
        bucket_max["equity_ru"] = min(cap, DEFAULT_BUCKET_MAX)
    return RiskProfile(
        horizon_years=a.horizon_years, max_drawdown=a.max_drawdown, needs_income=a.needs_income,
        bucket_max=bucket_max, excluded=tuple(excluded), fixed=fixed,
        growth_buckets=tuple(b for b in cfg.growth_buckets if b not in excluded),
    )


class AllocationService:
    def __init__(self, portfolio: PortfolioService, proxies: ProxyStore, config_path: Path, answers_path: Path,
                 ips_path: Path, journal: Journal) -> None:
        self._portfolio = portfolio
        self._proxies = proxies
        self._config_path = config_path
        self._answers_path = answers_path
        self._ips_path = ips_path
        self._journal = journal

    @property
    def config(self) -> AllocationConfig:
        return load_allocation_config(self._config_path)

    def answers(self) -> ProfileAnswers | None:
        return load_answers(self._answers_path)

    def save_answers(self, answers: ProfileAnswers) -> None:
        save_answers(answers, self._answers_path)
        self._journal.append("profile_answers", answers.model_dump(), author="user")

    def profile(self) -> RiskProfile:
        answers = self.answers()
        if answers is None:
            raise LookupError("анкета не заполнена")
        return to_risk_profile(answers, self.config)

    async def returns_data(self, profile: RiskProfile, today: date) -> ReturnsData:
        proxies = {b: s for b, s in DEFAULT_PROXIES.items() if b not in profile.excluded}
        if "corp_hy" not in profile.excluded:
            proxies["corp_hy"] = HY_PROXY
        series = await self._proxies.bucket_series(proxies, HISTORY_START, today)
        missing = [b for b, rows in series.items() if not rows]
        if missing:
            raise NotEnoughHistory(f"нет истории индексов для: {', '.join(missing)} — проверьте доступ к MOEX ISS")
        dates, buckets, rets = weekly_returns(series)
        if len(dates) < 52:
            raise NotEnoughHistory(f"мало общей истории индексов: {len(dates)} недель")
        if "money_market" not in profile.excluded:
            buckets.append("money_market")
            rets = np.column_stack([rets, synthetic_rate_series(dates, MONEY_MARKET_RATE)])
        return ReturnsData(buckets=tuple(buckets), dates=tuple(dates), returns=rets)

    async def current_weights(self) -> dict[str, Decimal]:
        try:
            ov = await self._portfolio.overview()
        except Exception as e:
            log.warning("allocation.no_portfolio", error=type(e).__name__)
            return {}
        return {b: w for b, w in ov.valuation.bucket_weights.items() if b != "unclassified"}

    async def propose(self, today: date) -> list[AllocationVariant]:
        profile = self.profile()
        cfg = self.config
        data = await self.returns_data(profile, today)
        current = await self.current_weights()
        variants = propose(
            data, current, profile, cfg.scenarios,
            max_cvar95_weekly=cfg.max_cvar95_weekly,
            diversification_lambda=cfg.diversification_lambda,
            grid_step=cfg.grid_step,
        )
        variants = [
            v.model_copy(update={"violations": (*v.violations, w)}) if (w := drawdown_warning(v.metrics, profile))
            else v
            for v in variants
        ]
        self._journal.append("ips_proposal", {
            "as_of": today.isoformat(), "config_version": cfg.version, "current": current,
            "variants": {v.code: v.weights for v in variants},
        })
        return variants

    async def edit(self, weights: Mapping[str, Decimal], edit: AllocationEdit, today: date) -> DraftDiff:
        profile = self.profile()
        cfg = self.config
        data = await self.returns_data(profile, today)
        diff = apply_edit(dict(weights), edit, profile, data, cfg.scenarios)
        if w := drawdown_warning(diff.metrics_after, profile):
            diff = diff.model_copy(update={"violations": (*diff.violations, w)})
        self._journal.append("ips_edit", {"edit": edit.model_dump(), "after": diff.after,
                                          "violations": list(diff.violations)}, author="user")
        return diff

    def approve(self, weights: Mapping[str, Decimal], source_variant: str | None) -> IPS:
        """Вызывается только по кнопке пользователя «Утвердить»."""
        previous = self._portfolio.load_ips()
        ips = build_ips({b: w for b, w in weights.items() if w > 0 or b in ("corp_hy", "fx_cash")}, previous)
        save_ips(ips, self._ips_path)
        self._journal.append("ips_version", {"version": ips.version, "source_variant": source_variant,
                                             "weights": dict(weights)}, author="user")
        return ips
