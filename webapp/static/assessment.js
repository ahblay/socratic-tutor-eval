/**
 * assessment.js — Pre-session assessment loop.
 *
 * Drives the question/answer cycle using a promise-based one-shot
 * form listener so the assessment loop can await user input without
 * attaching a permanent handler.
 */

const Assessment = (() => {
  async function start(sessionId) {
    return apiFetch(`/api/sessions/${sessionId}/assessment/start`, {
      method: "POST",
    });
  }

  async function answer(sessionId, text) {
    return apiFetch(`/api/sessions/${sessionId}/assessment/answer`, {
      method: "POST",
      body: JSON.stringify({ answer: text }),
    });
  }

  async function complete(sessionId) {
    return apiFetch(`/api/sessions/${sessionId}/assessment/complete`, {
      method: "POST",
    });
  }

  /**
   * Orchestrate the full assessment loop.
   * Renders questions as tutor bubbles, awaits user input, loops until done.
   * Calls App.startTutoring() when assessment is complete.
   */
  async function runLoop(sessionId) {
    Chat.setPhaseLabel("Assessment");
    Chat.unlockInput();

    // Start assessment — get the opener question
    let resp;
    try {
      resp = await start(sessionId);
    } catch (e) {
      Chat.appendBubble("system", "Could not start assessment. Please refresh.");
      return;
    }

    // Assessment question/answer loop
    while (true) {
      Chat.appendBubble("tutor", resp.question_text);

      const userText = await _waitForSubmit();
      Chat.appendBubble("user", userText);
      Chat.showThinking();
      Chat.lockInput();

      try {
        resp = await answer(sessionId, userText);
      } catch (e) {
        Chat.hideThinking();
        Chat.appendBubble("system", `Assessment error: ${e.message}`);
        Chat.unlockInput();
        return;
      }

      Chat.hideThinking();

      if (resp.assessment_complete) {
        break;
      }
    }

    // Finalize
    Chat.appendBubble("system", "Completing assessment…");
    try {
      await complete(sessionId);
    } catch (e) {
      Chat.appendBubble("system", `Could not finalize assessment: ${e.message}`);
      return;
    }

    Chat.appendBubble("system", "Assessment complete. Starting your session.");

    // Fetch the tutor's opening question before unlocking the input bar
    Chat.showThinking();
    try {
      const openData = await apiFetch(`/api/sessions/${sessionId}/open`, { method: "POST" });
      Chat.hideThinking();
      Chat.appendBubble("tutor", openData.reply);
    } catch (e) {
      Chat.hideThinking();
      Chat.appendBubble("system", `Could not load opening question: ${e.message}`);
    }

    App.startTutoring();
  }

  /**
   * Returns a Promise that resolves with the next non-empty textarea submission.
   * Removes itself after firing once.
   */
  function _waitForSubmit() {
    return new Promise((resolve) => {
      const form    = document.getElementById("form-chat");
      const textarea = document.getElementById("inp-chat");

      function handler(e) {
        e.preventDefault();
        const text = textarea.value.trim();
        if (!text) return;
        textarea.value = "";
        form.removeEventListener("submit", handler);
        resolve(text);
      }

      form.addEventListener("submit", handler);
    });
  }

  return { runLoop };
})();
