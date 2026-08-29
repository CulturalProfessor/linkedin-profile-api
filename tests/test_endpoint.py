"""Endpoint tests for /profile - the layer that wires the fan-out, the cache,
the quota and the session rules together. Everything here runs against an
httpx.MockTransport: no request leaves the process, and no LinkedIn session is
spent to exercise a 429 or an expired cookie.

The module-level `settings`, `cache` and `quota_backend` in app.main are
singletons built at import, so each test swaps them out and puts them back -
see the `api` fixture.
"""
import json
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.cache import DiskCache
from app.quota import InMemoryQuotaBackend
from app.rate_limit import RateLimiter

FIXTURE = json.loads((Path(__file__).parent.parent / "fixtures" / "sample_raw.json").read_text())
URN = FIXTURE["urn"]
PROFILE_URL = "https://www.linkedin.com/in/jamie-lin-synthetic"
CALLER_COOKIE = 'li_at=caller-token; JSESSIONID="ajax:caller"; bcookie="v=2&caller"'


def _voyager_handler(*, resolve_status=200, section_status=200, retry_after=None, calls=None):
    """Serves the synthetic fixture for every Voyager path, so a full fan-out
    reassembles into a real Profile without a network call."""

    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(str(request.url))
        if request.url.path.endswith("/dash/profiles"):
            if resolve_status != 200:
                return httpx.Response(resolve_status, text="nope")
            return httpx.Response(200, json=FIXTURE["profile"]["body"])
        if section_status != 200:
            headers = {"retry-after": str(retry_after)} if retry_after else {}
            return httpx.Response(section_status, text="nope", headers=headers)
        # Fixture entries are {"status": ..., "body": ...}; the body is what
        # the real endpoint returns.
        section = request.url.path.rsplit("/", 1)[-1]
        entry = FIXTURE.get(section) or {}
        return httpx.Response(200, json=entry.get("body", {"data": {"*elements": []}, "included": []}))

    return handler


@pytest.fixture
def api(tmp_path, monkeypatch):
    """A TestClient with an isolated cache dir, a fresh in-memory quota, no
    backend session and no upstream pacing. Returns a small handle so tests
    can reconfigure settings without reaching into module globals themselves.
    """
    originals = {
        name: getattr(main.settings, name)
        for name in ("full_cookie", "li_at", "jsessionid", "api_key", "allow_live",
                     "daily_quota", "global_daily_quota", "min_delay", "max_delay")
    }

    def configure(**kwargs):
        # Plain setattr: Settings is a pydantic model now, not a frozen
        # dataclass that needed object.__setattr__ to get past it.
        for key, value in kwargs.items():
            setattr(main.settings, key, value)

    configure(full_cookie=None, li_at=None, jsessionid=None, api_key=None,
              allow_live=True, daily_quota=10, global_daily_quota=1000,
              min_delay=0.0, max_delay=0.0)

    main._refreshing.clear()
    old_cache, old_backend, old_limiter = main.cache, main.quota_backend, main.rate_limiter
    main.cache = DiskCache(str(tmp_path / "cache"))
    main.quota_backend = InMemoryQuotaBackend()
    main.rate_limiter = RateLimiter(main.quota_backend, main.settings.daily_quota,
                                    main.settings.global_daily_quota)

    class Handle:
        set = staticmethod(configure)

        @staticmethod
        def transport(handler):
            main.app.state.http = httpx.AsyncClient(
                base_url="https://www.linkedin.com", transport=httpx.MockTransport(handler)
            )

        @staticmethod
        def limit(daily_quota, global_daily_quota=1000):
            configure(daily_quota=daily_quota, global_daily_quota=global_daily_quota)
            main.rate_limiter = RateLimiter(main.quota_backend, daily_quota,
                                            global_daily_quota)

    try:
        with TestClient(main.app) as client:
            Handle.client = client
            Handle.transport(_voyager_handler())
            yield Handle
    finally:
        main._refreshing.clear()
        main.cache, main.quota_backend, main.rate_limiter = old_cache, old_backend, old_limiter
        configure(**originals)


def _drain(seconds=0.2):
    """Lets the background refresh task actually run.

    The endpoint returns as soon as the stale response is sent - that is the
    whole point of the feature - leaving the refresh pending. TestClient runs
    the event loop on its own thread, so a plain sleep here does yield to it.
    Nothing in production waits for the refresh; the tests have to.
    """
    time.sleep(seconds)


def _get(api, **kwargs):
    headers = kwargs.pop("headers", {"x-li-cookie": CALLER_COOKIE})
    params = {"url": PROFILE_URL, **kwargs}
    return api.client.get("/profile", params=params, headers=headers)


