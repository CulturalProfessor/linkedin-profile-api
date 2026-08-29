"""Offline tests for the fan-out, using httpx's MockTransport - no network."""
import httpx
import pytest

from app.voyager_client import FETCHED_SECTIONS, VoyagerClient, VoyagerError

URN = "urn:li:fsd_profile:ACoAAFake"


def _empty_section() -> dict:
    return {"data": {"*elements": []}, "included": []}


def _resolve_body() -> dict:
    return {"data": {"*elements": [URN]}, "included": []}


def _client(handler, **kwargs) -> VoyagerClient:
    client = VoyagerClient("li_at=x; JSESSIONID=\"ajax:1\"", "ajax:1", **kwargs)
    client._client = httpx.AsyncClient(
        base_url="https://www.linkedin.com", transport=httpx.MockTransport(handler)
    )
    return client


def test_captured_headers_replace_defaults_without_duplicating_them():
    """HTTP header names are case-insensitive but dict keys are not. Merging a
    captured "User-Agent" over a default "User-Agent" once produced *two*
    user-agent headers, and LinkedIn's front-end proxy answered the malformed
    request with an HTML 400 that looked nothing like a session problem."""
    client = VoyagerClient(
        'li_at=x; JSESSIONID="ajax:1"',
        "ajax:1",
        browser_headers={
            "User-Agent": "Captured/1.0",
            "Sec-CH-UA-Platform": '"Linux"',
        },
        http_client=httpx.AsyncClient(),
    )

    # Auth + fingerprint headers are per-request now, so one pooled connection
    # can serve several callers' sessions.
    assert all(key == key.lower() for key in client._headers)
    assert client._headers["user-agent"] == "Captured/1.0"
    assert client._headers["sec-ch-ua-platform"] == '"Linux"'
    assert client._headers["cookie"] == 'li_at=x; JSESSIONID="ajax:1"'


@pytest.mark.asyncio
async def test_injected_client_is_reused_and_not_closed():
    """A per-request client means a new TLS handshake per fetch, and LinkedIn
    revokes a replayed session after only a few new connections. The pooled
    client outlives any one VoyagerClient, so closing it here would break
    every later request."""
    shared = httpx.AsyncClient(
        base_url="https://www.linkedin.com",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json=_resolve_body() if "q=memberIdentity" in str(request.url) else _empty_section(),
            )
        ),
    )
    async with VoyagerClient("li_at=x", "ajax:1", http_client=shared) as client:
        assert client._client is shared
        await client.fetch_profile("someone")

    assert not shared.is_closed
    await shared.aclose()


def test_default_headers_are_internally_consistent():
    """A Windows User-Agent alongside sec-ch-ua-platform "Linux" is a
    combination no real browser emits - louder than sending neither."""
    from app.voyager_client import _BROWSER_HEADERS

    ua = _BROWSER_HEADERS["user-agent"]
    platform = _BROWSER_HEADERS["sec-ch-ua-platform"].strip('"')
    assert platform.lower() in ua.lower() or (platform == "Linux" and "X11" in ua)
    assert all(key == key.lower() for key in _BROWSER_HEADERS)


@pytest.mark.asyncio
async def test_only_consumed_sections_are_fetched():
    """Every extra section is another request against the same session. The
    four unmapped ones (courses, projects, honors, volunteering) were being
    fetched and thrown away."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path.rsplit("/", 1)[-1])
        body = _resolve_body() if "q=memberIdentity" in str(request.url) else _empty_section()
        return httpx.Response(200, json=body)

    async with _client(handler) as client:
        await client.fetch_profile("someone")

    assert seen[0] == "profiles"  # the resolve
    assert seen[1:] == list(FETCHED_SECTIONS)
    for unmapped in ("profileCourses", "profileProjects", "profileHonors"):
        assert unmapped not in seen


@pytest.mark.asyncio
async def test_positions_fetched_early_so_throttling_hits_optional_sections_first():
    """When LinkedIn starts refusing mid-sequence, whatever is last dies.
    profilePositions carries every job title, the real per-role dates and the
    city, so it must not be last - it was, and a profile came back 200 with
    every title null."""
    order = list(FETCHED_SECTIONS)
    assert order.index("profilePositions") <= 1
    assert order[-1] != "profilePositions"


@pytest.mark.asyncio
async def test_rejected_session_mid_fanout_raises_instead_of_degrading():
    """A 302 to the login page is not 'this member has no positions'. Swallowing
    it returns 200 with a silently gutted profile and hides a dying session."""

    def handler(request: httpx.Request) -> httpx.Response:
        if "q=memberIdentity" in str(request.url):
            return httpx.Response(200, json=_resolve_body())
        if request.url.path.endswith("profilePositions"):
            return httpx.Response(302, text="")
        return httpx.Response(200, json=_empty_section())

    async with _client(handler) as client:
        with pytest.raises(VoyagerError) as exc:
            await client.fetch_profile("someone")
    assert exc.value.status_code == 302


@pytest.mark.asyncio
async def test_missing_optional_section_is_tolerated():
    """A 404 genuinely does mean the member has no such section."""

    def handler(request: httpx.Request) -> httpx.Response:
        if "q=memberIdentity" in str(request.url):
            return httpx.Response(200, json=_resolve_body())
        if request.url.path.endswith("profileLanguages"):
            return httpx.Response(404, text="")
        return httpx.Response(200, json=_empty_section())

    async with _client(handler) as client:
        raw = await client.fetch_profile("someone")

    assert "profileLanguages" not in raw
    assert "profilePositions" in raw


@pytest.mark.asyncio
async def test_delay_is_applied_between_requests_not_once_per_profile(monkeypatch):
    """The regression: one pause before the fan-out still let every request in
    it go out back-to-back. There must be a pause between each pair."""
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("app.voyager_client.asyncio.sleep", fake_sleep)

    def handler(request: httpx.Request) -> httpx.Response:
        body = _resolve_body() if "q=memberIdentity" in str(request.url) else _empty_section()
        return httpx.Response(200, json=body)

    async with _client(handler, min_delay=0.8, max_delay=2.5) as client:
        await client.fetch_profile("someone")

    # One request to resolve + one per fetched section; a gap between each pair.
    assert len(sleeps) == len(FETCHED_SECTIONS)
    assert all(0.8 <= s <= 2.5 for s in sleeps)


@pytest.mark.asyncio
async def test_unknown_profile_is_404_not_an_auth_error():
    """LinkedIn answers 403 at the resolve step for an identifier that doesn't
    exist, and the same session keeps working right afterwards. Reporting that
    as 401 "session expired - capture a fresh session" sends the caller to
    re-auth over a cookie that was never the problem."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="")

    async with _client(handler) as client:
        with pytest.raises(VoyagerError) as exc:
            await client.fetch_profile("nobody-here")
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_resolve_returning_no_urns_is_also_404():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"*elements": []}, "included": []})

    async with _client(handler) as client:
        with pytest.raises(VoyagerError) as exc:
            await client.fetch_profile("nobody-here")
    assert exc.value.status_code == 404
