// Frontend regression tests. Run with:  node --test tests/
//
// These cover the bugs that were invisible from Python: the confirm dialog that
// never settled, autosave that lost edits, and a thumbnail cache that could be
// poisoned by an error object.

import test from "node:test";
import assert from "node:assert/strict";
import { makeEnvironment, loadApp } from "./domshim.mjs";

const tick = () => new Promise((r) => setTimeout(r, 0));

function setup(apiImpl = {}) {
  const env = makeEnvironment(apiImpl);
  const app = loadApp(env);
  return { env, app };
}

function click(el) {
  el.dispatch("click", { preventDefault() {}, stopPropagation() {} });
}

// --------------------------------------------------------------- showConfirm

test("showConfirm resolves true on Continue", async () => {
  const { env, app } = setup();
  const p = app.showConfirm("Delete", "Remove it?");
  click(env.document.getElementById("confirm-ok"));
  assert.equal(await p, true);
});

test("showConfirm resolves false on Cancel", async () => {
  // Regression: Cancel used to carry data-close, whose generic handler only hid
  // the modal, so this promise never settled and the awaiting action hung.
  const { env, app } = setup();
  const p = app.showConfirm("Delete", "Remove it?");
  click(env.document.getElementById("confirm-cancel"));
  assert.equal(await p, false);
});

test("showConfirm resolves false on backdrop click", async () => {
  const { env, app } = setup();
  const modal = env.document.getElementById("modal-confirm");
  const p = app.showConfirm("Delete", "Remove it?");
  modal.dispatch("click", { target: modal });
  assert.equal(await p, false);
});

test("showConfirm ignores clicks inside the card", async () => {
  const { env, app } = setup();
  const modal = env.document.getElementById("modal-confirm");
  const innerChild = { nodeType: 1 }; // a target that is not the backdrop
  const p = app.showConfirm("Delete", "Remove it?");
  modal.dispatch("click", { target: innerChild });
  // Still pending: a click inside the card must not settle it.
  let settled = false;
  p.then(() => { settled = true; });
  await tick();
  assert.equal(settled, false);
  click(env.document.getElementById("confirm-ok"));
  assert.equal(await p, true);
});

test("showConfirm resolves false on Escape", async () => {
  const { env, app } = setup();
  const p = app.showConfirm("Delete", "Remove it?");
  env.document.dispatch("keydown", { key: "Escape", stopPropagation() {}, preventDefault() {} });
  assert.equal(await p, false);
});

test("showConfirm removes every listener on settle", async () => {
  const { env, app } = setup();
  const ok = env.document.getElementById("confirm-ok");
  const cancel = env.document.getElementById("confirm-cancel");
  const modal = env.document.getElementById("modal-confirm");

  // Settle one by Cancel, then one by Continue; neither may leave handlers
  // behind, or repeated prompts would multiply their work.
  for (const settle of [cancel, ok]) {
    const p = app.showConfirm("Delete", "?");
    click(settle);
    assert.equal(await p, settle === ok);

    assert.equal(ok.listenerCount("click"), 0, "ok handler leaked");
    assert.equal(cancel.listenerCount("click"), 0, "cancel handler leaked");
    assert.equal(modal.listenerCount("click"), 0, "backdrop handler leaked");
    assert.equal(env.document.docListenerCount("keydown"), 0, "escape handler leaked");
  }
  assert.equal(app.pendingConfirm, null);
});

test("a second confirm settles the first", async () => {
  const { app } = setup();
  const first = app.showConfirm("One", "a");
  const second = app.showConfirm("Two", "b");
  assert.equal(await first, false, "the superseded confirm must not hang");
  assert.ok(app.pendingConfirm, "the new confirm is still pending");
  void second;
});

test("Cancel hides the modal", async () => {
  const { env, app } = setup();
  const p = app.showConfirm("Delete", "?");
  assert.equal(env.document.getElementById("modal-confirm").classList.contains("hidden"), false);
  click(env.document.getElementById("confirm-cancel"));
  await p;
  assert.equal(env.document.getElementById("modal-confirm").classList.contains("hidden"), true);
});

// ---------------------------------------------------------------- ensureThumb

test("ensureThumb ignores a non-string bridge response", async () => {
  // call() resolves to {ok:false} when the bridge throws. That object is truthy,
  // so it used to be cached and rendered as src="[object Object]".
  const api = { get_thumbnail: async () => ({ ok: false }) };
  const { app } = setup(api);
  const entry = { id: "avtr_1", thumb: "avtr_1.png" };

  const first = app.ensureThumb(entry);
  assert.equal(first, "", "must not return the error object");
  await tick();
  assert.equal(app.thumbCache["avtr_1"], undefined, "error object must not be cached");
  assert.equal(app.thumbPending["avtr_1"], false, "must be retryable");
  assert.equal(app.ensureThumb(entry), "", "still no bogus src");
});

test("ensureThumb ignores an empty response", async () => {
  const api = { get_thumbnail: async () => "" };
  const { app } = setup(api);
  app.ensureThumb({ id: "avtr_2", thumb: "a.png" });
  await tick();
  assert.equal(app.thumbCache["avtr_2"], undefined);
});

test("ensureThumb caches a real data uri", async () => {
  const api = { get_thumbnail: async () => "data:image/png;base64,AAAA" };
  const { app } = setup(api);
  app.ensureThumb({ id: "avtr_3", thumb: "a.png" });
  await tick();
  assert.equal(app.thumbCache["avtr_3"], "data:image/png;base64,AAAA");
  assert.equal(app.thumbKey["avtr_3"], "a.png");
  assert.equal(app.ensureThumb({ id: "avtr_3", thumb: "a.png" }), "data:image/png;base64,AAAA");
});

test("ensureThumb does nothing without a thumb name", async () => {
  let calls = 0;
  const api = { get_thumbnail: async () => { calls++; return "x"; } };
  const { app } = setup(api);
  app.ensureThumb({ id: "avtr_4", thumb: null });
  await tick();
  assert.equal(calls, 0);
});

test("thumbnail cache is capped and evicts oldest first", async () => {
  const total = 260;
  const api = { get_thumbnail: async (id) => "data:image/png;base64," + id };
  const { app } = setup(api);
  for (let i = 0; i < total; i++) {
    app.ensureThumb({ id: "avtr_" + i, thumb: "t" + i + ".png" });
  }
  await tick();

  const cached = Object.keys(app.thumbCache).filter((k) => app.thumbCache[k]);
  assert.ok(cached.length <= 200, `cache held ${cached.length}, expected <= 200`);
  assert.equal(app.thumbOrder.length, cached.length,
    "eviction order and cache must not drift apart");
  assert.ok(app.thumbCache["avtr_" + (total - 1)], "newest thumbnail was evicted");
  assert.equal(app.thumbCache["avtr_0"], undefined, "oldest thumbnail was not evicted");
  // Everything evicted must also have dropped its key, or it can never reload.
  assert.equal(app.thumbKey["avtr_0"], undefined, "evicted entry kept a stale thumbKey");
});

test("a cached thumbnail refreshes its recency", async () => {
  const api = { get_thumbnail: async (id) => "src-" + id };
  const { app } = setup(api);
  // Fill past the cap so the early entries are gone.
  for (let i = 0; i < 210; i++) app.ensureThumb({ id: "a" + i, thumb: "t" + i });
  await tick();

  // 210 insertions against a 200 cap evicts the first ten (a0..a9).
  assert.equal(app.thumbCache["a9"], undefined, "precondition: a9 should be evicted");
  assert.ok(app.thumbCache["a150"], "precondition: a150 should be cached");

  const survivor = "a150";
  for (let i = 0; i < 40; i++) app.ensureThumb({ id: survivor, thumb: "t150" });
  assert.equal(app.thumbOrder[app.thumbOrder.length - 1], survivor,
    "the touched entry should become most recent");
});

test("the fallback cache is capped by total size, not just by count", async () => {
  // A count cap alone is meaningless when thumbnails range from 10 KB to 4 MB:
  // 200 of the large ones is most of a gigabyte. Each response here is well over
  // the 24 MB budget once a handful have landed, so eviction has to kick in
  // before the count limit is reached.
  const chunk = "A".repeat(512 * 1024);
  const api = { get_thumbnail: async (id) => "data:image/png;base64," + id + chunk };
  const { app } = setup(api);
  for (let i = 0; i < 120; i++) app.ensureThumb({ id: "b" + i, thumb: "b" + i + ".png" });
  await tick();

  const cached = Object.keys(app.thumbCache).filter((k) => app.thumbCache[k]);
  const bytes = cached.reduce((n, k) => n + app.thumbCache[k].length, 0);
  assert.ok(cached.length < 120, `cache held every entry (${cached.length})`);
  assert.ok(bytes <= 24 * 1024 * 1024 + 512 * 1024,
    `cache held ${bytes} bytes, over the byte budget`);
  assert.ok(app.thumbCache["b119"], "the newest thumbnail was evicted");
  assert.equal(app.thumbCache["b0"], undefined, "the oldest was not evicted");
});

// ------------------------------------------------------- lazy thumbnails
//
// The memory fix. A decoded avatar thumbnail is about a megabyte and the
// webview keeps one for every image it has painted, so a list of a few thousand
// rows used to pin a few gigabytes. Only images near the viewport get a source.

test("with the loopback server, a thumbnail is a URL and never base64", async () => {
  let calls = 0;
  const api = { get_thumbnail: async (id) => { calls++; return "data:image/png;base64,X"; } };
  const { app } = setup(api);
  app.setThumbBase("http://127.0.0.1:51234/tok");

  const img = { dataset: {}, src: "", addEventListener() {} };
  app.lazyThumb(img, { id: "avtr_x", thumb: "avtr_x.png" }, "ph");

  // Registering an image must not fetch it at all.
  assert.equal(calls, 0, "registration fetched the image eagerly");
  assert.equal(img.dataset.thumbKey, "avtr_x.png");

  app.onThumbVisibility(img, true);
  assert.equal(img.src, "http://127.0.0.1:51234/tok/avtr_x.png", img.src);
  assert.equal(calls, 0, "showing an image fetched base64 anyway");
});

test("scrolling an image out of view drops its source", () => {
  const { app } = setup();
  app.setThumbBase("http://127.0.0.1:1/tok");
  const img = { dataset: {}, src: "", addEventListener() {} };
  app.lazyThumb(img, { id: "avtr_y", thumb: "y.png" }, "ph");

  app.onThumbVisibility(img, true);
  const loaded = img.src;
  assert.match(loaded, /\/tok\/y\.png$/);

  app.onThumbVisibility(img, false);
  assert.notEqual(img.src, loaded, "the real source must be released off-screen");
  assert.ok(img.src.startsWith("data:image/svg+xml"), img.src);

  app.onThumbVisibility(img, true);
  assert.equal(img.src, loaded, "and restored when it comes back");
});

