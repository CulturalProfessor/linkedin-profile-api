from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone

from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Query

from app.cache import DiskCache
from app.config import settings
from app.denormalize import denormalize
from app.models import ProfileResponse
from app.quota import InMemoryQuotaBackend, UpstashQuotaBackend
from app.rate_limit import QuotaExceeded, RateLimiter
from app.voyager_client import (
    VoyagerClient,
    VoyagerError,
    extract_cookie_value,
    new_http_client,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """One pooled HTTP client for the process, so connections are reused
    across requests. Building a client per /profile meant a new TLS
    handshake per fetch, and LinkedIn revokes a replayed session after only
    a few new connections - so the API died after a handful of calls."""
    app.state.http = new_http_client()
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
    version="0.1.0",
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
    x_li_cookie: str | None, x_li_at: str | None, x_jsessionid: str | None
) -> tuple[str, str, str] | None:
    """Picks a session in order of preference: caller's full cookie jar,
    caller's minimal li_at/JSESSIONID pair, then the backend demo session
    (full jar preferred there too). Returns (li_at, cookie_header, csrf_token)
    or None if nothing usable was supplied anywhere.

    Replaying the full jar (not just li_at+JSESSIONID stripped of everything
    else) is preferred because the minimal pair, presented in isolation from
    the cookies it normally travels with, is itself an anomaly-detection
    signal - see app/voyager_client.py's module docstring.
    """
    full_cookie = x_li_cookie or settings.full_cookie
    if full_cookie:
        li_at = extract_cookie_value(full_cookie, "li_at")
        jsessionid = extract_cookie_value(full_cookie, "JSESSIONID")
        if li_at and jsessionid:
            return li_at, full_cookie, jsessionid

    li_at = x_li_at or settings.li_at
    jsessionid = x_jsessionid or settings.jsessionid
    if li_at and jsessionid:
        jsessionid = jsessionid.strip('"')
        return li_at, f'li_at={li_at}; JSESSIONID="{jsessionid}"', jsessionid

    return None


@app.get("/health")
async def health() -> dict:
    payload = {
        "ok": True,
        "allow_live": settings.allow_live,
        "shared_quota_store": settings.has_shared_quota_store(),
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
    url: str = Query(..., description="LinkedIn profile URL, e.g. https://www.linkedin.com/in/someone"),
    force_refresh: bool = Query(False, description="Bypass cache and re-fetch live"),
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
    public_id = _extract_public_identifier(url)

    if not force_refresh:
        cached = cache.get(public_id)
        if cached is not None:
            return ProfileResponse(**cached, source="cache")

    session = _resolve_session(x_li_cookie, x_li_at, x_jsessionid)
    if session is None:
        raise HTTPException(
            status_code=401,
            detail=(
                "no session available: pass x-li-cookie (recommended) or "
                "x-li-at + x-jsessionid headers, or configure a backend demo session"
            ),
        )
    li_at, cookie_header, csrf_token = session

    if not settings.allow_live:
        raise HTTPException(status_code=503, detail="live fetches are disabled (kill switch)")

    try:
        await rate_limiter.before_live_fetch(_account_key(li_at, cookie_header))
    except QuotaExceeded as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc

    async with VoyagerClient(
        cookie_header,
        csrf_token,
        http_client=app.state.http,
        min_delay=settings.min_delay,
        max_delay=settings.max_delay,
        browser_headers=settings.browser_headers,
    ) as client:
        try:
            raw = await client.fetch_profile(public_id)
        except VoyagerError as exc:
            status = exc.status_code
            if status == 404:
                raise HTTPException(
                    status_code=404,
                    detail="profile not found, or not visible to the session in use",
                ) from exc
            if status in (401, 403):
                raise HTTPException(status_code=401, detail="session cookie rejected by LinkedIn") from exc
            if status == 302:
                # LinkedIn redirects to the login/authwall page instead of
                # returning JSON when the session cookie is invalid or
                # expired - this is an auth problem, not an infra one.
                raise HTTPException(
                    status_code=401,
                    detail="session expired or invalid: LinkedIn redirected to login - capture a fresh session",
                ) from exc
            raise HTTPException(status_code=502, detail=f"upstream error: {exc}") from exc

    profile, limitations = denormalize(public_id, raw)
    payload = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "profile": profile,
        "limitations": limitations,
    }
    cache.set(public_id, {**payload, "profile": profile.model_dump()})
    return ProfileResponse(**payload, source="live")
