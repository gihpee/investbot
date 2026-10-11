"""Недельный отчёт, /report, /invest и кнопки решений (§13.4–13.6).

Шаблонный отчёт без LLM (уровень L2): все числа — из плана движка. В режиме `weights` в чат не уходят
рублёвые суммы и количества; их можно получить по кнопке отдельным сообщением с автоудалением.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from datetime import date
from decimal import Decimal, InvalidOperation
from html import escape
from typing import Protocol

import structlog
from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from investbot.channels.telegram.render import DISCLAIMER, title
from investbot.core.money import pct, round_money, safe_div
from investbot.rebalance.models import RebalanceAction, RebalancePlan
from investbot.services.rebalance import Decision, PreparedPlan

log = structlog.get_logger(__name__)

VERDICT = {
    "no_action": "✅ Действий не требуется",
    "cashflow_only": "💸 Распределить свободные деньги, без продаж",
    "rebalance": "🔁 Нужна ребалансировка",
    "blocked": "⛔ План заблокирован лимитами",
}
REASONS = {
    "BAND_RETURN": "возврат в коридор",
    "CASHFLOW_FIRST": "свободные деньги в недовес",
    "UNDERWEIGHT": "недовес класса",
    "CAP_BREACH": "превышен потолок",
    "CASH_SURPLUS": "излишек денег",
}
DECISION_MARK = {"accept": "✅", "postpone": "⏸", "skip": "✖"}
DECISION_TEXT = {"accept": "принято", "postpone": "отложено", "skip": "пропущено"}


def rub(x: Decimal) -> str:
    return f"{round_money(x, 0):,} ₽".replace(",", " ")


def price_str(x: Decimal) -> str:
    return f"{x.normalize():f}".replace(".", ",")


def reasons_text(codes: tuple[str, ...], limit: int = 2) -> str:
    return ", ".join(list(dict.fromkeys(REASONS.get(c.split(":")[0], c) for c in codes))[:limit])


def _action_line(a: RebalanceAction, account_name: str, privacy_mode: str) -> str:
    side = "🟥 Продать" if a.side == "sell" else "🟩 Купить"
    zone = f"{price_str(a.limit_zone[0])}–{price_str(a.limit_zone[1])}"
    head = (f"{a.seq}. {side} <b>{escape(a.ticker)}</b> · {escape(title(a.bucket))} "
            f"{pct(a.w_bucket_before)} → {pct(a.w_bucket_after)}")
    detail = f"   зона {zone} · до {a.valid_until:%d.%m} · {escape(account_name)} · {reasons_text(a.reason_codes)}"
    if privacy_mode == "full":
        detail += f"\n   {a.lots} лот(ов), {a.qty} шт., ≈{rub(a.est_amount_rub)}"
        if a.est_tax_rub:
            detail += f", налог ≈{rub(a.est_tax_rub)}"
    return f"{head}\n{detail}"


def _partial_report(prep: PreparedPlan, scheduled: bool, events_html: str | None) -> str:
    """L4: плана нет — отчёт с долями, событиями, новостями и статусом данных (§4.5)."""
    ov = prep.overview
    lines = ["<b>🗓 Недельный отчёт</b> · план отложен"]
    lines.extend(escape(b) for b in prep.degradation.banners())
    lines.append("")
    lines.append("<b>Доли по классам сейчас</b>")
    for d in ov.drift:
        lines.append(f"• {escape(title(d.bucket))}: {pct(d.weight)}" +
                     (f" (цель {pct(d.target, 0)})" if d.target is not None else ""))
    if events_html:
        lines.append("")
        lines.append(events_html)
    if not scheduled:
        lines.append("")
        lines.append("<i>Сформирован по запросу, вне расписания.</i>")
    lines.append("")
    lines.append(DISCLAIMER)
    return "\n".join(lines)


def weekly_report_message(prep: PreparedPlan, privacy_mode: str = "weights", events_html: str | None = None,
                          scheduled: bool = True) -> str:
    if prep.plan is None:
        return _partial_report(prep, scheduled, events_html)
    p: RebalancePlan = prep.plan
    lines = [f"<b>🗓 Недельный отчёт · {p.as_of:%d.%m.%Y}</b> · IPS v{p.ips_version}"]
    if not scheduled:
        lines.append("<i>Сформирован по запросу, вне расписания.</i>")
    snap = prep.overview.snapshot
    lines.append(f"Данные: снимок {snap.taken_at:%d.%m %H:%M} UTC · счетов {len(snap.accounts)}")
    lines.extend(escape(b) for b in prep.degradation.banners())
    lines.append("")
    lines.append(f"<b>{VERDICT[p.verdict]}</b>")
    lines.append(f"Отклонение от целей: {pct(p.drift_before)} → {pct(p.drift_after)}")
    lines.append("")
    lines.append("<b>Классы активов</b> (сейчас → после плана)")
    for b in p.buckets:
        if b.target is None:
            lines.append(f"• {escape(title(b.bucket))}: {pct(b.weight_before)} — нет в IPS")
            continue
        corridor = f"{pct(b.low or b.target, 0)}–{pct(b.high or b.target, 0)}"
        arrow = f" → {pct(b.weight_after)}" if b.weight_after != b.weight_before else ""
        flag = "" if b.status_before == "ok" else " ⚠️"
        lines.append(f"• {escape(title(b.bucket))}: {pct(b.weight_before)}{arrow} (цель {pct(b.target, 0)}, "
                     f"коридор {corridor}){flag}")
    if p.actions:
        lines.append("")
        lines.append("<b>Предлагаемые действия</b> (по вашему IPS, не по прогнозу)")
        lines.extend(_action_line(a, prep.account_names.get(a.account_id, a.account_id), privacy_mode)
                     for a in p.actions)
        lines.append("")
        nav = p.nav_rub
        sells = sum((a.est_amount_rub for a in p.actions if a.side == "sell"), Decimal(0))
        ips = prep.overview.ips
        limit = f" (недельный лимит {pct(ips.limits.max_weekly_turnover, 0)})" if ips else ""
        if privacy_mode == "full":
            lines.append(f"Продажи ≈{rub(sells)}{limit} · всего сделок ≈{rub(p.turnover_rub)} · "
                         f"комиссии ≈{rub(p.fees_rub)} · налог ≈{rub(p.tax_rub)}")
        else:
            lines.append(f"Продажи {pct(safe_div(sells, nav))} портфеля{limit} · всего сделок "
                         f"{pct(safe_div(p.turnover_rub, nav))} · издержки {pct(safe_div(p.fees_rub, nav), 2)} · "
                         f"налог {pct(safe_div(p.tax_rub, nav), 2)} портфеля")
        lines.append("<i>Покупки на свободные деньги и пополнения в недельный лимит оборота не входят.</i>")
    if p.deferred:
        lines.append("")
        lines.append("<b>При следующем пополнении</b>")
        lines.extend(f"• {escape(x)}" for x in p.deferred)
    if p.unfunded:
        lines.append("")
        lines.append("<b>Нужен ваш выбор</b>")
        lines.extend(f"• {escape(x)}" for x in p.unfunded)
    if p.violations:
        lines.append("")
        lines.extend(f"⛔ {escape(x)}" for x in p.violations)
    if events_html:
        lines.append("")
        lines.append(events_html)
    notes = [*p.notes, *prep.notes, *prep.overview.warnings]
    if notes:
        lines.append("")
        lines.append("<b>Примечания</b>")
        lines.extend(f"• {escape(x)}" for x in notes)
    lines.append("")
    lines.append(DISCLAIMER)
    text = "\n".join(lines)
    return text if len(text) <= 4000 else text[:3990] + "\n…"


def actions_keyboard(plan_id: str, plan: RebalancePlan | None,
                     decided: dict[int, Decision]) -> InlineKeyboardMarkup | None:
    if plan is None or not plan.actions:
        return None
    rows: list[list[InlineKeyboardButton]] = []
    for a in plan.actions:
        if a.seq in decided:
            mark = DECISION_MARK[decided[a.seq]]
            rows.append([InlineKeyboardButton(text=f"{mark} {a.seq}. {a.ticker}: {DECISION_TEXT[decided[a.seq]]}",
                                              callback_data="rp:noop")])
            continue
        rows.append([
            InlineKeyboardButton(text=f"✅ {a.seq}", callback_data=f"rp:{plan_id}:{a.seq}:accept"),
            InlineKeyboardButton(text=f"⏸ {a.seq}", callback_data=f"rp:{plan_id}:{a.seq}:postpone"),
            InlineKeyboardButton(text=f"✖ {a.seq}", callback_data=f"rp:{plan_id}:{a.seq}:skip"),
        ])
    if len(decided) < len(plan.actions):
        rows.append([InlineKeyboardButton(text="✅ Принять всё", callback_data=f"rp:{plan_id}:all:accept"),
                     InlineKeyboardButton(text="🔐 Количества", callback_data=f"rp:{plan_id}:qty")])
    if any(a.side == "sell" for a in plan.actions):
        rows.append([InlineKeyboardButton(text="🔄 Без продаж", callback_data=f"rp:{plan_id}:wi:nosell"),
                     InlineKeyboardButton(text="♻️ Пересчитать", callback_data=f"rp:{plan_id}:wi:fresh")])
    else:
        rows.append([InlineKeyboardButton(text="♻️ Пересчитать", callback_data=f"rp:{plan_id}:wi:fresh")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


ActionKey = tuple[str, str, str]  # (счёт, тикер, сторона)


def _key(a: RebalanceAction) -> ActionKey:
    return (a.account_id, a.ticker, a.side)


def plan_diff(old: RebalancePlan, new: RebalancePlan) -> tuple[list[str], list[str], list[str]]:
    """Понедельничная сверка: (больше не требуется, изменилось, новое) — строки без сумм и количеств."""
    old_by = {_key(a): a for a in old.actions}
    new_by = {_key(a): a for a in new.actions}
    side = {"buy": "покупка", "sell": "продажа"}
    gone = [f"{side[k[2]]} {escape(k[1])}" for k in old_by if k not in new_by]
    changed = []
    for k, a in new_by.items():
        if k in old_by:
            b = old_by[k]
            parts = []
            if a.lots != b.lots:
                parts.append("объём " + ("больше" if a.lots > b.lots else "меньше"))
            if a.limit_zone != b.limit_zone:
                parts.append(f"зона {price_str(a.limit_zone[0])}–{price_str(a.limit_zone[1])}")
            if parts:
                changed.append(f"{side[k[2]]} {escape(k[1])}: {', '.join(parts)}")
    added = [f"{side[k[2]]} {escape(k[1])}" for k in new_by if k not in old_by]
    return gone, changed, added


def refresh_message(old: PreparedPlan, new: PreparedPlan) -> str:
    lines = [f"<b>🔁 План актуализирован по свежим ценам</b> · {new.plan.as_of:%d.%m}" if new.plan
             else "<b>🔁 Актуализация плана</b>"]
    lines.extend(escape(b) for b in new.degradation.banners())
    if old.plan is None or new.plan is None:
        lines.append("План сейчас не строится — см. причину выше." if new.plan is None
                     else "Субботний план не был построен; ниже — актуальный.")
        return "\n".join(lines)
    gone, changed, added = plan_diff(old.plan, new.plan)
    if not (gone or changed or added):
        lines.append("Изменений нет — субботний план в силе.")
    if gone:
        lines.append("<b>Больше не требуется:</b> " + "; ".join(gone))
    if changed:
        lines.append("<b>Изменилось:</b> " + "; ".join(changed))
    if added:
        lines.append("<b>Новое:</b> " + "; ".join(added))
    lines.append("")
    lines.append(DISCLAIMER)
    return "\n".join(lines)


def quantities_message(prep: PreparedPlan) -> str:
    lines = ["<b>🔐 Количества и суммы</b> (сообщение удалится автоматически)"]
    for a in (prep.plan.actions if prep.plan else ()):
        side = "Продать" if a.side == "sell" else "Купить"
        lines.append(f"{a.seq}. {side} {escape(a.ticker)}: {a.lots} лот(ов) = {a.qty} шт., ≈{rub(a.est_amount_rub)}, "
                     f"комиссия ≈{rub(a.est_fee_rub)}" + (f", налог ≈{rub(a.est_tax_rub)}" if a.est_tax_rub else ""))
    lines.append("<i>Налог ориентировочный, не налоговая консультация.</i>")
    return "\n".join(lines)


def invest_message(prep: PreparedPlan, amount: Decimal, privacy_mode: str) -> str:
    head = f"<b>💰 Распределение пополнения</b> {rub(amount) if privacy_mode == 'full' else ''}".rstrip()
    lines = [head, *(escape(b) for b in prep.degradation.banners()), ""]
    p = prep.plan
    if p is None:
        lines.append("Распределение сейчас не строится — см. причину выше.")
        lines.append(DISCLAIMER)
        return "\n".join(lines)
    if not p.actions:
        lines.append("Покупок не получилось — см. примечания.")
    for a in p.actions:
        lines.append(_action_line(a, prep.account_names.get(a.account_id, a.account_id), privacy_mode))
    for x in (*p.unfunded, *p.notes):
        lines.append(f"• {escape(x)}")
    lines.append("")
    lines.append(DISCLAIMER)
    return "\n".join(lines)


class RebalanceServiceLike(Protocol):
    async def weekly_plan(self, as_of: date) -> PreparedPlan: ...

    async def invest(self, amount: Decimal, account_hint: str | None, as_of: date) -> PreparedPlan: ...

    async def whatif(self, as_of: date, no_sells: bool = False) -> PreparedPlan: ...

    async def refresh(self, as_of: date) -> PreparedPlan: ...

    async def record_decision(self, plan_id: str, seq: int | None, decision: Decision) -> None: ...


class ReportCenter:
    """Хранит последние планы для кнопок решений (однопользовательский бот, в памяти)."""

    def __init__(self, service: RebalanceServiceLike, privacy_mode: str, quantity_ttl_sec: int = 600,
                 events_block: Callable[[date], object] | None = None, today: Callable[[], date] = date.today) -> None:
        self.service = service
        self.privacy_mode = privacy_mode
        self.ttl = quantity_ttl_sec
        self.events_block = events_block
        self.today = today
        self.plans: dict[str, PreparedPlan] = {}
        self.last_weekly: PreparedPlan | None = None
        self.retry_after_sec = 3 * 3600
        self.decided: dict[str, dict[int, Decision]] = {}

    async def _events(self, as_of: date) -> str | None:
        if self.events_block is None:
            return None
        try:
            result = self.events_block(as_of)
            if asyncio.iscoroutine(result):
                result = await result
            return result if isinstance(result, str) else None
        except Exception:
            log.exception("report.events_failed")
            return None

    async def send_weekly(self, bot: Bot, chat_id: int, scheduled: bool = True, retry: bool = True) -> None:
        try:
            prep = await self.service.weekly_plan(self.today())
        except LookupError as e:
            await bot.send_message(chat_id, f"Недельный отчёт не построен: {escape(str(e))}")
            return
        self.plans[prep.plan_id] = prep
        self.decided[prep.plan_id] = {}
        self.last_weekly = prep
        events = await self._events(prep.plan.as_of if prep.plan else self.today())
        await bot.send_message(chat_id, weekly_report_message(prep, self.privacy_mode, events, scheduled),
                               reply_markup=actions_keyboard(prep.plan_id, prep.plan, {}))
        if prep.plan is None and scheduled and retry:
            # L4: повтор через 3 часа (один раз), §4.5
            async def later() -> None:
                await asyncio.sleep(self.retry_after_sec)
                await self.send_weekly(bot, chat_id, scheduled=True, retry=False)

            asyncio.get_running_loop().create_task(later())

    def _carry_decisions(self, old: PreparedPlan, new: PreparedPlan) -> dict[int, Decision]:
        """Решения по субботнему плану переносятся на совпадающие действия нового плана."""
        if old.plan is None or new.plan is None:
            return {}
        old_decided = self.decided.get(old.plan_id, {})
        by_key = {_key(a): old_decided[a.seq] for a in old.plan.actions if a.seq in old_decided}
        return {a.seq: by_key[_key(a)] for a in new.plan.actions if _key(a) in by_key}

    async def send_refresh(self, bot: Bot, chat_id: int) -> None:
        old = self.last_weekly
        if old is None:
            return  # субботнего отчёта не было (рестарт бота) — понедельничная сверка не нужна
        new = await self.service.refresh(self.today())
        self.plans[new.plan_id] = new
        self.decided[new.plan_id] = self._carry_decisions(old, new)
        self.last_weekly = new
        await bot.send_message(chat_id, refresh_message(old, new),
                               reply_markup=actions_keyboard(new.plan_id, new.plan, self.decided[new.plan_id]))

    async def send_whatif(self, bot: Bot, chat_id: int, no_sells: bool) -> None:
        prep = await self.service.whatif(self.today(), no_sells=no_sells)
        self.plans[prep.plan_id] = prep
        self.decided[prep.plan_id] = {}
        title_line = "<b>Что если: без продаж</b>\n" if no_sells else "<b>Пересчёт по текущим данным</b>\n"
        await bot.send_message(chat_id, title_line + weekly_report_message(prep, self.privacy_mode, None, False),
                               reply_markup=actions_keyboard(prep.plan_id, prep.plan, {}))

    async def send_quantities(self, bot: Bot, chat_id: int, plan_id: str) -> None:
        prep = self.plans.get(plan_id)
        if prep is None:
            return
        msg = await bot.send_message(chat_id, quantities_message(prep), protect_content=True)

        async def delete_later() -> None:
            await asyncio.sleep(self.ttl)
            with contextlib.suppress(Exception):  # удаление не гарантирует стирания на серверах Telegram (§13.1)
                await bot.delete_message(chat_id, msg.message_id)

        asyncio.get_running_loop().create_task(delete_later())


def parse_amount(raw: str) -> Decimal | None:
    """«50000», «50 000», «50к», «1,5k», «50000₽» → Decimal; мусор и неположительные — None."""
    s = raw.replace("\u00a0", "").replace(" ", "").replace(",", ".").lower().removesuffix("₽")
    mult = Decimal(1)
    if s.endswith(("k", "к")):
        s, mult = s[:-1], Decimal(1000)
    try:
        v = Decimal(s) * mult
    except InvalidOperation:
        return None
    return v if v.is_finite() and v > 0 else None


def build_report_router(center: ReportCenter) -> Router:
    router = Router(name="report")

    @router.message(Command("report"))
    async def report(message: Message) -> None:
        if message.bot is None:
            return
        await message.answer("Собираю план по текущему портфелю…")
        try:
            await center.send_weekly(message.bot, message.chat.id, scheduled=False)
        except Exception as e:
            log.exception("report.failed")
            await message.answer(f"Не удалось построить отчёт: {escape(type(e).__name__)}. Подробности — /status")

    @router.message(Command("invest"))
    async def invest(message: Message, command: CommandObject) -> None:
        args = (command.args or "").split()
        amount = parse_amount(args[0]) if args else None
        if amount is None:
            await message.answer("Укажите сумму: <code>/invest 50000</code> или <code>/invest 50к ИИС</code>")
            return
        try:
            prep = await center.service.invest(amount, " ".join(args[1:]) or None, center.today())
        except LookupError as e:
            await message.answer(escape(str(e)))
            return
        center.plans[prep.plan_id] = prep
        center.decided[prep.plan_id] = {}
        await message.answer(invest_message(prep, amount, center.privacy_mode),
                             reply_markup=actions_keyboard(prep.plan_id, prep.plan, {}))

    @router.callback_query(F.data == "rp:noop")
    async def noop(cb: CallbackQuery) -> None:
        await cb.answer()

    @router.callback_query(F.data.startswith("rp:"))
    async def decide(cb: CallbackQuery) -> None:
        parts = (cb.data or "").split(":")
        plan_id = parts[1]
        prep = center.plans.get(plan_id)
        if prep is None:
            await cb.answer("План устарел — сформируйте новый: /report", show_alert=True)
            return
        if parts[2] == "qty":
            await cb.answer()
            if cb.bot is not None and cb.message is not None:
                await center.send_quantities(cb.bot, cb.message.chat.id, plan_id)
            return
        if parts[2] == "wi":
            await cb.answer("Пересчитываю…")
            if cb.bot is not None and cb.message is not None:
                await center.send_whatif(cb.bot, cb.message.chat.id, no_sells=parts[3] == "nosell")
            return
        decision = parts[3]
        decided = center.decided.setdefault(plan_id, {})
        if parts[2] == "all":
            for a in prep.plan.actions if prep.plan else ():
                if a.seq not in decided:
                    decided[a.seq] = decision
            await center.service.record_decision(plan_id, None, decision)
        else:
            seq = int(parts[2])
            decided[seq] = decision
            await center.service.record_decision(plan_id, seq, decision)
        await cb.answer(f"Записано: {DECISION_TEXT[decision]}. Сделку совершаете вы в приложении брокера.")
        if isinstance(cb.message, Message):
            with contextlib.suppress(Exception):
                await cb.message.edit_reply_markup(reply_markup=actions_keyboard(plan_id, prep.plan, decided))

    return router
