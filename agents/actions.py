"""The structured action schema factions choose from each turn.

Phase 4: actions are grounded in the real map (`target_province` must be a
real, currently-adjacent province — see `agents/graph.py`'s sanitization)
instead of Phase 2's abstract `action_type`/`target_faction` pair. Effects are
computed by `game/rules.py`, not here — this module only defines the shape an
LLM call must produce.

Scoped deliberately: no per-province garrisons (a faction's units are one
pooled army — a siege tracks who's attacking which province, not where
either side's units physically sit). Diplomacy is a simple reciprocal
handshake (a matching `negotiate` from both sides resolves it) rather than
the fuller power-triggered coalition mechanics documented as a later
gameplay-depth stage (see CLAUDE.md's "Gameplay depth rollout").
"""

from typing import Literal

from pydantic import BaseModel, Field

ActionType = Literal[
    "move_army", "build_unit", "develop_province", "negotiate", "declare_war", "hold"
]
ProposalType = Literal["truce", "alliance", "trade"]
# Rock-paper-scissors unit composition (see game/rules.py's COUNTERS):
# cavalry > legion > siege_engine > cavalry.
UnitType = Literal["legion", "cavalry", "siege_engine"]


class FactionAction(BaseModel):
    """One faction's decision for a single turn."""

    action_type: ActionType = Field(
        description=(
            "move_army: send your army into target_province — must be one "
            "of your current territory or adjacent to it. Unclaimed or "
            "your-own territory is captured/reinforced peacefully; enemy "
            "territory (only if you're at war with its owner) begins or "
            "presses a siege — the decisive battle only happens once you've "
            "targeted that same province on consecutive turns of your own. "
            "build_unit: spend resources to add a unit of "
            "unit_type (defaults to legion if unset). "
            "develop_province: spend resources to raise target_province's "
            "development level by 1 (must be your own territory). "
            "negotiate: propose (or, if target_faction already proposed the "
            "same thing to you, accept) a truce or alliance with "
            "target_faction; or propose/accept a recurring 'trade' (give "
            "offer_amount of offer_resource to target_faction every turn — "
            "a trade activates once you both have an outstanding trade "
            "offer to each other, not necessarily matching amounts). "
            "declare_war: unilaterally go to war with target_faction. "
            "hold: do nothing notable this turn."
        )
    )
    target_province: str | None = Field(
        default=None,
        description="Required for move_army/develop_province: a real province id.",
    )
    target_faction: str | None = Field(
        default=None, description="Required for negotiate/declare_war: another faction's id."
    )
    proposal: ProposalType | None = Field(
        default=None, description="Required for negotiate: 'truce', 'alliance', or 'trade'."
    )
    unit_type: UnitType | None = Field(
        default=None,
        description=(
            "Optional for build_unit: which unit type to build — "
            "'legion', 'cavalry', or 'siege_engine'. Defaults to 'legion' "
            "if unset."
        ),
    )
    offer_resource: str | None = Field(
        default=None, description="Required for negotiate with proposal='trade': e.g. 'gold'."
    )
    offer_amount: int | None = Field(
        default=None,
        description="Required for negotiate with proposal='trade': how much offer_resource to give per turn.",
    )
    rationale: str = Field(description="One short sentence explaining the choice.")


# Narrower per-specialist schemas — see agents/graph.py's `_dispatch_specialist`.
# Each restricts action_type to only what that specialist actually decides
# (Pydantic-enforced, not just a prompt request — the same reasoning as
# UnitType being a closed Literal above), and each converts directly into a
# FactionAction (its fields are always a subset, with FactionAction's
# existing defaults covering everything else) — resolve_action/
# _sanitize_action never see these narrower types, only the FactionAction
# built from one.

class MilitaryAction(BaseModel):
    """The military commander's decision: move or hold. Declaring war is a
    diplomatic act (see DiplomaticAction), not a troop movement."""

    action_type: Literal["move_army", "hold"] = Field(
        description=(
            "move_army: send your army into target_province — must be one "
            "of your current territory or adjacent to it. Unclaimed or "
            "your-own territory is captured/reinforced peacefully; enemy "
            "territory (only if already at war with its owner) begins or "
            "presses a siege. hold: do nothing notable this turn."
        )
    )
    target_province: str | None = Field(
        default=None, description="Required for move_army: a real province id."
    )
    rationale: str = Field(description="One short sentence explaining the choice.")


class EconomicAction(BaseModel):
    """The economic/logistics agent's decision: build, develop, or hold."""

    action_type: Literal["build_unit", "develop_province", "hold"] = Field(
        description=(
            "build_unit: spend resources to add a unit of unit_type "
            "(defaults to legion if unset). develop_province: spend "
            "resources to raise target_province's development level by 1 "
            "(must be your own territory). hold: do nothing notable this turn."
        )
    )
    unit_type: UnitType | None = Field(
        default=None,
        description=(
            "Optional for build_unit: which unit type to build — "
            "'legion', 'cavalry', or 'siege_engine'. Defaults to 'legion' "
            "if unset."
        ),
    )
    target_province: str | None = Field(
        default=None, description="Required for develop_province: a real province id you own."
    )
    rationale: str = Field(description="One short sentence explaining the choice.")


class DiplomaticAction(BaseModel):
    """The diplomat/trade agent's decision: negotiate, declare war, or hold."""

    action_type: Literal["negotiate", "declare_war", "hold"] = Field(
        description=(
            "negotiate: propose (or, if target_faction already proposed the "
            "same thing to you, accept) a truce or alliance with "
            "target_faction; or propose/accept a recurring 'trade' (give "
            "offer_amount of offer_resource to target_faction every turn — "
            "a trade activates once you both have an outstanding trade "
            "offer to each other, not necessarily matching amounts). "
            "declare_war: unilaterally go to war with target_faction. "
            "hold: do nothing notable this turn."
        )
    )
    target_faction: str | None = Field(
        default=None, description="Required for negotiate/declare_war: another faction's id."
    )
    proposal: ProposalType | None = Field(
        default=None, description="Required for negotiate: 'truce', 'alliance', or 'trade'."
    )
    offer_resource: str | None = Field(
        default=None, description="Required for negotiate with proposal='trade': e.g. 'gold'."
    )
    offer_amount: int | None = Field(
        default=None,
        description="Required for negotiate with proposal='trade': how much offer_resource to give per turn.",
    )
    rationale: str = Field(description="One short sentence explaining the choice.")
