// Timer screen card (custom:timer-screen, bundled in the Satellite
// Timers bridge). From scratch, Phase 2 Wave W5, against the settled
// architecture — the ~3,700-line tangled card the Nov 13 rulings were
// written about is deleted, not edited, and must not re-form by
// accretion.
//
// One file, named sections, one owner each (the structure the G-10
// code read enforces):
//   rendering from the store ........... W5-1 (TimerScreenCard render)
//   touch entry + chips ................ W5-2 (entry row, stepper)
//   send protocol ...................... W5-3 (CardSendProtocol, M4)
//   dial actions + undo ................ W5-4 (dial handlers, error slot)
//   ringing + finished states .......... W5-5 (dial render per state)
//   wall layout ........................ W5-6, GATED — NOT BUILT HERE
// No section holds state owned by another section; no cross-section
// duplication. The card holds NO timer state of its own: what it
// renders comes from sensors ONLY (§09 C2), through TimerSensorStore.
//
// Store access (§08 ADDED-3, §0 O-W5-01 of the W5 log): the card reads
// through TimerSensorStore — never subscribes to hass itself. The store
// is resolved in order: (1) an injected store (test seam), (2) the
// shell registry on `window.__familyHub.shell`, (3) a dynamic import of
// the shell module (`/local/family-hub-shell.js`, v2 §02.8) found through
// the Lovelace resource list, then `ensureShell({hass})`. The store's
// `onSnapshot` is single-assignment and owned by the shell, so the card
// re-reads `store.snapshot()` on its 1 s display tick and on each
// `set hass` push (a wake-up nudge only — no timer state is read from
// hass). The card's only hass uses are `callService` (writes),
// `callWS lovelace/resources/list` (shell lookup), `callWS
// get_services` (the 60 s re-probe), and reading two non-timer sensors
// from the pushed object (voice health, bridge version).
//
// Sound stays deleted (§03.17): no sound code, no volume /
// quiet-hours UI anywhere in this file. Voice health is never
// announced as a failure: voice recovers itself, and no
// voice-unavailable line is ever rendered.

const CARD_VERSION = "2.0.0";
// The oldest bridge this card speaks: contract 2, first shipped in
// bridge 2.0.0 (J-W5-04 — the card's version line is the bridge
// release line; core is 0.1.0 and the two lines are not unified).
const REQUIRED_BRIDGE_MIN_VERSION = "2.0.0";
const BRIDGE_DOMAIN = "satellite_timers";
const BRIDGE_VERSION_ENTITY_ID = "sensor.family_hub_bridge_version";
const VOICE_HEALTH_ENTITY_ID = "sensor.family_hub_voice_health";
const SHELL_RESOURCE_PATH = "/local/family-hub-shell.js";

// M4 (§03.2): how long a send waits for its acknowledgement before
// the transient indicator appears and the SAME command_id is re-sent.
const ACK_TIMEOUT_MS = 3000;
// ERS re-probe (§07.29 AD-1, §08 row 41): how often the card silently
// re-checks a persistent failure. One interval, armed only while a
// persistent-class error is held, disarmed when it clears.
const RE_PROBE_INTERVAL_MS = 60 * 1000;
// Display tick (§03.13): the sensor tick cadence, 1 s.
const DISPLAY_TICK_MS = 1000;
// Undo window (§03.7): the bridge keeps a removed timer restorable
// for 5 s. The Undo bar lives exactly that long.
const UNDO_WINDOW_MS = 5 * 1000;
// Cancel arm (J-W5-07): a tap-to-arm Cancel disarms after 5 s — the
// bridge's undo window, not a measured value. An armed cancel that
// expires simply disarms; a mistaken cancel is still recoverable
// through Undo.
const CANCEL_ARM_MS = 5 * 1000;
// Stale bound (§03.12): a sensor quiet longer than this is a freeze,
// not tick jitter (the bridge ticks at 1 s).
const STALE_AFTER_MS = 5 * 1000;
// Soon (I22 meanwhile rule; the exact 60/61 sample is gated): soon
// when remaining is under 60 s.
const SOON_THRESHOLD_SECONDS = 60;
// afterAnimation bound (§03.18 — core's value): a safety bound only,
// not a timing assumption. Hub animations are sub-second; the scroll
// proceeds rather than hanging if a real animation ever runs longer.
const AFTER_ANIMATION_TIMEOUT_MS = 2000;
// Ring diameter (J-W5-09): the D1.4 wall floor is 120 px; one value
// on every device satisfies the floor by construction, with no
// per-device branch and no wall-specific layout code. The constants
// below DRIVE the ring: the CSS sizes and the SVG geometry are built
// from them, so the ring cannot drift from its floor.
const DIAL_RING_PX = 120;
// The progress arc's centreline radius: the ring edge sits at half
// the diameter (60 px) and the arc stroke is 10 px, so a 52 px radius
// leaves the arc's outer edge 3 px inside the ring edge.
const DIAL_RING_ARC_RADIUS_PX = 52;
// Countdown sizing (U19 meanwhile rule, §11 line 2767): TIMER-6 inside
// dials, 72 px outside dials. The 72–96 px range is the ruling for
// Home rows, NOT a floor for time inside phone dials.
const COUNTDOWN_IN_DIAL_CSS = "clamp(40px, 11vw, 72px)";
const COUNTDOWN_OUTSIDE_DIAL_PX = 72;
// Countdown fit (CB-2): the TIMER-6 clamp alone lets a long countdown
// paint past the ring. The longest legal countdown, "23:59:59" (a
// 24 h timer), is 280 px wide at the 72 px cap on one line (rig,
// headless shell 1248, Liberation Sans — measured with a Range over
// the text; scrollWidth reads only 204 px for it, because the time
// is centred and scrollWidth counts only the right-hand overflow —
// the first fit, built on 204 px, landed at 42 px and still
// overflowed by 24 px). It needs 72 × 120 / 280 ≈ 31 px to fit the
// ring. The floor is 30 px: the largest size that fits it while
// staying above the 28 px dial title. Shorter countdowns never come
// near it ("5:00" is 140 px at 72 px, fitting at 61 px). The floor
// sits below the 40 px TIMER-6 clamp only for strings that need it —
// a measured necessity, not a silent rule change (see U19).
const DIAL_TIME_FIT_MIN_PX = 30;
// Dial-enter animation length (§03.18: hub animations are sub-second;
// 240 ms is the chosen entrance feel inside that bound, not a spec
// value).
const DIAL_ENTER_ANIMATION_MS = 240;
// Lovelace card size: `getCardSize` returns the card's height in
// dashboard grid rows (one row is roughly 50 px); the Timers card
// reports 4 rows. The value sizes the card in the auto layout only —
// the card's own content lays out independently of it.
const CARD_SIZE_ROWS = 4;

// Preset chips (Q5 meanwhile default, U18(a)): stepper + four chips,
// always present.
const PRESET_MINUTES = [5, 10, 15, 30];
// Stepper steps (old-card convention, kept): hours ±1, minutes and
// seconds ±1/±5.
const STEPPER_STEPS = Object.freeze({ hours: [1], minutes: [1, 5], seconds: [1, 5] });
// Stepper bounds (J-W5-05): the bridge caps duration at 86,400 s
// (24:00:00), so the stepper covers exactly 1 s–24:00:00 — hours 0–24,
// minutes/seconds 0–59, disabled at 24 h.
const DURATION_MAX_HOURS = 24;
const DURATION_FIELD_MAX = Object.freeze({ hours: 24, minutes: 59, seconds: 59 });

// The PIP pin (D7.3 / T-IMPL-20) and the Home arrival flag (T-IMPL-23)
// — sessionStorage keys, read once per mount and cleared after
// reading, so a later mount does not re-pin a stale timer.
const TIMERS_PIN_KEY = "family-hub:timers:pin";
const TIMERS_ARRIVAL_KEY = "family-hub:timers:arrival";
// Scroll persistence (DESIGN-7): both positions are saved because the
// view can overflow two ways — the document scrolls, or the card's own
// scroll container. sessionStorage survives the screensaver round-trip
// and view switches within the tab; a fresh tab starts at the top.
const TIMER_SCROLL_KEY = "family-hub:timers:scroll";

// -- Pure helpers ----------------------------------------------------------

// UUID v4 (M4 step 1: one fresh command_id per gesture).
// `crypto.randomUUID` in secure contexts (HA is one); the fallback
// builds the same shape from `crypto.getRandomValues`, and the last
// resort from `Math.random` (unseeded input is still unique enough for
// a dedupe key that only needs to differ per gesture — but the
// fallback is never taken in a browser or in node, both of which have
// `crypto.randomUUID`).
function newCommandId() {
  const cryptoObject =
    (typeof globalThis !== "undefined" && globalThis.crypto) || null;
  if (cryptoObject && typeof cryptoObject.randomUUID === "function") {
    return cryptoObject.randomUUID();
  }
  const bytes = new Uint8Array(16);
  if (cryptoObject && typeof cryptoObject.getRandomValues === "function") {
    cryptoObject.getRandomValues(bytes);
  } else {
    for (let i = 0; i < 16; i += 1) {
      bytes[i] = Math.floor(Math.random() * 256);
    }
  }
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = [...bytes].map((b) => b.toString(16).padStart(2, "0")).join("");
  return (
    `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-` +
    `${hex.slice(16, 20)}-${hex.slice(20)}`
  );
}

// Countdown text for a remaining-seconds value. The canonical copy is
// `formatRemaining` in pi-family-hub `core/format.js` (J-W5-03);
// this copy is pinned identical to it by the node tests on a shared
// case table, and switches to importing it when the shell module
// ships. Clamped at zero, floored — a display value, never a
// countdown source (the server-side `remaining` is the source;
// `finishes_at` is a cross-check only).
function formatRemaining(totalSeconds) {
  const total = Math.max(0, Math.floor(Number(totalSeconds)) || 0);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  const two = (n) => String(n).padStart(2, "0");
  if (hours > 0) return `${hours}:${two(minutes)}:${two(seconds)}`;
  return `${minutes}:${two(seconds)}`;
}

// The font size that fits `contentPx` of text inside a `boxPx` box,
// starting from the CSS size `cssPx` (CB-2). Null when the text
// already fits (no inline size needed) or the measurement is unusable.
// The shrink is linear in font size, rounded down so the result fits;
// the floor is the legibility bound (DIAL_TIME_FIT_MIN_PX), not a
// second clamp. Pure, so the node tests pin it on the measured
// widths and the rig legs measure the real ones.
function fitFontSizeToWidth(cssPx, contentPx, boxPx) {
  if (!(cssPx > 0) || !(contentPx > 0) || !(boxPx > 0)) return null;
  if (contentPx <= boxPx) return null;
  return Math.max(DIAL_TIME_FIT_MIN_PX, Math.floor((cssPx * boxPx) / contentPx));
}

// "N min ago" for a finished timer, from the moment the sensor entered
// `finished` (J-W5-11: `last_changed` — the store record carries no
// `finished_at`). Under a minute reads "just now"; an unparseable or
// missing timestamp reads null (the caller omits "· N min ago" rather
// than inventing a time).
function formatMinutesAgo(finishedAtMs, nowMs = Date.now()) {  if (!Number.isFinite(finishedAtMs)) return null;
  const elapsedMs = nowMs - finishedAtMs;
  if (elapsedMs < 0) return null;
  if (elapsedMs < 60 * 1000) return "just now";
  const minutes = Math.floor(elapsedMs / (60 * 1000));
  return `${minutes} min ago`;
}

// Clamp one stepper field to its range: non-numbers and negatives
// become 0, values above the field max become the max. Pure.
function clampDurationPart(unit, value) {
  const max = DURATION_FIELD_MAX[unit];
  if (max === undefined) return 0;
  const n = Math.floor(Number(value));
  if (!Number.isFinite(n) || n <= 0) return 0;
  return Math.min(n, max);
}

// Normalise a raw { hours, minutes, seconds } (strings or numbers,
// missing fields allowed) into clamped integers. Pure.
function parseDurationParts(raw) {
  const src = raw && typeof raw === "object" ? raw : {};
  return {
    hours: clampDurationPart("hours", src.hours),
    minutes: clampDurationPart("minutes", src.minutes),
    seconds: clampDurationPart("seconds", src.seconds),
  };
}

// Total seconds for a parsed parts object. Pure.
function durationTotalSeconds(parts) {
  const p = parseDurationParts(parts);
  return p.hours * 3600 + p.minutes * 60 + p.seconds;
}

// Human name for a duration in seconds, as the timer's display name:
// "45 second timer", "7 minute timer", "1 minute 30 second timer",
// "1 hour 15 minute timer". Units stay SINGULAR at every value — the
// card's existing naming convention ("10 minute timer" from the preset
// chips), so a picker timer and a chip timer read the same way. Zero
// or negative reads "0 second timer" (the picker rejects it before any
// start — the name is never used for a zero start). Pure.
function formatDurationName(totalSeconds) {
  const total = Math.max(0, Math.floor(Number(totalSeconds)) || 0);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  const bits = [];
  if (hours > 0) bits.push(`${hours} hour`);
  if (minutes > 0) bits.push(`${minutes} minute`);
  if (seconds > 0 || bits.length === 0) bits.push(`${seconds} second`);
  return `${bits.join(" ")} timer`;
}

// Is this error a service-not-found — the bridge (or the service)
// is missing? Home Assistant's message reads
// "Service satellite_timers.<name> not found". The same regex the old
// card used, kept because the message shape is HA's, not ours.
function isServiceNotFound(error) {
  const message = error instanceof Error ? error.message : String(error ?? "");
  return /service.*not found/i.test(message);
}

// Bridge gate (mirrors `core/degrade.js evaluateBridgeGate`, J-W5-04,
// with the card's own versions). The bridge version sensor's state is
// the bridge release tag; its `min_card_version` attribute is the
// oldest card the bridge supports. Returns
// `{ gate: "FULL" | "DEGRADED", reason, detail }`.
function parseVersion(tag) {
  if (typeof tag !== "string") return null;
  const match = /^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?$/.exec(tag.trim());
  if (!match) return null;
  return [Number(match[1]), Number(match[2] ?? 0), Number(match[3] ?? 0)];
}

