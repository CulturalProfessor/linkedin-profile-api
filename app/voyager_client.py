"""Thin client over LinkedIn's internal Voyager dash API.

No browser involved: this sends the same XHR requests the profile page's own
JS makes, using a session cookie the caller already holds. Endpoint paths
below are the plain dash resource names (no decorationId) - confirmed live
by hand against a real profile, including the `?q=memberIdentity` resolve
and the per-section `?q=viewee` calls. If resolution starts 404ing, check
the Network tab on a profile page for the real
`identity/dash/profiles?q=...` call and update `PROFILE_RESOLVE_PATH` below.

`VoyagerClient` takes the full `Cookie:` header value to replay, not just
li_at+JSESSIONID in isolation. Sending only those two, stripped of the
cookie jar (bcookie, lidc, ...) a real browser normally carries them in, is
itself a signal LinkedIn's session-anomaly detection can key on - replaying
the full jar looks much closer to a real request. See app/main.py's
`_resolve_session` for how the full jar vs. the minimal li_at/JSESSIONID
pair is chosen.
"""
from __future__ import annotations

import asyncio
import logging
import random

import httpx

logger = logging.getLogger(__name__)

BASE_URL = "https://www.linkedin.com"
PROFILE_RESOLVE_PATH = "/voyager/api/identity/dash/profiles"

# Section name -> Voyager dash resource path. Names match the fixture keys
# 1:1 so denormalize.py can consume {section_name: body} directly.
SECTION_PATHS = {
    "profilePositionGroups": "/voyager/api/identity/dash/profilePositionGroups",
    "profileEducations": "/voyager/api/identity/dash/profileEducations",
    "profileSkills": "/voyager/api/identity/dash/profileSkills",
    "profileCertifications": "/voyager/api/identity/dash/profileCertifications",
    "profileLanguages": "/voyager/api/identity/dash/profileLanguages",
    "profileCourses": "/voyager/api/identity/dash/profileCourses",
    "profileProjects": "/voyager/api/identity/dash/profileProjects",
    "profileHonors": "/voyager/api/identity/dash/profileHonors",
    "profileVolunteerExperiences": "/voyager/api/identity/dash/profileVolunteerExperiences",

    # Confirmed live. This is the primary source for the experience section:
    # it carries per-*role* title, dateRange, description and location, where
    # profilePositionGroups only has one company-level tenure span. See
    # app/denormalize.py::_experience for why that distinction matters.
    "profilePositions": "/voyager/api/identity/dash/profilePositions",
}

# Fetch order, and the only sections actually fetched. Two things matter here:
#
# 1. Only sections denormalize.py consumes are requested. profileCourses,
#    profileProjects, profileHonors and profileVolunteerExperiences are known
#    -good paths that nothing maps yet - fetching them spent four extra
#    requests per profile against the same session to build data that was
#    thrown away, on a sequence that LinkedIn was already throttling.
# 2. Most-valuable first. When throttling does start mid-sequence, whatever is
#    last is what dies. profilePositions - the source of every job title, the
#    real per-role dates and the city - used to be last in iteration order,
#    which is exactly how a profile came back 200 with every title null.
FETCHED_SECTIONS = (
    "profilePositionGroups",
    "profilePositions",
    "profileEducations",
    "profileSkills",
    "profileCertifications",
    "profileLanguages",
)

# Statuses that mean "this session is being rejected or challenged", as opposed
# to "this member has no such section".
_SESSION_REJECTED = frozenset({302, 401, 403})

# Statuses that must abort the fan-out immediately rather than being treated as
# "this member has no such section". 429 joins the rejection statuses here:
# continuing to fire the remaining sections into a rate limit is the surest way
# to turn throttling into revocation.
_FAN_OUT_FATAL = _SESSION_REJECTED | {429}

# Static browser-shaped headers a real Voyager XHR always carries, captured
# from a live profile-page request. Values here are generic/current-Chrome
# defaults, not tied to any one person's device - the point is presence
# (a request missing these entirely reads as obviously non-browser), not
# matching one specific captured fingerprint.
_BROWSER_HEADERS = {
    "accept-language": "en-US,en;q=0.9",
    "referer": "https://www.linkedin.com/",
    "x-li-lang": "en_US",
    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Linux"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
    # Must stay consistent with sec-ch-ua-platform above. This previously
    # claimed "Windows NT 10.0" while the platform hint said "Linux" - a
    # combination no real browser produces, and a far louder anomaly signal
    # than any of these headers being missing. Prefer overriding the whole set
    # with the real ones captured by tools/curl_to_env.py.
    # Every key here is lowercase on purpose. HTTP header names are
    # case-insensitive, but a Python dict merge is not: mixing "User-Agent"
    # here with a captured "user-agent" produces *both* keys, httpx sends two
    # User-Agent headers, and LinkedIn's front-end proxy rejects the malformed
    # request with an HTML 400 that looks nothing like a session problem.
    "user-agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
}


