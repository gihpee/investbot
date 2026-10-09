"""Анкета профиля для конструктора аллокации (§10.8): короткие вопросы с вариантами ответов."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field

from investbot.core.models import Strict


@dataclass(frozen=True)
class Question:
    key: str
    text: str
    options: tuple[tuple[str, str], ...]  # (код ответа, подпись кнопки)


QUESTIONS: tuple[Question, ...] = (
    Question("horizon", "На какой срок вы инвестируете основную часть денег?",
             (("1", "до 1 года"), ("3", "1–3 года"), ("5", "3–5 лет"), ("10", "больше 5 лет"))),
    Question("max_drawdown", "Какую временную просадку портфеля вы готовы пережить, не продавая в панике?",
             (("0.10", "до −10%"), ("0.15", "до −15%"), ("0.20", "до −20%"), ("0.30", "до −30%"))),
    Question("needs_income", "Нужен ли регулярный денежный поток (купоны, дивиденды) на расходы?",
             (("yes", "да"), ("no", "нет, всё реинвестирую"))),
    Question("dfa_share", "Какую долю держать в ЦФА? (они не участвуют в оптимизации, это ваш параметр)",
             (("0", "0%"), ("0.05", "5%"), ("0.10", "10%"))),
    Question("allow_hy", "Допускать высокодоходные облигации (рейтинг ниже A-)?",
             (("no", "нет"), ("yes", "да, до 5%"))),
    Question("gold_share", "Какую долю держать в золоте? (как и ЦФА — ваш параметр, без оптимизации)",
             (("0", "0%"), ("0.05", "5%"), ("0.10", "10%"))),
)


class ProfileAnswers(Strict):
    horizon_years: int = Field(ge=1)
    max_drawdown: Decimal = Field(gt=0, lt=1)
    needs_income: bool
    dfa_share: Decimal = Field(ge=0, le=Decimal("0.5"))
    allow_hy: bool
    gold_share: Decimal = Field(ge=0, le=Decimal("0.5"))

    @classmethod
    def from_codes(cls, codes: dict[str, str]) -> ProfileAnswers:
        return cls(
            horizon_years=int(codes["horizon"]),
            max_drawdown=Decimal(codes["max_drawdown"]),
            needs_income=codes["needs_income"] == "yes",
            dfa_share=Decimal(codes["dfa_share"]),
            allow_hy=codes["allow_hy"] == "yes",
            gold_share=Decimal(codes["gold_share"]),
        )


def load_answers(path: Path) -> ProfileAnswers | None:
    if not path.exists():
        return None
    raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    return ProfileAnswers.model_validate(raw)


def save_answers(answers: ProfileAnswers, path: Path) -> None:
    data = {k: (str(v) if isinstance(v, Decimal) else v) for k, v in answers.model_dump().items()}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
