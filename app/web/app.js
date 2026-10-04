"use strict";

const $ = (id) => document.getElementById(id);
const api = () => window.pywebview.api;

let state = {
  entries: [],
  logs: [],
  changes: [],
  current_avatar_id: null,
  status: "",
  logged_in: false,
  session_expired: false,
  username: "",
  pending_2fa: false,
  version: "",
  discovery: { sources: {}, backlog: 0, db_path: "" },
  osc: { listening: false, error: null, seen_traffic: false },
};

let currentView = "home";     // home | logs
let currentFilter = "all";    // all | favorites | public | quest | pc
let logTab = "avatars";       // avatars | players
let selectedId = null;
let lastStatus = "";
let lastRevs = null;
let lastGridSig = null;
let lastLogsSig = null;
let lastChangesSig = null;
let suppressSave = false;
let saveTimer = null;

const thumbCache = {};    // id -> data URI
const thumbKey = {};      // id -> thumb filename currently cached
const thumbPending = {};  // id -> true
// Base64 thumbnails are held in the webview for the session. With hundreds of
// avatars that is tens of megabytes, so the cache is capped and the oldest
// entries evicted once it is full.
const THUMB_CACHE_MAX = 200;
const thumbOrder = [];

const VIEW_TITLES = {
  home: "Avatars",
  logs: "Avatar Logs",
};

/* ------------------------------------------------------------------ helpers */
function escapeHtml(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function formatDate(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  return d.toLocaleString();
}

function relTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d)) return "";
  const s = Math.max(0, (Date.now() - d.getTime()) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return Math.floor(s / 60) + "m ago";
  if (s < 86400) return Math.floor(s / 3600) + "h ago";
  return Math.floor(s / 86400) + "d ago";
}

function entryById(id) {
  return state.entries.find((e) => e.id === id) || null;
}

function matchesFilter(entry) {
  switch (currentFilter) {
    case "favorites": return !!entry.favorite;
    case "public": return (entry.release_status || "") === "public";
    case "quest": return (entry.platforms || []).includes("Quest");
    case "pc": return (entry.platforms || []).includes("PC");
    default: return true;
  }
}

function visibleEntries() {
  let list = state.entries.slice();
  list = list.filter(matchesFilter);

  const q = $("search").value.trim().toLowerCase();
  if (q) {
    list = list.filter((e) => {
      const hay = [e.name, e.id, e.notes, e.author, (e.tags || []).join(" "),
        (e.platforms || []).join(" ")].join(" ").toLowerCase();
      return hay.includes(q);
    });
  }

  const sort = $("sort").value;
  list.sort((a, b) => {
    if (sort === "name") return (a.name || "").toLowerCase().localeCompare((b.name || "").toLowerCase());
    return String(b.added || "").localeCompare(String(a.added || ""));
  });
  return list;
}

function cardSub(entry) {
  if (entry.author) return entry.author;
  if (entry.platforms && entry.platforms.length) return entry.platforms.join(" · ");
  if (entry.tags && entry.tags.length) return entry.tags.map((t) => "#" + t).join("  ");
  return entry.id;
}

/* ------------------------------------------------------------------ toast */
function toast(msg) {
  const el = $("toast");
  el.textContent = msg;
  el.classList.add("show");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.remove("show"), 3200);
}

/* ------------------------------------------------------------------ modals */
function openModal(id) { $(id).classList.remove("hidden"); }
function closeModal(id) { $(id).classList.add("hidden"); }

function showAlert(title, message) {
  $("alert-title").textContent = title || "Notice";
  $("alert-message").textContent = message || "";
  openModal("modal-alert");
}

// Only one confirm may be pending; opening a second settles the first so its
// awaiting caller can never be left hanging forever.
let pendingConfirm = null;

function showConfirm(title, message) {
  if (pendingConfirm) pendingConfirm(false);
  return new Promise((resolve) => {
    $("confirm-title").textContent = title || "Confirm";
    $("confirm-message").textContent = message || "";
    openModal("modal-confirm");

    const modal = $("modal-confirm");
    const ok = $("confirm-ok");
    const cancel = $("confirm-cancel");

    // Every exit path must settle the promise: OK, Cancel, backdrop and Escape.
    // Previously only OK and the backdrop resolved, so clicking Cancel left the
    // awaiting code running forever.
    const finish = (value) => {
      ok.removeEventListener("click", onOk);
      cancel.removeEventListener("click", onCancel);
      modal.removeEventListener("click", onBackdrop);
      document.removeEventListener("keydown", onKeydown, true);
      pendingConfirm = null;
      closeModal("modal-confirm");
      resolve(value);
    };
    const onOk = () => finish(true);
    const onCancel = () => finish(false);
    const onBackdrop = (e) => { if (e.target === modal) finish(false); };
    const onKeydown = (e) => {
      if (e.key !== "Escape") return;
      e.stopPropagation();
      e.preventDefault();
      finish(false);
    };

    ok.addEventListener("click", onOk);
    cancel.addEventListener("click", onCancel);
    modal.addEventListener("click", onBackdrop);
    document.addEventListener("keydown", onKeydown, true);

    pendingConfirm = finish;
  });
}

