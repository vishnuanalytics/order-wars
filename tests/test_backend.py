"""backend/main.py against in-memory SQLite (via the app's dependency
override), with the game loop's LLM calls mocked and GameHub.start patched
to run synchronously — no live LLM/Neon calls, and no waiting on background
threads to observe results deterministically.
"""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from agents import graph as graph_module
from agents.actions import FactionAction
from backend.game_hub import hub
from backend.main import _default_session_factory, app
from db.models import Base, DiplomaticRelation, DiplomaticStatus, EvalScore
from eval import llm_wrapper as eval_llm_wrapper_module
from game import run_game as run_game_module

ROME_HOME = "831e80fffffffff"  # Italy 20
ROME_NEIGHBOR = "831e81fffffffff"  # Italy 19, adjacent to ROME_HOME


@pytest.fixture()
def sqlite_sessionmaker():
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture()
def client(sqlite_sessionmaker):
    app.dependency_overrides[_default_session_factory] = lambda: sqlite_sessionmaker
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


class _FakeIntentLLM:
    def __init__(self, *a, **k):
        pass

    def invoke(self, prompt):
        class _Response:
            content = "test intent"

        return _Response()


class _HoldLLM:
    """Always holds — used for tests that just need the game to complete a
    couple of harmless turns, not to test combat/capture specifically.
    """

    def __init__(self, *a, **k):
        pass

    def invoke(self, prompt):
        return FactionAction(action_type="hold", rationale="test")


class _FakeEvalChatModel:
    """Stands in for eval/llm_wrapper.py's underlying build_llm() call."""

    def __init__(self, schema=None):
        self.schema = schema

    def invoke(self, prompt):
        if self.schema is not None:
            return self.schema(score=8.0, reason="fits the doctrine")

        class _Response:
            content = "n/a"

        return _Response()


@pytest.fixture(autouse=True)
def _no_real_llm_or_thread(monkeypatch):
    monkeypatch.setattr(
        graph_module,
        "build_llm",
        lambda max_tokens=64, schema=None: (_HoldLLM() if schema is not None else _FakeIntentLLM()),
    )
    monkeypatch.setattr(run_game_module, "require_llm_configured", lambda: None)
    monkeypatch.setattr(
        eval_llm_wrapper_module,
        "build_llm",
        lambda max_tokens=64, schema=None, structured_output_method=None: _FakeEvalChatModel(schema),
    )
    # Run the "background" game synchronously so the test can assert on its
    # result immediately after the POST /games response, no polling/sleeping.
    # Mirrors the real GameHub.start's exception handling (catch, don't
    # propagate) — without this, a test simulating play_game raising would
    # have that exception escape straight out of the route handler instead
    # of being swallowed the way it actually is in production.
    def _sync_start(game_id, target):
        try:
            target()
        except Exception:
            pass

    monkeypatch.setattr(hub, "start", _sync_start)


AD_HOC_FACTIONS = [
    {"faction_id": "a", "name": "Rome", "role_preset": "expansionist", "home_province": ROME_HOME},
    {"faction_id": "b", "name": "Carthage", "role_preset": "warmonger", "home_province": ROME_NEIGHBOR},
]


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_cors_allows_cross_origin_requests(client):
    """The future browser frontend calls this API from a different origin
    (e.g. a Vite dev server) — without CORSMiddleware this would 200 for
    curl/TestClient (which don't enforce CORS) but be silently blocked by
    an actual browser. Check the header a browser would actually gate on.
    """
    response = client.get("/health", headers={"Origin": "http://localhost:5173"})
    assert response.headers["access-control-allow-origin"] == "*"

    preflight = client.options(
        "/games",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == "*"


def test_get_provinces_returns_real_geojson(client):
    response = client.get("/map/provinces")
    assert response.status_code == 200
    body = response.json()
    assert body["type"] == "FeatureCollection"
    assert len(body["features"]) > 0


def test_get_rivers_returns_real_geojson(client):
    response = client.get("/map/rivers")
    assert response.status_code == 200
    body = response.json()
    assert body["type"] == "FeatureCollection"
    assert len(body["features"]) > 0


def test_get_cities_returns_real_geojson(client):
    response = client.get("/map/cities")
    assert response.status_code == 200
    body = response.json()
    assert body["type"] == "FeatureCollection"
    assert len(body["features"]) > 0


GOOGLE_CLAIMS = {
    "sub": "108234098234098234",
    "email": "player@example.com",
    "name": "Test Player",
    "picture": "https://example.com/pic.jpg",
}


def _sign_in(client, monkeypatch, claims=None):
    """Returns a real session_token from a real (mocked-at-Google-only)
    POST /auth/google round trip — every auth-dependent test below uses
    this instead of hand-building a token, so it's also implicitly an
    end-to-end test of sign-in itself.
    """
    monkeypatch.setenv("SESSION_SECRET", "test-secret-not-for-real-use-padded-to-32-bytes")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "test-client-id.apps.googleusercontent.com")
    monkeypatch.setattr("backend.main.verify_google_id_token", lambda token: claims or GOOGLE_CLAIMS)
    response = client.post("/auth/google", json={"id_token": "fake-token-verified-by-the-mock-above"})
    assert response.status_code == 200
    return response.json()


