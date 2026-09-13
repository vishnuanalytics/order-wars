import { startGame, getGame, getGameEvents, listGames, gameLiveSocketUrl } from "./api.js";
import { escapeHtml } from "./utils.js";

export class GameView {
  constructor({ mapView }) {
    this.mapView = mapView;
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
        await this.refreshGameDetail();
        await this.refreshGameList();
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
    li.textContent =
      `Turn ${event.turn} — ${actor}: ${event.action_type}` +
      (target ? ` -> ${target}` : "") +
      (event.resolution ? ` (${event.resolution})` : "");
    this.eventLogEl.prepend(li);
  }

  async refreshGameDetail() {
    const game = await getGame(this.currentGameId);
    this.setStatus(`${game.status} — turn ${game.current_turn}`, game.status);

    this.factionNameById = {};
    for (const faction of game.factions) this.factionNameById[faction.id] = faction.faction_name;

    const ownerByProvinceId = {};
    for (const factionState of game.faction_states) {
      for (const provinceId of factionState.territory) {
        ownerByProvinceId[provinceId] = factionState.faction_name;
      }
    }
    this.mapView.setOwnership(ownerByProvinceId);

    this.gameDetailEl.innerHTML = `
      <ul class="faction-summary">
        ${game.factions
          .map((faction) => {
            const swatch = this.mapView.colorFor(faction.faction_name);
            const status = faction.is_alive
              ? "alive"
              : `eliminated (turn ${faction.eliminated_at_turn})`;
            const isWinner = faction.id === game.winner_faction_id;
            return `<li><span class="swatch" style="background:${swatch}"></span>
              ${escapeHtml(faction.faction_name)} (${escapeHtml(faction.role_preset)}) — ${status}
              ${isWinner ? " 🏆" : ""}</li>`;
          })
          .join("")}
      </ul>
    `;
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
