# Adaptive layout

All four dashboard cards — the AnyList recipe card
(`k-mehta/ha-anylist-recipe-card`), the Satellite Timers card
(`k-mehta/ha-satellite-timers`), the home card and the nav strip in
`k-mehta/pi-family-hub` — use **one** identity mechanism and the same
four device categories. The mechanism lives in one module,
`installer/dashboard/family-hub-core.js` in pi-family-hub, which the
cards import; there are no per-card copies (enforced by the
no-copies test, `installer/dashboard/no-copies.test.cjs`). This file
is identical in both card repos; if you change the identity or the
layout rules, change them in both places.

## Device categories

| Category | What it is | How it is recognised |
| --- | --- | --- |
| `hub` | The kitchen kiosk: the Chromium kiosk on Raspberry Pi OS | The browser's registered Browser Mod id matches the hub id — identity, never size |
| `desktop` | A large screen with a mouse or trackpad | Fine pointer at 1024 px or wider, or a wide screen with no pointer information |
| `tablet` | A touch tablet, either orientation | Touch screen 768–1024 px wide with the shorter side at least 600 px |
| `mobile` | A phone, portrait or landscape | A narrow viewport, or a coarse pointer with no fine pointer |

Orientation is separate from category: **landscape** is width ≥
height, **portrait** is width < height. The category picks the control
sizes and the base layout; orientation picks the grid (for example one
column in portrait, two in landscape on mobile).

## How identity works

Identity is the registered Browser Mod browser id — never a screen
size, never the `?kiosk` launch parameter alone. `identify(hass, env)`
in core.js resolves the browser's id (`resolveBrowserModId`: the id
stored by Browser Mod in local storage, or the exactly-one-match
`browser_path` sensor) and compares it with the hub id registered
in the core (`HUB_BROWSER_ID`, substituted at deploy time by the
wizard). A match is `kind: "wall"`; anything else is `kind:
"unknown"` and runs desktop-safe. A mismatch on a `?kiosk` launch
shows the identity-mismatch banner; a mismatch without `?kiosk`
logs a console warning and shows full Home Assistant chrome. With
no hub id registered at all (deploy, or a no-browser wizard run) the
card runs desktop-safe with no banner.

