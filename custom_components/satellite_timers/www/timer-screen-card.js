// Timer screen card (custom:timer-screen, bundled in the Satellite
// Timers bridge). One dial per timer, tap the dial for the primary
// action (pause / resume / dismiss), Cancel arms before it cancels.
//
// Phase 3 (consolidated spec rev 2, TIMER-1..15) changes, by spec ID:
//   TIMER-1  Full timer name, wrapped to at most two lines, never
//            shrunk below 20px on the hub. The name is HTML text in
//            its own block ABOVE the ring (Wave 1): the old overlay
//            centred text on the ring and clipped sub-labels at the
//            circle's bottom edge (NAV-O2). The name block is the
//            full cell width; the ring holds only the countdown.
//   TIMER-2  Hub identity comes from family-hub-core.js (§4, R1):
//            the registered Browser Mod browser id — never a screen
//            size, never the ?kiosk parameter. The old per-card rule
//            (exact-size panel signature first, then user agents,
//            then width/pointer bands) is DELETED with the per-card
//            copies; the viewport + pointer mapping now lives in
//            core's deviceFromIdentity() (one copy). Bare width
//            >=1600 + landscape -> hub is FORBIDDEN. (Live repro is
//            a §13 item: headless cannot present touch + fine pointer
//            simultaneously — see pointer_diag.mjs.)
//   TIMER-3  Finished timers show "Done — N min ago", counting up
//            from finished_at on the tick (same as HOME-3).
//   TIMER-4  Viewport-split creation: the hub is voice-only + a hint;
//            phone/tablet get on-screen preset chips (5/10/15/30 min)
//            that call satellite_timers.start_timer. Wave 1: the chips
//            live in the "+ Add timer" row (shown directly with no
//            timers, toggled while timers run); the wall shows the
//            same chips only when voice is provably unavailable (D4
//            fallback, T-IMPL-12).
//   TIMER-5  Warm palette, all passing the §3.3 bars: running ring
//            #C2410C (4.96:1), soon/finished ring #B45309 (4.81:1),
//            track #757575 (4.41:1), error #B71C1C (6.29:1).
//   TIMER-6  Dial text minima (hub): name >=20px, state/hint >=18px,
//            time 72px. The time is centred INSIDE the ring (Wave 1)
//            and steps down (72→64→56→48→44→40) when it is wider
//            than the ring minus 16px (_fitTimes) — never below 40px,
//            because the ring is decorative and the text is the
//            content.
//   TIMER-7  NO cap: the view scrolls, a floating "↓" pill shows
//            while a dial sits below the fold, and finished timers
//            live in a collapsible "Finished (N)" section as compact
//            74px rows — never as dials (Wave 1).
//   TIMER-9  Dial taps (and Restart) clear a pending Cancel arm.
//   TIMER-10 "Back to recipe" bar when the jump came from a recipe:
//            the timer-jump automation navigates to
//            /family-hub/timers?from_recipe=<recipe_id>&step=<n> and
//            this card renders the return control; tapping it goes
//            back to /family-hub/recipes?recipe=<id>&step=<n>.
//            (Jump-once-per-start, the 5 s manual-nav override and the
//            screensaver pre-view save are automation-side — Phase 6.)
//   TIMER-11 Duplicates render distinguishably: full name + start
//            time ("Pasta — started 6:02 PM"), from the sensor's
//            started_at attribute (the bridge owns it; the card falls
//            back to deriving it from finishes_at / finished_at).
//   TIMER-12 A finished timer's state persists until dismissed (the
//            bridge owns this; the card renders the finished state
//            whenever it next looks). Missed-timer announcement is
//            bridge-side (replay on satellite reconnect).
//   TIMER-13 Cancelling shows an Undo toast for 5 s; Undo re-creates
//            the timer via the bridge restore_timer service with the
//            same name and the REMAINING time at cancel (min 1 s) —
//            not the full duration (F-T1) — and restores a paused
//            timer paused (D6). Dismissing a finished timer offers
//            the same Undo (restore_finished_timer, no re-ring).
//            The toast is position: fixed with its bottom set from
//            the measured strip height, so it never hides behind the
//            strip (F-T17).
//   TIMER-14 "A few more minutes" is voice-only (add_time): the empty
//            state names that path. There is NO on-screen +1 min
//            button (deliberate user choice).
//   TIMER-15 Offline timers are protected by the built-in intent
//            path (no LLM needed); verified in the rig via
//            conversation/process with no LLM configured.
//
// Design tokens (DESIGN-1): the values below are INLINED from
// design-tokens.json at the repo root (human-readable twin:
// design-tokens.css) — the card renders in shadow DOM and cannot
// @import the stylesheet at runtime, so the JSON is the source of
// truth and this file is its copy, enforced by
// www/design-tokens.test.cjs (change a value in the JSON and the card
// changes in the same commit, or the test fails). The vendored JSON
// is a copy of pi-family-hub's dashboard/design-tokens.json —
// refresh both files together when the tokens change upstream.
// Mapping: timer palette = color-timer-*, error = color-error /
// color-danger, muted = color-muted, dark surfaces = color-ink,
// borders = color-line, type = font-dial-*, targets, radii, spacing,
// shadows and motion = their token names. Two deliberate splits:
// body text stays HA's --primary-text-color (#212121), not the ink
// token, and the screen stays transparent (no screen-bg token) —
// both are HA-theme behaviour, recorded in the parity test.

const RING_CIRCUMFERENCE = 2 * Math.PI * 88;

// A timer is "finishing soon" in its last 59 seconds: the ring turns
// warm and pulses gently so it catches the eye without shouting.
// 59, not 60 (Wave 1, OQ-5): the design flips "soon" at 59 s and the
// flip follows the SENSOR's remaining attribute (sensorRemaining),
// not the displayed countdown — the two can differ by a second, and
// the sensor is the source of truth. The strip badge amber stays at
// <=60 s (coordinator decision #25-D, literal) — a different rule
// for a different surface.
const FINISHING_SOON_SECONDS = 59;
// How long the Cancel button stays armed ("Tap again to cancel") after the
// first tap, before it disarms itself.
const CANCEL_ARM_MS = 4000;
// How long the cancel/dismiss Undo toast stays up (TIMER-13).
const UNDO_TOAST_MS = 5000;
// Entrance animation length for a newly added dial.
const ENTER_ANIMATION_MS = 240;
// Time-fit steps (T-IMPL-7): when the rendered countdown is wider
// than the ring minus 16px, the font steps down this ladder to the
// 40px floor (TIMER-6) — never below it.
const TIME_FIT_STEPS = [72, 64, 56, 48, 44, 40];
// Preset chip durations, minutes (TIMER-4 / D4 fallback).
const PRESET_MINUTES = [5, 10, 15, 30];
// The PIP pin (written by the recipe card, read once per mount) and
// the Home arrival flag (written by the Home card, read once per
// mount) — sessionStorage keys (D7.3 / T-IMPL-20/23).
const TIMERS_PIN_KEY = "family-hub:timers:pin";
const TIMERS_ARRIVAL_KEY = "family-hub:timers:arrival";

function displayName(stateObj) {
  const attrs = (stateObj && stateObj.attributes) || {};
  const name = attrs.timer_name || attrs.friendly_name;
  if (name && String(name).trim()) return String(name).trim();
  return "Timer";
}

function formatRemaining(totalSeconds) {
  const total = Math.max(0, Math.floor(totalSeconds || 0));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  const mm = hours > 0 ? String(minutes).padStart(2, "0") : String(minutes);
  const ss = String(seconds).padStart(2, "0");
  return hours > 0 ? `${hours}:${mm}:${ss}` : `${mm}:${ss}`;
}

// Elapsed-since-finish for a finished timer (TIMER-3 / HOME-3).
// "Done — just now" under a minute, "Done — N min ago" under an hour,
// "Done — H h M min ago" past that. `finishedAtMs` is an epoch-ms time.
function formatElapsedSince(finishedAtMs, nowMs) {
  if (!finishedAtMs) return "Done";
  const mins = Math.max(0, Math.floor((nowMs - finishedAtMs) / 60000));
  if (mins < 1) return "Done — just now";
  if (mins < 60) return `Done — ${mins} min ago`;
  const hours = Math.floor(mins / 60);
  const rest = mins % 60;
  return rest === 0
    ? `Done — ${hours} h ago`
    : `Done — ${hours} h ${rest} min ago`;
}

// "6:02 PM" for a start time (TIMER-11 duplicate disambiguation).
function formatStartTime(startedAtMs) {
  if (!startedAtMs) return "";
  return new Date(startedAtMs).toLocaleTimeString("en-US", {
    hour: "numeric",
    minute: "2-digit",
  });
}

function remainingSeconds(timer, nowMs) {
  if (!timer) return 0;
  if (timer.state === "paused") return Math.max(0, timer.remaining || 0);
  if (timer.finishesAt) {
    return Math.max(0, Math.round((Date.parse(timer.finishesAt) - nowMs) / 1000));
  }
  return Math.max(0, timer.remaining || 0);
}

// The sensor's own remaining attribute (T-IMPL-13 / OQ-5): the "soon"
// flip and the strip badge read this, not the displayed countdown
// derived from finishes_at — the two can differ by a second.
function sensorRemaining(timer) {
  if (!timer) return 0;
  return Math.max(0, Number(timer.remaining) || 0);
}

function progressFraction(timer, nowMs) {
  if (!timer || !timer.duration || timer.duration <= 0) return 0;
  return Math.min(1, Math.max(0, remainingSeconds(timer, nowMs) / timer.duration));
}

// When a timer started, in epoch ms. Prefers the sensor's started_at
// attribute (the bridge owns it — exact even for timers that were
// already paused when the card loaded). Falls back to deriving it:
// active from finishes_at - duration, finished from finished_at -
// duration, paused from now - elapsed (best effort; the card caches
// the value the first time it sees the timer, see _startedAtMs).
function startedAtMs(timer, nowMs, cachedMs) {
  if (!timer) return null;
  if (timer.startedAt) {
    const parsed = Date.parse(timer.startedAt);
    if (!Number.isNaN(parsed)) return parsed;
  }
  if (cachedMs) return cachedMs;
  if (!timer.duration) return null;
  if (timer.state === "finished" && timer.finishedAt) {
    return Date.parse(timer.finishedAt) - timer.duration * 1000;
  }
  if (timer.finishesAt) {
    return Date.parse(timer.finishesAt) - timer.duration * 1000;
  }
  if (timer.state === "paused") {
    return nowMs - (timer.duration - (timer.remaining || 0)) * 1000;
  }
  return null;
}

function discoverTimers(states) {
  const out = [];
  for (const s of states || []) {
    if (!s || typeof s.entity_id !== "string") continue;
    if (!s.entity_id.startsWith("sensor.satellite_timer_")) continue;
    if (
      s.state !== "active" &&
      s.state !== "paused" &&
      s.state !== "finished" &&
      s.state !== "ringing"
    ) {
      continue;
    }
    const attrs = s.attributes || {};
    out.push({
      entity_id: s.entity_id,
      timer_id:
        attrs.timer_id || s.entity_id.replace("sensor.satellite_timer_", ""),
      name: displayName(s),
      state: s.state,
      duration: Number(attrs.duration) || 0,
      remaining: Number(attrs.remaining) || 0,
      finishesAt: attrs.finishes_at || null,
      finishedAt: attrs.finished_at || null,
      startedAt: attrs.started_at || null,
      deviceId: attrs.device_id || null,
      // BRIDGE-1: how the timer was started — the dismiss Undo must
      // restore it the same way (restore_finished_timer).
      startedViaService: attrs.started_via === "service",
    });
  }
  const now = Date.now();
  // Wave 1 sort (T-IMPL-2/3 — the finished-first defect F-T20 is
  // gone): running timers first (active, soonest first), then paused
  // (soonest first), then finished and ringing together,
  // most-recently-finished first. A finished timer no longer takes
  // focus over a running one.
  const groupOf = (t) =>
    t.state === "active" ? 0 : t.state === "paused" ? 1 : 2;
  out.sort((a, b) => {
    const ga = groupOf(a);
    const gb = groupOf(b);
    if (ga !== gb) return ga - gb;
    if (ga === 2) {
      return String(b.finishedAt || "").localeCompare(String(a.finishedAt || ""));
    }
    return remainingSeconds(a, now) - remainingSeconds(b, now);
  });
  return out;
}

function escapeHtml(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

// How many columns the grid uses, by viewport class ONLY (Wave 1,
// T-IMPL-1/4 — the count-driven grid is deleted: it shrank dials to
// 30px rings at 12 timers and overlapped names onto bars). Hub
// portrait 2, hub landscape 3, mobile portrait 1, mobile landscape 2;
// tablet and desktop by width (>=1600 -> 3, 900-1599 -> 2, <900 -> 1).
// Pure — unit-testable in node.
function gridColumnsFor(category, orientation, width) {
  const w = Number(width) || 0;
  if (category === "hub") return orientation === "portrait" ? 2 : 3;
  if (orientation === "portrait" && category === "mobile") return 1;
  if (category === "mobile") return 2;
  if (w >= 1600) return 3;
  if (w >= 900) return 2;
  return 1;
}

// Whether voice is provably unavailable (D4 fallback trigger,
// T-IMPL-12 — resolved against Navigation D-N4 and HA 2026.10.4):
// at least one assist_satellite entity exists AND every one is
// unavailable/unknown — or muted, for integrations that publish a
// muted/is_muted attribute (HA core publishes NO mute state: mute is
// device-internal, so the state leg is the only provable one). Zero
// assist_satellite entities is NOT proof of anything (fail-open, like
// the screensaver voice-as-activity condition): absence is not proof
// the satellite is down. Pure — pass hass.states (object or array).
function voiceUnavailable(states) {
  if (!states) return false;
  const sats = [];
  for (const s of Object.values(states)) {
    if (!s || typeof s.entity_id !== "string") continue;
    if (s.entity_id.indexOf("assist_satellite.") === 0) sats.push(s);
  }
  if (sats.length === 0) return false;
  return sats.every((s) => {
    if (s.state === "unavailable" || s.state === "unknown") return true;
    const attrs = s.attributes || {};
    return attrs.muted === true || attrs.is_muted === true;
  });
}

// The PIP pin (D7.3 / T-IMPL-20): the recipe card writes
// family-hub:timers:pin = {timerId} when the user taps a PIP. Read
// once per mount and clear after reading, so a later mount does not
// re-pin a stale timer. Returns the timer id, or null.
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
    // Clear failure: the pin is still consumed below (returned once
    // — the caller keeps it in memory, never re-reads).
  }
  try {
    const parsed = JSON.parse(raw);
    if (parsed && typeof parsed === "object" && typeof parsed.timerId === "string" && parsed.timerId) {
      return parsed.timerId;
    }
    if (typeof parsed === "string" && parsed) return parsed;
  } catch (_err) {
    if (typeof raw === "string" && raw) return raw;
  }
  return null;
}

