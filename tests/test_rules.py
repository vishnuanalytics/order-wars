"""game/rules.py is pure and LLM-free, so these hit it directly with no
mocking — real province ids from the committed map, real adjacency.
"""

from typing import get_args

from agents.actions import UnitType
from agents.state import FactionState, GameState
from game.rules import (
    COUNTERS,
    UNIT_COSTS,
    diplomatic_status_between,
    pair_key,
    resolve_action,
    territory_of,
)

HOME = "831e80fffffffff"  # Italy 20, terrain=plains -> income yields grain
NEIGHBOR = "831e81fffffffff"  # Italy 19, adjacent to HOME
FAR_AWAY = "83386efffffffff"  # Tunisia 2, not adjacent to HOME


def _faction(
    faction_id: str,
    name: str,
    gold: int = 20,
    iron: int = 20,
    grain: int = 0,
    legions: int = 2,
    units: dict[str, int] | None = None,
) -> FactionState:
    return {
        "faction_id": faction_id,
        "name": name,
        "role_preset": "custom",
        "intent": None,
        "last_action": None,
        "resources": {"gold": gold, "iron": iron, "grain": grain},
        "units": units if units is not None else {"legion": legions},
    }


def _state(**overrides) -> GameState:
    base: GameState = {
        "turn": 0,
        "max_turns": 5,
        "turn_order": ["rome", "carthage"],
        "active_faction_idx": 0,
        "factions": {
            "rome": _faction("rome", "Rome"),
            "carthage": _faction("carthage", "Carthage"),
        },
        "province_owner": {HOME: "rome"},
        "diplomatic_status": {},
        "pending_proposals": {},
        "log": [],
    }
    base.update(overrides)
    return base


def _action(action_type, **kwargs):
    from agents.actions import FactionAction

    return FactionAction(action_type=action_type, rationale="test", **kwargs)


def test_income_applied_every_turn_regardless_of_action():
    state = _state()
    result = resolve_action(state, "rome", _action("hold"))
    # HOME (Italy 20) is a "plains" province -> income yields grain, not
    # gold; gold/iron are untouched since Rome owns no coastal/hills province.
    assert result["factions"]["rome"]["resources"]["grain"] == 1  # 0 + 1/province
    assert result["factions"]["rome"]["resources"]["gold"] == 20  # unchanged


def test_income_yields_resource_matching_each_owned_provinces_terrain():
    coastal = NEIGHBOR  # Italy 19, terrain=coastal -> gold
    hills = "831eebfffffffff"  # Bulgaria 6, terrain=hills -> iron
    state = _state(province_owner={HOME: "rome", coastal: "rome", hills: "rome"})
    result = resolve_action(state, "rome", _action("hold"))
    resources = result["factions"]["rome"]["resources"]
    assert resources["grain"] == 1  # HOME, plains
    assert resources["gold"] == 21  # 20 starting + 1 from the coastal province
    assert resources["iron"] == 21  # 20 starting + 1 from the hills province


def test_move_army_into_unclaimed_province_captures_it():
    state = _state()
    result = resolve_action(state, "rome", _action("move_army", target_province=NEIGHBOR))
    assert result["province_owner"][NEIGHBOR] == "rome"
    assert "now held" in result["resolution"]


def test_move_army_into_own_province_is_a_safe_no_op():
    state = _state()
    result = resolve_action(state, "rome", _action("move_army", target_province=HOME))
    assert result["province_owner"][HOME] == "rome"


def test_move_army_into_enemy_territory_without_war_is_rejected():
    state = _state(province_owner={HOME: "rome", NEIGHBOR: "carthage"})
    result = resolve_action(state, "rome", _action("move_army", target_province=NEIGHBOR))
    assert result["province_owner"][NEIGHBOR] == "carthage"  # unchanged
    assert "without being at war" in result["resolution"]


