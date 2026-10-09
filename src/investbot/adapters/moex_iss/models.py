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
