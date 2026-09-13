"""game/run_game.py against in-memory SQLite (via a StaticPool so every
`_scoped_session()` call shares the same in-memory DB, not a fresh empty one
per connection) — no live LLM or Neon calls in the suite.
"""

import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from agents import graph as graph_module
from agents.actions import FactionAction
from db.models import (
    Base,
    DiplomaticRelation,
    FactionStateSnapshot,
    Game,
    GameEvent,
    GameFaction,
    GameStatus,
    RolePreset,
    Scenario,
    ScenarioFaction,
)
from game import run_game as run_game_module
from game.run_game import (
    MAX_TURNS_LIMIT,
    ScenarioNotFoundError,
    check_winner,
    create_game,
    load_faction_configs,
    play_game,
    run_game,
)

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
    strong enough to win a single battle outright, both factions already at
    war, and a's siege of ROME_NEIGHBOR already one turn in — lets a
    2-faction elimination happen on "a"'s very next move_army (the decisive
    battle, per game.rules.SIEGE_TURNS_TO_DECIDE), without needing to
    script several rounds of fake LLM output.
    """
    from game.rules import SIEGE_TURNS_TO_DECIDE, pair_key

    state = graph_module.initial_state_for(faction_configs, max_turns)
    state["factions"]["a"]["units"] = {"legion": 10}
    state["factions"]["b"]["units"] = {"legion": 1}
    state["diplomatic_status"] = {pair_key("a", "b"): "war"}
    state["sieges"] = {
        ROME_NEIGHBOR: {"attacker_id": "a", "progress": SIEGE_TURNS_TO_DECIDE - 1}
    }
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
        assert "broke the siege" in events[0].payload["resolution"]


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


def test_snapshot_exists_mid_round_not_only_after_it_completes(sqlite_sessionmaker, monkeypatch):
    """A frontend watching a game live needs to color the map before the
    first full round finishes, not just after — this reproduces that with 3
    factions so there's an observable "mid-round" moment (after faction
    a's turn, before b and c have acted).
    """

    def _peaceful_state(faction_configs, max_turns):
        return graph_module.initial_state_for(faction_configs, max_turns)

    monkeypatch.setattr(run_game_module, "initial_state_for", _peaceful_state)

    class _HoldLLM:
        def __init__(self, *a, **k):
            pass

        def invoke(self, prompt):
            return FactionAction(action_type="hold", rationale="test")

    monkeypatch.setattr(
        graph_module, "build_llm",
        lambda max_tokens=64, schema=None: (_HoldLLM() if schema is not None else _FakeIntentLLM()),
    )

    three_factions = FACTION_CONFIGS + [
        {"faction_id": "c", "name": "Third", "role_preset": "custom", "home_province": "83386efffffffff"}
    ]

    seen_mid_round_snapshot_count = []

    def _on_event(state):
        if state.get("last_event") and state["last_event"]["faction_id"] == "a" and state["turn"] == 0:
            with sqlite_sessionmaker() as session:
                seen_mid_round_snapshot_count.append(session.query(FactionStateSnapshot).count())

    run_game(three_factions, max_turns=1, session_factory=sqlite_sessionmaker, on_event=_on_event)

    assert seen_mid_round_snapshot_count == [3]  # a's own turn already snapshots all 3 factions
    with sqlite_sessionmaker() as session:
        snapshot = (
            session.query(FactionStateSnapshot)
            .filter_by(game_id=session.query(Game).one().id, turn=1)
            .count()
        )
        assert snapshot == 3  # still exactly 3 after b and c also act — upserted, not duplicated


def test_create_game_returns_id_before_playing(sqlite_sessionmaker):
    """The whole point of the create/play split: backend/ needs the game id
    fast, without waiting for any turns to actually run.
    """
    game_id, faction_configs, db_faction_id = create_game(
        FACTION_CONFIGS, max_turns=10, session_factory=sqlite_sessionmaker
    )

    assert faction_configs == FACTION_CONFIGS
    assert set(db_faction_id) == {"a", "b"}
    with sqlite_sessionmaker() as session:
        game = session.get(Game, game_id)
        assert game is not None
        assert game.status == GameStatus.RUNNING  # play_game hasn't run yet


def test_play_game_after_create_game_matches_run_game(sqlite_sessionmaker):
    game_id, faction_configs, db_faction_id = create_game(
        FACTION_CONFIGS, max_turns=10, session_factory=sqlite_sessionmaker
    )
    final_state = play_game(
        game_id, faction_configs, db_faction_id, max_turns=10, session_factory=sqlite_sessionmaker
    )

    assert check_winner(final_state) == "a"
    with sqlite_sessionmaker() as session:
        assert session.get(Game, game_id).status == GameStatus.COMPLETED


def test_play_game_calls_on_event_for_every_yielded_state(sqlite_sessionmaker):
    game_id, faction_configs, db_faction_id = create_game(
        FACTION_CONFIGS, max_turns=10, session_factory=sqlite_sessionmaker
    )
    seen = []
    play_game(
        game_id, faction_configs, db_faction_id, max_turns=10,
        session_factory=sqlite_sessionmaker, on_event=seen.append,
    )

    # Initial state (last_event=None) plus one real turn before "a" wins.
    assert len(seen) == 2
    assert seen[0]["last_event"] is None
    assert seen[1]["last_event"]["faction_id"] == "a"


def test_play_game_wires_the_human_action_provider_for_the_turn_loop(sqlite_sessionmaker):
    """agents.graph.faction_turn reads whatever provider is currently set
    via _get_human_action_provider — play_game is responsible for setting
    it before the graph runs and clearing it afterward (see
    set_human_action_provider's docstring on why this is thread-local
    rather than passed through every node).
    """
    seen_during_run = []

    def _provider(faction_id):
        seen_during_run.append(graph_module._get_human_action_provider())
        return None  # let the AI decide, same as "not human-controlled"

    game_id, faction_configs, db_faction_id = create_game(
        FACTION_CONFIGS, max_turns=10, session_factory=sqlite_sessionmaker
    )
    assert graph_module._get_human_action_provider() is None  # nothing set before the call

    play_game(
        game_id, faction_configs, db_faction_id, max_turns=10,
        session_factory=sqlite_sessionmaker, human_action_provider=_provider,
    )

    # faction_turn actually called our provider (proves the wiring reaches
    # the node, not just that play_game accepted the parameter) ...
    assert len(seen_during_run) > 0
    # ... and it's cleared afterward, so a later game on a reused thread
    # doesn't inherit a stale provider.
    assert graph_module._get_human_action_provider() is None


def test_load_faction_configs_from_scenario(sqlite_sessionmaker):
    with sqlite_sessionmaker() as session:
        scenario = Scenario(name="Test Scenario", max_turns=5)
        scenario.factions.append(
            ScenarioFaction(
                faction_name="Rome",
                role_preset=RolePreset.EXPANSIONIST,
                starting_resources={"gold": 50},
                starting_units={"legion": 4},
                starting_territory=[ROME_HOME],
            )
        )
        scenario.factions.append(
            ScenarioFaction(
                faction_name="Rome",  # duplicate name — exercises slug dedup
                role_preset=RolePreset.WARMONGER,
                starting_territory=[ROME_NEIGHBOR],
            )
        )
        session.add(scenario)
        session.commit()
        scenario_id = scenario.id

    with sqlite_sessionmaker() as session:
        configs = load_faction_configs(session, scenario_id)

    assert [c["faction_id"] for c in configs] == ["rome", "rome_2"]
    assert configs[0]["home_province"] == ROME_HOME
    assert configs[0]["resources"] == {"gold": 50}
    assert configs[0]["units"] == {"legion": 4}
    assert configs[1]["role_preset"] == "warmonger"


def test_run_game_from_scenario_id(sqlite_sessionmaker, monkeypatch):
    with sqlite_sessionmaker() as session:
        scenario = Scenario(name="From Scenario", max_turns=10)
        scenario.factions.append(
            ScenarioFaction(
                faction_name="Strong", role_preset=RolePreset.WARMONGER,
                starting_territory=[ROME_HOME],
            )
        )
        scenario.factions.append(
            ScenarioFaction(
                faction_name="Weak", role_preset=RolePreset.ISOLATIONIST,
                starting_territory=[ROME_NEIGHBOR],
            )
        )
        session.add(scenario)
        session.commit()
        scenario_id = scenario.id

    def _lopsided_from_scenario(faction_configs, max_turns):
        from game.rules import SIEGE_TURNS_TO_DECIDE, pair_key

        state = graph_module.initial_state_for(faction_configs, max_turns)
        strong_id = next(c["faction_id"] for c in faction_configs if c["name"] == "Strong")
        weak_id = next(c["faction_id"] for c in faction_configs if c["name"] == "Weak")
        state["factions"][strong_id]["units"] = {"legion": 10}
        state["factions"][weak_id]["units"] = {"legion": 1}
        state["diplomatic_status"] = {pair_key(strong_id, weak_id): "war"}
        state["sieges"] = {
            ROME_NEIGHBOR: {"attacker_id": strong_id, "progress": SIEGE_TURNS_TO_DECIDE - 1}
        }
        return state

    monkeypatch.setattr(run_game_module, "initial_state_for", _lopsided_from_scenario)
    final_state = run_game(
        scenario_id=scenario_id, max_turns=10, session_factory=sqlite_sessionmaker
    )

    winner_id = check_winner(final_state)
    assert final_state["factions"][winner_id]["name"] == "Strong"

    with sqlite_sessionmaker() as session:
        game = session.query(Game).filter(Game.scenario_id == scenario_id).one()
        assert game.status == GameStatus.COMPLETED
        # Reused the existing scenario — didn't create a second one.
        assert session.query(Scenario).count() == 1


def test_create_game_rejects_duplicate_home_provinces(sqlite_sessionmaker):
    configs = [
        {"faction_id": "a", "name": "A", "role_preset": "custom", "home_province": ROME_HOME},
        {"faction_id": "b", "name": "B", "role_preset": "custom", "home_province": ROME_HOME},
    ]
    with pytest.raises(ValueError, match="both start in"):
        create_game(configs, max_turns=5, session_factory=sqlite_sessionmaker)

    with sqlite_sessionmaker() as session:
        # Rejected before anything was written — no orphaned Scenario/Game.
        assert session.query(Scenario).count() == 0
        assert session.query(Game).count() == 0


def test_create_game_rejects_unreal_province_id(sqlite_sessionmaker):
    configs = [
        {"faction_id": "a", "name": "A", "role_preset": "custom", "home_province": "not_a_real_id"},
    ]
    with pytest.raises(ValueError, match="isn't a real province id"):
        create_game(configs, max_turns=5, session_factory=sqlite_sessionmaker)


def test_create_game_rejects_max_turns_out_of_range(sqlite_sessionmaker):
    with pytest.raises(ValueError, match="max_turns"):
        create_game(FACTION_CONFIGS, max_turns=0, session_factory=sqlite_sessionmaker)
    with pytest.raises(ValueError, match="max_turns"):
        create_game(FACTION_CONFIGS, max_turns=MAX_TURNS_LIMIT + 1, session_factory=sqlite_sessionmaker)


def test_load_faction_configs_raises_scenario_not_found_specifically(sqlite_sessionmaker):
    """A ValueError subclass, not a plain ValueError — callers (backend/)
    need to tell "doesn't exist" (404) apart from "bad input" (422).
    """
    with sqlite_sessionmaker() as session:
        with pytest.raises(ScenarioNotFoundError):
            load_faction_configs(session, uuid.uuid4())


def test_play_game_marks_game_failed_and_logs_on_exception(sqlite_sessionmaker, monkeypatch, caplog):
    game_id, faction_configs, db_faction_id = create_game(
        FACTION_CONFIGS, max_turns=10, session_factory=sqlite_sessionmaker
    )

    def _boom():
        raise RuntimeError("no LLM provider configured")

    monkeypatch.setattr(run_game_module, "require_llm_configured", _boom)

    with pytest.raises(RuntimeError, match="no LLM provider configured"):
        play_game(game_id, faction_configs, db_faction_id, max_turns=10, session_factory=sqlite_sessionmaker)

    with sqlite_sessionmaker() as session:
        game = session.get(Game, game_id)
        assert game.status == GameStatus.FAILED
        assert game.ended_at is not None
    assert "failed" in caplog.text.lower()


def test_play_game_marks_faction_eliminated_when_territory_reaches_zero(sqlite_sessionmaker):
    game_id, faction_configs, db_faction_id = create_game(
        FACTION_CONFIGS, max_turns=10, session_factory=sqlite_sessionmaker
    )
    play_game(game_id, faction_configs, db_faction_id, max_turns=10, session_factory=sqlite_sessionmaker)

    with sqlite_sessionmaker() as session:
        winner = session.get(GameFaction, db_faction_id["a"])
        loser = session.get(GameFaction, db_faction_id["b"])
        assert winner.is_alive is True
        assert winner.eliminated_at_turn is None
        assert loser.is_alive is False
        assert loser.eliminated_at_turn == 1
