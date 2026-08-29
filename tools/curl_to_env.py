#!/usr/bin/env python3
r"""Turn a browser's "Copy as cURL" into the .env session line.

Why a copied cURL command rather than reading cookies in the browser: the
copied command already carries the *complete* Cookie header the browser sent,
li_at included. Page JS cannot read li_at at all, since it is HttpOnly, so any
console-based approach has to walk you through copying that one value by hand,
and that manual step is where a stray space or a wrapping quote sneaks in and
produces a cookie that looks perfectly fine and fails later as an opaque 401.

Usage:
    # In DevTools -> Network, click any www.linkedin.com request,
    # right-click -> Copy -> Copy as cURL (bash), then:
    python3 tools/curl_to_env.py            # paste, then press Enter
    python3 tools/curl_to_env.py --file c.txt
    python3 tools/curl_to_env.py --print    # show the line, don't write .env

No Ctrl-D needed: a copied curl command is a shell command spanning several
`\`-continued lines, so the paste is complete at the first line that doesn't
end in a backslash. (Ctrl-D only signals EOF at the start of a line, which is
a reliable way to appear to hang after pasting text with no trailing newline.)

The command is never taken as a command-line argument: that would put a live
session cookie into shell history and into `ps` output.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shlex
import sys
from pathlib import Path

ENV_PATH = Path(__file__).resolve().parents[1] / ".env"
KEY = "LINKEDIN_FULL_COOKIE_B64"
HEADERS_KEY = "LINKEDIN_BROWSER_HEADERS_B64"
REQUIRED = ("li_at", "JSESSIONID")

# Fingerprint headers worth replaying verbatim from the captured request, so
# the session is presented behind the same browser identity that created it.
# Hardcoding these instead means guessing, and a guess that contradicts itself
# (a Windows User-Agent alongside sec-ch-ua-platform "Linux", say) is a far
# louder signal than any of them being absent. Deliberately excluded: cookie
# and csrf-token (handled separately), accept and x-restli-protocol-version
# (Voyager-specific, set by the client), and anything request-specific.
CAPTURE_HEADERS = (
    "user-agent",
    "accept-language",
    "sec-ch-ua",
    "sec-ch-ua-mobile",
    "sec-ch-ua-platform",
    "sec-ch-ua-platform-version",
    "sec-ch-ua-full-version-list",
    "x-li-lang",
)
# Set these and they'd sit in .env as a second, staler copy of the same secret
# while being ignored - full_cookie wins in app/config.py.
SUPERSEDED = ("LINKEDIN_LI_AT", "LINKEDIN_JSESSIONID", "LINKEDIN_FULL_COOKIE")


def read_command(file: str | None) -> str:
    """Read the curl command from a file, a pipe, or an interactive paste.

    The interactive path stops at the first line not ending in a backslash,
    rather than waiting for EOF - so the user presses Enter, which is what they
    expect, instead of Ctrl-D, which only takes effect at the start of a line
    and otherwise looks like the program has hung.
    """
    if file:
        return Path(file).read_text()
    if not sys.stdin.isatty():
        return sys.stdin.read()

    print(
        "Paste the curl command (DevTools -> Network -> right-click a "
        "www.linkedin.com\nrequest -> Copy -> Copy as cURL (bash)), then press Enter:",
        file=sys.stderr,
    )
    lines: list[str] = []
    for line in sys.stdin:
        lines.append(line)
        if not line.rstrip("\n").rstrip().endswith("\\"):
            break
    return "".join(lines)


def extract_cookie(curl_text: str) -> str:
    """Pull the Cookie header out of a curl command.

    Chrome emits `-b 'a=1; b=2'`, Firefox `-H 'Cookie: a=1; b=2'`; both also
    have long forms. shlex handles the shell quoting, including Chrome's
    '\\'' escaping of embedded single quotes.
    """
    text = curl_text.replace("\\\n", " ").strip()
    if not text:
        sys.exit("No input received. Paste the curl command, then press Enter.")
    if not text.lstrip().startswith("curl"):
        sys.exit(
            "That doesn't look like a curl command (it should start with `curl`).\n"
            "Use DevTools -> Network -> right-click a request -> Copy -> Copy as cURL (bash).\n"
            "The Windows 'cmd' variant uses a different quoting style and won't parse."
        )
    try:
        tokens = shlex.split(text)
    except ValueError as exc:
        sys.exit(f"Could not parse the curl command ({exc}). Copy it again, unmodified.")

    for i, token in enumerate(tokens[:-1]):
        value = tokens[i + 1]
        if token in ("-b", "--cookie"):
            return value.strip()
        if token in ("-H", "--header"):
            name, sep, header_value = value.partition(":")
            if sep and name.strip().lower() == "cookie":
                return header_value.strip()

    sys.exit(
        "No Cookie header found in that curl command.\n"
        "Copy a request to www.linkedin.com made while logged in - a static asset\n"
        "request (an image, a .js file) often carries no cookies at all."
    )


def extract_headers(curl_text: str) -> dict[str, str]:
    """Collect the fingerprint headers from the same curl command."""
    text = curl_text.replace("\\\n", " ").strip()
    try:
        tokens = shlex.split(text)
    except ValueError:
        return {}

    found: dict[str, str] = {}
    for i, token in enumerate(tokens[:-1]):
        if token not in ("-H", "--header"):
            continue
        name, sep, value = tokens[i + 1].partition(":")
        if sep and name.strip().lower() in CAPTURE_HEADERS:
            found[name.strip().lower()] = value.strip()
    return found


def check(cookie: str) -> None:
    missing = [name for name in REQUIRED if f"{name}=" not in cookie]
    if missing:
        sys.exit(
            f"That cookie is missing: {', '.join(missing)}.\n"
            "li_at authenticates and JSESSIONID is the CSRF token - both are required.\n"
            "Copy a request to www.linkedin.com itself rather than to a CDN host."
        )


def update_env(path: Path, key: str, value: str) -> bool:
    """Replace `key` in .env, preserving everything else. Written atomically and
    chmod 0600 - it holds a live session."""
    lines = path.read_text().splitlines() if path.exists() else []
    out: list[str] = []
    replaced = False
    pattern = re.compile(rf"\s*{re.escape(key)}\s*=")
    for line in lines:
        if pattern.match(line):
            if not replaced:  # collapse any duplicates onto one line
                out.append(f"{key}={value}")
                replaced = True
            continue
        out.append(line)
    if not replaced:
        out.append(f"{key}={value}")

    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(out).rstrip("\n") + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return replaced


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--print", dest="print_only", action="store_true",
        help="print the .env line instead of writing it (it contains a live session)",
    )
    parser.add_argument(
        "--file", metavar="PATH",
        help="read the curl command from a file instead of pasting it",
    )
    args = parser.parse_args()

    command = read_command(args.file)
    cookie = extract_cookie(command)
    check(cookie)
    encoded = base64.b64encode(cookie.encode("utf-8")).decode("ascii")

    headers = extract_headers(command)
    headers_encoded = base64.b64encode(
        json.dumps(headers, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")

    names = [part.split("=", 1)[0].strip() for part in cookie.split(";") if "=" in part]
    print(f"parsed {len(names)} cookies, {len(cookie)} chars, li_at + JSESSIONID present")
    if headers:
        print(f"captured {len(headers)} fingerprint headers: {', '.join(sorted(headers))}")
    else:
        print(
            "warning: no fingerprint headers found in that curl command - the client\n"
            "  will fall back to generic defaults, which match your browser less well."
        )

    if args.print_only:
        print(f"\n{KEY}={encoded}")
        print(f"\n{HEADERS_KEY}={headers_encoded}")
        return

    replaced = update_env(ENV_PATH, KEY, encoded)
    update_env(ENV_PATH, HEADERS_KEY, headers_encoded)
    print(f"{'updated' if replaced else 'added'} {KEY} + {HEADERS_KEY} in {ENV_PATH} (chmod 600)")

    stale = [name for name in SUPERSEDED if re.search(rf"(?m)^\s*{name}\s*=\s*\S",
             ENV_PATH.read_text())]
    if stale:
        print(
            f"\nnote: {', '.join(stale)} still set in .env. {KEY} takes priority, so"
            "\nthose are ignored - consider blanking them so there's one copy of the"
            "\nsecret rather than several that drift apart."
        )

    print(
        "\nNow fully stop and restart uvicorn (Ctrl-C, then start it again)."
        "\n--reload only watches .py files, so editing .env triggers no reload at all"
        "\nand the running process keeps serving the previous cookie."
        "\n\nThen verify before spending a /profile fetch:"
        "\n    python3 tools/check_session.py"
    )


if __name__ == "__main__":
    main()