test("a thumbnail name is escaped into the URL", () => {
  const { app } = setup();
  app.setThumbBase("http://127.0.0.1:1/tok");
  assert.equal(app.thumbUrlFor("a b&c.png"), "http://127.0.0.1:1/tok/a%20b%26c.png");
  assert.equal(app.thumbUrlFor(""), "");
});

test("without the loopback server, images fall back to base64 across the bridge", async () => {
  const api = { get_thumbnail: async (id) => "data:image/png;base64," + id };
  const { env, app } = setup(api);
  const img = env.document.registerThumbImage({ dataset: {}, src: "", addEventListener() {} });

  // No observer available in this environment, so lazyThumb loads eagerly: the
  // behaviour must be exactly what it was before the loopback server existed.
  app.lazyThumb(img, { id: "avtr_z", thumb: "z.png" }, "ph");
  // The bridge answers asynchronously, so the placeholder stands in until the
  // patch below lands -- which is how it behaved before this change too.
  assert.equal(img.src, "ph");
  await tick();
  assert.equal(img.src, "data:image/png;base64,avtr_z", img.src);
});

test("an avatar with no thumbnail shows the placeholder and asks for nothing", async () => {
  let calls = 0;
  const api = { get_thumbnail: async () => { calls++; return "x"; } };
  const { app } = setup(api);
  app.setThumbBase("http://127.0.0.1:1/tok");

  const img = { dataset: {}, src: "", addEventListener() {} };
  const shown = app.lazyThumb(img, { id: "avtr_w", thumb: null }, "ph");
  await tick();

  assert.equal(shown, "ph");
  assert.equal(img.src, "ph");
  assert.equal(calls, 0, "a thumbnail-less avatar must not be fetched");
});

test("switching to the loopback transport drops the base64 copies", async () => {
  const api = { get_thumbnail: async (id) => "data:image/png;base64," + id };
  const { app } = setup(api);
  for (let i = 0; i < 20; i++) app.ensureThumb({ id: "c" + i, thumb: "c" + i });
  await tick();
  assert.ok(Object.keys(app.thumbCache).length > 0, "precondition: something cached");

  app.setThumbBase("http://127.0.0.1:1/tok");
  assert.equal(Object.keys(app.thumbCache).length, 0,
    "base64 copies must not outlive the transport they were for");
  assert.equal(app.thumbOrder.length, 0, "eviction order must not drift either");
});

test("a loopback URL that will not load falls back to base64", async () => {
  const api = { get_thumbnail: async () => "data:image/png;base64,FALLBACK" };
  const { env, app } = setup(api);
  app.setThumbBase("http://127.0.0.1:1/tok");

  const img = env.document.registerThumbImage({ dataset: {}, src: "", addEventListener() {} });
  app.lazyThumb(img, { id: "avtr_f", thumb: "f.png" }, "ph");
  app.onThumbVisibility(img, true);
  assert.match(img.src, /127\.0\.0\.1/);

  app.thumbLoadFailed(img);
  await tick();
  assert.equal(img.src, "data:image/png;base64,FALLBACK", img.src);
});

test("a failed fallback is not retried for ever", async () => {
  // If the fallback also fails to load, the error handler runs again. Retrying
  // unconditionally would set src to the same value again and spin.
  let calls = 0;
  const api = { get_thumbnail: async () => { calls++; return "data:image/png;base64,X"; } };
  const { env, app } = setup(api);
  app.setThumbBase("http://127.0.0.1:1/tok");

  const img = env.document.registerThumbImage({ dataset: {}, src: "", addEventListener() {} });
  app.lazyThumb(img, { id: "avtr_g", thumb: "g.png" }, "ph");
  for (let i = 0; i < 5; i++) app.thumbLoadFailed(img);
  await tick();
  assert.equal(calls, 1, "the bridge was asked " + calls + " times");

  // A refreshed avatar gets a new filename, and is allowed to try again.
  app.thumbLoadFailed({ ...img, dataset: { thumbId: "avtr_g", thumbKey: "g2.png" } });
  await tick();
  assert.equal(calls, 2, "a new thumbnail must be retried");
});

// --------------------------------------------------------- ignored avatars

test("the blocklist is fetched and rendered in Settings", async () => {
  const api = {
    get_ignores: async () => ({
      ok: true,
      ids: ["avtr_a", "avtr_b"],
      names: { avtr_a: "Noisy One", avtr_b: "" },
    }),
  };
  const { env, app } = setup(api);
  await app.loadIgnores();
  app.renderIgnoreList();

  assert.deepEqual(app.ignores.ids, ["avtr_a", "avtr_b"]);
  const list = env.document.getElementById("ignore-list");
  assert.equal(list.children.length, 2);
  assert.equal(list.children[0].children[0].textContent, "Noisy One");
  // An avatar blocked before its name was ever fetched still needs a row.
  assert.equal(list.children[1].children[0].textContent, "Unnamed avatar");
  assert.match(env.document.getElementById("ignore-note").textContent, /2 ignored/);
});

test("an empty blocklist says so rather than showing nothing", async () => {
  const api = { get_ignores: async () => ({ ok: true, ids: [], names: {} }) };
  const { env, app } = setup(api);
  await app.loadIgnores();
  app.renderIgnoreList();

  const list = env.document.getElementById("ignore-list");
  assert.equal(list.children.length, 1);
  assert.equal(list.children[0].textContent, "Nothing ignored yet.");
});

test("ignoring confirms first, then blocks and reports what it removed", async () => {
  const calls = [];
  const api = {
    get_ignores: async () => ({ ok: true, ids: [], names: {} }),
    add_ignore: async (id, name) => {
      calls.push([id, name]);
      return { ok: true, id, removed: 3 };
    },
    get_state: async () => ({ revs: {}, logs: [], entries: [], changes: [] }),
  };
  const { env, app } = setup(api);
  const p = app.ignoreAvatar("avtr_c", "Noisy One");
  await tick();
  // Declining must send nothing: this is a destructive, one-way action.
  click(env.document.getElementById("confirm-cancel"));
  await p;
  assert.equal(calls.length, 0, "cancelling the confirmation still blocked the avatar");

  const q = app.ignoreAvatar("avtr_c", "Noisy One");
  await tick();
  click(env.document.getElementById("confirm-ok"));
  await q;
  assert.deepEqual(calls, [["avtr_c", "Noisy One"]]);
});

test("a refused block surfaces the reason instead of silently doing nothing", async () => {
  const api = {
    get_ignores: async () => ({ ok: true, ids: [], names: {} }),
    add_ignore: async () => ({ ok: false, message: "Ignore list is full (5000)." }),
    get_state: async () => ({ revs: {}, logs: [], entries: [], changes: [] }),
  };
  const { env, app } = setup(api);
  const p = app.ignoreAvatar("avtr_d", "Noisy");
  await tick();
  click(env.document.getElementById("confirm-ok"));
  await p;

  const alert = env.document.getElementById("modal-alert");
  assert.ok(!alert.classList.contains("hidden"),
    "a refusal must be shown, not swallowed");
  assert.match(env.document.getElementById("alert-message").textContent, /full/);
});

test("un-ignoring reloads the list so the row disappears", async () => {
  const removed = [];
  let ids = ["avtr_e"];
  const api = {
    get_ignores: async () => ({ ok: true, ids: [...ids], names: { avtr_e: "Was Noisy" } }),
    remove_ignore: async (id) => { removed.push(id); ids = []; return { ok: true, id }; },
    get_state: async () => ({ revs: {}, logs: [], entries: [], changes: [] }),
  };
  const { env, app } = setup(api);
  await app.loadIgnores();
  app.renderIgnoreList();

  const row = env.document.getElementById("ignore-list").children[0];
  const button = row.children[2];
  assert.equal(button.textContent, "Remove");
  await click(button);
  await tick();

  assert.deepEqual(removed, ["avtr_e"]);
  assert.equal(app.ignores.ids.length, 0, "the list must not still show the removed row");
});

test("both the log row and the favourite offer to ignore", async () => {
  const api = { get_ignores: async () => ({ ok: true, ids: [], names: {} }) };
  const { app } = setup(api);
  const entry = { id: "avtr_f", name: "Fave" };
  app.state = { ...app.state, entries: [entry] };

  // The log row's context menu, which is where a first sighting is dealt with.
  const logItems = await app.forgetLogMenuItems(
    { id: "avtr_g", name: "Logged", count: 1, last_seen: "2026-01-01T00:00:00+00:00" });
  const logLabels = logItems.filter((i) => !i.sep).map((i) => i.label);
  assert.ok(logLabels.includes("Never log this avatar"), logLabels.join(" | "));
  assert.ok(logLabels.includes("Remove from Log"), logLabels.join(" | "));

  // And a favourite, since an avatar you already saved is often exactly the one
  // that keeps showing up in the log.
  const cardLabels = app.cardMenuItems(entry).filter((i) => !i.sep).map((i) => i.label);
  assert.ok(cardLabels.includes("Never log this avatar"), cardLabels.join(" | "));
});

test("ignoring prefers the resolved name over the raw id", async () => {
  const api = { get_ignores: async () => ({ ok: true, ids: [], names: {} }) };
  const { app } = setup(api);
  const saved = { id: "avtr_h", name: "Real Name" };
  app.state = { ...app.state, entries: [saved] };
  const items = await app.forgetLogMenuItems({ id: "avtr_h", name: "", last_seen: "" });
  const item = items.find((i) => i.label === "Never log this avatar");
  // An unnamed log row must not be blocked under a blank label when the
  // favourite already knows the real name.
  assert.ok(item, "the ignore action is missing");
});

// ------------------------------------------------------- progressive lists

test("a page is 50 rows, so the lazy image loading works in small units", () => {
  // One page is one unit of thumbnail work: only drawn rows hold an image, so a
  // large page would mean a large spike of decoded bitmaps on the first paint.
  const { app } = setup();
  assert.equal(app.PAGE_ROWS, 50);
});

function withAvatars(app, count) {
  app.state = { ...app.state, entries: Array.from({ length: count }, (_, i) => ({
    id: "avtr_" + i,
    name: "Avatar " + i,
    tags: [],
    platforms: [],
    added: `2026-01-${String((i % 28) + 1).padStart(2, "0")}T00:00:00+00:00`,
  })) };
  app.currentFilter = "all";
  app.currentGroup = null;
}

// The rendered rows are cards; a pager is appended after them.
const gridRows = (env) => {
  const grid = env.document.getElementById("grid");
  const kids = grid.children;
  const at = kids.findIndex((c) => c.className === "pager");
  return { cards: at === -1 ? kids : kids.slice(0, at), pager: at === -1 ? null : kids[at] };
};

