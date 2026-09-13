"""Phase 5: the game loop entrypoint (`python -m game.run_game`).

Drives `agents.graph`'s compiled graph turn-by-turn via `.stream()` (rather
than `agents.graph.run()`'s single blocking `.invoke()`), so every action can
be persisted to Postgres as it happens and a win condition can be checked
after each turn instead of only at the very end.

Split into `create_game()` (fast — just DB writes) and `play_game()` (slow —
the actual LLM-driven turn loop) so `backend/` can create a game
synchronously inside a request handler, return its id immediately, and run
`play_game()` in a background thread — a client shouldn't have to hold a
request open for what could be minutes of LLM calls. `run_game()` is the
simple combined version, for the CLI and tests that don't need that split.

Kept deliberately simple for Phase 5's first pass: a single elimination win
condition (one faction left holding any territory). Loading factions from a
saved `Scenario` (`load_faction_configs`) or building an ad-hoc one are both
supported — either way, the game gets real `Scenario`/`ScenarioFaction` rows
(an ad-hoc run still creates one), so `backend/`'s future scenario-editor
endpoints are just another way to populate the same tables.
"""

import argparse
import logging
import re
import uuid
from collections.abc import Callable
from datetime import datetime, timezone

from sqlalchemy.orm import Session, sessionmaker

from agents.graph import (
    DEMO_FACTIONS,
    HumanActionProvider,
    build_graph,
    initial_state_for,
    require_llm_configured,
    set_human_action_provider,
)
from agents.state import GameState
from db.models import (
    DiplomaticRelation,
    DiplomaticStatus,
    FactionStateSnapshot,
    Game,
    GameEvent,
    GameFaction,
    GameStatus,
    RolePreset,
    Scenario,
    ScenarioFaction,
)
from db.session import get_sessionmaker, scoped_session
from game.rules import territory_of
from map_data.loader import get_province

logger = logging.getLogger(__name__)

MAX_TURNS_LIMIT = 200  # sanity cap — the LLM fallback chain includes paid
# Anthropic Claude, so an unbounded max_turns (a typo, or a malicious
# request) has no ceiling on real API cost otherwise.


class ScenarioNotFoundError(ValueError):
    """`scenario_id` doesn't refer to a real Scenario — distinct from other
    `ValueError`s (invalid role preset, bad province id, ...) raised by the
    same call, so callers like `backend/main.py` can tell "not found" (404)
    apart from "bad input" (422) instead of guessing from the exception text.
    """


def check_winner(state: GameState) -> str | None:
    """Elimination win condition: exactly one faction still holds territory.

    Returns None while 2+ factions remain (game continues) or, in the
    unreached-in-practice case of simultaneous mutual elimination, 0 remain.
    """
    alive = [fid for fid in state["turn_order"] if territory_of(state, fid)]
    return alive[0] if len(alive) == 1 else None


def _validate_faction_configs(faction_configs: list[dict]) -> None:
    """Catch two ways a game silently breaks instead of erroring: a
    province id (capital or anywhere in starting_territory) that isn't
    real, or two factions starting with the same one. A collision inside
    starting_territory is just as broken as one on home_province used to
    be — agents.graph.initial_state_for builds province_owner by iterating
    every faction's territory in order, so an unvalidated collision would
    silently hand the province to whichever faction happens to come later
    in the list, not raise.
    """
    seen: dict[str, str] = {}
    for cfg in faction_configs:
        territory = set(cfg.get("starting_territory") or ()) | {cfg["home_province"]}
        for province_id in territory:
            if get_province(province_id) is None:
                raise ValueError(
                    f"Faction {cfg['faction_id']!r} has {province_id!r} in its starting "
                    "territory, which isn't a real province id"
                )
            if province_id in seen:
                raise ValueError(
                    f"Factions {seen[province_id]!r} and {cfg['faction_id']!r} both start "
                    f"in {province_id!r} — starting territory must not overlap"
                )
            seen[province_id] = cfg["faction_id"]


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "faction"


