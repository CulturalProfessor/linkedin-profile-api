"""Configuration tests.

These used to exercise the private `_raw` / `_number` / `_bool` / `_text`
helpers rather than the `Settings` object the application actually uses,
because `Settings` was a frozen dataclass whose field defaults called
os.getenv() at *class-definition* time: the values were fixed at import and
`monkeypatch.setenv` could not reach them. The tests were shaped around the
defect. Now they drive the real object two ways - through the environment, and
by constructing one directly - which is the point of the migration.
"""
import base64

import pytest

from app.config import ConfigError, Settings, get_settings

VALID = 'li_at=AQEDfake; JSESSIONID="ajax:123"; bcookie="v=2&y"'


def _b64(value: str) -> str:
    return base64.b64encode(value.encode()).decode()


@pytest.fixture
def env(monkeypatch):
    """Builds a Settings from a controlled environment. Clears the cache on the
    way in and out so a value set here can't leak into another test through
    the lru_cache."""
    get_settings.cache_clear()
    for name in ("LINKEDIN_FULL_COOKIE_B64", "LINKEDIN_FULL_COOKIE", "LINKEDIN_LI_AT",
                 "LINKEDIN_JSESSIONID", "API_KEY", "ALLOW_LIVE", "DAILY_QUOTA",
                 "MIN_DELAY", "MAX_DELAY", "CACHE_DIR", "CACHE_BACKEND",
                 "UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN"):
        monkeypatch.delenv(name, raising=False)

    def build(**environment):
        # Through get_settings(), not Settings() directly: that is the
        # application's entry point, and it is where pydantic's ValidationError
        # becomes the ConfigError the rest of the codebase promises.
        for key, value in environment.items():
            monkeypatch.setenv(key, value)
        get_settings.cache_clear()
        return get_settings()

    yield build
    get_settings.cache_clear()


# --- the migration's own guarantees --------------------------------------


def test_environment_reaches_the_real_settings_object(env):
    """The defect this migration fixes: monkeypatch.setenv could not affect
    Settings at all, because its defaults were evaluated at import."""
    assert env(DAILY_QUOTA="7").daily_quota == 7


def test_settings_can_be_built_without_the_environment():
    """No env vars, no monkeypatching, no object.__setattr__ to get past a
    frozen dataclass - just the values the test cares about."""
    settings = Settings(daily_quota=5, allow_live=False, full_cookie=None,
                        li_at=None, jsessionid=None)
    assert settings.daily_quota == 5
    assert settings.allow_live is False
    assert settings.has_backend_session() is False


def test_explicit_values_beat_the_environment(env):
    """Order within AliasChoices decides this: env values arrive keyed by the
    uppercase alias and init keywords by the field name, so both can be
    present at once. With the env name first, an explicit value silently lost."""
    env(DAILY_QUOTA="150")
    assert Settings(daily_quota=5).daily_quota == 5


def test_get_settings_is_cached(env):
    env(DAILY_QUOTA="11")
    assert get_settings() is get_settings()
    assert get_settings().daily_quota == 11


# --- value parsing --------------------------------------------------------


def test_full_cookie_prefers_base64_when_present(env):
    settings = env(LINKEDIN_FULL_COOKIE_B64=_b64(VALID), LINKEDIN_FULL_COOKIE="ignored")
    assert settings.full_cookie == VALID


def test_full_cookie_falls_back_to_plain(env):
    assert env(LINKEDIN_FULL_COOKIE=VALID).full_cookie == VALID


def test_full_cookie_none_when_neither_set(env):
    assert env().full_cookie is None


def test_full_cookie_b64_survives_special_characters(env):
    """The exact scenario that broke plain .env parsing: quotes, #, and a
    trailing = inside the value."""
    tricky = 'li_at=x#1; JSESSIONID="ajax:1=2=3"; note="quotes \' and # both here"'
    assert env(LINKEDIN_FULL_COOKIE_B64=_b64(tricky)).full_cookie == tricky


