import operator
from typing import Annotated, TypedDict


class GameState(TypedDict):
    """Shared state passed between every node in the graph.

    Every agent/node reads from and writes to this same dict-like object as
    the game progresses. This is the single source of truth for what fields
    exist — new fields get added here first, not scattered as ad-hoc keys.

    `log` uses the `operator.add` reducer so that concurrent/successive nodes
    append to it instead of overwriting each other's entries.
    """

    turn: int
    max_turns: int
    log: Annotated[list[str], operator.add]
    last_decision: str | None
