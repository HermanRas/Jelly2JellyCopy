// Shared helpers for every page. Page-specific code lives in its own file.
"use strict";

// Toast: short-lived feedback in the corner, e.g. "Queued 12 episodes".
function toast(message, kind = "ok") {
  const el = document.createElement("div");
  el.className = `toast toast-${kind}`;
  el.textContent = message;
  document.body.appendChild(el);
  setTimeout(() => el.classList.add("show"), 10);
  setTimeout(() => { el.classList.remove("show"); setTimeout(() => el.remove(), 300); }, 4000);
}
