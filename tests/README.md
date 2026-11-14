# Tests for the Wave W2 bridge (from-scratch build)

## Layout

- `test_bridge_v2.py` — pytest suite for the **pure** modules only:
  `records.py` (state machine), `resolution.py` (command_id + satellite
  resolution), `errors.py` (typed errors + the ERS-1 ladder),
  `tm_observer.py` (the TimerManager tap). The suite stubs the tiny
  HA surface those modules touch (`HomeAssistantError`, `callback`,
  `TimerEventType`/`TimerInfo`) in `conftest.py`, and loads the real
  component files through a fake package module so the real
  `__init__.py` (which needs the full HA stack) never executes.
- `bridge_integration_check.py` — standalone in-process check against
  **real** Home Assistant (HA 2026.10.4 from the venv): boots HA,
  sets up the integration from a config entry, and exercises services,
  intents, sensors, the restart table, the ringing model, and the
  options flow. NOT collected by pytest — run it directly.
- `README.md` — this file.

The old root-level `test_bridge.py`, `test_intents.py`,
`test_options_flow.py`, `test_sensor_device_info.py` tested the
superseded build and were removed with this wave (they import the old
bridge API).

## Run

```sh
# Pure-module suite (from the repo root):
~/workspace/ha-repro/venv-2026.10.4/bin/python -m pytest tests/ -v

# In-process integration check (real HA, fresh /tmp config each run):
~/workspace/ha-repro/venv-2026.10.4/bin/python tests/bridge_integration_check.py
```

Both must be green before a wave is handed to verification. The rig
legs (G-02/G-04/G-08/G-09/G-10 + voice funnel) are a separate layer on
top — see the implementation log.