test("94 favourites show 50 rows and numbered pages 1 and 2", () => {
  // The reported case, verbatim: a real collection of 94 showed no pagination.
  const { env, app } = setup();
  withAvatars(app, 94);

  app.renderGrid(true);

  assert.equal(app.listTotal.grid, 94, "all 94 are known about");
  assert.equal(app.pageCount(94), 2, "94 rows is two pages of 50");
  assert.equal(app.currentPage.grid, 1, "opens on page 1");

  const { cards, pager } = gridRows(env);
  assert.equal(cards.length, 50, "only the first page's cards are drawn");
  assert.ok(pager, "page buttons must be drawn");
  assert.equal(pager.className, "pager");

  // nav > [<] [1] [2] [>]
  const labels = pager.children.map((c) => c.textContent);
  assert.deepEqual(labels, ["‹", "1", "2", "›"], pager.children.map((c) => c.textContent).join("|"));
  assert.ok(pager.children[0].disabled, "Previous is disabled on page 1");
  assert.equal(pager.children[2].disabled, false, "Next is available on page 1");
});

test("clicking page 2 shows the remaining 44", () => {
  const { env, app } = setup();
  withAvatars(app, 94);
  app.renderGrid(true);

  // Re-read after the click: the grid is rebuilt, so the old nodes are detached.
  gridRows(env).pager.children[2].dispatch("click",
    { preventDefault() {}, stopPropagation() {} });

  assert.equal(app.currentPage.grid, 2, "now on page 2");
  assert.equal(gridRows(env).cards.length, 44, "the tail is 44 rows, not a full page");

  // Page 2 of 2: a way back, but no way forward.
  const pager = gridRows(env).pager;
  assert.equal(pager.children[0].textContent, "‹");
  assert.equal(pager.children[0].disabled, false, "Previous is available on page 2");
  assert.equal(pager.children[3].textContent, "›");
  assert.equal(pager.children[3].disabled, true, "Next is disabled on the last page");
});

test("the page buttons mark which page you are on", () => {
  const { env, app } = setup();
  withAvatars(app, 94);
  app.renderGrid(true);

  let pager = gridRows(env).pager;
  assert.ok(pager.children[1].className.includes("active"), "page 1 is active");
  assert.ok(!pager.children[2].className.includes("active"), "page 2 is not");

  pager.children[2].dispatch("click", { preventDefault() {}, stopPropagation() {} });
  pager = gridRows(env).pager;
  assert.ok(pager.children[2].className.includes("active"), "page 2 is now active");
  assert.ok(!pager.children[1].className.includes("active"), "page 1 is not");
});

test("exactly 50 favourites gets no pager at all", () => {
  // One full page is not a partial one, so a lone "1" would be noise.
  const { env, app } = setup();
  withAvatars(app, 50);
  app.renderGrid(true);
  const { cards, pager } = gridRows(env);
  assert.equal(cards.length, 50);
  assert.equal(pager, null);
  assert.equal(app.renderPager("grid"), null);
});

test("a long list caps the number of page buttons", () => {
  // 4,000 rows is 80 pages. Rendering a button per page would be worse than the
  // problem it solves.
  const { env, app } = setup();
  withAvatars(app, 4000);
  app.renderGrid(true);

  const pager = gridRows(env).pager;
  assert.ok(pager, "a list this long must be paginated");
  // 80 pages must not become 80 buttons: prev + a 5-page window + a gap on each
  // side + the last page + next.
  assert.equal(pager.children.length, 9,
    `pager drew ${pager.children.length} controls: `
    + pager.children.map((c) => c.textContent).join(""));
  const labels = pager.children.map((c) => c.textContent);
  assert.deepEqual(labels, ["‹", "1", "2", "3", "4", "5", "…", "80", "›"],
    labels.join(""));
  // One gap is enough here: the window starts at page 1, so nothing is hidden
  // before it. A gap appears on both sides once the window is in the middle.
  assert.equal(labels.filter((l) => l === "…").length, 1);
});

test("the pager window follows the current page", () => {
  const { env, app } = setup();
  withAvatars(app, 4000);          // 80 pages
  app.renderGrid(true);

  // Jump deep into the list, then check the window moved with it rather than
  // staying pinned to page 1 for the whole way down.
  app.goToPage("grid", 40);
  const pager = gridRows(env).pager;
  const labels = pager.children.map((c) => c.textContent);
  assert.ok(labels.includes("40"), labels.join("|"));
  // Page 1 is still offered as a jump target, but no longer as one of the
  // consecutive numbers: the window is centred on 40.
  assert.ok(labels.includes("1"), "the first page stays reachable");
  assert.deepEqual(
    labels.filter((l) => /^\d+$/.test(l) && l !== "1" && l !== "80"),
    ["38", "39", "40", "41", "42"],
    labels.join("|"));
  assert.equal(gridRows(env).cards.length, 50);
});

test("the last page is always reachable", () => {
  const { env, app } = setup();
  withAvatars(app, 1234);          // 25 pages
  app.renderGrid(true);

  app.goToPage("grid", 25);
  const labels = gridRows(env).pager.children.map((c) => c.textContent);
  assert.ok(labels.includes("25"), labels.join("|"));
  assert.ok(labels.includes("1"), "the first page stays reachable");
  assert.equal(gridRows(env).cards.length, 1234 - 24 * 50, "the tail only");
});

test("a list that shrinks clamps the page instead of stranding you", () => {
  // Searching can cut 94 rows down to 3 while the user sits on page 2. A pager
  // still offering page 2 would be a dead end.
  const { env, app } = setup();
  withAvatars(app, 94);
  app.renderGrid(true);
  app.goToPage("grid", 2);
  assert.equal(app.currentPage.grid, 2);

  env.document.getElementById("search").value = "Avatar 1";
  app.renderGrid(true);
  assert.equal(app.currentPage.grid, 1, "the page is pulled back into range");
  assert.ok(gridRows(env).cards.length <= 50);
});

test("changing the filter starts again at page 1", () => {
  const { env, app } = setup();
  withAvatars(app, 94);
  app.renderGrid(true);
  app.goToPage("grid", 2);
  assert.equal(app.currentPage.grid, 2);

  app.currentGroup = "mech";
  app.renderGrid(true);
  assert.equal(app.currentPage.grid, 1, "a new filter is a new list");
});

test("a data refresh does not throw you back to page 1", () => {
  // New metadata arrives every few seconds from the poll. Resetting the page on
  // each one would make page 2 impossible to stay on.
  const { app } = setup();
  withAvatars(app, 94);
  app.renderGrid(true);
  app.goToPage("grid", 2);

  app.renderGrid(true);
  assert.equal(app.currentPage.grid, 2, "stayed on page 2 across a re-render");
});

test("the log list paginates too, and says which page", () => {
  const { env, app } = setup();
  app.state = { ...app.state, logs: Array.from({ length: 120 }, (_, i) => ({
    id: "avtr_log" + i, name: "Logged " + i, count: 1,
    last_seen: "2026-01-01T00:00:00+00:00", private: false, source: "log",
  })) };
  app.currentView = "logs";
  app.logTab = "avatars";

  app.renderLogs(true);
  const logs = env.document.getElementById("logs");
  assert.equal(logs.children.length, 51, "50 rows plus the pager");
  assert.match(env.document.getElementById("logs-count").textContent, /page 1 of 3/);
});

// ----------------------------------------------------- infinite scroll option

test("the grid is paged until the setting is turned on", () => {
  // The default has to stay paged: paging is what keeps the DOM small, and a
  // first-run user with thousands of favourites should not get that all at once.
  const { env, app } = setup();
  withAvatars(app, 4000);
  app.renderGrid(true);

  assert.equal(app.state.infinite_scroll, false, "off by default");
  const { cards, pager } = gridRows(env);
  assert.equal(cards.length, 50);
  assert.ok(pager, "paging is still in force");
});

test("infinite scroll draws the whole grid and no pager", () => {
  const { env, app } = setup();
  withAvatars(app, 4000);
  app.state = { ...app.state, infinite_scroll: true };

  app.renderGrid(true);

  assert.equal(app.infiniteGrid(), true);
  const { cards, pager } = gridRows(env);
  assert.equal(cards.length, 4000, "every avatar is drawn, not one page");
  assert.equal(pager, null, "there is nothing to page to");
  assert.equal(app.pagerFor("grid"), null);
});

test("the status bar drops the page number under infinite scroll", () => {
  // Claiming "page 1 of 80" would be a dead end: there is no way to reach 80.
  const { env, app } = setup();
  withAvatars(app, 4000);
  app.renderGrid(true);
  assert.match(env.document.getElementById("count").textContent, /page 1 of 80/);

  app.state = { ...app.state, infinite_scroll: true };
  app.renderGrid(true);
  const text = env.document.getElementById("count").textContent;
  assert.equal(text, "4000 avatars", text);
});

test("infinite scroll still respects the filter and the search", () => {
  // Drawing everything is not a licence to draw everything regardless of the
  // filter, which is the whole point of visibleEntries.
  const { env, app } = setup();
  withAvatars(app, 4000);
  app.state = { ...app.state, infinite_scroll: true };
  env.document.getElementById("search").value = "Avatar 1";
  app.renderGrid(true);

  // "Avatar 1" also matches 10-19, 100-199, ... so this is deliberately loose:
  // what matters is that it is far short of the full 4,000.
  const drawn = gridRows(env).cards.length;
  assert.ok(drawn > 0 && drawn < 4000, `drew ${drawn} rows`);
});

test("the log tabs stay paged whatever the grid is doing", () => {
  // The log lists are bounded by the log limits and re-sorted constantly, so
  // letting them grow unbounded is a cost with nothing in return.
  const { env, app } = setup();
  app.state = { ...app.state, infinite_scroll: true, logs: Array.from({ length: 120 }, (_, i) => ({
    id: "avtr_log" + i, name: "Logged " + i, count: 1,
    last_seen: "2026-01-01T00:00:00+00:00", private: false, source: "log",
  })) };
  app.currentView = "logs";
  app.logTab = "avatars";

  app.renderLogs(true);

  const logs = env.document.getElementById("logs");
  assert.equal(logs.children.length, 51, "50 rows plus the pager, as before");
  assert.equal(app.pagerFor("logs").className, "pager");
  assert.match(env.document.getElementById("logs-count").textContent, /page 1 of 3/);
});

