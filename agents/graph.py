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
import threading
from collections.abc import Callable
from typing import Literal

from dotenv import load_dotenv
from langgraph.graph import END, StateGraph

from agents.actions import DiplomaticAction, EconomicAction, FactionAction, MilitaryAction
from agents.llm import build_llm
from agents.roles import describe, specialist_order
from agents.state import FactionState, GameState
from game.rules import (
    COUNTERS,
    DEVELOP_BASE_COST,
    MAX_PROVINCE_DEVELOPMENT,
    REBELLION_DISTANCE_THRESHOLD,
    REBELLION_GRACE_TURNS,
    SIEGE_TURNS_TO_DECIDE,
    SUPPLY_FREE_RANGE,
    UNIT_COSTS,
    check_call_to_arms,
    diplomatic_status_between,
    resolve_action,
    territory_of,
)
from map_data.loader import distance_between, get_province, name_of, neighbors_of, sea_neighbors_of

INTENT_REFRESH_INTERVAL = 3

# A human take-over hook (`backend/`'s live-play feature): given a faction
# id, returns a human-submitted FactionAction if that faction is currently
# human-controlled and one arrived in time, or None to let the AI decide as
# usual (not human-controlled, or the human timed out). Set per-thread, not
# globally — `backend/game_hub.py`'s GameHub already runs every game's
# `play_game()` call on its own dedicated background thread, so a plain
# `threading.local()` isolates one game's provider from another's without
# threading a new parameter through every LangGraph node or coupling this
# module to `backend/` (agents/ stays usable standalone, e.g. `python -m
# agents.graph`, where no provider is ever set and this is always a no-op).
HumanActionProvider = Callable[[str], FactionAction | None]
_human_action_context = threading.local()


def set_human_action_provider(provider: HumanActionProvider | None) -> None:
    _human_action_context.provider = provider


def _get_human_action_provider() -> HumanActionProvider | None:
    return getattr(_human_action_context, "provider", None)


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


def _describe_proposal(raw: str) -> str:
    """A stored proposal is usually just the ProposalType string itself,
    but trade/tribute offers are encoded as "trade:{resource}:{amount}" /
    "tribute:{resource}:{amount}:{ceded_province}" (see game.rules's trade/
    tribute docs) — decode those into readable text."""
    if raw.startswith("trade:"):
        _, resource, amount = raw.split(":", 2)
        return f"trade offering {amount} {resource}/turn"
    if raw.startswith("tribute:"):
        _, resource, amount, ceded = raw.split(":", 3)
        cede_note = f" and {name_of(ceded)}" if ceded != "none" else ""
        return f"tribute offering {amount} {resource}{cede_note} to end the war"
    return raw


def _diplomacy_summary(state: GameState, faction_id: str) -> str:
    lines = []
    for other_id in _other_faction_ids(state, faction_id):
        status = diplomatic_status_between(state, faction_id, other_id)
        other_name = state["factions"][other_id]["name"]
        line = f"{other_name} ({other_id}): {status}"
        incoming = state["pending_proposals"].get(f"{other_id}->{faction_id}")
        if incoming:
            line += f" — they have proposed a {_describe_proposal(incoming)}"
        lines.append(line)
    return "; ".join(lines) if lines else "no other factions"


def _trade_summary(state: GameState, faction_id: str) -> str:
    active = []
    for pair, terms in state["trade_agreements"].items():
        if faction_id not in terms:
            continue
        other_id = next(fid for fid in terms if fid != faction_id)
        give = terms[faction_id]
        get = terms[other_id]
        other_name = state["factions"][other_id]["name"]
        active.append(
            f"with {other_name}: give {give['amount']} {give['resource']}/turn, "
            f"receive {get['amount']} {get['resource']}/turn"
        )
    return "; ".join(active) if active else "none"


