const test = require("node:test");
const assert = require("node:assert/strict");

// Tests for the timer-screen card (custom:timer-screen, bundled in the
// Satellite Timers bridge). The card needs a DOM to load, so it is
// exercised here through a minimal harness: elements with children,
// attributes, dataset, classList, style, textContent, innerHTML (parsed
// from the strings the card writes), querySelector, and
// getBoundingClientRect. The card never touches anything else.
//
// Run: node --test custom_components/satellite_timers/www/timer-screen-card.test.cjs

function decodeEntities(value) {
  return String(value)
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'");
}

function parseHtml(html) {
  const roots = [];
  const stack = [];
  const token = /<(\/?)([a-zA-Z][\w-]*)([^>]*?)(\/?)>|([^<]+)/g;
  let match;
  while ((match = token.exec(html)) !== null) {
    if (match[5] !== undefined) {
      const text = decodeEntities(match[5]);
      if (text.trim() === "") continue;
      const node = new FakeElement("#text");
      node._ownText = text;
      if (stack.length > 0) stack[stack.length - 1].appendChild(node);
      else roots.push(node);
      continue;
    }
    const [, closing, tag, attrs, selfClosing] = match;
    if (closing) {
      stack.pop();
      continue;
    }
    const el = new FakeElement(tag);
    for (const [, name, value] of attrs.matchAll(/([\w-]+)="([^"]*)"/g)) {
      const decoded = decodeEntities(value);
      if (name === "class") {
        for (const cls of decoded.split(/\s+/)) if (cls) el.classList.add(cls);
      } else if (name.startsWith("data-")) {
        const key = name
          .slice(5)
          .replace(/-([a-z])/g, (_, ch) => ch.toUpperCase());
        el.dataset[key] = decoded;
      } else {
        el.setAttribute(name, decoded);
      }
    }
    if (stack.length > 0) stack[stack.length - 1].appendChild(el);
    else roots.push(el);
    if (!selfClosing) stack.push(el);
  }
  return roots;
}

class FakeElement {
  constructor(tagName) {
    this.tagName = String(tagName).toUpperCase();
    this.children = [];
    this.parentNode = null;
    this.style = {};
    this.dataset = {};
    this.attributes = {};
    this._classSet = new Set();
    this._ownText = "";
    this._listeners = {};
    this.hidden = false;
    this.disabled = false;
  }

  get classList() {
    const set = this._classSet;
    return {
      add: (...names) => names.forEach((n) => set.add(n)),
      remove: (...names) => names.forEach((n) => set.delete(n)),
      toggle: (name, force) => {
        if (force === undefined) {
          if (set.has(name)) set.delete(name);
          else set.add(name);
        } else if (force) set.add(name);
        else set.delete(name);
        return set.has(name);
      },
      contains: (name) => set.has(name),
    };
  }

  get className() {
    return [...this._classSet].join(" ");
  }

  set className(value) {
    this._classSet = new Set(String(value).split(/\s+/).filter(Boolean));
  }

  get textContent() {
    if (this._ownText) return this._ownText;
    return this.children.map((c) => c.textContent).join("");
  }

  set textContent(value) {
    this._ownText = String(value);
    this.children = [];
  }

  get innerHTML() {
    return this.children.map((c) => c._serialize()).join("");
  }

  set innerHTML(value) {
    this._ownText = "";
    this.children = [];
    for (const node of parseHtml(String(value))) this.appendChild(node);
  }

  _serialize() {
    if (this.tagName === "#TEXT") return this._ownText;
    const attrs = [];
    if (this._classSet.size > 0)
      attrs.push(`class="${[...this._classSet].join(" ")}"`);
    for (const [key, value] of Object.entries(this.dataset)) {
      const name =
        "data-" + key.replace(/[A-Z]/g, (ch) => "-" + ch.toLowerCase());
      attrs.push(`${name}="${value}"`);
    }
    for (const [name, value] of Object.entries(this.attributes)) {
      attrs.push(`${name}="${value}"`);
    }
    const open = `<${this.tagName.toLowerCase()}${attrs.length ? " " + attrs.join(" ") : ""}>`;
    return (
      open + this.children.map((c) => c._serialize()).join("") + this._ownText + `</${this.tagName.toLowerCase()}>`
    );
  }