// The Home arrival flag (T-IMPL-23): the Home card writes
// family-hub:timers:arrival = "home" when the user arrives from Home.
// Read once per mount and clear after reading. Returns true when the
// arrival was Home.
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

// -- scroll persistence (DESIGN-7, Phase 7) ------------------------------
//
// The screensaver return must restore the EXACT state, not just the
// view. The Timers view can overflow two ways (TIMER-7): the screen
// scrolls internally (its own overflow-y), or the Lovelace view sizes
// the card to content and the DOCUMENT scrolls instead — so both
// positions are saved. sessionStorage: survives the screensaver
// round-trip and view switches within the tab; a fresh tab starts at
// the top. Every read/write is guarded: a browser without storage
// behaves exactly as before.
const TIMER_SCROLL_KEY = "family-hub:timers:scroll";

function sanitizeTimerScrollTop(value) {
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
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return null;
  return {
    page: sanitizeTimerScrollTop(parsed.page),
    screen: sanitizeTimerScrollTop(parsed.screen),
  };
}

function serializeTimerScroll(state) {
  return JSON.stringify({
    page: sanitizeTimerScrollTop(state && state.page),
    screen: sanitizeTimerScrollTop(state && state.screen),
  });
}

// -- Hub identity: family-hub-core.js (Wave R1, RB-R1-3) ------------------
//
// Hub identity lives in family-hub-core.js (architecture v19 §4): the
// registered Browser Mod browser id — never a screen size, never the
// ?kiosk parameter. The per-card identity copies, the exact-size panel
// signature, and the kiosk session flag machinery are DELETED (§19);
// this card imports core for identity, and for the §9 version gates
// (T-6/T-10, RB-R1-5): REQUIRED_CORE + checkCoreVersion, and the
// bridge-contract gate (checkBridgeContract) wired into the render
// path below. The `ringing` code deletion itself is T-6/R2 work; with
// the gate in place a mismatched sensor never reaches those paths
// while core is loaded.
//
// Unlike the home card and the nav strip (whose URLs the wizard
// substitutes), this card never passes through the wizard — so it
// finds core at runtime: it asks Home Assistant for the Lovelace
// resource list (hass.callWS "lovelace/resources/list") and imports
// the resource whose bare path is /local/family-hub-core.js (the
// wizard registers the versioned URL, so the import gets the same
// module instance every other card uses). Until the import resolves,
// identity is unknown (desktop-safe, §4); on success every mounted
// instance re-applies its device classes and re-syncs at once. A
// failed lookup or import logs once and stays unknown — the card
// still renders, it just never reads as the wall.
//
// The lookup is re-attempted on every hass push (throttled) plus a
// 30 s interval while mounted, and never re-checked after success.
// The throttle/interval values mirror core.js (CORE_LOOKUP_RETRY_
// THROTTLE_MS / CORE_LOOKUP_INTERVAL_MS) — they live here as literals
// because the lookup runs before core is loaded, so they cannot be
// imported from it.
const CORE_RESOURCE_PATH = "/local/family-hub-core.js";
const CORE_LOOKUP_RETRY_THROTTLE_MS = 10 * 1000;
const CORE_LOOKUP_INTERVAL_MS = 30 * 1000;
// §9 core gate (T-10): the card declares the core version it was built
// against; on mismatch it renders the visible error instead of running
// against a contract it does not understand.
const REQUIRED_CORE = "2.0.0";

let _coreModule = null; // The imported core namespace, once loaded.
let _corePromise = null; // The in-flight lookup+import, shared by instances.
let _coreNextRetryAt = 0; // Failed lookup/import: retry at most every 10 s.
let _coreLoadWarned = false; // Failed lookup/import: log once, not per push.
const _liveCards = new Set(); // Mounted instances, for re-apply on load.

// Find the deployed core URL from the Lovelace resource list, or null
// when the list cannot be read or core is not registered yet.
async function _lookupCoreUrl(hass) {
  try {
    if (!hass || typeof hass.callWS !== "function") return null;
    const resources = await hass.callWS({ type: "lovelace/resources/list" });
    const list = Array.isArray(resources) ? resources : [];
    for (const res of list) {
      const url = res && typeof res.url === "string" ? res.url : "";
      if (url.split("?")[0] === CORE_RESOURCE_PATH) return url;
    }
    return null;
  } catch (_err) {
    return null;
  }
}

// Start (or reuse) the core lookup + import. Resolves to the core
// namespace, or null when the lookup/import fails (identity stays
// unknown, desktop-safe). A hass without callWS (node tests, or a
// push before hass exists) returns null immediately with no throttle
// set, so the first real hass push retries at once.
function _ensureCore(hass) {
  if (_coreModule) return Promise.resolve(_coreModule);
  if (_corePromise) return _corePromise;
  if (Date.now() < _coreNextRetryAt) return Promise.resolve(null);
  if (!hass || typeof hass.callWS !== "function") return Promise.resolve(null);
  _corePromise = _lookupCoreUrl(hass)
    .then((url) => (url ? import(url) : null))
    .then((mod) => {
      if (!mod) {
        _corePromise = null;
        _coreNextRetryAt = Date.now() + CORE_LOOKUP_RETRY_THROTTLE_MS;
        return null;
      }
      _coreModule = mod;
      for (const card of [..._liveCards]) {
        card._clearCoreRetry();
        card._applyDevice();
        card._sync();
      }
      return mod;
    })
    .catch((err) => {
      _corePromise = null;
      _coreNextRetryAt = Date.now() + CORE_LOOKUP_RETRY_THROTTLE_MS;
      if (!_coreLoadWarned) {
        _coreLoadWarned = true;
        if (typeof console !== "undefined") {
          console.warn(
            "Family Hub: could not load family-hub-core.js — running desktop-safe.",
            err
          );
        }
      }
      return null;
    });
  return _corePromise;
}

// Launch-only ?kiosk read: the same value core reports as
// layout.kioskParam, used before core loads (and never as identity).
function _isKioskLaunch() {
  try {
    const search =
      typeof window !== "undefined" && window.location
        ? window.location.search || ""
        : "";
    return /(?:^|[?&])kiosk(?:=|&|$)/.test(search);
  } catch (_err) {
    return false;
  }
}

// Test seam: inject a core module (the node tests load the real core.js
// through a .mjs copy and inject it here). Never used in the browser.
function _setCoreForTests(mod) {
  _coreModule = mod || null;
  _corePromise = mod ? Promise.resolve(mod) : null;
  _coreNextRetryAt = 0;
  _coreLoadWarned = false;
  for (const card of [..._liveCards]) {
    card._clearCoreRetry();
    card._applyDevice();
    card._sync();
  }
}

// -- Timers count badge (D-N1 / N-IMPL-5) ---------------------------------
// Same block as family-hub-nav-strip.js: counts ACTIVE + PAUSED
// (coordinator decision #25-D); colour from running/finished only
// (amber = a RUNNING timer in its last 60 s; red = any finished
// timer). Finished-only: when active+paused is 0 but a finished timer
// exists, the badge shows the finished count in red. Null = no badge.
// This card calls timerBadgeState(this._timers) directly — the timer
// objects from discoverTimers already carry {state, remaining}.
function timerBadgeState(timers) {
  let count = 0;
  let finished = 0;
  let soon = false;
  for (const timer of timers || []) {
    if (!timer || typeof timer.state !== "string") continue;
    if (timer.state === "active" || timer.state === "paused") count += 1;
    // Ringing is the finished group (RINGING-1): a ringing timer
    // counts in the red finished bucket, not the running count.
    if (timer.state === "finished" || timer.state === "ringing") finished += 1;
    if (timer.state === "active" && Number(timer.remaining) <= 60) soon = true;
  }
  if (count === 0 && finished === 0) return null;
  const shown = count === 0 ? finished : count;
  return {
    count: shown,
    tone: finished > 0 ? "finished" : soon ? "soon" : "count",
  };
}

function timerBadgeFromHassStates(states) {
  if (!states) return null;
  const timers = [];
  for (const key of Object.keys(states)) {
    if (key.indexOf("sensor.satellite_timer_") !== 0) continue;
    const state = states[key];
    if (!state) continue;
    const attrs = state.attributes || {};
    timers.push({ state: state.state, remaining: Number(attrs.remaining) || 0 });
  }
  return timerBadgeState(timers);
}

// -- Night mode (D-N10 / N-IMPL-24) ---------------------------------------
// Same block as family-hub-nav-strip.js: a setting with a schedule;
// the user sets the hours — there are NO default hours. Stored in
// localStorage family-hub:night-mode = {"start":"HH:MM","end":"HH:MM"};
// absent or invalid means night mode is off. The strip stays usable
// at night (dimmed palette, same targets).
const NIGHT_SETTING_KEY = "family-hub:night-mode";

function parseNightSetting(raw) {
  if (typeof raw !== "string" || !raw) return null;
  let parsed;
  try {
    parsed = JSON.parse(raw);
  } catch (_err) {
    return null;
  }
  if (!parsed || typeof parsed !== "object") return null;
  const toMinutes = (value) => {
    if (typeof value !== "string") return null;
    const m = /^(\d{1,2}):(\d{2})$/.exec(value.trim());
    if (!m) return null;
    const h = Number(m[1]);
    const min = Number(m[2]);
    if (h < 0 || h > 23 || min < 0 || min > 59) return null;
    return h * 60 + min;
  };
  const start = toMinutes(parsed.start);
  const end = toMinutes(parsed.end);
  if (start === null || end === null || start === end) return null;
  return { start, end };
}

function readNightSetting() {
  if (typeof localStorage === "undefined") return null;
  try {
    return parseNightSetting(localStorage.getItem(NIGHT_SETTING_KEY));
  } catch (_err) {
    return null;
  }
}

function isNightNow(setting, date) {
  if (!setting) return false;
  const now = date instanceof Date ? date : new Date();
  const minutes = now.getHours() * 60 + now.getMinutes();
  if (setting.start < setting.end) {
    return minutes >= setting.start && minutes < setting.end;
  }
  return minutes >= setting.start || minutes < setting.end;
}

// The recipe to go back to (TIMER-10 + N-IMPL-21): the recipe card's
// session state first (family-hub:recipe:state — openId + step, saved
// on every step change), the URL ?from_recipe=<id>&step=<n> as the
// fallback for a jump that arrived without a session. Returns null
// when neither carries a recipe. Pure enough to test: pass a query
// string in (the session is read from sessionStorage when present).
function readBackRecipeContext(search) {
  try {
    if (typeof sessionStorage !== "undefined") {
      const raw = sessionStorage.getItem("family-hub:recipe:state");
      if (raw) {
        const saved = JSON.parse(raw);
        if (saved && saved.openId) {
          return {
            recipeId: saved.openId,
            step: saved.step === undefined || saved.step === null ? null : saved.step,
          };
        }
      }
    }
  } catch (_err) {
    // Corrupt session state: fall through to the URL.
  }
  return readRecipeContext(search);
}

// The recipe the timer-jump came from (TIMER-10), if the Timers view
// URL carries it: ?from_recipe=<recipe_id>&step=<n>. Returns null
// outside a browser or when the params are absent. Pure enough to
// test: pass a query string in.
function readRecipeContext(search) {
  if (typeof search !== "string" || !search) return null;
  const params = new URLSearchParams(search);
  const recipeId = params.get("from_recipe");
  if (!recipeId) return null;
  const step = params.get("step");
  return { recipeId, step: step === null ? null : step };
}

// -- Navigation strip (NAV-1) --------------------------------------------
//
// The same strip on every view (see the NAV-STRIP CONTRACT in the
// implementation log; family-hub-nav-strip.js is the reference
// implementation and the AnyList recipe card renders the same strip
// from its own file). Noun labels, Home first, the current view
// (Timers, on this card) with the accent background, heavier weight,
// and aria-current. Navigation is history.pushState + a
// location-changed dispatch — no reload.

const NAV_STRIP_VIEWS = [
  { key: "home", label: "Home", path: "/family-hub" },
  { key: "timers", label: "Timers", path: "/family-hub/timers" },
  { key: "photos", label: "Photos", path: "/family-hub/family-photos" },
  { key: "recipes", label: "Recipes", path: "/family-hub/recipes" },
];

