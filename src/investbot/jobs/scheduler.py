"""Расписание (§14): недельный отчёт и ежедневная проверка прав токена.

Единственный экземпляр планировщика обеспечивается файловой блокировкой (ADR-08 до появления Postgres:
там будет advisory lock) — иначе при двойном запуске пришло бы два отчёта.
"""

from __future__ import annotations

import fcntl
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import IO, Any
from zoneinfo import ZoneInfo

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

log = structlog.get_logger(__name__)

Job = Callable[[], Awaitable[None]]


class AlreadyRunning(RuntimeError):
    pass


class SingleInstanceLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._fh: IO[str] | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = self.path.open("a+")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fh.close()
            raise AlreadyRunning(f"планировщик уже запущен (блокировка {self.path})") from None
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
        self._fh = fh

    def release(self) -> None:
        if self._fh is not None:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            self._fh.close()
            self._fh = None


def cron(expr: str, tz: str) -> CronTrigger:
    """Crontab-выражение в часовом поясе пользователя. Дни недели — именами (sat), а не числами:
    у APScheduler 0 = понедельник, у crontab 0 = воскресенье."""
    return CronTrigger.from_crontab(expr, timezone=ZoneInfo(tz))


def build_scheduler(tz: str, jobs: dict[str, tuple[str, Job]]) -> AsyncIOScheduler:
    """jobs: id → (crontab, корутина без аргументов)."""
    sched = AsyncIOScheduler(timezone=ZoneInfo(tz))
    for job_id, (expr, fn) in jobs.items():
        sched.add_job(_logged(job_id, fn), cron(expr, tz), id=job_id, coalesce=True, max_instances=1,
                      misfire_grace_time=3600, replace_existing=True)
    return sched


def _logged(job_id: str, fn: Job) -> Callable[[], Awaitable[None]]:
    async def run() -> None:
        log.info("job.start", job=job_id)
        try:
            await fn()
        except Exception:
            log.exception("job.failed", job=job_id)
            raise
        log.info("job.done", job=job_id)

    return run


def next_runs(sched: AsyncIOScheduler) -> dict[str, Any]:
    return {j.id: j.next_run_time for j in sched.get_jobs()}
