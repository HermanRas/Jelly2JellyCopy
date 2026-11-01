// Status page: connect to and disconnect from the source server.
"use strict";

const form = document.getElementById("connect-form");
if (form) {
  const error = document.getElementById("connect-error");
  const button = form.querySelector("button[type=submit]");

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    error.hidden = true;
    button.disabled = true;
    const original = button.innerHTML;
    button.innerHTML = '<i class="bi bi-hourglass-split"></i> Connecting…';
    try {
      const r = await fetch("/api/connect", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          url: form.url.value,
          username: form.username.value,
          password: form.password.value,
        }),
      });
      // The password has done its job either way; don't leave it in the page.
      form.password.value = "";
      if (r.ok) {
        location.reload();
        return;
      }
      const body = await r.json().catch(() => ({}));
      error.textContent = body.detail || `Connect failed (HTTP ${r.status}).`;
      error.hidden = false;
    } catch (e) {
      error.textContent = "Couldn't reach Jelly2JellyCopy itself. Is the container running?";
      error.hidden = false;
    } finally {
      button.disabled = false;
      button.innerHTML = original;
    }
  });
}

const disconnect = document.getElementById("disconnect");
if (disconnect) {
  disconnect.addEventListener("click", async () => {
    if (!confirm("Disconnect? This logs out of the source server and pauses the queue.")) return;
    disconnect.disabled = true;
    await fetch("/api/disconnect", { method: "POST" });
    location.reload();
  });
}
