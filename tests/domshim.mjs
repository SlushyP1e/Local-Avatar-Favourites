// Minimal DOM/browser stub, just enough to load app/web/app.js unmodified and
// exercise its pure logic. Avoids adding a jsdom dependency to a Python project.
import { readFileSync } from "node:fs";

export function makeElement(id = "el") {
  const listeners = new Map();
  const classes = new Set();
  let disabled = false;
  let value = "";
  const el = {
    id,
    textContent: "",
    title: "",
    style: { setProperty() {} },
    dataset: {},
    children: [],
    offsetWidth: 0,
    getAttribute: () => null,
    setAttribute: () => {},
    closest: () => null,
    // Children accumulate so renderGroupChips and renderGroupDropdown can be
    // inspected, and so applyStagger sees the real child count. Assigning
    // innerHTML clears them, which is what the renderers rely on to rebuild.
    // A real DocumentFragment empties itself into the parent: appending one
    // splices its children in, and the fragment is left with none. Treating it
    // as an ordinary element made every rendered row count as a single child,
    // so a test could not tell 50 cards from 1.
    appendChild(child) {
      if (child && child.nodeType === 11) {
        this.children.push(...child.children);
        child.children.length = 0;
        return child;
      }
      this.children.push(child);
      return child;
    },
    removeChild(child) {
      const i = this.children.indexOf(child);
      if (i >= 0) this.children.splice(i, 1);
      return child;
    },
    // A regular function, not an arrow, because the memo hangs off `this`.
    querySelector(sel) {
      // Cards wire listeners onto their own inner buttons. Returning a memoised
      // stub per selector keeps renderGrid runnable without parsing innerHTML.
      if (!this._stubs) this._stubs = new Map();
      if (!this._stubs.has(sel)) this._stubs.set(sel, makeElement(sel));
      return this._stubs.get(sel);
    },
    querySelectorAll: () => [],
    getBoundingClientRect: () => ({ width: 0, height: 0 }),
    classList: {
      add: (...c) => c.forEach((x) => classes.add(x)),
      remove: (...c) => c.forEach((x) => classes.delete(x)),
      contains: (c) => classes.has(c),
      toggle: (c, force) => {
        const on = force === undefined ? !classes.has(c) : !!force;
        if (on) classes.add(c);
        else classes.delete(c);
        return on;
      },
    },
    addEventListener(type, fn) {
      if (!listeners.has(type)) listeners.set(type, []);
      listeners.get(type).push(fn);
    },
    removeEventListener(type, fn) {
      const arr = listeners.get(type);
      if (!arr) return;
      const i = arr.indexOf(fn);
      if (i >= 0) arr.splice(i, 1);
    },
    dispatch(type, event = {}) {
      // Match a real disabled control: the browser does not deliver the event,
      // so a click handler cannot run and a test cannot pass by ignoring this.
      if (disabled && type !== "animationend") return;
      for (const fn of (listeners.get(type) || []).slice()) fn(event);
    },
    // Standard element methods. showPrompt focuses and selects its field, so
    // without these the shim would throw where a real DOM would not.
    focus() {},
    select() {},
    blur() {},
    // A real button swallows clicks while disabled, so a test that clicks one
    // must see nothing happen. Dispatch is below, where it can check this.
    get disabled() { return disabled; },
    set disabled(value) { disabled = !!value; },
    listenerCount(type) {
      return (listeners.get(type) || []).length;
    },
    totalListeners() {
      let n = 0;
      for (const arr of listeners.values()) n += arr.length;
      return n;
    },
  };
  // innerHTML is only ever assigned wholesale by the renderers, never parsed,
  // so a plain field is enough as long as it clears the child list.
  let html = "";
  Object.defineProperty(el, "innerHTML", {
    get: () => html,
    set: (v) => {
      html = String(v == null ? "" : v);
      if (html === "") el.children.length = 0;
    },
  });
  // The renderers build elements by assigning className, then ask classList
  // whether a class is present. Without this bridge those two disagree and a
  // test sees an "active" chip that reports itself as inactive.
  let className = "";
  Object.defineProperty(el, "className", {
    get: () => className,
    set: (v) => {
      className = String(v == null ? "" : v);
      classes.clear();
      for (const c of className.split(/\s+/).filter(Boolean)) classes.add(c);
    },
  });
  // A real <select> whose assigned value matches none of its options selects
  // nothing: reading .value back gives "". Modelling that matters, because the
  // alternative hides a whole class of bug from the tests -- the property would
  // keep the value it was given while the browser showed a blank dropdown, and
  // any test asserting on .value would pass against a UI that is visibly empty.
  Object.defineProperty(el, "value", {
    get() { return value; },
    set(v) {
      value = String(v == null ? "" : v);
      const opts = el.children.filter((c) => c && c.nodeName === "OPTION");
      if (opts.length && !opts.some((o) => o.value === value)) value = "";
    },
  });
  // Assigning textContent replaces the element's contents in a real DOM.
  let text = "";
  Object.defineProperty(el, "textContent", {
    get: () => text,
    set: (v) => {
      text = String(v == null ? "" : v);
      el.children.length = 0;
    },
  });
  return el;
}

