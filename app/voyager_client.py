"""Thin client over LinkedIn's internal Voyager dash API.

No browser involved: this sends the same XHR requests the profile page's own
JS makes, using a session cookie the caller already holds (li_at + JSESSIONID).
Endpoint paths below are the plain dash resource names (no decorationId) -
confirmed live by hand against a real profile (see project notes). The one
piece NOT independently verified here is the exact query param LinkedIn uses
to resolve a public profile URL to its URN (`memberIdentity` is the
long-standing Voyager convention); if it starts 404ing, check the Network
tab on a profile page for the real `identity/dash/profiles?q=...` call and
update `PROFILE_RESOLVE_PATH` below.
"""
from __future__ import annotations

import httpx

BASE_URL = "https://www.linkedin.com"
PROFILE_RESOLVE_PATH = "/voyager/api/identity/dash/profiles"

# Section name -> Voyager dash resource path. Names match the fixture keys
# 1:1 so denormalize.py can consume {section_name: body} directly.
SECTION_PATHS = {
    "profilePositionGroups": "/voyager/api/identity/dash/profilePositionGroups",
    "profileEducations": "/voyager/api/identity/dash/profileEducations",
    "profileSkills": "/voyager/api/identity/dash/profileSkills",
    "profileCertifications": "/voyager/api/identity/dash/profileCertifications",
    "profileLanguages": "/voyager/api/identity/dash/profileLanguages",
    "profileCourses": "/voyager/api/identity/dash/profileCourses",
    "profileProjects": "/voyager/api/identity/dash/profileProjects",
    "profileHonors": "/voyager/api/identity/dash/profileHonors",
    "profileVolunteerExperiences": "/voyager/api/identity/dash/profileVolunteerExperiences",
    # Not yet confirmed live - titles for profilePositionGroups live here.
    # Left wired up so it starts working the moment it's verified; denormalize()
    # already tolerates it being absent (see its "no job title" limitation).
    "profilePositions": "/voyager/api/identity/dash/profilePositions",
}


class VoyagerError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class VoyagerClient:
    def __init__(self, li_at: str, jsessionid: str, timeout: float = 15.0):
        jsessionid = jsessionid.strip('"')
        self._csrf_token = jsessionid
        cookie = f'li_at={li_at}; JSESSIONID="{jsessionid}"'
        self._client = httpx.AsyncClient(
            base_url=BASE_URL,
            headers={
                "Cookie": cookie,
                "csrf-token": self._csrf_token,
                "x-restli-protocol-version": "2.0.0",
                "accept": "application/vnd.linkedin.normalized+json+2.1",
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                ),
            },
            timeout=timeout,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, params: dict[str, str]) -> dict:
        resp = await self._client.get(path, params=params)
        if resp.status_code != 200:
            raise VoyagerError(
                f"Voyager request to {path} failed: {resp.status_code}",
                status_code=resp.status_code,
            )
        return resp.json()

    async def fetch_section(self, section: str, profile_urn: str) -> dict:
        path = SECTION_PATHS[section]
        return await self._get(path, {"q": "viewee", "profileUrn": profile_urn})

    async def fetch_profile(self, public_identifier: str) -> dict:
        """Returns {"urn": ..., "profile": <body>, <section>: <body>, ...} -
        the same shape as fixtures/sample_raw.json's per-section "body" values.
        """
        profile_body = await self._get(
            PROFILE_RESOLVE_PATH,
            {"q": "memberIdentity", "memberIdentity": public_identifier},
        )
        urns = profile_body.get("data", {}).get("*elements") or []
        if not urns:
            raise VoyagerError(f"could not resolve profile '{public_identifier}' to a URN")
        urn = urns[0]
        raw: dict = {"urn": urn, "profile": profile_body}
        for section in SECTION_PATHS:
            try:
                raw[section] = await self.fetch_section(section, urn)
            except VoyagerError:
                # Optional sections (courses, projects, honors, ...) commonly
                # come back empty/404 for profiles that don't use them.
                continue
        return raw

    async def __aenter__(self) -> "VoyagerClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()