async function call(method, ...args) {
  try {
    return await api()[method](...args);
  } catch (err) {
    showAlert("Something went wrong", String(err));
    return { ok: false };
  }
}

/* ------------------------------------------------------------------ context menu */
function hideContextMenu() {
  $("context-menu").classList.add("hidden");
}

function showContextMenu(x, y, items) {
  const menu = $("context-menu");
  menu.innerHTML = "";
  for (const item of items) {
    if (item.sep) {
      const sep = document.createElement("div");
      sep.className = "context-sep";
      menu.appendChild(sep);
      continue;
    }
    const btn = document.createElement("button");
    btn.className = "context-item" + (item.danger ? " danger" : "");
    btn.innerHTML = `<span class="ico">${item.icon || ""}</span>${escapeHtml(item.label)}`;
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      hideContextMenu();
      item.action();
    });
    menu.appendChild(btn);
  }
  menu.classList.remove("hidden");
  const rect = menu.getBoundingClientRect();
  const left = Math.min(x, window.innerWidth - rect.width - 8);
  const top = Math.min(y, window.innerHeight - rect.height - 8);
  menu.style.left = Math.max(8, left) + "px";
  menu.style.top = Math.max(8, top) + "px";
}

/* ------------------------------------------------------------------ thumbnails */
function placeholderDataUri(name) {
  const letter = (String(name || "?").trim()[0] || "?").toUpperCase();
  const svg = `<svg xmlns='http://www.w3.org/2000/svg' width='200' height='300'>
    <rect width='200' height='300' fill='#141414'/>
    <text x='100' y='168' font-size='84' font-family='Segoe UI, sans-serif'
      fill='rgba(255,255,255,0.2)' text-anchor='middle'>${escapeHtml(letter)}</text></svg>`;
  return "data:image/svg+xml;utf8," + encodeURIComponent(svg);
}

function cacheThumb(id, key, src) {
  if (!(id in thumbCache)) thumbOrder.push(id);
  thumbCache[id] = src;
  thumbKey[id] = key;
  while (thumbOrder.length > THUMB_CACHE_MAX) {
    const evicted = thumbOrder.shift();
    delete thumbCache[evicted];
    delete thumbKey[evicted];
  }
}

function ensureThumb(entry) {
  const id = entry.id;
  if (entry.thumb && thumbKey[id] === entry.thumb && thumbCache[id]) {
    // Refresh recency so the cards you are looking at are not the ones evicted.
    const at = thumbOrder.indexOf(id);
    if (at > 0) {
      thumbOrder.splice(at, 1);
      thumbOrder.push(id);
    }
    return thumbCache[id];
  }
  if (entry.thumb && !thumbPending[id]) {
    thumbPending[id] = true;
    call("get_thumbnail", id).then((data) => {
      thumbPending[id] = false;
      // call() resolves to {ok:false} when the bridge throws, and that object is
      // truthy. Storing it produced src="[object Object]" and, because it was
      // cached against the thumb name, the broken image never recovered.
      if (typeof data !== "string" || !data) return;
      if (data === thumbCache[id]) return;
      cacheThumb(id, entry.thumb, data);
      applyThumb(id);
    });
  }
  return thumbCache[id] || "";
}

function applyThumb(id) {
  const src = thumbCache[id];
  if (!src) return;
  document.querySelectorAll(`[data-thumb-id="${CSS.escape(id)}"]`).forEach((img) => {
    img.src = src;
  });
}

/* ------------------------------------------------------------------ render */
function renderHeader() {
  $("page-title").textContent = VIEW_TITLES[currentView] || "Avatars";
  document.querySelectorAll(".rail-btn[data-view]").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.view === currentView);
  });
  document.querySelectorAll(".chip").forEach((chip) => {
    chip.classList.toggle("active", chip.dataset.filter === currentFilter);
  });
  const isLogs = currentView === "logs";
  $("chips").classList.toggle("hidden", isLogs);
  $("grid-wrap").classList.toggle("hidden", isLogs);
  $("logs-wrap").classList.toggle("hidden", !isLogs);
  // Sort only affects the avatar grid, so hiding it here avoids a control that
  // visibly does nothing. Search applies to both views and stays visible.
  $("sort").classList.toggle("hidden", isLogs);
}

