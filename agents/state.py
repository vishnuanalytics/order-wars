import operator
from typing import Annotated, TypedDict


class FactionState(TypedDict):
    """Per-faction state, keyed by `faction_id` in `GameState.factions`.

    `intent` is the strategic-leader layer's output (refreshed every few
    turns); `last_action` is the executor layer's output for the most recent
    turn (a serialized `agents.actions.FactionAction`, or None before a
    faction has acted). See CLAUDE.md "Agent & simulation design" for why
    decisions are split into these two layers.
    """

    faction_id: str
    name: str
    role_preset: str
    intent: str | None
    last_action: dict | None


class GameState(TypedDict):
    """Shared state passed between every node in the graph.

    Every agent/node reads from and writes to this same dict-like object as
    the game progresses. This is the single source of truth for what fields
    exist — new fields get added here first, not scattered as ad-hoc keys.

    Supports an arbitrary number of factions (`turn_order`), not a hardcoded
    pair — see CLAUDE.md "Agent & simulation design": N factions from the
    start. `turn` counts completed full rounds (every faction having acted
    once); `active_faction_idx` tracks whose turn it is within the current
    round. `log` uses the `operator.add` reducer so that concurrent/
    successive nodes append to it instead of overwriting each other's
    entries.
    """

    turn: int
    max_turns: int
    turn_order: list[str]
    active_faction_idx: int
    factions: dict[str, FactionState]
    log: Annotated[list[str], operator.add]
