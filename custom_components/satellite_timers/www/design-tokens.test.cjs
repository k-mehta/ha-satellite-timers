// Tests for the design-token system (DESIGN-1) — design-tokens.json
// at the repo root is the source of truth, design-tokens.css is its
// human-readable twin, and the timer card inlines the values into its
// own shadow CSS (shadow DOM cannot @import the stylesheet at
// runtime). Both token files are VENDORED copies of pi-family-hub's
// dashboard/design-tokens.json + .css (pi-family-hub commit f9cc29d);
// refresh both files together when the tokens change upstream, or
// Part A fails. Before this test existed the token files were
// referenced by NOTHING in this repo — so the "token system" was a
// spec document, not a system: the next palette change would have
// been hand-edited with nothing to catch drift. This test is the
// wiring:
//
// Part A — every scalar token in the JSON appears as its --hub-*
//   variable in design-tokens.css (JSON <-> CSS parity).
// Part B — the timer card inlines the token values it consumes, with
//   the expected value read FROM the JSON (so a JSON change fails the
//   test until the card changes in the same commit). Every token the
//   card does NOT consume is listed in SKIPPED with its reason, and
//   the completeness check proves no token is silently unclassified.
//
// Two deliberate splits (documented, not drift): body text stays HA's
// --primary-text-color (#212121), not the ink token, and the screen
// stays transparent (no screen-bg token) — both are HA-theme
// behaviour, not token values the card consumes. The card's borders
// ARE the line token (#EDE8E3): the card used #e0e0e0 until this fix
// round, and the change is asserted here.
const assert = require("node:assert/strict");
const test = require("node:test");
const fs = require("node:fs");
const path = require("node:path");

// The card needs a DOM to load (HTMLElement/customElements), so stub
// the browser globals just enough for the module to define itself —
// the same harness the card tests use.
globalThis.HTMLElement = class {
  attachShadow() {
    return { addEventListener: () => {} };
  }
};
globalThis.customElements = { define: () => {} };

const { CARD_CSS: TIMER_CSS } = require("./timer-screen-card.js");
const CARD_SOURCE = fs.readFileSync(
  path.join(__dirname, "timer-screen-card.js"),
  "utf8",
);

const REPO_ROOT = path.join(__dirname, "..", "..", "..");
const TOKENS = JSON.parse(
  fs.readFileSync(path.join(REPO_ROOT, "design-tokens.json"), "utf8")
);
const TOKENS_CSS = fs.readFileSync(
  path.join(REPO_ROOT, "design-tokens.css"),
  "utf8"
);

const norm = (s) => String(s).replace(/\s+/g, " ").trim();
const esc = (s) => String(s).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

// All scalar tokens (group.key), for the completeness check.
function allScalarKeys() {
  const keys = [];
  for (const [group, tokens] of Object.entries(TOKENS)) {
    if (group === "meta" || group === "open_questions" || group === "decided_questions") continue;
    for (const [key, token] of Object.entries(tokens)) {
      if (!token || typeof token !== "object" || token.value === undefined) continue;
      const value = token.value;
      if (typeof value !== "string" && typeof value !== "number") continue;
      keys.push(`${group}.${key}`);
    }
  }
  return keys.sort();
}

// -- Part A: JSON <-> CSS parity -------------------------------------------
// Every token with a scalar value must appear in the CSS as its
// --hub-* variable with that exact value. The only exception is
// motion.rules: it is a prose rule, not a value, so it has no CSS
// variable — instead its text must appear in the CSS file's
// reduced-motion comment (same words, punctuation-insensitive).
test("DESIGN-1 Part A: every JSON token has its --hub-* twin in design-tokens.css", () => {
  let checked = 0;
  const skipped = [];
  for (const [group, tokens] of Object.entries(TOKENS)) {
    if (group === "meta" || group === "open_questions" || group === "decided_questions") continue;
    for (const [key, token] of Object.entries(tokens)) {
      if (!token || typeof token !== "object" || token.value === undefined) {
        skipped.push(`${group}.${key} (not a token)`);
        continue;
      }
      const value = token.value;
      if (typeof value !== "string" && typeof value !== "number") {
        skipped.push(`${group}.${key} (non-scalar value)`);
        continue;
      }
      checked++;
      if (group === "motion" && key === "rules") {
        const prose = norm(value).replace(/[.;,]/g, "").toLowerCase();
        const cssProse = norm(TOKENS_CSS).replace(/[.;,]/g, "").toLowerCase();
        assert.ok(
          cssProse.includes(prose),
          `motion.rules prose must appear in the CSS reduced-motion comment: ${value}`
        );
        continue;
      }
      const needle = norm(`--hub-${key}: ${value}`);
      assert.ok(
        norm(TOKENS_CSS).includes(needle),
        `token ${group}.${key} missing its CSS twin: ${needle}`
      );
    }
  }
  // The count pins the coverage: if a token is added to the JSON, it
  // is checked automatically; if this number changes, the list below
  // must change with it (a token silently dropped from the JSON
  // would otherwise pass by absence).
  assert.equal(checked, 118, `expected 118 scalar tokens, checked ${checked}`);
  assert.deepEqual(skipped, ["icon.names (not a token)"]);
});

