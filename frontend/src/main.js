import "leaflet/dist/leaflet.css";
import "./style.css";

import { getProvinces, listScenarios } from "./api.js";
import { MapView } from "./mapView.js";
import { ScenarioEditor } from "./scenarioEditor.js";
import { GameView } from "./gameView.js";
import { ReviewView } from "./reviewView.js";
import { Tour } from "./tour.js";

function switchTab(name) {
  document.querySelectorAll(".tab-button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll(".tab-panel").forEach((p) => p.classList.toggle("active", p.id === `tab-${name}`));
}

async function main() {
  const mapView = new MapView("map");
  const provinces = await getProvinces();
  mapView.loadProvinces(provinces.features);

  const reviewView = new ReviewView();
  await reviewView.refreshGameList();

  const gameView = new GameView({
    mapView,
    onReviewRequested: async (gameId) => {
      switchTab("review");
      await reviewView.selectGame(gameId);
    },
  });
  await gameView.refreshGameList();

  const scenarioListEl = document.getElementById("scenario-list");
  const startGameButton = document.getElementById("start-game-button");
  let selectedScenario = null; // { id, maxTurns }

  async function refreshScenarioList() {
    const scenarios = await listScenarios();
    scenarioListEl.innerHTML = scenarios
      .map(
        (scenario) => `
        <li>
          <button type="button" class="scenario-list-item" data-scenario-id="${scenario.id}"
                  data-max-turns="${scenario.max_turns}">
            ${scenario.name} (${scenario.factions.length} factions, ${scenario.max_turns} turns)
          </button>
        </li>`
      )
      .join("");
    scenarioListEl.querySelectorAll(".scenario-list-item").forEach((button) => {
      button.addEventListener("click", () => {
        selectedScenario = { id: button.dataset.scenarioId, maxTurns: Number(button.dataset.maxTurns) };
        startGameButton.disabled = false;
        scenarioListEl.querySelectorAll("button").forEach((b) => b.classList.remove("selected"));
        button.classList.add("selected");
      });
    });
  }
  await refreshScenarioList();

  new ScenarioEditor({
    mapView,
    onSaved: async (scenario) => {
      await refreshScenarioList();
      window.alert(`Scenario "${scenario.name}" saved.`);
    },
  });

  startGameButton.addEventListener("click", async () => {
    if (!selectedScenario) return;
    startGameButton.disabled = true;
    switchTab("games");
    try {
      await gameView.startFromScenario(selectedScenario.id, selectedScenario.maxTurns);
      await reviewView.refreshGameList();
    } catch (err) {
      window.alert(`Could not start game: ${err.message}`);
    } finally {
      startGameButton.disabled = false;
    }
  });

  document.querySelectorAll(".tab-button").forEach((button) => {
    button.addEventListener("click", () => {
      switchTab(button.dataset.tab);
      if (button.dataset.tab === "review") reviewView.refreshGameList();
    });
  });

  const tour = new Tour({ onStepChange: (tab) => { if (tab) switchTab(tab); } });
  document.getElementById("help-tour-button").addEventListener("click", () => tour.start());
  tour.startIfFirstVisit();
}

main().catch((err) => {
  console.error(err);
  document.getElementById("app").innerHTML =
    `<p class="fatal-error">Failed to start: ${err.message}. Is the backend running ` +
    `(uvicorn backend.main:app --reload)?</p>`;
});
