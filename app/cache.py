"""Profile cache, keyed by public identifier. Dedupes repeat lookups and makes
grader/demo runs fast after the first cold hit.

Two backends behind one interface, chosen by config the same way the quota
counter is (see app/quota.py):

* `DiskCache` - files on local disk. The right thing for local development,
  and useless on Render's free tier, where the container is replaced on every
  deploy and after ~15 minutes idle, taking the cache with it.
* `UpstashCache` - the same Redis the quota counter already uses. Survives
  restarts, which is what makes the 24h TTL and the stale-serving paths in
  app/main.py mean anything in the deployed service.

Three properties every backend must guarantee, each learned from the failure
mode it prevents:

1. A write is atomic. A crash mid-write must not leave a half-entry.
2. A read never raises. An unreadable, malformed or wrong-version entry is a
   *miss*, not an error - a cache is an optimisation, and the correct response
   to a broken one is to re-fetch. An entry truncated by a crash used to mean
   one profile stayed 500 until someone deleted the file by hand.
3. Expiry is decided here, in application code, from the `cached_at` stored
   inside the entry - never by handing the TTL to the storage layer. Redis
   `EXPIRE` *deletes* the key when it fires, which would destroy the stale
   copy at exactly the moment app/main.py's stale-while-revalidate and
   serve-stale-on-failure paths need it. The Redis TTL below is set far longer
   than the freshness TTL and exists only as garbage collection.
"""
from __future__ import annotations

import abc
import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

# Bumped whenever the cached `value` shape changes. Entries written by an
# older version are treated as misses rather than being fed to a model that
# no longer accepts them - which is what would otherwise happen the first
# time a field is added to Profile and a day-old cache is still on disk.
SCHEMA_VERSION = 2


@dataclass(frozen=True)
class CacheEntry:
    value: dict
    cached_at: float

    @property
    def age_seconds(self) -> int:
        return max(0, int(time.time() - self.cached_at))


def _decode(raw: str | None, key: str) -> tuple[dict, float] | None:
    """Shared entry parsing: returns (value, cached_at), or None for anything
    unreadable, malformed or written by an older schema. Never raises - see
    property 2 in the module docstring."""
    if raw is None:
        return None
    try:
        entry = json.loads(raw)
    except ValueError as exc:
        logger.warning("cache entry %s unreadable, treating as a miss: %s", key, exc)
        return None

    if not isinstance(entry, dict) or entry.get("v") != SCHEMA_VERSION:
        logger.info("cache entry %s has schema v%s (want v%s), ignoring",
                    key, entry.get("v") if isinstance(entry, dict) else None, SCHEMA_VERSION)
        return None

    cached_at, value = entry.get("cached_at"), entry.get("value")
    if not isinstance(cached_at, (int, float)) or not isinstance(value, dict):
        logger.warning("cache entry %s is malformed, treating as a miss", key)
        return None
    return value, float(cached_at)


def _encode(value: dict) -> str:
    return json.dumps({"v": SCHEMA_VERSION, "cached_at": time.time(), "value": value})


class CacheBackend(abc.ABC):
    """Async because one implementation talks to Redis over HTTP. The disk
    backend does blocking file IO inside an async method, which is fine: the
    entries are single-digit kilobytes and the alternative (a threadpool hop)
    costs more than the read."""

    _ttl: int

    @property
    def ttl_seconds(self) -> int:
        return self._ttl

    def _fresh(self, cached_at: float, include_expired: bool) -> bool:
        return include_expired or (time.time() - cached_at) <= self._ttl

    @abc.abstractmethod
    async def get_entry(self, key: str, *, include_expired: bool = False) -> CacheEntry | None:
        """`include_expired` returns entries past their TTL too, so a caller
        can choose to serve stale data (see the stale-while-revalidate and
        serve-stale-on-failure paths in app/main.py)."""

    @abc.abstractmethod
    async def set(self, key: str, value: dict) -> None:
        """Never raises. A cache that cannot be written is a performance
        problem, not a correctness one - the response the caller is waiting on
        is already computed."""

    async def get(self, key: str) -> dict | None:
        entry = await self.get_entry(key)
        return entry.value if entry else None

    async def aclose(self) -> None:  # noqa: B027 - optional hook, not abstract
        """Releases any connection the backend holds. A no-op by default:
        DiskCache has nothing to close, and forcing every backend to
        implement an empty method would be noise."""


