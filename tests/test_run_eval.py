"""eval/run_eval.py against in-memory SQLite, with the LLM call mocked at
the same seam eval/llm_wrapper.py calls through (agents.llm.build_llm) —
no live LLM calls in the suite.
"""

import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from db.models import (
    Base,
    EvalScore,
    Game,
    GameEvent,
    GameFaction,
    GameStatus,
    RolePreset,
)
from eval import llm_wrapper as llm_wrapper_module
from eval.run_eval import run_eval


@pytest.fixture()
def sqlite_sessionmaker():
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


class _FakeResponse:
    content = "n/a"


class _FakeChatModel:
    """Stands in for whatever agents.llm.build_llm would return — a schema
    instance when structured output is requested (the Role Alignment
    metric's path), a plain message otherwise.
    """

    def __init__(self, schema=None):
        self.schema = schema

    def invoke(self, prompt):
        if self.schema is not None:
            return self.schema(score=8.0, reason="fits the doctrine")
        return _FakeResponse()


@pytest.fixture(autouse=True)
def _no_real_llm(monkeypatch):
    monkeypatch.setattr(
        llm_wrapper_module,
        "build_llm",
        lambda max_tokens=64, schema=None, structured_output_method=None: _FakeChatModel(schema),
    )


def _seed_game(session) -> tuple[uuid.UUID, uuid.UUID]:
    """A minimal game with one faction and two events: a legal move and one
    that was sanitized. Returns (game_id, faction_id).
    """
    game = Game(status=GameStatus.COMPLETED, current_turn=2, config_snapshot={})
    session.add(game)
    session.flush()

    faction = GameFaction(game_id=game.id, faction_name="Rome", role_preset=RolePreset.EXPANSIONIST)
    session.add(faction)
    session.flush()

    session.add(
        GameEvent(
            game_id=game.id,
            turn=1,
            faction_id=faction.id,
            event_type="move_army",
            payload={
                "target_province": "831e81fffffffff",
                "target_faction": None,
                "rationale": "Expand into unclaimed territory.",
                "resolution": "moved into 831e81fffffffff (now held)",
            },
        )
    )
    session.add(
        GameEvent(
            game_id=game.id,
            turn=2,
            faction_id=faction.id,
            event_type="hold",
            payload={
                "target_province": None,
                "target_faction": None,
                "rationale": "invalid move target 'xyz' sanitized to hold",
                "resolution": None,
            },
        )
    )
    # A non-faction-scoped event (faction_id=None) — must be skipped, not crash.
    session.add(GameEvent(game_id=game.id, turn=2, faction_id=None, event_type="tick", payload={}))
    session.commit()
    return game.id, faction.id


def test_run_eval_scores_every_faction_scoped_event(sqlite_sessionmaker):
    with sqlite_sessionmaker() as session:
        game_id, _faction_id = _seed_game(session)

    results = run_eval(game_id, session_factory=sqlite_sessionmaker)

    # 2 faction-scoped events x 2 metrics = 4 (the faction_id=None tick event skipped)
    assert len(results) == 4
    metric_names = {r["metric_name"] for r in results}
    assert metric_names == {"Legal Action", "Role Alignment"}

    with sqlite_sessionmaker() as session:
        assert session.query(EvalScore).count() == 4


def test_run_eval_legal_action_metric_distinguishes_sanitized_actions(sqlite_sessionmaker):
    with sqlite_sessionmaker() as session:
        game_id, _faction_id = _seed_game(session)

    results = run_eval(game_id, session_factory=sqlite_sessionmaker)

    legal_scores = {r["turn"]: r["score"] for r in results if r["metric_name"] == "Legal Action"}
    assert legal_scores[1] == 1.0  # the real move_army
    assert legal_scores[2] == 0.0  # the sanitized-to-hold action


def test_run_eval_role_alignment_uses_the_mocked_model(sqlite_sessionmaker):
    with sqlite_sessionmaker() as session:
        game_id, _faction_id = _seed_game(session)

    results = run_eval(game_id, session_factory=sqlite_sessionmaker)

    role_scores = [r["score"] for r in results if r["metric_name"] == "Role Alignment"]
    assert all(score == 0.8 for score in role_scores)  # 8.0 / 10 normalized


def test_run_eval_raises_for_unknown_game(sqlite_sessionmaker):
    with pytest.raises(ValueError, match="No game with id"):
        run_eval(uuid.uuid4(), session_factory=sqlite_sessionmaker)
