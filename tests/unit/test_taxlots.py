from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from hypothesis import given
from hypothesis import strategies as st

from investbot.portfolio.taxlots import TaxLot, TradeOp, build_lots, estimate_sale, ldv_horizon, reconcile

D = Decimal
RATE = D("0.13")


def op(kind: str, y: int, m: int, d: int, qty: int, price: str | None = None, fee: str = "0") -> TradeOp:
    return TradeOp(account_id="B", ticker="AAAA", kind=kind, at=datetime(y, m, d, 12, tzinfo=UTC), qty=D(qty),  # type: ignore[arg-type]
                   price=D(price) if price else None, commission=D(fee))


def test_fifo_consumes_earliest_lots_and_includes_commission() -> None:
    res = build_lots([op("buy", 2022, 1, 10, 10, "100", "10"), op("buy", 2024, 5, 1, 10, "200"),
                      op("sell", 2025, 1, 1, 15, "250")])[("B", "AAAA")]
    assert [(lot.acquired, lot.qty, lot.unit_cost) for lot in res.lots] == [(date(2024, 5, 1), D(5), D(200))]
    first = build_lots([op("buy", 2022, 1, 10, 10, "100", "10")])[("B", "AAAA")].lots[0]
    assert first.unit_cost == D(101)


def test_oversell_and_transfer_without_price_are_flagged() -> None:
    res = build_lots([op("transfer_in", 2020, 1, 1, 5), op("sell", 2021, 1, 1, 8, "10")])[("B", "AAAA")]
    assert res.qty == 0 and "неполная" in res.warnings[0]
    assert reconcile(res, D(3)) is not None and reconcile(res, D(0)) is None


def test_ldv_exempts_lots_older_than_three_years() -> None:
    lots = [TaxLot(acquired=date(2022, 1, 10), qty=D(10), unit_cost=D(100)),
            TaxLot(acquired=date(2025, 1, 10), qty=D(10), unit_cost=D(100))]
    t = estimate_sale(lots, D(15), D(150), date(2026, 10, 9), RATE)
    assert t.gain == D(750) and t.ldv_exempt_gain == D(500) and t.taxable_gain == D(250)
    assert t.tax == D(250) * RATE


def test_near_ldv_is_reported() -> None:
    lots = [TaxLot(acquired=date(2023, 11, 1), qty=D(10), unit_cost=D(100))]
    t = estimate_sale(lots, D(10), D(150), date(2026, 10, 9), RATE)
    assert t.near_ldv_qty == D(10) and t.days_to_ldv_min == 23 and t.near_ldv_tax == D(500) * RATE
    assert ldv_horizon(lots, date(2026, 10, 9)) == date(2026, 11, 1)


def test_unknown_cost_lots_not_taxed_but_counted() -> None:
    lots = [TaxLot(acquired=date(2025, 1, 1), qty=D(5), unit_cost=None),
            TaxLot(acquired=date(2025, 2, 1), qty=D(5), unit_cost=D(100))]
    t = estimate_sale(lots, D(8), D(120), date(2026, 10, 9), RATE)
    assert t.unknown_cost_qty == D(5) and t.gain == D(60) and t.tax == D(60) * RATE


def test_losses_are_not_taxed() -> None:
    lots = [TaxLot(acquired=date(2025, 1, 1), qty=D(10), unit_cost=D(200))]
    t = estimate_sale(lots, D(10), D(150), date(2026, 10, 9), RATE)
    assert t.gain == D(-500) and t.tax == 0


@given(st.lists(st.tuples(st.sampled_from(["buy", "sell"]), st.integers(1, 50), st.integers(1, 500)),
                min_size=1, max_size=30))
def test_lot_quantity_conservation(seq: list[tuple[str, int, int]]) -> None:
    ops, held = [], 0
    for i, (kind, qty, price) in enumerate(seq):
        if kind == "sell":
            qty = min(qty, held)
            if qty == 0:
                continue
        held += qty if kind == "buy" else -qty
        ops.append(TradeOp(account_id="B", ticker="X", kind=kind, at=datetime(2024, 1, 1, tzinfo=UTC).replace(  # type: ignore[arg-type]
            day=1 + i % 28, month=1 + i // 28), qty=D(qty), price=D(price)))
    res = build_lots(ops).get(("B", "X"))
    assert (res.qty if res else 0) == held
    assert not (res.warnings if res else ())
