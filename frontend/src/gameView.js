import { startGame, getGame, getGameDiplomacy, getGameEvents, listGames, gameLiveSocketUrl } from "./api.js";
import { classifyEvent, escapeHtml } from "./utils.js";

export class GameView {
  constructor({ mapView, onGameComplete, onReviewRequested }) {
    this.mapView = mapView;
    // Called once, when a watched (live) game finishes — see watchGame's
    // stream_end handler. Not fired for loadReplay, since that's already
    // the user re-visiting a finished game, not discovering it just ended.
    this.onGameComplete = onGameComplete || (() => {});
    // Called when the user clicks the completion banner's "see how each
    // faction was scored" button — main.js wires this to switch to the
    // Review tab with this game preselected.
    this.onReviewRequested = onReviewRequested || (() => {});
    this.statusEl = document.getElementById("game-status");
    this.gameListEl = document.getElementById("game-list");
    this.gameDetailEl = document.getElementById("game-detail");
    this.eventLogEl = document.getElementById("event-log");

    this.socket = null;
    this.currentGameId = null;
    // Populated by refreshGameDetail(): db faction id (uuid) -> faction_name.
    // Only useful for replay's event log — GameEvent.faction_id (REST) is
    // the db uuid, but a live WS message's last_event.faction_id is the
    // short app-level slug (e.g. "rome"), a different id space entirely,
    // so this lookup naturally no-ops for live events and just falls back
    // to displaying the slug as-is, which is already reasonably readable.
    this.factionNameById = {};
  }

  async startFromScenario(scenarioId, maxTurns) {
    const { game_id } = await startGame({ scenario_id: scenarioId, max_turns: maxTurns });
    await this.watchGame(game_id);
    await this.refreshGameList();
  }

  /** Connect live to a running game (or one that might start running very
   * soon) — GameHub has no backlog for a subscriber that joins late (see
   * CLAUDE.md's Persistence/backend notes), so this may only catch the
   * tail end of a fast game. refreshGameDetail() below is what keeps the
   * map/status correct regardless of exactly which live messages arrive.
   */
  async watchGame(gameId) {
    this.currentGameId = gameId;
    this.eventLogEl.innerHTML = "";
    this._closeSocket();
    await this.refreshGameDetail();

    this.socket = new WebSocket(gameLiveSocketUrl(gameId));
    this.socket.addEventListener("message", async (event) => {
      const message = JSON.parse(event.data);
      if (message.type === "state") {
        this._appendLogEntry(message.last_event);
        await this.refreshGameDetail();
      } else if (message.type === "stream_end") {
        this._closeSocket();
        const game = await this.refreshGameDetail();
        await this.refreshGameList();
        if (game.status === "completed" || game.status === "failed") this.onGameComplete(game);
      } else if (message.type === "error") {
        this.setStatus(`error: ${message.message}`, "failed");
      }
    });
    this.socket.addEventListener("error", () => this.setStatus("connection error", "failed"));
  }

  /** Show a finished game's full history at once — no live connection. */
  async loadReplay(gameId) {
    this.currentGameId = gameId;
    this._closeSocket();
    this.eventLogEl.innerHTML = "";
    await this.refreshGameDetail(); // populates factionNameById before the log needs it
    const events = await getGameEvents(gameId);
    for (const event of events) {
      this._appendLogEntry({
        turn: event.turn,
        faction_id: event.faction_id,
        action_type: event.event_type,
        resolution: event.payload?.resolution,
        target_province: event.payload?.target_province,
        target_faction: event.payload?.target_faction,
        specialist: event.payload?.specialist,
        notable: event.notable,
        headline: event.headline,
      });
    }
  }

  _closeSocket() {
    if (this.socket) {
      this.socket.close();
      this.socket = null;
    }
  }

  _appendLogEntry(event) {
    if (!event) return; // the initial pre-game state carries no event
    const li = document.createElement("li");
    const actor = this.factionNameById[event.faction_id] || event.faction_id;
    const target = event.target_province || event.target_faction || "";
    // REST-fetched events (replay) already carry backend-computed
    // notable/headline; live WebSocket events don't (see
    // game/narrative.py's docstring), so compute them client-side with
    // the same markers instead — Stage 11.
    const tag = event.notable !== undefined
      ? { notable: event.notable, headline: event.headline }
      : classifyEvent(event.action_type, event.resolution);
    if (tag.notable) li.classList.add("notable-event");
    li.textContent =
      `Turn ${event.turn} — ${actor}` +
      (event.specialist ? ` [${event.specialist}]` : "") +
      `: ${event.action_type}` +
      (target ? ` -> ${target}` : "") +
      (event.resolution ? ` (${event.resolution})` : "") +
      (tag.notable && tag.headline ? ` ★ ${tag.headline}` : "");
    this.eventLogEl.prepend(li);
  }

