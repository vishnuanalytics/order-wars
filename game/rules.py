"""Deterministic rule resolution: turns a validated `FactionAction` into
state effects.

Kept separate from `agents/` on purpose: everything here is pure and
LLM-free, so it's fully unit-testable without mocking anything (see
`tests/test_rules.py`), and it's the one place capture/combat/economy rules
live — `agents/graph.py` calls `resolve_action` rather than mutating state
itself.

Scoped deliberately simple for Phase 4 (see CLAUDE.md "Project phases" and
`agents/actions.py`): one pooled army per faction (no per-province
garrisons), one-shot combat on `move_army` into enemy territory (no
multi-turn sieges), and diplomacy as a plain reciprocal handshake (no
power-triggered coalition mechanics). All of that is real Phase 5 territory,
not missing polish.
"""

import math

from agents.actions import FactionAction
from agents.state import FactionState, GameState
from map_data.loader import get_province

# Each owned province yields 1 unit/turn of the resource tied to its
# terrain (see map_data.generate_map's Stage 1 terrain classification) —
# coastal provinces trade for gold, plains grow grain, hills mine iron.
# An unrecognized/missing terrain falls back to gold, matching the
# project's pre-terrain behavior.
TERRAIN_RESOURCE: dict[str, str] = {
    "coastal": "gold",
    "plains": "grain",
    "hills": "iron",
}
INCOME_PER_PROVINCE = 1

# Per-unit-type build costs, keyed by resource. A dict-of-dicts (not a flat
# int) on purpose: unit composition (more unit types, each with their own
# cost) is a later stage — this shape lets that stage add entries here
# without changing the build_unit resolution logic below.
UNIT_COSTS: dict[str, dict[str, int]] = {
    "legion": {"gold": 10, "iron": 5},
}
BUILD_UNIT_TYPE = "legion"
COMBAT_LOSER_ATTRITION = 0.5  # fraction of units the loser sheds
COMBAT_WINNER_ATTRITION = 0.1  # fraction of units the winner still sheds


def pair_key(faction_a: str, faction_b: str) -> str:
    """Order-independent key for a pairwise relation between two factions."""
    return "|".join(sorted((faction_a, faction_b)))


def territory_of(state: GameState, faction_id: str) -> list[str]:
    return [pid for pid, owner in state["province_owner"].items() if owner == faction_id]


def diplomatic_status_between(state: GameState, faction_a: str, faction_b: str) -> str:
    return state["diplomatic_status"].get(pair_key(faction_a, faction_b), "neutral")


def _total_units(units: dict[str, int]) -> int:
    return sum(units.values())


def _apply_income(faction: FactionState, owned: list[str]) -> None:
    """Applied unconditionally every turn regardless of the faction's chosen
    action (same as before terrain existed) — each owned province adds
    INCOME_PER_PROVINCE of whatever resource its terrain yields."""
    faction["resources"] = dict(faction["resources"])
    for province_id in owned:
        province = get_province(province_id)
        resource = TERRAIN_RESOURCE.get(province.terrain, "gold") if province else "gold"
        faction["resources"][resource] = (
            faction["resources"].get(resource, 0) + INCOME_PER_PROVINCE
        )


def _attrit(units: dict[str, int], fraction: float) -> dict[str, int]:
    """Reduce every nonzero unit type by `fraction`, rounded up.

    Ceiling, not floor: a losing force should lose *something* — floor
    rounding let any 1-unit stack take 50% losses and keep its unit
    (`int(1 * 0.5) == 0`), making small forces effectively immortal.
    """
    return {
        unit_type: max(0, count - math.ceil(count * fraction))
        for unit_type, count in units.items()
    }


def resolve_action(state: GameState, faction_id: str, action: FactionAction) -> dict:
    """Apply `action` (already sanitized by the caller) for `faction_id`.

    Returns a partial `GameState` update: full replacement values for
    `factions`, `province_owner`, `diplomatic_status`, and
    `pending_proposals` (LangGraph has no merge reducer for these dict
    fields, so a node must return the complete new value, not a patch — see
    `agents/state.py`), plus `resolution` (a short human-readable string the
    caller appends to the turn's log line).
    """
    factions = dict(state["factions"])
    faction = dict(factions[faction_id])
    faction["resources"] = dict(faction["resources"])
    faction["units"] = dict(faction["units"])

    owned = territory_of(state, faction_id)
    _apply_income(faction, owned)

    province_owner = dict(state["province_owner"])
    diplomatic_status = dict(state["diplomatic_status"])
    pending_proposals = dict(state["pending_proposals"])
    resolution = ""

    if action.action_type == "hold":
        resolution = "held position"

    elif action.action_type == "build_unit":
        cost = UNIT_COSTS[BUILD_UNIT_TYPE]
        resources = faction["resources"]
        if all(resources.get(res, 0) >= amount for res, amount in cost.items()):
            for res, amount in cost.items():
                resources[res] -= amount
            faction["units"][BUILD_UNIT_TYPE] = faction["units"].get(BUILD_UNIT_TYPE, 0) + 1
            resolution = f"built 1 {BUILD_UNIT_TYPE}"
        else:
            cost_str = " and ".join(f"{amount} {res}" for res, amount in cost.items())
            resolution = f"tried to build a {BUILD_UNIT_TYPE} but lacked {cost_str}"

    elif action.action_type == "declare_war":
        target = action.target_faction
        diplomatic_status[pair_key(faction_id, target)] = "war"
        pending_proposals.pop(f"{faction_id}->{target}", None)
        pending_proposals.pop(f"{target}->{faction_id}", None)
        resolution = f"declared war on {target}"

    elif action.action_type == "negotiate":
        target = action.target_faction
        proposal = action.proposal
        incoming_key = f"{target}->{faction_id}"
        if pending_proposals.get(incoming_key) == proposal:
            diplomatic_status[pair_key(faction_id, target)] = proposal
            pending_proposals.pop(incoming_key, None)
            pending_proposals.pop(f"{faction_id}->{target}", None)
            resolution = f"and {target} agreed to a {proposal}"
        else:
            pending_proposals[f"{faction_id}->{target}"] = proposal
            resolution = f"proposed a {proposal} to {target}"

    elif action.action_type == "move_army":
        target_province = action.target_province
        owner = province_owner.get(target_province)
        if owner is None or owner == faction_id:
            province_owner[target_province] = faction_id
            resolution = f"moved into {target_province} (now held)"
        elif diplomatic_status_between(state, faction_id, owner) != "war":
            resolution = f"cannot move into {owner}'s {target_province} without being at war"
        else:
            defender = dict(factions[owner])
            defender["units"] = dict(defender["units"])
            attacker_strength = _total_units(faction["units"])
            defender_strength = _total_units(defender["units"])
            if attacker_strength > defender_strength:
                province_owner[target_province] = faction_id
                faction["units"] = _attrit(faction["units"], COMBAT_WINNER_ATTRITION)
                defender["units"] = _attrit(defender["units"], COMBAT_LOSER_ATTRITION)
                resolution = f"won the battle for {target_province}, captured from {owner}"
            else:
                faction["units"] = _attrit(faction["units"], COMBAT_LOSER_ATTRITION)
                defender["units"] = _attrit(defender["units"], COMBAT_WINNER_ATTRITION)
                resolution = f"lost the battle for {target_province} (held by {owner})"
            factions[owner] = defender

    factions[faction_id] = faction

    return {
        "factions": factions,
        "province_owner": province_owner,
        "diplomatic_status": diplomatic_status,
        "pending_proposals": pending_proposals,
        "resolution": resolution,
    }
