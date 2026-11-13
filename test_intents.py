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
from enum import StrEnum
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

# Stub homeassistant.components.intent.timers (lazy import in
# bridge._send_to_satellite) so the real bridge can send in the F1/F4
# flows below — the same stub test_bridge.py uses.
class _StubTimerEventType(StrEnum):
    STARTED = "started"
    UPDATED = "updated"
    CANCELLED = "cancelled"
    FINISHED = "finished"


class _StubTimerInfo:
    def __init__(self, id, name, seconds, device_id, start_hours=None,
                 start_minutes=None, start_seconds=None, created_at=0,
                 updated_at=0, language="en"):
        self.id = id
        self.name = name
        self.seconds = seconds
        self.device_id = device_id
        self.language = language
        self.is_active = True
        self._created_seconds = seconds

    @property
    def created_seconds(self):
        return self._created_seconds

    def finish(self):
        self.seconds = 0
        self.is_active = False


_ha_timers = types.ModuleType("homeassistant.components.intent.timers")
_ha_timers.TimerEventType = _StubTimerEventType
_ha_timers.TimerInfo = _StubTimerInfo
_ha_intent_pkg = types.ModuleType("homeassistant.components.intent")
_ha_intent_pkg.timers = _ha_timers
_ha_comp_pkg = types.ModuleType("homeassistant.components")
_ha_comp_pkg.intent = _ha_intent_pkg
sys.modules["homeassistant.components"] = _ha_comp_pkg
sys.modules["homeassistant.components.intent"] = _ha_intent_pkg
sys.modules["homeassistant.components.intent.timers"] = _ha_timers

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
    def __init__(self, finished, pending=None):
        self._finished = finished
        self._pending = pending or []

    def finished_timers(self):
        return list(self._finished)

    def has_live_timer(self, device_id, name):
        return False

    def dismiss_timer(self, timer_id):
        # Mirrors the real bridge (round-2 B6): returns
        # (finished snapshot, immediate-send outcome).
        target = [s for s in self._finished if s.timer_id == timer_id]
        if not target:
            raise KeyError(timer_id)
        self._finished = [s for s in self._finished if s.timer_id != timer_id]
        return target[0], "sent"

    def pending_cancelled(self):
        return list(self._pending)

    def redismiss_timer(self, timer_id):
        if timer_id not in [s.timer_id for s in self._pending]:
            raise KeyError(timer_id)
        return "sent"


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
      resp.speech == "Stopping the Rice timer"
      and [s.timer_id for s in bridge.finished_timers()] == ["t1"])
check("dismiss speech never claims the stop happened (B1)",
      "dismissed" not in resp.speech)

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
      resp.speech == "Stopping the Rice timer"
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

# --- fix-burst F1/F4: real bridge + real intent handler ------------------
# The fake bridge above cannot prove durability: only the real bridge
# with a real store can show a voice dismiss surviving a reload. The
# fake hass here has NO async_create_task, so no background tap-path
# save exists — a store write after the handler returns can only have
# come from the handler's own awaited persist() (F1).

class _RealFakeTimerInfo:
    def __init__(self, timer_id, name, seconds, device_id):
        self.id = timer_id
        self.name = name
        self.seconds = seconds
        self.device_id = device_id
        self.language = "en"
        self.is_active = True
        self.conversation_command = None

    @property
    def created_seconds(self):
        return self.seconds


class _RealFakeTimerManager:
    def __init__(self):
        self.timers = {}
        self.handlers = {}

    def register_handler(self, device_id, handler):
        self.handlers[device_id] = handler
        return lambda: self.handlers.pop(device_id, None)


class _RealFakeStore:
    def __init__(self, data=None):
        self.data = data

    async def async_load(self):
        return self.data

    async def async_save(self, data):
        self.data = data


class _RealFakeHass:
    def __init__(self, manager):
        self.data = {"intent.timer": manager,
                     "satellite_timers": {"entry1": {"bridge": None}}}