const CARD_CSS = `
  :host { display: block; height: 100%; position: relative; }
  .screen {
    height: 100%;
    min-height: 480px;
    box-sizing: border-box;
    padding: 24px;
    /* Wave 1 (T-IMPL-1/4/5): the screen is a grid in BOTH
       orientations — the columns come from the viewport class
       (gridColumnsFor), never from the timer count. Rows size to
       content; the view scrolls when they overflow. The old
       count-driven grid and the height-budget dial fit (dialFit /
       --dial-max / many-mode) are deleted: they shrank dials to
       30px rings and overlapped names onto bars (LB-7). */
    display: grid;
    grid-template-columns: repeat(1, 1fr);
    grid-auto-rows: auto;
    align-content: start;
    gap: 16px;
    background: transparent;
    overflow-y: auto;
    /* NAV-1: the nav strip's height on this device (96px hub, 80px
       tablet, 72px mobile, 0px desktop-hidden, 64px desktop+kiosk) —
       strip height + 8px (N-IMPL-20). The screen reserves it in bottom
       padding so the last row scrolls clear of the fixed strip; the
       Undo toast and the scroll pill read the same var. */
    --nav-strip-height: 96px;
    padding-bottom: calc(8px + var(--nav-strip-height, 96px));
  }
  .screen.cols-1 { grid-template-columns: repeat(1, 1fr); }
  .screen.cols-2 { grid-template-columns: repeat(2, 1fr); }
  .screen.cols-3 { grid-template-columns: repeat(3, 1fr); }
  .back-bar, .add-timer-row, .empty, .finished-section { grid-column: 1 / -1; }
  /* Dial (Wave 1, D1): a vertical stack — name block (full cell
     width, wraps to at most two lines) above the ring, the countdown
     centred INSIDE the ring button, state/hint lines below the ring,
     then the action button. The name and sub-text are HTML below and
     above the ring, never overlaid on it: the old .dial-text overlay
     centred text on the ring and clipped sub-labels at the circle's
     bottom edge (NAV-O2). The whole dial cell is the tap target
     (data-action="dial" on .dial). */
  .dial {
    min-width: 0;
    width: 100%;
    position: relative;
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 8px;
    cursor: pointer;
  }
  .dial.entering { animation: dial-in ${ENTER_ANIMATION_MS}ms ease-out; }
  @keyframes dial-in {
    from { opacity: 0; transform: scale(0.94); }
    to { opacity: 1; transform: scale(1); }
  }
  /* A dial the user just started from a chip pulses once so the new
     timer is easy to find after the scroll (T-IMPL-10). */
  .dial.pulse .dial-button { animation: dial-pulse 0.6s ease-out 1; }
  @keyframes dial-pulse {
    0% { transform: scale(0.94); }
    60% { transform: scale(1.03); }
    100% { transform: scale(1); }
  }
  .dial-name {
    width: 100%;
    color: #212121;
    font-size: 20px;
    line-height: 1.25;
    text-align: center;
    overflow-wrap: break-word;
    word-break: break-word;
    /* D1: at most two lines — a longer name is cut with an ellipsis
       rather than pushing the ring down into the next row (the D1.4
       gate: name-vs-previous-bar = 0). */
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }
  .dial-button {
    position: relative;
    /* Ring diameter = column width minus 32px padding (D1.4: the
       gate measures the .dial-button bounding-box width). */
    width: calc(100% - 32px);
    aspect-ratio: 1;
    padding: 0;
    border: none;
    background: none;
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
    /* No transition on width/height (DESIGN-6): animating layout
       properties causes reflow jank on a low-power board. */
  }
  .dial-button:active { transform: scale(0.98); }
  .dial-button:focus-visible { outline: 2px solid #C2410C; outline-offset: 4px; border-radius: 50%; }
  svg { display: block; width: 100%; height: auto; max-height: 100%; }
  /* TIMER-5 palette (spec §3.3, tokens --hub-color-timer-*): running
     ring #C2410C (4.96:1), soon/finished ring #B45309 (4.81:1), track
     #757575 (4.41:1). The old HA blue (2.52:1), amber (2.27:1) and
     track (1.31:1) all failed the 3:1 non-text bar. */
  .track { fill: none; stroke: #757575; stroke-width: 10; }
  .ring {
    fill: none;
    stroke: #C2410C;
    stroke-width: 10;
    stroke-linecap: round;
    stroke-dasharray: ${RING_CIRCUMFERENCE};
    transition: stroke-dashoffset 0.5s linear, stroke 0.3s ease;
  }
  .ring.paused { stroke: #C2410C; opacity: 0.45; }
  .dial.finished .ring { stroke: #B45309; }
  .dial.soon .ring { stroke: #B45309; animation: soon-pulse 1.6s ease-in-out infinite; }
  .dial.soon .time { color: #B45309; }
  .dial.paused .ring { animation: none; }
  @keyframes soon-pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.55; }
  }
  /* The countdown sits INSIDE the ring button, absolutely centred
     (TIMER-6: 72px, never below 40px — the time is the content, the
     ring is decorative). pointer-events none: the button underneath
     stays the tap target. _fitTimes steps the size down (72→64→56→
     48→44→40) when the rendered time is wider than the ring minus
     16px. */
  .time {
    position: absolute;
    inset: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    color: #212121;
    font-size: clamp(40px, 11vw, 72px);
    font-weight: 700;
    line-height: 1.1;
    pointer-events: none;
    text-align: center;
  }
  .dial-sub {
    display: flex;
    flex-direction: column;
    align-items: center;
    text-align: center;
    min-width: 0;
    width: 100%;
  }
  .state { color: #57534E; font-size: 18px; line-height: 1.3; }
  .started { color: #57534E; font-size: 16px; line-height: 1.3; }
  .hint { color: #57534E; font-size: 18px; line-height: 1.3; }
  .controls { display: flex; gap: 12px; margin-top: 4px; width: 100%; }
  .controls button {
    flex: 1;
    min-height: 80px;
    padding: 12px 8px;
    border-radius: 16px;
    border: 1px solid #EDE8E3;
    background: var(--card-background-color, #fff);
    color: var(--primary-text-color, #212121);
    font-size: 22px;
    font-weight: 700;
    cursor: pointer;
    animation: controls-in ${ENTER_ANIMATION_MS}ms ease-out;
    transition: background-color 0.2s ease, color 0.2s ease, border-color 0.2s ease;
  }
  @keyframes controls-in {
    from { opacity: 0; transform: translateY(8px); }
    to { opacity: 1; transform: translateY(0); }
  }
  .controls button:disabled { opacity: 0.5; cursor: default; }
  .controls button.cancel { background: #B71C1C; border-color: #B71C1C; color: #fff; position: relative; overflow: hidden; }
  .controls button.cancel.armed { background: #B71C1C; border-color: #B71C1C; }
  /* Cancel arm bar (T-IMPL-24): a bar drains across the bottom of
     the armed Cancel button over the 4 s arm window, so the cook can
     see how long the arm lasts. */
  .arm-bar {
    position: absolute;
    left: 0;
    bottom: 0;
    height: 6px;
    width: 100%;
    background: rgba(255, 255, 255, 0.75);
    animation: arm-drain ${CANCEL_ARM_MS}ms linear forwards;
    pointer-events: none;
  }
  @keyframes arm-drain {
    from { width: 100%; }
    to { width: 0; }
  }
  .controls button.restart { background: var(--card-background-color, #fff); }
  .error { color: #B71C1C; font-size: 14px; text-align: center; margin-top: 8px; width: 100%; }
  /* Empty state (D4): a centred card, at least 30% of the safe-area
     height, vertically centred within it — not a fixed-size block.
     "No timers running" at 32px, the two voice hints at 20px. */
  .empty {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    text-align: center;
    color: #57534E;
    gap: 12px;
    min-height: 30vh;
  }
  .empty[hidden] { display: none; }
  .empty svg { width: 72px; height: 72px; opacity: 0.6; margin-bottom: 12px; }
  .empty-title { font-size: 32px; line-height: 1.25; color: #212121; }
  .voice-hint { font-size: 20px; line-height: 1.4; max-width: 640px; }
  /* When the wall fallback chips show, the voice hints hide — they
     contradict the "Voice isn't available" line (T-IMPL-12). */
  .empty.voice-down .voice-hint { display: none; }
  .empty-error { color: #B71C1C; font-size: 18px; }
  .empty-error[hidden] { display: none; }
  /* "+ Add timer" row (T-IMPL-10) + fallback chips (T-IMPL-12): one
     full-width row at the top, above the dials. Non-hub only: a
     persistent "+ Add timer" button when timers run, toggling the
     chip row; the chips show directly when there are no timers. On
     the wall the row appears only when voice is down (fallback), with
     the plain line instead of the button. */
  .add-timer-row { display: flex; flex-direction: column; gap: 12px; }
  .add-timer-row[hidden] { display: none; }
  .add-timer-button {
    align-self: flex-start;
    min-height: 64px;
    padding: 12px 24px;
    border-radius: 16px;
    border: 1px solid #EDE8E3;
    background: var(--card-background-color, #fff);
    color: #212121;
    font-size: 22px;
    font-weight: 700;
    cursor: pointer;
  }
  .add-timer-hint { color: #57534E; font-size: 18px; line-height: 1.4; }
  .fallback-line { color: #212121; font-size: 20px; font-weight: 700; line-height: 1.4; }
  .preset-chips { display: none; flex-wrap: wrap; gap: 12px; justify-content: center; }
  .preset-chips.show { display: flex; }
  .preset-chips .chip-label { font-size: 20px; align-self: center; }
  .preset-chips button {
    min-height: 56px;
    min-width: 96px;
    padding: 12px 20px;
    border-radius: 16px;
    border: 1px solid #EDE8E3;
    background: var(--card-background-color, #fff);
    color: #212121;
    font-size: 20px;
    font-weight: 700;
    cursor: pointer;
  }
  .preset-chips button:disabled { opacity: 0.5; cursor: default; }
  /* Mobile landscape: the open chips anchor above the strip so they
     stay reachable while the view scrolls (T-IMPL-11). */
  .screen.dev-mobile.orient-landscape .preset-chips.show {
    position: fixed;
    left: 16px;
    right: 16px;
    bottom: calc(var(--nav-strip-height, 72px) + 16px);
    z-index: 30;
  }
  /* Back to recipe (TIMER-10): shown only when the jump came from a
     recipe (?from_recipe=<id>). Fixed size — never viewport-computed:
     at most 229x64 on the wall, 196x56 on the phone. */
  .back-bar { display: flex; justify-content: flex-start; }
  .back-bar button {
    height: 64px;
    max-width: 229px;
    padding: 12px 24px;
    border-radius: 16px;
    border: 1px solid #EDE8E3;
    background: var(--card-background-color, #fff);
    color: #212121;
    font-size: 22px;
    font-weight: 700;
    cursor: pointer;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  /* Finished section (TIMER-7, Wave 1): finished and ringing timers
     live here as compact rows, never as dials — a finished timer
     must not take a dial's space. The header collapses the section;
     it is collapsed by default when a timer is running or paused,
     expanded on arrival when none is, and forced expanded while any
     timer is ringing. Rows are 74px: name + sub-line on the left,
     Restart on the right. */
  .finished-section { display: flex; flex-direction: column; gap: 12px; }
  .finished-section[hidden] { display: none; }
  .finished-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
    width: 100%;
    min-height: 64px;
    padding: 12px 20px;
    box-sizing: border-box;
    border-radius: 16px;
    border: 1px solid #EDE8E3;
    background: var(--card-background-color, #fff);
    color: #212121;
    font-size: 22px;
    font-weight: 700;
    cursor: pointer;
    text-align: left;
  }
  .finished-header .finished-chevron { font-size: 22px; color: #57534E; }
  .finished-rows { display: flex; flex-direction: column; gap: 12px; }
  .finished-rows[hidden] { display: none; }
  .finished-row {
    display: flex;
    align-items: center;
    gap: 16px;
    width: 100%;
    min-height: 74px;
    padding: 8px 16px;
    box-sizing: border-box;
    border: 1px solid #EDE8E3;
    border-radius: 16px;
    background: var(--card-background-color, #fff);
    cursor: pointer;
  }
  .finished-text { flex: 1; min-width: 0; display: flex; flex-direction: column; text-align: left; }
  .finished-name {
    color: #212121;
    font-size: 20px;
    font-weight: 700;
    line-height: 1.25;
    overflow-wrap: break-word;
    word-break: break-word;
  }
  .finished-sub { color: #57534E; font-size: 18px; line-height: 1.3; overflow-wrap: break-word; }
  .finished-restart {
    flex: none;
    min-height: 56px;
    padding: 8px 20px;
    border-radius: 16px;
    border: 1px solid #EDE8E3;
    background: var(--card-background-color, #fff);
    color: #212121;
    font-size: 20px;
    font-weight: 700;
    cursor: pointer;
  }
  /* -- Navigation strip (NAV-1 / D-N1, wave 1) ------------------------
     The persistent strip, FIRST in the DOM (tab order). Noun labels,
     Home first, the current view (Timers) with the accent background,
     heavier weight, and aria-current. Hidden on DESKTOP only when NOT
     in kiosk mode, where HA tabs already exist and work; always shown
     on hub AND phone, and on desktop IN kiosk mode (HA tabs do not
     exist there — the kiosk-desktop hole). Heights: 96px wall, 72px
     tablet, 64px phone (+ safe area), 56px desktop. The strip is
     solid dark #171717 (coordinator decision B — the design mandate
     governs), white text, inactive labels white at 70%. The Timers
     button carries the count badge (N-IMPL-5, ACTIVE + PAUSED, amber
     last 60 s / red finished). The strip is fixed to the viewport
     bottom, in both orientations. */
  .nav-strip {
    position: fixed;
    bottom: 0;
    left: 0;
    width: 100%;
    z-index: 40;
    box-sizing: border-box;
    height: 96px;
    padding: 0;
    background: #171717;
  }
  .nav-strip-buttons { display: flex; gap: 0; height: 100%; }
  .nav-strip-button {
    position: relative;
    flex: 1;
    min-width: 0;
    height: 100%;
    min-height: 0;
    padding: 0 4px;
    border: none;
    border-radius: 0;
    background: transparent;
    color: rgba(255, 255, 255, 0.7);
    font-size: 24px;
    font-weight: 700;
    cursor: pointer;
    font-family: inherit;
    overflow-wrap: break-word;
    transition: transform 100ms ease-out;
  }
  .nav-strip-button:hover { background: rgba(255, 255, 255, 0.08); }
  .nav-strip-button:active { transform: scale(0.97); background: rgba(255, 255, 255, 0.14); }
  .nav-strip-button.active { background: #C2410C; border-color: #C2410C; color: #fff; font-weight: 800; }
  .nav-strip-button:focus-visible { outline: 3px solid #fff; outline-offset: 2px; }
  /* Timers count badge (D-N1 / N-IMPL-5): white circle, dark numerals,
     24 px, top-right of the Timers button. Amber = a running timer in
     its last 60 s; red = a finished timer. Never shows 0. Wave 1: it
     always shows while timers exist — the scroll indicator moved out
     of the strip into its own floating pill, so the two no longer
     share a slot and no reconciliation is needed. */
  .timer-badge {
    position: absolute;
    top: 6px;
    right: 6px;
    min-width: 24px;
    height: 24px;
    padding: 0 4px;
    box-sizing: border-box;
    border-radius: 999px;
    background: #fff;
    color: #171717;
    font-size: 14px;
    font-weight: 800;
    line-height: 24px;
    text-align: center;
    pointer-events: none;
    z-index: 1;
  }
  .timer-badge.soon { background: #B45309; color: #fff; }
  .timer-badge.finished { background: #B91C1C; color: #fff; }
  .timer-badge[hidden] { display: none; }
  /* Night mode (D-N10): the strip stays visible and usable — dimmed
     palette, same targets, same labels, same active state. */
  .screen.night .nav-strip { background: #0A0A0A; }
  .screen.night .nav-strip-button { color: rgba(255, 255, 255, 0.55); }
  .screen.night .nav-strip-button.active { background: #7C2D12; color: #fff; }
  /* Visible scroll indicator (TIMER-7 / P8-5, Wave 1): a floating
     "↓" pill in the content area — NOT on the strip button, so the
     count badge and the indicator can both show. It sits above the
     strip (bottom set from the strip height in JS) and scrolls the
     view down when tapped. Shown only while a dial sits below the
     viewport fold (P8-4 gate — see _updateScrollPill). */
  .scroll-pill {
    position: fixed;
    right: 16px;
    z-index: 50;
    min-width: 48px;
    height: 48px;
    padding: 0 12px;
    box-sizing: border-box;
    border-radius: 999px;
    border: none;
    background: #1C1917;
    color: #fff;
    font-size: 24px;
    font-weight: 700;
    line-height: 48px;
    text-align: center;
    cursor: pointer;
  }
  .scroll-pill[hidden] { display: none; }
  /* Cancel Undo toast (TIMER-13 / F-T17): fixed to the viewport, so
     it can never sit behind the fixed strip — the old absolute toast
     hid behind it. The bottom is set from the measured strip height
     + 8px in JS (see _showUndoToast); the 24px here is the no-strip
     fallback (desktop, strip hidden). */
  .toast {
    position: fixed;
    left: 50%;
    bottom: 24px;
    transform: translateX(-50%);
    display: flex;
    align-items: center;
    gap: 16px;
    background: #1C1917;
    color: #fff;
    font-size: 20px;
    padding: 12px 16px 12px 24px;
    border-radius: 16px;
    z-index: 50;
    max-width: calc(100% - 48px);
    box-sizing: border-box;
  }
  .toast[hidden] { display: none; }
  .toast button {
    min-height: 56px;
    padding: 8px 20px;
    border-radius: 12px;
    border: 1px solid #fff;
    background: transparent;
    color: #fff;
    font-size: 20px;
    font-weight: 700;
    cursor: pointer;
    white-space: nowrap;
  }
  /* -- Adaptive layout --------------------------------------------------
     The card reads its device from core identify() and carries the
     category + orientation as classes on .screen: dev-hub, dev-desktop,
     dev-tablet or dev-mobile, and orient-landscape or orient-portrait.
     The rules above ARE the hub layout (kitchen kiosk: big dials, big
     touch targets); the rules below only adjust for desktop, tablet
     and mobile. A card with no device classes (no browser environment)
     renders the hub layout. */
  /* Identity banner (§4): the re-register banner shows only on a ?kiosk
     launch whose browser id mismatches the registered wall id. It is a
     shadow-root child before .screen so no re-sync wipes it. */
  .fh-identity-banner {
    display: block;
    background: #7f1d1d;
    color: #fff;
    font-size: 18px;
    font-weight: 600;
    line-height: 1.4;
    text-align: center;
    padding: 12px 16px;
  }
  .fh-identity-banner[hidden] { display: none; }
  /* §9 gate error (T-10): core/bridge mismatch, same dark-red treatment
     as the identity banner; hidden until a gate fires. */
  .fh-gate-mismatch {
    display: block;
    background: #7f1d1d;
    color: #fff;
    font-size: 18px;
    font-weight: 600;
    line-height: 1.4;
    text-align: center;
    padding: 12px 16px;
  }
  .fh-gate-mismatch[hidden] { display: none; }
  /* Desktop: a mouse is precise, so the buttons shrink. */
  .screen.dev-desktop { --nav-strip-height: 0px; }
  .screen.dev-desktop .controls button { min-height: 56px; font-size: 18px; padding: 8px; }
  .screen.dev-desktop .dial-name { font-size: 20px; }
  .screen.dev-desktop .state, .screen.dev-desktop .hint { font-size: 16px; }
  .screen.dev-desktop .back-bar button { height: 56px; font-size: 18px; }
  .screen.dev-desktop .add-timer-button { min-height: 56px; font-size: 18px; }
  /* Desktop: the nav strip is hidden (HA tabs already exist and work
     there — NAV-1) ONLY when NOT in kiosk mode: in kiosk mode HA tabs
     do not exist, so a desktop-class viewport in kiosk mode would have
     ZERO navigation with the strip hidden (the kiosk-desktop hole).
     The strip therefore shows in kiosk mode ALWAYS (the ".kiosk"
     override below); desktop+kiosk reserves the strip height (64px)
     in --nav-strip-height so the last row scrolls clear of it, and
     the Undo toast clears it too. */
  .screen.dev-desktop .nav-strip { display: none; }
  .screen.dev-desktop.kiosk { --nav-strip-height: 64px; }
  .screen.dev-desktop.kiosk .nav-strip { height: 56px; }
  .screen.dev-desktop.kiosk .nav-strip-button { font-size: 20px; }
  .screen.dev-desktop.kiosk .nav-strip { display: block; }
  /* Mobile: touch targets stay large (a finger is not a mouse) and the
     buttons stay reachable. In portrait the dials stack; the screen
     scrolls when the timers need more room than the phone has.
     Preset chips (TIMER-4) show on mobile and tablet — the hub is
     voice-only by deliberate choice. Wave 1: the dial sub-text is
     BELOW the ring at full cell width now, so the old 14px chord
     workaround (Phase 7 Fix 2) is deleted — state/hint are 18px on
     mobile too, and there is no 300px portrait dial minimum. */
  .screen.dev-mobile { --nav-strip-height: 72px; }
  .screen.dev-mobile .controls button { min-height: 64px; font-size: 18px; }
  .screen.dev-mobile .dial-name { font-size: 18px; }
  .screen.dev-mobile .state, .screen.dev-mobile .hint { font-size: 18px; }
  .screen.dev-mobile .back-bar button { height: 56px; max-width: 196px; font-size: 18px; }
  /* Phone nav strip (NAV-1): 48px buttons, 16px labels, 8px gap,
     nowrap — four labels would wrap mid-word at 360px, so the strip
     is 16px + nowrap (same rule as the recipe card). */
  .screen.dev-mobile .nav-strip { height: calc(64px + env(safe-area-inset-bottom, 0px)); }
  .screen.dev-mobile .nav-strip-buttons { height: 64px; }
  .screen.dev-mobile .nav-strip-button { font-size: 16px; white-space: nowrap; }
  /* Tablet (DESIGN-9): 56-64px targets (spec §3.2); dial text at
     the TIMER-6 hub minima (name 20px, state/hint 18px) — readable
     at 1m. Preset chips show here too (TIMER-4: phone/tablet get
     on-screen presets; hub is voice-only). */
  .screen.dev-tablet { --nav-strip-height: 80px; }
  .screen.dev-tablet .controls button { min-height: 64px; font-size: 18px; }
  .screen.dev-tablet .dial-name { font-size: 20px; }
  .screen.dev-tablet .state, .screen.dev-tablet .hint { font-size: 18px; }
  .screen.dev-tablet .back-bar button { height: 56px; font-size: 18px; }
  .screen.dev-tablet .add-timer-button { min-height: 56px; font-size: 18px; }
  /* Tablet nav strip (NAV-1): 56px buttons, 20px labels — the 56-64px
     tablet band (spec §3.2). */
  .screen.dev-tablet .nav-strip { height: 72px; }
  .screen.dev-tablet .nav-strip-button { font-size: 20px; }
  /* Reduced motion (DESIGN-6, WCAG 2.2): no non-essential animation.
     The soon-ring pulse stops — the amber ring + orange time (the
     "soon" STATE, DESIGN-8 guardrail) still show. The ring sweep is
     continuous progress, not decoration, and its stroke-dashoffset
     transition is paint-only (no layout), so it stays. Dial entry,
     controls entry, the cancel-arm pulse and the new-dial pulse
     become instant; the press feedback is instant too. */
  @media (prefers-reduced-motion: reduce) {
    .dial.entering { animation: none; }
    .controls button { animation: none; }
    .dial.soon .ring { animation: none; }
    .dial.pulse .dial-button { animation: none; }
    .arm-bar { animation: none; }
    .dial-button:active { transform: none; }
    .nav-strip-button:active { transform: none; }
  }
`;

