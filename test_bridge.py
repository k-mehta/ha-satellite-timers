"""Standalone smoke test for bridge.py (the risky part: the handler tap).

Stubs the one Home Assistant import (``homeassistant.core.callback``) and
loads the real bridge.py, then drives it with a fake TimerManager that
behaves like Home Assistant's ``intent/timers.py``.

Run: python3 test_bridge.py
"""

from __future__ import annotations

import importlib.util
import sys
import time
import types
from dataclasses import dataclass, field
from dataclasses import replace as dataclass_replace
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path

# ---------------------------------------------------------------- stubs


def _identity(func):
    return func


ha_core = types.ModuleType("homeassistant.core")
ha_core.callback = _identity
ha_core.HomeAssistant = object
ha_pkg = types.ModuleType("homeassistant")
ha_pkg.core = ha_core
sys.modules["homeassistant"] = ha_pkg
sys.modules["homeassistant.core"] = ha_core


class _FakeTimerEventType(StrEnum):
    STARTED = "started"
    UPDATED = "updated"
    CANCELLED = "cancelled"
    FINISHED = "finished"


@dataclass
class _FakeTimerInfo:
    """Stand-in for Home Assistant's TimerInfo (enough for the bridge)."""

    id: str
    name: str | None
    seconds: int
    device_id: str | None
    start_hours: int | None
    start_minutes: int | None
    start_seconds: int | None
    created_at: int
    updated_at: int
    language: str
    is_active: bool = True
    _created_seconds: int = field(default=0, repr=False)

    def __post_init__(self):
        self._created_seconds = self.seconds

    @property
    def seconds_left(self):
        if not self.is_active:
            return self.seconds
        now = time.monotonic_ns()
        seconds_running = int((now - self.updated_at) / 1e9)
        return max(0, self.seconds - seconds_running)

    @property
    def created_seconds(self):
        return self._created_seconds

    def finish(self):
        self.seconds = 0
        self.updated_at = time.monotonic_ns()
        self.is_active = False


ha_components = types.ModuleType("homeassistant.components")
ha_intent = types.ModuleType("homeassistant.components.intent")
ha_timers = types.ModuleType("homeassistant.components.intent.timers")
ha_timers.TimerEventType = _FakeTimerEventType
ha_timers.TimerInfo = _FakeTimerInfo
ha_components.intent = ha_intent
ha_intent.timers = ha_timers
ha_pkg.components = ha_components
sys.modules["homeassistant.components"] = ha_components
sys.modules["homeassistant.components.intent"] = ha_intent
sys.modules["homeassistant.components.intent.timers"] = ha_timers

BRIDGE_PATH = Path(__file__).parent / "custom_components" / "satellite_timers" / "bridge.py"
spec = importlib.util.spec_from_file_location("bridge_under_test", BRIDGE_PATH)
bridge = importlib.util.module_from_spec(spec)
sys.modules["bridge_under_test"] = bridge
spec.loader.exec_module(bridge)

# ------------------------------------------------------- fake TimerManager


class FakeTimerInfo:
    """TimerInfo with Home Assistant's own seconds_left/created_seconds logic."""

    def __init__(self, timer_id, name, seconds, device_id, is_active=True,
                 conversation_command=None):
        self.id = timer_id
        self.name = name
        self.seconds = seconds
        self.device_id = device_id
        self.is_active = is_active
        self.conversation_command = conversation_command
        self.start_hours = None
        self.start_minutes = None
        self.start_seconds = seconds
        self.created_at = time.monotonic_ns()
        self.updated_at = self.created_at
        self._created_seconds = seconds

    @property
    def seconds_left(self):
        if not self.is_active:
            return self.seconds
        now = time.monotonic_ns()
        seconds_running = int((now - self.updated_at) / 1e9)
        return max(0, self.seconds - seconds_running)

    @property
    def created_seconds(self):
        return self._created_seconds

    def pause(self):
        self.seconds = self.seconds_left
        self.updated_at = time.monotonic_ns()
        self.is_active = False


