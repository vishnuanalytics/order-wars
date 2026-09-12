"""Phase 4: N-faction turn-taking, grounded in the real map.

Builds on Phase 2's structure (one reusable `faction_turn` node, a two-layer
intent/executor hierarchy) but the executor now chooses from real,
currently-legal options — its own territory and adjacent provinces (from
`map_data/provinces.geojson`), the other factions actually in play, and any
diplomatic proposals pending against it — rather than free-floating
`target_faction` strings. The LLM's raw output is still sanitized before use
(an LLM can still name a province that isn't legal this turn); resolution of
a sanitized action is delegated to `game.rules.resolve_action`, which is
pure/LLM-free and independently tested.
"""

import os
from typing import Literal

from dotenv import load_dotenv
from langgraph.graph import END, StateGraph

from agents.actions import FactionAction
from agents.llm import build_llm
from agents.roles import describe
from agents.state import FactionState, GameState
from game.rules import diplomatic_status_between, resolve_action, territory_of
from map_data.loader import name_of, neighbors_of

INTENT_REFRESH_INTERVAL = 3


def _other_faction_ids(state: GameState, faction_id: str) -> list[str]:
    return [fid for fid in state["turn_order"] if fid != faction_id]


def _legal_move_targets(state: GameState, faction_id: str) -> list[str]:
    """Own territory (reinforce) plus every province adjacent to it."""
    owned = territory_of(state, faction_id)
    targets = set(owned)
    for province_id in owned:
        targets.update(neighbors_of(province_id))
    return sorted(targets)


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


def _decide_action(
    state: GameState, faction_id: str, move_targets: list[str]
) -> FactionAction:
    faction = state["factions"][faction_id]
    owned = territory_of(state, faction_id)
    move_options = ", ".join(f"{pid} ({name_of(pid)})" for pid in move_targets) or "none"

    # Structured output goes out as a tool call, whose JSON args get cut off
    # mid-generation if hidden reasoning eats too much of a small budget
    # (confirmed live against Groq: a 200-token budget truncated the tool
    # call and failed to parse) — same cause as the note in _refresh_intent.
    llm = build_llm(max_tokens=600, schema=FactionAction)
    prompt = (
        f"You lead the faction '{faction['name']}' ({faction_id}) in a "
        f"strategy game. {describe(faction['role_preset'])}\n"
        f"Your current strategic intent: {faction['intent']}\n"
        f"Your territory ({len(owned)} provinces): "
        f"{', '.join(name_of(p) for p in owned) or 'none'}\n"
        f"Your resources: {faction['resources']}. Your units: {faction['units']}.\n"
        f"Provinces you may move_army into this turn (own or adjacent): {move_options}\n"
        f"Other factions and your relations with them: {_diplomacy_summary(state, faction_id)}\n"
        "Choose this turn's action. For move_army, target_province must be "
        "one of the listed province ids. For negotiate/declare_war, "
        "target_faction must be one of the other factions' ids listed above."
    )
    return llm.invoke(prompt)


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
    action = _decide_action(state, faction_id, move_targets)
    action = _sanitize_action(state, faction_id, action, move_targets)

    resolved = resolve_action(state, faction_id, action)
    factions = resolved["factions"]
    faction = dict(factions[faction_id])
    faction["last_action"] = action.model_dump()
    factions[faction_id] = faction

    next_idx = (idx + 1) % len(state["turn_order"])
    next_turn = state["turn"] + 1 if next_idx == 0 else state["turn"]

    log_line = (
        f"Turn {round_number} — {faction['name']} ({action.action_type}): "
        f"{resolved['resolution']} — {action.rationale}"
    )

    return {
        "factions": factions,
        "province_owner": resolved["province_owner"],
        "diplomatic_status": resolved["diplomatic_status"],
        "pending_proposals": resolved["pending_proposals"],
        "active_faction_idx": next_idx,
        "turn": next_turn,
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


def run(faction_configs: list[dict], max_turns: int = 3) -> GameState:
    """Run a game. Each entry in `faction_configs` is a dict with
    `faction_id`, `name`, `role_preset`, and `home_province` (a real province
    id from `map_data/provinces.geojson` — the faction's sole starting
    territory).
    """
    load_dotenv()
    if not any(
        os.environ.get(key)
        for key in ("GROQ_API_KEY", "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY")
    ):
        raise RuntimeError(
            "No LLM provider key is set. Copy .env.example to .env and fill in "
            "at least one of GROQ_API_KEY, OPENROUTER_API_KEY, or ANTHROPIC_API_KEY."
        )

    factions: dict[str, FactionState] = {
        cfg["faction_id"]: {
            "faction_id": cfg["faction_id"],
            "name": cfg["name"],
            "role_preset": cfg["role_preset"],
            "intent": None,
            "last_action": None,
            "resources": {"gold": 20},
            "units": {"legion": 2},
        }
        for cfg in faction_configs
    }
    province_owner = {cfg["home_province"]: cfg["faction_id"] for cfg in faction_configs}

    initial_state: GameState = {
        "turn": 0,
        "max_turns": max_turns,
        "turn_order": [cfg["faction_id"] for cfg in faction_configs],
        "active_faction_idx": 0,
        "factions": factions,
        "province_owner": province_owner,
        "diplomatic_status": {},
        "pending_proposals": {},
        "log": [],
    }

    graph = build_graph()
    # Default recursion_limit (25) is too low once turns * factions grows.
    config = {"recursion_limit": max_turns * len(faction_configs) + 10}
    return graph.invoke(initial_state, config)


if __name__ == "__main__":
    demo_factions = [
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
    result = run(demo_factions, max_turns=3)
    for line in result["log"]:
        print(line)
