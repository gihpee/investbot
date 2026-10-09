"""Сервис портфеля: брокерский снимок + ручные счета + оценка + дрейф к IPS.

Каналы (Telegram, CLI) работают только через этот сервис, не через адаптеры (ADR-17).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import structlog

from investbot.core.models import Account, CashBalance, PortfolioSnapshot, Position, Strict
from investbot.core.money import ZERO
from investbot.core.ports import BrokerReadPort
from investbot.portfolio.manual import load_manual, validate_against_broker
from investbot.portfolio.valuation import PortfolioValuation, value_portfolio
from investbot.rebalance.drift import BucketDrift, compute_drift
from investbot.rebalance.ips import IPS, load_ips

log = structlog.get_logger(__name__)

DFA_RECON_TOLERANCE = Decimal("0.01")


class PortfolioOverview(Strict):
    snapshot: PortfolioSnapshot
    valuation: PortfolioValuation
    ips: IPS | None
    drift: tuple[BucketDrift, ...]
    warnings: tuple[str, ...]


class PortfolioService:
    def __init__(self, broker: BrokerReadPort | None, manual_path: Path, ips_path: Path) -> None:
        self._broker = broker
        self._manual_path = manual_path
        self._ips_path = ips_path

    def load_ips(self) -> IPS | None:
        return load_ips(self._ips_path) if self._ips_path.exists() else None

    async def snapshot(self) -> tuple[PortfolioSnapshot, dict[str, Decimal]]:
        accounts: list[Account] = []
        positions: list[Position] = []
        cash: list[CashBalance] = []
        warnings: list[str] = []
        fx: dict[str, Decimal] = {"rub": Decimal(1)}
        dfa_totals: dict[str, Decimal] = {}

        if self._broker is not None:
            for acc in await self._broker.accounts():
                bp = await self._broker.portfolio(acc.account_id)
                accounts.append(acc)
                positions.extend(bp.positions)
                cash.extend(bp.cash)
                for k, v in bp.fx_rates.items():
                    fx.setdefault(k, v)
                if bp.total_dfa_rub:
                    dfa_totals[acc.account_id] = bp.total_dfa_rub

        manual = load_manual(self._manual_path)
        validate_against_broker(manual, {a.account_id for a in accounts})
        accounts.extend(manual.domain_accounts())
        positions.extend(manual.positions())
        cash.extend(manual.cash())

        for macc in manual.accounts:
            if macc.tinvest_account_id and macc.tinvest_account_id in dfa_totals:
                cards = sum((p.to_position(macc.id).value or ZERO for p in macc.positions), ZERO)
                api_total = dfa_totals[macc.tinvest_account_id]
                if api_total and abs(cards - api_total) / api_total > DFA_RECON_TOLERANCE:
                    warnings.append(
                        f"ЦФА «{macc.name}»: сумма карточек расходится с суммой на смарт-счёте больше чем на 1%"
                    )

        snap = PortfolioSnapshot(
            taken_at=datetime.now(UTC),
            accounts=tuple(accounts),
            positions=tuple(positions),
            cash=tuple(cash),
            warnings=tuple(warnings),
        )
        return snap, fx

    async def overview(self) -> PortfolioOverview:
        snap, fx = await self.snapshot()
        ips = self.load_ips()
        valuation = value_portfolio(
            snap, fx,
            ig_min=ips.rules.ig_min_rating if ips else "A-",
            overrides=ips.instrument_buckets if ips else None,
        )
        drift = tuple(compute_drift(valuation.bucket_weights, ips)) if ips else ()
        warnings = list(snap.warnings)
        if valuation.unpriced:
            warnings.append(f"без цены (не вошли в оценку): {', '.join(valuation.unpriced)}")
        if valuation.unclassified:
            warnings.append(
                f"не отнесены к бакетам: {', '.join(valuation.unclassified)} — укажите в instrument_buckets IPS"
            )
        return PortfolioOverview(
            snapshot=snap, valuation=valuation, ips=ips, drift=drift, warnings=tuple(warnings)
        )