function compareVersions(a, b) {
  const parsedA = parseVersion(a);
  const parsedB = parseVersion(b);
  if (!parsedA || !parsedB) return null;
  for (let i = 0; i < 3; i += 1) {
    if (parsedA[i] !== parsedB[i]) return parsedA[i] < parsedB[i] ? -1 : 1;
  }
  return 0;
}

function evaluateBridgeGate(bridgeVersion, bridgeMinCardVersion) {
  if (parseVersion(bridgeVersion) === null) {
    return {
      gate: "DEGRADED",
      reason: "unreadable-bridge-version",
      detail: "The bridge version could not be read.",
    };
  }
  const cardVersionComparison =
    bridgeMinCardVersion != null
      ? compareVersions(CARD_VERSION, bridgeMinCardVersion)
      : null;
  if (cardVersionComparison !== null && cardVersionComparison < 0) {
    return {
      gate: "DEGRADED",
      reason: "card-too-old",
      detail:
        `This card (${CARD_VERSION}) is older than the bridge requires ` +
        `(minimum card ${bridgeMinCardVersion}).`,
    };
  }
  const bridgeComparison = compareVersions(
    bridgeVersion,
    REQUIRED_BRIDGE_MIN_VERSION,
  );
  if (bridgeComparison !== null && bridgeComparison < 0) {
    return {
      gate: "DEGRADED",
      reason: "bridge-too-old",
      detail:
        `The bridge (${bridgeVersion}) is older than this card requires ` +
        `(minimum bridge ${REQUIRED_BRIDGE_MIN_VERSION}).`,
    };
  }
  return { gate: "FULL", reason: "ok", detail: "" };
}

// Plain-words gate notice (DEGRADED form, §07.24): render everything
// still understood + a plain-language notice naming both versions +
// one Repair action; browsing never blocked. Returns null when FULL.
function gateNoticeCopy(gateResult) {
  if (!gateResult || gateResult.gate === "FULL") return null;
  const detail =
    typeof gateResult.detail === "string" && gateResult.detail.trim()
      ? gateResult.detail.trim()
      : "No reason for the mismatch was recorded.";
  return {
    title: "This hub needs an update to work fully",
    body:
      `The hub's parts disagree about their versions (card ` +
      `${CARD_VERSION}, bridge ${gateResult.bridgeVersion ?? "unknown"}). ` +
      `${detail} Everything below still works as far as it can be read — ` +
      `nothing is hidden — but some features may be out of date until ` +
      `the update runs.`,
    repairLabel: "Repair — re-run the setup wizard",
  };
}

// The missed-alarm reading (§03.8, W5-5): a finished timer whose alarm
// never sounded reads "Done — the alarm didn't sound · N min ago".
// The reason is plain words, not the machine reason code — the code
// (`no_announcer` / `announcer_no_run` / …) goes to the diagnostics
// log, never to the dial. `agoText` is null when the finish moment is
// unknown (J-W5-11) — the "· N min ago" is omitted, not invented.
function missedAlarmReading(agoText) {
  return agoText
    ? `Done — the alarm didn't sound · ${agoText}`
    : "Done — the alarm didn't sound";
}

// The plain finished reading: "Done — N min ago".
function finishedReading(agoText) {
  return agoText ? `Done — ${agoText}` : "Done";
}

// Read the PIP pin once per mount and clear it, so a later mount does
// not re-pin a stale timer. Returns the timer id, or null.
function readTimersPin(storage) {
  if (!storage || typeof storage.getItem !== "function") return null;
  let raw = null;
  try {
    raw = storage.getItem(TIMERS_PIN_KEY);
  } catch (_err) {
    return null;
  }
  if (!raw) return null;
  try {
    storage.removeItem(TIMERS_PIN_KEY);
  } catch (_err) {
    // Clear failure: the pin is still consumed below (returned once —
    // the caller keeps it in memory, never re-reads).
  }
  try {
    const parsed = JSON.parse(raw);
    if (
      parsed &&
      typeof parsed === "object" &&
      typeof parsed.timerId === "string" &&
      parsed.timerId
    ) {
      return parsed.timerId;
    }
    if (typeof parsed === "string" && parsed) return parsed;
  } catch (_err) {
    if (typeof raw === "string" && raw) return raw;
  }
  return null;
}

// Read the Home arrival flag once per mount and clear it. Returns true
// when the arrival was Home.
function readTimersArrival(storage) {
  if (!storage || typeof storage.getItem !== "function") return false;
  let raw = null;
  try {
    raw = storage.getItem(TIMERS_ARRIVAL_KEY);
  } catch (_err) {
    return false;
  }
  if (!raw) return false;
  try {
    storage.removeItem(TIMERS_ARRIVAL_KEY);
  } catch (_err) {
    // Clear failure: the flag is still consumed once by the caller.
  }
  return raw === "home" || raw === '"home"';
}

function sanitizeScrollTop(value) {
  const n = Number(value);
  return Number.isFinite(n) && n > 0 ? Math.floor(n) : 0;
}

// Parse a stored scroll state; garbage and wrong shapes become null
// (nothing saved), a valid object becomes a clean zeroed state.
function parseTimerScroll(raw) {
  if (typeof raw !== "string" || !raw) return null;
  let parsed;
  try {
    parsed = JSON.parse(raw);
  } catch (_err) {
    return null;
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    return null;
  }
  return {
    page: sanitizeScrollTop(parsed.page),
    screen: sanitizeScrollTop(parsed.screen),
  };
}

function serializeTimerScroll(state) {
  return JSON.stringify({
    page: sanitizeScrollTop(state && state.page),
    screen: sanitizeScrollTop(state && state.screen),
  });
}

// afterAnimation (§03.18) — scroll waits for the animation to actually
// end, never a fixed delay. The canonical copy is `core/timers.js`
// `afterAnimation` (J-W5-03); this copy is pinned identical to it by
// the node tests on fake elements, and switches to importing it when
// the shell module ships.
//
// The wait rides the animations' `finished` promises, NOT the
// `animationend` event: `animationend` fires for CSS `@keyframes`
// animations but NEVER fires for WAAPI (`element.animate()`)
// animations in Chromium, while both kinds expose `finished`. A
// cancelled animation ends the wait promptly (its `finished`
// rejects; `allSettled` treats it as settled) — a cancel is a
// completion, not a failure. `timeoutMs` is a safety bound only, and
// its expiry resolves (not rejects): the caller asked to wait for an
// animation, not to fail the scroll.
function afterAnimation(element, { timeoutMs = AFTER_ANIMATION_TIMEOUT_MS } = {}) {
  if (!element || typeof element.getAnimations !== "function") {
    return Promise.resolve();
  }
  const animations = element.getAnimations() ?? [];
  const stillRunning = animations.filter((animation) => {
    const state = animation?.playState;
    return state === undefined || state === "running" || state === "pending";
  });
  if (stillRunning.length === 0) return Promise.resolve();
  const finishedPromises = stillRunning
    .map((animation) => animation?.finished)
    .filter((finished) => finished && typeof finished.then === "function");
  const hasFinishedForAll = finishedPromises.length === stillRunning.length;
  return new Promise((resolve) => {
    let done = false;
    const finish = () => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      resolve();
    };
    const timer = setTimeout(finish, timeoutMs);
    if (hasFinishedForAll) {
      void Promise.allSettled(finishedPromises).then(finish);
    }
    // When not every running animation has a `finished` promise, the
    // collected partial set is deliberately NOT awaited: waiting on it
    // would end the wait when a known animation settles while the
    // animation without a signal still runs — the opposite of the
    // all-animations guarantee. The bound timer above is the wait.
  });
}

// -- Send protocol (W5-3, §03.2 M4–M5) --------------------------------------
//
// `CardSendProtocol` is exactly M4, and nothing else. The ERS-1 ladder
// (§07.9) names this as its carve-out (a): interactive command
// acknowledgements — re-send once after 3 s, then user Retry. There is
// no other ladder in this file.
//
//   (1) On a user gesture, generate one fresh `command_id` (UUID v4).
//   (2) Send the command ONCE.
//   (3) Wait ACK_TIMEOUT_MS (3000) for the acknowledgement — the
//       service call resolving.
//   (4) On timeout, the caller shows a transient "Starting…" /
//       "Working…" indicator IN PLACE (never an optimistic dial — the
//       card never renders a timer with no sensor) and this protocol
//       re-sends the SAME `command_id` once.
//   (5) On a second timeout, the caller shows the honest in-place
//       error with a Retry button; Retry re-sends the SAME
//       `command_id` again (a fresh M4 cycle with the same id).
//   (6) NEVER generate a new `command_id` for the same gesture; NEVER
//       suppress a send because a previous send is in flight — two
//       distinct gestures are two distinct `command_id`s and therefore
//       two timers, even if the first has not acknowledged yet.
//
// PROHIBITION LIST (M5): no in-flight flag that blocks a send; no
// gesture-dropping delay mechanism; no local timer registry or
// cache used to decide whether to send; no "last sent" comparison; no
// check that suppresses a second start of any kind. The ONLY card-side
// command is the `command_id` of the gesture currently awaiting
// acknowledgement — and it lives here, in the per-gesture handle
// returned by `send()`, held solely so a retry can reuse it. Each
// `send()` call owns its own closure: there is no shared send state
// anywhere in this file, so one gesture's wait can never block or
// absorb another's.
//
// An immediate rejection is NOT a timeout: the call answered, so
// there is nothing to re-send automatically — the error is shown at
// once with Retry (re-send same id). Re-sending a call the bridge
// already refused (old bridge, bad duration) would only fail again.
//
// A late resolution after a timeout is an acknowledgement of the same
// command: harmless once the cycle has settled, and never a second
// effect — the bridge dedupes on `command_id` (M1).

// Error kinds for a send. `kind` is the machine classification (for
// logs and the retry/re-probe boundary); `message` is the plain-words
// text the user reads (ERS-4 form).
class SendError extends Error {
  constructor(kind, message) {
    super(message);
    this.name = "SendError";
    this.kind = kind;
  }
}

function sendErrorFromRejection(error) {
  const message = error instanceof Error ? error.message : String(error ?? "");
  const clean = message.trim() || "Home Assistant didn't answer.";
  if (isServiceNotFound(error)) {
    return new SendError("service-not-found", clean);
  }
  return new SendError("call-failed", clean);
}

function sendTimeoutError() {
  return new SendError(
    "timeout",
    "The timer didn't answer. Tap Retry to send it again.",
  );
}

// One gesture's send. `hooks.onWorking` runs on the FIRST timeout only
// (the transient indicator appears, then the same id is re-sent once);
// `hooks.onSettled` runs once, on ack or on final failure, so the
// caller can clear the transient indicator. The promise resolves to
// `{ commandId, response }` on ack — `response` is the service call's
// answer (a start answers with the timer's state, including its timer
// id) — and rejects with a SendError otherwise.
// `retry()` re-runs the whole cycle with the SAME command_id — it is
// the user Retry button's handler, never a new gesture.
class CardSendProtocol {
  constructor(hass, { schedule = null, cancelTimer = null } = {}) {
    if (!hass || typeof hass.callService !== "function") {
      throw new SendError(
        "no-hass",
        "Home Assistant isn't connected, so the timer can't be sent.",
      );
    }
    this.hass = hass;
    // Timer functions are injectable for tests; the defaults call the
    // host functions bare (W4 D-20: never store a bare host function
    // as a default and invoke it as a method). The arrow wrappers
    // below are the defaults.
    this._schedule =
      schedule ?? ((fn, ms) => setTimeout(fn, ms));
    this._cancelTimer =
      cancelTimer ?? ((id) => clearTimeout(id));
  }

  send(service, data, { label = service, onWorking = null, onSettled = null } = {}) {
    const options = { label, onWorking, onSettled };
    const handle = this._makeHandle(newCommandId(), service, data, options);
    handle.promise = this._runCycle(handle, options);
    // A caller that never awaits the promise must not produce an
    // unhandled rejection: the handle carries the outcome, and the
    // caller attaches its own handlers. Attach a no-op catch here so
    // a fire-and-forget send never surfaces as an unhandled rejection
    // in the console — the caller still observes the same rejection
    // through its own `.catch` on the same promise.
    handle.promise.catch(() => {});
    return handle;
  }

  // A handle for one gesture: its `command_id`, its payload, and the
  // Retry that re-runs the cycle with the SAME id (and the same
  // payload — D-W5-01). `retry()` returns a handle of the same shape
  // (D-W5-02: it used to return the bare cycle promise, so the
  // card's Retry handlers — which read `retryHandle.promise` — threw
  // `TypeError: Cannot read properties of undefined`), so a Retry of
  // a Retry keeps working and keeps the same id.
  _makeHandle(commandId, service, data, { label, onWorking, onSettled }) {
    const handle = {
      commandId,
      service,
      // The payload rides on the handle: `_callOnce` spreads it with
      // `command_id` on every attempt. Without this the call would
      // carry only the id (D-W5-01, caught by the node suite:
      // `data.name` / `data.timer_id` read undefined).
      data,
      label,
      promise: null,
      retry: null,
    };
    handle.retry = () => {
      const retryHandle = this._makeHandle(commandId, service, data, {
        label,
        onWorking,
        onSettled,
      });
      retryHandle.promise = this._runCycle(retryHandle, { onWorking, onSettled });
      retryHandle.promise.catch(() => {});
      return retryHandle;
    };
    return handle;
  }

