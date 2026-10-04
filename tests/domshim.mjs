// Minimal DOM/browser stub, just enough to load app/web/app.js unmodified and
// exercise its pure logic. Avoids adding a jsdom dependency to a Python project.
import { readFileSync } from "node:fs";

export function makeElement(id = "el") {
  const listeners = new Map();
  const classes = new Set();
  return {
    id,
    value: "",
    textContent: "",
    style: { setProperty() {} },
    dataset: {},
    innerHTML: "",
    // Enough for the stagger logic, which only reads children.length and
    // assigns --i on each one.
    children: [],
    offsetWidth: 0,
    getAttribute: () => null,
    setAttribute: () => {},
    closest: () => null,
    appendChild: () => {},
    removeChild: () => {},
    querySelector: () => null,
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
      for (const fn of (listeners.get(type) || []).slice()) fn(event);
    },
    listenerCount(type) {
      return (listeners.get(type) || []).length;
    },
    totalListeners() {
      let n = 0;
      for (const arr of listeners.values()) n += arr.length;
      return n;
    },
  };
}

export function makeEnvironment(apiImpl = {}, options = {}) {
  const env = options;
  const elements = new Map();
  const root = makeElement("html");
  root.setAttribute = (k, v) => { root.attributes[k] = v; };
  root.attributes = {};
  const docListeners = new Map();
  const document = {
    activeElement: null,
    documentElement: root,
    getAttribute: () => null,
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, makeElement(id));
      return elements.get(id);
    },
    createElement: () => makeElement("created"),
    createDocumentFragment: () => makeElement("fragment"),
    querySelector: () => null,
    querySelectorAll: () => [],
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
  showConfirm, showAlert, openModal, closeModal, ensureThumb,
  scheduleSave, flushDraft, captureDraft, openDrawer, closeDrawer, call,
  thumbCache, thumbKey, thumbPending, thumbOrder, visibleEntries, cardSub, escapeHtml,
  setView, animateView, applyStagger, revealOnce, replayAnimation,
  clearAnimationWhenDone, applyMotionPreference, systemPrefersReducedMotion,
  renderMotionNote, VIEW_ORDER, STAGGER_CAP,
  get currentView() { return currentView; },
  set currentView(v) { currentView = v; },
  get pendingConfirm() { return pendingConfirm; },
  get draft() { return draft; },
  get selectedId() { return selectedId; },
  set selectedId(v) { selectedId = v; },
  set draft(v) { draft = v; },
  get state() { return state; },
  set state(v) { state = v; },
};`,
  )(env.document, env.window, env.CSS, setTimeout, clearTimeout, setInterval);
}