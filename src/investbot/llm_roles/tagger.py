"""Tagger (§8.2 п. 6, §9.2): LLM-разметка одной новости по закрытой таксономии событий в `NewsAnnotationOut`.

Безопасность:
- G7 (prompt injection): у роли нет инструментов (front-matter промпта `tools: []` проверяется при загрузке),
  текст новости передаётся только внутри `untrusted(...)`, ответ — строгая схема. Сущности вне списка кандидатов
  отбрасываются и пишутся в лог как аномалия.
- G8 (приватность): в промпт попадают только публичный текст новости и кандидаты — тикер и название из
  справочника. Ни весов, ни позиций, ни сумм; поэтому `has_portfolio_ctx=False`. Кандидатами передаются все
  бумаги, связанные с новостью по справочнику, а не только портфельные: по списку не видно, что держит пользователь.

Пост-валидация: диапазоны зажимаются (тон −1…+1, relevance и confidence 0…1, magnitude 0…3), незнакомый код
события становится `other`, резюме обрезается до 300 символов. Регулярное выражение по маркировке рекламы (erid,
«#реклама», «на правах рекламы», строка «Реклама.») может только поднять `is_ad` до True, но не снять его.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal

import structlog
import yaml
from pydantic import Field, field_validator

from investbot.core.models import Strict
from investbot.llm_roles.base import JsonLLM, Message, untrusted
from investbot.news.taxonomy import EVENT_TYPES_V1, EventType, Taxonomy

log = structlog.get_logger(__name__)

ROLE = "tagger"
MAX_INPUT_CHARS = 2500  # §9.2: ≤ 2 500 символов входа
MAX_SUMMARY_CHARS = 300
MAX_NAME_CHARS = 80
MAX_OUTPUT_TOKENS = 800
TAXONOMY_PLACEHOLDER = "{{TAXONOMY}}"
_STEP = Decimal("0.001")

Surprise = Literal["low", "medium", "high"]

# Маркировка рекламы (38-ФЗ «О рекламе»): токен erid, хэштег, «на правах рекламы», пометка «Реклама.» в начале строки.
# Слово «реклама» внутри текста не считается: новости о рекламном рынке («выручка от рекламы») — не реклама.
_AD_RE = re.compile(
    r"\berid\b"
    r"|#\s*реклама"
    r"|\bна\s+правах\s+рекламы\b"
    r"|^\s*реклама\s*[.:|]",
    re.IGNORECASE | re.MULTILINE,
)


def looks_like_ad(*texts: str) -> bool:
    return any(_AD_RE.search(t) for t in texts if t)


def _clamp(v: Any, lo: Decimal, hi: Decimal) -> Decimal:
    if isinstance(v, bool):
        raise ValueError("ожидалось число, получено логическое значение")
    try:
        d = v if isinstance(v, Decimal) else Decimal(str(v).strip())
    except (InvalidOperation, ValueError) as e:
        raise ValueError(f"не число: {v!r}") from e
    if not d.is_finite():
        raise ValueError(f"не конечное число: {v!r}")
    return min(max(d, lo), hi).quantize(_STEP, rounding=ROUND_HALF_EVEN)


def _norm_ticker(t: str) -> str:
    return t.strip().upper()


class EntityTone(Strict):
    ticker: str = Field(min_length=1, max_length=32)
    tone: Decimal = Field(ge=-1, le=1)  # к этой бумаге: −1 негативно … +1 позитивно
    relevance: Decimal = Field(ge=0, le=1)  # насколько новость об этой бумаге

    @field_validator("ticker", mode="before")
    @classmethod
    def _ticker(cls, v: Any) -> Any:
        return v.strip() if isinstance(v, str) else v

    @field_validator("tone", mode="before")
    @classmethod
    def _tone(cls, v: Any) -> Decimal:
        return _clamp(v, Decimal(-1), Decimal(1))

    @field_validator("relevance", mode="before")
    @classmethod
    def _relevance(cls, v: Any) -> Decimal:
        return _clamp(v, Decimal(0), Decimal(1))


class NewsAnnotationOut(Strict):
    """Выход Tagger (`NewsAnnotation` §8.2 п. 6). Схема строгая (extra=forbid), диапазоны зажимаются при разборе."""

    event_type: EventType
    entities: list[EntityTone]
    confidence: Decimal = Field(ge=0, le=1)
    surprise: Surprise
    magnitude: int = Field(ge=0, le=3)
    is_ad: bool
    is_rumor: bool
    summary: str = Field(max_length=MAX_SUMMARY_CHARS)

    @field_validator("event_type", mode="before")
    @classmethod
    def _event(cls, v: Any) -> Any:
        if isinstance(v, str):
            code = v.strip().lower()
            return code if code in EVENT_TYPES_V1 else "other"  # таксономия закрытая: незнакомое — «прочее»
        return v

    @field_validator("confidence", mode="before")
    @classmethod
    def _confidence(cls, v: Any) -> Decimal:
        return _clamp(v, Decimal(0), Decimal(1))

    @field_validator("surprise", mode="before")
    @classmethod
    def _surprise(cls, v: Any) -> Any:
        return v.strip().lower() if isinstance(v, str) else v

    @field_validator("magnitude", mode="before")
    @classmethod
    def _magnitude(cls, v: Any) -> int:
        return int(_clamp(v, Decimal(0), Decimal(3)).to_integral_value(rounding=ROUND_HALF_EVEN))

    @field_validator("summary", mode="before")
    @classmethod
    def _summary(cls, v: Any) -> Any:
        if not isinstance(v, str):
            return v
        s = " ".join(v.split())
        return s if len(s) <= MAX_SUMMARY_CHARS else s[: MAX_SUMMARY_CHARS - 1].rstrip() + "…"


@dataclass(frozen=True)
class PromptFile:
    id: str
    version: int
    role: str
    body: str
    max_input_chars: int


def read_prompt(path: Path | str) -> PromptFile:
    """`prompts/<role>.v<N>.md`: YAML front-matter между строками `---` и тело промпта (§9.5)."""
    text = Path(path).read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise ValueError(f"{path}: нет front-matter")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise ValueError(f"{path}: front-matter не закрыт")
    meta: Any = yaml.safe_load(text[4:end])
    if not isinstance(meta, dict):
        raise ValueError(f"{path}: front-matter должен быть словарём")
    try:
        pid, version, role = str(meta["id"]), int(meta["version"]), str(meta["role"])
    except (KeyError, TypeError, ValueError) as e:
        raise ValueError(f"{path}: в front-matter нужны id, version и role") from e
    if meta.get("tools"):
        raise ValueError(f"{path}: у роли {role} не должно быть инструментов (G7)")
    return PromptFile(id=pid, version=version, role=role, body=text[end + 5:],
                      max_input_chars=int(meta.get("max_input_chars", MAX_INPUT_CHARS)))


def _clean_line(s: str, limit: int) -> str:
    """Тикер или название из справочника — одна строка без угловых скобок (не спутать с разметкой блока)."""
    return " ".join(s.replace("<", " ").replace(">", " ").split())[:limit]


class Tagger:
    def __init__(self, llm: JsonLLM, prompt_path: Path | str, taxonomy: Taxonomy) -> None:
        prompt = read_prompt(prompt_path)
        if prompt.role != ROLE:
            raise ValueError(f"промпт роли {prompt.role!r}, ожидалась {ROLE!r}")
        if TAXONOMY_PLACEHOLDER not in prompt.body:
            raise ValueError(f"в промпте нет места для таксономии {TAXONOMY_PLACEHOLDER}")
        self._llm = llm
        self.taxonomy = taxonomy
        self.system = prompt.body.replace(TAXONOMY_PLACEHOLDER, taxonomy.prompt_block()).strip()
        # хэш — от того, что реально уходит модели: тело промпта вместе с подставленной таксономией (G10)
        self.prompt_sha256 = hashlib.sha256(self.system.encode("utf-8")).hexdigest()
        self.prompt_id = f"{prompt.id}.v{prompt.version}#{self.prompt_sha256[:12]}"
        self.max_input_chars = prompt.max_input_chars

    def messages(self, news_id: int | str, title: str, lead: str, source: str,
                 candidates: Sequence[tuple[str, str | None]]) -> list[Message]:
        lines = []
        for ticker, name in candidates:
            t = _clean_line(ticker, 32)
            n = _clean_line(name, MAX_NAME_CHARS) if name else ""
            lines.append(f"- {t} — {n}" if n else f"- {t}")
        text = title.strip()
        if lead.strip():
            text = f"{text}\n\n{lead.strip()}"
        text = text[: self.max_input_chars]
        user = ("Кандидаты (тон и relevance — только к ним, тикер ровно как в списке):\n" + "\n".join(lines)
                + "\n\nНовость:\n" + untrusted(source, str(news_id), text))
        return [{"role": "system", "content": self.system}, {"role": "user", "content": user}]

    async def annotate(self, news_id: int | str, title: str, lead: str, source: str,
                       candidates: Sequence[tuple[str, str | None]]) -> NewsAnnotationOut | None:
        """Разметка новости или None, если LLM недоступна или бюджет исчерпан (вызывающий код уходит в L2)."""
        allowed = {_norm_ticker(t): t for t, _ in candidates}
        if not allowed:
            raise ValueError("Tagger: пустой список кандидатов")
        out = await self._llm.complete_json(
            ROLE, self.messages(news_id, title, lead, source, candidates), NewsAnnotationOut,
            has_portfolio_ctx=False, prompt_id=self.prompt_id, max_tokens=MAX_OUTPUT_TOKENS,
        )
        if out is None:
            return None
        return self.postprocess(out, allowed, ad_hint=looks_like_ad(title, lead), news_id=news_id)

    @staticmethod
    def postprocess(out: NewsAnnotationOut, allowed: dict[str, str], *, ad_hint: bool,
                    news_id: int | str | None = None) -> NewsAnnotationOut:
        """Только кандидаты (в их написании и порядке), одна запись на тикер (с наибольшей relevance),
        зажатые диапазоны, is_ad поднимается по маркировке рекламы."""
        best: dict[str, EntityTone] = {}
        foreign: list[str] = []
        for e in out.entities:
            key = _norm_ticker(e.ticker)
            if key not in allowed:
                foreign.append(e.ticker)
                continue
            if key not in best or e.relevance > best[key].relevance:
                best[key] = e
        if foreign:
            log.warning("tagger.foreign_entities", news_id=news_id, tickers=foreign[:10])  # G7: аномалия
        order = list(allowed)
        entities = [
            {"ticker": allowed[k], "tone": best[k].tone, "relevance": best[k].relevance}
            for k in sorted(best, key=order.index)
        ]
        data = out.model_dump()
        data.update(entities=entities, is_ad=out.is_ad or ad_hint)
        return NewsAnnotationOut.model_validate(data)
