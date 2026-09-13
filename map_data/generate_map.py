"""Generate the static province map: map_data/provinces.geojson.

Phase 3 — see CLAUDE.md "Project phases". Run this offline, once (or whenever
the region/resolution needs to change); the committed output is what the
game reads at runtime — nothing here runs during a game (see CLAUDE.md
Non-goals: no live map API calls in the core loop).

Pipeline:
  1. Download (once, cached under map_data/raw/, gitignored) Natural Earth
     50m land polygons and admin-0 country boundaries.
  2. Clip land to a bounding box — by default the Western/Central
     Mediterranean theater (Iberia, Gaul, Italy + islands, North Africa
     coast, the Balkans/Greece's west coast). This is a deliberate scope
     choice matching the Rome/Carthage/Gaul factions used as the Phase 2
     demo, not the full Roman world — widen --bbox to cover more later
     (e.g. add Egypt/the Levant for a full Punic-through-Republic map).
  3. Tile the clipped land with H3 hexagons (a real geodesic grid — avoids
     the planar-projection distortion a hand-rolled hex tiling would have
     over a region this large) using `contain="overlap"` rather than the
     default center-point containment: at any resolution, center-containment
     silently drops small/thin islands whose H3 cell center falls in the
     sea even though the island itself is real land (verified empirically —
     at resolution 3, center-containment misses Corsica, Cyprus, Malta, and
     Mallorca). Overlap containment catches any hex that touches land at
     all, then a land-fraction filter (--min-land-frac) drops the negligible
     slivers that mode also picks up along the coastline.
  4. Name each province from the dominant country its hex overlaps most (by
     area), suffixed with an index for countries split across many hexes.
     This is a placeholder naming scheme (modern country names, not
     historical Roman province names) — good enough for Phase 4's "agents
     move between named provinces"; historical/flavor renaming can happen
     later without touching this pipeline.
  5. Compute land-adjacency (H3 grid neighbors, intersected with the actual
     kept province set) and write everything to provinces.geojson.
  6. Classify each province's `terrain` (coastal/hills/plains) and compute
     `sea_neighbors` — short cross-water `move_army` lanes between coastal
     provinces that aren't already land-adjacent (e.g. Carthage <-> Sicily).
     Terrain is a gameplay-flavor proxy derived from data already computed
     above (land_frac + H3 adjacency gaps), not real elevation/coastline
     data — see `_classify_terrain`/`_compute_sea_neighbors` for exact
     rules and CLAUDE.md's Progress Log for how the thresholds/distance
     cutoff were calibrated against this map's actual geometry.

Each province's geometry is the full hexagon (not clipped to the coastline)
— a deliberate hex-grid-game look (Civ-style), not a realistic coastline map.
"""

import argparse
import zipfile
from pathlib import Path
from urllib.request import urlretrieve

import geopandas as gpd
import h3
from shapely.geometry import LineString, Polygon
from shapely.ops import substring, unary_union

MAP_DATA_DIR = Path(__file__).parent
RAW_DIR = MAP_DATA_DIR / "raw"
OUTPUT_PATH = MAP_DATA_DIR / "provinces.geojson"

NATURAL_EARTH_LAND_URL = "https://naturalearth.s3.amazonaws.com/50m_physical/ne_50m_land.zip"
NATURAL_EARTH_ADMIN0_URL = (
    "https://naturalearth.s3.amazonaws.com/50m_cultural/ne_50m_admin_0_countries.zip"
)

# lon_min, lat_min, lon_max, lat_max — see module docstring for why this
# region, not the whole Roman world. Tuned empirically: a lat_max above ~48
# starts pulling in Poland/Ukraine/Belarus, which have nothing to do with
# Rome/Carthage/Gaul, at the cost of clipping the northern third of France
# (Gaul retains its Mediterranean/central territory, just not Normandy or
# the Channel coast) — a deliberate trade-off, adjust via --bbox if the
# northern-France territory matters for your scenario.
DEFAULT_BBOX = (-10.0, 30.0, 28.0, 47.0)
DEFAULT_RESOLUTION = 3  # ~12,400 km^2 / hex, ~69 km edge (h3.average_hexagon_area)
DEFAULT_MIN_LAND_FRAC = 0.01

# Equal-area projection centered on DEFAULT_BBOX, for land-fraction math.
# Web Mercator/EPSG:4326 area math would distort ~3x across this bbox's
# latitude range (28-55N) — not accurate enough to threshold on.
_LAEA_CRS = "+proj=laea +lat_0=41.5 +lon_0=10 +datum=WGS84 +units=m +no_defs"