def test_sign_in_with_google_creates_a_user_and_returns_a_usable_session(client, monkeypatch):
    body = _sign_in(client, monkeypatch)
    assert body["user"]["email"] == "player@example.com"
    assert body["user"]["name"] == "Test Player"
    token = body["session_token"]

    me = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["email"] == "player@example.com"


def test_sign_in_twice_with_the_same_google_account_reuses_the_user(client, monkeypatch):
    first = _sign_in(client, monkeypatch)
    second = _sign_in(client, monkeypatch)
    assert first["user"]["id"] == second["user"]["id"]


def test_sign_in_with_invalid_google_token_is_401(client, monkeypatch):
    monkeypatch.setenv("SESSION_SECRET", "test-secret-not-for-real-use-padded-to-32-bytes")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "test-client-id.apps.googleusercontent.com")

    def _raise(token):
        raise ValueError("Token expired")

    monkeypatch.setattr("backend.main.verify_google_id_token", _raise)
    response = client.post("/auth/google", json={"id_token": "garbage"})
    assert response.status_code == 401


def test_get_me_401_without_a_token(client):
    assert client.get("/auth/me").status_code == 401


def test_get_me_401_with_a_bogus_token(client):
    response = client.get("/auth/me", headers={"Authorization": "Bearer not-a-real-token"})
    assert response.status_code == 401


