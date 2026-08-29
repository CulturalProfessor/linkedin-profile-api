import json
from pathlib import Path

from app.denormalize import denormalize

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures"


def _load(name: str) -> dict:
    fixture = json.loads((FIXTURES_DIR / name).read_text())
    # "urn" is a bare string and "_"-prefixed keys are fixture notes; every
    # other key is a section whose Voyager response body we want.
    return {
        k: v["body"] for k, v in fixture.items() if k != "urn" and not k.startswith("_")
    }


def test_denormalize_sample_fixture():
    """fixtures/sample_raw.json: a fully synthetic mid-level engineer profile.
    Covers: month-precision dates, an in-progress role (no end date), a
    populated languages list, and title-joining via profilePositions for
    two of three companies (the third exercises the no-title fallback).
    """
    raw = _load("sample_raw.json")
    profile, limitations = denormalize("jamie-lin-dev", raw)

    assert profile.name == "Jamie Lin"
    assert profile.headline == "Senior Backend Engineer | Distributed Systems | Open Source Maintainer"
    assert profile.location == "CA"
    assert profile.about.startswith("I build backend systems that stay boring under load")

    assert len(profile.experience) == 3
    nimbus = next(e for e in profile.experience if e.company == "Nimbus Cloud Systems")
    assert nimbus.title == "Senior Backend Engineer"
    assert nimbus.start == "2023-03"
    assert nimbus.end is None  # still current

    dataforge = next(e for e in profile.experience if e.company == "DataForge Inc.")
    assert dataforge.title == "Backend Engineer"
    assert dataforge.end == "2023-02"

    startwell = next(e for e in profile.experience if e.company == "StartWell Labs")
    assert startwell.title is None  # not present in profilePositions - fallback path

    assert len(profile.education) == 1
    edu = profile.education[0]
    assert edu.school == "Riverside State University"
    assert edu.degree == "Master of Science"
    assert edu.start == "2017"
    assert edu.end == "2019"

    assert profile.skills == [
        "Python", "Distributed Systems", "Kubernetes", "PostgreSQL",
        "Go", "gRPC", "System Design", "Mentoring",
    ]

    assert len(profile.certifications) == 2
    cka = next(c for c in profile.certifications if "Kubernetes" in c.name)
    assert cka.authority == "The Linux Foundation"
    assert cka.license_number == "CKA-2022-88213"
    assert cka.issued == "2022-09"

    assert len(profile.languages) == 2
    assert {lang.name for lang in profile.languages} == {"English", "Mandarin"}

    assert profile.images.profile_picture.startswith("https://media.licdn.com/")
    assert profile.images.background_picture.startswith("https://media.licdn.com/")

    assert any("location is a country code only" in note for note in limitations)
    # profilePositions is present here, so the title-missing limitation shouldn't fire.
    assert not any("no job title" in note for note in limitations)


def test_denormalize_notable_fixture_handles_missing_sections():
    """fixtures/sample_raw_notable.json: a fully synthetic founder/CTO-style
    profile covering the opposite edge cases - no summary, no images, no
    certifications/languages, multiple degrees, and no profilePositions
    section at all (title-missing limitation should fire).
    """
    raw = _load("sample_raw_notable.json")
    profile, limitations = denormalize("morgan-cole-founder", raw)

    assert profile.name == "Morgan Cole"
    assert profile.about is None
    assert profile.location == "US"

    assert len(profile.experience) == 1
    role = profile.experience[0]
    assert role.company == "Northstar Robotics"
    assert role.title is None
    assert role.start == "2015-01"
    assert role.end is None

    assert len(profile.education) == 2
    degrees = {e.degree for e in profile.education}
    assert degrees == {"Bachelor of Science", "Master of Business Administration"}

    assert profile.certifications == []
    assert profile.languages == []

    assert profile.images.profile_picture is None
    assert profile.images.background_picture is None

    assert any("no job title" in note for note in limitations)


def test_multiple_roles_at_one_company_keep_their_own_dates():
    """The bug this guards against: `profilePositionGroups` carries a single
    date range spanning the member's whole tenure at a company. Deriving each
    role's dates from the group reported every role at a multi-role company as
    starting when the member joined and never ending - on the live profile this
    was found against, four sequential Mastercard roles all rendered as
    "2010 - present". Dates must come from each Position instead.
    """
    raw = _load("sample_raw_multirole.json")
    profile, _ = denormalize("ava-reyes-synthetic", raw)

    helio = [e for e in profile.experience if e.company == "Helio Systems"]
    assert len(helio) == 2

    vp = next(e for e in helio if e.title == "VP Engineering")  # trailing space stripped
    assert (vp.start, vp.end) == ("2022-01", None)
    assert vp.description == "Lead a platform org of 40."
    assert vp.location == "Denver Metropolitan Area"

    staff = next(e for e in helio if e.title == "Staff Engineer")
    assert (staff.start, staff.end) == ("2018-04", "2021-12")

    # The group's own span (2018-04 -> present) must not have leaked onto both.
    assert {(e.start, e.end) for e in helio} == {("2022-01", None), ("2018-04", "2021-12")}