function renderGrid(force) {
  if (currentView === "logs") return;

  const list = visibleEntries();
  const sig = JSON.stringify({
    v: currentView,
    f: currentFilter,
    q: $("search").value,
    s: $("sort").value,
    c: state.current_avatar_id,
    items: list.map((e) => [e.id, e.name, e.thumb, e.author, e.favorite,
      e.release_status, (e.platforms || []).join(","), (e.tags || []).join(",")]),
  });
  if (!force && sig === lastGridSig) return;
  lastGridSig = sig;

  const grid = $("grid");
  grid.innerHTML = "";

  if (!list.length) {
    $("grid-empty").classList.remove("hidden");
    $("count").textContent = "0 avatars";
    return;
  }
  $("grid-empty").classList.add("hidden");
  $("count").textContent = `${list.length} avatar${list.length === 1 ? "" : "s"}`;

  const frag = document.createDocumentFragment();
  for (const entry of list) {
    const card = document.createElement("article");
    card.className = "card"
      + (entry.id === state.current_avatar_id ? " wearing" : "")
      + (entry.id === selectedId ? " selected" : "");
    card.dataset.id = entry.id;
    // The grid was previously mouse-only: cards were not focusable and the
    // hover-revealed controls could not be reached by keyboard.
    card.tabIndex = 0;
    card.setAttribute("role", "button");
    card.setAttribute("aria-label", entry.name || "Avatar");

    const src = ensureThumb(entry) || placeholderDataUri(entry.name);
    const favOn = entry.favorite ? "on" : "";
    const badges = [];
    if (entry.release_status) {
      badges.push(`<span class="platform-badge ${entry.release_status === "public" ? "public" : ""}">${escapeHtml(entry.release_status)}</span>`);
    }
    (entry.platforms || []).forEach((p) =>
      badges.push(`<span class="platform-badge">${escapeHtml(p)}</span>`));
    const badgesHtml = badges.length ? `<div class="card-badges">${badges.join("")}</div>` : "";

    card.innerHTML = `
      <div class="poster">
        <img data-thumb-id="${escapeHtml(entry.id)}" src="${src}" alt="" />
        <button class="fav-btn ${favOn}" title="Favorite">♥</button>
        ${entry.id === state.current_avatar_id ? '<span class="badge wearing-badge">● Wearing</span>' : ""}
        <div class="poster-actions">
          <button class="btn primary wear-quick">Wear</button>
        </div>
      </div>
      <div class="card-body">
        <div class="card-title">${escapeHtml(entry.name || "Unnamed avatar")}</div>
        <div class="card-sub">${escapeHtml(cardSub(entry))}</div>
        ${badgesHtml}
      </div>`;

    card.addEventListener("click", () => openDrawer(entry.id));
    card.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        openDrawer(entry.id);
      }
    });
    card.addEventListener("contextmenu", (e) => {
      e.preventDefault();
      showContextMenu(e.clientX, e.clientY, cardMenuItems(entry));
    });
    card.querySelector(".fav-btn").addEventListener("click", (e) => {
      e.stopPropagation();
      toggleFavorite(entry.id);
    });
    card.querySelector(".wear-quick").addEventListener("click", (e) => {
      e.stopPropagation();
      wear(entry.id);
    });
    frag.appendChild(card);
  }
  grid.appendChild(frag);
}

function matchesLogFilter(entry) {
  const q = $("search").value.trim().toLowerCase();
  if (!q) return true;
  const fav = entryById(entry.id);
  const hay = [entry.name, entry.id, fav ? fav.name : "", fav ? fav.author : ""]
    .join(" ").toLowerCase();
  return hay.includes(q);
}

function renderLogs(force) {
  if (currentView !== "logs") return;

  const showAvatars = logTab === "avatars";
  const allLogs = state.logs || [];
  const allChanges = state.changes || [];
  // The search box is shared with the grid, so honour it here too rather than
  // letting it sit there doing nothing on this tab.
  const logs = allLogs.filter(matchesLogFilter);
  const changes = allChanges.filter((c) =>
    (c.player + " " + c.avatar).toLowerCase().includes($("search").value.trim().toLowerCase()));

  document.querySelectorAll(".log-tab").forEach((tab) => {
    tab.classList.toggle("active", tab.dataset.logtab === logTab);
  });
  $("logs").classList.toggle("hidden", !showAvatars);
  $("changes").classList.toggle("hidden", showAvatars);
  $("logs-empty").classList.toggle("hidden", !showAvatars || logs.length > 0);
  $("changes-empty").classList.toggle("hidden", showAvatars || changes.length > 0);

  const saveable = logs.filter((l) => !l.private && !entryById(l.id)).length;
  const saveAll = $("logs-save-all");
  saveAll.classList.toggle("hidden", !showAvatars || saveable === 0);
  saveAll.textContent = saveable ? `Save all (${saveable})` : "Save all";

  if (showAvatars) {
    renderAvatarLogs(logs, force);
  } else {
    renderPlayerChanges(changes, force);
  }
}

