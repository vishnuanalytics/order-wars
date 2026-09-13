"""Phase 4: N-faction turn-taking, grounded in the real map.

Builds on Phase 2's structure (one reusable `faction_turn` node) with a
three-layer hierarchy per faction (see CLAUDE.md "Agent & simulation
design"): a strategic leader (`_refresh_intent`, refreshed every few turns)
sets intent, `_dispatch_specialist` picks which of three specialists —
military commander, economic/logistics agent, diplomat/trade agent — acts
on it this turn (one LLM call/turn, not three; a deliberate scope choice,
see CLAUDE.md), and that specialist chooses from real, currently-legal
options — its own territory and adjacent provinces (from
`map_data/provinces.geojson`), the other factions actually in play, and any
diplomatic proposals pending against it — rather than free-floating
`target_faction` strings. The LLM's raw output is still sanitized before use
(an LLM can still name a province that isn't legal this turn); resolution of
a sanitized action is delegated to `game.rules.resolve_action`, which is
pure/LLM-free and independently tested.
"""

import os
import random
from typing import Literal

from dotenv import load_dotenv
from langgraph.graph import END, StateGraph

from agents.actions import DiplomaticAction, EconomicAction, FactionAction, MilitaryAction
from agents.llm import build_llm
from agents.roles import describe, specialist_order
from agents.state import FactionState, GameState
from game.rules import (
    COUNTERS,
    REBELLION_DISTANCE_THRESHOLD,
    REBELLION_GRACE_TURNS,
    SIEGE_TURNS_TO_DECIDE,
    SUPPLY_FREE_RANGE,
    UNIT_COSTS,
    diplomatic_status_between,
    resolve_action,
    territory_of,
)
from map_data.loader import distance_between, get_province, name_of, neighbors_of, sea_neighbors_of

INTENT_REFRESH_INTERVAL = 3


def _other_faction_ids(state: GameState, faction_id: str) -> list[str]:
    return [fid for fid in state["turn_order"] if fid != faction_id]


def _legal_move_targets(state: GameState, faction_id: str) -> list[str]:
    """Own territory (reinforce), every province adjacent to it, and any
    short cross-water lane out of an owned coastal province (see
    `map_data.loader.sea_neighbors_of`) — treated identically to land
    adjacency for now (a sea-lane target still just resolves through the
    same `move_army` action)."""
    owned = territory_of(state, faction_id)
    targets = set(owned)
    for province_id in owned:
        targets.update(neighbors_of(province_id))
        targets.update(sea_neighbors_of(province_id))
    return sorted(targets)


def _unit_options_summary() -> str:
    """Derived from game.rules.UNIT_COSTS/COUNTERS rather than hardcoded, so
    this stays correct if either changes — same convention as move_options
    being derived from real map/state data rather than a fixed string."""
    parts = []
    for unit_type, cost in UNIT_COSTS.items():
        cost_str = ", ".join(f"{amount} {res}" for res, amount in cost.items())
        beats = COUNTERS.get(unit_type)
        parts.append(f"{unit_type} ({cost_str}{f' — beats {beats}' if beats else ''})")
    return "; ".join(parts)


def _siege_summary(state: GameState, faction_id: str) -> str:
    owned = territory_of(state, faction_id)
    own = []
    for pid, siege in state["sieges"].items():
        if siege["attacker_id"] != faction_id:
            continue
        hops = min((distance_between(pid, o) for o in owned), default=0)
        supply_note = (
            f", {hops} hexes from supply — attrition!" if hops > SUPPLY_FREE_RANGE
            else f", {hops} hexes from supply"
        )
        own.append(f"{pid} ({name_of(pid)}, progress {siege['progress']}/{SIEGE_TURNS_TO_DECIDE}{supply_note})")
    return "; ".join(own) if own else "none"