def test_company_with_no_position_entity_is_still_returned():
    """Quiet Foundry has a position *group* but no Position of its own. It must
    still appear - only the title is lost - so a partial or failed
    profilePositions fetch can never silently drop whole companies."""
    raw = _load("sample_raw_multirole.json")
    profile, _ = denormalize("ava-reyes-synthetic", raw)

    quiet = next(e for e in profile.experience if e.company == "Quiet Foundry")
    assert quiet.title is None
    assert (quiet.start, quiet.end) == ("2015-09", "2018-03")


def test_city_resolved_from_matching_position_geo_urn():
    """The profile entity has only `geoLocation.geoUrn` + a country code. When
    that geoUrn also appears on one of the member's positions, the readable
    place name comes free from data already fetched - no geo-resolve call."""
    raw = _load("sample_raw_multirole.json")
    profile, limitations = denormalize("ava-reyes-synthetic", raw)

    assert profile.location == "Denver Metropolitan Area"
    assert not any("country code only" in note for note in limitations)


def test_city_falls_back_to_country_code_when_geo_urn_unmatched():
    """sample_raw.json's positions carry no geoUrn, so the profile's geoUrn
    can't be resolved and we degrade to the country code - and say so."""
    raw = _load("sample_raw.json")
    profile, limitations = denormalize("jamie-lin-dev", raw)

    assert profile.location == "CA"
    assert any("country code only" in note for note in limitations)


def test_company_without_urn_is_not_duplicated():
    """Found on a real profile: self-employed ventures and personal projects
    have no LinkedIn company page, so companyUrn is null. Dedup keyed on the
    urn alone let those through twice - once from profilePositions with a
    title, then again from the position-group fallback without one."""
    raw = {
        "profile": {
            "data": {"*elements": ["urn:li:fsd_profile:x"]},
            "included": [{
                "$type": "com.linkedin.voyager.dash.identity.profile.Profile",
                "entityUrn": "urn:li:fsd_profile:x",
                "firstName": "Sam", "lastName": "Doe",
                "location": {"countryCode": "US"},
            }],
        },
        "profilePositionGroups": {
            "data": {"*elements": ["g1"]},
            "included": [{
                "entityUrn": "g1", "companyName": "Side Project", "companyUrn": None,
                "dateRange": {"start": {"year": 2020, "month": 1}},
            }],
        },
        "profilePositions": {
            "data": {"*elements": ["p1"]},
            "included": [{
                "entityUrn": "p1", "companyName": "Side Project", "companyUrn": None,
                "title": "Founder",
                "dateRange": {"start": {"year": 2020, "month": 1}},
            }],
        },
    }
    profile, _ = denormalize("sam-doe", raw)

    assert len(profile.experience) == 1
    assert profile.experience[0].title == "Founder"


def test_null_names_do_not_crash_and_are_reported():
    """Found on a real profile (Satya Nadella): an education entry carries a
    schoolUrn with schoolName null. `.get(key, "")` substitutes only when a key
    is *absent*, not when it is present-and-null, so None reached pydantic and
    the endpoint 500'd. Same pattern applied to certification names and to the
    first/last name join, where it would have rendered "None Nadella"."""
    raw = {
        "profile": {
            "data": {"*elements": ["urn:li:fsd_profile:x"]},
            "included": [{
                "$type": "com.linkedin.voyager.dash.identity.profile.Profile",
                "entityUrn": "urn:li:fsd_profile:x",
                "firstName": "Ada", "lastName": None,
                "location": {"countryCode": "US"},
            }],
        },
        "profileEducations": {
            "data": {"*elements": ["e1", "e2"]},
            "included": [
                {"entityUrn": "e1", "schoolName": None,
                 "schoolUrn": "urn:li:fsd_school:18315"},
                {"entityUrn": "e2", "schoolName": "Real University",
                 "schoolUrn": "urn:li:fsd_school:1"},
            ],
        },
        "profileCertifications": {
            "data": {"*elements": ["c1"]},
            "included": [{"entityUrn": "c1", "name": None, "authority": "Someone"}],
        },
    }
    profile, limitations = denormalize("ada", raw)

    assert profile.name == "Ada"  # not "Ada None"
    assert len(profile.education) == 2
    unnamed = next(e for e in profile.education if e.school is None)
    assert unnamed.school_urn == "urn:li:fsd_school:18315"
    assert profile.certifications[0].name is None
    assert any("no school name" in note for note in limitations)
