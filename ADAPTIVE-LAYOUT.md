# Adaptive layout

Both dashboard cards — the AnyList recipe card
(`k-mehta/ha-anylist-recipe-card`) and the Satellite Timers card
(`k-mehta/ha-satellite-timers`) — use **one** device-detection
mechanism and the same three device categories. This file is identical
in both repos; if you change the detection or the layout rules, change
them in both places.

## Device categories

| Category | What it is | How it is recognised |
| --- | --- | --- |
| `hub` | The kitchen kiosk: a Chromium kiosk on Raspberry Pi OS, 1920×1200 touch display, landscape | Touch screen, landscape, at least 1600 px wide |
| `desktop` | A large screen with a mouse or trackpad | Fine pointer (mouse), or a wide screen with no pointer information |
| `mobile` | A phone or tablet, portrait or landscape | Mobile user agent, or a narrow viewport, or a touch screen that is not hub-sized |

Orientation is separate from category: **landscape** is width ≥
height, **portrait** is width < height. The category picks the control
sizes and the base layout; orientation picks the grid (for example one
column in portrait, two in landscape on mobile).

## How detection works

Each card reads four signals from the browser it is running in:

- **Viewport width/height** — the strongest signal. The layout
  follows the viewport, not the device name, so a narrow desktop
  window gets the single-column layout too.
- **Pointer type** — `matchMedia("(pointer: coarse)")` is a
  finger/touch screen; `"(pointer: fine)"` is a mouse/trackpad.
- **Touch** — `"ontouchstart" in window`, or
  `navigator.maxTouchPoints > 0`.
- **User agent** — only as a tiebreaker for phones/tablets, never
  alone: a desktop browser can spoof it, and a kiosk can strip it.

Precedence (first match wins) — the unified §3.7 rule (Phase 3,
TIMER-2; consolidated spec rev 2):

1. The exact 1920×1200 panel signature (landscape) is the hub,
   whatever pointers are present: the kiosk has a mouse attached, so
   a fine pointer must never demote it to desktop.
2. A phone/tablet user agent (`Mobi`, `Android`, `iPhone`, `iPad`,
   `iPod`) is mobile.
3. A viewport narrower than 768 px is mobile.
4. Touch — or a coarse pointer, which IS a touch screen even when the
   touch flag mis-reports — on a landscape screen at least 1600 px
   wide is the hub.
5. A coarse pointer on anything else is mobile (phones and tablets).
6. A fine pointer is desktop. Bare width ≥1600 px + landscape NEVER
   implies hub on its own — that rule is FORBIDDEN (§3.7): it
   classifies every 1920×1080 / 1680×1050 / 1600×900 desktop monitor
   as a hub.
7. With no pointer information: a viewport at least 1024 px wide is
   desktop; anything else is mobile.

The detection is a pure function, `detectDevice(env)`, that takes
`{ width, height, coarsePointer, finePointer, touch, userAgent }` and
returns `{ category, orientation }`. It has no browser access, so it
is unit-testable in node. `readDeviceEnv()` reads the live browser
and returns `null` outside a browser, where the card keeps its base
layout. The card applies the result as classes on its root element —
`dev-hub`, `dev-desktop` or `dev-mobile`, plus `orient-landscape` or
`orient-portrait` — and the CSS keys the adaptive layout off them. It
re-detects on resize, so rotating a phone or resizing a window swaps
the layout live.

Known limits (every fallback still renders a usable layout, never a
broken one):

- A 1920×1200 laptop (rare) reads as the hub: it is indistinguishable
  from the wall panel by size alone. That is the documented residual
  misclassification (§3.7).
- A desktop touchscreen at 1920×1200 reads as the hub. That is fine:
  the hub layout is just big targets on a wide screen.
- A desktop touchscreen at 1920×1080 reads as the hub (touch +
  hub-size). That is the current behaviour in all three cards.
- A phone in desktop mode (no mobile user agent) still reads as
  mobile by width (rule 3).
- The hub is recognised by size + touch + landscape, not by name: if
  the kiosk display changes size, the thresholds above (1600 px hub
  minimum, 768 px mobile maximum, 1024 px desktop minimum) are the
  place to adjust.

## One mechanism, two copies

Each card is a single standalone file with no imports, so the
detection code cannot be shared by import. The block marked
`Device detection (SHARED MECHANISM)` is copied verbatim into each
card file — same code, same comments — and the two copies must stay
byte-identical. If you change the signals, the precedence, or the
category names in one card, change them in the other too, and update
this file. The tests in each repo cover the detection for that repo's
card.

## Layout per category

### Recipe card (`custom:anylist-recipe-view`)

The hub layout is the baseline and is unchanged: big targets
(≥ 64 px), multi-column grid, big readable text.

| | Hub (1920×1200 landscape) | Desktop (large screen, mouse) | Mobile (phone/tablet) |
| --- | --- | --- | --- |
| Grid | 3 columns (4 at ≥ 1700 px) | 3 columns (4 at ≥ 1700 px) | 1 column portrait; 2 columns landscape at ≥ 640 px width |
| Type | Base size | 18 px, tighter padding | 18 px, 16 px padding |
| Search / buttons | Base (64 px) | 52 px | 56 px |
| Collection chips | Base, scroll sideways with a fade | 44 px | 48 px, wrap onto a second row instead of scrolling |
| Recipe rows | Base (88 px, 68 px thumbnail) | 88 px, 68 px thumbnail | 88 px, 64 px thumbnail |
| Detail view | Max width 1150 px | Max width 1200 px | Full width, name 28 px, photo max height 260 px |
| Tabs | ≥ 64 px | 52 px | 56 px |

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

- **Node tests in each repo** cover `detectDevice` for every category
  and orientation (including the edge cases: desktop touchscreen
  reading as hub, narrow desktop window reading as mobile, phone in
  desktop mode, no-pointer-info fallbacks) and the layout classes
  being applied to the card root, plus a resize re-detection test.
- **Screenshots in a real Chromium** at all six sizes above, viewed
  before release: recipe list, detail view (all four tabs), a search,
  and a long-name recipe; timer screen with 1, 3, and 5 timers.
