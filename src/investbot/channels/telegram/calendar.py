"""`/calendar` (§13.2): купоны, амортизации, оферты (окна подачи), отсечки, погашения, погашения ЦФА.

Приватность (N4): суммы — только на 1 бумагу (купон на облигацию, дивиденд на акцию), количества и итоги
в рублях не выводятся. Все строки из источников экранируются. Сообщение укладывается в лимит Telegram
(4096 символов): лишние события сворачиваются в «…и ещё N».
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from html import escape
from typing import Protocol

import structlog
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message

from investbot.calendar.models import CalendarEvent, CalendarResult, EventKind, dedupe_and_sort, in_horizon
from investbot.channels.telegram.render import DISCLAIMER
from investbot.core.money import round_money

log = structlog.get_logger(__name__)

BTN_CALENDAR = "🗓 Календарь"
DEFAULT_DAYS = 30
MESSAGE_LIMIT = 4000  # лимит Telegram 4096; HTML-разметка длиннее видимого текста, так что запас с избытком
BLOCK_LIMIT = 1500  # блок недельного отчёта: календарь не должен вытеснять остальное
NAME_MAX = 48
NOTE_MAX = 160
WARNING_MAX = 200
MAX_WARNINGS = 5
MSK = timezone(timedelta(hours=3), "MSK")  # Europe/Moscow без перехода на летнее время с 2014 года

WEEKDAYS = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")
CURRENCY_SIGN = {"rub": "₽", "usd": "$", "eur": "€", "cny": "¥"}
KIND_ICON = {
    EventKind.COUPON: "💰",
    EventKind.AMORTIZATION: "🧩",
    EventKind.CALL_OFFER: "📞",
    EventKind.PUT_OFFER: "⚠️",
    EventKind.MATURITY: "🏁",
    EventKind.DIVIDEND: "💵",
    EventKind.DFA_MATURITY: "🪙",
}


class CalendarServiceLike(Protocol):
    async def upcoming(self, today: date, days: int = DEFAULT_DAYS) -> CalendarResult: ...


def moscow_today() -> date:
    return datetime.now(MSK).date()


# --- форматирование ------------------------------------------------------------------------------------------

def plural(n: int, one: str, few: str, many: str) -> str:
    """Русская форма числительного: 1 событие, 2 события, 5 событий."""
    n = abs(n) % 100
    if 11 <= n <= 14:
        return many
    if n % 10 == 1:
        return one
    if 2 <= n % 10 <= 4:
        return few
    return many


def _cap(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def fmt_date(d: date, today: date) -> str:
    return d.strftime("%d.%m") if d.year == today.year else d.strftime("%d.%m.%Y")


def fmt_amount(x: Decimal, currency: str) -> str:
    """35.4 rub → «35,40 ₽»; разделитель тысяч — неразрывный пробел."""
    q = round_money(x, 2)
    if q == 0 and x != 0:
        q = round_money(x, 4)
    digits = f"{q:,}".replace(",", " ").replace(".", ",")
    return f"{digits} {CURRENCY_SIGN.get(currency.lower(), currency.upper())}"


def day_header(d: date, today: date) -> str:
    rel = " · сегодня" if d == today else " · завтра" if d == today + timedelta(days=1) else ""
    return f"<b>{WEEKDAYS[d.weekday()]}, {fmt_date(d, today)}</b>{rel}"


def _with_amount(title: str, e: CalendarEvent, unit: str) -> str:
    if e.amount_per_unit is None:
        return title
    return f"{title} {fmt_amount(e.amount_per_unit, e.currency)} {unit}"


def _put_offer(e: CalendarEvent, today: date) -> str:
    ws, we = e.window_start, e.window_end
    if we is not None and we < today:
        return f"оферта: окно подачи заявок закрылось {fmt_date(we, today)}"
    if ws is not None and we is not None:
        return f"оферта: подать заявку в окне {fmt_date(ws, today)}–{fmt_date(we, today)}"
    if we is not None:
        return f"оферта: подать заявку до {fmt_date(we, today)}"
    if ws is not None:
        return f"оферта: подать заявку в окне с {fmt_date(ws, today)}"
    return "оферта: подать заявку в окне до даты оферты — сроки уточните у брокера"


def _dividend(e: CalendarEvent, today: date) -> str:
    text = _with_amount("дивиденд", e, "на бумагу")
    lb = e.last_buy_date
    if lb is None:
        return f"{text}, отсечка"
    if lb < today:
        return f"{text}, отсечка · последний день покупки прошёл ({fmt_date(lb, today)})"
    return f"{text}, отсечка · купить до {fmt_date(lb, today)} включительно"


def _what(e: CalendarEvent, today: date) -> str:
    match e.kind:
        case EventKind.COUPON:
            return _with_amount("купон", e, "на облигацию")
        case EventKind.AMORTIZATION:
            return _with_amount("амортизация (частичное погашение)", e, "на облигацию")
        case EventKind.MATURITY:
            return _with_amount("погашение", e, "на облигацию")
        case EventKind.DFA_MATURITY:
            return _with_amount("погашение ЦФА", e, "за 1 ЦФА")
        case EventKind.CALL_OFFER:
            return "колл-оферта: эмитент может погасить выпуск досрочно, действий не требуется"
        case EventKind.PUT_OFFER:
            return _put_offer(e, today)
        case EventKind.DIVIDEND:
            return _dividend(e, today)


def event_line(e: CalendarEvent, today: date) -> str:
    who = f"<b>{escape(e.ticker)}</b>"
    if e.name and e.name != e.ticker:
        who += f" · {escape(_cap(e.name, NAME_MAX))}"
    line = f"{KIND_ICON[e.kind]} {who} — {escape(_what(e, today))}"
    if e.note:
        line += f" <i>({escape(_cap(e.note, NOTE_MAX))})</i>"
    return line


def _size(lines: Iterable[str]) -> int:
    return sum(len(x) + 1 for x in lines)


def _fit(head: list[str], events: Sequence[CalendarEvent], today: date, tail: list[str], limit: int,
         *, spaced: bool) -> str:
    """Склеивает шапку, события по дням и подвал так, чтобы текст был не длиннее `limit` символов."""
    more_reserve = len("\n…и ещё 100000 событий") + 1
    size = _size(head) + _size(tail) + more_reserve
    body: list[str] = []
    shown = 0
    current: date | None = None
    for e in events:
        group: list[str] = []
        if e.date != current:
            group = ["", day_header(e.date, today)] if spaced else [day_header(e.date, today)]
        line = event_line(e, today)
        cost = _size(group) + len(line) + 1
        if size + cost > limit:
            break
        body.extend(group)
        body.append(line)
        size += cost
        shown += 1
        current = e.date
    hidden = len(events) - shown
    if hidden:
        body.append(f"…и ещё {hidden} {plural(hidden, 'событие', 'события', 'событий')}")
    return "\n".join(head + body + tail)


def calendar_message(result: CalendarResult, *, limit: int = MESSAGE_LIMIT) -> str:
    """Ответ на `/calendar`: события по дням, предупреждения об источниках, дисклеймер."""
    today, days = result.today, result.days
    events = [e for e in result.events if in_horizon(e, today, days)]
    end = today + timedelta(days=days)
    head = [
        "<b>🗓 Календарь событий</b>",
        f"<i>{today:%d.%m} – {end:%d.%m.%Y} · бумаги портфеля · суммы на 1 бумагу</i>",
    ]
    actions = sum(1 for e in events if e.needs_action)
    if actions:
        head.append(f"⚠️ Требуют действий: {actions} ({plural(actions, 'оферта', 'оферты', 'оферт')})")
    if not events:
        head += ["", f"Событий в ближайшие {days} {plural(days, 'день', 'дня', 'дней')} нет."]

    tail: list[str] = []
    if result.warnings:
        tail += ["", "<b>Не всё удалось проверить</b>"]
        tail += [f"• {escape(_cap(w, WARNING_MAX))}" for w in result.warnings[:MAX_WARNINGS]]
        if len(result.warnings) > MAX_WARNINGS:
            tail.append(f"• …и ещё {len(result.warnings) - MAX_WARNINGS}")
    tail += ["", DISCLAIMER]
    return _fit(head, events, today, tail, limit, spaced=True)


def calendar_block(events: Iterable[CalendarEvent], today: date, days: int = 7, *, limit: int = BLOCK_LIMIT) -> str:
    """Блок «События на неделю» для недельного отчёта: без дисклеймера и предупреждений, компактно."""
    evs = dedupe_and_sort(e for e in events if in_horizon(e, today, days))
    head = [f"<b>🗓 События на {days} {plural(days, 'день', 'дня', 'дней')}</b>"]
    if not evs:
        return "\n".join([*head, "Событий по бумагам портфеля нет."])
    return _fit(head, evs, today, [], limit, spaced=False)


# --- роутер --------------------------------------------------------------------------------------------------

def build_calendar_router(
    service: CalendarServiceLike, *, days: int = DEFAULT_DAYS, today: Callable[[], date] = moscow_today
) -> Router:
    router = Router(name="calendar")

    @router.message(Command("calendar"))
    @router.message(F.text == BTN_CALENDAR)
    async def calendar(message: Message) -> None:
        try:
            result = await service.upcoming(today(), days)
        except Exception as e:
            log.exception("calendar.failed")
            await message.answer(f"Не удалось собрать календарь: {escape(type(e).__name__)}. Подробности — /status")
            return
        await message.answer(calendar_message(result))

    return router
