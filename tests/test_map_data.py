"""Validates the committed map_data/provinces.geojson, not a live generation
run — regenerating (network + geopandas/h3) is a separate offline step
(`python map_data/generate_map.py`), not something the test suite does.
"""

import json
from pathlib import Path

import pytest

PROVINCES_PATH = Path(__file__).parent.parent / "map_data" / "provinces.geojson"

REQUIRED_PROPERTIES = {
    "province_id", "name", "country", "neighbors", "sea_neighbors", "terrain",
    "centroid_lon", "centroid_lat", "land_frac",
}

VALID_TERRAINS = {"coastal", "hills", "plains"}


@pytest.fixture(scope="module")
def provinces() -> dict[str, dict]:
    with open(PROVINCES_PATH) as f:
        data = json.load(f)
    return {f["properties"]["province_id"]: f["properties"] for f in data["features"]}


def test_provinces_file_has_features(provinces):
    assert len(provinces) > 0


def test_required_properties_present(provinces):
    sample = next(iter(provinces.values()))
    assert REQUIRED_PROPERTIES.issubset(sample.keys())


def test_province_ids_and_names_unique(provinces):
    names = [p["name"] for p in provinces.values()]
    assert len(names) == len(set(names))
    # province_id uniqueness is implicit in `provinces` being a dict keyed
    # by it, but confirm the source file didn't silently collide two
    # features onto the same id (which a dict comprehension would hide).
    with open(PROVINCES_PATH) as f:
        raw_ids = [f["properties"]["province_id"] for f in json.load(f)["features"]]
    assert len(raw_ids) == len(set(raw_ids))


def test_adjacency_is_symmetric(provinces):
    for province_id, props in provinces.items():
        for neighbor_id in props["neighbors"]:
            assert province_id in provinces[neighbor_id]["neighbors"], (
                f"{province_id} lists {neighbor_id} as a neighbor, but not vice versa"
            )


def test_no_orphans_or_self_loops(provinces):
    for province_id, props in provinces.items():
        assert props["neighbors"] or props["sea_neighbors"], (
            f"{province_id} has no land or sea neighbors"
        )
        assert province_id not in props["neighbors"], f"{province_id} neighbors itself"
        assert province_id not in props["sea_neighbors"], f"{province_id} sea_neighbors itself"


def test_land_frac_within_bounds(provinces):
    for props in provinces.values():
        assert 0.0 < props["land_frac"] <= 1.0


def test_terrain_is_a_known_value(provinces):
    for props in provinces.values():
        assert props["terrain"] in VALID_TERRAINS


def test_sea_neighbors_is_symmetric(provinces):
    for province_id, props in provinces.items():
        for other_id in props["sea_neighbors"]:
            assert province_id in provinces[other_id]["sea_neighbors"], (
                f"{province_id} lists {other_id} as a sea_neighbor, but not vice versa"
            )


def test_sea_neighbors_only_connect_coastal_provinces(provinces):
    for province_id, props in provinces.items():
        if props["sea_neighbors"]:
            assert props["terrain"] == "coastal", (
                f"{province_id} has sea_neighbors but isn't coastal"
            )
        for other_id in props["sea_neighbors"]:
            assert provinces[other_id]["terrain"] == "coastal"


def test_sea_neighbors_are_not_already_land_neighbors(provinces):
    for province_id, props in provinces.items():
        overlap = set(props["neighbors"]) & set(props["sea_neighbors"])
        assert not overlap, f"{province_id} has redundant land+sea neighbors: {overlap}"


def test_carthage_can_reach_sicily_or_italy_by_sea(provinces):
    """The original motivating case for naval lanes: Tunisia and Italy don't
    share a land border on this map, so a Carthage/Rome naval war needs at
    least one Tunisia<->Italy sea_neighbor pair to be possible at all.
    """
    tunisia_ids = {pid for pid, p in provinces.items() if p["country"] == "Tunisia"}
    italy_ids = {pid for pid, p in provinces.items() if p["country"] == "Italy"}
    reachable = any(
        set(provinces[pid]["sea_neighbors"]) & italy_ids for pid in tunisia_ids
    )
    assert reachable, "no Tunisia province has a sea lane into Italy"


def test_demo_faction_homelands_present(provinces):
    """The Phase 2 demo factions (agents/graph.py) are Rome, Carthage, Gaul —
    confirm their real-world homelands actually made it onto the map.
    """
    countries = {props["country"] for props in provinces.values()}
    assert "Italy" in countries
    assert "Tunisia" in countries
    assert "France" in countries
