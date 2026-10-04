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
  // Stand in for a few hundred cards.
  list.children = Array.from({ length: 200 }, () => ({
    style: { setProperty() {} },
  }));
  app.applyStagger(list);
  assert.equal(app.STAGGER_CAP, 14,
    "the cap is what keeps a 200 item list from taking seconds");
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