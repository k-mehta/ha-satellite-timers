"""Standalone test for the Dismiss intent's lists-and-asks behaviour
(TIMER-11, Phase 3).

When several finished timers match the name the handler must LIST them
(by name + start time) and ask which one — never silently pick one.

Stubs the Home Assistant modules intents.py imports, loads the real
bridge.py and intents.py, then drives the handler with a fake bridge
holding finished timers.

Run: python3 test_intents.py
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
COMPONENTS = HERE / "custom_components" / "satellite_timers"


def _module(name: str, **attrs: object) -> types.ModuleType:
    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    sys.modules[name] = mod
    return mod


class _IntentHandleError(Exception):
    pass


class _IntentResponse:
    def __init__(self):
        self.speech = None

    def async_set_speech(self, speech):
        self.speech = speech


class _IntentHandler:
    # HA's real async_validate_slots is a SYNC method despite the name.
    def async_validate_slots(self, slots):
        return slots


class _Optional:
    def __init__(self, key, default=None):
        self.key = key
        self.default = default

    def __hash__(self):
        return hash(("optional", self.key))

    def __eq__(self, other):
        return isinstance(other, _Optional) and other.key == self.key


class _Vol:
    Optional = _Optional


_module("homeassistant")
_module("homeassistant.core", HomeAssistant=object, callback=lambda f: f)
_module("homeassistant.helpers")
_module(
    "homeassistant.helpers.config_validation", string=str
)
_module(
    "homeassistant.helpers.intent",
    IntentHandler=_IntentHandler,
    IntentHandleError=_IntentHandleError,
    IntentResponse=_IntentResponse,
)
_module("voluptuous", Optional=_Optional, Schema=dict)

# Load the real package files without executing the package __init__.
pkg = types.ModuleType("satellite_timers")
pkg.__path__ = [str(COMPONENTS)]
sys.modules["satellite_timers"] = pkg

for _name in ("bridge", "const", "intents"):
    _spec = importlib.util.spec_from_file_location(
        f"satellite_timers.{_name}", COMPONENTS / f"{_name}.py"
    )
    assert _spec is not None and _spec.loader is not None
    _mod = importlib.util.module_from_spec(_spec)
    sys.modules[f"satellite_timers.{_name}"] = _mod
    _spec.loader.exec_module(_mod)

bridge_mod = sys.modules["satellite_timers.bridge"]
intents_mod = sys.modules["satellite_timers.intents"]

# ------------------------------------------------------------------ fakes


@dataclass
class FakeSnapshot:
    timer_id: str
    name: str
    device_id: str | None
    started_at: datetime | None = None


class FakeBridge:
    def __init__(self, finished):
        self._finished = finished

    def finished_timers(self):
        return list(self._finished)

    def has_live_timer(self, device_id, name):
        return False

    def dismiss_timer(self, timer_id):
        self._finished = [s for s in self._finished if s.timer_id != timer_id]


class FakeHass:
    def __init__(self, bridge):
        self.data = {"satellite_timers": {"entry1": {"bridge": bridge}}}


class FakeIntent:
    def __init__(self, hass, slots, device_id=None):
        self.hass = hass
        self.slots = slots
        self.device_id = device_id

    def create_response(self):
        return _IntentResponse()


failures = []


def check(label, condition):
    print(("PASS  " if condition else "FAIL  ") + label)
    if not condition:
        failures.append(label)


def run(handler, intent_obj):
    return asyncio.new_event_loop().run_until_complete(
        handler.async_handle(intent_obj)
    )


now = datetime.now(timezone.utc)
pasta_early = FakeSnapshot(
    "t1", "Pasta", "dev1", started_at=now - timedelta(minutes=20)
)
pasta_late = FakeSnapshot(
    "t2", "Pasta", "dev1", started_at=now - timedelta(minutes=5)
)
rice = FakeSnapshot("t3", "Rice", "dev1", started_at=now - timedelta(minutes=12))

# --- one match: dismisses it --------------------------------------------
bridge = FakeBridge([pasta_early, rice])
hass = FakeHass(bridge)
handler = intents_mod.DismissFinishedTimerIntentHandler()
resp = run(handler, FakeIntent(hass, {"name": {"value": "Rice"}}))
check("one match dismisses it",
      resp.speech == "Rice timer dismissed"
      and [s.timer_id for s in bridge.finished_timers()] == ["t1"])

# --- two matches: lists both with start times, dismisses neither --------
bridge = FakeBridge([pasta_early, pasta_late])
hass = FakeHass(bridge)
handler = intents_mod.DismissFinishedTimerIntentHandler()
try:
    run(handler, FakeIntent(hass, {"name": {"value": "Pasta"}}))
    check("two matches raise instead of picking one", False)
except _IntentHandleError as err:
    msg = str(err)
    check("two matches raise instead of picking one", True)
    check("two matches are listed by name",
          msg.count("Pasta") >= 2)
    early_when = (now - timedelta(minutes=20)).astimezone().strftime("%I:%M %p").lstrip("0")
    late_when = (now - timedelta(minutes=5)).astimezone().strftime("%I:%M %p").lstrip("0")
    check("two matches are listed with distinct start times",
          early_when in msg and late_when in msg
          and early_when != late_when)
    check("two matches ask which one", "which one" in msg.lower())
check("two matches dismiss neither",
      [s.timer_id for s in bridge.finished_timers()] == ["t1", "t2"])

# --- no name, several finished: lists them all ---------------------------
bridge = FakeBridge([pasta_early, rice])
hass = FakeHass(bridge)
handler = intents_mod.DismissFinishedTimerIntentHandler()
try:
    run(handler, FakeIntent(hass, {}))
    check("several finished with no name raise", False)
except _IntentHandleError as err:
    msg = str(err)
    check("several finished with no name raise", True)
    check("several finished are listed",
          "Pasta" in msg and "Rice" in msg)
    check("several finished ask which one", "which one" in msg.lower())
check("several finished dismiss none",
      len(bridge.finished_timers()) == 2)

# --- no name, one finished: dismisses it ---------------------------------
bridge = FakeBridge([rice])
hass = FakeHass(bridge)
handler = intents_mod.DismissFinishedTimerIntentHandler()
resp = run(handler, FakeIntent(hass, {}))
check("one finished with no name dismisses it",
      resp.speech == "Rice timer dismissed"
      and bridge.finished_timers() == [])

# --- no finished timers ---------------------------------------------------
bridge = FakeBridge([])
hass = FakeHass(bridge)
handler = intents_mod.DismissFinishedTimerIntentHandler()
try:
    run(handler, FakeIntent(hass, {}))
    check("no finished timers raises", False)
except _IntentHandleError as err:
    check("no finished timers raises", True)
    check("no finished timers says so",
          "no finished timers" in str(err).lower())

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("All intent checks passed.")
