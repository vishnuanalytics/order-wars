/** Escape user-controlled text (scenario/faction names) before interpolating
 * it into innerHTML — everything rendered this way originates from a form
 * the user just typed into, or from another user via the shared backend.
 */
export function escapeHtml(value) {
  const div = document.createElement("div");
  div.textContent = value ?? "";
  return div.innerHTML;
}
