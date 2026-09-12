"""The structured action schema factions choose from each turn.

Deliberately abstract — no army/province targets — because there's no map,
territory, or unit state until Phase 4. The point right now is the *shape*:
a schema-validated decision instead of a free-text sentence (Phase 1's
`last_decision`), obtained via `.with_structured_output()` (see
`agents/llm.py`) rather than parsing prose. Phase 4/5 will introduce real
mutating tools (`move_army`, `propose_trade`, `build_unit`, per CLAUDE.md
"Agent & simulation design") that supersede this; this schema does not try to
anticipate their exact shape.
"""

from typing import Literal

from pydantic import BaseModel, Field

ActionType = Literal["expand", "fortify", "negotiate", "raid", "hold"]


class FactionAction(BaseModel):
    """One faction's decision for a single turn."""

    action_type: ActionType = Field(
        description=(
            "expand: grow economy/influence. fortify: strengthen defenses. "
            "negotiate: seek peace/trade with target_faction. raid: act "
            "aggressively against target_faction. hold: do nothing notable "
            "this turn."
        )
    )
    target_faction: str | None = Field(
        default=None,
        description=(
            "The other faction this action concerns (required for "
            "negotiate/raid, omitted otherwise)."
        ),
    )
    rationale: str = Field(description="One short sentence explaining the choice.")