  async _runCycle(handle, { onWorking, onSettled }) {
    let settledNotified = false;
    const notifySettled = () => {
      if (!settledNotified) {
        settledNotified = true;
        if (typeof onSettled === "function") onSettled();
      }
    };
    try {
      for (let attempt = 1; attempt <= 2; attempt += 1) {
        const outcome = await this._callOnce(handle);
        if (outcome.acked) {
          notifySettled();
          // The ack carries the service call's answer: a start
          // answers with the timer's state, including its timer id
          // (the scroll-after-start key, NB-1). Other calls' answers
          // ride along unread.
          return { commandId: handle.commandId, response: outcome.response };
        }
        if (outcome.error) {
          // The call answered with a failure: no automatic re-send.
          throw outcome.error;
        }
        // Timeout (no answer within ACK_TIMEOUT_MS).
        if (attempt === 1) {
          if (typeof onWorking === "function") onWorking();
          // Fall through: re-send the SAME command_id once.
        } else {
          throw sendTimeoutError();
        }
      }
      // Unreachable: the loop always returns or throws.
      throw sendTimeoutError();
    } finally {
      notifySettled();
    }
  }

  // One attempt: race the service call against the ack timeout.
  // Returns `{ acked: true, response }` (the service call's answer —
  // a start answers with the timer's state, including its timer id),
  // `{ error }` (immediate rejection), or `{ timedOut: true }`. The
  // losing side is always settled (a catch is attached to the call
  // promise, the timer is cleared) so no dangling rejection or timer
  // survives the attempt.
  async _callOnce(handle) {
    let timeoutId = null;
    const callPromise = (async () => {
      try {
        const response = await this.hass.callService(BRIDGE_DOMAIN, handle.service, {
          ...handle.data,
          command_id: handle.commandId,
        });
        return { acked: true, response };
      } catch (error) {
        return { error: sendErrorFromRejection(error) };
      }
    })();
    const timeoutPromise = new Promise((resolve) => {
      timeoutId = this._schedule(() => resolve({ timedOut: true }), ACK_TIMEOUT_MS);
    });
    const outcome = await Promise.race([callPromise, timeoutPromise]);
    if (timeoutId !== null) this._cancelTimer(timeoutId);
    // A timeout while the call is still in flight needs no handling:
    // the call wrapper already catches its own rejection (it returns
    // an outcome object, never rejects), so the promise simply
    // resolves later, ignored — there is nothing to attach or cancel.
    return outcome;
  }
}

// ERS-1 ladder, named per error path (§07.29 AD-1 — the retry vs 60 s
// re-probe boundary). Every dial-action / chip error path in this file
// names the same boundary:
//   retry (inside the operation, bounded): the M4 re-send once after
//     3 s, then the user Retry button re-sending the SAME command_id.
//   re-probe (outside the operation, silent): the card's 60 s
//     re-probe, which re-checks bridge availability and clears (on
//     recovery, silently) or re-surfaces (on persistence) the error.
//     The re-probe never re-issues a user command.
const ERS_BOUNDARY =
  "Retry: M4 re-send once after 3 s, then user Retry (same command_id). " +
  "Re-probe: card 60 s availability check; clears silently on recovery.";

// -- The card (W5-1 rendering, W5-2 entry, W5-4 actions, W5-5 states) ----
//
// The card renders ONLY what the store reports. It holds no timer
// state: dials are keyed by entity id and reconciled against the
// snapshot; a timer the store drops disappears from the view. The one
// per-dial UI state (cancel-armed) lives on the dial element's record
// below, not in any timer registry — it changes no send and no render
// of any other dial.
//
// Voice hints (W5-2): the hint slot is an empty slot while the
// provider is not `healthy` — no status line, no banner, and never
// the retired voice-unavailable text.

const VOICE_HINTS = Object.freeze([
  "Say “Okay Nabu, set a 10 minute pasta timer”",
  "To add time, say “Okay Nabu, add 5 more minutes to the pasta timer”",
]);

class TimerScreenCard extends HTMLElement {
  constructor() {
    super();
    this._config = {};
    this._hass = null;
    this._store = null;
    this._injectedStore = null;
    this._storeState = "unresolved"; // unresolved | ready | failed
    this._storeLookup = null;
    this._snapshot = [];
    this._snapshotAtMs = 0;
    this._voiceHealthy = false;
    this._bridgeVersion = null;
    this._bridgeMinCardVersion = null;
    this._tickTimer = null;
    this._reProbeTimer = null;
    this._errorSlot = null; // ERS-5: exactly one, newer wins (J-W5-12)
    this._pinnedTimerId = null;
    this._arrivedFromHome = false;
    this._pinScrolled = false;
    this._scrollSaveTimer = null;
    this._stepper = { open: false, hours: 0, minutes: 10, seconds: 0 };
    this._dialEls = new Map(); // entityId -> { root, time, name, hint, ring, actions, ... }
    this._undoBars = new Map(); // timerId -> { root, timerId, name, expiresAtMs, hideTimer }
    this._cancelArmed = new Map(); // entityId -> arm timeout id
    this._pendingScrollTimerId = null; // start-scroll deferral (see _afterStartScroll)
    this._els = null;
    this._warned = new Set();
  }

  // -- Lifecycle -----------------------------------------------------------

  setConfig(config) {
    this._config = config && typeof config === "object" ? config : {};
    this._ensureShell();
    this._render();
  }

  getCardSize() {
    return CARD_SIZE_ROWS;
  }

  set hass(hass) {
    this._hass = hass || null;
    this._readProviderSensors(hass);
    void this._resolveStore();
    this._render();
  }

  get hass() {
    return this._hass;
  }

  connectedCallback() {
    this._ensureShell();
    this._pinnedTimerId = readTimersPin(this._storage());
    this._arrivedFromHome = readTimersArrival(this._storage());
    this._restoreScroll();
    if (this._tickTimer === null) {
      this._tickTimer = setInterval(() => this._tick(), DISPLAY_TICK_MS);
    }
    void this._resolveStore();
    this._render();
  }

  disconnectedCallback() {
    this._saveScroll();
    if (this._tickTimer !== null) {
      clearInterval(this._tickTimer);
      this._tickTimer = null;
    }
    this._stopReProbe();
    for (const [, id] of this._cancelArmed) clearTimeout(id);
    this._cancelArmed.clear();
    for (const [, bar] of this._undoBars) clearTimeout(bar.hideTimer);
    this._undoBars.clear();
  }

  // -- Test seams (never used in the browser) ------------------------------
  // Inject a store (the leg page injects the real shell's `timerStore`,
  // built by `ensureShell`). Also used by the node tests with a
  // hand-built store double.
  _setStoreForTests(store) {
    this._injectedStore = store || null;
    if (store) {
      this._store = store;
      this._storeState = "ready";
    } else {
      this._store = null;
      this._storeState = "unresolved";
    }
    this._render();
  }

  // Inject a hass object without going through the `set hass` setter's
  // store resolution (node tests that drive the protocol directly).
  _setHassForTests(hass) {
    this._hass = hass || null;
    this._readProviderSensors(hass);
  }

  _storage() {
    try {
      return typeof sessionStorage !== "undefined" ? sessionStorage : null;
    } catch (_err) {
      return null;
    }
  }

  _warnOnce(key, message, data) {
    if (this._warned.has(key)) return;
    this._warned.add(key);
    if (typeof console !== "undefined" && typeof console.warn === "function") {
      console.warn(`Family Hub timers: ${message}`, data ?? "");
    }
  }

  _warn(message, data) {
    if (typeof console !== "undefined" && typeof console.warn === "function") {
      console.warn(`Family Hub timers: ${message}`, data ?? "");
    }
  }

  // -- Store resolution (J-W5-01) ------------------------------------------
  //
  // Order: injected store → `window.__familyHub.shell` registry →
  // dynamic import of the shell module via the Lovelace resource list.
  // The lookup runs once per mount; a failure is a state ("failed"),
  // rendered honestly, not retried in a loop — the 60 s re-probe
  // covers the persistent case. No timer send waits on this: sends
  // need only hass, and the store is read-only for rendering.

  async _resolveStore() {
    if (this._store || this._storeLookup) return this._storeLookup;
    this._storeLookup = this._lookupStore();
    return this._storeLookup;
  }

  async _lookupStore() {
    if (this._injectedStore) {
      this._store = this._injectedStore;
      this._storeState = "ready";
      this._render();
      return this._store;
    }
    try {
      const registry =
        typeof globalThis !== "undefined" ? globalThis.__familyHub : null;
      const shell = registry && registry.shell;
      if (shell && shell.timerStore) {
        this._store = shell.timerStore;
        this._storeState = "ready";
        this._render();
        return this._store;
      }
    } catch (_err) {
      // A registry read that throws is no registry at all.
    }
    try {
      const url = await this._lookupShellUrl();
      if (url) {
        const mod = await import(url);
        if (mod && typeof mod.ensureShell === "function") {
          const built = mod.ensureShell({ hass: this._hass });
          if (built && built.timerStore) {
            this._store = built.timerStore;
            this._storeState = "ready";
            this._render();
            return this._store;
          }
        }
      }
    } catch (error) {
      this._warn("Could not load the hub shell module — timers will load from the hub shell when it is available.", error);
    }
    this._storeState = "failed";
    this._render();
    return null;
  }

  // Find the deployed shell-module URL from the Lovelace resource list
  // (the same lookup pattern the old card used for family-hub-core.js).
  // Returns null when the list cannot be read or the module is not
  // registered yet — the shell module is not deployed today (OQ-W5-01).
  async _lookupShellUrl() {
    try {
      const hass = this._hass;
      if (!hass || typeof hass.callWS !== "function") return null;
      const resources = await hass.callWS({ type: "lovelace/resources/list" });
      const list = Array.isArray(resources) ? resources : [];
      for (const res of list) {
        const url = res && typeof res.url === "string" ? res.url : "";
        if (url.split("?")[0] === SHELL_RESOURCE_PATH) return url;
      }
      return null;
    } catch (_err) {
      return null;
    }
  }

  // -- Provider sensors (non-timer reads from the pushed object) -----------
  //
  // Two sensors, read from the hass object Lovelace pushes — never a
  // subscription, never a call. Voice health (§08 row 18) and the
  // bridge version sensor (§03.6) are the only non-timer state this
  // card reads. A missing sensor reads as "not healthy" / "unknown"
  // respectively — never guessed at.

  _readProviderSensors(hass) {
    const states = hass && Array.isArray(hass.states) ? hass.states : [];
    const find = (entityId) => {
      for (const state of states) {
        if (state && state.entity_id === entityId) return state;
      }
      return null;
    };
    const voice = find(VOICE_HEALTH_ENTITY_ID);
    this._voiceHealthy = Boolean(voice) && voice.state === "healthy";
    const bridge = find(BRIDGE_VERSION_ENTITY_ID);
    if (bridge) {
      this._bridgeVersion =
        typeof bridge.state === "string" ? bridge.state : null;
      const attrs = bridge.attributes ?? {};
      this._bridgeMinCardVersion =
        typeof attrs.min_card_version === "string"
          ? attrs.min_card_version
          : null;
    } else {
      this._bridgeVersion = null;
      this._bridgeMinCardVersion = null;
    }
  }

  _bridgeGate(snapshotLength = 0) {
    // No version sensor at all is unreadable, never guessed. With
    // timers present the bridge is plainly there (sensors prove it),
    // so the gate is FULL; with zero timers and no version sensor the
    // mismatch would be invisible — that is the skew leg (G-08), so
    // the gate degrades and the notice says so.
    if (this._bridgeVersion === null) {
      if (snapshotLength === 0) {
        return { gate: "DEGRADED", reason: "unreadable-bridge-version", detail: "The bridge version could not be read.", bridgeVersion: null };
      }
      return { gate: "FULL", reason: "ok", detail: "", bridgeVersion: null };
    }
    const result = evaluateBridgeGate(this._bridgeVersion, this._bridgeMinCardVersion);
    result.bridgeVersion = this._bridgeVersion;
    return result;
  }

  // -- Rendering (W5-1) ----------------------------------------------------
  //
  // Keyed reconcile: dial elements are built once per entity id and
  // updated in place. A 1 s tick that rebuilt the list would destroy
  // focus, scroll, and the dial-enter animation; updating text nodes
  // does none of those. A dial whose record changed state is rebuilt
  // (its actions differ by state); a dial whose record only ticked
  // gets its time text and progress updated.

  _tick() {
    this._render();
  }

  _render() {
    if (!this._els) return;
    const nowMs = Date.now();
    this._renderVoiceHints();
    this._renderStepper();
    // The back bar reads the pin / arrival, which are only known
    // after `connectedCallback` reads them (CB-1): `_ensureShell`
    // runs before that read, so the bar is decided here, on every
    // render, not once at shell build. The update is cheap and
    // idempotent — the label is only rewritten when it changes.
    this._updateBackBar();
    if (this._storeState === "unresolved") {
      // The gate notice never flashes over the loading state.
      this._renderGateNotice(0);
      this._showLoading();
      return;
    }
    if (this._storeState === "failed" || !this._store) {
      // The gate notice never flashes over the store-failed state.
      this._renderGateNotice(this._snapshot.length);
      this._showStoreFailed();
      return;
    }
    // The snapshot is read BEFORE the gate is evaluated: the gate
    // must see this render's timers, not the previous render's.
    // (D-W5-08: evaluating the gate first left a stale DEGRADED
    // notice — a ~200 px banner — on screen for a full tick after a
    // timer appeared, pushing its dial below the fold on a phone.)
    const snapshot = this._store.snapshot();
    this._snapshot = snapshot;
    this._snapshotAtMs = nowMs;
    this._renderGateNotice(snapshot.length);
    this._renderTimers(snapshot, nowMs);
    this._renderEmpty(snapshot);
    this._maybeScrollToPin(nowMs);
  }

  _showLoading() {
    this._els.loading.hidden = false;
    this._els.storeError.hidden = true;
    this._els.empty.hidden = true;
    this._els.dials.hidden = true;
    this._els.undoWrap.hidden = true;
    this._els.loading.textContent = "Loading timers…";
  }

