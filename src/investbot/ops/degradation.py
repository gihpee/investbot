"""Мягкая деградация (ADR-14, §4.5): набор флагов, а не один уровень.

Ограничения применяются по каждому флагу отдельно, уровень для гейтов — максимум по флагам, баннеры — по всем.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo

from investbot.core.models import Strict

Flag = Literal["data_fallback", "llm_down", "broker_stale", "broker_too_old", "gate_critical"]

LEVEL: dict[str, int] = {
    "data_fallback": 1,
    "llm_down": 2,
    "broker_stale": 3,
    "broker_too_old": 4,
    "gate_critical": 4,
}
MSK = ZoneInfo("Europe/Moscow")
BROKER_STALE_MAX_DAYS = 7


class GateResult(Strict):
    name: str
    ok: bool
    critical: bool = False
    message: str = ""


class Degradation(Strict):
    flags: tuple[str, ...] = ()
    gates: tuple[GateResult, ...] = ()
    broker_as_of: datetime | None = None

    @property
    def level(self) -> int:
        return max((LEVEL.get(f, 0) for f in self.flags), default=0)

    @property
    def allow_sells(self) -> bool:
        return not any(f in self.flags for f in ("broker_stale", "broker_too_old"))

    @property
    def trade_list_allowed(self) -> bool:
        return self.level < 4

    def banners(self) -> list[str]:
        out: list[str] = []
        as_of = f" от {self.broker_as_of.astimezone(MSK):%d.%m %H:%M} МСК" if self.broker_as_of else ""
        if "broker_too_old" in self.flags:
            out.append(f"⛔ Т-Инвестиции недоступны, последний снимок портфеля{as_of} старше "
                       f"{BROKER_STALE_MAX_DAYS} дней: план не строится, только аналитика и события")
        elif "broker_stale" in self.flags:
            out.append(f"⚠️ Портфель по снимку{as_of}: Т-Инвестиции сейчас недоступны, продажи не предлагаются — "
                       "проверьте позиции и остаток")
        for g in self.gates:
            if not g.ok and g.critical:
                out.append(f"⛔ План отложен: {g.message}")
        if "data_fallback" in self.flags:
            soft = [g.message for g in self.gates if not g.ok and not g.critical]
            out.append("ℹ️ " + ("; ".join(soft) if soft else "часть данных из резервных источников"))
        if "llm_down" in self.flags:
            out.append("ℹ️ Текстовые пояснения временно недоступны, все расчёты выполнены")
        return out


def combine(flags: Iterable[str], gates: Iterable[GateResult], broker_as_of: datetime | None = None) -> Degradation:
    gates = tuple(gates)
    all_flags = set(flags)
    if any(not g.ok and g.critical for g in gates):
        all_flags.add("gate_critical")
    if any(not g.ok and not g.critical for g in gates):
        all_flags.add("data_fallback")
    return Degradation(flags=tuple(sorted(all_flags, key=lambda f: (LEVEL.get(f, 0), f))), gates=gates,
                       broker_as_of=broker_as_of)
