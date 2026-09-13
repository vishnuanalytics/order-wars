import L from "leaflet";

const UNCLAIMED_COLOR = "#c9c9c9";
// A qualitative palette (Okabe-Ito-ish, colorblind-friendlier than defaults)
// — cycled per faction, assigned in first-seen order and kept stable for
// the life of the page (MapView instance) via factionColors.
const PALETTE = [
  "#d55e00", "#0072b2", "#009e73", "#cc79a7",
  "#e69f00", "#56b4e9", "#f0e442", "#8c564b",
];

export class MapView {
  constructor(containerId) {
    this.map = L.map(containerId, { minZoom: 3, maxZoom: 8 }).setView([41, 12], 5);
    // CARTO's free anonymous tile endpoint (basemaps.cartocdn.com) turned out
    // to require an API key now — verified live, it served "API KEY
    // REQUIRED" watermark tiles instead of a basemap. Standard OSM tiles are
    // still keyless; toned down with a CSS grayscale filter (see
    // style.css's .leaflet-tile-pane) for a similarly subdued look so the
    // colored province hexes stay the visual focus.
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
      subdomains: "abc",
      maxZoom: 19,
    }).addTo(this.map);

    this.geoLayer = null;
    this.layersByProvinceId = new Map();
    this.factionColors = new Map();
    this.selectedProvinceId = null;
    this.ownerByProvinceId = {};

    // Set by callers that want clicks — the scenario editor for "pick a
    // home province" mode, otherwise clicks just show the tooltip.
    this.onProvinceClick = null;
  }

  loadProvinces(features) {
    this.geoLayer = L.geoJSON(
      { type: "FeatureCollection", features },
      {
        style: () => this._styleFor(null),
        onEachFeature: (feature, layer) => {
          const provinceId = feature.properties.province_id;
          this.layersByProvinceId.set(provinceId, layer);
          layer.bindTooltip(feature.properties.name, { sticky: true });
          layer.on("click", () => {
            if (this.onProvinceClick) this.onProvinceClick(provinceId, feature.properties);
          });
        },
      }
    ).addTo(this.map);
    this.map.fitBounds(this.geoLayer.getBounds());
  }

  colorFor(factionName) {
    if (!this.factionColors.has(factionName)) {
      this.factionColors.set(factionName, PALETTE[this.factionColors.size % PALETTE.length]);
    }
    return this.factionColors.get(factionName);
  }

  _styleFor(ownerFactionName, isSelected) {
    return {
      color: isSelected ? "#111" : "#555",
      weight: isSelected ? 3 : 1,
      fillColor: ownerFactionName ? this.colorFor(ownerFactionName) : UNCLAIMED_COLOR,
      fillOpacity: 0.6,
    };
  }

  /** `ownerByProvinceId`: plain object mapping province_id -> faction name. */
  setOwnership(ownerByProvinceId) {
    this.ownerByProvinceId = ownerByProvinceId;
    for (const [provinceId, layer] of this.layersByProvinceId) {
      const isSelected = provinceId === this.selectedProvinceId;
      layer.setStyle(this._styleFor(ownerByProvinceId[provinceId] ?? null, isSelected));
    }
  }

  /** Visual feedback while picking a home province in the scenario editor.
   * Restyles using the current ownership, not a blank slate — otherwise
   * picking a province while a game's colors are displayed would wipe them.
   */
  setSelected(provinceId) {
    const previous = this.selectedProvinceId;
    this.selectedProvinceId = provinceId;
    for (const id of [previous, provinceId]) {
      const layer = id && this.layersByProvinceId.get(id);
      if (layer) layer.setStyle(this._styleFor(this.ownerByProvinceId[id] ?? null, id === provinceId));
    }
  }
}
