from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from investbot.jobs.scheduler import AlreadyRunning, SingleInstanceLock, build_scheduler, cron

MSK = ZoneInfo("Europe/Moscow")


def test_weekly_cron_fires_on_saturday_10_msk() -> None:
    trig = cron("0 10 * * sat", "Europe/Moscow")
    nxt = trig.get_next_fire_time(None, datetime(2026, 10, 9, 12, 0, tzinfo=MSK))  # пятница
    assert nxt == datetime(2026, 10, 10, 10, 0, tzinfo=MSK)


def test_single_instance_lock(tmp_path: Path) -> None:
    a, b = SingleInstanceLock(tmp_path / "s.lock"), SingleInstanceLock(tmp_path / "s.lock")
    a.acquire()
    with pytest.raises(AlreadyRunning):
        b.acquire()
    a.release()
    b.acquire()
    b.release()


async def test_build_scheduler_registers_jobs() -> None:
    async def job() -> None:
        return None

    sched = build_scheduler("Europe/Moscow", {"weekly_report": ("0 10 * * sat", job), "daily": ("0 9 * * *", job)})
    assert {j.id for j in sched.get_jobs()} == {"weekly_report", "daily"}
