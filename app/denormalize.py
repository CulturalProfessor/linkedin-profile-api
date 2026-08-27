"""Turns raw Voyager section responses into the flat Profile shape.

Every Voyager dash response has the same skeleton:
    {"data": {"*elements": [urn, ...]}, "included": [entity, ...]}
`included` is an unordered bag of entities keyed by `entityUrn`; `data.*elements`
(or `data.elements` for some collections) is the ordered list of URNs that
actually belong to this section. We build an urn->entity index once, then
walk `*elements` in order so section ordering matches what LinkedIn renders.
"""
from __future__ import annotations

from typing import Any

from app.models import (
    CertificationEntry,
    EducationEntry,
    ExperienceEntry,
    LanguageEntry,
    Profile,
    ProfileImages,
)

Json = dict[str, Any]


def _index_by_urn(section: Json) -> tuple[list[Json], dict[str, Json]]:
    body = section.get("data", {})
    included = section.get("included", [])
    by_urn = {e["entityUrn"]: e for e in included if "entityUrn" in e}
    urns = body.get("*elements") or body.get("elements") or []
    ordered = [by_urn[u] for u in urns if u in by_urn]
    return ordered, by_urn


def _format_date(d: Json | None) -> str | None:
    if not d:
        return None
    year, month = d.get("year"), d.get("month")
    if year is None:
        return None
    return f"{year:04d}-{month:02d}" if month else f"{year:04d}"


def _date_range(entity: Json) -> tuple[str | None, str | None]:
    dr = entity.get("dateRange") or {}
    return _format_date(dr.get("start")), _format_date(dr.get("end"))


def _best_artifact_url(picture: Json | None) -> str | None:
    if not picture:
        return None
    vector = (picture.get("displayImage") or {}).get("vectorImage")
    if not vector:
        return None
    artifacts = vector.get("artifacts") or []
    if not artifacts:
        return None
    best = max(artifacts, key=lambda a: a.get("width", 0))
    return vector["rootUrl"] + best["fileIdentifyingUrlPathSegment"]


def _extract_profile_entity(section: Json) -> Json | None:
    _, by_urn = _index_by_urn(section)
    for entity in by_urn.values():
        if entity.get("$type", "").endswith(".Profile"):
            return entity
    return None


def _titles_by_company(positions_section: Json | None) -> dict[str, list[str]]:
    """Best-effort join of individual position titles onto their company.

    The `profilePositions` endpoint (titles live here, not on the position
    *group*) hasn't been captured in a fixture yet - see README limitations.
    If/when `raw["profilePositions"]` is populated this groups titles by
    companyUrn so `_experience` can attach them; until then this returns {}.
    """
    if not positions_section:
        return {}
    ordered, _ = _index_by_urn(positions_section)
    grouped: dict[str, list[str]] = {}
    for entity in ordered:
        company_urn = entity.get("companyUrn")
        title = entity.get("title")
        if company_urn and title:
            grouped.setdefault(company_urn, []).append(title)
    return grouped


def _experience(raw: dict[str, Json]) -> list[ExperienceEntry]:
    section = raw.get("profilePositionGroups")
    if not section:
        return []
    ordered, _ = _index_by_urn(section)
    titles_by_company = _titles_by_company(raw.get("profilePositions"))
    entries = []
    for entity in ordered:
        start, end = _date_range(entity)
        company_urn = entity.get("companyUrn")
        titles = titles_by_company.get(company_urn) or [None]
        for title in titles:
            entries.append(
                ExperienceEntry(
                    company=entity.get("companyName", ""),
                    company_urn=company_urn,
                    title=title,
                    start=start,
                    end=end,
                )
            )
    return entries


def _education(raw: dict[str, Json]) -> list[EducationEntry]:
    section = raw.get("profileEducations")
    if not section:
        return []
    ordered, _ = _index_by_urn(section)
    entries = []
    for entity in ordered:
        start, end = _date_range(entity)
        entries.append(
            EducationEntry(
                school=entity.get("schoolName", ""),
                school_urn=entity.get("schoolUrn"),
                degree=entity.get("degreeName"),
                field_of_study=entity.get("fieldOfStudy"),
                start=start,
                end=end,
            )
        )
    return entries


def _skills(raw: dict[str, Json]) -> list[str]:
    section = raw.get("profileSkills")
    if not section:
        return []
    ordered, _ = _index_by_urn(section)
    return [e["name"] for e in ordered if e.get("name")]


def _certifications(raw: dict[str, Json]) -> list[CertificationEntry]:
    section = raw.get("profileCertifications")
    if not section:
        return []
    ordered, _ = _index_by_urn(section)
    entries = []
    for entity in ordered:
        start, _ = _date_range(entity)
        entries.append(
            CertificationEntry(
                name=entity.get("name", ""),
                authority=entity.get("authority"),
                url=entity.get("url"),
                license_number=entity.get("licenseNumber"),
                issued=start,
            )
        )
    return entries


def _languages(raw: dict[str, Json]) -> list[LanguageEntry]:
    section = raw.get("profileLanguages")
    if not section:
        return []
    ordered, _ = _index_by_urn(section)
    return [
        LanguageEntry(name=e["name"], proficiency=e.get("proficiency"))
        for e in ordered
        if e.get("name")
    ]


def denormalize(public_identifier: str, raw: dict[str, Json]) -> tuple[Profile, list[str]]:
    """raw maps section name (e.g. "profile", "profileEducations") to that
    section's `{"data": ..., "included": ...}` body - i.e. one fixture entry.
    """
    limitations: list[str] = []

    profile_entity = _extract_profile_entity(raw.get("profile", {}))
    if profile_entity is None:
        raise ValueError("no Profile entity found in 'profile' section")

    name = f"{profile_entity.get('firstName', '')} {profile_entity.get('lastName', '')}".strip()

    country_code = ((profile_entity.get("location") or {}).get("countryCode"))
    location = country_code
    if country_code:
        limitations.append(
            "location is a country code only (e.g. 'IN') - LinkedIn no longer "
            "returns city text on this endpoint; resolving geoLocation.geoUrn "
            "to a human-readable place needs a separate, undocumented call."
        )

    if not raw.get("profilePositions"):
        limitations.append(
            "experience entries have no job title - titles live on the "
            "profilePositions endpoint, which hasn't been captured/wired up "
            "yet, so only company + dates are populated (see README)."
        )

    profile = Profile(
        public_identifier=public_identifier,
        name=name,
        headline=profile_entity.get("headline"),
        location=location,
        about=profile_entity.get("summary"),
        experience=_experience(raw),
        education=_education(raw),
        skills=_skills(raw),
        certifications=_certifications(raw),
        languages=_languages(raw),
        images=ProfileImages(
            profile_picture=_best_artifact_url(profile_entity.get("profilePicture")),
            background_picture=_best_artifact_url(profile_entity.get("backgroundPicture")),
        ),
    )
    return profile, limitations
