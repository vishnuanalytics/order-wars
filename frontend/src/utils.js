/** Escape user-controlled text (scenario/faction names) before interpolating
 * it into innerHTML — everything rendered this way originates from a form
 * the user just typed into, or from another user via the shared backend.
 */
export function escapeHtml(value) {
  const div = document.createElement("div");
  div.textContent = value ?? "";
  return div.innerHTML;
}

/** Mirrors game/narrative.py's classify_event (Stage 11) — used only for
 * live WebSocket events, which carry a raw action_type/resolution rather
 * than the backend-computed notable/headline that REST-fetched events
 * already have (see backend/main.py's GET /games/{id}/events). Keeping
 * this a direct port of the same markers, not an independent guess, is
 * what keeps live and replay narrative tagging consistent — see
 * game/narrative.py's docstring for why these markers are coupled to
 * game/rules.py's exact resolution wording.
 */
const NOTABLE_MARKERS = [
  ["began a siege", "siege begins"],
  ["pressed the siege", "siege continues"],
  ["broke the siege", "siege succeeds — province captured"],
  ["lost the siege", "siege fails"],
  ["rebelled and reverted to unclaimed", "rebellion"],
  ["call to arms", "call to arms"],
  ["agreed a trade", "trade agreement reached"],
  ["agreed to a alliance", "alliance formed"],
  ["agreed to a truce", "truce declared"],
  ["accepted", "tribute accepted — peace"],
];

/** Plain-English explanations of eval/metrics.py's metrics, for the Review
 * tab's glossary and the onboarding tour — kept in one place so both stay
 * in sync with what the backend actually scores. New metric_name values
 * just fall back to no glossary entry rather than breaking the UI.
 */
export const METRIC_GLOSSARY = {
  "Legal Action": {
    cost: "free",
    text:
      "Rule-based, no LLM call. 1.0 if the agent's proposed move was legal " +
      "as given; 0.0 if the game engine had to quietly downgrade it to a " +
      "safe “hold” because it named an illegal target (e.g. a " +
      "province it can't reach).",
  },
  "Resource Efficiency": {
    cost: "free",
    text:
      "Rule-based, no LLM call. 1.0 if a legal action actually executed as " +
      "intended; 0.0 if it was legal but still wasted the turn — " +
      "proposing a build or development the faction couldn't afford, or " +
      "targeting a province it doesn't own.",
  },
  "Role Alignment": {
    cost: "LLM call",
    text:
      "LLM-judged. Scores 0-1 on how well the chosen action and its stated " +
      "rationale reflect the faction's assigned role preset — e.g. an " +
      "Isolationist declaring an unprovoked war should score low even if " +
      "the move itself was perfectly legal.",
  },
};

export function classifyEvent(actionType, resolution) {
  if (actionType === "declare_war") return { notable: true, headline: "war declared" };
  const text = resolution || "";
  for (const [marker, headline] of NOTABLE_MARKERS) {
    if (text.includes(marker)) return { notable: true, headline };
  }
  return { notable: false, headline: null };
}