def load_faction_configs(session: Session, scenario_id: uuid.UUID) -> list[dict]:
    """Build `faction_configs` (the shape `initial_state_for` expects) from
    an existing `Scenario`'s `ScenarioFaction` rows.

    `faction_id` (the short string agents.graph uses internally, e.g. as
    dict keys) is derived from the faction's name, since ScenarioFaction has
    no such field of its own — only a DB UUID. Deduped with a numeric suffix
    if two factions in the scenario would otherwise slugify to the same id.
    `home_province` (the capital, a fixed geographic anchor — see
    agents.graph.initial_state_for) is `starting_territory[0]`; the rest of
    `starting_territory` is carried through too, so a scenario with a
    multi-province starting footprint (built in the scenario editor's
    "Adjust territory") actually starts that way, not shrunk back to just
    the capital.
    """
    scenario = session.get(Scenario, scenario_id)
    if scenario is None:
        raise ScenarioNotFoundError(f"No scenario with id {scenario_id}")

    faction_configs = []
    seen_ids: set[str] = set()
    for sf in scenario.factions:
        if not sf.starting_territory:
            raise ValueError(f"ScenarioFaction {sf.id} ({sf.faction_name}) has no starting_territory")
        base_id = _slugify(sf.faction_name)
        faction_id = base_id
        suffix = 2
        while faction_id in seen_ids:
            faction_id = f"{base_id}_{suffix}"
            suffix += 1
        seen_ids.add(faction_id)

        faction_configs.append(
            {
                "faction_id": faction_id,
                "name": sf.faction_name,
                "role_preset": sf.role_preset.value,
                "home_province": sf.starting_territory[0],
                "starting_territory": list(sf.starting_territory),
                "resources": dict(sf.starting_resources) or None,
                "units": dict(sf.starting_units) or None,
            }
        )
    return faction_configs


def _create_game_records(
    session: Session,
    faction_configs: list[dict],
    max_turns: int,
    scenario_id: uuid.UUID | None,
) -> tuple[uuid.UUID, dict[str, uuid.UUID]]:
    """Write the Game/GameFaction rows for a new run (and, if `scenario_id`
    is None, a fresh ad-hoc Scenario/ScenarioFaction too). Returns the new
    game's id and a faction_id (app) -> id (db) map, since every other table
    FKs to the db-generated GameFaction.id, not the app's string faction_id.
    """
    if scenario_id is None:
        scenario = Scenario(name="Ad-hoc run", max_turns=max_turns)
        for cfg in faction_configs:
            scenario.factions.append(
                ScenarioFaction(
                    faction_name=cfg["name"],
                    role_preset=RolePreset(cfg["role_preset"]),
                    starting_resources=dict(cfg.get("resources") or {"gold": 20}),
                    starting_units=dict(cfg.get("units") or {"legion": 2}),
                    starting_territory=cfg.get("starting_territory") or [cfg["home_province"]],
                )
            )
        session.add(scenario)
        session.flush()  # assigns scenario.id without committing yet
        scenario_id = scenario.id

    game = Game(
        scenario_id=scenario_id,
        status=GameStatus.RUNNING,
        config_snapshot={"factions": faction_configs, "max_turns": max_turns},
    )
    session.add(game)
    session.flush()

    db_faction_id: dict[str, uuid.UUID] = {}
    for cfg in faction_configs:
        game_faction = GameFaction(
            game_id=game.id,
            faction_name=cfg["name"],
            role_preset=RolePreset(cfg["role_preset"]),
        )
        session.add(game_faction)
        session.flush()
        db_faction_id[cfg["faction_id"]] = game_faction.id

    return game.id, db_faction_id


def create_game(
    faction_configs: list[dict] | None = None,
    scenario_id: uuid.UUID | None = None,
    max_turns: int = 10,
    session_factory: sessionmaker[Session] | None = None,
) -> tuple[uuid.UUID, list[dict], dict[str, uuid.UUID]]:
    """Fast, DB-only half of starting a game: write the records, return
    everything `play_game()` needs. Exactly one of `faction_configs` /
    `scenario_id` should be given; if neither is, falls back to the demo.

    Raises `ValueError` for bad input (invalid role preset, duplicate/unreal
    home provinces, `max_turns` out of range) and `ScenarioNotFoundError`
    (a `ValueError` subclass) specifically when `scenario_id` doesn't exist
    — callers needing to tell those apart (e.g. for HTTP status codes)
    should catch `ScenarioNotFoundError` before the general `ValueError`.
    """
    if not (1 <= max_turns <= MAX_TURNS_LIMIT):
        raise ValueError(f"max_turns must be between 1 and {MAX_TURNS_LIMIT}, got {max_turns}")

    session_factory = session_factory or get_sessionmaker()

    with scoped_session(session_factory) as session:
        if scenario_id is not None:
            faction_configs = load_faction_configs(session, scenario_id)
        elif faction_configs is None:
            faction_configs = DEMO_FACTIONS
        _validate_faction_configs(faction_configs)
        game_id, db_faction_id = _create_game_records(
            session, faction_configs, max_turns, scenario_id
        )

    return game_id, faction_configs, db_faction_id


