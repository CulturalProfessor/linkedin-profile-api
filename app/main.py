from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone

from fastapi import FastAPI, Header, HTTPException, Query

from app.cache import DiskCache
from app.config import settings
from app.denormalize import denormalize
from app.models import ProfileResponse
from app.quota import InMemoryQuotaBackend, UpstashQuotaBackend
from app.rate_limit import QuotaExceeded, RateLimiter
from app.voyager_client import VoyagerClient, VoyagerError

app = FastAPI(
    title="LinkedIn Profile API",
    description=(
        "LinkedIn profile URL in, structured JSON out. Reverse-engineered "
        "against LinkedIn's own Voyager endpoints - no browser automation. "
        "See README for auth model, legal framing, and known limitations."
    ),
    version="0.1.0",
)

cache = DiskCache(settings.cache_dir)
quota_backend = (
    UpstashQuotaBackend(settings.upstash_redis_rest_url, settings.upstash_redis_rest_token)
    if settings.has_shared_quota_store()
    else InMemoryQuotaBackend()
)
rate_limiter = RateLimiter(quota_backend, settings.daily_quota, settings.min_delay, settings.max_delay)

_PUBLIC_ID_RE = re.compile(r"linkedin\.com/in/([^/?#]+)")


def _account_key(li_at: str) -> str:
    """Quota bucket identifier for a session. Hashed so the raw li_at value
    is never used as a cache/Redis key or written anywhere - a leaked key
    name shouldn't leak a usable cookie."""
    return hashlib.sha256(li_at.encode()).hexdigest()[:16]


def _extract_public_identifier(profile_url: str) -> str:
    profile_url = profile_url.strip()
    match = _PUBLIC_ID_RE.search(profile_url)
    if match:
        return match.group(1)
    if re.fullmatch(r"[A-Za-z0-9\-_%]+", profile_url):
        return profile_url  # caller passed the identifier directly
    raise HTTPException(status_code=400, detail="not a recognizable LinkedIn profile URL")


@app.get("/health")
async def health() -> dict:
    payload = {
        "ok": True,
        "allow_live": settings.allow_live,
        "shared_quota_store": settings.has_shared_quota_store(),
    }
    if settings.has_backend_session():
        # The quota is per-account (see _account_key) - this reports the
        # backend demo session's own bucket, not a global figure. Callers
        # supplying their own session draw from a separate bucket entirely.
        payload["backend_session_remaining_quota_today"] = await rate_limiter.remaining_today(
            _account_key(settings.li_at)
        )
    return payload


@app.get("/profile", response_model=ProfileResponse)
async def get_profile(
    url: str = Query(..., description="LinkedIn profile URL, e.g. https://www.linkedin.com/in/someone"),
    force_refresh: bool = Query(False, description="Bypass cache and re-fetch live"),
    x_li_at: str | None = Header(
        default=None,
        description="Caller-supplied li_at session cookie. Falls back to the backend demo session if omitted.",
    ),
    x_jsessionid: str | None = Header(
        default=None,
        description="Caller-supplied JSESSIONID cookie (paired with x-li-at). Falls back to the backend demo session if omitted.",
    ),
) -> ProfileResponse:
    public_id = _extract_public_identifier(url)

    if not force_refresh:
        cached = cache.get(public_id)
        if cached is not None:
            return ProfileResponse(**cached, source="cache")

    li_at = x_li_at or settings.li_at
    jsessionid = x_jsessionid or settings.jsessionid
    if not li_at or not jsessionid:
        raise HTTPException(
            status_code=401,
            detail=(
                "no session available: pass x-li-at and x-jsessionid headers, "
                "or configure a backend demo session"
            ),
        )

    if not settings.allow_live:
        raise HTTPException(status_code=503, detail="live fetches are disabled (kill switch)")

    try:
        await rate_limiter.before_live_fetch(_account_key(li_at))
    except QuotaExceeded as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc

    async with VoyagerClient(li_at, jsessionid) as client:
        try:
            raw = await client.fetch_profile(public_id)
        except VoyagerError as exc:
            status = exc.status_code
            if status == 404:
                raise HTTPException(status_code=404, detail="profile not found") from exc
            if status in (401, 403):
                raise HTTPException(status_code=401, detail="session cookie rejected by LinkedIn") from exc
            raise HTTPException(status_code=502, detail=f"upstream error: {exc}") from exc

    profile, limitations = denormalize(public_id, raw)
    payload = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "profile": profile,
        "limitations": limitations,
    }
    cache.set(public_id, {**payload, "profile": profile.model_dump()})
    return ProfileResponse(**payload, source="live")
