"""Token bucket на сервис T-Invest API. Лимиты суммируются по всем методам сервиса и всем счетам."""

from __future__ import annotations

import asyncio
import time

# 90% от лимитов limits.md (контракты 1.51): instruments 200, market_data 600, operations 200, users 100 в минуту.
DEFAULT_PER_MINUTE = {"instruments": 180, "market_data": 540, "operations": 180, "users": 90}


class TokenBucket:
    def __init__(self, per_minute: int) -> None:
        self._rate = per_minute / 60
        self._cap = float(per_minute)
        self._tokens = float(per_minute)
        self._ts = time.monotonic()
        self._lock = asyncio.Lock()

    async def take(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self._tokens = min(self._cap, self._tokens + (now - self._ts) * self._rate)
                self._ts = now
                if self._tokens >= 1:
                    self._tokens -= 1
                    return
                await asyncio.sleep((1 - self._tokens) / self._rate)


class ServiceLimits:
    def __init__(self, per_minute: dict[str, int] | None = None) -> None:
        self._buckets = {k: TokenBucket(v) for k, v in (per_minute or DEFAULT_PER_MINUTE).items()}

    async def take(self, service: str) -> None:
        bucket = self._buckets.get(service)
        if bucket is not None:
            await bucket.take()
