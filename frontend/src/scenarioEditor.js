import { createScenario } from "./api.js";
import { escapeHtml } from "./utils.js";

const ROLE_PRESETS = ["expansionist", "warmonger", "diplomat_trader", "isolationist", "custom"];

export class ScenarioEditor {
  /** `provinces`: the raw GeoJSON features array (province_id, name,
   * neighbors per feature) — kept as ScenarioEditor's own lookup rather
   * than reaching into MapView's Leaflet layer internals for it, so this
   * module's only coupling to MapView stays the same small public surface
   * (onProvinceClick, setSelected, setOwnership) it already used before
   * territory support existed.
   */
  constructor({ mapView, provinces, onSaved }) {
    this.mapView = mapView;
    this.onSaved = onSaved;
    this.provincePropsById = new Map(provinces.map((f) => [f.properties.province_id, f.properties]));
    this.factions = [];
    // Exactly one of these is set at a time — picking a capital
    // (auto-clusters a fresh starting territory around it) vs. adjusting
    // an already-capitaled faction's territory province by province.
    this.pickingForFactionId = null;
    this.adjustingForFactionId = null;

    this.form = document.getElementById("scenario-form");
    this.factionListEl = document.getElementById("faction-list");
    this.pickHint = document.getElementById("pick-province-hint");
    this.pickTargetEl = document.getElementById("pick-province-target");
    this.territorySizeInput = document.getElementById("scenario-territory-size");

    document.getElementById("add-faction-button").addEventListener("click", () => this.addFaction());
    this.form.addEventListener("submit", (event) => this._handleSubmit(event));
    this.mapView.onProvinceClick = (provinceId, properties) => this._handleMapClick(provinceId, properties);

    this.addFaction();
    this.addFaction();
  }

  addFaction() {
    this.factions.push({
      id: crypto.randomUUID(),
      name: `Faction ${this.factions.length + 1}`,
      rolePreset: "custom",
      homeProvince: null,
      homeProvinceName: null,
      territory: [], // includes homeProvince once set — capital is just territory[0]
      gold: 20,
      grain: 20,
      iron: 10,
      legions: 2,
    });
    this._render();
  }

  removeFaction(id) {
    if (this.factions.length <= 2) {
      alert("A scenario needs at least 2 factions.");
      return;
    }
    this.factions = this.factions.filter((f) => f.id !== id);
    if (this.pickingForFactionId === id) this._stopPicking();
    if (this.adjustingForFactionId === id) this._stopAdjusting();
    this._render();
  }

  _claimedProvinceIds(excludingFactionId) {
    const claimed = new Set();
    for (const f of this.factions) {
      if (f.id === excludingFactionId) continue;
      for (const p of f.territory) claimed.add(p);
    }
    return claimed;
  }

  _startPicking(factionId) {
    this._stopAdjusting();
    this.pickingForFactionId = factionId;
    this.pickTargetEl.textContent = `${this.factions.find((f) => f.id === factionId).name} — capital`;
    this.pickHint.hidden = false;
  }

  _stopPicking() {
    this.pickingForFactionId = null;
    this.pickHint.hidden = true;
  }

  _startAdjusting(factionId) {
    this._stopPicking();
    this.adjustingForFactionId = factionId;
    this.pickTargetEl.textContent =
      `${this.factions.find((f) => f.id === factionId).name} — click a province to add/remove it`;
    this.pickHint.hidden = false;
  }

  _stopAdjusting() {
    this.adjustingForFactionId = null;
    this.pickHint.hidden = true;
  }

  /** Capital plus up to `size - 1` of its currently-unclaimed neighbors, in
   * the order the map data lists them — a fast default the scenario
   * author can then hand-adjust with "Adjust territory", not meant to be
   * a strategically balanced placement. Falls short of `size` (not an
   * error) if there aren't enough free neighbors.
   */
  _autoCluster(capitalId, size, excludingFactionId) {
    const claimed = this._claimedProvinceIds(excludingFactionId);
    const neighbors = this.provincePropsById.get(capitalId)?.neighbors || [];
    const cluster = [capitalId];
    for (const neighborId of neighbors) {
      if (cluster.length >= size) break;
      if (claimed.has(neighborId)) continue;
      cluster.push(neighborId);
    }
    return cluster;
  }

  _ownerOf(provinceId, excludingFactionId) {
    return this.factions.find((f) => f.id !== excludingFactionId && f.territory.includes(provinceId));
  }

  _handleMapClick(provinceId, properties) {
    if (this.pickingForFactionId) {
      const owner = this._ownerOf(provinceId, this.pickingForFactionId);
      if (owner) {
        alert(`${owner.name} already claims ${properties.name}. Pick a different province.`);
        return;
      }
      const faction = this.factions.find((f) => f.id === this.pickingForFactionId);
      const size = Math.max(1, Number(this.territorySizeInput.value) || 1);
      faction.homeProvince = provinceId;
      faction.homeProvinceName = properties.name;
      faction.territory = this._autoCluster(provinceId, size, faction.id);
      this._stopPicking();
      this.mapView.setSelected(null);
      this._render();
    } else if (this.adjustingForFactionId) {
      const faction = this.factions.find((f) => f.id === this.adjustingForFactionId);
      if (provinceId === faction.homeProvince) {
        alert("The capital is always part of its own territory — pick a new capital to change it.");
        return;
      }
      if (faction.territory.includes(provinceId)) {
        faction.territory = faction.territory.filter((p) => p !== provinceId);
      } else {
        const owner = this._ownerOf(provinceId, faction.id);
        if (owner) {
          alert(`${owner.name} already claims ${properties.name}.`);
          return;
        }
        faction.territory.push(provinceId);
      }
      this._render();
    }
  }

