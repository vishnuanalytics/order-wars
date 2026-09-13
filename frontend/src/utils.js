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

export function classifyEvent(actionType, resolution) {
  if (actionType === "declare_war") return { notable: true, headline: "war declared" };
  const text = resolution || "";
  for (const [marker, headline] of NOTABLE_MARKERS) {
    if (text.includes(marker)) return { notable: true, headline };
  }
  return { notable: false, headline: null };
}
