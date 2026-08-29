import pytest

from app.quota import InMemoryQuotaBackend
from app.rate_limit import QuotaExceeded, RateLimiter


@pytest.mark.asyncio
async def test_allows_up_to_quota_then_raises():
    limiter = RateLimiter(InMemoryQuotaBackend(), daily_quota=2)

    await limiter.before_live_fetch("account-a")
    await limiter.before_live_fetch("account-a")
    with pytest.raises(QuotaExceeded):
        await limiter.before_live_fetch("account-a")


@pytest.mark.asyncio
async def test_remaining_today_tracks_usage():
    limiter = RateLimiter(InMemoryQuotaBackend(), daily_quota=5)

    assert await limiter.remaining_today("account-a") == 5
    await limiter.before_live_fetch("account-a")
    assert await limiter.remaining_today("account-a") == 4


@pytest.mark.asyncio
async def test_accounts_have_independent_buckets():
    """The bug this guards against: one caller's own session must not eat
    into another account's (e.g. the backend demo session's) quota."""
    limiter = RateLimiter(InMemoryQuotaBackend(), daily_quota=1)

    await limiter.before_live_fetch("account-a")
    with pytest.raises(QuotaExceeded):
        await limiter.before_live_fetch("account-a")

    # A different account still has its full quota untouched.
    await limiter.before_live_fetch("account-b")
    assert await limiter.remaining_today("account-b") == 0
    assert await limiter.remaining_today("account-a") == 0