def _diplomacy_summary(state: GameState, faction_id: str) -> str:
    lines = []
    for other_id in _other_faction_ids(state, faction_id):
        status = diplomatic_status_between(state, faction_id, other_id)
        other_name = state["factions"][other_id]["name"]
        line = f"{other_name} ({other_id}): {status}"
        incoming = state["pending_proposals"].get(f"{other_id}->{faction_id}")
        if incoming:
            line += f" — they have proposed a {incoming}"
        lines.append(line)
    return "; ".join(lines) if lines else "no other factions"


def _refresh_intent(faction: FactionState, other_names: list[str]) -> str:
    # Groq's gpt-oss models spend some of max_tokens on hidden reasoning
    # before the visible answer (see agents/llm.py), so give more headroom
    # than the one-sentence answer alone would need.
    llm = build_llm(max_tokens=300)
    prompt = (
        f"You lead the faction '{faction['name']}' in a strategy game. "
        f"{describe(faction['role_preset'])}\n"
        f"Other factions in play: {', '.join(other_names) or 'none'}.\n"
        "In one short sentence, state your strategic intent for the next "
        "few turns."
    )
    response = llm.invoke(prompt)
    return response.content if isinstance(response.content, str) else str(response.content)


def _dispatch_specialist(state: GameState, faction_id: str) -> str:
    """Which specialist — "military", "economic", or "diplomatic" — decides
    this faction's action this turn. Pure and LLM-free (a rule-based
    dispatcher, not a fourth LLM call — see CLAUDE.md "Agent & simulation
    design" for why one dispatched decision/turn, not three parallel ones).

    Real state signals override the role-preset-ordered rotation fallback,
    so pressing sieges and answering proposals aren't left to chance:
    1. An active siege this faction is pressing must be pressed again every
       one of its own turns or it lapses (game.rules.SIEGE_TURNS_TO_DECIDE)
       — always route to military to protect that investment.
    2. An incoming pending proposal deserves a timely response, not one
       that depends on the rotation happening to land on diplomatic.
    3. Otherwise, rotate through this faction's role-preset-ordered
       priorities (agents.roles.specialist_order) by round number — a full
       permutation of all three domains, so no faction is ever permanently
       locked out of expansion/economy/diplomacy.
    """
    if any(siege["attacker_id"] == faction_id for siege in state["sieges"].values()):
        return "military"
    if any(key.endswith(f"->{faction_id}") for key in state["pending_proposals"]):
        return "diplomatic"

    role_preset = state["factions"][faction_id]["role_preset"]
    order = specialist_order(role_preset)
    round_number = state["turn"] + 1
    return order[(round_number - 1) % len(order)]


def _specialist_preamble(faction: FactionState, faction_id: str) -> str:
    return (
        f"You lead the faction '{faction['name']}' ({faction_id}) in a "
        f"strategy game. {describe(faction['role_preset'])}\n"
        f"Your current strategic intent: {faction['intent']}\n"
    )


def _military_prompt(state: GameState, faction_id: str, move_targets: list[str]) -> str:
    faction = state["factions"][faction_id]
    owned = territory_of(state, faction_id)
    move_options = ", ".join(
        f"{pid} ({name_of(pid)}, {get_province(pid).terrain})" for pid in move_targets
    ) or "none"
    return (
        _specialist_preamble(faction, faction_id)
        + "You are the military commander: choose this turn's troop "
        "movement, executing the leader's intent tactically.\n"
        f"Your territory ({len(owned)} provinces): "
        f"{', '.join(name_of(p) for p in owned) or 'none'}\n"
        f"Your units: {faction['units']}.\n"
        f"Provinces you may move_army into this turn (own or adjacent, with "
        f"terrain): {move_options}\n"
        "Attacking enemy territory is a siege, not instant combat: "
        f"move_army into the same enemy province {SIEGE_TURNS_TO_DECIDE} of "
        "your own turns in a row to force the decisive battle. Attacking "
        "anywhere else in between abandons the siege with no losses. A "
        f"siege more than {SUPPLY_FREE_RANGE} hexes from your own territory "
        "costs your army ongoing supply-line attrition every turn you "
        "maintain it — the further, the worse. A province you conquer more "
        f"than {REBELLION_DISTANCE_THRESHOLD} hexes from your capital risks "
        f"rebelling back to unclaimed for its first {REBELLION_GRACE_TURNS} "
        "turns under your rule — territory close to home settles safely.\n"
        f"Sieges you're actively pressing (press the same target again to "
        f"continue it): {_siege_summary(state, faction_id)}\n"
        "Choose this turn's action. For move_army, target_province must be "
        "one of the listed province ids."
    )


