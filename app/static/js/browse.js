// Library page: destination picker, item list with search, A-Z and paging.
"use strict";

const grid = document.getElementById("items");
const libraryId = grid.dataset.library;
const more = document.getElementById("more");
// Always in the layout (unlike `more`, which hides), so the observer can see it.
const sentinel = document.getElementById("sentinel");
const count = document.getElementById("count");
const search = document.getElementById("search");
const dest = document.getElementById("dest");

const state = { start: 0, total: null, search: "", letter: "", loading: false, generation: 0 };

// The queue buttons (phase 5) read this; empty means "not chosen yet".
window.j2jDest = dest && !dest.disabled ? dest.value : "";

async function api(url, options) {
  const r = await fetch(url, options);
  if (r.status === 401) { location.href = "/login?next=" + encodeURIComponent(location.pathname); throw new Error("login"); }
  if (r.status === 409) { location.href = "/"; throw new Error("reconnect"); }
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.detail || `HTTP ${r.status}`);
  return body;
}

// ---- destination picker ----------------------------------------------------
if (dest) {
  dest.addEventListener("change", async () => {
    try {
      await api(`/api/libraries/${libraryId}/dest`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: dest.value }),
      });
      window.j2jDest = dest.value;
      document.getElementById("dest-card").classList.remove("dest-missing");
      document.dispatchEvent(new CustomEvent("j2j:dest", { detail: dest.value }));
      toast(`Downloads from this library go to ${dest.value}`);
    } catch (e) {
      toast(e.message, "error");
    }
  });
}

// ---- item list ---------------------------------------------------------------
const ICONS = { Series: "bi-tv", Movie: "bi-film", MusicAlbum: "bi-disc" };

function card(item) {
  const el = document.createElement("article");
  el.className = "item";
  el.dataset.id = item.id;
  el.dataset.type = item.type;

  const poster = document.createElement("div");
  poster.className = "poster";
  // Swaps only the image for an icon, so the badge (added below) survives a
  // poster that fails to load.
  const fallback = () => {
    const icon = document.createElement("i");
    icon.className = `bi ${ICONS[item.type] || "bi-collection-play"}`;
    poster.querySelector("img")?.remove();
    poster.prepend(icon);
  };
  if (item.has_image) {
    const img = document.createElement("img");
    img.loading = "lazy";
    img.alt = "";
    img.src = `/api/items/${item.id}/poster`;
    img.onerror = fallback;
    poster.appendChild(img);
  } else {
    fallback();
  }
  if (item.on_server) {
    const badge = document.createElement("span");
    badge.className = "on-server";
    badge.innerHTML = '<i class="bi bi-check-lg"></i> On my server';
    poster.appendChild(badge);
    el.classList.add("have-it");
  }

  const name = document.createElement("h3");
  name.textContent = item.name;
  const meta = document.createElement("p");
  meta.className = "muted";
  const bits = [];
  if (item.year) bits.push(item.year);
  if (item.type === "Series" && item.child_count) bits.push(`${item.child_count} season${item.child_count === 1 ? "" : "s"}`);
  if (item.type === "MusicAlbum" && item.child_count) bits.push(`${item.child_count} tracks`);
  meta.textContent = bits.join(" · ");

  const actions = document.createElement("div");
  actions.className = "item-actions";
  if (item.type === "Series") {
    actions.append(
      queueButton("Queue all", "Every season", () => enqueue(item, { which: "all" })),
      queueButton("Queue S1", "Season 1 only", () => enqueue(item, { which: "first" })),
      queueButton("Manual…", "Pick seasons", () => manual(item)),
    );
  } else if (item.type === "Movie") {
    actions.append(queueButton("Queue", "", () => enqueue(item, {})));
  } else if (item.type === "MusicAlbum") {
    actions.append(queueButton("Queue album", "", () => enqueue(item, {})));
  }

  el.append(poster, name, meta, actions);
  return el;
}

// ---- queueing ----------------------------------------------------------------

function queueButton(label, title, onClick) {
  const b = document.createElement("button");
  b.className = "btn btn-small queue-btn";
  b.textContent = label;
  if (title) b.title = title;
  // A choice in the picker comes first: nothing is queued to a folder the
  // user didn't pick on this visit's page.
  b.disabled = !window.j2jDest;
  b.addEventListener("click", async () => {
    b.disabled = true;
    try { await onClick(); } finally { b.disabled = !window.j2jDest; }
  });
  return b;
}

document.addEventListener("j2j:dest", () => {
  document.querySelectorAll(".queue-btn").forEach((b) => { b.disabled = !window.j2jDest; });
});

function size(n) {
  if (!n) return "";
  const gb = n / 1e9;
  return gb >= 1 ? `${gb.toFixed(1)} GB` : `${(n / 1e6).toFixed(0)} MB`;
}

const NOUN = { episode: ["episode", "episodes"], movie: ["movie", "movies"], track: ["track", "tracks"] };