class FakeTimerManager:
    def __init__(self):
        self.timers = {}
        self.handlers = {}

    def register_handler(self, device_id, handler):
        self.handlers[device_id] = handler
        return lambda: self.handlers.pop(device_id, None)

    def start_timer(self, device_id, hours, minutes, seconds, language,
                    name=None, conversation_command=None):
        total = (hours or 0) * 3600 + (minutes or 0) * 60 + (seconds or 0)
        timer_id = f"NEW{len(self.timers) + 1:04d}"
        timer = FakeTimerInfo(timer_id, name or "", total, device_id)
        timer.language = language
        self.timers[timer_id] = timer
        # Like HA's TimerManager (intent/timers.py): the STARTED event
        # fires synchronously inside start_timer, before the id is
        # returned — so a listener reading a snapshot during the call
        # must already see the service-started mark. A fake that
        # never calls the handler cannot catch ordering bugs.
        handler = self.handlers.get(device_id)
        if handler is not None and not conversation_command:
            handler("started", timer)
        return timer_id


class FakeHass:
    def __init__(self, manager):
        self.data = {"intent.timer": manager}


# ------------------------------------------------------------------ checks

failures = []


def check(label, condition):
    print(("PASS  " if condition else "FAIL  ") + label)
    if not condition:
        failures.append(label)


# --- assist_supported -------------------------------------------------
mgr = FakeTimerManager()
hass = FakeHass(mgr)
check("supported: real manager shape", bridge.assist_supported(hass) is True)
check("supported: no manager", bridge.assist_supported(FakeHass(None)) is False)

broken = FakeTimerManager()
broken.handlers = ["not", "a", "dict"]
check("supported: handlers not a dict",
      bridge.assist_supported(FakeHass(broken)) is False)

# --- tap: original first, listener notified ---------------------------
calls: list[str] = []
cancelled_timers: list = []


def original_handler(event_type, timer):
    calls.append(f"original:{event_type}")
    if str(event_type) == "cancelled":
        cancelled_timers.append(timer)


mgr.handlers["dev1"] = original_handler
original_dict = mgr.handlers

changes: list[str] = []
b = bridge.SatelliteTimerBridge(hass, ["dev1"])
b.add_change_listener(lambda: changes.append("changed"))
check("start returns True", b.start() is True)
check("tap replaced handlers dict", mgr.handlers is not original_dict)

timer = FakeTimerInfo("01ABC", "Pasta", 600, "dev1")
mgr.timers["01ABC"] = timer
mgr.handlers["dev1"]("started", timer)
check("original handler called", "original:started" in calls)
check("listener notified", changes == ["changed"])
check("original runs before listener",
      calls.index("original:started") == 0)

# --- handler registered after the tap is also wrapped -----------------
calls2: list[str] = []


def second_handler(event_type, timer):
    calls2.append(f"second:{event_type}")


mgr.handlers["dev2"] = second_handler
b2 = bridge.SatelliteTimerBridge(hass, ["dev2"])
b2.add_change_listener(lambda: changes.append("changed2"))
check("second bridge start returns True", b2.start() is True)
timer2 = FakeTimerInfo("01XYZ", "Rice", 120, "dev2")
mgr.timers["01XYZ"] = timer2
mgr.handlers["dev2"]("started", timer2)
check("late-registered handler still called", "second:started" in calls2)
check("late-registered handler also notifies",
      changes.count("changed2") == 1)

# --- snapshot ----------------------------------------------------------
now = datetime.now(timezone.utc)
snap = b.snapshot(now)
check("snapshot sees the active timer", "01ABC" in snap)
s = snap["01ABC"]
check("snapshot name", s.name == "Pasta")
check("snapshot active", s.is_active is True)
check("snapshot total", s.total_seconds == 600)
check("snapshot seconds_left sane", 590 <= s.seconds_left <= 600)
check("snapshot finishes_at ~ now+left",
      s.finishes_at is not None
      and 590 <= (s.finishes_at - now).total_seconds() <= 600)