async def _f1_flow():
    """Dismiss via the intent handler, then reload: pending survives."""
    calls = []

    def _rec_handler(event_type, timer):
        calls.append(f"{event_type}:{timer.id}")

    mgr = _RealFakeTimerManager()
    mgr.handlers["dev1"] = _rec_handler
    store = _RealFakeStore()
    hass = _RealFakeHass(mgr)
    real = bridge_mod.SatelliteTimerBridge(hass, ["dev1"])
    hass.data["satellite_timers"]["entry1"]["bridge"] = real
    await real.async_load_persisted(store)
    await real.start()
    timer = _RealFakeTimerInfo("F101", "Pasta", 600, "dev1")
    mgr.timers["F101"] = timer
    mgr.timers.pop("F101")
    mgr.handlers["dev1"]("finished", timer)
    assert "F101" in [s.timer_id for s in real.finished_timers()]
    handler = intents_mod.DismissFinishedTimerIntentHandler()
    resp = await handler.async_handle(
        FakeIntent(hass, {"name": {"value": "Pasta"}}))
    # The handler returned — so the persist it awaits has landed.
    stored_pending = (store.data or {}).get("pending_events", [])
    stored_timers = (store.data or {}).get("timers", [])
    # Simulate a reload: a fresh bridge on the same store file.
    mgr2 = _RealFakeTimerManager()
    mgr2.handlers["dev1"] = _rec_handler
    hass2 = _RealFakeHass(mgr2)
    real2 = bridge_mod.SatelliteTimerBridge(hass2, ["dev1"])
    hass2.data["satellite_timers"]["entry1"]["bridge"] = real2
    await real2.async_load_persisted(store)
    return {
        "speech": resp.speech,
        "calls": calls,
        "stored_pending": stored_pending,
        "stored_timers": stored_timers,
        "reloaded_pending": real2._pending,
        "reloaded_records": real2._records,
    }


f1 = asyncio.new_event_loop().run_until_complete(_f1_flow())
check("F1: voice dismiss replies 'Stopping the Pasta timer'",
      f1["speech"] == "Stopping the Pasta timer")
check("F1: dismiss sent the stop once",
      f1["calls"].count("cancelled:F101") == 1)
check("F1: the store already holds the pending CANCELLED when the handler returns",
      any(p["timer_id"] == "F101" and p["event_type"] == "cancelled"
          for p in f1["stored_pending"]))
check("F1: the store no longer holds the finished timer",
      all(t["timer_id"] != "F101" for t in f1["stored_timers"]))
check("F1: reload keeps the pending CANCELLED",
      "F101" in f1["reloaded_pending"]
      and f1["reloaded_pending"]["F101"].event_type == "cancelled")
check("F1: reload does NOT resurrect the finished timer",
      "F101" not in f1["reloaded_records"])


async def _f4_flow():
    """Voice re-dismiss + failed-send surfacing, via the real bridge."""
    calls = []

    def _rec_handler(event_type, timer):
        calls.append(f"{event_type}:{timer.id}")

    mgr = _RealFakeTimerManager()
    mgr.handlers["dev1"] = _rec_handler
    store = _RealFakeStore()
    hass = _RealFakeHass(mgr)
    real = bridge_mod.SatelliteTimerBridge(hass, ["dev1"])
    hass.data["satellite_timers"]["entry1"]["bridge"] = real
    await real.async_load_persisted(store)
    await real.start()
    handler = intents_mod.DismissFinishedTimerIntentHandler()
    pasta = _RealFakeTimerInfo("F401", "Pasta", 600, "dev1")
    mgr.timers["F401"] = pasta
    mgr.timers.pop("F401")
    mgr.handlers["dev1"]("finished", pasta)
    resp1 = await handler.async_handle(
        FakeIntent(hass, {"name": {"value": "Pasta"}}))
    # Dismiss again by name: the finished record is gone, so this is
    # a re-dismiss of the pending CANCELLED.
    resp2 = await handler.async_handle(
        FakeIntent(hass, {"name": {"value": "Pasta"}}))
    # A second timer, dismissed while the handler is down: the
    # immediate send observably fails.
    rice = _RealFakeTimerInfo("F402", "Rice", 300, "dev1")
    mgr.timers["F402"] = rice
    mgr.timers.pop("F402")
    mgr.handlers["dev1"]("finished", rice)
    mgr.handlers.pop("dev1", None)
    resp3 = await handler.async_handle(
        FakeIntent(hass, {"name": {"value": "Rice"}}))
    return {
        "speech1": resp1.speech,
        "speech2": resp2.speech,
        "speech3": resp3.speech,
        "calls": calls,
        "pending": real._pending,
    }


