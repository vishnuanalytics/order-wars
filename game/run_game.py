"""Phase 5: the game loop entrypoint (`python -m game.run_game`).

Drives `agents.graph`'s compiled graph turn-by-turn via `.stream()` (rather
than `agents.graph.run()`'s single blocking `.invoke()`), so every action can
be persisted to Postgres as it happens and a win condition can be checked
after each turn instead of only at the very end.

Kept deliberately simple for Phase 5's first pass: a single elimination win
condition (one faction left holding any territory), and one hardcoded/
CLI-configurable scenario — the scenario-editor UI that would let a user
build `Scenario`/`ScenarioFaction` rows from a browser is separate, later
Phase 5 work. This module already writes those tables (an ad-hoc scenario is
still a real one), so that UI just needs to become another way to populate
the same rows.
"""

import argparse
import uuid
from collections.abc import Iterator
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
    parameterized so tests can inject a SQLite sessionmaker instead of
    hitting the real Neon database (see tests/test_run_game.py).
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


def _create_game_records(
    session: Session, faction_configs: list[dict], max_turns: int
) -> tuple[uuid.UUID, dict[str, uuid.UUID]]:
    """Write the Scenario/ScenarioFaction/Game/GameFaction rows for a new
    run. Returns the new game's id and a faction_id (app) -> id (db) map,
    since every other table FKs to the db-generated GameFaction.id, not the
    app's string faction_id.
    """
    scenario = Scenario(name="Ad-hoc run", max_turns=max_turns)
    for cfg in faction_configs:
        scenario.factions.append(
            ScenarioFaction(
                faction_name=cfg["name"],
                role_preset=RolePreset(cfg["role_preset"]),
                starting_resources=dict(cfg.get("resources", {"gold": 20})),
                starting_units=dict(cfg.get("units", {"legion": 2})),
                starting_territory=[cfg["home_province"]],
            )
        )
    session.add(scenario)
    session.flush()  # assigns scenario.id without committing yet

    game = Game(
        scenario_id=scenario.id,
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


def run_game(
    faction_configs: list[dict] | None = None,
    max_turns: int = 10,
    session_factory: sessionmaker[Session] | None = None,
) -> GameState:
    """Run a game turn-by-turn, persisting as it goes. Returns the final
    (or winning) `GameState`, same shape `agents.graph.run()` returns.
    """
    faction_configs = faction_configs if faction_configs is not None else DEMO_FACTIONS
    require_llm_configured()
    session_factory = session_factory or get_sessionmaker()

    with _scoped_session(session_factory) as session:
        game_id, db_faction_id = _create_game_records(session, faction_configs, max_turns)

    initial_state = initial_state_for(faction_configs, max_turns)
    graph = build_graph()
    config = {"recursion_limit": max_turns * len(faction_configs) + 10}

    prev_diplomatic_status: dict[str, str] = {}
    final_state = initial_state
    winner_faction_id: str | None = None

    for state in graph.stream(initial_state, config, stream_mode="values"):
        final_state = state
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