# paused timer
paused = FakeTimerInfo("01DEF", "Tea", 300, "dev1")
paused.pause()
mgr.timers["01DEF"] = paused
mgr.timers["01ABC"] = timer
snap = b.snapshot(datetime.now(timezone.utc))
p = snap["01DEF"]
check("paused snapshot not active", p.is_active is False)
check("paused snapshot has no finishes_at", p.finishes_at is None)
check("paused snapshot keeps remaining", p.seconds_left == 300)

# conversation-command timer and other-device timer are skipped
mgr.timers["01GHI"] = FakeTimerInfo("01GHI", "Cmd", 60, "dev1",
                                     conversation_command="turn on the light")
mgr.timers["01JKL"] = FakeTimerInfo("01JKL", "Other", 60, "dev9")
snap = b.snapshot(datetime.now(timezone.utc))
check("conversation timer skipped", "01GHI" not in snap)
check("other-device timer skipped", "01JKL" not in snap)

# --- finished timers ------------------------------------------------------
# Real TimerManager pops the timer before calling the handler, so mimic
# that: remove it from mgr.timers, then fire the finished event.
mgr.timers.pop("01ABC")
mgr.handlers["dev1"]("finished", timer)
check("original handler still called on finish", "original:finished" in calls)
snap = b.snapshot(datetime.now(timezone.utc))
check("finished timer stays in snapshot", "01ABC" in snap)
f = snap["01ABC"]
check("finished snapshot marked finished", f.is_finished is True)
check("finished snapshot has finished_at", f.finished_at is not None)
check("finished snapshot not active", f.is_active is False)
check("finished snapshot keeps total", f.total_seconds == 600)
check("finished snapshot remaining 0", f.seconds_left == 0)
check("finished_timers lists it",
      [s.timer_id for s in b.finished_timers()] == ["01ABC"])

# A second finished timer: finished_timers() returns them oldest first.
timer3 = FakeTimerInfo("01MNO", "Eggs", 120, "dev1")
mgr.timers["01MNO"] = timer3
mgr.timers.pop("01MNO")
mgr.handlers["dev1"]("finished", timer3)
check("second finished timer recorded",
      [s.timer_id for s in b.finished_timers()] == ["01ABC", "01MNO"])
earlier = f.finished_at - timedelta(minutes=10)
b._finished["01MNO"] = dataclass_replace(
    b._finished["01MNO"], finished_at=earlier)
check("finished_timers sorts oldest first",
      [s.timer_id for s in b.finished_timers()] == ["01MNO", "01ABC"])

# --- dismiss --------------------------------------------------------------
dismissed = b.dismiss_timer("01MNO")
check("dismiss returns the finished snapshot",
      dismissed.timer_id == "01MNO" and dismissed.is_finished)
snap = b.snapshot(datetime.now(timezone.utc))
check("dismissed timer leaves the snapshot", "01MNO" not in snap)
check("other finished timer stays", "01ABC" in snap)
# Dismissing a finished timer must tell the satellite to stop ringing:
# the handler hears CANCELLED for that timer id, with a finished
# TimerInfo (not active, nothing left, original duration kept).
check("dismiss tells the satellite handler (cancelled event)",
      "original:cancelled" in calls)
check("dismiss cancelled the right timer",
      len(cancelled_timers) == 1 and cancelled_timers[0].id == "01MNO")
check("dismissed timer info is a finished timer",
      cancelled_timers[0].is_active is False
      and cancelled_timers[0].seconds_left == 0
      and cancelled_timers[0].created_seconds == 120)
check("dismissed timer info keeps the name",
      cancelled_timers[0].name == "Eggs")

try:
    b.dismiss_timer("01DEF")  # Tea is still running (paused, but live)
    check("dismiss of a running timer raises ValueError", False)
except ValueError:
    check("dismiss of a running timer raises ValueError", True)

try:
    b.dismiss_timer("01NOPE")
    check("dismiss of an unknown timer raises KeyError", False)
except KeyError:
    check("dismiss of an unknown timer raises KeyError", True)