test("jumping to a page while infinite scroll is on does nothing", () => {
  // Nothing renders a page button in this mode, so this is the belt-and-braces
  // path: a stale control must not be able to slice the list or scroll the user
  // away from where they are reading.
  const { env, app } = setup();
  withAvatars(app, 4000);
  app.renderGrid(true);

  app.state = { ...app.state, infinite_scroll: true };
  app.renderGrid(true);
  const before = env.document.getElementById("grid-wrap").scrollTop;

  app.goToPage("grid", 40);

  assert.equal(app.currentPage.grid, 1, "unchanged: still page 1");
  assert.equal(gridRows(env).cards.length, 4000, "the grid is not re-sliced");
  assert.equal(env.document.getElementById("grid-wrap").scrollTop, before,
    "and the scroll position is left alone");
});

test("switching back to paging restores the pager where the user was", () => {
  const { env, app } = setup();
  withAvatars(app, 4000);
  app.renderGrid(true);
  app.goToPage("grid", 40);

  // The page number is preserved across the round trip on purpose, so toggling
  // the setting on and off again is not a jump back to the top of a long list.
  app.state = { ...app.state, infinite_scroll: true };
  app.renderGrid(true);
  assert.equal(gridRows(env).cards.length, 4000);
  assert.equal(gridRows(env).pager, null);

  app.state = { ...app.state, infinite_scroll: false };
  app.renderGrid(true);
  const { cards, pager } = gridRows(env);
  assert.equal(cards.length, 50, "back to one page");
  assert.ok(pager, "the pager comes back");
  assert.equal(app.currentPage.grid, 40, "and returns to the page it was on");
  assert.match(env.document.getElementById("count").textContent, /page 40 of 80/);
});

test("resuming paging after the list shrank lands on a real page", () => {
  // The preserved page number must not survive as a dead end. Here the search
  // both shrinks the list and resets the key, so this mostly re-checks existing
  // clamp behaviour -- which is the point: turning the setting off must not
  // introduce a page the user cannot get back from.
  const { env, app } = setup();
  withAvatars(app, 4000);
  app.renderGrid(true);
  app.goToPage("grid", 40);
  app.state = { ...app.state, infinite_scroll: true };

  env.document.getElementById("search").value = "Avatar 1";
  app.renderGrid(true);
  app.state = { ...app.state, infinite_scroll: false };
  app.renderGrid(true);

  const pages = app.pageCount(app.listTotal.grid);
  assert.ok(app.currentPage.grid >= 1 && app.currentPage.grid <= pages,
    `page ${app.currentPage.grid} of ${pages}`);
  assert.ok(gridRows(env).cards.length > 0, "something is drawn");
  assert.ok(gridRows(env).pager, "and the pager still offers a way back");
});

test("the settings note warns about the cost, and says how big the list is", () => {
  const { env, app } = setup();
  withAvatars(app, 4000);
  const note = env.document.getElementById("set-infinite-note");

  env.document.getElementById("set-infinite-scroll").checked = false;
  app.renderInfiniteNote();
  assert.match(note.textContent, /Off/);
  assert.ok(!note.classList.contains("warn-text"), "off is not a warning");

  env.document.getElementById("set-infinite-scroll").checked = true;
  app.renderInfiniteNote();
  assert.match(note.textContent, /4000 saved avatars/, note.textContent);
  assert.match(note.textContent, /stays in memory/, note.textContent);
  assert.match(note.textContent, /log tabs stay paged/, note.textContent);
  assert.ok(note.classList.contains("warn-text"),
    "a 4,000-avatar grid has to be flagged as expensive");
});

test("the note is calm about a small collection", () => {
  // Warning about everything trains people to ignore warnings, and a 12-avatar
  // grid is not a performance problem.
  const { env, app } = setup();
  withAvatars(app, 12);
  env.document.getElementById("set-infinite-scroll").checked = true;
  app.renderInfiniteNote();
  const note = env.document.getElementById("set-infinite-note");
  assert.ok(!note.classList.contains("warn-text"), note.textContent);
});

// ------------------------------------------------------------------- autosave

test("closing the drawer flushes a pending edit", async () => {
  // Regression: closing within the debounce window discarded the edit.
  const saved = [];
  const api = {
    save_details: async (id, name, notes, tags) => {
      saved.push({ id, name, notes, tags });
      return { ok: true };
    },
  };
  const { env, app } = setup(api);
  app.selectedId = "avtr_5";
  env.document.getElementById("d-name").value = "Edited Name";
  env.document.getElementById("d-notes").value = "my note";

  app.captureDraft();
  await app.closeDrawer();

  assert.equal(saved.length, 1);
  assert.equal(saved[0].id, "avtr_5");
  assert.equal(saved[0].name, "Edited Name");
  assert.equal(saved[0].notes, "my note");
});

test("switching drawers saves the previous avatar, not the new one", async () => {
  // Regression: the debounce read selectedId at fire time, so a fast switch
  // wrote the new avatar's values onto the previously selected one.
  const saved = [];
  const api = {
    save_details: async (id, name, notes, tags) => {
      saved.push({ id, name, notes });
      return { ok: true };
    },
  };
  const { env, app } = setup(api);
  // renderDrawer closes the drawer for an id that is not in state.entries, so
  // the fixture needs both avatars present.
  app.state = {
    ...app.state,
    entries: [
      { id: "avtr_first", name: "First", tags: [] },
      { id: "avtr_second", name: "Second", tags: [] },
    ],
  };
  app.selectedId = "avtr_first";
  env.document.getElementById("d-notes").value = "belongs to first";
  app.captureDraft();

  env.document.getElementById("d-notes").value = "belongs to second";
  await app.openDrawer("avtr_second");

  assert.equal(saved.length, 1);
  assert.equal(saved[0].id, "avtr_first");
  assert.equal(saved[0].notes, "belongs to first");
  assert.equal(app.selectedId, "avtr_second", "the new avatar must stay selected");
  assert.equal(env.document.getElementById("drawer").classList.contains("hidden"), false);
});

test("opening a drawer does not re-save when there is no draft", async () => {
  const saved = [];
  const api = { save_details: async (...a) => { saved.push(a); return { ok: true }; } };
  const { app } = setup(api);
  await app.openDrawer("avtr_six");
  assert.equal(saved.length, 0);
});

test("flushDraft is a no-op with nothing pending", async () => {
  const api = { save_details: async () => { throw new Error("should not be called"); } };
  const { app } = setup(api);
  assert.equal(await app.flushDraft(false), false);
});

// --------------------------------------------------------------------- misc

// ------------------------------------------------------------- view motion

test("animateView slides in the direction of travel", async () => {
  const { env, app } = setup();
  const { setView, animateView, VIEW_ORDER } = app;
  const gridWrap = env.document.getElementById("grid-wrap");
  const logsWrap = env.document.getElementById("logs-wrap");

  // Moving down the rail (home -> logs) slides in from the right.
  app.currentView = "home";
  animateView(1);
  assert.ok(gridWrap.classList.contains("view-enter-next"),
    "forward navigation should use view-enter-next");
  assert.ok(!gridWrap.classList.contains("view-enter-prev"),
    "only one direction may be active at a time");

  // Moving back up slides in from the left.
  app.currentView = "logs";
  animateView(-1);
  assert.ok(logsWrap.classList.contains("view-enter-prev"),
    "backward navigation should use view-enter-prev");
  assert.ok(!logsWrap.classList.contains("view-enter-next"),
    "the previous direction must not linger");

  assert.deepEqual(VIEW_ORDER, ["home", "logs"],
    "nav order drives the slide direction");
});

test("animateView clears its class when the animation ends", () => {
  const { env, app } = setup();
  const wrap = env.document.getElementById("grid-wrap");
  app.currentView = "home";
  app.animateView(1);
  assert.ok(wrap.classList.contains("view-enter-next"));

  // Bubbling animations from children must not strip the parent's class.
  wrap.dispatch("animationend", { target: env.document.getElementById("grid") });
  assert.ok(wrap.classList.contains("view-enter-next"),
    "a child's animationend must not clear the parent class");

  wrap.dispatch("animationend", { target: wrap });
  assert.ok(!wrap.classList.contains("view-enter-next"),
    "the class must be removed once its own animation ends");
});

test("setView ignores a switch to the current view", () => {
  const { app } = setup();
  app.currentView = "home";
  app.setView("home");
  // No animation should have been started on an unchanged view.
  assert.equal(app.pendingConfirm, null);
});

test("stagger caps the index so long lists do not crawl", () => {
  const { env, app } = setup();
  const list = env.document.getElementById("grid");
  // Stand in for a few hundred drawn rows. Well above one page of 50, so this
  // is the list the cap exists for.
  list.children = Array.from({ length: 400 }, () => ({
    style: { setProperty() {} },
  }));
  app.applyStagger(list);
  assert.equal(app.STAGGER_CAP, 14,
    "the cap is what keeps a long list from taking seconds");
});

test("revealOnce only animates on the hidden edge", () => {
  const { env, app } = setup();
  const bar = env.document.getElementById("bulk-bar");
  bar.classList.remove("hidden");
  app.revealOnce(bar);
  assert.ok(!bar.classList.contains("bar-enter"),
    "an already-visible bar must not re-animate on every poll");

  bar.classList.add("hidden");
  app.revealOnce(bar);
  assert.ok(bar.classList.contains("bar-enter"),
    "becoming visible should animate once");
});

// -------------------------------------------------------------- motion mode

test("applyMotionPreference writes the preference onto <html>", () => {
  const { env, app } = setup();
  app.state.motion = "full";
  app.applyMotionPreference();
  assert.equal(env.root.attributes["data-motion"], "full");

  app.state.motion = "none";
  app.applyMotionPreference();
  assert.equal(env.root.attributes["data-motion"], "none");

  app.state.motion = "system";
  app.applyMotionPreference();
  assert.equal(env.root.attributes["data-motion"], "system");
});

test("a system that reduces motion is detected", () => {
  // Windows "Show animations" off reaches WebView2 as prefers-reduced-motion.
  const off = makeEnvironment({}, { systemReducesMotion: true });
  const on = makeEnvironment({}, { systemReducesMotion: false });

  assert.equal(loadApp(off).systemPrefersReducedMotion(), true);
  assert.equal(loadApp(on).systemPrefersReducedMotion(), false);
});

test("system-reduced motion is flagged only in system mode", () => {
  // This is the exact case that made the animations look broken: Windows
  // suppresses motion, and "Follow Windows" then does nothing at all.
  const env = makeEnvironment({}, { systemReducesMotion: true });
  const app = loadApp(env);

  app.state.motion = "system";
  assert.equal(app.applyMotionPreference(), true,
    "system mode should report that motion is being suppressed");

  app.state.motion = "full";
  assert.equal(app.applyMotionPreference(), false,
    "an explicit override must not report suppression");
});