class TimerScreenCard extends HTMLElement {
  constructor() {
    super();
    this._hass = null;
    this._config = null;
    this._timers = [];
    this._timerBadgeEl = null;
    this._lastKey = "";
    this._busy = new Set();
    this._error = {};
    this._arming = new Map();
    this._tick = null;
    this._dialEls = new Map();
    this._rowEls = new Map(); // timer_id -> finished-section row element
    this._screenEl = null;
    this._emptyEl = null;
    this._emptyErrorEl = null;
    this._backBarEl = null; // built in _sync only when a recipe context exists
    this._addTimerRowEl = null; // "+ Add timer" / fallback chips row (built in _ensureShell)
    this._addTimerButtonEl = null;
    this._addTimerHintEl = null;
    this._fallbackLineEl = null;
    this._chipsEl = null; // preset chips row (shared by + Add timer and fallback)
    this._chipsOpen = false; // "+ Add timer" toggled open (timers running)
    this._finishedSectionEl = null;
    this._finishedHeaderEl = null;
    this._finishedRowsEl = null;
    this._finishedOpen = null; // null = not decided this mount (arrival rule applies)
    this._navStripEl = null; // NAV-1 strip (built in _ensureShell)
    this._lastNavigation = null; // Last path _navigate was asked for (test hook)
    this._scrollPillEl = null; // "↓" floating pill (TIMER-7), built in _ensureShell
    this._toastEl = null;
    this._toastTimer = null;
    this._undoTimer = null;
    this._presetBusy = false;
    this._presetBeforeIds = null; // timer ids before a chip tap (scroll+pulse the new dial)
    this._device = null; // { category, orientation } from core deviceFromIdentity, or null outside a browser.
    this._identity = null; // Last core identify() result, or null before core loads.
    this._identityBanner = null;
    this._coreMismatch = null; // checkCoreVersion() message, or null when core matches.
    this._bridgeMismatch = null; // checkBridgeContract() error, or null when all sensors are v2.
    this._bridgeMismatched = new Set(); // entity ids excluded from timer UI by the bridge gate.
    this._gateMismatchEl = null; // visible gate-error element, built in _ensureShell.
    this._coreRetryTimer = null; // 30 s core-lookup retry interval, while mounted.
    this._voiceDown = false; // voice provably unavailable (voiceUnavailable)
    this._pinTimerId = null; // PIP pin, read once per mount
    this._arrivalHome = false; // Home arrival flag, read once per mount
    this._overridesRead = false; // pin/arrival already read this mount
    this._resizeBound = null;
    this._scrollBound = null;
    this._windowScrollBound = null;
    this._scrollRestored = false; // Mount scroll restore done (see _restoreScroll).
    this._restoreWritten = null; // Scroll positions the last restore wrote (moved-guard).
    this._scrollSaveTimer = null;
    this._screenResizeObserver = null;
    this._duplicateNames = new Set();
    this._startedAtMs = new Map(); // timer_id -> epoch ms, cached on first sight (TIMER-11)
    this._recipeContext = null; // { recipeId, step } from ?from_recipe= (TIMER-10)
    this.attachShadow({ mode: "open" });
    this.shadowRoot.addEventListener("click", (event) => this._onClick(event));
  }

  setConfig(config) {
    this._config = config || {};
    this._ensureShell();
    this._sync();
  }

  // §9 version gates (T-6/T-10): evaluate both gates against the given
  // timer list and return the admitted timers. Core mismatch and bridge
  // mismatch are stored for the render path. Mismatched sensors are
  // excluded here, so no dial, finished row, badge, or tick ever sees
  // them. When core has not loaded yet there is nothing to compare
  // against, so both gates read null and the list passes through
  // unchanged (the R1-3 behaviour); the re-sync after core loads
  // re-evaluates (see _sync).
  _refreshGates(timers) {
    if (_coreModule && typeof _coreModule.checkCoreVersion === "function") {
      this._coreMismatch = _coreModule.checkCoreVersion(REQUIRED_CORE);
    } else {
      this._coreMismatch = null;
    }
    let mismatched = new Set();
    if (
      _coreModule &&
      typeof _coreModule.checkBridgeContract === "function" &&
      this._hass
    ) {
      const res = _coreModule.checkBridgeContract(this._hass.states);
      this._bridgeMismatch = res.error;
      mismatched = new Set(res.mismatched || []);
    } else {
      this._bridgeMismatch = null;
    }
    this._bridgeMismatched = mismatched;
    // A core-version skew admits nothing: the card does not run against
    // a contract it does not understand (C-5/T-10 "instead of running").
    if (this._coreMismatch) return [];
    return (timers || []).filter((t) => !mismatched.has(t.entity_id));
  }

  set hass(hass) {
    this._hass = hass;
    const timers = this._refreshGates(
      discoverTimers(hass ? Object.values(hass.states) : [])
    );
    const key = JSON.stringify(
      timers.map((t) => [t.entity_id, t.state, t.name, t.duration])
    );
    // The wall fallback chips are state-driven (D4): a voice change
    // re-syncs even when no timer changed.
    const voiceDown = voiceUnavailable(hass ? hass.states : null);
    const voiceChanged = voiceDown !== this._voiceDown;
    this._voiceDown = voiceDown;
    if (key !== this._lastKey || voiceChanged) {
      this._timers = timers;
      this._lastKey = key;
      const live = new Set(timers.map((t) => t.timer_id));
      for (const id of [...this._busy]) if (!live.has(id)) this._busy.delete(id);
      for (const id of Object.keys(this._error)) {
        if (!live.has(id)) delete this._error[id];
      }
      for (const [id, timeout] of [...this._arming]) {
        if (!live.has(id)) {
          clearTimeout(timeout);
          this._arming.delete(id);
        }
      }
      for (const id of [...this._startedAtMs.keys()]) {
        if (!live.has(id)) this._startedAtMs.delete(id);
      }
      this._ensureShell();
      this._sync();
    }
    // R1: (re-)attempt the core lookup on every hass push (throttled);
    // the retry interval itself only runs while mounted.
    this._ensureCoreForCard();
  }

  connectedCallback() {
    // R1: register for the core re-apply and kick off the lookup.
    // No kiosk re-apply: ?kiosk is launch-only (§4) — identity is
    // the Browser Mod id, so a bare-URL reload still identifies.
    _liveCards.add(this);
    this._ensureCoreForCard();
    this._stopTick();
    this._tick = setInterval(() => this._updateTimes(), 500);
    // The layout follows the viewport: a resize or an orientation
    // change re-detects the device and re-syncs the grid.
    if (
      typeof window !== "undefined" &&
      typeof window.addEventListener === "function" &&
      !this._resizeBound
    ) {
      this._resizeBound = () => this._onResize();
      window.addEventListener("resize", this._resizeBound);
    }
    // The scroll badge (TIMER-7) also tracks the document scroll: when
    // the Lovelace view sizes the card to content, the screen is
    // taller than the viewport and the page scrolls, not the screen —
    // without this listener the badge would never update as the user
    // scrolls down to the last dials. The same listener saves the
    // scroll position (DESIGN-7), so the screensaver return restores
    // the exact state.
    if (
      typeof window !== "undefined" &&
      typeof window.addEventListener === "function" &&
      !this._windowScrollBound
    ) {
      this._windowScrollBound = () => {
        this._updateScrollPill();
        this._scheduleScrollSave();
      };
      window.addEventListener("scroll", this._windowScrollBound, {
        passive: true,
      });
    }
  }

