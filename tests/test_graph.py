from langgraph.graph import END

from agents import graph as graph_module
from agents.actions import FactionAction, MilitaryAction
from agents.graph import (
    _dispatch_specialist,
    _sanitize_action,
    build_graph,
    faction_turn,
    route_after_turn,
)
from agents.state import FactionState, GameState
from game.rules import pair_key

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
        "sieges": {},
        "capitals": {"rome": ROME_HOME, "carthage": CARTHAGE_HOME},
        "province_captured_turn": {},
        "province_development": {},
        "trade_agreements": {},
        "rebellion_seed": 42,
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


class _FakeSpecialistLLM:
    """Stands in for a specialist's structured-output call, returning an
    instance of whichever schema `build_llm` was actually bound to — a real
    specialist call only ever gets back an instance of the schema it was
    given, so a schema-blind fake could mask a dispatch/schema mismatch.
    Always proposes moving into ROME_NEIGHBOR for MilitaryAction — legal for
    Rome, illegal for Carthage, which is what the sanitization test below
    relies on; a harmless hold for the other two schemas.
    """

    def __init__(self, schema, *args, **kwargs):
        self._schema = schema

    def invoke(self, prompt: str):
        if self._schema is MilitaryAction:
            return MilitaryAction(action_type="move_army", target_province=ROME_NEIGHBOR, rationale="testing")
        return self._schema(action_type="hold", rationale="testing")


def _fake_build_llm(max_tokens: int = 64, schema=None):
    return _FakeIntentLLM() if schema is None else _FakeSpecialistLLM(schema)


def _force_specialist(monkeypatch, domain: str) -> None:
    """Pin _dispatch_specialist to a fixed domain, decoupling a test from
    the role-preset rotation formula when the test is really about
    something else (intent refresh, sanitization)."""
    monkeypatch.setattr(graph_module, "_dispatch_specialist", lambda state, faction_id: domain)


def test_faction_turn_refreshes_intent_and_applies_legal_action(monkeypatch):
    monkeypatch.setattr(graph_module, "build_llm", _fake_build_llm)
    _force_specialist(monkeypatch, "military")
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
    assert "Turn 1 — Rome [military] (move_army)" in update["log"][0]


def test_faction_turn_sanitizes_illegal_move_to_hold(monkeypatch):
    monkeypatch.setattr(graph_module, "build_llm", _fake_build_llm)
    _force_specialist(monkeypatch, "military")
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


def test_sanitize_action_downgrades_trade_proposal_missing_offer_details():
    state = _state(
        {"rome": _faction("rome", "Rome"), "carthage": _faction("carthage", "Carthage")},
        {ROME_HOME: "rome", CARTHAGE_HOME: "carthage"},
    )
    action = FactionAction(
        action_type="negotiate", target_faction="carthage", proposal="trade", rationale="testing"
    )  # no offer_resource/offer_amount

    sanitized = _sanitize_action(state, "rome", action, move_targets=[])

    assert sanitized.action_type == "hold"
    assert "sanitized to hold" in sanitized.rationale


def test_sanitize_action_allows_a_complete_trade_proposal():
    state = _state(
        {"rome": _faction("rome", "Rome"), "carthage": _faction("carthage", "Carthage")},
        {ROME_HOME: "rome", CARTHAGE_HOME: "carthage"},
    )
    action = FactionAction(
        action_type="negotiate", target_faction="carthage", proposal="trade",
        offer_resource="gold", offer_amount=5, rationale="testing",
    )

    sanitized = _sanitize_action(state, "rome", action, move_targets=[])

    assert sanitized.action_type == "negotiate"
    assert sanitized.offer_amount == 5


def test_sanitize_action_downgrades_new_tribute_proposal_missing_offer_details():
    state = _state(
        {"rome": _faction("rome", "Rome"), "carthage": _faction("carthage", "Carthage")},
        {ROME_HOME: "rome", CARTHAGE_HOME: "carthage"},
    )
    action = FactionAction(
        action_type="negotiate", target_faction="carthage", proposal="tribute", rationale="testing"
    )  # no incoming tribute offer, no offer_resource/offer_amount of its own

    sanitized = _sanitize_action(state, "rome", action, move_targets=[])

    assert sanitized.action_type == "hold"


