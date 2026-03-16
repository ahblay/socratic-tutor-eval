/**
 * app.js — State machine orchestration.
 *
 * Loaded last. Calls App.init() on DOMContentLoaded.
 * All other modules (Auth, Article, Assessment, Chat) are already loaded.
 */

// ---------------------------------------------------------------------------
// Global state
// ---------------------------------------------------------------------------

const AppState = {
  token:          null,
  userId:         null,

  articleId:      null,
  articleTitle:   null,
  articleSummary: null,
  kcCount:        0,

  sessionId:      null,
  sessionStatus:  null,
  maxTurns:       null,
  turnCount:      0,

  apiKey:         null,
};

// ---------------------------------------------------------------------------
// Central fetch utility
// ---------------------------------------------------------------------------

async function apiFetch(path, options = {}) {
  const headers = {
    ...Auth.getHeaders(),
    ...(options.headers || {}),
  };
  // Merge Content-Type only if not already set and we have a body
  if (options.body && !headers["Content-Type"]) {
    headers["Content-Type"] = "application/json";
  }

  const resp = await fetch(path, { ...options, headers });

  if (resp.status === 401) {
    Auth.clearToken();
    window.location.href = "/";   // full reload re-wires all event listeners
    throw new Error("Session expired — please sign in again");
  }

  if (resp.status === 402) {
    const body = await resp.json().catch(() => ({}));
    throw new Error(body.detail || "Turn budget exhausted");
  }

  if (!resp.ok) {
    const body = await resp.json().catch(() => ({}));
    throw new Error(body.detail || `Error ${resp.status}`);
  }

  return resp.json();
}

// ---------------------------------------------------------------------------
// App
// ---------------------------------------------------------------------------

