"""Журнал вызовов LLM в таблице `llm_call` (G10, G11): токены, стоимость, модель, промпт, успех. Без текстов.

Месяц бюджета — календарный месяц по Europe/Moscow. Время пишется в UTC (SQLite отдаёт naive datetime,
поэтому и границы запросов переводятся в UTC).

Колонка `role` — роль первого запроса логического вызова (`qa`), последующие запросы того же вызова
(ретрай схемы, шаги инструментов, следующий провайдер) пишутся с номером: `qa#2`, `qa#3`. Так число
вопросов Q&A за месяц — это число строк с «голой» ролью, а стоимость считается по всем строкам.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncEngine

from investbot.data.db import llm_call, plain_decimal

MSK = ZoneInfo("Europe/Moscow")
_SUM_STEP = Decimal("0.000001")  # SQLite суммирует строки через float — копейки точны, хвост отбрасываем
MAX_ERROR_LEN = 500


@dataclass(frozen=True)
class CallRecord:
    at: datetime
    role: str
    provider: str
    model: str
    prompt_id: str
    input_tokens: int
    output_tokens: int
    cost_rub: Decimal
    ok: bool
    error: str | None = None


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        raise ValueError("ожидается datetime с часовым поясом")
    return dt.astimezone(UTC)


def _from_db(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def month_start(now: datetime) -> datetime:
    """Начало календарного месяца по Москве, в UTC: 1 октября 00:00 MSK = 30 сентября 21:00 UTC."""
    local = _utc(now).astimezone(MSK)
    return local.replace(day=1, hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)


class CallLedger:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def record(self, rec: CallRecord) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(insert(llm_call).values(
                at=_utc(rec.at),
                role=rec.role,
                provider=rec.provider,
                model=rec.model,
                prompt_id=rec.prompt_id,
                input_tokens=rec.input_tokens,
                output_tokens=rec.output_tokens,
                cost_rub=rec.cost_rub,
                ok=rec.ok,
                error=rec.error[:MAX_ERROR_LEN] if rec.error else None,
            ))

    async def spent_since(self, since: datetime, *, provider: str | None = None) -> Decimal:
        """Сумма `cost_rub` с момента `since` (все строки, включая неуспешные: токены оплачены)."""
        stmt = select(func.sum(llm_call.c.cost_rub)).where(llm_call.c.at >= _utc(since))
        if provider is not None:
            stmt = stmt.where(llm_call.c.provider == provider)
        async with self._engine.connect() as conn:
            total = (await conn.execute(stmt)).scalar_one()
        if total is None:
            return Decimal(0)
        return plain_decimal(Decimal(str(total)).quantize(_SUM_STEP))

    async def monthly_spent(self, now: datetime, *, provider: str | None = None) -> Decimal:
        return await self.spent_since(month_start(now), provider=provider)

    async def count_since(self, since: datetime, *, roles: Collection[str], provider: str | None = None) -> int:
        """Число строк с ролями из `roles` (точное совпадение: шаги `qa#N` не считаются)."""
        if not roles:
            return 0
        stmt = (select(func.count()).select_from(llm_call)
                .where(llm_call.c.at >= _utc(since), llm_call.c.role.in_(list(roles))))
        if provider is not None:
            stmt = stmt.where(llm_call.c.provider == provider)
        async with self._engine.connect() as conn:
            return int((await conn.execute(stmt)).scalar_one())

    async def calls_since(self, since: datetime) -> list[CallRecord]:
        stmt = select(llm_call).where(llm_call.c.at >= _utc(since)).order_by(llm_call.c.id)
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).mappings().all()
        return [CallRecord(
            at=_from_db(r["at"]), role=r["role"], provider=r["provider"], model=r["model"],
            prompt_id=r["prompt_id"], input_tokens=r["input_tokens"], output_tokens=r["output_tokens"],
            cost_rub=r["cost_rub"], ok=r["ok"], error=r["error"],
        ) for r in rows]