function renderAvatarLogs(logs, force) {
  const sig = JSON.stringify(logs.map((e) =>
    [e.id, e.name, e.count, e.last_seen, e.private, e.source, !!entryById(e.id)]));
  if (!force && sig === lastLogsSig) return;
  lastLogsSig = sig;

  const wrap = $("logs");
  wrap.innerHTML = "";
  $("logs-count").textContent = logs.length
    ? `${logs.length} avatar${logs.length === 1 ? "" : "s"} logged` : "";
  if (!logs.length) return;

  const frag = document.createDocumentFragment();
  for (const log of logs) {
    const fav = entryById(log.id);
    const name = log.name || (fav ? fav.name : "");
    const src = fav ? (ensureThumb(fav) || placeholderDataUri(name || log.id))
                    : placeholderDataUri(name || log.id);
    const countBadge = log.count > 1 ? `<span class="platform-badge">×${log.count}</span>` : "";
    const privateBadge = log.private ? `<span class="platform-badge">private</span>` : "";
    const sourceBadge = log.source
      ? `<span class="platform-badge" title="Discovered from ${escapeHtml(SOURCE_LABELS[log.source] || log.source)}">${escapeHtml(SOURCE_LABELS[log.source] || log.source)}</span>`
      : "";
    const saved = !!fav;
    const disabled = saved || log.private ? " disabled" : "";

    const row = document.createElement("div");
    row.className = "log-row" + (log.private ? " private" : "");
    row.innerHTML = `
      <img class="log-thumb" data-thumb-id="${saved ? escapeHtml(log.id) : ""}" src="${src}" alt="" />
      <div class="log-main">
        <div class="log-name">${escapeHtml(name || "Unknown avatar")}</div>
        <div class="log-sub">${escapeHtml(log.id)}</div>
      </div>
      <div class="log-meta">
        ${sourceBadge}${countBadge}${privateBadge}
        <span class="muted small">${escapeHtml(relTime(log.last_seen))}</span>
      </div>
      <div class="log-actions">
        <button class="btn small save-log"${disabled}>${saved ? "Saved" : "Save"}</button>
        <button class="btn small ghost forget-log" title="Remove from log">✕</button>
      </div>`;
    row.querySelector(".save-log").addEventListener("click", () => saveFromLog(log.id));
    row.querySelector(".forget-log").addEventListener("click", () => forgetLog(log.id));
    row.addEventListener("contextmenu", (e) => {
      e.preventDefault();
      const items = [];
      if (!saved && !log.private) {
        items.push({ icon: "＋", label: "Save to Favourites", action: () => saveFromLog(log.id) });
      }
      items.push({
        icon: "⧉", label: "Copy Avatar ID",
        action: async () => {
          const res = await call("copy_id", log.id);
          toast(res.ok ? "Avatar ID copied." : "Could not copy.");
        },
      });
      items.push({ sep: true });
      items.push({ icon: "✕", label: "Remove from Log", danger: true, action: () => forgetLog(log.id) });
      showContextMenu(e.clientX, e.clientY, items);
    });
    frag.appendChild(row);
  }
  wrap.appendChild(frag);
}

function renderPlayerChanges(changes, force) {
  const sig = JSON.stringify(changes.map((e) =>
    [e.player, e.avatar, e.count, e.last_seen]));
  if (!force && sig === lastChangesSig) return;
  lastChangesSig = sig;

  const wrap = $("changes");
  wrap.innerHTML = "";
  $("logs-count").textContent = changes.length
    ? `${changes.length} change${changes.length === 1 ? "" : "s"} logged` : "";
  if (!changes.length) return;

  const frag = document.createDocumentFragment();
  for (const change of changes) {
    const initial = (change.player || "?").trim()[0] || "?";
    const countBadge = change.count > 1 ? `<span class="log-badge">×${change.count}</span>` : "";
    const row = document.createElement("div");
    row.className = "log-row";
    row.innerHTML = `
      <div class="log-avatar">${escapeHtml(initial.toUpperCase())}</div>
      <div class="log-main">
        <div class="log-name">${escapeHtml(change.player)}</div>
        <div class="log-sub">wearing “${escapeHtml(change.avatar)}”</div>
      </div>
      <div class="log-meta">
        ${countBadge}
        <span class="muted small">${escapeHtml(relTime(change.last_seen))}</span>
      </div>
      <div class="log-actions">
        <button class="btn small copy-change" title="Copy avatar name">Copy name</button>
      </div>`;
    row.querySelector(".copy-change").addEventListener("click", async () => {
      const res = await call("copy_text", change.avatar);
      toast(res.ok ? "Avatar name copied." : "Could not copy.");
    });
    row.addEventListener("contextmenu", (e) => {
      e.preventDefault();
      showContextMenu(e.clientX, e.clientY, [
        {
          icon: "⧉", label: "Copy Avatar Name",
          action: async () => {
            const res = await call("copy_text", change.avatar);
            toast(res.ok ? "Avatar name copied." : "Could not copy.");
          },
        },
        {
          icon: "⧉", label: "Copy Player Name",
          action: async () => {
            const res = await call("copy_text", change.player);
            toast(res.ok ? "Player name copied." : "Could not copy.");
          },
        },
      ]);
    });
    frag.appendChild(row);
  }
  wrap.appendChild(frag);
}

