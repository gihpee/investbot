"""Перевод ответов SDK `t-tech-investments` в доменные модели.

Функции принимают объекты SDK «по форме» (duck typing), поэтому тестируются без установленного SDK.
Имена полей — по контрактам 1.51 (docs/research/01-tinvest-api.md).
"""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from investbot.calendar.models import CalendarEvent, EventKind, classify_offer
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
from investbot.news.models import RawNewsItem
from investbot.news.text import canonical_url, clip, normalize_ws, strip_html
from investbot.portfolio.buckets import RATING_ORDINAL, rating_ordinal
from investbot.portfolio.taxlots import TradeOp

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


def _trading_flags(x: Any) -> dict[str, Any]:
    inc = getattr(x, "min_price_increment", None)
    return {
        "for_qual_only": bool(getattr(x, "for_qual_investor_flag", False)),
        "buy_available": bool(getattr(x, "buy_available_flag", True)),
        "sell_available": bool(getattr(x, "sell_available_flag", True)),
        "for_iis": bool(getattr(x, "for_iis_flag", True)),
        "min_price_increment": quotation(inc) if inc is not None else None,
    }


def map_share(s: Any) -> InstrumentMeta:
    return InstrumentMeta(
        ticker=str(s.ticker), name=str(s.name), asset_class=AssetClass.EQUITY,
        currency=str(s.currency).lower(), isin=s.isin or None, tinvest_uid=str(s.uid),
        class_code=str(s.class_code), lot=int(s.lot or 1), issuer=getattr(s, "asset_uid", None) or None,
        **_trading_flags(s),
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
        **_trading_flags(b),
    )


