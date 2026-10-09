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
  motion: "full",
  infinite_scroll: false,
  discovery: { sources: {}, backlog: 0, db_path: "" },
  osc: { listening: false, error: null, seen_traffic: false },
};

let currentView = "home";     // home | logs
let currentFilter = "all";    // all | favorites | public | quest | pc
// Group is kept separate from currentFilter rather than folded into it: those
// five names are a fixed vocabulary the rest of the code compares against,
// while group names are whatever the user typed. Sharing one variable would let
// a group called "all" silently become the show-everything filter.
// null = every group. "" is reserved for the Ungrouped chip, so the two must
// not share a value.
let currentGroup = null;      // null = all groups | "" = ungrouped | key
let logTab = "avatars";       // avatars | players
let selectedId = null;
let lastStatus = "";
let lastRevs = null;
let lastGridSig = null;
let lastLogsSig = null;
let lastChangesSig = null;
let suppressSave = false;
let saveTimer = null;

// Multi-select. Ctrl+click toggles one, Shift+click takes a range, and a plain
// click keeps the old single-select drawer behaviour.
let selection = new Set();
let lastClickedId = null;
// Entries removed by the last delete, kept so it can be undone.
let undoBuffer = [];
let activeJob = null;

const thumbCache = {};    // id -> data URI
const thumbKey = {};      // id -> thumb filename currently cached
const thumbPending = {};  // id -> true
// Base64 thumbnails are the *fallback* path, used only when the backend's
// loopback thumbnail server is not running. Both limits are enforced: a count
// cap alone is useless when the files range from 10 KB to 4 MB, and a byte cap
// alone would let thousands of tiny placeholders pile up. 200 entries or 24 MB,
// whichever arrives first, keeps a fallback session in the tens of megabytes
// rather than the gigabytes the uncapped version reached.
const THUMB_CACHE_MAX = 200;
const THUMB_CACHE_MAX_BYTES = 24 * 1024 * 1024;
let thumbBytes = 0;
const thumbOrder = [];

// How far ahead of the viewport a thumbnail is fetched, in CSS pixels. A
// screenful of slack means scrolling never reveals an image that has not
// already been asked for, while a list of a few thousand rows still only ever
// holds a few dozen real images.
const THUMB_MARGIN = "600px";

// Shown wherever an avatar has no thumbnail yet. Shared by every placeholder
// slot, so it costs one string in memory rather than one per row.
const BLANK_THUMB =
  "data:image/svg+xml;utf8," +
  encodeURIComponent("<svg xmlns='http://www.w3.org/2000/svg' width='200' height='300'>"
    + "<rect width='200' height='300' fill='#141414'/></svg>");

// Group names are typed by hand, so they are bounded and stripped of control
// characters before they reach a chip or an export. These two mirror
// storage.MAX_GROUP_NAME and the isprintable() filter in
// storage.normalize_group.
const MAX_GROUP_NAME = 40;
const CONTROL_CHARS = /[\u0000-\u001f\u007f]/g;

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

/* ------------------------------------------------------------- selection */
function clearSelection() {
  selection.clear();
  lastClickedId = null;
  renderSelection();
}

function toggleSelect(id, additive) {
  if (!additive) {
    const only = selection.size === 1 && selection.has(id);
    selection.clear();
    if (!only) selection.add(id);
  } else if (selection.has(id)) {
    selection.delete(id);
  } else {
    selection.add(id);
  }
  lastClickedId = id;
  renderSelection();
}

function selectRange(toId) {
  const list = visibleEntries().map((e) => e.id);
  const from = list.indexOf(lastClickedId);
  const to = list.indexOf(toId);
  if (from === -1 || to === -1) {
    toggleSelect(toId, true);
    return;
  }
  const [lo, hi] = from < to ? [from, to] : [to, from];
  for (let i = lo; i <= hi; i++) selection.add(list[i]);
  renderSelection();
}

function renderSelection() {
  const bar = $("bulk-bar");
  if (!bar) return;
  const n = selection.size;
  if (n === 0) {
    bar.classList.add("hidden");
  } else {
    revealOnce(bar);
    bar.classList.remove("hidden");
  }
  $("bulk-count").textContent = n === 1 ? "1 selected" : `${n} selected`;
  document.querySelectorAll(".card").forEach((card) => {
    card.classList.toggle("checked", selection.has(card.dataset.id));
  });
}

async function runBulk(action, value) {
  const ids = [...selection];
  if (!ids.length) return;
  // Captured before the delete, because afterwards the entries are gone from
  // state and there is nothing left to restore. This is why bulk delete used to
  // show no undo at all while its own confirmation promised one: the promise was
  // made, and nothing was kept to keep it.
  const doomed = action === "delete"
    ? ids.map((id) => entryById(id)).filter(Boolean)
    : [];
  if (action === "delete") {
    const yes = await showConfirm(
      "Delete",
      `Remove ${ids.length} avatar${ids.length === 1 ? "" : "s"} from your favourites? ` +
      "You can undo this for a few seconds afterwards.");
    if (!yes) return;
  }
  const res = await call("bulk_action", ids, action, value || "");
  if (!res || !res.ok) {
    if (res && res.title) showAlert(res.title, res.message);
    else if (res && res.message) toast(res.message);
    return;
  }
  clearSelection();
  await refreshState();
  if (action === "delete" && doomed.length) {
    const label = doomed.length === 1
      ? `Removed "${doomed[0].name || doomed[0].id}".`
      : `Removed ${doomed.length} avatars.`;
    offerUndo(doomed, label);
  }
}

/* ----------------------------------------------------------------- undo */
function offerUndo(entries, label) {
  const bar = $("undo-bar");
  if (!bar || !entries.length) return;
  undoBuffer = entries;
  $("undo-label").textContent = label;
  bar.classList.remove("hidden");
  clearTimeout(offerUndo._t);
  offerUndo._t = setTimeout(() => {
    bar.classList.add("hidden");
    undoBuffer = [];
  }, 8000);
}

async function doUndo() {
  const entries = undoBuffer;
  undoBuffer = [];
  $("undo-bar").classList.add("hidden");
  let restored = 0;
  for (const entry of entries) {
    const res = await call("restore_entry", entry);
    if (res && res.ok) restored++;
  }
  toast(restored === 1 ? "Restored 1 avatar." : `Restored ${restored} avatars.`);
  await refreshState();
}

// Group names are matched case-insensitively, so "Furry" and "furry" are one
// group. Kept in step with storage.MAX_GROUP_NAME and storage.group_key on the
// Python side: collapsing whitespace, dropping control characters and truncating
// all happen before the comparison. If the two disagreed, a name the backend had
// already trimmed to 40 characters would get a different key here, and the
// dropdown would show "No group" for an avatar that plainly has one.
function groupKey(name) {
  const text = String(name == null ? "" : name);
  const collapsed = text.split(/\s+/).filter(Boolean).join(" ");
  const printable = collapsed.replace(CONTROL_CHARS, "");
  return printable.slice(0, MAX_GROUP_NAME).toLowerCase();
}

function entryGroupKey(entry) {
  return groupKey(entry.group);
}

// The distinct groups in the current list, with counts, sorted by name but
// carrying the casing the user first typed. Derived from the entries rather
// than stored separately: a group exists exactly when some avatar is in it, so
// the chips cannot drift out of sync and nothing needs pruning when a group
// empties.
function groupSummary() {
  const order = [];
  const byKey = new Map();
  let ungrouped = 0;
  for (const entry of state.entries || []) {
    const key = entryGroupKey(entry);
    if (!key) { ungrouped++; continue; }
    let row = byKey.get(key);
    if (!row) {
      row = { key, name: (entry.group || "").trim(), count: 0 };
      byKey.set(key, row);
      order.push(row);
    }
    row.count++;
  }
  order.sort((a, b) => a.name.localeCompare(b.name));
  return { groups: order, ungrouped };
}

