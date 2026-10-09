"""Недельный план: снимок → вход движка → план → журнал → сообщение и кнопки."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from investbot.channels.telegram.report import actions_keyboard, parse_amount, weekly_report_message
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
from investbot.journal.store import Journal
from investbot.rebalance.ips import load_ips
from investbot.rebalance.models import CostModel, TaxModel
from investbot.services.portfolio import PortfolioService
from investbot.services.rebalance import RebalanceService, build_plan_input

D = Decimal
TODAY = date(2026, 10, 10)


def meta(ticker: str, cls: AssetClass, lot: int = 1, **kw: object) -> InstrumentMeta:
    return InstrumentMeta(ticker=ticker, asset_class=cls, lot=lot, **kw)  # type: ignore[arg-type]


EQ = meta("AAAA", AssetClass.EQUITY, lot=10)
OFZ = meta("SU26XXX", AssetClass.BOND, is_government=True, coupon_kind=CouponKind.FIXED, nominal=D(1000))
MM = meta("MMFUND", AssetClass.FUND, fund_focus=FundFocus.MONEY_MARKET)


class FakeBroker:
    """Брокерский счёт и ИИС: акции сильно выше коридора, облигаций недовес."""

    async def accounts(self) -> list[Account]:
        return [Account(account_id="B", name="Брокерский", kind=AccountKind.BROKER, source=AccountSource.TINVEST),
                Account(account_id="I", name="ИИС", kind=AccountKind.IIS, source=AccountSource.TINVEST)]

    async def user_info(self) -> UserInfo:
        return UserInfo(qualified=False)

    async def portfolio(self, account_id: str) -> BrokerPortfolio:
        if account_id == "B":
            return BrokerPortfolio(account_id="B", positions=(
                Position(account_id="B", instrument=EQ, qty=D(400), price=D(150), avg_price=D(100)),
                Position(account_id="B", instrument=OFZ, qty=D(20), price=D(950), accrued=D(10)),
            ), cash=(CashBalance(account_id="B", currency="rub", amount=D(20000)),))
        return BrokerPortfolio(account_id="I", positions=(
            Position(account_id="I", instrument=EQ, qty=D(100), price=D(150), avg_price=D(140)),
            Position(account_id="I", instrument=MM, qty=D(10), price=D(1000)),
        ), cash=(CashBalance(account_id="I", currency="rub", amount=D(5000)),))


IPS_YAML = """version: 1
buckets:
  equity_ru: {target: 0.40, band_abs: 0.05, band_rel: 0.25}
  ofz_fixed: {target: 0.45, band_abs: 0.05, band_rel: 0.25}
  money_market: {target: 0.15, band_abs: 0.05, band_rel: 0.30}
"""


def make_service(tmp_path: Path) -> tuple[RebalanceService, Journal]:
    (tmp_path / "ips.yaml").write_text(IPS_YAML, encoding="utf-8")
    journal = Journal(tmp_path / "journal.jsonl")
    portfolio = PortfolioService(FakeBroker(), tmp_path / "manual.yaml", tmp_path / "ips.yaml")
    return RebalanceService(portfolio, None, journal, CostModel(), TaxModel()), journal


def test_plan_input_keeps_only_rub_priced_tradables(tmp_path: Path) -> None:
    snap = PortfolioSnapshot(
        taken_at=datetime.now(UTC),
        accounts=(Account(account_id="B", name="Б", kind=AccountKind.BROKER, source=AccountSource.TINVEST),),
        positions=(
            Position(account_id="B", instrument=EQ, qty=D(10), price=D(100)),
            Position(account_id="B", instrument=meta("ZO", AssetClass.BOND, currency="cny"), qty=D(1), price=D(100)),
            Position(account_id="B", instrument=meta("CFA", AssetClass.DFA), qty=D(1), price=D(1000),
                     source="dfa_card"),
            Position(account_id="B", instrument=meta("NOPRICE", AssetClass.EQUITY), qty=D(1), price=None),
        ),
        cash=(CashBalance(account_id="B", currency="rub", amount=D(500)),
              CashBalance(account_id="B", currency="cny", amount=D(10))),
    )
    (tmp_path / "ips.yaml").write_text(IPS_YAML, encoding="utf-8")
    inp = build_plan_input(snap, {"rub": D(1), "cny": D(11)}, load_ips(tmp_path / "ips.yaml"), TODAY)
    assert [h.instrument.ticker for h in inp.holdings] == ["AAAA"]
    assert inp.other_bucket_values == {"fx_cash": D(1100) + D(110), "dfa": D(1000)}
    assert [c.amount for c in inp.cash] == [D(500)]


async def test_weekly_plan_is_preregistered_and_rendered(tmp_path: Path) -> None:
    svc, journal = make_service(tmp_path)
    prep = await svc.weekly_plan(TODAY)
    plan = prep.plan
    assert plan.verdict == "rebalance"
    assert any(a.side == "sell" and a.ticker == "AAAA" for a in plan.actions)
    first_sell = next(a for a in plan.actions if a.side == "sell")
    assert first_sell.account_id == "I"  # сначала ИИС: продажа без текущего налога
    assert [r["kind"] for r in journal.records()] == ["rebalance_plan"]

    text = weekly_report_message(prep, "weights", events_html="<b>События</b>")
    assert "Нужна ребалансировка" in text and "AAAA" in text and "События" in text
    assert "лот" not in text and "≈" not in text.split("Оборот")[0]  # ни количеств, ни сумм в режиме «только доли»
    assert len(text) < 4096

    kb = actions_keyboard(prep.plan_id, plan, {first_sell.seq: "accept"})
    assert kb is not None
    assert any("принято" in b.text for row in kb.inline_keyboard for b in row)

    svc.record_decision(prep.plan_id, first_sell.seq, "accept")
    assert [r["kind"] for r in journal.records()] == ["rebalance_plan", "decision"]
    assert journal.verify_chain() == 2


async def test_invest_without_sells(tmp_path: Path) -> None:
    svc, _ = make_service(tmp_path)
    prep = await svc.invest(D(30000), "иис", TODAY)
    assert prep.plan.actions and all(a.side == "buy" for a in prep.plan.actions)
    assert any(a.account_id == "I" for a in prep.plan.actions)  # пополнение зачислено на ИИС


async def test_unknown_account_hint(tmp_path: Path) -> None:
    svc, _ = make_service(tmp_path)
    with pytest.raises(LookupError):
        await svc.invest(D(1000), "несуществующий", TODAY)


@pytest.mark.parametrize(("raw", "expected"), [
    ("50000", D(50000)), ("50 000", D(50000)), ("50к", D(50000)), ("1,5k", D(1500)), ("50000₽", D(50000)),
    ("abc", None), ("-5", None), ("0", None),
])
def test_parse_amount(raw: str, expected: Decimal | None) -> None:
    assert parse_amount(raw) == expected
