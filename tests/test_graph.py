from langgraph.graph import END

from agents import graph as graph_module
from agents.actions import FactionAction
from agents.graph import build_graph, faction_turn, route_after_turn
from agents.state import FactionState, GameState


def _faction(faction_id: str, name: str, role_preset: str = "custom", **overrides) -> FactionState:
    base: FactionState = {
        "faction_id": faction_id,
        "name": name,
        "role_preset": role_preset,
        "intent": None,
        "last_action": None,
    }
    base.update(overrides)
    return base


def _state(factions: dict[str, FactionState], **overrides) -> GameState:
    base: GameState = {
        "turn": 0,
        "max_turns": 2,
        "turn_order": list(factions.keys()),
        "active_faction_idx": 0,
        "factions": factions,
        "log": [],
    }
    base.update(overrides)
    return base


class _FakeIntentResponse:
    def __init__(self, content: str):
        self.content = content


class _FakeIntentLLM:
    """Stands in for the leader-layer call so tests never hit a live API."""

    def __init__(self, *args, **kwargs):
        pass

    def invoke(self, prompt: str) -> _FakeIntentResponse:
        return _FakeIntentResponse("expand toward the coast")


class _FakeActionLLM:
    """Stands in for the executor-layer structured-output call."""

    def __init__(self, *args, **kwargs):
        pass

    def invoke(self, prompt: str) -> FactionAction:
        return FactionAction(action_type="expand", target_faction=None, rationale="testing")


def _fake_build_llm(max_tokens: int = 64, schema=None):
    return _FakeActionLLM() if schema is not None else _FakeIntentLLM()


def test_faction_turn_refreshes_intent_and_records_action(monkeypatch):
    monkeypatch.setattr(graph_module, "build_llm", _fake_build_llm)
    state = _state({"rome": _faction("rome", "Rome"), "carthage": _faction("carthage", "Carthage")})

    update = faction_turn(state)

    assert update["active_faction_idx"] == 1
    assert update["turn"] == 0  # round not complete yet — only one faction has acted
    rome = update["factions"]["rome"]
    assert rome["intent"] == "expand toward the coast"
    assert rome["last_action"] == {
        "action_type": "expand",
        "target_faction": None,
        "rationale": "testing",
    }
    assert update["log"] == ["Turn 1 — Rome: expand (testing)"]


def test_faction_turn_skips_intent_refresh_when_not_due(monkeypatch):
    monkeypatch.setattr(graph_module, "build_llm", _fake_build_llm)
    # turn=1 -> round_number=2, not a refresh turn (interval=3); existing
    # intent should be preserved rather than overwritten.
    state = _state(
        {"rome": _faction("rome", "Rome", intent="hold the border"), "carthage": _faction("carthage", "Carthage")},
        turn=1,
    )

    update = faction_turn(state)

    assert update["factions"]["rome"]["intent"] == "hold the border"


def test_faction_turn_wraps_index_and_increments_turn(monkeypatch):
    monkeypatch.setattr(graph_module, "build_llm", _fake_build_llm)
    state = _state(
        {"rome": _faction("rome", "Rome"), "carthage": _faction("carthage", "Carthage")},
        active_faction_idx=1,
    )

    update = faction_turn(state)

    assert update["active_faction_idx"] == 0
    assert update["turn"] == 1


def test_route_after_turn_loops_until_max_turns():
    assert route_after_turn({"turn": 1, "max_turns": 3}) == "faction_turn"
    assert route_after_turn({"turn": 3, "max_turns": 3}) == END


def test_full_graph_runs_n_factions_to_completion(monkeypatch):
    monkeypatch.setattr(graph_module, "build_llm", _fake_build_llm)

    graph = build_graph()
    initial_state = _state(
        {
            "rome": _faction("rome", "Rome"),
            "carthage": _faction("carthage", "Carthage"),
            "gaul": _faction("gaul", "Gaul"),
        },
        max_turns=2,
    )

    result = graph.invoke(initial_state, {"recursion_limit": 20})

    assert result["turn"] == 2
    assert len(result["log"]) == 6  # 3 factions x 2 turns
    for faction in result["factions"].values():
        assert faction["last_action"]["action_type"] == "expand"
        assert faction["intent"] == "expand toward the coast"
