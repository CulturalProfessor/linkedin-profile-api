"""Endpoint tests for /profile - the layer that wires the fan-out, the cache,
the quota and the session rules together. Everything here runs against an
httpx.MockTransport: no request leaves the process, and no LinkedIn session is
spent to exercise a 429 or an expired cookie.

The module-level `settings`, `cache` and `quota_backend` in app.main are
singletons built at import, so each test swaps them out and puts them back -
see the `api` fixture.
"""
import json
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
                     "daily_quota", "min_delay", "max_delay")
    }

    def configure(**kwargs):
        for key, value in kwargs.items():
            # Settings is a frozen dataclass; setattr raises FrozenInstanceError.
            object.__setattr__(main.settings, key, value)

    configure(full_cookie=None, li_at=None, jsessionid=None, api_key=None,
      allow_live=True, daily_quota=10, min_delay=0.0, max_delay=0.0)

    old_cache, old_backend, old_limiter = main.cache, main.quota_backend, main.rate_limiter
    main.cache = DiskCache(str(tmp_path / "cache"))
    main.quota_backend = InMemoryQuotaBackend()
    main.rate_limiter = RateLimiter(main.quota_backend, main.settings.daily_quota)

    class Handle:
        set = staticmethod(configure)

        @staticmethod
        def transport(handler):
            main.app.state.http = httpx.AsyncClient(
                base_url="https://www.linkedin.com", transport=httpx.MockTransport(handler)
            )

        @staticmethod
        def limit(daily_quota):
            configure(daily_quota=daily_quota)
            main.rate_limiter = RateLimiter(main.quota_backend, daily_quota)

    try:
        with TestClient(main.app) as client:
            Handle.client = client
            Handle.transport(_voyager_handler())
            yield Handle
    finally:
        main.cache, main.quota_backend, main.rate_limiter = old_cache, old_backend, old_limiter
        configure(**originals)


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


def test_caller_with_own_session_skips_the_api_key(api):
    """They are spending their own account's risk budget, not the backend's."""
    api.set(full_cookie="li_at=backend; JSESSIONID=\"ajax:backend\"", api_key="s3cret")
    assert _get(api).status_code == 200


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
