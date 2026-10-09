"""Перевод ответов SDK `t-tech-investments` в доменные модели.

Функции принимают объекты SDK «по форме» (duck typing), поэтому тестируются без установленного SDK.
Имена полей — по контрактам 1.51 (docs/research/01-tinvest-api.md).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from investbot.core.models import (
    AccessLevel,
    Account,
    AccountKind,
    AccountSource,
    AssetClass,
    CashBalance,
    CouponKind,
    FundFocus,
    InstrumentMeta,
    Position,
)
from investbot.core.money import ZERO, from_units_nano
from investbot.portfolio.buckets import RATING_ORDINAL, rating_ordinal

_ACCOUNT_KINDS = {
    "ACCOUNT_TYPE_TINKOFF": AccountKind.BROKER,
    "ACCOUNT_TYPE_TINKOFF_IIS": AccountKind.IIS,
    "ACCOUNT_TYPE_DFA": AccountKind.DFA,
    "ACCOUNT_TYPE_INVEST_BOX": AccountKind.INVEST_BOX,
    "ACCOUNT_TYPE_INVEST_FUND": AccountKind.INVEST_FUND,
}
_ACCESS = {
    "ACCOUNT_ACCESS_LEVEL_READ_ONLY": AccessLevel.READ_ONLY,
    "ACCOUNT_ACCESS_LEVEL_FULL_ACCESS": AccessLevel.FULL,
    "ACCOUNT_ACCESS_LEVEL_NO_ACCESS": AccessLevel.NO_ACCESS,
}
_INSTRUMENT_TYPES = {
    "share": AssetClass.EQUITY,
    "bond": AssetClass.BOND,
    "etf": AssetClass.FUND,
    "currency": AssetClass.CURRENCY,
    "dfa": AssetClass.DFA,
}
_FUND_FOCUS = {
    "equity": FundFocus.EQUITY,
    "fixed_income": FundFocus.FIXED_INCOME,
    "money_market": FundFocus.MONEY_MARKET,
    # у фондов на металлы focus_type = commodity; прочие сырьевые — через instrument_buckets
    "commodity": FundFocus.GOLD,
    "mixed_allocation": FundFocus.MIXED,
}


def enum_name(x: Any) -> str:
    return str(getattr(x, "name", x))


def quotation(q: Any) -> Decimal:
    """Quotation / MoneyValue (units + nano) → Decimal; None → 0."""
    if q is None:
        return ZERO
    return from_units_nano(int(q.units), int(q.nano))


def money(m: Any) -> tuple[Decimal, str]:
    return quotation(m), str(getattr(m, "currency", "rub") or "rub").lower()


def _as_date(x: Any) -> date | None:
    if x is None:
        return None
    if isinstance(x, datetime):
        # пустые даты в protobuf приходят как 1970-01-01
        return None if x.year <= 1970 else x.date()
    if isinstance(x, date):
        return x
    return None


def map_account(a: Any) -> Account:
    return Account(
        account_id=str(a.id),
        name=str(a.name or a.id),
        kind=_ACCOUNT_KINDS.get(enum_name(a.type), AccountKind.OTHER),
        source=AccountSource.TINVEST,
        access=_ACCESS.get(enum_name(a.access_level), AccessLevel.UNKNOWN),
        opened=_as_date(getattr(a, "opened_date", None)),
    )


def _min_rating(ratings: Any) -> str | None:
    """Из нескольких рейтингов агентств берётся минимальный (multi_rating: lowest)."""
    worst: tuple[int, str] | None = None
    for r in ratings or ():
        level = str(getattr(r, "rating_level", "") or "")
        ordinal = rating_ordinal(level)
        if ordinal is not None and (worst is None or ordinal > worst[0]):
            worst = (ordinal, level)
    if worst is None:
        return None
    # нормализуем к шкале без префиксов: «ruAA-» → «AA-»
    return next(k for k, v in RATING_ORDINAL.items() if v == worst[0])


def _coupon_kind(b: Any) -> CouponKind:
    if getattr(b, "floating_coupon_flag", False):
        return CouponKind.FLOATING
    ticker = str(getattr(b, "ticker", ""))
    if ticker.startswith("SU52"):  # ОФЗ-ИН: серия 52xxx
        return CouponKind.LINKER
    return CouponKind.FIXED


def map_share(s: Any) -> InstrumentMeta:
    return InstrumentMeta(
        ticker=str(s.ticker), name=str(s.name), asset_class=AssetClass.EQUITY,
        currency=str(s.currency).lower(), isin=s.isin or None, tinvest_uid=str(s.uid),
        class_code=str(s.class_code), lot=int(s.lot or 1),
        for_qual_only=bool(getattr(s, "for_qual_investor_flag", False)),
    )


def map_bond(b: Any) -> InstrumentMeta:
    sector = str(getattr(b, "sector", "") or "").lower()
    ticker = str(b.ticker)
    nominal, _ = money(getattr(b, "nominal", None))
    return InstrumentMeta(
        ticker=ticker, name=str(b.name), asset_class=AssetClass.BOND,
        currency=str(b.currency).lower(), isin=b.isin or None, tinvest_uid=str(b.uid),
        class_code=str(b.class_code), lot=int(b.lot or 1),
        is_government=sector == "government" or ticker.startswith("SU"),
        coupon_kind=_coupon_kind(b), rating=_min_rating(getattr(b, "ratings", None)),
        nominal=nominal or None, maturity=_as_date(getattr(b, "maturity_date", None)),
        for_qual_only=bool(getattr(b, "for_qual_investor_flag", False)),
    )


def map_etf(e: Any) -> InstrumentMeta:
    focus = str(getattr(e, "focus_type", "") or "").lower()
    return InstrumentMeta(
        ticker=str(e.ticker), name=str(e.name), asset_class=AssetClass.FUND,
        currency=str(e.currency).lower(), isin=e.isin or None, tinvest_uid=str(e.uid),
        class_code=str(e.class_code), lot=int(e.lot or 1),
        fund_focus=_FUND_FOCUS.get(focus, FundFocus.OTHER),
        for_qual_only=bool(getattr(e, "for_qual_investor_flag", False)),
    )


def fallback_meta(pos: Any) -> InstrumentMeta:
    """Если бумаги нет в справочниках (делистинг, ЦФА, внебиржевые): минимум из самой позиции."""
    itype = str(getattr(pos, "instrument_type", "") or "").lower()
    _, cur = money(getattr(pos, "current_price", None))
    return InstrumentMeta(
        ticker=str(pos.ticker or pos.instrument_uid), asset_class=_INSTRUMENT_TYPES.get(itype, AssetClass.OTHER),
        currency=cur, tinvest_uid=str(pos.instrument_uid), class_code=str(getattr(pos, "class_code", "") or ""),
    )


def normalize_bond_price(price: Decimal, meta: InstrumentMeta) -> tuple[Decimal, str | None]:
    """Защита от путаницы единиц (спайк Ф0: в чём GetPortfolio отдаёт цену облигации).

    Если цена похожа на проценты номинала (≤ 200 при номинале ≥ 500), переводим в валюту и предупреждаем.
    """
    if meta.asset_class is not AssetClass.BOND or meta.nominal is None:
        return price, None
    if meta.nominal >= 500 and ZERO < price <= 200:
        return price * meta.nominal / 100, f"{meta.ticker}: цена {price} принята за % номинала"
    return price, None


def map_portfolio_position(
    account_id: str, pos: Any, meta_by_uid: dict[str, InstrumentMeta]
) -> tuple[Position | None, str | None]:
    """Позиция из GetPortfolio. Валютные позиции пропускаются: кэш берётся из GetPositions.money."""
    uid = str(pos.instrument_uid)
    meta = meta_by_uid.get(uid) or fallback_meta(pos)
    if meta.asset_class is AssetClass.CURRENCY:
        return None, None
    price, _ = money(pos.current_price)
    price, warning = normalize_bond_price(price, meta)
    nkd, _ = money(getattr(pos, "current_nkd", None))
    avg, _ = money(getattr(pos, "average_position_price", None))
    blocked_lots = quotation(getattr(pos, "blocked_lots", None))
    position = Position(
        account_id=account_id, instrument=meta, qty=quotation(pos.quantity),
        price=price if price > 0 else None, accrued=nkd, avg_price=avg or None,
        blocked_qty=blocked_lots * meta.lot, source="broker", price_as_of=datetime.now(UTC),
    )
    return position, warning


def map_cash(account_id: str, money_values: Any) -> list[CashBalance]:
    out = []
    for m in money_values or ():
        amount, cur = money(m)
        if amount != 0:
            out.append(CashBalance(account_id=account_id, currency=cur, amount=amount))
    return out


def fx_from_portfolio(positions: Any) -> dict[str, Decimal]:
    """Курсы к рублю из валютных позиций портфеля (current_price в рублях за 1 единицу)."""
    rates: dict[str, Decimal] = {"rub": Decimal(1)}
    for pos in positions or ():
        if str(getattr(pos, "instrument_type", "")).lower() != "currency":
            continue
        price, price_cur = money(pos.current_price)
        ticker = str(pos.ticker or "").upper()
        code = ticker[:3].lower()  # CNYRUB_TOM → cny, USD000UTSTOM → usd
        if price > 0 and price_cur == "rub" and code not in rates:
            rates[code] = price
    return rates
