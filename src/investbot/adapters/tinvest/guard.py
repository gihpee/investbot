"""Read-only в рантайме (ADR-05, слои 2 и 3).

Слой 1 — права самого токена (выпускается пользователем как «только чтение»).
Слой 2 — `check_read_only`: при старте и ежедневно; любой счёт с полным доступом → отказ работать.
Слой 3 — `ReadOnlyClientGuard`: обращение к торговым сервисам SDK — исключение.
Слой 4 — import-linter и тесты в CI (tests/security).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from investbot.core.models import AccessLevel, Account, AccountKind

FORBIDDEN_SERVICES = frozenset({"orders", "stop_orders", "sandbox"})
INVEST_KINDS = frozenset(AccountKind) - {AccountKind.OTHER}


class TradingAccessError(RuntimeError):
    """Токен даёт право торговать или его уровень доступа не удалось подтвердить."""


class ReadOnlyClientGuard:
    """Обёртка над сервисами SDK: торговые сервисы недоступны даже по ошибке в коде."""

    def __init__(self, services: Any) -> None:
        self._services = services

    def __getattr__(self, name: str) -> Any:
        if name in FORBIDDEN_SERVICES:
            raise PermissionError(f"торговый сервис '{name}' запрещён в investbot")
        return getattr(self._services, name)


def check_read_only(alias: str, accounts: Iterable[Account]) -> list[Account]:
    """Fail-closed проверка уровня доступа токена.

    - FULL на любом счёте — отказ.
    - На инвестиционных счетах допустимы только READ_ONLY и NO_ACCESS; UNKNOWN — отказ.
    - Банковские и прочие счета (kind=OTHER) с NO_ACCESS/UNKNOWN игнорируются.
    Возвращает счета, доступные токену на чтение.
    """
    accounts = list(accounts)
    full = [a.account_id for a in accounts if a.access is AccessLevel.FULL]
    if full:
        raise TradingAccessError(
            f"токен '{alias}' даёт полный доступ к счетам {full}: нужен токен «только чтение»; работа остановлена"
        )
    unknown = [a.account_id for a in accounts if a.kind in INVEST_KINDS and a.access is AccessLevel.UNKNOWN]
    if unknown:
        raise TradingAccessError(f"токен '{alias}': не удалось подтвердить read-only для счетов {unknown}")
    return [a for a in accounts if a.access is AccessLevel.READ_ONLY]