  appendChild(child) {
    if (child.parentNode) child.parentNode.removeChild(child);
    child.parentNode = this;
    this.children.push(child);
    return child;
  }

  removeChild(child) {
    const index = this.children.indexOf(child);
    if (index >= 0) this.children.splice(index, 1);
    child.parentNode = null;
    return child;
  }

  remove() {
    if (this.parentNode) this.parentNode.removeChild(this);
  }

  setAttribute(name, value) {
    this.attributes[name] = String(value);
  }

  getAttribute(name) {
    return this.attributes[name] ?? null;
  }

  addEventListener(type, fn) {
    this._listeners[type] = fn;
  }

  querySelector(selector) {
    return this.querySelectorAll(selector)[0] ?? null;
  }

  querySelectorAll(selector) {
    const parts = selector.trim().split(/\s+/);
    let current = [this];
    for (const part of parts) {
      const next = [];
      for (const root of current) {
        const walk = (el) => {
          if (el !== root && matches(el, part)) next.push(el);
          for (const child of el.children) walk(child);
        };
        for (const child of root.children) walk(child);
      }
      current = next;
    }
    return current;
  }

  getBoundingClientRect() {
    return { left: 0, top: 0, width: 0, height: 0 };
  }
}

function matches(el, selector) {
  if (el.tagName === "#TEXT") return false;
  const dataAttr = /^\[data-([\w-]+)(?:="([^"]*)")?\]$/.exec(selector);
  if (dataAttr) {
    const key = dataAttr[1].replace(/-([a-z])/g, (_, ch) => ch.toUpperCase());
    if (!(key in el.dataset)) return false;
    return dataAttr[2] === undefined || el.dataset[key] === dataAttr[2];
  }
  const classOnly = /^\.([\w-]+)$/.exec(selector);
  if (classOnly) return el.classList.contains(classOnly[1]);
  const tagClass = /^([a-zA-Z][\w-]*)\.([\w-]+)$/.exec(selector);
  if (tagClass) {
    return (
      el.tagName === tagClass[1].toUpperCase() &&
      el.classList.contains(tagClass[2])
    );
  }
  if (/^[a-zA-Z][\w-]*$/.test(selector)) {
    return el.tagName === selector.toUpperCase();
  }
  return false;
}

class FakeShadowRoot extends FakeElement {
  constructor() {
    super("#shadow-root");
  }
}

class FakeHTMLElement {
  attachShadow() {
    this.shadowRoot = new FakeShadowRoot();
    return this.shadowRoot;
  }
}

globalThis.HTMLElement = FakeHTMLElement;
globalThis.customElements = { define: () => {} };
globalThis.document = { createElement: (tag) => new FakeElement(tag) };

const {
  displayName,
  formatRemaining,
  remainingSeconds,
  progressFraction,
  discoverTimers,
  gridColumns,
  detectDevice,
  readDeviceEnv,
  TimerScreenCard,
} = require("./timer-screen-card.js");

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// -- fixtures ---------------------------------------------------------------

function timerState(id, name, state, overrides = {}) {
  const remaining = overrides.remaining ?? 600;
  const duration = overrides.duration ?? 900;
  return {
    entity_id: `sensor.satellite_timer_${id}`,
    state,
    attributes: {
      timer_id: id,
      timer_name: name,
      duration,
      remaining,
      finishes_at:
        overrides.finishesAt ??
        (state === "active"
          ? new Date(Date.now() + remaining * 1000).toISOString()
          : null),
      finished_at: overrides.finishedAt ?? null,
    },
  };
}

function fakeHass(states) {
  const calls = [];
  return {
    calls,
    states: states || [],
    callService: async (domain, service, data) => {
      calls.push([domain, service, data]);
    },
  };
}