def test_live_fetch_returns_profile_and_meta(api):
    resp = _get(api)
    assert resp.status_code == 200
    body = resp.json()

    assert body["source"] == "live"  # deprecated top-level key still present
    meta = body["meta"]
    assert meta["source"] == "live"
    assert meta["upstream_requests"] == 7  # one resolve + six sections
    assert meta["cache_age_seconds"] is None
    assert meta["quota_remaining"] == 9
    assert meta["request_id"] == resp.headers["x-request-id"]
    assert meta["duration_ms"] >= 0
    assert body["profile"]["name"]


def test_rate_limit_headers_on_success(api):
    resp = _get(api)
    assert resp.headers["x-ratelimit-limit"] == "10"
    assert resp.headers["x-ratelimit-remaining"] == "9"
    assert int(resp.headers["x-ratelimit-reset"]) > 0


def test_second_call_is_served_from_cache_without_upstream_requests(api):
    calls = []
    api.transport(_voyager_handler(calls=calls))
    assert _get(api).status_code == 200
    assert len(calls) == 7

    body = _get(api).json()
    assert body["meta"]["source"] == "cache"
    assert body["meta"]["upstream_requests"] == 0
    assert body["meta"]["cache_age_seconds"] == 0
    assert len(calls) == 7  # nothing new went upstream
    # A cache hit must not spend quota - it never touched the account.
    assert body["meta"]["quota_remaining"] == 9


def test_force_refresh_bypasses_the_cache(api):
    calls = []
    api.transport(_voyager_handler(calls=calls))
    _get(api)
    body = _get(api, force_refresh="true").json()
    assert body["meta"]["source"] == "live"
    assert len(calls) == 14


def test_unknown_profile_is_404(api):
    api.transport(_voyager_handler(resolve_status=404))
    resp = _get(api)
    assert resp.status_code == 404
    # Even a failure reports where the account's budget stands.
    assert resp.headers["x-ratelimit-remaining"] == "9"


def test_upstream_429_is_surfaced_as_429_with_retry_after(api):
    """The regression this guards: LinkedIn throttling used to arrive as a
    generic 502, which tells a client to retry rather than to back off."""
    api.transport(_voyager_handler(section_status=429, retry_after=120))
    resp = _get(api)
    assert resp.status_code == 429
    assert resp.headers["retry-after"] == "120"


def test_expired_session_redirect_is_401(api):
    api.transport(_voyager_handler(section_status=302))
    assert _get(api).status_code == 401


def test_no_session_anywhere_is_401(api):
    resp = _get(api, headers={})
    assert resp.status_code == 401
    assert "no session available" in resp.json()["detail"]


def test_kill_switch_beats_missing_session(api):
    """With live fetches off, 'no session available' is true but misleading -
    no cookie would have helped. The 503 has to win."""
    api.set(allow_live=False)
    resp = _get(api, headers={})
    assert resp.status_code == 503
    assert "kill switch" in resp.json()["detail"]


def test_kill_switch_still_serves_cache(api):
    _get(api)
    api.set(allow_live=False)
    resp = _get(api, headers={})
    assert resp.status_code == 200
    assert resp.json()["meta"]["source"] == "cache"


def test_quota_exhaustion_is_429_with_headers(api):
    api.limit(1)
    assert _get(api).status_code == 200
    resp = _get(api, force_refresh="true")
    assert resp.status_code == 429
    assert resp.headers["x-ratelimit-remaining"] == "0"
    assert int(resp.headers["retry-after"]) > 0


def test_api_key_required_when_backend_session_is_configured(api):
    api.set(full_cookie=CALLER_COOKIE, api_key="s3cret")
    assert _get(api, headers={}).status_code == 401
    assert _get(api, headers={"x-api-key": "wrong"}).status_code == 401
    assert _get(api, headers={"x-api-key": "s3cret"}).status_code == 200


def test_caller_with_own_session_still_needs_the_api_key(api):
    """Bringing your own cookie means your account absorbs the quota. It does
    not mean you may use someone else's server to reach LinkedIn."""
    api.set(full_cookie="li_at=backend; JSESSIONID=\"ajax:backend\"", api_key="s3cret")
    assert _get(api).status_code == 401
    assert _get(api, headers={"x-li-cookie": CALLER_COOKIE,
                              "x-api-key": "s3cret"}).status_code == 200


def test_junk_cookie_cannot_buy_a_free_pass(api):
    """The hole this closes: any well-formed cookie made `caller_session`
    non-None, which used to skip the key check entirely - after which the
    request still went out over this server's pooled connection and IP."""
    api.set(full_cookie=CALLER_COOKIE, api_key="s3cret")
    junk = {"x-li-cookie": 'li_at=garbage; JSESSIONID="garbage"'}
    assert _get(api, headers=junk).status_code == 401


