"""Writer (§9.2): текст недельного отчёта по FactSheet. Числа — только плейсхолдерами (G4), рендерит код.

Цикл: черновик LLM → `validate_draft` → рендер → `validate_rendered` (numeric grounding) → HTML-экранирование.
При ошибках — перегенерация с перечнем ошибок (не больше двух, всего три попытки). Не справилась или LLM
недоступна — `html=None`: вызывающий код показывает шаблонный отчёт (уровень L2).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import Literal

import structlog
import yaml
from pydantic import Field

from investbot.core.models import Strict
from investbot.llm_roles.base import JsonLLM, Message
from investbot.llm_roles.validator import format_fact, render, validate_draft, validate_rendered
from investbot.reports.factsheet import FactSheet

log = structlog.get_logger(__name__)

ROLE = "writer"
OUTPUT_SCHEMA = "ReportText"
MAX_ATTEMPTS = 3  # первая попытка + две перегенерации (§9.6 G4)
DEFAULT_PROMPT_PATH = Path(__file__).resolve().parents[3] / "prompts" / "writer.v1.md"

SectionKind = Literal["summary", "actions_why", "risks", "ideas", "quality"]
SECTION_ORDER: tuple[SectionKind, ...] = ("summary", "actions_why", "risks", "ideas", "quality")
SECTION_TITLES: dict[str, str | None] = {
    "summary": None,
    "actions_why": "Почему такие действия",
    "risks": "Риски и издержки",
    "ideas": "Идеи для входа",
    "quality": "Качество данных",
}


class ReportSection(Strict):
    kind: SectionKind
    text: str = Field(min_length=1, max_length=1500)  # числа и названия бумаг только как {{f:ID}} и {{i:ID}}
    used_fact_ids: list[str] = Field(default_factory=list, max_length=100)


class ReportText(Strict):
    sections: list[ReportSection] = Field(min_length=1, max_length=len(SECTION_ORDER))


class WriterResult(Strict):
    html: str | None  # None — уходим в шаблон (L2)
    attempts: int
    errors: list[str] = Field(default_factory=list)  # ошибки отклонённых попыток: «попытка N: …»
    report: ReportText | None = None  # принятый черновик (для журнала: used_fact_ids)
    prompt_id: str = ""


@dataclass(frozen=True)
class Prompt:
    id: str
    version: int
    role: str
    output: str
    sha256: str  # хэш файла целиком, считается при загрузке
    body: str

    @property
    def prompt_id(self) -> str:
        """Версия промпта для llm_calls и журнала: «writer.v1:1a2b3c4d5e6f»."""
        return f"{self.id}.v{self.version}:{self.sha256[:12]}"


_FRONT_MATTER = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n(.*)\Z", re.S)


def load_prompt(path: Path) -> Prompt:
    raw = path.read_bytes()
    m = _FRONT_MATTER.match(raw.decode("utf-8"))
    if m is None:
        raise ValueError(f"{path}: нет front-matter")
    meta = yaml.safe_load(m.group(1)) or {}
    missing = [k for k in ("id", "version", "role", "output") if k not in meta]
    if missing:
        raise ValueError(f"{path}: в front-matter нет {missing}")
    return Prompt(id=str(meta["id"]), version=int(meta["version"]), role=str(meta["role"]),
                  output=str(meta["output"]), sha256=hashlib.sha256(raw).hexdigest(), body=m.group(2).strip())


def factsheet_message(fs: FactSheet) -> str:
    """FactSheet для модели: компактный JSON, значения — так, как их увидит пользователь."""
    payload = {
        "task": fs.kind,
        "asof": fs.asof.isoformat() if fs.asof else None,
        "privacy_mode": fs.privacy_mode,
        "facts": [{"id": f.fact_id, "value": format_fact(f), "unit": f.unit} for f in fs.facts.values()],
        "names": fs.instruments,
        "links": fs.links,
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def feedback_message(errors: list[str]) -> str:
    lines = "\n".join(f"- {e}" for e in errors)
    return ("Черновик отклонён проверкой. Ошибки:\n" f"{lines}\n"
            "Исправь все ошибки и верни JSON целиком заново. Напоминание: числа, проценты и даты — только "
            "{{f:ID}}, названия бумаг и классов — {{i:ID}}; вне плейсхолдеров нет цифр и числительных словами.")


def check_report(report: ReportText, fs: FactSheet, max_chars: int) -> tuple[str | None, list[str]]:
    """Все проверки черновика и рендер в HTML. (html, []) — принято; (None, ошибки) — на перегенерацию."""
    errs: list[str] = []
    kinds = [s.kind for s in report.sections]
    if dup := sorted({k for k in kinds if kinds.count(k) > 1}):
        errs.append(f"разделы повторяются: {dup} — каждый kind не больше одного раза")
    for s in report.sections:
        errs.extend(f"[{s.kind}] {e}" for e in validate_draft(s.text, fs.facts, fs.instruments))
        if unknown := [f for f in s.used_fact_ids if f not in fs.facts]:
            errs.append(f"[{s.kind}] used_fact_ids содержит id, которых нет в facts: {unknown}")
        if not s.text.strip():
            errs.append(f"[{s.kind}] пустой текст")
    if errs:
        return None, errs
    parts: list[str] = []
    for s in sorted(report.sections, key=lambda x: SECTION_ORDER.index(x.kind)):
        rendered, spans = render(s.text, fs.facts, fs.instruments)
        if bad := validate_rendered(rendered, spans, fs.facts):
            errs.extend(f"[{s.kind}] после подстановки: {e}" for e in bad)
            continue
        body = escape(rendered.strip(), quote=False)  # подставленные названия экранируются вместе с текстом
        heading = SECTION_TITLES[s.kind]
        parts.append(f"<b>{heading}</b>\n{body}" if heading else body)
    if errs:
        return None, errs
    html = "\n\n".join(parts)
    if len(html) > max_chars:
        return None, [f"текст слишком длинный: {len(html)} символов при лимите {max_chars} — сократи"]
    return html, []


class Writer:
    def __init__(self, llm: JsonLLM, prompt_path: Path = DEFAULT_PROMPT_PATH, *, max_chars: int = 3000,
                 max_tokens: int = 3000) -> None:
        self._llm = llm
        self.prompt = load_prompt(prompt_path)
        if self.prompt.role != ROLE or self.prompt.output != OUTPUT_SCHEMA:
            raise ValueError(f"{prompt_path}: промпт роли {self.prompt.role}/{self.prompt.output}, "
                             f"ожидался {ROLE}/{OUTPUT_SCHEMA}")
        self._max_chars = max_chars
        self._max_tokens = max_tokens

    async def write(self, fs: FactSheet) -> WriterResult:
        base: list[Message] = [{"role": "system", "content": self.prompt.body},
                               {"role": "user", "content": factsheet_message(fs)}]
        messages = list(base)
        errors: list[str] = []
        pid = self.prompt.prompt_id
        for attempt in range(1, MAX_ATTEMPTS + 1):
            out = await self._llm.complete_json(ROLE, messages, ReportText, has_portfolio_ctx=True,
                                                prompt_id=pid, max_tokens=self._max_tokens)
            if out is None:  # провайдеры недоступны или бюджет исчерпан — сразу шаблон
                errors.append(f"попытка {attempt}: LLM недоступна")
                log.info("writer.llm_unavailable", attempt=attempt)
                return WriterResult(html=None, attempts=attempt, errors=errors, prompt_id=pid)
            html, errs = check_report(out, fs, self._max_chars)
            if html is not None:
                log.info("writer.ok", attempt=attempt, sections=len(out.sections))
                return WriterResult(html=html, attempts=attempt, errors=errors, report=out, prompt_id=pid)
            errors.extend(f"попытка {attempt}: {e}" for e in errs)
            log.info("writer.rejected", attempt=attempt, errors=len(errs))
            messages = [*base, {"role": "assistant", "content": out.model_dump_json()},
                        {"role": "user", "content": feedback_message(errs)}]
        log.warning("writer.gave_up", attempts=MAX_ATTEMPTS)
        return WriterResult(html=None, attempts=MAX_ATTEMPTS, errors=errors, prompt_id=pid)
