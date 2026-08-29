"""Runtime configuration, read from environment.

Nothing here is baked into the repo. The backend session cookie (used as the
demo default when a caller does not supply their own) lives only in the
environment, so the deployed service holds no credentials in source.
"""
from __future__ import annotations

import base64
import binascii
import json
import os
from pathlib import Path
from dataclasses import dataclass, field

from dotenv import load_dotenv

# The path is derived from this file, not searched for: the default lookup
# walks up from the *current working directory*, so `uvicorn app.main:app`
# started from anywhere else loads no .env and the app boots silently
# unconfigured, every setting having a fallback. (dotenv's own find_dotenv()
# is no better here - it inspects the call stack and quietly falls back to the
# cwd when __main__ has no __file__, which is the case under some launchers.)
#
# override=True makes .env authoritative over an inherited process environment.
# The stdlib-ish default is the reverse, on the reasoning that an explicitly
# exported variable should beat a file; here the file *is* the source of truth
# for local runs, and a stale exported value silently winning is a failure mode
# that costs far more to diagnose than it prevents. In deployment there is no
# .env at all - the host's dashboard variables are untouched by this.
ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(ENV_PATH, override=True)


class ConfigError(RuntimeError):
    """A configured value that cannot be used.

    Raised at import, so the process refuses to start rather than booting with
    a half-usable session. The rule is: *unset* is fine (the backend session is
    optional - callers can bring their own), but *set-but-broken* is an error
    worth stopping for. A mangled cookie that boots successfully surfaces much
    later as an opaque `401 session cookie rejected by LinkedIn`, which is
    indistinguishable from an expired session and from a cookie that was never
    configured - three very different problems wearing the same error message.
    """


def _raw(name: str) -> str | None:
    """A variable set to an empty string means 'not configured', not ''.

    os.getenv(name, default) only returns the default when the variable is
    *absent*, so `DAILY_QUOTA=` in a .env (which is exactly what copying
    .env.example leaves behind) yields "" and then int("") raises at import -
    a boot loop on a host, with a traceback that names neither the variable
    nor the file.
    """
    value = os.getenv(name)
    if value is None or not value.strip():
        return None
    return value.strip()


def _bool(name: str, default: bool) -> bool:
    raw = _raw(name)
    if raw is None:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def _number(name: str, default: str, cast):
    raw = _raw(name) or default
    try:
        return cast(raw)
    except ValueError as exc:
        raise ConfigError(
            f"{name} must be a number, got {raw!r}. Leave it unset or blank to "
            f"use the default ({default})."
        ) from exc


def _text(name: str, default: str) -> str:
    return _raw(name) or default


def _full_cookie() -> str | None:
    """A raw cookie string can contain quotes, #, spaces - any of which can
    collide with .env's own quoting/comment rules depending on how it's
    pasted in (ask how we found out). LINKEDIN_FULL_COOKIE_B64 sidesteps
    that entirely: base64 only ever produces [A-Za-z0-9+/=], which can never
    misparse regardless of what's inside the cookie, so it's the
    recommended way to set this. LINKEDIN_FULL_COOKIE (plain, unencoded) is
    kept as a fallback for anyone who'd rather not encode it - just wrap the
    whole value in single quotes in .env if using that form.
    """
    b64 = os.getenv("LINKEDIN_FULL_COOKIE_B64")
    if b64 and b64.strip():
        return _check_cookie(_decode_b64(b64), "LINKEDIN_FULL_COOKIE_B64")
    plain = os.getenv("LINKEDIN_FULL_COOKIE")
    if plain and plain.strip():
        return _check_cookie(plain.strip(), "LINKEDIN_FULL_COOKIE")
    return None


def _decode_b64(raw: str) -> str:
    # Whitespace is the one corruption that's safe to repair silently: hosting
    # dashboards and editors wrap long values, and that's harmless. Everything
    # else must be loud - the stdlib default (validate=False) *discards* any
    # character outside the base64 alphabet, so a genuinely corrupted value
    # decodes to a plausible-looking but wrong cookie instead of failing.
    compact = "".join(raw.split())
    try:
        decoded = base64.b64decode(compact, validate=True)
    except binascii.Error as exc:
        raise ConfigError(
            "LINKEDIN_FULL_COOKIE_B64 is not valid base64 "
            f"({exc}). It should be the output of tools/get_session_cookie.js - "
            "only A-Z a-z 0-9 + / = characters, and don't drop the trailing '=' "
            "padding. If you meant to paste the raw unencoded cookie, use "
            "LINKEDIN_FULL_COOKIE instead."
        ) from exc
    try:
        return decoded.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigError(
            "LINKEDIN_FULL_COOKIE_B64 decoded to bytes that aren't UTF-8 text, so "
            "it isn't a cookie header - the value was probably truncated or is "
            "base64 of something else."
        ) from exc


