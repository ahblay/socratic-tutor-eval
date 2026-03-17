/**
 * auth.js — Token management and authentication.
 */

const Auth = (() => {
  const TOKEN_KEY  = "socratic_token";
  const USERID_KEY = "socratic_user_id";

  function loadToken() {
    const token  = localStorage.getItem(TOKEN_KEY);
    const userId = localStorage.getItem(USERID_KEY);
    return { token, userId };
  }

  function saveToken(token, userId) {
    localStorage.setItem(TOKEN_KEY,  token);
    localStorage.setItem(USERID_KEY, userId);
  }

  function clearToken() {
    localStorage.removeItem(TOKEN_KEY);
    localStorage.removeItem(USERID_KEY);
  }

  function getHeaders() {
    const token  = localStorage.getItem(TOKEN_KEY);
    const headers = { "Content-Type": "application/json" };
    if (token) headers["Authorization"] = `Bearer ${token}`;
    return headers;
  }

  function isTokenValid() {
    const token = localStorage.getItem(TOKEN_KEY);
    if (!token) return false;
    try {
      const payload = JSON.parse(atob(token.split(".")[1]));
      // Treat as invalid if expiring within 5 minutes
      return payload.exp * 1000 > Date.now() + 5 * 60 * 1000;
    } catch {
      return false;
    }
  }

  async function login(email, password) {
    const form = new URLSearchParams();
    form.append("username", email);
    form.append("password", password);
    const resp = await fetch("/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: form,
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.detail || "Login failed");
    }
    const data = await resp.json();
    saveToken(data.access_token, data.user_id);
    return data;
  }

  async function register(email, password, consented) {
    const resp = await fetch("/api/auth/register", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password, consented }),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.detail || "Registration failed");
    }
    const data = await resp.json();
    saveToken(data.access_token, data.user_id);
    return data;
  }

  function logout() {
    clearToken();
    window.location.href = "/";
  }

  return { loadToken, saveToken, clearToken, getHeaders, isTokenValid,
           login, register, logout };
})();
