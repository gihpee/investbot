"""FIFO-лоты и оценка НДФЛ при продаже с учётом ЛДВ (§10.3, §10.4; п. 13 ст. 214.1 и ст. 219.1 НК РФ).

Упрощения (помечаются в отчёте как «ориентировочно, не налоговая консультация»):
- лимит ЛДВ (3 млн ₽ × полные годы владения) не проверяется;
- ставка одна (13% или 15% — из профиля), порог 2,4 млн по совокупности баз не моделируется;
- валютная переоценка и перенос убытков не учитываются;
- лоты, пришедшие переводом без цены (от другого брокера, корпоративные действия), имеют неизвестную стоимость:
  налог по ним не оценивается, пока пользователь не укажет себестоимость.
"""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Iterable, Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from investbot.core.models import Strict
from investbot.core.money import ZERO

OpKind = Literal["buy", "sell", "transfer_in", "transfer_out"]

LDV_YEARS = 3
LDV_HIGHTECH_YEARS = 1  # бумаги высокотехнологичных компаний (15-ФЗ от 30.01.2026), список — в конфиге
NEAR_LDV_DAYS = 90


class TradeOp(Strict):
    account_id: str
    ticker: str
    instrument_uid: str | None = None
    kind: OpKind
    at: datetime
    qty: Decimal  # штуки, > 0
    price: Decimal | None = None  # за 1 шт. в рублях; None — перевод без цены
    commission: Decimal = ZERO  # рубли, положительная; входит в расходы (себестоимость покупки)


class TaxLot(Strict):
    acquired: date
    qty: Decimal
    unit_cost: Decimal | None  # с учётом комиссии; None — неизвестна (перевод)


class LotsResult(Strict):
    lots: tuple[TaxLot, ...]
    warnings: tuple[str, ...] = ()

    @property
    def qty(self) -> Decimal:
        return sum((lot.qty for lot in self.lots), ZERO)


class SaleTax(Strict):
    qty: Decimal
    proceeds: Decimal
    cost: Decimal  # по лотам с известной себестоимостью
    gain: Decimal  # proceeds − cost по лотам с известной себестоимостью
    ldv_exempt_gain: Decimal
    taxable_gain: Decimal
    tax: Decimal
    unknown_cost_qty: Decimal  # штук из лотов без себестоимости — налог по ним не оценён
    near_ldv_qty: Decimal  # штук из лотов, которым до ЛДВ ≤ NEAR_LDV_DAYS
    near_ldv_tax: Decimal  # налог, которого можно избежать, подождав до ЛДВ
    days_to_ldv_min: int | None = None


def _ldv_date(acquired: date, years: int) -> date:
    try:
        return acquired.replace(year=acquired.year + years)
    except ValueError:  # 29 февраля
        return acquired.replace(year=acquired.year + years, day=28)


def build_lots(ops: Iterable[TradeOp]) -> dict[tuple[str, str], LotsResult]:
    """FIFO по паре (счёт, тикер). Операции сортируются по времени; продажа сверх лотов — предупреждение."""
    queues: dict[tuple[str, str], deque[TaxLot]] = defaultdict(deque)
    warnings: dict[tuple[str, str], list[str]] = defaultdict(list)
    for op in sorted(ops, key=lambda o: (o.at, 0 if o.kind in ("buy", "transfer_in") else 1)):
        key = (op.account_id, op.ticker)
        q = queues[key]
        if op.kind in ("buy", "transfer_in"):
            cost: Decimal | None = None
            if op.price is not None:
                cost = op.price + (op.commission / op.qty if op.qty else ZERO)
            elif op.kind == "buy":
                warnings[key].append(f"{op.ticker}: покупка {op.at:%d.%m.%Y} без цены")
            q.append(TaxLot(acquired=op.at.date(), qty=op.qty, unit_cost=cost))
            continue
        left = op.qty
        while left > 0 and q:
            head = q[0]
            take = min(left, head.qty)
            left -= take
            if take == head.qty:
                q.popleft()
            else:
                q[0] = head.model_copy(update={"qty": head.qty - take})
        if left > 0:
            warnings[key].append(
                f"{op.ticker}: списание {op.at:%d.%m.%Y} на {left} шт. больше известных покупок — "
                "история операций неполная (перевод от другого брокера или корпоративное действие)"
            )
    return {k: LotsResult(lots=tuple(q), warnings=tuple(warnings[k])) for k, q in queues.items()}


def reconcile(lots: LotsResult, position_qty: Decimal) -> str | None:
    """Расхождение лотов с фактическим количеством в портфеле → строка для отчёта (diff не правится молча)."""
    if lots.qty == position_qty:
        return None
    return (f"лоты по операциям: {lots.qty} шт., в портфеле: {position_qty} шт. — налог оценён по средней цене; "
            "укажите себестоимость переведённых бумаг")


def estimate_sale(
    lots: Sequence[TaxLot],
    qty: Decimal,
    price: Decimal,
    on: date,
    rate: Decimal,
    ldv_years: int = LDV_YEARS,
    near_days: int = NEAR_LDV_DAYS,
) -> SaleTax:
    """Налог при продаже qty штук по цене price (за 1 шт., за вычетом комиссии продажи — на стороне вызывающего)."""
    left = qty
    cost = gain = exempt = ZERO
    unknown = near = near_gain = ZERO
    days_min: int | None = None
    for lot in lots:
        if left <= 0:
            break
        take = min(left, lot.qty)
        left -= take
        if lot.unit_cost is None:
            unknown += take
            continue
        lot_cost = take * lot.unit_cost
        lot_gain = take * price - lot_cost
        cost += lot_cost
        gain += lot_gain
        ldv_at = _ldv_date(lot.acquired, ldv_years)
        if on >= ldv_at:
            if lot_gain > 0:
                exempt += lot_gain
            continue
        days = (ldv_at - on).days
        if days <= near_days:
            near += take
            near_gain += max(ZERO, lot_gain)
            days_min = days if days_min is None else min(days_min, days)
    taxable = max(ZERO, gain - exempt)
    return SaleTax(
        qty=qty, proceeds=qty * price, cost=cost, gain=gain, ldv_exempt_gain=exempt, taxable_gain=taxable,
        tax=taxable * rate, unknown_cost_qty=unknown + max(ZERO, left), near_ldv_qty=near,
        near_ldv_tax=near_gain * rate, days_to_ldv_min=days_min,
    )


def ldv_horizon(lots: Sequence[TaxLot], on: date, ldv_years: int = LDV_YEARS) -> date | None:
    """Ближайшая будущая дата, когда очередной лот получит ЛДВ."""
    future = [d for lot in lots if (d := _ldv_date(lot.acquired, ldv_years)) > on]
    return min(future) if future else None