function renderDrawer() {
  if (!selectedId) { closeDrawer(); return; }
  const entry = entryById(selectedId);
  if (!entry) { closeDrawer(); return; }

  const active = document.activeElement;
  suppressSave = true;
  // While a draft is pending these fields are the user's unsaved text, not stale
  // model values: render() runs on every poll, and overwriting them would make
  // the edit appear to vanish whenever the field lost focus.
  const editing = !!draft && draft.id === entry.id;
  if (!editing && active !== $("d-name")) $("d-name").value = entry.name || "";
  if (!editing && active !== $("d-notes")) $("d-notes").value = entry.notes || "";
  if (!editing && active !== $("d-tags")) $("d-tags").value = (entry.tags || []).join(", ");
  suppressSave = false;

  const src = ensureThumb(entry) || placeholderDataUri(entry.name);
  const preview = $("preview");
  if (preview.getAttribute("src") !== src) preview.src = src;
  preview.dataset.thumbId = entry.id;

  $("d-author").textContent = entry.author ? "by " + entry.author : "Author unknown";
  const badges = [];
  if (entry.release_status) {
    badges.push(`<span class="platform-badge ${entry.release_status === "public" ? "public" : ""}">${escapeHtml(entry.release_status)}</span>`);
  }
  (entry.platforms || []).forEach((p) => badges.push(`<span class="platform-badge">${escapeHtml(p)}</span>`));
  (entry.tags || []).forEach((t) => badges.push(`<span class="platform-badge">#${escapeHtml(t)}</span>`));
  $("d-platforms").innerHTML = badges.join("");

  $("d-id").textContent = entry.id;
  $("d-added").textContent = entry.added ? "Added " + formatDate(entry.added) : "";
  $("wearing-badge").classList.toggle("hidden", entry.id !== state.current_avatar_id);

  $("btn-fav").textContent = entry.favorite ? "♥ Favorited" : "♥ Favorite";
  $("btn-fav").classList.toggle("primary", !!entry.favorite);
}

const SOURCE_LABELS = {
  "cache-db": "local cache",
  amplitude: "live feed",
  log: "VRChat log",
  osc: "OSC",
};

const SOURCE_ORDER = ["cache-db", "amplitude", "log"];

// One line saying which local source is actually producing ids, so a degraded
// source is visible rather than looking like "no new avatars".
function renderDiscovery() {
  const el = $("discovery-state");
  if (!el) return;
  const sources = (state.discovery && state.discovery.sources) || {};
  const backlog = (state.discovery && state.discovery.backlog) || 0;
  const parts = [];
  for (const key of SOURCE_ORDER) {
    const status = sources[key];
    if (!status) continue;
    if (status === "ok" || status === "empty") {
      parts.push(SOURCE_LABELS[key] + (status === "empty" ? " (idle)" : ""));
    } else {
      parts.push(SOURCE_LABELS[key] + " unavailable");
    }
  }
  let text = parts.join(" · ");
  if (backlog) text += "  ·  " + backlog.toLocaleString() + " ids seen all-time";
  el.textContent = text;
  const down = SOURCE_ORDER.some((k) => ["missing", "unsupported", "unreadable"].includes(sources[k]));
  el.classList.toggle("warn-text", down);
  el.title = (state.discovery && state.discovery.db_path) || "";
}

function renderStatus() {
  const osc = state.osc;
  const dot = $("osc-dot");
  dot.className = "dot";
  if (osc.error) {
    dot.classList.add("bad");
    $("osc-text").textContent = "OSC: " + osc.error;
  } else if (!osc.listening) {
    dot.classList.add("bad");
    $("osc-text").textContent = "OSC: not listening";
  } else if (osc.seen_traffic) {
    dot.classList.add("ok");
    $("osc-text").textContent = "OSC: connected";
  } else {
    dot.classList.add("wait");
    $("osc-text").textContent = "OSC: waiting for VRChat...";
  }

  // A full WinError string blows out the footer height, so keep the pill short
  // and leave the detail in the tooltip.
  $("osc-pill").title = osc.error || "";

  $("session-expired").classList.toggle("hidden", !state.session_expired);

  const current = entryById(state.current_avatar_id);
  $("current").textContent = state.current_avatar_id
    ? "Current: " + (current ? current.name : state.current_avatar_id)
    : "";

  $("login-state").textContent = state.logged_in
    ? "Logged in" + (state.username ? " as " + state.username : "")
    : "Not logged in";

  renderDiscovery();

  if (state.status && state.status !== lastStatus) {
    lastStatus = state.status;
    toast(state.status);
  }
}

function render() {
  renderHeader();
  renderGrid();
  renderLogs();
  renderDrawer();
  renderStatus();
}

/* ------------------------------------------------------------------ drawer */
async function openDrawer(id) {
  // Persist any edit to the previously open avatar before switching away.
  if (draft && draft.id !== id) await flushDraft(false);
  selectedId = id;
  draft = null;
  document.querySelectorAll(".card").forEach((c) =>
    c.classList.toggle("selected", c.dataset.id === id));
  renderDrawer();
  $("drawer").classList.remove("hidden");
  $("drawer-backdrop").classList.remove("hidden");
}

async function closeDrawer() {
  // Flush first: closing used to drop a pending debounced edit entirely.
  if (draft) await flushDraft(false);
  $("drawer").classList.add("hidden");
  $("drawer-backdrop").classList.add("hidden");
  selectedId = null;
  draft = null;
  document.querySelectorAll(".card").forEach((c) => c.classList.remove("selected"));
}

/* ------------------------------------------------------------------ actions */
async function addCurrent() {
  const res = await call("add_current");
  if (!res.ok) { if (res.title) showAlert(res.title, res.message); return; }
  await refreshState();
  if (res.id) openDrawer(res.id);
}

