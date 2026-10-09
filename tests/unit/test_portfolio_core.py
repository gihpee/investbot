from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from investbot.core.models import (
    Account,
    AccountKind,
    AccountSource,
    AssetClass,
    BrokerPortfolio,
    CashBalance,
    CouponKind,
    FundFocus,
    InstrumentMeta,
    PortfolioSnapshot,
    Position,
    UserInfo,
)
from investbot.portfolio.buckets import UNCLASSIFIED, classify, is_investment_grade, rating_ordinal
from investbot.portfolio.manual import ManualPortfolio, ManualPortfolioError, validate_against_broker
from investbot.portfolio.valuation import value_portfolio
from investbot.rebalance.drift import compute_drift, needs_rebalance
from investbot.rebalance.ips import IPS, BucketTarget
from investbot.services.portfolio import PortfolioService

D = Decimal


def meta(ticker: str, cls: AssetClass, **kw: object) -> InstrumentMeta:
    return InstrumentMeta(ticker=ticker, asset_class=cls, **kw)  # type: ignore[arg-type]


@pytest.mark.parametrize(("raw", "expected"), [
    ("ruAAA", 1), ("AA-(RU)", 4), ("A+.ru", 5), ("BBB|ru|", 9), ("ruB-", 16), ("", None), ("нет", None),
])
def test_rating_ordinal(raw: str, expected: int | None) -> None:
    assert rating_ordinal(raw) == expected


def test_investment_grade_threshold() -> None:
    assert is_investment_grade("ruA-", "A-")
    assert not is_investment_grade("ruBBB+", "A-")
    assert not is_investment_grade(None, "A-")


def test_classify_buckets() -> None:
    assert classify(meta("AAAA", AssetClass.EQUITY), "A-") == "equity_ru"
    ofz = meta("SU26", AssetClass.BOND, is_government=True, coupon_kind=CouponKind.FIXED)
    assert classify(ofz, "A-") == "ofz_fixed"
    assert classify(meta("SU29", AssetClass.BOND, is_government=True, coupon_kind=CouponKind.FLOATING),
                    "A-") == "ofz_float_linker"
    assert classify(meta("C1", AssetClass.BOND, rating="AA"), "A-") == "corp_ig"
    assert classify(meta("C2", AssetClass.BOND, rating="BB"), "A-") == "corp_hy"
    assert classify(meta("C3", AssetClass.BOND, currency="cny", rating="AAA"), "A-") == "fx_cash"
    assert classify(meta("MM", AssetClass.FUND, fund_focus=FundFocus.MONEY_MARKET), "A-") == "money_market"
    assert classify(meta("BF", AssetClass.FUND, fund_focus=FundFocus.FIXED_INCOME), "A-") == UNCLASSIFIED
    assert classify(meta("BF", AssetClass.FUND, fund_focus=FundFocus.FIXED_INCOME), "A-",
                    {"BF": "corp_ig"}) == "corp_ig"
    assert classify(meta("CFA1", AssetClass.DFA), "A-") == "dfa"


def ips(**buckets: BucketTarget) -> IPS:
    return IPS(version=1, buckets=buckets)


def test_ips_targets_must_sum_to_one() -> None:
    with pytest.raises(ValidationError):
        ips(equity_ru=BucketTarget(target=D("0.5")), money_market=BucketTarget(target=D("0.4")))


def test_ips_rejects_unknown_bucket() -> None:
    with pytest.raises(ValidationError):
        ips(crypto=BucketTarget(target=D(1)))


def test_band_is_narrower_of_abs_and_rel() -> None:
    assert BucketTarget(target=D("0.40"), band_abs=D("0.05"), band_rel=D("0.25")).band() == D("0.05")
    assert BucketTarget(target=D("0.08"), band_abs=D("0.05"), band_rel=D("0.25")).band() == D("0.02")
    low, high = BucketTarget(target=D("0.04"), cap=D("0.07")).bounds()
    assert (low, high) == (D(0), D("0.07"))


def test_drift_statuses() -> None:
    p = ips(
        equity_ru=BucketTarget(target=D("0.40"), band_abs=D("0.05"), band_rel=D("0.25")),
        ofz_fixed=BucketTarget(target=D("0.40"), band_abs=D("0.05"), band_rel=D("0.25")),
        money_market=BucketTarget(target=D("0.15"), band_abs=D("0.03")),
        dfa=BucketTarget(target=D("0.05"), cap=D("0.07")),
    )
    drift = compute_drift({"equity_ru": D("0.47"), "ofz_fixed": D("0.34"), "money_market": D("0.10"),
                           "dfa": D("0.08"), "gold": D("0.01")}, p)
    status = {d.bucket: d.status for d in drift}
    assert status == {"equity_ru": "above", "ofz_fixed": "below", "money_market": "below",
                      "dfa": "cap_breach", "gold": "not_in_ips"}
    assert needs_rebalance(drift)
    ok = compute_drift({"equity_ru": D("0.42"), "ofz_fixed": D("0.38"), "money_market": D("0.15"),
                        "dfa": D("0.05")}, p)
    assert not needs_rebalance(ok)


def snapshot(*positions: Position, cash: tuple[CashBalance, ...] = ()) -> PortfolioSnapshot:
    return PortfolioSnapshot(taken_at=datetime.now(UTC), accounts=(), positions=positions, cash=cash)