  disconnectedCallback() {
    _liveCards.delete(this);
    this._clearCoreRetry();
    this._stopTick();
    if (
      typeof window !== "undefined" &&
      this._resizeBound &&
      typeof window.removeEventListener === "function"
    ) {
      window.removeEventListener("resize", this._resizeBound);
      this._resizeBound = null;
    }
    if (
      typeof window !== "undefined" &&
      this._windowScrollBound &&
      typeof window.removeEventListener === "function"
    ) {
      window.removeEventListener("scroll", this._windowScrollBound);
      this._windowScrollBound = null;
    }
    if (this._scrollSaveTimer !== null) {
      clearTimeout(this._scrollSaveTimer);
      this._scrollSaveTimer = null;
    }
    this._scrollRestored = false;
    this._restoreWritten = null;
    if (this._screenResizeObserver) {
      this._screenResizeObserver.disconnect();
      this._screenResizeObserver = null;
    }
  }

  _onResize() {
    // Re-sync only when the device actually changed: a category
    // change moves the grid columns, an orientation change moves the
    // layout. A pure viewport resize (same category, same
    // orientation) needs no re-sync — the grid is viewport-class
    // based and the ring sizes from the column width — but the time
    // fit and the scroll pill re-evaluate against the new size.
    const before = this._device;
    this._applyDevice();
    const after = this._device;
    if (
      before &&
      after &&
      (before.category !== after.category ||
        before.orientation !== after.orientation)
    ) {
      this._sync();
    } else {
      this._fitTimes();
    }
    this._updateScrollPill();
  }

  // Detect the device and carry it as classes on the screen element:
  // dev-hub, dev-desktop or dev-mobile, plus orient-landscape or
  // orient-portrait. The CSS keys the adaptive layout off these
  // classes; the base rules are the hub layout. Outside a browser
  // (node tests) there is no environment to read, so the classes are
  // left off and the base layout stands.
  _applyDevice() {
    if (!this._screenEl) return;
    // No viewport (node tests): keep whatever device was set — the
    // old null-environment early return. A browser always
    // has a window, so this never fires there.
    if (typeof window === "undefined") return;
    const width =
      typeof window !== "undefined" && typeof window.innerWidth === "number"
        ? window.innerWidth
        : 0;
    const height =
      typeof window !== "undefined" && typeof window.innerHeight === "number"
        ? window.innerHeight
        : 0;
    let device;
    let identity = null;
    if (_coreModule) {
      // Identity comes from core (§4): the registered Browser Mod id.
      identity = _coreModule.identify(this._hass);
      device = _coreModule.deviceFromIdentity(identity, width, height);
    } else {
      // Core not loaded yet (or failed): unknown → desktop-safe (§4).
      // The kiosk class still follows the launch URL so the strip does
      // not flash hidden while the lookup resolves.
      this._ensureCoreForCard();
      device = {
        category: "desktop",
        orientation: width >= height ? "landscape" : "portrait",
      };
    }
    this._identity = identity;
    this._device = device;
    this._screenEl.classList.remove(
      "dev-hub",
      "dev-desktop",
      "dev-tablet",
      "dev-mobile",
      "orient-landscape",
      "orient-portrait",
      "kiosk",
      "night"
    );
    this._screenEl.classList.add(
      `dev-${device.category}`,
      `orient-${device.orientation}`
    );
    // Kiosk mode is a class of its own (not a device category): the
    // nav strip shows in kiosk mode ALWAYS, even on desktop (NAV-1).
    // ?kiosk is launch-only (§4): the class follows the launch URL
    // (core's layout.kioskParam once loaded) — no flag is stored or
    // re-applied.
    const kioskLaunch = identity
      ? Boolean(identity.layout && identity.layout.kioskParam)
      : _isKioskLaunch();
    if (kioskLaunch) this._screenEl.classList.add("kiosk");
    // Night mode (D-N10): dimmed strip palette, same targets.
    if (isNightNow(readNightSetting(), new Date())) this._screenEl.classList.add("night");
    this._updateIdentityBanner();
  }

  // §4 re-register banner: visible only on a ?kiosk launch whose browser
  // id mismatches the registered wall id. Mismatch without ?kiosk is the
  // known silent case (fixes-18 item 25): no banner, only core's
  // once-only console warning.
  _updateIdentityBanner() {
    if (!this._identityBanner) return;
    const banner = this._identity && this._identity.banner;
    if (banner) {
      this._identityBanner.textContent = banner;
      this._identityBanner.hidden = false;
    } else {
      this._identityBanner.textContent = "";
      this._identityBanner.hidden = true;
    }
  }

  // Start the core lookup for this instance (idempotent): one attempt
  // now, then a retry every 30 s until core loads or the card
  // unmounts. The interval runs only while mounted (connectedCallback
  // registers the card in _liveCards) — node tests never mount, so no
  // interval is ever started there and the test process can exit.
  _ensureCoreForCard() {
    if (_coreModule) return;
    _ensureCore(this._hass);
    if (_liveCards.has(this) && !this._coreRetryTimer) {
      this._coreRetryTimer = setInterval(() => {
        if (_coreModule) {
          this._clearCoreRetry();
          return;
        }
        _ensureCore(this._hass);
      }, CORE_LOOKUP_INTERVAL_MS);
      // Node tests mount cards without ever unmounting; unref the
      // retry timer so it never holds the test process open. Browsers
      // return a plain number from setInterval (no unref), so this is
      // a no-op there.
      if (
        this._coreRetryTimer &&
        typeof this._coreRetryTimer.unref === "function"
      ) {
        this._coreRetryTimer.unref();
      }
    }
  }

  _clearCoreRetry() {
    if (this._coreRetryTimer !== null) {
      clearInterval(this._coreRetryTimer);
      this._coreRetryTimer = null;
    }
  }

  _stopTick() {
    if (this._tick !== null) {
      clearInterval(this._tick);
      this._tick = null;
    }
  }

  // -- shell: built once, then kept -------------------------------------

  _ensureShell() {
    if (this._screenEl) return;
    const style = document.createElement("style");
    style.textContent = CARD_CSS;
    this.shadowRoot.appendChild(style);
    this._identityBanner = document.createElement("div");
    this._identityBanner.className = "fh-identity-banner";
    this._identityBanner.hidden = true;
    // §9 gate error (§9/T-10): the visible mismatch error, a shadow-root
    // child after the banner and before .screen so no re-sync wipes it.
    this._gateMismatchEl = document.createElement("div");
    this._gateMismatchEl.className = "fh-gate-mismatch";
    this._gateMismatchEl.setAttribute("role", "alert");
    this._gateMismatchEl.hidden = true;
    this.shadowRoot.appendChild(this._gateMismatchEl);
    // Banner BEFORE the screen in the shadow tree (appendChild only —
    // the node test harness has no insertBefore, and neither do we
    // need it: create order is document order).
    this.shadowRoot.appendChild(this._identityBanner);
    this._screenEl = document.createElement("div");
    this._screenEl.className = "screen";
    this.shadowRoot.appendChild(this._screenEl);
    // NAV-1: the nav strip is the FIRST child of the screen (DOM-first
    // for tab order). It is fixed to the viewport bottom (see the
    // CARD_CSS comment: sticky cannot work here because the screen is
    // a scroll container and the card can grow past the viewport). It
    // is rendered on every path — the timer card only runs on the
    // Timers view, so Timers is the active view. Same views, labels,
    // paths and active rule as the recipe card and
    // family-hub-nav-strip.js.
    this._navStripEl = document.createElement("nav");
    this._navStripEl.className = "nav-strip";
    this._navStripEl.setAttribute("aria-label", "Views");
    this._navStripEl.innerHTML =
      '<div class="nav-strip-buttons">' +
      NAV_STRIP_VIEWS.map((view) => {
        const active = view.key === "timers";
        const badge =
          view.key === "timers"
            ? '<span class="timer-badge" aria-label="0 timers" hidden></span>'
            : "";
        return `<button type="button" class="nav-strip-button${active ? " active" : ""}" data-action="nav" data-path="${view.path}"${active ? ' aria-current="page"' : ""}>${view.label}${badge}</button>`;
      }).join("") +
      "</div>";
    this._screenEl.appendChild(this._navStripEl);
    this._timerBadgeEl = this._navStripEl.querySelector(".timer-badge");
    // The back bar is NOT built here: it exists only when a recipe
    // context exists (TIMER-10 / T-IMPL-18) and is created/removed in
    // _sync, so a plain load has zero DOM residue of it.
    // "+ Add timer" row (T-IMPL-10) + wall fallback chips (T-IMPL-12):
    // one full-width row at the top, above the dials. Its contents
    // are shown/hidden in _sync: the button on non-hub while timers
    // run, the chips directly when there are no timers, the fallback
    // line + chips on the wall while voice is down.
    this._addTimerRowEl = document.createElement("div");
    this._addTimerRowEl.className = "add-timer-row";
    this._addTimerRowEl.hidden = true;
    this._addTimerRowEl.innerHTML =
      '<button class="add-timer-button" data-action="add-timer" aria-expanded="false">+ Add timer</button>' +
      '<div class="add-timer-hint" hidden></div>' +
      '<div class="fallback-line" hidden>Voice isn\'t available right now — tap a time to start a timer</div>' +
      '<div class="preset-chips"><span class="chip-label">Start a timer:</span>' +
      PRESET_MINUTES.map(
        (m) =>
          `<button data-action="preset" data-minutes="${m}" aria-label="Start a ${m} minute timer">${m} min</button>`
      ).join("") +
      "</div>";
    this._screenEl.appendChild(this._addTimerRowEl);
    this._addTimerButtonEl = this._addTimerRowEl.querySelector(".add-timer-button");
    this._addTimerHintEl = this._addTimerRowEl.querySelector(".add-timer-hint");
    this._fallbackLineEl = this._addTimerRowEl.querySelector(".fallback-line");
    this._chipsEl = this._addTimerRowEl.querySelector(".preset-chips");
    this._emptyEl = document.createElement("div");
    this._emptyEl.className = "empty";
    this._emptyEl.innerHTML =
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="12" cy="13" r="8" /><path d="M12 9v4l2.5 2.5M9 2h6" /></svg><div class="empty-title">No timers running</div>' +
      '<div class="voice-hint voice-hint-1"></div>' +
      '<div class="voice-hint voice-hint-2"></div>' +
      '<div class="empty-error" hidden></div>';
    this._screenEl.appendChild(this._emptyEl);
    this._emptyErrorEl = this._emptyEl.querySelector(".empty-error");
    // Finished section (TIMER-7, Wave 1): finished and ringing timers
    // live here as compact rows, never as dials. Built once, hidden
    // while there is nothing finished; the header toggles it.
    this._finishedSectionEl = document.createElement("div");
    this._finishedSectionEl.className = "finished-section";
    this._finishedSectionEl.hidden = true;
    this._finishedSectionEl.innerHTML =
      '<button class="finished-header" data-action="finished-toggle" aria-expanded="false"><span class="finished-title">Finished (0)</span><span class="finished-chevron" aria-hidden="true">▾</span></button>' +
      '<div class="finished-rows" hidden></div>';
    this._screenEl.appendChild(this._finishedSectionEl);
    this._finishedHeaderEl = this._finishedSectionEl.querySelector(".finished-header");
    this._finishedRowsEl = this._finishedSectionEl.querySelector(".finished-rows");
    this._toastEl = document.createElement("div");
    this._toastEl.className = "toast";
    this._toastEl.hidden = true;
    this.shadowRoot.appendChild(this._toastEl);
    // Scroll pill (TIMER-7 / P8-5, Wave 1): a floating "↓" pill in
    // the content area, NOT on the strip — the count badge and the
    // indicator no longer share a slot. It is a shadow-root sibling
    // of .screen, fixed to the viewport; its bottom is set from the
    // strip height in _updateScrollPill.
    this._scrollPillEl = document.createElement("button");
    this._scrollPillEl.className = "scroll-pill";
    this._scrollPillEl.setAttribute("data-action", "scroll-more");
    this._scrollPillEl.setAttribute("aria-label", "Scroll to more timers");
    this._scrollPillEl.textContent = "↓";
    this._scrollPillEl.hidden = true;
    this.shadowRoot.appendChild(this._scrollPillEl);
    if (
      typeof window !== "undefined" &&
      window.location &&
      typeof window.location.search === "string"
    ) {
      this._recipeContext = readBackRecipeContext(window.location.search);
    }
    if (this._scrollBound === null && typeof this._screenEl.addEventListener === "function") {
      this._scrollBound = () => {
        this._updateScrollPill();
        this._scheduleScrollSave();
      };
      this._screenEl.addEventListener("scroll", this._scrollBound);
    }
    // The pill also re-evaluates when the screen itself changes size
    // (TIMER-7): _sync runs while the Lovelace view is still sizing
    // the card, so the first _updateScrollPill can measure a screen
    // that is still at its minimum height — and nothing else fires
    // when the view later grows the card to its full height. The
    // observer fires on that growth (and on dial adds/removes), and
    // it fires once on observe, so the pill is correct from the first
    // settled layout. (No ResizeObserver in node tests: skipped.)
    if (
      this._screenResizeObserver === null &&
      typeof ResizeObserver !== "undefined"
    ) {
      this._screenResizeObserver = new ResizeObserver(() =>
        this._updateScrollPill()
      );
      this._screenResizeObserver.observe(this._screenEl);
    }
  }

  // -- sync: keep one stable DOM node per timer -------------------------
  //
  // Dials are never rebuilt wholesale: each timer owns one .dial element,
  // keyed by timer id, and sync only creates, updates, moves or removes
  // nodes. That is what makes adds, removals and re-sorts animate instead
  // of flashing — a re-render would destroy the nodes mid-animation.

