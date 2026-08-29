#!/usr/bin/env python3
"""Is the configured session still usable? One request, no quota spent.

A /profile fetch makes seven Voyager requests, so using it to answer "is my
cookie dead, or is my code wrong?" is both slow and expensive against a daily
quota. This makes exactly one - the profile resolve - and reports what came
back, so the two questions can be told apart in a second.

Usage:
    python3 tools/check_session.py [public-identifier]

Exit status: 0 session works, 1 session rejected, 2 nothing configured.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402
from app.main import _resolve_session  # noqa: E402
from app.voyager_client import PROFILE_RESOLVE_PATH, VoyagerClient, VoyagerError  # noqa: E402

DIAGNOSIS = {
    302: (
        "REJECTED - LinkedIn redirected to the login page.\n"
        "  The session is expired, was signed out elsewhere, or the account is\n"
        "  sitting behind a checkpoint. Open linkedin.com in the browser you\n"
        "  captured from: if it shows a security check, clear it first - a fresh\n"
        "  capture taken while a checkpoint is pending will be rejected too."
    ),
    401: "REJECTED - LinkedIn refused the cookie outright (401).",
    403: (
        "REJECTED - forbidden (403). Usually a challenged session rather than an\n"
        "  expired one; clearing the checkpoint in the browser often restores it."
    ),
    429: (
        "THROTTLED - too many requests (429). The session itself may be fine.\n"
        "  Leave it alone for a while, and raise MIN_DELAY/MAX_DELAY."
    ),
}


async def main() -> int:
    target = sys.argv[1] if len(sys.argv) > 1 else "michaelmiebach"

    session = _resolve_session(None, None, None)
    if session is None:
        print("NOT CONFIGURED - no backend session found in the environment.")
        print("  Capture one with:  python3 tools/curl_to_env.py")
        return 2

    _li_at, cookie, csrf = session  # never printed
    print(f"checking configured session against /in/{target} ({len(cookie)} char cookie)...")

    # Must construct the client exactly as app/main.py does. Omitting
    # browser_headers here made this tool send a different fingerprint than the
    # server, so it could report SESSION OK for a request the server never
    # makes - a green light for a fetch that then failed.
    async with VoyagerClient(
        cookie, csrf, browser_headers=settings.browser_headers
    ) as client:
        try:
            body = await client._get(
                PROFILE_RESOLVE_PATH, {"q": "memberIdentity", "memberIdentity": target}
            )
        except VoyagerError as exc:
            status = exc.status_code
            print(DIAGNOSIS.get(status, f"FAILED - unexpected status {status}: {exc}"))
            return 1

    urns = body.get("data", {}).get("*elements") or []
    if not urns:
        print(f"SESSION OK, but '{target}' didn't resolve to a profile URN.")
        print("  The cookie works; that public identifier is wrong or the profile is private.")
        return 0

    print("SESSION OK - resolved to a profile URN, cookie is live.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
