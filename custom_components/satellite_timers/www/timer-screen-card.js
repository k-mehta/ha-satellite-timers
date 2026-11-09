// Timer screen card (custom:timer-screen, bundled in the Satellite
// Timers bridge). One dial per timer, tap the dial for the primary
// action (pause / resume / dismiss), Cancel arms before it cancels.
//
// Phase 3 (consolidated spec rev 2, TIMER-1..15) changes, by spec ID:
//   TIMER-1  Full timer name, wrapped to two lines, never truncated,
//            never shrunk below 20px on the hub. The name is HTML text
//            (not SVG <text>, which cannot wrap), overlaid on the dial
//            so it can use the full grid-cell width. The flex row also
//            carries min-width: 0 — the 50-char unbroken name used to
//            overflow 4 sizes in true kiosk for exactly that reason.
//   TIMER-2  detectDevice follows the unified §3.7 rule as reordered
//            by P8-16: the exact 1920x1200 panel signature FIRST, in
//            EITHER orientation (1920x1200 landscape, or 1200x1920
//            portrait — the same panel rotated, O2-01), then tablet
//            user agents, then mobile UA, then width <768, then the
//            tablet band, then fine + width >=1024 -> desktop, then
//            coarse without fine -> mobile. The old rule 8 (touch +
//            width >=1600 + landscape -> hub) is DELETED: it existed
//            only to misclassify touchscreen laptops as the hub. Bare
//            width >=1600 + landscape -> hub is FORBIDDEN. (Live repro
//            is a §13 item: headless cannot present touch + fine
//            pointer simultaneously — see pointer_diag.mjs.)
//   TIMER-3  Finished timers show "Done — N min ago", counting up
//            from finished_at on the tick (same as HOME-3).
//   TIMER-4  Viewport-split creation: the hub is voice-only + a hint;
//            phone/tablet get on-screen preset chips (5/10/15/30 min)
//            that call satellite_timers.start_timer.
//   TIMER-5  Warm palette, all passing the §3.3 bars: running ring
//            #C2410C (4.96:1), soon/finished ring #B45309 (4.81:1),
//            track #757575 (4.41:1), error #B71C1C (6.29:1).
//   TIMER-6  Dial text minima (hub): name >=20px, state/hint >=18px,
//            time 72px. The time sizes to the grid cell, not the ring
//            diameter (OQ-2 decision: the 72px minimum holds at normal
//            hub dial sizes; below that the time scales with the cell,
//            never below 40px, because the ring is decorative and the
//            text is the content).
//   TIMER-7  NO cap: past 12 timers the view becomes a scroll
//            container with a VISIBLE scroll indicator, and finished
//            timers collapse to compact full-width rows.
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
//            the timer via start_timer with the same name and the
//            REMAINING time at cancel (min 1 s) — not the full
//            duration (F-T1).
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