  _sync() {
    if (!this._screenEl) return;
    this._applyDevice();
    // Re-evaluate the gates on every sync: core may have loaded since
    // the last hass push, and _refreshGates drops newly-mismatched
    // timers from this._timers in place (the next hass push re-derives
    // from the raw states, so nothing is lost permanently).
    this._timers = this._refreshGates(this._timers);
    const gateError = this._coreMismatch || this._bridgeMismatch;
    if (this._gateMismatchEl) {
      if (gateError) {
        this._gateMismatchEl.textContent = gateError;
        this._gateMismatchEl.hidden = false;
      } else {
        this._gateMismatchEl.textContent = "";
        this._gateMismatchEl.hidden = true;
      }
    }
    const now = Date.now();
    // Pin + arrival overrides (D7.3 / T-IMPL-20/23): read once per
    // mount, cleared after reading — a later mount never re-pins.
    if (!this._overridesRead) {
      this._overridesRead = true;
      if (typeof sessionStorage !== "undefined") {
        this._pinTimerId = readTimersPin(sessionStorage);
        this._arrivalHome = readTimersArrival(sessionStorage);
      }
    }
    const live = new Set(this._timers.map((t) => t.timer_id));
    const running = this._timers.filter(
      (t) => t.state === "active" || t.state === "paused"
    );
    const finished = this._timers.filter(
      (t) => t.state === "finished" || t.state === "ringing"
    );
    const ringing = this._timers.filter((t) => t.state === "ringing");

    // Which names are duplicated (TIMER-11): only those timers get a
    // "started ..." line, so a lone timer stays uncluttered.
    const counts = new Map();
    for (const t of this._timers) {
      counts.set(t.name, (counts.get(t.name) || 0) + 1);
    }
    this._duplicateNames = new Set(
      [...counts.entries()].filter(([, n]) => n > 1).map(([name]) => name)
    );
    // Cache each timer's start time on first sight: for a paused timer
    // the exact start is only derivable while it is still active.
    for (const t of this._timers) {
      if (!this._startedAtMs.has(t.timer_id)) {
        const ms = startedAtMs(t, now, null);
        if (ms) this._startedAtMs.set(t.timer_id, ms);
      }
    }

    // Measure positions before moving anything, so a re-sort can be
    // animated back from where each dial was (FLIP).
    const before = new Map();
    for (const [id, el] of this._dialEls) {
      before.set(id, el.getBoundingClientRect());
    }

    // The pinned timer (PIP tap) renders first, with a "Just started"
    // marker — and appears once, not also in the sorted list.
    const dialOrder = [...running];
    if (this._pinTimerId) {
      const pinnedIdx = dialOrder.findIndex((t) => t.timer_id === this._pinTimerId);
      if (pinnedIdx > 0) {
        const [pinned] = dialOrder.splice(pinnedIdx, 1);
        dialOrder.unshift(pinned);
      }
    }
    for (const timer of dialOrder) {
      let el = this._dialEls.get(timer.timer_id);
      if (!el) {
        el = this._buildDial(timer);
        this._dialEls.set(timer.timer_id, el);
        el.classList.add("entering");
        setTimeout(() => el.classList.remove("entering"), ENTER_ANIMATION_MS + 60);
      }
      this._updateDial(el, timer, now);
      this._screenEl.appendChild(el);
    }
    for (const [id, el] of [...this._dialEls]) {
      if (!live.has(id) || finished.some((t) => t.timer_id === id)) {
        // A timer that finished moves out of the dial grid and into
        // the finished section (its dial is removed here; the row is
        // built below).
        this._dialEls.delete(id);
        el.remove();
      }
    }
    // Grid order is DOM order: the finished section sits after the
    // dials, so move it to the end (it was built before any dial
    // existed, and appendChild would otherwise leave it above them).
    this._screenEl.appendChild(this._finishedSectionEl);

    // Finished section: one compact row per finished/ringing timer,
    // in the same sort order (most-recently-finished first).
    for (const timer of finished) {
      let row = this._rowEls.get(timer.timer_id);
      if (!row) {
        row = this._buildFinishedRow(timer);
        this._rowEls.set(timer.timer_id, row);
      }
      this._updateFinishedRow(row, timer, now);
      this._finishedRowsEl.appendChild(row);
    }
    for (const [id, row] of [...this._rowEls]) {
      if (!live.has(id) || running.some((t) => t.timer_id === id)) {
        this._rowEls.delete(id);
        row.remove();
      }
    }
    const finishedCount = finished.length;
    this._finishedSectionEl.hidden = finishedCount === 0;
    if (finishedCount > 0) {
      // Collapse rule (D1.2): forced expanded while any timer is
      // ringing (a ringing timer must be visible); on arrival expanded
      // only when nothing is running or paused; otherwise keep the
      // user's toggle state. The ringing force is DERIVED, not stored:
      // writing it into _finishedOpen made the section stick open after
      // the ringing window ended (rig: Finished (1) still expanded 90 s
      // after the ring, Pasta running — D1.2 says collapsed by default
      // whenever anything runs). Once the window ends the section falls
      // back to the default (collapsed while anything runs) unless the
      // user opened it.
      let open;
      if (ringing.length > 0) open = true;
      else if (this._finishedOpen === null) open = running.length === 0;
      else open = this._finishedOpen === true;
      this._finishedRowsEl.hidden = !open;
      const title = this._finishedHeaderEl.querySelector(".finished-title");
      if (title) title.textContent = `Finished (${finishedCount})`;
      const chevron = this._finishedHeaderEl.querySelector(".finished-chevron");
      if (chevron) chevron.textContent = open ? "▴" : "▾";
      this._finishedHeaderEl.setAttribute("aria-expanded", open ? "true" : "false");
    }

    // Back bar (TIMER-10 / T-IMPL-18): built ONLY when a recipe
    // context exists, removed when it does not — a plain load has
    // zero DOM residue of it.
    if (this._recipeContext && !this._backBarEl) {
      const bar = document.createElement("div");
      bar.className = "back-bar";
      bar.innerHTML =
        '<button data-action="back-recipe">← Back to recipe</button>';
      this._backBarEl = bar;
      // First after the strip: the bar is the first grid row.
      this._screenEl.insertBefore(bar, this._screenEl.children[1] || null);
    } else if (!this._recipeContext && this._backBarEl) {
      this._backBarEl.remove();
      this._backBarEl = null;
    }

    // Add-timer row + fallback chips (T-IMPL-10/12) + empty hints.
    this._updateAddTimerRow();
    this._updateEmptyHints();

    this._emptyEl.hidden = this._timers.length > 0;
    if (gateError) {
      // A gate error replaces the timer UI: no empty-state hints and
      // no add-timer chips while the bridge/core contract is mismatched.
      this._emptyEl.hidden = true;
      if (this._addTimerRowEl) this._addTimerRowEl.hidden = true;
    }
    this._screenEl.classList.remove("cols-1", "cols-2", "cols-3");
    const device = this._device;
    this._screenEl.classList.add(
      `cols-${gridColumnsFor(
        device ? device.category : undefined,
        device ? device.orientation : "landscape",
        typeof window !== "undefined" ? window.innerWidth : 0
      )}`
    );
    this._flip(before);
    this._fitTimes();
    this._updateTimerBadge();
    this._updateScrollPill();
    // Arrival overrides suppress the saved-scroll restore: the pin
    // scrolls into view (= top) and Home forces scrollY 0.
    if (this._pinTimerId || this._arrivalHome) {
      if (this._screenEl) this._screenEl.scrollTop = 0;
      if (typeof window !== "undefined" && typeof window.scrollTo === "function") {
        window.scrollTo(0, 0);
      }
      this._scrollRestored = true;
    } else {
      this._restoreScroll();
    }
    // A chip tap scrolls the new dial into view and pulses it once.
    if (this._presetBeforeIds) {
      const beforeIds = this._presetBeforeIds;
      this._presetBeforeIds = null;
      for (const timer of dialOrder) {
        if (!beforeIds.has(timer.timer_id)) {
          const el = this._dialEls.get(timer.timer_id);
          if (el) {
            el.classList.add("pulse");
            setTimeout(() => el.classList.remove("pulse"), 700);
            // Scroll AFTER the dial-in entry animation settles, not
            // immediately: while .entering scales the dial (0.94) its
            // measured top sits ~16px below the settled top, so an
            // immediate scrollIntoView lands 16px too far and clips the
            // name (rig: name top -16px at 844x390, drifting -10 to -16
            // as the 240ms animation runs). ENTER_ANIMATION_MS + 60 is
            // when the entering class is removed (same delay as the
            // _buildDial cleanup), so the layout is settled by then.
            // block: "start" (not "nearest"): a dial taller than the
            // viewport is clipped by "nearest", which scrolls the minimum
            // to expose the bottom. "start" aligns the dial top with the
            // view top so the name is fully visible; instant so the
            // position is settled, not mid-animation, when read.
            setTimeout(() => {
              if (typeof el.scrollIntoView === "function") {
                el.scrollIntoView({ block: "start", behavior: "auto" });
              }
            }, ENTER_ANIMATION_MS + 60);
          }
          break;
        }
      }
    }
  }

  // "+ Add timer" row + fallback chips (T-IMPL-10/12).
  //
  // Non-hub: a persistent "+ Add timer" button at the top while timers
  // run (it toggles the chip row below it, above the dials); when
  // there are no timers the chips show directly — creation stays
  // one tap from the empty state. The row also carries the add-time
  // voice hint while timers run (TIMER-14).
  // Wall fallback (D4, LOCKED): when voice is provably unavailable
  // the wall shows the same 5/10/15/30 chips plus the plain line
  // "Voice isn't available right now — tap a time to start a timer",
  // whether or not timers run (§6 item 6 — the ruling's purpose is
  // that the wall is never dead when voice is down). When voice
  // recovers the chips disappear and the voice-only state returns.
  // The wall voice hints hide while the fallback shows (they would
  // contradict the "Voice isn't available" line).
  _updateAddTimerRow() {
    if (!this._addTimerRowEl) return;
    const isHub = this._identity !== null && this._identity.kind === "wall";
    const hasTimers = this._timers.length > 0;
    const fallback = isHub && this._voiceDown;
    const showRow = fallback || !isHub;
    this._addTimerRowEl.hidden = !showRow;
    if (!showRow) return;
    if (fallback) {
      this._addTimerButtonEl.hidden = true;
      this._addTimerHintEl.hidden = true;
      this._fallbackLineEl.hidden = false;
      this._chipsEl.classList.add("show");
    } else {
      this._fallbackLineEl.hidden = true;
      if (hasTimers) {
        this._addTimerButtonEl.hidden = false;
        this._addTimerButtonEl.textContent = this._chipsOpen
          ? "Close"
          : "+ Add timer";
        this._addTimerButtonEl.setAttribute(
          "aria-expanded",
          this._chipsOpen ? "true" : "false"
        );
        this._addTimerHintEl.hidden = false;
        this._addTimerHintEl.textContent =
          "To add time, tell the kitchen speaker: “Okay Nabu, add 5 more minutes to the pasta timer”";
        if (this._chipsOpen) this._chipsEl.classList.add("show");
        else this._chipsEl.classList.remove("show");
      } else {
        this._addTimerButtonEl.hidden = true;
        this._addTimerHintEl.hidden = true;
        this._chipsEl.classList.add("show");
      }
    }
    this._emptyEl.classList.toggle("voice-down", fallback);
  }

  // Empty-state voice hints (D4 / contract-hints): the wall gets the
  // verbatim TIMER-4 lines; phone/tablet/desktop name the kitchen
  // speaker (the phone has no wake word listening).
  _updateEmptyHints() {
    if (!this._emptyEl) return;
    const hint1 = this._emptyEl.querySelector(".voice-hint-1");
    const hint2 = this._emptyEl.querySelector(".voice-hint-2");
    if (!hint1 || !hint2) return;
    const isHub = this._identity !== null && this._identity.kind === "wall";
    if (isHub) {
      hint1.textContent =
        "Say “Okay Nabu, set a 10 minute pasta timer”";
      hint2.textContent =
        "To add time, say “Okay Nabu, add 5 more minutes to the pasta timer”";
    } else {
      hint1.textContent =
        "Or tell the kitchen speaker: “Okay Nabu, set a 10 minute pasta timer”";
      hint2.textContent =
        "To add time, tell the kitchen speaker: “Okay Nabu, add 5 more minutes to the pasta timer”";
    }
  }

  // -- scroll persistence (DESIGN-7) --------------------------------------
  //
  // Save on scroll (debounced 150ms). Restore once per mount, retried
  // on later syncs while the timer list is still growing: a restore
  // that cannot reach its target yet stays pending instead of giving
  // up. If the user moves anything after a restore attempt, their
  // scroll wins — the pending restore is abandoned, never yanking
  // them back.
  _scheduleScrollSave() {
    if (this._scrollSaveTimer !== null) return;
    if (typeof setTimeout !== "function") return;
    this._scrollSaveTimer = setTimeout(() => {
      this._scrollSaveTimer = null;
      this._saveScroll();
    }, 150);
  }

  _saveScroll() {
    if (typeof sessionStorage === "undefined") return;
    const state = {
      page: typeof window !== "undefined" ? sanitizeTimerScrollTop(window.scrollY) : 0,
      screen: this._screenEl ? sanitizeTimerScrollTop(this._screenEl.scrollTop) : 0,
    };
    try {
      sessionStorage.setItem(TIMER_SCROLL_KEY, serializeTimerScroll(state));
    } catch (_err) {
      // Storage full or blocked: scrolling still works, it just is
      // not remembered. Never throw from a cosmetic feature.
    }
  }

  _readSavedScroll() {
    if (typeof sessionStorage === "undefined") return null;
    try {
      return parseTimerScroll(sessionStorage.getItem(TIMER_SCROLL_KEY));
    } catch (_err) {
      return null;
    }
  }

  _currentScrollPositions() {
    return {
      page: typeof window !== "undefined" ? window.scrollY || 0 : 0,
      screen: this._screenEl ? this._screenEl.scrollTop : 0,
    };
  }

