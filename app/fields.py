"""Which output fields a caller asked for, and which Voyager sections that
implies fetching.

The whole point of `?fields=` is that a live fetch's cost is dominated by the
paced section fan-out - seven upstream requests, one at a time with a jittered
pause. A caller who wants a name and a headline should not pay for six section
fetches to get two values that came back on the resolve call. Every output
field maps to exactly one section (experience to two), so the mapping is
mechanical and the saving is real: ~9.5s down to ~0.5s at the narrow end.
"""
from __future__ import annotations

# Output field -> the sections that must be fetched to populate it, beyond
# the `profiles?q=memberIdentity` resolve that every request makes anyway.
FIELD_SECTIONS: dict[str, tuple[str, ...]] = {
    # Free: these all come off the Profile entity in the resolve response.
    "public_identifier": (),
    "name": (),
    "headline": (),
    "about": (),
    "images": (),

    # Not free, and not obvious: `location` needs profilePositions even though
    # it isn't experience data. LinkedIn's profile entity carries only a
    # country code and an opaque geoUrn - the readable city string appears
    # nowhere in the resolve response. The denormalizer recovers it by
    # matching that geoUrn against the geoUrn -> geoLocationName pairs the
    # *positions* response happens to carry. Omitting positions here would
    # make ?fields=location quietly return "US" instead of "Redmond,
    # Washington" - a silent degradation, which is worse than the extra
    # request.
    "location": ("profilePositions",),

    # Both, deliberately. profilePositionGroups alone gives one company-level
    # tenure span for every role at that company; profilePositions alone loses
    # companies that have no position entity. See app/denormalize.py::_experience.
    "experience": ("profilePositionGroups", "profilePositions"),

    "education": ("profileEducations",),
    "skills": ("profileSkills",),
    "certifications": ("profileCertifications",),
    "languages": ("profileLanguages",),
}

ALL_FIELDS = frozenset(FIELD_SECTIONS)

# Always returned, whatever was asked for: both come off the resolve call at
# no extra cost, and a response that can't be tied back to a person is not
# much use to the caller who receives it.
ALWAYS = frozenset({"public_identifier", "name"})


class UnknownField(ValueError):
    pass


def parse(raw: str | None) -> frozenset[str]:
    """`None` or an empty value means every field - narrowing is opt-in, so an
    existing caller who has never heard of this parameter keeps getting exactly
    what they got before."""
    if raw is None or not raw.strip():
        return frozenset(ALL_FIELDS)

    requested = {part.strip().lower() for part in raw.split(",") if part.strip()}
    if not requested:
        return frozenset(ALL_FIELDS)

    unknown = sorted(requested - ALL_FIELDS)
    if unknown:
        raise UnknownField(
            f"unknown field(s): {', '.join(unknown)}. "
            f"Valid fields: {', '.join(sorted(ALL_FIELDS))}"
        )
    return frozenset(requested | ALWAYS)


def sections_for(fields: frozenset[str], ordered: tuple[str, ...]) -> tuple[str, ...]:
    """The sections needed for `fields`, in `ordered`'s order.

    Order is preserved rather than rebuilt from the field set because it
    encodes something the mapping doesn't: when throttling starts mid-sequence
    whatever is last is what dies, so the most valuable sections go first.
    """
    needed = {section for field in fields for section in FIELD_SECTIONS.get(field, ())}
    return tuple(section for section in ordered if section in needed)
