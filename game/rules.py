"""Deterministic rule resolution: turns a validated `FactionAction` into
state effects.

Kept separate from `agents/` on purpose: everything here is pure and
LLM-free, so it's fully unit-testable without mocking anything (see
`tests/test_rules.py`), and it's the one place capture/combat/economy rules
live — `agents/graph.py` calls `resolve_action` rather than mutating state
itself.

Combat weighs unit-type matchups (COUNTERS/_effective_strength), attacking
enemy territory is a multi-turn siege (see SIEGE_TURNS_TO_DECIDE), a
faction sieging far from its own territory suffers ongoing supply-line
attrition (see SUPPLY_FREE_RANGE), and a recently conquered province far
from its new owner's capital risks rebelling back to unclaimed (see
REBELLION_GRACE_TURNS) — but is still scoped deliberately simple otherwise
(see CLAUDE.md "Gameplay depth rollout" and `agents/actions.py`): one
pooled army per faction (no per-province garrisons — a siege tracks *who*
is attacking *which* province, and supply attrition is a distance proxy
off that, rather than either tracking where units physically sit) and
diplomacy as a plain reciprocal handshake (no power-triggered coalition
mechanics). Those are later gameplay-depth stages, not missing polish.

Rebellion is the project's first randomized mechanic, and it's
deliberately *not* real randomness (`random.random()`) — `_rebellion_roll`
hashes `(rebellion_seed, province_id, turn)` into a deterministic pseudo-
random float instead, so `resolve_action` stays a pure function of its
inputs (this module's docstring's first sentence) and a rebellion outcome
is exactly reproducible/testable without mocking a `random.Random`
instance. `rebellion_seed` itself is picked once per game (a real random
draw, in `agents/graph.py`'s `initial_state_for`) so different games still
feel unpredictable — only the per-call resolution is deterministic, not
the game-to-game outcome.

Province development (`develop_province`) lets a faction invest resources
into its own territory for a persistent, stacking level (capped at
MAX_PROVINCE_DEVELOPMENT) that boosts income, reduces rebellion risk, and
adds a defense bonus there — see DEVELOPMENT_*_PER_LEVEL. Deliberately
placed after sieges/rebellion (not earlier) so it has real mechanics to
plug into instead of being a standalone number rewired later. Development
persists through a change of ownership — capturing a well-developed enemy
province is valuable, not reset to 0 — since it represents built
infrastructure (roads, fortifications, administration), not the prior
owner's loyalty.
"""

import hashlib
import math

from agents.actions import FactionAction
from agents.state import FactionState, GameState
from map_data.loader import distance_between, get_province

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

# Supply-line attrition: a lightweight proxy for logistics, since the
# project deliberately has no per-province garrisons to track an army's
# actual physical position (see the module docstring). Uses the farthest
# province a faction is currently besieging as its "front line" — a siege
# is the one persistent, well-defined marker of "where this faction's
# offensive is currently committed" that already exists in state, unlike a
# one-off peaceful move_army. A faction not currently sieging anywhere
# suffers no supply attrition; sieges within SUPPLY_FREE_RANGE hexes of
# owned territory are short enough to sustain for free.
SUPPLY_FREE_RANGE = 2
SUPPLY_ATTRITION_PER_HEX = 0.05  # additional fraction lost per hex beyond the free range

# Rebellion: a recently conquered province far from its owner's capital may
# revert to unclaimed. Distinct from supply attrition on purpose — supply
# is about distance from your *current* frontier/holdings (an operational
# concern), rebellion is about distance from your *capital* specifically
# (an administrative-reach/legitimacy concern) — a faction with plenty of
# nearby territory can still fail to pacify a far-flung new conquest.
# Grace period only: once a province survives REBELLION_GRACE_TURNS turns
# under its new owner without incident, it's considered settled and never
# rebels again regardless of distance (until it changes hands once more).
REBELLION_GRACE_TURNS = 3
REBELLION_DISTANCE_THRESHOLD = 3  # hexes from capital; closer provinces never rebel
REBELLION_CHANCE_PER_TURN = 0.15

