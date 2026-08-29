"""Account-safety guardrail for live Voyager requests: a hard daily ceiling
*per LinkedIn account*, so total exposure on any one session is a number we
chose rather than one traffic decided for us. The count itself lives in a
QuotaBackend (app/quota.py), keyed by account - in-memory by default, or a
shared Upstash Redis counter when configured, so local runs and the deployed
server draw down the same total for that account.

The other half of the guardrail - a jittered pause between requests - lives in
VoyagerClient._get rather than here. One /profile is a fan-out of several
Voyager requests, so pausing once at this level still let the requests
themselves go out back-to-back; the delay has to sit at the layer that
actually issues them."""
from __future__ import annotations

from app.quota import QuotaBackend


class QuotaExceeded(RuntimeError):
    pass


class RateLimiter:
    def __init__(self, backend: QuotaBackend, daily_quota: int):
        self._backend = backend
        self._daily_quota = daily_quota

    async def remaining_today(self, account_key: str) -> int:
        return max(0, self._daily_quota - await self._backend.current(account_key))

    async def before_live_fetch(self, account_key: str) -> None:
        count = await self._backend.increment_and_check(account_key)
        if count > self._daily_quota:
            raise QuotaExceeded(f"daily quota of {self._daily_quota} live fetches reached for this session")
