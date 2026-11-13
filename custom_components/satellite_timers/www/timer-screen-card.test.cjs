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
    // Attributes may be name="value" or bare (hidden, disabled):
    // a bare `hidden` sets the hidden property, as the browser does.
    for (const attrMatch of attrs.matchAll(/([\w-]+)(?:="([^"]*)")?/g)) {
      const name = attrMatch[1];
      const decoded = decodeEntities(attrMatch[2] ?? "");
      if (name === "class") {
        for (const cls of decoded.split(/\s+/)) if (cls) el.classList.add(cls);
      } else if (name === "hidden") {
        el.hidden = true;
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
    // Inline styles, modelled on CSSStyleDeclaration just far enough
    // for the card: setProperty/removeProperty/getPropertyValue for
    // custom properties (the --dial-max fit var), plus plain property
    // assignment (the ring's strokeDashoffset).
    this.style = {
      _props: {},
      setProperty(name, value) {
        this._props[name] = String(value);
      },
      removeProperty(name) {
        delete this._props[name];
      },
      getPropertyValue(name) {
        return this._props[name] || "";
      },
    };
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

  insertBefore(child, before) {
    if (child.parentNode) child.parentNode.removeChild(child);
    child.parentNode = this;
    const index = before ? this.children.indexOf(before) : -1;
    if (index >= 0) this.children.splice(index, 0, child);
    else this.children.push(child);
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
    // Tests may pin a rect (this._rect) to drive the scroll-badge
    // gate; otherwise the element sits at the origin with no size.
    if (this._rect) return { ...this._rect };
    return { left: 0, top: 0, right: 0, bottom: 0, width: 0, height: 0 };
  }

  scrollIntoView(options) {
    // Recorded so tests can assert the chip-tap scroll alignment.
    if (!this._scrollIntoViewCalls) this._scrollIntoViewCalls = [];
    this._scrollIntoViewCalls.push(options);
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
  formatElapsedSince,
  formatStartTime,
  sensorRemaining,
  recentlyFinished,
  FINISHED_FORCE_EXPAND_MS,
  PENDING_START_TIMEOUT_MS,
  discoverTimers,
  gridColumnsFor,
  // R3 (RB-R3-3): voiceUnavailable is DELETED with the voice-down
  // display it drove — the duration-picker helpers replace it here.
  clampDurationPart,
  parseDurationParts,
  durationTotalSeconds,
  formatDurationName,
  DURATION_MAX_HOURS,
  DURATION_FIELD_MAX,
  readTimersPin,
  readTimersArrival,
  CORE_RESOURCE_PATH,
  CORE_LOOKUP_RETRY_THROTTLE_MS,
  CORE_LOOKUP_INTERVAL_MS,
  _setCoreForTests,
  timerBadgeState,
  timerBadgeFromHassStates,
  parseNightSetting,
  readNightSetting,
  isNightNow,
  readBackRecipeContext,
  readRecipeContext,
  NAV_STRIP_VIEWS,
  TimerScreenCard,
  CARD_CSS,
  TIMER_SCROLL_KEY,
  parseTimerScroll,
  serializeTimerScroll,
} = require("./timer-screen-card.js");

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// core.js is an ES module in the pi-family-hub repo — node loads it
// through a byte-identical .mjs copy (same pattern as the core suite).
// The timer card imports core for identity, so its tests load the
// real core rather than a stub.
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const timerModule = require("./timer-screen-card.js");
// FAMILY_HUB_CORE_PATH overrides the core location (default: the
// shared pi-family-hub worktree's dashboard core).
const corePath =
  process.env.FAMILY_HUB_CORE_PATH ||
  path.join(
    __dirname,
    "../../../../local-git/pi-family-hub/installer/dashboard/family-hub-core.js"
  );
assert.ok(
  fs.existsSync(corePath),
  `family-hub-core.js missing at ${corePath} — the timer card imports it for identity`
);
const coreTmp = fs.mkdtempSync(path.join(os.tmpdir(), "family-hub-timer-test-"));
const coreMjs = path.join(coreTmp, "family-hub-core.mjs");
fs.copyFileSync(corePath, coreMjs);
const core = require(coreMjs);
// The core the tests load must be the R1-3-or-later core: a stale
// pre-R1-3 core (no deviceFromIdentity) used to fail ~30 tests with
// "deviceFromIdentity is not a function". Fail once, naming the cause
// and the override, instead of failing every identity test cryptically.
assert.equal(
  typeof core.deviceFromIdentity,
  "function",
  `family-hub-core.js at ${corePath} predates deviceFromIdentity — ` +
    `the pi-family-hub worktree is on a pre-R1-3 branch; switch it to ` +
    `rebuild/r1, or point FAMILY_HUB_CORE_PATH at a core that exports it`
);
assert.equal(
  typeof core.checkBridgeContract,
  "function",
  `family-hub-core.js at ${corePath} predates checkBridgeContract — ` +
    `the pi-family-hub worktree is on a pre-R1-5 core; switch it to ` +
    `rebuild/r1, or point FAMILY_HUB_CORE_PATH at a core that exports it`
);
const { deviceFromIdentity, HUB_BROWSER_ID } = core;

// Identity helpers for the mapping tests: deviceFromIdentity takes an
// identify() result + viewport. Wall identities are hub at ANY size
// (§4 — no size check, ever); unknown identities fall to the viewport
// + pointer mapping (core's deviceFromIdentity, one copy).
function wallIdentity() {
  return { kind: "wall", layout: {} };
}
function unknownIdentity(layout) {
  return { kind: "unknown", layout: layout || {} };
}

// Inject the real core into the card under test. The wall DOM test
// registers the wall's id via the sensor leg: the shipped core still
// carries the unsubstituted __HUB_BROWSER_ID__ placeholder, so the
// test registers THAT id (the wizard substitutes a real id at deploy;
// the mechanism — exact match → wall — is what's under test).
function injectCore() {
  _setCoreForTests(core);
}
function resetCore() {
  _setCoreForTests(null);
}
function wallBrowserPathState(pathname) {
  return {
    entity_id: `sensor.${HUB_BROWSER_ID}_browser_path`,
    state: pathname,
    attributes: {},
  };
}

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
      // R1-5: fixtures are contract-v2 sensors (the bridge gate admits
      // only contract_version 2). Pass contractVersion: 1 (or null) for
      // a v1-shaped sensor in a mismatch test — `in` (not ??) so an
      // explicit null is kept, and the gate reads it as mismatched.
      contract_version:
        "contractVersion" in overrides ? overrides.contractVersion : 2,
      duration,
      remaining,
      finishes_at:
        overrides.finishesAt ??
        (state === "active"
          ? new Date(Date.now() + remaining * 1000).toISOString()
          : null),
      finished_at: overrides.finishedAt ?? null,
      started_at: overrides.startedAt ?? null,
    },
  };
}