  _showStoreFailed() {
    this._els.loading.hidden = true;
    this._els.empty.hidden = true;
    this._els.dials.hidden = true;
    this._els.storeError.hidden = false;
    this._els.undoWrap.hidden = true;
    this._els.storeErrorMessage.textContent =
      "Timers couldn't load — the hub shell isn't available on this page yet. " +
      "Timers still work by voice, and this view recovers when the hub shell is deployed.";
    // The store failure is persistent-class: arm the 60 s re-probe so
    // the error re-surfaces / clears without a reload. The slot is
    // set ONCE, on entering the failed state (NB-3): re-minting it on
    // every 1 s tick rebuilt the chip-error DOM once a second for as
    // long as the store stayed failed. A newer error that supersedes
    // this slot (a chip failure, a success-clear) lets the next tick
    // re-assert the store failure — the persistent failure re-appears,
    // it is not lost — but a tick that finds the store-failed slot
    // still held changes nothing.
    if (!this._errorSlot || this._errorSlot.kind !== "store-failed") {
      this._setErrorSlot({
        kind: "store-failed",
        scope: "store",
        message: this._els.storeErrorMessage.textContent,
        repair: true,
        persistent: true,
        retry: null,
      });
    }
  }

  _renderEmpty(snapshot) {
    this._els.empty.hidden = snapshot.length !== 0;
    if (snapshot.length === 0) {
      this._els.empty.textContent =
        "No timers yet — start one below, or ask for one by voice.";
    }
  }

  _renderTimers(snapshot, nowMs) {
    this._els.loading.hidden = true;
    this._els.storeError.hidden = true;
    this._els.dials.hidden = false;
    const seen = new Set();
    for (const record of snapshot) {
      seen.add(record.entityId);
      const existing = this._dialEls.get(record.entityId);
      if (existing && existing.state === record.state) {
        this._updateDial(existing, record, nowMs);
      } else {
        if (existing) this._removeDial(record.entityId);
        const built = this._buildDial(record, nowMs);
        this._els.dials.appendChild(built.root);
        this._dialEls.set(record.entityId, built);
        // The dial is laid out only once appended: fit its countdown
        // to the ring now, so the first paint already fits (CB-2).
        this._fitDialTime(built);
      }
      // Scroll-after-start (NB-1): the pending id is the timer the
      // last start acked. The check runs for every record in the
      // snapshot, not only for a newly built dial — the sensor can
      // reach the store (and the tick can build its dial) before the
      // ack resolves, and the scroll must still fire for the dial the
      // user just started. The id is cleared on the first match, so
      // the scroll fires once at most.
      if (this._pendingScrollTimerId && this._pendingScrollTimerId === record.timerId) {
        this._pendingScrollTimerId = null;
        const built = this._dialEls.get(record.entityId);
        if (built) {
          void afterAnimation(built.root).then(() => {
            if (typeof built.root.scrollIntoView === "function") {
              built.root.scrollIntoView({ block: "nearest", behavior: "smooth" });
            }
          });
        }
      }
    }
    for (const entityId of [...this._dialEls.keys()]) {
      if (!seen.has(entityId)) this._removeDial(entityId);
    }
    this._els.undoWrap.hidden = this._undoBars.size === 0;
  }

  _removeDial(entityId) {
    const built = this._dialEls.get(entityId);
    if (!built) return;
    if (built.root && typeof built.root.remove === "function") built.root.remove();
    this._dialEls.delete(entityId);
    const arm = this._cancelArmed.get(entityId);
    if (arm !== undefined) {
      clearTimeout(arm);
      this._cancelArmed.delete(entityId);
    }
  }

  // The countdown for a record, from the server-side monotonic
  // `remaining` (gap B06-5): `finishes_at` is a cross-check only, never
  // the countdown source. `observedAtMs` is this card's snapshot-read
  // instant (the store does not expose its own); a paused timer's
  // remaining never moves.
  _remainingNow(record, nowMs) {
    if (record.state === "paused" || record.state === "finished") {
      return record.remainingSeconds;
    }
    const observedAtMs = this._snapshotAtMs || nowMs;
    const elapsedS = (nowMs - observedAtMs) / 1000;
    return Math.max(0, record.remainingSeconds - elapsedS);
  }

  _isStale(record, nowMs) {
    if (record.state === "paused" || record.state === "finished") return false;
    if (!record.lastUpdated) return false;
    const updatedMs = Date.parse(record.lastUpdated);
    if (!Number.isFinite(updatedMs)) return false;
    return nowMs - updatedMs > STALE_AFTER_MS;
  }

  _isSoon(record, nowMs) {
    return (
      record.state === "active" &&
      this._remainingNow(record, nowMs) < SOON_THRESHOLD_SECONDS
    );
  }

  // The dial's hint line per state (W5-5). Finished readings carry
  // the plain-words reason; the machine reason code is logged, never
  // shown (see _logMissedAlarm).
  _hintFor(record, nowMs) {
    const stale = this._isStale(record, nowMs);
    if (record.state === "ringing") return "Ringing — tap to stop";
    if (record.state === "paused") return stale ? "Paused — waiting for update" : "Paused";
    if (record.state === "active") {
      if (stale) return "Running — waiting for update";
      if (this._isSoon(record, nowMs)) return "Running — under a minute left";
      return "Running";
    }
    // finished
    const finishedAtMs = record.lastChanged ? Date.parse(record.lastChanged) : NaN;
    const agoText = formatMinutesAgo(finishedAtMs, nowMs);
    if (record.alarmMissed) {
      this._logMissedAlarm(record);
      return missedAlarmReading(agoText);
    }
    return finishedReading(agoText);
  }

  _logMissedAlarm(record) {
    const key = `missed:${record.timerId}:${record.lastChanged ?? ""}`;
    this._warnOnce(
      key,
      `Timer "${record.name}" finished without its alarm sounding` +
        ` (reason: ${record.alarmMissedReason ?? "unknown"}).`,
      { timerId: record.timerId },
    );
  }

  // Ring progress: remaining / duration. An adopted timer with no
  // duration renders a full track with no progress arc (no invented
  // denominator). A finished timer renders a full amber ring (§03,
  // Oct 31: "full amber ring, 'Done'").
  _ringProgress(record, nowMs) {
    if (record.state === "finished") return 1;
    if (!record.durationSeconds || record.durationSeconds <= 0) return null;
    return Math.min(
      1,
      Math.max(0, this._remainingNow(record, nowMs) / record.durationSeconds),
    );
  }

  _ringColor(record, nowMs) {
    if (record.state === "ringing") return "#B71C1C";
    if (record.state === "finished") return "#B45309";
    if (record.state === "paused") return "#C2410C";
    if (this._isSoon(record, nowMs)) return "#B45309";
    return "#C2410C";
  }

  _buildDial(record, nowMs) {
    const doc = this._document();
    const root = doc.createElement("article");
    root.className = "dial";
    root.setAttribute("data-entity-id", record.entityId);
    root.setAttribute("data-state", record.state);

    const ringButton = doc.createElement("button");
    ringButton.type = "button";
    ringButton.className = "dial-ring";
    ringButton.setAttribute("data-action", "dial");
    ringButton.setAttribute("aria-label", this._dialActionLabel(record));
    const svg = this._buildRingSvg(doc, record, nowMs);
    const time = doc.createElement("span");
    time.className = "dial-time";
    ringButton.appendChild(svg);
    ringButton.appendChild(time);

    const body = doc.createElement("div");
    body.className = "dial-body";
    const name = doc.createElement("div");
    name.className = "dial-name";
    const hint = doc.createElement("div");
    hint.className = "dial-hint";
    body.appendChild(name);
    body.appendChild(hint);

    const error = doc.createElement("div");
    error.className = "dial-error";
    error.hidden = true;
    body.appendChild(error);

    const actions = doc.createElement("div");
    actions.className = "dial-actions";

    root.appendChild(ringButton);
    root.appendChild(body);
    root.appendChild(actions);
    root.addEventListener("click", (event) => this._onDialClick(event, record.entityId));

    const built = {
      root,
      ringButton,
      ringSvg: svg,
      ringArc: svg._arc || null,
      time,
      name,
      hint,
      error,
      actions,
      state: record.state,
    };
    this._updateDial(built, record, nowMs);
    return built;
  }

  _dialActionLabel(record) {
    if (record.state === "active") return `Pause the ${record.name} timer`;
    if (record.state === "paused") return `Resume the ${record.name} timer`;
    if (record.state === "ringing") return `Stop the ${record.name} timer`;
    return `Dismiss the ${record.name} timer`;
  }

  _buildRingSvg(doc, record, nowMs) {
    const NS = "http://www.w3.org/2000/svg";
    const svg = doc.createElementNS
      ? doc.createElementNS(NS, "svg")
      : doc.createElement("svg");
    svg.setAttribute("viewBox", `0 0 ${DIAL_RING_PX} ${DIAL_RING_PX}`);
    svg.setAttribute("class", "dial-ring-svg");
    svg.setAttribute("aria-hidden", "true");
    const track = doc.createElementNS
      ? doc.createElementNS(NS, "circle")
      : doc.createElement("circle");
    track.setAttribute("cx", String(DIAL_RING_PX / 2));
    track.setAttribute("cy", String(DIAL_RING_PX / 2));
    track.setAttribute("r", String(DIAL_RING_ARC_RADIUS_PX));
    track.setAttribute("class", "dial-ring-track");
    const arc = doc.createElementNS
      ? doc.createElementNS(NS, "circle")
      : doc.createElement("circle");
    arc.setAttribute("cx", String(DIAL_RING_PX / 2));
    arc.setAttribute("cy", String(DIAL_RING_PX / 2));
    arc.setAttribute("r", String(DIAL_RING_ARC_RADIUS_PX));
    arc.setAttribute("class", "dial-ring-arc");
    svg.appendChild(track);
    svg.appendChild(arc);
    svg._arc = arc;
    return svg;
  }

  _updateDial(built, record, nowMs) {
    built.state = record.state;
    built.root.setAttribute("data-state", record.state);
    built.ringButton.setAttribute("aria-label", this._dialActionLabel(record));
    built.name.textContent = record.name;
    built.hint.textContent = this._hintFor(record, nowMs);
    built.hint.classList.toggle("stale", this._isStale(record, nowMs));
    if (record.state === "finished") {
      built.time.textContent = "Done";
    } else {
      built.time.textContent = formatRemaining(this._remainingNow(record, nowMs));
    }
    built.time.classList.toggle("stale", this._isStale(record, nowMs));
    // The countdown text changed (or the viewport did): re-fit it to
    // the ring (CB-2). Runs on every tick for every attached dial —
    // one layout read per dial per second — and self-corrects on
    // resize, because the fit always starts from the current CSS size.
    this._fitDialTime(built);
    const progress = this._ringProgress(record, nowMs);
    const circumference = 2 * Math.PI * DIAL_RING_ARC_RADIUS_PX;
    if (built.ringArc && typeof built.ringArc.setAttribute === "function") {
      built.ringArc.setAttribute("stroke", this._ringColor(record, nowMs));
      built.ringArc.setAttribute("stroke-dasharray", String(circumference.toFixed(1)));
      const offset =
        progress === null ? circumference : circumference * (1 - progress);
      built.ringArc.setAttribute("stroke-dashoffset", String(offset.toFixed(1)));
      built.ringArc.setAttribute(
        "opacity",
        record.state === "paused" ? "0.45" : "1",
      );
    }
    this._renderDialActions(built, record);
    this._renderDialError(built, record);
    // A ringing dial that never got its ring-start acknowledgement is
    // a bridge-side failure, not a card rendering choice — but the
    // card must never sit on it silently: the G-03 invariant is
    // asserted in the legs, and a ringing dial that outlives the ring
    // window is logged once here so a stuck ring is visible in the
    // diagnostics log (the 1-hour window is settled §03.8; the card
    // logs, it does not re-ring or stop anything itself).
    if (record.state === "ringing") {
      const ringingSinceMs = record.lastChanged
        ? Date.parse(record.lastChanged)
        : NaN;
      if (
        Number.isFinite(ringingSinceMs) &&
        nowMs - ringingSinceMs > 3600 * 1000
      ) {
        this._warnOnce(
          `ring-over-window:${record.timerId}`,
          `Timer "${record.name}" has been ringing for over an hour with no acknowledgement.`,
          { timerId: record.timerId },
        );
      }
    }
  }

  // Fit one dial's countdown inside its ring (CB-2): the TIMER-6
  // clamp is the base size, and a countdown that paints past the ring
  // is shrunk to fit — measured, not guessed. The inline size is
  // reset first, so a shorter countdown grows back to the clamp size
  // and a viewport change re-fits from the current clamp value. A
  // dial that is not laid out yet (detached, or a host without layout
  // measurement) is left alone — the render path fits it after
  // append, and the next tick re-fits it once attached.
  // Fit the countdown inside the ring (CB-2): the TIMER-6 clamp
  // alone lets a long countdown paint past the ring. The fit runs on
  // every dial update (per-second tick, resize, and first paint), so
  // a shorter countdown grows back and a resized viewport re-fits.
  // The width is measured with a Range over the text node, NOT
  // scrollWidth: the time is centred in the ring, and centred
  // overflowing text extends past BOTH edges — scrollWidth only
  // counts overflow past the right edge of the box, so it under-reads
  // by half the overflow (at 72 px "23:59:59" is 280 px wide but
  // scrollWidth reads 204 px; the first fit, built on that, landed
  // at 42 px and still overflowed by 24 px). A Range reads the
  // glyphs themselves. Where Range is unavailable (the node FakeDOM),
  // scrollWidth is the fallback — and the nowrap rule keeps the text
  // one line either way, so the fallback still measures the string.
  _fitDialTime(built) {
    const el = built && built.time;
    if (!el || typeof el.style !== "object") return;
    el.style.fontSize = "";
    if (typeof el.scrollWidth !== "number" || typeof el.clientWidth !== "number") return;
    const boxPx = el.clientWidth;
    if (boxPx <= 0) return;
    let cssPx = NaN;
    try {
      const computed = typeof getComputedStyle === "function" ? getComputedStyle(el) : null;
      cssPx = computed ? parseFloat(computed.fontSize) : NaN;
    } catch (_err) {
      return;
    }
    // Prefer the Range width (the glyphs); fall back to scrollWidth.
    let contentPx = el.scrollWidth;
    try {
      const doc = this._document();
      const createRange = doc && typeof doc.createRange === "function" ? doc.createRange : null;
      if (createRange) {
        const range = createRange.call(doc);
        range.selectNodeContents(el);
        const rect = range.getBoundingClientRect();
        if (rect && typeof rect.width === "number" && rect.width > 0) contentPx = rect.width;
      }
    } catch (_err) {
      // A Range that throws is no Range at all — the scrollWidth
      // measurement above stands.
    }
    const fitted = fitFontSizeToWidth(cssPx, contentPx, boxPx);
    if (fitted !== null) el.style.fontSize = `${fitted}px`;
  }

