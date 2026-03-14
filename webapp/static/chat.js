/**
 * chat.js — Pure DOM operations for the message list.
 * No API calls. No state mutations.
 */

const Chat = (() => {
  function appendBubble(role, text) {
    const list = document.getElementById("chat-messages");
    const div  = document.createElement("div");
    div.className = `bubble ${role}`;

    // Safe rendering: use textContent, replace \n with <br> via innerHTML
    // only after sanitizing (no HTML in tutor text expected, but be safe)
    if (text.includes("\n")) {
      div.innerHTML = text
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/\n/g, "<br>");
    } else {
      div.textContent = text;
    }

    list.appendChild(div);
    list.scrollTop = list.scrollHeight;
    return div;
  }

  function showThinking() {
    document.getElementById("thinking").classList.remove("hidden");
    document.getElementById("chat-messages").scrollTop =
      document.getElementById("chat-messages").scrollHeight;
  }

  function hideThinking() {
    document.getElementById("thinking").classList.add("hidden");
  }

  function clearMessages() {
    document.getElementById("chat-messages").innerHTML = "";
  }

  function setPhaseLabel(label) {
    const badge = document.getElementById("phase-badge");
    badge.textContent = label;
    badge.className   = "phase-badge " + label.toLowerCase();
  }

  function setTopic(title) {
    document.getElementById("chat-topic").textContent = title;
  }

  function updateTurnsRemaining(remaining, maxTurns) {
    const chip = document.getElementById("turns-remaining");
    if (remaining === null || remaining === undefined) {
      chip.classList.add("hidden");
      return;
    }
    chip.classList.remove("hidden");
    chip.textContent = `${remaining} / ${maxTurns} turns left`;
    chip.classList.toggle("low", remaining <= 5);
  }

  function lockInput() {
    document.getElementById("inp-chat").disabled  = true;
    document.getElementById("btn-send").disabled  = true;
  }

  function unlockInput() {
    document.getElementById("inp-chat").disabled  = false;
    document.getElementById("btn-send").disabled  = false;
    document.getElementById("inp-chat").focus();
  }

  return { appendBubble, showThinking, hideThinking, clearMessages,
           setPhaseLabel, setTopic, updateTurnsRemaining, lockInput, unlockInput };
})();
