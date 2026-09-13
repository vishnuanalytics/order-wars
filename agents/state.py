import operator
from typing import Annotated, TypedDict


class FactionState(TypedDict):
    """Per-faction state, keyed by `faction_id` in `GameState.factions`.

    `intent` is the strategic-leader layer's output (refreshed every few
    turns); `last_action` is the executor layer's output for the most recent
    turn (a serialized `agents.actions.FactionAction`, or None before a
    faction has acted). See CLAUDE.md "Agent & simulation design" for why
    decisions are split into these two layers.

    Territory is deliberately NOT a field here — `GameState.province_owner`
    is the single source of truth for who owns what, so it can't desync from
    a per-faction copy. Use `game.rules.territory_of(state, faction_id)`.
    """

    faction_id: str
    name: str
    role_preset: str
    intent: str | None
    last_action: dict | None
    resources: dict[str, int]
    units: dict[str, int]


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

    `province_owner` maps a province id (from `map_data/provinces.geojson`)
    to the owning faction id; an absent key means unclaimed. `diplomatic_status`
    is keyed by `game.rules.pair_key(a, b)` (order-independent) with values
    "war"/"truce"/"alliance"; an absent key means neutral (the default).
    `pending_proposals` is keyed `"{proposer}->{target}"` (order matters —
    it's a one-sided offer until the target reciprocates) with a
    `agents.actions.ProposalType` value.

    `last_event` is a self-describing record of the most recently resolved
    turn (turn/faction_id/the action's fields/its resolution) — added so
    `game/run_game.py`'s persistence layer (and later the backend's
    WebSocket broadcast) can consume "what just happened" directly instead
    of diffing consecutive states to figure it out.

    `sieges` maps a province id under active siege to `{"attacker_id":
    str, "progress": int}` — ephemeral operational state, not ownership
    (which stays solely in `province_owner`), following the same pattern
    as `pending_proposals`. A siege only persists while its attacker keeps
    pressing the same target every one of their own turns; see
    `game.rules.resolve_action` for the exact lapse/decisive-battle rules.

    `capitals` (faction id -> province id) and `rebellion_seed` are set
    once in `initial_state_for` and never rewritten afterward — a fixed
    geographic anchor and a fixed per-game seed, respectively (a capital
    doesn't move even if captured; see `game.rules`'s rebellion docs for
    why). `province_captured_turn` (province id -> the `turn` it was last
    captured) is the one of these three that *does* mutate every time
    ownership changes via `move_army`; a province with no entry has been
    held since game start and is exempt from rebellion.

    `province_development` maps a province id to its development level
    (absent means 0 — undeveloped). Keyed purely by province, not by
    owner: it deliberately persists through a change of ownership (see
    `game.rules`'s development docs for why capturing a well-developed
    province stays valuable rather than resetting).
    """

    turn: int
    max_turns: int
    turn_order: list[str]
    active_faction_idx: int
    factions: dict[str, FactionState]
    province_owner: dict[str, str]
    diplomatic_status: dict[str, str]
    pending_proposals: dict[str, str]
    sieges: dict[str, dict]
    capitals: dict[str, str]
    province_captured_turn: dict[str, int]
    province_development: dict[str, int]
    rebellion_seed: int
    last_event: dict | None
    log: Annotated[list[str], operator.add]