  _renderDialActions(built, record) {
    // Actions are rebuilt only when the set changes (state, or the
    // cancel arm). Rebuilding them on every 1 s tick would destroy
    // focus on the Cancel button once a second.
    const armed = this._cancelArmed.has(record.entityId);
    const key = `${record.state}:${armed ? "armed" : "idle"}`;
    if (built.actionsFor === key) return;
    built.actionsFor = key;
    const doc = this._document();
    while (built.actions.firstChild) {
      built.actions.removeChild(built.actions.firstChild);
    }
    if (record.state === "ringing") {
      const stop = doc.createElement("button");
      stop.type = "button";
      stop.className = "dial-stop";
      stop.setAttribute("data-action", "acknowledge");
      stop.textContent = "Stop";
      built.actions.appendChild(stop);
      return;
    }
    if (record.state === "finished") {
      const restart = doc.createElement("button");
      restart.type = "button";
      restart.className = "dial-restart";
      restart.setAttribute("data-action", "restart");
      restart.textContent = "Restart";
      built.actions.appendChild(restart);
      return;
    }
    // active / paused: Cancel, tap-to-arm, always visible (80 px,
    // Oct 31 ruling). There is NO +1 min button (Oct 31) and no
    // on-screen add-time control: add-time is voice / service only.
    const cancel = doc.createElement("button");
    cancel.type = "button";
    cancel.className = "dial-cancel";
    cancel.setAttribute("data-action", "cancel");
    cancel.textContent = armed ? "Tap again to cancel" : "Cancel";
    cancel.classList.toggle("armed", armed);
    built.actions.appendChild(cancel);
  }

  _renderDialError(built, record) {
    const slot = this._errorSlot;
    const key = slot && slot.scope === `dial:${record.entityId}` ? slot.id : null;
    if (built.errorFor === key) return;
    built.errorFor = key;
    if (!slot || slot.scope !== `dial:${record.entityId}`) {
      built.error.hidden = true;
      built.error.textContent = "";
      while (built.error.firstChild) built.error.removeChild(built.error.firstChild);
      return;
    }
    built.error.hidden = false;
    while (built.error.firstChild) built.error.removeChild(built.error.firstChild);
    const doc = this._document();
    const text = doc.createElement("span");
    text.textContent = slot.message;
    built.error.appendChild(text);
    if (slot.retry) {
      const retry = doc.createElement("button");
      retry.type = "button";
      retry.setAttribute("data-action", "dial-retry");
      retry.textContent = slot.retry.label || "Retry";
      retry.addEventListener("click", (event) => {
        event.stopPropagation();
        slot.retry.run();
      });
      built.error.appendChild(retry);
    }
    if (slot.repair) {
      const repair = doc.createElement("button");
      repair.type = "button";
      repair.setAttribute("data-action", "repair");
      repair.textContent = "Repair";
      repair.addEventListener("click", (event) => {
        event.stopPropagation();
        this._showRepairProcedure(built.error, slot);
      });
      built.error.appendChild(repair);
    }
  }

  // -- Arrival contracts (W5-1) ---------------------------------------------
  //
  // Pin: the PIP tap publishes `family-hub:timers:pin = {timerId}`;
  // the card scrolls the pinned dial into view (after its enter
  // animation, never a fixed delay) and marks it once. Arrival: the
  // Home card publishes `family-hub:timers:arrival = "home"`; the card
  // shows the "← Back" bar.

  _maybeScrollToPin(nowMs) {
    if (!this._pinnedTimerId || this._pinScrolled) return;
    // The pin holds a timer id, not an entity id (NB-2): the dial is
    // found through the snapshot record that carries the id, never by
    // building an entity id from the timer id — the two no longer
    // share a case (the store id is the canonical uppercase id while
    // entity ids stay lowercase), so the construction could only ever
    // find a dial while the B-1 defect lasted.
    const record = this._snapshot.find((r) => r.timerId === this._pinnedTimerId);
    const built = record ? this._dialEls.get(record.entityId) : undefined;
    if (!built) return;
    this._pinScrolled = true;
    built.root.classList.add("pinned");
    void afterAnimation(built.root).then(() => {
      if (typeof built.root.scrollIntoView === "function") {
        built.root.scrollIntoView({ block: "nearest", behavior: "smooth" });
      }
    });
  }

  // -- Scroll persistence ----------------------------------------------------

  _restoreScroll() {
    const storage = this._storage();
    if (!storage) return;
    let raw = null;
    try {
      raw = storage.getItem(TIMER_SCROLL_KEY);
    } catch (_err) {
      return;
    }
    const state = parseTimerScroll(raw);
    if (!state) return;
    try {
      if (typeof document !== "undefined" && document.documentElement) {
        document.documentElement.scrollTop = state.page;
      }
    } catch (_err) {
      // A scroll restore that cannot run is a no-op, never an error.
    }
  }

  _saveScroll() {
    const storage = this._storage();
    if (!storage) return;
    let page = 0;
    try {
      if (typeof document !== "undefined" && document.documentElement) {
        page = document.documentElement.scrollTop || 0;
      }
    } catch (_err) {
      page = 0;
    }
    try {
      storage.setItem(
        TIMER_SCROLL_KEY,
        serializeTimerScroll({ page, screen: 0 }),
      );
    } catch (_err) {
      // Storage that cannot be written is a no-op, never an error.
    }
  }

  _document() {
    if (typeof document !== "undefined") return document;
    throw new Error("TimerScreenCard: no document available.");
  }

  // -- Shell (static DOM, built once) ----------------------------------------
  //
  // Stable nodes: the shell is built once and only its contents change.
  // Rebuilding the whole screen on every tick would destroy focus in
  // the stepper and drop scroll position. Every dynamic region below
  // is a stable element whose children are reconciled, never replaced
  // wholesale — except the dial actions and dial errors, which rebuild
  // only when their key changes (see _renderDialActions).

  _ensureShell() {
    if (this._els) return;
    const shadow =
      typeof this.attachShadow === "function"
        ? this.attachShadow({ mode: "open" })
        : null;
    if (!shadow) return;
    this._shadow = shadow;
    const doc = this._document();

    const style = doc.createElement("style");
    style.textContent = CARD_CSS;
    shadow.appendChild(style);

    const root = doc.createElement("div");
    root.className = "screen";
    shadow.appendChild(root);

    const backBar = doc.createElement("div");
    backBar.className = "back-bar";
    backBar.hidden = true;
    const backButton = doc.createElement("button");
    backButton.type = "button";
    backButton.className = "back-button";
    backButton.setAttribute("data-action", "back");
    backButton.textContent = "← Back";
    backButton.addEventListener("click", () => this._goBack());
    backBar.appendChild(backButton);
    root.appendChild(backBar);

    const title = doc.createElement("h1");
    title.className = "title";
    title.textContent = "Timers";
    root.appendChild(title);

    const gateNotice = doc.createElement("div");
    gateNotice.className = "gate-notice";
    gateNotice.setAttribute("role", "status");
    gateNotice.hidden = true;
    root.appendChild(gateNotice);

    const loading = doc.createElement("div");
    loading.className = "loading";
    loading.hidden = true;
    root.appendChild(loading);

    const storeError = doc.createElement("div");
    storeError.className = "store-error";
    storeError.setAttribute("role", "alert");
    storeError.hidden = true;
    const storeErrorMessage = doc.createElement("span");
    storeError.appendChild(storeErrorMessage);
    root.appendChild(storeError);

    const empty = doc.createElement("div");
    empty.className = "empty";
    empty.hidden = true;
    root.appendChild(empty);

    const dials = doc.createElement("div");
    dials.className = "dials";
    root.appendChild(dials);

    const undoWrap = doc.createElement("div");
    undoWrap.className = "undo-wrap";
    undoWrap.hidden = true;
    root.appendChild(undoWrap);

    // Entry row (D2): the touch entry is anchored to the safe-area
    // bottom — sticky bottom, so the chips stay reachable after any
    // scroll, including after a screensaver dismiss (F-T22 / I26).
    const entryRow = doc.createElement("div");
    entryRow.className = "entry-row";

    const chipRow = doc.createElement("div");
    chipRow.className = "chip-row";
    for (const minutes of PRESET_MINUTES) {
      const chip = doc.createElement("button");
      chip.type = "button";
      chip.className = "preset-chip";
      chip.setAttribute("data-action", "preset");
      chip.setAttribute("data-minutes", String(minutes));
      chip.setAttribute("aria-label", `Start a ${minutes} minute timer`);
      chip.textContent = `${minutes} min`;
      chipRow.appendChild(chip);
    }
    const setTimeButton = doc.createElement("button");
    setTimeButton.type = "button";
    setTimeButton.className = "set-time-button";
    setTimeButton.setAttribute("data-action", "toggle-stepper");
    setTimeButton.setAttribute("aria-expanded", "false");
    setTimeButton.setAttribute("aria-label", "Set a timer for any length of time");
    setTimeButton.textContent = "Set a time";
    chipRow.appendChild(setTimeButton);
    entryRow.appendChild(chipRow);

    const stepper = doc.createElement("div");
    stepper.className = "stepper";
    stepper.hidden = true;
    entryRow.appendChild(stepper);

    // The chip-row error element (A5): the honest in-place error
    // renders here — OUTSIDE any element hidden when timers exist
    // (the old `_emptyErrorEl` defect, invisible when timers were
    // running, is fixed by construction: this element is never
    // hidden by timer presence).
    const chipError = doc.createElement("div");
    chipError.className = "chip-error";
    chipError.setAttribute("role", "alert");
    chipError.hidden = true;
    entryRow.appendChild(chipError);

    // The voice-hint slot: an empty slot while the provider is not
    // `healthy` — no status line, no banner, and never the retired
    // voice-unavailable text.
    const voiceHint = doc.createElement("div");
    voiceHint.className = "voice-hint";
    voiceHint.hidden = true;
    entryRow.appendChild(voiceHint);

    root.appendChild(entryRow);

    this._els = {
      root,
      backBar,
      backButton,
      gateNotice,
      loading,
      storeError,
      storeErrorMessage,
      empty,
      dials,
      undoWrap,
      entryRow,
      chipRow,
      setTimeButton,
      stepper,
      chipError,
      voiceHint,
    };
    this._buildStepper();
    root.addEventListener("click", (event) => this._onRootClick(event));
  }

  _updateBackBar() {
    if (!this._els) return;
    const show = Boolean(this._pinnedTimerId || this._arrivedFromHome);
    this._els.backBar.hidden = !show;
    // The label is only rewritten when it changes: `_render` calls
    // this on every tick, and rewriting the button's text every
    // second would churn the DOM for a label that never moves.
    const label = this._pinnedTimerId ? "← Back to recipe" : "← Back to Home";
    if (this._els.backButton.textContent !== label) {
      this._els.backButton.textContent = label;
    }
  }

  _goBack() {
    // The dismiss pop (§02.2): one history step back to the origin
    // view. A pop that does nothing FAILS — but the back bar only
    // renders when the card arrived from somewhere (pin or Home
    // arrival), so there is always an entry underneath.
    try {
      if (typeof window !== "undefined" && window.history) {
        window.history.back();
      }
    } catch (_err) {
      // No window/history (node tests): the tap is a no-op.
    }
  }

  // -- Gate notice (W5-1, §07.24 DEGRADED form) ------------------------------

  // snapshotLength is THIS render's timer count, passed in — the
  // gate must not read a stale snapshot (D-W5-08).
  _renderGateNotice(snapshotLength = 0) {
    if (!this._els) return;
    // Loading / store-failed have their own states; the gate notice
    // never flashes over them.
    if (this._storeState !== "ready") {
      this._els.gateNotice.hidden = true;
      return;
    }
    const gate = this._bridgeGate(snapshotLength);
    const copy = gateNoticeCopy(gate);
    if (!copy) {
      this._els.gateNotice.hidden = true;
      return;
    }
    this._els.gateNotice.hidden = false;
    // Rebuild only when the copy changes (same key guard as the dials).
    const key = `${gate.reason}:${copy.body}`;
    if (this._els.gateNoticeKey === key) return;
    this._els.gateNoticeKey = key;
    const doc = this._document();
    while (this._els.gateNotice.firstChild) {
      this._els.gateNotice.removeChild(this._els.gateNotice.firstChild);
    }
    const title = doc.createElement("strong");
    title.textContent = copy.title;
    const body = doc.createElement("p");
    body.textContent = copy.body;
    const repair = doc.createElement("button");
    repair.type = "button";
    repair.setAttribute("data-action", "repair");
    repair.textContent = copy.repairLabel;
    repair.addEventListener("click", (event) => {
      // The root listener also handles `repair` taps (by appending
      // the procedure to the chip-error element) — without the stop,
      // one click appends a procedure to BOTH elements (CNB-2: the
      // chip-error copy is invisible today, but it is a real
      // double-append with a stale-slot procedure). Every other
      // Repair button in this file stops propagation; this one was
      // the exception.
      event.stopPropagation();
      this._showRepairProcedure(this._els.gateNotice, {
        kind: "gate",
        message: copy.body,
        repair: true,
      });
    });
    this._els.gateNotice.appendChild(title);
    this._els.gateNotice.appendChild(body);
    this._els.gateNotice.appendChild(repair);
  }