def _parse_retry_after(raw: str | None) -> int | None:
    """LinkedIn sends Retry-After as delta-seconds when it sends one at all.
    The HTTP-date form is legal too but not produced here, so an unparseable
    value is reported as absent rather than guessed at."""
    if not raw:
        return None
    try:
        return max(0, int(raw.strip()))
    except ValueError:
        return None


def extract_cookie_value(cookie_header: str, name: str) -> str | None:
    """Pulls one cookie's value out of a raw `Cookie:` header string,
    stripping the surrounding quotes LinkedIn wraps JSESSIONID in."""
    for part in cookie_header.split(";"):
        part = part.strip()
        if part.startswith(f"{name}="):
            return part[len(name) + 1 :].strip('"')
    return None


def new_http_client(timeout: float = 15.0) -> httpx.AsyncClient:
    """One pooled client for the whole process.

    http2 when the `h2` package is available: Chrome negotiates HTTP/2 with
    linkedin.com, so speaking 1.1 is one more way a replayed session looks
    unlike the browser that created it. Degrades to 1.1 rather than failing to
    start if the extra isn't installed.
    """
    try:
        import h2  # noqa: F401

        http2 = True
    except ImportError:
        logger.warning("h2 not installed - falling back to HTTP/1.1 (pip install 'httpx[http2]')")
        http2 = False
    return httpx.AsyncClient(
        base_url=BASE_URL,
        timeout=timeout,
        http2=http2,
        # httpx keeps an idle connection for only 5s by default, which would
        # undo most of the benefit here: real traffic arrives minutes apart, so
        # every request would still open a fresh connection and burn through
        # the small allowance LinkedIn gives a replayed session before revoking
        # it. A long expiry with a single keep-alive slot makes the process
        # behave like one browser tab holding one connection open.
        limits=httpx.Limits(
            max_connections=4,
            max_keepalive_connections=1,
            keepalive_expiry=600.0,
        ),
    )


class VoyagerError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None, retry_after: int | None = None):
        super().__init__(message)
        self.status_code = status_code
        # Only ever set for a 429, from LinkedIn's own Retry-After when it
        # sends one. Passed through to the caller rather than invented here:
        # the upstream knows how long it wants to be left alone.
        self.retry_after = retry_after