// Renders the group chips after the five fixed ones. Called from renderHeader,
// so it keeps pace with edits and with the rev-driven state refresh.
function renderGroupChips() {
  const row = $("group-chips");
  if (!row) return;
  const { groups, ungrouped } = groupSummary();
  row.innerHTML = "";
  if (!groups.length && !ungrouped) return;

  const makeChip = (key, label, count) => {
    const btn = document.createElement("button");
    btn.className = "chip" + (currentGroup === key ? " active" : "");
    btn.dataset.group = key;
    btn.appendChild(document.createTextNode(label));
    const n = document.createElement("span");
    n.className = "chip-count";
    n.textContent = String(count);
    btn.appendChild(n);
    row.appendChild(btn);
  };

  for (const g of groups) makeChip(g.key, g.name, g.count);
  // Ungrouped needs a chip too, or those avatars become unreachable by browsing.
  if (ungrouped) makeChip("", "Ungrouped", ungrouped);
}

function matchesFilter(entry) {
  // Group and the fixed filter compose: selecting "Quest" inside "Furry" is the
  // intersection, which is what picking both implies.
  //
  // null means every group, "" means the ungrouped bucket specifically. Those
  // have to be distinct values: conflating them makes the Ungrouped chip a
  // duplicate of the All chip.
  if (currentGroup !== null) {
    const group = entryGroupKey(entry);
    if (currentGroup === "") {
      if (group) return false;
    } else if (group !== currentGroup) {
      return false;
    }
  }
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
function toast(msg, duration) {
  const el = $("toast");
  el.textContent = msg;
  el.classList.add("show");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.remove("show"), duration || 3200);
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

// Only one text prompt may be pending; opening a second settles the first so
// its awaiting caller is never left hanging, exactly as showConfirm does.
let pendingPrompt = null;

// Shows or clears the group-list mode. Sharing the prompt modal means one set
// of dismissal handlers rather than two that can drift apart.
function setPromptMode(mode) {
  $("prompt-input").classList.toggle("hidden", mode === "list");
  $("prompt-list").classList.toggle("hidden", mode !== "list");
  $("prompt-ok").classList.toggle("hidden", mode === "list");
}

// The drawer and the bulk bar both need "pick a group, or make one". A free-text
// field invites typos, and since group names match case-insensitively a typo
// becomes a near-duplicate group that is easy to miss and annoying to clean up.
// So this offers the real list, with "New group..." going through showPrompt.
function showGroupPicker(title, message) {
  // Same one-at-a-time rule as showPrompt: a second picker cancels the first
  // rather than leaving its caller awaiting forever.
  if (pendingPrompt) pendingPrompt(null);
  return new Promise((resolve) => {
    const { groups } = groupSummary();
    $("prompt-title").textContent = title || "Move to group";
    $("prompt-message").textContent = message || "";
    setPromptMode("list");

    const list = $("prompt-list");
    list.innerHTML = "";
    const add = (label, value) => {
      const btn = document.createElement("button");
      btn.className = "group-option";
      btn.textContent = label;
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        finish(value);
      });
      list.appendChild(btn);
    };

    if (groups.length) {
      for (const g of groups) {
        const count = document.createElement("span");
        count.className = "chip-count";
        count.textContent = String(g.count);
        const btn = document.createElement("button");
        btn.className = "group-option";
        btn.appendChild(document.createTextNode(g.name));
        btn.appendChild(count);
        btn.addEventListener("click", (e) => {
          e.stopPropagation();
          finish(g.name);
        });
        list.appendChild(btn);
      }
    } else {
      const none = document.createElement("p");
      none.className = "muted small";
      none.textContent = "No groups yet.";
      list.appendChild(none);
    }
    // "" is the sentinel for ungrouped, which the backend turns into "".
    add("No group (ungrouped)", "");
    add("＋ New group…", NEW_GROUP);

    openModal("modal-prompt");

    const modal = $("modal-prompt");
    const cancel = $("prompt-cancel");
    const finish = (value) => {
      cancel.removeEventListener("click", onCancel);
      modal.removeEventListener("click", onBackdrop);
      document.removeEventListener("keydown", onKeydown, true);
      pendingPrompt = null;
      closeModal("modal-prompt");
      resolve(value);
    };
    const onCancel = () => finish(null);
    const onBackdrop = (e) => { if (e.target === modal) finish(null); };
    const onKeydown = (e) => {
      if (e.key !== "Escape") return;
      e.stopPropagation();
      e.preventDefault();
      finish(null);
    };
    cancel.addEventListener("click", onCancel);
    modal.addEventListener("click", onBackdrop);
    document.addEventListener("keydown", onKeydown, true);
    pendingPrompt = finish;
  });
}

// Sentinel for "the user chose New group" in showGroupPicker. A name could
// never be this, since normalize_group strips whitespace and truncates.
const NEW_GROUP = "\u0000new";

// Creates a group by name, returning it, or null if the user backs out.
// Used by both the drawer's dropdown and the bulk picker.
async function promptForGroupName(title, message, initial) {
  const value = await showPrompt(title, message, initial);
  if (value === null) return null;
  // Trim the same way the backend will, so the name saved is the name shown.
  const collapsed = String(value).split(/\s+/).filter(Boolean).join(" ");
  return collapsed.replace(CONTROL_CHARS, "").slice(0, MAX_GROUP_NAME) || null;
}

