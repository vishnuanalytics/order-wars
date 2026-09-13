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
# Purely visual overlays (backend/main.py serves these as static files, the
# same way as PROVINCES_PATH) — no runtime game-logic reader for either one
# exists, unlike Province below, since no game mechanic keys off a river or
# city yet. See generate_map.py's module docstring.
RIVERS_PATH = Path(__file__).parent / "rivers.geojson"
CITIES_PATH = Path(__file__).parent / "cities.geojson"


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


def distance_between(start: str, goal: str) -> int:
    """Hop-distance between two provinces over the combined land+sea
    adjacency graph (BFS, unweighted — a hex is a hex whether crossed by
    land or by a naval lane). Returns 0 if start == goal.

    The whole graph is a single connected component (confirmed when naval
    lanes were added — see generate_map.py's Stage 1 notes: the land mass
    was already one component, and every coastal province has at least one
    sea lane by construction), so this always terminates via the goal
    branch; the `visited`-exhaustion guard only protects against a future,
    genuinely disconnected map rather than anything possible today.
    """
    if start == goal:
        return 0
    all_ids = set(all_province_ids())
    visited = {start}
    frontier = [start]
    distance = 0
    while frontier and len(visited) < len(all_ids):
        distance += 1
        next_frontier = []
        for pid in frontier:
            for neighbor in (*neighbors_of(pid), *sea_neighbors_of(pid)):
                if neighbor == goal:
                    return distance
                if neighbor not in visited:
                    visited.add(neighbor)
                    next_frontier.append(neighbor)
        frontier = next_frontier
    return distance  # goal unreachable from start — shouldn't happen today


def province_ids_in_country(country: str) -> list[str]:
    return [p.province_id for p in _load().values() if p.country == country]


def all_province_ids() -> list[str]:
    return list(_load().keys())


def name_of(province_id: str) -> str:
    province = get_province(province_id)
    return province.name if province else province_id
