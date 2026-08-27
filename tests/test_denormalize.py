import json
from pathlib import Path

from app.denormalize import denormalize

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures"


def _load(name: str) -> dict:
    fixture = json.loads((FIXTURES_DIR / name).read_text())
    return {k: v["body"] for k, v in fixture.items() if k != "urn"}


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
    assert {l.name for l in profile.languages} == {"English", "Mandarin"}

    assert profile.images.profile_picture.startswith("https://media.licdn.com/")
    assert profile.images.background_picture.startswith("https://media.licdn.com/")

    assert any("location is a country code only" in l for l in limitations)
    # profilePositions is present here, so the title-missing limitation shouldn't fire.
    assert not any("no job title" in l for l in limitations)


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

    assert any("no job title" in l for l in limitations)
