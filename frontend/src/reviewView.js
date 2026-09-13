import { createAnnotation, evaluateGame, getGame, getGameEvents, listGames } from "./api.js";
import { escapeHtml, METRIC_GLOSSARY } from "./utils.js";
import { getAnnotationStats, recordAnnotation } from "./annotationProgress.js";

export class ReviewView {
  constructor() {
    this.gameSelect = document.getElementById("review-game-select");
    this.runButton = document.getElementById("run-evaluation-button");
    this.eventsEl = document.getElementById("review-events");
    this.summaryEl = document.getElementById("review-summary");
    this.filtersEl = document.getElementById("review-filters");
    this.factionFilter = document.getElementById("review-faction-filter");
    this.lowOnlyCheckbox = document.getElementById("review-low-only");
    this.exportCsvButton = document.getElementById("review-export-csv");
    this.glossaryToggle = document.getElementById("metric-glossary-toggle");
    this.glossaryEl = document.getElementById("metric-glossary");
    this.progressWidgetEl = document.getElementById("annotation-progress-widget");
    this.factionNameById = {};
    this.selectedGameId = null;
    this._events = []; // the full, unfiltered list for the current game — filters re-render from this, no refetch

    this.factionFilter.addEventListener("change", () => this._renderEventList());
    this.lowOnlyCheckbox.addEventListener("change", () => this._renderEventList());
    this.exportCsvButton.addEventListener("click", () => this._exportCsv());

    this.glossaryEl.innerHTML = Object.entries(METRIC_GLOSSARY)
      .map(
        ([name, info]) =>
          `<div class="metric-glossary-entry">
            <strong>${escapeHtml(name)}</strong> <span class="metric-cost">(${escapeHtml(info.cost)})</span>
            <p>${escapeHtml(info.text)}</p>
          </div>`
      )
      .join("");
    this.glossaryToggle.addEventListener("click", () => {
      this.glossaryEl.hidden = !this.glossaryEl.hidden;
      this.glossaryToggle.textContent = this.glossaryEl.hidden
        ? "What do these metrics mean?"
        : "Hide metric explanations";
    });

    this.gameSelect.addEventListener("change", () => this._selectGame(this.gameSelect.value));
    this.runButton.addEventListener("click", () => this._runEvaluation());

    this._renderProgressWidget();
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

  /** Preselects a game (e.g. from the "Review this game" prompt on a just-
   * finished game) and loads it, without requiring the user to reopen the
   * dropdown themselves.
   */
  async selectGame(gameId) {
    await this.refreshGameList();
    this.gameSelect.value = gameId;
    await this._selectGame(gameId);
  }

  async _selectGame(gameId) {
    this.selectedGameId = gameId || null;
    this.runButton.disabled = !this.selectedGameId;
    if (!this.selectedGameId) {
      this.eventsEl.innerHTML = "";
      this.summaryEl.innerHTML = "";
      this.filtersEl.hidden = true;
      return;
    }
    const game = await getGame(this.selectedGameId);
    this.factionNameById = {};
    for (const faction of game.factions) this.factionNameById[faction.id] = faction.faction_name;
    this.factionFilter.innerHTML =
      `<option value="">All factions</option>` +
      game.factions.map((f) => `<option value="${f.id}">${escapeHtml(f.faction_name)}</option>`).join("");
    this.factionFilter.value = "";
    this.lowOnlyCheckbox.checked = false;
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
    this._events = await getGameEvents(this.selectedGameId);
    this.filtersEl.hidden = this._events.length === 0;
    this.summaryEl.innerHTML = this._renderSummary(this._events);
    this._renderEventList();
  }

  /** Applies the faction/low-score filters to the already-loaded event list
   * and re-renders — no refetch, so filtering stays instant even on a long
   * game. Separated from _loadEvents so a filter change doesn't need one.
   */
  _renderEventList() {
    const factionId = this.factionFilter.value;
    const lowOnly = this.lowOnlyCheckbox.checked;
    const filtered = this._events.filter((event) => {
      if (factionId && event.faction_id !== factionId) return false;
      if (lowOnly && !event.eval_scores.some((s) => !s.success)) return false;
      return true;
    });

    this.eventsEl.innerHTML =
      filtered.map((event) => this._renderEvent(event)).join("") ||
      `<p class="hint">No decisions match this filter.</p>`;
    this.eventsEl.querySelectorAll(".annotation-form").forEach((form) => {
      form.addEventListener("submit", (event) => this._handleAnnotationSubmit(event, form.dataset.eventId));
    });
  }

  /** An aggregate dashboard above the event list — per-metric pass rate
   * across the whole game (so "am I improving?" is answerable without
   * reading every event), plus how much of the game has been annotated
   * yet, to make annotation feel like measurable progress rather than an
   * open-ended chore.
   */
  _renderSummary(events) {
    const scoredEvents = events.filter((e) => e.eval_scores.length > 0);
    if (scoredEvents.length === 0) {
      return events.length > 0 ? `<p class="hint">Not evaluated yet — run evaluation above.</p>` : "";
    }

    const byMetric = new Map();
    for (const event of scoredEvents) {
      for (const score of event.eval_scores) {
        if (!byMetric.has(score.metric_name)) byMetric.set(score.metric_name, []);
        byMetric.get(score.metric_name).push(score.score);
      }
    }

    const metricRows = [...byMetric.entries()]
      .map(([name, scores]) => {
        const avg = scores.reduce((a, b) => a + b, 0) / scores.length;
        return `<div class="summary-metric">
          <span class="summary-metric-name">${escapeHtml(name)}</span>
          <span class="summary-metric-bar"><span class="summary-metric-fill" style="width:${(avg * 100).toFixed(0)}%"></span></span>
          <span class="summary-metric-value">${(avg * 100).toFixed(0)}%</span>
        </div>`;
      })
      .join("");

    const annotatedCount = events.filter((e) => e.annotations.length > 0).length;

    // Per-faction breakdown is what makes this genuinely useful for
    // *comparing* role presets (e.g. "did the Warmonger really play less
    // legally than the Diplomat-Trader?"), not just an overall grade.
    const metricNames = [...byMetric.keys()];
    const byFactionMetric = new Map(); // faction_id -> metric_name -> scores[]
    for (const event of scoredEvents) {
      if (!event.faction_id) continue;
      if (!byFactionMetric.has(event.faction_id)) byFactionMetric.set(event.faction_id, new Map());
      const forFaction = byFactionMetric.get(event.faction_id);
      for (const score of event.eval_scores) {
        if (!forFaction.has(score.metric_name)) forFaction.set(score.metric_name, []);
        forFaction.get(score.metric_name).push(score.score);
      }
    }
    const factionRows = [...byFactionMetric.entries()]
      .map(([factionId, metrics]) => {
        const cells = metricNames
          .map((name) => {
            const scores = metrics.get(name) || [];
            if (scores.length === 0) return `<td>—</td>`;
            const avg = scores.reduce((a, b) => a + b, 0) / scores.length;
            return `<td>${(avg * 100).toFixed(0)}%</td>`;
          })
          .join("");
        return `<tr><th>${escapeHtml(this.factionNameById[factionId] || factionId)}</th>${cells}</tr>`;
      })
      .join("");

    const byFactionTable =
      byFactionMetric.size > 1
        ? `<table class="summary-by-faction">
            <thead><tr><th>Faction</th>${metricNames.map((n) => `<th>${escapeHtml(n)}</th>`).join("")}</tr></thead>
            <tbody>${factionRows}</tbody>
          </table>`
        : "";

    return `
      <div class="review-summary-box">
        <h3>Game average, by metric</h3>
        ${metricRows}
        ${this._renderScoreTrend(byMetric)}
        ${byFactionTable}
        <p class="summary-annotation-progress">
          ${annotatedCount} of ${events.length} decisions annotated by you.
        </p>
      </div>
    `;
  }

  /** An average is a single number; this is the shape behind it — does
   * this metric trend up, down, or swing wildly across the game, not just
   * where it ended up on average? One sparkline per metric, x = decision
   * order (not turn number, since several factions can share a turn), y =
   * score. Pure inline SVG, no charting library needed for a handful of
   * points.
   */
  _renderScoreTrend(byMetric) {
    const WIDTH = 260;
    const HEIGHT = 36;
    const rows = [...byMetric.entries()]
      .map(([name, scores]) => {
        if (scores.length < 2) return ""; // a trend needs at least two points
        const points = scores
          .map((score, i) => {
            const x = (i / (scores.length - 1)) * WIDTH;
            const y = HEIGHT - score * HEIGHT;
            return `${x.toFixed(1)},${y.toFixed(1)}`;
          })
          .join(" ");
        return `
          <div class="score-trend-row">
            <span class="score-trend-name">${escapeHtml(name)}</span>
            <svg class="score-trend-sparkline" viewBox="0 0 ${WIDTH} ${HEIGHT}" preserveAspectRatio="none">
              <polyline points="${points}" fill="none" stroke="currentColor" stroke-width="1.5" />
            </svg>
          </div>
        `;
      })
      .join("");
    return rows ? `<div class="score-trend"><h4>Trend across the game</h4>${rows}</div>` : "";
  }

  /** All currently-filtered decisions (respects the faction/low-score
   * filters, matching what's visible in the list) as a downloadable CSV —
   * for analyzing DeepEval scores and annotations outside the app.
   */
  _exportCsv() {
    const factionId = this.factionFilter.value;
    const lowOnly = this.lowOnlyCheckbox.checked;
    const filtered = this._events.filter((event) => {
      if (factionId && event.faction_id !== factionId) return false;
      if (lowOnly && !event.eval_scores.some((s) => !s.success)) return false;
      return true;
    });
    if (filtered.length === 0) {
      window.alert("No decisions to export with the current filter.");
      return;
    }

    const metricNames = [...new Set(filtered.flatMap((e) => e.eval_scores.map((s) => s.metric_name)))];
    const header = [
      "turn", "faction", "action_type", "resolution", "rationale",
      ...metricNames.flatMap((m) => [`${m}_score`, `${m}_success`]),
      "annotation_rating", "annotation_note",
    ];
    const csvField = (value) => {
      const text = String(value ?? "");
      return /[",\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
    };
    const rows = filtered.map((event) => {
      const scoreByMetric = Object.fromEntries(event.eval_scores.map((s) => [s.metric_name, s]));
      const annotation = event.annotations[0];
      return [
        event.turn,
        this.factionNameById[event.faction_id] || event.faction_id || "",
        event.event_type,
        event.payload?.resolution || "",
        event.payload?.rationale || "",
        ...metricNames.flatMap((m) => [scoreByMetric[m]?.score ?? "", scoreByMetric[m]?.success ?? ""]),
        annotation?.rating ?? "",
        annotation?.note || "",
      ]
        .map(csvField)
        .join(",");
    });
    const csv = [header.join(","), ...rows].join("\n");

    const blob = new Blob([csv], { type: "text/csv" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `order-wars-${this.selectedGameId.slice(0, 8)}-eval.csv`;
    link.click();
    URL.revokeObjectURL(url);
  }

  _renderEvent(event) {
    const factionName = this.factionNameById[event.faction_id] || event.faction_id || "—";
    // A passing score's reason is unsurprising, so it stays a hover-only
    // tooltip; a low score shows its reason inline — that's the moment a
    // learner actually wants to know *why*, without having to hover.
    const scoresHtml = event.eval_scores
      .map((score) => {
        const badge =
          `<span class="score-badge ${score.success ? "score-ok" : "score-low"}" ` +
          `title="${escapeHtml(score.reason || "")}">${escapeHtml(score.metric_name)}: ${score.score.toFixed(2)}</span>`;
        const inlineReason = !score.success && score.reason
          ? `<span class="score-reason">${escapeHtml(score.reason)}</span>`
          : "";
        return badge + inlineReason;
      })
      .join(" ");
    const annotationsHtml = event.annotations
      .map(
        (a) =>
          `<li class="annotation-item">${a.rating ? `★${a.rating} ` : ""}${escapeHtml(a.note || "")}` +
          `${a.created_by ? ` — <em>${escapeHtml(a.created_by)}</em>` : ""}</li>`
      )
      .join("");

    return `
      <div class="review-event${event.notable ? " notable-event" : ""}">
        <div class="review-event-header">
          <strong>Turn ${event.turn}</strong> — ${escapeHtml(factionName)}: ${escapeHtml(event.event_type)}
          ${event.notable && event.headline ? `<span class="notable-badge">★ ${escapeHtml(event.headline)}</span>` : ""}
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
      const { newlyEarned } = recordAnnotation();
      this._renderProgressWidget();
      if (newlyEarned) this._showBadgeToast(newlyEarned);
      await this._loadEvents();
    } catch (err) {
      window.alert(`Could not save annotation: ${err.message}`);
    }
  }

  /** A small persistent stat pill — "how much reviewing have I done in
   * this browser" — separate from the per-game annotation count in
   * _renderSummary, which resets per selected game; this one is cumulative
   * across every game the viewer has ever annotated here.
   */
  _renderProgressWidget() {
    const { count, currentBadge, nextBadge } = getAnnotationStats();
    if (count === 0) {
      this.progressWidgetEl.innerHTML = "";
      return;
    }
    const badgeText = currentBadge ? `🏅 ${escapeHtml(currentBadge.name)} · ` : "";
    const nextText = nextBadge
      ? `${nextBadge.threshold - count} more to "${escapeHtml(nextBadge.name)}"`
      : "every badge earned";
    this.progressWidgetEl.innerHTML =
      `<span class="progress-count">${badgeText}${count} decision${count === 1 ? "" : "s"} annotated</span>` +
      `<span class="progress-next">${nextText}</span>`;
  }

  _showBadgeToast(badge) {
    const toast = document.createElement("div");
    toast.className = "badge-toast";
    toast.innerHTML = `
      <div class="badge-toast-icon">🏅</div>
      <div>
        <strong>${escapeHtml(badge.name)}</strong>
        <div class="badge-toast-description">${escapeHtml(badge.description)}</div>
      </div>
    `;
    document.body.appendChild(toast);
    // Force a reflow so the enter transition actually plays instead of
    // starting from its own end state (same class of bug as the tour
    // overlay's [hidden] fix — CSS transitions need the "before" state to
    // have painted at least once before the class toggle that starts it).
    // eslint-disable-next-line no-unused-expressions
    toast.offsetHeight;
    toast.classList.add("badge-toast-visible");
    setTimeout(() => {
      toast.classList.remove("badge-toast-visible");
      setTimeout(() => toast.remove(), 400);
    }, 3200);
  }
}
