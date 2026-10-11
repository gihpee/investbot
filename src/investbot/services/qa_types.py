"""Типы Q&A без зависимостей от адаптеров — чтобы каналы не тянули LLM-роутер (контракт .importlinter)."""

from __future__ import annotations

from investbot.core.models import Strict


class QaResult(Strict):
    html: str | None
    reason: str = ""
    attempts: int = 0
