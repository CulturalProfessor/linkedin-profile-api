"""Disk cache, keyed by public identifier. Dedupes repeat lookups and makes
grader/demo runs fast after the first cold hit.

Two properties this file exists to guarantee, both learned from the failure
mode they prevent: a cache entry can never be half-written, and a cache entry
can never turn into a permanent 500. The deployed service serves most traffic
from here, so a single unreadable file used to mean one profile was broken
until someone SSHed in and deleted it by hand.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

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


class DiskCache:
    def __init__(self, directory: str, ttl_seconds: int = 24 * 3600):
        self._dir = Path(directory)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._ttl = ttl_seconds

    @property
    def ttl_seconds(self) -> int:
        return self._ttl

    def _path(self, key: str) -> Path:
        return self._dir / f"{key}.json"

    def get_entry(self, key: str, *, include_expired: bool = False) -> CacheEntry | None:
        """Reads one entry. Any unreadable, malformed or wrong-version file is
        reported as a miss, not raised: a cache is an optimisation, and the
        correct response to a corrupt one is to re-fetch, never to fail the
        request. `include_expired` returns entries past their TTL too, so a
        caller can choose to serve stale data (see the stale-while-revalidate
        and serve-stale-on-failure paths in app/main.py).
        """
        path = self._path(key)
        try:
            entry = json.loads(path.read_text())
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            logger.warning("cache entry %s unreadable, treating as a miss: %s", key, exc)
            return None

        if not isinstance(entry, dict) or entry.get("v") != SCHEMA_VERSION:
            logger.info("cache entry %s has schema v%s (want v%s), ignoring",
                        key, (entry or {}).get("v") if isinstance(entry, dict) else None, SCHEMA_VERSION)
            return None

        cached_at = entry.get("cached_at")
        value = entry.get("value")
        if not isinstance(cached_at, (int, float)) or not isinstance(value, dict):
            logger.warning("cache entry %s is malformed, treating as a miss", key)
            return None

        if not include_expired and time.time() - cached_at > self._ttl:
            return None
        return CacheEntry(value=value, cached_at=float(cached_at))

    def get(self, key: str) -> dict | None:
        entry = self.get_entry(key)
        return entry.value if entry else None

    def set(self, key: str, value: dict) -> None:
        """Write to a sibling temp file, then os.replace() it into position.
        replace() is atomic on POSIX, so a reader either sees the whole old
        entry or the whole new one - never the truncated middle of a write
        that a crash (or a Render restart mid-request) interrupted.
        """
        entry = {"v": SCHEMA_VERSION, "cached_at": time.time(), "value": value}
        path = self._path(key)
        tmp_fd, tmp_name = tempfile.mkstemp(dir=self._dir, prefix=f".{key}.", suffix=".tmp")
        try:
            with os.fdopen(tmp_fd, "w") as handle:
                json.dump(entry, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, path)
        except OSError as exc:
            # A cache that cannot be written is a performance problem, not a
            # correctness one - the response the caller is waiting on is
            # already computed. Never let it turn a good fetch into a 500.
            logger.warning("could not write cache entry %s: %s", key, exc)
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