def _browser_headers() -> dict[str, str]:
    """Fingerprint headers captured alongside the cookie by tools/curl_to_env.py.

    Replaying the identity that actually established the session beats guessing
    at one. An invented fingerprint that contradicts itself - a Windows
    User-Agent sent with sec-ch-ua-platform "Linux", which is what this project
    shipped originally - is a stronger anomaly signal than sending nothing.
    Empty when unset; VoyagerClient then falls back to its own defaults.
    """
    raw = os.getenv("LINKEDIN_BROWSER_HEADERS_B64")
    if not (raw and raw.strip()):
        return {}
    try:
        decoded = base64.b64decode("".join(raw.split()), validate=True).decode("utf-8")
        headers = json.loads(decoded)
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConfigError(
            f"LINKEDIN_BROWSER_HEADERS_B64 is not base64-encoded JSON ({exc}). "
            "Re-run tools/curl_to_env.py, or blank the value to fall back to defaults."
        ) from exc
    if not isinstance(headers, dict):
        raise ConfigError("LINKEDIN_BROWSER_HEADERS_B64 must decode to a JSON object.")
    return {str(k): str(v) for k, v in headers.items()}


def _check_cookie(cookie: str, source: str) -> str:
    """Both halves must be present: li_at authenticates, JSESSIONID is the CSRF
    token. A cookie carrying only one of them can't be used, but `_resolve_session`
    would just fall through it to the next candidate and end up reporting 'no
    session available' - telling the operator nothing is configured when in fact
    something is, and it's broken."""
    missing = [name for name in ("li_at", "JSESSIONID") if f"{name}=" not in cookie]
    if missing:
        raise ConfigError(
            f"{source} is missing required cookie(s): {', '.join(missing)}. "
            "Capture the *whole* Cookie header from a logged-in linkedin.com "
            "request (DevTools -> Network -> Copy as cURL), not just one value."
        )
    return cookie


@dataclass(frozen=True)
class Settings:
    # Optional backend session, used only when the caller sends no session of
    # their own. Both halves are needed: li_at authenticates, JSESSIONID is the
    # CSRF token.
    li_at: str | None = _raw("LINKEDIN_LI_AT")
    jsessionid: str | None = _raw("LINKEDIN_JSESSIONID")

    # Preferred over li_at/jsessionid when set: the *entire* Cookie header
    # value captured from a real request (DevTools -> Network -> Copy as
    # cURL). Replaying the full cookie jar instead of just li_at+JSESSIONID
    # in isolation is a meaningfully weaker anomaly-detection signal - see
    # app/voyager_client.py and README's auth model section.
    full_cookie: str | None = _full_cookie()

    # Browser fingerprint captured with the cookie (see _browser_headers).
    # default_factory because dataclasses reject a mutable default.
    browser_headers: dict = field(default_factory=_browser_headers)

    # Kill switch. When false, the API never touches LinkedIn and serves only
    # cached profiles. Flip this if anything looks wrong in production.
    allow_live: bool = _bool("ALLOW_LIVE", True)

    # Hard ceiling on live fetches per calendar day, across all callers.
    # Caps account exposure at a number we choose, not one the traffic chooses.
    daily_quota: int = _number("DAILY_QUOTA", "150", int)

    # Politeness delay bounds (seconds) between live requests. Jittered to avoid
    # the even-interval timing signature that behavioural detection keys on.
    min_delay: float = _number("MIN_DELAY", "0.5", float)
    max_delay: float = _number("MAX_DELAY", "1.5", float)

    cache_dir: str = _text("CACHE_DIR", ".cache")

    # Shared daily-quota counter (Upstash Redis REST API). When both are set,
    # local runs and the deployed server draw down the same daily quota
    # against the same LinkedIn account instead of each counting on its own.
    # Falls back to a process-local in-memory counter when unset.
    upstash_redis_rest_url: str | None = _raw("UPSTASH_REDIS_REST_URL")
    upstash_redis_rest_token: str | None = _raw("UPSTASH_REDIS_REST_TOKEN")

    def has_backend_session(self) -> bool:
        return bool(self.full_cookie or (self.li_at and self.jsessionid))

    def has_shared_quota_store(self) -> bool:
        return bool(self.upstash_redis_rest_url and self.upstash_redis_rest_token)


settings = Settings()