def test_global_ceiling_caps_callers_who_mint_new_buckets(api):
    """The per-account bucket is derived from the caller's own cookie, so
    varying it hands out a fresh full quota every request. Without a global
    figure the daily quota bounds a cooperative caller and nobody else."""
    api.limit(daily_quota=10, global_daily_quota=2)
    for i in range(2):
        cookie = f'li_at=tok{i}; JSESSIONID="ajax:{i}"; bcookie="v=2&{i}"'
        assert _get(api, headers={"x-li-cookie": cookie},
                    force_refresh="true").status_code == 200

    third = 'li_at=tok9; JSESSIONID="ajax:9"; bcookie="v=2&9"'
    resp = _get(api, headers={"x-li-cookie": third}, force_refresh="true")
    assert resp.status_code == 429
    assert "deployment's daily ceiling" in resp.json()["detail"]


def test_malformed_caller_cookie_does_not_bypass_the_api_key(api):
    """A cookie missing JSESSIONID falls through to the backend session, so it
    must not count as 'the caller brought their own' for the key check -
    otherwise any junk header buys free use of the backend account."""
    api.set(full_cookie=CALLER_COOKIE, api_key="s3cret")
    resp = _get(api, headers={"x-li-cookie": "li_at=only-half"})
    assert resp.status_code == 401
    assert "x-api-key" in resp.json()["detail"]


def test_health_reports_posture(api):
    api.set(full_cookie=CALLER_COOKIE, api_key="s3cret")
    body = api.client.get("/health").json()
    assert body["ok"] is True
    assert body["api_key_required"] is True
    assert body["daily_quota"] == 10


# --- ?fields= -----------------------------------------------------------


def test_narrow_fields_costs_one_upstream_request(api):
    """The whole point: name and headline come off the resolve call, so
    asking for only those must not pay for six section fetches."""
    calls = []
    api.transport(_voyager_handler(calls=calls))
    body = _get(api, fields="name,headline").json()

    assert len(calls) == 1
    assert body["meta"]["upstream_requests"] == 1
    assert body["profile"]["name"]
    assert "headline" in body["profile"]


def test_unrequested_fields_are_absent_not_empty(api):
    """An absent key means 'you didn't ask'; `"skills": []` would mean 'this
    member has no skills'. Conflating them makes a narrow query look like a
    very sparse profile."""
    body = _get(api, fields="name").json()
    assert "skills" not in body["profile"]
    assert "experience" not in body["profile"]
    # public_identifier and name ride along free on the resolve call.
    assert body["profile"]["public_identifier"]
    assert body["meta"]["fields"] == ["name", "public_identifier"]


def test_experience_pulls_both_position_sections(api):
    calls = []
    api.transport(_voyager_handler(calls=calls))
    body = _get(api, fields="experience").json()
    assert len(calls) == 3  # resolve + positionGroups + positions
    assert body["profile"]["experience"]


def test_location_pulls_positions_so_the_city_still_resolves(api):
    """?fields=location would silently degrade to a country code without the
    positions response to resolve the geoUrn against."""
    calls = []
    api.transport(_voyager_handler(calls=calls))
    _get(api, fields="location")
    assert any("profilePositions" in url for url in calls)


def test_default_is_every_field(api):
    body = _get(api).json()
    assert body["meta"]["upstream_requests"] == 7
    assert set(body["meta"]["fields"]) == {
        "public_identifier", "name", "headline", "location", "about",
        "experience", "education", "skills", "certifications", "languages", "images",
    }


def test_unknown_field_is_400(api):
    resp = _get(api, fields="name,nonsense")
    assert resp.status_code == 400
    assert "nonsense" in resp.json()["detail"]


def test_narrow_fetch_does_not_poison_the_cache(api):
    """A narrowed fetch is missing sections. Caching it would let
    ?fields=name serve a later full request an entry with empty experience
    and education - a 200 that looks like a member who has neither."""
    calls = []
    api.transport(_voyager_handler(calls=calls))
    _get(api, fields="name")
    assert len(calls) == 1

    body = _get(api).json()  # full request must go live, not read that entry
    assert body["meta"]["source"] == "live"
    assert body["profile"]["experience"]
    assert len(calls) == 8


