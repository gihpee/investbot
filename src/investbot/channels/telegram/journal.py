"""`/journal` (§13.2): качество рекомендаций по бумажному исполнению (scorecard, §15.4–15.5).

Приватность (N4): только количества и проценты — никаких рублёвых сумм. Пока исходов меньше MIN_N,
группа показывается как «мало данных»; при достаточном n — с 90% ДИ среднего и пометкой
«статистически не подтверждено», пока ДИ включает 0.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from html import escape
from typing import Protocol

import structlog
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from investbot.channels.telegram.render import DISCLAIMER
from investbot.core.money import pct, pp
from investbot.journal.paper import JOURNAL_HORIZONS, MIN_N, VERDICT_TEXT, Scorecard

log = structlog.get_logger(__name__)

KIND_TITLES: dict[str | None, str] = {
    "rebalance_action": "Действия ребалансировки",
    "entry_idea": "Показанные идеи",
    "idea_candidate": "Непоказанные кандидаты",
    "candidates": "Все кандидаты топ-20",
    None: "Все рекомендации",
}
ARM_TITLES = {"bond_carry_roll": "облигации: carry + roll-down"}
EXPLAIN = ("<i>Избыточная доходность против бенчмарка класса на бумаге, без учёта налогов и комиссий; "
           "для продаж знак обратный. Попадание — исход лучше бенчмарка.</i>")


def _num(x: Decimal) -> str:
    """Доля → «+1,2» (п.п. без подписи) — для интервалов."""
    return pp(x).removesuffix(" п.п.")


def _group_title(card: Scorecard) -> str:
    title = KIND_TITLES.get(card.kind, card.kind or "")
    if card.source_arm is not None:
        title = f"{title} · {ARM_TITLES.get(card.source_arm, card.source_arm)}"
    return escape(title)


def _line(card: Scorecard) -> str:
    head = f"{card.horizon_weeks} нед.:"
    if not card.enough or card.mean_excess is None or card.hit_rate is None:
        return f"{head} мало данных (исходов {card.n}, нужно от {MIN_N})"
    parts = [f"n={card.n}", f"попадания {pct(card.hit_rate)}", f"среднее {pp(card.mean_excess)}"]
    if card.ci_low is not None and card.ci_high is not None:
        parts[-1] += f" (ДИ {pct(card.ci_level, 0)}: {_num(card.ci_low)}…{_num(card.ci_high)})"
    if card.median_excess is not None:
        parts.append(f"медиана {pp(card.median_excess)}")
    if card.worst_excess is not None:
        parts.append(f"худшее {pp(card.worst_excess)}")
    return f"{head} {' · '.join(parts)} — {VERDICT_TEXT[card.verdict]}"


def scorecard_message(cards: Sequence[Scorecard]) -> str:
    """Карточки группируются по (вид, источник) в порядке появления; внутри — строки по горизонтам."""
    lines = ["<b>📒 Журнал: качество рекомендаций</b>", EXPLAIN]
    if not cards or all(c.registered == 0 for c in cards):
        lines += ["", "Пока нечего оценивать: рекомендации попадут сюда после первого недельного отчёта "
                      "и ленты идей, исходы — через неделю и позже."]
        lines += ["", DISCLAIMER]
        return "\n".join(lines)
    groups: dict[tuple[str | None, str | None], list[Scorecard]] = {}
    for c in cards:
        groups.setdefault((c.kind, c.source_arm), []).append(c)
    for group in groups.values():
        first = group[0]
        lines += ["", f"<b>{_group_title(first)}</b> · всего {first.registered}"]
        if first.registered == 0:
            lines.append("пока нет")
            continue
        lines += [_line(c) for c in sorted(group, key=lambda c: c.horizon_weeks)]
    lines += ["", f"<i>Пока исходов меньше {MIN_N} — «мало данных». Вывод о качестве — только когда "
                  "ДИ не включает 0.</i>", DISCLAIMER]
    return "\n".join(lines)


class ScorecardsSource(Protocol):
    async def scorecards(self, horizons: Sequence[int] = JOURNAL_HORIZONS) -> list[Scorecard]: ...


def build_journal_router(service: ScorecardsSource, horizons: Sequence[int] = JOURNAL_HORIZONS) -> Router:
    router = Router(name="journal")

    @router.message(Command("journal"))
    async def journal(message: Message) -> None:
        try:
            cards = await service.scorecards(horizons)
        except Exception as e:
            log.exception("journal.scorecard_failed")
            await message.answer(f"Не удалось собрать scorecard: {escape(type(e).__name__)}. Подробности — /status")
            return
        await message.answer(scorecard_message(cards))

    return router
