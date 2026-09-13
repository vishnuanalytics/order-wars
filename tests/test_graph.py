from langgraph.graph import END

from agents import graph as graph_module
from agents.actions import FactionAction
from agents.graph import _sanitize_action, build_graph, faction_turn, route_after_turn
from agents.state import FactionState, GameState

ROME_HOME = "831e80fffffffff"  # Italy 20
ROME_NEIGHBOR = "831e81fffffffff"  # Italy 19, adjacent to ROME_HOME
CARTHAGE_HOME = "83386efffffffff"  # Tunisia 2, not adjacent to ROME_HOME


def _faction(faction_id: str, name: str, role_preset: str = "custom", **overrides) -> FactionState:
    base: FactionState = {
        "faction_id": faction_id,
        "name": name,
        "role_preset": role_preset,
        "intent": None,
        "last_action": None,
        "resources": {"gold": 20},
        "units": {"legion": 2},
    }
    base.update(overrides)
    return base


def _state(factions: dict[str, FactionState], province_owner: dict[str, str], **overrides) -> GameState:
    base: GameState = {
        "turn": 0,
        "max_turns": 2,
        "turn_order": list(factions.keys()),
        "active_faction_idx": 0,
        "factions": factions,
        "province_owner": province_owner,
        "diplomatic_status": {},
        "pending_proposals": {},
        "last_event": None,
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
    """Stands in for the executor-layer structured-output call. Always
    proposes moving into ROME_NEIGHBOR — legal for Rome, illegal for
    Carthage, which is what the sanitization tests below rely on.
    """

    def __init__(self, *args, **kwargs):
        pass

    def invoke(self, prompt: str) -> FactionAction:
        return FactionAction(action_type="move_army", target_province=ROME_NEIGHBOR, rationale="testing")


def _fake_build_llm(max_tokens: int = 64, schema=None):
    return _FakeActionLLM() if schema is not None else _FakeIntentLLM()


def test_faction_turn_refreshes_intent_and_applies_legal_action(monkeypatch):
    monkeypatch.setattr(graph_module, "build_llm", _fake_build_llm)
    state = _state(
        {"rome": _faction("rome", "Rome"), "carthage": _faction("carthage", "Carthage")},
        {ROME_HOME: "rome", CARTHAGE_HOME: "carthage"},
    )

    update = faction_turn(state)

    assert update["active_faction_idx"] == 1
    assert update["turn"] == 0  # round not complete yet
    rome = update["factions"]["rome"]
    assert rome["intent"] == "expand toward the coast"
    assert rome["last_action"]["action_type"] == "move_army"
    assert update["province_owner"][ROME_NEIGHBOR] == "rome"  # legal move, captured
    assert "Turn 1 — Rome (move_army)" in update["log"][0]


def test_faction_turn_sanitizes_illegal_move_to_hold(monkeypatch):
    monkeypatch.setattr(graph_module, "build_llm", _fake_build_llm)
    # Carthage's home isn't adjacent to ROME_NEIGHBOR, so the fake LLM's
    # proposed move is illegal for Carthage and must be sanitized to hold.
    state = _state(
        {"rome": _faction("rome", "Rome"), "carthage": _faction("carthage", "Carthage")},
        {ROME_HOME: "rome", CARTHAGE_HOME: "carthage"},
        active_faction_idx=1,
    )

    update = faction_turn(state)

    carthage = update["factions"]["carthage"]
    assert carthage["last_action"]["action_type"] == "hold"
    assert "sanitized to hold" in carthage["last_action"]["rationale"]
    assert ROME_NEIGHBOR not in update["province_owner"]  # never applied


def test_sanitize_action_allows_a_known_unit_type():
    state = _state(
        {"rome": _faction("rome", "Rome"), "carthage": _faction("carthage", "Carthage")},
        {ROME_HOME: "rome", CARTHAGE_HOME: "carthage"},
    )
    action = FactionAction(action_type="build_unit", unit_type="cavalry", rationale="testing")

    sanitized = _sanitize_action(state, "rome", action, move_targets=[])

    assert sanitized.action_type == "build_unit"
    assert sanitized.unit_type == "cavalry"


def test_faction_turn_skips_intent_refresh_when_not_due(monkeypatch):
    monkeypatch.setattr(graph_module, "build_llm", _fake_build_llm)
    state = _state(
        {"rome": _faction("rome", "Rome", intent="hold the border"), "carthage": _faction("carthage", "Carthage")},
        {ROME_HOME: "rome", CARTHAGE_HOME: "carthage"},
        turn=1,  # round_number=2, not a refresh turn (interval=3)
    )

    update = faction_turn(state)

    assert update["factions"]["rome"]["intent"] == "hold the border"


def test_faction_turn_wraps_index_and_increments_turn(monkeypatch):
    monkeypatch.setattr(graph_module, "build_llm", _fake_build_llm)
    state = _state(
        {"rome": _faction("rome", "Rome"), "carthage": _faction("carthage", "Carthage")},
        {ROME_HOME: "rome", CARTHAGE_HOME: "carthage"},
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
        {"rome": _faction("rome", "Rome"), "carthage": _faction("carthage", "Carthage")},
        {ROME_HOME: "rome", CARTHAGE_HOME: "carthage"},
        max_turns=2,
    )

    result = graph.invoke(initial_state, {"recursion_limit": 20})

    assert result["turn"] == 2
    assert len(result["log"]) == 4  # 2 factions x 2 turns
    # Rome's move is legal every turn (income doesn't change adjacency); it
    # already captured ROME_NEIGHBOR by turn 1, so later turns are a
    # peaceful reinforcement (still "now held"), never sanitized to hold.
    assert result["province_owner"][ROME_NEIGHBOR] == "rome"