// -- Part B: the timer card inlines the tokens it consumes ----------------
// Each entry names the token and how to find the inlined value. The
// expected value always comes from the JSON
// (tokens.<group>.<key>.value) — never a literal in the test — so the
// JSON is the source of truth and the test fails when the card drifts
// from it. The fourth element is the haystack: "css" (the default)
// or "source" for values that live in the card's JS — the ring
// colours and the paused opacity are set in `_updateDial`/`_ringColor`
// next to the ring geometry, so Part B asserts those against the
// source instead of pretending a CSS rule owns them. Rewritten for
// the Wave 5 from-scratch card: the old card's classes (.ring,
// .controls, .nav-strip-button, .toast) are gone with it.
function tokenValue(group, key) {
  const token = TOKENS[group] && TOKENS[group][key];
  assert.ok(token && token.value !== undefined, `unknown token ${group}.${key}`);
  return String(token.value);
}
function has(css, pattern, label) {
  if (typeof pattern === "string") assert.ok(css.includes(pattern), label);
  else assert.match(css, pattern, label);
}

const TIMER = [
  // Palette: the ring, the error text, the muted sub-text.
  ["palette", "color-timer-running", (v) => new RegExp(`\\.dial-ring-arc \\{[^}]*stroke: ${esc(v)};`), "css"],
  ["palette", "color-timer-soon", (v) => new RegExp(`if \\(this\\._isSoon\\(record, nowMs\\)\\) return "${esc(v)}";`), "source"],
  ["palette", "color-timer-finished", (v) => new RegExp(`if \\(record\\.state === "finished"\\) return "${esc(v)}";`), "source"],
  ["palette", "color-timer-track", (v) => new RegExp(`\\.dial-ring-track \\{[^}]*stroke: ${esc(v)};`), "css"],
  ["palette", "color-error", (v) => new RegExp(`\\.dial-error \\{[^}]*color: ${esc(v)};`), "css"],
  ["palette", "color-danger", (v) => new RegExp(`\\.dial-stop \\{[^}]*background: ${esc(v)};`), "css"],
  ["palette", "color-muted", (v) => new RegExp(`\\.dial-hint \\{[^}]*color: ${esc(v)};`), "css"],
  ["palette", "color-accent", (v) => new RegExp(`\\.stepper-start \\{[^}]*background: ${esc(v)};`), "css"],
  // Dark surfaces use the ink token (screen text).
  ["palette", "color-ink", (v) => new RegExp(`\\.screen \\{[^}]*color: ${esc(v)};`), "css"],
  // On-accent / on-error text is white. The old card spelled it #fff
  // (the shorthand of #FFFFFF); the new card spells the token
  // literally.
  ["palette", "color-on-accent", (v) => new RegExp(`\\.stepper-start \\{[^}]*color: ${esc(v)};`), "css"],
  ["palette", "color-on-error", (v) => new RegExp(`\\.dial-stop \\{[^}]*color: ${esc(v)};`), "css"],
  // Card surfaces spell the token literally (#FFFFFF), not as the HA
  // var the old card used.
  ["palette", "color-card", (v) => `background: ${v};`, "css"],
  // Borders are the line token (the count below pins all three
  // 1 px border rules: dial, stepper panel, repair procedure).
  ["palette", "color-line", (v) => `border: 1px solid ${v};`, "css"],
  // The screen is painted now (W5), so its background is the screen
  // token — the old card left the screen transparent.
  ["palette", "color-screen-bg", (v) => new RegExp(`\\.screen \\{[^}]*background: ${esc(v)};`), "css"],
  // Paused ring opacity (the token lives in the palette group) — set
  // in JS with the ring geometry.
  ["palette", "state-paused-opacity", (v) => new RegExp(`record\\.state === "paused" \\? "${esc(v)}" : "1"`), "source"],
  // Dial type (TIMER-6 hub minima). The state line and the hint line
  // are one element (.dial-hint) in the new card, so both 18 px
  // tokens match the same rule.
  ["type", "font-dial-name-hub", (v) => new RegExp(`\\.dial-name \\{[^}]*font-size: ${esc(v)};`), "css"],
  ["type", "font-dial-time-hub", (v) => `font-size: clamp(40px, 11vw, ${v});`, "css"],
  ["type", "font-dial-state-hub", (v) => new RegExp(`\\.dial-hint \\{[^}]*font-size: ${esc(v)};`), "css"],
  ["type", "font-dial-hint-hub", (v) => new RegExp(`\\.dial-hint \\{[^}]*font-size: ${esc(v)};`), "css"],
  ["type", "line-height-timer-name", (v) => new RegExp(`\\.dial-name \\{[^}]*line-height: ${esc(v)};`), "css"],
  // Targets: dial controls 80 px on every device. There are no
  // per-device branches in the new card (Nov 11 ruling), so the
  // tablet 64 px target is skipped below.
  ["targets", "target-hub-max", (v) => new RegExp(`\\.dial-cancel, \\.dial-restart, \\.dial-stop \\{[^}]*min-height: ${esc(v)};`), "css"],
  // Radii: rows 16 px.
  ["radius", "radius-row", (v) => `border-radius: ${v};`, "css"],
  // Spacing: dial gap 12 px, dials grid gap 16 px.
  ["spacing", "space-3", (v) => new RegExp(`\\.dial \\{[^}]*gap: ${esc(v)};`), "css"],
  ["spacing", "space-4", (v) => new RegExp(`\\.dials \\{[^}]*gap: ${esc(v)};`), "css"],
  // Elevation: the new card has no shadow anywhere (as before).
];

