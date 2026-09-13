// Thin fetch wrappers over backend/main.py's REST API. No business logic
// lives here — every rule (what's a legal move, when a game ends, ...) is
// server-side; this module just shapes the HTTP calls.

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || "http://localhost:8000";

async function request(path, options = {}) {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || `${response.status} ${response.statusText}`);
  }
  if (response.status === 204) return null;
  return response.json();
}

export function getProvinces() {
  return request("/map/provinces");
}

export function listScenarios() {
  return request("/scenarios");
}

export function createScenario(payload) {
  return request("/scenarios", { method: "POST", body: JSON.stringify(payload) });
}

export function listGames() {
  return request("/games");
}

export function getGame(gameId) {
  return request(`/games/${gameId}`);
}

export function getGameEvents(gameId) {
  return request(`/games/${gameId}/events?limit=1000`);
}

export function startGame(payload) {
  return request("/games", { method: "POST", body: JSON.stringify(payload) });
}

export function gameLiveSocketUrl(gameId) {
  const wsBase = API_BASE_URL.replace(/^http/, "ws");
  return `${wsBase}/games/${gameId}/live`;
}

export function evaluateGame(gameId) {
  return request(`/games/${gameId}/evaluate`, { method: "POST" });
}

export function createAnnotation(eventId, payload) {
  return request(`/events/${eventId}/annotations`, { method: "POST", body: JSON.stringify(payload) });
}

export function getGameDiplomacy(gameId) {
  return request(`/games/${gameId}/diplomacy`);
}

export function getRolePresetInsights() {
  return request("/insights/role-presets");
}
