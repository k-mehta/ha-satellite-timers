const RING_CIRCUMFERENCE = 2 * Math.PI * 88;

// A timer is "finishing soon" in its last minute: the ring turns warm and
// pulses gently so it catches the eye without shouting.
const FINISHING_SOON_SECONDS = 60;
// How long the Cancel button stays armed ("Tap again to cancel") after the
// first tap, before it disarms itself.
const CANCEL_ARM_MS = 4000;
// Entrance animation length for a newly added dial.
const ENTER_ANIMATION_MS = 240;
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

function shortName(name) {
  if (name.length <= 18) return name;
  return name.slice(0, 17) + "…";
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
// Precedence (first match wins):
//   1. A phone/tablet user agent ("Mobi", "Android", "iPhone", "iPad",
//      "iPod") is mobile.
//   2. A viewport narrower than 768px is mobile (a narrow desktop
//      window gets the single-column layout too — the layout follows
//      the viewport).
//   3. A coarse pointer on anything but a huge landscape screen is
//      mobile (phones and tablets).
//   4. A coarse pointer on a landscape screen at least 1600px wide is
//      the hub: the kitchen kiosk is a 1920x1200 touch display.
//   5. A fine pointer is desktop.
//   6. With no pointer information: a touch screen that is landscape
//      and at least 1600px wide is the hub; a viewport at least 1024px
//      wide is desktop; anything else is mobile.
//
// Known limits (every fallback still renders a usable layout, never a
// broken one):
//   - A desktop touchscreen at 1920x1200 reads as the hub. That is
//     fine: the hub layout is just big targets on a wide screen.
//   - A phone in desktop mode (no mobile user agent) still reads as
//     mobile by width (rule 2).
//   - The hub is recognised by size + touch + landscape, not by name:
//     if the kiosk display changes size, the thresholds above are the
//     place to adjust.
//
// Orientation is separate from category: landscape is width >= height,
// portrait is width < height. The category picks the control sizes and
// the base layout; orientation picks the grid (e.g. one column in
// portrait, two in landscape on mobile).

const HUB_MIN_WIDTH_PX = 1600;
const MOBILE_MAX_WIDTH_PX = 768;
const DESKTOP_MIN_WIDTH_PX = 1024;
const MOBILE_USER_AGENT = /Mobi|Android|iPhone|iPad|iPod/i;

// The device the card is running on, from a plain environment object
// (width/height in CSS pixels, pointer flags, touch flag, user agent).
// Pure — no browser access — so it is unit-testable in node. Returns
// { category: "hub" | "desktop" | "mobile", orientation:
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
  if (MOBILE_USER_AGENT.test(userAgent)) category = "mobile";
  else if (width < MOBILE_MAX_WIDTH_PX) category = "mobile";
  else if (coarse && !(width >= HUB_MIN_WIDTH_PX && orientation === "landscape"))
    category = "mobile";
  else if (coarse) category = "hub";
  else if (fine) category = "desktop";
  else if (touch && width >= HUB_MIN_WIDTH_PX && orientation === "landscape")
    category = "hub";
  else if (width >= DESKTOP_MIN_WIDTH_PX) category = "desktop";
  else category = "mobile";
  return { category, orientation };
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
  };
}

