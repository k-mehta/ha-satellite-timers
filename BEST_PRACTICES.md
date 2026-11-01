# Best practices applied to this repository

How this repository follows Home Assistant and HACS conventions, where each
rule comes from, and what is deliberately not done yet. Version 0.1.9.

## Sources

- Home Assistant developer docs, "The integration manifest":
  https://developers.home-assistant.io/docs/creating_integration_manifest
  (required keys, key sort order, `quality_scale`)
- Home Assistant contributors docs, "All rules" (quality scale, 54 rules):
  https://developers.home-assistant.io/docs/quality-scale/rules/
- HACS docs, "Integrations":
  https://hacs.xyz/docs/publish/integration/
  (repository structure, manifest keys, releases, reference template
  `ludeeus/integration_blueprint`)
- HACS docs, "Include default repositories":
  https://hacs.xyz/docs/publish/include/
  (add the hassfest and HACS actions and make them pass before submitting)
- Exemplar repository `moryoav/ha-anylist` (HACS integration with
  hassfest + HACS validation + tag-triggered ZIP releases, CHANGELOG-driven
  release notes, MIT license) — the workflows and release script here
  follow its shape.

## Applied

| Practice | Source | State in this repo |
|---|---|---|
| `manifest.json` defines `domain`, `documentation`, `issue_tracker`, `codeowners`, `name`, `version` | HACS integration requirements; HA manifest docs | Done. `documentation` and `issue_tracker` point at this repo; `codeowners` is `@k-mehta`. |
| Manifest keys sorted: `domain`, `name`, then the rest alphabetical | HA manifest docs (hassfest enforces it) | Done. CI checks the order. |
| `version` is SemVer and matches the release tag | HA manifest docs; HACS releases | `0.1.9`. The release workflow refuses a tag whose version differs from the manifest. |
| `min_hass_version` set | HA manifest docs | `2025.12.0`, matching `hacs.json`. |
| `config_flow: true`, `single_config_entry: true` | HA manifest docs | Done — the integration is UI-configured and allows exactly one entry. |
| `hacs.json` has `name`, `render_readme`, `homeassistant` | HACS docs; exemplar | Done. `homeassistant` matches `min_hass_version`. |
| Releases are ZIPs (`zip_release`, `filename`, `hide_default_branch`) | HACS docs ("releases are preferred"); exemplar | Done. `satellite-timers.zip` is built by CI from the tag. HACS installs releases only. |
| One integration per repo, all files under `custom_components/<domain>/` | HACS repository structure | Done. The timer-screen card ships inside the integration at `www/`. |
| Hassfest validation in CI | HACS "include default repositories"; exemplar | `.github/workflows/validate.yml` runs `home-assistant/actions/hassfest@master`. |
| HACS validation in CI | HACS "include default repositories"; exemplar | Same workflow runs `hacs/action@main` with `category: integration`, ignoring `brands` until a brand is submitted (below). |
| Smoke tests in CI | Quality scale Bronze (automated tests) | `.github/workflows/tests.yml` runs `test_bridge.py` and `test_sensor_device_info.py` on Python 3.12/3.13, plus `compileall`, JSON/YAML validity, manifest order, and `node --check` on the card. |
| `services.yaml` describes every service | HA service descriptions (Developer Tools → Services shows them) | Done — all six services, fields matching the `voluptuous` schemas in `__init__.py`. |
| `strings.json` + `translations/en.json` for all UI text | HA translations (Gold tier `entity-translations` direction) | Done. Config flow, options flow, aborts, errors, repair issue. |
| Sensors have a `unique_id` | Quality scale Bronze `entity-unique-id` | Done — `satellite_timers_<timer_id>`. |
| Repair issue when HA internals change | Quality scale Gold `repair-issues` direction | Done — `assist_supported()` guard raises a repair issue and switches off instead of guessing. |
| README for a general audience | HACS `render_readme`; quality scale `docs-installation-instructions`, `docs-actions` | Done — HACS install, manual install, setup, services table, card use, troubleshooting, how it works. No references to any one setup. |
| CHANGELOG drives release notes | Exemplar release flow | `CHANGELOG.md` (Keep a Changelog format). The release workflow refuses a tag with no changelog entry. |
| MIT license | Exemplar; standard for HACS integrations | `LICENSE`, copyright Kshitij Mehta. |
| `.gitignore` covers Python, Node, HA config, build artifacts, secrets | Exemplar `.gitignore`; secrets hygiene | Done — `__pycache__/`, `.venv/`, `node_modules/`, `config/`, `*.zip`, `.env`, `*.pem`, `*.key`. |
| No secrets in the repo | Owner's rule for public repos | Verified by scan (see below). The integration needs no credentials at all: setup is a UI config flow, so there is nothing that would belong in a `.env`. Any future dev/test value goes in a gitignored `.env` and is never committed. |

