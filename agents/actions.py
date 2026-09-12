"""The structured action schema factions choose from each turn.

Phase 4: actions are grounded in the real map (`target_province` must be a
real, currently-adjacent province — see `agents/graph.py`'s sanitization)
instead of Phase 2's abstract `action_type`/`target_faction` pair. Effects are
computed by `game/rules.py`, not here — this module only defines the shape an
LLM call must produce.

Scoped deliberately: no per-province garrisons or siege mechanics (a
faction's units are one pooled army, and a `move_army` into enemy territory
is one-shot combat, not a multi-turn siege) — that nuance is Phase 5, per
CLAUDE.md's directory structure. Diplomacy is a simple reciprocal handshake
(a matching `negotiate` from both sides resolves it) rather than the fuller
power-triggered coalition mechanics also documented as Phase 5 territory.
"""

from typing import Literal

from pydantic import BaseModel, Field

ActionType = Literal["move_army", "build_unit", "negotiate", "declare_war", "hold"]
ProposalType = Literal["truce", "alliance"]


class FactionAction(BaseModel):
    """One faction's decision for a single turn."""

    action_type: ActionType = Field(
        description=(
            "move_army: send your army into target_province — must be one "
            "of your current territory or adjacent to it. Unclaimed or "
            "your-own territory is captured/reinforced peacefully; enemy "
            "territory triggers combat and only succeeds if you're at war "
            "with its owner. build_unit: spend resources to add a unit. "
            "negotiate: propose (or, if target_faction already proposed the "
            "same thing to you, accept) a truce or alliance with "
            "target_faction. declare_war: unilaterally go to war with "
            "target_faction. hold: do nothing notable this turn."
        )
    )
    target_province: str | None = Field(
        default=None, description="Required for move_army: a real province id."
    )
    target_faction: str | None = Field(
        default=None, description="Required for negotiate/declare_war: another faction's id."
    )
    proposal: ProposalType | None = Field(
        default=None, description="Required for negotiate: 'truce' or 'alliance'."
    )
    rationale: str = Field(description="One short sentence explaining the choice.")