def _refresh_intent(state: GameState, faction_id: str) -> str:
    """Rule-based, no LLM call — a real cost cut, not just a simplification:
    this used to be one LLM call every INTENT_REFRESH_INTERVAL (3) turns,
    roughly a quarter of a game's total agent LLM calls (the other three-
    quarters being the per-turn action decision, which still needs a real
    judgment call and stays an LLM call). Safe to cut because `intent` is
    read-only flavor/context in the specialist prompts below ("Your
    current strategic intent: ...") — never parsed, validated, or acted on
    programmatically the way an actual action is, so a role-preset-driven
    heuristic serves the same purpose. Takes `state`/`faction_id` (not
    just the faction dict) specifically so it can react to real signals —
    an active war changes what "expand into unclaimed territory" even
    means — not just restate the role preset's static doctrine every time.
    """
    faction = state["factions"][faction_id]
    role = faction["role_preset"]
    at_war = [
        fid for fid in _other_faction_ids(state, faction_id)
        if diplomatic_status_between(state, faction_id, fid) == "war"
    ]

    if at_war:
        enemies = ", ".join(state["factions"][fid]["name"] for fid in at_war)
        if role == "warmonger":
            return f"Press the war against {enemies} — press the advantage while it lasts."
        if role == "isolationist":
            return f"Hold defensive ground against {enemies} without chasing further conflict."
        if role == "diplomat_trader":
            return f"Seek terms to end the war with {enemies} as soon as they're favorable."
        return f"Manage the war with {enemies} without losing sight of longer-term goals."

    territory = len(territory_of(state, faction_id))
    if role == "expansionist":
        return (
            "Expand into unclaimed territory and grow the economy."
            if territory < 4
            else "Consolidate recent gains, then keep expanding where it's safe."
        )
    if role == "warmonger":
        return "Look for a weaker neighbor worth raiding or conquering."
    if role == "diplomat_trader":
        return "Pursue trade agreements and alliances with nearby factions."
    if role == "isolationist":
        return "Fortify home territory and avoid entanglements with neighbors."
    return "Act on your own judgment, adapting to the situation as it develops."


def _dispatch_specialist(state: GameState, faction_id: str) -> str:
    """Which specialist — "military", "economic", or "diplomatic" — decides
    this faction's action this turn. Pure and LLM-free (a rule-based
    dispatcher, not a fourth LLM call — see CLAUDE.md "Agent & simulation
    design" for why one dispatched decision/turn, not three parallel ones).

    Real state signals override the role-preset-ordered rotation fallback,
    so pressing sieges, answering proposals, and an ally's call to arms
    aren't left to chance:
    1. An active siege this faction is pressing must be pressed again every
       one of its own turns or it lapses (game.rules.SIEGE_TURNS_TO_DECIDE)
       — always route to military to protect that investment.
    2. An incoming pending proposal deserves a timely response, not one
       that depends on the rotation happening to land on diplomatic.
    3. An ally outmatched in a war (game.rules.check_call_to_arms) deserves
       a timely diplomatic response too — join the war, send tribute, or
       otherwise react — rather than waiting for the rotation.
    4. Otherwise, rotate through this faction's role-preset-ordered
       priorities (agents.roles.specialist_order) by round number — a full
       permutation of all three domains, so no faction is ever permanently
       locked out of expansion/economy/diplomacy.
    """
    if any(siege["attacker_id"] == faction_id for siege in state["sieges"].values()):
        return "military"
    if any(key.endswith(f"->{faction_id}") for key in state["pending_proposals"]):
        return "diplomatic"
    if check_call_to_arms(state, faction_id):
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


def _development_summary(state: GameState, faction_id: str) -> str:
    owned = territory_of(state, faction_id)
    parts = []
    for pid in owned:
        level = state["province_development"].get(pid, 0)
        if level >= MAX_PROVINCE_DEVELOPMENT:
            parts.append(f"{pid} ({name_of(pid)}, level {level}/{MAX_PROVINCE_DEVELOPMENT}, maxed)")
        else:
            cost = DEVELOP_BASE_COST * (level + 1)
            parts.append(f"{pid} ({name_of(pid)}, level {level}/{MAX_PROVINCE_DEVELOPMENT}, {cost} gold to raise)")
    return "; ".join(parts) if parts else "none"


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
        "To develop_province, choose target_province from your own "
        "territory below — each level raises that province's income and "
        "defense, and lowers its rebellion risk, permanently.\n"
        f"Your territory and development levels: {_development_summary(state, faction_id)}\n"
        "Choose this turn's action."
    )


def _call_to_arms_note(state: GameState, faction_id: str) -> str:
    notes = check_call_to_arms(state, faction_id)
    if not notes:
        return ""
    return (
        "An ally needs help: " + "; ".join(notes) + ". Consider joining the "
        "war (declare_war), sending aid via trade, or another response.\n"
    )


