"""Narrative event tagging — Stage 11 of the gameplay-depth rollout (see
CLAUDE.md), and deliberately the last one: it benefits from every event
type the earlier stages added (sieges, rebellion, tribute, trade,
coalition calls to arms) to have something rich to narrate, instead of
just the original five action types.

A pure presentation layer over an already-persisted `GameEvent`, not a
gameplay mechanic — `classify_event` is a pure function of an event's
`event_type` and `payload`, called at *read* time (see
`backend/main.py`'s `GET /games/{id}/events`), not at write time. No DB
schema change, no new persisted state, and `game/rules.py`'s resolution
logic is completely untouched.

Deliberately reuses the human-readable `resolution` text `game/rules.py`
already writes (rather than re-deriving notability from raw structured
payload fields), since a rebellion or call-to-arms note can be appended
to *any* action's resolution regardless of `action_type` — matching on
`event_type` alone would miss those. This is a real coupling to
`game/rules.py`'s exact wording: `tests/test_narrative.py` drives real
`game.rules.resolve_action` calls (not fabricated payloads) specifically
to catch drift if that wording ever changes.
"""

from dataclasses import dataclass

# Ordered (first match wins) substring checks against an event's
# `resolution` text — used for everything except `declare_war` (see
# classify_event), which `event_type` alone already identifies unambiguously
# and more robustly than matching its resolution wording. Order only
# matters where two markers could otherwise both match the same string —
# none currently overlap, but keep more specific markers before more
# general ones if that changes.
_RESOLUTION_MARKERS: list[tuple[str, str]] = [
    ("began a siege", "siege begins"),
    ("pressed the siege", "siege continues"),
    ("broke the siege", "siege succeeds — province captured"),
    ("lost the siege", "siege fails"),
    ("rebelled and reverted to unclaimed", "rebellion"),
    ("call to arms", "call to arms"),
    ("agreed a trade", "trade agreement reached"),
    ("agreed to a alliance", "alliance formed"),
    ("agreed to a truce", "truce declared"),
    ("accepted", "tribute accepted — peace"),
]


@dataclass(frozen=True)
class NarrativeTag:
    notable: bool
    headline: str | None = None


def classify_event(event_type: str, payload: dict | None) -> NarrativeTag:
    """Is this event worth calling out in a narrative summary, and what's
    a short headline for it?

    `declare_war` is identified directly by `event_type` — unlike every
    other notable moment here, it's never a side effect appended to some
    other action's resolution, so this doesn't need the fragile-by-nature
    substring matching the rest of this function relies on. Everything
    else (a routine `hold`/`build_unit`/`develop_province` isn't
    inherently notable, but its resolution can still carry a notable side
    effect like rebellion or a call to arms, appended to *any* action's
    resolution regardless of `action_type`) has to check `resolution` text
    instead, since `event_type` alone can't distinguish a siege outcome
    from a peaceful move, or an agreed negotiation from a merely proposed
    one.
    """
    if event_type == "declare_war":
        return NarrativeTag(True, "war declared")

    resolution = (payload or {}).get("resolution") or ""
    for marker, headline in _RESOLUTION_MARKERS:
        if marker in resolution:
            return NarrativeTag(True, headline)
    return NarrativeTag(False)
