"""Daily quota counter, pluggable so it can be either process-local or
shared across every process hitting the account (local dev + deployed
server included).

Counters are keyed per LinkedIn account (an `account_key` derived from the
session in use - see `app.main._account_key`), never globally. The quota
exists to cap exposure on *one specific account*; a caller who brings their
own session is spending their own account's risk budget, not the backend
demo session's, so their traffic must land in a different bucket. A single
global counter would let an unrelated caller's session exhaust the demo
account's quota (denying everyone else), or worse, purport to "protect"
several distinct real accounts with one shared number that means nothing for
any of them individually.
"""
from __future__ import annotations

import abc
from datetime import date

import httpx


class QuotaBackend(abc.ABC):
    @abc.abstractmethod
    async def increment_and_check(self, account_key: str) -> int:
        """Atomically increments today's counter for this account and
        returns the new value."""

    @abc.abstractmethod
    async def current(self, account_key: str) -> int:
        """Today's count for this account, without incrementing it."""


class InMemoryQuotaBackend(QuotaBackend):
    """Process-local fallback for local dev without a shared store configured.
    Each process (a laptop run, each deployed instance) keeps its own count -
    fine for solo testing, but two processes sharing one LinkedIn account
    won't see each other's usage. Use UpstashQuotaBackend when that matters.
    """

    def __init__(self) -> None:
        self._counts: dict[str, tuple[date, int]] = {}

    def _bump(self, account_key: str) -> int:
        today = date.today()
        day, count = self._counts.get(account_key, (today, 0))
        if day != today:
            count = 0
        count += 1
        self._counts[account_key] = (today, count)
        return count

    async def increment_and_check(self, account_key: str) -> int:
        return self._bump(account_key)

    async def current(self, account_key: str) -> int:
        today = date.today()
        day, count = self._counts.get(account_key, (today, 0))
        return count if day == today else 0


class UpstashQuotaBackend(QuotaBackend):
    """Shared counter via Upstash Redis's REST API, so a local run and the
    deployed server draw down the same daily quota against the same
    LinkedIn account instead of each keeping its own count. Free tier,
    no server to run - see README for setup.
    """

    _KEY_PREFIX = "linkedin-profile-api:quota:"
    _TTL_SECONDS = 2 * 24 * 3600  # outlives the day it's for; key name rotates daily anyway

    def __init__(self, rest_url: str, rest_token: str, timeout: float = 5.0):
        self._client = httpx.AsyncClient(
            base_url=rest_url.rstrip("/"),
            headers={"Authorization": f"Bearer {rest_token}"},
            timeout=timeout,
        )

    def _key(self, account_key: str) -> str:
        return f"{self._KEY_PREFIX}{account_key}:{date.today().isoformat()}"

    async def increment_and_check(self, account_key: str) -> int:
        key = self._key(account_key)
        resp = await self._client.post("/pipeline", json=[["INCR", key], ["EXPIRE", key, self._TTL_SECONDS]])
        resp.raise_for_status()
        results = resp.json()
        return int(results[0]["result"])

    async def current(self, account_key: str) -> int:
        resp = await self._client.get(f"/get/{self._key(account_key)}")
        resp.raise_for_status()
        value = resp.json().get("result")
        return int(value) if value is not None else 0

    async def aclose(self) -> None:
        await self._client.aclose()