# --- restart --------------------------------------------------------------
new_id = b.restart_timer("01ABC")
check("restart returns a new id", new_id != "01ABC")
snap = b.snapshot(datetime.now(timezone.utc))
check("restart drops the finished record", "01ABC" not in snap)
check("restarted timer is live in the snapshot", new_id in snap)
n = snap[new_id]
check("restarted timer keeps the name", n.name == "Pasta")
check("restarted timer keeps the duration", n.total_seconds == 600)
check("restarted timer is active", n.is_active is True)

timer4 = FakeTimerInfo("01PQR", "Rice", 300, "dev1")
mgr.timers["01PQR"] = timer4
mgr.timers.pop("01PQR")
mgr.handlers["dev1"]("finished", timer4)
# The restart's new timer must be marked service-started during the
# synchronous STARTED notification too (same in-flight flag path as
# start_timer): the first snapshot captured after the new id appears
# is the one taken inside that notification.
during_r: list[dict] = []
b.add_change_listener(
    lambda: during_r.append(
        {tid: s.started_via_service
         for tid, s in b.snapshot(datetime.now(timezone.utc)).items()}
    )
)
over_id = b.restart_timer("01PQR", minutes=5)
first_with_new = next(c for c in during_r if over_id in c)
check("restart marks service-started during the notification",
      first_with_new.get(over_id) is True)
over = b.snapshot(datetime.now(timezone.utc))[over_id]
check("restart with override uses the override", over.total_seconds == 300)
check("restart with override keeps the name", over.name == "Rice")

try:
    b.restart_timer("01NOPE")
    check("restart of an unknown timer raises KeyError", False)
except KeyError:
    check("restart of an unknown timer raises KeyError", True)

# --- devices property ---------------------------------------------------
check("devices property lists configured satellites",
      b.devices == frozenset({"dev1"}))
b.set_devices(["dev1", "dev2"])
check("set_devices updates devices property",
      b.devices == frozenset({"dev1", "dev2"}))
b.set_devices(["dev1"])

# --- stop restores everything ------------------------------------------
# Both bridges must stop before the shared tap is removed.
n_changes = len(changes)
finished_before = calls.count("original:finished")
b2.stop()
b.stop()
check("stop restores original handlers dict", mgr.handlers is original_dict)
check("stop unwraps the handler",
      mgr.handlers["dev1"] is original_handler)
mgr.handlers["dev1"]("finished", timer)
check("after stop, listener not notified", len(changes) == n_changes)
check("after stop, original still works",
      calls.count("original:finished") == finished_before + 1)

# --- empty device set: no tap installed ---------------------------------
mgr2 = FakeTimerManager()
mgr2.handlers["dev1"] = original_handler
orig2 = mgr2.handlers
b3 = bridge.SatelliteTimerBridge(FakeHass(mgr2), [])
check("empty devices: start returns True", b3.start() is True)
check("empty devices: handlers untouched", mgr2.handlers is orig2)
check("empty devices: snapshot empty",
      b3.snapshot(datetime.now(timezone.utc)) == {})

# --- start_timer (BRIDGE-1) ----------------------------------------------
# The fake TimerManager must refuse a device with no handler, like the
# real one (is_timer_device): a satellite that cannot take timers is
# "unreachable", and the service must fail loudly.
class _TimersNotSupportedError(Exception):
    def __init__(self, device_id):
        super().__init__(f"Device {device_id} does not support timers")
        self.device_id = device_id


ha_timers.TimersNotSupportedError = _TimersNotSupportedError


_orig_start_timer = FakeTimerManager.start_timer


def _guarded_start_timer(self, device_id, hours, minutes, seconds,
                         language, name=None, conversation_command=None):
    if (not conversation_command) and (device_id not in self.handlers):
        raise _TimersNotSupportedError(device_id)
    return _orig_start_timer(
        self, device_id, hours, minutes, seconds, language,
        name=name, conversation_command=conversation_command)


FakeTimerManager.start_timer = _guarded_start_timer