class DiskCache(CacheBackend):
    def __init__(self, directory: str, ttl_seconds: int = 24 * 3600):
        self._dir = Path(directory)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._ttl = ttl_seconds

    def _path(self, key: str) -> Path:
        return self._dir / f"{key}.json"

    async def get_entry(self, key: str, *, include_expired: bool = False) -> CacheEntry | None:
        try:
            raw = self._path(key).read_text()
        except FileNotFoundError:
            return None
        except OSError as exc:
            logger.warning("cache entry %s unreadable, treating as a miss: %s", key, exc)
            return None

        decoded = _decode(raw, key)
        if decoded is None:
            return None
        value, cached_at = decoded
        if not self._fresh(cached_at, include_expired):
            return None
        return CacheEntry(value=value, cached_at=cached_at)

    async def set(self, key: str, value: dict) -> None:
        """Write to a sibling temp file, then os.replace() it into position.
        replace() is atomic on POSIX, so a reader either sees the whole old
        entry or the whole new one - never the truncated middle of a write
        that a crash (or a Render restart mid-request) interrupted.
        """
        path = self._path(key)
        tmp_fd, tmp_name = tempfile.mkstemp(dir=self._dir, prefix=f".{key}.", suffix=".tmp")
        try:
            with os.fdopen(tmp_fd, "w") as handle:
                handle.write(_encode(value))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, path)
        except OSError as exc:
            logger.warning("could not write cache entry %s: %s", key, exc)
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


class UpstashCache(CacheBackend):
    """Cache in the Redis the quota counter already uses.

    The reason to bother: on Render's free tier the container - and its disk -
    is replaced on every deploy and after ~15 minutes idle. A disk cache there
    is close to decorative, and the stale-serving paths almost never get to
    fire because entries rarely survive long enough to go stale.

    Commands go through /pipeline rather than the /get/{key} URL form so the
    key never has to survive URL encoding: a public identifier may legitimately
    contain a percent-escape, and that is not something to discover in
    production.
    """

    _KEY_PREFIX = "linkedin-profile-api:profile:"

    # Redis-side retention, deliberately far longer than the freshness TTL.
    # This is garbage collection, not expiry: expiry is decided in _fresh()
    # from the stored cached_at, because a key Redis has deleted cannot be
    # served as stale. See property 3 in the module docstring.
    _RETENTION_SECONDS = 7 * 24 * 3600

    # One retry, on a short per-attempt timeout. A false miss is expensive in
    # a way a slow read is not: it costs a full ~9.5s fan-out *and* a unit of
    # the daily quota, which is the genuinely scarce resource here. Two 3s
    # attempts bound the wait at the same 6s a single generous timeout would,
    # while surviving the one-off blip that was actually observed in testing.
    _ATTEMPTS = 2

    def __init__(self, rest_url: str, rest_token: str, ttl_seconds: int = 24 * 3600,
                 timeout: float = 3.0):
        self._ttl = ttl_seconds
        self._client = httpx.AsyncClient(
            base_url=rest_url.rstrip("/"),
            headers={"Authorization": f"Bearer {rest_token}"},
            timeout=timeout,
        )

    def _key(self, key: str) -> str:
        return f"{self._KEY_PREFIX}{key}"

    async def _command(self, command: list):
        """Runs one Redis command via the REST pipeline, retrying once.
        Raises only if every attempt failed - callers turn that into a miss."""
        last: Exception | None = None
        for attempt in range(self._ATTEMPTS):
            try:
                resp = await self._client.post("/pipeline", json=[command])
                resp.raise_for_status()
                return resp.json()[0]["result"]
            except Exception as exc:  # noqa: BLE001 - retried, then reported by the caller
                last = exc
                if attempt + 1 < self._ATTEMPTS:
                    logger.info(
                        "upstash %s attempt %d failed (%s), retrying",
                        command[0], attempt + 1, type(exc).__name__,
                    )
        raise last  # type: ignore[misc]

    async def get_entry(self, key: str, *, include_expired: bool = False) -> CacheEntry | None:
        try:
            raw = await self._command(["GET", self._key(key)])
        except Exception as exc:  # noqa: BLE001 - a cache read must never fail a request
            # The exception class matters as much as the message here: httpx
            # timeouts stringify to "", so logging only str(exc) produced a
            # warning that said a read had failed and nothing about why.
            logger.warning(
                "cache read for %s failed, treating as a miss: %s: %s",
                key, type(exc).__name__, exc,
            )
            return None

        decoded = _decode(raw, key)
        if decoded is None:
            return None
        value, cached_at = decoded
        if not self._fresh(cached_at, include_expired):
            return None
        return CacheEntry(value=value, cached_at=cached_at)

    async def set(self, key: str, value: dict) -> None:
        # SET is atomic server-side, so there is no equivalent of the disk
        # backend's temp-file dance: a reader sees either the old value or the
        # new one, never a partial write.
        try:
            await self._command(
                ["SET", self._key(key), _encode(value), "EX", self._RETENTION_SECONDS]
            )
        except Exception as exc:  # noqa: BLE001 - see CacheBackend.set
            logger.warning(
                "could not write cache entry %s: %s: %s", key, type(exc).__name__, exc
            )

    async def aclose(self) -> None:
        await self._client.aclose()