  // -- Voice hints (W5-2, §04.3) ----------------------------------------------

  _renderVoiceHints() {
    if (!this._els) return;
    const show = this._voiceHealthy;
    this._els.voiceHint.hidden = !show;
    if (!show) return;
    const key = VOICE_HINTS.join("|");
    if (this._els.voiceHintKey === key) return;
    this._els.voiceHintKey = key;
    const doc = this._document();
    while (this._els.voiceHint.firstChild) {
      this._els.voiceHint.removeChild(this._els.voiceHint.firstChild);
    }
    for (const line of VOICE_HINTS) {
      const div = doc.createElement("div");
      div.textContent = line;
      this._els.voiceHint.appendChild(div);
    }
  }

  // -- Stepper (W5-2, Q5) ------------------------------------------------------
  //
  // "Set a time" opens a stepper for hours, minutes, seconds — tap-only,
  // no typing, so it needs no on-screen keyboard and no fine typing with
  // wet/dirty hands. Every duration from 1 s to 24:00:00 is reachable by
  // stepping alone (hours ±1, minutes/seconds ±1/±5). A zero duration
  // never starts a timer: Start with 0 s shows an inline error and
  // makes no service call. At 24 h the minutes/seconds steppers disable
  // and zero (J-W5-05): 24:00:00 is the bridge's cap, and any minute or
  // second past it would be a duration the bridge rejects.

  _buildStepper() {
    const doc = this._document();
    const stepper = this._els.stepper;
    const total = doc.createElement("div");
    total.className = "stepper-total";
    const totalTime = doc.createElement("div");
    totalTime.className = "stepper-total-time";
    const totalName = doc.createElement("div");
    totalName.className = "stepper-total-name";
    total.appendChild(totalTime);
    total.appendChild(totalName);
    stepper.appendChild(total);

    this._stepperEls = { totalTime, totalName, rows: {} };
    for (const unit of ["hours", "minutes", "seconds"]) {
      const row = doc.createElement("div");
      row.className = "stepper-row";
      row.setAttribute("data-unit", unit);
      const label = doc.createElement("span");
      label.className = "stepper-label";
      label.textContent = unit;
      row.appendChild(label);
      const value = doc.createElement("span");
      value.className = "stepper-value";
      value.setAttribute("data-role", `stepper-${unit}`);
      row.appendChild(value);
      for (const delta of [...STEPPER_STEPS[unit]].flatMap((s) => [-s, s])) {
        const button = doc.createElement("button");
        button.type = "button";
        button.className = "stepper-step";
        button.setAttribute("data-action", "step");
        button.setAttribute("data-unit", unit);
        button.setAttribute("data-delta", String(delta));
        button.setAttribute(
          "aria-label",
          `${delta > 0 ? "More" : "Less"} ${Math.abs(delta)} ${unit}`,
        );
        button.textContent = delta > 0 ? `+${delta}` : `${delta}`;
        row.appendChild(button);
      }
      stepper.appendChild(row);
      this._stepperEls.rows[unit] = { row, value };
    }

    const stepperError = doc.createElement("div");
    stepperError.className = "stepper-error";
    stepperError.setAttribute("role", "alert");
    stepperError.hidden = true;
    stepper.appendChild(stepperError);
    this._stepperEls.error = stepperError;

    const start = doc.createElement("button");
    start.type = "button";
    start.className = "stepper-start";
    start.setAttribute("data-action", "stepper-start");
    start.textContent = "Start timer";
    stepper.appendChild(start);

    const close = doc.createElement("button");
    close.type = "button";
    close.className = "stepper-close";
    close.setAttribute("data-action", "stepper-close");
    close.textContent = "Close";
    stepper.appendChild(close);
  }

  _renderStepper() {
    if (!this._els || !this._stepperEls) return;
    const open = this._stepper.open;
    this._els.stepper.hidden = !open;
    this._els.setTimeButton.setAttribute("aria-expanded", String(open));
    if (!open) return;
    const total = durationTotalSeconds(this._stepper);
    this._stepperEls.totalTime.textContent = formatRemaining(total);
    this._stepperEls.totalName.textContent = formatDurationName(total);
    const atMaxHours = this._stepper.hours >= DURATION_MAX_HOURS;
    for (const unit of ["hours", "minutes", "seconds"]) {
      const { value, row } = this._stepperEls.rows[unit];
      value.textContent = String(this._stepper[unit]);
      const disabled = atMaxHours && unit !== "hours";
      const buttons = row.querySelectorAll
        ? [...row.querySelectorAll("[data-action='step']")]
        : [];
      for (const button of buttons) {
        if ("disabled" in button) button.disabled = disabled;
      }
      row.classList.toggle("at-max", disabled);
    }
  }

  _step(unit, delta) {
    const parts = { ...this._stepper };
    parts[unit] = clampDurationPart(unit, parts[unit] + delta);
    if (parts.hours >= DURATION_MAX_HOURS) {
      // At the 24 h cap the minutes/seconds zero (J-W5-05): 24:00:00
      // is the bridge's cap, and the steppers show it, not 24:05:00.
      parts.minutes = 0;
      parts.seconds = 0;
    }
    this._stepper = { ...parts, open: this._stepper.open };
    this._stepperEls.error.hidden = true;
    this._renderStepper();
  }

  // -- Click routing -----------------------------------------------------------

  _onRootClick(event) {
    const target = event.target && event.target.closest
      ? event.target.closest("[data-action]")
      : null;
    if (!target) return;
    const action = target.getAttribute("data-action");
    if (action === "preset") {
      const minutes = Number(target.getAttribute("data-minutes"));
      if (Number.isFinite(minutes) && minutes > 0) {
        this._startPreset(minutes, target);
      }
      return;
    }
    if (action === "toggle-stepper") {
      this._stepper.open = !this._stepper.open;
      this._renderStepper();
      return;
    }
    if (action === "step") {
      this._step(
        target.getAttribute("data-unit"),
        Number(target.getAttribute("data-delta")),
      );
      return;
    }
    if (action === "stepper-start") {
      this._startStepper();
      return;
    }
    if (action === "stepper-close") {
      this._stepper.open = false;
      this._renderStepper();
      return;
    }
    if (action === "repair") {
      // Repair buttons wired directly (dial errors, gate notice, chip
      // error) stop propagation and handle themselves; a root-level
      // repair tap reveals the procedure in the chip error element.
      this._showRepairProcedure(this._els.chipError, this._errorSlot);
      return;
    }
  }

  _onDialClick(event, entityId) {
    const target = event.target && event.target.closest
      ? event.target.closest("[data-action]")
      : null;
    if (!target) {
      // The tap landed on the dial itself (ring surround, name, hint)
      // — that IS the dial tap. A tap inside the dial's error line is
      // not: its Retry / Repair buttons carry their own listeners.
      const inError =
        event.target &&
        event.target.closest &&
        event.target.closest(".dial-error");
      if (inError) return;
      event.stopPropagation();
      this._dialTap(entityId);
      return;
    }
    const action = target.getAttribute("data-action");
    if (action === "dial-retry") return; // handled by its own listener
    if (action === "repair") return; // handled by its own listener
    event.stopPropagation();
    if (action === "dial") {
      this._dialTap(entityId);
      return;
    }
    if (action === "cancel") {
      this._cancelTap(entityId);
      return;
    }
    if (action === "restart") {
      this._restartTap(entityId);
      return;
    }
    if (action === "acknowledge") {
      this._acknowledgeTap(entityId);
      return;
    }
  }

  // -- Sends (W5-2 / W5-3 / W5-4) ----------------------------------------------
  //
  // Every send goes through CardSendProtocol (M4). The callers below
  // own only the user-visible half: the transient indicator, the
  // in-place error, and the Retry that re-sends the SAME command_id.
  // Nothing here decides whether to send — no guard, no registry, no
  // "last sent" comparison (M5). A second tap while the first is
  // pending is a second gesture and a second timer.

  _makeProtocol() {
    if (!this._hass) return null;
    try {
      return new CardSendProtocol(this._hass);
    } catch (error) {
      return null;
    }
  }

  _startPreset(minutes, chipEl) {
    const protocol = this._makeProtocol();
    if (!protocol) {
      this._setErrorSlot({
        kind: "no-hass",
        scope: "chip",
        message: "Home Assistant isn't connected, so the timer can't be started.",
        repair: false,
        persistent: false,
        retry: null,
      });
      return;
    }
    const name = formatDurationName(minutes * 60);
    // The chip is NOT disabled while the send is pending (M5): a
    // second tap is a second gesture. The "Starting…" label is a
    // signal, not a block.
    const handle = protocol.send(
      "start_timer",
      { name, duration_seconds: minutes * 60, ...(this._config.device_id ? { device_id: this._config.device_id } : {}) },
      {
        label: `start ${name}`,
        onWorking: () => {
          if (chipEl) chipEl.textContent = "Starting…";
        },
        onSettled: () => {
          if (chipEl) chipEl.textContent = `${minutes} min`;
        },
      },
    );
    handle.promise.then(
      (result) => {
        this._clearErrorSlot();
        this._afterStartScroll(result?.response?.timer_id);
      },
      (error) => this._onChipError(error, name, handle),
    );
  }

  _startStepper() {
    const total = durationTotalSeconds(this._stepper);
    if (total <= 0) {
      // Zero duration never starts a timer: an inline error, no
      // service call.
      this._stepperEls.error.hidden = false;
      this._stepperEls.error.textContent =
        "Pick a time first — a timer needs at least 1 second.";
      return;
    }
    this._stepperEls.error.hidden = true;
    const protocol = this._makeProtocol();
    if (!protocol) {
      this._setErrorSlot({
        kind: "no-hass",
        scope: "chip",
        message: "Home Assistant isn't connected, so the timer can't be started.",
        repair: false,
        persistent: false,
        retry: null,
      });
      return;
    }
    const name = formatDurationName(total);
    const handle = protocol.send(
      "start_timer",
      { name, duration_seconds: total, ...(this._config.device_id ? { device_id: this._config.device_id } : {}) },
      {
        label: `start ${name}`,
        onWorking: () => {
          this._setErrorSlot({
            kind: "working",
            scope: "chip",
            message: `Starting ${name}…`,
            repair: false,
            persistent: false,
            retry: null,
            transient: true,
          });
        },
        onSettled: () => {
          if (this._errorSlot && this._errorSlot.transient) this._clearErrorSlot();
        },
      },
    );
    handle.promise.then(
      (result) => {
        this._clearErrorSlot();
        this._stepper.open = false;
        this._renderStepper();
        this._afterStartScroll(result?.response?.timer_id);
      },
      (error) => this._onChipError(error, name, handle),
    );
  }

  // After a start lands, scroll the new dial into view — after its
  // enter animation ends (A6, §03.18), never on a fixed delay. The
  // sensor arrives on the next store push, so the scroll is deferred:
  // the timer id from the start's answer is remembered, and the
  // render that first sees a record with that id scrolls it (see
  // _renderTimers). The scroll is keyed on the timer id, never the
  // display name (NB-1): the store renders the sensor's
  // `friendly_name` ("kitchen_hub 5 minute timer"), which never
  // equals the bare start name ("5 minute timer"), so a name-keyed
  // scroll silently never fired against the real bridge — and a
  // pending name lingered to scroll a later, unrelated dial that
  // happened to carry the same bare name. A timer id is unique per
  // timer (ULID, never reused), so a pending id can only ever match
  // its own timer: a stale pending id — a start whose sensor never
  // appears — scrolls nothing later, and a newer start overwrites
  // it. A start whose answer carries no id scrolls nothing; the dial
  // still renders, the scroll is a convenience, not the start.
  _afterStartScroll(timerId) {
    if (typeof timerId !== "string" || !timerId) return;
    this._pendingScrollTimerId = timerId;
  }

  // The bridge-too-old check both error handlers share: a
  // service-not-found against a bridge older than this card requires
  // means re-tapping can never succeed, so the error names the update
  // procedure instead of the failure. The version comparison runs
  // once, here — not once per handler.
  _bridgeTooOld(isNotFound) {
    if (!isNotFound || this._bridgeVersion === null) return false;
    const comparison = compareVersions(this._bridgeVersion, REQUIRED_BRIDGE_MIN_VERSION);
    return comparison !== null && comparison < 0;
  }

  _onChipError(error, timerName, handle) {
    // G-07 failed-call leg: the service call raises → no PIP, the chip
    // shows the error state, and a warning is logged WITH THE TIMER NAME.
    this._warn(
      `Couldn't start the "${timerName}" timer: ${error.message}`,
      { timerName, kind: error.kind ?? "unknown" },
    );
    const isNotFound = error.kind === "service-not-found";
    const bridgeTooOld = this._bridgeTooOld(isNotFound);
    this._setErrorSlot({
      kind: error.kind ?? "call-failed",
      scope: "chip",
      // A4 (cop-out re-implemented): re-tapping can never succeed
      // while the bridge is old, so the error names the procedure —
      // never voice as a fallback path.
      message: bridgeTooOld
        ? "The timer controls need the updated bridge. Update Satellite Timers in HACS, restart Home Assistant, then reload this page."
        : isNotFound
          ? `Couldn't start the "${timerName}" timer — the timer service wasn't found.`
          : `Couldn't start the "${timerName}" timer — ${error.message}`,
      repair: isNotFound,
      persistent: isNotFound,
      retry: {
        label: "Retry",
        run: () => {
          // Retry re-sends the SAME command_id (M4 step 5).
          const retryHandle = handle.retry();
          retryHandle.promise.then(
            (result) => {
              this._clearErrorSlot();
              this._afterStartScroll(result?.response?.timer_id);
            },
            (retryError) => this._onChipError(retryError, timerName, retryHandle),
          );
        },
      },
    });
  }

