# Changelog

All notable changes to Satellite Timers are documented here. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the
version numbers follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Releases are tagged `vX.Y.Z`; the manifest version always matches the tag.

## [Unreleased]

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

[Unreleased]: https://github.com/k-mehta/ha-satellite-timers/compare/v0.1.9...HEAD
[0.1.9]: https://github.com/k-mehta/ha-satellite-timers/releases/tag/v0.1.9
[0.1.8]: https://github.com/k-mehta/ha-satellite-timers/releases/tag/v0.1.8
[0.1.7]: https://github.com/k-mehta/ha-satellite-timers/releases/tag/v0.1.7
[0.1.5]: https://github.com/k-mehta/ha-satellite-timers/releases/tag/v0.1.5
[0.1.4]: https://github.com/k-mehta/ha-satellite-timers/releases/tag/v0.1.4
[0.1.3]: https://github.com/k-mehta/ha-satellite-timers/releases/tag/v0.1.3
[0.1.2]: https://github.com/k-mehta/ha-satellite-timers/releases/tag/v0.1.2
[0.1.1]: https://github.com/k-mehta/ha-satellite-timers/releases/tag/v0.1.1
[0.1.0]: https://github.com/k-mehta/ha-satellite-timers/releases/tag/v0.1.0