  _restoreScroll() {
    if (this._scrollRestored) return;
    const state = this._readSavedScroll();
    if (!state) {
      this._scrollRestored = true;
      return;
    }
    if (this._restoreWritten) {
      const now = this._currentScrollPositions();
      if (now.page !== this._restoreWritten.page || now.screen !== this._restoreWritten.screen) {
        this._scrollRestored = true;
        return;
      }
    }
    let done = true;
    if (typeof window !== "undefined" && state.page > 0 && typeof document !== "undefined") {
      const maxPage = Math.max(
        0,
        (document.documentElement ? document.documentElement.scrollHeight : 0) - window.innerHeight
      );
      window.scrollTo(0, state.page);
      if (window.scrollY < Math.min(state.page, maxPage)) done = false;
    }
    if (this._screenEl && state.screen > 0) {
      const max = Math.max(0, this._screenEl.scrollHeight - this._screenEl.clientHeight);
      this._screenEl.scrollTop = state.screen;
      if (this._screenEl.scrollTop < Math.min(state.screen, max)) done = false;
    }
    this._restoreWritten = this._currentScrollPositions();
    this._scrollRestored = done;
  }

  // Scroll indicator (TIMER-7 / P8-4 / P8-5, Wave 1): the floating
  // "↓" pill shows only while at least one DIAL sits below the fold
  // — not whenever the screen's rect extends below it. The old gate
  // fired whenever the controls row overflowed (e.g. with 1 timer),
  // so the pill claimed "More timers below" when there were none.
  // The fold is the lower of the viewport bottom and the screen's own
  // bottom: in document-scroll mode the screen runs past the
  // viewport, in internal-scroll mode the screen is shorter than the
  // viewport and its own bottom is the fold. Either way the
  // getBoundingClientRect() of a dial already accounts for scrolling,
  // so one check covers both scroll modes — and once the user has
  // scrolled to the bottom the last dial is fully visible and the
  // pill hides itself. (No window in node tests: the pill stays
  // hidden and the logic is skipped.)
  // Timers count badge (D-N1 / N-IMPL-5): rendered from this._timers
  // (ACTIVE + PAUSED count, amber last-60 s / red finished). Wave 1:
  // it always shows while timers exist — the pill moved out of the
  // strip, so the two no longer share a slot and no reconciliation
  // is needed. _updateTimerBadge is called from _sync directly.
  _updateTimerBadge() {
    if (!this._timerBadgeEl) return;
    const badge = timerBadgeState(this._timers);
    if (!badge) {
      this._timerBadgeEl.hidden = true;
      this._timerBadgeEl.textContent = "";
      this._timerBadgeEl.className = "timer-badge";
      return;
    }
    this._timerBadgeEl.hidden = false;
    this._timerBadgeEl.textContent = String(badge.count);
    this._timerBadgeEl.className =
      "timer-badge" + (badge.tone === "count" ? "" : ` ${badge.tone}`);
    this._timerBadgeEl.setAttribute("aria-label", `${badge.count} timers`);
  }

  _updateScrollPill() {
    if (!this._screenEl || !this._scrollPillEl) return;
    const el = this._screenEl;
    let show = false;
    if (typeof window !== "undefined" && window.innerHeight) {
      const rect = el.getBoundingClientRect();
      const inView = rect.top < window.innerHeight && rect.bottom > 0;
      if (inView) {
        const foldY = Math.min(window.innerHeight, rect.bottom);
        for (const dialEl of this._dialEls.values()) {
          const r = dialEl.getBoundingClientRect();
          if (
            r &&
            typeof r.bottom === "number" &&
            r.bottom > foldY + 4
          ) {
            show = true;
            break;
          }
        }
      }
    }
    this._scrollPillEl.hidden = !show;
    if (show) {
      // Above the strip: measured strip height + 16px (the strip is
      // fixed chrome; the pill must clear it).
      let stripHeight = 0;
      if (this._navStripEl && typeof this._navStripEl.getBoundingClientRect === "function") {
        const r = this._navStripEl.getBoundingClientRect();
        if (r && typeof r.height === "number") stripHeight = r.height;
      }
      this._scrollPillEl.style.bottom = `${stripHeight + 16}px`;
    }
  }

  _onScrollPillTap() {
    // Scroll down by most of a viewport, in whichever element scrolls.
    const amount =
      typeof window !== "undefined" && window.innerHeight
        ? Math.floor(window.innerHeight * 0.8)
        : 400;
    if (
      this._screenEl &&
      this._screenEl.scrollHeight > this._screenEl.clientHeight
    ) {
      this._screenEl.scrollTop += amount;
      return;
    }
    if (typeof window !== "undefined" && typeof window.scrollBy === "function") {
      window.scrollBy(0, amount);
    }
  }

  _flip(before) {
    for (const [id, el] of this._dialEls) {
      if (el.classList.contains("entering")) continue;
      const prev = before.get(id);
      if (!prev) continue;
      const rect = el.getBoundingClientRect();
      const dx = prev.left - rect.left;
      const dy = prev.top - rect.top;
      if ((!dx && !dy) || typeof el.animate !== "function") continue;
      el.animate(
        [
          { transform: `translate(${dx}px, ${dy}px)` },
          { transform: "translate(0, 0)" },
        ],
        { duration: 300, easing: "ease" }
      );
    }
  }

  _buildDial(timer) {
    // Wave 1 (D1): a vertical stack — name block above the ring, the
    // countdown centred INSIDE the ring button, state/hint lines
    // below the ring, then the action button. Finished timers never
    // get a dial (they live in the finished section as rows), so
    // there is no compact-row markup here any more. The whole .dial
    // cell carries data-action="dial" so the name and sub-lines are
    // tappable too (the old overlay missed name taps).
    const el = document.createElement("div");
    el.className = "dial";
    el.dataset.timerId = timer.timer_id;
    el.setAttribute("data-action", "dial");
    el.setAttribute("data-timer", timer.timer_id);
    el.innerHTML = `
      <div class="dial-name" data-name="${escapeHtml(timer.timer_id)}"></div>
      <button class="dial-button" data-action="dial" data-timer="${escapeHtml(timer.timer_id)}" aria-label="${escapeHtml(timer.name)} timer">
        <svg viewBox="0 0 200 200" role="img" aria-label="${escapeHtml(timer.name)}">
          <circle class="track" cx="100" cy="100" r="88" />
          <circle class="ring" data-ring="${escapeHtml(timer.timer_id)}" cx="100" cy="100" r="88" transform="rotate(-90 100 100)" />
        </svg>
        <div class="time" data-remaining="${escapeHtml(timer.timer_id)}"></div>
      </button>
      <div class="dial-sub">
        <div class="state" data-state="${escapeHtml(timer.timer_id)}"></div>
        <div class="started" data-started="${escapeHtml(timer.timer_id)}"></div>
        <div class="hint" data-hint="${escapeHtml(timer.timer_id)}"></div>
      </div>
      <div class="controls" data-controls="${escapeHtml(timer.timer_id)}"></div>
      <div class="error" data-error="${escapeHtml(timer.timer_id)}"></div>`;
    el._refs = {
      button: el.querySelector(".dial-button"),
      ring: el.querySelector(`[data-ring="${timer.timer_id}"]`),
      name: el.querySelector(`[data-name="${timer.timer_id}"]`),
      time: el.querySelector(`[data-remaining="${timer.timer_id}"]`),
      state: el.querySelector(`[data-state="${timer.timer_id}"]`),
      started: el.querySelector(`[data-started="${timer.timer_id}"]`),
      hint: el.querySelector(`[data-hint="${timer.timer_id}"]`),
      controls: el.querySelector(`[data-controls="${timer.timer_id}"]`),
      error: el.querySelector(`[data-error="${timer.timer_id}"]`),
    };
    el._controlsKind = "";
    el._armState = false;
    return el;
  }

  _buildFinishedRow(timer) {
    // A finished or ringing timer as a compact row (TIMER-7, Wave 1):
    // name + sub-line on the left, Restart on the right. The row
    // itself dismisses on tap (same as a dial tap on a finished
    // timer); Restart is its own button inside it.
    const el = document.createElement("div");
    el.className = "finished-row";
    el.dataset.timerId = timer.timer_id;
    el.setAttribute("data-action", "dial");
    el.setAttribute("data-timer", timer.timer_id);
    el.setAttribute("role", "button");
    el.innerHTML = `
      <div class="finished-text">
        <span class="finished-name" data-finished-name="${escapeHtml(timer.timer_id)}"></span>
        <span class="finished-sub" data-finished-sub="${escapeHtml(timer.timer_id)}"></span>
      </div>
      <button class="finished-restart" data-action="restart" data-timer="${escapeHtml(timer.timer_id)}" aria-label="Restart ${escapeHtml(timer.name)} timer">Restart</button>`;
    el._refs = {
      name: el.querySelector(`[data-finished-name="${timer.timer_id}"]`),
      sub: el.querySelector(`[data-finished-sub="${timer.timer_id}"]`),
    };
    return el;
  }

  _updateFinishedRow(row, timer, now) {
    const refs = row._refs;
    const isRinging = timer.state === "ringing";
    refs.name.textContent = timer.name;
    const finishedMs = timer.finishedAt ? Date.parse(timer.finishedAt) : null;
    refs.sub.textContent = isRinging
      ? "Ringing · Tap to dismiss"
      : `${formatElapsedSince(finishedMs, now)} · Tap to dismiss`;
    row.setAttribute(
      "aria-label",
      isRinging
        ? `${timer.name} timer, ringing, tap to dismiss`
        : `${timer.name} timer, finished, tap to dismiss`
    );
    row.classList.toggle("ringing", isRinging);
  }

  _updateDial(el, timer, now) {
    const refs = el._refs;
    const isPaused = timer.state === "paused";
    const left = remainingSeconds(timer, now);
    // The "soon" flip follows the SENSOR's remaining attribute
    // (T-IMPL-13 / OQ-5), not the displayed countdown.
    const sensorLeft = sensorRemaining(timer);
    const isSoon =
      !isPaused &&
      timer.state === "active" &&
      sensorLeft > 0 &&
      sensorLeft <= FINISHING_SOON_SECONDS;

    el.classList.toggle("paused", isPaused);
    el.classList.toggle("soon", isSoon);
    // A dial is only ever running or paused now (finished timers are
    // rows); the "finished" class stays off so the ring keeps the
    // running colour.
    el.classList.toggle("finished", false);

    // The dial itself is the primary tap target — the biggest, most
    // forgiving thing on the screen. It pauses a running timer and
    // resumes a paused one, so the most common action for each state
    // is one big tap, never a small button.
    refs.button.setAttribute(
      "aria-label",
      `${timer.name} timer, tap to ${isPaused ? "resume" : "pause"}`
    );
    el.setAttribute(
      "aria-label",
      `${timer.name} timer, tap to ${isPaused ? "resume" : "pause"}`
    );

    // TIMER-1: the full name, wrapping to at most two lines at >=20px
    // on the hub (CSS line-clamp).
    refs.name.textContent = timer.name;
    refs.time.textContent = formatRemaining(left);
    // Soon word (D1): the state line reads exactly
    // "Soon · Tap to pause" and the hint is empty; running shows no
    // state line and "Tap to pause"; paused shows "Paused" +
    // "Tap to resume".
    if (isSoon) {
      refs.state.textContent = "Soon · Tap to pause";
      refs.hint.textContent = "";
    } else if (isPaused) {
      refs.state.textContent = "Paused";
      refs.hint.textContent = "Tap to resume";
    } else {
      refs.state.textContent = "";
      refs.hint.textContent = "Tap to pause";
    }
    // TIMER-11: duplicates get their start time, so two timers with
    // the same name are distinguishable. The PIP-pinned timer shows
    // "Just started" instead (D7.3).
    if (timer.timer_id === this._pinTimerId) {
      refs.started.textContent = "Just started";
    } else if (this._duplicateNames.has(timer.name)) {
      const started = startedAtMs(
        timer,
        now,
        this._startedAtMs.get(timer.timer_id)
      );
      refs.started.textContent = started
        ? `Started ${formatStartTime(started)}`
        : "";
    } else {
      refs.started.textContent = "";
    }

    refs.ring.style.strokeDashoffset = String(
      RING_CIRCUMFERENCE * (1 - progressFraction(timer, now))
    );
    refs.ring.classList.toggle("paused", isPaused);

    const kind = "running";
    if (el._controlsKind !== kind) {
      el._controlsKind = kind;
      refs.controls.innerHTML =
        `<button class="cancel" data-action="cancel" data-timer="${escapeHtml(timer.timer_id)}" aria-label="Cancel ${escapeHtml(timer.name)} timer">Cancel</button>`;
    }
    this._refreshCancelButton(el, timer);
    if (this._error[timer.timer_id]) {
      refs.error.textContent = this._error[timer.timer_id];
    } else {
      refs.error.textContent = "";
    }
    this._setBusy(el, this._busy.has(timer.timer_id));
  }

  _refreshCancelButton(el, timer) {
    if (!el) return;
    const button = el.querySelector(".cancel");
    if (!button) return;
    const armed = this._arming.has(timer.timer_id);
    // Only rewrite the button when the armed state changed: rewriting
    // restarts the arm-bar drain animation, and _updateDial runs on
    // every sync — a rewrite here would restart the drain whenever
    // any timer changed.
    if (el._armState === armed) {
      button.classList.toggle("armed", armed);
      return;
    }
    el._armState = armed;
    button.classList.toggle("armed", armed);
    button.innerHTML = armed
      ? 'Tap again to cancel<span class="arm-bar"></span>'
      : "Cancel";
  }

  _setBusy(el, busy) {
    for (const button of el.querySelectorAll(".controls button")) {
      button.disabled = busy;
    }
  }

  // -- ticking: text and ring move in place, no re-render ----------------

  _updateTimes() {
    if (!this._screenEl) return;
    const now = Date.now();
    for (const timer of this._timers) {
      const el = this._dialEls.get(timer.timer_id);
      if (el) {
        const left = remainingSeconds(timer, now);
        el._refs.time.textContent = formatRemaining(left);
        el._refs.ring.style.strokeDashoffset = String(
          RING_CIRCUMFERENCE * (1 - progressFraction(timer, now))
        );
        // The "soon" flip follows the sensor's remaining attribute
        // (T-IMPL-13 / OQ-5), not the displayed countdown.
        const sensorLeft = sensorRemaining(timer);
        const isSoon =
          timer.state === "active" &&
          sensorLeft > 0 &&
          sensorLeft <= FINISHING_SOON_SECONDS;
        el.classList.toggle("soon", isSoon);
        if (isSoon) {
          el._refs.state.textContent = "Soon · Tap to pause";
          el._refs.hint.textContent = "";
        } else if (timer.state === "paused") {
          el._refs.state.textContent = "Paused";
          el._refs.hint.textContent = "Tap to resume";
        } else {
          el._refs.state.textContent = "";
          el._refs.hint.textContent = "Tap to pause";
        }
        continue;
      }
      const row = this._rowEls.get(timer.timer_id);
      if (row) {
        // TIMER-3: the elapsed-since-finish counts up on the tick.
        this._updateFinishedRow(row, timer, now);
      }
    }
    // One batched fit pass per tick: reads happen after all the
    // writes above, so the tick costs one forced layout, not one per
    // dial (T-IMPL-7).
    this._fitTimes();
  }

