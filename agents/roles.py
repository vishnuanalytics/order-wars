"""Agent role presets — the behavior profiles a faction can be assigned.

Names intentionally mirror `db.models.RolePreset`'s values (Phase 5's schema
for the same concept), but this module doesn't import `db` — nothing in
`agents/` connects to the database until Phase 5 wires persistence into the
app (see CLAUDE.md "Persistence"). Keep the two lists in sync by hand until
then.
"""

from typing import Literal

RolePresetName = Literal[
    "expansionist", "warmonger", "diplomat_trader", "isolationist", "custom"
]

ROLE_PRESET_PROMPTS: dict[str, str] = {
    "expansionist": (
        "You prioritize claiming new territory and growing your economy. "
        "You avoid war unless it clearly serves expansion."
    ),
    "warmonger": (
        "You prioritize military conquest. You are quick to raid or declare "
        "war on weaker neighbors and slow to seek peace."
    ),
    "diplomat_trader": (
        "You prioritize alliances, trade, and negotiated settlements. You "
        "avoid open conflict unless directly threatened."
    ),
    "isolationist": (
        "You prioritize fortifying your own borders and avoid entangling "
        "with other factions unless provoked."
    ),
    "custom": "You act according to your own judgment, with no fixed doctrine.",
}


def describe(role_preset: str) -> str:
    """Return the personality prompt fragment for a role preset name.

    Falls back to "custom" for an unrecognized name rather than raising —
    role presets are user-supplied strings from the (future) scenario editor,
    not a closed set the code should hard-fail on.
    """
    return ROLE_PRESET_PROMPTS.get(role_preset, ROLE_PRESET_PROMPTS["custom"])


# Which specialist (see agents/graph.py's _dispatch_specialist) a faction
# reaches for first, second, and third absent a siege-in-progress or
# incoming-proposal override. Always a permutation of all three domains —
# never fully excludes one, so no role preset is permanently locked out of
# expansion/economy/diplomacy — just ordered to match each preset's stated
# priorities above.
ROLE_PRESET_SPECIALIST_ORDER: dict[str, list[str]] = {
    "expansionist": ["military", "economic", "diplomatic"],
    "warmonger": ["military", "diplomatic", "economic"],
    "diplomat_trader": ["diplomatic", "economic", "military"],
    "isolationist": ["economic", "military", "diplomatic"],
    "custom": ["military", "economic", "diplomatic"],
}


def specialist_order(role_preset: str) -> list[str]:
    """Return the specialist rotation order for a role preset name.

    Same unrecognized-name fallback as `describe()` — role presets are
    user-supplied, not a closed set the code should hard-fail on.
    """
    return ROLE_PRESET_SPECIALIST_ORDER.get(role_preset, ROLE_PRESET_SPECIALIST_ORDER["custom"])