def _download_and_extract(url: str, dest_name: str) -> Path:
    extract_dir = RAW_DIR / dest_name
    if extract_dir.exists():
        return extract_dir
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    zip_path = RAW_DIR / f"{dest_name}.zip"
    print(f"Downloading {url} ...")
    urlretrieve(url, zip_path)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(extract_dir)
    return extract_dir


def _load_land(bbox) -> gpd.GeoDataFrame:
    shp_dir = _download_and_extract(NATURAL_EARTH_LAND_URL, "ne_50m_land")
    land = gpd.read_file(shp_dir / "ne_50m_land.shp")
    from shapely.geometry import box

    return land.clip(box(*bbox))


def _load_countries(bbox) -> gpd.GeoDataFrame:
    shp_dir = _download_and_extract(NATURAL_EARTH_ADMIN0_URL, "ne_50m_admin_0_countries")
    countries = gpd.read_file(shp_dir / "ne_50m_admin_0_countries.shp")
    from shapely.geometry import box

    return countries.clip(box(*bbox))[["NAME", "geometry"]].rename(columns={"NAME": "country"})


def _hex_cells(land_union, resolution: int, min_land_frac: float) -> gpd.GeoDataFrame:
    """Return a GeoDataFrame of hex cells that meaningfully overlap land."""
    shape = h3.geo_to_h3shape(land_union.__geo_interface__)
    cell_ids = set(h3.polygon_to_cells_experimental(shape, resolution, contain="overlap"))

    rows = [
        {
            "h3_id": c,
            "geometry": Polygon([(lng, lat) for lat, lng in h3.cell_to_boundary(c)]),
        }
        for c in cell_ids
    ]
    hexes = gpd.GeoDataFrame(rows, crs="EPSG:4326").set_index("h3_id")

    hexes_proj = hexes.to_crs(_LAEA_CRS)
    hexes_proj["hex_area"] = hexes_proj.geometry.area
    land_proj = gpd.GeoDataFrame(geometry=[land_union], crs="EPSG:4326").to_crs(_LAEA_CRS)

    intersection = gpd.overlay(
        hexes_proj.reset_index()[["h3_id", "geometry", "hex_area"]], land_proj, how="intersection"
    )
    intersection["land_area"] = intersection.geometry.area
    land_area_by_hex = intersection.groupby("h3_id")["land_area"].sum()

    hexes["land_frac"] = (land_area_by_hex.reindex(hexes.index).fillna(0) / hexes_proj["hex_area"]).values
    kept = hexes[hexes["land_frac"] >= min_land_frac].copy()
    print(f"H3 res {resolution}: {len(cell_ids)} candidate hexes, {len(kept)} kept "
          f"(land_frac >= {min_land_frac})")
    return kept


