"""Runtime read access to the committed `provinces.geojson`.

Companion to `generate_map.py` (which only runs offline to produce that
file): this module is what `agents/` and `game/` import to look up
provinces, neighbors, and countries during an actual game. No geopandas/h3
dependency here — just `json`, so importing this at runtime doesn't pull in
the heavier Phase 3 generation toolchain.
"""

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

PROVINCES_PATH = Path(__file__).parent / "provinces.geojson"


@dataclass(frozen=True)
class Province:
    province_id: str
    name: str
    country: str
    neighbors: tuple[str, ...]
    sea_neighbors: tuple[str, ...]
    terrain: str
    centroid_lon: float
    centroid_lat: float
    land_frac: float


@lru_cache(maxsize=1)
def _load() -> dict[str, Province]:
    with open(PROVINCES_PATH) as f:
        data = json.load(f)
    provinces = {}
    for feature in data["features"]:
        props = feature["properties"]
        provinces[props["province_id"]] = Province(
            province_id=props["province_id"],
            name=props["name"],
            country=props["country"],
            neighbors=tuple(props["neighbors"]),
            sea_neighbors=tuple(props["sea_neighbors"]),
            terrain=props["terrain"],
            centroid_lon=props["centroid_lon"],
            centroid_lat=props["centroid_lat"],
            land_frac=props["land_frac"],
        )
    return provinces


def get_province(province_id: str) -> Province | None:
    return _load().get(province_id)


def neighbors_of(province_id: str) -> tuple[str, ...]:
    province = get_province(province_id)
    return province.neighbors if province else ()


def sea_neighbors_of(province_id: str) -> tuple[str, ...]:
    """Short cross-water move_army lanes — see generate_map.py's
    `_compute_sea_neighbors` for how these are computed and calibrated."""
    province = get_province(province_id)
    return province.sea_neighbors if province else ()


def province_ids_in_country(country: str) -> list[str]:
    return [p.province_id for p in _load().values() if p.country == country]


def all_province_ids() -> list[str]:
    return list(_load().keys())


def name_of(province_id: str) -> str:
    province = get_province(province_id)
    return province.name if province else province_id
