"""Phase 1: a minimal but complete LangGraph agent loop.

Two nodes take turns:
  - `start_turn`  : pure bookkeeping, increments the turn counter.
  - `agent_decide`: calls Claude for a one-line "decision" and logs it.

A conditional edge (`route_after_decision`) loops back to `start_turn` while
turns remain, otherwise ends the graph — this is the "routing" building
block later phases reuse for real game-loop control flow.
"""

import os
from typing import Literal

from dotenv import load_dotenv
from langgraph.graph import END, StateGraph

from agents.llm import build_llm
from agents.state import GameState


def start_turn(state: GameState) -> dict:
    """Advance the turn counter and log it. No LLM call — pure state update."""
    turn = state["turn"] + 1
    return {"turn": turn, "log": [f"Turn {turn} started"]}


def agent_decide(state: GameState) -> dict:
    """Ask the LLM for a short decision for the current turn and record it."""
    # Groq's gpt-oss models spend some of max_tokens on hidden reasoning
    # before the visible answer, so give more headroom than the answer alone
    # would need.
    llm = build_llm(max_tokens=200)
    prompt = (
        f"You are a faction leader in a strategy game. This is turn "
        f"{state['turn']} of {state['max_turns']}. In one short sentence, "
        f"state a single strategic decision for this turn."
    )
    response = llm.invoke(prompt)
    decision = response.content if isinstance(response.content, str) else str(response.content)

    return {
        "last_decision": decision,
        "log": [f"Turn {state['turn']} decision: {decision}"],
    }


def route_after_decision(state: GameState) -> Literal["start_turn", "__end__"]:
    """Loop back for another turn while turns remain, else end the graph."""
    if state["turn"] < state["max_turns"]:
        return "start_turn"
    return END


def build_graph():
    builder = StateGraph(GameState)
    builder.add_node("start_turn", start_turn)
    builder.add_node("agent_decide", agent_decide)

    builder.set_entry_point("start_turn")
    builder.add_edge("start_turn", "agent_decide")
    builder.add_conditional_edges(
        "agent_decide",
        route_after_decision,
        {"start_turn": "start_turn", END: END},
    )

    return builder.compile()


def run(max_turns: int = 3) -> GameState:
    load_dotenv()
    if not any(
        os.environ.get(key)
        for key in ("GROQ_API_KEY", "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY")
    ):
        raise RuntimeError(
            "No LLM provider key is set. Copy .env.example to .env and fill in "
            "at least one of GROQ_API_KEY, OPENROUTER_API_KEY, or ANTHROPIC_API_KEY."
        )

    graph = build_graph()
    initial_state: GameState = {
        "turn": 0,
        "max_turns": max_turns,
        "log": [],
        "last_decision": None,
    }
    return graph.invoke(initial_state)


if __name__ == "__main__":
    result = run()
    for line in result["log"]:
        print(line)
