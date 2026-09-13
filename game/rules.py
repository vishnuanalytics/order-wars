"""Deterministic rule resolution: turns a validated `FactionAction` into
state effects.

Kept separate from `agents/` on purpose: everything here is pure and
LLM-free, so it's fully unit-testable without mocking anything (see
`tests/test_rules.py`), and it's the one place capture/combat/economy rules
live — `agents/graph.py` calls `resolve_action` rather than mutating state
itself.

Combat weighs unit-type matchups (COUNTERS/_effective_strength) and
attacking enemy territory is a multi-turn siege (see SIEGE_TURNS_TO_DECIDE),
but is still scoped deliberately simple otherwise (see CLAUDE.md "Gameplay
depth rollout" and `agents/actions.py`): one pooled army per faction (no
per-province garrisons — a siege tracks *who* is attacking *which*
province, not where either side's units physically sit) and diplomacy as
a plain reciprocal handshake (no power-triggered coalition mechanics).
Those are later gameplay-depth stages, not missing polish.
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
# int) on purpose: this shape let Stage 2 land the single "legion" entry
# without the build_unit resolution logic below needing to change once
# Stage 3 (here) added cavalry/siege_engine alongside it.
UNIT_COSTS: dict[str, dict[str, int]] = {
    "legion": {"gold": 10, "iron": 5},
    "cavalry": {"gold": 15, "grain": 10},  # horses need feeding, not mining
    "siege_engine": {"iron": 20, "gold": 5},  # engineering-heavy, not manpower
}
DEFAULT_UNIT_TYPE = "legion"  # what build_unit builds if a turn doesn't specify

# Rock-paper-scissors: each key is stronger against its value.
# cavalry (mobility) > legion (line infantry) > siege_engine (immobile,
# vulnerable in melee) > cavalry (shredded by ranged bombardment).
COUNTERS: dict[str, str] = {
    "cavalry": "legion",
    "legion": "siege_engine",
    "siege_engine": "cavalry",
}
COUNTER_BONUS = 0.5  # +50% effective strength vs. the type this counters

COMBAT_LOSER_ATTRITION = 0.5  # fraction of units the loser sheds
COMBAT_WINNER_ATTRITION = 0.1  # fraction of units the winner still sheds

# A move_army into enemy territory at war begins or presses a siege rather
# than resolving combat immediately. SIEGE_TURNS_TO_DECIDE consecutive
# presses by the same attacker on the same province (their own turns, not
# calendar turns) force the decisive battle; pressing a different target,
# or having anything else happen to the province in between (ownership
# change, war ending), abandons the siege with no losses to either side.
SIEGE_TURNS_TO_DECIDE = 2

# Defenders fighting on hills get a home-terrain advantage; open plains and
# coastal provinces don't (an unrecognized/missing terrain defaults to 0).
TERRAIN_DEFENSE_BONUS: dict[str, float] = {"hills": 0.3}


def pair_key(faction_a: str, faction_b: str) -> str:
    """Order-independent key for a pairwise relation between two factions."""
    return "|".join(sorted((faction_a, faction_b)))


def territory_of(state: GameState, faction_id: str) -> list[str]:
    return [pid for pid, owner in state["province_owner"].items() if owner == faction_id]


def diplomatic_status_between(state: GameState, faction_a: str, faction_b: str) -> str:
    return state["diplomatic_status"].get(pair_key(faction_a, faction_b), "neutral")


def _effective_strength(units: dict[str, int], enemy_units: dict[str, int]) -> float:
    """Combat strength of `units` against a specific `enemy_units`
    composition — a plain unit-count sum (like before unit composition
    existed) scaled up by COUNTER_BONUS in proportion to how much of the
    enemy's force this side's unit types counter (see COUNTERS).

    Scaling by the *fraction* of the enemy composition countered (not a
    flat bonus whenever any countered unit is present at all) means a
    handful of cavalry can't claim a full bonus against an army that's
    mostly siege engines — only the legion slice of that army is actually
    a favorable matchup.
    """
    enemy_total = sum(enemy_units.values()) or 1
    strength = 0.0
    for unit_type, count in units.items():
        countered_type = COUNTERS.get(unit_type)
        countered_fraction = (enemy_units.get(countered_type, 0) / enemy_total) if countered_type else 0.0
        strength += count * (1 + COUNTER_BONUS * countered_fraction)
    return strength


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
    `factions`, `province_owner`, `diplomatic_status`, `pending_proposals`,
    and `sieges` (LangGraph has no merge reducer for these dict fields, so
    a node must return the complete new value, not a patch — see
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
    sieges = dict(state["sieges"])
    resolution = ""

    # A siege only persists while its attacker keeps pressing that exact
    # target every one of their own turns — abandon any of this faction's
    # other in-progress sieges before the action below (possibly) starts
    # or continues a new one.
    pressed_target = action.target_province if action.action_type == "move_army" else None
    for province_id, siege in list(sieges.items()):
        if siege["attacker_id"] == faction_id and province_id != pressed_target:
            del sieges[province_id]

    if action.action_type == "hold":
        resolution = "held position"

    elif action.action_type == "build_unit":
        unit_type = action.unit_type or DEFAULT_UNIT_TYPE
        cost = UNIT_COSTS[unit_type]
        resources = faction["resources"]
        if all(resources.get(res, 0) >= amount for res, amount in cost.items()):
            for res, amount in cost.items():
                resources[res] -= amount
            faction["units"][unit_type] = faction["units"].get(unit_type, 0) + 1
            resolution = f"built 1 {unit_type}"
        else:
            cost_str = " and ".join(f"{amount} {res}" for res, amount in cost.items())
            resolution = f"tried to build a {unit_type} but lacked {cost_str}"

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
            sieges.pop(target_province, None)  # moot once peacefully held
            resolution = f"moved into {target_province} (now held)"
        elif diplomatic_status_between(state, faction_id, owner) != "war":
            sieges.pop(target_province, None)  # can't besiege without being at war
            resolution = f"cannot move into {owner}'s {target_province} without being at war"
        else:
            existing = sieges.get(target_province)
            already_pressing = existing is not None and existing["attacker_id"] == faction_id
            progress = existing["progress"] + 1 if already_pressing else 1

            if progress < SIEGE_TURNS_TO_DECIDE:
                sieges[target_province] = {"attacker_id": faction_id, "progress": progress}
                resolution = (
                    f"began a siege of {target_province}" if progress == 1
                    else f"pressed the siege of {target_province}"
                )
            else:
                defender = dict(factions[owner])
                defender["units"] = dict(defender["units"])
                attacker_strength = _effective_strength(faction["units"], defender["units"])
                defender_strength = _effective_strength(defender["units"], faction["units"])
                province = get_province(target_province)
                terrain = province.terrain if province else None
                defender_strength *= 1 + TERRAIN_DEFENSE_BONUS.get(terrain, 0.0)

                sieges.pop(target_province, None)
                if attacker_strength > defender_strength:
                    province_owner[target_province] = faction_id
                    faction["units"] = _attrit(faction["units"], COMBAT_WINNER_ATTRITION)
                    defender["units"] = _attrit(defender["units"], COMBAT_LOSER_ATTRITION)
                    resolution = f"broke the siege of {target_province}, captured from {owner}"
                else:
                    faction["units"] = _attrit(faction["units"], COMBAT_LOSER_ATTRITION)
                    defender["units"] = _attrit(defender["units"], COMBAT_WINNER_ATTRITION)
                    resolution = f"lost the siege of {target_province} (held by {owner})"
                factions[owner] = defender

    factions[faction_id] = faction

    return {
        "factions": factions,
        "province_owner": province_owner,
        "diplomatic_status": diplomatic_status,
        "pending_proposals": pending_proposals,
        "sieges": sieges,
        "resolution": resolution,
    }
