"""Конструктор аллокации в Telegram (§10.8): анкета → варианты A/B/C → правки кнопками → утверждение.

Свободный текст (LLM-роль AllocationAdvisor) появится с MVP-2; здесь — только детерминированные кнопки.
Состояние диалога держится в памяти: бот однопользовательский (deployment_mode=personal).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from html import escape
from typing import Protocol

import structlog
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from investbot.allocation.models import AllocationEdit, AllocationVariant, DraftDiff, VariantMetrics
from investbot.channels.telegram.render import DISCLAIMER, title
from investbot.core.money import pct, pp
from investbot.rebalance.ips import IPS
from investbot.services.questionnaire import QUESTIONS, ProfileAnswers

log = structlog.get_logger(__name__)

STEP = Decimal("0.05")
BTN_ALLOCATION = "🎯 Аллокация"


class AllocationServiceLike(Protocol):
    def answers(self) -> ProfileAnswers | None: ...

    async def save_answers(self, answers: ProfileAnswers) -> None: ...

    async def propose(self, today: date) -> list[AllocationVariant]: ...

    async def edit(self, weights: Mapping[str, Decimal], edit: AllocationEdit, today: date) -> DraftDiff: ...

    async def approve(self, weights: Mapping[str, Decimal], source_variant: str | None) -> IPS: ...


@dataclass
class Session:
    codes: dict[str, str] = field(default_factory=dict)
    variants: dict[str, AllocationVariant] = field(default_factory=dict)
    draft: dict[str, Decimal] | None = None
    draft_metrics: VariantMetrics | None = None
    draft_violations: tuple[str, ...] = ()
    source: str | None = None


def kb(rows: Sequence[Sequence[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows]
    )


def question_view(i: int) -> tuple[str, InlineKeyboardMarkup]:
    q = QUESTIONS[i]
    text = f"<b>Анкета · вопрос {i + 1} из {len(QUESTIONS)}</b>\n\n{escape(q.text)}"
    return text, kb([[(label, f"al:q:{i}:{code}") for code, label in q.options]])


def _metrics_lines(m: VariantMetrics) -> list[str]:
    lines = [f"Волатильность: {pct(m.vol_annual)} в год · худшая просадка: {pct(m.max_drawdown_hist)}"]
    for name, r in m.stress.items():
        lines.append(f"• {escape(name)}: {pct(r)}")
    if m.current_yield is not None:
        lines.append(f"Текущая доходность: {pct(m.current_yield)} годовых (не прогноз)")
    lines.append(f"Переход от текущего портфеля: оборот {pct(m.turnover_from_current)}")
    return lines


def variants_view(variants: Sequence[AllocationVariant]) -> tuple[str, InlineKeyboardMarkup]:
    buckets = sorted({b for v in variants for b, w in v.weights.items() if w > 0},
                     key=lambda b: -max(v.weights.get(b, Decimal(0)) for v in variants))
    lines = ["<b>🎯 Варианты аллокации</b>", "<i>Метрики посчитаны на истории индексов, это не прогноз.</i>", ""]
    rows = [f"{'Класс':<24}" + "".join(f"{v.code:>6}" for v in variants)]
    for b in buckets:
        rows.append(f"{title(b)[:24]:<24}" + "".join(f"{pct(v.weights.get(b, Decimal(0)), 0):>6}" for v in variants))
    lines.append("<pre>" + escape("\n".join(rows)) + "</pre>")
    # примечания, общие для всех вариантов, показываем один раз внизу
    common = set.intersection(*(set(v.notes) for v in variants)) if variants else set()
    for v in variants:
        lines.append(f"<b>{v.code}. {escape(v.title)}</b>")
        lines.extend(_metrics_lines(v.metrics))
        lines.extend(f"⚠️ {escape(x)}" for x in v.violations)
        lines.extend(f"ℹ️ {escape(x)}" for x in v.notes if x not in common and not x.startswith("Оборот перехода"))
        lines.append("")
    if common:
        lines.append("<b>Примечания</b>")
        lines.extend(f"• {escape(x)}" for x in variants[0].notes if x in common)
        lines.append("")
    lines.append("Выберите вариант — его можно будет поправить перед утверждением.")
    lines.append(DISCLAIMER)
    buttons = [[(f"Взять {v.code}", f"al:v:{v.code}") for v in variants], [("📝 Анкета заново", "al:restart")]]
    return "\n".join(lines), kb(buttons)


def draft_view(s: Session, diff: DraftDiff | None = None) -> tuple[str, InlineKeyboardMarkup]:
    if s.draft is None:
        raise ValueError("черновика нет")
    lines = [f"<b>✏️ Черновик аллокации</b> (основа — вариант {s.source})", ""]
    for b, w in sorted(s.draft.items(), key=lambda kv: -kv[1]):
        if w <= 0 and not (diff and diff.before.get(b, Decimal(0)) > 0):
            continue
        change = ""
        if diff is not None:
            delta = w - diff.before.get(b, Decimal(0))
            change = f" ({pp(delta)})" if delta != 0 else ""
        lines.append(f"• {escape(title(b))}: {pct(w)}{change}")
    if s.draft_metrics is not None:
        lines.append("")
        lines.extend(_metrics_lines(s.draft_metrics))
    if s.draft_violations:
        lines.append("")
        lines.extend(f"⚠️ {escape(x)}" for x in s.draft_violations)
    lines.append("")
    lines.append(DISCLAIMER)
    buttons = [
        [("🛡 Меньше риска", "al:e:less"), ("🚀 Больше риска", "al:e:more")],
        [("± 5 п.п. к классу", "al:pick:shift"), ("🚫 Исключить класс", "al:pick:excl")],
        [("❓ Почему так?", "al:why"), ("↩️ К вариантам", "al:back")],
        [("✅ Утвердить", "al:approve")],
    ]
    return "\n".join(lines), kb(buttons)


WHY = {
    "A": "Вариант A минимально меняет текущий портфель: он только приводит доли к ограничениям анкеты "
         "(допустимая просадка в стресс-сценариях, исключённые и фиксированные классы).",
    "B": "Вариант B распределяет риск поровну: каждый класс вносит одинаковый вклад в колебания портфеля. "
         "Прогнозы доходности не используются — только историческая волатильность и корреляции.",
    "C": "Вариант C даёт максимум растущим классам (акции, корпоративные облигации), пока убыток в каждом "
         "стресс-сценарии не хуже вашей допустимой просадки. Небольшой штраф удерживает его рядом с B.",
}


def build_allocation_router(service: AllocationServiceLike, today: date | None = None) -> Router:
    router = Router(name="allocation")
    s = Session()

    def _today() -> date:
        return today or date.today()

    async def show_variants(message: Message) -> None:
        await message.answer("Считаю варианты на истории индексов MOEX…")
        try:
            variants = await service.propose(_today())
        except Exception as e:
            log.exception("allocation.propose_failed")
            await message.answer(f"Не удалось построить варианты: {escape(str(e))}")
            return
        s.variants = {v.code: v for v in variants}
        text, markup = variants_view(variants)
        await message.answer(text, reply_markup=markup)

    @router.message(Command("allocation"))
    @router.message(F.text == BTN_ALLOCATION)
    async def start(message: Message) -> None:
        if service.answers() is None:
            s.codes.clear()
            text, markup = question_view(0)
            await message.answer("Сначала короткая анкета — по ней бот построит варианты аллокации.")
            await message.answer(text, reply_markup=markup)
            return
        await show_variants(message)

    @router.callback_query(F.data.startswith("al:q:"))
    async def answer(cb: CallbackQuery) -> None:
        _, _, idx, code = (cb.data or "").split(":", 3)
        i = int(idx)
        s.codes[QUESTIONS[i].key] = code
        await cb.answer()
        if not isinstance(cb.message, Message):
            return
        if i + 1 < len(QUESTIONS):
            text, markup = question_view(i + 1)
            await cb.message.edit_text(text, reply_markup=markup)
            return
        await service.save_answers(ProfileAnswers.from_codes(s.codes))
        await cb.message.edit_text("Анкета сохранена ✅")
        await show_variants(cb.message)

    @router.callback_query(F.data == "al:restart")
    async def restart(cb: CallbackQuery) -> None:
        s.codes.clear()
        await cb.answer()
        if isinstance(cb.message, Message):
            text, markup = question_view(0)
            await cb.message.answer(text, reply_markup=markup)

    @router.callback_query(F.data.startswith("al:v:"))
    async def take(cb: CallbackQuery) -> None:
        code = (cb.data or "")[5:]
        v = s.variants.get(code)
        await cb.answer()
        if v is None or not isinstance(cb.message, Message):
            return
        s.draft, s.draft_metrics, s.draft_violations, s.source = dict(v.weights), v.metrics, v.violations, code
        text, markup = draft_view(s)
        await cb.message.answer(text, reply_markup=markup)

    async def run_edit(cb: CallbackQuery, edit: AllocationEdit) -> None:
        await cb.answer("Пересчитываю…")
        if s.draft is None or not isinstance(cb.message, Message):
            return
        diff = await service.edit(s.draft, edit, _today())
        s.draft, s.draft_metrics, s.draft_violations = dict(diff.after), diff.metrics_after, diff.violations
        text, markup = draft_view(s, diff)
        await cb.message.edit_text(text, reply_markup=markup)

    @router.callback_query(F.data.in_({"al:e:less", "al:e:more"}))
    async def risk(cb: CallbackQuery) -> None:
        op = "less_risk" if cb.data == "al:e:less" else "more_risk"
        await run_edit(cb, AllocationEdit(op=op, value=STEP))

    @router.callback_query(F.data.startswith("al:pick:"))
    async def pick(cb: CallbackQuery) -> None:
        mode = (cb.data or "")[8:]
        await cb.answer()
        if s.draft is None or not isinstance(cb.message, Message):
            return
        rows: list[list[tuple[str, str]]] = []
        for b in s.draft:
            if mode == "shift":
                rows.append([(f"{title(b)} +5", f"al:s:{b}:+"), ("−5", f"al:s:{b}:-")])
            else:
                rows.append([(f"🚫 {title(b)}", f"al:x:{b}")])
        rows.append([("↩️ Назад", "al:draft")])
        await cb.message.edit_reply_markup(reply_markup=kb(rows))

    @router.callback_query(F.data.startswith("al:s:"))
    async def shift(cb: CallbackQuery) -> None:
        _, _, bucket, sign = (cb.data or "").split(":")
        await run_edit(cb, AllocationEdit(op="shift_bucket", bucket=bucket, value=STEP if sign == "+" else -STEP))

    @router.callback_query(F.data.startswith("al:x:"))
    async def exclude(cb: CallbackQuery) -> None:
        await run_edit(cb, AllocationEdit(op="exclude", bucket=(cb.data or "")[5:]))

    @router.callback_query(F.data == "al:draft")
    async def back_to_draft(cb: CallbackQuery) -> None:
        await cb.answer()
        if s.draft is not None and isinstance(cb.message, Message):
            text, markup = draft_view(s)
            await cb.message.edit_text(text, reply_markup=markup)

    @router.callback_query(F.data == "al:why")
    async def why(cb: CallbackQuery) -> None:
        await cb.answer()
        if isinstance(cb.message, Message):
            await cb.message.answer(WHY.get(s.source or "", "Это черновик на основе одного из вариантов."))

    @router.callback_query(F.data == "al:back")
    async def back(cb: CallbackQuery) -> None:
        await cb.answer()
        if s.variants and isinstance(cb.message, Message):
            text, markup = variants_view(list(s.variants.values()))
            await cb.message.answer(text, reply_markup=markup)

    @router.callback_query(F.data == "al:approve")
    async def approve(cb: CallbackQuery) -> None:
        await cb.answer()
        if s.draft is None or not isinstance(cb.message, Message):
            return
        warn = "\n⚠️ Есть нарушения ограничений анкеты — проверьте их выше." if s.draft_violations else ""
        await cb.message.answer(
            "Утвердить эту аллокацию как целевую? Еженедельные рекомендации будут считаться от неё. "
            "Ничего не покупается и не продаётся автоматически." + warn,
            reply_markup=kb([[("✅ Да, утвердить", "al:approve!"), ("Отмена", "al:draft")]]),
        )

    @router.callback_query(F.data == "al:approve!")
    async def approve_confirmed(cb: CallbackQuery) -> None:
        await cb.answer()
        if s.draft is None or not isinstance(cb.message, Message):
            return
        ips = await service.approve(s.draft, s.source)
        await cb.message.edit_text(
            f"✅ Целевая аллокация утверждена (IPS версия {ips.version}).\n"
            "Теперь /portfolio показывает отклонения от неё, а недельный отчёт будет предлагать ребалансировку.\n"
            "Пересмотреть можно в любой момент: /allocation"
        )
        s.variants.clear()
        s.draft = None

    return router
