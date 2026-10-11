"""Закрытая таксономия событий новостей (§8.2 п. 5): config/taxonomy.yaml, версия 1.

Набор кодов зафиксирован в коде (`EventType`, `EVENT_TYPES_V1`): он входит в JSON-схему выхода Tagger и в
`news_annotation.event_type`. YAML даёт подписи и определения для промпта и рендера; загрузчик требует, чтобы
набор кодов в файле совпадал с кодом ровно, — расхождение означает новую версию таксономии, а не правку конфига.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any, Literal, get_args

import yaml
from pydantic import Field, model_validator

from investbot.core.models import Strict

TAXONOMY_VERSION = 1

EventType = Literal[
    # корпоративные
    "dividend_announce", "dividend_cut", "buyback", "spo_ipo", "m_and_a", "reporting", "guidance", "management",
    # регуляторные
    "sanctions", "regulatory_fine", "delisting_marking",
    # кредитные
    "rating_up", "rating_down", "rating_watch", "rating_withdraw", "tech_default", "default", "offer_put_call",
    "coupon_reset", "covenant",
    # макро и ЦФА
    "key_rate", "macro", "dfa_issue", "dfa_default",
    "other",
]
EVENT_TYPES_V1: tuple[str, ...] = get_args(EventType)
EventGroup = Literal["corporate", "regulatory", "credit", "macro_dfa", "other"]


class EventDef(Strict):
    code: EventType
    group: EventGroup
    title: str = Field(min_length=1, max_length=60)  # короткая подпись для пользователя
    description: str = Field(min_length=1, max_length=400)  # определение для промпта Tagger


class Taxonomy(Strict):
    version: int
    events: tuple[EventDef, ...]

    @model_validator(mode="after")
    def _closed(self) -> Taxonomy:
        if self.version != TAXONOMY_VERSION:
            raise ValueError(f"таксономия версии {self.version}, код знает только версию {TAXONOMY_VERSION}")
        dups = sorted(c for c, n in Counter(e.code for e in self.events).items() if n > 1)
        if dups:
            raise ValueError(f"повторяющиеся коды событий: {dups}")
        missing = sorted(set(EVENT_TYPES_V1) - {e.code for e in self.events})
        if missing:
            raise ValueError(f"в таксономии нет событий: {missing}")
        return self

    @property
    def codes(self) -> tuple[str, ...]:
        return tuple(e.code for e in self.events)

    def title(self, code: str) -> str:
        """Подпись события; неизвестный код показывается как есть."""
        return next((e.title for e in self.events if e.code == code), code)

    def titles(self) -> dict[str, str]:
        return {e.code: e.title for e in self.events}

    def prompt_block(self) -> str:
        """Список «код — определение» для системного промпта Tagger."""
        return "\n".join(f"- `{e.code}` — {e.description}" for e in self.events)


def load_taxonomy(path: Path | str) -> Taxonomy:
    raw: Any = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return Taxonomy.model_validate(raw)