// A timer is "finishing soon" in its last minute: the ring turns warm and
// pulses gently so it catches the eye without shouting.
const FINISHING_SOON_SECONDS = 60;
// How long the Cancel button stays armed ("Tap again to cancel") after the
// first tap, before it disarms itself.
const CANCEL_ARM_MS = 4000;
// How long the cancel Undo toast stays up (TIMER-13).
const UNDO_TOAST_MS = 5000;
// Entrance animation length for a newly added dial.
const ENTER_ANIMATION_MS = 240;
// Above this many timers the view degrades gracefully (TIMER-7): the
// screen scrolls, a scroll indicator shows, finished timers collapse.
const MANY_TIMERS = 12;
// Space reserved below each dial for its always-visible buttons, so the
// dial never overlaps them. The tallest button row is 80px plus a 12px
// gap and a little slack. It is a CSS variable on .screen (100px on the
// hub) so the desktop and mobile layouts can reserve less for their
// shorter buttons.

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
    if (s.state !== "active" && s.state !== "paused" && s.state !== "finished") {
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
    });
  }
  const now = Date.now();
  out.sort((a, b) => {
    // A finished timer takes focus: it sorts before every running timer,
    // oldest finish first. Running timers sort soonest-first by time left.
    const aFinished = a.state === "finished";
    const bFinished = b.state === "finished";
    if (aFinished && bFinished) {
      return String(a.finishedAt || "").localeCompare(String(b.finishedAt || ""));
    }
    if (aFinished) return -1;
    if (bFinished) return 1;
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

// How many columns the landscape grid uses, by device category. On the
// hub one to three timers stay in a single row; four split into a 2x2
// square so the dials stay big; five and up use three columns, rows as
// needed. Desktop is denser: up to four in a row, four columns past
// that. Mobile keeps the hub mapping in landscape; portrait ignores
// this — the vertical stack is the only layout there.
function gridColumns(count, category) {
  if (category === "desktop") {
    if (count <= 1) return 1;
    if (count <= 4) return count;
    return 4;
  }
  if (count <= 1) return 1;
  if (count <= 3) return count;
  if (count === 4) return 2;
  return 3;
}

// -- height-budget dial cap (P8-3 / P8-6) ----------------------------------
//
// The dial used to size by width only (the smaller of the column width
// and 100vh − controls − padding), so it knew nothing about how many
// rows exist: 1 timer scrolled 84px, 3 fit, 5 scrolled 368px, 12
// scrolled 1820px — non-monotonic, and 4 timers (2x2, dial 936px)
// were the worst unmeasured state. The fix caps the dial by the HEIGHT
// budget: the viewport height minus the screen's vertical padding
// (24px top + 24px bottom + the nav strip's height), minus the gaps
// between rows, minus any full-width extra row (the "Back to recipe"
// bar, 64px + one gap), divided by the row count, minus the controls
// reserve below each dial. The result is --dial-max, a CSS var on
// .screen that the landscape dial rule takes as a third min() term.
// Dials then shrink as rows grow (1 timer -> 968px, 4-5 timers ->
// 426px, 12 timers -> 155px at the hub) and docH stays <= viewport
// for counts <= 12. If --dial-max would fall below the many-mode dial
// size (140px), fitting is impossible and the view switches to
// many-mode behaviour (scroll + compact finished rows) regardless of
// count — at the hub that lands at 13 timers, the same threshold as
// the old count rule. The fit requirement is scoped to landscape:
// phone portrait is expected to scroll (TIMER-7), and the cap only
// applies inside the landscape media query, so portrait is untouched.
const DIAL_FIT_MIN_PX = 140; // many-mode dial size; the fit floor
// Below this fitted diameter the 72px time (TIMER-6) no longer fits
// inside the ring — "59:55" at 72px is ~200px wide — so the screen
// takes the many-mode text scale instead (44px time, 18px name,
// 16px state/hint; same values as .screen.many). 260px leaves room
// for the time plus the chord margin; the 426px dials at 4–5 timers
// keep the full 72px time.
const SMALL_DIAL_MAX_PX = 260;
const SCREEN_PAD_TOP_PX = 24;
const SCREEN_PAD_BOTTOM_PX = 24;
const GRID_GAP_PX = 16;

// Pure — unit-testable in node. Returns { dialMax } in px, or
// { dialMax: null, many: true } when the fit is impossible, or
// { dialMax: null, many: false } when there is nothing to fit (no
// timers, or no viewport to fit into).
function dialFit(count, cols, viewportHeight, controlsReserve, navStripHeight, extraPx) {
  if (
    !count ||
    count <= 0 ||
    !cols ||
    cols <= 0 ||
    !viewportHeight ||
    viewportHeight <= 0
  )
    return { dialMax: null, many: false };
  const rows = Math.ceil(count / cols);
  const verticalPad =
    SCREEN_PAD_TOP_PX + SCREEN_PAD_BOTTOM_PX + (navStripHeight || 0);
  const rowBudget = Math.floor(
    (viewportHeight -
      verticalPad -
      GRID_GAP_PX * (rows - 1) -
      (extraPx || 0)) /
      rows
  );
  const dialMax = rowBudget - controlsReserve;
  if (dialMax < DIAL_FIT_MIN_PX) return { dialMax: null, many: true };
  return { dialMax, many: false };
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

// -- Device detection (SHARED MECHANISM) ----------------------------------
//
// The same detection runs in the AnyList recipe card
// (k-mehta/ha-anylist-recipe-card) and the Satellite Timers card
// (k-mehta/ha-satellite-timers). It is ONE mechanism, copied verbatim
// into each card file: each card is a single standalone file with no
// imports, so the code cannot be shared by import — the two copies must
// stay identical. If you change the signals, the precedence, or the
// category names here, change them in the other card too, and update
// ADAPTIVE-LAYOUT.md (which is also identical in both repos).
//
// Signals (all read from the browser the card is running in):
//   viewport width/height — the strongest signal; the layout follows
//     the viewport, not the device name.
//   pointer type — matchMedia("(pointer: coarse)") is a finger/touch
//     screen; "(pointer: fine)" is a mouse/trackpad.
//   touch — "ontouchstart" in window, or navigator.maxTouchPoints > 0.
//   user agent — only as a tiebreaker for phones/tablets, never alone:
//     a desktop browser can spoof it, and a kiosk can strip it.
//
// Precedence (first match wins) — the unified §3.7 rule (TIMER-2),
// with the DESIGN-9 tablet category (Phase 7), as reordered by P8-16:
//   1. The exact panel signature FIRST, either orientation: 1920x1200
//      (landscape wall) or 1200x1920 (the same panel rotated — O2-01).
//      The signature holds whatever pointer the browser reports
//      (headless reports none; a mouse reports fine). This is what
//      makes the hub layout engage in the rig, where no touch is
//      reported.
//   2. An iPad user agent is a tablet; an Android tablet ("Android"
//      with no "Mobi"/"Mobile" token, viewport at least 768px) is a
//      tablet.
//   3. A phone/tablet user agent ("Mobi", "Android", "iPhone",
//      "iPod") is mobile.
//   4. A viewport narrower than 768px is mobile (a narrow desktop
//      window gets the single-column layout too — the layout follows
//      the viewport).
//   5. A touch screen in the tablet band (768-1024px wide, either
//      orientation) is a tablet.
//   6. A fine pointer at 1024px or wider is desktop — a touchscreen
//      laptop has a mouse or trackpad, so fine wins over coarse here
//      (P8-16; OQ-7 decided: 1366x768 touch laptop is desktop, and a
//      1080x1920 portrait touch laptop is desktop too, O2-07).
//   7. A coarse pointer with no fine pointer is mobile (phones and
//      touch-only tablets outside the band).
//   8. A fine pointer is desktop.
//   9. With no pointer information: a viewport at least 1024px wide
//      is desktop; anything else is mobile.
//
// The old rule 8 (touch + width >= 1600 + landscape = hub) is DELETED:
// it existed only to misclassify touchscreen laptops as the hub, and
// the kiosk is already caught by rule 1 above.
//
// Known limits (every fallback still renders a usable layout, never a
// broken one):
//   - A 1920x1200 laptop (rare) reads as the hub: it is
//     indistinguishable from the wall panel by size alone. That is
//     the documented residual misclassification (§3.7). The same
//     holds for a 1200x1920 laptop (the rotated signature).
//   - A phone in desktop mode (no mobile user agent) still reads as
//     mobile by width (rule 4).
//   - A touchscreen laptop at 1366x768 (coarse, outside the tablet
//     band) with NO fine pointer still reads as mobile — a laptop
//     without a mouse/trackpad is indistinguishable from a tablet
//     outside the band by these signals alone.
//   - Pi 5 pointer reporting with a mouse attached was never measured
//     on the device (spec §13 item 17) — rule 1 exists so the hub
//     layout does not depend on it.
//
// Orientation is separate from category: landscape is width >= height,
// portrait is width < height. The category picks the control sizes and
// the base layout; orientation picks the grid (e.g. one column in
// portrait, two in landscape on mobile).

const HUB_PANEL_WIDTH_PX = 1920;
const HUB_PANEL_HEIGHT_PX = 1200;
const MOBILE_MAX_WIDTH_PX = 768;
const DESKTOP_MIN_WIDTH_PX = 1024;
const MOBILE_USER_AGENT = /Mobi|Android|iPhone|iPad|iPod/i;
const TABLET_MAX_WIDTH_PX = 1024;
const TABLET_USER_AGENT = /iPad/i;
const ANDROID_USER_AGENT = /Android/i;
const MOBILE_TOKEN = /Mobi/i;

// The device the card is running on, from a plain environment object
// (width/height in CSS pixels, pointer flags, touch flag, user agent).
// Pure — no browser access — so it is unit-testable in node. Returns
// { category: "hub" | "desktop" | "tablet" | "mobile", orientation:
// "landscape" | "portrait" }.
function detectDevice(env) {
  const width = Number((env && env.width) || 0);
  const height = Number((env && env.height) || 0);
  const orientation = width >= height ? "landscape" : "portrait";
  const userAgent = String((env && env.userAgent) || "");
  const coarse = Boolean(env && env.coarsePointer);
  const fine = Boolean(env && env.finePointer);
  const touch = Boolean(env && env.touch);
  let category;
  // Rule 1 — the kitchen panel, either orientation: the exact
  // 1920x1200 signature is the hub in landscape, and the same panel
  // rotated (1200x1920) is the hub in portrait (O2-01 — the portrait
  // wall needs its OWN layout, not the phone layout). A 1920x1200
  // laptop reads as hub too — the documented residual (spec §3.7):
  // the panel signature is the only discriminator.
  if (
    (width === HUB_PANEL_WIDTH_PX && height === HUB_PANEL_HEIGHT_PX) ||
    (width === HUB_PANEL_HEIGHT_PX && height === HUB_PANEL_WIDTH_PX)
  )
    category = "hub";
  // A tablet user agent is a tablet, whatever the viewport: an iPad
  // in split view is still a tablet (its narrow width only changes
  // the column count, via the <900px override in the CSS).
  else if (TABLET_USER_AGENT.test(userAgent)) category = "tablet";
  // An Android tablet reports "Android" with no "Mobi"/"Mobile"
  // token; an Android phone reports "Mobile". The width guard keeps
  // a narrow Android phone in desktop mode from reading as a tablet.
  else if (
    ANDROID_USER_AGENT.test(userAgent) &&
    !MOBILE_TOKEN.test(userAgent) &&
    width >= MOBILE_MAX_WIDTH_PX
  )
    category = "tablet";
  else if (MOBILE_USER_AGENT.test(userAgent)) category = "mobile";
  else if (width < MOBILE_MAX_WIDTH_PX) category = "mobile";
  // A touch screen in the tablet band (768-1024px wide, either
  // orientation) is a tablet - 1024x768 must NOT fall to desktop and
  // 768x1024 must NOT fall to mobile (DESIGN-9).
  else if (
    (coarse || touch) &&
    width >= MOBILE_MAX_WIDTH_PX &&
    width <= TABLET_MAX_WIDTH_PX
  )
    category = "tablet";
  // A device with a fine pointer is desktop-class (P8-16): a
  // touchscreen laptop has a mouse or trackpad, so fine wins over
  // coarse here — a 1366x768 touch laptop is a desktop (OQ-7), and a
  // 1080x1920 portrait touch laptop is a desktop too (O2-07), not a
  // phone. The old rule 8 (touch + width >= 1600 + landscape = hub)
  // is DELETED: it existed only to misclassify touchscreen laptops
  // as the hub, and the kiosk is already caught by rule 1 above.
  else if (fine && width >= DESKTOP_MIN_WIDTH_PX) category = "desktop";
  else if (coarse) category = "mobile";
  else if (fine) category = "desktop";
  else if (width >= DESKTOP_MIN_WIDTH_PX) category = "desktop";
  else category = "mobile";
  return { category, orientation };
}

// Whether the card is running in kiosk mode, from the URL query
// string (pure — tested). The wall loads /family-hub?kiosk; a desktop
// browser tab does not. Kiosk mode is what decides the nav strip:
// the strip shows in kiosk mode ALWAYS and hides on desktop only when
// NOT in kiosk mode (NAV-1 kiosk-desktop hole — HA tabs do not exist
// in kiosk mode, so hiding the strip there leaves zero navigation).
// The category itself does not change: a desktop-class viewport in
// kiosk mode is still dev-desktop, it just keeps its strip.
function isKioskMode(search) {
  const query = String(
    (search === null || search === undefined ? "" : search) || ""
  );
  if (!query) return false;
  const params = query.replace(/^\?/, "").split("&");
  return params.some(
    (part) => part === "kiosk" || part.indexOf("kiosk=") === 0
  );
}

// The environment the card is actually running in, read from the
// browser. Returns null outside a browser (node tests), where the card
// keeps its base layout and detectDevice is never called.
function readDeviceEnv() {
  if (typeof window === "undefined" || typeof navigator === "undefined") {
    return null;
  }
  const coarse =
    typeof window.matchMedia === "function" &&
    window.matchMedia("(pointer: coarse)").matches;
  const fine =
    typeof window.matchMedia === "function" &&
    window.matchMedia("(pointer: fine)").matches;
  const touch =
    "ontouchstart" in window ||
    (typeof navigator.maxTouchPoints === "number" &&
      navigator.maxTouchPoints > 0);
  return {
    width: window.innerWidth,
    height: window.innerHeight,
    coarsePointer: coarse,
    finePointer: fine,
    touch,
    userAgent: navigator.userAgent || "",
    kiosk: isKioskMode(
      typeof window.location === "object" && window.location
        ? window.location.search || ""
        : ""
    ),
  };
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
    display: flex;
    flex-direction: column;
    gap: 16px;
    background: transparent;
    overflow-y: auto;
    --controls-reserve: 100px;
    /* NAV-1: the nav strip's height on this device (84px hub, 76px
       tablet, 60px mobile, 0px desktop-hidden, 84px desktop+kiosk).
       The screen reserves it in bottom padding so the last row
       scrolls clear of the fixed strip; the Undo toast reads the
       same var. */
    --nav-strip-height: 84px;
    padding-bottom: calc(24px + var(--nav-strip-height, 84px));
  }
  /* The hub display is landscape (1920x1200): timers sit in a grid —
     one row of up to three, then a balanced grid (at most three
     columns) so dials and buttons stay big instead of shrinking down
     one long row. Portrait is only a fallback (a phone or a rotated
     browser): the same dials stack vertically instead. */
  @media (orientation: landscape) {
    .screen { display: grid; grid-auto-rows: 1fr; align-content: stretch; }
    .screen.cols-1 { grid-template-columns: repeat(1, 1fr); }
    .screen.cols-2 { grid-template-columns: repeat(2, 1fr); }
    .screen.cols-3 { grid-template-columns: repeat(3, 1fr); }
    .screen.cols-4 { grid-template-columns: repeat(4, 1fr); }
    .dial { min-width: 0; }
    .empty { grid-column: 1 / -1; }
    .back-bar { grid-column: 1 / -1; }
  }
  .dial {
    flex: 1 1 0;
    min-height: 0;
    min-width: 0;
    width: 100%;
    position: relative;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
  }
  .dial.entering { animation: dial-in ${ENTER_ANIMATION_MS}ms ease-out; }
  @keyframes dial-in {
    from { opacity: 0; transform: scale(0.94); }
    to { opacity: 1; transform: scale(1); }
  }
  .dial-button {
    height: min(calc(100% - var(--controls-reserve)), calc(100vmin - 48px));
    aspect-ratio: 1;
    max-width: 100%;
    padding: 0;
    border: none;
    background: none;
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
    /* No transition on height/width (DESIGN-6): animating layout
       properties causes reflow jank on a low-power board. The button
       resizes instantly on rotation, which is the correct behaviour
       — a resize is not a moment that needs smoothing. */
  }
  .dial-button:active { transform: scale(0.98); }
  /* In a landscape grid the dial is a circle, so its diameter is the
     smaller of the column width, the height left after the screen
     padding and the button row below it, and --dial-max — the height
     budget for the ROW count (P8-3/P8-6: _applyDialFit sets it from
     the live viewport height, so the dials shrink as rows grow and
     the view fits the viewport for counts <= 12 instead of sizing
     by width alone). The var() fallback is effectively unbounded, so
     a card with no fit computed (portrait, no window) behaves as
     before. Sizing by width (with aspect-ratio) keeps the button
     square: sizing by height and clamping the width left a tall
     empty button with a small circle centred in it. The max-height
     is only a safety net for when the card is not full-screen (Home
     Assistant chrome visible): in kiosk mode the width cap already
     accounts for the same budget. This sits after the base
     .dial-button rule on purpose — same specificity, so the later
     rule wins for the properties it sets. */
  @media (orientation: landscape) {
    .dial-button {
      width: min(100%, calc(100vh - var(--controls-reserve) - 48px), var(--dial-max, 100000px));
      height: auto;
      max-height: calc(100% - var(--controls-reserve));
    }
  }
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
  /* Dial text is HTML, overlaid on the dial cell (TIMER-1 / TIMER-6):
     SVG <text> cannot wrap, and the name must wrap to two lines at
     >=20px without truncating. The overlay spans the full cell width
     (the ring is decorative; the text sizes to the cell, not the ring
     — OQ-2). It never intercepts taps (pointer-events: none): the
     dial button underneath stays the tap target.
     The bottom padding is the controls reserve (TIMER-7): the overlay
     covers the whole cell, but the ring is only the top part — the
     Cancel/Restart row sits below it. Without the padding the text
     centres on the cell and sits half the reserve below the ring
     centre, so in many-mode (140px ring) the name lands on the ring's
     top stroke and the hint is clipped by the button below. Padding
     the overlay by the reserve centres the text on the ring instead;
     the reserve matches the controls height per context (100px hub,
     76px desktop, 84px tablet/mobile), so the alignment holds everywhere. */
  .dial-text {
    position: absolute;
    inset: 0;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    text-align: center;
    pointer-events: none;
    padding: 0 8px var(--controls-reserve) 8px;
    box-sizing: border-box;
    min-width: 0;
  }
  .name {
    color: #212121;
    font-size: 20px;
    line-height: 1.25;
    overflow-wrap: break-word;
    word-break: break-word;
    min-width: 0;
    max-width: 100%;
  }
  .time {
    color: #212121;
    font-size: clamp(40px, 11vw, 72px);
    font-weight: 700;
    line-height: 1.1;
  }
  .state { color: #57534E; font-size: 18px; line-height: 1.3; }
  .started { color: #57534E; font-size: 16px; line-height: 1.3; }
  .hint { color: #57534E; font-size: 18px; line-height: 1.3; }
  .controls { display: flex; gap: 12px; margin-top: 12px; width: 100%; }
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
  .controls button.cancel { background: #B71C1C; border-color: #B71C1C; color: #fff; }
  .controls button.cancel.armed { background: #B71C1C; border-color: #B71C1C; animation: cancel-arm-pulse 0.8s ease-in-out infinite; }
  @keyframes cancel-arm-pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.75; }
  }
  .controls button.restart { background: var(--card-background-color, #fff); }
  .error { color: #B71C1C; font-size: 14px; text-align: center; margin-top: 8px; width: 100%; }
  .empty {
    flex: 1;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    text-align: center;
    color: #57534E;
    font-size: clamp(20px, 4vmin, 32px);
    gap: 12px;
  }
  .empty[hidden] { display: none; }
  .empty svg { width: 72px; height: 72px; opacity: 0.6; margin-bottom: 12px; }
  .voice-hint { font-size: 20px; line-height: 1.4; max-width: 640px; }
  .preset-chips { display: none; flex-wrap: wrap; gap: 12px; justify-content: center; margin-top: 8px; }
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
  .empty-error { color: #B71C1C; font-size: 18px; }
  .empty-error[hidden] { display: none; }
  /* Back to recipe (TIMER-10): shown only when the jump came from a
     recipe (?from_recipe=<id>). */
  .back-bar { display: flex; justify-content: flex-start; }
  .back-bar[hidden] { display: none; }
  .back-bar button {
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
  /* -- Navigation strip (NAV-1) ---------------------------------------
     The persistent strip, FIRST in the DOM (tab order). Noun labels,
     Home first, the current view (Timers) with the accent background,
     heavier weight, and aria-current. Hidden on DESKTOP only when NOT
     in kiosk mode, where HA tabs already exist and work; always shown
     on hub AND phone, and on desktop IN kiosk mode (HA tabs do not
     exist there — the kiosk-desktop hole). The button sizes and the
     strip heights match the recipe card exactly (hub 64px / 24px,
     tablet 56px / 20px, mobile 48px / 16px nowrap), so the strip is
     the same height on every view. The background is the HA page
     background (#FAFAFA by default) — the timer screen is
     transparent, so the strip must be opaque against the page its
     dials scroll over.
     Positioning: FIXED to the viewport bottom, in both orientations.
     The recipe card's strip is sticky — but its screen is not a
     scroll container, so sticky pins against the document. The timer
     screen IS a scroll container (overflow-y: auto, TIMER-7), and
     when the Lovelace view sizes the card to content the screen
     grows past the viewport: a sticky strip would sit at the content
     end, below the fold at scroll top, instead of staying visible.
     Fixed is viewport-relative in both scroll modes (the screen
     scrolling internally, and the document scrolling), and the screen
     reserves the strip's height in bottom padding so the last row
     scrolls clear of it. */
  .nav-strip {
    position: fixed;
    bottom: 0;
    left: 24px;
    right: 24px;
    z-index: 40;
    box-sizing: border-box;
    padding: 16px 0 4px;
    background: var(--primary-background-color, #fafafa);
  }
  .nav-strip-buttons { display: flex; gap: 16px; }
  .nav-strip-button {
    flex: 1;
    min-width: 0;
    min-height: 64px;
    padding: 12px 20px;
    border-radius: 16px;
    border: 1px solid #EDE8E3;
    background: var(--card-background-color, #fff);
    box-shadow: 0 1px 2px rgba(28, 25, 23, 0.05), 0 8px 20px rgba(28, 25, 23, 0.06);
    color: var(--primary-text-color, #212121);
    font-size: 24px;
    font-weight: 700;
    cursor: pointer;
    font-family: inherit;
    overflow-wrap: break-word;
    /* Anchor for the scroll badge (P8-5): the badge is absolutely
       positioned at this button's top-right corner. */
    position: relative;
  }
  .nav-strip-button:hover { box-shadow: 0 2px 4px rgba(28, 25, 23, 0.06), 0 14px 28px rgba(28, 25, 23, 0.10); }
  .nav-strip-button:active { transform: scale(0.98); }
  .nav-strip-button.active { background: #C2410C; border-color: #C2410C; color: #fff; font-weight: 800; }
  .nav-strip-button:focus-visible { outline: 2px solid #C2410C; outline-offset: 2px; }
  /* Visible scroll indicator (TIMER-7 / P8-5): a "↓" badge on the
     Timers strip button, shown only while a dial sits below the
     viewport fold (P8-4 gate — see _updateScrollBadge). The strip is
     already fixed chrome, so the indicator overlaps nothing — the
     old floating pill sat over the middle column's dial and its
     controls by construction. Corner-positioned, so it never changes
     the button's size or wraps its label (the strip buttons are
     tight at 360px); pointer-events none, so taps still hit the
     button underneath. */
  .nav-scroll-badge {
    position: absolute;
    top: -10px;
    right: 6px;
    min-width: 28px;
    height: 28px;
    padding: 0 6px;
    box-sizing: border-box;
    border-radius: 999px;
    background: #1C1917;
    color: #fff;
    font-size: 18px;
    font-weight: 700;
    line-height: 28px;
    text-align: center;
    pointer-events: none;
    z-index: 1;
  }
  .nav-scroll-badge[hidden] { display: none; }
  /* Compact finished rows (TIMER-7): past 12 timers a finished timer
     collapses to one short full-width row instead of a dial. */
  .compact-row { display: none; }
  .screen.many .dial.finished.compact-mode .dial-button,
  .screen.many .dial.finished.compact-mode .dial-text,
  .screen.many .dial.finished.compact-mode .controls,
  .screen.many .dial.finished.compact-mode .error { display: none; }
  .screen.many .dial.finished.compact-mode {
    grid-column: 1 / -1;
    flex: none;
    min-height: 0;
    justify-content: center;
  }
  .screen.many .dial.finished.compact-mode .compact-row {
    display: flex;
    align-items: center;
    gap: 16px;
    width: 100%;
    min-height: 64px;
    padding: 8px 16px;
    box-sizing: border-box;
    border: 1px solid #EDE8E3;
    border-radius: 16px;
    background: var(--card-background-color, #fff);
    cursor: pointer;
  }
  .compact-row .compact-name {
    color: #212121;
    font-size: 20px;
    font-weight: 700;
    overflow-wrap: break-word;
    word-break: break-word;
    min-width: 0;
    flex: 1;
    text-align: left;
  }
  .compact-row .compact-elapsed { color: #57534E; font-size: 18px; white-space: nowrap; }
  .compact-row .compact-restart {
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
  @media (orientation: landscape) {
    /* In many-mode the grid rows size to content and the screen
       scrolls (TIMER-7): running dials keep a fixed 190px row, so the
       %-sized dial button resolves against something definite. */
    .screen.many { grid-auto-rows: auto; align-content: start; }
    .screen.many .dial { min-height: 190px; }
    .screen.many .dial-button { height: 140px; width: 140px; flex: none; }
    .screen.many .time { font-size: 44px; }
    .screen.many .name { font-size: 18px; }
    .screen.many .state, .screen.many .hint { font-size: 16px; }
    /* Small FITTED dials (not many-mode, but shrunk by --dial-max
       below SMALL_DIAL_MAX_PX — 155px at 12 timers on the hub) take
       the same text scale: the 72px time is ~200px wide and bursts
       out of a 155px ring. The class is set by _applyDialFit. */
    .screen.small-dials .time { font-size: 44px; }
    .screen.small-dials .name { font-size: 18px; }
    .screen.small-dials .state, .screen.small-dials .hint { font-size: 16px; }
  }
  /* Cancel Undo toast (TIMER-13): overlaid on the card, never in the
     grid flow, so it cannot shift the dials when it appears. */
  .toast {
    position: absolute;
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
    z-index: 10;
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
  /* The Undo toast must clear the nav strip at the bottom of the
     viewport (NAV-1): it is a shadow-root sibling of .screen, so the
     category classes on .screen select it with ~. On desktop the
     strip is hidden, so the toast stays at 24px. */
  .screen.dev-hub ~ .toast { bottom: 108px; }
  .screen.dev-tablet ~ .toast { bottom: 100px; }
  .screen.dev-mobile ~ .toast { bottom: 84px; }
  .screen.dev-desktop.kiosk ~ .toast { bottom: 108px; }
  /* -- Adaptive layout --------------------------------------------------
     The card detects its device (see detectDevice) and carries the
     category + orientation as classes on .screen: dev-hub, dev-desktop,
     dev-tablet or dev-mobile, and orient-landscape or orient-portrait.
     The rules above ARE the hub layout (1920x1200 kitchen kiosk: big
     dials, big touch targets); the rules below only adjust for desktop,
     tablet and mobile. A card with no device classes (no browser
     environment) renders the hub layout. */
  /* Desktop: a mouse is precise, so the buttons shrink and the grid
     gets denser — up to four dials in a row (see gridColumns). The
     reserve below each dial shrinks with the buttons. */
  .screen.dev-desktop { --controls-reserve: 76px; --nav-strip-height: 0px; }
  .screen.dev-desktop .controls button { min-height: 56px; font-size: 18px; padding: 8px; }
  .screen.dev-desktop .empty { font-size: clamp(18px, 3vmin, 26px); }
  .screen.dev-desktop .name { font-size: 20px; }
  .screen.dev-desktop .state, .screen.dev-desktop .hint { font-size: 16px; }
  .screen.dev-desktop .back-bar button { min-height: 56px; font-size: 18px; }
  /* Desktop: the nav strip is hidden (HA tabs already exist and work
     there — NAV-1) ONLY when NOT in kiosk mode: in kiosk mode HA tabs
     do not exist, so a desktop-class viewport in kiosk mode would have
     ZERO navigation with the strip hidden (the kiosk-desktop hole).
     The strip therefore shows in kiosk mode ALWAYS (the ".kiosk"
     override below); desktop+kiosk reserves the strip height (84px)
     in --nav-strip-height so the last row scrolls clear of it, and
     the Undo toast clears it too. */
  .screen.dev-desktop .nav-strip { display: none; }
  .screen.dev-desktop.kiosk { --nav-strip-height: 84px; }
  .screen.dev-desktop.kiosk .nav-strip { display: block; }
  /* Mobile: touch targets stay large (a finger is not a mouse) and the
     buttons stay reachable. In portrait the dials stack; each dial
     keeps a minimum height so its time stays readable, and the screen
     scrolls when five timers need more room than the phone has.
     Preset chips (TIMER-4) show on mobile and tablet — the hub is
     voice-only by deliberate choice. */
  .screen.dev-mobile { --controls-reserve: 84px; --nav-strip-height: 60px; }
  .screen.dev-mobile .controls button { min-height: 64px; font-size: 18px; }
  .screen.dev-mobile.orient-portrait { overflow-y: auto; }
  /* Phase 7 follow-up (Fix 2): the finished dial's two sub-lines
     ("Done — N min ago", "Tap to dismiss") were wider than the ring's
     chord at their height on a 360px phone, so the ring crossed
     characters. Two changes, both mobile-only: the dial is taller
     (300px, so the ring is ~200px instead of 154px) and the sub-text
     is smaller (14px instead of 16px). Measured live at 360x640:
     the worst realistic line ("Done — 12 h 59 min ago", 148px at
     14px) clears the chord by ~12px, and the duplicate-timer hint
     line by ~29px. The cost is scroll for two timers at 360x640
     (the view already scrolls at three); the hub and tablet rules
     (TIMER-6: name >=20px, state/hint >=18px on the hub) are
     untouched, and the name stays 18px here. */
  .screen.dev-mobile.orient-portrait .dial { min-height: 300px; }
  .screen.dev-mobile .name { font-size: 18px; }
  .screen.dev-mobile .state, .screen.dev-mobile .hint { font-size: 14px; }
  .screen.dev-mobile .preset-chips { display: flex; }
  .screen.dev-mobile .back-bar button { min-height: 56px; font-size: 18px; }
  /* Phone nav strip (NAV-1): 48px buttons, 16px labels, 8px gap,
     nowrap — four labels would wrap mid-word at 360px, so the strip
     is 16px + nowrap (same rule as the recipe card). */
  .screen.dev-mobile .nav-strip { padding: 12px 0 0; }
  .screen.dev-mobile .nav-strip-buttons { gap: 8px; }
  .screen.dev-mobile .nav-strip-button { min-height: 48px; padding: 8px 6px; font-size: 16px; white-space: nowrap; }
  /* Tablet (DESIGN-9): 56-64px targets (spec §3.2); dial text at
     the TIMER-6 hub minima (name 20px, state/hint 18px) — readable
     at 1m. Preset chips show here too (TIMER-4: phone/tablet get
     on-screen presets; hub is voice-only). The dial grid keeps the
     non-desktop rule (at most three columns — see gridColumns). */
  .screen.dev-tablet { --controls-reserve: 84px; --nav-strip-height: 76px; }
  .screen.dev-tablet .controls button { min-height: 64px; font-size: 18px; }
  .screen.dev-tablet .empty { font-size: clamp(18px, 3vmin, 26px); }
  .screen.dev-tablet .name { font-size: 20px; }
  .screen.dev-tablet .state, .screen.dev-tablet .hint { font-size: 18px; }
  .screen.dev-tablet .preset-chips { display: flex; }
  .screen.dev-tablet .back-bar button { min-height: 56px; font-size: 18px; }
  /* Tablet nav strip (NAV-1): 56px buttons, 20px labels — the 56-64px
     tablet band (spec §3.2). */
  .screen.dev-tablet .nav-strip-button { min-height: 56px; padding: 10px 16px; font-size: 20px; }
  .screen.dev-tablet .nav-strip-buttons { gap: 12px; }
  /* Reduced motion (DESIGN-6, WCAG 2.2): no non-essential animation.
     The soon-ring pulse stops — the amber ring + orange time (the
     "soon" STATE, DESIGN-8 guardrail) still show. The ring sweep is
     continuous progress, not decoration, and its stroke-dashoffset
     transition is paint-only (no layout), so it stays. Dial entry,
     controls entry and the cancel-arm pulse become instant; the
     press feedback is instant too. */
  @media (prefers-reduced-motion: reduce) {
    .dial.entering { animation: none; }
    .controls button { animation: none; }
    .dial.soon .ring { animation: none; }
    .controls button.cancel.armed { animation: none; }
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
    this._lastKey = "";
    this._busy = new Set();
    this._error = {};
    this._arming = new Map();
    this._tick = null;
    this._dialEls = new Map();
    this._screenEl = null;
    this._emptyEl = null;
    this._emptyErrorEl = null;
    this._backBarEl = null;
    this._navStripEl = null; // NAV-1 strip (built in _ensureShell)
    this._lastNavigation = null; // Last path _navigate was asked for (test hook)
    this._scrollBadgeEl = null; // "↓" badge on the Timers strip button (P8-5), built in _ensureShell
    this._toastEl = null;
    this._toastTimer = null;
    this._undoTimer = null;
    this._presetBusy = false;
    this._device = null; // { category, orientation } from detectDevice, or null outside a browser.
    this._manyMode = false; // dialFit() says the height budget cannot fit (P8-3/P8-6), or count > MANY_TIMERS.
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

  set hass(hass) {
    this._hass = hass;
    const timers = discoverTimers(hass ? Object.values(hass.states) : []);
    const key = JSON.stringify(
      timers.map((t) => [t.entity_id, t.state, t.name, t.duration])
    );
    if (key !== this._lastKey) {
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
  }

  connectedCallback() {
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
        this._updateScrollBadge();
        this._scheduleScrollSave();
      };
      window.addEventListener("scroll", this._windowScrollBound, {
        passive: true,
      });
    }
  }

  disconnectedCallback() {
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
    // Re-sync only when the device actually changed: a category change
    // moves the grid columns, an orientation change moves the layout.
    // A pure viewport resize (same category, same orientation) still
    // re-fits the dials — --dial-max depends on the viewport height —
    // and a many-mode flip from the new height re-syncs too, so the
    // compact finished rows follow the fit.
    const before = this._device;
    const manyBefore = this._manyMode;
    this._applyDevice();
    const after = this._device;
    this._applyDialFit();
    if (
      before &&
      after &&
      (before.category !== after.category ||
        before.orientation !== after.orientation ||
        manyBefore !== this._manyMode)
    ) {
      this._sync();
    }
    this._updateScrollBadge();
  }

  // Detect the device and carry it as classes on the screen element:
  // dev-hub, dev-desktop or dev-mobile, plus orient-landscape or
  // orient-portrait. The CSS keys the adaptive layout off these
  // classes; the base rules are the hub layout. Outside a browser
  // (node tests) there is no environment to read, so the classes are
  // left off and the base layout stands.
  _applyDevice() {
    if (!this._screenEl) return;
    const env = readDeviceEnv();
    if (!env) return;
    const device = detectDevice(env);
    this._device = device;
    this._screenEl.classList.remove(
      "dev-hub",
      "dev-desktop",
      "dev-tablet",
      "dev-mobile",
      "orient-landscape",
      "orient-portrait",
      "kiosk"
    );
    this._screenEl.classList.add(
      `dev-${device.category}`,
      `orient-${device.orientation}`
    );
    // Kiosk mode is a class of its own (not a device category): the
    // nav strip shows in kiosk mode ALWAYS, even on desktop (NAV-1).
    if (env.kiosk) this._screenEl.classList.add("kiosk");
  }

  // Height-budget dial cap (P8-3/P8-6): set --dial-max on .screen from
  // the live viewport, row count and per-category reserves, or clear
  // it when there is nothing to fit (portrait — the cap only applies
  // inside the landscape media query — no window, or no timers). The
  // many-mode flag is computed here too, so _sync and _onResize share
  // one decision: count > MANY_TIMERS, or the fit floor.
  _applyDialFit() {
    if (!this._screenEl) return;
    const device = this._device;
    if (
      !device ||
      device.orientation !== "landscape" ||
      typeof window === "undefined" ||
      !window.innerHeight
    ) {
      this._screenEl.style.removeProperty("--dial-max");
      this._screenEl.classList.remove("small-dials");
      this._manyMode = this._timers.length > MANY_TIMERS;
      return;
    }
    // The controls reserve is the space BELOW the ring in one dial:
    // 12px .controls margin-top + the button min-height (80px hub,
    // 56px desktop, 64px tablet/mobile) + the 8px margin-top of the
    // (usually empty) .error div. The empty error div still takes its
    // margin, so a reserve without it is 8px short per row and the
    // view scrolls that much on desktop/tablet/mobile (measured live
    // at 1366x768: docH 776 vs vh 768; the hub's 100 already included
    // it: 12 + 80 + 8). The CSS --controls-reserve vars below must
    // match these numbers: they size the button AND centre the
    // .dial-text overlay on the ring.
    const reserves = {
      hub: { controls: 100, nav: 84 },
      desktop: { controls: 76, nav: 0 },
      tablet: { controls: 84, nav: 76 },
      mobile: { controls: 84, nav: 60 },
    };
    const reserve = reserves[device.category] || reserves.hub;
    // Desktop in kiosk mode keeps the strip (NAV-1), so it reserves
    // the strip height like the hub instead of 0.
    const nav =
      device.category === "desktop" &&
      this._screenEl.classList.contains("kiosk")
        ? 84
        : reserve.nav;
    const cols = gridColumns(this._timers.length, device.category);
    // The "Back to recipe" bar (TIMER-10) is a full-width grid row:
    // 64px button + one 16px gap, taken out of the height budget.
    const extra = this._recipeContext ? 80 : 0;
    const fit = dialFit(
      this._timers.length,
      cols,
      window.innerHeight,
      reserve.controls,
      nav,
      extra
    );
    if (fit.dialMax !== null) {
      this._screenEl.style.setProperty("--dial-max", `${fit.dialMax}px`);
    } else {
      this._screenEl.style.removeProperty("--dial-max");
    }
    // Small fitted dials take the many-mode text scale (see
    // SMALL_DIAL_MAX_PX): without it the 72px time bursts out of a
    // 155px ring at 12 timers.
    this._screenEl.classList.toggle(
      "small-dials",
      fit.dialMax !== null && fit.dialMax < SMALL_DIAL_MAX_PX
    );
    this._manyMode = fit.many || this._timers.length > MANY_TIMERS;
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
    // P8-5: the scroll indicator lives ON the strip — a "↓" badge on
    // the Timers button (the strip is already fixed chrome, so the
    // indicator overlaps nothing). It is aria-hidden: the badge is a
    // visual cue only; the dials themselves are in the DOM and
    // reachable by scrolling.
    this._navStripEl.innerHTML =
      '<div class="nav-strip-buttons">' +
      NAV_STRIP_VIEWS.map((view) => {
        const active = view.key === "timers";
        const badge =
          view.key === "timers"
            ? '<span class="nav-scroll-badge" aria-hidden="true" hidden>↓</span>'
            : "";
        return `<button type="button" class="nav-strip-button${active ? " active" : ""}" data-action="nav" data-path="${view.path}"${active ? ' aria-current="page"' : ""}>${view.label}${badge}</button>`;
      }).join("") +
      "</div>";
    this._screenEl.appendChild(this._navStripEl);
    this._scrollBadgeEl = this._navStripEl.querySelector(".nav-scroll-badge");
    this._backBarEl = document.createElement("div");
    this._backBarEl.className = "back-bar";
    this._backBarEl.hidden = true;
    this._backBarEl.innerHTML =
      '<button data-action="back-recipe">← Back to recipe</button>';
    this._screenEl.appendChild(this._backBarEl);
    this._emptyEl = document.createElement("div");
    this._emptyEl.className = "empty";
    this._emptyEl.innerHTML =
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="12" cy="13" r="8" /><path d="M12 9v4l2.5 2.5M9 2h6" /></svg><div>No timers running</div>' +
      '<div class="voice-hint">Say &ldquo;Okay Nabu, set a 10 minute pasta timer&rdquo;</div>' +
      '<div class="voice-hint">To add time, say &ldquo;Okay Nabu, add 5 more minutes to the pasta timer&rdquo;</div>' +
      '<div class="preset-chips"><span class="chip-label">Start a timer:</span>' +
      [5, 10, 15, 30]
        .map(
          (m) =>
            `<button data-action="preset" data-minutes="${m}" aria-label="Start a ${m} minute timer">${m} min</button>`
        )
        .join("") +
      "</div>" +
      '<div class="empty-error" hidden></div>';
    this._screenEl.appendChild(this._emptyEl);
    this._emptyErrorEl = this._emptyEl.querySelector(".empty-error");
    this._toastEl = document.createElement("div");
    this._toastEl.className = "toast";
    this._toastEl.hidden = true;
    this.shadowRoot.appendChild(this._toastEl);
    if (
      typeof window !== "undefined" &&
      window.location &&
      typeof window.location.search === "string"
    ) {
      this._recipeContext = readRecipeContext(window.location.search);
    }
    if (this._scrollBound === null && typeof this._screenEl.addEventListener === "function") {
      this._scrollBound = () => {
        this._updateScrollBadge();
        this._scheduleScrollSave();
      };
      this._screenEl.addEventListener("scroll", this._scrollBound);
    }
    // The badge also re-evaluates when the screen itself changes size
    // (TIMER-7): _sync runs while the Lovelace view is still sizing
    // the card, so the first _updateScrollBadge can measure a screen
    // that is still at its minimum height — and nothing else fires
    // when the view later grows the card to its full height. The
    // observer fires on that growth (and on dial adds/removes), and
    // it fires once on observe, so the badge is correct from the first
    // settled layout. (No ResizeObserver in node tests: skipped.)
    if (
      this._screenResizeObserver === null &&
      typeof ResizeObserver !== "undefined"
    ) {
      this._screenResizeObserver = new ResizeObserver(() =>
        this._updateScrollBadge()
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
    // Fit the dials to the height budget before building them: the
    // many-mode flag decides the compact finished rows below.
    this._applyDialFit();
    const now = Date.now();
    const live = new Set(this._timers.map((t) => t.timer_id));

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

    for (const timer of this._timers) {
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
      if (!live.has(id)) {
        this._dialEls.delete(id);
        el.remove();
      }
    }

    this._emptyEl.hidden = this._timers.length > 0;
    this._backBarEl.hidden = !this._recipeContext;
    this._screenEl.classList.toggle("many", this._manyMode);
    this._screenEl.classList.remove("cols-1", "cols-2", "cols-3", "cols-4");
    this._screenEl.classList.add(
      `cols-${gridColumns(this._timers.length, this._device ? this._device.category : undefined)}`
    );
    this._flip(before);
    this._updateScrollBadge();
    this._restoreScroll();
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

  // Scroll indicator (TIMER-7 / P8-4 / P8-5): the "↓" badge on the
  // Timers strip button shows only while at least one DIAL sits below
  // the fold — not whenever the screen's rect extends below it. The
  // old gate fired whenever the controls row overflowed (e.g. with 1
  // timer), so the pill claimed "More timers below" when there were
  // none. The fold is the lower of the viewport bottom and the
  // screen's own bottom: in document-scroll mode the screen runs past
  // the viewport, in internal-scroll mode the screen is shorter than
  // the viewport and its own bottom is the fold. Either way the
  // getBoundingClientRect() of a dial already accounts for scrolling,
  // so one check covers both scroll modes — and once the user has
  // scrolled to the bottom the last dial is fully visible and the
  // badge hides itself. (No window in node tests: the badge stays
  // hidden and the logic is skipped.)
  _updateScrollBadge() {
    if (!this._screenEl || !this._scrollBadgeEl) return;
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
    this._scrollBadgeEl.hidden = !show;
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
    const el = document.createElement("div");
    el.className = "dial";
    el.dataset.timerId = timer.timer_id;
    el.innerHTML = `
      <button class="dial-button" data-action="dial" data-timer="${escapeHtml(timer.timer_id)}" aria-label="${escapeHtml(timer.name)} timer">
        <svg viewBox="0 0 200 200" role="img" aria-label="${escapeHtml(timer.name)}">
          <circle class="track" cx="100" cy="100" r="88" />
          <circle class="ring" data-ring="${escapeHtml(timer.timer_id)}" cx="100" cy="100" r="88" transform="rotate(-90 100 100)" />
        </svg>
      </button>
      <div class="dial-text" aria-hidden="true">
        <div class="name" data-name="${escapeHtml(timer.timer_id)}"></div>
        <div class="time" data-remaining="${escapeHtml(timer.timer_id)}"></div>
        <div class="state" data-state="${escapeHtml(timer.timer_id)}"></div>
        <div class="started" data-started="${escapeHtml(timer.timer_id)}"></div>
        <div class="hint" data-hint="${escapeHtml(timer.timer_id)}"></div>
      </div>
      <div class="compact-row" data-compact="${escapeHtml(timer.timer_id)}" data-action="dial" data-timer="${escapeHtml(timer.timer_id)}" role="button" aria-label="${escapeHtml(timer.name)} timer, finished, tap to dismiss">
        <span class="compact-name" data-compact-name="${escapeHtml(timer.timer_id)}"></span>
        <span class="compact-elapsed" data-compact-elapsed="${escapeHtml(timer.timer_id)}"></span>
        <button class="compact-restart" data-action="restart" data-timer="${escapeHtml(timer.timer_id)}" aria-label="Restart ${escapeHtml(timer.name)} timer">Restart</button>
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
      compactRow: el.querySelector(`[data-compact="${timer.timer_id}"]`),
      compactName: el.querySelector(`[data-compact-name="${timer.timer_id}"]`),
      compactElapsed: el.querySelector(
        `[data-compact-elapsed="${timer.timer_id}"]`
      ),
      controls: el.querySelector(`[data-controls="${timer.timer_id}"]`),
      error: el.querySelector(`[data-error="${timer.timer_id}"]`),
    };
    el._controlsKind = "";
    return el;
  }

  _updateDial(el, timer, now) {
    const refs = el._refs;
    const isFinished = timer.state === "finished";
    const isPaused = timer.state === "paused";
    const left = remainingSeconds(timer, now);

    el.classList.toggle("finished", isFinished);
    el.classList.toggle("paused", isPaused);
    el.classList.toggle(
      "soon",
      !isFinished && timer.state === "active" && left > 0 && left <= FINISHING_SOON_SECONDS
    );
    // Past 12 timers — or whenever the height budget cannot fit the
    // dials (P8-3/P8-6) — a finished timer collapses to a compact row
    // (TIMER-7).
    el.classList.toggle("compact-mode", isFinished && this._manyMode);

    // The dial itself is the primary tap target — the biggest, most
    // forgiving thing on the screen. It pauses a running timer, resumes
    // a paused one, and dismisses a finished one, so the most common
    // action for each state is one big tap, never a small button.
    refs.button.setAttribute(
      "aria-label",
      isFinished
        ? `${timer.name} timer, finished, tap to dismiss`
        : `${timer.name} timer, tap to ${isPaused ? "resume" : "pause"}`
    );

    // TIMER-1: the full name, wrapping to two lines at >=20px on the
    // hub. No truncation, no shrinking.
    refs.name.textContent = timer.name;
    refs.time.textContent = isFinished ? "Done" : formatRemaining(left);
    if (isFinished) {
      const finishedMs = timer.finishedAt ? Date.parse(timer.finishedAt) : null;
      refs.state.textContent = formatElapsedSince(finishedMs, now);
    } else {
      refs.state.textContent = isPaused ? "Paused" : "";
    }
    // TIMER-11: duplicates get their start time, so two timers with
    // the same name are distinguishable.
    if (this._duplicateNames.has(timer.name)) {
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
    refs.hint.textContent = isFinished
      ? "Tap to dismiss"
      : isPaused
        ? "Tap to resume"
        : "Tap to pause";

    if (isFinished) {
      const finishedMs = timer.finishedAt ? Date.parse(timer.finishedAt) : null;
      refs.compactName.textContent = timer.name;
      refs.compactElapsed.textContent = formatElapsedSince(finishedMs, now);
    }

    refs.ring.style.strokeDashoffset = String(
      isFinished ? 0 : RING_CIRCUMFERENCE * (1 - progressFraction(timer, now))
    );
    refs.ring.classList.toggle("paused", isPaused);

    const kind = isFinished ? "finished" : "running";
    if (el._controlsKind !== kind) {
      el._controlsKind = kind;
      refs.controls.innerHTML = isFinished
        ? `<button class="restart" data-action="restart" data-timer="${escapeHtml(timer.timer_id)}" aria-label="Restart ${escapeHtml(timer.name)} timer">Restart</button>`
        : `<button class="cancel" data-action="cancel" data-timer="${escapeHtml(timer.timer_id)}" aria-label="Cancel ${escapeHtml(timer.name)} timer">Cancel</button>`;
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
    button.classList.toggle("armed", armed);
    button.textContent = armed ? "Tap again to cancel" : "Cancel";
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
      if (!el) continue;
      if (timer.state === "finished") {
        // TIMER-3: the elapsed-since-finish counts up on the tick.
        const finishedMs = timer.finishedAt ? Date.parse(timer.finishedAt) : null;
        const text = formatElapsedSince(finishedMs, now);
        el._refs.state.textContent = text;
        el._refs.compactElapsed.textContent = text;
        continue;
      }
      const left = remainingSeconds(timer, now);
      el._refs.time.textContent = formatRemaining(left);
      el._refs.ring.style.strokeDashoffset = String(
        RING_CIRCUMFERENCE * (1 - progressFraction(timer, now))
      );
      el.classList.toggle(
        "soon",
        timer.state === "active" && left > 0 && left <= FINISHING_SOON_SECONDS
      );
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

  _showUndoToast(timer) {
    // TIMER-13: cancelling offers a 5 s Undo; Undo re-creates the
    // timer with the same name and the REMAINING time at cancel
    // (F-T1) — a 10-min timer cancelled with 3 min left comes back
    // as a 3-min timer, not a fresh 10-min one. Min 1 s: a timer
    // cancelled at 0 s remaining still comes back.
    if (!this._toastEl) return;
    if (this._toastTimer) {
      clearTimeout(this._toastTimer);
      this._toastTimer = null;
    }
    const remaining = Math.max(1, remainingSeconds(timer, Date.now()));
    this._undoTimer = { name: timer.name, duration: remaining };
    this._toastEl.innerHTML = "";
    const message = document.createElement("span");
    message.textContent = `${timer.name} timer cancelled`;
    const undo = document.createElement("button");
    undo.setAttribute("data-action", "undo-cancel");
    undo.textContent = "Undo";
    this._toastEl.appendChild(message);
    this._toastEl.appendChild(undo);
    this._toastEl.hidden = false;
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
      await this._hass.callService("satellite_timers", "start_timer", {
        name: undo.name,
        duration: undo.duration,
      });
    } catch (err) {
      if (this._emptyErrorEl) {
        this._emptyErrorEl.textContent =
          "Couldn't bring the timer back — try voice";
        this._emptyErrorEl.hidden = false;
      }
    }
  }

  async _onPresetTap(minutes) {
    // TIMER-4: phone/tablet preset chips start a timer by duration.
    // The name is the duration ("10 minute timer"); the recipe name
    // comes from recipe chips instead (RECIPE-5 / BRIDGE-1).
    if (this._presetBusy || !this._hass) return;
    this._presetBusy = true;
    this._setPresetBusy(true);
    try {
      await this._hass.callService("satellite_timers", "start_timer", {
        name: `${minutes} minute timer`,
        duration: minutes * 60,
      });
      if (this._emptyErrorEl) this._emptyErrorEl.hidden = true;
    } catch (err) {
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
    if (!this._emptyEl) return;
    for (const button of this._emptyEl.querySelectorAll(
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
  _navigate(path) {
    this._lastNavigation = path;
    if (typeof window === "undefined") return;
    try {
      if (window.history && typeof window.history.pushState === "function") {
        window.history.pushState(null, "", path);
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
    // TIMER-10: return to the recipe at the same step. The recipe id
    // and step came in as ?from_recipe=<id>&step=<n>; the recipe card
    // (Phase 4) honours ?recipe=<id>&step=<n> as a deep link.
    if (
      typeof window === "undefined" ||
      !window.location ||
      !this._recipeContext
    ) {
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
    window.location.assign(url);
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

    const timerId = target.dataset.timer;
    const timer = this._timers.find((t) => t.timer_id === timerId);
    if (!timer) return;

    if (action === "dial") {
      // TIMER-9: a dial tap clears any pending Cancel arm first.
      this._clearArm(timerId);
      // One tap on the dial does the primary thing for the timer's
      // state: pause a running timer, resume a paused one, dismiss a
      // finished one. Adding time stays voice-only.
      if (timer.state === "finished") this._control(timerId, "dismiss_timer");
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
    progressFraction,
    startedAtMs,
    discoverTimers,
    gridColumns,
    dialFit,
    detectDevice,
    isKioskMode,
    readDeviceEnv,
    readRecipeContext,
    NAV_STRIP_VIEWS,
    TimerScreenCard,
    CARD_CSS,
    TIMER_SCROLL_KEY,
    parseTimerScroll,
    serializeTimerScroll,
  };
}