async function enqueue(item, options) {
  try {
    const r = await api("/api/queue", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ library_id: libraryId, item_id: item.id, type: item.type,
                             dest: window.j2jDest, ...options }),
    });
    const [one, many] = NOUN[r.kind];
    let text = r.added
      ? `Queued ${r.added} ${r.added === 1 ? one : many}${r.bytes ? ` (${size(r.bytes)})` : ""} → ${r.dest}`
      : `Nothing new to queue for ${item.name}`;
    const notes = [];
    if (r.subtitles) notes.push(`+${r.subtitles} subtitle file${r.subtitles === 1 ? "" : "s"}`);
    if (r.already_queued) notes.push(`${r.already_queued} already queued`);
    if (r.unavailable) notes.push(`${r.unavailable} have no file on the source`);
    if (r.unsafe) notes.push(`${r.unsafe} skipped: unsafe name`);
    if (notes.length) text += ` (${notes.join(", ")})`;
    toast(text, r.added ? "ok" : "error");
    refreshQueueBadge();
  } catch (e) {
    toast(e.message, "error");
  }
}

const modal = document.getElementById("seasons-modal");
const seasonList = document.getElementById("seasons-list");
const seasonStatus = document.getElementById("seasons-status");
const selectAll = document.getElementById("seasons-all");
const okButton = document.getElementById("seasons-ok");

function ticked() {
  return [...seasonList.querySelectorAll("input:checked")].map((c) => c.value);
}

function syncModal() {
  const boxes = seasonList.querySelectorAll("input");
  const n = ticked().length;
  okButton.disabled = n === 0;
  selectAll.checked = boxes.length > 0 && n === boxes.length;
  selectAll.indeterminate = n > 0 && n < boxes.length;
}

seasonList.addEventListener("change", syncModal);
selectAll.addEventListener("change", () => {
  seasonList.querySelectorAll("input").forEach((c) => { c.checked = selectAll.checked; });
  syncModal();
});

async function manual(item) {
  document.getElementById("seasons-title").textContent = item.name;
  seasonList.replaceChildren();
  seasonStatus.textContent = "Loading seasons…";
  seasonStatus.hidden = false;
  selectAll.parentElement.hidden = true;
  okButton.disabled = true;
  modal.returnValue = "";
  // Listen before anything is awaited: Cancel or Esc may come while the
  // seasons are still loading.
  const closed = new Promise((resolve) => modal.addEventListener("close", () => resolve(modal.returnValue), { once: true }));
  modal.showModal();
  try {
    const { seasons } = await api(`/api/series/${item.id}/seasons`);
    seasonStatus.hidden = seasons.length > 0;
    seasonStatus.textContent = "This series has no seasons on the source server.";
    selectAll.parentElement.hidden = seasons.length === 0;
    seasonList.replaceChildren(...seasons.map((s) => {
      const li = document.createElement("li");
      const label = document.createElement("label");
      label.className = "check";
      const box = document.createElement("input");
      box.type = "checkbox";
      box.value = s.id;
      box.disabled = s.episodes === 0;
      const text = document.createElement("span");
      text.textContent = s.name;
      const meta = document.createElement("span");
      meta.className = "muted";
      meta.textContent = `${s.episodes} episode${s.episodes === 1 ? "" : "s"}${s.bytes ? " · " + size(s.bytes) : ""}`;
      label.append(box, text, meta);
      li.append(label);
      return li;
    }));
    // Disabled (empty) seasons can't be ticked, so leave them out of "all".
    seasonList.querySelectorAll("input:disabled").forEach((c) => c.closest("li").classList.add("empty-season"));
    syncModal();
  } catch (e) {
    seasonStatus.textContent = e.message;
  }

  const choice = await closed;
  const ids = ticked();
  if (choice === "ok" && ids.length) await enqueue(item, { which: "seasons", season_ids: ids });
}

async function loadPage() {
  if (state.loading || (state.total !== null && state.start >= state.total)) return;
  state.loading = true;
  more.hidden = false;
  const generation = state.generation;
  const params = new URLSearchParams({ start: state.start, search: state.search, letter: state.letter });
  try {
    const page = await api(`/api/libraries/${libraryId}/items?${params}`);
    // A newer search started while this page was in flight: drop it.
    if (generation !== state.generation) return;
    state.total = page.total;
    state.start += page.items.length;
    page.items.forEach((item) => grid.appendChild(card(item)));
    count.textContent = page.total === 0 ? "Nothing matches." : `${page.total} item${page.total === 1 ? "" : "s"}`;
    if (page.items.length === 0) state.total = state.start;  // guard against a short last page
  } catch (e) {
    if (generation === state.generation) count.textContent = `Couldn't load items: ${e.message}`;
  } finally {
    if (generation === state.generation) {
      state.loading = false;
      more.hidden = true;
    }
  }
  // The first page may not fill the screen; keep going until it does.
  if (generation === state.generation && sentinel.getBoundingClientRect().top < window.innerHeight + 400) loadPage();
}

function reset() {
  state.generation += 1;
  state.start = 0;
  state.total = null;
  state.loading = false;
  grid.replaceChildren();
  count.textContent = "";
  loadPage();
}

let searchTimer;
search.addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    state.search = search.value.trim();
    reset();
  }, 300);
});

document.getElementById("az").addEventListener("click", (event) => {
  const button = event.target.closest("button");
  if (!button) return;
  document.querySelectorAll("#az button").forEach((b) => b.classList.toggle("active", b === button));
  state.letter = button.dataset.letter;
  reset();
});

new IntersectionObserver((entries) => {
  if (entries.some((e) => e.isIntersecting)) loadPage();
}, { rootMargin: "400px" }).observe(sentinel);

loadPage();