const App = (() => {
  // Show one phase, hide all others
  function transition(phase) {
    document.querySelectorAll(".phase").forEach(el => el.classList.remove("active"));
    const el = document.getElementById(`phase-${phase}`);
    if (el) el.classList.add("active");
    AppState.phase = phase;
  }

  function showError(msg) {
    document.getElementById("overlay-msg").textContent = msg;
    document.getElementById("error-overlay").classList.remove("hidden");
  }

  // ------------------------------------------------------------------
  // Init
  // ------------------------------------------------------------------

  function init() {
    // Wire global dismiss
    document.getElementById("btn-overlay-dismiss").addEventListener("click", () => {
      document.getElementById("error-overlay").classList.add("hidden");
    });

    const { token, userId, apiKey } = Auth.loadToken();

    if (token && Auth.isTokenValid()) {
      AppState.token  = token;
      AppState.userId = userId;
      AppState.apiKey = apiKey;
      transition("article");
      _wireArticlePhase();
    } else {
      Auth.clearToken();
      transition("auth");
      _wireAuthPhase();
    }
  }

  // ------------------------------------------------------------------
  // Auth phase
  // ------------------------------------------------------------------

  function _wireAuthPhase() {
    const btnShowReg    = document.getElementById("btn-show-register");
    const btnBackLogin  = document.getElementById("btn-show-login-back");
    const formLogin     = document.getElementById("form-login");
    const formRegister  = document.getElementById("form-register");
    const loginError    = document.getElementById("login-error");
    const registerError = document.getElementById("register-error");

    btnShowReg.addEventListener("click", () => {
      formLogin.classList.add("hidden");
      formRegister.classList.remove("hidden");
    });

    btnBackLogin.addEventListener("click", () => {
      formRegister.classList.add("hidden");
      formLogin.classList.remove("hidden");
    });

    formLogin.addEventListener("submit", async (e) => {
      e.preventDefault();
      loginError.classList.add("hidden");
      const email    = document.getElementById("inp-email").value.trim();
      const password = document.getElementById("inp-password").value;
      try {
        await Auth.login(email, password);
        transition("article");
        _wireArticlePhase();
      } catch (err) {
        loginError.textContent = err.message;
        loginError.classList.remove("hidden");
      }
    });

    formRegister.addEventListener("submit", async (e) => {
      e.preventDefault();
      registerError.classList.add("hidden");
      const email     = document.getElementById("inp-reg-email").value.trim();
      const password  = document.getElementById("inp-reg-password").value;
      const consented = document.getElementById("chk-consent").checked;
      if (!consented) {
        registerError.textContent = "You must agree to data collection to register.";
        registerError.classList.remove("hidden");
        return;
      }
      try {
        await Auth.register(email, password, consented);
        transition("article");
        _wireArticlePhase();
      } catch (err) {
        registerError.textContent = err.message;
        registerError.classList.remove("hidden");
      }
    });
  }

  // ------------------------------------------------------------------
  // Article phase
  // ------------------------------------------------------------------

  function _wireArticlePhase() {
    // Pre-fill API key if stored
    const { apiKey } = Auth.loadToken();
    if (apiKey) document.getElementById("inp-api-key").value = apiKey;

    document.getElementById("btn-logout").addEventListener("click", Auth.logout);

    document.getElementById("form-article").addEventListener("submit", async (e) => {
      e.preventDefault();
      await _handleArticleSubmit();
    });

    document.getElementById("btn-start-session").addEventListener("click", async () => {
      await _handleStartSession();
    });
  }

  async function _handleArticleSubmit() {
    const urlInput    = document.getElementById("inp-url").value.trim();
    const apiKeyInput = document.getElementById("inp-api-key").value.trim();
    const maxTurns    = parseInt(document.getElementById("inp-max-turns").value, 10) || 50;
    const errorEl     = document.getElementById("article-error");
    const pollingEl   = document.getElementById("article-polling");
    const cardEl      = document.getElementById("article-card");

    errorEl.classList.add("hidden");
    cardEl.classList.add("hidden");
    pollingEl.classList.add("hidden");

    if (!urlInput) {
      errorEl.textContent = "Please enter a Wikipedia URL or article title.";
      errorEl.classList.remove("hidden");
      return;
    }
    if (!apiKeyInput) {
      errorEl.textContent = "Please enter your Anthropic API key.";
      errorEl.classList.remove("hidden");
      return;
    }

    Auth.saveApiKey(apiKeyInput);
    AppState.apiKey   = apiKeyInput;
    AppState.maxTurns = maxTurns;

    document.getElementById("btn-resolve").disabled = true;

    try {
      const article = await Article.resolve(urlInput);
      AppState.articleId      = article.article_id;
      AppState.articleTitle   = article.title;
      AppState.articleSummary = article.summary || "";

      if (article.domain_map_status === "ready") {
        const full = await Article.get(article.article_id);
        AppState.kcCount = full.kc_count || 0;
        Article.renderCard({ ...article, kc_count: AppState.kcCount });
      } else {
        pollingEl.classList.remove("hidden");
        const full = await Article.poll(article.article_id);
        pollingEl.classList.add("hidden");
        AppState.kcCount = full.kc_count || 0;
        Article.renderCard(full);
      }
    } catch (e) {
      pollingEl.classList.add("hidden");
      errorEl.textContent = e.message;
      errorEl.classList.remove("hidden");
    } finally {
      document.getElementById("btn-resolve").disabled = false;
    }
  }

  async function _handleStartSession() {
    const errorEl = document.getElementById("article-error");
    errorEl.classList.add("hidden");
    document.getElementById("btn-start-session").disabled = true;

    try {
      const session = await apiFetch("/api/sessions", {
        method: "POST",
        body: JSON.stringify({
          article_id: AppState.articleId,
          max_turns:  AppState.maxTurns,
        }),
      });
      AppState.sessionId     = session.session_id;
      AppState.sessionStatus = session.status;
      AppState.turnCount     = session.turn_count;

      _enterChatPhase();
    } catch (e) {
      errorEl.textContent = e.message;
      errorEl.classList.remove("hidden");
      document.getElementById("btn-start-session").disabled = false;
    }
  }

  // ------------------------------------------------------------------
  // Chat phase (assessment → tutoring)
  // ------------------------------------------------------------------

  function _enterChatPhase() {
    transition("chat");
    Chat.clearMessages();
    Chat.setTopic(AppState.articleTitle || "");
    Chat.updateTurnsRemaining(null, null); // hide until tutoring starts

    document.getElementById("btn-logout2").addEventListener("click", Auth.logout);
    document.getElementById("btn-end-session").addEventListener("click", _endSession);

    // Enter submits; Shift+Enter inserts newline
    document.getElementById("inp-chat").addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        document.getElementById("form-chat").requestSubmit();
      }
    });

    Chat.lockInput();
    if (AppState.sessionStatus === "active") {
      _resumeOrOpenSession();
    } else {
      // Start assessment loop; it calls App.startTutoring() when done
      Assessment.runLoop(AppState.sessionId);
    }
  }

  async function _resumeOrOpenSession() {
    // Load existing turns from DB
    Chat.showThinking();
    let hasTurns = false;
    try {
      const transcript = await apiFetch(`/api/sessions/${AppState.sessionId}/transcript`);
      Chat.hideThinking();
      const turns = transcript.turns || [];
      for (const t of turns) {
        if (t.role === "tutor") Chat.appendBubble("tutor", t.content);
        else if (t.role === "user") Chat.appendBubble("user", t.content);
      }
      hasTurns = turns.length > 0;
    } catch (e) {
      Chat.hideThinking();
      Chat.appendBubble("system", `Could not load session history: ${e.message}`);
    }

    if (!hasTurns) {
      // Fresh session — fetch the opener question
      Chat.showThinking();
      try {
        const openData = await apiFetch(`/api/sessions/${AppState.sessionId}/open`, { method: "POST" });
        Chat.hideThinking();
        Chat.appendBubble("tutor", openData.reply);
      } catch (e) {
        Chat.hideThinking();
        Chat.appendBubble("system", `Could not load opening question: ${e.message}`);
      }
    }

    startTutoring();
  }

  function startTutoring() {
    AppState.sessionStatus = "active";
    Chat.setPhaseLabel("Tutoring");
    Chat.updateTurnsRemaining(
      AppState.maxTurns - AppState.turnCount,
      AppState.maxTurns,
    );
    document.getElementById("btn-end-session").classList.remove("hidden");
    Chat.unlockInput();

    // Wire the persistent turn handler
    document.getElementById("form-chat").addEventListener("submit", _handleTurn);
  }

  async function _handleTurn(e) {
    e.preventDefault();
    const textarea = document.getElementById("inp-chat");
    const text     = textarea.value.trim();
    if (!text) return;

    textarea.value = "";
    Chat.appendBubble("user", text);
    Chat.showThinking();
    Chat.lockInput();

    try {
      const data = await apiFetch(`/api/sessions/${AppState.sessionId}/turn`, {
        method: "POST",
        body: JSON.stringify({ message: text }),
      });

      AppState.turnCount = data.turn_number;
      Chat.hideThinking();
      Chat.appendBubble("tutor", data.reply);
      Chat.updateTurnsRemaining(
        AppState.maxTurns - AppState.turnCount,
        AppState.maxTurns,
      );
      Chat.unlockInput();
    } catch (e) {
      Chat.hideThinking();
      if (e.message.includes("budget")) {
        Chat.appendBubble("system", "Turn budget reached. Session ended.");
        _endSession();
      } else {
        Chat.appendBubble("system", `Error: ${e.message}`);
        Chat.unlockInput();
      }
    }
  }

  async function _endSession() {
    document.getElementById("btn-end-session").classList.add("hidden");
    Chat.lockInput();
    try {
      await apiFetch(`/api/sessions/${AppState.sessionId}/end`, { method: "POST" });
    } catch { /* best-effort */ }

    transition("ended");
    document.getElementById("ended-summary").textContent =
      `You completed ${AppState.turnCount} turn${AppState.turnCount !== 1 ? "s" : ""} on "${AppState.articleTitle}".`;

    document.getElementById("btn-new-article").addEventListener("click", _resetForNewArticle);
  }

  function _resetForNewArticle() {
    AppState.articleId      = null;
    AppState.articleTitle   = null;
    AppState.articleSummary = null;
    AppState.sessionId      = null;
    AppState.sessionStatus  = null;
    AppState.turnCount      = 0;

    // Reset article form UI
    document.getElementById("inp-url").value       = "";
    document.getElementById("article-error").classList.add("hidden");
    document.getElementById("article-card").classList.add("hidden");
    document.getElementById("article-polling").classList.add("hidden");
    document.getElementById("btn-resolve").disabled = false;
    document.getElementById("btn-start-session").disabled = false;
    document.getElementById("btn-end-session").classList.add("hidden");

    // Remove old turn handler to avoid duplicate listeners
    const form = document.getElementById("form-chat");
    form.replaceWith(form.cloneNode(true));

    transition("article");
  }

  return { init, transition, showError, startTutoring };
})();

// ---------------------------------------------------------------------------
// Bootstrap
// ---------------------------------------------------------------------------

document.addEventListener("DOMContentLoaded", App.init);