def test_move_army_into_enemy_territory_at_war_stronger_attacker_wins():
    state = _state(
        province_owner={HOME: "rome", NEIGHBOR: "carthage"},
        diplomatic_status={pair_key("rome", "carthage"): "war"},
        factions={
            "rome": _faction("rome", "Rome", legions=5),
            "carthage": _faction("carthage", "Carthage", legions=1),
        },
    )
    result = resolve_action(state, "rome", _action("move_army", target_province=NEIGHBOR))
    assert result["province_owner"][NEIGHBOR] == "rome"
    assert result["factions"]["carthage"]["units"]["legion"] < 1 or result["factions"]["carthage"]["units"]["legion"] == 0
    assert result["factions"]["rome"]["units"]["legion"] < 5  # winner still takes some attrition


def test_move_army_into_enemy_territory_at_war_weaker_attacker_loses():
    state = _state(
        province_owner={HOME: "rome", NEIGHBOR: "carthage"},
        diplomatic_status={pair_key("rome", "carthage"): "war"},
        factions={
            "rome": _faction("rome", "Rome", legions=1),
            "carthage": _faction("carthage", "Carthage", legions=5),
        },
    )
    result = resolve_action(state, "rome", _action("move_army", target_province=NEIGHBOR))
    assert result["province_owner"][NEIGHBOR] == "carthage"  # attacker failed to capture
    assert result["factions"]["rome"]["units"]["legion"] == 0  # loser attrition


def test_unit_type_literal_matches_unit_costs_and_counters():
    """agents.actions.UnitType is a closed Pydantic Literal -- an LLM can
    never actually produce a unit_type outside it (Pydantic rejects that at
    parse time, unlike the free-text target_province/target_faction fields
    _sanitize_action has to repair). The real risk is these three staying
    in sync by hand as unit types get added; this guards that, since a
    silent drift wouldn't be caught by Pydantic at all.
    """
    unit_types = set(get_args(UnitType))
    assert unit_types == set(UNIT_COSTS.keys())
    assert unit_types == set(COUNTERS.keys())


def test_cavalry_beats_a_larger_legion_force_via_counter_bonus():
    """4 cavalry (attacker) vs 5 legion (defender): fewer raw units, but
    cavalry counters legion for a +50% effective-strength bonus (4 * 1.5 =
    6.0 > 5.0) -- the whole point of unit composition mattering, not just
    headcount."""
    state = _state(
        province_owner={HOME: "rome", NEIGHBOR: "carthage"},
        diplomatic_status={pair_key("rome", "carthage"): "war"},
        factions={
            "rome": _faction("rome", "Rome", units={"cavalry": 4}),
            "carthage": _faction("carthage", "Carthage", units={"legion": 5}),
        },
    )
    result = resolve_action(state, "rome", _action("move_army", target_province=NEIGHBOR))
    assert result["province_owner"][NEIGHBOR] == "rome"


def test_siege_engine_beats_a_larger_cavalry_force_via_counter_bonus():
    state = _state(
        province_owner={HOME: "rome", NEIGHBOR: "carthage"},
        diplomatic_status={pair_key("rome", "carthage"): "war"},
        factions={
            "rome": _faction("rome", "Rome", units={"siege_engine": 4}),
            "carthage": _faction("carthage", "Carthage", units={"cavalry": 5}),
        },
    )
    result = resolve_action(state, "rome", _action("move_army", target_province=NEIGHBOR))
    assert result["province_owner"][NEIGHBOR] == "rome"


def test_legion_beats_a_larger_siege_engine_force_via_counter_bonus():
    state = _state(
        province_owner={HOME: "rome", NEIGHBOR: "carthage"},
        diplomatic_status={pair_key("rome", "carthage"): "war"},
        factions={
            "rome": _faction("rome", "Rome", units={"legion": 4}),
            "carthage": _faction("carthage", "Carthage", units={"siege_engine": 5}),
        },
    )
    result = resolve_action(state, "rome", _action("move_army", target_province=NEIGHBOR))
    assert result["province_owner"][NEIGHBOR] == "rome"