mgr_s = FakeTimerManager()
hass_s = FakeHass(mgr_s)
mgr_s.handlers["dev1"] = original_handler
b_s = bridge.SatelliteTimerBridge(hass_s, ["dev1"])
check("start_timer bridge starts", b_s.start() is True)
new_id = b_s.start_timer("dev1", "Pasta", 600)
check("start_timer returns an id", isinstance(new_id, str) and new_id)
snap = b_s.snapshot(datetime.now(timezone.utc))
check("start_timer timer is live", new_id in snap)
check("start_timer keeps the name", snap[new_id].name == "Pasta")
check("start_timer keeps the duration", snap[new_id].total_seconds == 600)
check("start_timer is active", snap[new_id].is_active is True)
check("start_timer marks service-started",
      snap[new_id].started_via_service is True)
# The mark must ALREADY be visible DURING the synchronous STARTED
# notification, not only after start_timer returns: the timer-jump
# automation reads the first state_changed event, which fires inside
# that notification. Capture the snapshot from inside a listener and
# check the very first capture made during the call.
during: list[dict] = []
b_s.add_change_listener(
    lambda: during.append(
        {tid: s.started_via_service
         for tid, s in b_s.snapshot(datetime.now(timezone.utc)).items()}
    )
)
during_id = b_s.start_timer("dev1", "Soup", 300)
check("in-notification snapshot marks service-started",
      bool(during) and during[0].get(during_id) is True)
check("in-notification snapshot is the first capture, not a later one",
      len(during) >= 1 and during_id in during[0])
# A voice-cancelled service-started timer drops its mark (it must not
# stay in _service_started forever).
mgr_s.timers.pop(during_id)
mgr_s.handlers["dev1"]("cancelled",
                       FakeTimerInfo(during_id, "Soup", 300, "dev1"))
check("cancelled service-started timer drops its mark",
      during_id not in b_s._service_started)
check("start_timer records started_at",
      snap[new_id].started_at is not None
      and abs((snap[new_id].finishes_at - snap[new_id].started_at)
              .total_seconds() - 600) < 2)
# Duplicates are accepted: same name, different id (TIMER-11).
dup_id = b_s.start_timer("dev1", "Pasta", 300)
check("start_timer accepts duplicate names", dup_id != new_id)
snap = b_s.snapshot(datetime.now(timezone.utc))
pastas = [s for s in snap.values() if s.name == "Pasta"]
check("duplicate names both live", len(pastas) == 2)
check("duplicate names distinct start times",
      pastas[0].started_at != pastas[1].started_at)
try:
    b_s.start_timer("dev1", "Pasta", 0)
    check("start_timer rejects zero duration", False)
except ValueError:
    check("start_timer rejects zero duration", True)
try:
    b_s.start_timer("dev9", "Pasta", 60)
    check("start_timer rejects unconfigured device", False)
except ValueError:
    check("start_timer rejects unconfigured device", True)
b_s2 = bridge.SatelliteTimerBridge(hass_s, ["dev1", "dev9"])
check("start_timer bridge with unreachable device starts",
      b_s2.start() is True)
try:
    b_s2.start_timer("dev9", "Pasta", 60)
    check("start_timer raises for a device with no handler", False)
except _TimersNotSupportedError:
    check("start_timer raises for a device with no handler", True)
b_s.stop()
b_s2.stop()

# --- persistence (BRIDGE-2) ----------------------------------------------
import asyncio as _asyncio

_LOOP = _asyncio.new_event_loop()


class FakeStore:
    """HA Store stand-in: async_load / async_save over a plain dict."""

    def __init__(self, data=None):
        self.data = data

    async def async_load(self):
        return self.data

    async def async_save(self, data):
        self.data = data


class FakeHassWithTasks(FakeHass):
    def async_create_task(self, coro):
        return _LOOP.run_until_complete(coro)


mgr_p = FakeTimerManager()
hass_p = FakeHassWithTasks(mgr_p)
mgr_p.handlers["dev1"] = original_handler
store_p = FakeStore()
b_p = bridge.SatelliteTimerBridge(hass_p, ["dev1"])
check("persistence: load with no data restores 0",
      _LOOP.run_until_complete(b_p.async_load_persisted(store_p)) == 0)
