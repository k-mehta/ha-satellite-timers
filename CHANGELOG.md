# Changelog

All notable changes to Satellite Timers are documented here. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the
version numbers follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Releases are tagged `vX.Y.Z`; the manifest version always matches the tag.

## [0.1.13] - 2026-11-09

### Added

- `satellite_timers.start_timer` service: start a timer by name and
  duration (in seconds), optionally naming the satellite (defaults to
  the kitchen satellite). Duplicate names are accepted; the service
  returns the created timer sensor's entity id. Recipe timer chips
  call it. A chip-started timer is marked service-started, so the
  timer-jump automation does not fire for it — the jump fires only
  for voice-started timers.
- Timer persistence across Home Assistant restarts: active, paused,
  and finished timers are stored on every change and re-created on
  start, so a restart no longer loses timers. Restored timers keep
  pause/resume/cancel/add-time controls.
- Timer sound policy (DESIGN-5): quiet hours 22:00–07:00 at 30% of
  timer volume, escalation from 40% rising 20 points every 10 s to
  full in 30 s, evaluated in local time. Published as attributes on
  finished timers; defaults are timer volume 100, quiet volume 30%,
  escalation on.
- Navigation strip on the Timers view (persistent Home / cross-view
  navigation with an active-view indicator, shown in kiosk mode and
  on phone; also on desktop in kiosk mode).
- On-screen timer creation on phone/tablet: preset chips (5/10/15/30
  min) calling `start_timer`. The hub stays voice-only, with a voice
  hint in the empty state.
- Undo for cancelling a timer: a 5 s toast restores the timer with
  its *remaining* time, not the full duration.
- Design tokens (`design-tokens.json` / `design-tokens.css`): one
  token source shared by the timer card, with a `--hub-*` twin for
  every token.

### Fixed

- Timers no longer vanish on device churn / re-pair (P8-1): saved
  state now persists the union of the current device's timers plus
  all held finished/restored records, any device; unknown-device
  records are kept across saves (never surfaced as sensors), logged
  with a warning naming the device id, and pruned only once older
  than 7 days.
- `started_via` ordering (P8-2): a service-started timer is now
  marked correctly on its very first STARTED event. The id is
  generated inside Home Assistant's `start_timer` and the event
  fires synchronously within that call, so the bridge sets an
  in-flight flag around the call and marks the id from the event
  itself, before notifying listeners. Applies to `start_timer` and
  `restart_timer`. Service-started ids are also discarded on
  `cancelled`.
- Service handlers no longer 500 on a stale or double-cancelled
  timer: `handle_pause`, `handle_resume`, `handle_cancel`, and
  `handle_add_time` now catch `KeyError` like `handle_restart` and
  `handle_dismiss` already did.
- One timer on the hub no longer overflows below the fold (P8-3):
  dials are capped by the height budget (viewport minus controls,
  padding, and nav strip), so the Cancel button stays visible; the
  same cap fixes the non-monotonic 5-timer scrolls / 12-timer fits
  (P8-6).
- The "More timers below" hint only fires when there really are more
  timers (P8-4); the scroll badge is gated on the dials, so it no
  longer overlaps dial time (P8-5).
- Device detection reordered (P8-16): the exact panel signature
  (1920×1200 landscape, or 1200×1920 rotated) is checked first; a
  fine pointer at 1024 px or wider is desktop, so touchscreen
  laptops read as desktop. The old "touch + width ≥ 1600 +
  landscape = hub" rule is deleted. Tablet is now its own category.
- Timer names are never truncated (TIMER-1): full name, wrapping,
  minimum 20 px on the hub. Finished timers show elapsed-since-finish
  ("Done — N min ago", TIMER-3). Duplicate names render with their
  start time (TIMER-11). Cancel arming clears on dial tap and
  Restart (TIMER-9). Finished timers collapse to compact rows past
  12, with a visible scroll indicator (TIMER-7).
- Dial text scales with dial size: small fitted dials (12-timer hub
  fit) take the many-mode text scale instead of bursting out of the
  ring.

## [0.1.12] - 2026-11-02

### Fixed

- Dismissing a finished timer now tells the satellite to stop ringing.
  Previously dismiss only cleared the dashboard sensor: the satellite
  keeps ringing until its timer handler hears a CANCELLED event, and
  Home Assistant never sends one for a timer that already finished
  (it pops the timer from the TimerManager the moment it goes off).
  Dismiss now sends CANCELLED for that timer to the satellite's
  handler, so tapping the dial (or saying "dismiss the timer") silences
  the alarm as well as clearing the sensor.

