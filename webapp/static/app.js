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
  token:           null,
  userId:          null,
  isSuperuser:     false,

  articleId:       null,
  articleTitle:    null,
  articleSummary:  null,
  kcCount:         0,

  sessionId:       null,
  sessionStatus:   null,
  turnCount:       0,

  domainMap:       null,
  bktSnapshot:     null,

  reviewerEnabled: true,
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

    const { token, userId } = Auth.loadToken();

    if (token && Auth.isTokenValid()) {
      AppState.token  = token;
      AppState.userId = userId;
      // Fetch superuser status (non-blocking — UI degrades gracefully on failure)
      apiFetch("/api/auth/me").then(me => {
        AppState.isSuperuser = !!me.is_superuser;
      }).catch(() => {});
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
  // Article / catalog phase
  // ------------------------------------------------------------------

  function _wireArticlePhase() {
    document.getElementById("btn-logout").addEventListener("click", Auth.logout);
    _loadCatalog();
  }

  async function _loadCatalog() {
    const loadingEl = document.getElementById("catalog-loading");
    const emptyEl   = document.getElementById("catalog-empty");
    const errorEl   = document.getElementById("article-error");

    errorEl.classList.add("hidden");
    emptyEl.classList.add("hidden");
    loadingEl.classList.remove("hidden");
    AppState.articleId = null;

    try {
      const articles = await apiFetch("/api/articles");
      loadingEl.classList.add("hidden");
      if (articles.length === 0) {
        emptyEl.classList.remove("hidden");
      } else {
        Article.renderCatalog(articles, _onArticleSelected);
      }
    } catch (e) {
      loadingEl.classList.add("hidden");
      errorEl.textContent = e.message;
      errorEl.classList.remove("hidden");
    }
  }

  function _onArticleSelected(article) {
    AppState.articleId      = article.article_id;
    AppState.articleTitle   = article.title;
    AppState.articleSummary = article.summary || "";
    AppState.kcCount        = article.kc_count || 0;
    _handleStartSession();
  }

  async function _handleStartSession() {
    const errorEl = document.getElementById("article-error");
    errorEl.classList.add("hidden");

    try {
      const session = await apiFetch("/api/sessions", {
        method: "POST",
        body: JSON.stringify({ article_id: AppState.articleId }),
      });
      AppState.sessionId     = session.session_id;
      AppState.sessionStatus = session.status;
      AppState.turnCount     = session.turn_count;

      _enterChatPhase();
    } catch (e) {
      errorEl.textContent = e.message;
      errorEl.classList.remove("hidden");
      // Clear loading state on all cards so user can retry
      document.querySelectorAll(".catalog-item.loading").forEach(el => el.classList.remove("loading"));
    }
  }

  // ------------------------------------------------------------------
  // Chat phase (assessment → tutoring)
  // ------------------------------------------------------------------

  function _enterChatPhase() {
    transition("chat");
    Chat.clearMessages();
    Chat.setTopic(AppState.articleTitle || "");

    document.getElementById("btn-logout2").onclick     = Auth.logout;
    document.getElementById("btn-end-session").onclick  = _endSession;
    document.getElementById("btn-back-catalog").onclick = _backToCatalog;

    // Reviewer toggle — superusers only
    const btnReviewer = document.getElementById("btn-reviewer-toggle");
    if (AppState.isSuperuser) {
      AppState.reviewerEnabled = true;
      btnReviewer.classList.remove("hidden");
      btnReviewer.onclick = () => {
        AppState.reviewerEnabled = !AppState.reviewerEnabled;
        btnReviewer.textContent = AppState.reviewerEnabled ? "Reviewer: ON" : "Reviewer: OFF";
        btnReviewer.classList.toggle("reviewer-on",  AppState.reviewerEnabled);
        btnReviewer.classList.toggle("reviewer-off", !AppState.reviewerEnabled);
      };
    } else {
      btnReviewer.classList.add("hidden");
    }

    // Enter submits; Shift+Enter inserts newline
    document.getElementById("inp-chat").addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        document.getElementById("form-chat").requestSubmit();
      }
    });

    // Load graph state (domain map + BKT snapshot) and initialise graph panel
    _loadGraphState();

    // Wire obs panel resize handle and show-all toggle
    _wireObsPanel();

    Chat.lockInput();
    if (AppState.sessionStatus === "active") {
      _resumeOrOpenSession();
    } else {
      // Start assessment loop; it calls App.startTutoring() when done
      Assessment.runLoop(AppState.sessionId);
    }
  }

  async function _loadGraphState() {
    try {
      const gs = await apiFetch(`/api/sessions/${AppState.sessionId}/graph-state`);
      AppState.domainMap   = gs.domain_map;
      AppState.bktSnapshot = gs.bkt_snapshot;
      await KCGraph.init(gs.domain_map);
      await KCGraph.setBKT(gs.bkt_snapshot);
      if (gs.tutor_state) KCGraph.setTutorState(gs.tutor_state);
    } catch (e) {
      // Graph panel is non-critical — silently fail
      console.warn("graph-state fetch failed:", e.message);
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
    document.getElementById("btn-end-session").classList.remove("hidden");
    Chat.unlockInput();

    // (Re-)load graph state: assessment has just written initial knowledge estimates;
    // these are the tutor's starting model and do not change during the session.
    _loadGraphState();

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
        body: JSON.stringify({ message: text, reviewer_enabled: AppState.reviewerEnabled }),
      });

      AppState.turnCount = data.turn_number;
      Chat.hideThinking();
      Chat.appendBubble("tutor", data.reply);
      if (data.tutor_state) KCGraph.setTutorState(data.tutor_state);
      Chat.unlockInput();
    } catch (e) {
      Chat.hideThinking();
      if (e.message.includes("credits") || e.message.includes("No credits")) {
        Chat.appendBubble("system", "You have no credits remaining. Contact the administrator to receive more credits. Your session has been saved and can be resumed.");
        // Session stays active — do not end it; leave input locked until credits are restored
      } else {
        Chat.appendBubble("system", `Error: ${e.message}`);
        Chat.unlockInput();
      }
    }
  }

  function _backToCatalog() {
    // Clear session state — session stays open on the backend (resumable)
    AppState.sessionId     = null;
    AppState.sessionStatus = null;
    AppState.articleId     = null;
    AppState.articleTitle  = null;
    AppState.domainMap     = null;
    AppState.bktSnapshot   = null;
    KCGraph.init(null);
    document.getElementById("tutor-obs-section").classList.add("hidden");
    document.getElementById("obs-resize-handle").classList.add("hidden");
    transition("article");
    _loadCatalog();
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

    document.getElementById("btn-end-session").classList.add("hidden");

    // Remove old turn handler to avoid duplicate listeners
    const form = document.getElementById("form-chat");
    form.replaceWith(form.cloneNode(true));

    transition("article");
    _loadCatalog();
  }

  // ------------------------------------------------------------------
  // Obs panel: resize handle + show-all toggle
  // ------------------------------------------------------------------

  function _wireObsPanel() {
    const handle     = document.getElementById('obs-resize-handle');
    const obsSection = document.getElementById('tutor-obs-section');
    const chatMsgs   = document.getElementById('chat-messages');
    const obsList    = document.getElementById('obs-list');
    const btnToggle  = document.getElementById('btn-obs-toggle');
    if (!handle || !obsSection || !chatMsgs || !obsList || !btnToggle) return;

    function _setExpanded(expanded, maxH) {
      if (expanded) {
        const panelH = chatMsgs.parentElement.getBoundingClientRect().height;
        const h = maxH ?? Math.min(200, Math.floor(panelH * 0.4));
        obsList.style.maxHeight = h + 'px';
        obsList.classList.add('expanded');
        btnToggle.textContent = 'Show less';
      } else {
        obsList.classList.remove('expanded');
        btnToggle.textContent = 'Show all';
      }
    }

    // Show-all toggle
    btnToggle.addEventListener('click', () => {
      _setExpanded(!obsList.classList.contains('expanded'));
    });

    // Drag-to-resize: dragging up increases the expanded obs-list max-height
    let _startY = 0;
    let _startH = 0;

    handle.addEventListener('mousedown', (e) => {
      _startY = e.clientY;
      _startH = obsList.getBoundingClientRect().height;
      handle.classList.add('dragging');
      document.addEventListener('mousemove', _onObsDrag);
      document.addEventListener('mouseup',   _stopObsDrag);
    });

    function _onObsDrag(e) {
      const delta  = _startY - e.clientY;   // drag up → increase height
      const panelH = chatMsgs.parentElement.getBoundingClientRect().height;
      const newH   = Math.max(24, Math.min(_startH + delta, panelH - 120));
      _setExpanded(newH > 30, newH);
    }

    function _stopObsDrag() {
      handle.classList.remove('dragging');
      document.removeEventListener('mousemove', _onObsDrag);
      document.removeEventListener('mouseup',   _stopObsDrag);
    }
  }

  return { init, transition, showError, startTutoring };
})();

// ---------------------------------------------------------------------------
// Bootstrap
// ---------------------------------------------------------------------------

document.addEventListener("DOMContentLoaded", App.init);