check("persistence: bridge starts", b_p.start() is True)
pasta_id = b_p.start_timer("dev1", "Pasta", 3600)
rice_id = b_p.start_timer("dev1", "Rice", 600)
rice_timer = mgr_p.timers[rice_id]
rice_timer.pause()  # a paused timer, as in the item-68 fixture
eggs = FakeTimerInfo("01EGG", "Eggs", 120, "dev1")
mgr_p.timers["01EGG"] = eggs
mgr_p.timers.pop("01EGG")
mgr_p.handlers["dev1"]("finished", eggs)
check("persistence: store was written",
      isinstance(store_p.data, dict)
      and len(store_p.data.get("timers", [])) == 3)
by_id = {t["timer_id"]: t for t in store_p.data["timers"]}
check("persistence: active timer stored active",
      by_id[pasta_id]["state"] == "active")
check("persistence: paused timer stored paused",
      by_id[rice_id]["state"] == "paused"
      and by_id[rice_id]["seconds_left"] == 600)
check("persistence: finished timer stored finished",
      by_id["01EGG"]["state"] == "finished")
check("persistence: service mark stored",
      by_id[pasta_id]["started_via_service"] is True)
check("persistence: started_at stored",
      by_id[pasta_id]["started_at"] is not None)

# A fresh bridge with an EMPTY TimerManager restores all three.
mgr_p2 = FakeTimerManager()
hass_p2 = FakeHassWithTasks(mgr_p2)
mgr_p2.handlers["dev1"] = original_handler
b_p2 = bridge.SatelliteTimerBridge(hass_p2, ["dev1"])
check("persistence: restore returns 3",
      _LOOP.run_until_complete(b_p2.async_load_persisted(store_p)) == 3)
check("persistence: restored bridge starts", b_p2.start() is True)
snap2 = b_p2.snapshot(datetime.now(timezone.utc))
check("persistence: all 3 timers back", len(snap2) == 3)
check("persistence: restored Pasta active",
      snap2[pasta_id].is_active is True and not snap2[pasta_id].is_finished)
check("persistence: restored Rice paused",
      snap2[rice_id].is_active is False and not snap2[rice_id].is_finished)
check("persistence: restored Rice keeps remaining",
      snap2[rice_id].seconds_left == 600)
check("persistence: restored Eggs finished",
      snap2["01EGG"].is_finished is True)
check("persistence: restored Pasta keeps started_at",
      snap2[pasta_id].started_at is not None)
check("persistence: restored Pasta keeps service mark",
      snap2[pasta_id].started_via_service is True)

# Restored-timer controls work without the TimerManager (BRIDGE-2).
paused = b_p2.pause_restored(pasta_id)
check("restored pause freezes the timer", paused.is_active is False)
resumed = b_p2.resume_restored(pasta_id)
check("restored resume restarts the countdown",
      resumed.is_active is True and resumed.finishes_at is not None)
before = b_p2.snapshot(datetime.now(timezone.utc))[pasta_id].finishes_at
b_p2.add_time_restored(pasta_id, 60)
after = b_p2.snapshot(datetime.now(timezone.utc))[pasta_id].finishes_at
check("restored add_time extends the finish",
      (after - before).total_seconds() == 60)
b_p2.cancel_restored(rice_id)
snap2 = b_p2.snapshot(datetime.now(timezone.utc))
check("restored cancel drops the timer", rice_id not in snap2)
check("restored cancel keeps the others", pasta_id in snap2)
try:
    b_p2.pause_restored(rice_id)
    check("restored op on a dropped timer raises KeyError", False)
except KeyError:
    check("restored op on a dropped timer raises KeyError", True)

