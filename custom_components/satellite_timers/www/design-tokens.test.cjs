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
// Each entry names the token and how to find the inlined value in the
// card's CSS. The expected value always comes from the JSON
// (tokens.<group>.<key>.value) — never a literal in the test — so the
// JSON is the source of truth and the test fails when the card drifts
// from it.
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
  // Palette: the timer rings, the error text, the muted sub-text.
  ["palette", "color-timer-running", (v) => new RegExp(`\\.ring \\{[^}]*stroke: ${esc(v)};`)],
  ["palette", "color-timer-soon", (v) => `.dial.soon .ring { stroke: ${v};`],
  ["palette", "color-timer-finished", (v) => `.dial.finished .ring { stroke: ${v};`],
  ["palette", "color-timer-track", (v) => `.track { fill: none; stroke: ${v};`],
  ["palette", "color-error", (v) => `.error { color: ${v};`],
  ["palette", "color-danger", (v) => `.controls button.cancel { background: ${v};`],
  ["palette", "color-muted", (v) => `.state { color: ${v}; font-size: 18px;`],
  ["palette", "color-accent", (v) => `.nav-strip-button.active { background: ${v};`],
  // Dark surfaces use the ink token (toast + scroll badge).
  ["palette", "color-ink", (v) => new RegExp(`\\.toast \\{[^}]*background: ${esc(v)};`)],
  // On-accent / on-error text is white; the card spells it #fff (the
  // shorthand of the token's #FFFFFF) — same value, shorter spelling.
  // Asserted in context (the full rule), not as a bare "#fff".
  ["palette", "color-on-accent", () => `.nav-strip-button.active { background: ${tokenValue("palette", "color-accent")}; border-color: ${tokenValue("palette", "color-accent")}; color: #fff; font-weight: 800; }`],
  ["palette", "color-on-error", () => `.controls button.cancel { background: ${tokenValue("palette", "color-danger")}; border-color: ${tokenValue("palette", "color-danger")}; color: #fff; }`],
  // Card surfaces use the HA var with the #fff shorthand fallback.
  ["palette", "color-card", () => "background: var(--card-background-color, #fff);"],
  // Borders are the line token (the card used #e0e0e0 until this fix
  // round; the count pins all six border rules).
  ["palette", "color-line", (v) => `border: 1px solid ${v};`],
  // Paused ring opacity (the token lives in the palette group).
  ["palette", "state-paused-opacity", (v) => `.ring.paused { stroke: ${tokenValue("palette", "color-timer-running")}; opacity: ${v}; }`],
  // Motion: the soon pulse and the cancel-arm pulse.
  ["motion", "motion-soon-pulse", (v) => `animation: soon-pulse ${v};`],
  ["motion", "motion-cancel-arm-pulse", (v) => `animation: cancel-arm-pulse ${v};`],
  // Dial type (TIMER-6 hub minima).
  ["type", "font-dial-name-hub", (v) => new RegExp(`\\.name \\{[^}]*font-size: ${esc(v)};`)],
  ["type", "font-dial-time-hub", (v) => `font-size: clamp(40px, 11vw, ${v});`],
  ["type", "font-dial-state-hub", (v) => new RegExp(`\\.state \\{[^}]*font-size: ${esc(v)};`)],
  ["type", "font-dial-hint-hub", (v) => new RegExp(`\\.hint \\{[^}]*font-size: ${esc(v)};`)],
  ["type", "line-height-timer-name", (v) => new RegExp(`\\.name \\{[^}]*line-height: ${esc(v)};`)],
  // Targets: hub controls 80px, hub strip button 64px, tablet
  // controls 64px / strip 56px, phone strip button 48px.
  ["targets", "target-hub-max", (v) => new RegExp(`\\.controls button \\{[^}]*min-height: ${esc(v)};`)],
  ["targets", "target-hub-min", (v) => new RegExp(`\\.nav-strip-button \\{[^}]*min-height: ${esc(v)};`)],
  ["targets", "target-tablet-max", (v) => `.screen.dev-tablet .controls button { min-height: ${v};`],
  ["targets", "target-tablet-min", (v) => `.screen.dev-tablet .nav-strip-button { min-height: ${v};`],
  ["targets", "target-phone-min", (v) => `.screen.dev-mobile .nav-strip-button { min-height: ${v};`],
  // Radii: rows 16px, the circular dial button 50%.
  ["radius", "radius-row", (v) => `border-radius: ${v};`],
  ["radius", "radius-dial", (v) => `.dial-button:focus-visible { outline: 2px solid ${tokenValue("palette", "color-accent")}; outline-offset: 4px; border-radius: ${v}; }`],
  // Spacing: strip bottom pad 4px, dial-text side pad 8px, controls
  // gap 12px, screen gap 16px, screen padding 24px.
  ["spacing", "space-1", (v) => `padding: 16px 0 ${v};`],
  ["spacing", "space-2", (v) => `padding: 0 ${v} var(--controls-reserve) ${v};`],
  ["spacing", "space-3", (v) => `.controls { display: flex; gap: ${v};`],
  ["spacing", "space-4", (v) => new RegExp(`\\.screen \\{[^}]*gap: ${esc(v)};`)],
  ["spacing", "space-5", (v) => new RegExp(`\\.screen \\{[^}]*padding: ${esc(v)};`)],
  // Elevation: the strip button's card shadow and its hover lift.
  ["elevation", "shadow-card", (v) => `box-shadow: ${v};`],
  ["elevation", "shadow-lift", (v) => `box-shadow: ${v};`],
];

test("DESIGN-1 Part B: the timer card inlines the tokens it consumes (values read from the JSON)", () => {
  for (const [group, key, pattern] of TIMER) {
    has(TIMER_CSS, pattern(tokenValue(group, key)), `timer card must inline ${group}.${key}`);
  }
  // The line token's count pins all six border rules (controls,
  // back-bar, preset chip, nav-strip button, compact row,
  // compact restart) — a seventh border, or a drifted one, fails.
  const line = tokenValue("palette", "color-line");
  const count = TIMER_CSS.split(`border: 1px solid ${line};`).length - 1;
  assert.equal(count, 6, `expected 6 borders in ${line}, found ${count}`);
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
  "screen is transparent; the strip uses HA --primary-background-color": [
    "palette.color-screen-bg",
  ],
  "no notes/paper surfaces in the timer card": [
    "palette.color-paper",
    "palette.color-paper-line",
    "palette.color-on-paper",
  ],
  "no chips in the timer card (preset chips use the card background)": [
    "palette.color-chip-bg",
    "palette.color-on-chip",
  ],
  "no scrim in the timer card": ["palette.color-scrim"],
  "elevation the timer card does not use": [
    "elevation.shadow-accent",
    "elevation.shadow-tab",
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
  "radius tokens for surfaces the timer card does not have": [
    "radius.radius-chip",
    "radius.radius-input",
    "radius.radius-section",
  ],
  "spacing tokens the timer card does not consume": [
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
  "target band edges the card does not consume as values (its control sizes are asserted where they match a token: hub 80/64, tablet 64/56, phone 48)": [
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
  assert.equal(asserted.length, 35, `expected 35 asserted tokens, got ${asserted.length}`);
  assert.equal(skipped.length, 82, `expected 82 skipped tokens, got ${skipped.length}`);
});