def _diplomatic_prompt(state: GameState, faction_id: str) -> str:
    faction = state["factions"][faction_id]
    other_ids = _other_faction_ids(state, faction_id)
    return (
        _specialist_preamble(faction, faction_id)
        + "You are the diplomat/trade agent: manage external relations, "
        "executing the leader's intent tactically.\n"
        f"Your resources: {faction['resources']}.\n"
        f"Your territory: {', '.join(territory_of(state, faction_id)) or 'none'}\n"
        f"Other factions and your relations with them: {_diplomacy_summary(state, faction_id)}\n"
        "To negotiate a recurring trade, propose (or accept an outstanding "
        "offer from) target_faction with proposal='trade', offer_resource, "
        "and offer_amount to give per turn — the trade activates once you "
        "both have an outstanding trade offer to each other, even if the "
        "amounts/resources differ.\n"
        f"Your active trade agreements: {_trade_summary(state, faction_id)}\n"
        "If losing a war, negotiate proposal='tribute' with target_faction, "
        "offer_resource, and offer_amount (a one-time payment, optionally "
        "also target_province from your own territory to cede) to sue for "
        "peace. They accept by negotiating proposal='tribute' back to you "
        "with no offer details of their own — this cashes in your offer "
        "immediately and declares a truce.\n"
        f"{_call_to_arms_note(state, faction_id)}"
        "Choose this turn's action. For negotiate/declare_war, "
        f"target_faction must be one of: {', '.join(other_ids) or 'none'}. "
        "If ceding territory as part of tribute, target_province must be "
        "one of your own territory ids above."
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
    if action.action_type == "negotiate" and action.proposal == "trade" and (
        not action.offer_resource or not action.offer_amount or action.offer_amount <= 0
    ):
        return FactionAction(
            action_type="hold",
            rationale="trade proposal missing a valid offer_resource/offer_amount sanitized to hold",
        )
    if action.action_type == "negotiate" and action.proposal == "tribute":
        incoming = state["pending_proposals"].get(f"{action.target_faction}->{faction_id}")
        is_accepting = isinstance(incoming, str) and incoming.startswith("tribute:")
        if not is_accepting and (
            not action.offer_resource or not action.offer_amount or action.offer_amount <= 0
        ):
            return FactionAction(
                action_type="hold",
                rationale="tribute proposal missing a valid offer_resource/offer_amount sanitized to hold",
            )
    return action


def faction_turn(state: GameState) -> dict:
    """Run one faction's turn: refresh intent if due, decide, then resolve."""
    idx = state["active_faction_idx"]
    faction_id = state["turn_order"][idx]
    faction: FactionState = dict(state["factions"][faction_id])
    round_number = state["turn"] + 1

    if faction["intent"] is None or (round_number - 1) % INTENT_REFRESH_INTERVAL == 0:
        faction["intent"] = _refresh_intent(state, faction_id)
    state = {**state, "factions": {**state["factions"], faction_id: faction}}

    move_targets = _legal_move_targets(state, faction_id)
    provider = _get_human_action_provider()
    human_action = provider(faction_id) if provider else None
    if human_action is not None:
        action, specialist = human_action, "human"
    else:
        action, specialist = _decide_action(state, faction_id, move_targets)
    # Same sanitize/resolve path either way — a human can't submit anything
    # the AI couldn't also have (illegally) attempted, and isn't restricted
    # to whichever specialist the dispatcher would have picked this turn.
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
        "province_development": resolved["province_development"],
        "trade_agreements": resolved["trade_agreements"],
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
    `faction_configs` is a dict with `faction_id`, `name`, `role_preset`,
    `home_province` (a real province id — permanently the faction's
    `capitals` entry, a fixed geographic anchor regardless of what it
    later owns; see `game.rules`'s rebellion docs for why), and optional
    `starting_territory` (a list of real province ids the faction begins
    owning — `home_province` itself is always included even if the caller
    left it out of the list). Factions with only `home_province` and no
    `starting_territory` still start owning just that one province,
    unchanged from before territory support existed. Optional
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
    province_owner: dict[str, str] = {}
    for cfg in faction_configs:
        territory = set(cfg.get("starting_territory") or ()) | {cfg["home_province"]}
        for province_id in territory:
            province_owner[province_id] = cfg["faction_id"]
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
        "province_development": {},
        "trade_agreements": {},
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
