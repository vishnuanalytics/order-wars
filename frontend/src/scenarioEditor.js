import { createScenario } from "./api.js";
import { escapeHtml } from "./utils.js";

const ROLE_PRESETS = ["expansionist", "warmonger", "diplomat_trader", "isolationist", "custom"];

export class ScenarioEditor {
  constructor({ mapView, onSaved }) {
    this.mapView = mapView;
    this.onSaved = onSaved;
    this.factions = [];
    this.pickingForFactionId = null;

    this.form = document.getElementById("scenario-form");
    this.factionListEl = document.getElementById("faction-list");
    this.pickHint = document.getElementById("pick-province-hint");
    this.pickTargetEl = document.getElementById("pick-province-target");

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
      gold: 20,
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
    this._render();
  }

  _startPicking(factionId) {
    this.pickingForFactionId = factionId;
    this.pickTargetEl.textContent = this.factions.find((f) => f.id === factionId).name;
    this.pickHint.hidden = false;
  }

  _stopPicking() {
    this.pickingForFactionId = null;
    this.pickHint.hidden = true;
  }

  _handleMapClick(provinceId, properties) {
    if (!this.pickingForFactionId) return;
    const alreadyTaken = this.factions.find(
      (f) => f.homeProvince === provinceId && f.id !== this.pickingForFactionId
    );
    if (alreadyTaken) {
      alert(`${alreadyTaken.name} already starts in ${properties.name}. Pick a different province.`);
      return;
    }
    const faction = this.factions.find((f) => f.id === this.pickingForFactionId);
    faction.homeProvince = provinceId;
    faction.homeProvinceName = properties.name;
    this._stopPicking();
    this.mapView.setSelected(null);
    this._render();
  }

  _render() {
    this.factionListEl.innerHTML = "";
    for (const faction of this.factions) {
      const row = document.createElement("div");
      row.className = "faction-row";
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
            ${faction.homeProvince ? `📍 ${escapeHtml(faction.homeProvinceName)}` : "Pick home province"}
          </button>
        </div>
        <div class="faction-row-line">
          <span class="faction-stat">Gold<input type="number" class="faction-gold" value="${faction.gold}" min="0" aria-label="Starting gold" /></span>
          <span class="faction-stat">Legions<input type="number" class="faction-legions" value="${faction.legions}" min="0" aria-label="Starting legions" /></span>
        </div>
      `;
      row.querySelector(".faction-name").addEventListener("input", (e) => (faction.name = e.target.value));
      row.querySelector(".faction-role").addEventListener("change", (e) => (faction.rolePreset = e.target.value));
      row.querySelector(".faction-gold").addEventListener("input", (e) => (faction.gold = Number(e.target.value)));
      row
        .querySelector(".faction-legions")
        .addEventListener("input", (e) => (faction.legions = Number(e.target.value)));
      row.querySelector(".pick-province-button").addEventListener("click", () => {
        this._startPicking(faction.id);
        this.mapView.setSelected(faction.homeProvince);
      });
      row.querySelector(".remove-faction-button").addEventListener("click", () => this.removeFaction(faction.id));
      this.factionListEl.appendChild(row);
    }
  }

  async _handleSubmit(event) {
    event.preventDefault();
    const missingHome = this.factions.find((f) => !f.homeProvince);
    if (missingHome) {
      alert(`Pick a home province for ${missingHome.name}.`);
      return;
    }

    const payload = {
      name: document.getElementById("scenario-name").value,
      max_turns: Number(document.getElementById("scenario-max-turns").value),
      factions: this.factions.map((f) => ({
        faction_name: f.name,
        role_preset: f.rolePreset,
        starting_resources: { gold: f.gold },
        starting_units: { legion: f.legions },
        starting_territory: [f.homeProvince],
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