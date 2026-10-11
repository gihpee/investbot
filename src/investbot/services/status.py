"""Состояние системы для /status: что подключено и в каком режиме работает бот."""

from __future__ import annotations

from datetime import datetime

from investbot.core.models import Strict


class SystemStatus(Strict):
    checked_at: datetime
    deployment_mode: str
    broker_connected: bool
    broker_accounts: int
    ips_version: int | None
    manual_accounts: int
    privacy_mode: str
    llm: str = "не подключена"
    problems: tuple[str, ...] = ()