# Province development: a persistent, stacking per-province level (see the
# module docstring). Cost scales with the level being bought (buying level
# N+1 costs DEVELOP_BASE_COST * (N+1) gold) so each successive improvement
# is more expensive — a deliberate diminishing-returns curve, not a flat
# price that would let one province stack indefinitely for cheap.
MAX_PROVINCE_DEVELOPMENT = 3
DEVELOP_BASE_COST = 15  # gold, times (current_level + 1)
DEVELOPMENT_YIELD_BONUS_PER_LEVEL = 1  # extra terrain-resource income per level
DEVELOPMENT_REBELLION_REDUCTION_PER_LEVEL = 0.05  # subtracted from REBELLION_CHANCE_PER_TURN
DEVELOPMENT_DEFENSE_BONUS_PER_LEVEL = 0.1  # added on top of TERRAIN_DEFENSE_BONUS


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


def _apply_income(
    faction: FactionState, owned: list[str], province_development: dict[str, int]
) -> None:
    """Applied unconditionally every turn regardless of the faction's chosen
    action (same as before terrain existed) — each owned province adds
    INCOME_PER_PROVINCE of whatever resource its terrain yields, plus
    DEVELOPMENT_YIELD_BONUS_PER_LEVEL per level of development it has."""
    faction["resources"] = dict(faction["resources"])
    for province_id in owned:
        province = get_province(province_id)
        resource = TERRAIN_RESOURCE.get(province.terrain, "gold") if province else "gold"
        level = province_development.get(province_id, 0)
        amount = INCOME_PER_PROVINCE + DEVELOPMENT_YIELD_BONUS_PER_LEVEL * level
        faction["resources"][resource] = faction["resources"].get(resource, 0) + amount


def _apply_supply_attrition(
    faction: FactionState, faction_id: str, owned: list[str], sieges: dict[str, dict]
) -> None:
    """Ongoing attrition on a faction's pooled army for maintaining a siege
    far from its own territory — see SUPPLY_FREE_RANGE/SUPPLY_ATTRITION_PER_HEX.
    Uses the *farthest* of this faction's active sieges (one pooled army,
    strained by its most extended commitment, not summed across sieges).
    """
    own_sieges = [pid for pid, siege in sieges.items() if siege["attacker_id"] == faction_id]
    if not own_sieges or not owned:
        return

    farthest = max(min(distance_between(pid, o) for o in owned) for pid in own_sieges)
    if farthest <= SUPPLY_FREE_RANGE:
        return

    fraction = min(1.0, SUPPLY_ATTRITION_PER_HEX * (farthest - SUPPLY_FREE_RANGE))
    faction["units"] = _attrit(faction["units"], fraction)