  // Time fit (T-IMPL-7 / D1.4): the rendered countdown must fit
  // inside its ring (time width <= ring width - 16px). Measure at the
  // CSS size first — a shorter time or a wider ring may fit again at
  // full size — then step the font down the TIME_FIT_STEPS ladder
  // (72→64→56→48→44→40) until it fits, never below the 40px floor
  // (TIMER-6). No layout engine (node tests: no scrollWidth) means
  // no measurement — skip silently.
  _fitTimes() {
    for (const el of this._dialEls.values()) {
      const refs = el._refs;
      if (!refs.time || !refs.button) continue;
      if (typeof refs.time.scrollWidth !== "number") continue;
      if (typeof refs.button.getBoundingClientRect !== "function") continue;
      const ringW = refs.button.getBoundingClientRect().width || 0;
      if (ringW <= 0) continue;
      const maxW = ringW - 16;
      if (refs.time.style.fontSize) refs.time.style.fontSize = "";
      if (refs.time.scrollWidth <= maxW) continue;
      const current =
        typeof getComputedStyle === "function"
          ? parseFloat(getComputedStyle(refs.time).fontSize) || 72
          : 72;
      for (const size of TIME_FIT_STEPS) {
        if (size >= current) continue;
        refs.time.style.fontSize = `${size}px`;
        if (refs.time.scrollWidth <= maxW) break;
      }
      if (refs.time.scrollWidth > maxW) refs.time.style.fontSize = "40px";
    }
  }

  // -- controls -----------------------------------------------------------

  async _control(timerId, service, data) {
    if (this._busy.has(timerId)) return false;
    this._busy.add(timerId);
    const el = this._dialEls.get(timerId);
    if (el) this._setBusy(el, true);
    let ok = true;
    try {
      await this._hass.callService("satellite_timers", service, {
        timer_id: timerId,
        ...(data || {}),
      });
      delete this._error[timerId];
    } catch (err) {
      ok = false;
      const message = String((err && err.message) || err || "");
      this._error[timerId] = /service.*not found/i.test(message)
        ? "Timer controls need the updated bridge"
        : "Couldn't do that — try voice";
      setTimeout(() => {
        delete this._error[timerId];
        const dial = this._dialEls.get(timerId);
        if (dial) dial._refs.error.textContent = "";
      }, 6000);
    } finally {
      this._busy.delete(timerId);
      const dial = this._dialEls.get(timerId);
      if (dial) {
        this._setBusy(dial, false);
        dial._refs.error.textContent = this._error[timerId] || "";
      }
    }
    return ok;
  }

  _clearArm(timerId) {
    // TIMER-9: any other tap clears a pending Cancel arm, so an armed
    // Cancel can never fire later by surprise.
    if (!this._arming.has(timerId)) return;
    clearTimeout(this._arming.get(timerId));
    this._arming.delete(timerId);
    this._refreshCancelButton(this._dialEls.get(timerId), { timer_id: timerId });
  }

  _showUndoToast(timer, kind) {
    // TIMER-13 / D6: cancelling or dismissing offers a 5 s Undo.
    // Undo re-creates the timer with the same name and the REMAINING
    // time at cancel (F-T1) — a 10-min timer cancelled with 3 min
    // left comes back as a 3-min timer, not a fresh 10-min one. Min
    // 1 s: a timer cancelled at 0 s remaining still comes back. A
    // paused timer comes back paused (D6) — the bridge restores it
    // atomically (restore_timer), so the sensor never observes it as
    // active. A dismissed finished timer comes back finished, with
    // its original finished_at and WITHOUT re-ringing the satellite
    // (restore_finished_timer).
    // F-T17: the toast is position: fixed and its bottom is set from
    // the measured strip height + 8px, so it can never sit behind
    // the fixed strip.
    if (!this._toastEl) return;
    if (this._toastTimer) {
      clearTimeout(this._toastTimer);
      this._toastTimer = null;
    }
    if (kind === "dismissed") {
      this._undoTimer = {
        kind: "dismissed",
        name: timer.name,
        totalSeconds: timer.duration,
        finishedAt: timer.finishedAt,
        startedViaService: timer.startedViaService,
        startedAt: timer.startedAt,
      };
    } else {
      const remaining = Math.max(1, remainingSeconds(timer, Date.now()));
      this._undoTimer = {
        kind: "cancelled",
        name: timer.name,
        duration: remaining,
        paused: timer.state === "paused",
      };
    }
    this._toastEl.innerHTML = "";
    const message = document.createElement("span");
    message.textContent =
      kind === "dismissed"
        ? `${timer.name} timer dismissed`
        : `${timer.name} timer cancelled`;
    const undo = document.createElement("button");
    undo.setAttribute("data-action", "undo-cancel");
    undo.textContent = "Undo";
    this._toastEl.appendChild(message);
    this._toastEl.appendChild(undo);
    this._toastEl.hidden = false;
    let stripHeight = 0;
    if (
      this._navStripEl &&
      typeof this._navStripEl.getBoundingClientRect === "function"
    ) {
      const r = this._navStripEl.getBoundingClientRect();
      if (r && typeof r.height === "number") stripHeight = r.height;
    }
    // No strip (desktop, strip hidden): the CSS 24px bottom stands.
    if (stripHeight > 0) {
      this._toastEl.style.bottom = `${stripHeight + 8}px`;
    }
    this._toastTimer = setTimeout(() => this._hideToast(), UNDO_TOAST_MS);
  }

  _hideToast() {
    if (this._toastTimer) {
      clearTimeout(this._toastTimer);
      this._toastTimer = null;
    }
    this._undoTimer = null;
    if (this._toastEl) this._toastEl.hidden = true;
  }

  async _onUndoCancel() {
    const undo = this._undoTimer;
    this._hideToast();
    if (!undo || !this._hass) return;
    try {
      if (undo.kind === "dismissed") {
        await this._hass.callService(
          "satellite_timers",
          "restore_finished_timer",
          {
            name: undo.name,
            total_seconds: undo.totalSeconds,
            finished_at: undo.finishedAt,
            started_via_service: Boolean(undo.startedViaService),
            ...(undo.startedAt ? { started_at: undo.startedAt } : {}),
          }
        );
      } else {
        await this._hass.callService("satellite_timers", "restore_timer", {
          name: undo.name,
          duration: undo.duration,
          paused: Boolean(undo.paused),
        });
      }
    } catch (err) {
      if (this._emptyErrorEl) {
        this._emptyErrorEl.textContent =
          "Couldn't bring the timer back — try voice";
        this._emptyErrorEl.hidden = false;
      }
    }
  }

  async _onPresetTap(minutes) {
    // TIMER-4: preset chips start a timer by duration. The name is
    // the duration ("10 minute timer"); the recipe name comes from
    // recipe chips instead (RECIPE-5 / BRIDGE-1). After the start,
    // _sync scrolls the new dial into view and pulses it once — the
    // ids seen before the tap tell it which dial is new.
    if (this._presetBusy || !this._hass) return;
    this._presetBusy = true;
    this._setPresetBusy(true);
    try {
      this._presetBeforeIds = new Set(this._timers.map((t) => t.timer_id));
      await this._hass.callService("satellite_timers", "start_timer", {
        name: `${minutes} minute timer`,
        duration: minutes * 60,
      });
      if (this._emptyErrorEl) this._emptyErrorEl.hidden = true;
    } catch (err) {
      this._presetBeforeIds = null;
      if (this._emptyErrorEl) {
        const message = String((err && err.message) || err || "");
        this._emptyErrorEl.textContent = /service.*not found/i.test(message)
          ? "Timer start needs the updated bridge — try voice"
          : "Couldn't start the timer — try voice";
        this._emptyErrorEl.hidden = false;
      }
    } finally {
      this._presetBusy = false;
      this._setPresetBusy(false);
    }
  }

  _setPresetBusy(busy) {
    if (!this._chipsEl) return;
    for (const button of this._chipsEl.querySelectorAll(
      '[data-action="preset"]'
    )) {
      button.disabled = busy;
    }
  }

  // -- navigation (NAV-1) -------------------------------------------------
  //
  // history.pushState + a location-changed dispatch — no reload. The
  // same mechanism the standalone nav strip and the recipe card use.
  // _lastNavigation is the test hook (also used by the standalone strip).
  // N-IMPL-6: tapping the already-active strip button (Timers, on
  // this card) is a no-op — no pushState, no location-changed.
  // R1: in-app navigation no longer carries ?kiosk — it is launch-only
  // (§4/§19). (The core SPA helper replaces this whole method in R4.)
  _navigate(path) {
    const target = String(path || "/family-hub/timers");
    if (target.split("?")[0].replace(/\/+$/, "") === "/family-hub/timers") return;
    const url = target;
    this._lastNavigation = url;
    if (typeof window === "undefined") return;
    try {
      if (window.history && typeof window.history.pushState === "function") {
        window.history.pushState(null, "", url);
      }
    } catch (_err) {
      // A pushState failure (a test stub, a file URL) never breaks
      // the tap; the location-changed dispatch below still runs.
    }
    if (typeof window.dispatchEvent === "function") {
      window.dispatchEvent(new CustomEvent("location-changed"));
    }
  }

  _onBackToRecipe() {
    // TIMER-10 + N-IMPL-21: return to the recipe at the same step, by
    // SPA navigation (history.pushState + location-changed — no
    // full-page reload, so the recipe card's session restores in
    // place). The recipe id and step come from the recipe card's
    // session state first (family-hub:recipe:state openId/step), the
    // URL ?from_recipe=<id>&step=<n> as the fallback; the recipe card
    // honours ?recipe=<id>&step=<n> as a deep link.
    if (typeof window === "undefined" || !window.location || !this._recipeContext) {
      return;
    }
    const path = window.location.pathname || "";
    const base = path.replace(/\/[^/]*$/, "") || "/family-hub";
    const step = this._recipeContext.step;
    const url =
      `${base}/recipes?recipe=${encodeURIComponent(this._recipeContext.recipeId)}` +
      (step !== null && step !== undefined
        ? `&step=${encodeURIComponent(step)}`
        : "");
    this._navigate(url);
  }

  _onClick(event) {
    const target = event.target.closest("[data-action]");
    if (!target) return;
    const action = target.dataset.action;

    if (action === "undo-cancel") {
      this._onUndoCancel();
      return;
    }
    if (action === "preset") {
      this._onPresetTap(Number(target.dataset.minutes) || 0);
      return;
    }
    if (action === "back-recipe") {
      this._onBackToRecipe();
      return;
    }
    if (action === "nav") {
      // NAV-1: a strip tap navigates (pushState + location-changed,
      // no reload) — the same mechanism the recipe card uses.
      this._navigate(target.dataset.path || "/family-hub");
      return;
    }
    if (action === "add-timer") {
      // T-IMPL-10: toggle the chip row below the button.
      this._chipsOpen = !this._chipsOpen;
      this._updateAddTimerRow();
      return;
    }
    if (action === "finished-toggle") {
      // Flip from the RENDERED state, not from _finishedOpen: while a
      // timer rings the section is forced open but _finishedOpen is
      // still null (the force is derived, not stored), so flipping
      // _finishedOpen alone would no-op on the first tap. During
      // ringing the force still wins on the next sync (D1.2: the
      // collapse never hides a ringing timer), so the tap takes effect
      // once the window ends.
      const currentlyOpen = this._finishedRowsEl ? !this._finishedRowsEl.hidden : false;
      this._finishedOpen = !currentlyOpen;
      this._sync();
      return;
    }
    if (action === "scroll-more") {
      this._onScrollPillTap();
      return;
    }

    const timerId = target.dataset.timer;
    const timer = this._timers.find((t) => t.timer_id === timerId);
    if (!timer) return;

    if (action === "dial") {
      // TIMER-9: a dial tap clears any pending Cancel arm first.
      this._clearArm(timerId);
      // One tap on the dial does the primary thing for the timer's
      // state: pause a running timer, resume a paused one, dismiss a
      // finished or ringing one. Adding time stays voice-only.
      // Dismissing offers the same 5 s Undo as cancelling (the old
      // dismiss path had no toast — F-T13 asymmetry).
      if (timer.state === "finished" || timer.state === "ringing") {
        this._control(timerId, "dismiss_timer").then((ok) => {
          if (ok) this._showUndoToast(timer, "dismissed");
        });
      }
      else if (timer.state === "paused") this._control(timerId, "resume_timer");
      else if (timer.state === "active") this._control(timerId, "pause_timer");
      return;
    }
    if (action === "cancel") {
      this._onCancelTap(timerId);
      return;
    }
    if (action === "restart") {
      this._clearArm(timerId);
      this._control(timerId, "restart_timer");
    }
  }

  async _onCancelTap(timerId) {
    // Cancel is destructive and the screen is tapped with wet hands, so a
    // single stray tap must never kill a timer: the first tap only arms
    // the button, and only a second tap within a few seconds cancels.
    if (this._arming.has(timerId)) {
      clearTimeout(this._arming.get(timerId));
      this._arming.delete(timerId);
      this._refreshCancelButton(this._dialEls.get(timerId), { timer_id: timerId });
      const timer = this._timers.find((t) => t.timer_id === timerId);
      const ok = await this._control(timerId, "cancel_timer");
      // TIMER-13: offer the Undo toast for 5 s after a real cancel.
      if (ok && timer) this._showUndoToast(timer);
      return;
    }
    const timeout = setTimeout(() => {
      this._arming.delete(timerId);
      const el = this._dialEls.get(timerId);
      if (el) this._refreshCancelButton(el, { timer_id: timerId });
    }, CANCEL_ARM_MS);
    this._arming.set(timerId, timeout);
    this._refreshCancelButton(this._dialEls.get(timerId), { timer_id: timerId });
  }
}

customElements.define("timer-screen", TimerScreenCard);

if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    displayName,
    formatRemaining,
    formatElapsedSince,
    formatStartTime,
    remainingSeconds,
    sensorRemaining,
    progressFraction,
    startedAtMs,
    discoverTimers,
    gridColumnsFor,
    voiceUnavailable,
    readTimersPin,
    readTimersArrival,
    CORE_RESOURCE_PATH,
    REQUIRED_CORE,
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
  };
}