  // -- Dial actions (W5-4) -----------------------------------------------------
  //
  // Dial tap is the primary action: pause (active), resume (paused),
  // acknowledge (ringing), dismiss (finished). Cancel arms before it
  // cancels. Every action goes through CardSendProtocol; every failure
  // shows the honest in-place error with a Retry button that re-sends
  // the SAME command_id. The 6 s `setTimeout` auto-clear with no
  // re-attempt is deleted; voice is never offered as a path
  // (cop-out A3/A4/A6 re-implemented per the plan).

  _dialTap(entityId) {
    const record = this._snapshot.find((r) => r.entityId === entityId);
    if (!record) return;
    if (record.state === "active") return this._sendDialAction(record, "pause_timer", "Pausing…");
    if (record.state === "paused") return this._sendDialAction(record, "resume_timer", "Resuming…");
    if (record.state === "ringing") return this._acknowledgeTap(entityId);
    if (record.state === "finished") return this._dismissTap(entityId);
  }

  _cancelTap(entityId) {
    const record = this._snapshot.find((r) => r.entityId === entityId);
    if (!record) return;
    if (!this._cancelArmed.has(entityId)) {
      // First tap arms. The arm expires (J-W5-07); re-render to show
      // the armed label.
      this._cancelArmed.set(
        entityId,
        setTimeout(() => {
          this._cancelArmed.delete(entityId);
          // Re-render from the fresh snapshot (the timer may have
          // changed state while armed); the render rebuilds the
          // actions with the arm cleared.
          this._render();
        }, CANCEL_ARM_MS),
      );
      const built = this._dialEls.get(entityId);
      if (built) {
        built.actionsFor = null;
        this._renderDialActions(built, record);
      }
      return;
    }
    const arm = this._cancelArmed.get(entityId);
    clearTimeout(arm);
    this._cancelArmed.delete(entityId);
    this._sendDialAction(record, "cancel_timer", "Cancelling…", {
      onSuccess: () => this._recordUndo(record, "cancelled"),
    });
  }

  _restartTap(entityId) {
    const record = this._snapshot.find((r) => r.entityId === entityId);
    if (!record) return;
    this._sendDialAction(record, "restart_timer", "Restarting…");
  }

  _acknowledgeTap(entityId) {
    const record = this._snapshot.find((r) => r.entityId === entityId);
    if (!record) return;
    this._sendDialAction(record, "acknowledge_timer", "Stopping…");
  }

  _dismissTap(entityId) {
    const record = this._snapshot.find((r) => r.entityId === entityId);
    if (!record) return;
    this._sendDialAction(record, "dismiss_timer", "Dismissing…", {
      onSuccess: () => this._recordUndo(record, "dismissed"),
    });
  }

  _sendDialAction(record, service, workingLabel, { onSuccess = null } = {}) {
    const protocol = this._makeProtocol();
    if (!protocol) {
      this._setErrorSlot({
        kind: "no-hass",
        scope: `dial:${record.entityId}`,
        message: "Home Assistant isn't connected, so that couldn't be done.",
        repair: false,
        persistent: false,
        retry: null,
      });
      return;
    }
    const handle = protocol.send(
      service,
      { timer_id: record.timerId },
      {
        label: `${service} ${record.name}`,
        onWorking: () => {
          this._setErrorSlot({
            kind: "working",
            scope: `dial:${record.entityId}`,
            message: `${workingLabel} ${record.name}…`,
            repair: false,
            persistent: false,
            retry: null,
            transient: true,
          });
        },
        onSettled: () => {
          if (this._errorSlot && this._errorSlot.transient) this._clearErrorSlot();
        },
      },
    );
    handle.promise.then(
      () => {
        this._clearErrorSlot();
        if (typeof onSuccess === "function") onSuccess();
      },
      (error) => this._onDialError(error, record, service, handle),
    );
  }

  _onDialError(error, record, service, handle) {
    this._warn(
      `Couldn't ${service} the "${record.name}" timer: ${error.message}`,
      { timerName: record.name, timerId: record.timerId, kind: error.kind ?? "unknown" },
    );
    const isNotFound = error.kind === "service-not-found";
    const bridgeTooOld = this._bridgeTooOld(isNotFound);
    // ERS-1 ladder, named per path (§07.29 AD-1): retry inside the
    // operation (M4 re-send + user Retry, same command_id); re-probe
    // outside it (60 s availability check). See ERS_BOUNDARY.
    this._setErrorSlot({
      kind: error.kind ?? "call-failed",
      scope: `dial:${record.entityId}`,
      message: bridgeTooOld
        ? "Timer controls need the updated bridge. Update Satellite Timers in HACS, restart Home Assistant, then reload this page."
        : isNotFound
          ? `Couldn't do that — the timer service wasn't found. (${ERS_BOUNDARY})`
          : `Couldn't do that — ${error.message}`,
      repair: isNotFound,
      persistent: isNotFound,
      retry: {
        label: "Retry",
        run: () => {
          const retryHandle = handle.retry();
          retryHandle.promise.then(
            () => {
              this._clearErrorSlot();
            },
            (retryError) => this._onDialError(retryError, record, service, retryHandle),
          );
        },
      },
    });
  }

  // -- Undo (W5-4, §03.7) -------------------------------------------------------
  //
  // Undo sends `undo_remove`; the bridge restores the exact snapshot
  // (same timer, same state, same remaining / finished_at, within 5 s)
  // and the card renders the sensor that reappears. Undo failure
  // retries and re-offers Undo (A6); the error is visible whenever any
  // timer exists — the undo bar is outside the dials list, never in a
  // container hidden by timer presence.

  _recordUndo(record, verb) {
    const doc = this._document();
    const existing = this._undoBars.get(record.timerId);
    if (existing) {
      clearTimeout(existing.hideTimer);
      if (existing.root && typeof existing.root.remove === "function") {
        existing.root.remove();
      }
    }
    const root = doc.createElement("div");
    root.className = "undo-bar";
    root.setAttribute("role", "status");
    const text = doc.createElement("span");
    text.textContent = `"${record.name}" ${verb} — `;
    const undoButton = doc.createElement("button");
    undoButton.type = "button";
    undoButton.setAttribute("data-action", "undo");
    undoButton.textContent = "Undo";
    root.appendChild(text);
    root.appendChild(undoButton);
    this._els.undoWrap.appendChild(root);
    this._els.undoWrap.hidden = false;
    const bar = {
      root,
      timerId: record.timerId,
      name: record.name,
      hideTimer: setTimeout(() => {
        this._removeUndoBar(record.timerId);
      }, UNDO_WINDOW_MS),
    };
    this._undoBars.set(record.timerId, bar);
    undoButton.addEventListener("click", () => this._undoTap(record.timerId));
  }

  _removeUndoBar(timerId) {
    const bar = this._undoBars.get(timerId);
    if (!bar) return;
    clearTimeout(bar.hideTimer);
    if (bar.root && typeof bar.root.remove === "function") bar.root.remove();
    this._undoBars.delete(timerId);
    if (this._els) this._els.undoWrap.hidden = this._undoBars.size === 0;
  }

  _undoTap(timerId) {
    const bar = this._undoBars.get(timerId);
    const protocol = this._makeProtocol();
    if (!protocol) {
      this._warn(`Couldn't bring back the "${bar ? bar.name : timerId}" timer: Home Assistant isn't connected.`, { timerId });
      return;
    }
    const handle = protocol.send("undo_remove", { timer_id: timerId }, {
      label: `undo ${timerId}`,
    });
    handle.promise.then(
      () => {
        this._removeUndoBar(timerId);
        this._clearErrorSlot();
      },
      (error) => {
        // A6 re-implemented: undo failure retries (same command_id)
        // and re-offers Undo — voice is never the recovery path.
        this._warn(
          `Couldn't bring back the "${bar ? bar.name : timerId}" timer: ${error.message}`,
          { timerId, kind: error.kind ?? "unknown" },
        );
        this._setErrorSlot({
          kind: error.kind ?? "call-failed",
          scope: "undo",
          message: `Couldn't bring the timer back — ${error.message}`,
          repair: error.kind === "service-not-found",
          persistent: error.kind === "service-not-found",
          retry: {
            label: "Retry undo",
            run: () => {
              const retryHandle = handle.retry();
              retryHandle.promise.then(
                () => {
                  this._removeUndoBar(timerId);
                  this._clearErrorSlot();
                },
                (retryError) => {
                  this._warn(
                    `Couldn't bring back the "${bar ? bar.name : timerId}" timer: ${retryError.message}`,
                    { timerId, kind: retryError.kind ?? "unknown" },
                  );
                },
              );
            },
          },
        });
        // The Undo bar stays: the undo is still offered (the 5 s
        // bridge window is enforced by the bridge, not by hiding the
        // button — a tap after expiry fails honestly at the bridge).
        this._renderUndoError();
      },
    );
  }

  _renderUndoError() {
    // The undo-scope error renders in the chip-row error element:
    // always visible, never inside a container hidden when timers
    // exist (the A5/A6 hidden-element defect, fixed by construction).
    this._renderChipError();
  }

  // -- Error slot (W5-4, ERS-5, §08 row 41) --------------------------------------
  //
  // Exactly one error slot for the card, in memory, newer-wins,
  // success-clears; a reload clears it; a persistent failure re-appears
  // via the 60 s re-probe. The slot holds the record; the rendering
  // site follows the operation that failed (J-W5-12): chip-scope in
  // the chip-row error element, dial-scope on the dial, undo-scope in
  // the chip-row error element, store-scope in the store error element.

  _setErrorSlot(record) {
    const slot = {
      id: `${Date.now()}-${Math.floor(Math.random() * 1e9)}`,
      kind: record.kind,
      scope: record.scope,
      message: record.message,
      repair: Boolean(record.repair),
      persistent: Boolean(record.persistent),
      retry: record.retry ?? null,
      transient: Boolean(record.transient),
    };
    this._errorSlot = slot;
    // The re-probe is armed only while a persistent-class error is
    // held. A newer non-persistent slot (a transient "Starting…" or a
    // plain failure) replaces the persistent one (newer wins) and the
    // probe disarms; a later failure re-arms it if it is persistent.
    if (slot.persistent) {
      this._armReProbe();
    } else {
      this._stopReProbe();
    }
    this._renderChipError();
    this._renderDialErrors();
  }

  _clearErrorSlot() {
    if (!this._errorSlot) return;
    this._errorSlot = null;
    this._stopReProbe();
    this._renderChipError();
    this._renderDialErrors();
  }

  _renderDialErrors() {
    for (const [entityId, built] of this._dialEls) {
      const record = this._snapshot.find((r) => r.entityId === entityId);
      if (record) {
        built.errorFor = null;
        this._renderDialError(built, record);
      }
    }
  }

  _renderChipError() {
    if (!this._els) return;
    const slot = this._errorSlot;
    const show =
      slot &&
      !slot.transient &&
      (slot.scope === "chip" || slot.scope === "undo" || slot.scope === "store");
    if (slot && slot.transient && slot.scope === "chip") {
      // A transient "Starting…" / "Working…" line renders in the same
      // element (in place on the chip row), never as an optimistic dial.
      this._els.chipError.hidden = false;
      const key = `transient:${slot.message}`;
      if (this._els.chipErrorKey === key) return;
      this._els.chipErrorKey = key;
      const doc = this._document();
      while (this._els.chipError.firstChild) {
        this._els.chipError.removeChild(this._els.chipError.firstChild);
      }
      const text = doc.createElement("span");
      text.textContent = slot.message;
      this._els.chipError.appendChild(text);
      return;
    }
    if (!show) {
      this._els.chipError.hidden = true;
      this._els.chipErrorKey = null;
      return;
    }
    const key = slot.id;
    if (this._els.chipErrorKey === key) return;
    this._els.chipErrorKey = key;
    this._els.chipError.hidden = false;
    const doc = this._document();
    while (this._els.chipError.firstChild) {
      this._els.chipError.removeChild(this._els.chipError.firstChild);
    }
    const text = doc.createElement("span");
    text.textContent = slot.message;
    this._els.chipError.appendChild(text);
    if (slot.retry) {
      const retry = doc.createElement("button");
      retry.type = "button";
      retry.setAttribute("data-action", "chip-retry");
      retry.textContent = slot.retry.label || "Retry";
      retry.addEventListener("click", (event) => {
        event.stopPropagation();
        slot.retry.run();
      });
      this._els.chipError.appendChild(retry);
    }
    if (slot.repair) {
      const repair = doc.createElement("button");
      repair.type = "button";
      repair.setAttribute("data-action", "repair");
      repair.textContent = "Repair";
      repair.addEventListener("click", (event) => {
        event.stopPropagation();
        this._showRepairProcedure(this._els.chipError, slot);
      });
      this._els.chipError.appendChild(repair);
    }
  }

  // -- Repair (W1-4) --------------------------------------------------------------
  //
  // The Repair action names the procedure in place — the card cannot
  // run the wizard or update HACS itself, so the honest recovery path
  // is the exact steps, shown where the error is. The procedure text
  // depends on the failure: an old bridge names the HACS update (A4),
  // a missing bridge names the load check, a version skew names the
  // wizard re-run.

  _showRepairProcedure(container, slot) {
    if (!container) return;
    const doc = this._document();
    const existing = container.querySelector
      ? container.querySelector(".repair-procedure")
      : null;
    if (existing) {
      existing.hidden = !existing.hidden;
      return;
    }
    const procedure = doc.createElement("p");
    procedure.className = "repair-procedure";
    if (slot && slot.kind === "service-not-found") {
      procedure.textContent =
        "Repair: make sure Satellite Timers is installed and loaded " +
        "(Settings → Devices & Services → Satellite Timers). If it is " +
        "missing, install it from HACS, restart Home Assistant, then " +
        "reload this page.";
    } else if (slot && slot.kind === "gate") {
      procedure.textContent =
        "Repair: on the Pi 5, in its own terminal, run " +
        "python3 ~/family-hub/setup_wizard.py --resume, then reload this page.";
    } else {
      procedure.textContent =
        "Repair: reload this page. If the problem stays, restart Home " +
        "Assistant, then reload this page.";
    }
    container.appendChild(procedure);
  }

