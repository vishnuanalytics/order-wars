"""backend/main.py against in-memory SQLite (via the app's dependency
override), with the game loop's LLM calls mocked and GameHub.start patched
to run synchronously — no live LLM/Neon calls, and no waiting on background
threads to observe results deterministically.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from agents import graph as graph_module
from agents.actions import FactionAction
from backend.game_hub import hub
from backend.main import _default_session_factory, app
from db.models import Base
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


@pytest.fixture(autouse=True)
def _no_real_llm_or_thread(monkeypatch):
    monkeypatch.setattr(
        graph_module,
        "build_llm",
        lambda max_tokens=64, schema=None: (_HoldLLM() if schema is not None else _FakeIntentLLM()),
    )
    monkeypatch.setattr(run_game_module, "require_llm_configured", lambda: None)
    # Run the "background" game synchronously so the test can assert on its
    # result immediately after the POST /games response, no polling/sleeping.
    monkeypatch.setattr(hub, "start", lambda game_id, target: target())


AD_HOC_FACTIONS = [
    {"faction_id": "a", "name": "Rome", "role_preset": "expansionist", "home_province": ROME_HOME},
    {"faction_id": "b", "name": "Carthage", "role_preset": "warmonger", "home_province": ROME_NEIGHBOR},
]


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_get_provinces_returns_real_geojson(client):
    response = client.get("/map/provinces")
    assert response.status_code == 200
    body = response.json()
    assert body["type"] == "FeatureCollection"
    assert len(body["features"]) > 0


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


def test_list_games(client):
    client.post("/games", json={"factions": AD_HOC_FACTIONS, "max_turns": 1})
    client.post("/games", json={"factions": AD_HOC_FACTIONS, "max_turns": 1})
    assert len(client.get("/games").json()) == 2


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
