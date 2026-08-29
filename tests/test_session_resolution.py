import pytest

from app.main import _resolve_session
from app.voyager_client import extract_cookie_value


@pytest.fixture
def no_backend_session():
    """Clears the backend demo session for the duration of a test, so tests
    that pass caller headers aren't affected by whatever's actually configured
    in the environment's .env - `settings` is a module-level singleton shared
    across the whole test session, and this repo is routinely run with a real
    LINKEDIN_FULL_COOKIE set for live testing.

    Plain setattr, because Settings is a pydantic model rather than a frozen
    dataclass. This used to need object.__setattr__ to get past
    FrozenInstanceError - a workaround the tests carried purely because of how
    config was built.
    """
    import app.main as main

    attrs = ("full_cookie", "li_at", "jsessionid")
    originals = {attr: getattr(main.settings, attr) for attr in attrs}
    for attr in attrs:
        setattr(main.settings, attr, None)
    try:
        yield
    finally:
        for attr, value in originals.items():
            setattr(main.settings, attr, value)


def test_extract_cookie_value_handles_quotes_and_siblings():
    raw = 'bcookie="v=2&abc"; li_at=AQE_fake_token; JSESSIONID="ajax:12345"; lidc="b=1"'
    assert extract_cookie_value(raw, "li_at") == "AQE_fake_token"
    assert extract_cookie_value(raw, "JSESSIONID") == "ajax:12345"
    assert extract_cookie_value(raw, "missing") is None


def test_caller_full_cookie_takes_precedence(no_backend_session):
    full = 'li_at=caller-token; JSESSIONID="caller-csrf"; bcookie="v=2&x"'
    li_at, cookie_header, csrf_token = _resolve_session(full, "x-li-at-token", "x-jsessionid-token")
    assert li_at == "caller-token"
    assert cookie_header == full  # replayed verbatim, sibling cookies included
    assert csrf_token == "caller-csrf"


def test_caller_minimal_pair_used_when_no_full_cookie(no_backend_session):
    li_at, cookie_header, csrf_token = _resolve_session(None, "minimal-li-at", '"minimal-csrf"')
    assert li_at == "minimal-li-at"
    assert cookie_header == 'li_at=minimal-li-at; JSESSIONID="minimal-csrf"'
    assert csrf_token == "minimal-csrf"


def test_none_when_nothing_supplied(no_backend_session):
    assert _resolve_session(None, None, None) is None
