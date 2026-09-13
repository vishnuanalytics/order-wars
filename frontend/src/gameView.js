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
    this.tickerEl = document.getElementById("live-ticker");

    this.socket = null;
    this.currentGameId = null;
    // DB faction ids (uuids) currently human-controlled, from the
    // control_state snapshot a fresh connection gets and control_changed
    // broadcasts thereafter — see watchGame. No-auth, like everything else
    // here: "controlled" means whoever last clicked "Play as", not tied to
    // a signed-in identity.
    this.controlledFactions = new Set();
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
    this.tickerEl.hidden = true;
    this.controlledFactions = new Set();
    this._closeSocket();
    await this.refreshGameDetail();

    this.socket = new WebSocket(gameLiveSocketUrl(gameId));
    this.socket.addEventListener("message", async (event) => {
      const message = JSON.parse(event.data);
      if (message.type === "state") {
        const tag = this._appendLogEntry(message.last_event);
        if (tag) this._showTicker(tag);
        await this.refreshGameDetail();
      } else if (message.type === "control_state") {
        this.controlledFactions = new Set(message.controlled_factions);
        await this.refreshGameDetail();
      } else if (message.type === "control_changed") {
        if (message.human_controlled) this.controlledFactions.add(message.faction_id);
        else this.controlledFactions.delete(message.faction_id);
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

  _sendControlMessage(message) {
    if (this.socket && this.socket.readyState === WebSocket.OPEN) {
      this.socket.send(JSON.stringify(message));
    }
  }

  /** Show a finished game's full history at once — no live connection. */
  async loadReplay(gameId) {
    this.currentGameId = gameId;
    this._closeSocket();
    this.eventLogEl.innerHTML = "";
    this.tickerEl.hidden = true;
    this.controlledFactions = new Set();
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

  /** Returns the notable/headline tag it used, so live callers can also
   * drive the ticker (_showTicker) off the same classification without
   * recomputing it.
   */
  _appendLogEntry(event) {
    if (!event) return null; // the initial pre-game state carries no event
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
    return { ...tag, actor, turn: event.turn };
  }

  /** Flashes the most recent notable moment as a large, animated banner —
   * live spectating only (see watchGame): a scrolling event log rewards
   * reading closely, this rewards glancing over. Re-triggers its entrance
   * animation on every call, including repeats of the same text, via the
   * same force-reflow pattern as reviewView.js's badge toast.
   */
  _showTicker(tag) {
    if (!this.tickerEl || !tag.notable) return;
    this.tickerEl.hidden = false;
    this.tickerEl.textContent = `★ Turn ${tag.turn} — ${tag.actor}: ${tag.headline}`;
    this.tickerEl.classList.remove("live-ticker-pulse");
    // eslint-disable-next-line no-unused-expressions
    this.tickerEl.offsetHeight;
    this.tickerEl.classList.add("live-ticker-pulse");
  }

  /** refreshGameDetail rebuilds the faction panel's innerHTML on every
   * live event (any faction's turn, not just a human-controlled one) — an
   * in-progress action form would otherwise reset mid-type. Snapshot
   * before the rebuild, restore after.
   */
  _snapshotActionForms() {
    const snapshot = {};
    this.gameDetailEl.querySelectorAll(".action-form").forEach((form) => {
      snapshot[form.dataset.factionId] = Object.fromEntries(new FormData(form).entries());
    });
    return snapshot;
  }

  _restoreActionForms(snapshot) {
    this.gameDetailEl.querySelectorAll(".action-form").forEach((form) => {
      const saved = snapshot[form.dataset.factionId];
      if (!saved) return;
      for (const [name, value] of Object.entries(saved)) {
        const field = form.elements.namedItem(name);
        if (field) field.value = value;
      }
      form.dispatchEvent(new Event("action-type-restored"));
    });
  }

  async refreshGameDetail() {
    const formSnapshot = this._snapshotActionForms();
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

    // Live-play control is only meaningful while actually spectating a
    // running game — a finished/replayed game has no turn loop left to
    // hand a turn to, so no button/form renders for those.
    const canTakeControl = this.socket !== null && game.status === "running";

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
            const isControlled = this.controlledFactions.has(faction.id);
            const controlButton =
              canTakeControl && faction.is_alive
                ? `<button type="button" class="control-toggle-button ${isControlled ? "release" : "take"}"
                     data-faction-id="${faction.id}" data-action="${isControlled ? "release_control" : "take_control"}">
                     ${isControlled ? "Release control" : "Play as this faction"}
                   </button>`
                : "";
            const otherFactions = game.factions.filter((f) => f.id !== faction.id);
            const controlForm = isControlled ? this._actionFormHtml(faction, otherFactions) : "";
            return `<li>
              <div class="faction-row-header">
                <span class="swatch" style="background:${swatch}"></span>
                ${escapeHtml(faction.faction_name)} (${escapeHtml(faction.role_preset)}) — ${status}
                ${isWinner ? " 🏆" : ""}
                ${isControlled ? `<span class="human-controlled-badge">🎮 you</span>` : ""}
              </div>
              ${resources ? `<div class="faction-resources">${escapeHtml(resources)} — ${escapeHtml(units)}</div>` : ""}
              ${powerBar}
              ${controlButton}
              ${controlForm}
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
    this.gameDetailEl.querySelectorAll(".control-toggle-button").forEach((button) => {
      button.addEventListener("click", () => {
        this._sendControlMessage({ type: button.dataset.action, faction_id: button.dataset.factionId });
      });
    });
    this.gameDetailEl.querySelectorAll(".action-form").forEach((form) => {
      this._wireActionForm(form);
    });
    this._restoreActionForms(formSnapshot);
    return game;
  }

  /** The full FactionAction shape (agents/actions.py) — a human playing a
   * faction isn't restricted to whichever specialist the dispatcher would
   * have picked that turn, unlike the AI (a deliberate choice, see the
   * commit this shipped in). Fields irrelevant to the selected action_type
   * are hidden, not removed, so switching action_type back doesn't lose
   * what was typed.
   */
  _actionFormHtml(faction, otherFactions) {
    const factionOptions = otherFactions
      .map((f) => `<option value="${f.id}">${escapeHtml(f.faction_name)}</option>`)
      .join("");
    return `
      <form class="action-form" data-faction-id="${faction.id}">
        <label>
          Action
          <select class="action-type-field" name="action_type">
            <option value="hold">Hold</option>
            <option value="move_army">Move army</option>
            <option value="build_unit">Build unit</option>
            <option value="develop_province">Develop province</option>
            <option value="negotiate">Negotiate</option>
            <option value="declare_war">Declare war</option>
          </select>
        </label>
        <label class="action-field field-target_province" hidden>
          Target province id
          <input type="text" name="target_province" placeholder="e.g. 831e80fffffffff" />
        </label>
        <label class="action-field field-unit_type" hidden>
          Unit type
          <select name="unit_type">
            <option value="legion">Legion</option>
            <option value="cavalry">Cavalry</option>
            <option value="siege_engine">Siege engine</option>
          </select>
        </label>
        <label class="action-field field-target_faction" hidden>
          Target faction
          <select name="target_faction">${factionOptions}</select>
        </label>
        <label class="action-field field-proposal" hidden>
          Proposal
          <select name="proposal">
            <option value="truce">Truce</option>
            <option value="alliance">Alliance</option>
            <option value="trade">Trade</option>
            <option value="tribute">Tribute</option>
          </select>
        </label>
        <label class="action-field field-offer_resource" hidden>
          Offer resource
          <select name="offer_resource">
            <option value="gold">Gold</option>
            <option value="grain">Grain</option>
            <option value="iron">Iron</option>
          </select>
        </label>
        <label class="action-field field-offer_amount" hidden>
          Offer amount
          <input type="number" name="offer_amount" min="0" value="0" />
        </label>
        <label class="action-field-rationale">
          Rationale (optional)
          <input type="text" name="rationale" placeholder="Why this move?" />
        </label>
        <button type="submit">Submit for your next turn</button>
        <p class="hint">
          Sent whenever you like — it's used the next time this is your turn.
        </p>
      </form>
    `;
  }

  _wireActionForm(form) {
    const FIELDS_BY_ACTION = {
      hold: [],
      move_army: ["target_province"],
      build_unit: ["unit_type"],
      develop_province: ["target_province"],
      negotiate: ["target_faction", "proposal", "offer_resource", "offer_amount"],
      declare_war: ["target_faction"],
    };
    const typeSelect = form.querySelector(".action-type-field");
    const updateVisibleFields = () => {
      const visible = new Set(FIELDS_BY_ACTION[typeSelect.value] || []);
      form.querySelectorAll(".action-field").forEach((field) => {
        const fieldName = [...field.classList].find((c) => c.startsWith("field-")).slice("field-".length);
        field.hidden = !visible.has(fieldName);
      });
    };
    typeSelect.addEventListener("change", updateVisibleFields);
    // Fired by _restoreActionForms after setting the action_type field's
    // value back from a pre-rebuild snapshot — the visible-field set
    // depends on whatever action_type ends up selected, restored or not.
    form.addEventListener("action-type-restored", updateVisibleFields);
    updateVisibleFields();

    form.addEventListener("submit", (event) => {
      event.preventDefault();
      const data = new FormData(form);
      const action = { action_type: data.get("action_type"), rationale: data.get("rationale") || "Player choice." };
      const visible = new Set(FIELDS_BY_ACTION[action.action_type] || []);
      for (const field of ["target_province", "unit_type", "target_faction", "proposal", "offer_resource"]) {
        if (visible.has(field)) action[field] = data.get(field) || null;
      }
      if (visible.has("offer_amount")) action.offer_amount = Number(data.get("offer_amount")) || 0;
      this._sendControlMessage({
        type: "submit_action",
        faction_id: form.dataset.factionId,
        action,
      });
      form.querySelector("button[type=submit]").textContent = "Queued ✓";
      setTimeout(() => {
        const button = form.querySelector("button[type=submit]");
        if (button) button.textContent = "Submit for your next turn";
      }, 1500);
    });
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
