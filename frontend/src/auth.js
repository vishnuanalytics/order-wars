import { getCurrentUser, setAuthToken, signInWithGoogle } from "./api.js";
import { escapeHtml } from "./utils.js";

/** Google Sign-In — optional everywhere it touches the rest of the app
 * (see backend/auth.py's module docstring and CLAUDE.md Non-goals): with
 * VITE_GOOGLE_CLIENT_ID unset, this renders nothing and every request
 * just goes out anonymous, exactly as before this feature existed.
 *
 * Loads Google Identity Services dynamically (a plain <script> tag, no
 * npm package) rather than always including it, matching this project's
 * existing pattern of only pulling in an external script when a feature
 * that needs it is actually configured.
 */
const STORAGE_KEY = "orderWarsAuth"; // {session_token, user: {id, name, email, picture_url}}

export class Auth {
  constructor() {
    this.areaEl = document.getElementById("auth-area");
    this.current = null;
  }

  getToken() {
    return this.current?.session_token || null;
  }

  getUser() {
    return this.current?.user || null;
  }

  async init() {
    this._restoreFromStorage();
    if (this.current) await this._verifyStoredSession();
    this._render();

    const clientId = import.meta.env.VITE_GOOGLE_CLIENT_ID;
    if (!clientId || this.current) return; // already signed in, or sign-in not configured — nothing to render
    try {
      await this._loadGoogleScript();
      window.google.accounts.id.initialize({
        client_id: clientId,
        callback: (response) => this._handleCredential(response),
      });
      this._render();
    } catch {
      // Google's script failed to load (offline, blocked, ...) — the rest
      // of the app must keep working anonymously either way.
    }
  }

  signOut() {
    this.current = null;
    this._persist();
    setAuthToken(null);
    this._render();
  }

  _restoreFromStorage() {
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      this.current = raw ? JSON.parse(raw) : null;
    } catch {
      this.current = null;
    }
    setAuthToken(this.getToken());
  }

  /** A stored session token could be stale (expired, or SESSION_SECRET
   * rotated server-side) — confirm it's still good on load rather than
   * showing a signed-in name that every subsequent request would
   * silently fail to actually authenticate as.
   */
  async _verifyStoredSession() {
    try {
      await getCurrentUser();
    } catch {
      this.current = null;
      this._persist();
    }
  }

  _persist() {
    try {
      if (this.current) localStorage.setItem(STORAGE_KEY, JSON.stringify(this.current));
      else localStorage.removeItem(STORAGE_KEY);
    } catch {
      // best-effort — sign-in just won't survive a reload if this fails
    }
  }

  _loadGoogleScript() {
    if (window.google?.accounts?.id) return Promise.resolve();
    return new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = "https://accounts.google.com/gsi/client";
      script.async = true;
      script.defer = true;
      script.onload = resolve;
      script.onerror = reject;
      document.head.appendChild(script);
    });
  }

  async _handleCredential(response) {
    try {
      const auth = await signInWithGoogle(response.credential);
      this.current = auth;
      this._persist();
      setAuthToken(this.getToken());
      this._render();
    } catch (err) {
      window.alert(`Sign-in failed: ${err.message}`);
    }
  }

  _render() {
    if (this.current) {
      const user = this.current.user;
      this.areaEl.innerHTML = `
        <div class="auth-signed-in">
          ${user.picture_url ? `<img class="auth-avatar" src="${escapeHtml(user.picture_url)}" alt="" />` : ""}
          <span class="auth-name">${escapeHtml(user.name)}</span>
          <button type="button" id="auth-sign-out-button" class="link-button">Sign out</button>
        </div>
      `;
      document.getElementById("auth-sign-out-button").addEventListener("click", () => this.signOut());
    } else if (window.google?.accounts?.id) {
      this.areaEl.innerHTML = `<div id="google-signin-button"></div>`;
      window.google.accounts.id.renderButton(document.getElementById("google-signin-button"), {
        theme: "outline",
        size: "medium",
        type: "standard",
      });
    } else {
      this.areaEl.innerHTML = "";
    }
  }
}
