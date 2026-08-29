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

from app.fields import ALL_FIELDS
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


def _clean(value: Any) -> str | None:
    """Voyager returns human-entered strings verbatim, trailing spaces and
    all (e.g. a real title of "Managing Director ")."""
    if not isinstance(value, str):
        return None
    return value.strip() or None


def _geo_names(raw: dict[str, Json]) -> dict[str, str]:
    """geoUrn -> readable place name, harvested from the positions section.

    The profile entity carries only `geoLocation.geoUrn` and a bare
    `location.countryCode` - the readable city string is genuinely absent
    from that response (its `included` bag holds the Profile and nothing
    else). Each Position, however, carries both its own `geoUrn` *and* the
    resolved `geoLocationName`. So when a member's profile location matches
    one of their role locations - the common case - the profile's geoUrn can
    be resolved for free against data already fetched, instead of via the
    separate undocumented geo-resolve call. When it doesn't match we fall
    back to the country code and say so in `limitations`.
    """
    section = raw.get("profilePositions")
    if not section:
        return {}
    ordered, _ = _index_by_urn(section)
    names: dict[str, str] = {}
    for entity in ordered:
        geo_urn = entity.get("geoUrn")
        name = _clean(entity.get("geoLocationName")) or _clean(entity.get("locationName"))
        if geo_urn and name:
            names.setdefault(geo_urn, name)
    return names


def _experience(raw: dict[str, Json]) -> list[ExperienceEntry]:
    """One entry per *role*, sourced from `profilePositions`.

    Not from `profilePositionGroups`: a group carries a single date range
    spanning the member's entire tenure at that company, so attaching it to
    each role reports every role at a multi-role company as having started
    when the member joined and never ended - four concurrent, still-current
    Mastercard roles dating from 2010, on a profile where they were actually
    sequential. Each Position carries its own dateRange, title, description
    and location.

    The group is still used for two things: filling in a company name or
    date range an individual position omits, and keeping companies that have
    no position entity of their own (all that's lost for those is the title).
    """
    groups_section = raw.get("profilePositionGroups")
    group_order: list[Json] = []
    groups: dict[str, Json] = {}
    if groups_section:
        group_order, _ = _index_by_urn(groups_section)
        for group in group_order:
            company_urn = group.get("companyUrn")
            if company_urn:
                groups.setdefault(company_urn, group)

    positions_section = raw.get("profilePositions")
    positions: list[Json] = []
    if positions_section:
        positions, _ = _index_by_urn(positions_section)

    entries: list[ExperienceEntry] = []
    covered: set[tuple[str, str]] = set()

    def coverage_key(company_urn: str | None, company: str) -> tuple[str, str] | None:
        """What identifies a company for dedup purposes.

        Not every company has a companyUrn - self-employed ventures and
        personal projects have no LinkedIn company page. Keying dedup on the
        urn alone let those through twice: once from profilePositions with a
        title, then again from the position-group fallback without one.
        """
        if company_urn:
            return ("urn", company_urn)
        return ("name", company.casefold()) if company else None

    for position in positions:
        company_urn = position.get("companyUrn")
        group = groups.get(company_urn) if company_urn else None
        company = (
            _clean(position.get("companyName"))
            or _clean((group or {}).get("companyName"))
            or ""
        )
        key = coverage_key(company_urn, company)
        if key is not None:
            covered.add(key)
        start, end = _date_range(position)
        if start is None and end is None and group is not None:
            start, end = _date_range(group)
        entries.append(
            ExperienceEntry(
                company=company,
                company_urn=company_urn,
                title=_clean(position.get("title")),
                description=_clean(position.get("description")),
                location=_clean(position.get("geoLocationName"))
                or _clean(position.get("locationName")),
                start=start,
                end=end,
            )
        )

    for group in group_order:
        company_urn = group.get("companyUrn")
        company = _clean(group.get("companyName")) or ""
        if coverage_key(company_urn, company) in covered:
            continue
        start, end = _date_range(group)
        entries.append(
            ExperienceEntry(
                company=company,
                company_urn=company_urn,
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
                school=_clean(entity.get("schoolName")),
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
                name=_clean(entity.get("name")),
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


def denormalize(
    public_identifier: str,
    raw: dict[str, Json],
    fields: frozenset[str] | None = None,
) -> tuple[Profile, list[str]]:
    """raw maps section name (e.g. "profile", "profileEducations") to that
    section's `{"data": ..., "included": ...}` body - i.e. one fixture entry.

    `fields` is the set the caller asked for (None means all). It gates the
    `limitations` notes: a section that wasn't requested is absent by choice,
    not degraded, and reporting "experience entries have no job title" to
    someone who only asked for skills would be actively misleading.
    """
    wanted = fields if fields is not None else ALL_FIELDS
    limitations: list[str] = []

    profile_entity = _extract_profile_entity(raw.get("profile", {}))
    if profile_entity is None:
        raise ValueError("no Profile entity found in 'profile' section")

    # `or ''` rather than a .get default: these keys are present-with-null
    # on some profiles, and .get only substitutes when a key is absent -
    # the difference between "Satya Nadella" and "None Nadella".
    name = " ".join(
        part for part in (
            _clean(profile_entity.get("firstName")),
            _clean(profile_entity.get("lastName")),
        ) if part
    )

    country_code = ((profile_entity.get("location") or {}).get("countryCode"))
    geo_urn = ((profile_entity.get("geoLocation") or {}).get("geoUrn"))
    location = _geo_names(raw).get(geo_urn) if geo_urn else None
    if location is None:
        location = country_code
        if country_code and "location" in wanted:
            limitations.append(
                "location is a country code only (e.g. 'IN') - LinkedIn's profile "
                "entity carries just geoLocation.geoUrn plus a country code, and "
                "this member's geoUrn didn't appear on any of their positions, so "
                "there was no readable place name to resolve it against without a "
                "separate, undocumented geo call."
            )

    if "experience" in wanted and not raw.get("profilePositions"):
        limitations.append(
            "experience entries have no job title, and each role's dates fall back "
            "to the company-level tenure span - the profilePositions endpoint "
            "returned nothing for this profile, so only company + overall dates "
            "are populated."
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
    unnamed = sum(1 for e in profile.education if e.school is None and e.school_urn)
    if unnamed and "education" in wanted:
        limitations.append(
            f"{unnamed} education entr{'y has' if unnamed == 1 else 'ies have'} no "
            "school name - LinkedIn returned only a schoolUrn with schoolName null "
            "and no School entity to resolve it against, the same shape as the "
            "geoUrn case above. The urn is returned in school_urn."
        )

    return profile, limitations
