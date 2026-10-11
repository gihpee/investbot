"""Контракт между LLM-ролями и роутером провайдеров (§9).

Роль формирует сообщения и схему выхода; роутер выбирает провайдера и модель по цепочке роли, соблюдает
резидентность (портфельный контекст — только моделям с данными в РФ), бюджет и circuit breaker.
`None` в ответе — LLM недоступна или бюджет исчерпан: вызывающий код уходит в шаблон (уровень L2).
"""

from __future__ import annotations

from typing import Protocol, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)

Message = dict[str, str]  # {"role": "system" | "user" | "assistant", "content": "…"}


class JsonLLM(Protocol):
    async def complete_json(
        self,
        role: str,
        messages: list[Message],
        output: type[T],
        *,
        has_portfolio_ctx: bool,
        prompt_id: str,
        max_tokens: int = 2000,
    ) -> T | None:
        """Ответ модели, разобранный в `output` (pydantic, extra=forbid). Невалидный JSON — ретрай внутри
        роутера, затем следующий провайдер; если никто не справился — None."""
        ...


def untrusted(source: str, doc_id: str, text: str) -> str:
    """Изоляция внешнего текста (G7): содержимое — данные, а не команды."""
    safe = text.replace("</untrusted_document>", "</untrusted_document_>")
    return f'<untrusted_document source="{source}" id="{doc_id}">\n{safe}\n</untrusted_document>'