def _centroids_lonlat(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """WGS84 lon/lat centroids, computed via the equal-area CRS.

    Centroid is only well-defined as a planar operation — computing it
    directly on EPSG:4326 geometries (degrees) is a geopandas warning
    (`UserWarning: Geometry is in a geographic CRS`) for good reason, so
    project to _LAEA_CRS first and reproject the resulting points back.
    """
    centroids_proj = gdf.geometry.to_crs(_LAEA_CRS).centroid
    centroids_lonlat = centroids_proj.to_crs("EPSG:4326")
    return gpd.GeoDataFrame({"lon": centroids_lonlat.x, "lat": centroids_lonlat.y}, index=gdf.index)


def _name_provinces(hexes: gpd.GeoDataFrame, countries: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Assign `country` (dominant overlapping country by area) and a `name`."""
    hexes_proj = hexes.to_crs(_LAEA_CRS)
    countries_proj = countries.to_crs(_LAEA_CRS)

    overlap = gpd.overlay(
        hexes_proj.reset_index()[["h3_id", "geometry"]], countries_proj, how="intersection"
    )
    overlap["area"] = overlap.geometry.area
    dominant = overlap.loc[overlap.groupby("h3_id")["area"].idxmax()].set_index("h3_id")["country"]

    hexes = hexes.copy()
    hexes["country"] = dominant.reindex(hexes.index).fillna("Unclaimed")

    # Stable per-country numbering: sort by (lat, lon) so numbering reads
    # roughly north-to-south, west-to-east rather than by hash order.
    centroids = _centroids_lonlat(hexes)
    hexes = hexes.join(centroids).sort_values(
        ["country", "lat", "lon"], ascending=[True, False, True]
    )
    hexes["name"] = (
        hexes.groupby("country").cumcount().add(1).astype(str).radd(hexes["country"] + " ")
    )
    return hexes.drop(columns=["lat", "lon"])


def _compute_neighbors(hexes: gpd.GeoDataFrame) -> dict[str, list[str]]:
    kept_ids = set(hexes.index)
    return {
        h3_id: sorted(set(h3.grid_disk(h3_id, 1)) - {h3_id} & kept_ids)
        for h3_id in hexes.index
    }


def _real_water_gaps(hexes: gpd.GeoDataFrame, bbox) -> dict[str, set[str]]:
    """For each kept hex, which of its H3 neighbors are missing because they
    were tiled and then dropped for insufficient land (a real water gap) —
    as opposed to missing only because they fall outside the generation
    bbox (a clipping artifact, not water). Distinguishing the two matters:
    ~44 provinces near this map's northern bbox edge would otherwise be
    misclassified as coastal purely because land north of the bbox was
    clipped away before tiling, not because they border open sea.
    """
    from shapely.geometry import Point, box

    region = box(*bbox)
    kept_ids = set(hexes.index)
    gaps: dict[str, set[str]] = {}
    for h3_id in hexes.index:
        missing = set(h3.grid_disk(h3_id, 1)) - {h3_id} - kept_ids
        gaps[h3_id] = {
            m for m in missing
            if region.contains(Point(*reversed(h3.cell_to_latlng(m))))
        }
    return gaps


def _classify_terrain(hexes: gpd.GeoDataFrame, bbox) -> tuple[dict[str, str], dict[str, bool]]:
    """Gameplay-flavor `terrain` per province, derived only from data this
    pipeline already computes (land_frac, H3 adjacency) — no new Natural
    Earth layers (elevation/bathymetry) are downloaded for this.

    `is_coastal` (also returned, for `_compute_sea_neighbors` to reuse) is
    true if a province borders real open water (`_real_water_gaps`) OR is
    mostly water itself despite being topologically land-ringed at this hex
    resolution (land_frac < 0.5) — e.g. small islands/gulfs fully encircled
    by land hexes at ~69km resolution. Remaining provinces are `plains`
    (land_frac >= 0.9) or `hills` (the partial-water minority in between);
    the 0.9 cutoff was chosen empirically since non-coastal hexes in this
    map cluster overwhelmingly at land_frac == 1.0.
    """
    gaps = _real_water_gaps(hexes, bbox)
    terrain: dict[str, str] = {}
    is_coastal: dict[str, bool] = {}
    for h3_id, land_frac in hexes["land_frac"].items():
        coastal = bool(gaps[h3_id]) or land_frac < 0.5
        is_coastal[h3_id] = coastal
        if coastal:
            terrain[h3_id] = "coastal"
        elif land_frac >= 0.9:
            terrain[h3_id] = "plains"
        else:
            terrain[h3_id] = "hills"
    return terrain, is_coastal


# Naval-lane tuning constants — calibrated against this map's real geometry
# (see CLAUDE.md Progress Log), not chosen arbitrarily.
MAX_SEA_CROSSING_M = 400_000  # centroid-to-centroid cap; includes Sicily<->Tunisia
SEA_LANE_TRIM_M = 30_000  # trimmed off each end of the crossing line before testing
SEA_LANE_TOP_K = 3  # nearest candidates kept per province before symmetrizing


def _compute_sea_neighbors(
    hexes: gpd.GeoDataFrame,
    is_coastal: dict[str, bool],
    neighbors: dict[str, list[str]],
    land_union,
) -> dict[str, list[str]]:
    """Short cross-water `move_army` lanes between coastal provinces that
    aren't already land-adjacent.

    Candidate pairs are filtered by centroid distance (in the pipeline's
    existing equal-area CRS) and by a trimmed-line-vs-land check: both
    centroids sit inside land by construction, so testing the raw
    centroid-to-centroid line against the real land polygon rejects real
    open-water crossings too (confirmed against real Adriatic/Ionian
    pairs) — trimming `min(SEA_LANE_TRIM_M, 30% of length)` off each end
    before testing the remaining "core" segment fixes this. Each province
    keeps its nearest `SEA_LANE_TOP_K` candidates, then the result is
    symmetrized by union (if A picks B, the edge exists even if B's own
    top-K didn't independently pick A) — so a few real chokepoints can end
    up with more than `SEA_LANE_TOP_K` lanes, which is expected, not a bug.
    Any coastal province left with zero lanes after this falls back to a
    guaranteed connection to its single nearest coastal province, so no
    coastal province is ever fully unreachable by sea.
    """
    coastal_ids = [h3_id for h3_id in hexes.index if is_coastal.get(h3_id, False)]
    result: dict[str, set[str]] = {h3_id: set() for h3_id in hexes.index}
    if not coastal_ids:
        return {h3_id: [] for h3_id in hexes.index}

    centroids_proj = hexes.geometry.to_crs(_LAEA_CRS).centroid
    land_proj = gpd.GeoSeries([land_union], crs="EPSG:4326").to_crs(_LAEA_CRS).iloc[0]

    candidates: dict[str, list[tuple[str, float]]] = {h3_id: [] for h3_id in coastal_ids}
    for i, a in enumerate(coastal_ids):
        point_a = centroids_proj[a]
        for b in coastal_ids[i + 1:]:
            if b in neighbors.get(a, ()):
                continue  # already land-adjacent, no lane needed
            point_b = centroids_proj[b]
            distance = point_a.distance(point_b)
            if distance > MAX_SEA_CROSSING_M:
                continue
            trim = min(SEA_LANE_TRIM_M, distance * 0.3)
            core = substring(LineString([point_a, point_b]), trim, distance - trim)
            if core.length <= 0 or core.intersects(land_proj):
                continue
            candidates[a].append((b, distance))
            candidates[b].append((a, distance))

    for h3_id, cands in candidates.items():
        for other, _distance in sorted(cands, key=lambda pair: pair[1])[:SEA_LANE_TOP_K]:
            result[h3_id].add(other)
            result[other].add(h3_id)

    for h3_id in coastal_ids:
        if result[h3_id]:
            continue
        point_a = centroids_proj[h3_id]
        already_adjacent = set(neighbors.get(h3_id, ()))
        nearest = min(
            (
                other for other in coastal_ids
                if other != h3_id and other not in already_adjacent
            ),
            key=lambda other: point_a.distance(centroids_proj[other]),
            default=None,
        )
        if nearest is not None:
            result[h3_id].add(nearest)
            result[nearest].add(h3_id)

    return {h3_id: sorted(others) for h3_id, others in result.items()}


def generate(
    bbox=DEFAULT_BBOX,
    resolution: int = DEFAULT_RESOLUTION,
    min_land_frac: float = DEFAULT_MIN_LAND_FRAC,
    output_path: Path = OUTPUT_PATH,
) -> gpd.GeoDataFrame:
    land = _load_land(bbox)
    countries = _load_countries(bbox)
    land_union = unary_union(land.geometry.values)

    hexes = _hex_cells(land_union, resolution, min_land_frac)
    hexes = _name_provinces(hexes, countries)
    neighbors = _compute_neighbors(hexes)
    terrain, is_coastal = _classify_terrain(hexes, bbox)
    sea_neighbors = _compute_sea_neighbors(hexes, is_coastal, neighbors, land_union)

    centroids = _centroids_lonlat(hexes)
    hexes = hexes.reset_index().rename(columns={"h3_id": "province_id"})
    hexes["neighbors"] = hexes["province_id"].map(neighbors)
    hexes["sea_neighbors"] = hexes["province_id"].map(sea_neighbors)
    hexes["terrain"] = hexes["province_id"].map(terrain)
    hexes["centroid_lon"] = centroids["lon"].values
    hexes["centroid_lat"] = centroids["lat"].values
    hexes["land_frac"] = hexes["land_frac"].round(4)

    columns = [
        "province_id", "name", "country", "neighbors", "sea_neighbors", "terrain",
        "centroid_lon", "centroid_lat", "land_frac", "geometry",
    ]
    hexes = hexes[columns]

    output_path.parent.mkdir(parents=True, exist_ok=True)
    hexes.to_file(output_path, driver="GeoJSON")
    print(f"Wrote {len(hexes)} provinces to {output_path}")
    return hexes


def _parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--bbox", type=float, nargs=4, default=DEFAULT_BBOX,
        metavar=("LON_MIN", "LAT_MIN", "LON_MAX", "LAT_MAX"),
        help="Region to tile, in WGS84 degrees (default: Western/Central Mediterranean).",
    )
    parser.add_argument(
        "--resolution", type=int, default=DEFAULT_RESOLUTION,
        help="H3 resolution (default: 3, ~12,400 km^2/hex). Higher = more, smaller provinces.",
    )
    parser.add_argument(
        "--min-land-frac", type=float, default=DEFAULT_MIN_LAND_FRAC,
        help="Drop hexes with less than this fraction of land area (default: 0.01).",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    generate(bbox=tuple(args.bbox), resolution=args.resolution, min_land_frac=args.min_land_frac)