# A stored active timer whose finishes_at has passed restores as
# finished (it went off while Home Assistant was down — TIMER-12).
past = datetime.now(timezone.utc) - timedelta(minutes=5)
store_past = FakeStore({
    "version": 1,
    "timers": [{
        "timer_id": "01OLD", "name": "Soup", "device_id": "dev1",
        "state": "active", "total_seconds": 600, "seconds_left": 0,
        "finishes_at": past.isoformat(), "finished_at": None,
        "started_at": (past - timedelta(seconds=600)).isoformat(),
        "language": "en", "started_via_service": False,
    }],
})
mgr_p3 = FakeTimerManager()
hass_p3 = FakeHassWithTasks(mgr_p3)
mgr_p3.handlers["dev1"] = original_handler
b_p3 = bridge.SatelliteTimerBridge(hass_p3, ["dev1"])
check("expired restore returns 1",
      _LOOP.run_until_complete(b_p3.async_load_persisted(store_past)) == 1)
b_p3.start()
snap3 = b_p3.snapshot(datetime.now(timezone.utc))
check("expired active restores as finished",
      snap3["01OLD"].is_finished is True)
check("expired restore keeps the true finish time",
      snap3["01OLD"].finished_at == past)

# --- P8-1: device churn must not erase persisted timers ---------------
# The load path is the P8-1 scenario: storage holds timers for dev1,
# the bridge now mirrors dev2 (re-pair / HA restore changed the device
# id). The records must be kept in storage across saves, with no
# sensor surfacing for the unknown device.
now_p8 = datetime.now(timezone.utc)
store_churn = FakeStore({
    "version": 1,
    "timers": [
        {"timer_id": "01CHURN-A", "name": "ChurnActive", "device_id": "dev1",
         "state": "active", "total_seconds": 600, "seconds_left": 500,
         "finishes_at": (now_p8 + timedelta(seconds=500)).isoformat(),
         "finished_at": None,
         "started_at": (now_p8 - timedelta(seconds=100)).isoformat(),
         "language": "en", "started_via_service": False},
        {"timer_id": "01CHURN-F", "name": "ChurnDone", "device_id": "dev1",
         "state": "finished", "total_seconds": 300, "seconds_left": 0,
         "finishes_at": None,
         "finished_at": (now_p8 - timedelta(minutes=2)).isoformat(),
         "started_at": (now_p8 - timedelta(seconds=300)).isoformat(),
         "language": "en", "started_via_service": True},
    ],
})
mgr_c = FakeTimerManager()
hass_c = FakeHassWithTasks(mgr_c)
mgr_c.handlers["dev1"] = original_handler
b_c = bridge.SatelliteTimerBridge(hass_c, ["dev2"])
check("churn: load keeps unknown-device records",
      _LOOP.run_until_complete(b_c.async_load_persisted(store_churn)) == 2)
check("churn: bridge starts", b_c.start() is True)
check("churn: no sensor surfaces for the unknown device",
      b_c.snapshot(datetime.now(timezone.utc)) == {})
b_c._notify_listeners()  # first save after the churn
kept = {t["timer_id"] for t in store_churn.data["timers"]}
check("churn: first save after churn preserves both records",
      kept == {"01CHURN-A", "01CHURN-F"})
# A second bridge loading the same store keeps them too, and a save
# from it still preserves them.
mgr_c2 = FakeTimerManager()
hass_c2 = FakeHassWithTasks(mgr_c2)
b_c2 = bridge.SatelliteTimerBridge(hass_c2, ["dev2"])
check("churn: reload keeps unknown-device records",
      _LOOP.run_until_complete(b_c2.async_load_persisted(store_churn)) == 2)
b_c2.start()
b_c2._notify_listeners()
kept2 = {t["timer_id"] for t in store_churn.data["timers"]}
check("churn: second save still preserves both records",
      kept2 == {"01CHURN-A", "01CHURN-F"})