def _economic_prompt(state: GameState, faction_id: str) -> str:
    faction = state["factions"][faction_id]
    return (
        _specialist_preamble(faction, faction_id)
        + "You are the economic/logistics agent: manage resources and "
        "production, executing the leader's intent tactically.\n"
        f"Your resources: {faction['resources']}. Your units: {faction['units']}.\n"
        "Each province you hold yields a resource every turn based on its "
        "terrain: coastal -> gold, plains -> grain, hills -> iron.\n"
        f"To build_unit, choose unit_type: {_unit_options_summary()}. "
        "Defaults to legion if unset.\n"
        "Choose this turn's action."
    )


def _diplomatic_prompt(state: GameState, faction_id: str) -> str:
    faction = state["factions"][faction_id]
    other_ids = _other_faction_ids(state, faction_id)
    return (
        _specialist_preamble(faction, faction_id)
        + "You are the diplomat/trade agent: manage external relations, "
        "executing the leader's intent tactically.\n"
        f"Other factions and your relations with them: {_diplomacy_summary(state, faction_id)}\n"
        "Choose this turn's action. For negotiate/declare_war, "
        f"target_faction must be one of: {', '.join(other_ids) or 'none'}."
    )


# Structured output goes out as a tool call, whose JSON args get cut off
# mid-generation if hidden reasoning eats too much of a small budget
# (confirmed live against Groq: a 200-token budget truncated the tool call
# and failed to parse) — same cause as the note in _refresh_intent. 600 was
# enough for Groq but not for the OpenRouter fallback model
# (nvidia/nemotron-3-super-120b-a12b:free spent 135 tokens on hidden
# reasoning and still ran out mid-schema at 600 — confirmed live via
# openai.LengthFinishReasonError); 900 has margin for both. Each specialist
# schema below has fewer fields than the old monolithic FactionAction did,
# so 900 stays a safe (if generous) budget for all three — not re-tuned
# down without live data confirming it's safe to.
_ACTION_MAX_TOKENS = 900

_SPECIALIST_SCHEMAS = {
    "military": MilitaryAction,
    "economic": EconomicAction,
    "diplomatic": DiplomaticAction,
}


def _decide_action(
    state: GameState, faction_id: str, move_targets: list[str]
) -> tuple[FactionAction, str]:
    """Returns (action, domain) — the caller (faction_turn) records domain
    on last_event purely for observability (which specialist decided this
    turn), not because resolve_action/_sanitize_action need it."""
    domain = _dispatch_specialist(state, faction_id)
    llm = build_llm(max_tokens=_ACTION_MAX_TOKENS, schema=_SPECIALIST_SCHEMAS[domain])

    if domain == "military":
        prompt = _military_prompt(state, faction_id, move_targets)
    elif domain == "economic":
        prompt = _economic_prompt(state, faction_id)
    else:
        prompt = _diplomatic_prompt(state, faction_id)

    result = llm.invoke(prompt)
    # Each specialist schema's fields are always a subset of FactionAction's,
    # with FactionAction's own defaults covering the rest — _sanitize_action
    # and game.rules.resolve_action only ever see this common type.
    return FactionAction(**result.model_dump()), domain


