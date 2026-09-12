from langgraph.graph import END

from agents import graph as graph_module
from agents.graph import build_graph, route_after_decision, start_turn


class _FakeResponse:
    def __init__(self, content: str):
        self.content = content


class _FakeLLM:
    """Stands in for ChatAnthropic so tests never hit the real API."""

    def __init__(self, *args, **kwargs):
        pass

    def invoke(self, prompt: str) -> _FakeResponse:
        return _FakeResponse("hold the border")


def test_start_turn_increments_and_logs():
    state = {"turn": 0, "max_turns": 3, "log": [], "last_decision": None}
    update = start_turn(state)
    assert update["turn"] == 1
    assert update["log"] == ["Turn 1 started"]


def test_route_after_decision_loops_until_max_turns():
    assert route_after_decision({"turn": 1, "max_turns": 3}) == "start_turn"
    assert route_after_decision({"turn": 3, "max_turns": 3}) == END


def test_full_graph_runs_to_completion(monkeypatch):
    monkeypatch.setattr(graph_module, "build_llm", lambda **kwargs: _FakeLLM())

    graph = build_graph()
    result = graph.invoke(
        {"turn": 0, "max_turns": 2, "log": [], "last_decision": None}
    )

    assert result["turn"] == 2
    assert result["last_decision"] == "hold the border"
    assert result["log"] == [
        "Turn 1 started",
        "Turn 1 decision: hold the border",
        "Turn 2 started",
        "Turn 2 decision: hold the border",
    ]
