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
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic import AliasChoices, Field, ValidationError, field_validator
from pydantic_core import PydanticUseDefault
from pydantic_settings import BaseSettings, SettingsConfigDict

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
            f"({exc}). It should be the output of tools/curl_to_env.py - "
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


class Settings(BaseSettings):
    """Runtime configuration, validated at *instantiation*.

    That word is the whole point of this class's shape. This was previously a
    frozen dataclass whose field defaults called os.getenv() at
    class-definition time, which meant the values were frozen at import and
    `monkeypatch.setenv` could never affect them. Tests had to exercise the
    private `_raw`/`_number`/`_bool` helpers instead of the object the
    application actually uses, and anything wanting to override a setting had
    to go through `object.__setattr__` to get past the frozen dataclass. The
    tests were shaped around the defect rather than testing the thing.

    Now: `Settings()` reads the environment when it is called, so a test can
    set an env var and build one, or bypass the environment entirely with
    `Settings(daily_quota=5)`. `get_settings()` below is the cached accessor
    the application uses.
    """

    model_config = SettingsConfigDict(
        # Unrelated variables (PATH, RENDER_*, ...) are not this class's
        # business, and erroring on them would make the app un-deployable.
        extra="ignore",
        case_sensitive=False,
        # Every alias below is an AliasChoices listing the field name *then*
        # the env var, so `Settings(daily_quota=5)` works as well as
        # DAILY_QUOTA=5. Two subtleties, both found the hard way:
        #
        # populate_by_name does not cover this on its own - in pydantic v2 it
        # extends `alias`, not `validation_alias`, so a plain validation_alias
        # silently ignores the keyword argument, leaving the object exactly as
        # untestable as the frozen dataclass it replaced.
        #
        # And the order within AliasChoices matters: env values arrive keyed by
        # the uppercase alias while init keyword arguments arrive keyed by the
        # field name, so both can be present at once and AliasChoices takes the
        # first one it finds. With the env name first, an explicit
        # Settings(daily_quota=5) was silently losing to DAILY_QUOTA from .env.
        populate_by_name=True,
        # .env is loaded by load_dotenv above rather than by pydantic, on
        # purpose - see the note at the top of this module about override=True.
        # Letting pydantic read it too would reverse that precedence.
    )

    # Optional backend session, used only when the caller sends no session of
    # their own. Both halves are needed: li_at authenticates, JSESSIONID is the
    # CSRF token.
    li_at: str | None = Field(default=None, validation_alias=AliasChoices("li_at", "LINKEDIN_LI_AT"))
    jsessionid: str | None = Field(
        default=None, validation_alias=AliasChoices("jsessionid", "LINKEDIN_JSESSIONID")
    )

    # Preferred over li_at/jsessionid when set: the *entire* Cookie header
    # value captured from a real request (DevTools -> Network -> Copy as
    # cURL). Replaying the full cookie jar instead of just li_at+JSESSIONID
    # in isolation is a meaningfully weaker anomaly-detection signal - see
    # app/voyager_client.py and README's auth model section.
    #
    # default_factory rather than an alias because this is derived from two
    # variables with a preference order and custom decoding; passing it
    # explicitly (as tests do) still overrides.
    full_cookie: str | None = Field(default_factory=_full_cookie)

    # Browser fingerprint captured with the cookie (see _browser_headers).
    browser_headers: dict[str, str] = Field(default_factory=_browser_headers)

    # Shared secret required on /profile when the request would spend the
    # *backend* session. Unset means no key is required - fine locally, but a
    # deployment that carries a backend cookie and no key is an open proxy for
    # that LinkedIn account: anyone who finds the URL can scrape through it on
    # your identity and burn your daily quota. app.main warns loudly at startup
    # when that combination is configured.
    api_key: str | None = Field(default=None, validation_alias=AliasChoices("api_key", "API_KEY"))

    # Kill switch. When false, the API never touches LinkedIn and serves only
    # cached profiles. Flip this if anything looks wrong in production.
    allow_live: bool = Field(default=True, validation_alias=AliasChoices("allow_live", "ALLOW_LIVE"))

    # Hard ceiling on live fetches per calendar day, per LinkedIn account.
    # Caps account exposure at a number we choose, not one the traffic chooses.
    daily_quota: int = Field(default=150, gt=0, validation_alias=AliasChoices("daily_quota", "DAILY_QUOTA"))

    # Ceiling on live fetches per day across *every* bucket combined.
    #
    # daily_quota alone bounds nobody: the bucket is derived from the caller's
    # own cookie, so a caller who varies it gets a fresh full quota on every
    # request. That is fine as an account-exposure measure and useless as an
    # abuse limit, and without this the deployment will happily relay unlimited
    # traffic to LinkedIn from its own IP and connection pool.
    global_daily_quota: int = Field(
        default=400, gt=0,
        validation_alias=AliasChoices("global_daily_quota", "GLOBAL_DAILY_QUOTA"),
    )

    # Politeness delay bounds (seconds) between live requests. Jittered to avoid
    # the even-interval timing signature that behavioural detection keys on.
    min_delay: float = Field(default=0.5, ge=0, validation_alias=AliasChoices("min_delay", "MIN_DELAY"))
    max_delay: float = Field(default=1.5, ge=0, validation_alias=AliasChoices("max_delay", "MAX_DELAY"))

    cache_dir: str = Field(default=".cache", validation_alias=AliasChoices("cache_dir", "CACHE_DIR"))

    # Where cached profiles live: "auto" (default) uses Upstash when it's
    # configured and falls back to disk, "disk" and "upstash" force one.
    # Upstash is strongly preferred for a deployment: Render's free tier
    # replaces the container on every deploy and after ~15 minutes idle,
    # taking a disk cache with it, which leaves the TTL and the stale-
    # serving paths with almost nothing to work on.
    cache_backend: str = Field(
        default="auto", validation_alias=AliasChoices("cache_backend", "CACHE_BACKEND")
    )

    # Shared daily-quota counter (Upstash Redis REST API). When both are set,
    # local runs and the deployed server draw down the same daily quota
    # against the same LinkedIn account instead of each counting on its own.
    # Falls back to a process-local in-memory counter when unset.
    upstash_redis_rest_url: str | None = Field(
        default=None, validation_alias=AliasChoices("upstash_redis_rest_url", "UPSTASH_REDIS_REST_URL")
    )
    upstash_redis_rest_token: str | None = Field(
        default=None, validation_alias=AliasChoices("upstash_redis_rest_token", "UPSTASH_REDIS_REST_TOKEN")
    )

    @field_validator("*", mode="before")
    @classmethod
    def _blank_means_unset(cls, value):
        """A variable set to an empty string means 'not configured', not ''.

        `cp .env.example .env` leaves `DAILY_QUOTA=` behind, and the
        environment reports that as "" rather than as absent - which then made
        int("") raise at import: a boot loop on a host, with a traceback naming
        neither the variable nor the file. PydanticUseDefault is the supported
        way to say "treat this as if it were never set".
        """
        if isinstance(value, str) and not value.strip():
            raise PydanticUseDefault
        return value.strip() if isinstance(value, str) else value

    @field_validator("cache_backend")
    @classmethod
    def _known_backend(cls, value: str) -> str:
        value = value.lower()
        if value not in {"auto", "disk", "upstash"}:
            raise ValueError(f"must be auto, disk or upstash, got {value!r}")
        return value

    def has_backend_session(self) -> bool:
        return bool(self.full_cookie or (self.li_at and self.jsessionid))

    def has_shared_quota_store(self) -> bool:
        return bool(self.upstash_redis_rest_url and self.upstash_redis_rest_token)

    def requires_api_key(self) -> bool:
        """True whenever a key is configured, whatever session the caller brings.

        This deliberately does *not* exempt callers who supply their own
        cookie. That exemption was the reasoning behind the quota buckets -
        your cookie, your risk budget - and applying it to access control was
        a mistake: `x-li-cookie: li_at=garbage; JSESSIONID="garbage"` is enough
        to look like "brought their own session" and skip the check entirely,
        after which the request still goes out from this server, over this
        server's pooled connection, from this server's IP. The key gates use of
        the deployment; the quota gates exposure of an account. They are
        different questions.
        """
        return bool(self.api_key)

    def use_upstash_cache(self) -> bool:
        if self.cache_backend == "disk":
            return False
        if self.cache_backend == "upstash":
            if not self.has_shared_quota_store():
                raise ConfigError(
                    "CACHE_BACKEND=upstash but UPSTASH_REDIS_REST_URL / "
                    "UPSTASH_REDIS_REST_TOKEN are not both set. Set them, or "
                    "use CACHE_BACKEND=disk."
                )
            return True
        return self.has_shared_quota_store()


