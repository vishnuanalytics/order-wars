/** Browser-local annotation progress — no login system exists (see
 * CLAUDE.md Non-goals), so this tracks a running total per browser in
 * localStorage rather than per-user, the same convention tour.js already
 * uses for "have they seen the tour." Purely a motivational layer: it
 * never affects what gets persisted server-side, only what this viewer
 * sees about their own reviewing activity.
 */
const STORAGE_KEY = "orderWarsAnnotationCount";

// Roman/strategy flavor, matching the game's theme, in ascending order.
const BADGES = [
  { threshold: 1, name: "First Dispatch", description: "You annotated your first AI decision." },
  { threshold: 5, name: "Field Observer", description: "5 decisions annotated." },
  { threshold: 15, name: "Senate Archivist", description: "15 decisions annotated." },
  { threshold: 30, name: "Legion Historian", description: "30 decisions annotated." },
  { threshold: 60, name: "Master Strategos", description: "60 decisions annotated." },
];

function readCount() {
  try {
    return Number(localStorage.getItem(STORAGE_KEY)) || 0;
  } catch {
    return 0; // e.g. a private window blocking storage — degrade to "no progress yet", not a crash
  }
}

/** Current state for rendering the progress widget: how many annotations
 * so far, the highest badge already earned, and the next one to work
 * toward (null once every badge is earned).
 */
export function getAnnotationStats() {
  const count = readCount();
  const earned = BADGES.filter((b) => count >= b.threshold);
  const next = BADGES.find((b) => b.threshold > count);
  return { count, currentBadge: earned[earned.length - 1] || null, nextBadge: next || null };
}

/** Call once per successful annotation submission. Returns the badge just
 * newly crossed (for a celebratory toast), or null if this annotation
 * didn't cross a new threshold.
 */
export function recordAnnotation() {
  const count = readCount() + 1;
  try {
    localStorage.setItem(STORAGE_KEY, String(count));
  } catch {
    // best-effort only — the count just won't persist across reloads
  }
  return { count, newlyEarned: BADGES.find((b) => b.threshold === count) || null };
}