def play_game(
    game_id: uuid.UUID,
    faction_configs: list[dict],
    db_faction_id: dict[str, uuid.UUID],
    max_turns: int = 10,
    session_factory: sessionmaker[Session] | None = None,
    on_event: Callable[[GameState], None] | None = None,
    human_action_provider: HumanActionProvider | None = None,
) -> GameState:
    """Slow half: the actual LLM-driven turn loop, persisting as it goes.

    `on_event`, if given, is called with every yielded `GameState` (the
    initial one too, before any faction has acted) — `backend/` uses this to
    broadcast live updates over WebSocket. Runs synchronously/blocking on
    whatever thread calls it; `backend/` is responsible for putting that on
    a background thread, not this function.

    `human_action_provider`, if given, lets a live viewer take over a
    faction's turns (`backend/`'s live-play feature) — see
    `agents.graph.set_human_action_provider`'s docstring for why this is
    injected as a plain callable rather than `game/` importing anything
    from `backend/` (it stays optional and unused by the CLI/tests, which
    never pass one).

    On any exception (no LLM key configured, a DB error, ...), marks the
    `Game` row `FAILED` (instead of leaving it stuck at `RUNNING` forever)
    and logs it server-side before re-raising — a failure with nobody
    connected via WebSocket at that instant would otherwise be completely
    silent, with no trace anywhere.
    """
    session_factory = session_factory or get_sessionmaker()
    try:
        return _play_game(
            game_id, faction_configs, db_faction_id, max_turns, session_factory, on_event,
            human_action_provider,
        )
    except Exception:
        logger.exception("Game %s failed", game_id)
        try:
            with scoped_session(session_factory) as session:
                game = session.get(Game, game_id)
                if game is not None:
                    game.status = GameStatus.FAILED
                    game.ended_at = datetime.now(timezone.utc)
        except Exception:
            logger.exception("Game %s: also failed to record FAILED status", game_id)
        raise


def _mark_eliminated_factions(
    session: Session, state: GameState, db_faction_id: dict[str, uuid.UUID], turn: int
) -> None:
    for faction_id in state["turn_order"]:
        if territory_of(state, faction_id):
            continue
        game_faction = session.get(GameFaction, db_faction_id[faction_id])
        if game_faction.is_alive:
            game_faction.is_alive = False
            game_faction.eliminated_at_turn = turn


def _play_game(
    game_id: uuid.UUID,
    faction_configs: list[dict],
    db_faction_id: dict[str, uuid.UUID],
    max_turns: int,
    session_factory: sessionmaker[Session],
    on_event: Callable[[GameState], None] | None,
    human_action_provider: HumanActionProvider | None = None,
) -> GameState:
    require_llm_configured()

    initial_state = initial_state_for(faction_configs, max_turns)
    graph = build_graph()
    config = {"recursion_limit": max_turns * len(faction_configs) + 10}

    # Set for the duration of this game's turn loop only, on whatever
    # thread is calling us (see agents.graph.set_human_action_provider) —
    # cleared in the `finally` below so a thread pool reusing this OS
    # thread for another game later doesn't inherit a stale provider.
    set_human_action_provider(human_action_provider)
    try:
        return _run_turn_loop(
            game_id, db_faction_id, session_factory, on_event, graph, initial_state, config
        )
    finally:
        set_human_action_provider(None)


