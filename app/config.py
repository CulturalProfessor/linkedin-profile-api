"""Runtime configuration, read from environment.

Nothing here is baked into the repo. The backend session cookie (used as the
demo default when a caller does not supply their own) lives only in the
environment, so the deployed service holds no credentials in source.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    # Optional backend session, used only when the caller sends no session of
    # their own. Both halves are needed: li_at authenticates, JSESSIONID is the
    # CSRF token.
    li_at: str | None = os.getenv("LINKEDIN_LI_AT") or None
    jsessionid: str | None = os.getenv("LINKEDIN_JSESSIONID") or None

    # Kill switch. When false, the API never touches LinkedIn and serves only
    # cached profiles. Flip this if anything looks wrong in production.
    allow_live: bool = _bool("ALLOW_LIVE", True)

    # Hard ceiling on live fetches per calendar day, across all callers.
    # Caps account exposure at a number we choose, not one the traffic chooses.
    daily_quota: int = int(os.getenv("DAILY_QUOTA", "50"))

    # Politeness delay bounds (seconds) between live requests. Jittered to avoid
    # the even-interval timing signature that behavioural detection keys on.
    min_delay: float = float(os.getenv("MIN_DELAY", "0.8"))
    max_delay: float = float(os.getenv("MAX_DELAY", "2.5"))

    cache_dir: str = os.getenv("CACHE_DIR", "fixtures/cache")

    # Shared daily-quota counter (Upstash Redis REST API). When both are set,
    # local runs and the deployed server draw down the same daily quota
    # against the same LinkedIn account instead of each counting on its own.
    # Falls back to a process-local in-memory counter when unset.
    upstash_redis_rest_url: str | None = os.getenv("UPSTASH_REDIS_REST_URL") or None
    upstash_redis_rest_token: str | None = os.getenv("UPSTASH_REDIS_REST_TOKEN") or None

    def has_backend_session(self) -> bool:
        return bool(self.li_at and self.jsessionid)

    def has_shared_quota_store(self) -> bool:
        return bool(self.upstash_redis_rest_url and self.upstash_redis_rest_token)


settings = Settings()