class VoyagerClient:
    def __init__(
        self,
        cookie: str,
        csrf_token: str,
        timeout: float = 15.0,
        min_delay: float = 0.0,
        max_delay: float = 0.0,
        browser_headers: dict[str, str] | None = None,
        http_client: httpx.AsyncClient | None = None,
    ):
        """`cookie` is the full Cookie header value to replay; `csrf_token`
        is the unquoted JSESSIONID value. `min_delay`/`max_delay` bound a
        jittered pause inserted *between* upstream requests - see `_get`.

        `http_client` lets the caller inject a long-lived, shared client so
        connections are pooled across requests. This matters more than it
        looks: building a client per /profile means a fresh TLS handshake per
        fetch, and LinkedIn revokes a replayed session after only a handful of
        new connections - measured at about three. A real browser opens one
        connection and reuses it for everything, which is precisely the
        behaviour being imitated. Auth headers are therefore per-request rather
        than baked into the client, so one pool can serve many callers'
        sessions. When omitted, a private client is created and closed with
        this object (convenient for scripts, wrong for a server).
        """
        self._min_delay = min_delay
        self._max_delay = max_delay
        self._sent_first_request = False
        # Counts requests *issued*, not requests that succeeded. This is the
        # number that measures account exposure (and the one reported as
        # meta.upstream_requests): a request that went out and came back 429
        # cost the session just as much as one that came back 200.
        self.upstream_requests = 0
        self._owns_client = http_client is None
        self._client = http_client or new_http_client(timeout)
        self._headers = {
            # Captured headers override the generic defaults, so the session is
            # presented behind the browser identity that actually created it
            # rather than an invented one.
            **_BROWSER_HEADERS,
            # Lowercased so a captured "User-Agent" replaces the default rather
            # than being sent alongside it as a duplicate header.
            **{k.lower(): v for k, v in (browser_headers or {}).items()},
            "cookie": cookie,
            "csrf-token": csrf_token,
            "x-restli-protocol-version": "2.0.0",
            "accept": "application/vnd.linkedin.normalized+json+2.1",
        }

    async def aclose(self) -> None:
        # Never close a client we were handed - it outlives this object.
        if self._owns_client:
            await self._client.aclose()

    async def _get(self, path: str, params: dict[str, str]) -> dict:
        # Space out every upstream request, not just one per /profile call.
        # Fetching a profile is a fan-out of several Voyager requests, so a
        # single delay before the fan-out leaves the requests themselves going
        # out back-to-back - the exact burst signature the delay exists to
        # avoid. Observed in practice: ten sections returned 200 and the
        # eleventh got a 302 to the login page, after which the session was
        # refused outright.
        if self._sent_first_request and self._max_delay > 0:
            await asyncio.sleep(random.uniform(self._min_delay, self._max_delay))
        self._sent_first_request = True

        self.upstream_requests += 1
        resp = await self._client.get(path, params=params, headers=self._headers)

        # LinkedIn revokes a session by returning Set-Cookie for li_at with
        # Max-Age=0 - a deletion instruction, not an expiry. Worth calling out
        # separately: it means the session was killed server-side during this
        # request, which is a different problem from a cookie that had already
        # gone stale before we started, and it is otherwise invisible.
        for raw_cookie in resp.headers.get_list("set-cookie"):
            if raw_cookie.startswith("li_at=") and "max-age=0" in raw_cookie.lower():
                logger.warning(
                    "LinkedIn revoked this session mid-request (Set-Cookie li_at "
                    "Max-Age=0) on %s - the cookie is now dead and must be recaptured",
                    path,
                )
                break

        if resp.status_code == 429:
            # The single most important upstream status to surface faithfully:
            # it is the earliest signal the account is under pressure, and the
            # only one where the caller's correct response is to back off
            # rather than retry or re-auth.
            retry_after = _parse_retry_after(resp.headers.get("retry-after"))
            logger.warning(
                "LinkedIn rate-limited this session on %s (429, retry-after=%s) - "
                "back off now; continuing to push risks the session being revoked",
                path,
                retry_after,
            )
            raise VoyagerError(
                f"Voyager request to {path} was rate-limited (429)",
                status_code=429,
                retry_after=retry_after,
            )

        if resp.status_code != 200:
            raise VoyagerError(
                f"Voyager request to {path} failed: {resp.status_code}",
                status_code=resp.status_code,
            )
        return resp.json()

    async def fetch_section(self, section: str, profile_urn: str) -> dict:
        path = SECTION_PATHS[section]
        return await self._get(path, {"q": "viewee", "profileUrn": profile_urn})

    async def fetch_profile(self, public_identifier: str) -> dict:
        """Returns {"urn": ..., "profile": <body>, <section>: <body>, ...} -
        the same shape as fixtures/sample_raw.json's per-section "body" values.
        """
        try:
            profile_body = await self._get(
                PROFILE_RESOLVE_PATH,
                {"q": "memberIdentity", "memberIdentity": public_identifier},
            )
        except VoyagerError as exc:
            if exc.status_code in (403, 404):
                # At the *resolve* step these mean "no such member", not "this
                # session is finished" - LinkedIn answers 403 for an identifier
                # that doesn't exist, and the same session keeps working
                # immediately afterwards. Reporting it as an auth failure sends
                # the caller off to recapture a cookie that was never the
                # problem. (Mid-fan-out the same statuses are treated as session
                # rejection, since by then the member has already resolved.)
                raise VoyagerError(
                    f"profile '{public_identifier}' not found or not visible to this session",
                    status_code=404,
                ) from exc
            raise

        urns = profile_body.get("data", {}).get("*elements") or []
        if not urns:
            raise VoyagerError(
                f"profile '{public_identifier}' not found or not visible to this session",
                status_code=404,
            )
        urn = urns[0]
        raw: dict = {"urn": urn, "profile": profile_body}
        for section in FETCHED_SECTIONS:
            try:
                raw[section] = await self.fetch_section(section, urn)
            except VoyagerError as exc:
                if exc.status_code in _FAN_OUT_FATAL:
                    # Not an absent section - the session is being rejected or
                    # challenged mid-sequence. Swallowing this returns a 200
                    # carrying a silently degraded profile (no titles, no city)
                    # and hides a dying session behind an apparently successful
                    # response. Fail instead, so the caller is told to re-auth.
                    raise
                # A genuinely missing section (404) is normal - not every member
                # has certifications or languages. Logged rather than swallowed
                # silently, so an endpoint LinkedIn has changed or retired is
                # distinguishable from a section that is legitimately empty.
                logger.warning(
                    "section %s unavailable for %s: %s", section, public_identifier, exc
                )
                continue
        return raw

    async def __aenter__(self) -> "VoyagerClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()