def _sanitize_action(
    state: GameState, faction_id: str, action: FactionAction, move_targets: list[str]
) -> FactionAction:
    """Repair or downgrade-to-hold an LLM action that isn't actually legal.

    LLMs occasionally invent a province id or target a nonexistent faction
    despite the prompt listing valid options — this is the sanitization
    boundary `game.rules.resolve_action` relies on not having to re-check.
    """
    other_ids = set(_other_faction_ids(state, faction_id))

    if action.action_type == "move_army" and action.target_province not in move_targets:
        return FactionAction(
            action_type="hold",
            rationale=f"invalid move target {action.target_province!r} sanitized to hold",
        )
    if action.action_type in ("negotiate", "declare_war") and action.target_faction not in other_ids:
        return FactionAction(
            action_type="hold",
            rationale=f"invalid target faction {action.target_faction!r} sanitized to hold",
        )
    if action.action_type == "negotiate" and action.proposal is None:
        return FactionAction(
            action_type="hold",
            rationale="negotiate without a proposal type sanitized to hold",
        )
    return action


def faction_turn(state: GameState) -> dict:
    """Run one faction's turn: refresh intent if due, decide, then resolve."""
    idx = state["active_faction_idx"]
    faction_id = state["turn_order"][idx]
    faction: FactionState = dict(state["factions"][faction_id])
    other_names = [state["factions"][fid]["name"] for fid in _other_faction_ids(state, faction_id)]
    round_number = state["turn"] + 1

    if faction["intent"] is None or (round_number - 1) % INTENT_REFRESH_INTERVAL == 0:
        faction["intent"] = _refresh_intent(faction, other_names)
    state = {**state, "factions": {**state["factions"], faction_id: faction}}

    move_targets = _legal_move_targets(state, faction_id)
    action, specialist = _decide_action(state, faction_id, move_targets)
    action = _sanitize_action(state, faction_id, action, move_targets)

    resolved = resolve_action(state, faction_id, action)
    factions = resolved["factions"]
    faction = dict(factions[faction_id])
    faction["last_action"] = action.model_dump()
    factions[faction_id] = faction

    next_idx = (idx + 1) % len(state["turn_order"])
    next_turn = state["turn"] + 1 if next_idx == 0 else state["turn"]

    log_line = (
        f"Turn {round_number} — {faction['name']} [{specialist}] ({action.action_type}): "
        f"{resolved['resolution']} — {action.rationale}"
    )
    last_event = {
        "turn": round_number,
        "faction_id": faction_id,
        "specialist": specialist,
        **action.model_dump(),
        "resolution": resolved["resolution"],
    }

    return {
        "factions": factions,
        "province_owner": resolved["province_owner"],
        "diplomatic_status": resolved["diplomatic_status"],
        "pending_proposals": resolved["pending_proposals"],
        "sieges": resolved["sieges"],
        "province_captured_turn": resolved["province_captured_turn"],
        "active_faction_idx": next_idx,
        "turn": next_turn,
        "last_event": last_event,
        "log": [log_line],
    }


def route_after_turn(state: GameState) -> Literal["faction_turn", "__end__"]:
    """Loop back for another faction's turn while rounds remain, else end."""
    if state["turn"] < state["max_turns"]:
        return "faction_turn"
    return END


def build_graph():
    builder = StateGraph(GameState)
    builder.add_node("faction_turn", faction_turn)

    builder.set_entry_point("faction_turn")
    builder.add_conditional_edges(
        "faction_turn",
        route_after_turn,
        {"faction_turn": "faction_turn", END: END},
    )

    return builder.compile()


STARTING_RESOURCES = {"gold": 20, "grain": 20, "iron": 10}
STARTING_UNITS = {"legion": 2}


def require_llm_configured() -> None:
    load_dotenv()
    if not any(
        os.environ.get(key)
        for key in ("GROQ_API_KEY", "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY")
    ):
        raise RuntimeError(
            "No LLM provider key is set. Copy .env.example to .env and fill in "
            "at least one of GROQ_API_KEY, OPENROUTER_API_KEY, or ANTHROPIC_API_KEY."
        )