test("motion defaults to full so it works regardless of the OS toggle", () => {
  // Windows uses one "Show animations" switch for accessibility and for plain
  // performance tuning. Honouring it by default left the app looking broken.
  const off = makeEnvironment({}, { systemReducesMotion: true });
  const on = makeEnvironment({}, { systemReducesMotion: false });

  for (const env of [off, on]) {
    const app = loadApp(env);
    assert.equal(app.state.motion, "full",
      "the default must animate even when Windows suppresses motion");
    // Default mode must never report suppression, so no nagging toast.
    assert.equal(app.applyMotionPreference(), false);
  }
});

test("the motion note explains the cause", () => {
  const env = makeEnvironment({}, { systemReducesMotion: true });
  const app = loadApp(env);
  const note = env.document.getElementById("set-motion-note");
  const select = env.document.getElementById("set-motion");

  select.value = "system";
  app.state.motion = "system";
  app.renderMotionNote();
  assert.match(note.textContent, /Show animations/i,
    "the note must name the actual Windows setting");
  assert.match(note.textContent, /Always animate/i,
    "and say how to override it");

  select.value = "full";
  app.renderMotionNote();
  assert.match(note.textContent, /always play/i);
  assert.ok(!note.classList.contains("warn-text"));
});

test("escapeHtml escapes angle brackets and quotes", () => {
  const { app } = setup();
  assert.equal(app.escapeHtml('<img src=x onerror="a">'),
    "&lt;img src=x onerror=&quot;a&quot;&gt;");
  assert.equal(app.escapeHtml(null), "");
});

test("cardSub prefers author, then platforms, then tags, then id", () => {
  const { app } = setup();
  assert.equal(app.cardSub({ id: "x", author: "Someone" }), "Someone");
  assert.equal(app.cardSub({ id: "x", platforms: ["PC", "Quest"] }), "PC · Quest");
  assert.equal(app.cardSub({ id: "x", tags: ["a", "b"] }), "#a  #b");
  assert.equal(app.cardSub({ id: "avtr_x" }), "avtr_x");
});

// ---------------------------------------------------------------------------
// styled prompt
// ---------------------------------------------------------------------------

test("showPrompt resolves with the trimmed value", async () => {
  // Replaces window.prompt, which rendered as a separate browser window titled
  // "127.0.0.1:23017 says" and looked like a download warning.
  const env = makeEnvironment();
  const app = loadApp(env);
  const modal = env.document.getElementById("modal-prompt");
  const input = env.document.getElementById("prompt-input");

  const pending = app.showPrompt("Add a tag", "Applied to every selected avatar.");
  assert.ok(!modal.classList.contains("hidden"), "the styled modal must open");
  assert.equal(env.document.getElementById("prompt-title").textContent, "Add a tag");
  assert.equal(env.document.getElementById("prompt-message").textContent,
    "Applied to every selected avatar.");
  assert.equal(input.value, "", "the field must start empty, not stale");

  input.value = "  catboy  ";
  click(env.document.getElementById("prompt-ok"));
  assert.equal(await pending, "catboy", "surrounding whitespace is trimmed");
  assert.ok(modal.classList.contains("hidden"), "and the modal closes again");
});

test("showPrompt seeds the field with an initial value", async () => {
  const env = makeEnvironment();
  const app = loadApp(env);
  const input = env.document.getElementById("prompt-input");

  const pending = app.showPrompt("Remove a tag", "From the selection.", "favourite");
  assert.equal(input.value, "favourite");
  click(env.document.getElementById("prompt-ok"));
  assert.equal(await pending, "favourite");
});

test("showPrompt resolves null on cancel, backdrop and Escape", async () => {
  const env = makeEnvironment();
  const app = loadApp(env);
  const modal = env.document.getElementById("modal-prompt");
  const input = env.document.getElementById("prompt-input");

  let pending = app.showPrompt("t", "m");
  input.value = "typed";
  click(env.document.getElementById("prompt-cancel"));
  assert.equal(await pending, null);

  pending = app.showPrompt("t", "m");
  input.value = "typed";
  modal.dispatch("click", { target: modal });
  assert.equal(await pending, null, "backdrop click cancels");

  pending = app.showPrompt("t", "m");
  input.value = "typed";
  env.document.dispatch("keydown",
    { key: "Escape", stopPropagation() {}, preventDefault() {} });
  assert.equal(await pending, null, "Escape cancels");
});

test("showPrompt submits on Enter", async () => {
  const env = makeEnvironment();
  const app = loadApp(env);
  const input = env.document.getElementById("prompt-input");

  const pending = app.showPrompt("t", "m");
  input.value = "quicktag";
  input.dispatch("keydown", { key: "Enter", preventDefault() {} });
  assert.equal(await pending, "quicktag",
    "Enter should submit, which a lone text field otherwise will not");
});

test("showPrompt treats a blank value as a cancel", async () => {
  // An empty tag would silently do nothing, so it is the same as declining.
  const env = makeEnvironment();
  const app = loadApp(env);
  const input = env.document.getElementById("prompt-input");

  const pending = app.showPrompt("t", "m");
  input.value = "   ";
  click(env.document.getElementById("prompt-ok"));
  assert.equal(await pending, null);
});

test("showPrompt settles an earlier prompt rather than orphaning it", async () => {
  // Same guard showConfirm has: two awaits must never both be left waiting.
  const env = makeEnvironment();
  const app = loadApp(env);
  const input = env.document.getElementById("prompt-input");

  const first = app.showPrompt("t", "m");
  input.value = "first";
  const second = app.showPrompt("t", "m");
  assert.equal(await first, null, "the first is settled as cancelled");

  input.value = "second";
  click(env.document.getElementById("prompt-ok"));
  assert.equal(await second, "second");
});

// ---------------------------------------------------------------------------
// discovery line
// ---------------------------------------------------------------------------

test("discovery states how many default avatars are ignored", () => {
  // Robot, Unity-chan and the rest are filtered out silently, which is
  // indistinguishable from a broken source unless the UI says so.
  const env = makeEnvironment();
  const app = loadApp(env);
  const el = env.document.getElementById("discovery-state");

  app.state = {
    ...app.state,
    discovery: {
      sources: { "cache-db": "ok", amplitude: "empty", log: "ok" },
      backlog: 22670,
      db_path: "C:/somewhere/avatars.sqlite",
      defaults: 257,
    },
  };
  app.renderDiscovery();

  assert.match(el.textContent, /local cache/);
  assert.match(el.textContent, /ids seen all-time/);
  assert.match(el.textContent, /257 default avatars ignored/);
  // A healthy source must not be painted as a warning.
  assert.ok(!el.classList.contains("warn-text"));
});

test("discovery omits the defaults count when the backend sends none", () => {
  // Guards against a stale build: an older backend has no `defaults` key, and
  // rendering "undefined default avatars ignored" would look like a bug.
  const env = makeEnvironment();
  const app = loadApp(env);
  const el = env.document.getElementById("discovery-state");

  app.state = {
    ...app.state,
    discovery: { sources: { "cache-db": "ok" }, backlog: 0, db_path: "" },
  };
  app.renderDiscovery();

  assert.match(el.textContent, /local cache/);
  assert.ok(!/default avatars/.test(el.textContent), el.textContent);
});

test("discovery still flags a source that is unavailable", () => {
  const env = makeEnvironment();
  const app = loadApp(env);
  const el = env.document.getElementById("discovery-state");

  app.state = {
    ...app.state,
    discovery: {
      sources: { "cache-db": "ok", amplitude: "missing", log: "ok" },
      backlog: 0,
      db_path: "",
      defaults: 257,
    },
  };
  app.renderDiscovery();

  assert.match(el.textContent, /live feed unavailable/);
  assert.match(el.textContent, /257 default avatars ignored/);
  assert.ok(el.classList.contains("warn-text"),
    "an unavailable source must still be highlighted");
});

test("a caught-up source is not labelled 'idle'", () => {
  // Regression: both of these statuses are the healthy steady state. The cache
  // database is simply caught up, and the live feed is empty because VRChat
  // uploads and clears it on every world switch. Calling that "idle" made a
  // working source look like a fault.
  const env = makeEnvironment();
  const app = loadApp(env);
  const el = env.document.getElementById("discovery-state");

  app.state = {
    ...app.state,
    discovery: {
      sources: { "cache-db": "empty", amplitude: "empty", log: "ok" },
      backlog: 22673,
      db_path: "C:/x/avatars.sqlite",
      defaults: 257,
    },
  };
  app.renderDiscovery();

  assert.ok(!/idle/i.test(el.textContent), el.textContent);
  assert.match(el.textContent, /local cache \(up to date\)/);
  assert.match(el.textContent, /live feed \(waiting for a world switch\)/);
  assert.match(el.textContent, /VRChat log/);
  assert.ok(!el.classList.contains("warn-text"),
    "a caught-up source is not a fault, so no warning styling");
  assert.match(el.title, /no avatar has[\s\S]*been cached/i,
    "the tooltip must explain what 'up to date' means");
});

