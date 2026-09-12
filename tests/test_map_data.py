"""Validates the committed map_data/provinces.geojson, not a live generation
run — regenerating (network + geopandas/h3) is a separate offline step
(`python map_data/generate_map.py`), not something the test suite does.
"""

import json
from pathlib import Path

import pytest

PROVINCES_PATH = Path(__file__).parent.parent / "map_data" / "provinces.geojson"

REQUIRED_PROPERTIES = {
    "province_id", "name", "country", "neighbors",
    "centroid_lon", "centroid_lat", "land_frac",
}


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
        assert props["neighbors"], f"{province_id} has no neighbors"
        assert province_id not in props["neighbors"], f"{province_id} neighbors itself"


def test_land_frac_within_bounds(provinces):
    for props in provinces.values():
        assert 0.0 < props["land_frac"] <= 1.0


def test_demo_faction_homelands_present(provinces):
    """The Phase 2 demo factions (agents/graph.py) are Rome, Carthage, Gaul —
    confirm their real-world homelands actually made it onto the map.
    """
    countries = {props["country"] for props in provinces.values()}
    assert "Italy" in countries
    assert "Tunisia" in countries
    assert "France" in countries
