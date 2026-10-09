"""Денежная арифметика: только Decimal, без float в расчётах портфеля."""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal

ZERO = Decimal(0)
ONE = Decimal(1)
NANO = Decimal("1e-9")


def from_units_nano(units: int, nano: int) -> Decimal:
    """Quotation/MoneyValue брокера (units + nano·1e-9) → Decimal."""
    return Decimal(units) + Decimal(nano) * NANO


def round_money(x: Decimal, places: int = 2) -> Decimal:
    return x.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_EVEN)


def pct(x: Decimal, places: int = 1) -> str:
    """Доля 0.1234 → «12,3%» (запятая — десятичный разделитель в текстах бота)."""
    return f"{round_money(x * 100, places)}%".replace(".", ",").replace("-", "−")


def pp(x: Decimal, places: int = 1) -> str:
    """Разница долей 0.023 → «+2,3 п.п.»."""
    v = round_money(x * 100, places)
    sign = "+" if v > 0 else ""
    return f"{sign}{str(v).replace('.', ',').replace('-', '−')} п.п."


def safe_div(a: Decimal, b: Decimal) -> Decimal:
    return a / b if b != 0 else ZERO