def test_same_unit_type_combat_is_unaffected_by_counter_bonus():
    """No counter relationship applies when both sides field the same type
    -- combat degenerates to the pre-Stage-3 raw headcount comparison."""
    state = _state(
        province_owner={HOME: "rome", NEIGHBOR: "carthage"},
        diplomatic_status={pair_key("rome", "carthage"): "war"},
        factions={
            "rome": _faction("rome", "Rome", units={"cavalry": 4}),
            "carthage": _faction("carthage", "Carthage", units={"cavalry": 5}),
        },
    )
    result = resolve_action(state, "rome", _action("move_army", target_province=NEIGHBOR))
    assert result["province_owner"][NEIGHBOR] == "carthage"  # attacker still weaker, no bonus to save it


def test_build_unit_spends_gold_and_adds_unit():
    state = _state()
    result = resolve_action(state, "rome", _action("build_unit"))
    # A legion costs 10 gold + 5 iron (UNIT_COSTS); HOME's income yields
    # grain (plains terrain), so gold/iron only reflect the build cost.
    assert result["factions"]["rome"]["resources"]["gold"] == 10  # 20 - 10
    assert result["factions"]["rome"]["resources"]["iron"] == 15  # 20 - 5
    assert result["factions"]["rome"]["units"]["legion"] == 3


def test_build_unit_defaults_to_legion_when_unit_type_unset():
    state = _state()
    result = resolve_action(state, "rome", _action("build_unit"))
    assert set(result["factions"]["rome"]["units"].keys()) == {"legion"}


def test_build_unit_can_choose_a_different_unit_type():
    state = _state(
        factions={
            "rome": _faction("rome", "Rome", gold=20, iron=0, grain=10),
            "carthage": _faction("carthage", "Carthage"),
        }
    )
    result = resolve_action(state, "rome", _action("build_unit", unit_type="cavalry"))
    # Cavalry costs 15 gold + 10 grain, not gold+iron -- succeeds despite 0 iron.
    resources = result["factions"]["rome"]["resources"]
    assert resources["gold"] == 5  # 20 - 15
    assert resources["grain"] == 1  # 10 starting + 1 income (plains) - 10 cost
    assert result["factions"]["rome"]["units"]["cavalry"] == 1


def test_build_unit_is_a_no_op_when_short_on_a_required_resource():
    state = _state(factions={"rome": _faction("rome", "Rome", gold=0), "carthage": _faction("carthage", "Carthage")})
    result = resolve_action(state, "rome", _action("build_unit"))
    assert result["factions"]["rome"]["units"]["legion"] == 2  # unchanged
    assert "lacked" in result["resolution"]


def test_declare_war_is_unilateral_and_clears_pending_proposals():
    state = _state(pending_proposals={"rome->carthage": "truce"})
    result = resolve_action(state, "rome", _action("declare_war", target_faction="carthage"))
    assert result["diplomatic_status"][pair_key("rome", "carthage")] == "war"
    assert "rome->carthage" not in result["pending_proposals"]


def test_negotiate_one_sided_only_records_a_pending_proposal():
    state = _state()
    result = resolve_action(state, "rome", _action("negotiate", target_faction="carthage", proposal="truce"))
    assert result["pending_proposals"]["rome->carthage"] == "truce"
    assert pair_key("rome", "carthage") not in result["diplomatic_status"]


def test_negotiate_matching_proposal_from_both_sides_resolves_it():
    state = _state(pending_proposals={"carthage->rome": "alliance"})
    result = resolve_action(state, "rome", _action("negotiate", target_faction="carthage", proposal="alliance"))
    assert result["diplomatic_status"][pair_key("rome", "carthage")] == "alliance"
    assert "carthage->rome" not in result["pending_proposals"]
    assert "rome->carthage" not in result["pending_proposals"]


def test_territory_of_reflects_province_owner():
    state = _state(province_owner={HOME: "rome", NEIGHBOR: "rome", FAR_AWAY: "carthage"})
    assert sorted(territory_of(state, "rome")) == sorted([HOME, NEIGHBOR])
    assert territory_of(state, "carthage") == [FAR_AWAY]


def test_diplomatic_status_defaults_to_neutral():
    state = _state()
    assert diplomatic_status_between(state, "rome", "carthage") == "neutral"


def test_pair_key_is_order_independent():
    assert pair_key("rome", "carthage") == pair_key("carthage", "rome")
