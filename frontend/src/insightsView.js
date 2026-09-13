import { getRolePresetInsights } from "./api.js";
import { escapeHtml } from "./utils.js";

/** Cross-game role-preset performance — distinct from the Review tab's
 * per-game breakdown, this answers "does this role preset play this way
 * on average, across every game evaluated so far," not just in one
 * playthrough. Pure display: all the aggregation happens server-side in
 * GET /insights/role-presets.
 */
export class InsightsView {
  constructor() {
    this.containerEl = document.getElementById("insights-content");
  }

  async refresh() {
    const data = await getRolePresetInsights();
    if (data.role_presets.length === 0) {
      this.containerEl.innerHTML =
        `<p class="hint">No evaluated games yet — run evaluation on a game in the Review tab ` +
        `to start building this table.</p>`;
      return;
    }

    const metricNames = [...new Set(data.role_presets.map((r) => r.metric_name))];
    const rolePresets = [...new Set(data.role_presets.map((r) => r.role_preset))].sort();
    const cellFor = (rolePreset, metricName) =>
      data.role_presets.find((r) => r.role_preset === rolePreset && r.metric_name === metricName);

    this.containerEl.innerHTML = `
      <p class="hint">
        Averaged across ${data.games_analyzed} evaluated game${data.games_analyzed === 1 ? "" : "s"}.
      </p>
      <table class="insights-table">
        <thead>
          <tr><th>Role preset</th>${metricNames.map((n) => `<th>${escapeHtml(n)}</th>`).join("")}</tr>
        </thead>
        <tbody>
          ${rolePresets
            .map((rolePreset) => {
              const cells = metricNames
                .map((name) => {
                  const cell = cellFor(rolePreset, name);
                  return cell
                    ? `<td>${(cell.avg_score * 100).toFixed(0)}%
                        <span class="insights-sample-count">(n=${cell.sample_count})</span></td>`
                    : `<td>—</td>`;
                })
                .join("");
              return `<tr><th>${escapeHtml(rolePreset.replace(/_/g, " "))}</th>${cells}</tr>`;
            })
            .join("")}
        </tbody>
      </table>
    `;
  }
}
