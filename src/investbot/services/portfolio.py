"""Сервис портфеля: брокерский снимок + ручные счета + оценка + дрейф к IPS.

Каналы (Telegram, CLI) работают только через этот сервис, не через адаптеры (ADR-17).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import structlog

from investbot.core.models import Account, BrokerPortfolio, CashBalance, PortfolioSnapshot, Position, Strict
from investbot.core.money import ZERO
from investbot.core.ports import BrokerReadPort
from investbot.portfolio.manual import validate_against_broker
from investbot.portfolio.manual_store import ManualStore, YamlManualStore
from investbot.portfolio.valuation import PortfolioValuation, value_portfolio
from investbot.rebalance.drift import BucketDrift, compute_drift
from investbot.rebalance.ips import IPS, load_ips

log = structlog.get_logger(__name__)

DFA_RECON_TOLERANCE = Decimal("0.01")
BROKER_STALE_MAX = timedelta(days=7)


class BrokerSnapshotCache:
    """Последний успешный брокерский снимок (L3, §4.5): при недоступности Т-Инвестиций план строится по нему."""

    def __init__(self, directory: Path) -> None:
        self.dir = directory

    def save(self, taken_at: datetime, accounts: list[Account], portfolios: list[BrokerPortfolio]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        data = {"taken_at": taken_at.isoformat(), "accounts": [a.model_dump(mode="json") for a in accounts],
                "portfolios": [p.model_dump(mode="json") for p in portfolios]}
        tmp = self.dir / "broker_snapshot.tmp"
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.dir / "broker_snapshot.json")

    def load(self) -> tuple[datetime, list[Account], list[BrokerPortfolio]] | None:
        path = self.dir / "broker_snapshot.json"
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return (datetime.fromisoformat(data["taken_at"]),
                [Account.model_validate(a) for a in data["accounts"]],
                [BrokerPortfolio.model_validate(p) for p in data["portfolios"]])


class PortfolioOverview(Strict):
    snapshot: PortfolioSnapshot
    valuation: PortfolioValuation
    ips: IPS | None
    drift: tuple[BucketDrift, ...]
    warnings: tuple[str, ...]


class PortfolioService:
    def __init__(self, broker: BrokerReadPort | None, manual: ManualStore | Path, ips_path: Path,
                 cache: BrokerSnapshotCache | None = None) -> None:
        self._broker = broker
        self._cache = cache
        self.manual: ManualStore = YamlManualStore(manual) if isinstance(manual, Path) else manual
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

        flags: list[str] = []
        broker_as_of: datetime | None = None
        if self._broker is not None:
            try:
                broker_accounts = await self._broker.accounts()
                portfolios = [await self._broker.portfolio(a.account_id) for a in broker_accounts]
                if self._cache is not None:
                    self._cache.save(datetime.now(UTC), broker_accounts, portfolios)
            except Exception as e:
                cached = self._cache.load() if self._cache is not None else None
                if cached is None:
                    raise
                broker_as_of, broker_accounts, portfolios = cached
                age = datetime.now(UTC) - broker_as_of
                flags.append("broker_too_old" if age > BROKER_STALE_MAX else "broker_stale")
                log.warning("portfolio.broker_unavailable", error=type(e).__name__,
                            cache_age_h=int(age.total_seconds() // 3600))
            for acc, bp in zip(broker_accounts, portfolios, strict=True):
                accounts.append(acc)
                positions.extend(bp.positions)
                cash.extend(bp.cash)
                for k, v in bp.fx_rates.items():
                    fx.setdefault(k, v)
                if bp.total_dfa_rub:
                    dfa_totals[acc.account_id] = bp.total_dfa_rub

        manual = await self.manual.load()
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
            flags=tuple(flags),
            broker_as_of=broker_as_of,
        )
        return snap, fx

    async def overview(self) -> PortfolioOverview:
        snap, fx = await self.snapshot()
        return self.overview_from(snap, fx)

    def overview_from(self, snap: PortfolioSnapshot, fx: dict[str, Decimal]) -> PortfolioOverview:
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