def _rebellion_roll(seed: int, province_id: str, turn: int) -> float:
    """Deterministic pseudo-random float in [0, 1) for a (seed, province,
    turn) triple — see the module docstring for why this is a hash, not
    `random.random()`."""
    digest = hashlib.sha256(f"{seed}:{province_id}:{turn}".encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


def _check_rebellions(
    faction_id: str,
    owned: list[str],
    province_owner: dict[str, str],
    province_captured_turn: dict[str, int],
    province_development: dict[str, int],
    capital: str | None,
    rebellion_seed: int,
    turn: int,
) -> list[str]:
    """Mutates `province_owner`/`province_captured_turn` in place, reverting
    any province that rebels to unclaimed. Returns the ids that rebelled
    this turn (for the caller's resolution message).

    Only provinces both recently captured by this faction (an entry in
    `province_captured_turn` within REBELLION_GRACE_TURNS of `turn`) and
    far from its capital (beyond REBELLION_DISTANCE_THRESHOLD hexes) are at
    risk — a province with no capture-turn entry has been held since game
    start (or has already survived its grace period) and is exempt. A
    faction with no recorded capital (shouldn't happen — every faction gets
    one in `initial_state_for`) never rebels, defensively. Each level of
    development lowers the effective chance by
    DEVELOPMENT_REBELLION_REDUCTION_PER_LEVEL (floored at 0) — a developed
    province is administered well enough to resist unrest even before its
    grace period ends.
    """
    if capital is None:
        return []
    rebelled = []
    for province_id in owned:
        captured_turn = province_captured_turn.get(province_id)
        if captured_turn is None or turn - captured_turn > REBELLION_GRACE_TURNS:
            continue
        if distance_between(province_id, capital) <= REBELLION_DISTANCE_THRESHOLD:
            continue
        level = province_development.get(province_id, 0)
        chance = max(0.0, REBELLION_CHANCE_PER_TURN - DEVELOPMENT_REBELLION_REDUCTION_PER_LEVEL * level)
        if _rebellion_roll(rebellion_seed, province_id, turn) < chance:
            province_owner.pop(province_id, None)
            province_captured_turn.pop(province_id, None)
            rebelled.append(province_id)
    return rebelled


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
    `sieges`, `province_captured_turn`, and `province_development`
    (LangGraph has no merge reducer for these dict fields, so a node must
    return the complete new value, not a patch — see `agents/state.py`),
    plus `resolution` (a short human-readable string the caller appends to
    the turn's log line).
    """
    factions = dict(state["factions"])
    faction = dict(factions[faction_id])
    faction["resources"] = dict(faction["resources"])
    faction["units"] = dict(faction["units"])

    owned = territory_of(state, faction_id)
    _apply_income(faction, owned, state["province_development"])
    _apply_supply_attrition(faction, faction_id, owned, state["sieges"])

    province_owner = dict(state["province_owner"])
    diplomatic_status = dict(state["diplomatic_status"])
    pending_proposals = dict(state["pending_proposals"])
    province_development = dict(state["province_development"])
    sieges = dict(state["sieges"])
    province_captured_turn = dict(state["province_captured_turn"])
    resolution = ""

    rebelled = _check_rebellions(
        faction_id, owned, province_owner, province_captured_turn, province_development,
        state["capitals"].get(faction_id), state["rebellion_seed"], state["turn"],
    )

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

    elif action.action_type == "develop_province":
        target_province = action.target_province
        if target_province is None or target_province not in owned:
            resolution = f"cannot develop {target_province!r}: not your territory"
        else:
            level = province_development.get(target_province, 0)
            if level >= MAX_PROVINCE_DEVELOPMENT:
                resolution = f"{target_province} is already at maximum development"
            else:
                cost = DEVELOP_BASE_COST * (level + 1)
                if faction["resources"].get("gold", 0) >= cost:
                    faction["resources"]["gold"] -= cost
                    province_development[target_province] = level + 1
                    resolution = f"developed {target_province} to level {level + 1}"
                else:
                    resolution = f"tried to develop {target_province} but lacked {cost} gold"

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
            if owner is None:
                province_captured_turn[target_province] = state["turn"]
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
                development_level = province_development.get(target_province, 0)
                defender_strength *= (
                    1 + TERRAIN_DEFENSE_BONUS.get(terrain, 0.0)
                    + DEVELOPMENT_DEFENSE_BONUS_PER_LEVEL * development_level
                )

                sieges.pop(target_province, None)
                if attacker_strength > defender_strength:
                    province_owner[target_province] = faction_id
                    province_captured_turn[target_province] = state["turn"]
                    faction["units"] = _attrit(faction["units"], COMBAT_WINNER_ATTRITION)
                    defender["units"] = _attrit(defender["units"], COMBAT_LOSER_ATTRITION)
                    resolution = f"broke the siege of {target_province}, captured from {owner}"
                else:
                    faction["units"] = _attrit(faction["units"], COMBAT_LOSER_ATTRITION)
                    defender["units"] = _attrit(defender["units"], COMBAT_WINNER_ATTRITION)
                    resolution = f"lost the siege of {target_province} (held by {owner})"
                factions[owner] = defender

    factions[faction_id] = faction

    if rebelled:
        resolution += f" (meanwhile, {', '.join(rebelled)} rebelled and reverted to unclaimed)"

    return {
        "factions": factions,
        "province_owner": province_owner,
        "diplomatic_status": diplomatic_status,
        "pending_proposals": pending_proposals,
        "sieges": sieges,
        "province_captured_turn": province_captured_turn,
        "province_development": province_development,
        "resolution": resolution,
    }
