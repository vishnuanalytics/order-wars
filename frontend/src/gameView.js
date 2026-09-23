import {
  startGame,
  getGame,
  getGameDiplomacy,
  getGameEvents,
  getGameSnapshots,
  listGames,
  gameLiveSocketUrl,
} from "./api.js";
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
    this.replayControlsEl = document.getElementById("replay-controls");
    this.replayPlayPauseButton = document.getElementById("replay-play-pause");
    this.replaySpeedSelect = document.getElementById("replay-speed");
    this.replayScrubberEl = document.getElementById("replay-scrubber");
    this.replayPositionEl = document.getElementById("replay-position");

    this.socket = null;
    this.currentGameId = null;
    // Replay-scrubbing state (loadReplay only) — the full event list plus
    // every turn's FactionStateSnapshot (not just the latest), so stepping
    // to any index can recolor the map as it actually looked at that point
    // instead of only ever showing the game's final board.
    this.replayEvents = [];
    this.replaySnapshots = [];
    this.replayTimer = null;
    this.replayIndex = -1;
    // Predict-the-winner mini-game state — purely client-side, reset per
    // watched/replayed game; a live viewer's guess vs. the actual outcome
    // is only meaningful for the one game currently open.
    this.prediction = null;
    // Cached once a finished game's notable events have been fetched for
    // the completion banner's highlights reel, keyed by game id so
    // refreshGameDetail's repeated calls while showing a finished game
    // don't refetch every time.
    this.highlightsByGameId = new Map();
    // {gameId, promise} for the events fetch loadReplay starts up front, so
    // the highlights reel reuses it instead of fetching the same list again.
    this.replayEventsRequest = null;
    // "Pick a province on the map" state for an action form's
    // target_province field — see _armProvincePicker. Which faction's form
    // (by id) is currently picking, and the onProvinceClick handler to
    // restore once picking ends (cancelled, completed, or the game
    // switches). null/null means nobody is picking right now.
    this.pickingProvinceForFaction = null;
    this.pickingRestoreHandler = null;
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
    this.prediction = this._loadPrediction(gameId);
    if (this.pickingProvinceForFaction) this._stopProvincePicking();
    this._stopReplay();
    this.replayControlsEl.hidden = true;
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

  /** A finished game's full history, scrubbable/playable turn by turn —
   * not just dumped into the log at once. Needs every turn's
   * FactionStateSnapshot (see backend's GET /games/{id}/snapshots), not
   * only the latest one refreshGameDetail's getGame call already fetches,
   * so the map can show ownership as it actually looked at any point, not
   * only the game's final board.
   */
  async loadReplay(gameId, prefetchedGame = null) {
    // Start the two slow fetches now, in parallel with refreshGameDetail's
    // own requests, rather than after it — each is a round trip to Neon,
    // and serializing them was most of a finished game's load time.
    const eventsPromise = getGameEvents(gameId);
    const snapshotsPromise = getGameSnapshots(gameId);
    // Already handled via Promise.all below; this only stops an early
    // rejection (if refreshGameDetail throws first) being reported unhandled.
    eventsPromise.catch(() => {});
    snapshotsPromise.catch(() => {});
    this.replayEventsRequest = { gameId, promise: eventsPromise };

    this.currentGameId = gameId;
    this._closeSocket();
    this.eventLogEl.innerHTML = "";
    this.tickerEl.hidden = true;
    this.controlledFactions = new Set();
    // Not just resetting to null: a replay can be revisiting a game the
    // viewer watched live and predicted on earlier — load whatever's
    // stored so the reveal is still correct now, rather than either
    // leaking a stale prediction from whatever game was watched most
    // recently (the actual bug this replaced) or always showing nothing.
    this.prediction = this._loadPrediction(gameId);
    if (this.pickingProvinceForFaction) this._stopProvincePicking();
    await this.refreshGameDetail(prefetchedGame); // populates factionNameById before the log needs it

    const [events, snapshots] = await Promise.all([eventsPromise, snapshotsPromise]);
    this.replayEvents = events;
    this.replaySnapshots = snapshots;
    this._wireReplayControls();

    if (events.length === 0) {
      this.replayControlsEl.hidden = true;
      return;
    }
    this.replayControlsEl.hidden = false;
    this.replayScrubberEl.max = String(events.length - 1);
    this._showReplayStep(events.length - 1); // start fully revealed, matching the old dump-everything behavior
  }

  /** Ownership as of a given turn, derived from every faction's most
   * recent snapshot at or before that turn — a snapshot doesn't exist for
   * every single turn number for a faction (e.g. one eliminated earlier),
   * so this can't just filter for turn === N.
   */
  _ownershipAsOfTurn(turn) {
    const latestByFaction = new Map();
    for (const snapshot of this.replaySnapshots) {
      if (snapshot.turn > turn) continue;
      const current = latestByFaction.get(snapshot.faction_id);
      if (!current || snapshot.turn > current.turn) latestByFaction.set(snapshot.faction_id, snapshot);
    }
    const ownerByProvinceId = {};
    for (const snapshot of latestByFaction.values()) {
      for (const provinceId of snapshot.territory) ownerByProvinceId[provinceId] = snapshot.faction_name;
    }
    return ownerByProvinceId;
  }

  _showReplayStep(index) {
    this.replayIndex = Math.max(0, Math.min(index, this.replayEvents.length - 1));
    const event = this.replayEvents[this.replayIndex];

    this.eventLogEl.innerHTML = "";
    for (let i = 0; i <= this.replayIndex; i++) {
      const e = this.replayEvents[i];
      this._appendLogEntry({
        turn: e.turn,
        faction_id: e.faction_id,
        action_type: e.event_type,
        resolution: e.payload?.resolution,
        target_province: e.payload?.target_province,
        target_faction: e.payload?.target_faction,
        specialist: e.payload?.specialist,
        notable: e.notable,
        headline: e.headline,
      });
    }
    this.mapView.setOwnership(this._ownershipAsOfTurn(event.turn));
    this.replayScrubberEl.value = String(this.replayIndex);
    this.replayPositionEl.textContent =
      `Turn ${event.turn} — event ${this.replayIndex + 1} of ${this.replayEvents.length}`;
  }

  _wireReplayControls() {
    this._stopReplay();
    this.replayPlayPauseButton.onclick = () => {
      if (this.replayTimer) this._stopReplay();
      else this._startReplay();
    };
    this.replayScrubberEl.oninput = () => {
      this._stopReplay();
      this._showReplayStep(Number(this.replayScrubberEl.value));
    };
    this.replaySpeedSelect.onchange = () => {
      if (this.replayTimer) {
        this._stopReplay();
        this._startReplay();
      }
    };
  }

  _startReplay() {
    if (this.replayIndex >= this.replayEvents.length - 1) this.replayIndex = -1; // restart from the beginning
    this.replayPlayPauseButton.textContent = "⏸ Pause";
    const speedMs = Number(this.replaySpeedSelect.value);
    this.replayTimer = setInterval(() => {
      if (this.replayIndex >= this.replayEvents.length - 1) {
        this._stopReplay();
        return;
      }
      this._showReplayStep(this.replayIndex + 1);
    }, speedMs);
  }

  _stopReplay() {
    if (this.replayTimer) {
      clearInterval(this.replayTimer);
      this.replayTimer = null;
    }
    this.replayPlayPauseButton.textContent = "▶ Play";
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

  /** The finished game's notable moments (war declared, sieges, rebellions,
   * agreements — see game/narrative.py), for the completion banner's
   * highlights reel — a short recap instead of just "X wins!" with nothing
   * about how the game actually went. Cached per game id since
   * refreshGameDetail can be called repeatedly while showing the same
   * finished game (e.g. from the games list) without needing a refetch.
   */
  async _highlightsHtml(gameId) {
    if (!this.highlightsByGameId.has(gameId)) {
      const pending = this.replayEventsRequest;
      const events = await (pending && pending.gameId === gameId ? pending.promise : getGameEvents(gameId));
      const notable = events.filter((e) => e.notable);
      this.highlightsByGameId.set(gameId, notable);
    }
    const notable = this.highlightsByGameId.get(gameId);
    if (notable.length === 0) return "";
    return `
      <div class="highlights-reel">
        <h4>Highlights</h4>
        <ul>
          ${notable
            .map(
              (e) =>
                `<li>Turn ${e.turn} — ${escapeHtml(this.factionNameById[e.faction_id] || "")}: ${escapeHtml(e.headline || e.event_type)}</li>`
            )
            .join("")}
        </ul>
      </div>
    `;
  }

  /** `prefetchedGame`: a getGame() result the caller already has for this
   * same game (the games-list click fetches it to pick live vs. replay),
   * so it isn't fetched a second time. Only used if its id matches.
   */
  async refreshGameDetail(prefetchedGame = null) {
    const formSnapshot = this._snapshotActionForms();
    const reuse = prefetchedGame && prefetchedGame.id === this.currentGameId;
    const [game, diplomacy] = await Promise.all([
      reuse ? prefetchedGame : getGame(this.currentGameId),
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
    // Live spectating only — control and prediction both stop meaning
    // anything once a game is over or being replayed.
    const canTakeControl = this.socket !== null && game.status === "running";

    const predictionReveal =
      isFinished && this.prediction
        ? this.prediction === game.winner_faction_id
          ? `<p class="prediction-reveal prediction-correct">🎯 You called it — you predicted ${escapeHtml(this.factionNameById[this.prediction] || "")}.</p>`
          : `<p class="prediction-reveal prediction-wrong">You predicted ${escapeHtml(this.factionNameById[this.prediction] || "")} — not this time.</p>`
        : "";
    const highlightsHtml = isFinished ? await this._highlightsHtml(game.id) : "";
    const banner = isFinished
      ? `<div class="game-complete-banner">
          <strong>${game.status === "failed" ? "Game failed." : winner ? `🏆 ${escapeHtml(winner.faction_name)} wins!` : "Game complete — no winner (max turns reached)."}</strong>
          ${predictionReveal}
          ${highlightsHtml}
          <button type="button" class="review-this-game-button" data-game-id="${game.id}">
            See how each faction was scored →
          </button>
        </div>`
      : "";

    const predictionPicker =
      canTakeControl && !isFinished
        ? `<div class="prediction-picker">
            <span>Predict the winner:</span>
            ${game.factions
              .map(
                (f) =>
                  `<button type="button" class="prediction-button ${this.prediction === f.id ? "picked" : ""}"
                     data-faction-id="${f.id}">${escapeHtml(f.faction_name)}</button>`
              )
              .join("")}
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
      ${predictionPicker}
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
                ${isControlled ? `<span class="human-controlled-badge">🎮 human-controlled</span>` : ""}
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
    this.gameDetailEl.querySelectorAll(".prediction-button").forEach((button) => {
      button.addEventListener("click", async () => {
        this.prediction = button.dataset.factionId;
        this._savePrediction(this.currentGameId, this.prediction);
        await this.refreshGameDetail();
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
          <div class="target-province-row">
            <input type="text" name="target_province" placeholder="e.g. 831e80fffffffff" />
            <button type="button" class="pick-target-province-button">📍 Pick on map</button>
          </div>
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

    // "Pick on map" — target_province is a real province id
    // (e.g. "831e80fffffffff"), not discoverable by typing; this borrows
    // MapView's single onProvinceClick slot (otherwise owned by
    // ScenarioEditor, which safely no-ops unless it's actively picking a
    // scenario's home province — see its _handleMapClick) for exactly one
    // click, then hands it back.
    //
    // Picking state lives on `this` (the GameView instance), not a
    // per-form closure: refreshGameDetail rebuilds every .action-form's
    // DOM on every live event (any faction's turn, not just this one), so
    // a closure captured by the form that existed *before* that rebuild
    // would end up pointing MapView's click handler at a now-detached,
    // never-rendered-again form — clicking a province would silently do
    // nothing visible. _armProvincePicker below re-establishes picking
    // against whatever the *current* form element is, every time this
    // faction's form gets rewired, for as long as picking stays armed.
    const pickButton = form.querySelector(".pick-target-province-button");
    if (pickButton) {
      pickButton.addEventListener("click", () => {
        if (this.pickingProvinceForFaction === form.dataset.factionId) {
          this._stopProvincePicking();
          pickButton.textContent = "📍 Pick on map";
        } else {
          this.pickingProvinceForFaction = form.dataset.factionId;
          this.pickingRestoreHandler = this.mapView.onProvinceClick;
          this._armProvincePicker(form, pickButton);
        }
      });
      if (this.pickingProvinceForFaction === form.dataset.factionId) {
        this._armProvincePicker(form, pickButton); // resume across a rebuild mid-pick
      }
    }

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

  _armProvincePicker(form, button) {
    button.textContent = "Click a province… (cancel)";
    this.mapView.onProvinceClick = (provinceId) => {
      const input = form.querySelector("input[name=target_province]");
      if (input) input.value = provinceId;
      this._stopProvincePicking();
      button.textContent = "📍 Pick on map";
    };
  }

  _stopProvincePicking() {
    this.mapView.onProvinceClick = this.pickingRestoreHandler;
    this.pickingProvinceForFaction = null;
    this.pickingRestoreHandler = null;
  }

  /** sessionStorage, not a bare instance field: reloading the page mid-game
   * used to silently lose a locked-in prediction, discovered while
   * verifying the feature live. Per-tab (sessionStorage, not
   * localStorage) since a prediction is about watching *this* game right
   * now, not a standing preference worth carrying to a new tab/session.
   */
  _savePrediction(gameId, factionId) {
    try {
      sessionStorage.setItem(`orderWarsPrediction:${gameId}`, factionId);
    } catch {
      // best-effort — the reveal just won't survive a reload if this fails
    }
  }

  _loadPrediction(gameId) {
    try {
      return sessionStorage.getItem(`orderWarsPrediction:${gameId}`);
    } catch {
      return null;
    }
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
          await this.loadReplay(gameId, game);
        }
      });
    });
  }
}
