"""Календарь событий по текущим позициям (§13.2 `/calendar`, §14 `alerts.calendar`).

Источники по позициям снимка `PortfolioService.snapshot()`:
- брокерские позиции — `EventsPort` (T-Invest GetBondEvents / GetDividends) по `InstrumentMeta.tinvest_uid`:
  облигации — купоны, амортизации, оферты, погашение; акции и фонды — дивиденды. Если T-Invest не ответил
  по облигации, используется резерв ISS bondization по тикеру (у облигаций тикер T-Invest = SECID MOEX);
- облигации ручных счетов — ISS bondization по **тикеру карточки, который должен быть SECID MOEX**
  (у корпоративных выпусков он совпадает с ISIN, например RU000A10FLT1; у ОФЗ — вида SU26238RMFS4).
  ISIN у ручных карточек нет; ISS-эндпоинт `/securities/{secid}/bondization` ждёт SECID. Если ISS не ответил —
  остаётся погашение из поля `maturity` карточки;
- карточки ЦФА — `dfa_maturity` из поля `maturity` (сумма — номинал за 1 ЦФА).
Дивиденды по бумагам ручных счетов не отслеживаются (бесплатного источника нет, §5.2) — об этом предупреждение.

Сбой одного источника не ломает остальные: ошибка становится строкой в `CalendarResult.warnings`.
Ошибка самого снимка портфеля пробрасывается вызывающему: без него неизвестно, какие бумаги в портфеле.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Protocol

import structlog

from investbot.calendar.models import (
    PUT_OFFER_LOOKAHEAD,
    CalendarEvent,
    CalendarResult,
    EventKind,
    EventSource,
    classify_offer,
    dedupe_and_sort,
    in_horizon,
)
from investbot.core.models import AssetClass, InstrumentMeta, PortfolioSnapshot, Position
from investbot.core.ports import EventsPort

log = structlog.get_logger(__name__)

DEFAULT_CONCURRENCY = 4  # одновременных запросов к источникам; лимиты T-Invest держит сам адаптер


# --- Форма ответа ISS bondization (структурно совпадает с adapters.moex_iss.models.Bondization) ------------
# Сервис не импортирует адаптер: каналы импортируют сервисы, а им адаптеры запрещены (.importlinter).

class CouponLike(Protocol):
    @property
    def coupondate(self) -> date: ...

    @property
    def value(self) -> Decimal | None: ...


class AmortizationLike(Protocol):
    @property
    def amortdate(self) -> date: ...

    @property
    def value(self) -> Decimal | None: ...

    @property
    def facevalue(self) -> Decimal | None: ...


class OfferLike(Protocol):
    @property
    def offerdate(self) -> date | None: ...

    @property
    def offertype(self) -> str | None: ...

    @property
    def price(self) -> Decimal | None: ...


class BondizationLike(Protocol):
    @property
    def coupons(self) -> Sequence[CouponLike]: ...

    @property
    def amortizations(self) -> Sequence[AmortizationLike]: ...

    @property
    def offers(self) -> Sequence[OfferLike]: ...


BondizationFetcher = Callable[[str], Awaitable[BondizationLike]]


class SnapshotSource(Protocol):
    """Достаточно `PortfolioService`: нужен только снимок позиций."""

    async def snapshot(self) -> tuple[PortfolioSnapshot, dict[str, Decimal]]: ...


def _opt_date(x: Any) -> date | None:
    return x if isinstance(x, date) else None


def _fmt_pct(x: Decimal) -> str:
    return f"{x.normalize():f}".replace(".", ",")


def events_from_bondization(
    bz: BondizationLike, *, ticker: str, name: str, currency: str = "rub"
) -> list[CalendarEvent]:
    """Купоны, амортизации, погашение и оферты из ISS bondization (все даты, без фильтра по горизонту).

    Погашение отличается от амортизации так: колонка `data_source` ('maturity'/'amortization'), если модель
    её отдаёт; иначе выплата не меньше остатка номинала (`value >= facevalue`); иначе — последняя дата графика.
    Окно подачи put-оферты (`offerdatestart`/`offerdateend`) берётся, если модель ISS его отдаёт.
    """
    out: list[CalendarEvent] = []
    for c in bz.coupons:
        amount = c.value if c.value is not None and c.value > 0 else None
        out.append(CalendarEvent(
            date=c.coupondate, kind=EventKind.COUPON, ticker=ticker, name=name, amount_per_unit=amount,
            currency=currency, source="iss", note="" if amount is not None else "размер купона ещё не объявлен",
        ))

    amorts = sorted(bz.amortizations, key=lambda a: a.amortdate)
    last = amorts[-1].amortdate if amorts else None
    for a in amorts:
        tag = getattr(a, "data_source", None)
        if tag in ("maturity", "amortization"):
            final = tag == "maturity"
        elif a.value is not None and a.facevalue is not None:
            final = a.value >= a.facevalue
        else:
            final = a.amortdate == last
        out.append(CalendarEvent(
            date=a.amortdate, kind=EventKind.MATURITY if final else EventKind.AMORTIZATION, ticker=ticker,
            name=name, amount_per_unit=a.value if a.value is not None and a.value > 0 else None,
            currency=currency, source="iss",
        ))

    for o in bz.offers:
        if o.offerdate is None:
            continue
        detected = classify_offer(o.offertype or "")
        notes: list[str] = []
        if detected is None:
            notes.append("put или call — в ISS не указано, проверьте условия выпуска")
        if o.price is not None and o.price > 0:
            notes.append(f"цена выкупа {_fmt_pct(o.price)}% номинала")
        out.append(CalendarEvent(
            date=o.offerdate, kind=detected or EventKind.PUT_OFFER, ticker=ticker, name=name,
            currency=currency, source="iss", note="; ".join(notes),
            window_start=_opt_date(getattr(o, "offerdatestart", None)),
            window_end=_opt_date(getattr(o, "offerdateend", None)),
        ))
    return out


def _source_key(pos: Position) -> tuple[str, str]:
    meta = pos.instrument
    if meta.asset_class is AssetClass.DFA:
        return "dfa", meta.ticker.upper()
    if pos.source == "broker" and meta.tinvest_uid:
        return "uid", meta.tinvest_uid
    return "ticker", meta.ticker.upper()


class CalendarService:
    def __init__(
        self,
        portfolio: SnapshotSource,
        events: EventsPort | None,
        iss_bondization: BondizationFetcher | None,
        *,
        concurrency: int = DEFAULT_CONCURRENCY,
    ) -> None:
        self._portfolio = portfolio
        self._events = events
        self._iss = iss_bondization
        self._concurrency = max(1, concurrency)

    async def upcoming(self, today: date, days: int = 30) -> CalendarResult:
        """События по бумагам портфеля на `days` дней вперёд (включительно), без дублей, по дате.

        Put-оферты за горизонтом попадают в результат, если окно подачи начинается в горизонте
        (см. `calendar.models.in_horizon`), поэтому источники опрашиваются с запасом `PUT_OFFER_LOOKAHEAD`.
        """
        if days < 0:
            raise ValueError("горизонт календаря не может быть отрицательным")
        snap, _ = await self._portfolio.snapshot()
        frm, to = today, today + timedelta(days=days) + PUT_OFFER_LOOKAHEAD
        sem = asyncio.Semaphore(self._concurrency)  # на вызов: семафор привязывается к своему event loop

        jobs: list[Awaitable[tuple[list[CalendarEvent], list[str]]]] = []
        seen: set[tuple[str, str]] = set()
        untracked: list[str] = []
        for pos in snap.positions:
            key = _source_key(pos)
            if key in seen:
                continue
            seen.add(key)
            meta = pos.instrument
            if meta.asset_class is AssetClass.DFA:
                jobs.append(self._dfa(meta))
            elif key[0] == "uid" and self._events is not None and meta.asset_class is AssetClass.BOND:
                jobs.append(self._broker_bond(sem, self._events, meta, frm, to))
            elif key[0] == "uid" and self._events is not None and meta.asset_class in (AssetClass.EQUITY,
                                                                                     AssetClass.FUND):
                jobs.append(self._broker_dividends(sem, self._events, meta, frm, to))
            elif meta.asset_class is AssetClass.BOND:
                jobs.append(self._iss_bond(sem, meta, from_broker=pos.source == "broker"))
            elif meta.asset_class in (AssetClass.EQUITY, AssetClass.FUND):
                untracked.append(meta.ticker)

        events: list[CalendarEvent] = []
        warnings: list[str] = []
        for evs, warns in await asyncio.gather(*jobs):
            events.extend(evs)
            warnings.extend(warns)
        if untracked:
            tickers = ", ".join(sorted(set(untracked)))
            warnings.append(f"дивиденды не отслеживаются (нет источника для бумаг вне Т-Инвестиций): {tickers}")

        merged = dedupe_and_sort(events)
        return CalendarResult(
            today=today, days=days,
            events=tuple(e for e in merged if in_horizon(e, today, days)),
            warnings=tuple(warnings),
        )

    # --- источники -----------------------------------------------------------------------------------------

    async def _dfa(self, meta: InstrumentMeta) -> tuple[list[CalendarEvent], list[str]]:
        if meta.maturity is None:
            return [], [f"ЦФА {meta.ticker}: в карточке не указана дата погашения"]
        event = CalendarEvent(
            date=meta.maturity, kind=EventKind.DFA_MATURITY, ticker=meta.ticker, name=meta.name,
            amount_per_unit=meta.nominal, currency=meta.currency, source="manual",
        )
        return [event], []

    @staticmethod
    def _normalize(events: list[CalendarEvent], meta: InstrumentMeta) -> list[CalendarEvent]:
        """Тикер и название — из позиции: так события одной бумаги из разных источников склеиваются."""
        return [e.model_copy(update={"ticker": meta.ticker, "name": meta.name or e.name}) for e in events]

    async def _broker_bond(
        self, sem: asyncio.Semaphore, port: EventsPort, meta: InstrumentMeta, frm: date, to: date
    ) -> tuple[list[CalendarEvent], list[str]]:
        uid = meta.tinvest_uid or ""
        try:
            async with sem:
                return self._normalize(await port.bond_events(uid, frm, to), meta), []
        except Exception as exc:
            log.warning("calendar.tinvest_failed", ticker=meta.ticker, kind="bond", error=type(exc).__name__)
            warning = f"{meta.ticker}: Т-Инвестиции не отдали события облигации ({type(exc).__name__})"
        if self._iss is None:
            return self._maturity_fallback(meta, "tinvest"), [warning]
        events, warns = await self._iss_bond(sem, meta, from_broker=True)
        if not warns:
            warning += " — показаны данные MOEX ISS"
        return events, [warning, *warns]

    async def _broker_dividends(
        self, sem: asyncio.Semaphore, port: EventsPort, meta: InstrumentMeta, frm: date, to: date
    ) -> tuple[list[CalendarEvent], list[str]]:
        uid = meta.tinvest_uid or ""
        try:
            async with sem:
                return self._normalize(await port.dividends(uid, frm, to), meta), []
        except Exception as exc:
            log.warning("calendar.tinvest_failed", ticker=meta.ticker, kind="dividends", error=type(exc).__name__)
            return [], [f"{meta.ticker}: Т-Инвестиции не отдали дивиденды ({type(exc).__name__})"]

    async def _iss_bond(
        self, sem: asyncio.Semaphore, meta: InstrumentMeta, *, from_broker: bool
    ) -> tuple[list[CalendarEvent], list[str]]:
        fallback_source: EventSource = "tinvest" if from_broker else "manual"
        if self._iss is None:
            return self._maturity_fallback(meta, fallback_source), [
                f"{meta.ticker}: купоны и оферты не отслеживаются — источник MOEX ISS не подключён"
            ]
        try:
            async with sem:
                bz = await self._iss(meta.ticker)
            return events_from_bondization(bz, ticker=meta.ticker, name=meta.name, currency=meta.currency), []
        except Exception as exc:
            log.warning("calendar.iss_failed", ticker=meta.ticker, error=type(exc).__name__)
            return self._maturity_fallback(meta, fallback_source), [
                f"{meta.ticker}: MOEX ISS не отдал купоны и оферты ({type(exc).__name__})"
            ]

    @staticmethod
    def _maturity_fallback(meta: InstrumentMeta, source: EventSource) -> list[CalendarEvent]:
        """Если график выплат недоступен — хотя бы погашение из справочника или карточки."""
        if meta.maturity is None:
            return []
        return [CalendarEvent(
            date=meta.maturity, kind=EventKind.MATURITY, ticker=meta.ticker, name=meta.name,
            currency=meta.currency, source=source,
        )]