// A styled replacement for window.prompt. The native one renders as a separate
// browser window titled by host and port -- "127.0.0.1:23017 says" -- which
// looks like a download warning rather than part of the app. Resolves to the
// trimmed string, or null when cancelled or left blank.
function showPrompt(title, message, initial = "") {
  if (pendingPrompt) pendingPrompt(null);
  return new Promise((resolve) => {
    $("prompt-title").textContent = title || "Enter a value";
    $("prompt-message").textContent = message || "";
    setPromptMode("text");

    const input = $("prompt-input");
    input.value = initial == null ? "" : String(initial);

    const modal = $("modal-prompt");
    const ok = $("prompt-ok");
    const cancel = $("prompt-cancel");
    openModal("modal-prompt");

    // Every exit path must settle the promise: OK, Cancel, backdrop, Escape.
    const finish = (value) => {
      ok.removeEventListener("click", onOk);
      cancel.removeEventListener("click", onCancel);
      input.removeEventListener("keydown", onInputKey);
      modal.removeEventListener("click", onBackdrop);
      document.removeEventListener("keydown", onKeydown, true);
      pendingPrompt = null;
      closeModal("modal-prompt");
      resolve(value);
    };
    const onOk = () => finish(input.value.trim() || null);
    const onCancel = () => finish(null);
    const onBackdrop = (e) => { if (e.target === modal) finish(null); };
    const onKeydown = (e) => {
      if (e.key !== "Escape") return;
      e.stopPropagation();
      e.preventDefault();
      finish(null);
    };
    // Enter submits, which a lone text field otherwise will not do.
    const onInputKey = (e) => {
      if (e.key !== "Enter") return;
      e.preventDefault();
      finish(input.value.trim() || null);
    };

    ok.addEventListener("click", onOk);
    cancel.addEventListener("click", onCancel);
    input.addEventListener("keydown", onInputKey);
    modal.addEventListener("click", onBackdrop);
    document.addEventListener("keydown", onKeydown, true);

    pendingPrompt = finish;
    input.focus();
    input.select();
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
    btn.className = "context-item" + (item.danger ? " danger" : "")
      + (item.disabled ? " disabled" : "");
    btn.innerHTML = `<span class="ico">${item.icon || ""}</span>${escapeHtml(item.label)}`;
    if (item.disabled) btn.disabled = true;
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      // Guarded as well as disabled: a disabled button swallows clicks in a real
      // browser, but the action is still callable from a keyboard path.
      if (item.disabled) return;
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
  const previous = thumbCache[id];
  if (previous === undefined) {
    thumbOrder.push(id);
  } else if (previous !== src) {
    thumbBytes -= previous.length;
  }
  thumbCache[id] = src;
  thumbKey[id] = key;
  thumbBytes += src.length;
  while (thumbOrder.length > THUMB_CACHE_MAX || thumbBytes > THUMB_CACHE_MAX_BYTES) {
    const evicted = thumbOrder.shift();
    if (evicted === undefined) break;
    thumbBytes -= (thumbCache[evicted] || "").length;
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

/* ---------------------------------------------------- lazy thumbnail loading */
// A decoded avatar thumbnail costs about a megabyte, and the webview keeps a
// decoded copy for every image that has been painted as long as the element
// lives. Building one <img> per row therefore costs roughly a megabyte per row
// no matter how small the file on disk is -- a few thousand favourites is a few
// gigabytes, which is exactly what this replaces.
//
// So an <img> only gets a real source while it is near the viewport, and drops
// back to a shared placeholder once it scrolls away. Off-screen rows then cost
// a DOM node and nothing else.
let thumbObserver = null;

// Where cached thumbnails can be fetched from as plain URLs. Empty when the
// backend's loopback server is not running, which is the signal to carry image
// bytes across the bridge as base64 instead.
let thumbBase = "";

function setThumbBase(base) {
  const next = typeof base === "string" ? base : "";
  if (next === thumbBase) return;
  thumbBase = next;
  // Every base64 copy is now redundant, and it is the largest single thing the
  // webview is holding, so drop the lot rather than waiting for eviction.
  for (const id of Object.keys(thumbCache)) {
    delete thumbCache[id];
    delete thumbKey[id];
  }
  thumbOrder.length = 0;
  thumbBytes = 0;
}

function thumbObserverReady() {
  if (thumbObserver) return thumbObserver;
  if (typeof IntersectionObserver !== "function") return null;
  try {
    // Root null, not the scroll container: intersection is already clipped by
    // the scrolling ancestor, so this one observer covers both the avatar grid
    // and the log lists without caring which is on screen.
    thumbObserver = new IntersectionObserver((entries) => {
      for (const entry of entries) onThumbVisibility(entry.target, entry.isIntersecting);
    }, { root: null, rootMargin: THUMB_MARGIN });
  } catch (err) {
    return null;
  }
  return thumbObserver;
}

// A cached thumbnail as a plain URL, or "" when there is no loopback server to
// fetch it from.
function thumbUrlFor(name) {
  if (!thumbBase || !name) return "";
  return thumbBase + "/" + encodeURIComponent(name);
}

// Resolve an <img>'s real source from what it was registered with. Only ever
// called for an image that is about to be shown.
function resolveThumbSrc(img) {
  const key = img.dataset.thumbKey;
  if (!key) return "";
  if (thumbBase) {
    // Already proved this exact file will not load from the loopback server, so
    // do not ask again on every repaint.
    if (img.dataset.thumbBroken === key) return "";
    return thumbUrlFor(key);
  }
  return ensureThumb({ id: img.dataset.thumbId, thumb: key }) || "";
}

function onThumbVisibility(img, visible) {
  if (!visible) {
    // Dropping the source is what actually frees the decoded bitmap. The element
    // stays, so nothing reflows and scrolling back restores it.
    img.src = BLANK_THUMB;
    return;
  }
  if (img.dataset.thumbBroken === img.dataset.thumbKey) return;
  const src = resolveThumbSrc(img);
  img.src = src || BLANK_THUMB;
}

// If a loopback URL will not load, fall back to base64 for this image.
//
// Marked against the thumbnail's own filename, not as a bare flag: a refreshed
// avatar gets a new filename, so it is allowed to try again, while a genuinely
// missing file is attempted exactly once. Setting the marker *before* the
// fallback is applied is what stops a broken base64 payload re-triggering this
// handler for ever.
function thumbLoadFailed(img) {
  if (!thumbBase) return;
  const key = img.dataset.thumbKey;
  if (!key || img.dataset.thumbBroken === key) return;
  img.dataset.thumbBroken = key;
  const src = ensureThumb({ id: img.dataset.thumbId, thumb: key });
  if (src) img.src = src;
}

function lazyThumb(img, entry, placeholder) {
  if (!img) return placeholder || "";
  const key = (entry && entry.thumb) || "";
  const fallback = placeholder || BLANK_THUMB;
  img.dataset.thumbId = entry ? entry.id : "";
  img.dataset.thumbKey = key;
  if (!key) {
    img.src = fallback;
    return fallback;
  }
  img.addEventListener("error", () => thumbLoadFailed(img));
  const observer = thumbObserverReady();
  if (!observer) {
    // No IntersectionObserver: behave exactly as before, loading everything.
    const src = resolveThumbSrc(img);
    img.src = src || fallback;
    return src || fallback;
  }
  img.src = BLANK_THUMB;
  observer.observe(img);
  return fallback;
}

function applyThumb(id) {
  const src = thumbCache[id];
  if (!src) return;
  document.querySelectorAll(`[data-thumb-id="${CSS.escape(id)}"]`).forEach((img) => {
    img.src = src;
  });
}

/* ---------------------------------------------------------------- paging */
// The three long lists here (the avatar grid and the two log tabs) can each hold
// thousands of rows, and one row is not one node: a card is a dozen elements,
// so a 5,000-avatar grid is tens of thousands of live nodes the webview carries
// for as long as the view is open. So the lists are paged rather than drawn
// whole, and only the current page's rows exist in the DOM.
//
// 50 rows per page, not more: this is also the unit the lazy image loading works
// in, since only drawn rows ever hold a thumbnail. A smaller page means fewer
// decoded images alive at once and a faster first paint.
const PAGE_ROWS = 50;
// How many numbered buttons to show around the current page. A hard cap, so a
// 10,000-row list does not render 200 buttons.
const PAGE_BUTTON_CAP = 7;

// True when the user has opted out of paging on the avatar grid, in which case
// the whole list is drawn at once. Applies to the grid only: the log tabs stay
// paged, because their lists are bounded by the log limits the user has already
// agreed to and are re-sorted as new rows arrive, so a growing DOM there is a
// cost with nothing to show for it.
function infiniteGrid() {
  return !!state.infinite_scroll;
}

// How many rows the grid may draw. The whole list when infinite scrolling is on,
// otherwise one page.
function gridSlice(name, list) {
  if (name === "grid" && infiniteGrid()) return list;
  return pageSlice(name, list);
}

// Paging is off entirely for the grid when infinite scrolling is on, so there is
// no pager to draw -- not a pager that does nothing.
function pagerFor(name) {
  if (name === "grid" && infiniteGrid()) return null;
  return renderPager(name);
}

// The status bar. Under infinite scrolling there are no pages to describe, so
// this must not claim there is a page 1 of 7 the user cannot reach.
function gridCountText(total) {
  const n = `${total} avatar${total === 1 ? "" : "s"}`;
  if (infiniteGrid()) return n;
  const pages = pageCount(total);
  return pages > 1 ? `${n} · page ${currentPage.grid} of ${pages}` : n;
}

// Which page each list is on. Not reset by a data refresh -- a new avatar
// appearing must not throw the user back to page 1 -- but reset by anything that
// changes what the list is showing.
const currentPage = { grid: 1, logs: 1, changes: 1 };
const listTotal = { grid: 0, logs: 0, changes: 0 };
// null means "this list has never been drawn".
const pageKey = { grid: null, logs: null, changes: null };

function pageCount(total) {
  return Math.max(1, Math.ceil(total / PAGE_ROWS));
}

// Keeps the stored page inside the list. A search or a delete can shrink the list
// out from under the page the user was on, and a pager pointing at page 9 of a
// 2-page list would be a dead end with no obvious way back.
function clampPage(name) {
  const pages = pageCount(listTotal[name]);
  if (currentPage[name] > pages) currentPage[name] = pages;
  if (currentPage[name] < 1) currentPage[name] = 1;
  return currentPage[name];
}

// The slice of the list to draw for the current page.
function pageSlice(name, list) {
  const page = clampPage(name);
  const start = (page - 1) * PAGE_ROWS;
  return list.slice(start, start + PAGE_ROWS);
}

// Called by each renderer before it slices, to record the list's total and
// settle which page to draw. Resets to page 1 whenever the filters, search, sort
// or tab changed since last time. The key is the JSON of the parts rather than
// a joined string, because a group name and a search term are arbitrary text
// and must not be able to join into the same key.
//
// Returns the page to draw. Use pageCount() on the *total*, not on this: this is
// a page number, and conflating the two silently yields a single page forever.
function pageFor(name, parts, total) {
  const key = JSON.stringify(parts);
  if (pageKey[name] !== null && pageKey[name] !== key) {
    currentPage[name] = 1;
  }
  pageKey[name] = key;
  listTotal[name] = total;
  return clampPage(name);
}

function activeListName() {
  if (currentView !== "logs") return "grid";
  return logTab === "players" ? "changes" : "logs";
}

function goToPage(name, page) {
  // Infinite scrolling has no pages, so a call here must change nothing -- not
  // the page number either. Deliberately left intact rather than reset to 1:
  // nothing renders a page button in this mode, so this only fires from a stale
  // control, and keeping the number means switching back to paging returns the
  // user to the page they were on rather than to the top.
  if (name === "grid" && infiniteGrid()) return;
  const pages = pageCount(listTotal[name]);
  const next = Math.min(Math.max(1, page), pages);
  if (next === currentPage[name]) return;
  // Set before rendering, because the renderers read the page to draw it and
  // clamp it into range. Setting it afterwards would draw the old page and then
  // claim the new one.
  currentPage[name] = next;
  if (name === "grid") renderGrid(true);
  else renderLogs(true);
  // Jumping pages leaves the previous page's scroll offset in place, which would
  // otherwise park the user halfway down a short final page.
  const wrap = $(name === "grid" ? "grid-wrap" : "logs-wrap");
  if (wrap) wrap.scrollTop = 0;
}

function goToActivePage(page) {
  goToPage(activeListName(), page);
}

// The page buttons. Returns null when everything fits on one page, because a
// lone "1" is noise rather than navigation.
function renderPager(name) {
  const total = listTotal[name];
  const pages = pageCount(total);
  if (pages <= 1) return null;

  const nav = document.createElement("nav");
  nav.className = "pager";
  nav.setAttribute("aria-label", "Pagination");

  const addButton = (label, page, opts) => {
    const btn = document.createElement("button");
    btn.className = "pager-btn" + ((opts && opts.active) ? " active" : "")
      + ((opts && opts.disabled) ? " disabled" : "");
    btn.textContent = label;
    btn.disabled = !!(opts && opts.disabled);
    if (opts && opts.title) btn.title = opts.title;
    btn.addEventListener("click", () => goToPage(name, page));
    nav.appendChild(btn);
    return btn;
  };

  const here = clampPage(name);
  addButton("‹", here - 1, {
    disabled: here <= 1,
    title: "Previous page",
  });

  // A window of PAGE_BUTTON_CAP - 2 consecutive pages centred on the current one,
  // so the numbers move with the user instead of stranding them on a row that
  // does not change until they are halfway through the list.
  const span = PAGE_BUTTON_CAP - 2;
  let first = Math.max(1, here - Math.floor(span / 2));
  let last = Math.min(pages, first + span - 1);
  // Sliding back when we hit the end keeps the window full, so the last page is
  // never shown alone.
  first = Math.max(1, Math.min(first, last - span + 1));

  if (first > 1) {
    addButton("1", 1, {});
    if (first > 2) nav.appendChild(gap());
  }
  for (let page = first; page <= last; page++) {
    addButton(String(page), page, { active: page === here });
  }
  if (last < pages) {
    if (last < pages - 1) nav.appendChild(gap());
    addButton(String(pages), pages, {});
  }

  addButton("›", here + 1, {
    disabled: here >= pages,
    title: "Next page",
  });

  return nav;
}

// The ellipsis between page groups. A plain span, so it is never focusable and
// cannot be mistaken for a page you can jump to.
function gap() {
  const el = document.createElement("span");
  el.className = "pager-gap";
  el.textContent = "…";
  return el;
}

/* ------------------------------------------------------------------ render */
function renderHeader() {
  $("page-title").textContent = VIEW_TITLES[currentView] || "Avatars";
  document.querySelectorAll(".rail-btn[data-view]").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.view === currentView);
  });
  // Group chips are rebuilt from state, so the fixed-chip loop below must not
  // also touch them: they carry data-group, not data-filter.
  document.querySelectorAll(".chip[data-filter]").forEach((chip) => {
    chip.classList.toggle("active", chip.dataset.filter === currentFilter);
  });
  const isLogs = currentView === "logs";
  renderGroupChips();
  // The whole row, groups included, is hidden on the log scanner: it filters
  // the avatar grid, so showing it there would be a control doing nothing.
  $("chips").classList.toggle("hidden", isLogs);
  $("group-chips").classList.toggle("hidden", isLogs);
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
    // The selected group changes which entries survive visibleEntries, so it has
    // to be in the signature or a chip click is a no-op once the grid has been
    // rendered at least once.
    g: currentGroup,
    q: $("search").value,
    s: $("sort").value,
    c: state.current_avatar_id,
    // Part of the signature because it changes what the grid draws: toggling it
    // in Settings has to repaint, and it arrives on the poll rather than with
    // the save that set it.
    inf: infiniteGrid(),
    items: list.map((e) => [e.id, e.name, e.thumb, e.author, e.favorite, !!e.inaccessible,
      e.release_status, (e.platforms || []).join(","), (e.tags || []).join(",")]),
  });
  if (!force && sig === lastGridSig) return;
  lastGridSig = sig;

  const grid = $("grid");
  grid.innerHTML = "";

  if (!list.length) {
    pageFor("grid", [currentFilter, currentGroup, $("search").value,
      $("sort").value], 0);
    $("grid-empty").classList.remove("hidden");
    $("count").textContent = "0 avatars";
    return;
  }
  $("grid-empty").classList.add("hidden");
  // Recorded either way: listTotal.grid is what the clamp and the pager read,
  // and under infinite scrolling clampPage is what stops a stale page number
  // from surviving a later switch back to paging.
  pageFor("grid", [currentFilter, currentGroup, $("search").value,
    $("sort").value], list.length);
  $("count").textContent = gridCountText(list.length);

  const frag = document.createDocumentFragment();
  for (const entry of gridSlice("grid", list)) {
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

    const favOn = entry.favorite ? "on" : "";
    const badges = [];
    if (entry.inaccessible) {
      // This records a previous refusal, not a permanent wearability verdict.
      badges.push('<span class="platform-badge" title="VRChat refused the previous wear request. '
        + 'You can try again.">last attempt refused</span>');
    }
    if (entry.release_status) {
      badges.push(`<span class="platform-badge ${entry.release_status === "public" ? "public" : ""}">${escapeHtml(entry.release_status)}</span>`);
    }
    (entry.platforms || []).forEach((p) =>
      badges.push(`<span class="platform-badge">${escapeHtml(p)}</span>`));
    const badgesHtml = badges.length ? `<div class="card-badges">${badges.join("")}</div>` : "";

    card.innerHTML = `
      <div class="poster">
        <img data-thumb-id="${escapeHtml(entry.id)}" alt="" />
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

    // Hands the image its real source only while it is near the viewport, and
    // drops it again once it scrolls away.
    lazyThumb(card.querySelector(".poster img"), entry, placeholderDataUri(entry.name));

    card.addEventListener("click", (e) => {
      // Ctrl/Cmd toggles selection, Shift extends a range, plain click opens
      // the drawer as before.
      if (e.ctrlKey || e.metaKey) { toggleSelect(entry.id, false); return; }
      if (e.shiftKey) { selectRange(entry.id); return; }
      openDrawer(entry.id);
    });
    card.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        openDrawer(entry.id);
      } else if (e.key.toLowerCase() === "x" && (e.ctrlKey || e.metaKey)) {
        e.preventDefault();
        toggleSelect(entry.id, false);
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
  const pager = pagerFor("grid");
  if (pager) frag.appendChild(pager);
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
  pageFor("logs", [logTab, $("search").value], logs.length);
  const pages = pageCount(logs.length);
  const page = currentPage.logs;
  $("logs-count").textContent = !logs.length ? ""
    : pages > 1
      ? `${logs.length} logged · page ${page} of ${pages}`
      : `${logs.length} avatar${logs.length === 1 ? "" : "s"} logged`;
  if (!logs.length) return;

  const frag = document.createDocumentFragment();
  for (const log of pageSlice("logs", logs)) {
    const fav = entryById(log.id);
    const name = log.name || (fav ? fav.name : "");
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
      <img class="log-thumb" data-thumb-id="${saved ? escapeHtml(log.id) : ""}" alt="" />
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
    // Only a saved avatar has an image on disk to show. An unsaved one keeps the
    // cheap letter placeholder, so the log tab costs nothing extra.
    if (fav) lazyThumb(row.querySelector(".log-thumb"), fav, placeholderDataUri(name || log.id));
    else row.querySelector(".log-thumb").src = placeholderDataUri(name || log.id);
    row.querySelector(".save-log").addEventListener("click", () => saveFromLog(log.id));
    row.querySelector(".forget-log").addEventListener("click", () => forgetLog(log.id));
    row.addEventListener("contextmenu", async (e) => {
      e.preventDefault();
      showContextMenu(e.clientX, e.clientY, await forgetLogMenuItems(log));
    });
    frag.appendChild(row);
  }
  const pager = renderPager("logs");
  if (pager) frag.appendChild(pager);
  wrap.appendChild(frag);
}

function renderPlayerChanges(changes, force) {
  const sig = JSON.stringify(changes.map((e) =>
    [e.player, e.avatar, e.count, e.last_seen]));
  if (!force && sig === lastChangesSig) return;
  lastChangesSig = sig;

  const wrap = $("changes");
  wrap.innerHTML = "";
  pageFor("changes", [logTab, $("search").value], changes.length);
  const pages = pageCount(changes.length);
  const page = currentPage.changes;
  $("logs-count").textContent = !changes.length ? ""
    : pages > 1
      ? `${changes.length} logged · page ${page} of ${pages}`
      : `${changes.length} change${changes.length === 1 ? "" : "s"} logged`;
  if (!changes.length) return;

  const frag = document.createDocumentFragment();
  for (const change of pageSlice("changes", changes)) {
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
  const pager = renderPager("changes");
  if (pager) frag.appendChild(pager);
  wrap.appendChild(frag);
}

// Signature of the options currently in the drawer's group dropdown.
//
// render() runs on every 700ms poll, and this function used to rewrite the
// <select>'s options every time. Replacing a select's options closes an open
// dropdown and discards the choice in flight, which is why picking "New group"
// did nothing at all: the list was rebuilt out from under the click before the
// change event could fire. The options are now only rewritten when they are
// actually different; otherwise only the value is set.
let groupDropdownSig = null;

// Populates the drawer's group dropdown: every existing group, the current
// one if it somehow is not in the list (a stale or hand-edited file), "No
// group", and a New group entry.
function renderGroupDropdown(entry, keep) {
  const select = $("d-group");
  if (!select) return;
  const current = groupKey(entry.group);
  const { groups } = groupSummary();

  // Built first, so the signature can be compared before anything is touched.
  const options = groups.map((g) => [g.key, g.name]);
  // The entry's own group is normally already in the list. If not -- a stale
  // filter, or a hand-edited favourites.json -- keep it selectable rather than
  // silently showing "No group" and inviting a change nobody made.
  if (current && !options.some(([value]) => value === current)) {
    options.push([current, (entry.group || "").trim()]);
  }
  options.push(["", "No group"], [NEW_GROUP, "\uFF0B New group\u2026"]);
  // A pick the user has made but not yet saved has no option yet, and render()
  // runs on every poll, so keep it rather than snapping back to the saved
  // value for a moment.
  const wanted = keep || current;
  if (keep && !options.some(([value]) => value === keep)) options.push([keep, keep]);

  const sig = JSON.stringify(options);
  if (sig !== groupDropdownSig) {
    groupDropdownSig = sig;
    select.innerHTML = "";
    for (const [value, label] of options) {
      const opt = document.createElement("option");
      opt.value = value;
      opt.textContent = label;
      select.appendChild(opt);
    }
  }
  // Assigning the same value is harmless; assigning a different one is the point.
  if (select.value !== wanted) select.value = wanted;
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
  // Rebuilt every render, since the set of groups changes as other avatars are
  // edited. A <select> is rebuilt rather than patched for the same reason the
  // option list can grow or shrink between renders.
  renderGroupDropdown(entry, editing ? $("d-group").value : null);
  suppressSave = false;

  // The drawer holds a single image and is only open while the user is looking
  // at it, so it loads eagerly rather than through the scroll observer.
  const src = thumbUrlFor(entry.thumb) || ensureThumb(entry) || placeholderDataUri(entry.name);
  const preview = $("preview");
  preview.dataset.thumbId = entry.id;
  preview.dataset.thumbKey = entry.thumb || "";
  if (preview.getAttribute("src") !== src) preview.src = src;

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

// Why the local-cache source is not producing ids, per failure kind.
// avatars.sqlite is not created by VRChat -- it is absent from VRChat's own
// documentation of AppData/LocalLow -- so on a clean install there is nothing
// to read, and no amount of looking in the folder will help. Naming that is the
// difference between a user who fixes it and one who gives up.
const CACHE_HINTS = {
  missing: "no database yet - install an avatar tracker such as VRC-LOG to enable this",
  locked: "database is locked - close VRChat and any avatar tracker, then restart",
  unreadable: "could not read the database - check permissions",
  unsupported: "the file is not a readable SQLite database",
};

// What "empty" means, per source. Deliberately not the word "idle": both of
// these are the *healthy* steady state, and a source that is working perfectly
// must not be labelled in a way that reads like a fault. The cache database is
// simply caught up, and the live feed is empty because VRChat uploads and
// clears it on every world switch.
const EMPTY_NOTES = {
  "cache-db": "up to date",
  amplitude: "waiting for a world switch",
};

function renderDiscovery() {
  const el = $("discovery-state");
  if (!el) return;
  const sources = (state.discovery && state.discovery.sources) || {};
  const backlog = (state.discovery && state.discovery.backlog) || 0;
  const parts = [];
  let cacheStatus = "";
  for (const key of SOURCE_ORDER) {
    const status = sources[key];
    if (!status) continue;
    if (status === "ok") {
      parts.push(SOURCE_LABELS[key]);
    } else if (status === "empty") {
      parts.push(SOURCE_LABELS[key] + " (" + (EMPTY_NOTES[key] || "no new") + ")");
    } else {
      parts.push(SOURCE_LABELS[key] + " unavailable");
      if (key === "cache-db") cacheStatus = status;
    }
  }
  let text = parts.join(" · ");
  if (backlog) text += "  ·  " + backlog.toLocaleString() + " ids seen all-time";
  // Name what is being suppressed. VRChat's built-in defaults are wearable, so
  // they reach the log scanner like any other finding; saying they are filtered
  // is the difference between "working" and "quietly broken".
  const defaults = (state.discovery && state.discovery.defaults) || 0;
  if (defaults) {
    text += "  ·  " + defaults.toLocaleString() + " default avatars ignored";
  }
  // Say what to do about a dead source, not merely that it is dead.
  const hint = cacheStatus ? CACHE_HINTS[cacheStatus] : "";
  if (hint) text += "  ·  " + hint;
  el.textContent = text;
  el.title = cacheStatus
    ? "Checked: " + ((state.discovery && state.discovery.db_path) || "?")
      + "\n\nVRChat does not create this file. It appears only once an avatar"
      + "\ntracker such as VRC-LOG has written to it."
      + "\n\nWithout it the other sources still work: your own avatar changes"
      + "\narrive over OSC, and other players' avatars are read from VRChat's"
      + "\nown log."
    // Parenthesised on purpose: `path || "" + text` parses as
    // `path || ("" + text)`, which silently drops the explanation whenever the
    // path is non-empty -- which is the normal case.
    : (((state.discovery && state.discovery.db_path) || "")
        + "\n\n\"Up to date\" means the database is being read and no avatar has"
        + "\nbeen cached since the last check - not that anything is wrong.");
  const down = SOURCE_ORDER.some((k) => ["missing", "unsupported", "unreadable"].includes(sources[k]));
  el.classList.toggle("warn-text", down);
}

// Show an element with its entrance animation, but only on the hidden ->
// visible edge. These bars are toggled from render(), which runs on every
// 700ms poll, so animating unconditionally would make them flicker forever.
function revealOnce(el) {
  if (!el || !el.classList.contains("hidden")) return;
  replayAnimation(el, "bar-enter");
  clearAnimationWhenDone(el, "bar-enter");
}

/* ---------------------------------------------------------------- job bar */
function renderJob(job) {
  const bar = $("job-bar");
  if (!bar) return;
  activeJob = job || null;
  if (!job) {
    bar.classList.add("hidden");
    return;
  }
  revealOnce(bar);
  bar.classList.remove("hidden");
  const known = job.total > 0;
  bar.classList.toggle("indeterminate", !known);

  const label = job.cancelled
    ? (job.finished ? "Cancelled" : "Cancelling...")
    : (job.finished ? "Finished" : "Working...");
  $("job-label").textContent = label;

  const bits = [];
  if (known) bits.push(`${job.done} of ${job.total}`);
  if (job.failed) bits.push(`${job.failed} failed`);
  if (job.elapsed != null) bits.push(`${Math.round(job.elapsed)}s`);
  $("job-detail").textContent = job.message
    ? `${job.message}${bits.length ? "  ·  " + bits.join(" · ") : ""}`
    : bits.join(" · ");

  $("job-fill").style.width = (job.percent || 0) + "%";
  $("job-cancel").classList.toggle("hidden", !!job.finished);
  $("job-cancel").disabled = !!job.cancelled;
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
  renderSelection();
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
    {
      icon: "▶",
      label: "Wear Avatar",
      action: () => wear(entry.id),
    },
    {
      icon: "♥",
      label: entry.favorite ? "Remove from Favorites" : "Add to Favorites",
      action: () => toggleFavorite(entry.id),
    },
    { icon: "⧉", label: "Copy Avatar ID", action: () => copyEntryId(entry.id) },
    { icon: "⟳", label: "Refresh Metadata", action: () => refreshMetaFor(entry.id) },
    { sep: true },
    // Offered on a favourite too: an avatar you have already saved is often
    // exactly the one you keep seeing and no longer want cluttering the log.
    {
      icon: "🚫",
      label: "Never log this avatar",
      action: () => ignoreAvatar(entry.id, entry.name),
    },
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
  const entry = entryById(id);
  const res = await call("delete", id);
  if (selectedId === id) closeDrawer();
  selection.delete(id);
  await refreshState();
  // Keep the entry so it can be put back, notes and all.
  if (entry && (!res || res.ok)) offerUndo([entry], `Removed "${name}".`);
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

// Both "remove one row" and "never again" live in the same place, because they
// are the same decision at two levels: the row is what is on screen now, the
// block is what stops it coming back.
async function forgetLogMenuItems(log) {
  const fav = entryById(log.id);
  const name = log.name || (fav ? fav.name : "") || log.id;
  const items = [];
  if (!fav && !log.private) {
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
  items.push({
    icon: "🚫", label: "Never log this avatar",
    action: () => ignoreAvatar(log.id, name),
  });
  return items;
}

async function del() {
  const entry = entryById(selectedId);
  if (!entry) return;
  await deleteEntry(selectedId, entry.name);
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

// The group a draft should be saved under, resolving the dropdown's lowercase
// key back to the display name. Sending the key instead would quietly rewrite
// every group's stored casing to lowercase the first time it is picked from the
// drawer.
function draftGroupName() {
  const value = $("d-group").value;
  const match = groupSummary().groups.find((g) => g.key === value);
  return match ? match.name : value;
}

function captureDraft() {
  if (suppressSave || !selectedId) return;
  draft = {
    id: selectedId,
    name: $("d-name").value,
    notes: $("d-notes").value,
    tags: $("d-tags").value,
    // "" clears the group; null means "leave it alone" and is what the New
    // group sentinel needs, since that value is a mid-choice placeholder rather
    // than a selection. Confusing the two would make "No group" silently do
    // nothing, because save_details treats None as not supplied.
    group: $("d-group").value === NEW_GROUP ? null : draftGroupName(),
  };
}

async function flushDraft(announce) {
  clearTimeout(saveTimer);
  saveTimer = null;
  const pending = draft;
  draft = null;
  if (!pending) return false;
  const res = await call("save_details", pending.id, pending.name, pending.notes,
                         pending.tags, pending.group);
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
  $("set-exit-on-close").checked = s.exit_on_close !== false;
  $("set-motion").value = s.motion || "system";
  renderMotionNote();
  // Falling back to false matches storage.DEFAULT_SETTINGS: get_settings always
  // reports the resolved value, so an older settings file simply gets paging.
  $("set-infinite-scroll").checked = !!s.infinite_scroll;
  renderInfiniteNote();
  // Fallbacks only: get_settings always reports the resolved value, so these
  // match storage.DEFAULT_MAX_AVATAR_LOG / DEFAULT_MAX_PLAYER_CHANGES.
  $("set-max-avatar-log").value = s.max_avatar_log || 200;
  $("set-max-player-changes").value = s.max_player_changes || 1000;
  renderLimitsNote();
  // The blocklist has its own buttons rather than being saved with the rest of
  // the form, so it is read fresh every time the panel opens.
  await loadIgnores();
  renderIgnoreList();
  $("set-tray-note").textContent = s.tray
    ? "With this off, closing the window keeps the app in the notification area. " +
      "Right-click the tray icon for Open, Wear last avatar and Quit."
    : "The notification-area icon is unavailable, so closing the window always exits.";
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
  const res = await call("save_settings", $("set-send").value, $("set-recv").value,
                         $("set-exit-on-close").checked, $("set-motion").value,
                         $("set-max-avatar-log").value, $("set-max-player-changes").value,
                         $("set-infinite-scroll").checked);
  if (!res.ok) { showAlert("Invalid settings", res.message || "Could not save settings."); return; }
  closeModal("modal-settings");
  // Say so when rows were discarded, rather than letting the list quietly be
  // shorter than it was a moment ago.
  const dropped = (res.dropped_logs || 0) + (res.dropped_changes || 0);
  toast(dropped
    ? `Settings saved. Dropped ${dropped} older row${dropped !== 1 ? "s" : ""} to fit.`
    : "Settings saved.");
  await refreshState();
}

/* Explains what the two log caps will cost before the user commits to them. */
// Avatars the user has blocked from the log. Held as ids plus a display label
// each, so the Settings list can say something more useful than a UUID while
// still working for an avatar whose name was never fetched.
let ignores = { ids: [], names: {} };

async function loadIgnores() {
  const res = await call("get_ignores");
  if (!res || !res.ok) return;
  ignores = { ids: res.ids || [], names: res.names || {} };
}

async function ignoreAvatar(id, name) {
  const label = name || id;
  const yes = await showConfirm("Ignore avatar",
    `Stop logging "${label}"? Any rows it already has are removed, and it will not `
    + "appear in the log again. You can undo this from Settings > Ignored avatars.");
  if (!yes) return;
  const res = await call("add_ignore", id, label);
  if (!res || !res.ok) {
    if (res && res.message) showAlert("Could not ignore that avatar", res.message);
    return;
  }
  if (res.already) toast("That avatar was already ignored.");
  else if (res.removed) toast(`Ignoring ${label}. Removed ${res.removed} log row(s).`);
  else toast(`Ignoring ${label}. It will not be logged again.`);
  await refreshState();
}

async function unignoreAvatar(id) {
  const res = await call("remove_ignore", id);
  if (!res || !res.ok) {
    if (res && res.message) showAlert("Could not remove that avatar", res.message);
    return;
  }
  await refreshState();
  await loadIgnores();
  renderIgnoreList();
  toast("No longer ignoring this avatar. It may reappear in the log.");
}

function renderIgnoreList() {
  const box = $("ignore-list");
  if (!box) return;
  box.innerHTML = "";
  const ids = ignores.ids || [];
  if (!ids.length) {
    const empty = document.createElement("p");
    empty.className = "muted small";
    empty.textContent = "Nothing ignored yet.";
    box.appendChild(empty);
    if ($("ignore-note")) $("ignore-note").textContent = "";
    return;
  }
  for (const id of ids) {
    const row = document.createElement("div");
    row.className = "ignore-row";
    const name = document.createElement("span");
    name.className = "ignore-name";
    // A name is a best effort: an avatar blocked before it was ever fetched has
    // none, and the id is the only thing that identifies it.
    name.textContent = (ignores.names && ignores.names[id]) || "Unnamed avatar";
    name.title = id;
    const idEl = document.createElement("code");
    idEl.className = "muted small";
    idEl.textContent = id;
    const btn = document.createElement("button");
    btn.className = "btn small ghost";
    btn.textContent = "Remove";
    btn.addEventListener("click", () => unignoreAvatar(id));
    row.appendChild(name);
    row.appendChild(idEl);
    row.appendChild(btn);
    box.appendChild(row);
  }
  if ($("ignore-note")) {
    $("ignore-note").textContent =
      `${ids.length} ignored. Removed avatars are not logged again, but they can `
      + "come back if VRChat reports them and the block is removed.";
  }
}

function renderLimitsNote() {
  const el = $("set-limits-note");
  if (!el) return;
  const avatars = Number($("set-max-avatar-log").value);
  const changes = Number($("set-max-player-changes").value);
  const bad = (n) => !Number.isInteger(n) || n < 1 || n > 10000;
  if (bad(avatars) || bad(changes)) {
    el.textContent = "Each limit must be a whole number between 1 and 10000.";
    el.classList.add("warn-text");
    return;
  }
  const rows = state.logs ? state.logs.length : 0;
  const changesNow = state.changes ? state.changes.length : 0;
  const bits = [];
  if (rows > avatars) bits.push(`trimming ${rows - avatars} of ${rows} logged avatars`);
  if (changesNow > changes) {
    bits.push(`trimming ${changesNow - changes} of ${changesNow} player changes`);
  }
  el.textContent = bits.length
    ? `Saving now will drop the oldest rows: ${bits.join(", ")}.`
    : "Each list keeps its newest rows and discards the oldest past the limit. " +
      "The two limits are independent.";
  el.classList.toggle("warn-text", bits.length > 0);
}

/* Warns about the cost of continuous scrolling, using the user's own collection
   size rather than a vague caveat. The trade is real and asymmetric: paging
   holds 50 rows alive whatever the collection size, so the same setting that is
   pleasant at 200 avatars is what brings back the memory growth the pager
   exists to prevent. */
function renderInfiniteNote() {
  const el = $("set-infinite-note");
  if (!el) return;
  if (!$("set-infinite-scroll").checked) {
    el.textContent = "Off: the avatar grid shows 50 at a time with page buttons. "
      + "Only those rows are held in memory, however many avatars you have saved.";
    el.classList.remove("warn-text");
    return;
  }
  const total = state.entries ? state.entries.length : 0;
  // Thresholds are where the behaviour actually changes rather than round
  // numbers: under one screenful there is nothing to scroll, and past a few
  // hundred the grid stops being cheap to repaint on every poll.
  let advice;
  if (total <= 50) {
    advice = "Your collection fits in one page anyway, so this changes nothing yet.";
  } else if (total <= 300) {
    advice = "That should still feel quick.";
  } else {
    advice = "Expect scrolling and search to get slower, and to use more memory.";
  }
  el.textContent = `On: all ${total} saved avatar${total === 1 ? "" : "s"} are drawn at `
    + `once instead of 50 at a time. Every card stays in memory with its `
    + `thumbnail, and the whole grid repaints whenever the list changes. ${advice} `
    + "The log tabs stay paged either way.";
  el.classList.toggle("warn-text", total > 300);
}

/* Spells out *why* nothing is moving. "Follow Windows" silently doing nothing
   reads as a broken app, so name the actual cause and the fix. */
function renderMotionNote() {
  const el = $("set-motion-note");
  if (!el) return;
  const pref = $("set-motion").value;
  if (pref === "full") {
    el.textContent = "Animations always play, even if Windows has them turned off.";
    el.classList.remove("warn-text");
    return;
  }
  if (pref === "none") {
    el.textContent = "Transitions and hover motion are disabled.";
    el.classList.remove("warn-text");
    return;
  }
  if (systemPrefersReducedMotion()) {
    el.textContent =
      "Windows has \"Show animations\" turned off, so nothing moves. " +
      "Choose \"Always animate\" to override that, or turn animations back on in " +
      "Windows Settings > Accessibility > Visual effects.";
    el.classList.add("warn-text");
    return;
  }
  el.textContent = "Follows your Windows animation setting.";
  el.classList.remove("warn-text");
}

/* ------------------------------------------------------------------ files & updates */
function previewMotion() {
  // Preview live so the choice can be seen before saving.
  state.motion = $("set-motion").value;
  applyMotionPreference();
  renderMotionNote();
  animateView(1);
}

async function importVrchatFavourites() {
  const res = await call("import_vrchat_favourites", 100);
  if (!res || !res.ok) {
    if (res && res.title) showAlert(res.title, res.message);
    return;
  }
  // Three distinct outcomes, because "Imported 0" reads as a failure whether it
  // means "nothing new" or "the call came back empty".
  let message;
  if (!res.remote) {
    message = "VRChat returned no favourites. Log in again in Settings and retry.";
  } else if (res.added) {
    message = `Imported ${res.added} avatar${res.added === 1 ? "" : "s"} from VRChat.`;
  } else {
    message = `All ${res.already || res.remote} of your VRChat favourites are `
      + "already saved.";
  }
  toast(message, 5000);
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
  // Adopted on every poll rather than once at start-up: the loopback thumbnail
  // server can fail to bind, and a backend that comes up late still needs the UI
  // to notice.
  setThumbBase(next.thumb_base);
  if (next.motion && next.motion !== state.motion) state.motion = next.motion;
  // Same reasoning as motion: adopted every poll so an edit to settings.json made
  // outside the app takes effect. renderGrid() below repaints, because the flag
  // is part of the grid's signature.
  if (next.infinite_scroll != null) state.infinite_scroll = !!next.infinite_scroll;
  // Re-apply every poll so a change made outside the app is picked up.
  applyMotionPreference();
  renderJob(next.job);
  if (next.entries != null) state.entries = next.entries;
  if (next.logs != null) state.logs = next.logs;
  if (next.changes != null) state.changes = next.changes;
  lastRevs = next.revs;
  if (selectedId && !entryById(selectedId)) selectedId = null;
  render();
}

function startPolling() {
  // Set before the first paint so the correct motion rules apply immediately.
  applyMotionPreference();
  refreshState().then(() => {
    // Give the opening view the same entrance as any navigation into it.
    animateView(1);
    if (motionSuppressedBySystem) {
      toast("Windows has animations turned off — nothing will move. "
          + "Change this in Settings > Appearance.", 6000);
    }
  });
  setInterval(refreshState, 700);
}

/* ------------------------------------------------------------------ wiring */
/* ------------------------------------------------------------ view motion */
const VIEW_ORDER = ["home", "logs"];
const STAGGER_CAP = 14;

// Windows has a system-wide "Show animations in Windows" toggle, which WebView2
// reports as prefers-reduced-motion: reduce. That silently kills every animation
// in the app, which looks like a broken build rather than a setting. So the
// effective preference is resolved here and written onto <html>, and the
// stylesheet keys off that attribute instead of the media query alone.
const REDUCE_QUERY = "(prefers-reduced-motion: reduce)";

function systemPrefersReducedMotion() {
  try {
    return !!window.matchMedia && window.matchMedia(REDUCE_QUERY).matches;
  } catch (err) {
    return false;
  }
}

function applyMotionPreference() {
  const pref = state.motion || "full";
  const root = document.documentElement;
  root.setAttribute("data-motion", pref);
  const suppressed = pref === "system" && systemPrefersReducedMotion();
  motionSuppressedBySystem = suppressed;
  return suppressed;
}

let motionSuppressedBySystem = false;

// Restart a CSS animation that may already have run on this element.
function replayAnimation(el, className) {
  if (!el) return;
  el.classList.remove(className);
  void el.offsetWidth; // force reflow so re-adding restarts the animation
  el.classList.add(className);
}

// Clean up once the animation finishes, so the class cannot replay later and
// so a data refresh that rebuilds the children does not inherit a stale state.
// Guarded on e.target because the stagger children's animations also bubble.
function clearAnimationWhenDone(el, className) {
  if (!el) return;
  el.addEventListener("animationend", function done(e) {
    if (e.target !== el) return;
    el.classList.remove(className);
    el.removeEventListener("animationend", done);
  });
}

// Cap the index so a few hundred rows do not crawl in over several seconds.
function applyStagger(container) {
  if (!container) return;
  const items = container.children;
  if (!items.length) return;
  for (let i = 0; i < items.length; i++) {
    items[i].style.setProperty("--i", String(Math.min(i, STAGGER_CAP)));
  }
  replayAnimation(container, "stagger");
  clearAnimationWhenDone(container, "stagger");
}

// Slide the incoming panel in from the side the navigation is heading towards.
function animateView(direction) {
  const wrap = currentView === "logs" ? $("logs-wrap") : $("grid-wrap");
  // The logs view holds two lists; stagger whichever one is actually visible,
  // otherwise the players tab would animate the hidden avatar list.
  const list = currentView !== "logs"
    ? $("grid")
    : (logTab === "players" ? $("changes") : $("logs"));
  const entering = direction >= 0 ? "view-enter-next" : "view-enter-prev";

  // Clear both without adding either first. replayAnimation() adds as it goes,
  // so using it to clear would leave BOTH classes attached -- and because
  // view-enter-prev is declared last it would win, making every transition
  // animate backwards.
  wrap.classList.remove("view-enter-next", "view-enter-prev");
  void wrap.offsetWidth;
  wrap.classList.add(entering);
  clearAnimationWhenDone(wrap, entering);

  applyStagger(list);
  replayAnimation($("page-title"), "chrome-enter");
  clearAnimationWhenDone($("page-title"), "chrome-enter");
}

function setView(view) {
  const previous = currentView;
  if (previous === view) return;
  const from = VIEW_ORDER.indexOf(previous);
  const to = VIEW_ORDER.indexOf(view);
  const direction = from === -1 || to === -1 ? 1 : (to > from ? 1 : -1);
  currentView = view;
  renderHeader();
  if (view === "logs") renderLogs(true);
  else renderGrid(true);
  animateView(direction);
}

function wire() {
  // The drawer preview is not lazy, but it gets the same one-shot fallback: a
  // thumbnail the loopback server cannot find must still appear.
  $("preview").addEventListener("error", () => thumbLoadFailed($("preview")));
  $("bulk-clear").addEventListener("click", clearSelection);
  $("bulk-fav").addEventListener("click", () => runBulk("favorite"));
  $("bulk-wear").addEventListener("click", () => runBulk("wear"));
  $("bulk-refresh").addEventListener("click", () => runBulk("refresh"));
  $("bulk-delete").addEventListener("click", () => runBulk("delete"));
  $("bulk-tag").addEventListener("click", async () => {
    const value = await showPrompt("Add a tag",
      "Applied to every selected avatar. Separate several with commas.");
    if (value) await runBulk("tag", value);
  });
  $("bulk-untag").addEventListener("click", async () => {
    const value = await showPrompt("Remove a tag",
      "Removed from every selected avatar. Separate several with commas.");
    if (value) await runBulk("untag", value);
  });
  $("bulk-group").addEventListener("click", async () => {
    let value = await showGroupPicker("Move to group",
      "Applies to every selected avatar. Each avatar ends up in exactly one group.");
    if (value === NEW_GROUP) {
      value = await promptForGroupName("New group",
        "Name the group these avatars will appear under.");
    }
    // null means the user backed out; "" is a real choice, the ungrouped one.
    // Testing falsiness for both would make "No group" do nothing at all.
    if (value === null) return;
    await runBulk(value ? "group" : "ungroup", value);
  });
  $("undo-btn").addEventListener("click", doUndo);
  $("job-cancel").addEventListener("click", async () => {
    if (!activeJob || !activeJob.id) return;
    const res = await call("cancel_job", activeJob.id);
    toast(res && res.ok ? "Cancelling..." : "That job already finished.");
  });

  $("btn-add-current").addEventListener("click", addCurrent);
  $("btn-add-id").addEventListener("click", addByIdPrompt);
  $("rail-settings").addEventListener("click", openSettings);
  $("session-expired").addEventListener("click", openSettings);

  document.querySelectorAll(".rail-btn[data-view]").forEach((btn) => {
    btn.addEventListener("click", () => setView(btn.dataset.view));
  });
  document.querySelectorAll(".chip[data-filter]").forEach((chip) => {
    chip.addEventListener("click", () => {
      currentFilter = chip.dataset.filter;
      renderHeader();
      renderGrid();
    });
  });

  // Delegated, because the group chips are rebuilt whenever the entries change
  // and a per-chip listener would be dropped every time.
  $("group-chips").addEventListener("click", (e) => {
    const chip = e.target.closest ? e.target.closest(".chip[data-group]") : null;
    if (!chip) return;
    // Clicking the active group clears it, so there is always a way back to
    // every group without hunting for the All chip.
    currentGroup = currentGroup === chip.dataset.group ? null : chip.dataset.group;
    renderHeader();
    renderGrid();
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
      // Animate the incoming list, not just the surrounding panel.
      const list = logTab === "players" ? $("changes") : $("logs");
      applyStagger(list);
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
  $("d-group").addEventListener("change", async (e) => {
    if (e.target.value !== NEW_GROUP) { scheduleSave(); return; }
    // Freeze the drawer's in-progress draft first. renderDrawer() runs after
    // this and would otherwise rebuild the dropdown, knocking the prompt flow
    // out from under it.
    scheduleSave();
    const wasOpen = selectedId;
    const name = await promptForGroupName("New group",
      "Name the group this avatar will appear under.");
    // The drawer can be closed while the prompt is up, in which case there is
    // nothing left to set a group on.
    if (!selectedId || selectedId !== wasOpen) return;
    // Backing out of the name prompt must put the dropdown back where it was,
    // otherwise the entry sits on the sentinel and looks ungrouped until the
    // next redraw.
    const entry = entryById(selectedId);
    if (!name) {
      renderGroupDropdown(entry || {});
      return;
    }
    const key = groupKey(name);
    const match = groupSummary().groups.find((g) => g.key === key);
    // Reuse the existing option when the typed name already matches a group
    // case-insensitively, so a new spelling cannot fork "Furry" into two.
    const wanted = match ? match.key : name;
    // The typed name has to be added as a real option *before* it is selected.
    // Assigning a <select> a value it does not have selects nothing, so the
    // value silently became "" -- which captureDraft then read and saved as "No
    // group". That is why a brand new group came back blank and was never
    // created, while the prompt had accepted the name perfectly well.
    renderGroupDropdown(entry || {}, wanted);
    $("d-group").value = wanted;
    // Re-capture: the draft frozen above still holds the sentinel's null, so
    // flushing it as-is would save no group at all.
    scheduleSave();
    // A name with no existing option has to be saved before the refresh below,
    // or renderDrawer rebuilds the dropdown without it and the selection
    // visibly springs back until the debounce lands.
    await flushDraft(false);
    await refreshState();
  });
  $("d-notes").addEventListener("keydown", (e) => {
    if (e.key === "s" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); scheduleSave(); }
  });

  $("id-submit").addEventListener("click", addByIdSubmit);
  $("id-input").addEventListener("keydown", (e) => { if (e.key === "Enter") addByIdSubmit(); });

  $("set-login").addEventListener("click", doLogin);
  $("set-logout").addEventListener("click", doLogout);
  $("set-motion").addEventListener("change", previewMotion);
  // The note only; the grid itself is repainted by the next poll, which carries
  // the saved value. Previewing it here would mean the grid changing under the
  // user before they had committed to anything.
  $("set-infinite-scroll").addEventListener("change", renderInfiniteNote);
  $("set-method").addEventListener("change", updateTwoFactorLabel);
  $("set-max-avatar-log").addEventListener("input", renderLimitsNote);
  $("set-max-player-changes").addEventListener("input", renderLimitsNote);
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
