import { createAnnotation, evaluateGame, getGame, getGameEvents, listGames } from "./api.js";
import { escapeHtml } from "./utils.js";

export class ReviewView {
  constructor() {
    this.gameSelect = document.getElementById("review-game-select");
    this.runButton = document.getElementById("run-evaluation-button");
    this.eventsEl = document.getElementById("review-events");
    this.factionNameById = {};
    this.selectedGameId = null;

    this.gameSelect.addEventListener("change", () => this._selectGame(this.gameSelect.value));
    this.runButton.addEventListener("click", () => this._runEvaluation());
  }

  async refreshGameList() {
    const games = await listGames();
    const previousValue = this.gameSelect.value;
    this.gameSelect.innerHTML =
      `<option value="">Select a game…</option>` +
      games.map((g) => `<option value="${g.id}">${g.id.slice(0, 8)} — ${g.status}</option>`).join("");
    if (games.some((g) => g.id === previousValue)) {
      this.gameSelect.value = previousValue;
    }
  }

  async _selectGame(gameId) {
    this.selectedGameId = gameId || null;
    this.runButton.disabled = !this.selectedGameId;
    if (!this.selectedGameId) {
      this.eventsEl.innerHTML = "";
      return;
    }
    const game = await getGame(this.selectedGameId);
    this.factionNameById = {};
    for (const faction of game.factions) this.factionNameById[faction.id] = faction.faction_name;
    await this._loadEvents();
  }

  async _runEvaluation() {
    if (!this.selectedGameId) return;
    this.runButton.disabled = true;
    this.runButton.textContent = "Running…";
    try {
      await evaluateGame(this.selectedGameId);
      await this._loadEvents();
    } catch (err) {
      window.alert(`Evaluation failed: ${err.message}`);
    } finally {
      this.runButton.disabled = false;
      this.runButton.textContent = "Run evaluation";
    }
  }

  async _loadEvents() {
    const events = await getGameEvents(this.selectedGameId);
    this.eventsEl.innerHTML = events.map((event) => this._renderEvent(event)).join("");
    this.eventsEl.querySelectorAll(".annotation-form").forEach((form) => {
      form.addEventListener("submit", (event) => this._handleAnnotationSubmit(event, form.dataset.eventId));
    });
  }

  _renderEvent(event) {
    const factionName = this.factionNameById[event.faction_id] || event.faction_id || "—";
    const scoresHtml = event.eval_scores
      .map(
        (score) =>
          `<span class="score-badge ${score.success ? "score-ok" : "score-low"}" ` +
          `title="${escapeHtml(score.reason || "")}">${escapeHtml(score.metric_name)}: ${score.score.toFixed(2)}</span>`
      )
      .join(" ");
    const annotationsHtml = event.annotations
      .map(
        (a) =>
          `<li class="annotation-item">${a.rating ? `★${a.rating} ` : ""}${escapeHtml(a.note || "")}` +
          `${a.created_by ? ` — <em>${escapeHtml(a.created_by)}</em>` : ""}</li>`
      )
      .join("");

    return `
      <div class="review-event">
        <div class="review-event-header">
          <strong>Turn ${event.turn}</strong> — ${escapeHtml(factionName)}: ${escapeHtml(event.event_type)}
        </div>
        <div class="review-event-rationale">${escapeHtml(event.payload?.rationale || "")}</div>
        <div class="review-scores">${scoresHtml || "<em>Not evaluated yet.</em>"}</div>
        <ul class="annotation-list">${annotationsHtml}</ul>
        <form class="annotation-form" data-event-id="${event.id}">
          <select class="annotation-rating" aria-label="Rating">
            <option value="">Rating…</option>
            ${[1, 2, 3, 4, 5].map((n) => `<option value="${n}">${n}</option>`).join("")}
          </select>
          <input type="text" class="annotation-note" placeholder="Note (optional)" aria-label="Note" />
          <button type="submit">Add</button>
        </form>
      </div>
    `;
  }

  async _handleAnnotationSubmit(event, eventId) {
    event.preventDefault();
    const form = event.target;
    const rating = form.querySelector(".annotation-rating").value;
    const note = form.querySelector(".annotation-note").value;
    if (!rating && !note) {
      window.alert("Provide a rating, a note, or both.");
      return;
    }
    try {
      await createAnnotation(eventId, { rating: rating ? Number(rating) : null, note: note || null });
      await this._loadEvents();
    } catch (err) {
      window.alert(`Could not save annotation: ${err.message}`);
    }
  }
}