test("DESIGN-1 Part B: the timer card inlines the tokens it consumes (values read from the JSON)", () => {
  for (const [group, key, pattern, haystack = "css"] of TIMER) {
    const text = haystack === "source" ? CARD_SOURCE : TIMER_CSS;
    has(text, pattern(tokenValue(group, key)), `timer card must inline ${group}.${key}`);
  }
  // The line token's count pins all three 1 px border rules (dial,
  // stepper panel, repair procedure — the entry-row's border-top is a
  // top border, not counted). A fourth border, or a drifted one,
  // fails.
  const line = tokenValue("palette", "color-line");
  const count = TIMER_CSS.split(`border: 1px solid ${line};`).length - 1;
  assert.equal(count, 3, `expected 3 borders in ${line}, found ${count}`);
});

// Tokens Part B does NOT assert, with the reason each is skipped.
// The completeness test below proves every scalar token is either
// asserted above, covered by Part A (motion.rules), or listed here —
// a token silently dropped from this list fails.
const SKIPPED = {
  "no success state in the timer card": [
    "palette.color-success",
    "palette.color-on-success",
  ],
  "no ratings/stars in the timer card": ["palette.color-star"],
  "same value as color-timer-soon; covered by the soon/finished assertions": [
    "palette.color-warning",
    "palette.color-on-warning",
  ],
  "no notes/paper surfaces in the timer card": [
    "palette.color-paper",
    "palette.color-paper-line",
    "palette.color-on-paper",
  ],
  "preset chips use the card background (#FFFFFF), not the chip background token": [
    "palette.color-chip-bg",
    "palette.color-on-chip",
  ],
  "no scrim in the timer card": ["palette.color-scrim"],
  "elevation the timer card does not use": [
    "elevation.shadow-accent",
    "elevation.shadow-tab",
  ],
  "no nav strip in the timer card — no min-height, no strip padding, no shadow": [
    "targets.target-hub-min",
    "targets.target-tablet-min",
    "targets.target-phone-min",
    "spacing.space-1",
    "elevation.shadow-card",
    "elevation.shadow-lift",
  ],
  "no icons sized by the timer card": [
    "icon.icon-desktop",
    "icon.icon-hub",
    "icon.icon-phone",
    "icon.icon-tablet",
  ],
  "no screensaver in the timer card": [
    "motion.motion-screensaver-enter",
    "motion.motion-screensaver-exit",
  ],
  "wave 1: the cancel arm is a width drain (arm-drain), not a pulse — the arm-pulse token is not consumed": [
    "motion.motion-cancel-arm-pulse",
  ],
  "W5: no soon pulse — 'soon' is shown by the amber ring color, not an animation": [
    "motion.motion-soon-pulse",
  ],
  "W5: no per-device branches (Nov 11 ruling) — one 80 px control minimum on every device; the tablet 64 px target is not consumed": [
    "targets.target-tablet-max",
  ],
  "wave 1: the .dial-text overlay (and its --controls-reserve padding) is deleted — the name sits above the ring": [
    "spacing.space-2",
  ],
  "radius tokens for surfaces the timer card does not have (the ring is an SVG inside a square button — no circular dial button)": [
    "radius.radius-chip",
    "radius.radius-dial",
    "radius.radius-input",
    "radius.radius-section",
  ],
  "spacing tokens the timer card does not consume (screen padding is 16 px, space-4, on every side)": [
    "spacing.space-5",
    "spacing.space-6",
    "spacing.space-7",
    "spacing.spacing-inner-gap",
    "spacing.spacing-row-gap",
    "spacing.spacing-screen-pad-mobile",
    "spacing.spacing-screen-pad-x",
    "spacing.spacing-screen-pad-y",
    "spacing.spacing-section-gap",
    "spacing.spacing-section-pad",
    "spacing.spacing-section-pad-mobile",
  ],
  "state.* are var(--hub-...) references, not literals — the card does not spell them": [
    "state.state-active-bg",
    "state.state-active-fg",
    "state.state-error",
    "state.state-finished",
    "state.state-offline",
    "state.state-paused",
    "state.state-running",
    "state.state-soon",
  ],
  "target band edges the card does not consume as values (its control size is asserted where it matches a token: dial controls 80 px)": [
    "targets.target-chip-desktop",
    "targets.target-chip-hub",
    "targets.target-chip-mobile",
    "targets.target-desktop-max",
    "targets.target-desktop-min",
    "targets.target-dial-min-hub",
    "targets.target-note-delete-min",
    "targets.target-phone-max",
    "targets.target-time-chip-hub-min",
    "targets.target-time-chip-phone-min",
  ],
  "type tokens for text the timer card does not render (home/recipe text, clock, chips, dates)": [
    "type.font-body-desktop",
    "type.font-body-hub",
    "type.font-body-mobile",
    "type.font-body-tablet",
    "type.font-chip-desktop",
    "type.font-chip-hub",
    "type.font-chip-mobile",
    "type.font-clock-desktop",
    "type.font-clock-hub",
    "type.font-clock-mobile",
    "type.font-clock-tablet",
    "type.font-date-desktop",
    "type.font-date-hub",
    "type.font-date-mobile",
    "type.font-date-tablet",
    "type.font-family",
    "type.font-meta",
    "type.font-section-title-desktop",
    "type.font-section-title-hub",
    "type.font-section-title-mobile",
    "type.font-section-title-tablet",
    "type.font-timer-detail-desktop",
    "type.font-timer-detail-hub",
    "type.font-timer-detail-mobile",
    "type.font-timer-detail-tablet",
    "type.font-timer-name-desktop",
    "type.font-timer-name-hub",
    "type.font-timer-name-mobile",
    "type.font-timer-name-tablet",
    "type.line-height-body",
    "type.line-height-clock",
  ],
};

test("DESIGN-1 Part B completeness: every scalar token is asserted, Part-A-covered, or skipped with a reason", () => {
  const asserted = TIMER.map(([group, key]) => `${group}.${key}`).sort();
  const skipped = Object.values(SKIPPED).flat().sort();
  // motion.rules is prose, covered by Part A — not by Part B.
  const partACovered = ["motion.rules"];
  const all = allScalarKeys();
  assert.deepEqual(
    [...asserted, ...skipped, ...partACovered].sort(),
    all,
    "every scalar token must be asserted, Part-A-covered, or skipped with a reason"
  );
  assert.equal(asserted.length, 24, `expected 24 asserted tokens, got ${asserted.length}`);
  assert.equal(skipped.length, 93, `expected 93 skipped tokens, got ${skipped.length}`);
});
