"""Календарь событий по позициям (§13.2 `/calendar`, §14 `alerts.calendar`).

Доменный пакет: не импортирует адаптеры и каналы. Источники событий (T-Invest, MOEX ISS, ручные
карточки ЦФА) переводятся в `CalendarEvent` в адаптерах и в `services.calendar`.

Суммы в событиях — только на 1 бумагу (купон на облигацию, дивиденд на акцию): по ним нельзя
восстановить размер позиции, поэтому их можно показывать в Telegram (приватность N4, §13.1).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from investbot.core.models import Strict

EventSource = Literal["tinvest", "iss", "manual"]

# Окно подачи заявок на put-оферту обычно открывается за 1–2 недели до даты оферты и закрывается
# за несколько рабочих дней до неё. Чтобы пользователь не пропустил окно, put-оферта попадает в календарь
# заранее: если окно известно — когда оно начинается в горизонте; если нет — за 14 дней до даты оферты.
PUT_OFFER_LOOKAHEAD = dt.timedelta(days=14)


class EventKind(StrEnum):
    COUPON = "coupon"
    AMORTIZATION = "amortization"  # частичное погашение номинала
    CALL_OFFER = "call_offer"  # досрочное погашение по решению эмитента: действий не требует
    PUT_OFFER = "put_offer"  # выкуп по требованию владельца: нужно подать заявку в окне
    MATURITY = "maturity"
    DIVIDEND = "dividend"
    DFA_MATURITY = "dfa_maturity"


# порядок внутри одного дня: сначала то, что требует действий
KIND_ORDER = {
    EventKind.PUT_OFFER: 0,
    EventKind.DIVIDEND: 1,
    EventKind.MATURITY: 2,
    EventKind.DFA_MATURITY: 3,
    EventKind.AMORTIZATION: 4,
    EventKind.CALL_OFFER: 5,
    EventKind.COUPON: 6,
}
SOURCE_PRIORITY: dict[str, int] = {"tinvest": 0, "iss": 1, "manual": 2}


class CalendarEvent(Strict):
    """Одно событие по бумаге.

    `date` — дата события: выплаты купона, амортизации, погашения, исполнения оферты; у дивиденда —
    дата фиксации реестра (отсечка), а последний день покупки — в `last_buy_date`.
    """

    date: dt.date
    kind: EventKind
    ticker: str
    name: str = ""
    amount_per_unit: Decimal | None = None  # в валюте `currency` на 1 бумагу; None — не объявлено
    currency: str = "rub"
    last_buy_date: dt.date | None = None  # дивиденды: последний день покупки под отсечку
    window_start: dt.date | None = None  # put-оферта: окно подачи заявок, если известно
    window_end: dt.date | None = None
    source: EventSource
    note: str = ""

    @property
    def needs_action(self) -> bool:
        """Событие требует действий пользователя (подать заявку на put-оферту)."""
        return self.kind is EventKind.PUT_OFFER

    def key(self) -> tuple[dt.date, EventKind, str]:
        """Ключ дедупликации: одно событие одной бумаги из разных источников или со счетов."""
        return self.date, self.kind, self.ticker.upper()


class CalendarResult(Strict):
    """Результат сборки календаря: события и предупреждения об источниках, которые не ответили."""

    today: dt.date
    days: int
    events: tuple[CalendarEvent, ...] = ()
    warnings: tuple[str, ...] = ()


_PUT_MARKERS = ("put", "пут", "по требованию", "безотзывн")
_CALL_MARKERS = ("call", "колл", "по усмотрению эмитента", "по решению эмитента", "досрочное погашение", "отзывн")


def classify_offer(text: str) -> EventKind | None:
    """Put или call по свободному тексту источника («Оферта (Put)», «Колл-опцион», ...); None — не понятно.

    Put-маркеры проверяются первыми: «безотзывная» содержит «отзывн».
    """
    t = text.lower()
    if any(m in t for m in _PUT_MARKERS):
        return EventKind.PUT_OFFER
    if any(m in t for m in _CALL_MARKERS):
        return EventKind.CALL_OFFER
    return None


def in_horizon(e: CalendarEvent, today: dt.date, days: int) -> bool:
    """Попадает ли событие в календарь на `days` дней вперёд от `today` (включительно).

    - Обычные события — по `date`.
    - Дивиденд — ещё и по `last_buy_date`: отсечка может быть сразу за горизонтом, а купить нужно в нём.
    - Put-оферта за горизонтом — если окно подачи начинается в горизонте; если окно неизвестно —
      когда до даты оферты не больше `PUT_OFFER_LOOKAHEAD` после конца горизонта.
    Прошедшие события (дата раньше `today`) не показываются.
    """
    if e.date < today:
        return False
    end = today + dt.timedelta(days=days)
    if e.date <= end:
        return True
    if e.kind is EventKind.DIVIDEND and e.last_buy_date is not None:
        return today <= e.last_buy_date <= end
    if e.kind is EventKind.PUT_OFFER:
        if e.window_start is not None:
            return e.window_start <= end
        return e.date <= end + PUT_OFFER_LOOKAHEAD
    return False


def _merge(a: CalendarEvent, b: CalendarEvent) -> CalendarEvent:
    """Из двух записей одного события оставляет более полную; недостающие поля берёт из второй."""
    def rank(e: CalendarEvent) -> tuple[int, int]:
        return (0 if e.amount_per_unit is not None else 1), SOURCE_PRIORITY.get(e.source, 9)

    best, other = (a, b) if rank(a) <= rank(b) else (b, a)
    update: dict[str, object] = {}
    for field in ("amount_per_unit", "last_buy_date", "window_start", "window_end"):
        if getattr(best, field) is None and getattr(other, field) is not None:
            update[field] = getattr(other, field)
    if not best.name and other.name:
        update["name"] = other.name
    if not best.note and other.note:
        update["note"] = other.note
    return best.model_copy(update=update) if update else best


def sort_key(e: CalendarEvent) -> tuple[dt.date, int, str]:
    return e.date, KIND_ORDER[e.kind], e.ticker.upper()


def dedupe_and_sort(events: Iterable[CalendarEvent]) -> list[CalendarEvent]:
    """Убирает дубли (одна бумага на нескольких счетах, несколько источников) и сортирует по дате."""
    merged: dict[tuple[dt.date, EventKind, str], CalendarEvent] = {}
    for e in events:
        k = e.key()
        merged[k] = _merge(merged[k], e) if k in merged else e
    return sorted(merged.values(), key=sort_key)
