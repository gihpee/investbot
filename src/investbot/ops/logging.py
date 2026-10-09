"""structlog с фильтром секретов: токены не попадают в логи даже по ошибке."""

from __future__ import annotations

import logging
import re
from collections.abc import MutableMapping
from typing import Any

import structlog

_SECRET_KEYS = re.compile(r"token|secret|password|authorization", re.I)
_TOKEN_VALUE = re.compile(r"t\.[A-Za-z0-9_\-]{20,}|\d{6,}:[A-Za-z0-9_\-]{30,}")  # T-Invest и Telegram


def redact(_: Any, __: str, event: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    for k, v in list(event.items()):
        if _SECRET_KEYS.search(k):
            event[k] = "***"
        elif isinstance(v, str):
            event[k] = _TOKEN_VALUE.sub("***", v)
    return event


def setup_logging(level: str = "INFO", json: bool = False) -> None:
    logging.basicConfig(level=level, format="%(message)s")
    renderer: Any = structlog.processors.JSONRenderer() if json else structlog.dev.ConsoleRenderer()
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            redact,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level)),
    )