# Retention bound: a finished record over 7 days old is pruned on
# load; one 6 days old is kept.
old = datetime.now(timezone.utc) - timedelta(days=8)
recent = datetime.now(timezone.utc) - timedelta(days=6)
store_old = FakeStore({
    "version": 1,
    "timers": [
        {"timer_id": "01OLD8", "name": "Old", "device_id": "dev1",
         "state": "finished", "total_seconds": 60, "seconds_left": 0,
         "finishes_at": None, "finished_at": old.isoformat(),
         "started_at": (old - timedelta(seconds=60)).isoformat(),
         "language": "en", "started_via_service": False},
        {"timer_id": "01OLD6", "name": "Recent", "device_id": "dev1",
         "state": "finished", "total_seconds": 60, "seconds_left": 0,
         "finishes_at": None, "finished_at": recent.isoformat(),
         "started_at": (recent - timedelta(seconds=60)).isoformat(),
         "language": "en", "started_via_service": False},
    ],
})
mgr_c3 = FakeTimerManager()
hass_c3 = FakeHassWithTasks(mgr_c3)
b_c3 = bridge.SatelliteTimerBridge(hass_c3, ["dev2"])
check("churn: load prunes the 8-day-old record, keeps the 6-day one",
      _LOOP.run_until_complete(b_c3.async_load_persisted(store_old)) == 1
      and "01OLD6" in b_c3._finished and "01OLD8" not in b_c3._finished)
b_c3.start()
b_c3._notify_listeners()
check("churn: save after prune keeps only the 6-day record",
      {t["timer_id"] for t in store_old.data["timers"]} == {"01OLD6"})
b_c.stop()
b_c2.stop()
b_c3.stop()

# A live timer wins over its stored record on load.
mgr_p4 = FakeTimerManager()
hass_p4 = FakeHassWithTasks(mgr_p4)
mgr_p4.handlers["dev1"] = original_handler
live = FakeTimerInfo("01LIVE", "Live", 600, "dev1")
mgr_p4.timers["01LIVE"] = live
store_live = FakeStore({
    "version": 1,
    "timers": [{
        "timer_id": "01LIVE", "name": "Stale", "device_id": "dev1",
        "state": "paused", "total_seconds": 600, "seconds_left": 100,
        "finishes_at": None, "finished_at": None, "started_at": None,
        "language": "en", "started_via_service": False,
    }],
})
b_p4 = bridge.SatelliteTimerBridge(hass_p4, ["dev1"])
check("live timer wins over its stored record",
      _LOOP.run_until_complete(b_p4.async_load_persisted(store_live)) == 0)
b_p.stop()
b_p2.stop()
b_p3.stop()
b_p4.stop()

# --- missed-timer replay (TIMER-12 / BRIDGE-3) ---------------------------
mgr_r = FakeTimerManager()
hass_r = FakeHass(mgr_r)
replayed: list = []


def handler_v1(event_type, timer):
    replayed.append(("v1", str(event_type), timer.id))


def handler_v2(event_type, timer):
    replayed.append(("v2", str(event_type), timer.id))


mgr_r.handlers["dev1"] = handler_v1
b_r = bridge.SatelliteTimerBridge(hass_r, ["dev1"])
check("replay: bridge starts", b_r.start() is True)
t_r = FakeTimerInfo("01REP", "Pasta", 300, "dev1")
mgr_r.timers["01REP"] = t_r
mgr_r.timers.pop("01REP")
mgr_r.handlers["dev1"]("finished", t_r)
check("replay: satellite hears FINISHED when the timer goes off",
      ("v1", "finished", "01REP") in replayed)
# The satellite reconnects (re-registers its handler) with the finished
# timer still undismissed: it must hear FINISHED again.
mgr_r.handlers["dev1"] = handler_v2
check("replay: reconnect re-asserts the finished timer",
      ("v2", "finished", "01REP") in replayed)
check("replay: exactly one re-assertion per reconnect",
      [e for e in replayed if e[0] == "v2"] == [("v2", "finished", "01REP")])
# Once dismissed, a reconnect hears nothing.
b_r.dismiss_timer("01REP")
mgr_r.handlers["dev1"] = handler_v1
check("replay: nothing re-asserted after dismiss",
      [e for e in replayed if e[0] == "v1"
       and e[1] == "finished"] == [("v1", "finished", "01REP")])
b_r.stop()

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("All bridge checks passed.")
