"""Output shape returned by the API. Independent of Voyager's own field names."""
from __future__ import annotations

from pydantic import BaseModel


class Image(BaseModel):
    url: str
    width: int
    height: int


class ExperienceEntry(BaseModel):
    company: str
    company_urn: str | None = None
    title: str | None = None
    description: str | None = None
    location: str | None = None
    start: str | None = None
    end: str | None = None


class EducationEntry(BaseModel):
    # Nullable: LinkedIn returns entries carrying only a schoolUrn, with
    # schoolName null and no School entity in the response to resolve it
    # against. Dropping those would lose a real education entry, so the
    # urn is returned and the name left null.
    school: str | None = None
    school_urn: str | None = None
    degree: str | None = None
    field_of_study: str | None = None
    start: str | None = None
    end: str | None = None


class CertificationEntry(BaseModel):
    name: str | None = None
    authority: str | None = None
    url: str | None = None
    license_number: str | None = None
    issued: str | None = None


class LanguageEntry(BaseModel):
    name: str
    proficiency: str | None = None


class ProfileImages(BaseModel):
    profile_picture: str | None = None
    background_picture: str | None = None


class Profile(BaseModel):
    public_identifier: str
    name: str
    headline: str | None = None
    location: str | None = None
    about: str | None = None
    experience: list[ExperienceEntry] = []
    education: list[EducationEntry] = []
    skills: list[str] = []
    certifications: list[CertificationEntry] = []
    languages: list[LanguageEntry] = []
    images: ProfileImages = ProfileImages()


class ProfileResponse(BaseModel):
    source: str  # "live" | "cache"
    fetched_at: str
    profile: Profile
    limitations: list[str] = []
