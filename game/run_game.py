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
import re
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone

from sqlalchemy.orm import Session, sessionmaker

from agents.graph import DEMO_FACTIONS, build_graph, initial_state_for, require_llm_configured
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
from db.session import get_sessionmaker
from game.rules import territory_of


def check_winner(state: GameState) -> str | None:
    """Elimination win condition: exactly one faction still holds territory.

    Returns None while 2+ factions remain (game continues) or, in the
    unreached-in-practice case of simultaneous mutual elimination, 0 remain.
    """
    alive = [fid for fid in state["turn_order"] if territory_of(state, fid)]
    return alive[0] if len(alive) == 1 else None


@contextmanager
def _scoped_session(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    """Same commit/rollback shape as `db.session.session_scope()`, but
    parameterized so tests (and, soon, `backend/`) can inject a session
    factory instead of always hitting the real Neon database.
    """
    session = session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "faction"


def load_faction_configs(session: Session, scenario_id: uuid.UUID) -> list[dict]:
    """Build `faction_configs` (the shape `initial_state_for` expects) from
    an existing `Scenario`'s `ScenarioFaction` rows.

    `faction_id` (the short string agents.graph uses internally, e.g. as
    dict keys) is derived from the faction's name, since ScenarioFaction has
    no such field of its own — only a DB UUID. Deduped with a numeric suffix
    if two factions in the scenario would otherwise slugify to the same id.
    `home_province` is `starting_territory[0]` — this project doesn't yet
    support a multi-province starting position (see agents/actions.py: one
    pooled army, no per-province garrisons).
    """
    scenario = session.get(Scenario, scenario_id)
    if scenario is None:
        raise ValueError(f"No scenario with id {scenario_id}")

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
                    starting_territory=[cfg["home_province"]],
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
    """
    session_factory = session_factory or get_sessionmaker()

    with _scoped_session(session_factory) as session:
        if scenario_id is not None:
            faction_configs = load_faction_configs(session, scenario_id)
        elif faction_configs is None:
            faction_configs = DEMO_FACTIONS
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
) -> GameState:
    """Slow half: the actual LLM-driven turn loop, persisting as it goes.

    `on_event`, if given, is called with every yielded `GameState` (the
    initial one too, before any faction has acted) — `backend/` uses this to
    broadcast live updates over WebSocket. Runs synchronously/blocking on
    whatever thread calls it; `backend/` is responsible for putting that on
    a background thread, not this function.
    """
    require_llm_configured()
    session_factory = session_factory or get_sessionmaker()

    initial_state = initial_state_for(faction_configs, max_turns)
    graph = build_graph()
    config = {"recursion_limit": max_turns * len(faction_configs) + 10}

    prev_diplomatic_status: dict[str, str] = {}
    final_state = initial_state
    winner_faction_id: str | None = None

    for state in graph.stream(initial_state, config, stream_mode="values"):
        final_state = state
        if on_event is not None:
            on_event(state)

        event = state.get("last_event")
        if event is None:
            continue  # the initial state, before any faction has acted

        with _scoped_session(session_factory) as session:
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

            if state["active_faction_idx"] == 0:  # a full round just completed
                for faction_id, faction in state["factions"].items():
                    owned = territory_of(state, faction_id)
                    session.add(
                        FactionStateSnapshot(
                            game_id=game_id,
                            faction_id=db_faction_id[faction_id],
                            turn=state["turn"],
                            resources=dict(faction["resources"]),
                            territory_count=len(owned),
                            territory=owned,
                            unit_count=sum(faction["units"].values()),
                        )
                    )

        winner_faction_id = check_winner(state)
        if winner_faction_id is not None:
            break

    with _scoped_session(session_factory) as session:
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


def _parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--max-turns", type=int, default=10, help="Rounds to play if no winner emerges sooner."
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    result = run_game(max_turns=args.max_turns)
    for line in result["log"]:
        print(line)
    winner = check_winner(result)
    print(f"\nWinner: {winner}" if winner else "\nNo winner — max turns reached.")