const CARD_CSS = `
  :host { display: block; height: 100%; }
  .screen {
    height: 100%;
    min-height: 480px;
    box-sizing: border-box;
    padding: 24px;
    display: flex;
    flex-direction: column;
    gap: 16px;
    background: transparent;
    --controls-reserve: 100px;
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
  }
  .dial {
    flex: 1 1 0;
    min-height: 0;
    width: 100%;
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
    transition: height 0.3s ease, width 0.3s ease;
  }
  .dial-button:active { transform: scale(0.98); }
  /* In a landscape grid the dial is a circle, so its diameter is the
     smaller of the column width and the height left after the screen
     padding and the button row below it. Sizing by width (with
     aspect-ratio) keeps the button square: sizing by height and
     clamping the width left a tall empty button with a small circle
     centred in it. The max-height is only a safety net for when the
     card is not full-screen (Home Assistant chrome visible): in kiosk
     mode the width cap already accounts for the same budget.
     This sits after the base .dial-button rule on purpose — same
     specificity, so the later rule wins for the properties it sets. */
  @media (orientation: landscape) {
    .dial-button {
      width: min(100%, calc(100vh - var(--controls-reserve) - 48px));
      height: auto;
      max-height: calc(100% - var(--controls-reserve));
    }
  }
  .dial-button:focus-visible { outline: 2px solid var(--primary-color); outline-offset: 4px; border-radius: 50%; }
  svg { display: block; width: 100%; height: auto; max-height: 100%; }
  .track { fill: none; stroke: var(--divider-color, #e0e0e0); stroke-width: 10; }
  .ring {
    fill: none;
    stroke: var(--primary-color, #03a9f4);
    stroke-width: 10;
    stroke-linecap: round;
    stroke-dasharray: ${RING_CIRCUMFERENCE};
    transition: stroke-dashoffset 0.5s linear, stroke 0.3s ease;
  }
  .ring.paused { stroke: var(--primary-color, #03a9f4); opacity: 0.45; }
  .dial.finished .ring { stroke: #fb8c00; }
  .dial.soon .ring { stroke: #fb8c00; animation: soon-pulse 1.6s ease-in-out infinite; }
  .dial.soon .time { fill: #e65100; }
  .dial.paused .ring { animation: none; }
  @keyframes soon-pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.55; }
  }
  .name { fill: var(--primary-text-color, #212121); font-size: 14px; text-anchor: middle; }
  .time { fill: var(--primary-text-color, #212121); font-size: 36px; font-weight: 700; text-anchor: middle; }
  .state { fill: var(--secondary-text-color, #727272); font-size: 12px; text-anchor: middle; }
  .hint { fill: var(--secondary-text-color, #727272); font-size: 11px; text-anchor: middle; }
  .controls { display: flex; gap: 12px; margin-top: 12px; width: 100%; }
  .controls button {
    flex: 1;
    min-height: 80px;
    padding: 12px 8px;
    border-radius: 16px;
    border: 1px solid var(--divider-color, #e0e0e0);
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
  .controls button.cancel { background: var(--error-color, #db4437); border-color: var(--error-color, #db4437); color: #fff; }
  .controls button.cancel.armed { background: #b71c1c; border-color: #b71c1c; animation: cancel-arm-pulse 0.8s ease-in-out infinite; }
  @keyframes cancel-arm-pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.75; }
  }
  .controls button.restart { background: var(--card-background-color, #fff); }
  .error { color: var(--error-color, #db4437); font-size: 14px; text-align: center; margin-top: 8px; width: 100%; }
  .empty {
    flex: 1;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    text-align: center;
    color: var(--secondary-text-color, #727272);
    font-size: clamp(20px, 4vmin, 32px);
  }
  .empty[hidden] { display: none; }
  .empty svg { width: 72px; height: 72px; opacity: 0.6; margin-bottom: 12px; }
  /* -- Adaptive layout --------------------------------------------------
     The card detects its device (see detectDevice) and carries the
     category + orientation as classes on .screen: dev-hub, dev-desktop
     or dev-mobile, and orient-landscape or orient-portrait. The rules
     above ARE the hub layout (1920x1200 kitchen kiosk: big dials, big
     touch targets); the rules below only adjust for desktop and mobile.
     A card with no device classes (no browser environment) renders the
     hub layout. */
  /* Desktop: a mouse is precise, so the buttons shrink and the grid
     gets denser — up to four dials in a row (see gridColumns). The
     reserve below each dial shrinks with the buttons. */
  .screen.dev-desktop { --controls-reserve: 68px; }
  .screen.dev-desktop .controls button { min-height: 56px; font-size: 18px; padding: 8px; }
  .screen.dev-desktop .empty { font-size: clamp(18px, 3vmin, 26px); }
  /* Mobile: touch targets stay large (a finger is not a mouse) and the
     buttons stay reachable. In portrait the dials stack; each dial
     keeps a minimum height so its time stays readable, and the screen
     scrolls when five timers need more room than the phone has. */
  .screen.dev-mobile { --controls-reserve: 76px; }
  .screen.dev-mobile .controls button { min-height: 64px; font-size: 18px; }
  .screen.dev-mobile.orient-portrait { overflow-y: auto; }
  .screen.dev-mobile.orient-portrait .dial { min-height: 230px; }
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
    this._device = null; // { category, orientation } from detectDevice, or null outside a browser.
    this._resizeBound = null;
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
  }

  _onResize() {
    // Re-sync only when the device actually changed: a category change
    // moves the grid columns, an orientation change moves the layout.
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
    }
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
      "dev-mobile",
      "orient-landscape",
      "orient-portrait"
    );
    this._screenEl.classList.add(
      `dev-${device.category}`,
      `orient-${device.orientation}`
    );
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
    this._emptyEl = document.createElement("div");
    this._emptyEl.className = "empty";
    this._emptyEl.innerHTML =
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="12" cy="13" r="8" /><path d="M12 9v4l2.5 2.5M9 2h6" /></svg><div>No timers running</div>';
    this._screenEl.appendChild(this._emptyEl);
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
    const now = Date.now();
    const live = new Set(this._timers.map((t) => t.timer_id));

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
    this._screenEl.classList.remove("cols-1", "cols-2", "cols-3", "cols-4");
    this._screenEl.classList.add(
      `cols-${gridColumns(this._timers.length, this._device ? this._device.category : undefined)}`
    );
    this._flip(before);
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
          <text class="name" data-name="${escapeHtml(timer.timer_id)}" x="100" y="56"></text>
          <text class="time" data-remaining="${escapeHtml(timer.timer_id)}" x="100" y="110"></text>
          <text class="state" data-state="${escapeHtml(timer.timer_id)}" x="100" y="140"></text>
          <text class="hint" data-hint="${escapeHtml(timer.timer_id)}" x="100" y="168"></text>
        </svg>
      </button>
      <div class="controls" data-controls="${escapeHtml(timer.timer_id)}"></div>
      <div class="error" data-error="${escapeHtml(timer.timer_id)}"></div>`;
    el._refs = {
      button: el.querySelector(".dial-button"),
      ring: el.querySelector(`[data-ring="${timer.timer_id}"]`),
      name: el.querySelector(`[data-name="${timer.timer_id}"]`),
      time: el.querySelector(`[data-remaining="${timer.timer_id}"]`),
      state: el.querySelector(`[data-state="${timer.timer_id}"]`),
      hint: el.querySelector(`[data-hint="${timer.timer_id}"]`),
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

    refs.name.textContent = shortName(timer.name);
    refs.time.textContent = isFinished ? "Done" : formatRemaining(left);
    refs.state.textContent = isFinished ? "" : isPaused ? "Paused" : "";
    refs.hint.textContent = isFinished
      ? "Tap to dismiss"
      : isPaused
        ? "Tap to resume"
        : "Tap to pause";

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
      if (timer.state === "finished") continue;
      const el = this._dialEls.get(timer.timer_id);
      if (!el) continue;
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
    if (this._busy.has(timerId)) return;
    this._busy.add(timerId);
    const el = this._dialEls.get(timerId);
    if (el) this._setBusy(el, true);
    try {
      await this._hass.callService("satellite_timers", service, {
        timer_id: timerId,
        ...(data || {}),
      });
      delete this._error[timerId];
    } catch (err) {
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
  }

  _onClick(event) {
    const target = event.target.closest("[data-action]");
    if (!target) return;
    const action = target.dataset.action;
    const timerId = target.dataset.timer;
    const timer = this._timers.find((t) => t.timer_id === timerId);
    if (!timer) return;

    if (action === "dial") {
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
      this._control(timerId, "restart_timer");
    }
  }

  _onCancelTap(timerId) {
    // Cancel is destructive and the screen is tapped with wet hands, so a
    // single stray tap must never kill a timer: the first tap only arms
    // the button, and only a second tap within a few seconds cancels.
    if (this._arming.has(timerId)) {
      clearTimeout(this._arming.get(timerId));
      this._arming.delete(timerId);
      this._refreshCancelButton(this._dialEls.get(timerId), { timer_id: timerId });
      this._control(timerId, "cancel_timer");
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
    remainingSeconds,
    progressFraction,
    discoverTimers,
    gridColumns,
    detectDevice,
    readDeviceEnv,
    TimerScreenCard,
  };
}