f4 = asyncio.new_event_loop().run_until_complete(_f4_flow())
check("F4: first voice dismiss stops the timer",
      f4["speech1"] == "Stopping the Pasta timer")
check("F4: second voice dismiss re-sends the stop for the pending id",
      f4["speech2"] == "Stopping the Pasta timer"
      and f4["calls"].count("cancelled:F401") == 2)
check("F4: re-dismiss keeps the record pending",
      "F401" in f4["pending"])
check("F4: observably-failed send is surfaced in the voice reply",
      f4["speech3"].startswith("Stopping the Rice timer")
      and "couldn't reach" in f4["speech3"]
      and "keep trying" in f4["speech3"])
check("F4: failed send marks delivery_failed",
      f4["pending"]["F402"].delivery_failed is True)

# --- fix-burst F4 (fake bridge): re-dismiss without a finished record ----
bridge = FakeBridge([], pending=[rice])
hass = FakeHass(bridge)
handler = intents_mod.DismissFinishedTimerIntentHandler()
resp = run(handler, FakeIntent(hass, {}))
check("F4: one pending CANCELLED with no name re-dismisses it",
      resp.speech == "Stopping the Rice timer")
bridge = FakeBridge([], pending=[pasta_early])
hass = FakeHass(bridge)
handler = intents_mod.DismissFinishedTimerIntentHandler()
resp = run(handler, FakeIntent(hass, {"name": {"value": "Pasta"}}))
check("F4: named pending CANCELLED re-dismisses it",
      resp.speech == "Stopping the Pasta timer")

# --- round-2 B4: pending re-dismiss is device-scoped ---------------------
# A pending CANCELLED on satellite A must never be re-dismissed by a
# command spoken to satellite B — pending snapshots are filtered by
# device like finished timers are.
pend_a = FakeSnapshot(
    "pA", "Pasta", "devA", started_at=now - timedelta(minutes=20)
)
bridge = FakeBridge([], pending=[pend_a])
hass = FakeHass(bridge)
handler = intents_mod.DismissFinishedTimerIntentHandler()
try:
    run(handler, FakeIntent(hass, {"name": {"value": "Pasta"}}, device_id="devB"))
    check("B4: pending on A is not re-dismissed from B", False)
except _IntentHandleError as err:
    check("B4: pending on A is not re-dismissed from B",
          "couldn't find a finished timer called Pasta" in str(err))
check("B4: the pending record on A is untouched",
      [s.timer_id for s in bridge.pending_cancelled()] == ["pA"])
bridge = FakeBridge([], pending=[pend_a])
hass = FakeHass(bridge)
handler = intents_mod.DismissFinishedTimerIntentHandler()
try:
    run(handler, FakeIntent(hass, {}, device_id="devB"))
    check("B4: unnamed dismiss from B does not re-dismiss A's pending", False)
except _IntentHandleError as err:
    check("B4: unnamed dismiss from B does not re-dismiss A's pending",
          "no finished timers" in str(err).lower())
# Positive control: the same pending re-dismisses fine from its own device.
bridge = FakeBridge([], pending=[pend_a])
hass = FakeHass(bridge)
handler = intents_mod.DismissFinishedTimerIntentHandler()
resp = run(handler, FakeIntent(hass, {"name": {"value": "Pasta"}}, device_id="devA"))
check("B4: pending on A re-dismisses from A",
      resp.speech == "Stopping the Pasta timer")

