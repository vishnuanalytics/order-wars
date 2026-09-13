"""Drives real game.rules.resolve_action calls (reusing tests/test_rules.py's
fixtures) rather than fabricated payloads wherever possible — the whole
point of these tests is to catch it if game/rules.py's resolution wording
ever drifts out of sync with game/narrative.py's markers (see that
module's docstring).
"""

from game.narrative import classify_event
from game.rules import pair_key, resolve_action
from tests.test_rules import FAR_AWAY, HOME, NEIGHBOR, _action, _besiege, _faction, _state


def test_hold_with_no_side_effects_is_not_notable():
    state = _state()
    result = resolve_action(state, "rome", _action("hold"))
    tag = classify_event("hold", {"resolution": result["resolution"]})
    assert tag.notable is False


def test_declare_war_is_notable():
    state = _state()
    result = resolve_action(state, "rome", _action("declare_war", target_faction="carthage"))
    tag = classify_event("declare_war", {"resolution": result["resolution"]})
    assert tag.notable is True
    assert tag.headline == "war declared"


def test_siege_begins_is_notable():
    state = _state(
        province_owner={HOME: "rome", NEIGHBOR: "carthage"},
        diplomatic_status={pair_key("rome", "carthage"): "war"},
    )
    result = resolve_action(state, "rome", _action("move_army", target_province=NEIGHBOR))
    tag = classify_event("move_army", {"resolution": result["resolution"]})
    assert tag.notable is True
    assert tag.headline == "siege begins"


def test_siege_pressed_is_notable():
    """Unreachable via real gameplay under the current SIEGE_TURNS_TO_DECIDE
    (2) — a second press is already the decisive battle, not an
    intermediate "pressed" turn (see game/rules.py) — but the marker exists
    for when that constant is ever tuned up, so this checks it directly
    rather than skipping it.
    """
    tag = classify_event("move_army", {"resolution": f"pressed the siege of {NEIGHBOR}"})
    assert tag.notable is True
    assert tag.headline == "siege continues"


def test_siege_broken_is_notable():
    state = _state(
        province_owner={HOME: "rome", NEIGHBOR: "carthage"},
        diplomatic_status={pair_key("rome", "carthage"): "war"},
        factions={
            "rome": _faction("rome", "Rome", legions=5),
            "carthage": _faction("carthage", "Carthage", legions=1),
        },
    )
    result = _besiege(state, "rome", NEIGHBOR)
    tag = classify_event("move_army", {"resolution": result["resolution"]})
    assert tag.notable is True
    assert tag.headline == "siege succeeds — province captured"


def test_siege_lost_is_notable():
    state = _state(
        province_owner={HOME: "rome", NEIGHBOR: "carthage"},
        diplomatic_status={pair_key("rome", "carthage"): "war"},
        factions={
            "rome": _faction("rome", "Rome", legions=1),
            "carthage": _faction("carthage", "Carthage", legions=5),
        },
    )
    result = _besiege(state, "rome", NEIGHBOR)
    tag = classify_event("move_army", {"resolution": result["resolution"]})
    assert tag.notable is True
    assert tag.headline == "siege fails"


def test_rebellion_is_notable():
    # Real, computed values from test_rules.py: seed=3 rolls 0.1006 for
    # FAR_AWAY at turn=1, below the 15% rebellion threshold.
    state = _state(
        province_owner={HOME: "rome", FAR_AWAY: "rome"},
        capitals={"rome": HOME},
        province_captured_turn={FAR_AWAY: 0},
        rebellion_seed=3,
        turn=1,
    )
    result = resolve_action(state, "rome", _action("hold"))
    tag = classify_event("hold", {"resolution": result["resolution"]})
    assert tag.notable is True
    assert tag.headline == "rebellion"


def test_call_to_arms_is_notable():
    state = _state(
        factions={
            "rome": _faction("rome", "Rome"),
            "carthage": _faction("carthage", "Carthage", legions=2),
            "gaul": _faction("gaul", "Gaul", legions=30),
        },
        turn_order=["rome", "carthage", "gaul"],
        diplomatic_status={
            pair_key("rome", "carthage"): "alliance",
            pair_key("carthage", "gaul"): "war",
        },
    )
    result = resolve_action(state, "rome", _action("hold"))
    tag = classify_event("hold", {"resolution": result["resolution"]})
    assert tag.notable is True
    assert tag.headline == "call to arms"


def test_trade_agreed_is_notable():
    state = _state(pending_proposals={"carthage->rome": "trade:grain:8"})
    result = resolve_action(
        state, "rome",
        _action("negotiate", target_faction="carthage", proposal="trade", offer_resource="gold", offer_amount=5),
    )
    tag = classify_event("negotiate", {"resolution": result["resolution"]})
    assert tag.notable is True
    assert tag.headline == "trade agreement reached"


def test_alliance_agreed_is_notable():
    state = _state(pending_proposals={"carthage->rome": "alliance"})
    result = resolve_action(state, "rome", _action("negotiate", target_faction="carthage", proposal="alliance"))
    tag = classify_event("negotiate", {"resolution": result["resolution"]})
    assert tag.notable is True
    assert tag.headline == "alliance formed"


def test_truce_agreed_is_notable():
    state = _state(pending_proposals={"carthage->rome": "truce"})
    result = resolve_action(state, "rome", _action("negotiate", target_faction="carthage", proposal="truce"))
    tag = classify_event("negotiate", {"resolution": result["resolution"]})
    assert tag.notable is True
    assert tag.headline == "truce declared"


def test_negotiate_proposal_not_yet_agreed_is_not_notable():
    """A one-sided proposal (nobody has accepted yet) isn't a narrative
    moment -- only the agreement is."""
    state = _state()
    result = resolve_action(state, "rome", _action("negotiate", target_faction="carthage", proposal="truce"))
    tag = classify_event("negotiate", {"resolution": result["resolution"]})
    assert tag.notable is False


def test_tribute_accepted_is_notable():
    state = _state(
        pending_proposals={"carthage->rome": "tribute:gold:15:none"},
        diplomatic_status={pair_key("rome", "carthage"): "war"},
    )
    result = resolve_action(state, "rome", _action("negotiate", target_faction="carthage", proposal="tribute"))
    tag = classify_event("negotiate", {"resolution": result["resolution"]})
    assert tag.notable is True
    assert tag.headline == "tribute accepted — peace"


def test_classify_event_handles_a_missing_payload():
    tag = classify_event("hold", None)
    assert tag.notable is False
