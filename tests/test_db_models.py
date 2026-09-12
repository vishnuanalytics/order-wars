"""Model/schema tests only — run against in-memory SQLite, never a live database."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from db.models import (
    Base,
    DiplomaticRelation,
    DiplomaticStatus,
    FactionStateSnapshot,
    Game,
    GameEvent,
    GameFaction,
    RolePreset,
    Scenario,
    ScenarioFaction,
)


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def test_scenario_with_factions(session):
    scenario = Scenario(name="Punic Wars", max_turns=30)
    scenario.factions.append(
        ScenarioFaction(
            faction_name="Rome",
            role_preset=RolePreset.EXPANSIONIST,
            starting_resources={"gold": 100},
            starting_units={"legion": 3},
            starting_territory=["p1", "p2"],
        )
    )
    scenario.factions.append(
        ScenarioFaction(faction_name="Carthage", role_preset=RolePreset.WARMONGER)
    )
    session.add(scenario)
    session.commit()

    fetched = session.get(Scenario, scenario.id)
    assert len(fetched.factions) == 2
    assert {f.faction_name for f in fetched.factions} == {"Rome", "Carthage"}
    rome = next(f for f in fetched.factions if f.faction_name == "Rome")
    assert rome.starting_resources == {"gold": 100}


def test_scenario_delete_cascades_to_factions(session):
    scenario = Scenario(name="Temp")
    scenario.factions.append(ScenarioFaction(faction_name="Rome", role_preset=RolePreset.CUSTOM))
    session.add(scenario)
    session.commit()

    session.delete(scenario)
    session.commit()

    assert session.query(ScenarioFaction).count() == 0


def test_game_run_records_events_and_snapshots(session):
    scenario = Scenario(name="Punic Wars")
    session.add(scenario)
    session.commit()

    game = Game(scenario_id=scenario.id, config_snapshot={"name": scenario.name})
    rome = GameFaction(game=game, faction_name="Rome", role_preset=RolePreset.EXPANSIONIST)
    carthage = GameFaction(game=game, faction_name="Carthage", role_preset=RolePreset.WARMONGER)
    game.factions.extend([rome, carthage])
    session.add(game)
    session.commit()

    session.add(
        GameEvent(game_id=game.id, turn=1, faction_id=rome.id, event_type="move", payload={"to": "p2"})
    )
    session.add(
        FactionStateSnapshot(
            game_id=game.id, faction_id=rome.id, turn=1,
            resources={"gold": 90}, territory_count=2, unit_count=3,
        )
    )
    session.add(
        DiplomaticRelation(
            game_id=game.id, faction_a_id=rome.id, faction_b_id=carthage.id,
            status=DiplomaticStatus.WAR, turn_changed=1,
        )
    )
    session.commit()

    fetched = session.get(Game, game.id)
    assert len(fetched.factions) == 2
    assert len(fetched.events) == 1
    assert fetched.events[0].event_type == "move"
    assert len(fetched.snapshots) == 1
    assert len(fetched.diplomatic_relations) == 1
    assert fetched.diplomatic_relations[0].status == DiplomaticStatus.WAR


def test_game_survives_scenario_deletion(session):
    """config_snapshot is the point of freezing state — a run must outlive its scenario."""
    scenario = Scenario(name="Ephemeral")
    session.add(scenario)
    session.commit()

    game = Game(scenario_id=scenario.id, config_snapshot={"name": scenario.name})
    session.add(game)
    session.commit()
    game_id = game.id

    session.delete(scenario)
    session.commit()

    survived = session.get(Game, game_id)
    assert survived is not None
    assert survived.scenario_id is None
    assert survived.config_snapshot == {"name": "Ephemeral"}


def test_snapshot_unique_constraint_per_turn(session):
    game = Game(config_snapshot={})
    faction = GameFaction(game=game, faction_name="Rome", role_preset=RolePreset.CUSTOM)
    game.factions.append(faction)
    session.add(game)
    session.commit()

    session.add(FactionStateSnapshot(game_id=game.id, faction_id=faction.id, turn=1, territory_count=1))
    session.commit()

    session.add(FactionStateSnapshot(game_id=game.id, faction_id=faction.id, turn=1, territory_count=2))
    with pytest.raises(Exception):
        session.commit()