function timerCard(states) {
  const card = new TimerScreenCard();
  card.setConfig({});
  card.hass = fakeHass(states);
  return card;
}

// -- pure helpers -----------------------------------------------------------

test("formatRemaining renders mm:ss and h:mm:ss", () => {
  assert.equal(formatRemaining(0), "0:00");
  assert.equal(formatRemaining(59), "0:59");
  assert.equal(formatRemaining(600), "10:00");
  assert.equal(formatRemaining(3661), "1:01:01");
  assert.equal(formatRemaining(-5), "0:00");
});

test("discoverTimers keeps only satellite timer sensors in a live state, soonest first", () => {
  const states = [
    timerState("t-late", "Pasta", "active", { remaining: 700 }),
    { entity_id: "sensor.other", state: "active", attributes: {} },
    timerState("t-soon", "Rice", "active", { remaining: 120 }),
    timerState("t-idle", "Idle", "idle", {}),
    timerState("t-done", "Done", "finished", {}),
  ];
  const timers = discoverTimers(states);
  assert.deepEqual(
    timers.map((t) => t.timer_id),
    ["t-done", "t-soon", "t-late"]
  );
  assert.equal(timers[1].name, "Rice");
});

test("remainingSeconds counts down from finishes_at and freezes while paused", () => {
  const active = discoverTimers([
    timerState("t1", "Pasta", "active", { remaining: 600 }),
  ])[0];
  const left = remainingSeconds(active, Date.now());
  assert.ok(left >= 590 && left <= 600, `left was ${left}`);
  const paused = discoverTimers([
    timerState("t2", "Rice", "paused", { remaining: 300 }),
  ])[0];
  assert.equal(remainingSeconds(paused, Date.now()), 300);
});

test("progressFraction is the share of time left", () => {
  const timer = discoverTimers([
    timerState("t1", "Pasta", "paused", { remaining: 450, duration: 900 }),
  ])[0];
  assert.equal(progressFraction(timer, Date.now()), 0.5);
  assert.equal(progressFraction(null, Date.now()), 0);
});

// -- gridColumns per category -----------------------------------------------

test("gridColumns on the hub: one row to three, 2x2 for four, three columns past that", () => {
  assert.equal(gridColumns(0), 1);
  assert.equal(gridColumns(1), 1);
  assert.equal(gridColumns(2), 2);
  assert.equal(gridColumns(3), 3);
  assert.equal(gridColumns(4), 2);
  assert.equal(gridColumns(5), 3);
  assert.equal(gridColumns(9), 3);
  // No category given is the hub mapping (the old call shape).
  assert.equal(gridColumns(4, "hub"), 2);
  assert.equal(gridColumns(5, "hub"), 3);
});

test("gridColumns on desktop: up to four in a row, four columns past that", () => {
  assert.equal(gridColumns(1, "desktop"), 1);
  assert.equal(gridColumns(3, "desktop"), 3);
  assert.equal(gridColumns(4, "desktop"), 4);
  assert.equal(gridColumns(5, "desktop"), 4);
  assert.equal(gridColumns(9, "desktop"), 4);
});

test("gridColumns on mobile keeps the hub mapping (portrait stacks; landscape grids)", () => {
  assert.equal(gridColumns(1, "mobile"), 1);
  assert.equal(gridColumns(3, "mobile"), 3);
  assert.equal(gridColumns(4, "mobile"), 2);
  assert.equal(gridColumns(5, "mobile"), 3);
});

// -- Device detection (shared mechanism; the recipe card carries the
// identical block — see ADAPTIVE-LAYOUT.md) ---------------------------------

