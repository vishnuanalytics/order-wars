"""game/run_game.py against in-memory SQLite (via a StaticPool so every
`_scoped_session()` call shares the same in-memory DB, not a fresh empty one
per connection) — no live LLM or Neon calls in the suite.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from agents import graph as graph_module
from agents.actions import FactionAction
from db.models import Base, DiplomaticRelation, FactionStateSnapshot, Game, GameEvent, GameStatus
from game import run_game as run_game_module
from game.run_game import check_winner, run_game

ROME_HOME = "831e80fffffffff"  # Italy 20
ROME_NEIGHBOR = "831e81fffffffff"  # Italy 19, adjacent to ROME_HOME — this
# test's faction "b"'s home, so faction "a" moving there is a real invasion.

FACTION_CONFIGS = [
    {"faction_id": "a", "name": "Strong", "role_preset": "warmonger", "home_province": ROME_HOME},
    {"faction_id": "b", "name": "Weak", "role_preset": "isolationist", "home_province": ROME_NEIGHBOR},
]


@pytest.fixture()
def sqlite_sessionmaker():
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


class _AlwaysInvadeLLM:
    """Executor fake: always tries to move into ROME_NEIGHBOR. Legal for
    faction "a" (adjacent, at war) and a harmless no-op for faction "b"
    (already its own territory) — one fake behavior covers both factions.
    """

    def __init__(self, *args, **kwargs):
        pass

    def invoke(self, prompt):
        return FactionAction(action_type="move_army", target_province=ROME_NEIGHBOR, rationale="test")


class _FakeIntentLLM:
    def __init__(self, *args, **kwargs):
        pass

    def invoke(self, prompt):
        class _Response:
            content = "test intent"

        return _Response()


def _fake_build_llm(max_tokens: int = 64, schema=None):
    return _AlwaysInvadeLLM() if schema is not None else _FakeIntentLLM()


def _lopsided_initial_state(faction_configs, max_turns):
    """Same shape as agents.graph.initial_state_for, but with faction "a"
    strong enough to win a single battle outright and both factions already
    at war — lets a 2-faction elimination happen in exactly one turn,
    without needing to script several rounds of fake LLM output.
    """
    from game.rules import pair_key

    state = graph_module.initial_state_for(faction_configs, max_turns)
    state["factions"]["a"]["units"] = {"legion": 10}
    state["factions"]["b"]["units"] = {"legion": 1}
    state["diplomatic_status"] = {pair_key("a", "b"): "war"}
    return state


@pytest.fixture(autouse=True)
def _no_real_llm_or_env(monkeypatch):
    monkeypatch.setattr(graph_module, "build_llm", _fake_build_llm)
    monkeypatch.setattr(run_game_module, "require_llm_configured", lambda: None)
    monkeypatch.setattr(run_game_module, "initial_state_for", _lopsided_initial_state)


def test_run_game_detects_winner_and_marks_game_completed(sqlite_sessionmaker):
    final_state = run_game(FACTION_CONFIGS, max_turns=10, session_factory=sqlite_sessionmaker)

    assert check_winner(final_state) == "a"

    with sqlite_sessionmaker() as session:
        game = session.query(Game).one()
        assert game.status == GameStatus.COMPLETED
        assert game.winner_faction_id is not None
        assert game.ended_at is not None
        # Stopped after the first (winning) turn, not all 10 requested.
        assert game.current_turn == 0  # turn only increments once a full round completes


def test_run_game_persists_the_winning_event(sqlite_sessionmaker):
    run_game(FACTION_CONFIGS, max_turns=10, session_factory=sqlite_sessionmaker)

    with sqlite_sessionmaker() as session:
        events = session.query(GameEvent).all()
        assert len(events) == 1
        assert events[0].event_type == "move_army"
        assert events[0].payload["target_province"] == ROME_NEIGHBOR
        assert "won the battle" in events[0].payload["resolution"]


def test_run_game_records_the_starting_diplomatic_status_once(sqlite_sessionmaker):
    # War was already in the initial state, not declared via an action
    # during the run — `prev_diplomatic_status` starts empty, so the first
    # observation of any non-default status is correctly treated as "new"
    # and recorded exactly once (not once per turn it continues to hold).
    run_game(FACTION_CONFIGS, max_turns=10, session_factory=sqlite_sessionmaker)

    with sqlite_sessionmaker() as session:
        relations = session.query(DiplomaticRelation).all()
        assert len(relations) == 1
        assert relations[0].status.value == "war"


def test_run_game_stops_at_max_turns_with_no_winner(sqlite_sessionmaker, monkeypatch):
    # Symmetric forces, no war declared: nobody can ever capture anything,
    # so the game should run out the clock instead of ending early.
    def _peaceful_state(faction_configs, max_turns):
        return graph_module.initial_state_for(faction_configs, max_turns)

    monkeypatch.setattr(run_game_module, "initial_state_for", _peaceful_state)

    class _HoldLLM:
        def __init__(self, *a, **k):
            pass

        def invoke(self, prompt):
            return FactionAction(action_type="hold", rationale="test")

    monkeypatch.setattr(graph_module, "build_llm", lambda max_tokens=64, schema=None: (
        _HoldLLM() if schema is not None else _FakeIntentLLM()
    ))

    final_state = run_game(FACTION_CONFIGS, max_turns=2, session_factory=sqlite_sessionmaker)

    assert final_state["turn"] == 2
    assert check_winner(final_state) is None

    with sqlite_sessionmaker() as session:
        game = session.query(Game).one()
        assert game.status == GameStatus.COMPLETED
        assert game.winner_faction_id is None
        # 2 rounds x 2 factions = 4 events, and a snapshot per faction after
        # each of the 2 completed rounds = 4 snapshots.
        assert session.query(GameEvent).count() == 4
        assert session.query(FactionStateSnapshot).count() == 4