def test_b64_tolerates_whitespace_from_wrapped_dashboard_paste(env):
    """Hosting dashboards and editors wrap long values. Whitespace is the one
    corruption safe to repair silently - everything else must be loud."""
    wrapped = "\n  ".join([_b64(VALID)[:20], _b64(VALID)[20:]])
    assert env(LINKEDIN_FULL_COOKIE_B64=wrapped).full_cookie == VALID


def test_corrupt_b64_raises_instead_of_silently_decoding_garbage(env):
    """The default b64decode(validate=False) *discards* out-of-alphabet
    characters, turning a corrupted value into a plausible-looking wrong
    cookie that only fails much later, as a 401 from LinkedIn."""
    with pytest.raises(ConfigError, match="not valid base64"):
        env(LINKEDIN_FULL_COOKIE_B64=_b64(VALID)[:-4] + "!!@@")


def test_cookie_missing_jsessionid_is_rejected(env):
    """Without this, a half-configured cookie falls through _resolve_session and
    reports 'no session available' - i.e. 'nothing is configured', when in fact
    something is and it's broken."""
    with pytest.raises(ConfigError, match="JSESSIONID"):
        env(LINKEDIN_FULL_COOKIE='li_at=AQEDfake; bcookie="v=2"')


def test_blank_values_are_treated_as_unset_not_as_errors(env):
    """`cp .env.example .env` leaves these empty. Empty means 'no backend
    session configured', which is legal - callers can bring their own."""
    assert env(LINKEDIN_FULL_COOKIE_B64="   ", LINKEDIN_FULL_COOKIE="").full_cookie is None


def test_blank_values_fall_back_to_defaults(env):
    """`cp .env.example .env` leaves `DAILY_QUOTA=` behind. The environment
    reports that as "" rather than as absent, and int("") used to raise at
    import - a boot loop on a host, with a traceback naming neither the
    variable nor the file."""
    settings = env(DAILY_QUOTA="", MIN_DELAY="   ", CACHE_DIR="", ALLOW_LIVE="")
    assert settings.daily_quota == 150
    assert settings.min_delay == 0.5
    assert settings.cache_dir == ".cache"
    assert settings.allow_live is True


def test_non_numeric_value_names_itself(env):
    with pytest.raises(ConfigError, match="DAILY_QUOTA must|DAILY_QUOTA:"):
        env(DAILY_QUOTA="fifty")


def test_unknown_cache_backend_is_rejected(env):
    with pytest.raises(ConfigError, match="CACHE_BACKEND"):
        env(CACHE_BACKEND="memcached")


def test_cache_backend_is_case_insensitive(env):
    assert env(CACHE_BACKEND="UPSTASH").cache_backend == "upstash"


def test_values_are_stripped(env):
    """Dashboard paste and .env editing both leave stray whitespace."""
    assert env(CACHE_DIR="  /tmp/cache  ").cache_dir == "/tmp/cache"


# --- derived posture ------------------------------------------------------


def test_api_key_is_required_whenever_one_is_configured():
    """It gates use of the deployment, not exposure of an account. Exempting
    callers who bring their own cookie was a hole: a junk but well-formed
    x-li-cookie looked like "brought their own" and skipped the check, while
    the request still went out from this server's IP and connection pool."""
    assert Settings(api_key="k", full_cookie=VALID).requires_api_key() is True
    assert Settings(api_key="k", full_cookie=None, li_at=None,
                    jsessionid=None).requires_api_key() is True
    assert Settings(api_key=None, full_cookie=VALID).requires_api_key() is False


def test_upstash_cache_requires_credentials():
    with pytest.raises(ConfigError, match="CACHE_BACKEND=upstash"):
        Settings(cache_backend="upstash", upstash_redis_rest_url=None,
                 upstash_redis_rest_token=None).use_upstash_cache()


def test_auto_cache_backend_follows_the_credentials():
    assert Settings(cache_backend="auto", upstash_redis_rest_url=None,
                    upstash_redis_rest_token=None).use_upstash_cache() is False
    assert Settings(cache_backend="auto", upstash_redis_rest_url="https://x",
                    upstash_redis_rest_token="t").use_upstash_cache() is True
