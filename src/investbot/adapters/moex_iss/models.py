"""Типизированные ответы ISS. Имена полей — колонки ISS в нижнем регистре."""

from __future__ import annotations

from datetime import date, time
from decimal import Decimal
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, ConfigDict

DAYS_PER_YEAR = Decimal(365)


def _null_date(v: Any) -> Any:
    """ISS местами отдаёт «0000-00-00» вместо null."""
    return None if v in ("0000-00-00", "") else v


IssDate = Annotated[date | None, BeforeValidator(_null_date)]


class IssModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class IndexClose(IssModel):
    tradedate: date
    close: Decimal | None


class DailyBar(IssModel):
    tradedate: date
    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    close: Decimal | None
    volume: int | None  # штук
    value: Decimal | None  # оборот в валюте торгов
    waprice: Decimal | None


class BondDailyBar(IssModel):
    tradedate: date
    close: Decimal | None  # % номинала
    yieldclose: Decimal | None  # % годовых
    accint: Decimal | None  # НКД на 1 бумагу в валюте номинала
    facevalue: Decimal | None
    duration_days: Decimal | None  # ISS отдаёт дюрацию в днях

    @property
    def duration_years(self) -> Decimal | None:
        return None if self.duration_days is None else self.duration_days / DAYS_PER_YEAR


class Coupon(IssModel):
    coupondate: date
    recorddate: IssDate
    startdate: IssDate
    value: Decimal | None  # у флоатеров будущие купоны — null
    valueprc: Decimal | None  # ставка, % годовых
    facevalue: Decimal | None


class Amortization(IssModel):
    amortdate: date
    value: Decimal | None
    valueprc: Decimal | None  # % номинала
    facevalue: Decimal | None


class Offer(IssModel):
    offerdate: IssDate
    offertype: str | None
    price: Decimal | None  # % номинала


class Bondization(IssModel):
    secid: str
    coupons: tuple[Coupon, ...] = ()
    amortizations: tuple[Amortization, ...] = ()
    offers: tuple[Offer, ...] = ()


class CurvePoint(IssModel):
    """Точка КБД из блока yearyields."""

    tradedate: date
    tradetime: time | None
    period: Decimal  # срок, лет
    value: Decimal  # доходность, % годовых


class SecurityInfo(IssModel):
    secid: str
    description: dict[str, str | None]  # name → value из блока description: ISIN, TYPE, LISTLEVEL, ...
    boardid: str | None  # основной режим: boards.is_primary == 1
    market: str | None
    engine: str | None


_FACE_UNITS = {"SUR": "rub", "RUB": "rub"}


def _face_unit(v: Any) -> Any:
    """ISS FACEUNIT: «SUR» (исторический код рубля) → «rub», прочие коды валют — в нижнем регистре."""
    if v is None or v == "":
        return None
    s = str(v).upper()
    return _FACE_UNITS.get(s, s.lower())


def _zero_none(v: Any) -> Any:
    """Ноль в доходности, дюрации и цене ISS — «нет значения» (ночные обнуления, флоатеры без ставки)."""
    return None if v in (0, "", None) else v


def _as_str(v: Any) -> Any:
    return None if v is None or v == "" else str(v)


IssPositive = Annotated[Decimal | None, BeforeValidator(_zero_none)]


class BondMarketRow(IssModel):
    """Строка рынка облигаций одного режима: securities + marketdata + marketdata_yields, склеенные по SECID.

    Единицы: цены — % номинала; доходности — % годовых (эффективные, с годовой капитализацией);
    дюрация — дни (Маколея, как отдаёт ISS); спреды — б.п.; оборот — в валюте расчётов режима.
    Нули в effective_yield, yield_to_offer, duration_days и price_pct означают «нет значения» и приходят как None;
    при отсутствии эффективной доходности G- и Z-спред тоже None (их считает та же ночная/флоатерная логика ISS).
    """

    secid: str
    board: str
    shortname: str | None
    isin: str | None
    face_value: Decimal | None
    face_unit: Annotated[str | None, BeforeValidator(_face_unit)]  # "rub", "usd", "cny", ...
    coupon_percent: Decimal | None  # % годовых
    coupon_value: Decimal | None  # в валюте номинала на 1 бумагу
    next_coupon: IssDate
    accrued_int: Decimal | None  # НКД на 1 бумагу в валюте номинала
    mat_date: IssDate
    offer_date: IssDate
    buyback_date: IssDate
    call_option_date: IssDate
    put_option_date: IssDate
    lot_size: int | None
    list_level: int | None
    sec_type: Annotated[str | None, BeforeValidator(_as_str)]
    price_pct: IssPositive  # PRICE из marketdata_yields, иначе LAST, иначе PREVPRICE
    effective_yield: IssPositive  # EFFECTIVEYIELD, % годовых — к дате yield_date (погашение или оферта)
    yield_to_offer: IssPositive  # YIELDTOOFFER, % годовых
    yield_date: IssDate  # YIELDDATE: дата, к которой посчитаны доходность и дюрация
    duration_days: IssPositive  # DURATION (Маколея) в днях
    gspread_bp: Decimal | None
    zspread_bp: Decimal | None
    val_today: Decimal | None  # VALTODAY: оборот за текущую (или последнюю) сессию
    num_trades: int | None
    trading_status: Annotated[str | None, BeforeValidator(_as_str)]

    @property
    def duration_years(self) -> Decimal | None:
        return None if self.duration_days is None else self.duration_days / DAYS_PER_YEAR
