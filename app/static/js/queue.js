// Status page: the live download queue, fed by Server-Sent Events.
"use strict";

const list = document.getElementById("queue-list");
const summary = document.getElementById("queue-summary");
const empty = document.getElementById("queue-empty");
const pausedNote = document.getElementById("queue-paused");
const btnPause = document.getElementById("q-pause");
const btnResume = document.getElementById("q-resume");

function bytes(n) {
  if (n == null) return "?";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1000 && i < units.length - 1) { n /= 1000; i += 1; }
  return `${n.toFixed(i >= 3 ? 2 : i ? 1 : 0)} ${units[i]}`;
}

// Line speeds are quoted in bits, so show transfer rates the same way.
function bitrate(bytesPerSec) {
  let n = bytesPerSec * 8;
  const units = ["bps", "Kbps", "Mbps", "Gbps"];
  let i = 0;
  while (n >= 1000 && i < units.length - 1) { n /= 1000; i += 1; }
  return `${n.toFixed(i ? 1 : 0)} ${units[i]}`;
}

function eta(job) {
  if (!job.speed || !job.size) return "";
  const s = Math.max(0, (job.size - job.bytes_done) / job.speed);
  if (s < 60) return `${Math.round(s)}s left`;
  if (s < 3600) return `${Math.round(s / 60)} min left`;
  return `${(s / 3600).toFixed(1)} h left`;
}

const STATUS_TEXT = {
  queued: "queued", paused: "paused", done: "done", skipped: "already have it",
  failed: "failed", cancelled: "cancelled",
};
const ICON = { episode: "bi-tv", movie: "bi-film", track: "bi-music-note", subtitle: "bi-badge-cc" };

function row(job) {
  const li = document.createElement("li");
  li.className = `job job-${job.status}`;

  const head = document.createElement("div");
  head.className = "job-head";
  const name = document.createElement("span");
  name.className = "job-name";
  name.innerHTML = `<i class="bi ${ICON[job.kind] || "bi-file-earmark"}"></i> `;
  name.append(job.name);
  name.title = `${job.dest_root}/${job.target}`;

  const state = document.createElement("span");
  state.className = "job-state";
  if (job.status === "downloading") {
    const pct = job.size ? Math.floor((job.bytes_done / job.size) * 100) : null;
    state.textContent = [pct != null ? `${pct}%` : bytes(job.bytes_done),
                         job.speed ? bitrate(job.speed) : "", eta(job)].filter(Boolean).join(" · ");
  } else {
    state.textContent = STATUS_TEXT[job.status] || job.status;
    if (job.size && ["queued", "paused"].includes(job.status)) state.textContent += ` · ${bytes(job.size)}`;
  }

  const actions = document.createElement("span");
  actions.className = "job-actions";
  if (["queued", "downloading", "paused"].includes(job.status)) {
    actions.append(button("bi-x-lg", "Cancel", () => post(`/api/queue/jobs/${job.id}/cancel`)));
  } else if (job.status === "failed") {
    actions.append(button("bi-arrow-clockwise", "Retry", () => post(`/api/queue/jobs/${job.id}/retry`)));
  }
  head.append(name, state, actions);
  li.append(head);

  if (job.status === "downloading" || (job.status === "paused" && job.bytes_done)) {
    const bar = document.createElement("div");
    bar.className = "progress";
    const fill = document.createElement("span");
    fill.style.width = job.size ? `${Math.min(100, (job.bytes_done / job.size) * 100)}%` : "100%";
    bar.append(fill);
    li.append(bar);
  }
  if (job.error) {
    const err = document.createElement("p");
    err.className = job.status === "failed" ? "error job-error" : "muted job-error";
    err.textContent = job.error;
    li.append(err);
  }
  return li;
}

function button(icon, label, onClick) {
  const b = document.createElement("button");
  b.className = "icon-btn";
  b.title = label;
  b.setAttribute("aria-label", label);
  b.innerHTML = `<i class="bi ${icon}"></i>`;
  b.addEventListener("click", onClick);
  return b;
}

async function post(url) {
  const r = await fetch(url, { method: "POST" });
  if (r.status === 401) { location.href = "/login"; return; }
  const body = await r.json().catch(() => ({}));
  if (!r.ok) toast(body.detail || `Failed (HTTP ${r.status})`, "error");
  else if (body.jobs) render(body);
}

function render(q) {
  const c = q.counts;
  const parts = [];
  if (c.queued) parts.push(`${c.queued} queued`);
  if (c.downloading) parts.push(`${c.downloading} active`);
  if (c.paused) parts.push(`${c.paused} paused`);
  if (c.failed) parts.push(`${c.failed} failed`);
  const free = Object.values(q.free).filter((v) => v != null);
  if (free.length) parts.push(`${bytes(Math.min(...free))} free`);
  summary.textContent = parts.join(" · ") || "idle";

  pausedNote.hidden = !q.paused;
  pausedNote.querySelector("span").textContent = q.connected
    ? "The queue is paused."
    : "The queue is paused until you connect to a source server.";
  btnPause.hidden = q.paused;
  btnResume.hidden = !q.paused;
  btnResume.disabled = !q.connected;

  list.replaceChildren(...q.jobs.map(row));
  empty.hidden = q.jobs.length > 0;
}

btnPause.addEventListener("click", () => post("/api/queue/pause"));
btnResume.addEventListener("click", () => post("/api/queue/resume"));
document.getElementById("q-retry").addEventListener("click", () => post("/api/queue/retry-failed"));
document.getElementById("q-clear").addEventListener("click", () => post("/api/queue/clear-finished"));

// EventSource reconnects by itself if the stream drops (a container restart,
// a proxy timeout); the first snapshot comes over a plain fetch so the card
// fills in straight away.
fetch("/api/queue").then((r) => r.ok && r.json()).then((q) => q && render(q));
const events = new EventSource("/api/events");
events.addEventListener("queue", (e) => render(JSON.parse(e.data)));