  // -- 60 s re-probe (W5-4, ERS-10) ------------------------------------------------
  //
  // THIS RE-PROBE EXISTS IN CODE — a named re-probe with no code behind
  // it fails the task. One interval, 60 s, armed only while a
  // persistent-class error is held (service-not-found, store-failed).
  // Each tick re-checks bridge availability (`get_services` — one
  // websocket call, no user command re-issued). On recovery the error
  // clears SILENTLY (success-clears, no toast, no new render noise)
  // and the interval disarms. On persistence the error stays rendered
  // (it "re-appears" across reloads because the mount-time check below
  // raises it again and this probe keeps it current). The bound is the
  // arming condition: the probe runs only while the card is mounted
  // AND a persistent error is held — it never runs unbounded in a
  // healthy card, and it never re-issues a user command.

  _armReProbe() {
    if (this._reProbeTimer !== null) return;
    this._reProbeTimer = setInterval(() => {
      void this._runReProbe();
    }, RE_PROBE_INTERVAL_MS);
  }

  _stopReProbe() {
    if (this._reProbeTimer === null) return;
    clearInterval(this._reProbeTimer);
    this._reProbeTimer = null;
  }

  async _runReProbe() {
    if (!this._errorSlot || !this._errorSlot.persistent) {
      this._stopReProbe();
      return;
    }
    const available = await this._probeBridgeAvailability();
    if (available) {
      // Recovery is silent: the slot clears, the interval disarms,
      // the next render simply stops showing the error.
      this._clearErrorSlot();
      return;
    }
    // Still failing: the error stays rendered. The slot record is
    // refreshed (same id is kept — the error is the same failure,
    // re-surfaced, not a new one) so the render does not flicker.
    this._renderChipError();
    this._renderDialErrors();
  }

  // The availability probe: does the `satellite_timers` domain exist
  // in the service list? One `get_services` call; a failure to read
  // the list counts as unavailable (the connection is down, which is
  // the same persistent class for the card's purposes).
  async _probeBridgeAvailability() {
    try {
      const hass = this._hass;
      if (!hass || typeof hass.callWS !== "function") return false;
      const services = await hass.callWS({ type: "get_services" });
      if (!services || typeof services !== "object") return false;
      return Object.prototype.hasOwnProperty.call(services, BRIDGE_DOMAIN);
    } catch (_err) {
      return false;
    }
  }
}

// -- Styles (W5-1) --------------------------------------------------------------
//
// Warm palette (TIMER-5): running ring #C2410C, soon/finished ring
// #B45309, track #757575, error #B71C1C. No exact-size checks anywhere:
// the grid is `auto-fill` over the card's own width (a container band,
// not a screen signature), dials are horizontal flex that wraps, and
// there is no media query in this file. The entry row is sticky to the
// safe-area bottom (D2). Countdown inside dials follows the U19
// meanwhile rule (`clamp(40px, 11vw, 72px)`, never below 40 px);
// the stepper total — time OUTSIDE a dial — is 72 px.
//
// Colours and sizes below are the design tokens (DESIGN-1,
// design-tokens.json at the repo root): the card inlines the token
// values it consumes, and `design-tokens.test.cjs` Part B pins them.
// Ring colours live in JS (`_ringColor`) instead of CSS because the
// ring's geometry (dashoffset) is computed in the same place — Part B
// asserts those token values against this file's source.

const CARD_CSS = `
:host { display: block; }
.screen {
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  color: #1C1917;
  background: #FAF7F4;
  min-height: 100%;
  padding: 16px 16px calc(16px + env(safe-area-inset-bottom, 0px));
  padding-left: calc(16px + env(safe-area-inset-left, 0px));
  padding-right: calc(16px + env(safe-area-inset-right, 0px));
  box-sizing: border-box;
}
.title { font-size: 28px; line-height: 1.2; margin: 0 0 12px; }
.back-bar { grid-column: 1 / -1; margin-bottom: 8px; }
.back-button {
  background: none; border: none; color: #0E7490; font-size: 18px;
  padding: 12px 8px; cursor: pointer; min-height: 48px;
}
.gate-notice, .store-error {
  border: 2px solid #B45309; border-radius: 12px; background: #FFFBEB;
  padding: 12px 14px; margin: 0 0 16px; font-size: 18px; line-height: 1.4;
}
.gate-notice strong, .store-error strong { display: block; margin-bottom: 4px; }
.gate-notice p, .store-error p { margin: 4px 0 8px; }
.gate-notice button, .store-error button {
  min-height: 48px; padding: 10px 18px; font-size: 18px; cursor: pointer;
  border-radius: 10px; border: 2px solid #92400E; background: #FFF7ED; color: #92400E;
}
.loading, .empty {
  font-size: 20px; line-height: 1.4; color: #57534E;
  padding: 24px 8px; text-align: center;
}
/* Container-band grid (W5-1): the band is the card's own width.
   No wall-specific layout code lives here (W5-6 gated). */
.dials {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
  gap: 16px;
  align-items: start;
}
.dial {
  display: flex; flex-wrap: wrap; align-items: center; gap: 12px;
  background: #FFFFFF; border: 1px solid #EDE8E3; border-radius: 16px;
  padding: 12px;
  animation: dial-enter ${DIAL_ENTER_ANIMATION_MS}ms ease-out;
}
@keyframes dial-enter { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: translateY(0); } }
.dial.pinned { outline: 3px solid #0E7490; outline-offset: 2px; }
/* A14 horizontal dial: ring + body + actions in one row. */
.dial-ring {
  position: relative; flex: 0 0 auto; width: ${DIAL_RING_PX}px; height: ${DIAL_RING_PX}px;
  padding: 0; border: none; background: none; cursor: pointer;
}
.dial-ring-svg { width: ${DIAL_RING_PX}px; height: ${DIAL_RING_PX}px; transform: rotate(-90deg); display: block; }
.dial-ring-track { fill: none; stroke: #757575; stroke-width: 10; opacity: 0.35; }
.dial-ring-arc { fill: none; stroke: #C2410C; stroke-width: 10; stroke-linecap: round; }
.dial-time {
  position: absolute; inset: 0; display: flex; align-items: center; justify-content: center;
  font-size: clamp(40px, 11vw, 72px); font-weight: 600; line-height: 1;
  color: #1C1917; pointer-events: none; text-align: center;
  /* A countdown is one line: without this it wraps inside the ring,
  and a wrapped countdown is itself corruption — two lines of digits
  in a ring sized for one. */
  white-space: nowrap;
}
.dial-time.stale { opacity: 0.55; }
.dial-body { flex: 1 1 160px; min-width: 0; }
.dial-name {
  font-size: 20px; font-weight: 600; line-height: 1.25;
  overflow-wrap: anywhere; word-break: break-word;
}
.dial-hint { font-size: 18px; line-height: 1.4; color: #57534E; margin-top: 4px; }
.dial-hint.stale { color: #B45309; }
.dial[data-state="ringing"] .dial-hint { color: #B71C1C; font-weight: 600; }
.dial[data-state="finished"] .dial-hint { color: #57534E; }
.dial-error {
  margin-top: 8px; font-size: 18px; line-height: 1.4; color: #B71C1C;
}
.dial-error button, .chip-error button {
  min-height: 48px; padding: 8px 16px; font-size: 18px; margin: 6px 8px 0 0;
  cursor: pointer; border-radius: 10px; border: 2px solid #B71C1C;
  background: #FEF2F2; color: #B71C1C;
}
.dial-actions { flex: 0 0 auto; display: flex; flex-direction: column; gap: 8px; }
.dial-cancel, .dial-restart, .dial-stop {
  min-width: 80px; min-height: 80px; padding: 12px 16px; font-size: 18px;
  cursor: pointer; border-radius: 12px; border: 2px solid #78716C;
  background: #FFFFFF; color: #1C1917;
}
.dial-cancel.armed { border-color: #B71C1C; color: #B71C1C; background: #FEF2F2; }
.dial-restart { border-color: #0E7490; color: #0E7490; }
.dial-stop { border-color: #B71C1C; color: #FFFFFF; background: #B71C1C; font-weight: 700; }
.undo-wrap { margin: 12px 0; display: flex; flex-direction: column; gap: 8px; }
.undo-bar {
  display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
  background: #EFF6FF; border: 2px solid #0E7490; border-radius: 12px;
  padding: 12px 14px; font-size: 18px; line-height: 1.4;
}
.undo-bar button {
  min-height: 48px; padding: 8px 18px; font-size: 18px; cursor: pointer;
  border-radius: 10px; border: 2px solid #0E7490; background: #FFFFFF; color: #0E7490;
}
/* Entry row (D2): anchored to the safe-area bottom. */
.entry-row {
  position: sticky; bottom: 0; z-index: 10;
  background: #FAF7F4;
  padding: 12px 0 calc(4px + env(safe-area-inset-bottom, 0px));
  margin-top: 16px;
  border-top: 1px solid #EDE8E3;
}
.chip-row { display: flex; flex-wrap: wrap; gap: 12px; justify-content: center; }
.preset-chip, .set-time-button {
  min-height: 56px; padding: 12px 22px; font-size: 20px; cursor: pointer;
  border-radius: 999px; border: 2px solid #C2410C; background: #FFFFFF; color: #C2410C;
}
.set-time-button { border-color: #0E7490; color: #0E7490; }
.stepper {
  margin-top: 12px; background: #FFFFFF; border: 1px solid #EDE8E3;
  border-radius: 16px; padding: 16px;
}
.stepper-total-time { font-size: 72px; font-weight: 600; line-height: 1; text-align: center; }
.stepper-total-name { font-size: 18px; color: #57534E; text-align: center; margin-top: 4px; }
.stepper-row {
  display: flex; align-items: center; gap: 10px; margin-top: 12px;
  justify-content: center; flex-wrap: wrap;
}
.stepper-label { font-size: 18px; min-width: 76px; text-transform: capitalize; }
.stepper-value { font-size: 28px; font-weight: 600; min-width: 48px; text-align: center; }
.stepper-step {
  min-width: 56px; min-height: 56px; font-size: 22px; cursor: pointer;
  border-radius: 12px; border: 2px solid #78716C; background: #FFFFFF;
}
.stepper-step:disabled { opacity: 0.4; cursor: default; }
.stepper-error { color: #B71C1C; font-size: 18px; line-height: 1.4; margin-top: 12px; text-align: center; }
.stepper-start, .stepper-close {
  min-height: 56px; padding: 12px 24px; font-size: 20px; cursor: pointer;
  border-radius: 12px; margin: 16px 8px 0 0;
}
.stepper-start { border: 2px solid #C2410C; background: #C2410C; color: #FFFFFF; }
.stepper-close { border: 2px solid #78716C; background: #FFFFFF; }
.chip-error {
  margin-top: 12px; font-size: 18px; line-height: 1.4; color: #B71C1C;
  text-align: center;
}
.voice-hint { margin-top: 12px; font-size: 18px; line-height: 1.4; color: #57534E; text-align: center; }
.repair-procedure {
  margin-top: 8px; font-size: 18px; line-height: 1.4; color: #1C1917;
  background: #FFFFFF; border: 1px solid #EDE8E3; border-radius: 8px; padding: 10px 12px;
  text-align: left;
}
`;

// -- Registration --------------------------------------------------------------

if (typeof customElements !== "undefined" && customElements.define) {
  // Skip re-definition when the element is already registered (a
  // double load in one page throws). `get` is only called when the
  // host provides it — the repo's design-tokens suite stubs
  // customElements with `define` alone, and the card must still load
  // under that stub (D-W5-03).
  const alreadyDefined =
    typeof customElements.get === "function" &&
    customElements.get("timer-screen");
  if (!alreadyDefined) {
    customElements.define("timer-screen", TimerScreenCard);
  }
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    TimerScreenCard,
    CardSendProtocol,
    SendError,
    CARD_VERSION,
    REQUIRED_BRIDGE_MIN_VERSION,
    BRIDGE_DOMAIN,
    ACK_TIMEOUT_MS,
    RE_PROBE_INTERVAL_MS,
    DISPLAY_TICK_MS,
    UNDO_WINDOW_MS,
    CANCEL_ARM_MS,
    STALE_AFTER_MS,
    SOON_THRESHOLD_SECONDS,
    AFTER_ANIMATION_TIMEOUT_MS,
    DIAL_RING_PX,
    DIAL_RING_ARC_RADIUS_PX,
    DIAL_TIME_FIT_MIN_PX,
    DIAL_ENTER_ANIMATION_MS,
    CARD_SIZE_ROWS,
    COUNTDOWN_IN_DIAL_CSS,
    COUNTDOWN_OUTSIDE_DIAL_PX,
    PRESET_MINUTES,
    STEPPER_STEPS,
    DURATION_MAX_HOURS,
    DURATION_FIELD_MAX,
    TIMERS_PIN_KEY,
    TIMERS_ARRIVAL_KEY,
    TIMER_SCROLL_KEY,
    ERS_BOUNDARY,
    newCommandId,
    formatRemaining,
    fitFontSizeToWidth,
    formatMinutesAgo,
    clampDurationPart,
    parseDurationParts,
    durationTotalSeconds,
    formatDurationName,
    isServiceNotFound,
    parseVersion,
    compareVersions,
    evaluateBridgeGate,
    gateNoticeCopy,
    missedAlarmReading,
    finishedReading,
    readTimersPin,
    readTimersArrival,
    parseTimerScroll,
    serializeTimerScroll,
    afterAnimation,
    CARD_CSS,
  };
}
