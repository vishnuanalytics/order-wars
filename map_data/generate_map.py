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

Each province's geometry is the full hexagon (not clipped to the coastline)
— a deliberate hex-grid-game look (Civ-style), not a realistic coastline map.
"""

import argparse
import zipfile
from pathlib import Path
from urllib.request import urlretrieve

import geopandas as gpd
import h3
from shapely.geometry import Polygon
from shapely.ops import unary_union

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

    centroids = _centroids_lonlat(hexes)
    hexes = hexes.reset_index().rename(columns={"h3_id": "province_id"})
    hexes["neighbors"] = hexes["province_id"].map(neighbors)
    hexes["centroid_lon"] = centroids["lon"].values
    hexes["centroid_lat"] = centroids["lat"].values
    hexes["land_frac"] = hexes["land_frac"].round(4)

    columns = [
        "province_id", "name", "country", "neighbors",
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
