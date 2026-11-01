"""Verify the sensor's device-info fix without a live Home Assistant.

Reproduces the exact failure from the HA log (Oct 31, 2026):

    satellite_timers: Not adding entity, error adding device: Invalid device
    info {'identifiers': set(), 'name': 'kitchen_hub', ...} for
    'satellite_timers' config entry: device info must include at least one
    of identifiers or connections

The kitchen hub's ESPHome device registers with EMPTY identifiers, and the
old sensor code passed that empty set through as its device link, which HA
rejects. The fix: only attach device info when identifiers or connections
are non-empty, otherwise the sensor stands alone.

This test stubs the Home Assistant modules sensor.py imports, loads the
real sensor.py and bridge.py, and runs HA's own validation rule against
whatever device info the sensor produces.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
COMPONENTS = HERE / "custom_components" / "satellite_timers"


# ---------------------------------------------------------------- stubs
def _module(name: str, **attrs: object) -> types.ModuleType:
    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    sys.modules[name] = mod
    return mod


class _SensorEntity:
    """Minimal stand-in for homeassistant.components.sensor.SensorEntity."""

    _attr_device_info = None

    def async_write_ha_state(self) -> None:
        return None

    async def async_remove(self) -> None:
        return None


def _callback(func):
    return func


class _HomeAssistant:
    pass


class _ConfigEntry:
    pass


_module("homeassistant")
_module("homeassistant.components")
_module(
    "homeassistant.components.sensor", SensorEntity=_SensorEntity
)
_module(
    "homeassistant.config_entries", ConfigEntry=_ConfigEntry
)
_module(
    "homeassistant.core", HomeAssistant=_HomeAssistant, callback=_callback
)
_module("homeassistant.helpers")
_module("homeassistant.helpers.device_registry")
_module("homeassistant.helpers.entity_registry")
_module("homeassistant.helpers.entity_platform", AddEntitiesCallback=object)
_module(
    "homeassistant.helpers.event",
    async_track_time_interval=lambda *a, **k: (lambda: None),
)

# Load the real package files without executing the package __init__.
pkg = types.ModuleType("satellite_timers")
pkg.__path__ = [str(COMPONENTS)]
sys.modules["satellite_timers"] = pkg

spec = importlib.util.spec_from_file_location(
    "satellite_timers.sensor", COMPONENTS / "sensor.py"
)
assert spec is not None and spec.loader is not None
sensor_mod = importlib.util.module_from_spec(spec)
sys.modules["satellite_timers.sensor"] = sensor_mod
spec.loader.exec_module(sensor_mod)

bridge_spec = importlib.util.spec_from_file_location(
    "satellite_timers.bridge", COMPONENTS / "bridge.py"
)
assert bridge_spec is not None and bridge_spec.loader is not None
bridge_mod = importlib.util.module_from_spec(bridge_spec)
sys.modules["satellite_timers.bridge"] = bridge_mod
bridge_spec.loader.exec_module(bridge_mod)


# ------------------------------------------------------- fakes + validation
@dataclass
class FakeDevice:
    identifiers: set
    connections: set
    manufacturer: str = "Open Home Foundation"
    model: str = "Linux Voice Assistant"
    name: str = "kitchen_hub"


class FakeDeviceRegistry:
    def __init__(self, device):
        self._device = device

    def async_get(self, device_id):
        return self._device


class FakeHass:
    def __init__(self, device):
        self._device = device


def ha_device_info_valid(device_info) -> bool:
    """Home Assistant's rule: device info must carry identifiers or connections."""
    if device_info is None:
        return True  # no device link at all is fine
    return bool(device_info.get("identifiers") or device_info.get("connections"))


def make_sensor(device):
    hass = FakeHass(device)
    # device_registry.async_get(hass) must return the fake registry.
    sys.modules[
        "homeassistant.helpers.device_registry"
    ].async_get = lambda _hass: FakeDeviceRegistry(device)
    snapshot = bridge_mod.TimerSnapshot(
        timer_id="01K9TESTTIMERID0000000001",
        name="Pasta",
        device_id="kitchen-hub-device-id",
        is_active=True,
        total_seconds=600,
        seconds_left=590,
        finishes_at=datetime.now(timezone.utc),
    )
    return sensor_mod.SatelliteTimerSensor(hass, snapshot)


def check(label: str, condition: bool) -> None:
    print(("PASS  " if condition else "FAIL  ") + label)
    if not condition:
        raise SystemExit(1)


# ------------------------------------------------------------- the cases
# 1. The exact failing case: ESPHome device with empty identifiers and no
#    connections. Old code built {'identifiers': set(), ...} -> HA rejected it.
kitchen_hub = FakeDevice(identifiers=set(), connections=set())
sensor = make_sensor(kitchen_hub)
info = getattr(sensor, "_attr_device_info", None)
check("empty identifiers: sensor sets no device info", info is None)
check("empty identifiers: passes HA validation", ha_device_info_valid(info))

# 1b. Prove the validator catches what the old code produced.
old_device_info = {
    "identifiers": set(),
    "manufacturer": "Open Home Foundation",
    "model": "Linux Voice Assistant",
    "name": "kitchen_hub",
}
check(
    "validator flags the old code's device info",
    not ha_device_info_valid(old_device_info),
)

# 2. A device WITH identifiers still gets linked normally.
normal = FakeDevice(
    identifiers={("esphome", "lva-2ccf67f0938a")}, connections=set()
)
sensor = make_sensor(normal)
info = getattr(sensor, "_attr_device_info", None)
check("with identifiers: device info is attached", info is not None)
check(
    "with identifiers: identifiers carried through",
    info is not None
    and info.get("identifiers") == {("esphome", "lva-2ccf67f0938a")},
)
check("with identifiers: passes HA validation", ha_device_info_valid(info))

# 3. Connections-only device links by connections, no empty identifiers key.
conn_only = FakeDevice(
    identifiers=set(), connections={("mac", "aa:bb:cc:dd:ee:ff")}
)
sensor = make_sensor(conn_only)
info = getattr(sensor, "_attr_device_info", None)
check("connections only: device info is attached", info is not None)
check(
    "connections only: no empty identifiers key",
    info is not None and "identifiers" not in info,
)
check("connections only: passes HA validation", ha_device_info_valid(info))

# 4. The sensor still reports the timer itself.
sensor = make_sensor(kitchen_hub)
check("sensor state is active", sensor._attr_native_value == "active")
check(
    "sensor carries duration/remaining",
    sensor._attr_extra_state_attributes.get("duration") == 600
    and sensor._attr_extra_state_attributes.get("remaining") == 590,
)

# 5. REGRESSION (Oct 31, 2026): _attr_suggested_object_id alone does NOT
#    set the entity id — HA 2026.2 derives the id from the entity NAME, so
#    the sensor used to land as sensor.pasta / sensor.pasta_2 and nothing
#    matching sensor.satellite_timer_* ever appeared. The fix sets
#    entity_id itself, which the platform picks up as the suggested id.
check(
    "entity_id carries the timer id",
    getattr(sensor, "entity_id", None)
    == "sensor.satellite_timer_01k9testtimerid0000000001",
)
check(
    "entity_id is a valid HA entity id",
    getattr(sensor, "entity_id", "").startswith("sensor.satellite_timer_"),
)

print("\nAll sensor device-info checks passed.")
