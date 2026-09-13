// Thin fetch wrappers over backend/main.py's REST API. No business logic
// lives here — every rule (what's a legal move, when a game ends, ...) is
// server-side; this module just shapes the HTTP calls.

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || "http://localhost:8000";

// Set by auth.js whenever sign-in state changes (on load from storage, on
// sign-in, on sign-out) — kept here rather than auth.js importing this
// module's `request` directly, so api.js never has to import auth.js back
// (which would be circular, since auth.js already needs signInWithGoogle
// from here). Harmless to attach on every request, signed in or not: every
// route treats a missing/absent Authorization header as anonymous.
let authToken = null;

export function setAuthToken(token) {
  authToken = token;
}

async function request(path, options = {}) {
  const headers = { "Content-Type": "application/json", ...options.headers };
  if (authToken) headers["Authorization"] = `Bearer ${authToken}`;
  const response = await fetch(`${API_BASE_URL}${path}`, { ...options, headers });
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

export function getRivers() {
  return request("/map/rivers");
}

export function getCities() {
  return request("/map/cities");
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

export function getGameSnapshots(gameId) {
  return request(`/games/${gameId}/snapshots`);
}

export function getRolePresetInsights() {
  return request("/insights/role-presets");
}

export function signInWithGoogle(idToken) {
  return request("/auth/google", { method: "POST", body: JSON.stringify({ id_token: idToken }) });
}

export function getCurrentUser() {
  return request("/auth/me");
}