## Deliberate choices

- **No `quality_scale` claim in the manifest.** The scale is a
  self-assessed tier backed by hassfest's rule checks. This integration
  meets the Bronze shape (UI setup, docs, tests) but has known gaps
  (below), so no tier is claimed until the gaps are closed and the claim
  is verified.
- **Standalone smoke tests at the repository root, not a
  `pytest-homeassistant-custom-component` suite under `tests/`.** The two
  tests stub Home Assistant's imports and drive the real modules; they
  are the tests that proved the risky parts (handler tap, device-info
  fix). Moving them would churn paths the local rig depends on. A full
  pytest suite is the follow-up (below).
- **HACS validation ignores `brands` for now.** See below.
- **The manifest/hacs.json fixes in this tree ship with the next version
  bump.** The installed 0.1.9 copy has the old manifest (no
  `documentation`/`issue_tracker`/`codeowners`). Tagging `v0.1.9` from
  this tree publishes the fixed metadata, but HACS will not offer it as
  an update over the installed 0.1.9 because the version number is the
  same. The fixes take effect from the next bump.

## Not done — flagged, not silently changed

These would need behaviour or architecture changes, so they are recorded
here instead of being done quietly:

| Gap | Why it is a gap | What it would take |
|---|---|---|
| `action-setup` (Bronze): actions must be registered when the integration loads, not per config entry | Services are registered in `_register_services(hass, entry, bridge)` from `async_setup_entry`, scoped to that entry's bridge | Restructure service registration into `async_setup` with entry-scoped resolution. Behaviour change — needs its own feature branch and rig verification. |
| `runtime-data` (Bronze): runtime data in `ConfigEntry.runtime_data` | State lives in `hass.data[DOMAIN]` (per-entry dicts plus process flags) | Migrate per-entry state to `entry.runtime_data`. Refactor — needs its own feature branch and rig verification. |
| Config-flow test coverage and >95% module coverage (Bronze/Silver) | Only standalone smoke tests exist; no pytest suite, no config-flow tests | Build a `tests/` suite on `pytest-homeassistant-custom-component` covering config flow, options flow, sensors, and services. |
| `brands` (Bronze): logo and icon in `home-assistant/brands` | No brand assets submitted | Submit a PR to `home-assistant/brands` with icon/logo, then drop `ignore: brands` from the HACS validation workflow. Needed before asking HACS to list the repo by default. |
| `test-before-configure` (Bronze) | The config flow does not test a connection before creating the entry | Not applicable in the usual sense: there is no device or cloud connection to test — the flow checks the local TimerManager is present (`not_ready` abort). Recorded so the exception is explicit. |
| `common-modules` (Bronze) | No `coordinator.py`/`entity.py` split | Not applicable: this integration has no coordinator; the sensor is a single thin entity over the bridge. Recorded so the exception is explicit. |

## Secrets scan

2026-11-01: scanned the final tree (code, tests, workflows, README,
CHANGELOG, `services.yaml`) for passwords, tokens, API keys, bearer
credentials, private keys, IP addresses, personal names, and email
addresses. Clean — no matches. The scan is repeated before any push to
the public repository.
