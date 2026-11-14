// Tests for the new timer-screen card (custom:timer-screen, Phase 2
// Wave W5, from scratch). The card needs a DOM to load, so it is
// exercised here through a minimal harness: elements with children,
// attributes, classList, style, textContent, querySelector /
// querySelectorAll, event bubbling with stopPropagation, and
// attachShadow. The card never touches anything else.
//
// Split: pure helpers + CardSendProtocol + store-driven rendering are
// tested here in node; layout, viewports, the real bridge, and the
// ringing representation are proven by the rig legs in
// hidden_files/redesign/w5-legs/ (the browser is the only place those
// can be measured).
//
// Equivalence tests (formatRemaining / afterAnimation) import the real
// core modules from pi-family-hub when present
// (FAMILY_HUB_CORE_DIR or the workspace default). When the core
// checkout is absent the equivalence tests skip with a clear note —
// the repo's own tests never fail for a missing sibling checkout.

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { pathToFileURL } = require("node:url");

const CARD_PATH = path.join(__dirname, "timer-screen-card.js");
const CARD_SOURCE = fs.readFileSync(CARD_PATH, "utf8");

// ---------------------------------------------------------------------------
// Minimal DOM
// ---------------------------------------------------------------------------

function matchesSelector(el, selector) {
  const sel = selector.trim();
  if (!sel) return false;
  let tag = null;
  let rest = sel;
  const tagMatch = /^([a-zA-Z][\w-]*)/.exec(rest);
  if (tagMatch) {
    tag = tagMatch[1].toUpperCase();
    rest = rest.slice(tagMatch[0].length);
  }
  if (tag && el.tagName !== tag) return false;
  // Any number of .class / [attr] / [attr="value"] segments, in any
  // order — the card's selectors combine them (e.g. a preset chip is
  // [data-action="preset"][data-minutes="10"]).
  while (rest.length > 0) {
    const classMatch = /^\.([\w-]+)/.exec(rest);
    if (classMatch) {
      if (!el.classList.contains(classMatch[1])) return false;
      rest = rest.slice(classMatch[0].length);
      continue;
    }
    const attrMatch = /^\[([\w-]+)(?:=(?:"([^"]*)"|'([^']*)'))?\]/.exec(rest);
    if (attrMatch) {
      const value = el.getAttribute(attrMatch[1]);
      if (value === null) return false;
      const wanted = attrMatch[2] ?? attrMatch[3];
      if (wanted !== undefined && value !== wanted) return false;
      rest = rest.slice(attrMatch[0].length);
      continue;
    }
    return false;
  }
  return true;
}

class FakeClassList {
  constructor(el) {
    this._el = el;
  }
  add(...names) {
    for (const n of names) this._el._classes.add(n);
  }
  remove(...names) {
    for (const n of names) this._el._classes.delete(n);
  }
  toggle(name, force) {
    if (force === undefined) {
      if (this._el._classes.has(name)) this._el._classes.delete(name);
      else this._el._classes.add(name);
    } else if (force) this._el._classes.add(name);
    else this._el._classes.delete(name);
    return this._el._classes.has(name);
  }
  contains(name) {
    return this._el._classes.has(name);
  }
}

class FakeElement {
  constructor(tag = "div") {
    this.tagName = String(tag).toUpperCase();
    this.children = [];
    this.parentNode = null;
    this._attrs = {};
    this._classes = new Set();
    this._classList = new FakeClassList(this);
    this.style = {};
    this._listeners = {};
    this._text = "";
    this.hidden = false;
    this.disabled = false;
  }
  get classList() {
    return this._classList;
  }
  get className() {
    return [...this._classes].join(" ");
  }
  set className(value) {
    this._classes = new Set(String(value ?? "").split(/\s+/).filter(Boolean));
  }
  get firstChild() {
    return this.children[0] ?? null;
  }
  appendChild(child) {
    child.parentNode = this;
    this.children.push(child);
    return child;
  }
  removeChild(child) {
    const i = this.children.indexOf(child);
    if (i >= 0) this.children.splice(i, 1);
    child.parentNode = null;
    return child;
  }
  remove() {
    if (this.parentNode) this.parentNode.removeChild(this);
  }
  setAttribute(name, value) {
    this._attrs[name] = String(value);
  }
  getAttribute(name) {
    return name in this._attrs ? this._attrs[name] : null;
  }
  set textContent(value) {
    this.children = [];
    this._text = value == null ? "" : String(value);
  }
  get textContent() {
    return this._text + this.children.map((c) => c.textContent).join("");
  }
  addEventListener(type, fn) {
    if (!this._listeners[type]) this._listeners[type] = [];
    this._listeners[type].push(fn);
  }
  removeEventListener(type, fn) {
    const list = this._listeners[type];
    if (!list) return;
    const i = list.indexOf(fn);
    if (i >= 0) list.splice(i, 1);
  }
  dispatchEvent(event) {
    const evt = event;
    if (!evt.target) evt.target = this;
    if (typeof evt.stopPropagation !== "function") {
      let stopped = false;
      evt.stopPropagation = () => {
        stopped = true;
      };
      evt._isStopped = () => stopped;
    }
    let node = this;
    while (node) {
      const list = node._listeners[evt.type] || [];
      for (const fn of [...list]) fn(evt);
      if (evt._isStopped && evt._isStopped()) break;
      node = node.parentNode;
    }
    return true;
  }
  click() {
    this.dispatchEvent({ type: "click" });
  }
  closest(selector) {
    let node = this;
    while (node) {
      if (matchesSelector(node, selector)) return node;
      node = node.parentNode;
    }
    return null;
  }
  querySelector(selector) {
    const all = this.querySelectorAll(selector);
    return all[0] ?? null;
  }
  querySelectorAll(selector) {
    const found = [];
    const walk = (node) => {
      for (const child of node.children) {
        if (matchesSelector(child, selector)) found.push(child);
        walk(child);
      }
    };
    walk(this);
    return found;
  }
  scrollIntoView() {}
  getAnimations() {
    return [];
  }
}

class FakeHTMLElement extends FakeElement {
  constructor() {
    super("div");
    this.shadowRoot = null;
  }
  attachShadow() {
    const root = new FakeElement("shadow-root");
    this.shadowRoot = root;
    return root;
  }
}

class FakeDocument {
  constructor() {
    this.documentElement = new FakeElement("html");
    this.body = new FakeElement("body");
  }
  createElement(tag) {
    const registered =
      globalThis.customElements && globalThis.customElements.get(tag);
    if (registered) return new registered();
    return new FakeElement(tag);
  }
  createElementNS(_ns, tag) {
    return new FakeElement(tag);
  }
}

function makeSessionStorage(initial = {}) {
  const store = new Map(Object.entries(initial));
  return {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => store.set(k, String(v)),
    removeItem: (k) => store.delete(k),
  };
}

function installDom({ sessionStorage: storage = null } = {}) {
  globalThis.HTMLElement = FakeHTMLElement;
  globalThis.customElements = {
    _defs: {},
    define(name, ctor) {
      this._defs[name] = ctor;
    },
    get(name) {
      return this._defs[name] || undefined;
    },
  };
  globalThis.document = new FakeDocument();
  globalThis.sessionStorage = storage ?? makeSessionStorage();
}

// The card module must be required AFTER installDom (it extends the
// global HTMLElement at load time). Require once, reuse everywhere.
installDom();
const cardModule = require("./timer-screen-card.js");

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const UUID_V4 =
  /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

function flush(ms = 0) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// A minimal TimerSensorStore double: the card trusts the store's order
// (running-first is the store's job, proven in core) and reads only
// snapshot().
function makeStore(records = []) {
  let current = [...records];
  return {
    snapshot() {
      return [...current];
    },
    byEntityId(entityId) {
      return current.find((r) => r.entityId === entityId) ?? null;
    },
    setRecords(next) {
      current = [...next];
    },
  };
}

function makeRecord(overrides = {}) {
  const now = new Date();
  return {
    entityId: "sensor.satellite_timer_01K8PASTA",
    timerId: "01K8PASTA",
    state: "active",
    name: "Pasta",
    remainingSeconds: 290,
    finishesAt: new Date(now.getTime() + 290 * 1000).toISOString(),
    startedAt: new Date(now.getTime() - 10 * 1000).toISOString(),
    durationSeconds: 300,
    announceOn: null,
    alarmMissed: false,
    alarmMissedReason: null,
    lastChanged: now.toISOString(),
    lastUpdated: now.toISOString(),
    ...overrides,
  };
}

function mountCard({ store = null, hass = null, config = {} } = {}) {
  const card = document.createElement("timer-screen");
  if (store) card._setStoreForTests(store);
  if (hass) card._setHassForTests(hass);
  card.setConfig(config);
  card.connectedCallback();
  if (hass) card.hass = hass;
  return card;
}

function unmountCard(card) {
  try {
    card.disconnectedCallback();
  } catch (_err) {
    // A disconnect that throws still counts as unmounted for the test.
  }
}

function findButton(root, action, extra = "") {
  return root.querySelector(`[data-action="${action}"]${extra}`);
}

// A fake hass: callService is recorded (and can be made to hang,
// reject, or succeed); callWS answers get_services +
// lovelace/resources/list from the test's tables.
function makeFakeHass({ states = [], services = null, resources = [] } = {}) {
  const calls = [];
  const behavior = { mode: "ok", error: null, hang: false };
  let deferred = null;
  const hass = {
    states,
    calls,
    behavior,
    connection: { subscribeEvents: () => () => {} },
    callService(domain, service, data) {
      calls.push({ domain, service, data });
      if (behavior.hang) {
        return new Promise(() => {});
      }
      if (behavior.mode === "raise") {
        return Promise.reject(behavior.error ?? new Error("boom"));
      }
      return Promise.resolve({ ok: true });
    },
    callWS(message) {
      if (message.type === "get_services") {
        return Promise.resolve(services ?? { satellite_timers: { start_timer: {} } });
      }
      if (message.type === "lovelace/resources/list") {
        return Promise.resolve(resources);
      }
      return Promise.reject(new Error(`unexpected callWS ${message.type}`));
    },
  };
  hass._deferred = deferred;
  return hass;
}

// A controllable clock for CardSendProtocol's schedule/cancelTimer.
function makeFakeClock() {
  let now = 0;
  let nextId = 1;
  const timers = new Map();
  return {
    now: () => now,
    schedule(fn, ms) {
      const id = nextId;
      nextId += 1;
      timers.set(id, { fn, at: now + ms });
      return id;
    },
    cancelTimer(id) {
      timers.delete(id);
    },
    advance(ms) {
      now += ms;
      const due = [...timers.entries()]
        .filter(([, t]) => t.at <= now)
        .sort((a, b) => a[1].at - b[1].at);
      for (const [id, t] of due) {
        timers.delete(id);
        t.fn();
      }
    },
    pendingCount() {
      return timers.size;
    },
  };
}

function coreDir() {
  if (process.env.FAMILY_HUB_CORE_DIR) return process.env.FAMILY_HUB_CORE_DIR;
  return path.join(
    __dirname,
    "..",
    "..",
    "..",
    "..",
    "..",
    "hidden_files",
    "redesign",
    "worktrees",
    "local-git",
    "pi-family-hub",
    "core",
  );
}

async function importCoreModule(name, t) {
  const file = path.join(coreDir(), name);
  if (!fs.existsSync(file)) {
    t.skip(`core module ${name} not present (sibling checkout absent)`);
    return null;
  }
  return import(pathToFileURL(file).href);
}

// ---------------------------------------------------------------------------
// Pure helpers
// ---------------------------------------------------------------------------

test("formatRemaining: M:SS below an hour, H:MM:SS above, clamped at zero", () => {
  const { formatRemaining } = cardModule;
  assert.equal(formatRemaining(0), "0:00");
  assert.equal(formatRemaining(5), "0:05");
  assert.equal(formatRemaining(59), "0:59");
  assert.equal(formatRemaining(60), "1:00");
  assert.equal(formatRemaining(90), "1:30");
  assert.equal(formatRemaining(3599), "59:59");
  assert.equal(formatRemaining(3600), "1:00:00");
  assert.equal(formatRemaining(3661), "1:01:01");
  assert.equal(formatRemaining(86399), "23:59:59");
  assert.equal(formatRemaining(-5), "0:00");
  assert.equal(formatRemaining(90.9), "1:30");
});

test("formatRemaining matches core/format.js on every finite input (J-W5-03)", async (t) => {
  const core = await importCoreModule("format.js", t);
  if (!core) return;
  const { formatRemaining } = cardModule;
  const inputs = [0, 1, 5, 59, 60, 61, 90, 599, 3599, 3600, 3661, 7325, 86399, 86400, "90", 90.9];
  for (const input of inputs) {
    assert.equal(
      formatRemaining(input),
      core.formatRemaining(input),
      `input ${JSON.stringify(input)}`,
    );
  }
  // Known, documented divergence (not a failure): non-numeric input.
  // Core yields "NaN:NaN" (Math.max(0, NaN) is NaN); the card clamps
  // to "0:00". Unreachable in production — the store drops a sensor
  // whose remaining_seconds is not finite — so the card keeps the
  // defensive form and the equivalence table above stays finite-only.
  assert.equal(cardModule.formatRemaining(NaN), "0:00");
  assert.equal(core.formatRemaining(NaN), "NaN:NaN");
});

test("formatMinutesAgo: just now, minutes, null for unknown/future", () => {
  const { formatMinutesAgo } = cardModule;
  const now = Date.now();
  assert.equal(formatMinutesAgo(now - 30 * 1000, now), "just now");
  assert.equal(formatMinutesAgo(now - 60 * 1000, now), "1 min ago");
  assert.equal(formatMinutesAgo(now - 3 * 60 * 1000, now), "3 min ago");
  assert.equal(formatMinutesAgo(now - 61 * 60 * 1000, now), "61 min ago");
  assert.equal(formatMinutesAgo(NaN, now), null);
  assert.equal(formatMinutesAgo(now + 60 * 1000, now), null);
});

test("formatDurationName: singular units at every value", () => {
  const { formatDurationName } = cardModule;
  assert.equal(formatDurationName(45), "45 second timer");
  assert.equal(formatDurationName(60), "1 minute timer");
  assert.equal(formatDurationName(600), "10 minute timer");
  assert.equal(formatDurationName(90), "1 minute 30 second timer");
  assert.equal(formatDurationName(4500), "1 hour 15 minute timer");
  assert.equal(formatDurationName(3600), "1 hour timer");
  assert.equal(formatDurationName(0), "0 second timer");
});

test("duration parts: clamp, parse, total", () => {
  const { clampDurationPart, parseDurationParts, durationTotalSeconds } = cardModule;
  assert.equal(clampDurationPart("hours", 99), 24);
  assert.equal(clampDurationPart("minutes", 60), 59);
  assert.equal(clampDurationPart("seconds", -3), 0);
  assert.equal(clampDurationPart("seconds", "abc"), 0);
  assert.equal(clampDurationPart("nope", 5), 0);
  assert.deepEqual(parseDurationParts({ hours: "2", minutes: 70 }), {
    hours: 2,
    minutes: 59,
    seconds: 0,
  });
  assert.deepEqual(parseDurationParts(null), { hours: 0, minutes: 0, seconds: 0 });
  assert.equal(durationTotalSeconds({ hours: 1, minutes: 15, seconds: 0 }), 4500);
  assert.equal(durationTotalSeconds({ hours: 24, minutes: 0, seconds: 0 }), 86400);
});

test("evaluateBridgeGate: FULL / card-too-old / bridge-too-old / unreadable", () => {
  const { evaluateBridgeGate } = cardModule;
  assert.equal(evaluateBridgeGate("2.0.0", "2.0.0").gate, "FULL");
  assert.equal(evaluateBridgeGate("2.0.0", null).gate, "FULL");
  assert.equal(evaluateBridgeGate("2.1.0", "2.0.0").gate, "FULL");
  const tooOldCard = evaluateBridgeGate("3.0.0", "3.0.0");
  assert.equal(tooOldCard.gate, "DEGRADED");
  assert.equal(tooOldCard.reason, "card-too-old");
  const tooOldBridge = evaluateBridgeGate("1.0.0", "1.0.0");
  assert.equal(tooOldBridge.gate, "DEGRADED");
  assert.equal(tooOldBridge.reason, "bridge-too-old");
  assert.equal(evaluateBridgeGate("unknown", null).gate, "DEGRADED");
  assert.equal(evaluateBridgeGate(null, null).gate, "DEGRADED");
});

test("gateNoticeCopy: null when FULL, names versions when DEGRADED", () => {
  const { gateNoticeCopy } = cardModule;
  assert.equal(gateNoticeCopy({ gate: "FULL" }), null);
  assert.equal(gateNoticeCopy(null), null);
  const copy = gateNoticeCopy({
    gate: "DEGRADED",
    reason: "bridge-too-old",
    detail: "The bridge (1.0.0) is older than this card requires (minimum bridge 2.0.0).",
    bridgeVersion: "1.0.0",
  });
  assert.match(copy.body, /2\.0\.0/);
  assert.match(copy.body, /1\.0\.0/);
  assert.equal(copy.repairLabel, "Repair — re-run the setup wizard");
});

test("missed-alarm and finished readings", () => {
  const { missedAlarmReading, finishedReading } = cardModule;
  assert.equal(
    missedAlarmReading("3 min ago"),
    "Done — the alarm didn't sound · 3 min ago",
  );
  assert.equal(missedAlarmReading(null), "Done — the alarm didn't sound");
  assert.equal(finishedReading("3 min ago"), "Done — 3 min ago");
  assert.equal(finishedReading(null), "Done");
});

test("newCommandId: UUID v4 shape, unique per call", () => {
  const { newCommandId } = cardModule;
  const seen = new Set();
  for (let i = 0; i < 1000; i += 1) {
    const id = newCommandId();
    assert.match(id, UUID_V4);
    assert.ok(!seen.has(id), "duplicate command_id");
    seen.add(id);
  }
});

test("pin / arrival / scroll: read-once, clear, garbage-safe", () => {
  const { readTimersPin, readTimersArrival, parseTimerScroll, serializeTimerScroll } = cardModule;
  const storage = makeSessionStorage({
    "family-hub:timers:pin": JSON.stringify({ timerId: "01K8PASTA" }),
    "family-hub:timers:arrival": "home",
    "family-hub:timers:scroll": JSON.stringify({ page: 120, screen: 0 }),
  });
  assert.equal(readTimersPin(storage), "01K8PASTA");
  assert.equal(storage.getItem("family-hub:timers:pin"), null);
  assert.equal(readTimersArrival(storage), true);
  assert.equal(storage.getItem("family-hub:timers:arrival"), null);
  assert.deepEqual(parseTimerScroll(storage.getItem("family-hub:timers:scroll")), {
    page: 120,
    screen: 0,
  });
  assert.deepEqual(parseTimerScroll("garbage"), null);
  assert.deepEqual(parseTimerScroll(JSON.stringify({ page: -5, screen: "x" })), {
    page: 0,
    screen: 0,
  });
  assert.equal(
    serializeTimerScroll({ page: 120, screen: 0 }),
    JSON.stringify({ page: 120, screen: 0 }),
  );
  const bareStorage = makeSessionStorage({ "family-hub:timers:pin": "01K8RICE" });
  assert.equal(readTimersPin(bareStorage), "01K8RICE");
  const badStorage = makeSessionStorage({ "family-hub:timers:pin": "{nope" });
  assert.equal(readTimersPin(badStorage), "{nope");
});

test("afterAnimation matches core/timers.js on fake elements (J-W5-03)", async (t) => {
  const core = await importCoreModule("timers.js", t);
  if (!core) return;
  const { afterAnimation } = cardModule;
  const noAnimations = { getAnimations: () => [] };
  for (const fn of [afterAnimation, core.afterAnimation]) {
    const start = Date.now();
    await fn(noAnimations);
    assert.ok(Date.now() - start < 100, "no animations resolves at once");
  }
  const missingFn = {};
  for (const fn of [afterAnimation, core.afterAnimation]) {
    await fn(missingFn);
  }
  // A running animation whose `finished` resolves: both wait for it.
  for (const fn of [afterAnimation, core.afterAnimation]) {
    let resolveFinished;
    const finished = new Promise((r) => {
      resolveFinished = r;
    });
    const el = {
      getAnimations: () => [{ playState: "running", finished }],
    };
    let done = false;
    const waiting = fn(el).then(() => {
      done = true;
    });
    await flush(10);
    assert.equal(done, false, "still waiting while the animation runs");
    resolveFinished();
    await waiting;
    assert.equal(done, true);
  }
  // A running animation with NO `finished` promise: both wait for the
  // bound (the partial set is never awaited).
  for (const fn of [afterAnimation, core.afterAnimation]) {
    const el = { getAnimations: () => [{ playState: "running" }] };
    const start = Date.now();
    await fn(el, { timeoutMs: 120 });
    const elapsed = Date.now() - start;
    assert.ok(elapsed >= 100 && elapsed < 400, `bound wait, got ${elapsed} ms`);
  }
});

// ---------------------------------------------------------------------------
// CardSendProtocol (M4/M5)
// ---------------------------------------------------------------------------

test("protocol: one send, one fresh command_id, ack resolves", async () => {
  const { CardSendProtocol } = cardModule;
  const hass = makeFakeHass();
  const protocol = new CardSendProtocol(hass);
  const handle = protocol.send("start_timer", { name: "10 minute timer", duration_seconds: 600 });
  assert.match(handle.commandId, UUID_V4);
  const result = await handle.promise;
  assert.equal(result.commandId, handle.commandId);
  assert.equal(hass.calls.length, 1);
  assert.equal(hass.calls[0].domain, "satellite_timers");
  assert.equal(hass.calls[0].service, "start_timer");
  assert.equal(hass.calls[0].data.command_id, handle.commandId);
  assert.equal(hass.calls[0].data.name, "10 minute timer");
});

test("protocol: timeout shows working, re-sends SAME id once, then fails", async () => {
  const { CardSendProtocol, ACK_TIMEOUT_MS } = cardModule;
  const clock = makeFakeClock();
  const hass = makeFakeHass();
  hass.behavior.hang = true;
  const protocol = new CardSendProtocol(hass, {
    schedule: (fn, ms) => clock.schedule(fn, ms),
    cancelTimer: (id) => clock.cancelTimer(id),
  });
  let workingCount = 0;
  const handle = protocol.send("start_timer", { name: "Pasta", duration_seconds: 300 }, {
    onWorking: () => {
      workingCount += 1;
    },
  });
  let settled = null;
  handle.promise.then(
    () => {
      settled = "resolved";
    },
    (error) => {
      settled = error;
    },
  );
  await flush();
  assert.equal(hass.calls.length, 1);
  assert.equal(workingCount, 0);
  clock.advance(ACK_TIMEOUT_MS);
  await flush();
  assert.equal(workingCount, 1, "working indicator on first timeout");
  assert.equal(hass.calls.length, 2, "re-sent once");
  assert.equal(hass.calls[1].data.command_id, handle.commandId, "same command_id");
  clock.advance(ACK_TIMEOUT_MS);
  await flush();
  assert.ok(settled instanceof Error, "rejects after the second timeout");
  assert.equal(settled.kind, "timeout");
  assert.equal(hass.calls.length, 2, "no third automatic send");
  assert.equal(clock.pendingCount(), 0, "no timer left running");
});

test("protocol: Retry re-sends the SAME command_id, and an ack then resolves", async () => {
  const { CardSendProtocol, ACK_TIMEOUT_MS } = cardModule;
  const clock = makeFakeClock();
  const hass = makeFakeHass();
  hass.behavior.hang = true;
  const protocol = new CardSendProtocol(hass, {
    schedule: (fn, ms) => clock.schedule(fn, ms),
    cancelTimer: (id) => clock.cancelTimer(id),
  });
  const handle = protocol.send("start_timer", { name: "Pasta", duration_seconds: 300 });
  let firstError = null;
  handle.promise.catch((error) => {
    firstError = error;
  });
  clock.advance(ACK_TIMEOUT_MS);
  await flush();
  clock.advance(ACK_TIMEOUT_MS);
  await flush();
  assert.equal(firstError.kind, "timeout");
  assert.equal(hass.calls.length, 2);
  // The bridge comes back: Retry succeeds with the same id.
  hass.behavior.hang = false;
  const retryHandle = handle.retry();
  assert.equal(retryHandle.commandId, handle.commandId);
  const result = await retryHandle.promise;
  assert.equal(result.commandId, handle.commandId);
  assert.equal(hass.calls.length, 3);
  assert.equal(hass.calls[2].data.command_id, handle.commandId);
});

test("protocol: immediate rejection is an immediate error, no auto re-send", async () => {
  const { CardSendProtocol } = cardModule;
  const hass = makeFakeHass();
  hass.behavior.mode = "raise";
  hass.behavior.error = new Error("Service satellite_timers.start_timer not found");
  const protocol = new CardSendProtocol(hass);
  const handle = protocol.send("start_timer", { name: "Pasta", duration_seconds: 300 });
  let error = null;
  handle.promise.catch((e) => {
    error = e;
  });
  await flush();
  await flush();
  assert.equal(error.kind, "service-not-found");
  assert.equal(hass.calls.length, 1, "a refused call is not re-sent automatically");
});

test("protocol: the ack carries the service call's answer (start timer_id)", async () => {
  const { CardSendProtocol } = cardModule;
  const hass = makeFakeHass();
  // The bridge answers a start with the timer's state, including its
  // timer id (services.py: supports_response=OPTIONAL; bridge
  // _state_response carries timer_id). The protocol surfaces the
  // answer so the card can key the after-start scroll on the id
  // (NB-1) instead of the display name.
  hass.callService = () =>
    Promise.resolve({
      timer_id: "01K8PASTA",
      state: "active",
      remaining_seconds: 300,
      name: "5 minute timer",
      duration_seconds: 300,
      finishes_at: null,
      finished_at: null,
    });
  const protocol = new CardSendProtocol(hass);
  const handle = protocol.send("start_timer", { name: "5 minute timer", duration_seconds: 300 });
  const result = await handle.promise;
  assert.equal(result.commandId, handle.commandId);
  assert.equal(result.response.timer_id, "01K8PASTA");
});

test("protocol: two gestures in flight are two timers, never suppressed (M4.6)", async () => {
  const { CardSendProtocol } = cardModule;
  const hass = makeFakeHass();
  hass.behavior.hang = true;
  const protocol = new CardSendProtocol(hass);
  const first = protocol.send("start_timer", { name: "Pasta", duration_seconds: 300 });
  const second = protocol.send("start_timer", { name: "Rice", duration_seconds: 600 });
  await flush();
  assert.notEqual(first.commandId, second.commandId);
  assert.equal(hass.calls.length, 2, "the second gesture sends while the first is pending");
  assert.equal(hass.calls[0].data.name, "Pasta");
  assert.equal(hass.calls[1].data.name, "Rice");
});

test("protocol: a late ack after the timeout is harmless", async () => {
  const { CardSendProtocol, ACK_TIMEOUT_MS } = cardModule;
  const clock = makeFakeClock();
  const hass = makeFakeHass();
  let releaseFirst = null;
  let callCount = 0;
  const realCallService = hass.callService;
  hass.callService = (...args) => {
    callCount += 1;
    if (callCount === 1) {
      hass.calls.push({ domain: args[0], service: args[1], data: args[2] });
      return new Promise((resolve) => {
        releaseFirst = resolve;
      });
    }
    return realCallService(...args);
  };
  const protocol = new CardSendProtocol(hass, {
    schedule: (fn, ms) => clock.schedule(fn, ms),
    cancelTimer: (id) => clock.cancelTimer(id),
  });
  const handle = protocol.send("start_timer", { name: "Pasta", duration_seconds: 300 });
  await flush();
  clock.advance(ACK_TIMEOUT_MS);
  await flush();
  assert.equal(hass.calls.length, 2, "re-sent after the first timeout");
  releaseFirst({ ok: true });
  const result = await handle.promise;
  assert.equal(result.commandId, handle.commandId, "the cycle resolves on the late ack");
});

test("protocol: no hass throws a typed error at construction", () => {
  const { CardSendProtocol } = cardModule;
  assert.throws(() => new CardSendProtocol(null), (error) => error.kind === "no-hass");
  assert.throws(() => new CardSendProtocol({}), (error) => error.kind === "no-hass");
});

// ---------------------------------------------------------------------------
// Card rendering (FakeDOM)
// ---------------------------------------------------------------------------

test("card: loading state until the store resolves, then dials from the store", async () => {
  const store = makeStore([makeRecord()]);
  const card = mountCard({ store, hass: makeFakeHass() });
  try {
    const shadow = card.shadowRoot;
    assert.equal(shadow.querySelector(".loading").hidden, true, "loading hides once the store is ready");
    assert.equal(card._storeState, "ready");
    const dials = shadow.querySelector(".dials");
    assert.equal(dials.children.length, 1);
    const dial = dials.children[0];
    assert.equal(dial.querySelector(".dial-name").textContent, "Pasta");
    assert.equal(dial.querySelector(".dial-hint").textContent, "Running");
    assert.equal(dial.getAttribute("data-state"), "active");
    assert.ok(dial.querySelector(".dial-time").textContent.length > 0);
  } finally {
    unmountCard(card);
  }
});

test("card: loading state with no store, store-failed with no shell", async () => {
  const card = mountCard({ hass: makeFakeHass() });
  try {
    await flush(20);
    assert.equal(card._storeState, "failed");
    const shadow = card.shadowRoot;
    assert.equal(shadow.querySelector(".store-error").hidden, false);
    assert.match(
      shadow.querySelector(".store-error").textContent,
      /hub shell/,
    );
  } finally {
    unmountCard(card);
  }
});

test("card: empty state + entry row always present, with and without timers", () => {
  const emptyStore = makeStore([]);
  const emptyCard = mountCard({ store: emptyStore, hass: makeFakeHass() });
  try {
    const shadow = emptyCard.shadowRoot;
    assert.equal(shadow.querySelector(".empty").hidden, false);
    assert.equal(shadow.querySelectorAll(".preset-chip").length, 4);
    assert.ok(shadow.querySelector(".set-time-button"));
  } finally {
    unmountCard(emptyCard);
  }
  const fullStore = makeStore([makeRecord(), makeRecord({ entityId: "sensor.satellite_timer_01K8RICE", timerId: "01K8RICE", name: "Rice" })]);
  const fullCard = mountCard({ store: fullStore, hass: makeFakeHass() });
  try {
    const shadow = fullCard.shadowRoot;
    assert.equal(shadow.querySelector(".empty").hidden, true);
    assert.equal(shadow.querySelectorAll(".preset-chip").length, 4);
    assert.ok(shadow.querySelector(".set-time-button"));
    assert.equal(shadow.querySelector(".dials").children.length, 2);
  } finally {
    unmountCard(fullCard);
  }
});

test("card: render order follows the store (running-first is the store's job)", () => {
  const finished = makeRecord({
    entityId: "sensor.satellite_timer_01K8DONE",
    timerId: "01K8DONE",
    name: "Done Soup",
    state: "finished",
    remainingSeconds: 0,
  });
  const active = makeRecord();
  const store = makeStore([active, finished]);
  const card = mountCard({ store, hass: makeFakeHass() });
  try {
    const dials = card.shadowRoot.querySelector(".dials");
    assert.equal(dials.children[0].querySelector(".dial-name").textContent, "Pasta");
    assert.equal(dials.children[1].querySelector(".dial-name").textContent, "Done Soup");
  } finally {
    unmountCard(card);
  }
});

test("card: finished and missed-alarm readings", () => {
  const now = Date.now();
  const twoMinAgo = new Date(now - 2 * 60 * 1000).toISOString();
  const missed = makeRecord({
    entityId: "sensor.satellite_timer_01K8MISS",
    timerId: "01K8MISS",
    name: "Missed",
    state: "finished",
    remainingSeconds: 0,
    alarmMissed: true,
    alarmMissedReason: "no_announcer",
    lastChanged: twoMinAgo,
  });
  const plain = makeRecord({
    entityId: "sensor.satellite_timer_01K8PLAIN",
    timerId: "01K8PLAIN",
    name: "Plain",
    state: "finished",
    remainingSeconds: 0,
    lastChanged: twoMinAgo,
  });
  const store = makeStore([missed, plain]);
  const card = mountCard({ store, hass: makeFakeHass() });
  try {
    const dials = card.shadowRoot.querySelector(".dials");
    assert.equal(
      dials.children[0].querySelector(".dial-hint").textContent,
      "Done — the alarm didn't sound · 2 min ago",
    );
    assert.equal(
      dials.children[1].querySelector(".dial-hint").textContent,
      "Done — 2 min ago",
    );
    assert.equal(dials.children[0].querySelector(".dial-time").textContent, "Done");
  } finally {
    unmountCard(card);
  }
});

test("card: ringing dial carries the stop control; tap acknowledges", async () => {
  const hass = makeFakeHass();
  const ringing = makeRecord({
    entityId: "sensor.satellite_timer_01K8RING",
    timerId: "01K8RING",
    name: "Ringing",
    state: "ringing",
    remainingSeconds: 0,
  });
  const store = makeStore([ringing]);
  const card = mountCard({ store, hass });
  try {
    const dial = card.shadowRoot.querySelector(".dials").children[0];
    assert.equal(dial.querySelector(".dial-hint").textContent, "Ringing — tap to stop");
    assert.ok(findButton(dial, "acknowledge"));
    assert.equal(findButton(dial, "acknowledge").textContent, "Stop");
    findButton(dial, "acknowledge").click();
    await flush();
    assert.equal(hass.calls.length, 1);
    assert.equal(hass.calls[0].service, "acknowledge_timer");
    assert.equal(hass.calls[0].data.timer_id, "01K8RING");
    assert.match(hass.calls[0].data.command_id, UUID_V4);
  } finally {
    unmountCard(card);
  }
});

test("card: voice hints hidden unless the provider is healthy (empty slot)", () => {
  const store = makeStore([]);
  const noSensorHass = makeFakeHass({ states: [] });
  const cardA = mountCard({ store, hass: noSensorHass });
  try {
    assert.equal(cardA.shadowRoot.querySelector(".voice-hint").hidden, true);
    assert.equal(cardA.shadowRoot.querySelector(".voice-hint").children.length, 0);
  } finally {
    unmountCard(cardA);
  }
  const degradedHass = makeFakeHass({
    states: [{ entity_id: "sensor.family_hub_voice_health", state: "degraded", attributes: {} }],
  });
  const cardB = mountCard({ store, hass: degradedHass });
  try {
    assert.equal(cardB.shadowRoot.querySelector(".voice-hint").hidden, true);
  } finally {
    unmountCard(cardB);
  }
  const healthyHass = makeFakeHass({
    states: [{ entity_id: "sensor.family_hub_voice_health", state: "healthy", attributes: {} }],
  });
  const cardC = mountCard({ store, hass: healthyHass });
  try {
    const hint = cardC.shadowRoot.querySelector(".voice-hint");
    assert.equal(hint.hidden, false);
    assert.equal(hint.children.length, 2);
    assert.match(hint.textContent, /Okay Nabu/);
  } finally {
    unmountCard(cardC);
  }
});

test("card: preset chip starts a timer with one command_id; chip never blocks", async () => {
  const hass = makeFakeHass();
  const card = mountCard({ store: makeStore([]), hass });
  try {
    const chip = findButton(card.shadowRoot, "preset", '[data-minutes="10"]');
    assert.ok(chip);
    chip.click();
    chip.click();
    await flush();
    assert.equal(hass.calls.length, 2, "two taps are two gestures, two timers");
    assert.notEqual(hass.calls[0].data.command_id, hass.calls[1].data.command_id);
    assert.equal(hass.calls[0].data.name, "10 minute timer");
    assert.equal(hass.calls[0].data.duration_seconds, 600);
    assert.equal(hass.calls[0].service, "start_timer");
  } finally {
    unmountCard(card);
  }
});

test("card: chip start failure shows the in-place error with working Retry", async () => {
  const hass = makeFakeHass();
  hass.behavior.mode = "raise";
  hass.behavior.error = new Error("boom");
  const store = makeStore([makeRecord()]);
  const card = mountCard({ store, hass });
  try {
    const chip = findButton(card.shadowRoot, "preset", '[data-minutes="5"]');
    chip.click();
    await flush();
    await flush();
    const chipError = card.shadowRoot.querySelector(".chip-error");
    assert.equal(chipError.hidden, false, "error renders on the chip row with timers running");
    assert.match(chipError.textContent, /Couldn't start the "5 minute timer" timer/);
    const retry = findButton(chipError, "chip-retry");
    assert.ok(retry, "Retry button present");
    hass.behavior.mode = "ok";
    retry.click();
    await flush();
    assert.equal(hass.calls.length, 2, "Retry re-sends");
    assert.equal(hass.calls[1].data.command_id, hass.calls[0].data.command_id, "same command_id");
    assert.equal(card.shadowRoot.querySelector(".chip-error").hidden, true, "success clears");
  } finally {
    unmountCard(card);
  }
});

test("card: service-not-found on a chip shows the error + Repair (never try voice)", async () => {
  const hass = makeFakeHass();
  hass.behavior.mode = "raise";
  hass.behavior.error = new Error("Service satellite_timers.start_timer not found");
  const card = mountCard({ store: makeStore([]), hass });
  try {
    findButton(card.shadowRoot, "preset", '[data-minutes="15"]').click();
    await flush();
    await flush();
    const chipError = card.shadowRoot.querySelector(".chip-error");
    assert.equal(chipError.hidden, false);
    assert.match(chipError.textContent, /service wasn't found/);
    assert.ok(findButton(chipError, "repair"), "Repair action present");
    assert.doesNotMatch(card.shadowRoot.textContent, /try voice/i);
    assert.doesNotMatch(CARD_SOURCE, /try voice/i);
  } finally {
    unmountCard(card);
  }
});

test("card: stepper opens, steps, rejects zero, starts a named timer", async () => {
  const hass = makeFakeHass();
  const card = mountCard({ store: makeStore([]), hass });
  try {
    const shadow = card.shadowRoot;
    assert.equal(shadow.querySelector(".stepper").hidden, true);
    findButton(shadow, "toggle-stepper").click();
    assert.equal(shadow.querySelector(".stepper").hidden, false);
    // Default 0 h 10 m 0 s.
    assert.equal(shadow.querySelector(".stepper-total-name").textContent, "10 minute timer");
    // Step minutes +5, seconds +5.
    const minutesPlus = shadow.querySelectorAll("[data-action='step']").find(
      (b) => b.getAttribute("data-unit") === "minutes" && b.getAttribute("data-delta") === "5",
    );
    minutesPlus.click();
    assert.equal(shadow.querySelector(".stepper-total-name").textContent, "15 minute timer");
    // Hours to 24 disables minutes/seconds and zeroes them.
    const hoursPlus = shadow.querySelectorAll("[data-action='step']").find(
      (b) => b.getAttribute("data-unit") === "hours" && b.getAttribute("data-delta") === "1",
    );
    for (let i = 0; i < 24; i += 1) hoursPlus.click();
    assert.equal(shadow.querySelector(".stepper-total-time").textContent, "24:00:00");
    assert.equal(shadow.querySelector(".stepper-total-name").textContent, "24 hour timer");
    findButton(shadow, "stepper-start").click();
    await flush();
    assert.equal(hass.calls.length, 1);
    assert.equal(hass.calls[0].data.duration_seconds, 86400);
    assert.equal(hass.calls[0].data.name, "24 hour timer");
  } finally {
    unmountCard(card);
  }
});

test("card: stepper Start with zero shows an inline error and makes no call", async () => {
  const hass = makeFakeHass();
  const card = mountCard({ store: makeStore([]), hass });
  try {
    const shadow = card.shadowRoot;
    findButton(shadow, "toggle-stepper").click();
    const minutesMinus = shadow.querySelectorAll("[data-action='step']").find(
      (b) => b.getAttribute("data-unit") === "minutes" && b.getAttribute("data-delta") === "-5",
    );
    minutesMinus.click();
    minutesMinus.click();
    assert.equal(shadow.querySelector(".stepper-total-name").textContent, "0 second timer");
    findButton(shadow, "stepper-start").click();
    await flush();
    assert.equal(hass.calls.length, 0);
    assert.equal(shadow.querySelector(".stepper-error").hidden, false);
    assert.match(shadow.querySelector(".stepper-error").textContent, /Pick a time first/);
  } finally {
    unmountCard(card);
  }
});

test("card: dial tap pauses, resume, dismiss offers Undo, Undo re-sends", async () => {
  const hass = makeFakeHass();
  const active = makeRecord();
  const store = makeStore([active]);
  const card = mountCard({ store, hass });
  try {
    const dial = card.shadowRoot.querySelector(".dials").children[0];
    // Dial tap on an active timer pauses.
    findButton(dial, "dial").click();
    await flush();
    assert.equal(hass.calls[0].service, "pause_timer");
    // Paused timer: dial tap resumes.
    store.setRecords([makeRecord({ state: "paused" })]);
    card._render();
    const pausedDial = card.shadowRoot.querySelector(".dials").children[0];
    findButton(pausedDial, "dial").click();
    await flush();
    assert.equal(hass.calls[1].service, "resume_timer");
    // Finished timer: dial tap dismisses, Undo bar appears.
    store.setRecords([makeRecord({ state: "finished", remainingSeconds: 0 })]);
    card._render();
    const finishedDial = card.shadowRoot.querySelector(".dials").children[0];
    findButton(finishedDial, "dial").click();
    await flush();
    assert.equal(hass.calls[2].service, "dismiss_timer");
    const undoBar = card.shadowRoot.querySelector(".undo-bar");
    assert.ok(undoBar, "Undo bar renders where the dial was");
    assert.match(undoBar.textContent, /dismissed/);
    findButton(undoBar, "undo").click();
    await flush();
    assert.equal(hass.calls[3].service, "undo_remove");
    assert.equal(hass.calls[3].data.timer_id, "01K8PASTA");
  } finally {
    unmountCard(card);
  }
});

test("card: Cancel arms on first tap, cancels on second", async () => {
  const hass = makeFakeHass();
  const store = makeStore([makeRecord()]);
  const card = mountCard({ store, hass });
  try {
    const dial = card.shadowRoot.querySelector(".dials").children[0];
    const cancel = findButton(dial, "cancel");
    assert.equal(cancel.textContent, "Cancel");
    cancel.click();
    assert.equal(hass.calls.length, 0, "first tap only arms");
    const armedDial = card.shadowRoot.querySelector(".dials").children[0];
    assert.equal(findButton(armedDial, "cancel").textContent, "Tap again to cancel");
    findButton(armedDial, "cancel").click();
    await flush();
    assert.equal(hass.calls.length, 1);
    assert.equal(hass.calls[0].service, "cancel_timer");
  } finally {
    unmountCard(card);
  }
});

test("card: finished Restart sends restart_timer", async () => {
  const hass = makeFakeHass();
  const store = makeStore([makeRecord({ state: "finished", remainingSeconds: 0 })]);
  const card = mountCard({ store, hass });
  try {
    const dial = card.shadowRoot.querySelector(".dials").children[0];
    findButton(dial, "restart").click();
    await flush();
    assert.equal(hass.calls.length, 1);
    assert.equal(hass.calls[0].service, "restart_timer");
    assert.equal(hass.calls[0].data.timer_id, "01K8PASTA");
  } finally {
    unmountCard(card);
  }
});

test("card: error slot is newer-wins; a later success clears it", async () => {
  const hass = makeFakeHass();
  hass.behavior.mode = "raise";
  hass.behavior.error = new Error("first failure");
  const card = mountCard({ store: makeStore([]), hass });
  try {
    findButton(card.shadowRoot, "preset", '[data-minutes="5"]').click();
    await flush();
    await flush();
    assert.match(card.shadowRoot.querySelector(".chip-error").textContent, /first failure/);
    hass.behavior.error = new Error("second failure");
    findButton(card.shadowRoot, "preset", '[data-minutes="10"]').click();
    await flush();
    await flush();
    assert.match(card.shadowRoot.querySelector(".chip-error").textContent, /second failure/);
    assert.doesNotMatch(card.shadowRoot.querySelector(".chip-error").textContent, /first failure/);
    hass.behavior.mode = "ok";
    findButton(card.shadowRoot, "preset", '[data-minutes="15"]').click();
    await flush();
    assert.equal(card.shadowRoot.querySelector(".chip-error").hidden, true);
  } finally {
    unmountCard(card);
  }
});

test("card: the 60 s re-probe EXISTS — armed on a persistent error, clears silently on recovery", async () => {
  const { RE_PROBE_INTERVAL_MS } = cardModule;
  assert.equal(RE_PROBE_INTERVAL_MS, 60000);
  const hass = makeFakeHass();
  hass.behavior.mode = "raise";
  hass.behavior.error = new Error("Service satellite_timers.start_timer not found");
  const card = mountCard({ store: makeStore([]), hass });
  try {
    findButton(card.shadowRoot, "preset", '[data-minutes="5"]').click();
    await flush();
    await flush();
    assert.equal(card._errorSlot.persistent, true);
    assert.notEqual(card._reProbeTimer, null, "re-probe armed");
    // The bridge comes back: the probe clears the error silently.
    hass.behavior.mode = "ok";
    await card._runReProbe();
    assert.equal(card._errorSlot, null, "recovery clears the slot");
    assert.equal(card._reProbeTimer, null, "re-probe disarms on recovery");
    assert.equal(card.shadowRoot.querySelector(".chip-error").hidden, true);
  } finally {
    unmountCard(card);
  }
});

test("card: re-probe keeps a persistent failure rendered when the bridge stays down", async () => {
  const hass = makeFakeHass({ services: {} });
  hass.behavior.mode = "raise";
  hass.behavior.error = new Error("Service satellite_timers.start_timer not found");
  const card = mountCard({ store: makeStore([]), hass });
  try {
    findButton(card.shadowRoot, "preset", '[data-minutes="5"]').click();
    await flush();
    await flush();
    assert.equal(card._errorSlot.persistent, true);
    await card._runReProbe();
    assert.notEqual(card._errorSlot, null, "still failing: the error stays");
    assert.notEqual(card._reProbeTimer, null, "still armed");
  } finally {
    unmountCard(card);
  }
});

test("card: stale active dial dims with a waiting line (5 s bound)", () => {
  const old = new Date(Date.now() - 30 * 1000).toISOString();
  const stale = makeRecord({ lastUpdated: old });
  // Distinct entity id: the reconcile keys dials by entity id, so a
  // second record with the same id would update the same dial.
  const fresh = makeRecord({
    entityId: "sensor.satellite_timer_01K8FRESH",
    timerId: "01K8FRESH",
    name: "Fresh",
  });
  const store = makeStore([stale, fresh]);
  const card = mountCard({ store, hass: makeFakeHass() });
  try {
    const dials = card.shadowRoot.querySelector(".dials");
    assert.equal(dials.children[0].querySelector(".dial-hint").textContent, "Running — waiting for update");
    assert.equal(dials.children[1].querySelector(".dial-hint").textContent, "Running");
    assert.ok(dials.children[0].querySelector(".dial-time").classList.contains("stale"));
  } finally {
    unmountCard(card);
  }
});

test("card: 58-char name renders without clipping the name node", () => {
  const name = "A".repeat(58);
  const store = makeStore([makeRecord({ name })]);
  const card = mountCard({ store, hass: makeFakeHass() });
  try {
    const dial = card.shadowRoot.querySelector(".dials").children[0];
    assert.equal(dial.querySelector(".dial-name").textContent, name);
  } finally {
    unmountCard(card);
  }
});

test("card: soon dials read under a minute left", () => {
  const soon = makeRecord({ remainingSeconds: 45, durationSeconds: 300 });
  const store = makeStore([soon]);
  const card = mountCard({ store, hass: makeFakeHass() });
  try {
    const dial = card.shadowRoot.querySelector(".dials").children[0];
    assert.equal(dial.querySelector(".dial-hint").textContent, "Running — under a minute left");
  } finally {
    unmountCard(card);
  }
});

// ---------------------------------------------------------------------------
// Round-1 fix-up (CB-1, CB-2, NB-1, NB-2, NB-3, CNB-1, CNB-2 +
// readability notes). Each fix carries its own test here; the rig
// legs in hidden_files/redesign/w5-legs/ prove the same fixes in a
// real browser against the real bridge.
// ---------------------------------------------------------------------------

test("card: back bar renders after the pin/arrival read (CB-1)", () => {
  // Pin set: the bar shows with the recipe label. setConfig runs
  // before connectedCallback reads the pin, so the bar must be
  // decided on render, not once at shell build.
  sessionStorage.setItem("family-hub:timers:pin", JSON.stringify({ timerId: "01K8PASTA" }));
  const pinned = mountCard({ store: makeStore([makeRecord()]), hass: makeFakeHass() });
  try {
    assert.equal(pinned.shadowRoot.querySelector(".back-bar").hidden, false);
    assert.equal(pinned.shadowRoot.querySelector(".back-button").textContent, "← Back to recipe");
  } finally {
    unmountCard(pinned);
  }
  // Home arrival, no pin: the Home label.
  sessionStorage.setItem("family-hub:timers:arrival", "home");
  const arrived = mountCard({ store: makeStore([]), hass: makeFakeHass() });
  try {
    assert.equal(arrived.shadowRoot.querySelector(".back-bar").hidden, false);
    assert.equal(arrived.shadowRoot.querySelector(".back-button").textContent, "← Back to Home");
  } finally {
    unmountCard(arrived);
  }
  // Neither: the bar stays hidden.
  const plain = mountCard({ store: makeStore([]), hass: makeFakeHass() });
  try {
    assert.equal(plain.shadowRoot.querySelector(".back-bar").hidden, true);
  } finally {
    unmountCard(plain);
  }
});

test("fitFontSizeToWidth: fits the measured countdown widths (CB-2)", () => {
  const { fitFontSizeToWidth } = cardModule;
  // The rig measurements (headless shell 1248, Liberation Sans,
  // single-line widths with white-space: nowrap): "5:00" is 140 px
  // at the 72 px cap, "23:59:59" 280 px; at the 40 px floor the long
  // string is 156 px. Each fits, and stays far above unreadable —
  // the longest string lands on the 30 px floor, still above the
  // 28 px dial title.
  assert.equal(fitFontSizeToWidth(72, 140, 120), 61);
  assert.equal(fitFontSizeToWidth(72, 280, 120), 30);
  assert.equal(fitFontSizeToWidth(40, 156, 120), 30);
  // Already fits: no inline size.
  assert.equal(fitFontSizeToWidth(40, 78, 120), null);
  assert.equal(fitFontSizeToWidth(72, 120, 120), null);
  // Unusable measurement: no size.
  assert.equal(fitFontSizeToWidth(NaN, 280, 120), null);
  assert.equal(fitFontSizeToWidth(72, 0, 120), null);
  assert.equal(fitFontSizeToWidth(72, 280, 0), null);
  // The floor binds before unreadable: a far wider font still fits
  // at the legibility floor, never below it.
  assert.equal(fitFontSizeToWidth(72, 600, 120), 30);
});

test("card: the countdown is fitted inside the ring when it overflows (CB-2)", () => {
  const long = makeRecord({ remainingSeconds: 86399, durationSeconds: 86400, name: "Nearly a day" });
  const store = makeStore([long]);
  const card = mountCard({ store, hass: makeFakeHass() });
  try {
    const built = card._dialEls.get(long.entityId);
    assert.equal(built.time.textContent, "23:59:59");
    // No layout in the FakeDOM: the fit skips, and no inline size is
    // set at build.
    assert.equal(built.time.style.fontSize, "");
    // Stub the measured widths and the computed size, then re-run
    // the update: the fit shrinks the countdown to fit the ring.
    // Without a Range (the FakeDOM default) the fit reads
    // scrollWidth; with one it reads the glyphs — and the glyphs
    // are what matters, because the time is centred and scrollWidth
    // under-reads centred overflow (204 px for a 280 px string).
    const realGetComputedStyle = globalThis.getComputedStyle;
    globalThis.getComputedStyle = () => ({ fontSize: "72px" });
    try {
      built.time.scrollWidth = 280;
      built.time.clientWidth = 120;
      card._updateDial(built, long, Date.now());
      assert.equal(built.time.style.fontSize, "30px");
      // The Range path: scrollWidth under-reads (204 px) but the
      // Range reads the true 280 px, and the fit lands the same.
      const doc = globalThis.document;
      const realCreateRange = doc.createRange;
      doc.createRange = () => ({
        selectNodeContents() {},
        getBoundingClientRect: () => ({ width: 280 }),
      });
      try {
        built.time.scrollWidth = 204;
        built.time.style.fontSize = "";
        card._updateDial(built, long, Date.now());
        assert.equal(built.time.style.fontSize, "30px");
      } finally {
        if (realCreateRange === undefined) delete doc.createRange;
        else doc.createRange = realCreateRange;
      }
      // A shorter countdown grows back: the inline size clears when
      // the text fits at the clamp size again.
      built.time.scrollWidth = 118;
      card._updateDial(built, { ...long, remainingSeconds: 300 }, Date.now());
      assert.equal(built.time.style.fontSize, "");
    } finally {
      if (realGetComputedStyle === undefined) delete globalThis.getComputedStyle;
      else globalThis.getComputedStyle = realGetComputedStyle;
    }
  } finally {
    unmountCard(card);
  }
});

test("card: scroll-after-start is keyed on the timer id from the start's answer (NB-1)", async () => {
  const hass = makeFakeHass();
  hass.callService = () =>
    Promise.resolve({
      timer_id: "01K8RICE",
      state: "active",
      remaining_seconds: 600,
      name: "10 minute timer",
      duration_seconds: 600,
      finishes_at: null,
      finished_at: null,
    });
  const store = makeStore([]);
  const card = mountCard({ store, hass });
  try {
    // The chip start acks with the bridge's timer id — the pending
    // scroll remembers the id, not the bare chip name.
    findButton(card.shadowRoot, "preset", '[data-minutes="10"]').click();
    await flush();
    assert.equal(card._pendingScrollTimerId, "01K8RICE");
    // The sensor arrives (its store name carries the friendly_name
    // prefix — the old name-keyed comparison could never match it).
    const record = makeRecord({
      entityId: "sensor.satellite_timer_01k8rice",
      timerId: "01K8RICE",
      name: "kitchen_hub 10 minute timer",
      remainingSeconds: 600,
      durationSeconds: 600,
    });
    store.setRecords([record]);
    card._render();
    const built = card._dialEls.get(record.entityId);
    let scrolled = false;
    built.root.scrollIntoView = () => {
      scrolled = true;
    };
    await flush();
    assert.equal(scrolled, true, "the new dial scrolled into view");
    assert.equal(card._pendingScrollTimerId, null, "the pending scroll cleared on the match");
  } finally {
    unmountCard(card);
  }
});

test("card: a stale pending scroll never fires for a later, different timer (NB-1)", async () => {
  const store = makeStore([]);
  const card = mountCard({ store, hass: makeFakeHass() });
  try {
    // A start acked but its sensor never appeared: the pending id
    // lingers. A later, unrelated timer must not be scrolled for it —
    // a timer id is unique per timer, so the stale id can only ever
    // match its own timer.
    card._afterStartScroll("01K8STALE");
    const record = makeRecord({
      entityId: "sensor.satellite_timer_01k8rice",
      timerId: "01K8RICE",
      name: "Rice",
    });
    store.setRecords([record]);
    card._render();
    const built = card._dialEls.get(record.entityId);
    let scrolled = false;
    built.root.scrollIntoView = () => {
      scrolled = true;
    };
    await flush();
    assert.equal(scrolled, false);
    assert.equal(card._pendingScrollTimerId, "01K8STALE", "the stale id stays, harmless — it can only match its own timer");
  } finally {
    unmountCard(card);
  }
});

test("card: a pin with the canonical id scrolls to the pinned dial (NB-2)", () => {
  // The pin carries the store's timer id (canonical, uppercase);
  // the dial's entity id is the same id lowercased. The lookup must
  // go through the store record, never build an entity id from the
  // timer id — the construction only worked while the B-1 defect
  // kept the store id lowercase.
  sessionStorage.setItem("family-hub:timers:pin", JSON.stringify({ timerId: "01K8PASTA" }));
  const record = makeRecord({ entityId: "sensor.satellite_timer_01k8pasta", timerId: "01K8PASTA", name: "Pasta" });
  const card = mountCard({ store: makeStore([record]), hass: makeFakeHass() });
  try {
    assert.equal(card._pinScrolled, true);
    const dial = card.shadowRoot.querySelector(".dials").children[0];
    assert.ok(dial.classList.contains("pinned"));
  } finally {
    unmountCard(card);
  }
});

test("card: the store-failed error slot is set once, not re-minted per tick (NB-3)", async () => {
  const card = mountCard({ hass: makeFakeHass() });
  try {
    await flush(20);
    assert.equal(card._storeState, "failed");
    const firstId = card._errorSlot.id;
    const firstChild = card.shadowRoot.querySelector(".chip-error").firstChild;
    assert.ok(firstChild, "chip error rendered");
    // Three more renders — the 1 s tick's work — must not re-mint.
    card._render();
    card._render();
    card._render();
    assert.equal(card._errorSlot.id, firstId, "the slot id is stable across renders");
    assert.equal(card.shadowRoot.querySelector(".chip-error").firstChild, firstChild, "the chip-error DOM is not rebuilt");
  } finally {
    unmountCard(card);
  }
});

test("card: gate-notice Repair appends the procedure exactly once (CNB-2)", () => {
  // An old bridge degrades the gate; the Repair button must stop
  // propagation — without it the root listener appends a second
  // procedure to the chip-error element.
  const hass = makeFakeHass({
    states: [{ entity_id: "sensor.family_hub_bridge_version", state: "1.0.0", attributes: { min_card_version: "2.0.0" } }],
  });
  const card = mountCard({ store: makeStore([]), hass });
  try {
    const gateNotice = card.shadowRoot.querySelector(".gate-notice");
    assert.equal(gateNotice.hidden, false, "the gate notice shows for an old bridge");
    const repair = findButton(gateNotice, "repair");
    assert.ok(repair, "Repair action present");
    repair.click();
    assert.equal(gateNotice.querySelectorAll(".repair-procedure").length, 1, "one procedure, in the gate notice");
    assert.equal(card.shadowRoot.querySelector(".chip-error").querySelectorAll(".repair-procedure").length, 0, "none in the chip error");
  } finally {
    unmountCard(card);
  }
});

test("card: an old bridge names the update on chip and dial errors (one comparison)", async () => {
  const hass = makeFakeHass({
    states: [{ entity_id: "sensor.family_hub_bridge_version", state: "1.0.0", attributes: { min_card_version: "2.0.0" } }],
  });
  hass.behavior.mode = "raise";
  hass.behavior.error = new Error("Service satellite_timers.start_timer not found");
  const card = mountCard({ store: makeStore([makeRecord()]), hass });
  try {
    // Chip path.
    findButton(card.shadowRoot, "preset", '[data-minutes="5"]').click();
    await flush();
    await flush();
    assert.match(card.shadowRoot.querySelector(".chip-error").textContent, /Update Satellite Timers in HACS/);
    // Dial path: pause the active dial against the same old bridge.
    hass.behavior.error = new Error("Service satellite_timers.pause_timer not found");
    const dial = card.shadowRoot.querySelector(".dials").children[0];
    findButton(dial, "dial").click();
    await flush();
    await flush();
    const dialError = dial.querySelector(".dial-error");
    assert.equal(dialError.hidden, false);
    assert.match(dialError.textContent, /Update Satellite Timers in HACS/);
  } finally {
    unmountCard(card);
  }
});

test("card: ring geometry is driven by DIAL_RING_PX, not literals", () => {
  const { CARD_CSS, DIAL_RING_PX, DIAL_RING_ARC_RADIUS_PX, DIAL_ENTER_ANIMATION_MS } = cardModule;
  assert.equal(DIAL_RING_PX, 120);
  assert.equal(DIAL_RING_ARC_RADIUS_PX, 52);
  assert.equal(DIAL_ENTER_ANIMATION_MS, 240);
  assert.ok(CARD_CSS.includes("width: 120px; height: 120px;"), "ring CSS comes from the constant");
  assert.ok(CARD_CSS.includes("animation: dial-enter 240ms ease-out;"), "enter animation comes from its constant");
  // The rendered ring SVG carries the same geometry.
  const card = mountCard({ store: makeStore([makeRecord()]), hass: makeFakeHass() });
  try {
    const record = makeRecord();
    const built = card._dialEls.get(record.entityId);
    assert.equal(built.ringSvg.getAttribute("viewBox"), "0 0 120 120");
    assert.equal(built.ringArc.getAttribute("r"), "52");
    assert.equal(built.ringArc.getAttribute("cx"), "60");
  } finally {
    unmountCard(card);
  }
});

test("card: getCardSize returns the named Lovelace row count", () => {
  const { CARD_SIZE_ROWS } = cardModule;
  assert.equal(CARD_SIZE_ROWS, 4);
  const card = mountCard({ store: makeStore([]), hass: makeFakeHass() });
  try {
    assert.equal(card.getCardSize(), CARD_SIZE_ROWS);
  } finally {
    unmountCard(card);
  }
});

// ---------------------------------------------------------------------------
// Source greps (the prohibitions, enforced in node)
// ---------------------------------------------------------------------------

test("source: no retired or forbidden text anywhere in the card", () => {
  // Case-insensitive: a comment quoting a retired line is still the
  // line in the file, and a plain grep cannot tell it from a render.
  for (const needle of [
    /voice isn't available/i,
    /voice is down/i,
    /try voice/i,
    /quiet_hours/i,
    /sound_volume/i,
    /TODO/,
    /FIXME/,
    /XXX/,
    /HACK/,
    /1920/,
    /1200/,
    /first-tap/,
    /VoiceClock/,
  ]) {
    assert.doesNotMatch(CARD_SOURCE, needle, `card source matches ${needle}`);
  }
  // No wall-layout code and no wall size: the D1.4 gate is W5-6's.
  assert.doesNotMatch(CARD_SOURCE, /wall-layout|wallLayout/i);
});

test("source: no M5-prohibited mechanism (guard machinery)", () => {
  for (const needle of [
    "_service_start_in_flight",
    "_inFlight",
    "inFlight",
    "debounce",
    "throttle",
    "lastSent",
    "_lastSent",
    "duplicate-start",
    "duplicateStart",
  ]) {
    assert.ok(!CARD_SOURCE.includes(needle), `card source contains "${needle}"`);
  }
  // The ack wait is M4's timeout, not a retry ladder: no line may
  // contain both setTimeout and retry.
  for (const line of CARD_SOURCE.split("\n")) {
    assert.ok(
      !(line.includes("setTimeout") && /retry/i.test(line)),
      `line couples setTimeout and retry: ${line.trim()}`,
    );
  }
});

test("source: no write-only pending maps (CNB-1)", () => {
  // The two pending maps were never read; they existed only as
  // write-only state inviting a future send registry. They are gone
  // — maps, setters, and deletes alike.
  assert.ok(!CARD_SOURCE.includes("_pendingByChip"), "dead _pendingByChip map");
  assert.ok(!CARD_SOURCE.includes("_pendingByDial"), "dead _pendingByDial map");
});

test("source: the 60 s re-probe exists in code, not just in a comment", () => {
  assert.ok(CARD_SOURCE.includes("RE_PROBE_INTERVAL_MS = 60 * 1000"));
  assert.ok(CARD_SOURCE.includes("_runReProbe"));
  assert.ok(CARD_SOURCE.includes("_probeBridgeAvailability"));
  assert.ok(CARD_SOURCE.includes("setInterval"));
  // The re-probe never re-issues a user command: its only hass call
  // is get_services.
  const probeSection = CARD_SOURCE.slice(
    CARD_SOURCE.indexOf("_probeBridgeAvailability"),
  );
  assert.match(probeSection, /get_services/);
  assert.ok(!/callService/.test(probeSection.slice(0, 1200)));
});

test("source: countdown sizing follows the U19 meanwhile rule", () => {
  assert.ok(CARD_SOURCE.includes('clamp(40px, 11vw, 72px)'));
  assert.ok(CARD_SOURCE.includes("COUNTDOWN_OUTSIDE_DIAL_PX = 72"));
  assert.ok(CARD_SOURCE.includes("font-size: 72px"));
});
