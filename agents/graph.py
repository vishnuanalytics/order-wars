"""Phase 2: N-faction turn-taking with a two-layer planner/executor hierarchy.

One reusable node (`faction_turn`) handles whichever faction is up next,
looping via `turn_order`/`active_faction_idx` in `GameState` rather than a
fixed pair of nodes — this is what lets the same graph run 2 factions or 20.

Each faction's turn has two layers (see CLAUDE.md "Agent & simulation
design"):
  - Strategic leader: sets/refreshes a short `intent` every
    `INTENT_REFRESH_INTERVAL` turns, not every turn.
  - Executor: chooses this turn's `FactionAction` (schema-validated, not free
    text) within that intent.

Full role specialization (separate military/diplomat/economic agents) isn't
built yet — there's no territory, resources, or diplomatic state for them to
act on distinctly until Phase 4, so splitting now would be hollow. This two-
layer intent/executor split is the real hierarchy Phase 2 delivers; Phase 4/5
add the state that makes further specialization meaningful.
"""

import os
from typing import Literal

from dotenv import load_dotenv
from langgraph.graph import END, StateGraph

from agents.actions import FactionAction
from agents.llm import build_llm
from agents.roles import describe
from agents.state import FactionState, GameState

INTENT_REFRESH_INTERVAL = 3


def _other_faction_names(state: GameState, faction_id: str) -> list[str]:
    return [f["name"] for fid, f in state["factions"].items() if fid != faction_id]


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


def _decide_action(faction: FactionState, other_names: list[str]) -> FactionAction:
    # Structured output goes out as a tool call, whose JSON args get cut off
    # mid-generation if hidden reasoning eats too much of a small budget
    # (confirmed live against Groq: a 200-token budget truncated the tool
    # call and failed to parse) — same cause as the note in _refresh_intent.
    llm = build_llm(max_tokens=600, schema=FactionAction)
    prompt = (
        f"You lead the faction '{faction['name']}' in a strategy game. "
        f"{describe(faction['role_preset'])}\n"
        f"Your current strategic intent: {faction['intent']}\n"
        f"Other factions: {', '.join(other_names) or 'none'}.\n"
        "Choose this turn's action."
    )
    return llm.invoke(prompt)


def faction_turn(state: GameState) -> dict:
    """Run one faction's turn: refresh intent if due, then decide an action."""
    idx = state["active_faction_idx"]
    faction_id = state["turn_order"][idx]
    faction: FactionState = dict(state["factions"][faction_id])
    other_names = _other_faction_names(state, faction_id)
    round_number = state["turn"] + 1

    if faction["intent"] is None or (round_number - 1) % INTENT_REFRESH_INTERVAL == 0:
        faction["intent"] = _refresh_intent(faction, other_names)

    action = _decide_action(faction, other_names)
    faction["last_action"] = action.model_dump()

    factions = dict(state["factions"])
    factions[faction_id] = faction

    next_idx = (idx + 1) % len(state["turn_order"])
    next_turn = state["turn"] + 1 if next_idx == 0 else state["turn"]

    target = f" -> {action.target_faction}" if action.target_faction else ""
    log_line = (
        f"Turn {round_number} — {faction['name']}: {action.action_type}{target} "
        f"({action.rationale})"
    )

    return {
        "factions": factions,
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
    """Run a game. `faction_configs` is a list of
    `{"faction_id": ..., "name": ..., "role_preset": ...}` dicts, in turn order.
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
        }
        for cfg in faction_configs
    }
    initial_state: GameState = {
        "turn": 0,
        "max_turns": max_turns,
        "turn_order": [cfg["faction_id"] for cfg in faction_configs],
        "active_faction_idx": 0,
        "factions": factions,
        "log": [],
    }

    graph = build_graph()
    # Default recursion_limit (25) is too low once turns * factions grows.
    config = {"recursion_limit": max_turns * len(faction_configs) + 10}
    return graph.invoke(initial_state, config)


if __name__ == "__main__":
    demo_factions = [
        {"faction_id": "rome", "name": "Rome", "role_preset": "expansionist"},
        {"faction_id": "carthage", "name": "Carthage", "role_preset": "warmonger"},
        {"faction_id": "gaul", "name": "Gaul", "role_preset": "isolationist"},
    ]
    result = run(demo_factions, max_turns=2)
    for line in result["log"]:
        print(line)
