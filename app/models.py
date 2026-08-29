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


class Meta(BaseModel):
    """Everything about *this call* that isn't profile data.

    Exists so a caller can answer, from the response alone: where did this
    come from, how old is it, what did it cost, and how much budget is left.
    All of it was knowable server-side before and simply wasn't exposed -
    which also made every latency change unmeasurable from the outside.
    """

    source: str  # "live" | "cache"
    fetched_at: str
    request_id: str
    duration_ms: int
    # 0 on a cache hit; on a live fetch, the number of Voyager requests
    # actually issued - one resolve plus one per section. Note this is
    # normally ~7x the number of /profile calls the daily quota counts.
    upstream_requests: int = 0
    # None on a live fetch (the data is new), otherwise how long the cached
    # copy has been sitting on disk.
    cache_age_seconds: int | None = None
    # The output fields this response carries. Always present, so a caller
    # can tell "you didn't ask for experience" from "this member has none".
    fields: list[str] = []
    # None when the quota store couldn't be consulted, which is deliberately
    # not an error - an Upstash hiccup must not fail a good response.
    quota_remaining: int | None = None


class ProfileResponse(BaseModel):
    # `source` and `fetched_at` are duplicated from `meta` on purpose: they
    # were top-level before `meta` existed, and removing them in the same
    # release that adds it would break every existing consumer for no reason.
    # Deprecated - read them from `meta`.
    source: str  # "live" | "cache"
    fetched_at: str
    meta: Meta
    profile: Profile
    limitations: list[str] = []