def initial_state_for(
    faction_configs: list[dict], max_turns: int, rebellion_seed: int | None = None
) -> GameState:
    """Build the starting `GameState` for a game. Each entry in
    `faction_configs` is a dict with `faction_id`, `name`, `role_preset`, and
    `home_province` (a real province id from `map_data/provinces.geojson` —
    the faction's sole starting territory, and permanently its `capitals`
    entry — see `game.rules`'s rebellion docs for why a capital is a fixed
    geographic anchor, not wherever a faction currently holds). Optional
    `resources`/`units` override `STARTING_RESOURCES`/`STARTING_UNITS` per
    faction — this is what makes a scenario's customized starting
    resources/units (see `db.models.ScenarioFaction`) actually affect the
    simulation, not just get recorded in the persisted config and then
    silently ignored.

    `rebellion_seed` defaults to a real random draw (different each game,
    for unpredictability) but can be pinned for reproducible tests/replays —
    everything downstream of it (`game.rules._rebellion_roll`) is a
    deterministic function of this one value plus (province, turn).

    Shared by `run()` (below, single blocking `.invoke()`) and
    `game/run_game.py` (which needs the same state but drives the graph via
    `.stream()` instead, to persist and check win conditions turn-by-turn).
    """
    factions: dict[str, FactionState] = {
        cfg["faction_id"]: {
            "faction_id": cfg["faction_id"],
            "name": cfg["name"],
            "role_preset": cfg["role_preset"],
            "intent": None,
            "last_action": None,
            "resources": dict(cfg.get("resources") or STARTING_RESOURCES),
            "units": dict(cfg.get("units") or STARTING_UNITS),
        }
        for cfg in faction_configs
    }
    province_owner = {cfg["home_province"]: cfg["faction_id"] for cfg in faction_configs}
    capitals = {cfg["faction_id"]: cfg["home_province"] for cfg in faction_configs}

    return {
        "turn": 0,
        "max_turns": max_turns,
        "turn_order": [cfg["faction_id"] for cfg in faction_configs],
        "active_faction_idx": 0,
        "factions": factions,
        "province_owner": province_owner,
        "diplomatic_status": {},
        "pending_proposals": {},
        "sieges": {},
        "capitals": capitals,
        "province_captured_turn": {},
        "rebellion_seed": (
            rebellion_seed if rebellion_seed is not None else random.SystemRandom().getrandbits(32)
        ),
        "last_event": None,
        "log": [],
    }


def run(faction_configs: list[dict], max_turns: int = 3) -> GameState:
    """Run a game to completion in one blocking call — for the CLI demo and
    tests. `game/run_game.py` is the persistence-aware, turn-by-turn entrypoint.
    """
    require_llm_configured()
    initial_state = initial_state_for(faction_configs, max_turns)
    graph = build_graph()
    # Default recursion_limit (25) is too low once turns * factions grows.
    config = {"recursion_limit": max_turns * len(faction_configs) + 10}
    return graph.invoke(initial_state, config)


# Rome/Carthage/Gaul, matching the theater map_data/generate_map.py tiled.
# Shared by the CLI demo below and game/run_game.py's default scenario.
DEMO_FACTIONS = [
    {
        "faction_id": "rome", "name": "Rome", "role_preset": "expansionist",
        "home_province": "831e80fffffffff",  # Italy 20
    },
    {
        "faction_id": "carthage", "name": "Carthage", "role_preset": "warmonger",
        "home_province": "83386efffffffff",  # Tunisia 2
    },
    {
        "faction_id": "gaul", "name": "Gaul", "role_preset": "isolationist",
        "home_province": "833968fffffffff",  # France 31
    },
]


if __name__ == "__main__":
    result = run(DEMO_FACTIONS, max_turns=3)
    for line in result["log"]:
        print(line)