def _env_name(field: str) -> str:
    """The environment variable a field is set from, for error messages.

    Pydantic reports failures against the field name (`daily_quota`), but the
    operator set `DAILY_QUOTA` in a .env or a hosting dashboard. Naming the
    field would send them looking for something that appears nowhere in their
    configuration.
    """
    alias = getattr(Settings.model_fields.get(field), "validation_alias", None)
    for choice in getattr(alias, "choices", []):
        if isinstance(choice, str) and choice.isupper():
            return choice
    return str(field)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """The application's settings, built once.

    Cached because reading and validating the environment on every request is
    pointless, and because two Settings objects disagreeing about the daily
    quota would be a genuinely confusing bug. Tests that change the
    environment call `get_settings.cache_clear()`.

    Pydantic's ValidationError is re-raised as ConfigError so the failure keeps
    the shape the rest of this module promises: the process refuses to start,
    and the message names the variable rather than a field path.
    """
    try:
        return Settings()
    except ValidationError as exc:
        problems = "; ".join(
            f"{_env_name(e['loc'][0]) if e['loc'] else 'config'}: {e['msg']}"
            for e in exc.errors()
        )
        raise ConfigError(
            f"invalid configuration - {problems}. Leave a variable unset or "
            "blank to use its default."
        ) from exc


settings = get_settings()
