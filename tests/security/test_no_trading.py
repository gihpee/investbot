"""Слой 4 read-only (ADR-05): в коде бота нет торговых вызовов SDK."""

from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "investbot"

TRADING_PATTERNS = re.compile(
    r"\b(post_order|post_stop_order|replace_order|cancel_order|cancel_stop_order|post_order_async"
    r"|\.orders\b|\.stop_orders\b|\.sandbox\b|SandboxService|OrdersService|StopOrdersService)"
)
ALLOWED = {SRC / "adapters" / "tinvest" / "guard.py"}  # там только список запрещённых сервисов


def test_no_trading_calls_in_source() -> None:
    offenders = []
    for path in SRC.rglob("*.py"):
        if path in ALLOWED:
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if TRADING_PATTERNS.search(line):
                offenders.append(f"{path.relative_to(SRC)}:{lineno}: {line.strip()}")
    assert not offenders, "торговые вызовы в коде:\n" + "\n".join(offenders)


def test_secrets_are_not_logged_by_settings_repr() -> None:
    from investbot.settings import Settings

    s = Settings(_env_file=None, tinvest_token="t.secret-value", telegram_bot_token="123:abc")  # type: ignore[call-arg]
    assert "secret-value" not in repr(s) and "123:abc" not in repr(s)
