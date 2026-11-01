// Shared helpers for every page. Page-specific code lives in its own file.
"use strict";

// Toast: short-lived feedback in the corner, e.g. "Queued 12 episodes".
function toast(message, kind = "ok") {
  // One stack in the corner, so several toasts in a row don't overlap.
  let stack = document.getElementById("toasts");
  if (!stack) {
    stack = document.createElement("div");
    stack.id = "toasts";
    document.body.appendChild(stack);
  }
  const el = document.createElement("div");
  el.className = `toast toast-${kind}`;
  el.textContent = message;
  stack.appendChild(el);
  setTimeout(() => el.classList.add("show"), 10);
  setTimeout(() => { el.classList.remove("show"); setTimeout(() => el.remove(), 300); }, 4000);
}

// Header queue counter: how much is still to do, on every page.
async function refreshQueueBadge() {
  const badge = document.getElementById("queue-badge");
  if (!badge) return;
  try {
    const r = await fetch("/api/queue/counts");
    if (!r.ok) return;
    const { counts, paused } = await r.json();
    const pending = (counts.queued || 0) + (counts.downloading || 0) + (counts.paused || 0);
    badge.hidden = pending === 0;
    badge.querySelector("span").textContent = paused ? `${pending} paused` : `${pending} in queue`;
  } catch (e) {
    // Offline for a moment; the next poll catches up.
  }
}

if (document.getElementById("queue-badge")) {
  refreshQueueBadge();
  setInterval(refreshQueueBadge, 5000);
}