`?kiosk` is launch-only: it tells the card the kiosk was launched
from the kiosk URL (so the nav strip shows even on desktop, where
Home Assistant's own tabs would otherwise be the navigation). It
never confers hub identity — a desktop tab opened with `?kiosk`
still reads as a desktop.

## How the layout category is picked

`deviceFromIdentity(identity, widthPx, heightPx)` in core.js maps an
`identify()` result plus the live viewport to a category. The wall
identity is checked FIRST: a wall is `hub` at any size, in either
orientation — if the kiosk display changes size, nothing here needs
to change. For everything else the mapping uses only viewport width
and pointer/touch signals, in this order:

1. The wall (`kind: "wall"`) is `hub`.
2. A viewport narrower than 768 px is `mobile`.
3. A touch screen 768–1024 px wide with the shorter side at least
   600 px is `tablet`.
4. A fine pointer at 1024 px or wider is `desktop`.
5. A coarse pointer with no fine pointer is `mobile`.
6. A fine pointer is `desktop`.
7. With no pointer information: a viewport at least 1024 px wide is
   `desktop`; anything else is `mobile`.

There is no user-agent sniffing anywhere in the mechanism, and no
exact-size check: a 1920×1080 desktop monitor is `desktop`, a
1920×1200 laptop is `desktop`, and the 1920×1200 wall is `hub`
because its browser id matches, not because of its size. The card
applies the result as classes on its root element — `dev-hub`,
`dev-desktop`, `dev-tablet` or `dev-mobile`, plus `orient-landscape`
or `orient-portrait` — and the CSS keys the adaptive layout off
them. It re-detects on resize, so rotating a phone or resizing a
window swaps the layout live.

Known limits (every fallback still renders a usable layout, never a
broken one):

- A browser whose id is not registered (for example a fresh kiosk
  before the wizard runs, or any other browser) runs desktop-safe:
  usable, but without the wall-only behaviour.
- A phone in desktop mode still reads as mobile by width (rule 2).
- A touchscreen laptop with no fine pointer outside the tablet band
  reads as mobile — it is indistinguishable from a touch-only
  screen by these signals alone.

## One mechanism, one copy

The identity and layout code lives in exactly one place:
`installer/dashboard/family-hub-core.js`. Each card imports it
(`_loadCore` / the core-injection harness in the tests) and calls
`identify()` and `deviceFromIdentity()` — no card carries its own
detection. The old per-card `detectDevice` copies, the exact
1920×1200 panel signature, and the kiosk session flag are deleted;
the no-copies test fails the build if any of them reappears outside
core.js.

## Layout per category

### Recipe card (`custom:anylist-recipe-view`)

The hub layout is the baseline: big targets (≥ 72 px), multi-column
grid, big readable text — the hub base is 24 px type (steps 26 px),
readable from the counter at 3 feet on a 10.1" 192 ppi display. The
cooking view shows ingredients and steps together: two columns on
the hub (ingredients left, steps right), stacked on a phone
(ingredients above steps, step tracker sticky). There is no photo
and no metadata in the cooking view — the photo lives only in the
list, and there is no Details tab. Two tabs: Cook and Notes. The
Notes tab is editable (a note editor with Save, and five tappable
rating stars); the Cook tab has a transient serving scaler above the
ingredients (it changes only the quantities shown, never the stored
recipe).

| | Hub (1920×1200 landscape) | Desktop (large screen, mouse) | Mobile (phone/tablet) |
| --- | --- | --- | --- |
| Grid | 3 columns (4 at ≥ 1700 px) | 3 columns (4 at ≥ 1700 px) | 1 column portrait; 2 columns landscape at ≥ 640 px width |
| Type | Base 24 px (steps 26 px) | 18 px, tighter padding | 18 px, 16 px padding |
| Search / buttons | Base (72 px) | 52 px | 56 px |
| Collection chips | Base, scroll sideways with a fade | 44 px | 48 px, wrap onto a second row instead of scrolling |
| Recipe rows | Base (112 px, 96 px thumbnail) | 88 px, 68 px thumbnail | 88 px, 64 px thumbnail |
| Detail view | Max width 1500 px, two-column cooking view | Max width 1200 px, two-column cooking view | Full width, name 28 px, stacked cooking view, sticky step tracker |
| Tabs | 72 px | 52 px | 56 px |
| Notes editor | 24 px text, 180 px min height, 72 px Save | 19 px, 140 px min height, 52 px | 19 px, 160 px min height, 56 px |
| Rating stars | 72 px targets, 44 px stars | 52 px, 32 px stars | 56 px, 36 px stars |
| Serving scaler | 72 px −/+ buttons, 26 px count | 52 px, 20 px count | 56 px, 22 px count |

Long recipe names wrap to two lines in the list (then ellipsis) and
in full in the detail view. Search, tabs, ingredient headings and the
one-`get_recipes`-per-view-open load behave the same at every size.

### Timer screen card (`custom:timer-screen`)

The hub layout is the baseline and is unchanged: big dials, Cancel
always visible (80 px, tap-to-arm), tap a dial to pause/resume (or
dismiss a finished timer), Restart under a finished timer, no +1 min
button, no Dismiss button.

| | Hub (1920×1200 landscape) | Desktop (large screen, mouse) | Mobile (phone/tablet) |
| --- | --- | --- | --- |
| Grid | ≤ 3 timers in one row; 4 → 2×2; 5+ → 3 columns | ≤ 4 in one row; 5+ → 4 columns | Same as hub (≤ 3 in one row; 4 → 2×2; 5+ → 3 columns) |
| Buttons | 80 px | 56 px | 64 px |
| Portrait | — | — | Dials at least 230 px tall; the screen scrolls instead of shrinking the dials, so 5 timers stay readable |
| Landscape | — | — | 3 columns; second row scrolls on a short phone screen |

All timer behaviour is the same at every size: tap dial to
pause/resume/dismiss, Cancel tap-to-arm, finished timer with Restart,
grid for 4+ timers.

## Support matrix

| Device | Recipe card | Timer card |
| --- | --- | --- |
| Hub kiosk 1920×1200 landscape | Supported | Supported |
| Desktop 1440×900 (and wider) | Supported | Supported |
| Tablet 1024×768 landscape | Supported | Supported |
| Tablet 768×1024 portrait | Supported | Supported |
| Phone 390×844 portrait | Supported | Supported |
| Phone 844×390 landscape | Supported | Supported |

No horizontal scrolling and no clipped text at any of these sizes;
on a phone the list and the timer screen scroll vertically, as
expected.

## How it is verified

- **Node tests in pi-family-hub** cover `identify()` and
  `deviceFromIdentity()` for every category and orientation
  (including the edge cases: a 1920×1080 desktop monitor reading as
  desktop, the wall reading as hub at any size, a narrow desktop
  window reading as mobile, no-pointer-info fallbacks, an
  unregistered browser running desktop-safe) and the layout classes
  being applied to the card root, plus a resize re-detection test.
  The per-card DOM suites inject the real core and cover the same
  mapping through each card.
- **The no-copies test** (`installer/dashboard/no-copies.test.cjs`)
  fails the build if a per-card `detectDevice`, an exact-size
  signature constant, or any kiosk session-flag machinery reappears
  outside core.js.
- **Screenshots in a real Chromium** at all six sizes above, viewed
  before release: recipe list, cooking view (Cook tab with
  ingredients + steps, and the Notes tab), a search, and a long-name
  recipe; timer screen with 1, 3, and 5 timers. The Notes tab is
  also screenshotted with the editor and the rating stars, and the
  Cook tab with the serving scaler in use, so the editing controls
  are seen at hub and phone sizes — not just asserted by tests.