## [0.1.11] - 2026-11-02

### Added

- Adaptive layout for the timer-screen card: it detects whether it
  runs on the hub kiosk (1920×1200 touch display), a desktop, or a
  phone/tablet, in portrait or landscape, and changes control sizes and
  layout for that device (see ADAPTIVE-LAYOUT.md). The hub layout is
  unchanged (big dials, 80 px Cancel); desktop gets a denser grid (up
  to four timers in a row) with 56 px buttons; mobile keeps the hub
  grid with 64 px buttons, and in portrait the dials stay at least
  230 px tall with the screen scrolling instead of shrinking them.
  Detection re-runs on resize/rotation. The detection mechanism is
  shared verbatim with the AnyList recipe card
  (`k-mehta/ha-anylist-recipe-card`).

## [0.1.10] - 2026-11-01

### Fixed

- Declare the `http`, `intent`, and `lovelace` components as dependencies
  in the manifest (hassfest requires any component the integration uses
  to be listed), and drop the `min_hass_version` key, which is not a
  valid manifest key in current Home Assistant (the minimum version is
  declared in `hacs.json`).

## [0.1.9] - 2026-10-31

### Changed

- The on-screen Dismiss button is gone: tapping the dial is the only
  on-screen dismiss path for a finished timer (Restart stays underneath).
- Timer grid has no cap past six timers: rows keep being added as needed.

## [0.1.8] - 2026-10-31

### Changed

- Removed the on-screen +1 min button; adding time stays voice-only
  ("add 1 minute to the pasta timer").
- Finished timers are dismissed by tapping the dial.
- Timers view uses a grid for four or more timers.

## [0.1.7] - 2026-10-31

### Changed

- Landscape-first polish of the timer screen card.

## [0.1.5] - 2026-10-31

### Added

- The timer screen card is bundled into the integration: the integration
  serves the card file and registers it as a Lovelace resource on setup,
  so there is no separate card upload and no Resources form to fill in.

## [0.1.4] - 2026-10-31

### Added

- Control services: `pause_timer`, `resume_timer`, `cancel_timer`,
  `add_time`, `dismiss_timer`, `restart_timer`, all by timer id.

## [0.1.3] - 2026-10-31

### Fixed

- Removing the handler tap twice no longer wipes the timer handlers, which
  had made every set-timer voice command fail with "no timers" until a
  restart.

## [0.1.2] - 2026-10-31

### Fixed

- Timer sensors are named `sensor.satellite_timer_<id>` instead of taking
  the spoken name as the entity id.

## [0.1.1] - 2026-10-31

### Fixed

- Setup no longer refuses satellites whose device registers with empty
  identifiers.

## [0.1.0] - 2026-10-31

### Added

- First release: mirrors each active voice-assistant timer into a sensor
  (`active` / `paused` / `finished`), one sensor per timer, with the
  timer-screen Lovelace card bundled in.

[Unreleased]: https://github.com/k-mehta/ha-satellite-timers/compare/v0.1.11...HEAD
[0.1.11]: https://github.com/k-mehta/ha-satellite-timers/releases/tag/v0.1.11
[0.1.10]: https://github.com/k-mehta/ha-satellite-timers/releases/tag/v0.1.10
[0.1.9]: https://github.com/k-mehta/ha-satellite-timers/releases/tag/v0.1.9
[0.1.8]: https://github.com/k-mehta/ha-satellite-timers/releases/tag/v0.1.8
[0.1.7]: https://github.com/k-mehta/ha-satellite-timers/releases/tag/v0.1.7
[0.1.5]: https://github.com/k-mehta/ha-satellite-timers/releases/tag/v0.1.5
[0.1.4]: https://github.com/k-mehta/ha-satellite-timers/releases/tag/v0.1.4
[0.1.3]: https://github.com/k-mehta/ha-satellite-timers/releases/tag/v0.1.3
[0.1.2]: https://github.com/k-mehta/ha-satellite-timers/releases/tag/v0.1.2
[0.1.1]: https://github.com/k-mehta/ha-satellite-timers/releases/tag/v0.1.1
[0.1.0]: https://github.com/k-mehta/ha-satellite-timers/releases/tag/v0.1.0