test("detectDevice: the hub is a huge landscape touch screen", () => {
  assert.deepEqual(
    detectDevice({
      width: 1920,
      height: 1200,
      coarsePointer: true,
      finePointer: false,
      touch: true,
      userAgent:
        "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    }),
    { category: "hub", orientation: "landscape" }
  );
});

test("detectDevice: a mouse is desktop; a phone is mobile in both orientations", () => {
  assert.deepEqual(
    detectDevice({
      width: 1440,
      height: 900,
      coarsePointer: false,
      finePointer: true,
      touch: false,
      userAgent:
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    }),
    { category: "desktop", orientation: "landscape" }
  );
  assert.deepEqual(
    detectDevice({
      width: 390,
      height: 844,
      coarsePointer: true,
      finePointer: false,
      touch: true,
      userAgent:
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
    }),
    { category: "mobile", orientation: "portrait" }
  );
  assert.deepEqual(
    detectDevice({
      width: 844,
      height: 390,
      coarsePointer: true,
      finePointer: false,
      touch: true,
      userAgent:
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
    }),
    { category: "mobile", orientation: "landscape" }
  );
});

test("detectDevice: a mobile user agent wins over a desktop-sized viewport", () => {
  assert.deepEqual(
    detectDevice({
      width: 1440,
      height: 900,
      coarsePointer: false,
      finePointer: true,
      touch: false,
      userAgent:
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
    }),
    { category: "mobile", orientation: "landscape" }
  );
});

test("readDeviceEnv returns null outside a browser", () => {
  assert.equal(readDeviceEnv(), null);
});

// -- device classes on the screen -------------------------------------------

function stubWindow({ width, height, coarse, fine, touch, userAgent }) {
  globalThis.window = {
    innerWidth: width,
    innerHeight: height,
    matchMedia: (query) =>
      query === "(pointer: coarse)"
        ? { matches: coarse }
        : query === "(pointer: fine)"
          ? { matches: fine }
          : { matches: false },
    addEventListener: () => {},
    removeEventListener: () => {},
    ...(touch ? { ontouchstart: () => {} } : {}),
  };
  globalThis.navigator = {
    maxTouchPoints: touch ? 5 : 0,
    userAgent,
  };
}

function clearWindow() {
  delete globalThis.window;
  delete globalThis.navigator;
}

const HUB_WINDOW = {
  width: 1920,
  height: 1200,
  coarse: true,
  fine: false,
  touch: true,
  userAgent:
    "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
};
const DESKTOP_WINDOW = {
  width: 1440,
  height: 900,
  coarse: false,
  fine: true,
  touch: false,
  userAgent:
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
};
const PHONE_WINDOW = {
  width: 390,
  height: 844,
  coarse: true,
  fine: false,
  touch: true,
  userAgent:
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
};

function fiveTimers() {
  return [
    timerState("t1", "Pasta", "active", { remaining: 720 }),
    timerState("t2", "Rice", "active", { remaining: 540 }),
    timerState("t3", "Sauce", "paused", { remaining: 300 }),
    timerState("t4", "Oven", "active", { remaining: 1500 }),
    timerState("t5", "Tea", "finished", {}),
  ];
}

test("the screen carries dev-hub + orient-landscape on the kiosk, with the hub grid", () => {
  stubWindow(HUB_WINDOW);
  try {
    const card = timerCard(fiveTimers());
    const screen = card.shadowRoot.querySelector(".screen");
    assert.ok(screen.classList.contains("dev-hub"));
    assert.ok(screen.classList.contains("orient-landscape"));
    assert.ok(screen.classList.contains("cols-3"));
    assert.equal(card.shadowRoot.querySelectorAll(".dial").length, 5);
  } finally {
    clearWindow();
  }
});

test("the screen carries dev-desktop and four columns for five timers", () => {
  stubWindow(DESKTOP_WINDOW);
  try {
    const card = timerCard(fiveTimers());
    const screen = card.shadowRoot.querySelector(".screen");
    assert.ok(screen.classList.contains("dev-desktop"));
    assert.ok(screen.classList.contains("cols-4"));
  } finally {
    clearWindow();
  }
});

test("the screen carries dev-mobile + orient-portrait on a phone", () => {
  stubWindow(PHONE_WINDOW);
  try {
    const card = timerCard(fiveTimers());
    const screen = card.shadowRoot.querySelector(".screen");
    assert.ok(screen.classList.contains("dev-mobile"));
    assert.ok(screen.classList.contains("orient-portrait"));
    assert.equal(card.shadowRoot.querySelectorAll(".dial").length, 5);
  } finally {
    clearWindow();
  }
});

test("a resize re-detects the device and re-grids", () => {
  stubWindow(PHONE_WINDOW);
  try {
    const card = timerCard(fiveTimers());
    const screen = card.shadowRoot.querySelector(".screen");
    assert.ok(screen.classList.contains("dev-mobile"));
    assert.ok(screen.classList.contains("cols-3"));
    stubWindow(DESKTOP_WINDOW);
    card._onResize();
    assert.ok(screen.classList.contains("dev-desktop"));
    assert.ok(!screen.classList.contains("dev-mobile"));
    assert.ok(screen.classList.contains("cols-4"));
  } finally {
    clearWindow();
  }
});

test("with no browser environment the screen carries no device classes (base layout stands)", () => {
  clearWindow();
  const card = timerCard(fiveTimers());
  const screen = card.shadowRoot.querySelector(".screen");
  assert.ok(!screen.classList.contains("dev-hub"));
  assert.ok(!screen.classList.contains("dev-desktop"));
  assert.ok(!screen.classList.contains("dev-mobile"));
  // No category given is the hub grid.
  assert.ok(screen.classList.contains("cols-3"));
});

test("an empty screen shows the empty state and one column", () => {
  const card = timerCard([]);
  const screen = card.shadowRoot.querySelector(".screen");
  assert.ok(screen.classList.contains("cols-1"));
  assert.match(card.shadowRoot.innerHTML, /No timers running/);
});

// -- behaviour is unchanged ---------------------------------------------------

test("tapping a dial pauses a running timer, resumes a paused one, dismisses a finished one", async () => {
  const hass = fakeHass([
    timerState("t1", "Pasta", "active", { remaining: 600 }),
    timerState("t2", "Rice", "paused", { remaining: 300 }),
    timerState("t3", "Tea", "finished", {}),
  ]);
  const card = new TimerScreenCard();
  card.setConfig({});
  card.hass = hass;
  const click = (timerId) =>
    card._onClick({
      target: {
        closest: () => ({ dataset: { action: "dial", timer: timerId } }),
      },
    });
  click("t1");
  click("t2");
  click("t3");
  await sleep(20);
  assert.deepEqual(hass.calls, [
    ["satellite_timers", "pause_timer", { timer_id: "t1" }],
    ["satellite_timers", "resume_timer", { timer_id: "t2" }],
    ["satellite_timers", "dismiss_timer", { timer_id: "t3" }],
  ]);
});

test("Cancel arms on the first tap and only cancels on the second", async () => {
  const hass = fakeHass([timerState("t1", "Pasta", "active", {})]);
  const card = new TimerScreenCard();
  card.setConfig({});
  card.hass = hass;
  const click = () =>
    card._onClick({
      target: {
        closest: () => ({ dataset: { action: "cancel", timer: "t1" } }),
      },
    });
  click();
  await sleep(20);
  assert.deepEqual(hass.calls, []);
  const button = card.shadowRoot.querySelector(".cancel");
  assert.equal(button.textContent, "Tap again to cancel");
  click();
  await sleep(20);
  assert.deepEqual(hass.calls, [
    ["satellite_timers", "cancel_timer", { timer_id: "t1" }],
  ]);
});

test("there is no +1 min button and no Dismiss button", () => {
  const card = timerCard([
    timerState("t1", "Pasta", "active", {}),
    timerState("t2", "Tea", "finished", {}),
  ]);
  const html = card.shadowRoot.innerHTML;
  assert.doesNotMatch(html, /\+1/);
  assert.doesNotMatch(html, /data-action="(dismiss|add_time)"/);
  // A finished timer gets Restart; a running one gets Cancel.
  assert.match(html, /Restart/);
  assert.match(html, /Cancel/);
});