function addByIdPrompt() {
  $("id-input").value = "";
  openModal("modal-id");
  setTimeout(() => $("id-input").focus(), 50);
}

async function addByIdSubmit() {
  const value = $("id-input").value.trim();
  let res = await call("add_by_id", value);
  if (!res.ok && res.confirm) {
    const yes = await showConfirm(res.title, res.message);
    if (!yes) return;
    res = await call("add_by_id", value, true);
  }
  if (!res.ok) { if (res.title) showAlert(res.title, res.message); return; }
  closeModal("modal-id");
  await refreshState();
  if (res.id) openDrawer(res.id);
}

async function wear(id) {
  if (!id) return;
  const res = await call("wear", id);
  if (!res.ok && res.title) showAlert(res.title, res.message);
}

async function toggleFavorite(id) {
  await call("toggle_favorite", id);
  await refreshState();
}

function cardMenuItems(entry) {
  return [
    { icon: "▶", label: "Wear Avatar", action: () => wear(entry.id) },
    {
      icon: "♥",
      label: entry.favorite ? "Remove from Favorites" : "Add to Favorites",
      action: () => toggleFavorite(entry.id),
    },
    { icon: "⧉", label: "Copy Avatar ID", action: () => copyEntryId(entry.id) },
    { icon: "⟳", label: "Refresh Metadata", action: () => refreshMetaFor(entry.id) },
    { sep: true },
    {
      icon: "✕",
      label: "Delete",
      danger: true,
      action: () => deleteEntry(entry.id, entry.name),
    },
  ];
}

async function copyEntryId(id) {
  const res = await call("copy_id", id);
  toast(res.ok ? "Avatar ID copied to clipboard." : "Could not copy.");
}

async function refreshMetaFor(id) {
  const res = await call("refresh_metadata", id);
  if (!res.ok && res.title) showAlert(res.title, res.message);
}

async function deleteEntry(id, name) {
  const yes = await showConfirm("Delete", `Remove "${name}" from your favourites?`);
  if (!yes) return;
  await call("delete", id);
  if (selectedId === id) closeDrawer();
  await refreshState();
}

async function saveFromLog(id) {
  const res = await call("save_from_log", id);
  if (!res.ok) { if (res.title) showAlert(res.title, res.message); return; }
  toast(res.already ? "Already in favourites." : "Saved. Fetching metadata...");
  await refreshState();
}

async function forgetLog(id) {
  await call("delete_log", id);
  await refreshState();
}

async function del() {
  const entry = entryById(selectedId);
  if (!entry) return;
  const yes = await showConfirm("Delete", `Remove "${entry.name}" from your favourites?`);
  if (!yes) return;
  await call("delete", selectedId);
  closeDrawer();
  await refreshState();
}

async function copyId() {
  const entry = entryById(selectedId);
  if (!entry) return;
  const res = await call("copy_id", entry.id);
  toast(res.ok ? "Avatar ID copied to clipboard." : "Could not copy to clipboard.");
}

async function refreshMeta() {
  if (!selectedId) return;
  const res = await call("refresh_metadata", selectedId);
  if (!res.ok && res.title) showAlert(res.title, res.message);
}

/* The in-progress edit for the currently open drawer.
   scheduleSave() used to read selectedId and the field values when the
   debounce timer fired, so closing the drawer or clicking another card within
   600ms of typing either discarded the edit or wrote it to the wrong avatar.
   The snapshot is taken on every keystroke instead, and flushed explicitly
   whenever the selection changes or the drawer closes. */
let draft = null;

function captureDraft() {
  if (suppressSave || !selectedId) return;
  draft = {
    id: selectedId,
    name: $("d-name").value,
    notes: $("d-notes").value,
    tags: $("d-tags").value,
  };
}

async function flushDraft(announce) {
  clearTimeout(saveTimer);
  saveTimer = null;
  const pending = draft;
  draft = null;
  if (!pending) return false;
  const res = await call("save_details", pending.id, pending.name, pending.notes, pending.tags);
  if (announce && res && res.ok) toast("Saved.");
  return !!(res && res.ok);
}

function scheduleSave() {
  if (suppressSave || !selectedId) return;
  captureDraft();
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => { flushDraft(false); }, 600);
}

/* ------------------------------------------------------------------ settings */
async function openSettings() {
  const s = await call("get_settings");
  $("set-send").value = s.osc_send_port;
  $("set-recv").value = s.osc_receive_port;
  $("set-user").value = s.username || "";
  $("set-pass").value = "";
  $("set-totp").value = "";
  $("set-totp").disabled = !s.pending_2fa;
  setTwoFactorUI(s.pending_2fa ? (s.twofa_methods || ["totp"]) : [], s.twofa_method);
  $("set-login-status").textContent = s.logged_in
    ? "Logged in" + (s.username ? " as " + s.username : "") + "."
    : (s.pending_2fa ? "2FA required - enter your code and click Log in." : "");
  $("set-login").disabled = !!s.logged_in;
  $("set-logout").disabled = !s.logged_in;
  $("set-version").textContent = s.version ? "Local Avatar Favourites v" + s.version : "";
  openModal("modal-settings");
}

