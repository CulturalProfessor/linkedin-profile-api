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

import time

from app.quota import QuotaBackend, next_utc_midnight


class QuotaExceeded(RuntimeError):
    def __init__(self, message: str, retry_after: int):
        super().__init__(message)
        self.retry_after = retry_after


class RateLimiter:
    """Two ceilings, checked together.

    The per-account one caps exposure on a single LinkedIn account. The global
    one caps this deployment's total outbound traffic, and exists because the
    per-account key is derived from the caller's own cookie: vary it and you
    mint a fresh bucket every request. Without a global figure the daily quota
    bounds a cooperative caller and nobody else.
    """

    GLOBAL_KEY = "__all__"

    def __init__(self, backend: QuotaBackend, daily_quota: int,
                 global_daily_quota: int | None = None):
        self._backend = backend
        self._daily_quota = daily_quota
        self._global_daily_quota = global_daily_quota

    @property
    def daily_quota(self) -> int:
        return self._daily_quota

    @staticmethod
    def resets_at() -> int:
        """Unix timestamp the counter rolls over at - X-RateLimit-Reset."""
        return next_utc_midnight()

    def _remaining(self, count: int) -> int:
        return max(0, self._daily_quota - count)

    async def remaining_today(self, account_key: str) -> int:
        return self._remaining(await self._backend.current(account_key))

    async def before_live_fetch(self, account_key: str) -> int:
        """Counts one live fetch and returns the quota remaining afterwards,
        so the caller can emit rate-limit headers without a second round-trip
        to the shared store."""
        retry_after = max(1, next_utc_midnight() - int(time.time()))

        if self._global_daily_quota is not None:
            total = await self._backend.increment_and_check(self.GLOBAL_KEY)
            if total > self._global_daily_quota:
                raise QuotaExceeded(
                    f"this deployment's daily ceiling of {self._global_daily_quota} "
                    "live fetches is reached",
                    retry_after=retry_after,
                )

        count = await self._backend.increment_and_check(account_key)
        if count > self._daily_quota:
            # The global counter was already incremented for a fetch that is
            # not going to happen, so give it back before refusing.
            if self._global_daily_quota is not None:
                await self._backend.decrement(self.GLOBAL_KEY)
            raise QuotaExceeded(
                f"daily quota of {self._daily_quota} live fetches reached for this session",
                retry_after=retry_after,
            )
        return self._remaining(count)

    async def refund(self, account_key: str) -> int:
        """Gives back a unit counted for a fetch that never reached LinkedIn.

        The quota exists to cap *account exposure*, and exposure is measured
        in requests actually sent. A call that was counted and then failed
        before issuing a single upstream request spent no exposure, so
        charging for it just shrinks the day's real budget. Anything that did
        reach LinkedIn is not refunded, however it ended - a 404 or a rejected
        session still put traffic on the account.
        """
        if self._global_daily_quota is not None:
            await self._backend.decrement(self.GLOBAL_KEY)
        return self._remaining(await self._backend.decrement(account_key))
