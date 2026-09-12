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

    # Costs a request, but not a *section* one - see FOLLOWER_COUNT below.
    "follower_count": (),
}

# Opt-in: valid in an explicit `?fields=` list, absent from the default set.
#
# Every other field here is either free off the resolve or pays for a section
# in the existing fan-out, so folding them all into one "everything" default
# costs nothing extra. follower_count is different: it is a separate upstream
# request to a separate resource (feed/dash/followingStates - see
# app/voyager_client.py). Putting it in the default would make every existing
# caller pay for a field they never asked for, and would change the default
# response shape for all of them. So "all fields" and "the default" stop being
# the same set here, and the default is the one that stayed still.
OPT_IN = frozenset({"follower_count"})

# Everything that exists - the validation set.
ALL_FIELDS = frozenset(FIELD_SECTIONS)

# What a caller who says nothing gets. Byte-identical to what ALL_FIELDS
# returned before follower_count existed.
DEFAULT_FIELDS = ALL_FIELDS - OPT_IN

# Always returned, whatever was asked for: both come off the resolve call at
# no extra cost, and a response that can't be tied back to a person is not
# much use to the caller who receives it.
ALWAYS = frozenset({"public_identifier", "name"})


class UnknownField(ValueError):
    pass


def parse(raw: str | None) -> frozenset[str]:
    """`None` or an empty value means every *default* field - narrowing is
    opt-in, so an existing caller who has never heard of this parameter keeps
    getting exactly what they got before. Note this is DEFAULT_FIELDS, not
    ALL_FIELDS: an opt-in field has to be named explicitly, which is the whole
    point of it being opt-in."""
    if raw is None or not raw.strip():
        return frozenset(DEFAULT_FIELDS)

    requested = {part.strip().lower() for part in raw.split(",") if part.strip()}
    if not requested:
        return frozenset(DEFAULT_FIELDS)

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


def needs_following_state(fields: frozenset[str]) -> bool:
    """Whether this request has to fetch the FollowingState entity.

    Kept out of `sections_for` deliberately: that function's contract is the
    dash section fan-out, whose order encodes which sections survive
    throttling. FollowingState is a different resource with a different URL
    shape and no place in that ordering, so pretending it is a section would
    make `sections_for` lie about what it returns.
    """
    return "follower_count" in fields