def test_create_scenario_signed_in_sets_owner(client, monkeypatch):
    token = _sign_in(client, monkeypatch)["session_token"]
    payload = {
        "name": "My Scenario",
        "factions": [
            {"faction_name": "Rome", "role_preset": "expansionist", "starting_territory": [ROME_HOME]},
            {"faction_name": "Carthage", "role_preset": "warmonger", "starting_territory": [ROME_NEIGHBOR]},
        ],
    }
    response = client.post("/scenarios", json=payload, headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 201
    body = response.json()
    assert body["owner_user_id"] is not None
    assert body["owner_name"] == "Test Player"

    # Ownership survives a fetch, and shows up in the list too.
    fetched = client.get(f"/scenarios/{body['id']}").json()
    assert fetched["owner_name"] == "Test Player"
    listed = client.get("/scenarios").json()
    assert any(s["id"] == body["id"] and s["owner_name"] == "Test Player" for s in listed)


def test_create_scenario_anonymous_has_no_owner(client):
    payload = {
        "name": "Anon Scenario",
        "factions": [
            {"faction_name": "Rome", "role_preset": "expansionist", "starting_territory": [ROME_HOME]},
            {"faction_name": "Carthage", "role_preset": "warmonger", "starting_territory": [ROME_NEIGHBOR]},
        ],
    }
    response = client.post("/scenarios", json=payload)
    assert response.status_code == 201
    body = response.json()
    assert body["owner_user_id"] is None
    assert body["owner_name"] is None


def test_annotation_uses_the_signed_in_users_real_name_not_the_client_payload(client, monkeypatch):
    token = _sign_in(client, monkeypatch)["session_token"]
    game_id = client.post("/games", json={"factions": AD_HOC_FACTIONS, "max_turns": 1}).json()["game_id"]
    event_id = client.get(f"/games/{game_id}/events").json()[0]["id"]

    response = client.post(
        f"/events/{event_id}/annotations",
        json={"rating": 4, "note": "Looks reasonable.", "created_by": "someone else entirely"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 201
    assert response.json()["created_by"] == "Test Player"  # not "someone else entirely"


def test_create_and_fetch_scenario(client):
    payload = {
        "name": "Test Scenario",
        "max_turns": 5,
        "factions": [
            {"faction_name": "Rome", "role_preset": "expansionist", "starting_territory": [ROME_HOME]},
            {"faction_name": "Carthage", "role_preset": "warmonger", "starting_territory": [ROME_NEIGHBOR]},
        ],
    }
    created = client.post("/scenarios", json=payload)
    assert created.status_code == 201
    scenario_id = created.json()["id"]
    assert len(created.json()["factions"]) == 2

    fetched = client.get(f"/scenarios/{scenario_id}")
    assert fetched.status_code == 200
    assert fetched.json()["name"] == "Test Scenario"

    listed = client.get("/scenarios")
    assert len(listed.json()) == 1


def test_create_scenario_rejects_unknown_role_preset(client):
    payload = {
        "name": "Bad Scenario",
        "factions": [{"faction_name": "Rome", "role_preset": "not_a_real_preset", "starting_territory": [ROME_HOME]}],
    }
    response = client.post("/scenarios", json=payload)
    assert response.status_code == 422


def test_get_scenario_404_for_unknown_id(client):
    response = client.get("/scenarios/00000000-0000-0000-0000-000000000000")
    assert response.status_code == 404


def test_start_ad_hoc_game_and_fetch_result(client):
    response = client.post("/games", json={"factions": AD_HOC_FACTIONS, "max_turns": 2})
    assert response.status_code == 202
    game_id = response.json()["game_id"]

    # hub.start runs synchronously in this test suite, so the game has
    # already finished (2 turns of "hold") by the time we get here.
    game = client.get(f"/games/{game_id}").json()
    assert game["status"] == "completed"
    assert game["current_turn"] == 2
    assert game["winner_faction_id"] is None
    assert len(game["faction_states"]) == 2
    assert game["faction_states"][0]["territory"] == [ROME_HOME] or game["faction_states"][0]["territory"] == [ROME_NEIGHBOR]

    events = client.get(f"/games/{game_id}/events").json()
    assert len(events) == 4  # 2 turns x 2 factions
    assert all(e["event_type"] == "hold" for e in events)


def test_start_game_from_scenario(client):
    scenario_payload = {
        "name": "Reusable",
        "max_turns": 1,
        "factions": [
            {"faction_name": "Rome", "role_preset": "expansionist", "starting_territory": [ROME_HOME]},
            {"faction_name": "Carthage", "role_preset": "warmonger", "starting_territory": [ROME_NEIGHBOR]},
        ],
    }
    scenario_id = client.post("/scenarios", json=scenario_payload).json()["id"]

    started = client.post("/games", json={"scenario_id": scenario_id, "max_turns": 1})
    assert started.status_code == 202
    game_id = started.json()["game_id"]

    game = client.get(f"/games/{game_id}").json()
    assert game["scenario_id"] == scenario_id
    assert game["status"] == "completed"

    # Reused the scenario rather than creating a second one.
    assert len(client.get("/scenarios").json()) == 1


def test_start_game_rejects_both_scenario_and_factions(client):
    response = client.post(
        "/games", json={"scenario_id": "00000000-0000-0000-0000-000000000000", "factions": AD_HOC_FACTIONS}
    )
    assert response.status_code == 422


def test_start_game_404_for_unknown_scenario(client):
    response = client.post("/games", json={"scenario_id": "00000000-0000-0000-0000-000000000000"})
    assert response.status_code == 404


def test_start_ad_hoc_game_with_invalid_role_preset_is_422_not_404(client):
    """Previously both this and the "scenario not found" case above raised
    plain ValueError and were caught by one `except ValueError: 404` —
    conflating "bad input" with "not found". This has nothing to do with a
    missing scenario, so it must not come back as 404.
    """
    bad_factions = [
        {**AD_HOC_FACTIONS[0], "role_preset": "not_a_real_preset"},
        AD_HOC_FACTIONS[1],
    ]
    response = client.post("/games", json={"factions": bad_factions, "max_turns": 1})
    assert response.status_code == 422


def test_start_game_rejects_duplicate_home_provinces(client):
    dup_factions = [
        {**AD_HOC_FACTIONS[0], "home_province": ROME_HOME},
        {**AD_HOC_FACTIONS[1], "home_province": ROME_HOME},
    ]
    response = client.post("/games", json={"factions": dup_factions, "max_turns": 1})
    assert response.status_code == 422


def test_start_game_rejects_unreal_province_id(client):
    bad_factions = [{**AD_HOC_FACTIONS[0], "home_province": "not_a_real_id"}, AD_HOC_FACTIONS[1]]
    response = client.post("/games", json={"factions": bad_factions, "max_turns": 1})
    assert response.status_code == 422


def test_start_game_rejects_max_turns_out_of_range(client):
    assert client.post("/games", json={"factions": AD_HOC_FACTIONS, "max_turns": 0}).status_code == 422
    assert client.post("/games", json={"factions": AD_HOC_FACTIONS, "max_turns": 10_000}).status_code == 422


def test_create_scenario_rejects_max_turns_out_of_range(client):
    payload = {
        "name": "Bad turns",
        "max_turns": 10_000,
        "factions": [{"faction_name": "Rome", "starting_territory": [ROME_HOME]}],
    }
    assert client.post("/scenarios", json=payload).status_code == 422


def test_eliminated_faction_reflected_in_get_game(client, monkeypatch):
    """End-to-end check of the is_alive/eliminated_at_turn fix through the
    actual API, not just game/run_game.py directly. The autouse fixture's
    LLM always holds (nothing ever gets eliminated), so this test needs its
    own lopsided setup — same pattern as tests/test_run_game.py's.
    """
    from game.rules import SIEGE_TURNS_TO_DECIDE, pair_key

    def _lopsided_state(faction_configs, max_turns):
        state = graph_module.initial_state_for(faction_configs, max_turns)
        state["factions"]["a"]["units"] = {"legion": 10}
        state["factions"]["b"]["units"] = {"legion": 1}
        state["diplomatic_status"] = {pair_key("a", "b"): "war"}
        state["sieges"] = {
            ROME_NEIGHBOR: {"attacker_id": "a", "progress": SIEGE_TURNS_TO_DECIDE - 1}
        }
        return state

    class _AlwaysInvadeLLM:
        def __init__(self, *a, **k):
            pass

        def invoke(self, prompt):
            return FactionAction(action_type="move_army", target_province=ROME_NEIGHBOR, rationale="test")

    monkeypatch.setattr(run_game_module, "initial_state_for", _lopsided_state)
    monkeypatch.setattr(
        graph_module, "build_llm",
        lambda max_tokens=64, schema=None: (_AlwaysInvadeLLM() if schema is not None else _FakeIntentLLM()),
    )

    response = client.post("/games", json={"factions": AD_HOC_FACTIONS, "max_turns": 10})
    game_id = response.json()["game_id"]

    game = client.get(f"/games/{game_id}").json()
    factions_by_name = {f["faction_name"]: f for f in game["factions"]}
    assert factions_by_name["Rome"]["is_alive"] is True
    assert factions_by_name["Rome"]["eliminated_at_turn"] is None
    assert factions_by_name["Carthage"]["is_alive"] is False
    assert factions_by_name["Carthage"]["eliminated_at_turn"] == 1

    # game.narrative.classify_event, computed at serve time (Stage 11) --
    # the decisive siege battle that just eliminated Carthage is notable;
    # ordinary "hold" events (per-faction upkeep before the invasion) aren't.
    events = client.get(f"/games/{game_id}/events").json()
    decisive = next(e for e in events if "broke the siege" in e["payload"].get("resolution", ""))
    assert decisive["notable"] is True
    assert decisive["headline"] == "siege succeeds — province captured"


def test_declare_war_event_is_flagged_notable(client, monkeypatch):
    class _AlwaysDeclareWarLLM:
        def __init__(self, *a, **k):
            pass

        def invoke(self, prompt):
            return FactionAction(action_type="declare_war", target_faction="b", rationale="test")

    monkeypatch.setattr(
        graph_module, "build_llm",
        lambda max_tokens=64, schema=None: (_AlwaysDeclareWarLLM() if schema is not None else _FakeIntentLLM()),
    )

    response = client.post("/games", json={"factions": AD_HOC_FACTIONS, "max_turns": 1})
    game_id = response.json()["game_id"]

    events = client.get(f"/games/{game_id}/events").json()
    war_events = [e for e in events if e["event_type"] == "declare_war"]
    assert war_events
    assert all(e["notable"] is True and e["headline"] == "war declared" for e in war_events)


def test_failed_game_is_marked_failed_not_stuck_running(client, monkeypatch):
    """Simulates play_game raising (e.g. no LLM key configured) — the game
    must end up FAILED, not stuck at RUNNING forever with no trace.
    """
    monkeypatch.setattr(
        run_game_module, "require_llm_configured",
        lambda: (_ for _ in ()).throw(RuntimeError("no LLM provider configured")),
    )

    response = client.post("/games", json={"factions": AD_HOC_FACTIONS, "max_turns": 1})
    game_id = response.json()["game_id"]

    game = client.get(f"/games/{game_id}").json()
    assert game["status"] == "failed"


def test_list_games(client):
    client.post("/games", json={"factions": AD_HOC_FACTIONS, "max_turns": 1})
    client.post("/games", json={"factions": AD_HOC_FACTIONS, "max_turns": 1})
    assert len(client.get("/games").json()) == 2


def test_evaluate_game_scores_events_and_persists(client):
    game_id = client.post("/games", json={"factions": AD_HOC_FACTIONS, "max_turns": 1}).json()["game_id"]

    response = client.post(f"/games/{game_id}/evaluate")
    assert response.status_code == 200
    results = response.json()
    # 1 turn x 2 factions x 3 metrics ("hold" is always legal and never a
    # wasted economic attempt, and the fake eval model always returns
    # 8.0/10 = 0.8 for Role Alignment)
    assert len(results) == 6
    assert {r["metric_name"] for r in results} == {"Legal Action", "Resource Efficiency", "Role Alignment"}
    assert all(r["metric_name"] != "Legal Action" or r["score"] == 1.0 for r in results)
    assert all(r["metric_name"] != "Resource Efficiency" or r["score"] == 1.0 for r in results)
    assert all(r["metric_name"] != "Role Alignment" or r["score"] == 0.8 for r in results)

    # Scores show up on the event when fetched afterward.
    events = client.get(f"/games/{game_id}/events").json()
    assert all(len(e["eval_scores"]) == 3 for e in events)


def test_evaluate_game_404_for_unknown_game(client):
    response = client.post("/games/00000000-0000-0000-0000-000000000000/evaluate")
    assert response.status_code == 404


def test_get_game_snapshots_returns_every_turn_not_just_the_latest(client):
    # 2 turns so a real second-round snapshot exists per faction.
    game_id = client.post("/games", json={"factions": AD_HOC_FACTIONS, "max_turns": 2}).json()["game_id"]

    response = client.get(f"/games/{game_id}/snapshots")
    assert response.status_code == 200
    snapshots = response.json()

    rome_id = next(f["id"] for f in client.get(f"/games/{game_id}").json()["factions"] if f["faction_name"] == "Rome")
    rome_turns = sorted(s["turn"] for s in snapshots if s["faction_id"] == rome_id)
    assert rome_turns == [1, 2]


def test_get_game_snapshots_404_for_unknown_game(client):
    response = client.get("/games/00000000-0000-0000-0000-000000000000/snapshots")
    assert response.status_code == 404


def test_get_game_diplomacy_returns_only_the_latest_non_neutral_status(client, sqlite_sessionmaker):
    game_id = client.post("/games", json={"factions": AD_HOC_FACTIONS, "max_turns": 1}).json()["game_id"]
    game = client.get(f"/games/{game_id}").json()
    rome_id = next(f["id"] for f in game["factions"] if f["faction_name"] == "Rome")
    carthage_id = next(f["id"] for f in game["factions"] if f["faction_name"] == "Carthage")

    with sqlite_sessionmaker() as session:
        # Two rows for the same pair — the endpoint should return only the
        # one with the higher turn_changed (truce superseded by war).
        session.add_all(
            [
                DiplomaticRelation(
                    game_id=uuid.UUID(game_id), faction_a_id=uuid.UUID(rome_id), faction_b_id=uuid.UUID(carthage_id),
                    status=DiplomaticStatus.TRUCE, turn_changed=1,
                ),
                DiplomaticRelation(
                    game_id=uuid.UUID(game_id), faction_a_id=uuid.UUID(rome_id), faction_b_id=uuid.UUID(carthage_id),
                    status=DiplomaticStatus.WAR, turn_changed=2,
                ),
            ]
        )
        session.commit()

    response = client.get(f"/games/{game_id}/diplomacy")
    assert response.status_code == 200
    relations = response.json()
    assert len(relations) == 1
    assert relations[0]["status"] == "war"
    assert relations[0]["turn_changed"] == 2
    assert {relations[0]["faction_a_name"], relations[0]["faction_b_name"]} == {"Rome", "Carthage"}


def test_get_game_diplomacy_omits_neutral_pairs(client, sqlite_sessionmaker):
    game_id = client.post("/games", json={"factions": AD_HOC_FACTIONS, "max_turns": 1}).json()["game_id"]
    game = client.get(f"/games/{game_id}").json()
    rome_id = game["factions"][0]["id"]
    carthage_id = game["factions"][1]["id"]

    with sqlite_sessionmaker() as session:
        session.add(
            DiplomaticRelation(
                game_id=uuid.UUID(game_id), faction_a_id=uuid.UUID(rome_id), faction_b_id=uuid.UUID(carthage_id),
                status=DiplomaticStatus.NEUTRAL, turn_changed=0,
            )
        )
        session.commit()

    assert client.get(f"/games/{game_id}/diplomacy").json() == []


def test_get_game_diplomacy_404_for_unknown_game(client):
    response = client.get("/games/00000000-0000-0000-0000-000000000000/diplomacy")
    assert response.status_code == 404


def test_role_preset_insights_aggregates_across_games(client, sqlite_sessionmaker):
    # 2 turns so Rome ("expansionist" in AD_HOC_FACTIONS) acts twice,
    # giving two events to score on the same metric — makes the average
    # checkable by hand.
    game_id = client.post("/games", json={"factions": AD_HOC_FACTIONS, "max_turns": 2}).json()["game_id"]
    game = client.get(f"/games/{game_id}").json()
    rome_id = next(f["id"] for f in game["factions"] if f["faction_name"] == "Rome")
    events = client.get(f"/games/{game_id}/events").json()
    rome_event_ids = [e["id"] for e in events if e["faction_id"] == rome_id]
    assert len(rome_event_ids) == 2

    with sqlite_sessionmaker() as session:
        for event_id, score in zip(rome_event_ids, [1.0, 0.5]):
            session.add(
                EvalScore(game_event_id=uuid.UUID(event_id), metric_name="Legal Action", score=score, success=True)
            )
        session.commit()

    response = client.get("/insights/role-presets")
    assert response.status_code == 200
    body = response.json()
    assert body["games_analyzed"] == 1
    row = next(r for r in body["role_presets"] if r["role_preset"] == "expansionist" and r["metric_name"] == "Legal Action")
    assert row["sample_count"] == 2
    assert row["avg_score"] == pytest.approx(0.75)


def test_role_preset_insights_empty_when_nothing_evaluated(client):
    body = client.get("/insights/role-presets").json()
    assert body == {"games_analyzed": 0, "role_presets": []}


def test_create_and_fetch_annotation(client):
    game_id = client.post("/games", json={"factions": AD_HOC_FACTIONS, "max_turns": 1}).json()["game_id"]
    event_id = client.get(f"/games/{game_id}/events").json()[0]["id"]

    response = client.post(
        f"/events/{event_id}/annotations",
        json={"rating": 4, "note": "Reasonable given the context.", "created_by": "reviewer@example.com"},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["rating"] == 4
    assert body["note"] == "Reasonable given the context."

    events = client.get(f"/games/{game_id}/events").json()
    annotated = next(e for e in events if e["id"] == event_id)
    assert len(annotated["annotations"]) == 1
    assert annotated["annotations"][0]["rating"] == 4


def test_create_annotation_requires_rating_or_note(client):
    game_id = client.post("/games", json={"factions": AD_HOC_FACTIONS, "max_turns": 1}).json()["game_id"]
    event_id = client.get(f"/games/{game_id}/events").json()[0]["id"]

    response = client.post(f"/events/{event_id}/annotations", json={"created_by": "reviewer@example.com"})
    assert response.status_code == 422


def test_create_annotation_404_for_unknown_event(client):
    response = client.post(
        "/events/00000000-0000-0000-0000-000000000000/annotations", json={"rating": 3}
    )
    assert response.status_code == 404


def test_annotation_rating_out_of_range_is_422(client):
    game_id = client.post("/games", json={"factions": AD_HOC_FACTIONS, "max_turns": 1}).json()["game_id"]
    event_id = client.get(f"/games/{game_id}/events").json()[0]["id"]

    response = client.post(f"/events/{event_id}/annotations", json={"rating": 6})
    assert response.status_code == 422


def test_websocket_receives_live_updates_then_stream_end(client, monkeypatch):
    """GameHub has no backlog/replay for a subscriber that joins late — a
    synchronous game (the autouse fixture's default, used by every other
    test in this file) would broadcast everything to zero subscribers
    before the test could ever connect, and then hang waiting for a message
    that will never come. So this one test restores the real threaded
    `hub.start` and slows the fake LLM down slightly, giving the main
    thread a realistic window to connect before the game finishes.
    """
    import time
    from backend.game_hub import GameHub

    monkeypatch.setattr(hub, "start", GameHub.start.__get__(hub, GameHub))

    class _SlowHoldLLM:
        def __init__(self, *a, **k):
            pass

        def invoke(self, prompt):
            time.sleep(0.02)
            return FactionAction(action_type="hold", rationale="test")

    monkeypatch.setattr(
        graph_module,
        "build_llm",
        lambda max_tokens=64, schema=None: (_SlowHoldLLM() if schema is not None else _FakeIntentLLM()),
    )

    response = client.post("/games", json={"factions": AD_HOC_FACTIONS, "max_turns": 3})
    game_id = response.json()["game_id"]

    with client.websocket_connect(f"/games/{game_id}/live") as ws:
        messages = []
        while True:
            message = ws.receive_json()
            messages.append(message)
            if message["type"] in ("stream_end", "error"):
                break

    assert messages[-1] == {"type": "stream_end"}
    assert any(m["type"] == "state" for m in messages)


def test_websocket_sends_initial_control_state_snapshot(client):
    game_id = client.post("/games", json={"factions": AD_HOC_FACTIONS, "max_turns": 1}).json()["game_id"]

    with client.websocket_connect(f"/games/{game_id}/live") as ws:
        first = ws.receive_json()

    assert first == {"type": "control_state", "controlled_factions": []}


def test_handle_live_control_message_take_and_release_control():
    from backend.main import _handle_live_control_message

    hub._human_controlled.pop("game-x", None)
    _handle_live_control_message("game-x", {"type": "take_control", "faction_id": "f1"})
    assert hub.is_human_controlled("game-x", "f1") is True

    _handle_live_control_message("game-x", {"type": "release_control", "faction_id": "f1"})
    assert hub.is_human_controlled("game-x", "f1") is False


def test_handle_live_control_message_submit_action_parses_and_queues_it():
    from backend.main import _handle_live_control_message

    _handle_live_control_message(
        "game-y",
        {"type": "submit_action", "faction_id": "f1", "action": {"action_type": "hold", "rationale": "human pick"}},
    )

    action = hub.await_human_action("game-y", "f1", timeout=1)
    assert action.action_type == "hold"
    assert action.rationale == "human pick"


def test_handle_live_control_message_translates_target_faction_to_slug():
    from backend.main import _handle_live_control_message

    hub.register_factions("game-w", {"carthage-uuid": "carthage"})
    _handle_live_control_message(
        "game-w",
        {
            "type": "submit_action",
            "faction_id": "f1",
            "action": {"action_type": "declare_war", "target_faction": "carthage-uuid", "rationale": "test"},
        },
    )

    action = hub.await_human_action("game-w", "f1", timeout=1)
    assert action.target_faction == "carthage"


def test_handle_live_control_message_drops_a_malformed_submission():
    from backend.main import _handle_live_control_message

    # "declare_war" without target_faction fails FactionAction's own
    # validation — this must be silently dropped, not raise, since a
    # malformed client message is not a server error.
    _handle_live_control_message(
        "game-z", {"type": "submit_action", "faction_id": "f1", "action": {"action_type": "not_a_real_type"}}
    )

    assert hub.await_human_action("game-z", "f1", timeout=0.05) is None


def test_human_takes_control_and_their_submitted_action_is_applied(client, monkeypatch):
    """End-to-end over the real WebSocket wire protocol: a human sends
    take_control then submit_action as actual messages (exercising
    _handle_live_control_message's parsing too, not just GameHub directly),
    and the resulting persisted GameEvent reflects exactly what they chose,
    tagged specialist="human" — not whatever the AI would have picked.

    Targets Carthage, not Rome: turn_order is [Rome, Carthage], and the
    human-controlled check for whoever acts *first* happens essentially
    instantly (before any LLM call, slow or not) — there's no natural
    window to get a take_control message there in time. Carthage acts only
    after Rome's own turn (AI-decided here, consuming the slow fake LLM's
    sleep) has fully elapsed, which is exactly the margin this test needs.
    A first version of this test targeted Rome and was consistently too
    late, not flaky — caught by a real assertion failure, not a hang.

    Real-threading/slow-LLM technique borrowed from
    test_websocket_receives_live_updates_then_stream_end, with the
    websocket opened immediately after the game starts (before the extra
    GET-for-faction-id round trip) — GameHub keeps no backlog for a
    subscriber or control message that arrives after a fast game has
    already finished (an earlier version raced exactly that and hung; see
    _forward_broadcasts' bounded-poll fix in backend/main.py, a real bug
    that test surfaced, not just a test issue).
    """
    import time
    from backend.game_hub import GameHub

    monkeypatch.setattr(hub, "start", GameHub.start.__get__(hub, GameHub))

    class _SlowHoldLLM:
        def __init__(self, *a, **k):
            pass

        def invoke(self, prompt):
            time.sleep(0.3)
            return FactionAction(action_type="hold", rationale="AI would have picked this")

    monkeypatch.setattr(
        graph_module,
        "build_llm",
        lambda max_tokens=64, schema=None: (_SlowHoldLLM() if schema is not None else _FakeIntentLLM()),
    )

    game_id = client.post("/games", json={"factions": AD_HOC_FACTIONS, "max_turns": 1}).json()["game_id"]

    with client.websocket_connect(f"/games/{game_id}/live") as ws:
        control_state = ws.receive_json()
        assert control_state == {"type": "control_state", "controlled_factions": []}

        carthage_id = next(
            f["id"] for f in client.get(f"/games/{game_id}").json()["factions"] if f["faction_name"] == "Carthage"
        )
        ws.send_json({"type": "take_control", "faction_id": carthage_id})
        ws.send_json(
            {
                "type": "submit_action",
                "faction_id": carthage_id,
                "action": {"action_type": "hold", "rationale": "the human chose this"},
            }
        )

        messages = []
        while True:
            message = ws.receive_json()
            messages.append(message)
            if message["type"] in ("stream_end", "error"):
                break

    assert any(
        m == {"type": "control_changed", "faction_id": carthage_id, "human_controlled": True} for m in messages
    )

    events = client.get(f"/games/{game_id}/events").json()
    carthage_event = next(e for e in events if e["faction_id"] == carthage_id)
    assert carthage_event["payload"]["rationale"] == "the human chose this"
    assert carthage_event["payload"]["specialist"] == "human"