def _run_turn_loop(
    game_id: uuid.UUID,
    db_faction_id: dict[str, uuid.UUID],
    session_factory: sessionmaker[Session],
    on_event: Callable[[GameState], None] | None,
    graph,
    initial_state: GameState,
    config: dict,
) -> GameState:
    prev_diplomatic_status: dict[str, str] = {}
    final_state = initial_state
    winner_faction_id: str | None = None

    for state in graph.stream(initial_state, config, stream_mode="values"):
        final_state = state
        event = state.get("last_event")

        if event is None:
            # The initial state, before any faction has acted — nothing to
            # persist yet, but backend/ still wants this one broadcast so a
            # freshly-connected client sees the starting board immediately.
            if on_event is not None:
                on_event(state)
            continue

        with scoped_session(session_factory) as session:
            session.add(
                GameEvent(
                    game_id=game_id,
                    turn=event["turn"],
                    faction_id=db_faction_id[event["faction_id"]],
                    event_type=event["action_type"],
                    payload={
                        k: v
                        for k, v in event.items()
                        if k not in ("turn", "faction_id", "action_type")
                    },
                )
            )

            for key, status in state["diplomatic_status"].items():
                if prev_diplomatic_status.get(key) != status:
                    faction_a, faction_b = key.split("|")
                    session.add(
                        DiplomaticRelation(
                            game_id=game_id,
                            faction_a_id=db_faction_id[faction_a],
                            faction_b_id=db_faction_id[faction_b],
                            status=DiplomaticStatus(status),
                            turn_changed=event["turn"],
                        )
                    )
            prev_diplomatic_status = dict(state["diplomatic_status"])

            # Checked after every action, not just move_army: only combat
            # can currently empty a faction's territory, but checking
            # unconditionally costs a handful of dict lookups and doesn't
            # assume that stays true as more action types are added.
            _mark_eliminated_factions(session, state, db_faction_id, event["turn"])

            # Upserted after every action, not just once per completed round
            # (verified live: a frontend watching a game found the map stayed
            # completely uncolored for the entire first round, since that's
            # how long it took for any FactionStateSnapshot to exist at all).
            # Keyed on event["turn"] (the round number, constant for every
            # action within a round) rather than state["turn"] (which only
            # advances once the round wraps) — using the latter here would
            # give a round's earlier actions a different, stale turn number
            # than its last one, fragmenting what should be one snapshot.
            # Upsert, not insert: this can now run multiple times against
            # the same (game_id, faction_id, turn) — e.g. a faction acting,
            # then getting raided as a defender, both inside round N — and
            # a second plain insert would violate FactionStateSnapshot's
            # unique constraint on that triple.
            for faction_id, faction in state["factions"].items():
                owned = territory_of(state, faction_id)
                snapshot = (
                    session.query(FactionStateSnapshot)
                    .filter_by(game_id=game_id, faction_id=db_faction_id[faction_id], turn=event["turn"])
                    .one_or_none()
                )
                if snapshot is None:
                    snapshot = FactionStateSnapshot(
                        game_id=game_id, faction_id=db_faction_id[faction_id], turn=event["turn"]
                    )
                    session.add(snapshot)
                snapshot.resources = dict(faction["resources"])
                snapshot.territory_count = len(owned)
                snapshot.territory = owned
                snapshot.unit_count = sum(faction["units"].values())

        # Only now, after the transaction above has committed — broadcasting
        # first (as this used to) let a WebSocket subscriber's immediate
        # REST follow-up race the still-in-flight DB write and read stale
        # data. Caught by a test asserting on DB state from inside on_event,
        # not by inspection.
        if on_event is not None:
            on_event(state)

        winner_faction_id = check_winner(state)
        if winner_faction_id is not None:
            break

    with scoped_session(session_factory) as session:
        game = session.get(Game, game_id)
        game.status = GameStatus.COMPLETED
        game.current_turn = final_state["turn"]
        game.ended_at = datetime.now(timezone.utc)
        if winner_faction_id is not None:
            game.winner_faction_id = db_faction_id[winner_faction_id]

    return final_state


def run_game(
    faction_configs: list[dict] | None = None,
    scenario_id: uuid.UUID | None = None,
    max_turns: int = 10,
    session_factory: sessionmaker[Session] | None = None,
    on_event: Callable[[GameState], None] | None = None,
) -> GameState:
    """Create and play a game in one call. See `create_game`/`play_game` for
    the split version `backend/` uses to avoid blocking a request on the
    whole game.
    """
    session_factory = session_factory or get_sessionmaker()
    game_id, faction_configs, db_faction_id = create_game(
        faction_configs, scenario_id, max_turns, session_factory
    )
    return play_game(
        game_id, faction_configs, db_faction_id, max_turns, session_factory, on_event
    )


def _bounded_max_turns(value: str) -> int:
    n = int(value)
    if not (1 <= n <= MAX_TURNS_LIMIT):
        raise argparse.ArgumentTypeError(f"must be between 1 and {MAX_TURNS_LIMIT}")
    return n


def _parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--max-turns", type=_bounded_max_turns, default=10,
        help=f"Rounds to play if no winner emerges sooner (1-{MAX_TURNS_LIMIT}).",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    result = run_game(max_turns=args.max_turns)
    for line in result["log"]:
        print(line)
    winner = check_winner(result)
    print(f"\nWinner: {winner}" if winner else "\nNo winner — max turns reached.")