# --- round-2 B5: two pending same-name timers list distinguishably -----
# Pending snapshots now carry started_at (bridge B5), so the
# list-and-ask for dismissed timers can tell same-name timers apart.
pend_early = FakeSnapshot(
    "p1", "Pasta", "dev1", started_at=now - timedelta(minutes=20)
)
pend_late = FakeSnapshot(
    "p2", "Pasta", "dev1", started_at=now - timedelta(minutes=5)
)
bridge = FakeBridge([], pending=[pend_early, pend_late])
hass = FakeHass(bridge)
handler = intents_mod.DismissFinishedTimerIntentHandler()
try:
    run(handler, FakeIntent(hass, {"name": {"value": "Pasta"}}))
    check("B5: two pending same-name timers raise instead of picking one", False)
except _IntentHandleError as err:
    msg = str(err)
    check("B5: two pending same-name timers raise instead of picking one", True)
    b5_early_when = (now - timedelta(minutes=20)).astimezone().strftime("%I:%M %p").lstrip("0")
    b5_late_when = (now - timedelta(minutes=5)).astimezone().strftime("%I:%M %p").lstrip("0")
    check("B5: two pending same-name timers are listed with distinct start times",
          b5_early_when in msg and b5_late_when in msg
          and b5_early_when != b5_late_when)

# --- round-2 B6: interleaved dismisses report their own outcomes -------
# Two dismisses on two satellites, interleaved at the awaited persist:
# pre-fix the bridge-global last outcome let the second dismiss
# overwrite the first's reply. The store's save yields (sleep(0)),
# so the two handlers genuinely interleave at the persist.
class _YieldingStore(_RealFakeStore):
    async def async_save(self, data):
        await asyncio.sleep(0)
        self.data = data


async def _b6_flow():
    calls = []

    def _rec_handler(event_type, timer):
        calls.append(f"{event_type}:{timer.id}")

    mgr = _RealFakeTimerManager()
    mgr.handlers["dev1"] = _rec_handler
    mgr.handlers["dev2"] = _rec_handler
    store = _YieldingStore()
    hass = _RealFakeHass(mgr)
    real = bridge_mod.SatelliteTimerBridge(hass, ["dev1", "dev2"])
    hass.data["satellite_timers"]["entry1"]["bridge"] = real
    await real.async_load_persisted(store)
    await real.start()
    pasta = _RealFakeTimerInfo("B601", "Pasta", 600, "dev1")
    mgr.timers["B601"] = pasta
    mgr.timers.pop("B601")
    mgr.handlers["dev1"]("finished", pasta)
    rice = _RealFakeTimerInfo("B602", "Rice", 300, "dev2")
    mgr.timers["B602"] = rice
    mgr.timers.pop("B602")
    mgr.handlers["dev2"]("finished", rice)
    mgr.handlers.pop("dev2", None)  # dev2 down: its dismiss cannot reach it
    handler = intents_mod.DismissFinishedTimerIntentHandler()
    resp_pasta, resp_rice = await asyncio.gather(
        handler.async_handle(
            FakeIntent(hass, {"name": {"value": "Pasta"}}, device_id="dev1")),
        handler.async_handle(
            FakeIntent(hass, {"name": {"value": "Rice"}}, device_id="dev2")),
    )
    return {
        "pasta": resp_pasta.speech,
        "rice": resp_rice.speech,
        "calls": calls,
    }


b6 = asyncio.new_event_loop().run_until_complete(_b6_flow())
check("B6: interleaved dismiss on the live satellite reports a clean send",
      b6["pasta"] == "Stopping the Pasta timer")
check("B6: interleaved dismiss on the down satellite reports the failure",
      b6["rice"].startswith("Stopping the Rice timer")
      and "couldn't reach" in b6["rice"])
check("B6: the live satellite's stop was attempted once",
      b6["calls"].count("cancelled:B601") == 1)

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("All intent checks passed.")