function fakeHass(states) {
  const calls = [];
  const wsCalls = [];
  return {
    calls,
    wsCalls,
    states: states || [],
    callService: async (domain, service, data) => {
      calls.push([domain, service, data]);
    },
    // Websocket path the card uses for return_response calls (§8.1)
    // and the core resource lookup. start_timer answers with the
    // fixed new-timer entity id; the resource list is empty.
    callWS: async (msg) => {
      wsCalls.push(msg);
      if (msg && msg.type === "lovelace/resources/list") return [];
      if (msg && msg.type === "call_service") {
        return { response: { entity_id: "sensor.satellite_timer_new10" } };
      }
      return null;
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

test("formatRemaining renders mm:ss and h:mm:ss (core's formatter, R2 §19)", () => {
  // The card no longer defines formatRemaining — it imports core's.
  // The core version renders the same shapes the card used to.
  assert.equal(core.formatRemaining(0), "0:00");
  assert.equal(core.formatRemaining(59), "0:59");
  assert.equal(core.formatRemaining(600), "10:00");
  assert.equal(core.formatRemaining(3661), "1:01:01");
  assert.equal(core.formatRemaining(-5), "0:00");
});

test("discoverTimers keeps only satellite timer sensors in a live state, running first (Wave 1 sort)", () => {
  const states = [
    timerState("t-late", "Pasta", "active", { remaining: 700 }),
    { entity_id: "sensor.other", state: "active", attributes: {} },
    timerState("t-soon", "Rice", "active", { remaining: 120 }),
    timerState("t-idle", "Idle", "idle", {}),
    timerState("t-done", "Done", "finished", {}),
    timerState("t-paused", "Tea", "paused", { remaining: 300 }),
  ];
  const timers = discoverTimers(states);
  // Wave 1 (T-IMPL-2/3): active soonest-first, then paused, then
  // finished — the finished-first defect (F-T20) is gone.
  assert.deepEqual(
    timers.map((t) => t.timer_id),
    ["t-soon", "t-late", "t-paused", "t-done"]
  );
  assert.equal(timers[0].name, "Rice");
});

test("discoverTimers drops a ringing sensor entirely (R2, §5.1 — the state is deleted)", () => {
  const states = [
    timerState("t-old", "Old", "finished", {
      finishedAt: new Date(Date.now() - 600000).toISOString(),
    }),
    timerState("t-ring", "Ring", "ringing", {
      finishedAt: new Date(Date.now() - 10000).toISOString(),
    }),
    timerState("t-new", "New", "finished", {
      finishedAt: new Date(Date.now() - 30000).toISOString(),
    }),
    timerState("t-run", "Run", "active", { remaining: 60 }),
  ];
  const timers = discoverTimers(states);
  // A `ringing` sensor is not a timer any more — it is a contract
  // mismatch (the R1-5 gate test), so discoverTimers never sees one.
  assert.deepEqual(
    timers.map((t) => t.timer_id),
    ["t-run", "t-new", "t-old"]
  );
});

test("sensorRemaining reads the sensor's remaining attribute (OQ-5)", () => {
  const timer = discoverTimers([
    timerState("t1", "Pasta", "active", { remaining: 59 }),
  ])[0];
  assert.equal(sensorRemaining(timer), 59);
  assert.equal(sensorRemaining(null), 0);
  assert.equal(sensorRemaining({}), 0);
});

test("TimerClock drives the card countdown: active ticks, paused stays frozen, stale freezes + marks (R2, §14)", async () => {
  injectCore();
  try {
    const card = timerCard([
      timerState("t1", "Pasta", "active", { remaining: 300, duration: 600 }),
      timerState("t2", "Rice", "paused", { remaining: 300, duration: 600 }),
    ]);
    const timeText = (id) =>
      card.shadowRoot.querySelector(`[data-remaining="${id}"]`).textContent;
    const staleHidden = (id) =>
      card.shadowRoot.querySelector(`[data-stale="${id}"]`).hidden;
    // The active clock floors its remaining, so a fresh 300 s push
    // renders "4:59" within milliseconds (300 − 0.00x s, floored) —
    // the same floor the old finishes_at derivation had. The paused
    // timer reads the frozen server value exactly.
    assert.ok(["5:00", "4:59"].includes(timeText("t1")), timeText("t1"));
    assert.equal(timeText("t2"), "5:00");
    assert.equal(staleHidden("t1"), true);
    assert.equal(staleHidden("t2"), true);
    // No new hass push: the active clock counts down on the monotonic
    // clock; the paused value is the frozen server remaining.
    await sleep(1200);
    card._updateTimes();
    assert.equal(timeText("t1"), "4:58");
    assert.equal(timeText("t2"), "5:00");
    assert.equal(staleHidden("t1"), true);
    assert.equal(staleHidden("t2"), true);
    // Past the 5 s staleness bound with no push: the active countdown
    // freezes at the boundary (300 − 5 = 295 s) and the stale mark
    // shows; the paused timer is never stale and never moves.
    await sleep(4600);
    card._updateTimes();
    assert.equal(timeText("t1"), "4:55");
    assert.equal(staleHidden("t1"), false);
    assert.equal(
      card.shadowRoot.querySelector('[data-stale="t1"]').textContent,
      "Time may be stale"
    );
    assert.equal(timeText("t2"), "5:00");
    assert.equal(staleHidden("t2"), true);
    // A fresh push unfreezes it: the clock re-anchors to the new
    // server remaining.
    card.hass = fakeHass([
      timerState("t1", "Pasta", "active", { remaining: 280, duration: 600 }),
      timerState("t2", "Rice", "paused", { remaining: 300, duration: 600 }),
    ]);
    card._updateTimes();
    // Floor again: the fresh 280 s push renders "4:39" within
    // milliseconds of the push (280 − 0.00x s, floored).
    assert.ok(["4:40", "4:39"].includes(timeText("t1")), timeText("t1"));
    assert.equal(staleHidden("t1"), true);
  } finally {
    resetCore();
  }
});

test("progressFraction is the share of time left (core's helper, R2 §19)", () => {
  assert.equal(core.progressFraction(450, 900), 0.5);
  assert.equal(core.progressFraction(0, 900), 0);
  assert.equal(core.progressFraction(450, 0), 0);
});

// -- gridColumnsFor: columns by viewport class only (Wave 1) ----------
// The count-driven grid is deleted: columns never depend on how many
// timers exist.

test("gridColumnsFor on the hub: 3 landscape, 2 portrait", () => {
  assert.equal(gridColumnsFor("hub", "landscape", 1920), 3);
  assert.equal(gridColumnsFor("hub", "portrait", 1200), 2);
});

test("gridColumnsFor on mobile: 1 portrait, 2 landscape", () => {
  assert.equal(gridColumnsFor("mobile", "portrait", 390), 1);
  assert.equal(gridColumnsFor("mobile", "landscape", 844), 2);
});

test("gridColumnsFor on tablet/desktop: by width (>=1600 -> 3, 900-1599 -> 2, <900 -> 1)", () => {
  assert.equal(gridColumnsFor("tablet", "landscape", 1024), 2);
  assert.equal(gridColumnsFor("tablet", "portrait", 768), 1);
  assert.equal(gridColumnsFor("desktop", "landscape", 1920), 3);
  assert.equal(gridColumnsFor("desktop", "landscape", 1366), 2);
  assert.equal(gridColumnsFor("desktop", "landscape", 1024), 2);
  assert.equal(gridColumnsFor("desktop", "portrait", 800), 1);
});

// -- Device detection (shared mechanism; the recipe card carries the
// identical block — see ADAPTIVE-LAYOUT.md) ---------------------------------

test("identity: the wall is hub at any size (TIMER-2, §4)", () => {
  // Kind, not size: the registered wall reads hub whatever its
  // viewport or pointer — a huge landscape touch screen, a mouse-only
  // screen, the rotated wall.
  assert.deepEqual(deviceFromIdentity(wallIdentity(), 1920, 1200), {
    category: "hub",
    orientation: "landscape",
  });
  assert.deepEqual(deviceFromIdentity(wallIdentity(), 1200, 1920), {
    category: "hub",
    orientation: "portrait",
  });
  // The same viewport WITHOUT the wall identity is not the hub: a
  // coarse-only touch screen is a mobile by the pointer rule.
  assert.deepEqual(
    deviceFromIdentity(
      unknownIdentity({ coarsePointer: true, touchPoints: 1 }),
      1920,
      1200
    ),
    { category: "mobile", orientation: "landscape" }
  );
});

test("identity: a mouse is desktop; a phone is mobile in both orientations", () => {
  assert.deepEqual(
    deviceFromIdentity(unknownIdentity({ finePointer: true }), 1440, 900),
    { category: "desktop", orientation: "landscape" }
  );
  assert.deepEqual(
    deviceFromIdentity(
      unknownIdentity({ coarsePointer: true, touchPoints: 1 }),
      390,
      844
    ),
    { category: "mobile", orientation: "portrait" }
  );
  assert.deepEqual(
    deviceFromIdentity(
      unknownIdentity({ coarsePointer: true, touchPoints: 1 }),
      844,
      390
    ),
    { category: "mobile", orientation: "landscape" }
  );
});

test("identity: no user-agent sniffing — a wide viewport is desktop, not mobile", () => {
  // The old "iPhone UA wins over a desktop-sized viewport" rule is
  // gone with the UA sniffing: a 1440px viewport with a fine pointer
  // is a desktop, whatever the UA string used to say. A phone reads
  // mobile by its width (<768px), not its UA.
  assert.deepEqual(
    deviceFromIdentity(unknownIdentity({ finePointer: true }), 1440, 900),
    { category: "desktop", orientation: "landscape" }
  );
  assert.equal(deviceFromIdentity(unknownIdentity({}), 390, 844).category, "mobile");
});

test("R1: no environment reader — _applyDevice keeps the set device with no window", () => {
  // readDeviceEnv is deleted; the no-window early return in
  // _applyDevice is what keeps node tests on the base layout. A card
  // mounted with no window carries no device classes (see the
  // no-browser-environment test below).
  const card = timerCard([timerState("t1", "Pasta", "active", {})]);
  assert.equal(card._device, null);
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
  globalThis.window.location = { search: "", pathname: "/family-hub/timers" };
  injectCore();
  try {
    const card = timerCard([...fiveTimers(), wallBrowserPathState("/family-hub/timers")]);
    const screen = card.shadowRoot.querySelector(".screen");
    assert.ok(screen.classList.contains("dev-hub"));
    assert.ok(screen.classList.contains("orient-landscape"));
    assert.ok(screen.classList.contains("cols-3"));
    // Wave 1: 4 running/paused dials; the finished timer is a row
    // in the finished section, not a fifth dial.
    assert.equal(card.shadowRoot.querySelectorAll(".dial").length, 4);
    assert.equal(card.shadowRoot.querySelectorAll(".finished-row").length, 1);
  } finally {
    resetCore();
    clearWindow();
  }
});

test("the screen carries dev-desktop and two columns at 1440px for five timers", () => {
  stubWindow(DESKTOP_WINDOW);
  injectCore();
  try {
    const card = timerCard(fiveTimers());
    const screen = card.shadowRoot.querySelector(".screen");
    assert.ok(screen.classList.contains("dev-desktop"));
    assert.ok(screen.classList.contains("cols-2"));
  } finally {
    resetCore();
    clearWindow();
  }
});

test("the screen carries dev-mobile + orient-portrait on a phone", () => {
  stubWindow(PHONE_WINDOW);
  injectCore();
  try {
    const card = timerCard(fiveTimers());
    const screen = card.shadowRoot.querySelector(".screen");
    assert.ok(screen.classList.contains("dev-mobile"));
    assert.ok(screen.classList.contains("orient-portrait"));
    assert.ok(screen.classList.contains("cols-1"));
    assert.equal(card.shadowRoot.querySelectorAll(".dial").length, 4);
    assert.equal(card.shadowRoot.querySelectorAll(".finished-row").length, 1);
  } finally {
    resetCore();
    clearWindow();
  }
});

test("a resize re-detects the device and re-grids", () => {
  stubWindow(PHONE_WINDOW);
  injectCore();
  try {
    const card = timerCard(fiveTimers());
    const screen = card.shadowRoot.querySelector(".screen");
    assert.ok(screen.classList.contains("dev-mobile"));
    assert.ok(screen.classList.contains("cols-1"));
    stubWindow(DESKTOP_WINDOW);
    card._onResize();
    assert.ok(screen.classList.contains("dev-desktop"));
    assert.ok(!screen.classList.contains("dev-mobile"));
    assert.ok(screen.classList.contains("cols-2"));
  } finally {
    resetCore();
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
  // No device detected: the single-column base layout.
  assert.ok(screen.classList.contains("cols-1"));
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

test("a chip tap scrolls the new dial to the top of the view (block start), so its name is fully visible", async () => {
  const card = new TimerScreenCard();
  card.setConfig({});
  // Simulate a chip tap: no timers existed before it.
  card._presetBeforeIds = new Set();
  card.hass = fakeHass([timerState("t1", "Pasta", "active", { remaining: 600 })]);
  const dial = card._dialEls.get("t1");
  assert.ok(dial, "dial element exists");
  // The scroll is deferred until the 240ms dial-in entry animation
  // settles: while .entering scales the dial (0.94) its measured top sits
  // ~16px below the settled top, so an immediate scrollIntoView lands
  // 16px too far and clips the name (rig: name top -16px at 844x390).
  assert.deepEqual(dial._scrollIntoViewCalls || [], []);
  await sleep(400);
  // block: "nearest" clipped the name on a taller-than-viewport dial;
  // "start" aligns the dial top so the name is fully visible, and the
  // scroll is instant, not animated.
  assert.deepEqual(dial._scrollIntoViewCalls, [{ block: "start", behavior: "auto" }]);
  assert.ok(dial.classList.contains("pulse"));
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

test("started_at is observed-or-omitted: no derivation, ever (TIMER-11, R2 §5.1)", () => {
  const now = Date.now();
  // With the attribute, the timer carries it through discovery.
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
  assert.equal(withAttr.startedAt, new Date(now - 300 * 1000).toISOString());
  // Without the attribute, there is nothing to derive from: the
  // finished timer below has both finished_at and duration, and the
  // old code derived started_at = finished_at − duration from them.
  // The field stays null — the card shows no start line.
  const finished = discoverTimers([
    timerState("t3", "Tea", "finished", {
      duration: 600,
      finishedAt: new Date(now - 180 * 1000).toISOString(),
    }),
  ])[0];
  assert.equal(finished.startedAt, null);
  const active = discoverTimers([
    timerState("t2", "Rice", "active", { remaining: 600, duration: 900 }),
  ])[0];
  assert.equal(active.startedAt, null);
});

test("identity: unified rule — wall kind first, bare width never hub (TIMER-2, P8-16)", () => {
  // Hub with a mouse attached (fine + touch at 1920x1200) is the hub
  // because its browser id matches — not because of its size.
  assert.deepEqual(deviceFromIdentity(wallIdentity(), 1920, 1200), {
    category: "hub",
    orientation: "landscape",
  });
  // Bare width >=1600 + landscape must NEVER imply hub (FORBIDDEN):
  // a 1920x1080 desktop monitor with a mouse is a desktop.
  assert.deepEqual(
    deviceFromIdentity(unknownIdentity({ finePointer: true }), 1920, 1080),
    { category: "desktop", orientation: "landscape" }
  );
  assert.deepEqual(
    deviceFromIdentity(unknownIdentity({ finePointer: true }), 1680, 1050),
    { category: "desktop", orientation: "landscape" }
  );
  // P8-16: a 1920x1080 touchscreen with a coarse pointer and NO fine
  // pointer is mobile — there is no size rule left that could call
  // it the hub.
  assert.deepEqual(
    deviceFromIdentity(
      unknownIdentity({ coarsePointer: true, touchPoints: 1 }),
      1920,
      1080
    ),
    { category: "mobile", orientation: "landscape" }
  );
  // A coarse pointer on a tablet-size landscape screen is a tablet
  // (DESIGN-9: 1024x768 must NOT fall to desktop — and not to
  // mobile either).
  assert.deepEqual(
    deviceFromIdentity(
      unknownIdentity({ coarsePointer: true, touchPoints: 1 }),
      1024,
      768
    ),
    { category: "tablet", orientation: "landscape" }
  );
  // P8-16 / OQ-7 DECIDED: a touchscreen laptop has a fine pointer
  // (mouse/trackpad) AS WELL as a coarse one, and fine wins — so a
  // 1366x768 touch laptop is a desktop, not a mobile.
  assert.deepEqual(
    deviceFromIdentity(
      unknownIdentity({ coarsePointer: true, finePointer: true, touchPoints: 1 }),
      1366,
      768
    ),
    { category: "desktop", orientation: "landscape" }
  );
  // O2-07: the same for a portrait touch laptop (1080x1920).
  assert.deepEqual(
    deviceFromIdentity(
      unknownIdentity({ coarsePointer: true, finePointer: true, touchPoints: 1 }),
      1080,
      1920
    ),
    { category: "desktop", orientation: "portrait" }
  );
});

test("identity: the rotated wall is the portrait hub (O2-01)", () => {
  // The wall rotated is the SAME wall, so kind wins in either
  // orientation — it gets the hub layout (with the portrait reflow),
  // not the phone layout.
  assert.deepEqual(deviceFromIdentity(wallIdentity(), 1200, 1920), {
    category: "hub",
    orientation: "portrait",
  });
  // 1080x1920 is NOT the wall: a fine pointer is a desktop (O2-07),
  // and a coarse-only touch screen is a mobile outside the band.
  assert.equal(
    deviceFromIdentity(unknownIdentity({ finePointer: true }), 1080, 1920).category,
    "desktop"
  );
  assert.equal(
    deviceFromIdentity(
      unknownIdentity({ coarsePointer: true, touchPoints: 1 }),
      1080,
      1920
    ).category,
    "mobile"
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

test("a finished timer shows as a compact row: Done — N min ago, counting up (TIMER-3)", () => {
  const card = timerCard([
    timerState("t1", "Tea", "finished", {
      finishedAt: new Date(Date.now() - 3 * 60000).toISOString(),
    }),
  ]);
  // Wave 1: finished timers are rows in the "Finished (N)" section,
  // never dials — there is no dial time or dial state line for t1.
  assert.equal(card.shadowRoot.querySelectorAll(".dial").length, 0);
  const row = card.shadowRoot.querySelector(".finished-row");
  assert.ok(row, "the finished row renders");
  assert.equal(row.dataset.timerId, "t1");
  assert.equal(
    card.shadowRoot.querySelector('[data-finished-sub="t1"]').textContent,
    "Done — 3 min ago · Tap to dismiss"
  );
  assert.match(
    card.shadowRoot.querySelector(".finished-title").textContent,
    /Finished \(1\)/
  );
});

test("duplicate names get distinct start times only when observed; lone timers get none (TIMER-11, R2 §5.1)", () => {
  const now = Date.now();
  const card = timerCard([
    timerState("t1", "Pasta", "active", {
      remaining: 600,
      duration: 900,
      startedAt: new Date(now - 600 * 1000).toISOString(),
    }),
    timerState("t2", "Pasta", "active", {
      remaining: 300,
      duration: 900,
      startedAt: new Date(now - 300 * 1000).toISOString(),
    }),
    // A duplicate with no observed start shows no start line —
    // the old code derived one from finishes_at − duration.
    timerState("t3", "Pasta", "active", { remaining: 450, duration: 900 }),
    timerState("t4", "Rice", "active", { remaining: 600, duration: 900 }),
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
  assert.equal(
    card.shadowRoot.querySelector('[data-started="t4"]').textContent,
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

test("a preset chip binds by the returned entity id and shows starting… until the sensor appears (TIMER-4, §8.1)", async () => {
  injectCore();
  try {
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
    // The call goes over the websocket with return_response: true —
    // not callService, which has no return-response form from a card.
    assert.deepEqual(
      hass.wsCalls.filter((c) => c.type === "call_service"),
      [
        {
          type: "call_service",
          domain: "satellite_timers",
          service: "start_timer",
          service_data: { name: "10 minute timer", duration: 600 },
          return_response: true,
        },
      ]
    );
    // While the sensor has not arrived: one "starting…" dial, no
    // countdown-as-fact, and the pending binding carries the id the
    // service returned.
    assert.equal(card.shadowRoot.querySelectorAll('[data-action="starting"]').length, 1);
    assert.equal(
      card.shadowRoot.querySelector('[data-action="starting"] .time').textContent,
      "starting…"
    );
    assert.equal(card._pendingStart.entityId, "sensor.satellite_timer_new10");
    // The sensor arrives on the next push: the starting dial is
    // replaced by the real dial and the binding clears.
    card.hass = fakeHass([
      timerState("new10", "10 minute timer", "active", {
        remaining: 600,
        duration: 600,
      }),
    ]);
    await sleep(20);
    assert.equal(card.shadowRoot.querySelectorAll('[data-action="starting"]').length, 0);
    assert.equal(card.shadowRoot.querySelectorAll(".dial").length, 1);
    assert.equal(card._pendingStart, null);
  } finally {
    resetCore();
  }
});

test("a chip start that never binds hides the starting… dial after 10 s and warns with the entity id (TIMER-4, §8.1 F4)", async () => {
  injectCore();
  const realSetTimeout = global.setTimeout;
  const realWarn = console.warn;
  let timeoutFn = null;
  global.setTimeout = (fn, ms, ...rest) => {
    if (ms === PENDING_START_TIMEOUT_MS) {
      timeoutFn = fn;
      return 0;
    }
    return realSetTimeout(fn, ms, ...rest);
  };
  const warns = [];
  console.warn = (...args) => {
    warns.push(args.map(String).join(" "));
  };
  try {
    const hass = fakeHass([]);
    const card = new TimerScreenCard();
    card.setConfig({});
    card.hass = hass;
    card._onClick({
      target: {
        closest: () => ({ dataset: { action: "preset", minutes: "5" } }),
      },
    });
    await sleep(20);
    assert.equal(card.shadowRoot.querySelectorAll('[data-action="starting"]').length, 1);
    assert.ok(timeoutFn, "the 10 s bind timeout is armed");
    timeoutFn();
    assert.equal(card.shadowRoot.querySelectorAll('[data-action="starting"]').length, 0);
    assert.equal(card._pendingStart, null);
    assert.ok(
      warns.some(
        (w) =>
          w.includes("sensor.satellite_timer_new10") &&
          w.includes("did not appear")
      ),
      `expected a bind-timeout warning, got: ${warns.join(" | ")}`
    );
  } finally {
    global.setTimeout = realSetTimeout;
    console.warn = realWarn;
    resetCore();
  }
});

test("a failed chip start shows the visible error, warns, and never retries (TIMER-4, §8.1 F14)", async () => {
  injectCore();
  const realWarn = console.warn;
  const warns = [];
  console.warn = (...args) => {
    warns.push(args.map(String).join(" "));
  };
  try {
    const hass = fakeHass([]);
    hass.callWS = async () => {
      throw new Error("Service satellite_timers.start_timer not found");
    };
    const card = new TimerScreenCard();
    card.setConfig({});
    card.hass = hass;
    card._onClick({
      target: {
        closest: () => ({ dataset: { action: "preset", minutes: "15" } }),
      },
    });
    await sleep(20);
    // No starting dial, no pending binding, and the error is visible
    // on the empty state — the user re-taps; the card never retries.
    assert.equal(card.shadowRoot.querySelectorAll('[data-action="starting"]').length, 0);
    assert.equal(card._pendingStart, null);
    const errEl = card.shadowRoot.querySelector(".empty-error");
    assert.equal(errEl.hidden, false);
    assert.match(errEl.textContent, /updated bridge/);
    assert.ok(
      warns.some((w) => w.includes("could not start the 15 minute timer")),
      `expected a start-failure warning, got: ${warns.join(" | ")}`
    );
  } finally {
    console.warn = realWarn;
    resetCore();
  }
});

test("R2 fix1: a hung chip-start call times out, frees the chips, and shows the error — no retry (critic N4)", async () => {
  injectCore();
  const realWarn = console.warn;
  const warns = [];
  console.warn = (...args) => {
    warns.push(args.map(String).join(" "));
  };
  try {
    const hass = fakeHass([]);
    // A websocket call that never settles: the old code awaited it
    // forever with _presetBusy set and every chip dead until reload.
    hass.callWS = async (msg) => {
      if (msg && msg.type === "call_service") return new Promise(() => {});
      return null;
    };
    const card = new TimerScreenCard();
    card.setConfig({});
    card.hass = hass;
    card._callWsTimeoutMs = 20;
    card._onClick({
      target: {
        closest: () => ({ dataset: { action: "preset", minutes: "10" } }),
      },
    });
    await sleep(150);
    // The timeout rejects the race: no starting dial, no pending
    // binding, the chips are usable again, and the error is visible.
    assert.equal(card._presetBusy, false);
    assert.equal(card._pendingStart, null);
    assert.equal(card.shadowRoot.querySelectorAll('[data-action="starting"]').length, 0);
    const errEl = card.shadowRoot.querySelector(".empty-error");
    assert.equal(errEl.hidden, false);
    assert.match(errEl.textContent, /Couldn't start the timer/);
    assert.ok(
      warns.some((w) => w.includes("could not start the 10 minute timer")),
      `expected a start-failure warning, got: ${warns.join(" | ")}`
    );
    // The chips are re-enabled: a second tap issues a second call
    // (the card never retries on its own, but the user can re-tap).
    assert.equal(
      [...card.shadowRoot.querySelectorAll('[data-action="preset"]')].every(
        (b) => !b.disabled
      ),
      true
    );
    // _callStartTimer itself rejects on the timeout, not on the hang.
    const card2 = new TimerScreenCard();
    card2.setConfig({});
    card2.hass = hass;
    card2._callWsTimeoutMs = 10;
    await assert.rejects(card2._callStartTimer("x", 60), /timed out/);
  } finally {
    console.warn = realWarn;
    resetCore();
  }
});

test("R2 fix1: disconnecting the card clears a live pending chip-start (critic N4)", async () => {
  injectCore();
  const realSetTimeout = global.setTimeout;
  let timeoutFn = null;
  global.setTimeout = (fn, ms, ...rest) => {
    if (ms === PENDING_START_TIMEOUT_MS) {
      timeoutFn = fn;
      return 0;
    }
    return realSetTimeout(fn, ms, ...rest);
  };
  try {
    const hass = fakeHass([]);
    const card = new TimerScreenCard();
    card.setConfig({});
    card.hass = hass;
    card._onClick({
      target: {
        closest: () => ({ dataset: { action: "preset", minutes: "5" } }),
      },
    });
    await sleep(20);
    assert.equal(card.shadowRoot.querySelectorAll('[data-action="starting"]').length, 1);
    assert.ok(timeoutFn, "the 10 s bind timeout is armed");
    assert.notEqual(card._pendingStart, null);
    card.disconnectedCallback();
    assert.equal(card._pendingStart, null);
    assert.equal(card.shadowRoot.querySelectorAll('[data-action="starting"]').length, 0);
    // The orphaned timeout firing afterwards is a no-op (its pending
    // no longer matches), not a _sync() on a detached card.
    timeoutFn();
    assert.equal(card._pendingStart, null);
  } finally {
    global.setTimeout = realSetTimeout;
    resetCore();
  }
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

test("finished timers live in a collapsible Finished section; running timers keep their dials (TIMER-7)", () => {
  const states = [];
  for (let i = 0; i < 12; i++) {
    states.push(timerState(`a${i}`, `Timer ${i}`, "active", { remaining: 600 + i }));
  }
  states.push(timerState("done1", "Tea", "finished", {}));
  const card = timerCard(states);
  const screen = card.shadowRoot.querySelector(".screen");
  // Wave 1: no count-driven degradation — no "many" class, no
  // compact dial mode; all 12 running timers keep full dials.
  assert.ok(!screen.classList.contains("many"));
  assert.equal(card.shadowRoot.querySelectorAll(".dial").length, 12);
  // The finished timer is a row in the section, not a dial.
  assert.equal(card.shadowRoot.querySelectorAll(".finished-row").length, 1);
  assert.equal(
    card.shadowRoot.querySelector('[data-finished-name="done1"]').textContent,
    "Tea"
  );
  // With running timers present the section starts collapsed.
  const header = card.shadowRoot.querySelector(".finished-header");
  assert.equal(header.getAttribute("aria-expanded"), "false");
  // 13 running timers: still no cap, still no degradation.
  const thirteen = timerCard(
    Array.from({ length: 13 }, (_, i) =>
      timerState(`b${i}`, `Timer ${i}`, "active", { remaining: 600 + i })
    )
  );
  assert.equal(thirteen.shadowRoot.querySelectorAll(".dial").length, 13);
  assert.ok(!thirteen.shadowRoot.querySelector(".screen").classList.contains("many"));
});

test("a timer finished < 60 s ago forces the Finished section expanded, even with running timers (R2, §5.1 F6)", () => {
  const now = Date.now();
  const card = timerCard([
    timerState("run1", "Pasta", "active", { remaining: 600 }),
    timerState("done1", "Tea", "finished", {
      finishedAt: new Date(now - 30000).toISOString(),
    }),
  ]);
  // The force is derived, not stored: _finishedOpen is still null.
  assert.equal(card._finishedOpen, null);
  const header = card.shadowRoot.querySelector(".finished-header");
  assert.equal(header.getAttribute("aria-expanded"), "true");
  assert.equal(card.shadowRoot.querySelectorAll(".finished-row").length, 1);
});

test("recentlyFinished: the < 60 s window is a strict boundary (R2, §5.1 F6)", () => {
  const now = Date.now();
  const finished = (msAgo) =>
    discoverTimers([
      timerState("t1", "Tea", "finished", {
        finishedAt: new Date(now - msAgo).toISOString(),
      }),
    ])[0];
  assert.equal(recentlyFinished(finished(30000), now), true);
  assert.equal(recentlyFinished(finished(59000), now), true);
  assert.equal(recentlyFinished(finished(60000), now), false);
  assert.equal(recentlyFinished(finished(180000), now), false);
  // A finished_at in the future (clock skew) still counts as recent.
  const future = discoverTimers([
    timerState("t2", "Eggs", "finished", {
      finishedAt: new Date(now + 5000).toISOString(),
    }),
  ])[0];
  assert.equal(recentlyFinished(future, now), true);
  // Not finished, or no finished_at at all: never recent.
  const active = discoverTimers([
    timerState("t3", "Pasta", "active", { remaining: 600 }),
  ])[0];
  assert.equal(recentlyFinished(active, now), false);
  assert.equal(recentlyFinished(null, now), false);
  assert.equal(FINISHED_FORCE_EXPAND_MS, 60000);
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

test("cancelling shows a 5 s Undo toast; Undo restores the timer with the REMAINING time, paused as it was (TIMER-13, F-T1, D6)", async () => {
  // Paused with 180 s left of a 900 s timer: Undo must bring back
  // 180 s, not a fresh 900 s. (The old test asserted the full
  // duration here; that was the F-T1 defect.) Wave 1 (D6): the
  // restore goes through the bridge restore_timer service, which
  // re-creates the timer already paused — no active flash.
  const hass = fakeHass([
    timerState("t1", "Pasta", "paused", { remaining: 180, duration: 900 }),
  ]);
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
    ["satellite_timers", "restore_timer", { name: "Pasta", duration: 180, paused: true }],
  ]);
  assert.equal(toast.hidden, true);
});

test("Undo of a running timer restores it running (D6)", async () => {
  const hass = fakeHass([
    timerState("t1", "Soup", "active", { remaining: 420, duration: 900 }),
  ]);
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
  click("undo-cancel", undefined);
  await sleep(20);
  assert.deepEqual(hass.calls, [
    ["satellite_timers", "cancel_timer", { timer_id: "t1" }],
    ["satellite_timers", "restore_timer", { name: "Soup", duration: 420, paused: false }],
  ]);
});

test("dismissing a finished timer shows an Undo toast; Undo restores it finished without re-ringing (TIMER-13, D6)", async () => {
  const finishedAt = new Date(Date.now() - 3 * 60000).toISOString();
  const hass = fakeHass([
    timerState("t1", "Tea", "finished", { finishedAt, duration: 600 }),
  ]);
  const card = new TimerScreenCard();
  card.setConfig({});
  card.hass = hass;
  card._onClick({
    target: { closest: () => ({ dataset: { action: "dial", timer: "t1" } }) },
  });
  await sleep(20);
  assert.deepEqual(hass.calls, [
    ["satellite_timers", "dismiss_timer", { timer_id: "t1" }],
  ]);
  const toast = card.shadowRoot.querySelector(".toast");
  assert.equal(toast.hidden, false);
  assert.match(toast.textContent, /Tea timer dismissed/);
  card._onClick({
    target: { closest: () => ({ dataset: { action: "undo-cancel" } }) },
  });
  await sleep(20);
  assert.deepEqual(hass.calls, [
    ["satellite_timers", "dismiss_timer", { timer_id: "t1" }],
    [
      "satellite_timers",
      "restore_finished_timer",
      { name: "Tea", total_seconds: 600, finished_at: finishedAt, started_via_service: false },
    ],
  ]);
  assert.equal(toast.hidden, true);
});

test("Undo never restores less than 1 s (F-T1 floor)", () => {
  const card = timerCard([]);
  // A timer cancelled at 0 s remaining still comes back (as 1 s).
  card._showUndoToast(
    {
      name: "Tea",
      duration: 600,
      state: "active",
      finishesAt: new Date(Date.now() - 5000).toISOString(),
      remaining: 0,
    },
    "cancelled"
  );
  assert.deepEqual(card._undoTimer, {
    kind: "cancelled",
    name: "Tea",
    duration: 1,
    paused: false,
  });
  card._hideToast();
  // And a timer with time left stores exactly that remainder, paused
  // as it was.
  card._showUndoToast(
    {
      name: "Pasta",
      duration: 900,
      state: "paused",
      remaining: 240,
    },
    "cancelled"
  );
  assert.deepEqual(card._undoTimer, {
    kind: "cancelled",
    name: "Pasta",
    duration: 240,
    paused: true,
  });
  card._hideToast();
  // A dismissed finished timer stores the restore_finished shape:
  // original finished_at, total seconds, no re-ring.
  const finishedAt = new Date(Date.now() - 3 * 60000).toISOString();
  const startedAt = new Date(Date.now() - 9 * 60000).toISOString();
  card._showUndoToast(
    {
      name: "Tea",
      duration: 600,
      state: "finished",
      finishedAt,
      startedViaService: true,
      startedAt,
    },
    "dismissed"
  );
  assert.deepEqual(card._undoTimer, {
    kind: "dismissed",
    name: "Tea",
    totalSeconds: 600,
    finishedAt,
    startedViaService: true,
    startedAt,
  });
  card._hideToast();
});

test("Back to recipe shows with ?from_recipe= and navigates back at the same step (TIMER-10)", () => {
  // Wave 1 (N-IMPL-21): the return is SPA navigation now
  // (pushState + location-changed, recorded in _lastNavigation) — no
  // full-page location.assign, so the recipe card's session restores
  // in place.
  stubWindow(HUB_WINDOW);
  globalThis.window.location = {
    search: "?from_recipe=rec-1&step=3",
    pathname: "/family-hub/timers",
  };
  try {
    const card = timerCard([timerState("t1", "Pasta", "active", {})]);
    // Wave 1: the back bar is created only when a recipe context
    // exists — it is present here, not merely un-hidden.
    const bar = card.shadowRoot.querySelector(".back-bar");
    assert.ok(bar, "the back bar renders with a recipe context");
    card._onClick({
      target: { closest: () => ({ dataset: { action: "back-recipe" } }) },
    });
    assert.equal(card._lastNavigation, "/family-hub/recipes?recipe=rec-1&step=3");
  } finally {
    clearWindow();
  }
  // Without the param there is no back bar at all (zero DOM residue).
  const plain = timerCard([timerState("t1", "Pasta", "active", {})]);
  assert.equal(plain.shadowRoot.querySelector(".back-bar"), null);
});

// -- RB-R4-2 (§16): _navigate goes through the core SPA helper, and --
// "Back to recipe" carries ?kiosk when the session launched with it.

function r42Window({ search, pathname }) {
  stubWindow(HUB_WINDOW);
  const pushes = [];
  const dispatches = [];
  globalThis.window.location = { search, pathname };
  globalThis.window.history = {
    pushState: (_state, _title, url) => pushes.push(url),
  };
  globalThis.window.dispatchEvent = (event) => {
    dispatches.push(event.type);
    return true;
  };
  return { pushes, dispatches };
}

test("R4-2: _navigate with core loaded calls the core SPA helper, not pushState", () => {
  const { pushes, dispatches } = r42Window({
    search: "",
    pathname: "/family-hub/timers",
  });
  const navigated = [];
  _setCoreForTests({
    ...core,
    navigateTo: (url) => {
      navigated.push(url);
      return Promise.resolve(true);
    },
  });
  try {
    const card = timerCard([timerState("t1", "Pasta", "active", {})]);
    card._navigate("/family-hub/recipes");
    assert.deepEqual(navigated, ["/family-hub/recipes"]);
    assert.equal(card._lastNavigation, "/family-hub/recipes");
    assert.deepEqual(pushes, [], "the legacy pushState never runs when the helper is used");
    assert.deepEqual(dispatches, [], "no location-changed dispatch from the card either");
  } finally {
    resetCore();
    clearWindow();
  }
});

test("R4-2: _navigate falls back to the legacy pushState when the helper throws", () => {
  const { pushes, dispatches } = r42Window({
    search: "",
    pathname: "/family-hub/timers",
  });
  _setCoreForTests({
    ...core,
    navigateTo: () => {
      throw new Error("SPA call unavailable");
    },
  });
  try {
    const card = timerCard([timerState("t1", "Pasta", "active", {})]);
    card._navigate("/family-hub/recipes");
    assert.equal(card._lastNavigation, "/family-hub/recipes");
    assert.deepEqual(pushes, ["/family-hub/recipes"]);
    assert.deepEqual(dispatches, ["location-changed"]);
  } finally {
    resetCore();
    clearWindow();
  }
});

test("R4-2: _navigate falls back to the legacy pushState when the helper rejects", async () => {
  const { pushes, dispatches } = r42Window({
    search: "",
    pathname: "/family-hub/timers",
  });
  _setCoreForTests({
    ...core,
    navigateTo: () => Promise.reject(new Error("SPA call failed")),
  });
  try {
    const card = timerCard([timerState("t1", "Pasta", "active", {})]);
    card._navigate("/family-hub/recipes");
    assert.equal(card._lastNavigation, "/family-hub/recipes");
    assert.deepEqual(pushes, [], "nothing synchronous — the rejection lands on a microtask");
    await Promise.resolve();
    await Promise.resolve();
    assert.deepEqual(pushes, ["/family-hub/recipes"]);
    assert.deepEqual(dispatches, ["location-changed"]);
  } finally {
    resetCore();
    clearWindow();
  }
});

test("R4-2: _navigate without core uses the legacy pushState (pre-core-load window)", () => {
  const { pushes, dispatches } = r42Window({
    search: "",
    pathname: "/family-hub/timers",
  });
  try {
    const card = timerCard([timerState("t1", "Pasta", "active", {})]);
    card._navigate("/family-hub/recipes");
    assert.equal(card._lastNavigation, "/family-hub/recipes");
    assert.deepEqual(pushes, ["/family-hub/recipes"]);
    assert.deepEqual(dispatches, ["location-changed"]);
  } finally {
    clearWindow();
  }
});

test("R4-2: Back to recipe carries ?kiosk when the session launched with it", () => {
  const { pushes } = r42Window({
    search: "?from_recipe=rec-1&step=3&kiosk",
    pathname: "/family-hub/timers",
  });
  try {
    const card = timerCard([timerState("t1", "Pasta", "active", {})]);
    card._onClick({
      target: { closest: () => ({ dataset: { action: "back-recipe" } }) },
    });
    assert.equal(
      card._lastNavigation,
      "/family-hub/recipes?recipe=rec-1&step=3&kiosk"
    );
    assert.deepEqual(pushes, ["/family-hub/recipes?recipe=rec-1&step=3&kiosk"]);
  } finally {
    clearWindow();
  }
});

test("R4-2: Back to recipe carries no ?kiosk when the session did not launch with it", () => {
  const { pushes } = r42Window({
    search: "?from_recipe=rec-1&step=3",
    pathname: "/family-hub/timers",
  });
  try {
    const card = timerCard([timerState("t1", "Pasta", "active", {})]);
    card._onClick({
      target: { closest: () => ({ dataset: { action: "back-recipe" } }) },
    });
    assert.equal(card._lastNavigation, "/family-hub/recipes?recipe=rec-1&step=3");
    assert.deepEqual(pushes, ["/family-hub/recipes?recipe=rec-1&step=3"]);
  } finally {
    clearWindow();
  }
});

// -- tablet category (DESIGN-9, Phase 7) -----------------------------------
// The fourth device category: hub / desktop / tablet / mobile. The
// tablet band is a touch screen 768-1024px wide, either orientation,
// plus the tablet user agents (iPad; Android without a "Mobi" token).

test("identity: tablet category — band only, no UA (DESIGN-9)", () => {
  const touch = { coarsePointer: true, touchPoints: 1 };
  // The spec viewports: 1024x768 landscape and 768x1024 portrait.
  assert.equal(deviceFromIdentity(unknownIdentity(touch), 1024, 768).category, "tablet");
  assert.equal(deviceFromIdentity(unknownIdentity(touch), 768, 1024).category, "tablet");
  // The other spec tablet viewports: 900x1440, 768x1366.
  assert.equal(deviceFromIdentity(unknownIdentity(touch), 900, 1440).category, "tablet");
  assert.equal(deviceFromIdentity(unknownIdentity(touch), 768, 1366).category, "tablet");
  // Boundaries of the band: 767px wide is mobile, 1025px wide with a
  // coarse pointer is mobile too.
  assert.equal(deviceFromIdentity(unknownIdentity(touch), 767, 1024).category, "mobile");
  assert.equal(deviceFromIdentity(unknownIdentity(touch), 1025, 768).category, "mobile");
  // Short side under 600px is a phone, not a tablet (844x390).
  assert.equal(deviceFromIdentity(unknownIdentity(touch), 844, 390).category, "mobile");
  // P8-16 / OQ-7 DECIDED: a touchscreen laptop at 1366x768 WITH a
  // fine pointer (mouse/trackpad) is a desktop — fine wins over
  // coarse. Without a fine pointer the same screen is still mobile:
  // a laptop without a mouse is indistinguishable from a touch-only
  // screen outside the tablet band by these signals alone.
  assert.equal(
    deviceFromIdentity(
      unknownIdentity({ coarsePointer: true, finePointer: true, touchPoints: 1 }),
      1366,
      768
    ).category,
    "desktop"
  );
  assert.equal(deviceFromIdentity(unknownIdentity(touch), 1366, 768).category, "mobile");
  // O2-01: the rotated wall (1200x1920) is the portrait HUB, not
  // mobile — kind wins over size.
  assert.equal(deviceFromIdentity(wallIdentity(), 1200, 1920).category, "hub");
});

// -- Phase 7 CSS: tablet rules, reduced motion, no layout animation ------

test("Phase 7 CSS: tablet rules, reduced motion, no layout-property animation", () => {
  // Tablet (DESIGN-9): 64px controls, preset chips shown when their
  // row is open (TIMER-4).
  assert.match(CARD_CSS, /\.screen\.dev-tablet \.controls button \{ min-height: 64px;/);
  assert.match(CARD_CSS, /\.preset-chips\.show \{ display: flex; \}/);
  assert.match(CARD_CSS, /\.screen\.dev-tablet \.dial-name \{ font-size: 20px; \}/);
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
// DELETED in Wave 1: the old fix shrank finished-dial sub-text to
// 14px and stretched the portrait dial to 300px because the sub-lines
// sat INSIDE the ring and the ring crossed characters. The Wave 1
// stack layout puts the name above the ring and the state/hint lines
// BELOW it, so there is nothing for the ring to cross — the
// workaround is gone and the sub-text stays at the TIMER-6 18px on
// every category. The replacement test below pins the new structure.

test("Wave 1 (Fix 2 superseded): sub-text sits below the ring at 18px on mobile — no 14px rule, no 300px portrait dial", () => {
  // The dial is a vertical stack: name block above the ring button,
  // countdown inside it, state/hint lines below it.
  assert.match(
    CARD_CSS,
    /\.dial \{\n    min-width: 0;\n    width: 100%;\n    position: relative;\n    display: flex;\n    flex-direction: column;/
  );
  assert.match(
    CARD_CSS,
    /\.dial-sub \{\n    display: flex;\n    flex-direction: column;/
  );
  assert.match(CARD_CSS, /\.screen\.dev-mobile \.state, \.screen\.dev-mobile \.hint \{ font-size: 18px; \}/);
  assert.doesNotMatch(CARD_CSS, /\.screen\.dev-mobile \.state, \.screen\.dev-mobile \.hint \{ font-size: 14px;/);
  assert.doesNotMatch(CARD_CSS, /min-height: 300px;/);
  // The finished timer's sub-lines live in the compact row, not in
  // a dial at all.
  assert.match(CARD_CSS, /\.finished-sub \{/);
});

test("Phase 7 follow-up (Fix 2): hub and tablet type sizes are unchanged (TIMER-6)", () => {
  // Base (hub) rules: name 20px, state/hint 18px.
  assert.match(CARD_CSS, /\.dial-name \{\n    width: 100%;\n    color: #212121;\n    font-size: 20px;/);
  assert.match(CARD_CSS, /\.state \{ color: #57534E; font-size: 18px; line-height: 1\.3; \}/);
  assert.match(CARD_CSS, /\.hint \{ color: #57534E; font-size: 18px; line-height: 1\.3; \}/);
  // Tablet keeps the hub minima.
  assert.match(
    CARD_CSS,
    /\.screen\.dev-tablet \.state, \.screen\.dev-tablet \.hint \{ font-size: 18px; \}/
  );
  // Mobile name is 18px (the sub-lines never shrank in Wave 1).
  assert.match(CARD_CSS, /\.screen\.dev-mobile \.dial-name \{ font-size: 18px; \}/);
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
  // Wave 1: the scroll indicator moved OUT of the strip into a
  // floating pill, so the Timers button carries only the count badge
  // (N-IMPL-5, "1" for the one active timer in this fixture) — its
  // textContent reads "Timers1". Labels are read with the count
  // digits stripped.
  assert.deepEqual(
    buttons.map((button) => button.textContent.replace(/[0-9]+$/, "")),
    ["Home", "Timers", "Photos", "Recipes"]
  );
  // No scroll badge on any strip button any more.
  assert.equal(card.shadowRoot.querySelectorAll(".nav-scroll-badge").length, 0);
  assert.doesNotMatch(buttons[1].innerHTML, /nav-scroll-badge/);
  // The count badge shows (Wave 1: it always shows while timers
  // exist — no reconciliation with the pill): 1 active timer.
  const countBadges = card.shadowRoot.querySelectorAll(".timer-badge");
  assert.equal(countBadges.length, 1);
  assert.equal(countBadges[0].hidden, false);
  assert.equal(countBadges[0].textContent, "1");
  assert.ok(buttons[1].innerHTML.includes("timer-badge"));
  assert.ok(!buttons[0].innerHTML.includes("timer-badge"));
  assert.ok(!buttons[2].innerHTML.includes("timer-badge"));
  assert.ok(!buttons[3].innerHTML.includes("timer-badge"));
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

test("Phase 7 follow-up (Fix 3): strip CSS — fixed to the viewport bottom, hidden on desktop unless kiosk", () => {
  // Fixed in both orientations (not sticky): the screen is a scroll
  // container and the card can grow past the viewport, so a sticky
  // strip would sit at the content end, below the fold at scroll top.
  // Fixed pins to the viewport bottom in both scroll modes, and the
  // screen reserves the strip's height in bottom padding.
  assert.match(
    CARD_CSS,
    /\.nav-strip \{\n    position: fixed;\n    bottom: 0;\n    left: 0;\n    width: 100%;/
  );
  assert.match(CARD_CSS, /\.nav-strip \{[^}]*height: 96px;/s);
  assert.match(CARD_CSS, /\.nav-strip \{[^}]*background: #171717;/s);
  assert.ok(
    CARD_CSS.includes("padding-bottom: calc(8px + var(--nav-strip-height, 96px));")
  );
  // Desktop only when NOT in kiosk mode: hidden (HA tabs already exist
  // and work there). In kiosk mode the strip shows ALWAYS — HA tabs
  // do not exist there (the kiosk-desktop hole).
  assert.match(CARD_CSS, /\.screen\.dev-desktop \.nav-strip \{ display: none; \}/);
  assert.match(
    CARD_CSS,
    /\.screen\.dev-desktop\.kiosk \.nav-strip \{ display: block; \}/
  );
  assert.match(
    CARD_CSS,
    /\.screen\.dev-desktop\.kiosk \{ --nav-strip-height: 64px; \}/
  );
  // Focus is 3px white, press is scale(0.97) within 100 ms (D-N1/D-N11).
  assert.match(CARD_CSS, /\.nav-strip-button:focus-visible \{ outline: 3px solid #fff; outline-offset: 2px; \}/);
  assert.match(CARD_CSS, /\.nav-strip-button:active \{ transform: scale\(0.97\);/);
  // Count badge tones + night palette match the reference strip.
  assert.ok(CARD_CSS.includes(".timer-badge.soon { background: #B45309; color: #fff; }"));
  assert.ok(CARD_CSS.includes(".timer-badge.finished { background: #B91C1C; color: #fff; }"));
  assert.ok(CARD_CSS.includes(".screen.night .nav-strip { background: #0A0A0A; }"));
  // Active state: the accent background, heavier weight.
  assert.ok(
    CARD_CSS.includes(
      ".nav-strip-button.active { background: #C2410C; border-color: #C2410C; color: #fff; font-weight: 800; }"
    )
  );
  // Per-category strip heights (D-N1, wave 1): 96px wall, 72px
  // tablet, 64px phone (+ safe area), 56px desktop+kiosk.
  assert.ok(CARD_CSS.includes(".screen.dev-tablet .nav-strip { height: 72px; }"));
  assert.match(CARD_CSS, /\.screen\.dev-mobile \.nav-strip \{ height: calc\(64px \+ env\(safe-area-inset-bottom, 0px\)\); \}/);
  assert.ok(CARD_CSS.includes(".screen.dev-desktop.kiosk .nav-strip { height: 56px; }"));
  assert.match(
    CARD_CSS,
    /\.screen\.dev-tablet \.nav-strip-button \{ font-size: 20px; \}/
  );
  assert.match(
    CARD_CSS,
    /\.screen\.dev-mobile \.nav-strip-button \{ font-size: 16px; white-space: nowrap; \}/
  );
  // P8-5 (Wave 1): the scroll indicator is a floating pill, not a
  // badge on the Timers strip button — no .nav-scroll-badge rule may
  // remain, and no .scroll-hint rule either. The pill is fixed to
  // the viewport (bottom set from the strip height in JS) with its
  // own [hidden] rule.
  assert.doesNotMatch(CARD_CSS, /\.scroll-hint/);
  assert.doesNotMatch(CARD_CSS, /\.nav-scroll-badge/);
  assert.match(CARD_CSS, /\.scroll-pill \{\n    position: fixed;/);
  assert.ok(CARD_CSS.includes(".scroll-pill[hidden] { display: none; }"));
  // F-T17 (Wave 1): the Undo toast is fixed to the viewport with its
  // bottom set from the measured strip height + 8px in JS — no
  // per-category sibling bottom rules remain. The CSS 24px bottom
  // is the no-strip fallback (desktop, strip hidden).
  assert.match(CARD_CSS, /\.toast \{\n    position: fixed;/);
  assert.ok(CARD_CSS.includes("bottom: 24px;"));
  assert.doesNotMatch(CARD_CSS, /~ \.toast/);
  // Reduced motion: the press feedback is instant.
  assert.match(CARD_CSS, /\.nav-strip-button:active \{ transform: none; \}/);
});

// -- Scroll pill gating (P8-4/P8-5, Wave 1) --------------------------------
// Wave 1: the indicator moved out of the strip into a floating "↓"
// pill (button.scroll-pill, a shadow-root sibling of .screen). The
// gate is unchanged: the pill shows only while a DIAL sits below the
// fold. Rects are pinned on the fake elements (see
// getBoundingClientRect): the screen rect sets the fold, the dial
// rects decide.

function pillCard() {
  stubWindow(HUB_WINDOW);
  const card = timerCard([
    timerState("t1", "Pasta", "active", { remaining: 600 }),
    timerState("t2", "Rice", "active", { remaining: 300 }),
  ]);
  const screen = card.shadowRoot.querySelector(".screen");
  screen._rect = { top: 0, bottom: 1200, left: 0, right: 1920, width: 1920, height: 1200 };
  const dials = [...card._dialEls.values()];
  return { card, screen, dials };
}

test("the scroll pill stays hidden when every dial is in view (P8-4)", () => {
  const { card, dials } = pillCard();
  try {
    for (const d of dials) {
      d._rect = { top: 100, bottom: 1100, left: 0, right: 600, width: 600, height: 1000 };
    }
    card._updateScrollPill();
    assert.equal(card.shadowRoot.querySelector(".scroll-pill").hidden, true);
  } finally {
    clearWindow();
  }
});

test("the scroll pill shows when a dial extends below the viewport fold (P8-4)", () => {
  const { card, dials } = pillCard();
  try {
    dials[0]._rect = { top: 100, bottom: 1100, left: 0, right: 600, width: 600, height: 1000 };
    dials[1]._rect = { top: 1100, bottom: 1350, left: 0, right: 600, width: 600, height: 250 };
    card._updateScrollPill();
    const pill = card.shadowRoot.querySelector(".scroll-pill");
    assert.equal(pill.hidden, false);
    assert.equal(pill.textContent, "↓");
    // The pill clears the strip: bottom = strip height + 16px.
    // (The fake strip rect is 0 high here, so the bottom is 16px.)
    assert.equal(pill.style.bottom, "16px");
    // Scrolled to the bottom, the last dial is fully visible again.
    dials[1]._rect = { top: 900, bottom: 1150, left: 0, right: 600, width: 600, height: 250 };
    card._updateScrollPill();
    assert.equal(card.shadowRoot.querySelector(".scroll-pill").hidden, true);
  } finally {
    clearWindow();
  }
});

test("the scroll pill stays hidden when only the screen (not a dial) extends below the fold (P8-4)", () => {
  // The 1-timer regression: the controls row overflowed, the screen
  // rect ran past the viewport, and the old gate showed "More timers
  // below" with no timers below at all.
  const { card, screen, dials } = pillCard();
  try {
    screen._rect = { top: 0, bottom: 1284, left: 0, right: 1920, width: 1920, height: 1284 };
    for (const d of dials) {
      d._rect = { top: 100, bottom: 1050, left: 0, right: 600, width: 600, height: 950 };
    }
    card._updateScrollPill();
    assert.equal(card.shadowRoot.querySelector(".scroll-pill").hidden, true);
  } finally {
    clearWindow();
  }
});

test("the scroll pill uses the screen's own bottom as the fold in internal-scroll mode (P8-4)", () => {
  const { card, screen, dials } = pillCard();
  try {
    // Screen shorter than the viewport, scrolling internally: a dial
    // below the screen's visible bottom (800) is below the fold even
    // though it is above the viewport bottom (1200).
    screen._rect = { top: 0, bottom: 800, left: 0, right: 1920, width: 1920, height: 800 };
    dials[0]._rect = { top: 100, bottom: 700, left: 0, right: 600, width: 600, height: 600 };
    dials[1]._rect = { top: 700, bottom: 950, left: 0, right: 600, width: 600, height: 250 };
    card._updateScrollPill();
    assert.equal(card.shadowRoot.querySelector(".scroll-pill").hidden, false);
  } finally {
    clearWindow();
  }
});

test("tapping the scroll pill scrolls the screen down (P8-5)", () => {
  const { card, screen } = pillCard();
  try {
    // Internal scroll: the screen scrolls by 80% of the viewport.
    screen.scrollHeight = 2400;
    screen.clientHeight = 1200;
    screen.scrollTop = 0;
    card._onScrollPillTap();
    assert.equal(screen.scrollTop, 960);
  } finally {
    clearWindow();
  }
});

// -- Kiosk mode (NAV-1 kiosk-desktop hole) ---------------------------------

test("R1: the deleted machinery is gone from the timer exports (§19)", () => {
  for (const name of [
    "detectDevice",
    "isKioskMode",
    "readDeviceEnv",
    "KIOSK_SESSION_KEY",
    "readKioskFlag",
    "storeKioskFlag",
    "isKioskSession",
    "withKioskQuery",
    "reapplyKioskIfNeeded",
    // R2 (RB-R2-11, §19): the per-card time copies are deleted —
    // they live in core now.
    "formatRemaining",
    "remainingSeconds",
    "progressFraction",
    "startedAtMs",
    // R3 (RB-R3-3): the per-card voiceUnavailable derivation is
    // deleted with the voice-down display it drove.
    "voiceUnavailable",
  ]) {
    assert.equal(timerModule[name], undefined, `${name} must not be exported`);
  }
  // The R2 recency rule and its constant ARE exported.
  assert.equal(typeof recentlyFinished, "function");
  assert.equal(FINISHED_FORCE_EXPAND_MS, 60000);
  assert.equal(PENDING_START_TIMEOUT_MS, 10000);
  // The core lookup surface is there instead: the Lovelace resource
  // path, the throttle/interval mirrors of core's constants, and the
  // test seam accepts a core module (and null) without throwing.
  assert.equal(CORE_RESOURCE_PATH, "/local/family-hub-core.js");
  assert.equal(CORE_LOOKUP_RETRY_THROTTLE_MS, 10 * 1000);
  assert.equal(CORE_LOOKUP_INTERVAL_MS, 30 * 1000);
  assert.equal(typeof _setCoreForTests, "function");
  assert.doesNotThrow(() => _setCoreForTests(null));
  assert.doesNotThrow(() => _setCoreForTests(core));
  _setCoreForTests(null);
});

test("R2: no per-card time copies and no ringing code in the card source (§19, §5.1)", () => {
  const src = fs.readFileSync(
    path.join(__dirname, "timer-screen-card.js"),
    "utf8"
  );
  // The §19 deletion table: these definitions lived in the card and
  // are now core's. (Comments may still name them; definitions may
  // not.) The `ringing` state is deleted the same way — the only
  // surviving mention is the bridge-behaviour prose "WITHOUT
  // re-ringing" and deletion-history comments.
  for (const def of [
    "function formatRemaining",
    "function remainingSeconds",
    "function progressFraction",
    "function startedAtMs",
  ]) {
    assert.ok(!src.includes(def), `card source must not define ${def}`);
  }
  assert.doesNotMatch(src, /state === "ringing"/);
  assert.doesNotMatch(src, /state !== "ringing"/);
  assert.doesNotMatch(src, /"ringing"/);
  // The card-file sound grep is zero (RB-R2-11 owns this check).
  assert.equal((src.match(/sound/gi) || []).length, 0);
  // R3 (RB-R3-3): no voice-down strings anywhere in the card source —
  // not in rendered text, not in a comment quoting the old line. The
  // provider drives recovery, not display.
  assert.doesNotMatch(src, /isn't available/);
  assert.doesNotMatch(src, /voice is down/);
  assert.doesNotMatch(src, /unavailable/);
  assert.doesNotMatch(src, /function voiceUnavailable/);
});

test("R1: identity banner CSS exists (§4)", () => {
  // The re-register banner (kiosk launch, mismatched id) is styled,
  // dark red on white.
  assert.ok(CARD_CSS.includes(".fh-identity-banner {"));
  assert.ok(CARD_CSS.includes("background: #7f1d1d;"));
  assert.ok(CARD_CSS.includes(".fh-identity-banner[hidden] { display: none; }"));
});

test("R1-5: gate error CSS exists (§9/T-10)", () => {
  assert.ok(CARD_CSS.includes(".fh-gate-mismatch {"));
  assert.ok(CARD_CSS.includes(".fh-gate-mismatch[hidden] { display: none; }"));
});

function gateEl(card) {
  return card.shadowRoot.querySelector(".fh-gate-mismatch");
}

test("R1-5: old bridge (no contract_version) + new card -> visible Timer bridge mismatch, no timer UI", () => {
  injectCore();
  try {
    const v1 = [
      timerState("t1", "Pasta", "active", { contractVersion: null }),
      timerState("t2", "Rice", "paused", { contractVersion: null }),
    ];
    const card = timerCard(v1);
    assert.equal(
      card._bridgeMismatch,
      "Timer bridge mismatch: card expects contract v2 — update Satellite Timers"
    );
    assert.equal(card._timers.length, 0);
    assert.equal(gateEl(card).hidden, false);
    assert.equal(
      gateEl(card).textContent,
      "Timer bridge mismatch: card expects contract v2 — update Satellite Timers"
    );
    assert.equal(card.shadowRoot.querySelectorAll(".dial").length, 0);
    assert.equal(card.shadowRoot.querySelectorAll(".finished-row").length, 0);
    // No badge counts a mismatched timer.
    assert.equal(card._timerBadgeEl.hidden, true);
    // No empty-state hints under the error either.
    assert.equal(card._emptyEl.hidden, true);
  } finally {
    resetCore();
  }
});

test("R1-5: a ringing sensor is a mismatch, not a timer", () => {
  injectCore();
  try {
    const card = timerCard([
      timerState("t1", "Pasta", "ringing", { contractVersion: 2 }),
    ]);
    assert.equal(
      card._bridgeMismatch,
      "Timer bridge mismatch: card expects contract v2 — update Satellite Timers"
    );
    assert.equal(card._timers.length, 0);
    assert.equal(gateEl(card).hidden, false);
    assert.equal(card.shadowRoot.querySelectorAll(".dial").length, 0);
    assert.equal(card.shadowRoot.querySelectorAll(".finished-row").length, 0);
  } finally {
    resetCore();
  }
});

test("R1-5: mixed v2 + v1 sensors -> the v2 timer renders, the error still shows", () => {
  injectCore();
  try {
    const v1 = timerState("old", "Old", "active", { contractVersion: null });
    const card = timerCard([timerState("t1", "Pasta", "active", {}), v1]);
    assert.equal(
      card._bridgeMismatch,
      "Timer bridge mismatch: card expects contract v2 — update Satellite Timers"
    );
    assert.equal(card._timers.length, 1);
    assert.equal(card._timers[0].timer_id, "t1");
    assert.equal(gateEl(card).hidden, false);
    assert.equal(card.shadowRoot.querySelectorAll(".dial").length, 1);
  } finally {
    resetCore();
  }
});

test("R1-5: core version skew -> visible Hub core mismatch, no timer UI", () => {
  const wrongCore = {
    identify: core.identify,
    deviceFromIdentity: core.deviceFromIdentity,
    checkCoreVersion: () =>
      "Hub core mismatch: card needs 2.0.0, core is 9.9.9",
    checkBridgeContract: core.checkBridgeContract,
    layoutBand: core.layoutBand,
  };
  _setCoreForTests(wrongCore);
  try {
    const card = timerCard(fiveTimers());
    assert.equal(
      card._coreMismatch,
      "Hub core mismatch: card needs 2.0.0, core is 9.9.9"
    );
    assert.equal(gateEl(card).hidden, false);
    assert.equal(
      gateEl(card).textContent,
      "Hub core mismatch: card needs 2.0.0, core is 9.9.9"
    );
    // The core gate replaces the timer UI too: no dials render.
    assert.equal(card.shadowRoot.querySelectorAll(".dial").length, 0);
  } finally {
    resetCore();
  }
});

test("R1-5: core absent -> no gate evaluation (R1-3 behaviour, documented)", () => {
  // No injectCore: the v1-shaped sensors render as before, because
  // there is no core to compare against. The gate is core-dependent
  // by construction; on the rig and the live hub core is served, so
  // this path is the no-core fallback only.
  const v1 = timerState("t1", "Pasta", "active", { contractVersion: null });
  const card = timerCard([v1]);
  assert.equal(card._coreMismatch, null);
  assert.equal(card._bridgeMismatch, null);
  assert.equal(card._timers.length, 1);
  assert.equal(gateEl(card).hidden, true);
});

test("a desktop viewport in kiosk mode carries the kiosk class (strip stays); without it, it does not", () => {
  stubWindow(DESKTOP_WINDOW);
  globalThis.window.location = { search: "?kiosk" };
  injectCore();
  try {
    const card = timerCard(fiveTimers());
    const screen = card.shadowRoot.querySelector(".screen");
    assert.ok(screen.classList.contains("dev-desktop"));
    assert.ok(screen.classList.contains("kiosk"));
  } finally {
    resetCore();
    clearWindow();
  }
  stubWindow(DESKTOP_WINDOW);
  injectCore();
  try {
    const card = timerCard(fiveTimers());
    const screen = card.shadowRoot.querySelector(".screen");
    assert.ok(screen.classList.contains("dev-desktop"));
    assert.ok(!screen.classList.contains("kiosk"));
  } finally {
    resetCore();
    clearWindow();
  }
});

// -- P8-7: DOM-removal latency, unit bound ---------------------------------
// The card removes a dial synchronously in the hass setter's _sync —
// the re-render itself adds no delay. (The rig probe measures the
// full tap → sensor-gone → DOM-gone path, where HA propagation is the
// remaining term.)

test("removing a timer removes its dial from the DOM synchronously (P8-7 unit bound)", () => {
  const card = timerCard([timerState("t1", "Pasta", "active", {})]);
  assert.equal(card.shadowRoot.querySelectorAll(".dial").length, 1);
  card.hass = fakeHass([]);
  assert.equal(card.shadowRoot.querySelectorAll(".dial").length, 0);
});

// -- Wave 1 (Navigation N-IMPL-1/2/3/5/6/10/21/24): fixed dark strip, --
// kiosk as a session property, timers badge, night mode, SPA back-to-recipe

test("R1: _navigate never carries ?kiosk — it is launch-only (§4/§19)", () => {
  const store = new Map();
  globalThis.sessionStorage = {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => store.set(k, String(v)),
  };
  try {
    // A stale flag from the old machinery must not re-attach ?kiosk:
    // the flag machinery is deleted, and in-app navigation is plain.
    store.set("family-hub:kiosk", "1");
    const card = timerCard([timerState("t1", "Pasta", "active", {})]);
    card._navigate("/family-hub/recipes");
    assert.equal(card._lastNavigation, "/family-hub/recipes");
    card._navigate("/family-hub/recipes?recipe=x&step=2");
    assert.equal(card._lastNavigation, "/family-hub/recipes?recipe=x&step=2");
  } finally {
    delete globalThis.sessionStorage;
  }
});

test("Wave 1: timerBadgeState counts ACTIVE + PAUSED, colour from running/finished only", () => {
  assert.equal(timerBadgeState([]), null);
  assert.deepEqual(timerBadgeState([{ state: "active", remaining: 300 }]), { count: 1, tone: "count" });
  assert.deepEqual(timerBadgeState([{ state: "paused", remaining: 30 }]), { count: 1, tone: "count" });
  assert.deepEqual(timerBadgeState([{ state: "active", remaining: 45 }]), { count: 1, tone: "soon" });
  assert.deepEqual(timerBadgeState([{ state: "finished", remaining: 0 }]), { count: 1, tone: "finished" });
  assert.deepEqual(
    timerBadgeState([
      { state: "active", remaining: 500 },
      { state: "paused", remaining: 20 },
      { state: "finished", remaining: 0 },
    ]),
    { count: 2, tone: "finished" }
  );
  assert.deepEqual(
    timerBadgeFromHassStates({
      "sensor.satellite_timer_a": { state: "active", attributes: { remaining: 500 } },
      "sensor.satellite_timer_b": { state: "active", attributes: { remaining: 30 } },
    }),
    { count: 2, tone: "soon" }
  );
});

test("R2 fix1: badge soon uses the dial's 59 s rule, from the clock-anchored remaining, on the tick (critic F3)", () => {
  // One threshold for both consumers (design D5.2 / OQ-5, measured
  // ≤59 s): 60 s is count, 59 s is soon, 0 s is count (an active timer
  // at exactly 0 s is a finished timer on the next push, not soon).
  assert.deepEqual(timerBadgeState([{ state: "active", remaining: 60 }]), { count: 1, tone: "count" });
  assert.deepEqual(timerBadgeState([{ state: "active", remaining: 59 }]), { count: 1, tone: "soon" });
  assert.deepEqual(timerBadgeState([{ state: "active", remaining: 0 }]), { count: 1, tone: "count" });
  // The override map wins over the stored value — and the other way:
  // the card passes clock-anchored values, so a stale stored 300 s
  // with a live 45 s clock reads soon, and a stale stored 45 s with
  // a live 300 s clock reads count.
  assert.deepEqual(
    timerBadgeState(
      [{ timer_id: "a", state: "active", remaining: 300 }],
      new Map([["a", 45]])
    ),
    { count: 1, tone: "soon" }
  );
  assert.deepEqual(
    timerBadgeState(
      [{ timer_id: "a", state: "active", remaining: 45 }],
      new Map([["a", 300]])
    ),
    { count: 1, tone: "count" }
  );
  // The tick flips the badge: a lone timer whose stored remaining is
  // 300 s (frozen between structural pushes) shows count at setup,
  // then soon once the clock-anchored remaining crosses 59 s — with
  // no new hass push in between. Before this fix the badge read the
  // stored object on _sync only and never flipped on the tick.
  const card = timerCard([
    timerState("t1", "Pasta", "active", { remaining: 300, duration: 600 }),
  ]);
  const badgeEl = () => card.shadowRoot.querySelector(".timer-badge");
  assert.equal(badgeEl().hidden, false);
  assert.equal(badgeEl().textContent, "1");
  assert.ok(!badgeEl().className.includes("soon"), badgeEl().className);
  card._remainingFor = () => 45;
  card._updateTimes();
  assert.ok(badgeEl().className.includes("soon"), badgeEl().className);
  assert.equal(badgeEl().textContent, "1");
  // And back: the clock re-anchoring above 59 s clears the amber.
  card._remainingFor = () => 300;
  card._updateTimes();
  assert.ok(!badgeEl().className.includes("soon"), badgeEl().className);
});

test("Wave 1: night mode has no default hours; overnight spans work", () => {
  assert.equal(parseNightSetting(null), null);
  assert.equal(readNightSetting(), null);
  assert.equal(isNightNow(null, new Date(2026, 10, 10, 23, 0)), false);
  const overnight = parseNightSetting('{"start":"22:00","end":"06:00"}');
  assert.equal(isNightNow(overnight, new Date(2026, 10, 10, 23, 30)), true);
  assert.equal(isNightNow(overnight, new Date(2026, 10, 10, 12, 0)), false);
});

test("Wave 1: readBackRecipeContext — session first, URL fallback, corrupt session falls through", () => {
  globalThis.sessionStorage = {
    getItem: (k) =>
      k === "family-hub:recipe:state"
        ? JSON.stringify({ openId: "rec-9", step: 4 })
        : null,
  };
  assert.deepEqual(readBackRecipeContext("?from_recipe=rec-1&step=3"), {
    recipeId: "rec-9",
    step: 4,
  });
  globalThis.sessionStorage = { getItem: () => null };
  assert.deepEqual(readBackRecipeContext("?from_recipe=rec-1&step=3"), {
    recipeId: "rec-1",
    step: "3",
  });
  assert.equal(readBackRecipeContext(""), null);
  globalThis.sessionStorage = { getItem: () => "not json{" };
  assert.deepEqual(readBackRecipeContext("?from_recipe=rec-1&step=3"), {
    recipeId: "rec-1",
    step: "3",
  });
  delete globalThis.sessionStorage;
});

test("Wave 1: _navigate is a no-op on the active Timers tab", () => {
  const card = timerCard([timerState("t1", "Pasta", "active", {})]);
  // _lastNavigation starts null; a no-op navigate leaves it there.
  assert.equal(card._lastNavigation, null);
  card._navigate("/family-hub/timers");
  assert.equal(card._lastNavigation, null);
  card._navigate("/family-hub/timers?from_recipe=rec-1");
  assert.equal(card._lastNavigation, null);
});

test("Wave 1: count badge always shows while timers exist — the scroll pill is independent, no reconciliation", () => {
  const { card, dials } = pillCard();
  try {
    // A dial below the fold: the pill shows AND the count badge
    // shows (2 active timers). Wave 1 moved the pill out of the
    // strip, so the two no longer share a slot.
    dials[0]._rect = { top: 100, bottom: 1100, left: 0, right: 600, width: 600, height: 1000 };
    dials[1]._rect = { top: 1100, bottom: 1350, left: 0, right: 600, width: 600, height: 250 };
    card._updateScrollPill();
    assert.equal(card.shadowRoot.querySelector(".scroll-pill").hidden, false);
    const countBadge = card.shadowRoot.querySelector(".timer-badge");
    assert.equal(countBadge.hidden, false);
    assert.equal(countBadge.textContent, "2");
    // Scrolled to the bottom: the pill hides, the count stays.
    dials[1]._rect = { top: 900, bottom: 1150, left: 0, right: 600, width: 600, height: 250 };
    card._updateScrollPill();
    assert.equal(card.shadowRoot.querySelector(".scroll-pill").hidden, true);
    assert.equal(card.shadowRoot.querySelector(".timer-badge").hidden, false);
    assert.equal(card.shadowRoot.querySelector(".timer-badge").textContent, "2");
  } finally {
    clearWindow();
  }
});

// -- R3: general touch duration control (TIMER-17, Q5 SETTLED) --------
// Q5 SETTLED ~19:33 PST Nov 12: the touch duration control is NOT a
// voice-down-only fallback — any duration by touch, ALWAYS. The voice
// ruling ~19:36 PST Nov 12 deletes every voice-down message: the
// per-card voiceUnavailable derivation is gone (see the R1 deleted-
// machinery test), the wall fallback chips are gone, and the control
// does not read voice state at all. These tests prove the picker
// logic, the always-present control, and the absence of voice-down
// text — in every voice state.

test("duration helpers: parse/clamp/total/name (TIMER-17, R3)", () => {
  // Clamping: non-numbers and negatives become 0, over-max clamps.
  assert.equal(clampDurationPart("hours", "3"), 3);
  assert.equal(clampDurationPart("minutes", -5), 0);
  assert.equal(clampDurationPart("seconds", "abc"), 0);
  assert.equal(clampDurationPart("minutes", 90), 59);
  assert.equal(clampDurationPart("hours", 150), 99);
  assert.equal(clampDurationPart("hours", 99), 99);
  assert.equal(clampDurationPart("nope", 5), 0);
  // Parsing: missing fields default to 0, strings coerce.
  assert.deepEqual(parseDurationParts({}), { hours: 0, minutes: 0, seconds: 0 });
  assert.deepEqual(parseDurationParts({ minutes: "7" }), {
    hours: 0,
    minutes: 7,
    seconds: 0,
  });
  assert.deepEqual(parseDurationParts(null), { hours: 0, minutes: 0, seconds: 0 });
  // Totals: the leg durations, exactly.
  assert.equal(durationTotalSeconds({ hours: 0, minutes: 7, seconds: 0 }), 420);
  assert.equal(durationTotalSeconds({ hours: 0, minutes: 1, seconds: 30 }), 90);
  assert.equal(durationTotalSeconds({ hours: 1, minutes: 15, seconds: 0 }), 4500);
  assert.equal(durationTotalSeconds({ hours: 0, minutes: 0, seconds: 0 }), 0);
  // Names: singular/plural, parts joined, zero named but never started.
  assert.equal(formatDurationName(420), "7 minute timer");
  assert.equal(formatDurationName(90), "1 minute 30 second timer");
  assert.equal(formatDurationName(4500), "1 hour 15 minute timer");
  assert.equal(formatDurationName(45), "45 second timer");
  assert.equal(formatDurationName(60), "1 minute timer");
  assert.equal(formatDurationName(3600), "1 hour timer");
  assert.equal(formatDurationName(7260), "2 hour 1 minute timer");
  assert.equal(formatDurationName(0), "0 second timer");
  assert.equal(DURATION_MAX_HOURS, 99);
  assert.deepEqual(DURATION_FIELD_MAX, { hours: 99, minutes: 59, seconds: 59 });
});

function clickAction(card, dataset) {
  card._onClick({ target: { closest: () => ({ dataset }) } });
}

test("wall: the duration control is always present, with presets alongside (TIMER-17, R3)", () => {
  stubWindow(HUB_WINDOW);
  globalThis.window.location = { search: "", pathname: "/family-hub/timers" };
  injectCore();
  try {
    const card = timerCard([wallBrowserPathState("/family-hub/timers")]);
    const screen = card.shadowRoot.querySelector(".screen");
    assert.ok(screen.classList.contains("dev-hub"));
    // The row is shown (not hidden) on the wall even with no timers
    // and no voice state at all — the control is unconditional.
    const row = card.shadowRoot.querySelector(".add-timer-row");
    assert.equal(row.hidden, false);
    // Four preset chips, as before — plus the "Set a time" button.
    const chips = card.shadowRoot.querySelectorAll('[data-action="preset"]');
    assert.equal(chips.length, 4);
    assert.deepEqual(
      chips.map((c) => c.dataset.minutes),
      ["5", "10", "15", "30"]
    );
    const durationButton = card.shadowRoot.querySelector(
      '[data-action="duration-picker"]'
    );
    assert.ok(durationButton, "the Set a time button is present on the wall");
    assert.equal(durationButton.textContent, "Set a time");
    // The picker panel starts closed; the "+ Add timer" toggle button
    // is hidden on the wall (no toggle — the control shows directly).
    assert.equal(
      card.shadowRoot.querySelector(".duration-picker").hidden,
      true
    );
    assert.equal(card.shadowRoot.querySelector(".add-timer-button").hidden, true);
    // No voice-down text anywhere in the rendered card.
    const html = card.shadowRoot.innerHTML;
    assert.doesNotMatch(html, /isn't available/);
    assert.doesNotMatch(html, /voice is down/);
    assert.doesNotMatch(html, /unavailable/);
  } finally {
    resetCore();
    clearWindow();
  }
});

test("wall: the control stays present with voice down AND with zero satellites (TIMER-17, R3)", () => {
  stubWindow(HUB_WINDOW);
  globalThis.window.location = { search: "", pathname: "/family-hub/timers" };
  injectCore();
  try {
    // Voice DOWN: kitchen satellite unavailable, provider unavailable.
    const downStates = [
      wallBrowserPathState("/family-hub/timers"),
      {
        entity_id: "assist_satellite.kitchen",
        state: "unavailable",
        attributes: {},
      },
      {
        entity_id: "sensor.family_hub_voice_status",
        state: "unavailable",
        attributes: { satellite_entity_id: "assist_satellite.kitchen" },
      },
    ];
    const downCard = timerCard(downStates);
    assert.equal(
      downCard.shadowRoot.querySelector(".add-timer-row").hidden,
      false
    );
    assert.ok(downCard.shadowRoot.querySelector('[data-action="duration-picker"]'));
    assert.doesNotMatch(downCard.shadowRoot.innerHTML, /isn't available/);
    assert.doesNotMatch(downCard.shadowRoot.innerHTML, /voice is down/);
    assert.doesNotMatch(downCard.shadowRoot.innerHTML, /unavailable/);
    // ZERO satellites: no assist_satellite entity at all, provider unknown.
    const zeroStates = [
      wallBrowserPathState("/family-hub/timers"),
      {
        entity_id: "sensor.family_hub_voice_status",
        state: "unknown",
        attributes: { satellite_entity_id: "assist_satellite.kitchen" },
      },
    ];
    const zeroCard = timerCard(zeroStates);
    assert.equal(
      zeroCard.shadowRoot.querySelector(".add-timer-row").hidden,
      false
    );
    assert.ok(zeroCard.shadowRoot.querySelector('[data-action="duration-picker"]'));
    assert.doesNotMatch(zeroCard.shadowRoot.innerHTML, /isn't available/);
    assert.doesNotMatch(zeroCard.shadowRoot.innerHTML, /voice is down/);
    assert.doesNotMatch(zeroCard.shadowRoot.innerHTML, /unavailable/);
  } finally {
    resetCore();
    clearWindow();
  }
});

test("picker: steppers move hours/minutes/seconds and clamp at the bounds (TIMER-17, R3)", () => {
  const card = timerCard([]);
  assert.deepEqual(
    { ...card._picker },
    { hours: 0, minutes: 0, seconds: 0, open: false }
  );
  clickAction(card, { action: "duration-picker" });
  assert.equal(card._picker.open, true);
  assert.equal(
    card.shadowRoot.querySelector(".duration-picker").hidden,
    false
  );
  // Stepping below zero stays at zero — the value never goes negative.
  clickAction(card, { action: "picker-step", unit: "minutes", delta: "-5" });
  assert.equal(card._picker.minutes, 0);
  // 7 minutes: +5 then +1 twice. 90 seconds: 1 minute + 30 seconds.
  clickAction(card, { action: "picker-step", unit: "minutes", delta: "5" });
  clickAction(card, { action: "picker-step", unit: "minutes", delta: "1" });
  clickAction(card, { action: "picker-step", unit: "minutes", delta: "1" });
  assert.equal(card._picker.minutes, 7);
  assert.equal(card._pickerValueEls.minutes.textContent, "7");
  clickAction(card, { action: "picker-step", unit: "hours", delta: "1" });
  assert.equal(card._picker.hours, 1);
  assert.equal(card._pickerValueEls.hours.textContent, "1");
  // Clamp: minutes stop at 59 even after repeated +5s.
  for (let i = 0; i < 20; i++) {
    clickAction(card, { action: "picker-step", unit: "minutes", delta: "5" });
  }
  assert.equal(card._picker.minutes, 59);
  // Cancel closes the panel and keeps the values for next time.
  clickAction(card, { action: "picker-cancel" });
  assert.equal(card._picker.open, false);
  assert.equal(
    card.shadowRoot.querySelector(".duration-picker").hidden,
    true
  );
  assert.equal(card._picker.minutes, 59);
});

test("picker: a zero duration is rejected loudly and starts nothing (TIMER-17, R3)", async () => {
  injectCore();
  try {
    const hass = fakeHass([]);
    const card = new TimerScreenCard();
    card.setConfig({});
    card.hass = hass;
    clickAction(card, { action: "duration-picker" });
    assert.equal(card._picker.open, true);
    clickAction(card, { action: "picker-start" });
    await sleep(20);
    // The error shows, the panel stays open, and NO service call
    // was made — never a 0 s timer.
    const errorEl = card.shadowRoot.querySelector(".picker-error");
    assert.equal(errorEl.hidden, false);
    assert.match(errorEl.textContent, /Pick a time first/);
    assert.match(errorEl.textContent, /more than 0 seconds/);
    assert.equal(card._picker.open, true);
    assert.deepEqual(
      hass.wsCalls.filter((c) => c.type === "call_service"),
      []
    );
    // Stepping to a real duration clears the error on the next render.
    clickAction(card, { action: "picker-step", unit: "minutes", delta: "5" });
    assert.equal(errorEl.hidden, true);
  } finally {
    resetCore();
  }
});

test("picker: a 7 minute entry starts through the same service call as a chip (TIMER-17, R3)", async () => {
  injectCore();
  try {
    const hass = fakeHass([]);
    const card = new TimerScreenCard();
    card.setConfig({});
    card.hass = hass;
    clickAction(card, { action: "duration-picker" });
    clickAction(card, { action: "picker-step", unit: "minutes", delta: "5" });
    clickAction(card, { action: "picker-step", unit: "minutes", delta: "1" });
    clickAction(card, { action: "picker-step", unit: "minutes", delta: "1" });
    clickAction(card, { action: "picker-start" });
    await sleep(20);
    assert.deepEqual(
      hass.wsCalls.filter((c) => c.type === "call_service"),
      [
        {
          type: "call_service",
          domain: "satellite_timers",
          service: "start_timer",
          service_data: { name: "7 minute timer", duration: 420 },
          return_response: true,
        },
      ]
    );
    // The panel closed on Start, the entry reset to 0:00:00, and the
    // shared binding path shows the "starting…" dial until the sensor
    // appears.
    assert.equal(card._picker.open, false);
    assert.deepEqual(
      { hours: card._picker.hours, minutes: card._picker.minutes, seconds: card._picker.seconds },
      { hours: 0, minutes: 0, seconds: 0 }
    );
    assert.equal(card.shadowRoot.querySelectorAll('[data-action="starting"]').length, 1);
    assert.equal(card._pendingStart.entityId, "sensor.satellite_timer_new10");
  } finally {
    resetCore();
  }
});

test("picker: a 90 second entry and a 1 hour 15 minute entry start with exact durations (TIMER-17, R3)", async () => {
  injectCore();
  try {
    const hass = fakeHass([]);
    const card = new TimerScreenCard();
    card.setConfig({});
    card.hass = hass;
    // 90 s = 1 minute + 30 seconds (six +5 s steps).
    clickAction(card, { action: "duration-picker" });
    clickAction(card, { action: "picker-step", unit: "minutes", delta: "1" });
    for (let i = 0; i < 6; i++) {
      clickAction(card, { action: "picker-step", unit: "seconds", delta: "5" });
    }
    clickAction(card, { action: "picker-start" });
    await sleep(20);
    const calls = hass.wsCalls.filter((c) => c.type === "call_service");
    assert.equal(calls.length, 1);
    assert.equal(calls[0].service_data.duration, 90);
    assert.equal(calls[0].service_data.name, "1 minute 30 second timer");
    // A second entry in the same mount: 1 hour 15 minutes.
    card.hass = fakeHass([]);
    const hass2 = card._hass;
    clickAction(card, { action: "duration-picker" });
    clickAction(card, { action: "picker-step", unit: "hours", delta: "1" });
    for (let i = 0; i < 3; i++) {
      clickAction(card, { action: "picker-step", unit: "minutes", delta: "5" });
    }
    clickAction(card, { action: "picker-start" });
    await sleep(20);
    const calls2 = hass2.wsCalls.filter((c) => c.type === "call_service");
    assert.equal(calls2.length, 1);
    assert.equal(calls2[0].service_data.duration, 4500);
    assert.equal(calls2[0].service_data.name, "1 hour 15 minute timer");
  } finally {
    resetCore();
  }
});

test("phone: the picker rides in the shared chips row — it shows wherever the chips show (TIMER-17, R3, observation only)", () => {
  // OPEN ITEM for the R7 re-lock (no policy decision here): the picker
  // is built into the shared chips row, so on phone/tablet it appears
  // exactly where the preset chips appear — directly with no timers,
  // behind the "+ Add timer" toggle while timers run. This test
  // records that behaviour; whether phone/tablet SHOULD get arbitrary
  // durations is the re-lock's call, not this step's.
  stubWindow(PHONE_WINDOW);
  injectCore();
  try {
    const empty = timerCard([]);
    assert.ok(empty.shadowRoot.querySelector('[data-action="duration-picker"]'));
    assert.equal(
      empty.shadowRoot.querySelector(".preset-chips").classList.contains("show"),
      true
    );
    const running = timerCard([timerState("t1", "Pasta", "active", {})]);
    assert.ok(running.shadowRoot.querySelector('[data-action="duration-picker"]'));
    // While timers run the chips (and the picker button with them)
    // hide behind the toggle, exactly like the presets.
    assert.equal(
      running.shadowRoot.querySelector(".preset-chips").classList.contains("show"),
      false
    );
  } finally {
    resetCore();
    clearWindow();
  }
});

// -- Wave 1: PIP pin + Home arrival (D7.3 / T-IMPL-20/23) ------------------

function memoryStorage(initial = {}) {
  const store = new Map(Object.entries(initial));
  return {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => store.set(k, v),
    removeItem: (k) => store.delete(k),
    _store: store,
  };
}

test("readTimersPin: JSON object, plain string, missing, cleared after one read", () => {
  assert.equal(readTimersPin(null), null);
  assert.equal(readTimersPin(memoryStorage()), null);
  const store = memoryStorage({
    "family-hub:timers:pin": JSON.stringify({ timerId: "t2" }),
  });
  assert.equal(readTimersPin(store), "t2");
  assert.equal(readTimersPin(store), null);
  const plain = memoryStorage({ "family-hub:timers:pin": "t1" });
  assert.equal(readTimersPin(plain), "t1");
  assert.equal(readTimersPin(plain), null);
});

test("readTimersArrival: 'home' only, cleared after one read", () => {
  assert.equal(readTimersArrival(null), false);
  assert.equal(readTimersArrival(memoryStorage()), false);
  const store = memoryStorage({ "family-hub:timers:arrival": "home" });
  assert.equal(readTimersArrival(store), true);
  assert.equal(readTimersArrival(store), false);
  assert.equal(
    readTimersArrival(memoryStorage({ "family-hub:timers:arrival": '"home"' })),
    true
  );
  assert.equal(
    readTimersArrival(memoryStorage({ "family-hub:timers:arrival": "recipes" })),
    false
  );
});

test("a PIP pin puts that timer first with a 'Just started' marker, read once per mount (D7.3)", () => {
  const storage = memoryStorage({
    "family-hub:timers:pin": JSON.stringify({ timerId: "t2" }),
  });
  globalThis.sessionStorage = storage;
  try {
    const card = timerCard([
      timerState("t1", "Pasta", "active", { remaining: 300 }),
      timerState("t2", "Rice", "active", { remaining: 600 }),
    ]);
    const dials = [...card.shadowRoot.querySelectorAll(".dial")];
    assert.equal(dials[0].dataset.timerId, "t2");
    assert.equal(
      card.shadowRoot.querySelector('[data-started="t2"]').textContent,
      "Just started"
    );
    assert.equal(
      card.shadowRoot.querySelector('[data-started="t1"]').textContent,
      ""
    );
    // The pin was consumed: a second mount with the same storage
    // does not re-pin — t1 (300 s, soonest) sorts first again.
    const second = timerCard([
      timerState("t1", "Pasta", "active", { remaining: 300 }),
      timerState("t2", "Rice", "active", { remaining: 600 }),
    ]);
    assert.equal(
      [...second.shadowRoot.querySelectorAll(".dial")][0].dataset.timerId,
      "t1"
    );
  } finally {
    delete globalThis.sessionStorage;
  }
});

// -- Wave 1: finished-section collapse rules (D1.2) ------------------------

test("the finished section is forced open while a timer finished < 60 s ago, collapsed otherwise with timers running (R2, §5.1 F6)", () => {
  // A timer that finished 20 s ago forces the section open even
  // while another timer runs — this replaces the old ringing force
  // (the `ringing` state is deleted in R2).
  const card = timerCard([
    timerState("t1", "Pasta", "active", { remaining: 600 }),
    timerState("t2", "Tea", "finished", {
      finishedAt: new Date(Date.now() - 20000).toISOString(),
    }),
  ]);
  const header = card.shadowRoot.querySelector(".finished-header");
  assert.equal(header.getAttribute("aria-expanded"), "true");
  assert.equal(
    card.shadowRoot.querySelector('[data-finished-sub="t2"]').textContent,
    "Done — just now · Tap to dismiss"
  );
  // Finished 3 min ago: the window has passed, so the section falls
  // back to the running-timers rule (collapsed while a timer runs).
  const settled = timerCard([
    timerState("t1", "Pasta", "active", { remaining: 600 }),
    timerState("t2", "Tea", "finished", {
      finishedAt: new Date(Date.now() - 180000).toISOString(),
    }),
  ]);
  assert.equal(
    settled.shadowRoot.querySelector(".finished-header").getAttribute("aria-expanded"),
    "false"
  );
  // Nothing running: expanded on arrival.
  const alone = timerCard([
    timerState("t2", "Tea", "finished", {
      finishedAt: new Date(Date.now() - 20000).toISOString(),
    }),
  ]);
  assert.equal(
    alone.shadowRoot.querySelector(".finished-header").getAttribute("aria-expanded"),
    "true"
  );
});

test("the finished section closes again on the same mount once the < 60 s window ends (R2, §5.1 F6)", () => {
  const card = timerCard([
    timerState("t1", "Pasta", "active", { remaining: 600 }),
    timerState("t2", "Tea", "finished", {
      finishedAt: new Date(Date.now() - 20000).toISOString(),
    }),
  ]);
  const header = () => card.shadowRoot.querySelector(".finished-header");
  // Forced open while the finish is recent.
  assert.equal(header().getAttribute("aria-expanded"), "true");
  // Window over on the SAME card: re-sync with Tea finished 3 min
  // ago (the re-sync stands in for the 60 s passing — the unit run
  // does not wait a minute). The old code stored the ringing force
  // in _finishedOpen, so the section stuck open after the window
  // (rig: still expanded 90 s after the ring with Pasta running).
  // D1.2 says collapsed by default whenever anything runs, so it
  // must fall back to collapsed.
  card.hass = fakeHass([
    timerState("t1", "Pasta", "active", { remaining: 600 }),
    timerState("t2", "Tea", "finished", {
      finishedAt: new Date(Date.now() - 180000).toISOString(),
    }),
  ]);
  assert.equal(header().getAttribute("aria-expanded"), "false");
});

test("the finished-section toggle flips it and survives a re-sync", () => {
  const card = timerCard([
    timerState("t1", "Pasta", "active", { remaining: 600 }),
    timerState("t2", "Tea", "finished", {}),
  ]);
  const header = () => card.shadowRoot.querySelector(".finished-header");
  assert.equal(header().getAttribute("aria-expanded"), "false");
  card._onClick({
    target: { closest: () => ({ dataset: { action: "finished-toggle" } }) },
  });
  assert.equal(header().getAttribute("aria-expanded"), "true");
  // A re-sync (same timers) keeps the user's toggle state.
  card.hass = fakeHass([
    timerState("t1", "Pasta", "active", { remaining: 600 }),
    timerState("t2", "Tea", "finished", {}),
  ]);
  assert.equal(header().getAttribute("aria-expanded"), "true");
});

// -- Wave 1: time fit inside the ring (T-IMPL-5 / NAV-O2) -----------------

test("_fitTimes: a time that fits at CSS size is untouched; one wider than the ring steps to the 40px floor", () => {
  const card = timerCard([timerState("t1", "Pasta", "active", {})]);
  const refs = card._dialEls.get("t1")._refs;
  refs.button._rect = { top: 0, bottom: 200, left: 0, right: 200, width: 200, height: 200 };
  // Fits: 150px wide in a 200px ring (limit 184px).
  refs.time.scrollWidth = 150;
  card._fitTimes();
  assert.equal(refs.time.style.fontSize, undefined);
  // Too wide: the pinned scrollWidth never shrinks, so the fit runs
  // the whole ladder and lands on the 40px floor.
  refs.time.scrollWidth = 500;
  card._fitTimes();
  assert.equal(refs.time.style.fontSize, "40px");
});

test("_fitTimes: skips silently with no layout engine (no scrollWidth, zero ring width)", () => {
  const card = timerCard([timerState("t1", "Pasta", "active", {})]);
  const refs = card._dialEls.get("t1")._refs;
  // No scrollWidth pinned at all: the guard skips the dial.
  card._fitTimes();
  assert.equal(refs.time.style.fontSize, undefined);
  // A zero-width ring is skipped too.
  refs.time.scrollWidth = 500;
  refs.button._rect = { top: 0, bottom: 0, left: 0, right: 0, width: 0, height: 0 };
  card._fitTimes();
  assert.equal(refs.time.style.fontSize, undefined);
});

// -- Wave 1: soon flip from the sensor's remaining (T-IMPL-13 / OQ-5) -----

test("the soon word flips at 59 s from the sensor remaining, not the displayed time", () => {
  const soon = timerCard([timerState("t1", "Pasta", "active", { remaining: 59 })]);
  assert.equal(
    soon.shadowRoot.querySelector('[data-state="t1"]').textContent,
    "Soon · Tap to pause"
  );
  assert.equal(soon.shadowRoot.querySelector('[data-hint="t1"]').textContent, "");
  const notSoon = timerCard([timerState("t1", "Pasta", "active", { remaining: 60 })]);
  assert.equal(notSoon.shadowRoot.querySelector('[data-state="t1"]').textContent, "");
  assert.equal(
    notSoon.shadowRoot.querySelector('[data-hint="t1"]').textContent,
    "Tap to pause"
  );
  // Paused at 30 s: "Paused", never "Soon".
  const paused = timerCard([timerState("t1", "Pasta", "paused", { remaining: 30 })]);
  assert.equal(paused.shadowRoot.querySelector('[data-state="t1"]').textContent, "Paused");
  assert.equal(
    paused.shadowRoot.querySelector('[data-hint="t1"]').textContent,
    "Tap to resume"
  );
});

// -- Screensaver mount (§7.5, RB-R4-1) ---------------------------------------
//
// The shared singleton's refcount is file-global (one core module per
// test file), so these tests read the count before and after instead
// of assuming it starts at zero.

test("Screensaver mount: connect attaches, hass pushes update without touching the refcount, disconnect detaches", () => {
  injectCore();
  try {
    const before = core.screensaver.getRefcount();
    const card = timerCard([
      timerState("t1", "Pasta", "active", { remaining: 600 }),
    ]);
    card.connectedCallback();
    assert.equal(core.screensaver.getRefcount(), before + 1);
    // Every hass push feeds the controller + the PIP Host; the
    // refcount is mount-scoped and never moves on a push.
    card.hass = fakeHass([
      timerState("t1", "Pasta", "active", { remaining: 599 }),
    ]);
    card.hass = fakeHass([
      timerState("t1", "Pasta", "active", { remaining: 598 }),
    ]);
    assert.equal(core.screensaver.getRefcount(), before + 1);
    card.disconnectedCallback();
    assert.equal(core.screensaver.getRefcount(), before);
  } finally {
    resetCore();
  }
});

test("Screensaver mount: two cards mounted at once attach twice; each detach decrements", () => {
  injectCore();
  try {
    const before = core.screensaver.getRefcount();
    const first = timerCard([
      timerState("t1", "Pasta", "active", { remaining: 600 }),
    ]);
    const second = timerCard([
      timerState("t2", "Rice", "active", { remaining: 300 }),
    ]);
    first.connectedCallback();
    second.connectedCallback();
    assert.equal(core.screensaver.getRefcount(), before + 2);
    first.disconnectedCallback();
    assert.equal(core.screensaver.getRefcount(), before + 1);
    second.disconnectedCallback();
    assert.equal(core.screensaver.getRefcount(), before);
  } finally {
    resetCore();
  }
});

test("Screensaver mount: attach is once per instance even when core loads after connect", () => {
  resetCore();
  try {
    const before = core.screensaver.getRefcount();
    const card = timerCard([
      timerState("t1", "Pasta", "active", { remaining: 600 }),
    ]);
    card.connectedCallback();
    assert.equal(core.screensaver.getRefcount(), before);
    // Core loads: the re-apply attaches the live card once.
    injectCore();
    assert.equal(core.screensaver.getRefcount(), before + 1);
    // A second connect-side attach attempt is a no-op (the flag).
    card._screensaverAttach();
    assert.equal(core.screensaver.getRefcount(), before + 1);
    card.disconnectedCallback();
    assert.equal(core.screensaver.getRefcount(), before);
  } finally {
    resetCore();
  }
});
