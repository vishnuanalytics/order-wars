/** A short, dismissible first-run walkthrough across the tabs.
 *
 * Deliberately not framework-driven (matches the rest of frontend/ — plain
 * DOM, no state library): a fixed overlay + a small card, `_render()` swaps
 * the card's content and re-renders on every step rather than diffing,
 * since the whole thing is at most 6 steps and never stays mounted long.
 *
 * The point isn't just "what does each tab do" (that's mostly guessable
 * from the labels) — it's specifically calling out *why* the Review tab
 * exists: DeepEval scores every AI decision automatically, and annotation
 * is where a human adds judgment DeepEval can't. That's the one thing a
 * new visitor won't discover just by clicking around.
 */
const STORAGE_KEY = "orderWarsTourSeen";

const STEPS = [
  {
    tab: null,
    title: "Welcome to Order Wars",
    body:
      "A multi-agent strategy sim: you configure factions, each one run by " +
      "an AI agent with its own role and personality, and watch them " +
      "expand, trade, ally, and go to war over a real map. This tour is " +
      "five short stops.",
  },
  {
    tab: "scenario",
    title: "1. Scenario editor",
    body:
      "Add factions, give each one a role preset (Expansionist, Warmonger, " +
      "Diplomat-Trader, Isolationist…), set starting resources, and click " +
      "the map to place each faction's capital — this also claims a small " +
      "starting territory around it, adjustable by hand. Save it, then " +
      "start a game from it.",
  },
  {
    tab: "games",
    title: "2. Games",
    body:
      "Watch a game live turn-by-turn on the map, or pick a finished one " +
      "to replay its full history. Each log line shows which specialist " +
      "agent (military / economic / diplomatic) made the call.",
  },
  {
    tab: "review",
    title: "3. Review — this is the important one",
    body:
      "Every AI decision gets scored automatically by DeepEval (free, " +
      "rule-based checks plus one LLM-judged metric — see “What do " +
      "these metrics mean?” on this tab). Then you add your own " +
      "annotation: a 1-5 rating and a note. That combination — automatic " +
      "scoring plus human judgment — is what “annotation and eval” means here.",
  },
  {
    tab: "insights",
    title: "4. Insights",
    body:
      "A cross-game view: how does each role preset tend to score, " +
      "averaged over every decision it's made across every evaluated " +
      "game — not just one playthrough. This is where patterns in agent " +
      "behavior actually become visible.",
  },
  {
    tab: null,
    title: "That's it",
    body: "Start a scenario whenever you're ready. You can replay this tour anytime from the ? button.",
  },
];

export class Tour {
  constructor({ onStepChange }) {
    this.onStepChange = onStepChange || (() => {});
    this.stepIndex = 0;
    this.overlayEl = null;
  }

  hasBeenSeen() {
    try {
      return localStorage.getItem(STORAGE_KEY) === "1";
    } catch {
      return false; // e.g. a private window blocking storage — just show it every time
    }
  }

  startIfFirstVisit() {
    if (!this.hasBeenSeen()) this.start();
  }

  start() {
    this.stepIndex = 0;
    if (!this.overlayEl) this._mount();
    this.overlayEl.hidden = false;
    this._render();
  }

  _mount() {
    this.overlayEl = document.createElement("div");
    this.overlayEl.className = "tour-overlay";
    this.overlayEl.hidden = true;
    document.body.appendChild(this.overlayEl);
  }

  _finish() {
    try {
      localStorage.setItem(STORAGE_KEY, "1");
    } catch {
      // best-effort only — not critical if it can't persist
    }
    this.overlayEl.hidden = true;
  }

  _render() {
    const step = STEPS[this.stepIndex];
    this.onStepChange(step.tab);

    const isLast = this.stepIndex === STEPS.length - 1;
    this.overlayEl.innerHTML = `
      <div class="tour-card">
        <p class="tour-progress">Step ${this.stepIndex + 1} of ${STEPS.length}</p>
        <h3>${step.title}</h3>
        <p>${step.body}</p>
        <div class="tour-actions">
          <button type="button" class="tour-skip">Skip tour</button>
          <div>
            ${this.stepIndex > 0 ? `<button type="button" class="tour-back">Back</button>` : ""}
            <button type="button" class="tour-next">${isLast ? "Done" : "Next"}</button>
          </div>
        </div>
      </div>
    `;
    this.overlayEl.querySelector(".tour-skip").addEventListener("click", () => this._finish());
    this.overlayEl.querySelector(".tour-next").addEventListener("click", () => {
      if (isLast) {
        this._finish();
      } else {
        this.stepIndex += 1;
        this._render();
      }
    });
    const backButton = this.overlayEl.querySelector(".tour-back");
    if (backButton) {
      backButton.addEventListener("click", () => {
        this.stepIndex -= 1;
        this._render();
      });
    }
  }
}