def test_sanitize_action_allows_accepting_tribute_with_no_offer_details():
    """Accepting an existing tribute offer needs no offer_resource/
    offer_amount of the accepter's own -- it cashes in the incoming one."""
    state = _state(
        {"rome": _faction("rome", "Rome"), "carthage": _faction("carthage", "Carthage")},
        {ROME_HOME: "rome", CARTHAGE_HOME: "carthage"},
        pending_proposals={"carthage->rome": "tribute:gold:15:none"},
    )
    action = FactionAction(
        action_type="negotiate", target_faction="carthage", proposal="tribute", rationale="testing"
    )

    sanitized = _sanitize_action(state, "rome", action, move_targets=[])

    assert sanitized.action_type == "negotiate"


def test_dispatch_specialist_prioritizes_a_siege_in_progress():
    """A siege lapses if not pressed every one of the attacker's own turns
    (Stage 4) -- the dispatcher must never let the rotation override this."""
    state = _state(
        {"rome": _faction("rome", "Rome", role_preset="diplomat_trader")},
        {ROME_HOME: "rome"},
        sieges={ROME_NEIGHBOR: {"attacker_id": "rome", "progress": 1}},
        turn=1,  # would otherwise rotate away from military for this role
    )
    assert _dispatch_specialist(state, "rome") == "military"


def test_dispatch_specialist_prioritizes_an_incoming_proposal():
    state = _state(
        {"rome": _faction("rome", "Rome", role_preset="warmonger")},
        {ROME_HOME: "rome"},
        pending_proposals={"carthage->rome": "truce"},
        turn=0,  # warmonger's round-1 rotation slot is "military", not diplomatic
    )
    assert _dispatch_specialist(state, "rome") == "diplomatic"


def test_dispatch_specialist_prioritizes_an_allys_call_to_arms():
    state = _state(
        {
            "rome": _faction("rome", "Rome", role_preset="warmonger"),
            "carthage": _faction("carthage", "Carthage", units={"legion": 2}),
            "gaul": _faction("gaul", "Gaul", units={"legion": 30}),
        },
        {ROME_HOME: "rome"},
        diplomatic_status={
            pair_key("carthage", "rome"): "alliance",
            pair_key("carthage", "gaul"): "war",
        },
        turn=0,  # warmonger's round-1 rotation slot is "military", not diplomatic
    )
    assert _dispatch_specialist(state, "rome") == "diplomatic"


def test_dispatch_specialist_ignores_another_factions_pending_proposal():
    state = _state(
        {"rome": _faction("rome", "Rome", role_preset="warmonger")},
        {ROME_HOME: "rome"},
        pending_proposals={"rome->carthage": "truce"},  # outgoing, not incoming
        turn=0,
    )
    assert _dispatch_specialist(state, "rome") == "military"


def test_dispatch_specialist_rotates_through_the_role_presets_full_order():
    state = _state({"rome": _faction("rome", "Rome", role_preset="isolationist")}, {ROME_HOME: "rome"})
    # isolationist's order is ["economic", "military", "diplomatic"].
    domains = []
    for turn in range(3):
        domains.append(_dispatch_specialist({**state, "turn": turn}, "rome"))
    assert domains == ["economic", "military", "diplomatic"]


def test_dispatch_specialist_falls_back_to_custom_order_for_an_unknown_role():
    state = _state({"rome": _faction("rome", "Rome", role_preset="something_new")}, {ROME_HOME: "rome"})
    assert _dispatch_specialist(state, "rome") == "military"  # custom's round-1 slot


def test_decide_action_converts_each_specialist_schema_to_a_faction_action(monkeypatch):
    monkeypatch.setattr(graph_module, "build_llm", _fake_build_llm)
    state = _state(
        {"rome": _faction("rome", "Rome"), "carthage": _faction("carthage", "Carthage")},
        {ROME_HOME: "rome", CARTHAGE_HOME: "carthage"},
    )

    for domain, expected_action_type in [
        ("military", "move_army"),
        ("economic", "hold"),
        ("diplomatic", "hold"),
    ]:
        _force_specialist(monkeypatch, domain)
        action, chosen = graph_module._decide_action(state, "rome", move_targets=[ROME_NEIGHBOR])
        assert chosen == domain
        assert isinstance(action, FactionAction)
        assert action.action_type == expected_action_type


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
