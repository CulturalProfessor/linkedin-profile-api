from __future__ import annotations

import hashlib
import hmac
import logging
import re
import time
import uuid
from datetime import datetime, timezone

from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse

from app import fields as field_spec
from app.cache import DiskCache
from app.config import settings
from app.denormalize import denormalize
from app.models import Meta, ProfileResponse
from app.quota import InMemoryQuotaBackend, UpstashQuotaBackend
from app.rate_limit import QuotaExceeded, RateLimiter
from app.voyager_client import (
    FETCHED_SECTIONS,
    VoyagerClient,
    VoyagerError,
    extract_cookie_value,
    new_http_client,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """One pooled HTTP client for the process, so connections are reused
    across requests. Building a client per /profile meant a new TLS
    handshake per fetch, and LinkedIn revokes a replayed session after only
    a few new connections - so the API died after a handful of calls."""
    app.state.http = new_http_client()
    if settings.has_backend_session() and not settings.api_key:
        # Not fatal - a laptop run wants exactly this. On a public URL it is
        # an open proxy for the backend LinkedIn account, so it is said out
        # loud rather than left for someone to discover from the quota graph.
        logger.warning(
            "a backend LinkedIn session is configured but API_KEY is unset: "
            "/profile will serve anyone who finds this URL, spending the backend "
            "account's daily quota. Set API_KEY before exposing this publicly."
        )
    try:
        yield
    finally:
        await app.state.http.aclose()

app = FastAPI(
    title="LinkedIn Profile API",
    description=(
        "LinkedIn profile URL in, structured JSON out. Reverse-engineered "
        "against LinkedIn's own Voyager endpoints - no browser automation. "
        "See README for auth model, legal framing, and known limitations."
    ),
    version="0.2.0",
    lifespan=lifespan,
)

cache = DiskCache(settings.cache_dir)
quota_backend = (
    UpstashQuotaBackend(settings.upstash_redis_rest_url, settings.upstash_redis_rest_token)
    if settings.has_shared_quota_store()
    else InMemoryQuotaBackend()
)
rate_limiter = RateLimiter(quota_backend, settings.daily_quota)

_PUBLIC_ID_RE = re.compile(r"linkedin\.com/in/([^/?#]+)")


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    """Stamps every response - success or error - with an id the logs also
    carry. Lives in middleware rather than the endpoint because the
    interesting responses are the failures, and a `raise HTTPException`
    discards whatever the handler had put on its Response object."""
    request_id = uuid.uuid4().hex[:16]
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response


def _keys_match(supplied: str, configured: str | None) -> bool:
    """Constant-time compare, so the API key can't be recovered a byte at a
    time from response timings."""
    return bool(configured) and hmac.compare_digest(supplied, configured)


def _account_key(li_at: str, cookie_header: str | None = None) -> str:
    """Quota bucket identifier, hashed so the raw value is never used as a
    Redis key or written anywhere - a leaked key name shouldn't leak a usable
    cookie.

    Keyed on `bcookie` (LinkedIn's long-lived browser identifier) in
    preference to `li_at`. li_at is reissued on every fresh capture, so keying
    on it gave each recapture a brand-new bucket with a full quota - the
    counter reset exactly when someone was iterating hardest, which is when it
    most needed to hold. bcookie survives re-login on the same browser, so the
    bucket persists across captures the way an account-level cap must.
    Falls back to li_at when bcookie isn't in the jar (the minimal
    x-li-at/x-jsessionid path carries no sibling cookies).
    """
    identifier = None
    if cookie_header:
        identifier = extract_cookie_value(cookie_header, "bcookie")
    return hashlib.sha256((identifier or li_at).encode()).hexdigest()[:16]


def _extract_public_identifier(profile_url: str) -> str:
    profile_url = profile_url.strip()
    match = _PUBLIC_ID_RE.search(profile_url)
    if match:
        return match.group(1)
    if re.fullmatch(r"[A-Za-z0-9\-_%]+", profile_url):
        return profile_url  # caller passed the identifier directly
    raise HTTPException(status_code=400, detail="not a recognizable LinkedIn profile URL")


def _resolve_session(
    x_li_cookie: str | None,
    x_li_at: str | None,
    x_jsessionid: str | None,
    *,
    allow_backend: bool = True,
) -> tuple[str, str, str] | None:
    """Picks a session in order of preference: caller's full cookie jar,
    caller's minimal li_at/JSESSIONID pair, then the backend demo session
    (full jar preferred there too). Returns (li_at, cookie_header, csrf_token)
    or None if nothing usable was supplied anywhere.

    Replaying the full jar (not just li_at+JSESSIONID stripped of everything
    else) is preferred because the minimal pair, presented in isolation from
    the cookies it normally travels with, is itself an anomaly-detection
    signal - see app/voyager_client.py's module docstring.

    `allow_backend=False` answers a different question: did *the caller* bring
    a usable session? That is what decides whether the API key is required, so
    it must not fall through to the backend session - otherwise sending a
    malformed x-li-cookie would look like "caller brought their own", skip the
    key, and then quietly spend the backend account anyway.
    """
    full_cookie = x_li_cookie or (settings.full_cookie if allow_backend else None)
    if full_cookie:
        li_at = extract_cookie_value(full_cookie, "li_at")
        jsessionid = extract_cookie_value(full_cookie, "JSESSIONID")
        if li_at and jsessionid:
            return li_at, full_cookie, jsessionid

    li_at = x_li_at or (settings.li_at if allow_backend else None)
    jsessionid = x_jsessionid or (settings.jsessionid if allow_backend else None)
    if li_at and jsessionid:
        jsessionid = jsessionid.strip('"')
        return li_at, f'li_at={li_at}; JSESSIONID="{jsessionid}"', jsessionid

    return None


def _set_quota_headers(response: Response, remaining: int | None) -> None:
    """Conventional, machine-readable pacing information. Emitted on every
    response that could identify an account, so a client can slow down before
    it gets refused rather than after."""
    response.headers["X-RateLimit-Limit"] = str(rate_limiter.daily_quota)
    response.headers["X-RateLimit-Reset"] = str(rate_limiter.resets_at())
    if remaining is not None:
        response.headers["X-RateLimit-Remaining"] = str(remaining)


async def _quota_remaining(account_key: str | None) -> int | None:
    """Best-effort: the shared quota store is over the network, and a response
    that is otherwise perfectly good must never fail because Upstash blipped.
    None means 'unknown', which is what the header and meta then report."""
    if account_key is None:
        return None
    try:
        return await rate_limiter.remaining_today(account_key)
    except Exception as exc:  # noqa: BLE001 - deliberately catch-all, see above
        logger.warning("could not read quota (reporting unknown): %s", exc)
        return None


def _render(payload: ProfileResponse, wanted: frozenset[str], response: Response):
    """Drops unrequested keys from `profile` entirely rather than returning
    them empty. An absent key says "you didn't ask for this"; `"skills": []`
    says "this member has no skills" - conflating the two would make a narrow
    query look like a member with a very sparse profile.

    Returned as a JSONResponse so the headers set on `response` survive: a
    handler that returns a Response object replaces FastAPI's own, and the
    rate-limit headers live on the injected one.
    """
    data = payload.model_dump()
    if wanted != field_spec.ALL_FIELDS:
        data["profile"] = {k: v for k, v in data["profile"].items() if k in wanted}
    return JSONResponse(data, headers=dict(response.headers))


@app.get("/health")
async def health() -> dict:
    payload = {
        "ok": True,
        "allow_live": settings.allow_live,
        "shared_quota_store": settings.has_shared_quota_store(),
        "api_key_required": settings.requires_api_key(),
        "daily_quota": settings.daily_quota,
        "quota_resets_at": rate_limiter.resets_at(),
    }
    backend_session = _resolve_session(None, None, None)
    if backend_session is not None:
        # The quota is per-account (see _account_key) - this reports the
        # backend demo session's own bucket, not a global figure. Callers
        # supplying their own session draw from a separate bucket entirely.
        li_at, backend_cookie, _ = backend_session
        payload["backend_session_remaining_quota_today"] = await rate_limiter.remaining_today(
            _account_key(li_at, backend_cookie)
        )
    return payload


@app.get("/profile", response_model=ProfileResponse)
async def get_profile(
    request: Request,
    response: Response,
    url: str = Query(..., description="LinkedIn profile URL, e.g. https://www.linkedin.com/in/someone"),
    force_refresh: bool = Query(False, description="Bypass cache and re-fetch live"),
    fields: str | None = Query(
        None,
        description=(
            "Comma-separated subset of output fields to return, e.g. "
            "`name,headline` or `experience,education`. Defaults to all of them. "
            "Narrowing cuts real latency: most fields map to one Voyager section "
            "each, and sections are fetched one at a time with a pause between, "
            "so `?fields=name,headline` costs one upstream request (~0.5s) against "
            "seven (~9.5s) for the full set. public_identifier and name are always "
            "included - they are free. Valid: "
            + ", ".join(sorted(field_spec.ALL_FIELDS))
        ),
    ),
    x_api_key: str | None = Header(
        default=None,
        description=(
            "Required when this deployment carries a backend session and the caller "
            "does not supply one of their own - the key is what stops a stranger "
            "spending the backend account's quota. Not needed if you send your own "
            "x-li-cookie."
        ),
    ),
    x_li_cookie: str | None = Header(
        default=None,
        description=(
            "Recommended: the full Cookie header value from a real linkedin.com request "
            "(DevTools -> Network -> Copy as cURL). Overrides x-li-at/x-jsessionid when "
            "present - replaying the whole cookie jar reads much closer to a real browser "
            "than li_at+JSESSIONID alone."
        ),
    ),
    x_li_at: str | None = Header(
        default=None,
        description="Minimal alternative to x-li-cookie: just the li_at session cookie.",
    ),
    x_jsessionid: str | None = Header(
        default=None,
        description="Paired with x-li-at when not using x-li-cookie.",
    ),
) -> ProfileResponse:
    started = time.perf_counter()
    request_id = getattr(request.state, "request_id", uuid.uuid4().hex[:16])

    def elapsed_ms() -> int:
        return int((time.perf_counter() - started) * 1000)

    public_id = _extract_public_identifier(url)
    try:
        wanted = field_spec.parse(fields)
    except field_spec.UnknownField as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    sections = field_spec.sections_for(wanted, FETCHED_SECTIONS)

    # Does the *caller* have a session of their own? This decides both whether
    # the API key is required and whose quota bucket the call lands in.
    caller_session = _resolve_session(x_li_cookie, x_li_at, x_jsessionid, allow_backend=False)
    if caller_session is None and settings.requires_api_key():
        if not x_api_key or not _keys_match(x_api_key, settings.api_key):
            logger.warning(
                "request_id=%s public_id=%s outcome=rejected reason=bad_api_key",
                request_id, public_id,
            )
            raise HTTPException(
                status_code=401,
                detail=(
                    "this deployment requires an x-api-key header, or your own "
                    "LinkedIn session via x-li-cookie"
                ),
            )

    session = caller_session or _resolve_session(x_li_cookie, x_li_at, x_jsessionid)
    account_key = _account_key(session[0], session[1]) if session else None

    if not force_refresh:
        entry = cache.get_entry(public_id)
        if entry is not None:
            remaining = await _quota_remaining(account_key)
            _set_quota_headers(response, remaining)
            logger.info(
                "request_id=%s public_id=%s outcome=ok source=cache duration_ms=%d "
                "upstream_requests=0 cache_age_s=%d quota_remaining=%s",
                request_id, public_id, elapsed_ms(), entry.age_seconds, remaining,
            )
            # A cached entry is always complete (see the write below), so it
            # can serve any field subset - `?fields=name` off a warm cache
            # costs nothing at all.
            return _render(
                ProfileResponse(
                    **entry.value,
                    source="cache",
                    meta=Meta(
                        source="cache",
                        fetched_at=entry.value["fetched_at"],
                        request_id=request_id,
                        duration_ms=elapsed_ms(),
                        upstream_requests=0,
                        cache_age_seconds=entry.age_seconds,
                        quota_remaining=remaining,
                        fields=sorted(wanted),
                    ),
                ),
                wanted,
                response,
            )

    # Deliberately above session resolution: with live fetching switched off,
    # "no session available" is a true statement but the wrong answer - it
    # sends the operator to recapture a cookie when the actual state is that
    # the kill switch is on and no cookie would have helped.
    if not settings.allow_live:
        raise HTTPException(status_code=503, detail="live fetches are disabled (kill switch)")

    if session is None:
        raise HTTPException(
            status_code=401,
            detail=(
                "no session available: pass x-li-cookie (recommended) or "
                "x-li-at + x-jsessionid headers, or configure a backend demo session"
            ),
        )
    li_at, cookie_header, csrf_token = session
    assert account_key is not None

    try:
        remaining = await rate_limiter.before_live_fetch(account_key)
    except QuotaExceeded as exc:
        logger.warning(
            "request_id=%s public_id=%s outcome=rejected reason=quota_exceeded",
            request_id, public_id,
        )
        raise HTTPException(
            status_code=429,
            detail=str(exc),
            headers={
                "Retry-After": str(exc.retry_after),
                "X-RateLimit-Limit": str(rate_limiter.daily_quota),
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset": str(rate_limiter.resets_at()),
            },
        ) from exc

    upstream_requests = 0
    async with VoyagerClient(
        cookie_header,
        csrf_token,
        http_client=app.state.http,
        min_delay=settings.min_delay,
        max_delay=settings.max_delay,
        browser_headers=settings.browser_headers,
    ) as client:
        try:
            raw = await client.fetch_profile(public_id, sections)
        except VoyagerError as exc:
            upstream_requests = client.upstream_requests
            status = exc.status_code
            if upstream_requests == 0:
                # Nothing reached LinkedIn, so nothing was spent on the
                # account - see RateLimiter.refund. A fetch that did put
                # traffic on the session is not refunded, however it ended.
                remaining = await rate_limiter.refund(account_key)
            logger.warning(
                "request_id=%s public_id=%s outcome=error source=live status=%s "
                "upstream_requests=%d duration_ms=%d: %s",
                request_id, public_id, status, upstream_requests, elapsed_ms(), exc,
            )
            quota_headers = {
                "X-RateLimit-Limit": str(rate_limiter.daily_quota),
                "X-RateLimit-Remaining": str(remaining),
                "X-RateLimit-Reset": str(rate_limiter.resets_at()),
            }
            if status == 404:
                raise HTTPException(
                    status_code=404,
                    detail="profile not found, or not visible to the session in use",
                    headers=quota_headers,
                ) from exc
            if status == 429:
                # LinkedIn throttling us is the one upstream status the caller
                # can act on correctly, and mapping it to a generic 502 threw
                # that away. Retry-After is LinkedIn's own when it sent one.
                raise HTTPException(
                    status_code=429,
                    detail="LinkedIn is rate-limiting this session - back off and retry later",
                    headers={**quota_headers, "Retry-After": str(exc.retry_after or 60)},
                ) from exc
            if status in (401, 403):
                raise HTTPException(
                    status_code=401,
                    detail="session cookie rejected by LinkedIn",
                    headers=quota_headers,
                ) from exc
            if status == 302:
                # LinkedIn redirects to the login/authwall page instead of
                # returning JSON when the session cookie is invalid or
                # expired - this is an auth problem, not an infra one.
                raise HTTPException(
                    status_code=401,
                    detail="session expired or invalid: LinkedIn redirected to login - capture a fresh session",
                    headers=quota_headers,
                ) from exc
            raise HTTPException(
                status_code=502,
                detail=f"upstream error: {exc}",
                headers=quota_headers,
            ) from exc
        upstream_requests = client.upstream_requests

    profile, limitations = denormalize(public_id, raw, wanted)
    payload = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "profile": profile,
        "limitations": limitations,
    }
    # Only a complete fetch is cached. A narrowed one is missing sections, and
    # storing it would let `?fields=name` poison the entry that a later full
    # request reads - the caller would get a 200 carrying empty experience and
    # education, indistinguishable from a member who has neither.
    if wanted == field_spec.ALL_FIELDS:
        cache.set(public_id, {**payload, "profile": profile.model_dump()})

    _set_quota_headers(response, remaining)
    logger.info(
        "request_id=%s public_id=%s outcome=ok source=live duration_ms=%d "
        "upstream_requests=%d quota_remaining=%s fields=%s",
        request_id, public_id, elapsed_ms(), upstream_requests, remaining,
        "all" if wanted == field_spec.ALL_FIELDS else ",".join(sorted(wanted)),
    )
    return _render(
        ProfileResponse(
            **payload,
            source="live",
            meta=Meta(
                source="live",
                fetched_at=payload["fetched_at"],
                request_id=request_id,
                duration_ms=elapsed_ms(),
                upstream_requests=upstream_requests,
                cache_age_seconds=None,
                quota_remaining=remaining,
                fields=sorted(wanted),
            ),
        ),
        wanted,
        response,
    )
