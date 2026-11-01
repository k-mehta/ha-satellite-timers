# Contributing to Satellite Timers

## Workflow

- Work happens on feature branches; `main` stays clean.
- A feature is squash-merged into `main` only after it is fully tested.
- Each feature is its own commit — never bundle several features into one
  "miscellaneous" commit.
- No half-broken work lands anywhere final. If a change cannot be proven
  against a real Home Assistant, say so in the pull request instead of
  asking the maintainer to test it.

## Tests

The repository ships standalone smoke tests that stub Home Assistant's
imports and drive the real modules (no test framework needed):

```sh
python3 test_bridge.py
python3 test_sensor_device_info.py
```

CI also runs hassfest validation, HACS validation, and these tests on
every push and pull request. A pull request is only merged when all of
them pass.

## Releases

1. Add a `CHANGELOG.md` entry under a new `## [X.Y.Z] - YYYY-MM-DD`
   heading.
2. Bump `version` in `custom_components/satellite_timers/manifest.json`
   to match.
3. Tag the merge commit `vX.Y.Z` and push the tag. The release workflow
   validates the tag against the manifest, builds the HACS ZIP
   (`satellite-timers.zip`), and publishes the GitHub release with the
   changelog notes.
4. HACS picks the release up automatically (`zip_release` in `hacs.json`).

The version stays put in feature branches: only the release step bumps it.