  async refreshGameDetail() {
    const [game, diplomacy] = await Promise.all([
      getGame(this.currentGameId),
      getGameDiplomacy(this.currentGameId),
    ]);
    this.setStatus(`${game.status} — turn ${game.current_turn}`, game.status);

    this.factionNameById = {};
    for (const faction of game.factions) this.factionNameById[faction.id] = faction.faction_name;

    const ownerByProvinceId = {};
    const stateByFactionId = {};
    for (const factionState of game.faction_states) {
      stateByFactionId[factionState.faction_id] = factionState;
      for (const provinceId of factionState.territory) {
        ownerByProvinceId[provinceId] = factionState.faction_name;
      }
    }
    this.mapView.setOwnership(ownerByProvinceId);

    // A direct port of game/rules.py's faction_power (territory + units +
    // resources/10) — a simple "who's currently ahead" heuristic, not the
    // matchup-specific combat math. Client-side because the backend
    // doesn't expose it directly; keeping the same formula, not inventing
    // a new one, is what keeps this bar meaningful rather than decorative.
    const powerByFactionId = {};
    for (const factionState of game.faction_states) {
      const resourceTotal = Object.values(factionState.resources).reduce((a, b) => a + b, 0);
      powerByFactionId[factionState.faction_id] =
        factionState.territory.length + factionState.unit_count + resourceTotal / 10;
    }
    const maxPower = Math.max(1, ...Object.values(powerByFactionId));

    const isFinished = game.status === "completed" || game.status === "failed";
    const winner = game.factions.find((f) => f.id === game.winner_faction_id);
    const banner = isFinished
      ? `<div class="game-complete-banner">
          <strong>${game.status === "failed" ? "Game failed." : winner ? `🏆 ${escapeHtml(winner.faction_name)} wins!` : "Game complete — no winner (max turns reached)."}</strong>
          <button type="button" class="review-this-game-button" data-game-id="${game.id}">
            See how each faction was scored →
          </button>
        </div>`
      : "";

    // War/truce/alliance status between every faction pair — only the
    // non-neutral pairs (see backend/main.py's get_game_diplomacy), so an
    // N-faction game with mostly-neutral relations doesn't drown this in
    // uninteresting rows.
    const DIPLOMACY_LABEL = { war: "at war with", truce: "in a truce with", alliance: "allied with" };
    const diplomacyHtml = diplomacy.length
      ? `<div class="diplomacy-panel">
          <h4>Diplomacy</h4>
          <ul>
            ${diplomacy
              .map(
                (rel) =>
                  `<li class="diplomacy-${escapeHtml(rel.status)}">
                    ${escapeHtml(rel.faction_a_name)} ${DIPLOMACY_LABEL[rel.status] || rel.status} ${escapeHtml(rel.faction_b_name)}
                    <span class="diplomacy-turn">since turn ${rel.turn_changed}</span>
                  </li>`
              )
              .join("")}
          </ul>
        </div>`
      : "";

    this.gameDetailEl.innerHTML = `
      ${banner}
      <ul class="faction-summary">
        ${game.factions
          .map((faction) => {
            const swatch = this.mapView.colorFor(faction.faction_name);
            const status = faction.is_alive
              ? "alive"
              : `eliminated (turn ${faction.eliminated_at_turn})`;
            const isWinner = faction.id === game.winner_faction_id;
            const state = stateByFactionId[faction.id];
            const resources = state
              ? Object.entries(state.resources)
                  .map(([resource, amount]) => `${amount} ${resource}`)
                  .join(", ")
              : "";
            const units = state ? `${state.unit_count} units` : "";
            const power = powerByFactionId[faction.id];
            const powerBar =
              power !== undefined
                ? `<div class="power-bar-track" title="Power ${power.toFixed(1)} (territory + units + resources/10)">
                     <span class="power-bar-fill" style="width:${((power / maxPower) * 100).toFixed(0)}%;background:${swatch}"></span>
                   </div>`
                : "";
            return `<li><span class="swatch" style="background:${swatch}"></span>
              ${escapeHtml(faction.faction_name)} (${escapeHtml(faction.role_preset)}) — ${status}
              ${isWinner ? " 🏆" : ""}
              ${resources ? `<div class="faction-resources">${escapeHtml(resources)} — ${escapeHtml(units)}</div>` : ""}
              ${powerBar}
              </li>`;
          })
          .join("")}
      </ul>
      ${diplomacyHtml}
    `;
    const reviewButton = this.gameDetailEl.querySelector(".review-this-game-button");
    if (reviewButton) {
      reviewButton.addEventListener("click", () => this.onReviewRequested(reviewButton.dataset.gameId));
    }
    return game;
  }

  setStatus(text, statusClass) {
    this.statusEl.textContent = text;
    this.statusEl.className = `game-status status-${statusClass}`;
  }

  async refreshGameList() {
    const games = await listGames();
    this.gameListEl.innerHTML = games
      .map(
        (game) => `
        <li>
          <button type="button" class="game-list-item" data-game-id="${game.id}">
            ${game.id.slice(0, 8)} — ${game.status}${game.winner_faction_id ? " 🏆" : ""}
          </button>
        </li>`
      )
      .join("");
    this.gameListEl.querySelectorAll(".game-list-item").forEach((button) => {
      button.addEventListener("click", async () => {
        const gameId = button.dataset.gameId;
        const game = await getGame(gameId);
        if (game.status === "running") {
          await this.watchGame(gameId);
        } else {
          await this.loadReplay(gameId);
        }
      });
    });
  }
}
