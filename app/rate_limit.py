"""Account-safety guardrails for live Voyager requests: a jittered delay
before each live fetch (avoids the even-interval timing signature that
behavioural anti-scraping keys on) and a hard daily ceiling *per LinkedIn
account*, so total exposure on any one session is a number we chose rather
than one traffic decided for us. The count itself lives in a QuotaBackend
(app/quota.py), keyed by account - in-memory by default, or a shared Upstash
Redis counter when configured, so local runs and the deployed server draw
down the same total for that account."""
from __future__ import annotations

import asyncio
import random

from app.quota import QuotaBackend


class QuotaExceeded(RuntimeError):
    pass


class RateLimiter:
    def __init__(self, backend: QuotaBackend, daily_quota: int, min_delay: float, max_delay: float):
        self._backend = backend
        self._daily_quota = daily_quota
        self._min_delay = min_delay
        self._max_delay = max_delay

    async def remaining_today(self, account_key: str) -> int:
        return max(0, self._daily_quota - await self._backend.current(account_key))

    async def before_live_fetch(self, account_key: str) -> None:
        count = await self._backend.increment_and_check(account_key)
        if count > self._daily_quota:
            raise QuotaExceeded(f"daily quota of {self._daily_quota} live fetches reached for this session")
        await asyncio.sleep(random.uniform(self._min_delay, self._max_delay))