const TWOFA_OPTION_LABELS = { totp: "Authenticator app", emailotp: "Email code", otp: "Recovery code" };
const TWOFA_FIELD_LABELS = { totp: "Authenticator code", emailotp: "Email code", otp: "Recovery code" };
const TWOFA_PLACEHOLDERS = { totp: "6-digit code", emailotp: "code from your email", otp: "recovery code" };

function setTwoFactorUI(methods, selected) {
  const sel = $("set-method");
  methods = methods || [];
  sel.innerHTML = "";
  methods.forEach((m) => {
    const opt = document.createElement("option");
    opt.value = m;
    opt.textContent = TWOFA_OPTION_LABELS[m] || m;
    sel.appendChild(opt);
  });
  sel.value = selected || methods[0] || "totp";
  sel.classList.toggle("hidden", methods.length <= 1);
  updateTwoFactorLabel();
}

function updateTwoFactorLabel() {
  const sel = $("set-method");
  const method = sel.value || "totp";
  $("set-totp-label").textContent = TWOFA_FIELD_LABELS[method] || "2FA code";
  $("set-totp").placeholder = TWOFA_PLACEHOLDERS[method] || "only if enabled";
}

async function doLogin() {
  const user = $("set-user").value.trim();
  const pass = $("set-pass").value;
  const code = $("set-totp").value.trim();
  const method = $("set-method").value || "";
  $("set-login-status").textContent = "Logging in...";
  const res = await call("login", user, pass, code, method);
  if (res.status === "2fa") {
    $("set-login-status").textContent = res.message;
    $("set-totp").disabled = false;
    setTwoFactorUI(res.methods || ["totp"], res.method);
    $("set-totp").focus();
    return;
  }
  if (res.status === "ok") {
    $("set-login-status").textContent = "Logged in.";
    $("set-pass").value = "";
    $("set-totp").value = "";
    $("set-totp").disabled = true;
    $("set-method").classList.add("hidden");
    $("set-login").disabled = true;
    $("set-logout").disabled = false;
    toast("Logged in.");
    await refreshState();
    return;
  }
  $("set-login-status").textContent = res.message || "Login failed.";
}

async function doLogout() {
  await call("logout");
  $("set-login-status").textContent = "Logged out.";
  $("set-login").disabled = false;
  $("set-logout").disabled = true;
  $("set-totp").disabled = true;
  setTwoFactorUI([], "");
  toast("Logged out.");
  await refreshState();
}

async function saveSettings() {
  const res = await call("save_settings", $("set-send").value, $("set-recv").value);
  if (!res.ok) { showAlert("Invalid settings", res.message || "Could not save settings."); return; }
  closeModal("modal-settings");
  toast("Settings saved.");
  await refreshState();
}

/* ------------------------------------------------------------------ files & updates */
async function importVrchatFavourites() {
  const res = await call("import_vrchat_favourites", 100);
  if (!res || !res.ok) {
    if (res && res.title) showAlert(res.title, res.message);
    return;
  }
  toast(res.added
    ? `Imported ${res.added} avatar${res.added === 1 ? "" : "s"} from VRChat.`
    : "All your VRChat Favourites are already in your list.");
  closeModal("modal-settings");
  await refreshState();
}

async function exportFavourites() {
  const res = await call("export_favourites");
  if (!res || res.cancelled) return;
  if (!res.ok) { if (res.title) showAlert(res.title, res.message); return; }
  toast(`Exported ${res.count} avatar${res.count === 1 ? "" : "s"}.`);
}

async function importFavourites() {
  const res = await call("import_favourites");
  if (!res || res.cancelled) return;
  if (!res.ok) { if (res.title) showAlert(res.title, res.message); return; }
  toast(res.added
    ? `Imported ${res.added} avatar${res.added === 1 ? "" : "s"}.`
    : "No new avatars to import.");
  await refreshState();
}

async function checkUpdates(quiet) {
  const res = await call("check_updates");
  if (!res || !res.ok) {
    if (!quiet) toast("Could not check for updates.");
    return;
  }
  if (res.update) {
    const yes = await showConfirm(
      "Update available",
      `A newer version (v${res.latest}) is available. You're on v${res.current}. ` +
      "Open the download page?");
    if (yes) await call("open_url", res.url);
  } else if (!quiet) {
    toast("You're up to date (v" + res.current + ").");
  }
}

/* ------------------------------------------------------------------ state loop */
async function refreshState() {
  const next = await call("get_state", lastRevs);
  if (!next || !next.revs) return;
  state.current_avatar_id = next.current_avatar_id;
  state.status = next.status;
  state.logged_in = next.logged_in;
  state.session_expired = !!next.session_expired;
  state.username = next.username;
  state.pending_2fa = next.pending_2fa;
  state.osc = next.osc || state.osc;
  state.discovery = next.discovery || state.discovery;
  state.version = next.version || state.version;
  if (next.entries != null) state.entries = next.entries;
  if (next.logs != null) state.logs = next.logs;
  if (next.changes != null) state.changes = next.changes;
  lastRevs = next.revs;
  if (selectedId && !entryById(selectedId)) selectedId = null;
  render();
}

