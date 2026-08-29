"""The cache's job is to be an optimisation that can never become a failure.
These tests pin the two ways it previously could: a corrupt file raising on
every subsequent read, and a schema change turning yesterday's entries into
500s."""
import json
import time

import httpx

from app.cache import SCHEMA_VERSION, DiskCache, UpstashCache, _encode


async def test_round_trip_and_age(tmp_path):
    cache = DiskCache(str(tmp_path))
    await cache.set("someone", {"fetched_at": "now", "profile": {}})
    entry = await cache.get_entry("someone")
    assert entry is not None
    assert entry.value["fetched_at"] == "now"
    assert entry.age_seconds == 0


async def test_missing_key_is_a_miss(tmp_path):
    assert await DiskCache(str(tmp_path)).get("nobody") is None


async def test_corrupt_entry_is_a_miss_not_an_exception(tmp_path):
    """The production failure this prevents: a file truncated by a crash
    mid-write used to raise from json.loads on *every* later request, so one
    profile stayed permanently 500 until someone deleted the file by hand."""
    cache = DiskCache(str(tmp_path))
    await cache.set("someone", {"profile": {}})
    (tmp_path / "someone.json").write_text('{"v": 2, "cached_at": 17880')
    assert await cache.get("someone") is None


async def test_entry_from_an_older_schema_is_a_miss(tmp_path):
    cache = DiskCache(str(tmp_path))
    (tmp_path / "someone.json").write_text(
        json.dumps({"cached_at": time.time(), "value": {"old": "shape"}})
    )
    assert await cache.get("someone") is None


async def test_expired_entry_is_a_miss_but_readable_when_asked_for(tmp_path):
    cache = DiskCache(str(tmp_path), ttl_seconds=1)
    await cache.set("someone", {"profile": {}})
    (tmp_path / "someone.json").write_text(
        json.dumps({"v": SCHEMA_VERSION, "cached_at": time.time() - 3600, "value": {"profile": {}}})
    )
    assert await cache.get("someone") is None
    stale = await cache.get_entry("someone", include_expired=True)
    assert stale is not None and stale.age_seconds >= 3600


async def test_write_leaves_no_temp_files_behind(tmp_path):
    cache = DiskCache(str(tmp_path))
    await cache.set("someone", {"profile": {}})
    assert sorted(p.name for p in tmp_path.iterdir()) == ["someone.json"]


# --- UpstashCache --------------------------------------------------------


def _redis(store=None, fail=False):
    """A mock Upstash REST endpoint backed by a dict, understanding just the
    two commands the cache issues."""
    store = {} if store is None else store

    def handler(request: httpx.Request) -> httpx.Response:
        if fail:
            return httpx.Response(500, text="redis is down")
        command = json.loads(request.content)[0]
        verb, key = command[0], command[1]
        if verb == "GET":
            return httpx.Response(200, json=[{"result": store.get(key)}])
        if verb == "SET":
            store[key] = command[2]
            return httpx.Response(200, json=[{"result": "OK"}])
        raise AssertionError(f"unexpected command {verb}")

    return handler, store


def _upstash(handler, **kwargs):
    cache = UpstashCache("https://fake.upstash.io", "token", **kwargs)
    cache._client = httpx.AsyncClient(
        base_url="https://fake.upstash.io", transport=httpx.MockTransport(handler)
    )
    return cache


async def test_upstash_round_trip():
    handler, store = _redis()
    cache = _upstash(handler)
    await cache.set("someone", {"fetched_at": "now"})
    entry = await cache.get_entry("someone")
    assert entry is not None and entry.value["fetched_at"] == "now"
    assert list(store) == ["linkedin-profile-api:profile:someone"]


async def test_upstash_missing_key_is_a_miss():
    handler, _ = _redis()
    assert await _upstash(handler).get("nobody") is None


async def test_upstash_outage_is_a_miss_not_an_error():
    """A cache read must never fail a request - the fallback is a live fetch,
    which is slow but correct."""
    handler, _ = _redis(fail=True)
    assert await _upstash(handler).get("someone") is None


async def test_upstash_write_failure_is_swallowed():
    """The response the caller is waiting on is already computed; failing to
    cache it must not turn a good fetch into a 500."""
    handler, _ = _redis(fail=True)
    await _upstash(handler).set("someone", {"fetched_at": "now"})  # must not raise


async def test_upstash_expiry_is_decided_locally_not_by_redis():
    """The critical one. If the TTL were handed to Redis via EXPIRE, the key
    would be *deleted* at expiry and the stale copy would vanish at exactly
    the moment the serve-stale paths need it."""
    handler, store = _redis()
    cache = _upstash(handler, ttl_seconds=1)
    await cache.set("someone", {"fetched_at": "then"})

    key = "linkedin-profile-api:profile:someone"
    aged = json.loads(store[key])
    aged["cached_at"] = time.time() - 3600
    store[key] = json.dumps(aged)

    assert await cache.get_entry("someone") is None            # expired
    stale = await cache.get_entry("someone", include_expired=True)
    assert stale is not None and stale.age_seconds >= 3600     # but still there


async def test_upstash_retention_is_far_longer_than_the_freshness_ttl():
    """The Redis TTL is garbage collection, not expiry - it must outlive the
    window in which an entry is still useful as stale data."""
    handler, _ = _redis()
    cache = _upstash(handler, ttl_seconds=24 * 3600)
    assert cache._RETENTION_SECONDS > cache.ttl_seconds * 2


async def test_upstash_corrupt_entry_is_a_miss():
    handler, store = _redis()
    cache = _upstash(handler)
    await cache.set("someone", {"fetched_at": "now"})
    store["linkedin-profile-api:profile:someone"] = "{not json"
    assert await cache.get("someone") is None


async def test_upstash_retries_once_before_giving_up():
    """A false miss costs a full live fan-out and a unit of daily quota, so a
    single transient blip - which is what was actually observed in testing -
    must not cause one."""
    attempts = []

    def flaky(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) == 1:
            raise httpx.ReadTimeout("", request=request)
        return httpx.Response(200, json=[{"result": _encode({"fetched_at": "now"})}])

    entry = await _upstash(flaky).get_entry("someone")
    assert len(attempts) == 2
    assert entry is not None and entry.value["fetched_at"] == "now"


async def test_upstash_gives_up_after_the_retry():
    attempts = []

    def always_fails(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        raise httpx.ReadTimeout("", request=request)

    assert await _upstash(always_fails).get("someone") is None
    assert len(attempts) == 2