  /** Colors every faction's current starting territory on the map (not
   * just one selected province) — reuses setOwnership wholesale (the same
   * mechanism live gameplay uses to color the board by owner), keyed by
   * faction name exactly like gameplay does, so the preview looks like
   * what the actual game will look like on turn 1.
   */
  _renderMapPreview() {
    const preview = {};
    for (const faction of this.factions) {
      for (const provinceId of faction.territory) preview[provinceId] = faction.name;
    }
    this.mapView.setOwnership(preview);
  }

  _render() {
    this._renderMapPreview();
    this.factionListEl.innerHTML = "";
    for (const faction of this.factions) {
      const row = document.createElement("div");
      row.className = "faction-row";
      const territoryLabel = faction.territory.length
        ? `Adjust territory (${faction.territory.length})`
        : "Adjust territory";
      row.innerHTML = `
        <div class="faction-row-line">
          <input type="text" class="faction-name" value="${escapeHtml(faction.name)}" aria-label="Faction name" />
          <select class="faction-role" aria-label="Role preset">
            ${ROLE_PRESETS.map(
              (preset) =>
                `<option value="${preset}" ${preset === faction.rolePreset ? "selected" : ""}>${preset}</option>`
            ).join("")}
          </select>
          <button type="button" class="remove-faction-button" aria-label="Remove faction">✕</button>
        </div>
        <div class="faction-row-line">
          <button type="button" class="pick-province-button ${faction.homeProvince ? "picked" : ""}">
            ${faction.homeProvince ? `📍 ${escapeHtml(faction.homeProvinceName)}` : "Pick capital"}
          </button>
          <button type="button" class="adjust-territory-button ${this.adjustingForFactionId === faction.id ? "active" : ""}"
                  ${faction.homeProvince ? "" : "disabled"}>
            ${this.adjustingForFactionId === faction.id ? "Done adjusting" : territoryLabel}
          </button>
        </div>
        <div class="faction-row-line">
          <span class="faction-stat">Gold<input type="number" class="faction-gold" value="${faction.gold}" min="0" aria-label="Starting gold" /></span>
          <span class="faction-stat">Grain<input type="number" class="faction-grain" value="${faction.grain}" min="0" aria-label="Starting grain" /></span>
        </div>
        <div class="faction-row-line">
          <span class="faction-stat">Iron<input type="number" class="faction-iron" value="${faction.iron}" min="0" aria-label="Starting iron" /></span>
          <span class="faction-stat">Legions<input type="number" class="faction-legions" value="${faction.legions}" min="0" aria-label="Starting legions" /></span>
        </div>
      `;
      row.querySelector(".faction-name").addEventListener("input", (e) => {
        faction.name = e.target.value;
        this._renderMapPreview();
      });
      row.querySelector(".faction-role").addEventListener("change", (e) => (faction.rolePreset = e.target.value));
      row.querySelector(".faction-gold").addEventListener("input", (e) => (faction.gold = Number(e.target.value)));
      row.querySelector(".faction-grain").addEventListener("input", (e) => (faction.grain = Number(e.target.value)));
      row.querySelector(".faction-iron").addEventListener("input", (e) => (faction.iron = Number(e.target.value)));
      row
        .querySelector(".faction-legions")
        .addEventListener("input", (e) => (faction.legions = Number(e.target.value)));
      row.querySelector(".pick-province-button").addEventListener("click", () => {
        this._startPicking(faction.id);
        this.mapView.setSelected(faction.homeProvince);
      });
      row.querySelector(".adjust-territory-button").addEventListener("click", () => {
        if (this.adjustingForFactionId === faction.id) this._stopAdjusting();
        else this._startAdjusting(faction.id);
        this._render();
      });
      row.querySelector(".remove-faction-button").addEventListener("click", () => this.removeFaction(faction.id));
      this.factionListEl.appendChild(row);
    }
  }

  async _handleSubmit(event) {
    event.preventDefault();
    const missingHome = this.factions.find((f) => !f.homeProvince);
    if (missingHome) {
      alert(`Pick a capital for ${missingHome.name}.`);
      return;
    }

    const payload = {
      name: document.getElementById("scenario-name").value,
      max_turns: Number(document.getElementById("scenario-max-turns").value),
      factions: this.factions.map((f) => ({
        faction_name: f.name,
        role_preset: f.rolePreset,
        starting_resources: { gold: f.gold, grain: f.grain, iron: f.iron },
        starting_units: { legion: f.legions },
        starting_territory: f.territory,
      })),
    };

    const submitButton = this.form.querySelector('button[type="submit"]');
    submitButton.disabled = true;
    try {
      const scenario = await createScenario(payload);
      this.onSaved(scenario);
    } catch (err) {
      alert(`Could not save scenario: ${err.message}`);
    } finally {
      submitButton.disabled = false;
    }
  }
}
