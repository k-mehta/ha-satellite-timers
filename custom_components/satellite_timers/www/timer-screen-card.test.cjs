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
  formatElapsedSince,
  formatStartTime,
  remainingSeconds,
  progressFraction,
  startedAtMs,
  discoverTimers,
  gridColumns,
  detectDevice,
  readDeviceEnv,
  readRecipeContext,
  NAV_STRIP_VIEWS,
  TimerScreenCard,
  CARD_CSS,
  TIMER_SCROLL_KEY,
  parseTimerScroll,
  serializeTimerScroll,
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

test("gridColumns on tablet keeps the hub mapping (DESIGN-9: at most three dials in a row)", () => {
  assert.equal(gridColumns(1, "tablet"), 1);
  assert.equal(gridColumns(3, "tablet"), 3);
  assert.equal(gridColumns(4, "tablet"), 2);
  assert.equal(gridColumns(5, "tablet"), 3);
  assert.equal(gridColumns(9, "tablet"), 3);
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

// -- Phase 3: TIMER-1/2/3/4/5/7/9/10/11/13 ------------------------------

test("formatElapsedSince counts up from finished_at (TIMER-3)", () => {
  const now = Date.now();
  assert.equal(formatElapsedSince(null, now), "Done");
  assert.equal(formatElapsedSince(now - 20 * 1000, now), "Done — just now");
  assert.equal(formatElapsedSince(now - 3 * 60000, now), "Done — 3 min ago");
  assert.equal(
    formatElapsedSince(now - 65 * 60000, now),
    "Done — 1 h 5 min ago"
  );
  assert.equal(
    formatElapsedSince(now - 120 * 60000, now),
    "Done — 2 h ago"
  );
});

test("formatStartTime renders a clock time (TIMER-11)", () => {
  const ms = new Date(2026, 10, 8, 18, 2, 0).getTime();
  assert.equal(formatStartTime(ms), "6:02 PM");
  assert.equal(formatStartTime(null), "");
});

test("startedAtMs prefers the sensor attribute, else derives it (TIMER-11)", () => {
  const now = Date.now();
  const withAttr = discoverTimers([
    {
      entity_id: "sensor.satellite_timer_t1",
      state: "active",
      attributes: {
        timer_id: "t1",
        timer_name: "Pasta",
        duration: 900,
        remaining: 600,
        finishes_at: new Date(now + 600 * 1000).toISOString(),
        started_at: new Date(now - 300 * 1000).toISOString(),
      },
    },
  ])[0];
  assert.equal(startedAtMs(withAttr, now, null), now - 300 * 1000);
  const active = discoverTimers([
    timerState("t2", "Rice", "active", { remaining: 600, duration: 900 }),
  ])[0];
  const derived = startedAtMs(active, now, null);
  assert.ok(Math.abs(derived - (now - 300 * 1000)) < 2000);
  const finished = discoverTimers([
    timerState("t3", "Tea", "finished", {
      duration: 600,
      finishedAt: new Date(now - 180 * 1000).toISOString(),
    }),
  ])[0];
  assert.equal(startedAtMs(finished, now, null), now - 780 * 1000);
});

test("detectDevice: unified §3.7 rule — panel signature first, bare width never hub (TIMER-2)", () => {
  const base = {
    userAgent:
      "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
  };
  // Hub with a mouse attached (fine + touch at 1920x1200) is the hub,
  // not a desktop — the old card said desktop here.
  assert.deepEqual(
    detectDevice({ ...base, width: 1920, height: 1200, coarsePointer: false, finePointer: true, touch: true }),
    { category: "hub", orientation: "landscape" }
  );
  // 1920x1200 with a mouse only (no touch at all) is still the hub:
  // the exact panel signature wins over the pointer.
  assert.deepEqual(
    detectDevice({ ...base, width: 1920, height: 1200, coarsePointer: false, finePointer: true, touch: false }),
    { category: "hub", orientation: "landscape" }
  );
  // Bare width >=1600 + landscape must NEVER imply hub (FORBIDDEN):
  // a 1920x1080 desktop monitor with a mouse is a desktop.
  assert.deepEqual(
    detectDevice({ ...base, width: 1920, height: 1080, coarsePointer: false, finePointer: true, touch: false }),
    { category: "desktop", orientation: "landscape" }
  );
  assert.deepEqual(
    detectDevice({ ...base, width: 1680, height: 1050, coarsePointer: false, finePointer: true, touch: false }),
    { category: "desktop", orientation: "landscape" }
  );
  // Touch + hub-size below the panel signature is still the hub.
  assert.deepEqual(
    detectDevice({ ...base, width: 1920, height: 1080, coarsePointer: true, finePointer: false, touch: true }),
    { category: "hub", orientation: "landscape" }
  );
  // A coarse pointer on a tablet-size landscape screen is a tablet
  // (DESIGN-9: 1024x768 must NOT fall to desktop — and not to
  // mobile either).
  assert.deepEqual(
    detectDevice({ ...base, width: 1024, height: 768, coarsePointer: true, finePointer: false, touch: true }),
    { category: "tablet", orientation: "landscape" }
  );
});

test("readRecipeContext parses ?from_recipe= and ?step= (TIMER-10)", () => {
  assert.deepEqual(readRecipeContext("?from_recipe=rec-1&step=3"), {
    recipeId: "rec-1",
    step: "3",
  });
  assert.deepEqual(readRecipeContext("?from_recipe=rec-1"), {
    recipeId: "rec-1",
    step: null,
  });
  assert.equal(readRecipeContext(""), null);
  assert.equal(readRecipeContext("?kiosk"), null);
});

test("the full timer name renders — never truncated (TIMER-1)", () => {
  const longName = "Chicken in oven - do not forget";
  const unbroken =
    "VeryLongTimerNameThatGoesOnAndOnAndOnBeyondFortyCharacters";
  const card = timerCard([
    timerState("t1", longName, "active", {}),
    timerState("t2", unbroken, "active", {}),
  ]);
  assert.equal(
    card.shadowRoot.querySelector('[data-name="t1"]').textContent,
    longName
  );
  assert.equal(
    card.shadowRoot.querySelector('[data-name="t2"]').textContent,
    unbroken
  );
  const html = card.shadowRoot.innerHTML;
  assert.doesNotMatch(html, /Chicken in oven -…/);
});

test("a finished timer shows Done — N min ago, counting up (TIMER-3)", () => {
  const card = timerCard([
    timerState("t1", "Tea", "finished", {
      finishedAt: new Date(Date.now() - 3 * 60000).toISOString(),
    }),
  ]);
  assert.equal(
    card.shadowRoot.querySelector('[data-state="t1"]').textContent,
    "Done — 3 min ago"
  );
  assert.equal(
    card.shadowRoot.querySelector('[data-remaining="t1"]').textContent,
    "Done"
  );
});

test("duplicate names get distinct start times; lone timers get none (TIMER-11)", () => {
  const card = timerCard([
    timerState("t1", "Pasta", "active", { remaining: 600, duration: 900 }),
    timerState("t2", "Pasta", "active", { remaining: 300, duration: 900 }),
    timerState("t3", "Rice", "active", { remaining: 600, duration: 900 }),
  ]);
  const first = card.shadowRoot.querySelector('[data-started="t1"]').textContent;
  const second = card.shadowRoot.querySelector('[data-started="t2"]').textContent;
  assert.match(first, /^Started \d{1,2}:\d{2} [AP]M$/);
  assert.match(second, /^Started \d{1,2}:\d{2} [AP]M$/);
  assert.notEqual(first, second);
  assert.equal(
    card.shadowRoot.querySelector('[data-started="t3"]').textContent,
    ""
  );
});

test("the empty state names the voice paths and offers preset chips (TIMER-4/14)", () => {
  const card = timerCard([]);
  const html = card.shadowRoot.innerHTML;
  assert.match(html, /Okay Nabu, set a 10 minute pasta timer/);
  assert.match(html, /add 5 more minutes to the pasta timer/);
  const chips = card.shadowRoot.querySelectorAll('[data-action="preset"]');
  assert.equal(chips.length, 4);
  assert.deepEqual(
    chips.map((c) => c.dataset.minutes),
    ["5", "10", "15", "30"]
  );
});

test("a preset chip starts a timer by duration (TIMER-4)", async () => {
  const hass = fakeHass([]);
  const card = new TimerScreenCard();
  card.setConfig({});
  card.hass = hass;
  card._onClick({
    target: {
      closest: () => ({ dataset: { action: "preset", minutes: "10" } }),
    },
  });
  await sleep(20);
  assert.deepEqual(hass.calls, [
    ["satellite_timers", "start_timer", { name: "10 minute timer", duration: 600 }],
  ]);
});

test("the palette is the warm §3.3 set — no HA blue, amber, or #db4437 (TIMER-5)", () => {
  const card = timerCard([timerState("t1", "Pasta", "active", {})]);
  const css = card.shadowRoot.querySelector("style").textContent;
  for (const color of ["#C2410C", "#B45309", "#757575", "#B71C1C"]) {
    assert.ok(css.includes(color), `CSS should contain ${color}`);
  }
  for (const gone of ["#03a9f4", "#fb8c00", "#db4437", "#e65100"]) {
    assert.ok(!css.includes(gone), `CSS should not contain ${gone}`);
  }
});

test("past 12 timers the view degrades: many class + finished collapse (TIMER-7)", () => {
  const states = [];
  for (let i = 0; i < 12; i++) {
    states.push(timerState(`a${i}`, `Timer ${i}`, "active", { remaining: 600 + i }));
  }
  states.push(timerState("done1", "Tea", "finished", {}));
  const card = timerCard(states);
  const screen = card.shadowRoot.querySelector(".screen");
  assert.ok(screen.classList.contains("many"));
  const finishedDial = [...card.shadowRoot.querySelectorAll(".dial")].find(
    (d) => d.dataset.timerId === "done1"
  );
  assert.ok(finishedDial.classList.contains("compact-mode"));
  assert.equal(
    card.shadowRoot.querySelector('[data-compact-name="done1"]').textContent,
    "Tea"
  );
  // At 12 timers exactly there is no degradation.
  const twelve = timerCard(states.slice(0, 12));
  assert.ok(!twelve.shadowRoot.querySelector(".screen").classList.contains("many"));
});

test("a dial tap clears a pending Cancel arm (TIMER-9)", async () => {
  const hass = fakeHass([timerState("t1", "Pasta", "active", {})]);
  const card = new TimerScreenCard();
  card.setConfig({});
  card.hass = hass;
  const click = (action, timer) =>
    card._onClick({
      target: { closest: () => ({ dataset: { action, timer } }) },
    });
  click("cancel", "t1"); // arms
  await sleep(20);
  assert.deepEqual(hass.calls, []);
  click("dial", "t1"); // pauses AND clears the arm
  await sleep(20);
  assert.deepEqual(hass.calls, [
    ["satellite_timers", "pause_timer", { timer_id: "t1" }],
  ]);
  click("cancel", "t1"); // arms again instead of cancelling outright
  await sleep(20);
  assert.deepEqual(hass.calls, [
    ["satellite_timers", "pause_timer", { timer_id: "t1" }],
  ]);
  assert.equal(
    card.shadowRoot.querySelector(".cancel").textContent,
    "Tap again to cancel"
  );
});

test("cancelling shows a 5 s Undo toast; Undo re-creates the timer (TIMER-13)", async () => {
  const hass = fakeHass([timerState("t1", "Pasta", "active", { duration: 900 })]);
  const card = new TimerScreenCard();
  card.setConfig({});
  card.hass = hass;
  const click = (action, timer) =>
    card._onClick({
      target: { closest: () => ({ dataset: { action, timer } }) },
    });
  click("cancel", "t1");
  await sleep(20);
  click("cancel", "t1");
  await sleep(20);
  assert.deepEqual(hass.calls, [
    ["satellite_timers", "cancel_timer", { timer_id: "t1" }],
  ]);
  const toast = card.shadowRoot.querySelector(".toast");
  assert.equal(toast.hidden, false);
  assert.match(toast.textContent, /Pasta timer cancelled/);
  assert.match(toast.textContent, /Undo/);
  click("undo-cancel", undefined);
  await sleep(20);
  assert.deepEqual(hass.calls, [
    ["satellite_timers", "cancel_timer", { timer_id: "t1" }],
    ["satellite_timers", "start_timer", { name: "Pasta", duration: 900 }],
  ]);
  assert.equal(toast.hidden, true);
});

test("Back to recipe shows with ?from_recipe= and navigates back at the same step (TIMER-10)", () => {
  const assigned = [];
  stubWindow(HUB_WINDOW);
  globalThis.window.location = {
    search: "?from_recipe=rec-1&step=3",
    pathname: "/family-hub/timers",
    assign: (url) => assigned.push(url),
  };
  try {
    const card = timerCard([timerState("t1", "Pasta", "active", {})]);
    const bar = card.shadowRoot.querySelector(".back-bar");
    assert.equal(bar.hidden, false);
    card._onClick({
      target: { closest: () => ({ dataset: { action: "back-recipe" } }) },
    });
    assert.deepEqual(assigned, ["/family-hub/recipes?recipe=rec-1&step=3"]);
  } finally {
    clearWindow();
  }
  // Without the param there is no back bar.
  const plain = timerCard([timerState("t1", "Pasta", "active", {})]);
  assert.equal(plain.shadowRoot.querySelector(".back-bar").hidden, true);
});

// -- tablet category (DESIGN-9, Phase 7) -----------------------------------
// The fourth device category: hub / desktop / tablet / mobile. The
// tablet band is a touch screen 768-1024px wide, either orientation,
// plus the tablet user agents (iPad; Android without a "Mobi" token).

test("detectDevice: tablet category — band, iPad, Android tablet (DESIGN-9)", () => {
  // The spec viewports: 1024x768 landscape and 768x1024 portrait.
  assert.equal(
    detectDevice({ width: 1024, height: 768, coarsePointer: true, touch: true }).category,
    "tablet"
  );
  assert.equal(
    detectDevice({ width: 768, height: 1024, coarsePointer: true, touch: true }).category,
    "tablet"
  );
  // The other spec tablet viewports: 900x1440, 768x1366.
  assert.equal(
    detectDevice({ width: 900, height: 1440, coarsePointer: true, touch: true }).category,
    "tablet"
  );
  assert.equal(
    detectDevice({ width: 768, height: 1366, coarsePointer: true, touch: true }).category,
    "tablet"
  );
  // iPad user agent is a tablet whatever the viewport (even a wide
  // one — the UA wins over size, as the mobile UA does for phones).
  assert.equal(
    detectDevice({
      width: 1440,
      height: 900,
      userAgent:
        "Mozilla/5.0 (iPad; CPU OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
    }).category,
    "tablet"
  );
  // Android tablet: "Android" with no "Mobi"/"Mobile" token.
  assert.equal(
    detectDevice({
      width: 1280,
      height: 800,
      coarsePointer: true,
      touch: true,
      userAgent:
        "Mozilla/5.0 (Linux; Android 13; SM-X710) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    }).category,
    "tablet"
  );
  // Android phone: the same OS string WITH the "Mobile" token.
  assert.equal(
    detectDevice({
      width: 1280,
      height: 800,
      coarsePointer: true,
      touch: true,
      userAgent:
        "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36",
    }).category,
    "mobile"
  );
  // Boundaries of the band: 767px wide is mobile, 1025px wide with a
  // coarse pointer (and no hub-size landscape) is mobile too.
  assert.equal(
    detectDevice({ width: 767, height: 1024, coarsePointer: true, touch: true }).category,
    "mobile"
  );
  assert.equal(
    detectDevice({ width: 1025, height: 768, coarsePointer: true, touch: true }).category,
    "mobile"
  );
  // Open question OQ-7: a touchscreen laptop at 1366x768 (coarse,
  // outside the tablet band) still reads as mobile — recorded, not
  // silently decided.
  assert.equal(
    detectDevice({ width: 1366, height: 768, coarsePointer: true, touch: true }).category,
    "mobile"
  );
  // A huge portrait touch screen is mobile, not the hub (the hub is
  // landscape) — and not a tablet either (1200px is outside the band).
  assert.equal(
    detectDevice({ width: 1200, height: 1920, coarsePointer: true, touch: true }).category,
    "mobile"
  );
});

// -- Phase 7 CSS: tablet rules, reduced motion, no layout animation ------

test("Phase 7 CSS: tablet rules, reduced motion, no layout-property animation", () => {
  // Tablet (DESIGN-9): 64px controls, preset chips shown (TIMER-4).
  assert.match(CARD_CSS, /\.screen\.dev-tablet \.controls button \{ min-height: 64px;/);
  assert.match(CARD_CSS, /\.screen\.dev-tablet \.preset-chips \{ display: flex; \}/);
  assert.match(CARD_CSS, /\.screen\.dev-tablet \.name \{ font-size: 20px; \}/);
  assert.match(CARD_CSS, /\.screen\.dev-tablet \.state, \.screen\.dev-tablet \.hint \{ font-size: 18px; \}/);
  // DESIGN-6: no layout-property animation anywhere — the dial
  // button's height/width transition is gone.
  assert.ok(!CARD_CSS.includes("transition: height"));
  assert.ok(!CARD_CSS.includes("transition: width"));
  // Reduced motion (DESIGN-6, WCAG 2.2): pulses and entries stop.
  assert.ok(CARD_CSS.includes("@media (prefers-reduced-motion: reduce)"));
  assert.match(CARD_CSS, /\.dial\.soon \.ring \{ animation: none; \}/);
  // The soon pulse itself still exists in the base rules (DESIGN-8
  // guardrail — reduced motion only disables it under the media query).
  assert.ok(CARD_CSS.includes("animation: soon-pulse 1.6s ease-in-out infinite"));
});

// -- Phase 7 scroll persistence (DESIGN-7) --------------------------------

test("parseTimerScroll/serializeTimerScroll: round-trip, garbage -> null, negatives -> 0", () => {
  assert.equal(TIMER_SCROLL_KEY, "family-hub:timers:scroll");
  const state = { page: 240, screen: 120 };
  assert.deepEqual(parseTimerScroll(serializeTimerScroll(state)), state);
  assert.equal(parseTimerScroll(null), null);
  assert.equal(parseTimerScroll(""), null);
  assert.equal(parseTimerScroll("not json"), null);
  assert.equal(parseTimerScroll("[1,2]"), null);
  assert.deepEqual(parseTimerScroll("{}"), { page: 0, screen: 0 });
  assert.deepEqual(parseTimerScroll('{"page": -5, "screen": 12.9}'), {
    page: 0,
    screen: 12,
  });
});

// -- Phase 7 follow-up: Fix 2 (finished-dial sub-text) ----------------------
// At 360x640 the finished dial's two sub-lines ("Done — N min ago",
// "Tap to dismiss") were wider than the ring's chord at their height,
// so the ring crossed characters. Mobile-only fix: smaller sub-text
// (14px) and a taller portrait dial (300px, so the ring is ~200px
// instead of 154px). The hub and tablet type minima (TIMER-6) are
// untouched.

test("Phase 7 follow-up (Fix 2): mobile sub-text is 14px and the portrait dial is taller", () => {
  assert.match(
    CARD_CSS,
    /\.screen\.dev-mobile \.state, \.screen\.dev-mobile \.hint \{ font-size: 14px; \}/
  );
  assert.match(
    CARD_CSS,
    /\.screen\.dev-mobile\.orient-portrait \.dial \{ min-height: 300px; \}/
  );
});

test("Phase 7 follow-up (Fix 2): hub and tablet type sizes are unchanged (TIMER-6)", () => {
  // Base (hub) rules: name 20px, state/hint 18px.
  assert.match(CARD_CSS, /\.name \{\n    color: #212121;\n    font-size: 20px;/);
  assert.match(CARD_CSS, /\.state \{ color: #57534E; font-size: 18px; line-height: 1\.3; \}/);
  assert.match(CARD_CSS, /\.hint \{ color: #57534E; font-size: 18px; line-height: 1\.3; \}/);
  // Tablet keeps the hub minima.
  assert.match(
    CARD_CSS,
    /\.screen\.dev-tablet \.state, \.screen\.dev-tablet \.hint \{ font-size: 18px; \}/
  );
  // Mobile name is unchanged at 18px — only the sub-lines shrank.
  assert.match(CARD_CSS, /\.screen\.dev-mobile \.name \{ font-size: 18px; \}/);
});

// -- Phase 7 follow-up: Fix 3 (nav strip, NAV-1) ---------------------------
// The Timers view had no nav strip at all (Phase 3 never delivered the
// Phase 1+2 contract item). The strip now renders inside the timer
// card, mirroring the recipe card: same views, labels, paths and
// active rule; pushState + location-changed navigation, no reload.

test("Phase 7 follow-up (Fix 3): NAV_STRIP_VIEWS matches the other views' strip", () => {
  assert.deepEqual(
    NAV_STRIP_VIEWS.map((view) => [view.key, view.label, view.path]),
    [
      ["home", "Home", "/family-hub"],
      ["timers", "Timers", "/family-hub/timers"],
      ["photos", "Photos", "/family-hub/family-photos"],
      ["recipes", "Recipes", "/family-hub/recipes"],
    ]
  );
});

test("Phase 7 follow-up (Fix 3): the strip renders first in the screen, Timers active", () => {
  const card = timerCard([timerState("t1", "Pasta", "active", {})]);
  const screen = card.shadowRoot.querySelector(".screen");
  const strip = card.shadowRoot.querySelector(".nav-strip");
  assert.ok(strip, "the strip renders");
  assert.equal(screen.children[0], strip, "the strip is first in the DOM (tab order)");
  const buttons = card.shadowRoot.querySelectorAll(".nav-strip-button");
  assert.equal(buttons.length, 4);
  assert.deepEqual(
    buttons.map((button) => button.textContent),
    ["Home", "Timers", "Photos", "Recipes"]
  );
  assert.deepEqual(
    buttons.map((button) => button.dataset.path),
    ["/family-hub", "/family-hub/timers", "/family-hub/family-photos", "/family-hub/recipes"]
  );
  const timersButton = buttons[1];
  assert.ok(timersButton.classList.contains("active"));
  assert.equal(timersButton.getAttribute("aria-current"), "page");
  assert.ok(!buttons[0].classList.contains("active"));
  assert.ok(!buttons[2].classList.contains("active"));
  assert.ok(!buttons[3].classList.contains("active"));
  // The strip renders with no timers too (empty state).
  const empty = timerCard([]);
  assert.equal(empty.shadowRoot.querySelectorAll(".nav-strip-button").length, 4);
});

test("Phase 7 follow-up (Fix 3): a strip tap navigates (pushState + location-changed, no reload)", async () => {
  const card = timerCard([timerState("t1", "Pasta", "active", {})]);
  // No window in node: _navigate records the path and returns.
  card._onClick({
    target: {
      closest: () => ({ dataset: { action: "nav", path: "/family-hub/recipes" } }),
    },
  });
  assert.equal(card._lastNavigation, "/family-hub/recipes");

  // With a stubbed window the pushState + dispatch run.
  const pushed = [];
  const dispatched = [];
  stubWindow(HUB_WINDOW);
  globalThis.window.history = {
    pushState: (_state, _title, url) => pushed.push(url),
  };
  globalThis.window.dispatchEvent = (event) => dispatched.push(event.type);
  globalThis.CustomEvent = class {
    constructor(type) {
      this.type = type;
    }
  };
  try {
    card._navigate("/family-hub/family-photos");
    assert.equal(card._lastNavigation, "/family-hub/family-photos");
    assert.deepEqual(pushed, ["/family-hub/family-photos"]);
    assert.deepEqual(dispatched, ["location-changed"]);
  } finally {
    delete globalThis.CustomEvent;
    clearWindow();
  }
  await sleep(0);
});

test("Phase 7 follow-up (Fix 3): strip CSS — fixed to the viewport bottom, hidden on desktop", () => {
  // Fixed in both orientations (not sticky): the screen is a scroll
  // container and the card can grow past the viewport, so a sticky
  // strip would sit at the content end, below the fold at scroll top.
  // Fixed pins to the viewport bottom in both scroll modes, and the
  // screen reserves the strip's height in bottom padding.
  assert.match(
    CARD_CSS,
    /\.nav-strip \{\n    position: fixed;\n    bottom: 0;\n    left: 24px;\n    right: 24px;/
  );
  assert.ok(
    CARD_CSS.includes("padding-bottom: calc(24px + var(--nav-strip-height, 84px));")
  );
  // Desktop only: hidden (HA tabs already exist and work there).
  assert.match(CARD_CSS, /\.screen\.dev-desktop \.nav-strip \{ display: none; \}/);
  // Active state: the accent background, heavier weight.
  assert.ok(
    CARD_CSS.includes(
      ".nav-strip-button.active { background: #C2410C; border-color: #C2410C; color: #fff; font-weight: 800; }"
    )
  );
  // Per-category sizes match the recipe card (hub 64px/24px, tablet
  // 56px/20px, mobile 48px/16px nowrap).
  assert.match(
    CARD_CSS,
    /\.screen\.dev-tablet \.nav-strip-button \{ min-height: 56px; padding: 10px 16px; font-size: 20px; \}/
  );
  assert.match(
    CARD_CSS,
    /\.screen\.dev-mobile \.nav-strip-button \{ min-height: 48px; padding: 8px 6px; font-size: 16px; white-space: nowrap; \}/
  );
  // The strip never overlaps the scroll hint or the Undo toast: both
  // clear it by the strip's height.
  assert.ok(
    CARD_CSS.includes("bottom: calc(16px + var(--nav-strip-height, 84px));")
  );
  assert.ok(CARD_CSS.includes(".screen.dev-hub ~ .toast { bottom: 108px; }"));
  assert.ok(CARD_CSS.includes(".screen.dev-tablet ~ .toast { bottom: 100px; }"));
  assert.ok(CARD_CSS.includes(".screen.dev-mobile ~ .toast { bottom: 84px; }"));
  // Reduced motion: the press feedback is instant.
  assert.match(CARD_CSS, /\.nav-strip-button:active \{ transform: none; \}/);
});