def test_valuation_weights_with_fx_and_unpriced() -> None:
    snap = snapshot(
        Position(account_id="a", instrument=meta("AAAA", AssetClass.EQUITY), qty=D(10), price=D(100)),
        Position(account_id="a", instrument=meta("B", AssetClass.BOND, is_government=True,
                                                 coupon_kind=CouponKind.FIXED), qty=D(1), price=D(950),
                 accrued=D(50)),
        Position(account_id="a", instrument=meta("X", AssetClass.EQUITY), qty=D(1), price=None),
        cash=(CashBalance(account_id="a", currency="rub", amount=D(500)),
              CashBalance(account_id="a", currency="cny", amount=D(50))),
    )
    v = value_portfolio(snap, {"rub": D(1), "cny": D(10)})
    assert v.total_rub == D(3000)
    assert v.bucket_values == {"equity_ru": D(1000), "ofz_fixed": D(1000), "money_market": D(500),
                               "fx_cash": D(500)}
    assert sum(v.bucket_weights.values()) == D(1)
    assert v.unpriced == ("X",)


def test_manual_position_on_broker_account_is_rejected() -> None:
    manual = ManualPortfolio.model_validate({"accounts": [{"id": "A1", "name": "дубль", "positions": []}]})
    with pytest.raises(ManualPortfolioError):
        validate_against_broker(manual, {"A1"})


def test_manual_dfa_card_priced_at_nominal() -> None:
    manual = ManualPortfolio.model_validate({"accounts": [{
        "id": "dfa_alfa", "name": "ЦФА Альфа", "kind": "dfa",
        "positions": [{"ticker": "ЦФА-1", "asset_class": "dfa", "qty": 5, "nominal": 10000, "operator": "A"}],
    }]})
    [pos] = manual.positions()
    assert pos.source == "dfa_card" and pos.value == D(50000)


class FakeBroker:
    async def accounts(self) -> list[Account]:
        return [Account(account_id="A1", name="Брокерский", kind=AccountKind.BROKER, source=AccountSource.TINVEST)]

    async def user_info(self) -> UserInfo:
        return UserInfo(qualified=False)

    async def portfolio(self, account_id: str) -> BrokerPortfolio:
        return BrokerPortfolio(
            account_id=account_id,
            positions=(Position(account_id=account_id, instrument=meta("AAAA", AssetClass.EQUITY), qty=D(10),
                                price=D(400)),),
            cash=(CashBalance(account_id=account_id, currency="rub", amount=D(1000)),),
        )


async def test_service_overview_combines_broker_manual_and_ips(tmp_path: Path) -> None:
    (tmp_path / "manual.yaml").write_text(
        "accounts:\n  - id: other\n    name: Другой брокер\n    positions:\n"
        "      - {ticker: SU26XXX, asset_class: bond, qty: 5, price_pct: 100, nominal: 1000,"
        " is_government: true, coupon_kind: fixed}\n",
        encoding="utf-8",
    )
    (tmp_path / "ips.yaml").write_text(
        "version: 1\nbuckets:\n  equity_ru: {target: 0.5, band_abs: 0.05}\n"
        "  ofz_fixed: {target: 0.4, band_abs: 0.05}\n  money_market: {target: 0.1, band_abs: 0.03}\n",
        encoding="utf-8",
    )
    svc = PortfolioService(FakeBroker(), tmp_path / "manual.yaml", tmp_path / "ips.yaml")
    ov = await svc.overview()
    assert ov.valuation.total_rub == D(10000)
    assert ov.valuation.bucket_weights == {"equity_ru": D("0.4"), "ofz_fixed": D("0.5"), "money_market": D("0.1")}
    status = {d.bucket: d.status for d in ov.drift}
    assert status["equity_ru"] == "below" and status["ofz_fixed"] == "above" and status["money_market"] == "ok"


def test_example_configs_are_valid() -> None:
    from investbot.portfolio.manual import load_manual
    from investbot.rebalance.ips import load_ips

    root = Path(__file__).resolve().parents[2] / "config"
    assert load_ips(root / "ips.example.yaml").version == 1
    manual = load_manual(root / "manual_portfolio.example.yaml")
    assert len(manual.positions()) == 2
    bond = next(p for p in manual.positions() if p.instrument.asset_class is AssetClass.BOND)
    assert bond.price == D("985.0")


def test_build_and_save_ips_roundtrip(tmp_path: Path) -> None:
    from investbot.rebalance.ips import load_ips
    from investbot.rebalance.ips_builder import build_ips, save_ips

    weights = {"equity_ru": D("0.35"), "ofz_fixed": D("0.30"), "corp_ig": D("0.15"), "money_market": D("0.10"),
               "gold": D("0.05"), "dfa": D("0.05"), "corp_hy": D(0)}
    first = build_ips(weights, None)
    assert first.version == 1
    assert first.buckets["dfa"].cap == D("0.08") and first.buckets["dfa"].band() is None
    assert first.buckets["corp_hy"].cap == D("0.03")
    assert first.buckets["equity_ru"].band() == D("0.05")
    save_ips(first, tmp_path / "ips.yaml")
    loaded = load_ips(tmp_path / "ips.yaml")
    assert loaded.buckets["equity_ru"].target == D("0.35")
    second = build_ips(weights, loaded)
    assert second.version == 2


def test_money_formatting() -> None:
    from investbot.core.money import pct, pp

    assert pct(D("0.1234")) == "12,3%"
    assert pp(D("0.023")) == "+2,3 п.п."
    assert pp(D("-0.05")) == "−5,0 п.п."
    assert pct(D("-0.235")) == "−23,5%"