test("a working source carries no parenthetical", () => {
  const env = makeEnvironment();
  const app = loadApp(env);
  const el = env.document.getElementById("discovery-state");

  app.state = {
    ...app.state,
    discovery: { sources: { "cache-db": "ok", log: "ok" }, backlog: 5, db_path: "", defaults: 0 },
  };
  app.renderDiscovery();
  assert.match(el.textContent, /local cache · VRChat log/);
  assert.ok(!/\(/.test(el.textContent), el.textContent);
});

test("a missing local-cache database says VRC-LOG is the prerequisite", () => {
  // avatars.sqlite is not created by VRChat, so "unavailable" with no
  // explanation sends people hunting through folders for a file that was never
  // written. The fix has to be named.
  const env = makeEnvironment();
  const app = loadApp(env);
  const el = env.document.getElementById("discovery-state");

  app.state = {
    ...app.state,
    discovery: {
      sources: { "cache-db": "missing", amplitude: "missing", log: "ok" },
      backlog: 0,
      db_path: "C:/nowhere/avatars.sqlite",
      defaults: 257,
    },
  };
  app.renderDiscovery();

  assert.match(el.textContent, /local cache unavailable/);
  assert.match(el.textContent, /VRC-LOG/, "must name the prerequisite");
  assert.match(el.title, /VRChat does not create this file/);
  assert.match(el.title, /C:\/nowhere\/avatars\.sqlite/,
    "the tooltip must still name the path that was checked");
  // Not every failure means "not installed".
  assert.ok(!/locked/.test(el.textContent));
});

test("other local-cache failures get their own remedy", () => {
  const env = makeEnvironment();
  const app = loadApp(env);
  const el = env.document.getElementById("discovery-state");

  const cases = [
    ["locked", /close VRChat/],
    ["unreadable", /permissions/],
    ["unsupported", /not a readable SQLite/],
  ];
  for (const [status, pattern] of cases) {
    app.state = {
      ...app.state,
      discovery: { sources: { "cache-db": status }, backlog: 0, db_path: "", defaults: 0 },
    };
    app.renderDiscovery();
    assert.match(el.textContent, pattern, `for ${status}`);
    assert.ok(!/VRC-LOG/.test(el.textContent),
      `a locked database is not a missing one, so ${status} must not suggest installing`);
  }
});

// ---------------------------------------------------------------------------
// log caps
// ---------------------------------------------------------------------------

test("the log-limit note warns before rows are dropped", () => {
  // Silently discarding rows the user can currently see is the one outcome they
  // would not expect, so the cost is spelled out before they commit to it.
  const env = makeEnvironment();
  const app = loadApp(env);
  const note = env.document.getElementById("set-limits-note");
  const avatarCap = env.document.getElementById("set-max-avatar-log");
  const changeCap = env.document.getElementById("set-max-player-changes");

  app.state = { ...app.state, logs: new Array(500), changes: new Array(340) };

  avatarCap.value = "100";
  changeCap.value = "300";
  app.renderLimitsNote();
  assert.match(note.textContent, /trimming 400 of 500 logged avatars/);
  assert.match(note.textContent, /trimming 40 of 340 player changes/);
  assert.ok(note.classList.contains("warn-text"));

  // Raising one cap must not keep warning about the other.
  changeCap.value = "400";
  app.renderLimitsNote();
  assert.match(note.textContent, /trimming 400 of 500 logged avatars/);
  assert.ok(!/player changes/.test(note.textContent), note.textContent);
});

test("the log-limit note stays quiet when nothing would be dropped", () => {
  const env = makeEnvironment();
  const app = loadApp(env);
  const note = env.document.getElementById("set-limits-note");

  app.state = { ...app.state, logs: new Array(12), changes: new Array(4) };
  env.document.getElementById("set-max-avatar-log").value = "800";
  env.document.getElementById("set-max-player-changes").value = "1000";
  app.renderLimitsNote();

  assert.match(note.textContent, /keeps its newest rows/i);
  assert.ok(!note.classList.contains("warn-text"));
});

test("the log-limit note rejects unusable numbers", () => {
  const env = makeEnvironment();
  const app = loadApp(env);
  const note = env.document.getElementById("set-limits-note");
  const avatarCap = env.document.getElementById("set-max-avatar-log");
  const changeCap = env.document.getElementById("set-max-player-changes");

  app.state = { ...app.state, logs: [], changes: [] };
  changeCap.value = "1000";

  for (const bad of ["0", "-1", "abc", "10001", ""]) {
    avatarCap.value = bad;
    app.renderLimitsNote();
    assert.match(note.textContent, /whole number between 1 and 10000/, `for ${bad}`);
    assert.ok(note.classList.contains("warn-text"), `for ${bad}`);
  }
});

// --------------------------------------------------------------------- groups

const GROUPED = [
  { id: "avtr_1", name: "Catboy", group: "Furry", platforms: ["Quest"], tags: [] },
  { id: "avtr_2", name: "Catgirl", group: "furry", platforms: ["PC"], tags: [] },
  { id: "avtr_3", name: "Robot", group: "Mech", platforms: ["PC", "Quest"], tags: [] },
  { id: "avtr_4", name: "Loose", tags: [] },
];

function withGroups(apiImpl = {}) {
  const env = makeEnvironment(apiImpl);
  const app = loadApp(env);
  app.state = { ...app.state, entries: GROUPED };
  env.document.getElementById("search").value = "";
  return { env, app };
}

// The chips, the labels on each chip, and the count badge.
function chipLabels(env) {
  return env.document.getElementById("group-chips").children.map((c) => [
    c.children.map((k) => k.textContent).join(""),
    c.dataset.group,
    c.classList.contains("active"),
  ]);
}

test("group chips come from the entries, case-insensitively", () => {
  const { env, app } = withGroups();
  app.renderGroupChips();

  // "Furry" and "furry" are one group, not two, and the chip shows the count of
  // both. Sorted by name, with Ungrouped last since it has no name.
  assert.deepEqual(chipLabels(env), [
    ["Furry2", "furry", false],
    ["Mech1", "mech", false],
    ["Ungrouped1", "", false],
  ]);
});

test("an entry with no group key is simply ungrouped", () => {
  const { env, app } = setup();
  app.state = { ...app.state, entries: [{ id: "avtr_9", name: "Legacy", tags: [] }] };
  app.renderGroupChips();
  assert.deepEqual(chipLabels(env), [["Ungrouped1", "", false]]);
});

test("clicking a group chip filters, clicking it again clears", () => {
  const { env, app } = withGroups();
  app.wire();
  const row = env.document.getElementById("group-chips");
  app.renderGroupChips();
  const chip = row.children[0];
  // chip.dataset.group is "furry"; the shim's closest() is a stub, so the event
  // carries the chip as its target the way real delegation delivers it.
  row.dispatch("click", { target: { ...chip, dataset: chip.dataset, closest: () => chip } });

  assert.equal(app.currentGroup, "furry");
  assert.deepEqual(app.visibleEntries().map((e) => e.id), ["avtr_1", "avtr_2"]);
  assert.ok(chipLabels(env)[0][2], "the clicked chip is marked active");

  row.dispatch("click", { target: { ...chip, dataset: chip.dataset, closest: () => chip } });
  assert.equal(app.currentGroup, null);
  assert.equal(app.visibleEntries().length, 4);
});

test("the Ungrouped chip is not the same as no group filter", () => {
  const { env, app } = withGroups();
  app.wire();
  const row = env.document.getElementById("group-chips");
  app.renderGroupChips();
  const ungrouped = row.children[2];
  row.dispatch("click", { target: { ...ungrouped, dataset: ungrouped.dataset, closest: () => ungrouped } });

  assert.equal(app.currentGroup, "");
  assert.deepEqual(app.visibleEntries().map((e) => e.id), ["avtr_4"]);
});

test("a group and a fixed filter intersect", () => {
  const { app } = withGroups();
  app.currentGroup = "mech";
  app.currentFilter = "quest";
  // Mech holds one avatar, and it is Quest, so the intersection is not empty.
  assert.deepEqual(app.visibleEntries().map((e) => e.id), ["avtr_3"]);

  app.currentGroup = "furry";
  app.currentFilter = "quest";
  // Catgirl is Furry but PC only, so picking both must exclude her.
  assert.deepEqual(app.visibleEntries().map((e) => e.id), ["avtr_1"]);
});

test("groupKey mirrors the backend's normalization", () => {
  // storage.group_key collapses whitespace, drops control characters, truncates
  // to 40 and casefolds. A frontend key that skipped any step would fail to
  // match a name the backend had already cleaned, so the avatar's own group
  // would look ungrouped in the dropdown.
  const { app } = setup();
  const g = (v) => app.groupKey(v);
  assert.equal(g("  Big   Furry "), "big furry");
  assert.equal(g("big\nfurry"), "big furry");
  assert.equal(g("b\x07ig furry"), "big furry");
  assert.equal(g("Furry"), g("furry"));
  assert.equal(g(""), "");
  assert.equal(g(null), "");
  assert.equal(g("x".repeat(80)).length, 40);
  assert.equal(g("x".repeat(80)).length, app.MAX_GROUP_NAME);
  assert.notEqual(g("furry"), g("quest"));
});

test("a group named like a fixed filter does not become one", () => {
  // currentGroup and currentFilter are separate on purpose: folding them
  // together would let a group called "all" become the show-everything filter.
  const { app } = setup();
  app.state = { ...app.state, entries: [
    { id: "avtr_1", name: "One", group: "all", tags: [] },
    { id: "avtr_2", name: "Two", tags: [] },
  ] };
  app.currentGroup = "all";
  assert.deepEqual(app.visibleEntries().map((e) => e.id), ["avtr_1"]);
});

test("changing group invalidates the grid", () => {
  // The grid is rebuilt from a signature of everything it displays. Group was
  // missing from that signature, so a chip click left the old grid on screen.
  const { env, app } = withGroups();
  // The grid container's children are the cards themselves: a DocumentFragment
  // empties into its parent, so there is no wrapper between them.
  const cards = () => env.document.getElementById("grid").children;

  app.renderGrid(true);
  assert.equal(cards().length, 4);

  app.currentGroup = "mech";
  app.renderGrid();
  assert.equal(cards().length, 1);

  app.currentGroup = null;
  app.renderGrid();
  assert.equal(cards().length, 4);
});

test("bulk delete offers undo, as its confirmation promises", async () => {
  // Regression: runBulk's confirm dialog says "You can undo this for a few
  // seconds afterwards" and then never offered one, because the entries were
  // only looked up after the backend had removed them.
  const deleted = [];
  const api = {
    bulk_action: async (ids, action) => {
      if (action === "delete") deleted.push(...ids);
      return { ok: true, changed: ids.length };
    },
    restore_entry: async (e) => ({ ok: true }),
    get_state: async () => ({ revs: {}, entries: [], logs: [], changes: [] }),
  };
  const { env, app } = setup(api);
  app.state = { ...app.state, entries: [
    { id: "avtr_1", name: "One", tags: [] },
    { id: "avtr_2", name: "Two", tags: [] },
    { id: "avtr_3", name: "Three", tags: [] },
  ] };
  app.selection.clear();
  app.selection.add("avtr_1");
  app.selection.add("avtr_2");

  const p = app.runBulk("delete");
  await tick();
  env.document.getElementById("confirm-ok").dispatch("click",
    { preventDefault() {}, stopPropagation() {} });
  await p;
  await tick();

  assert.deepEqual(deleted, ["avtr_1", "avtr_2"], "the delete went through");
  assert.equal(app.__test_undoBuffer().length, 2,
    "both deleted entries must be restorable");
  assert.match(env.document.getElementById("undo-label").textContent, /Removed 2 avatars/);
});

test("cancelling the bulk delete offers no undo and deletes nothing", async () => {
  let called = 0;
  const api = {
    bulk_action: async () => { called++; return { ok: true }; },
    get_state: async () => ({ revs: {}, entries: [], logs: [], changes: [] }),
  };
  const { env, app } = setup(api);
  app.state = { ...app.state, entries: [{ id: "avtr_9", name: "Kept", tags: [] }] };
  app.selection.clear();
  app.selection.add("avtr_9");

  const p = app.runBulk("delete");
  await tick();
  env.document.getElementById("confirm-cancel").dispatch("click",
    { preventDefault() {}, stopPropagation() {} });
  await p;
  await tick();

  assert.equal(called, 0, "cancelling must not delete");
  // Asserted on the buffer, not on the bar's class: the DOM shim builds
  // elements without parsing index.html's class attribute, so "hidden" is never
  // set on a fresh element and a class check here would pass or fail for
  // reasons that have nothing to do with the behaviour.
  assert.equal(app.__test_undoBuffer().length, 0,
    "cancelling must not offer to undo something that did not happen");
});

test("importing VRChat favourites distinguishes nothing-new from empty", async () => {
  // Regression: every outcome reported "Imported 0 avatar(s) from VRChat" in the
  // status bar, which is indistinguishable from the button being broken.
  const cases = [
    { added: 3, already: 0, remote: 3, expect: /Imported 3 avatars/ },
    { added: 0, already: 12, remote: 12, expect: /All 12 of your VRChat favourites/ },
    { added: 0, already: 0, remote: 0, expect: /returned no favourites/ },
  ];
  for (const c of cases) {
    const api = {
      import_vrchat_favourites: async () => ({ ok: true, ...c }),
      get_state: async () => ({ revs: {}, entries: [], logs: [], changes: [] }),
    };
    const { env, app } = setup(api);
    await app.importVrchatFavourites();
    const toast = env.document.getElementById("toast");
    assert.match(toast.textContent, c.expect, `for ${JSON.stringify(c)}`);
  }
});

test("the group dropdown is not rebuilt while the user is choosing", () => {
  // Regression: renderGroupDropdown rewrote the <select>'s options on every
  // 700ms poll. Replacing a select's options closes an open dropdown and drops
  // the choice in flight, so picking "New group" did nothing at all.
  const { env, app } = withGroups();
  app.renderGroupDropdown({ group: "Furry" });
  const select = env.document.getElementById("d-group");
  const first = select.children;
  assert.ok(first.length > 0, "the dropdown was populated");

  // The user opens the dropdown and picks something. Re-rendering must not
  // replace the nodes underneath them.
  select.value = "mech";
  app.renderGroupDropdown({ group: "Furry" });
  assert.equal(select.children.length, first.length,
    "a poll rebuilt the options out from under an open dropdown");
  assert.equal(select.children, first, "the option elements are the same nodes");

  // Re-rendering with no change must also leave the user's in-flight pick alone
  // rather than snapping it back to the saved value.
  app.renderGroupDropdown({ group: "Furry" }, "mech");
  assert.equal(select.value, "mech", "the pending pick was discarded");
});

test("the dropdown is rebuilt when the group set really changes", () => {
  // The guard above must not freeze the list: a group added by another avatar
  // has to appear.
  const { env, app } = withGroups();
  app.renderGroupDropdown({ group: "Furry" });
  const select = env.document.getElementById("d-group");
  const before = select.children.length;

  app.state = { ...app.state, entries: [
    ...GROUPED,
    { id: "avtr_new", name: "New", group: "Brand New", tags: [] },
  ] };
  app.renderGroupDropdown({ group: "Furry" });
  assert.ok(select.children.length > before,
    "a newly created group did not reach the dropdown");
  assert.ok(select.children.some((o) => o.value === "brand new"),
    "the new group is not selectable");
});

test("creating a group from the drawer dropdown saves it", async () => {
  // The end-to-end version of the reported bug: pick "New group", type a name,
  // and it must actually be saved onto the avatar.
  const saved = [];
  const api = {
    save_details: async (id, name, notes, tags, group) => {
      saved.push({ id, name, group });
      return { ok: true };
    },
    get_state: async () => ({ revs: {}, entries: [], logs: [], changes: [] }),
  };
  const { env, app } = setup(api);
  app.state = { ...app.state, entries: [
    { id: "avtr_g1", name: "Subject", notes: "", tags: [], group: "" },
  ] };
  app.selectedId = "avtr_g1";
  app.wire();
  app.renderDrawer();

  const select = env.document.getElementById("d-group");
  // The sentinel is the last option, as shipped.
  const sentinel = select.children[select.children.length - 1];
  assert.match(sentinel.textContent, /New group/);

  // Choosing it is what the user does; the change handler must then open the
  // name prompt.
  select.value = sentinel.value;
  select.dispatch("change", { target: select });
  await tick();

  assert.equal(env.document.getElementById("modal-prompt").classList.contains("hidden"),
    false, "the name prompt never opened");

  env.document.getElementById("prompt-input").value = "  Furry  ";
  env.document.getElementById("prompt-ok").dispatch("click",
    { preventDefault() {}, stopPropagation() {} });
  await tick();
  await tick();

  assert.equal(saved.length, 1, `the group was never saved (${JSON.stringify(saved)})`);
  assert.equal(saved[0].group, "Furry", saved[0].group);
  // The regression itself: the name must survive as a *selected* option, not
  // just be saved. Assigning a <select> a value it has no option for selects
  // nothing, so the dropdown went blank and captureDraft read "" and saved "No
  // group" instead.
  assert.notEqual(select.value, "",
    "the dropdown went blank: the typed name was never added as an option");
  assert.ok(select.children.some((o) => o.value === "Furry"),
    "the typed name is not present as a selectable option");
});

test("typing a name that already exists reuses that group", async () => {
  // Same casing-insensitive match as the dropdown keys, so "furry" must not fork
  // a second "furry" group alongside "Furry".
  const saved = [];
  const api = {
    save_details: async (id, name, notes, tags, group) => {
      saved.push({ id, group });
      return { ok: true };
    },
    get_state: async () => ({ revs: {}, entries: [], logs: [], changes: [] }),
  };
  const { env, app } = setup(api);
  app.state = { ...app.state, entries: [
    { id: "avtr_g2", name: "Subject", notes: "", tags: [], group: "" },
    { id: "avtr_g3", name: "Other", notes: "", tags: [], group: "Furry" },
  ] };
  app.selectedId = "avtr_g2";
  app.wire();
  app.renderDrawer();

  const select = env.document.getElementById("d-group");
  select.value = select.children[select.children.length - 1].value;
  select.dispatch("change", { target: select });
  await tick();

  env.document.getElementById("prompt-input").value = "furry";
  env.document.getElementById("prompt-ok").dispatch("click",
    { preventDefault() {}, stopPropagation() {} });
  await tick();
  await tick();

  assert.equal(saved.length, 1, JSON.stringify(saved));
  // The existing group's own casing, not the lowercased key that was typed.
  assert.equal(saved[0].group, "Furry", saved[0].group);
});

test("a prior VRChat refusal does not disable Wear", async () => {
  const requested = [];
  const { env, app } = setup({ wear: async (id) => { requested.push(id); return { ok: true }; } });
  app.state = {
    ...app.state,
    entries: [{ id: "avtr_retry", name: "Retry me", tags: [], inaccessible: true }],
  };

  app.renderGrid(true);
  const card = env.document.getElementById("grid").children[0];
  assert.match(card.innerHTML, /last attempt refused/);
  assert.doesNotMatch(card.innerHTML, /wear-quick[^>]*disabled/);

  card.querySelector(".wear-quick").dispatch("click", { stopPropagation() {} });
  await tick();
  assert.deepEqual(requested, ["avtr_retry"]);
});

test("group chips are hidden on the log scanner", () => {
  // They filter the avatar grid, so on the log view they would be dead controls.
  const { env, app } = withGroups();
  app.renderHeader();
  assert.ok(!env.document.getElementById("group-chips").classList.contains("hidden"));

  app.currentView = "logs";
  app.renderHeader();
  assert.ok(env.document.getElementById("group-chips").classList.contains("hidden"));
  assert.ok(env.document.getElementById("chips").classList.contains("hidden"));
});

// --------------------------------------------------------- drawer group field

test("the drawer dropdown lists the groups and the current one is selected", () => {
  const { env, app } = withGroups();
  const select = env.document.getElementById("d-group");
  app.renderGroupDropdown({ group: "furry" });

  const values = select.children.map((o) => o.value);
  assert.deepEqual(values, ["furry", "mech", "", app.NEW_GROUP]);
  assert.equal(select.value, "furry", "case-insensitive match still selects");
  assert.equal(select.children[0].textContent, "Furry", "the first-typed casing shows");
});

test("an avatar with no group selects No group", () => {
  const { env, app } = withGroups();
  const select = env.document.getElementById("d-group");
  app.renderGroupDropdown({});
  assert.equal(select.value, "");
});

test("a group missing from the list stays selectable", () => {
  // A hand-edited favourites.json, or a stale chip, must not silently show
  // "No group" and invite a change nobody made.
  const { env, app } = withGroups();
  const select = env.document.getElementById("d-group");
  app.renderGroupDropdown({ group: "Ghosts" });
  assert.ok(select.children.some((o) => o.value === "ghosts"));
  assert.equal(select.value, "ghosts");
});

test("a pending pick survives the dropdown being rebuilt", () => {
  // renderDrawer runs on every poll. Rebuilding from the saved group would make
  // an unsaved pick flicker back to the old value.
  const { env, app } = withGroups();
  const select = env.document.getElementById("d-group");
  app.renderGroupDropdown({ group: "furry" }, "mech");
  assert.equal(select.value, "mech");

  // A brand new name has no option yet, so the rebuild has to add it back.
  app.renderGroupDropdown({ group: "furry" }, "Brand New");
  assert.equal(select.value, "Brand New");
});

test("the draft saves the group's real casing, not the dropdown key", () => {
  // Option values are lowercase keys so the two spellings of one group stay
  // distinct options. Sending that key would rewrite every group's stored
  // casing the first time it is picked.
  const { env, app } = withGroups();
  const saved = [];
  app.state = app.state;
  env.document.getElementById("d-group").value = "furry";
  assert.equal(app.draftGroupName(), "Furry");

  app.selectedId = "avtr_2";
  app.captureDraft();
  assert.equal(app.draft.group, "Furry");
  assert.deepEqual(saved, []);
});

test("No group clears it, while the New group sentinel changes nothing", async () => {
  // save_details treats a missing group as "leave it alone", so "" and null
  // have to stay distinct: conflating them makes "No group" silently do nothing.
  const saved = [];
  const api = {
    save_details: async (id, name, notes, tags, group) => {
      saved.push(group);
      return { ok: true };
    },
  };
  const { env, app } = withGroups(api);
  app.selectedId = "avtr_1";

  env.document.getElementById("d-group").value = "";
  app.captureDraft();
  await app.flushDraft();
  assert.equal(saved.at(-1), "");

  env.document.getElementById("d-group").value = app.NEW_GROUP;
  app.captureDraft();
  await app.flushDraft();
  assert.equal(saved.at(-1), null);
});

test("the four-argument drawer save leaves the group alone", async () => {
  // The name/notes/tags autosave predates groups and passes no group at all.
  const saved = [];
  const api = {
    save_details: async (id, name, notes, tags, group) => {
      saved.push(group);
      return { ok: true };
    },
  };
  const { env, app } = withGroups(api);
  app.selectedId = "avtr_1";
  // The drawer populates the dropdown on every render; editing the name
  // afterwards must send the avatar's group, not an empty one.
  app.renderGroupDropdown({ group: "Furry" });
  env.document.getElementById("d-name").value = "Edited";
  app.scheduleSave();
  await app.flushDraft();
  assert.equal(saved.at(-1), "Furry");
});

// The other half of that contract -- a missing group means "leave it alone" --
// is asserted in app/selftest.py against the real backend.

// -------------------------------------------------------------- group picker

test("the picker resolves a group's display casing, not its key", async () => {
  const { env, app } = withGroups();
  const p = app.showGroupPicker("Move to group");
  const list = env.document.getElementById("prompt-list");
  click(list.children[0]);
  assert.equal(await p, "Furry");
});

test("the picker offers ungrouped and a way to make a new one", async () => {
  const { env, app } = withGroups();
  const p = app.showGroupPicker("Move to group");
  const list = env.document.getElementById("prompt-list");
  // Group rows carry a count child; the trailing actions are plain text.
  const labels = list.children.map((b) =>
    (b.children.length ? b.children.map((k) => k.textContent).join("") : b.textContent));

  assert.deepEqual(labels, ["Furry2", "Mech1", "No group (ungrouped)", "＋ New group…"]);
  click(list.children[2]);
  // "" is the ungrouped choice, which the drawer turns into a separate action.
  assert.equal(await p, "");
  assert.notEqual(app.NEW_GROUP, "", "the sentinel cannot collide with ungrouped");
});

test("the picker says so when there are no groups yet", async () => {
  const { env, app } = setup();
  app.state = { ...app.state, entries: [{ id: "avtr_1", name: "Solo", tags: [] }] };
  const p = app.showGroupPicker("Move to group");
  const list = env.document.getElementById("prompt-list");
  assert.match(list.children[0].textContent, /No groups yet/i);
  click(list.children[1]);
  assert.equal(await p, "");
});

test("the picker resolves null on cancel, backdrop and Escape", async () => {
  for (const how of ["cancel", "backdrop", "escape"]) {
    const { env, app } = withGroups();
    const modal = env.document.getElementById("modal-prompt");
    const p = app.showGroupPicker("Move to group");
    if (how === "cancel") click(env.document.getElementById("prompt-cancel"));
    else if (how === "backdrop") modal.dispatch("click", { target: modal });
    else env.document.dispatch("keydown", { key: "Escape", stopPropagation() {}, preventDefault() {} });
    assert.equal(await p, null, how);
    assert.equal(app.pendingPrompt, null, `${how} leaves nothing pending`);
  }
});

test("a second picker settles the first", async () => {
  // Otherwise the first caller's await never returns and its bulk action hangs.
  const { app } = withGroups();
  const first = app.showGroupPicker("One");
  const second = app.showGroupPicker("Two");
  assert.equal(await first, null);
  assert.ok(app.pendingPrompt, "the second is the pending one");
});

test("the picker hides the text field and keeps the list visible", () => {
  const { env, app } = withGroups();
  app.showGroupPicker("Move to group");
  assert.ok(env.document.getElementById("prompt-list").classList.contains("hidden") === false);
  assert.ok(env.document.getElementById("prompt-input").classList.contains("hidden"));
  assert.ok(env.document.getElementById("prompt-ok").classList.contains("hidden"));
  assert.ok(env.document.getElementById("prompt-cancel").classList.contains("hidden") === false);
});

// ----------------------------------------------------------------- bulk moves

function withSelected(apiImpl = {}, ids = ["avtr_1", "avtr_2"]) {
  const { env, app } = withGroups(apiImpl);
  app.wire();
  // additive, because a plain toggleSelect replaces the selection with one card.
  app.toggleSelect(ids[0], false);
  for (const id of ids.slice(1)) app.toggleSelect(id, true);
  return { env, app };
}

test("the bulk bar moves several avatars into one group", async () => {
  const calls = [];
  const { env, app } = withSelected({
    bulk_action: async (ids, action, value) => {
      calls.push({ ids, action, value });
      return { ok: true, changed: ids.length };
    },
    get_state: async () => ({ revs: { entries: 1 } }),
  });

  click(env.document.getElementById("bulk-group"));
  await tick();
  const list = env.document.getElementById("prompt-list");
  // Furry first, and the chip row is what the picker lists, in the same order.
  click(list.children[0]);
  await tick();

  assert.deepEqual(calls, [{ ids: ["avtr_1", "avtr_2"], action: "group", value: "Furry" }]);
});

test("the bulk picker's ungrouped option is a different action", async () => {
  // "" would be sent as "group" with an empty value, which the backend rejects,
  // so choosing No group has to map onto the ungroup action.
  const calls = [];
  const { env, app } = withSelected({
    bulk_action: async (ids, action, value) => {
      calls.push({ action, value });
      return { ok: true, changed: ids.length };
    },
    get_state: async () => ({ revs: { entries: 1 } }),
  });

  click(env.document.getElementById("bulk-group"));
  await tick();
  const list = env.document.getElementById("prompt-list");
  const noGroup = list.children[list.children.length - 2];
  assert.equal(noGroup.textContent, "No group (ungrouped)");
  click(noGroup);
  await tick();

  assert.deepEqual(calls, [{ action: "ungroup", value: "" }]);
});

test("the bulk picker can create a group on the way", async () => {
  const calls = [];
  const { env, app } = withSelected({
    bulk_action: async (ids, action, value) => {
      calls.push({ action, value });
      return { ok: true, changed: ids.length };
    },
    get_state: async () => ({ revs: { entries: 1 } }),
  });

  click(env.document.getElementById("bulk-group"));
  await tick();
  const list = env.document.getElementById("prompt-list");
  click(list.children[list.children.length - 1]);
  await tick();

  // The picker hands back a sentinel; the button has to turn it into a name.
  assert.equal(env.document.getElementById("prompt-list").classList.contains("hidden"), true);
  env.document.getElementById("prompt-input").value = "  Mech   Squad ";
  click(env.document.getElementById("prompt-ok"));
  await tick();

  assert.deepEqual(calls, [{ action: "group", value: "Mech Squad" }]);
});

test("backing out of the bulk picker sends nothing", async () => {
  const calls = [];
  const { env, app } = withSelected({
    bulk_action: async (ids, action, value) => {
      calls.push({ action });
      return { ok: true };
    },
  });

  click(env.document.getElementById("bulk-group"));
  await tick();
  click(env.document.getElementById("prompt-cancel"));
  await tick();
  assert.deepEqual(calls, []);
});

test("backing out of the bulk new-group prompt sends nothing", async () => {
  const calls = [];
  const { env, app } = withSelected({
    bulk_action: async (ids, action, value) => {
      calls.push({ action });
      return { ok: true };
    },
  });

  click(env.document.getElementById("bulk-group"));
  await tick();
  const list = env.document.getElementById("prompt-list");
  click(list.children[list.children.length - 1]);
  await tick();
  click(env.document.getElementById("prompt-cancel"));
  await tick();
  assert.deepEqual(calls, []);
});

test("a blank new-group name sends nothing", async () => {
  const calls = [];
  const { env, app } = withSelected({
    bulk_action: async (ids, action, value) => {
      calls.push({ action });
      return { ok: true };
    },
  });

  click(env.document.getElementById("bulk-group"));
  await tick();
  const list = env.document.getElementById("prompt-list");
  click(list.children[list.children.length - 1]);
  await tick();
  // A name of only whitespace or control characters is not a group name.
  env.document.getElementById("prompt-input").value = "   \x07  ";
  click(env.document.getElementById("prompt-ok"));
  await tick();
  assert.deepEqual(calls, []);
});

// ------------------------------------------------------- drawer new-group flow

test("the drawer's New group creates one and saves it", async () => {
  const saved = [];
  const { env, app } = withGroups({
    save_details: async (id, name, notes, tags, group) => {
      saved.push(group);
      return { ok: true };
    },
    get_state: async () => ({ revs: { entries: 1 } }),
  });
  app.wire();
  app.selectedId = "avtr_1";
  const select = env.document.getElementById("d-group");
  app.renderGroupDropdown({ group: "Furry" });

  select.value = app.NEW_GROUP;
  select.dispatch("change", { target: select });
  await tick();
  env.document.getElementById("prompt-input").value = "  Brand   New  ";
  click(env.document.getElementById("prompt-ok"));
  await tick();

  assert.deepEqual(saved, ["Brand New"], "saved under the cleaned, typed name");
});

test("cancelling the drawer's New group leaves the saved group alone", async () => {
  const saved = [];
  const { env, app } = withGroups({
    save_details: async (id, name, notes, tags, group) => {
      saved.push(group);
      return { ok: true };
    },
  });
  app.wire();
  app.selectedId = "avtr_1";
  const select = env.document.getElementById("d-group");
  app.renderGroupDropdown({ group: "Furry" });

  select.value = app.NEW_GROUP;
  select.dispatch("change", { target: select });
  await tick();
  click(env.document.getElementById("prompt-cancel"));
  await tick();

  // The dropdown must not be left sitting on the sentinel, which would read as
  // "ungrouped" until the next redraw.
  assert.equal(select.value, "furry");
  assert.deepEqual(saved, [], "a cancelled prompt changes nothing");
});

test("typing an existing group's spelling does not fork it", async () => {
  const saved = [];
  const { env, app } = withGroups({
    save_details: async (id, name, notes, tags, group) => {
      saved.push(group);
      return { ok: true };
    },
  });
  app.wire();
  app.selectedId = "avtr_1";
  const select = env.document.getElementById("d-group");
  app.renderGroupDropdown({ group: "" });

  select.value = app.NEW_GROUP;
  select.dispatch("change", { target: select });
  await tick();
  env.document.getElementById("prompt-input").value = "FURRY";
  click(env.document.getElementById("prompt-ok"));
  await tick();

  assert.equal(select.value, "furry", "the existing option is reused");
  assert.deepEqual(saved, ["Furry"], "and the first-typed casing is kept");
});

test("a text prompt after a picker still shows its field", () => {
  // Both modes share one modal, so switching has to restore what it hid.
  const { env, app } = withGroups();
  app.showGroupPicker("Move to group");
  const p = app.showPrompt("Add a tag");
  assert.ok(!env.document.getElementById("prompt-input").classList.contains("hidden"));
  assert.ok(env.document.getElementById("prompt-list").classList.contains("hidden"));
  assert.ok(!env.document.getElementById("prompt-ok").classList.contains("hidden"));
  click(env.document.getElementById("prompt-cancel"));
  return p;
});