def test_narrow_query_is_free_off_a_warm_cache(api):
    """A cached entry is always complete, so it can serve any subset."""
    calls = []
    api.transport(_voyager_handler(calls=calls))
    _get(api)
    body = _get(api, fields="name,skills").json()

    assert body["meta"]["source"] == "cache"
    assert body["meta"]["upstream_requests"] == 0
    assert len(calls) == 7
    assert "experience" not in body["profile"]


def test_rate_limit_headers_survive_field_narrowing(api):
    """The pruned response is a hand-built JSONResponse, which replaces the
    injected one - the headers set on it have to be carried over by hand."""
    resp = _get(api, fields="name")
    assert resp.headers["x-ratelimit-remaining"] == "9"
    assert resp.headers["x-request-id"]


# --- stale-while-revalidate, and stale on failure ------------------------


def _expire(cache_dir, key="jamie-lin-synthetic", age=48 * 3600):
    """Ages a cache entry on disk past its TTL, without waiting a day."""
    import json
    path = next(p for p in cache_dir.rglob(f"{key}.json"))
    entry = json.loads(path.read_text())
    entry["cached_at"] = time.time() - age
    path.write_text(json.dumps(entry))


def test_expired_entry_is_served_immediately_as_stale(api, tmp_path):
    calls = []
    api.transport(_voyager_handler(calls=calls))
    _get(api)
    _expire(tmp_path)

    body = _get(api).json()
    assert body["meta"]["source"] == "stale"
    assert body["meta"]["upstream_requests"] == 0
    assert body["meta"]["cache_age_seconds"] > 24 * 3600
    # The caller is never silently handed old data.
    assert any("expired cache entry" in note for note in body["limitations"])


def test_stale_response_triggers_a_background_refresh(api, tmp_path):
    calls = []
    api.transport(_voyager_handler(calls=calls))
    _get(api)
    _expire(tmp_path)
    assert len(calls) == 7

    assert _get(api).json()["meta"]["source"] == "stale"
    _drain()
    assert len(calls) == 14  # the refresh ran after the response was sent

    # And the refreshed entry is fresh again.
    assert _get(api).json()["meta"]["source"] == "cache"


def test_no_second_refresh_while_one_is_already_in_flight(api, tmp_path):
    """Ten requests for one stale profile must not launch ten fan-outs - that
    would be seventy upstream requests to produce one cache entry. Simulated
    by marking a refresh in flight, because a real one finishes too fast here
    to overlap with the next request."""
    calls = []
    api.transport(_voyager_handler(calls=calls))
    _get(api)
    _expire(tmp_path)

    main._refreshing.add("jamie-lin-synthetic")
    for _ in range(5):
        assert _get(api).json()["meta"]["source"] == "stale"
    _drain()
    assert len(calls) == 7  # no refresh launched on top of the one "running"


def test_failed_refresh_keeps_the_stale_entry(api, tmp_path):
    """Losing good stale data because the refresh of it failed is the one
    outcome that would make stale-while-revalidate worse than plain expiry."""
    api.transport(_voyager_handler())
    _get(api)
    _expire(tmp_path)

    api.transport(_voyager_handler(section_status=302))  # session dies
    assert _get(api).json()["meta"]["source"] == "stale"
    _drain()

    body = _get(api).json()
    assert body["meta"]["source"] == "stale"
    assert body["profile"]["name"]  # still there, not evicted


def test_dead_session_serves_stale_instead_of_401(api, tmp_path):
    """A dead session used to 401 even for profiles sitting in cache, which
    needed no session at all."""
    api.transport(_voyager_handler())
    _get(api)
    _expire(tmp_path, age=0)  # still fresh, but force_refresh will go live

    api.transport(_voyager_handler(section_status=302))
    resp = _get(api, force_refresh="true")
    assert resp.status_code == 200
    body = resp.json()
    assert body["meta"]["source"] == "stale"
    assert any("upstream status 302" in note for note in body["limitations"])


def test_upstream_429_serves_stale_when_a_copy_exists(api):
    api.transport(_voyager_handler())
    _get(api)
    api.transport(_voyager_handler(section_status=429, retry_after=60))
    body = _get(api, force_refresh="true").json()
    assert body["meta"]["source"] == "stale"


def test_404_never_serves_stale(api):
    """'No such member' may mean the profile was deleted or renamed. Answering
    that with old data asserts something that is no longer true."""
    api.transport(_voyager_handler())
    _get(api)
    api.transport(_voyager_handler(resolve_status=404))
    assert _get(api, force_refresh="true").status_code == 404


def test_stale_response_can_still_be_narrowed(api, tmp_path):
    api.transport(_voyager_handler())
    _get(api)
    _expire(tmp_path)
    body = _get(api, fields="name").json()
    assert body["meta"]["source"] == "stale"
    assert "experience" not in body["profile"]