function startPolling() {
  refreshState();
  setInterval(refreshState, 700);
}

/* ------------------------------------------------------------------ wiring */
function setView(view) {
  currentView = view;
  renderHeader();
  if (view === "logs") renderLogs(true);
  else renderGrid(true);
}

function wire() {
  $("btn-add-current").addEventListener("click", addCurrent);
  $("btn-add-id").addEventListener("click", addByIdPrompt);
  $("rail-settings").addEventListener("click", openSettings);
  $("session-expired").addEventListener("click", openSettings);

  document.querySelectorAll(".rail-btn[data-view]").forEach((btn) => {
    btn.addEventListener("click", () => setView(btn.dataset.view));
  });
  document.querySelectorAll(".chip").forEach((chip) => {
    chip.addEventListener("click", () => {
      currentFilter = chip.dataset.filter;
      renderHeader();
      renderGrid();
    });
  });

  $("search").addEventListener("input", () => {
    renderGrid();
    renderLogs();
  });
  $("sort").addEventListener("change", renderGrid);

  document.querySelectorAll(".log-tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      logTab = tab.dataset.logtab;
      renderLogs(true);
    });
  });

  $("logs-clear").addEventListener("click", async () => {
    const players = logTab === "players";
    const label = players ? "player changes" : "logged avatars";
    const yes = await showConfirm("Clear logs", `Remove all ${label}?`);
    if (!yes) return;
    await call(players ? "clear_changes" : "clear_logs");
    await refreshState();
  });

  $("logs-save-all").addEventListener("click", async () => {
    const res = await call("save_all_logs");
    if (!res || !res.ok) return;
    toast(res.added
      ? `Saved ${res.added} avatar${res.added === 1 ? "" : "s"}. Fetching metadata...`
      : "Nothing new to save.");
    await refreshState();
  });

  $("drawer-close").addEventListener("click", closeDrawer);
  $("drawer-backdrop").addEventListener("click", closeDrawer);

  $("btn-wear").addEventListener("click", () => wear(selectedId));
  $("btn-delete").addEventListener("click", del);
  $("btn-copy").addEventListener("click", copyId);
  $("btn-refresh").addEventListener("click", refreshMeta);
  $("btn-fav").addEventListener("click", () => { if (selectedId) toggleFavorite(selectedId); });

  $("d-name").addEventListener("input", scheduleSave);
  $("d-notes").addEventListener("input", scheduleSave);
  $("d-tags").addEventListener("input", scheduleSave);
  $("d-notes").addEventListener("keydown", (e) => {
    if (e.key === "s" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); scheduleSave(); }
  });

  $("id-submit").addEventListener("click", addByIdSubmit);
  $("id-input").addEventListener("keydown", (e) => { if (e.key === "Enter") addByIdSubmit(); });

  $("set-login").addEventListener("click", doLogin);
  $("set-logout").addEventListener("click", doLogout);
  $("set-method").addEventListener("change", updateTwoFactorLabel);
  $("set-save").addEventListener("click", saveSettings);
  $("set-open-folder").addEventListener("click", () => call("open_data_folder"));
  $("set-export").addEventListener("click", exportFavourites);
  $("set-import-vrc").addEventListener("click", importVrchatFavourites);
  $("set-import").addEventListener("click", importFavourites);
  $("set-update").addEventListener("click", () => checkUpdates(false));
  $("set-refresh-all").addEventListener("click", async () => {
    const res = await call("refresh_all_metadata");
    if (!res.ok && res.title) showAlert(res.title, res.message);
  });
  $("set-clean-thumbs").addEventListener("click", async () => {
    const yes = await showConfirm("Clean thumbnails",
      "Delete cached thumbnails that no longer belong to a favourite? " +
      "Anything still in use is kept.");
    if (!yes) return;
    const res = await call("prune_thumbnails");
    if (res && res.ok) toast(`${res.removed} thumbnail(s) removed.`);
  });

  document.querySelectorAll("[data-close]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const modal = btn.closest(".modal");
      if (modal) closeModal(modal.id);
    });
  });
  document.querySelectorAll(".modal").forEach((modal) => {
    modal.addEventListener("click", (e) => {
      if (e.target === modal && modal.id !== "modal-confirm") closeModal(modal.id);
    });
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      if (!$("context-menu").classList.contains("hidden")) { hideContextMenu(); return; }
      // showConfirm owns Escape while it is open; it stops propagation in the
      // capture phase so the promise settles. This guard is belt-and-braces so
      // the dialog can never be hidden without settling.
      if (pendingConfirm) return;
      const open = [...document.querySelectorAll(".modal:not(.hidden)")];
      if (open.length) { open.forEach((m) => closeModal(m.id)); return; }
      if (!$("drawer").classList.contains("hidden")) closeDrawer();
    }
  });
  document.addEventListener("click", hideContextMenu);
  document.addEventListener("scroll", hideContextMenu, true);
  window.addEventListener("resize", hideContextMenu);
}

window.addEventListener("pywebviewready", () => {
  wire();
  startPolling();
  setTimeout(() => checkUpdates(true), 3000);
});