export function makeEnvironment(apiImpl = {}, options = {}) {
  const env = options;
  const elements = new Map();
  const root = makeElement("html");
  root.setAttribute = (k, v) => { root.attributes[k] = v; };
  root.attributes = {};
  const docListeners = new Map();
  // Stand-ins for <img> elements the app has registered with registerThumbImage,
  // so the selector app.js uses to patch a freshly loaded thumbnail finds them.
  const thumbImages = [];
  const THUMB_SELECTOR = /^\[data-thumb-id="(.*)"\]$/;
  const document = {
    activeElement: null,
    documentElement: root,
    getAttribute: () => null,
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, makeElement(id));
      return elements.get(id);
    },
    createElement: (tag) => {
      const node = makeElement(tag || "created");
      node.nodeName = String(tag || "div").toUpperCase();
      return node;
    },
    createTextNode: (text) => ({ nodeType: 3, textContent: String(text) }),
    createDocumentFragment: () => {
      const frag = makeElement("fragment");
      frag.nodeType = 11;
      return frag;
    },
    querySelector: () => null,
    querySelectorAll: (sel) => {
      const match = THUMB_SELECTOR.exec(String(sel || ""));
      if (!match) return [];
      const id = match[1];
      return thumbImages.filter((img) => img.dataset.thumbId === id);
    },
    registerThumbImage(img) {
      thumbImages.push(img);
      return img;
    },
    addEventListener(type, fn) {
      if (!docListeners.has(type)) docListeners.set(type, []);
      docListeners.get(type).push(fn);
    },
    removeEventListener(type, fn) {
      const arr = docListeners.get(type);
      if (!arr) return;
      const i = arr.indexOf(fn);
      if (i >= 0) arr.splice(i, 1);
    },
    dispatch(type, event) {
      // Capture listeners registered with capture=true run before the rest,
      // mirroring real propagation order.
      for (const fn of (docListeners.get(type) || []).slice()) fn(event);
    },
    docListenerCount(type) {
      return (docListeners.get(type) || []).length;
    },
  };

  const window = {
    pywebview: { api: apiImpl },
    addEventListener: () => {},
    innerWidth: 1280,
    innerHeight: 800,
    // Overridable so the reduced-motion path can be exercised.
    matchMedia: (query) => ({
      media: query,
      matches: !!env.systemReducesMotion,
      addEventListener() {},
      removeEventListener() {},
    }),
  };

  const CSS = { escape: (s) => String(s) };
  return { document, window, CSS, elements, root, systemReducesMotion: !!options.systemReducesMotion };
}

// Load app.js and hand back its internals. app.js is a plain script with
// top-level consts, so it is evaluated inside a function body and the pieces we
// want are returned explicitly.
export function loadApp(env) {
  const src = readFileSync("app/web/app.js", "utf8");
  return new Function(
    "document", "window", "CSS", "setTimeout", "clearTimeout", "setInterval",
    `${src}
; return {
  showConfirm, showAlert, showPrompt, openModal, closeModal, ensureThumb,
  scheduleSave, flushDraft, captureDraft, openDrawer, closeDrawer, call,
  thumbCache, thumbKey, thumbPending, thumbOrder, visibleEntries, cardSub, escapeHtml,
  setView, animateView, applyStagger, revealOnce, replayAnimation,
  clearAnimationWhenDone, applyMotionPreference, systemPrefersReducedMotion,
  renderMotionNote, renderDiscovery, renderLimitsNote, renderInfiniteNote,
  VIEW_ORDER, STAGGER_CAP,
  groupKey, entryGroupKey, groupSummary, renderGroupChips, matchesFilter,
  MAX_GROUP_NAME,
  renderGroupDropdown, draftGroupName, showGroupPicker, setPromptMode, NEW_GROUP,
  toggleSelect, clearSelection, promptForGroupName, runBulk, importVrchatFavourites,
  __test_deleteEntry: deleteEntry, __test_undoBuffer() { return undoBuffer; },
  get selection() { return selection; },
  wire, renderGrid, renderHeader, render, renderLogs, renderDrawer,
  __test_groupDropdownSig() { return groupDropdownSig; },
  setThumbBase, thumbUrlFor, lazyThumb, resolveThumbSrc, onThumbVisibility,
  thumbLoadFailed, PAGE_ROWS, PAGE_BUTTON_CAP, pageCount, clampPage, pageSlice,
  pageFor, currentPage, listTotal, renderPager, goToPage, goToActivePage,
  activeListName, infiniteGrid, gridSlice, pagerFor, gridCountText,
  ignoreAvatar, unignoreAvatar, loadIgnores, renderIgnoreList, forgetLogMenuItems,
  cardMenuItems, get ignores() { return ignores; },
  set ignores(v) { ignores = v; },
  get logTab() { return logTab; },
  set logTab(v) { logTab = v; },
  get currentView() { return currentView; },
  set currentView(v) { currentView = v; },
  get currentFilter() { return currentFilter; },
  set currentFilter(v) { currentFilter = v; },
  get currentGroup() { return currentGroup; },
  set currentGroup(v) { currentGroup = v; },
  get pendingConfirm() { return pendingConfirm; },
  get pendingPrompt() { return pendingPrompt; },
  get draft() { return draft; },
  get selectedId() { return selectedId; },
  set selectedId(v) { selectedId = v; },
  set draft(v) { draft = v; },
  get state() { return state; },
  set state(v) { state = v; },
};`,
  )(env.document, env.window, env.CSS, setTimeout, clearTimeout, setInterval);
}