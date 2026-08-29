import base64

import pytest

from app.config import ConfigError, _bool, _full_cookie, _number, _text

VALID = 'li_at=AQEDfake; JSESSIONID="ajax:123"; bcookie="v=2&y"'


def _b64(value: str) -> str:
    return base64.b64encode(value.encode()).decode()


def test_full_cookie_prefers_base64_when_present(monkeypatch):
    raw = 'li_at=x; JSESSIONID="ajax:123"; bcookie="v=2&y"'
    monkeypatch.setenv("LINKEDIN_FULL_COOKIE_B64", base64.b64encode(raw.encode()).decode())
    monkeypatch.setenv("LINKEDIN_FULL_COOKIE", "should-be-ignored")
    assert _full_cookie() == raw


def test_full_cookie_falls_back_to_plain(monkeypatch):
    monkeypatch.delenv("LINKEDIN_FULL_COOKIE_B64", raising=False)
    monkeypatch.setenv("LINKEDIN_FULL_COOKIE", 'li_at=x; JSESSIONID="ajax:123"')
    assert _full_cookie() == 'li_at=x; JSESSIONID="ajax:123"'


def test_full_cookie_none_when_neither_set(monkeypatch):
    monkeypatch.delenv("LINKEDIN_FULL_COOKIE_B64", raising=False)
    monkeypatch.delenv("LINKEDIN_FULL_COOKIE", raising=False)
    assert _full_cookie() is None


def test_full_cookie_b64_survives_special_characters(monkeypatch):
    """The exact scenario that broke plain .env parsing: quotes, #, and a
    trailing = inside the value."""
    tricky = 'li_at=x#1; JSESSIONID="ajax:1=2=3"; note="quotes \' and # both here"'
    monkeypatch.setenv("LINKEDIN_FULL_COOKIE_B64", base64.b64encode(tricky.encode()).decode())
    assert _full_cookie() == tricky


def test_b64_tolerates_whitespace_from_wrapped_dashboard_paste(monkeypatch):
    """Hosting dashboards and editors wrap long values. Whitespace is the one
    corruption safe to repair silently - everything else must be loud."""
    wrapped = "\n  ".join([_b64(VALID)[:20], _b64(VALID)[20:]])
    monkeypatch.setenv("LINKEDIN_FULL_COOKIE_B64", wrapped)
    assert _full_cookie() == VALID


def test_corrupt_b64_raises_instead_of_silently_decoding_garbage(monkeypatch):
    """The default b64decode(validate=False) *discards* out-of-alphabet
    characters, turning a corrupted value into a plausible-looking wrong
    cookie that only fails much later, as a 401 from LinkedIn."""
    monkeypatch.setenv("LINKEDIN_FULL_COOKIE_B64", _b64(VALID)[:-4] + "!!@@")
    with pytest.raises(ConfigError, match="not valid base64"):
        _full_cookie()


def test_cookie_missing_jsessionid_is_rejected(monkeypatch):
    """Without this, a half-configured cookie falls through _resolve_session and
    reports 'no session available' - i.e. 'nothing is configured', when in fact
    something is and it's broken."""
    monkeypatch.delenv("LINKEDIN_FULL_COOKIE_B64", raising=False)
    monkeypatch.setenv("LINKEDIN_FULL_COOKIE", "li_at=AQEDfake; bcookie=\"v=2\"")
    with pytest.raises(ConfigError, match="JSESSIONID"):
        _full_cookie()


def test_blank_values_are_treated_as_unset_not_as_errors(monkeypatch):
    """`cp .env.example .env` leaves these empty. Empty means 'no backend
    session configured', which is legal - callers can bring their own."""
    monkeypatch.setenv("LINKEDIN_FULL_COOKIE_B64", "   ")
    monkeypatch.setenv("LINKEDIN_FULL_COOKIE", "")
    assert _full_cookie() is None


def test_blank_numeric_values_fall_back_to_defaults(monkeypatch):
    """`cp .env.example .env` leaves `DAILY_QUOTA=` behind. os.getenv returns
    "" for that, not the default, and int("") raises at import - a boot loop on
    a host, with a traceback naming neither the variable nor the file."""
    monkeypatch.setenv("DAILY_QUOTA", "")
    monkeypatch.setenv("MIN_DELAY", "   ")
    assert _number("DAILY_QUOTA", "50", int) == 50
    assert _number("MIN_DELAY", "0.8", float) == 0.8


def test_non_numeric_value_names_itself(monkeypatch):
    monkeypatch.setenv("DAILY_QUOTA", "fifty")
    with pytest.raises(ConfigError, match="DAILY_QUOTA must be a number"):
        _number("DAILY_QUOTA", "50", int)


def test_blank_text_and_flags_fall_back(monkeypatch):
    monkeypatch.setenv("CACHE_DIR", "")
    monkeypatch.setenv("ALLOW_LIVE", "")
    assert _text("CACHE_DIR", "fixtures/cache") == "fixtures/cache"
    assert _bool("ALLOW_LIVE", True) is True
