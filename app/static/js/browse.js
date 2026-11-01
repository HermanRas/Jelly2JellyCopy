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
  const fallback = () => { poster.innerHTML = `<i class="bi ${ICONS[item.type] || "bi-collection-play"}"></i>`; };
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

  el.append(poster, name, meta, actions);
  return el;
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