def map_etf(e: Any) -> InstrumentMeta:
    focus = str(getattr(e, "focus_type", "") or "").lower()
    return InstrumentMeta(
        ticker=str(e.ticker), name=str(e.name), asset_class=AssetClass.FUND,
        currency=str(e.currency).lower(), isin=e.isin or None, tinvest_uid=str(e.uid),
        class_code=str(e.class_code), lot=int(e.lot or 1),
        fund_focus=_FUND_FOCUS.get(focus, FundFocus.OTHER),
        **_trading_flags(e),
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


# --- События по бумагам (GetBondEvents, GetDividends) и последние цены (GetLastPrices) ---------------------
#
# Поля BondEvent из docs/research/01-tinvest-api.md: event_date, pay_one_bond, value, coupon_interest_rate,
# default_date, real_pay_date, execution, note. Не подтверждены отчётом и читаются через getattr с запасным
# значением: event_type (тип события в ответе; запасной — тип из запроса), operation_type.

BOND_EVENT_TYPES = ("EVENT_TYPE_CPN", "EVENT_TYPE_CALL", "EVENT_TYPE_MTY")  # EVENT_TYPE_CONV не показываем
_AMORTIZATION_MARKERS = ("амортиз", "частичн", "amort", "partial")
_FULL_NOMINAL = Decimal(100)


def _fmt_date(d: date) -> str:
    return d.strftime("%d.%m.%Y")


def _fmt_pct(x: Decimal) -> str:
    return f"{x.normalize():f}".replace(".", ",")


def _event_text(e: Any) -> str:
    """Свободный текст события: по нему отличаем put от call и амортизацию от погашения."""
    parts = (getattr(e, name, None) for name in ("note", "operation_type", "execution"))
    return " ".join(str(p) for p in parts if p)


def map_bond_event(
    e: Any, *, ticker: str, name: str, type_hint: str = "", maturity: date | None = None
) -> CalendarEvent | None:
    """BondEvent → CalendarEvent; None — тип не показывается в календаре (конвертация) или нет даты.

    - EVENT_TYPE_CPN → купон, сумма — `pay_one_bond` (у флоатеров будущие купоны могут быть 0 → «не объявлен»).
    - EVENT_TYPE_CALL → оферта. Отдельного поля «put/call» в отчёте нет: направление ищем в `note`/`operation_type`;
      если не нашли — put (окно подачи пропустить дороже, чем лишний раз проверить условия) с пометкой.
    - EVENT_TYPE_MTY → погашение; амортизация, если `value` (доля номинала, **[проверить]**) в (0, 100),
      текст говорит о частичном погашении или дата раньше даты погашения выпуска.
    """
    raw_type = getattr(e, "event_type", None)
    type_name = enum_name(raw_type) if raw_type is not None else ""
    if not type_name or type_name.endswith("UNSPECIFIED") or type_name.isdigit():
        type_name = type_hint
    when = _as_date(getattr(e, "event_date", None))
    if when is None:
        return None
    amount, currency = money(getattr(e, "pay_one_bond", None))
    value = quotation(getattr(e, "value", None))
    text = _event_text(e)
    notes: list[str] = []

    if type_name == "EVENT_TYPE_CPN":
        kind = EventKind.COUPON
        if amount <= 0:
            notes.append("размер купона ещё не объявлен")
    elif type_name == "EVENT_TYPE_CALL":
        detected = classify_offer(text)
        kind = detected or EventKind.PUT_OFFER
        if detected is None:
            notes.append("put или call — в API не указано, проверьте условия выпуска")
        if value > 0:
            notes.append(f"цена выкупа {_fmt_pct(value)}% номинала")
    elif type_name == "EVENT_TYPE_MTY":
        partial = (
            ZERO < value < _FULL_NOMINAL
            or any(m in text.lower() for m in _AMORTIZATION_MARKERS)
            or (maturity is not None and when < maturity)
        )
        kind = EventKind.AMORTIZATION if partial else EventKind.MATURITY
    else:
        return None

    pay_date = _as_date(getattr(e, "real_pay_date", None))
    if pay_date is not None and pay_date != when and kind is not EventKind.PUT_OFFER:
        notes.append(f"фактическая выплата {_fmt_date(pay_date)}")
    default = _as_date(getattr(e, "default_date", None))
    if default is not None:
        notes.insert(0, f"дефолт по выплате ({_fmt_date(default)})")
    return CalendarEvent(
        date=when, kind=kind, ticker=ticker, name=name,
        amount_per_unit=amount if amount > 0 else None, currency=currency,
        source="tinvest", note="; ".join(notes),
    )


def map_dividend(d: Any, *, ticker: str, name: str) -> CalendarEvent | None:
    """Dividend → CalendarEvent. Дата события — `record_date` (отсечка), иначе `last_buy_date`/`payment_date`.

    `dividend_net` — по proto «величина дивиденда на 1 бумагу»; до или после НДФЛ — **[проверить]**,
    поэтому в тексте налог не упоминается. `dividend_type` — строка («Regular Cash», «Cancelled», ...).
    """
    record = _as_date(getattr(d, "record_date", None))
    last_buy = _as_date(getattr(d, "last_buy_date", None))
    payment = _as_date(getattr(d, "payment_date", None))
    when = record or last_buy or payment
    if when is None:
        return None
    amount, currency = money(getattr(d, "dividend_net", None))
    notes: list[str] = []
    if "cancel" in str(getattr(d, "dividend_type", "") or "").lower():
        notes.append("дивиденд отменён")
    if payment is not None and payment != when:
        notes.append(f"выплата {_fmt_date(payment)}")
    return CalendarEvent(
        date=when, kind=EventKind.DIVIDEND, ticker=ticker, name=name,
        amount_per_unit=amount if amount > 0 else None, currency=currency,
        last_buy_date=last_buy, source="tinvest", note="; ".join(notes),
    )


def map_last_prices(last_prices: Any, meta_by_uid: dict[str, InstrumentMeta]) -> dict[str, Decimal]:
    """LastPrice[] → {instrument_uid: цена за 1 шт. в валюте}.

    Цена облигаций в GetLastPrices — в пунктах (% номинала), переводится в валюту по текущему номиналу
    из справочника. Нулевые цены (сделок не было) пропускаются.
    """
    out: dict[str, Decimal] = {}
    for lp in last_prices or ():
        uid = str(getattr(lp, "instrument_uid", "") or "")
        price = quotation(getattr(lp, "price", None))
        if not uid or price <= 0:
            continue
        meta = meta_by_uid.get(uid)
        if meta is not None and meta.asset_class is AssetClass.BOND and meta.nominal:
            price = price * meta.nominal / 100
        out[uid] = price
    return out


# --- Лента новостей InstrumentsService.News (с 1.49) ------------------------------------------------------
#
# Поля по docs/research/01 (proto и OpenAPI 1.51): items[] — id, source, title, content, summary (опц.), tables[],
# instrument_id[] (упомянутые бумаги), priority, ts; в ответе has_next, next_cursor. Не подтверждены и читаются
# через getattr с запасным значением:
# - форма instrument_id[]: в примере SDK из отчёта тикер вложен (`ni.instrument.ticker`), в описании полей —
#   плоский (`instrument_uid`/`ticker`/`class_code`); поддержаны оба, элементы без тикера пропускаются;
# - тип priority (флаг «важная новость» или число/enum) — приводится к int;
# - текст: `summary`, затем `content` (по описанию полей), затем `text` (на случай другого имени);
# - ссылка на первоисточник: в отчёте её нет, пробуем `url` и `link`;
# - первоисточник `source` («Интерфакс» и т. п.): в RawNewsItem отдельного поля нет, кладётся в categories
#   с префиксом NEWS_ORIGIN_PREFIX.

TINVEST_NEWS_SOURCE = "tinvest_news"  # ключ источника в config/sources.yaml
NEWS_ORIGIN_PREFIX = "source:"
NEWS_LEAD_MAX = 1000
NEWS_TITLE_MAX = 500


def _utc_ts(x: Any) -> datetime | None:
    """datetime SDK (или protobuf Timestamp) → tz-aware UTC; пустая дата protobuf (1970-01-01) → None."""
    if isinstance(x, datetime):
        dt = (x if x.tzinfo else x.replace(tzinfo=UTC)).astimezone(UTC)
        return None if dt.year <= 1970 else dt
    seconds = getattr(x, "seconds", None)
    if seconds:
        return datetime.fromtimestamp(int(seconds) + int(getattr(x, "nanos", 0) or 0) / 1e9, UTC)
    return None


def _news_priority(p: Any) -> int | None:
    if p is None:
        return None
    if isinstance(p, bool):
        return int(p)
    try:
        return int(getattr(p, "value", p))
    except (TypeError, ValueError):
        return None


def _news_tickers(refs: Any) -> tuple[str, ...]:
    out: list[str] = []
    for ref in refs or ():
        inner = getattr(ref, "instrument", None)
        ticker = (getattr(inner, "ticker", None) if inner is not None else None) or getattr(ref, "ticker", None)
        t = str(ticker or "").strip()
        if t and t not in out:
            out.append(t)
    return tuple(out)


def _news_url(n: Any) -> str | None:
    for name in ("url", "link"):
        raw = str(getattr(n, name, "") or "").strip()
        if raw.lower().startswith(("http://", "https://")):
            return canonical_url(raw)
    return None


def map_news_item(n: Any, now: datetime) -> RawNewsItem:
    """Элемент `News` → RawNewsItem: связи с бумагами — тикеры из instrument_id[] (метод связывания "api")."""
    if now.tzinfo is None:
        raise ValueError("now должен быть tz-aware")
    text = next((str(v) for name in ("summary", "content", "text") if (v := getattr(n, name, None))), "")
    lead = clip(strip_html(text), NEWS_LEAD_MAX)
    title = clip(strip_html(str(getattr(n, "title", "") or "")), NEWS_TITLE_MAX) or clip(lead, NEWS_TITLE_MAX)
    published = _utc_ts(getattr(n, "ts", None))
    news_id = str(getattr(n, "id", "") or "").strip()
    if not news_id:
        basis = f"{published.isoformat() if published else ''}|{title}"
        news_id = "sha1:" + hashlib.sha1(basis.encode(), usedforsecurity=False).hexdigest()
    origin = normalize_ws(str(getattr(n, "source", "") or ""))
    return RawNewsItem(
        source=TINVEST_NEWS_SOURCE,
        external_id=news_id,
        url=_news_url(n),
        title=title,
        lead=lead,
        published_at=published,
        fetched_at=now.astimezone(UTC),
        instrument_tickers=_news_tickers(getattr(n, "instrument_id", None)),
        priority=_news_priority(getattr(n, "priority", None)),
        categories=(f"{NEWS_ORIGIN_PREFIX}{origin}",) if origin else (),
    )


# Типы операций, двигающие количество бумаг. Имена — по контрактам T-Invest; часть [проверить в Ф0]:
# BUY_CARD/SELL_CARD (сделки с карты), INPUT/OUTPUT_SECURITIES (перевод бумаг), BOND_REPAYMENT_FULL (погашение).
TRADE_OP_KINDS: dict[str, str] = {
    "OPERATION_TYPE_BUY": "buy",
    "OPERATION_TYPE_BUY_CARD": "buy",
    "OPERATION_TYPE_BUY_MARGIN": "buy",
    "OPERATION_TYPE_SELL": "sell",
    "OPERATION_TYPE_SELL_CARD": "sell",
    "OPERATION_TYPE_SELL_MARGIN": "sell",
    "OPERATION_TYPE_BOND_REPAYMENT_FULL": "sell",
    "OPERATION_TYPE_INPUT_SECURITIES": "transfer_in",
    "OPERATION_TYPE_OUTPUT_SECURITIES": "transfer_out",
}


def _as_datetime(x: Any) -> datetime | None:
    if isinstance(x, datetime):
        return x if x.tzinfo else x.replace(tzinfo=UTC)
    seconds = getattr(x, "seconds", None)
    if seconds is not None:
        return datetime.fromtimestamp(int(seconds), tz=UTC)
    return None


def map_trade_op(account_id: str, op: Any, meta_by_uid: dict[str, InstrumentMeta]) -> TradeOp | None:
    """Операция GetOperationsByCursor → TradeOp; операции, не меняющие количество бумаг, — None."""
    kind = TRADE_OP_KINDS.get(enum_name(op.type))
    if kind is None:
        return None
    raw_qty = getattr(op, "quantity", 0)
    qty = quotation(raw_qty) if hasattr(raw_qty, "units") else Decimal(int(raw_qty or 0))
    at = _as_datetime(getattr(op, "date", None))
    if qty <= 0 or at is None:
        return None
    uid = str(getattr(op, "instrument_uid", "") or "")
    meta = meta_by_uid.get(uid)
    ticker = meta.ticker if meta else str(getattr(op, "ticker", "") or getattr(op, "name", "") or uid)
    price, cur = money(getattr(op, "price", None))
    commission, _ = money(getattr(op, "commission", None))
    return TradeOp(
        account_id=account_id, ticker=ticker, instrument_uid=uid or None, kind=kind,
        at=at, qty=qty,
        price=price if price > 0 and cur == "rub" else None,  # валютные сделки: себестоимость в ₽ — [проверить]
        commission=abs(commission),
    )
