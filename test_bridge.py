"""Standalone smoke test for bridge.py (the risky part: the handler tap).

Stubs the one Home Assistant import (``homeassistant.core.callback``) and
loads the real bridge.py, then drives it with a fake TimerManager that
behaves like Home Assistant's ``intent/timers.py``.

Run: python3 test_bridge.py
"""

from __future__ import annotations

import importlib.util
import logging
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

    def pause_timer(self, timer_id):
        timer = self.timers[timer_id]
        timer.pause()
        handler = self.handlers.get(timer.device_id)
        if handler is not None:
            handler("updated", timer)

    def cancel_timer(self, timer_id):
        timer = self.timers.pop(timer_id)
        handler = self.handlers.get(timer.device_id)
        if handler is not None:
            handler("cancelled", timer)


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
import asyncio as _asyncio

_LOOP = _asyncio.new_event_loop()

b = bridge.SatelliteTimerBridge(hass, ["dev1"])
b.add_change_listener(lambda: changes.append("changed"))
check("start returns True", _LOOP.run_until_complete(b.start()) is True)
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
check("second bridge start returns True", _LOOP.run_until_complete(b2.start()) is True)
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
b._records["01MNO"] = dataclass_replace(
    b._records["01MNO"], finished_at=earlier)
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
check("empty devices: start returns True", _LOOP.run_until_complete(b3.start()) is True)
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
check("start_timer bridge starts", _LOOP.run_until_complete(b_s.start()) is True)
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
time.sleep(0.01)  # distinct observed starts
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
      _LOOP.run_until_complete(b_s2.start()) is True)
try:
    b_s2.start_timer("dev9", "Pasta", 60)
    check("start_timer raises for a device with no handler", False)
except _TimersNotSupportedError:
    check("start_timer raises for a device with no handler", True)
b_s.stop()
b_s2.stop()

# --- no ringing, no re-assert (§5.1) --------------------------------------
# There is no "ringing" state: a finished timer is finished from the
# moment it goes off. The bridge never re-announces a stored finished
# timer — not on a (re)registration, not on a snapshot, not on a
# restart (Q11/Q12 settled). The satellite hears FINISHED exactly once:
# when the timer goes off.
mgr_r2 = FakeTimerManager()
hass_r2 = FakeHass(mgr_r2)
mgr_r2.handlers["dev1"] = original_handler
b_r2 = bridge.SatelliteTimerBridge(hass_r2, ["dev1"])
check("no-ringing bridge starts", _LOOP.run_until_complete(b_r2.start()) is True)
ring_id = b_r2.start_timer("dev1", "Pasta", 600)
ring_timer = mgr_r2.timers[ring_id]
mgr_r2.timers.pop(ring_id)
mgr_r2.handlers["dev1"]("finished", ring_timer)
snap = b_r2.snapshot(datetime.now(timezone.utc))
check("fresh finished timer is finished",
      snap[ring_id].is_finished is True)
check("finished timer has no ringing flag",
      not hasattr(snap[ring_id], "is_ringing"))
# 59 s / 61 s after finish: still finished, unchanged — no window.
b_r2._records[ring_id] = dataclass_replace(
    b_r2._records[ring_id],
    finished_at=datetime.now(timezone.utc) - timedelta(seconds=59),
)
snap = b_r2.snapshot(datetime.now(timezone.utc))
check("59 s after finish still finished",
      snap[ring_id].is_finished is True)
b_r2._records[ring_id] = dataclass_replace(
    b_r2._records[ring_id],
    finished_at=datetime.now(timezone.utc) - timedelta(seconds=61),
)
snap = b_r2.snapshot(datetime.now(timezone.utc))
check("61 s after finish still finished",
      snap[ring_id].is_finished is True)
# A (re)registration sends NOTHING for the finished timer.
finished_before = calls.count("original:finished")
b_r2._on_handler_registered("dev1")
snap = b_r2.snapshot(datetime.now(timezone.utc))
check("handler (re)registration sends nothing for a finished timer",
      calls.count("original:finished") == finished_before)
# Dismiss clears the record.
b_r2.dismiss_timer(ring_id)
snap = b_r2.snapshot(datetime.now(timezone.utc))
check("dismissed finished timer leaves the snapshot", ring_id not in snap)
# The persisted record carries no ringing_until key at all.
ring2_id = b_r2.start_timer("dev1", "Rice", 300)
ring2_timer = mgr_r2.timers[ring2_id]
mgr_r2.timers.pop(ring2_id)
mgr_r2.handlers["dev1"]("finished", ring2_timer)
stored = b_r2._persisted_data(datetime.now(timezone.utc))
rec = next(t for t in stored["timers"] if t["timer_id"] == ring2_id)
check("persisted record has no ringing_until",
      "ringing_until" not in rec)
rebuilt = b_r2._snapshot_from_record(rec, datetime.now(timezone.utc))
check("rebuilt record has no ringing flag",
      not hasattr(rebuilt, "ringing_until"))
# A record from before this change (with a ringing_until key) loads
# fine — the key is ignored, not an error.
old_rec = dict(rec)
old_rec["ringing_until"] = (
    datetime.now(timezone.utc) + timedelta(seconds=60)
).isoformat()
rebuilt_old = b_r2._snapshot_from_record(old_rec, datetime.now(timezone.utc))
check("old record with ringing_until loads as finished",
      rebuilt_old is not None and rebuilt_old.is_finished is True)
# Pure snapshot: a restored active timer whose finishes_at has passed
# is emitted as active with 0 s left, and the held record is NOT
# converted by the snapshot — the conversion is reconcile_finished's
# (RB-R2-3), not the snapshot's.
runout_id = "01RUNOUT"
b_r2._records[runout_id] = bridge.TimerRecord(
    runout_id, "Runout", "dev1", True, 300, 42,
    datetime.now(timezone.utc) - timedelta(seconds=10),
)
snap = b_r2.snapshot(datetime.now(timezone.utc))
check("snapshot emits a passed restored timer as active with 0 s left",
      snap[runout_id].is_active is True
      and snap[runout_id].seconds_left == 0
      and snap[runout_id].is_finished is False)
check("snapshot does not convert the held record",
      b_r2._records[runout_id].is_active is True
      and b_r2._records[runout_id].is_finished is False)
b_r2.stop()

# --- restore (D6 Undo) -----------------------------------------------------
# Undo on a cancelled timer puts it back with its REMAINING time, in
# the state it was in — atomically: no await separates the start and
# the pause, so HA's scheduled syncs (which run after the call
# returns) only ever see the restored state.
mgr_u = FakeTimerManager()
hass_u = FakeHass(mgr_u)
mgr_u.handlers["dev1"] = original_handler
b_u = bridge.SatelliteTimerBridge(hass_u, ["dev1"])
check("restore bridge starts", _LOOP.run_until_complete(b_u.start()) is True)
restored_id = b_u.restore_timer("dev1", "Pasta", 420, paused=True)
snap = b_u.snapshot(datetime.now(timezone.utc))
check("restore returns a live timer", restored_id in snap)
r = snap[restored_id]
check("restored timer keeps the name", r.name == "Pasta")
check("restored timer keeps the remaining time",
      r.total_seconds == 420 and r.seconds_left == 420)
check("restored paused timer comes back paused", r.is_active is False)
check("restored timer is service-started",
      r.started_via_service is True)
running_id = b_u.restore_timer("dev1", "Soup", 300, paused=False)
snap = b_u.snapshot(datetime.now(timezone.utc))
check("restored running timer comes back running",
      snap[running_id].is_active is True)
check("restored running timer keeps the remaining time",
      snap[running_id].seconds_left == 300)
try:
    b_u.restore_timer("dev1", "Pasta", 0, paused=False)
    check("restore rejects zero remaining time", False)
except ValueError:
    check("restore rejects zero remaining time", True)
try:
    b_u.restore_timer("dev9", "Pasta", 60, paused=False)
    check("restore rejects unconfigured device", False)
except ValueError:
    check("restore rejects unconfigured device", True)
# A pause that fails must not leave a half-restored running timer.
class _FailPauseManager(FakeTimerManager):
    def pause_timer(self, timer_id):
        raise RuntimeError("satellite unreachable")

mgr_f = _FailPauseManager()
hass_f = FakeHass(mgr_f)
mgr_f.handlers["dev1"] = original_handler
b_f = bridge.SatelliteTimerBridge(hass_f, ["dev1"])
check("failing-pause bridge starts", _LOOP.run_until_complete(b_f.start()) is True)
try:
    b_f.restore_timer("dev1", "Pasta", 420, paused=True)
    check("restore with failing pause raises ValueError", False)
except ValueError:
    check("restore with failing pause raises ValueError", True)
check("failed restore leaves no timer behind",
      b_f.snapshot(datetime.now(timezone.utc)) == {})
b_f.stop()
# Undo on a dismissed finished timer puts the finished record back
# WITHOUT re-ringing the satellite, preserving finished_at.
fin_id = b_u.start_timer("dev1", "Eggs", 120)
fin_timer = mgr_u.timers[fin_id]
mgr_u.timers.pop(fin_id)
mgr_u.handlers["dev1"]("finished", fin_timer)
orig_finished_at = b_u._records[fin_id].finished_at
b_u.dismiss_timer(fin_id)
check("dismissed finished timer is gone",
      fin_id not in b_u.snapshot(datetime.now(timezone.utc)))
finished_before = calls.count("original:finished")
new_fin_id = b_u.restore_finished(
    "dev1", "Eggs", 120, orig_finished_at, started_via_service=True
)
check("restore_finished returns a new id", new_fin_id != fin_id)
snap = b_u.snapshot(datetime.now(timezone.utc))
check("restored finished timer is back", new_fin_id in snap)
rf = snap[new_fin_id]
check("restored finished timer keeps the name", rf.name == "Eggs")
check("restored finished timer keeps finished_at",
      rf.finished_at == orig_finished_at)
check("restored finished timer is finished",
      rf.is_finished is True)
check("restore_finished omits started_at when none was passed",
      rf.started_at is None)
# A passed started_at round-trips (the caller observed it).
passed_start = orig_finished_at - timedelta(seconds=120)
new_fin_id2 = b_u.restore_finished(
    "dev1", "Eggs", 120, orig_finished_at, started_via_service=True,
    started_at=passed_start,
)
check("restore_finished keeps a passed started_at",
      b_u.snapshot(datetime.now(timezone.utc))[new_fin_id2].started_at
      == passed_start)
check("restored finished timer keeps service-started",
      rf.started_via_service is True)
check("restore_finished does not re-ring the satellite",
      calls.count("original:finished") == finished_before)
try:
    b_u.restore_finished("dev1", "Eggs", 0, orig_finished_at, False)
    check("restore_finished rejects zero duration", False)
except ValueError:
    check("restore_finished rejects zero duration", True)
try:
    b_u.restore_finished("dev1", "Eggs", 120, None, False)
    check("restore_finished rejects missing finished_at", False)
except (ValueError, TypeError):
    check("restore_finished rejects missing finished_at", True)
try:
    b_u.restore_finished("dev9", "Eggs", 120, orig_finished_at, False)
    check("restore_finished rejects unconfigured device", False)
except ValueError:
    check("restore_finished rejects unconfigured device", True)
b_u.stop()


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
check("persistence: bridge starts", _LOOP.run_until_complete(b_p.start()) is True)
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
check("persistence: restored bridge starts", _LOOP.run_until_complete(b_p2.start()) is True)
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
_LOOP.run_until_complete(b_p3.start())
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
check("churn: bridge starts", _LOOP.run_until_complete(b_c.start()) is True)
check("churn: no sensor surfaces for the unknown device",
      b_c.snapshot(datetime.now(timezone.utc)) == {})
_LOOP.run_until_complete(b_c.persist())  # first save after the churn
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
_LOOP.run_until_complete(b_c2.start())
_LOOP.run_until_complete(b_c2.persist())
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
      and "01OLD6" in b_c3._records and "01OLD8" not in b_c3._records)
_LOOP.run_until_complete(b_c3.start())
_LOOP.run_until_complete(b_c3.persist())
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

# --- no missed-timer replay (§5.1 / Q11 / Q12) ---------------------------
# The old BRIDGE-3 behaviour (reconnect re-asserts the finished timer)
# is deleted: a stored finished timer is never re-announced, and a
# load-time run-out never rings. The satellite hears FINISHED exactly
# once — when the timer goes off — and a reconnect hears nothing.
mgr_r = FakeTimerManager()
hass_r = FakeHass(mgr_r)
replayed: list = []


def handler_v1(event_type, timer):
    replayed.append(("v1", str(event_type), timer.id))


def handler_v2(event_type, timer):
    replayed.append(("v2", str(event_type), timer.id))


mgr_r.handlers["dev1"] = handler_v1
b_r = bridge.SatelliteTimerBridge(hass_r, ["dev1"])
check("no-replay: bridge starts", _LOOP.run_until_complete(b_r.start()) is True)
t_r = FakeTimerInfo("01REP", "Pasta", 300, "dev1")
mgr_r.timers["01REP"] = t_r
mgr_r.timers.pop("01REP")
mgr_r.handlers["dev1"]("finished", t_r)
check("no-replay: satellite hears FINISHED when the timer goes off",
      ("v1", "finished", "01REP") in replayed)
# The satellite reconnects (re-registers its handler) with the finished
# timer still undismissed: it must hear NOTHING — no re-assert.
mgr_r.handlers["dev1"] = handler_v2
check("no-replay: reconnect sends nothing for the finished timer",
      [e for e in replayed if e[0] == "v2"] == [])
# Once dismissed, a reconnect still hears nothing.
b_r.dismiss_timer("01REP")
mgr_r.handlers["dev1"] = handler_v1
check("no-replay: nothing sent after dismiss either",
      [e for e in replayed if e[0] == "v1"
       and e[1] == "finished"] == [("v1", "finished", "01REP")])
b_r.stop()

# --- store foundation (RB-R2-1 / RB-R2-9 / RB-R2-10) ----------------------
# Pure snapshot + explicit persist (BUG-T1): a save issued during a
# snapshot raises, never silently drops.
mgr_n = FakeTimerManager()
hass_n = FakeHassWithTasks(mgr_n)
mgr_n.handlers["dev1"] = original_handler
store_n = FakeStore({"version": 1, "timers": [], "pending_events": []})
b_n = bridge.SatelliteTimerBridge(hass_n, ["dev1"])
check("store: load empty store restores 0",
      _LOOP.run_until_complete(b_n.async_load_persisted(store_n)) == 0)
check("store bridge starts", _LOOP.run_until_complete(b_n.start()) is True)
n_id = b_n.start_timer("dev1", "Pasta", 600)
b_n._in_snapshot = True
try:
    _LOOP.run_until_complete(b_n.persist())
    check("persist during snapshot raises", False)
except RuntimeError:
    check("persist during snapshot raises", True)
finally:
    b_n._in_snapshot = False
# started_at omission, branch 1 (live): a live timer whose start was
# never observed omits started_at — no derivation from finishes_at.
b_n._observed_started_at.pop(n_id, None)
snap = b_n.snapshot(datetime.now(timezone.utc))
check("live timer without an observed start omits started_at",
      snap[n_id].started_at is None)
# started_at omission, branch 2 (restored finished — the §27 undo shape):
# a finished record stored WITHOUT started_at must not show
# started_at = finished_at - duration.
now_n = datetime.now(timezone.utc)
store_omit = FakeStore({
    "version": 1,
    "timers": [{
        "timer_id": "01OMIT", "name": "Eggs", "device_id": "dev1",
        "state": "finished", "total_seconds": 600, "seconds_left": 0,
        "finishes_at": None,
        "finished_at": (now_n - timedelta(minutes=5)).isoformat(),
        "started_at": None,
        "language": "en", "started_via_service": True,
    }],
})
mgr_n2 = FakeTimerManager()
hass_n2 = FakeHassWithTasks(mgr_n2)
b_n2 = bridge.SatelliteTimerBridge(hass_n2, ["dev1"])
check("omission: finished record without started_at loads",
      _LOOP.run_until_complete(b_n2.async_load_persisted(store_omit)) == 1)
_LOOP.run_until_complete(b_n2.start())
snap = b_n2.snapshot(datetime.now(timezone.utc))
check("restored finished record omits started_at (no derivation)",
      snap["01OMIT"].started_at is None)
# started_at omission, branch 3 (finished live): a timer that finishes
# without a preceding STARTED tap omits started_at.
mgr_n3 = FakeTimerManager()
hass_n3 = FakeHassWithTasks(mgr_n3)
mgr_n3.handlers["dev1"] = original_handler
b_n3 = bridge.SatelliteTimerBridge(hass_n3, ["dev1"])
_LOOP.run_until_complete(b_n3.start())
ghost = FakeTimerInfo("01GHOST", "Ghost", 300, "dev1")
mgr_n3.timers["01GHOST"] = ghost
mgr_n3.timers.pop("01GHOST")
mgr_n3.handlers["dev1"]("finished", ghost)
snap = b_n3.snapshot(datetime.now(timezone.utc))
check("finished timer without an observed start omits started_at",
      snap["01GHOST"].started_at is None)
b_n.stop()
b_n2.stop()
b_n3.stop()

# --- load-loop guard (RB-R2-9) -------------------------------------------
# A corrupt timer record is logged and skipped; every record after it
# still loads, and `restored` counts the loaded records only.
store_bad = FakeStore({
    "version": 1,
    "timers": [
        {"timer_id": "01GOOD1", "name": "Good1", "device_id": "dev1",
         "state": "paused", "total_seconds": 300, "seconds_left": 200,
         "finishes_at": None, "finished_at": None, "started_at": None,
         "language": "en", "started_via_service": False},
        {"timer_id": "01BAD", "name": "Bad", "device_id": "dev1",
         "state": "paused", "total_seconds": "abc", "seconds_left": 0,
         "finishes_at": None, "finished_at": None, "started_at": None,
         "language": "en", "started_via_service": False},
        {"timer_id": "01GOOD2", "name": "Good2", "device_id": "dev1",
         "state": "paused", "total_seconds": 300, "seconds_left": 100,
         "finishes_at": None, "finished_at": None, "started_at": None,
         "language": "en", "started_via_service": False},
    ],
})
mgr_bad = FakeTimerManager()
hass_bad = FakeHassWithTasks(mgr_bad)
b_bad = bridge.SatelliteTimerBridge(hass_bad, ["dev1"])
# The load must LOG the corrupt record with its id (RB-R2-9) — capture
# the bridge logger's output and assert the warning is emitted. (On
# the rig the log sink is not readable, so this unit-level capture is
# the proof the log call executes.)
captured: list = []


class _Capture(logging.Handler):
    def emit(self, record):
        captured.append(record.getMessage())


_bridge_logger = logging.getLogger("bridge_under_test")
_bridge_logger.addHandler(_Capture())
_bridge_logger.setLevel(logging.INFO)
check("fault injection: corrupt record skipped, later records load",
      _LOOP.run_until_complete(b_bad.async_load_persisted(store_bad)) == 2
      and "01GOOD1" in b_bad._records
      and "01GOOD2" in b_bad._records
      and "01BAD" not in b_bad._records)
check("fault injection: corrupt record is logged with its id",
      any("Skipping corrupt persisted timer 01BAD" in m for m in captured))
check("fault injection: load logs a nonzero restored count",
      any("Restored 2 timers from storage" in m for m in captured))
b_bad.stop()
# A malformed pending record is skipped; a well-formed one loads.
now_pend = datetime.now(timezone.utc)
store_pend = FakeStore({
    "version": 1,
    "timers": [],
    "pending_events": [
        {"timer_id": "01P1", "device_id": "dev1", "event_type": "cancelled",
         "enqueued_at": now_pend.isoformat(),
         "direct_delivery_attempted": None},
        {"timer_id": "01P2", "device_id": "dev1", "event_type": "bogus",
         "enqueued_at": now_pend.isoformat(),
         "direct_delivery_attempted": None},
        {"timer_id": "01P3", "device_id": "dev1", "event_type": "finished",
         "enqueued_at": now_pend.isoformat(),
         "direct_delivery_attempted": None},
    ],
})
mgr_pend = FakeTimerManager()
hass_pend = FakeHassWithTasks(mgr_pend)
b_pend = bridge.SatelliteTimerBridge(hass_pend, ["dev1"])
_LOOP.run_until_complete(b_pend.async_load_persisted(store_pend))
check("pending: well-formed CANCELLED loads, malformed skipped",
      "01P1" in b_pend._pending
      and "01P2" not in b_pend._pending)
check("pending: FINISHED without a delivery flag is malformed",
      "01P3" not in b_pend._pending)
b_pend.stop()
# A v3-era store (no pending_events key) loads cleanly as an empty queue.
store_v3 = FakeStore({"version": 1, "timers": []})
mgr_v3 = FakeTimerManager()
hass_v3 = FakeHassWithTasks(mgr_v3)
b_v3 = bridge.SatelliteTimerBridge(hass_v3, ["dev1"])
check("v3-era store loads with an empty pending queue",
      _LOOP.run_until_complete(b_v3.async_load_persisted(store_v3)) == 0
      and b_v3._pending == {})
b_v3.stop()

# --- restore-carry invariant (RB-R2-10) ----------------------------------
# A pre-seeded pending CANCELLED for the OLD id survives a
# restore_finished (new id) and a persist round-trip untouched — the
# restore is a cross-id operation and the §5.3 supersede rule applies
# per timer id. This is the store-level invariant; the rig leg (dismiss
# -> undo-restore -> voice STARTED drain -> disclosed drain, no
# user-visible effect) runs after RB-R2-2 lands the queue drain.
now_carry = datetime.now(timezone.utc)
store_carry = FakeStore({
    "version": 1,
    "timers": [],
    "pending_events": [
        {"timer_id": "01OLD", "device_id": "dev1", "event_type": "cancelled",
         "enqueued_at": now_carry.isoformat(),
         "direct_delivery_attempted": None},
    ],
})
mgr_carry = FakeTimerManager()
hass_carry = FakeHassWithTasks(mgr_carry)
mgr_carry.handlers["dev1"] = original_handler
b_carry = bridge.SatelliteTimerBridge(hass_carry, ["dev1"])
_LOOP.run_until_complete(b_carry.async_load_persisted(store_carry))
_LOOP.run_until_complete(b_carry.start())
check("carry: pending CANCELLED loads",
      "01OLD" in b_carry._pending)
new_carry_id = b_carry.restore_finished(
    "dev1", "Eggs", 120, now_carry - timedelta(minutes=2),
    started_via_service=True,
)
check("carry: restore_finished enqueues nothing",
      b_carry._pending == {"01OLD": b_carry._pending["01OLD"]}
      and new_carry_id not in b_carry._pending)
_LOOP.run_until_complete(b_carry.persist())
mgr_carry2 = FakeTimerManager()
hass_carry2 = FakeHassWithTasks(mgr_carry2)
b_carry2 = bridge.SatelliteTimerBridge(hass_carry2, ["dev1"])
_LOOP.run_until_complete(b_carry2.async_load_persisted(store_carry))
check("carry: pending CANCELLED survives a persist round-trip",
      "01OLD" in b_carry2._pending
      and b_carry2._pending["01OLD"].event_type == "cancelled")
check("carry: restored finished record survives the round-trip",
      new_carry_id in b_carry2._records
      and b_carry2._records[new_carry_id].is_finished is True)
b_carry.stop()
b_carry2.stop()

# --- voice start survives a restart (tap-path persist, site 5) ------------
# A voice STARTED tap writes the store (FakeHassWithTasks runs the
# background save immediately); a fresh bridge with an empty
# TimerManager restores the timer as active with its observed start.
mgr_v = FakeTimerManager()
hass_v = FakeHassWithTasks(mgr_v)
mgr_v.handlers["dev1"] = original_handler
store_v = FakeStore()
b_v = bridge.SatelliteTimerBridge(hass_v, ["dev1"])
_LOOP.run_until_complete(b_v.async_load_persisted(store_v))
_LOOP.run_until_complete(b_v.start())
voice_timer = FakeTimerInfo("01VOICE", "Pasta", 600, "dev1")
mgr_v.timers["01VOICE"] = voice_timer
mgr_v.handlers["dev1"]("started", voice_timer)
check("voice start: tap-path persist wrote the store",
      isinstance(store_v.data, dict)
      and any(t["timer_id"] == "01VOICE"
              for t in store_v.data.get("timers", [])))
mgr_v2 = FakeTimerManager()
hass_v2 = FakeHassWithTasks(mgr_v2)
b_v2 = bridge.SatelliteTimerBridge(hass_v2, ["dev1"])
check("voice start: restart restores the timer",
      _LOOP.run_until_complete(b_v2.async_load_persisted(store_v)) == 1)
_LOOP.run_until_complete(b_v2.start())
snap = b_v2.snapshot(datetime.now(timezone.utc))
check("voice start: restored timer is active with an observed start",
      snap["01VOICE"].is_active is True
      and snap["01VOICE"].started_at is not None)
# ------------------------------------------------------- RB-R2-2 queue ----

def _make_handler(log):
    def handler(event_type, timer):
        log.append(f"{event_type}:{timer.id}")
    return handler


# --- enqueue / drain / supersede / purge (RB-R2-2) ---------------------
q_calls = []
q_mgr = FakeTimerManager()
q_hass = FakeHassWithTasks(q_mgr)
q_mgr.handlers["dev1"] = _make_handler(q_calls)
q_store = FakeStore()
q = bridge.SatelliteTimerBridge(q_hass, ["dev1"])
_LOOP.run_until_complete(q.async_load_persisted(q_store))
check("queue: bridge starts", _LOOP.run_until_complete(q.start()) is True)
t1 = FakeTimerInfo("Q01", "Pasta", 600, "dev1")
q_mgr.timers["Q01"] = t1
q_mgr.timers.pop("Q01")
q_mgr.handlers["dev1"]("finished", t1)
check("queue: tap finish enqueues flag-true FINISHED",
      len(q._pending) == 1
      and q._pending["Q01"].event_type == "finished"
      and q._pending["Q01"].direct_delivery_attempted is True)
check("queue: tap finish delivered once directly",
      q_calls.count("finished:Q01") == 1)
q._drain_device("dev1", purge=True, reason="test")
check("queue: flag-true FINISHED never re-sent on purge drain",
      q_calls.count("finished:Q01") == 1)
check("queue: flag-true record stays pending after drain",
      "Q01" in q._pending)
q.dismiss_timer("Q01")
check("queue: dismiss supersedes FINISHED with CANCELLED",
      len(q._pending) == 1
      and q._pending["Q01"].event_type == "cancelled")
check("queue: dismiss attempts CANCELLED at once",
      q_calls.count("cancelled:Q01") == 1)
check("queue: dismiss attempt does not purge", "Q01" in q._pending)
q._drain_device("dev1", purge=False, reason="test")
check("queue: non-purge drain re-attempts CANCELLED",
      q_calls.count("cancelled:Q01") == 2)
check("queue: non-purge drain keeps the record", "Q01" in q._pending)
q_mgr.handlers.pop("dev1", None)
q_mgr.handlers["dev1"] = _make_handler(q_calls)  # trigger 1
check("queue: trigger-1 re-register re-sends but does not purge",
      q_calls.count("cancelled:Q01") == 3 and "Q01" in q._pending)
qvt = FakeTimerInfo("Q03", "Rice", 300, "dev1")
q_mgr.timers["Q03"] = qvt
q_mgr.handlers["dev1"]("started", qvt)  # trigger 3, liveness drain
check("queue: voice STARTED purges after the trigger-1 re-send",
      q_calls.count("cancelled:Q01") == 4 and "Q01" not in q._pending)
t2 = FakeTimerInfo("Q02", "Rice", 300, "dev1")
q_mgr.timers["Q02"] = t2
q_mgr.timers.pop("Q02")
q_mgr.handlers["dev1"]("finished", t2)
q.dismiss_timer("Q02")
q._enqueue(bridge.PendingRecord(
    timer_id="Q02", device_id="dev1", event_type="finished",
    enqueued_at=datetime.now(timezone.utc),
    direct_delivery_attempted=False, name="Rice", total_seconds=300,
    language="en", is_finished=True))
check("queue: FINISHED never supersedes CANCELLED",
      q._pending["Q02"].event_type == "cancelled")
q.stop()

# --- _finished_may_resend matrix (Q12(b) + O1 gate) --------------------
fin_true = bridge.PendingRecord(
    timer_id="M1", device_id="dev1", event_type="finished",
    enqueued_at=datetime.now(timezone.utc),
    direct_delivery_attempted=True, name="Pasta", total_seconds=600,
    language="en", is_finished=True)
fin_false = bridge.PendingRecord(
    timer_id="M2", device_id="dev1", event_type="finished",
    enqueued_at=datetime.now(timezone.utc),
    direct_delivery_attempted=False, name="Pasta", total_seconds=600,
    language="en", is_finished=True)
check("may-resend: flag-true never re-sends (purge)",
      q._finished_may_resend(fin_true, purge_drain=True) is False)
check("may-resend: flag-true never re-sends (non-purge)",
      q._finished_may_resend(fin_true, purge_drain=False) is False)
check("may-resend: flag-false re-sends on purge drain",
      q._finished_may_resend(fin_false, purge_drain=True) is True)
check("may-resend: flag-false blocked on non-purge while O1 unsettled",
      q._finished_may_resend(fin_false, purge_drain=False) is False)
q._o1_alarm_behaviour = "rings_until_cancelled"
check("may-resend: flag-false re-sends on non-purge under O1 rings-until-cancelled",
      q._finished_may_resend(fin_false, purge_drain=False) is True)
q._o1_alarm_behaviour = "auto_stop"
check("may-resend: flag-false blocked on non-purge under O1 auto-stop",
      q._finished_may_resend(fin_false, purge_drain=False) is False)
q._o1_alarm_behaviour = None

# --- trigger 3: voice-originated STARTED drains with purge -------------
t3_calls = []
t3_mgr = FakeTimerManager()
t3_hass = FakeHassWithTasks(t3_mgr)
t3_mgr.handlers["dev1"] = _make_handler(t3_calls)
t3 = bridge.SatelliteTimerBridge(t3_hass, ["dev1"])
_LOOP.run_until_complete(t3.async_load_persisted(FakeStore()))
_LOOP.run_until_complete(t3.start())
t3f = FakeTimerInfo("T01", "Pasta", 600, "dev1")
t3_mgr.timers["T01"] = t3f
t3_mgr.timers.pop("T01")
t3_mgr.handlers["dev1"]("finished", t3f)
t3.dismiss_timer("T01")
check("trigger-3: CANCELLED pending before voice start", "T01" in t3._pending)
vt = FakeTimerInfo("T02", "Rice", 300, "dev1")
t3_mgr.timers["T02"] = vt
t3_mgr.handlers["dev1"]("started", vt)
check("trigger-3: voice STARTED purges the pending CANCELLED",
      "T01" not in t3._pending)
check("trigger-3: voice STARTED re-sent the CANCELLED once",
      t3_calls.count("cancelled:T01") == 2)
t3f2 = FakeTimerInfo("T03", "Soup", 600, "dev1")
t3_mgr.timers["T03"] = t3f2
t3_mgr.timers.pop("T03")
t3_mgr.handlers["dev1"]("finished", t3f2)
t3.dismiss_timer("T03")
t3.start_timer("dev1", "Eggs", 120)
check("trigger-3: service STARTED does not drain",
      "T03" in t3._pending
      and t3_calls.count("cancelled:T03") == 1)
t3.stop()

# --- trigger 2: state-change purge (ESPHome path) ----------------------
class _FakeState:
    def __init__(self, state):
        self.state = state


class _FakeEvent:
    def __init__(self, entity_id, old, new):
        self.data = {"entity_id": entity_id,
                     "old_state": old, "new_state": new}


t2_calls = []
t2_mgr = FakeTimerManager()
t2_hass = FakeHassWithTasks(t2_mgr)
t2_mgr.handlers["dev1"] = _make_handler(t2_calls)
t2 = bridge.SatelliteTimerBridge(t2_hass, ["dev1"])
_LOOP.run_until_complete(t2.async_load_persisted(FakeStore()))
_LOOP.run_until_complete(t2.start())
t2._device_entities = {"dev1": "assist_satellite.kitchen_hub"}
tf = FakeTimerInfo("S01", "Pasta", 600, "dev1")
t2_mgr.timers["S01"] = tf
t2_mgr.timers.pop("S01")
t2_mgr.handlers["dev1"]("finished", tf)
t2.dismiss_timer("S01")
_LOOP.run_until_complete(t2._on_satellite_state_change(
    _FakeEvent("assist_satellite.kitchen_hub",
               _FakeState("unavailable"), _FakeState("idle"))))
check("trigger-2: out-of-unavailable purges", "S01" not in t2._pending)
check("trigger-2: out-of-unavailable re-sends once",
      t2_calls.count("cancelled:S01") == 2)
tf2 = FakeTimerInfo("S02", "Rice", 300, "dev1")
t2_mgr.timers["S02"] = tf2
t2_mgr.timers.pop("S02")
t2_mgr.handlers["dev1"]("finished", tf2)
t2.dismiss_timer("S02")
_LOOP.run_until_complete(t2._on_satellite_state_change(
    _FakeEvent("assist_satellite.kitchen_hub",
               _FakeState("idle"), _FakeState("idle"))))
check("trigger-2: same-state change does not purge", "S02" in t2._pending)
check("trigger-2: same-state change re-attempts",
      t2_calls.count("cancelled:S02") == 2)
t2.stop()

# --- N3 (R2 fix1): a purge drain that observably fails does NOT purge --
n3_calls = []
n3_mgr = FakeTimerManager()
n3_hass = FakeHassWithTasks(n3_mgr)
n3_mgr.handlers["dev1"] = _make_handler(n3_calls)
n3 = bridge.SatelliteTimerBridge(n3_hass, ["dev1"])
_LOOP.run_until_complete(n3.async_load_persisted(FakeStore()))
_LOOP.run_until_complete(n3.start())
n3._device_entities = {"dev1": "assist_satellite.kitchen_hub"}
nf = FakeTimerInfo("N01", "Pasta", 600, "dev1")
n3_mgr.timers["N01"] = nf
n3_mgr.timers.pop("N01")
n3_mgr.handlers["dev1"]("finished", nf)
n3.dismiss_timer("N01")
check("N3: dismiss enqueues CANCELLED and sends it once",
      "N01" in n3._pending
      and n3._pending["N01"].event_type == "cancelled"
      and n3_calls.count("cancelled:N01") == 1)
check("N3: dismiss-time send succeeded, no failure mark",
      n3._pending["N01"].delivery_failed is False)
# The critic's window: the handler drops between the dismiss and the
# liveness drain, so the purge-drain attempt fails observably
# (no-handler). The record must survive for retry, with the mark set.
n3_mgr.handlers.pop("dev1", None)
_LOOP.run_until_complete(n3._on_satellite_state_change(
    _FakeEvent("assist_satellite.kitchen_hub",
               _FakeState("unavailable"), _FakeState("idle"))))
check("N3: failed purge drain does NOT purge the record",
      "N01" in n3._pending)
check("N3: failed purge drain marks delivery_failed",
      n3._pending["N01"].delivery_failed is True)
check("N3: failed purge drain made no successful send",
      n3_calls.count("cancelled:N01") == 1)
# The retry survives: on the next liveness signal with the handler
# back, the attempt succeeds and the record purges (a record that
# failed once keeps delivery_failed=True in the store — the purge
# decision keys on THIS attempt's outcome, not the stored flag).
# Restoring the handler fires trigger 1 first (re-send, no purge);
# the out-of-unavailable event then purges with one more send.
n3_mgr.handlers["dev1"] = _make_handler(n3_calls)
check("N3: handler restore (trigger 1) re-sends but does not purge",
      n3_calls.count("cancelled:N01") == 2 and "N01" in n3._pending)
_LOOP.run_until_complete(n3._on_satellite_state_change(
    _FakeEvent("assist_satellite.kitchen_hub",
               _FakeState("unavailable"), _FakeState("idle"))))
check("N3: retry on the next liveness signal re-sends once",
      n3_calls.count("cancelled:N01") == 3)
check("N3: retry on the next liveness signal purges",
      "N01" not in n3._pending)
n3.stop()

# --- trigger 4: 24 h prune ---------------------------------------------
p_calls = []
p_mgr = FakeTimerManager()
p_hass = FakeHassWithTasks(p_mgr)
p_mgr.handlers["dev1"] = _make_handler(p_calls)
p = bridge.SatelliteTimerBridge(p_hass, ["dev1"])
_LOOP.run_until_complete(p.async_load_persisted(FakeStore()))
_LOOP.run_until_complete(p.start())
p_now = datetime.now(timezone.utc)
p._enqueue(bridge.PendingRecord(
    timer_id="P01", device_id="dev1", event_type="cancelled",
    enqueued_at=p_now - timedelta(hours=25), name="Old",
    total_seconds=600, language="en", is_finished=True))
p._enqueue(bridge.PendingRecord(
    timer_id="P02", device_id="dev1", event_type="cancelled",
    enqueued_at=p_now - timedelta(hours=23), name="Young",
    total_seconds=600, language="en", is_finished=True))
p._records["P03"] = bridge.TimerRecord(
    "P03", "Soup", "dev1", False, 600, 0, None, is_finished=True,
    finished_at=p_now - timedelta(hours=25), language="en")
p._enqueue(bridge.PendingRecord(
    timer_id="P03", device_id="dev1", event_type="finished",
    enqueued_at=p_now - timedelta(hours=25),
    direct_delivery_attempted=False, name="Soup", total_seconds=600,
    language="en", is_finished=True))
p._records["P04"] = bridge.TimerRecord(
    "P04", "Rice", "dev1", False, 300, 0, None, is_finished=True,
    finished_at=p_now - timedelta(hours=23), language="en")
p._enqueue(bridge.PendingRecord(
    timer_id="P04", device_id="dev1", event_type="finished",
    enqueued_at=p_now - timedelta(hours=23),
    direct_delivery_attempted=False, name="Rice", total_seconds=300,
    language="en", is_finished=True))
_LOOP.run_until_complete(p._trigger4_tick(p_now))
check("prune: 25 h CANCELLED pruned", "P01" not in p._pending)
check("prune: 23 h CANCELLED survives", "P02" in p._pending)
check("prune: 25 h FINISHED pair pruned", "P03" not in p._pending)
check("prune: finished timer record survives its prune", "P03" in p._records)
check("prune: 23 h FINISHED pair survives", "P04" in p._pending)
check("prune: flag-false FINISHED not re-sent on trigger-4 while O1 unsettled",
      p_calls.count("finished:P04") == 0)
vt = FakeTimerInfo("P05", "Eggs", 120, "dev1")
p_mgr.timers["P05"] = vt
p_mgr.handlers["dev1"]("started", vt)
check("prune: 23 h FINISHED pair purges on voice STARTED",
      "P04" not in p._pending
      and p_calls.count("finished:P04") == 1)
p.stop()

# --- O1 face at tick level (rig leg waits on RB-R2-7's options flow) ---
o_calls = []
o_mgr = FakeTimerManager()
o_hass = FakeHassWithTasks(o_mgr)
o_mgr.handlers["dev1"] = _make_handler(o_calls)
o = bridge.SatelliteTimerBridge(o_hass, ["dev1"])
_LOOP.run_until_complete(o.async_load_persisted(FakeStore()))
_LOOP.run_until_complete(o.start())
o_now = datetime.now(timezone.utc)
o._records["O01"] = bridge.TimerRecord(
    "O01", "Pasta", "dev1", False, 600, 0, None, is_finished=True,
    finished_at=o_now - timedelta(hours=23), language="en")
o._enqueue(bridge.PendingRecord(
    timer_id="O01", device_id="dev1", event_type="finished",
    enqueued_at=o_now - timedelta(hours=23),
    direct_delivery_attempted=False, name="Pasta", total_seconds=600,
    language="en", is_finished=True))
o._o1_alarm_behaviour = "rings_until_cancelled"
_LOOP.run_until_complete(o._trigger4_tick(o_now))
check("O1: flag-false FINISHED re-sent on trigger-4 under rings-until-cancelled",
      o_calls.count("finished:O01") == 1)
check("O1: non-purge tick keeps the record", "O01" in o._pending)
o.stop()

# --- pending survival: failed trigger-4 attempt while disconnected -----
d_calls = []
d_mgr = FakeTimerManager()
d_hass = FakeHassWithTasks(d_mgr)
d_mgr.handlers["dev1"] = _make_handler(d_calls)
d = bridge.SatelliteTimerBridge(d_hass, ["dev1"])
d_store = FakeStore()
_LOOP.run_until_complete(d.async_load_persisted(d_store))
_LOOP.run_until_complete(d.start())
df = FakeTimerInfo("D01", "Pasta", 600, "dev1")
d_mgr.timers["D01"] = df
d_mgr.timers.pop("D01")
d_mgr.handlers["dev1"]("finished", df)
d.dismiss_timer("D01")
d_mgr.handlers.pop("dev1", None)  # disconnect
_LOOP.run_until_complete(d._trigger4_tick(datetime.now(timezone.utc)))
check("survival: failed trigger-4 attempt does not purge",
      "D01" in d._pending)
check("survival: failed attempt marks delivery_failed",
      d._pending["D01"].delivery_failed is True)
check("survival: the delivery_failed mark persists (repair-issue age)",
      any(p["timer_id"] == "D01" and p["delivery_failed"] is True
          for p in d_store.data["pending_events"]))
d.stop()

# --- repair issue (fake issue_registry injected into sys.modules) -------
class _FakeIssueRegistry:
    def __init__(self):
        self.issues = {}
        self.created = []
        self.deleted = []

    def async_get_issue(self, domain, issue_id):
        return self.issues.get((domain, issue_id))


ir_registry = _FakeIssueRegistry()
ir_module = types.ModuleType("homeassistant.helpers.issue_registry")
ir_module.async_get = lambda hass: ir_registry


def _ir_create(hass, domain, issue_id, **kwargs):
    ir_registry.created.append((domain, issue_id))
    ir_registry.issues[(domain, issue_id)] = object()


def _ir_delete(hass, domain, issue_id):
    ir_registry.deleted.append((domain, issue_id))
    ir_registry.issues.pop((domain, issue_id), None)


ir_module.async_create_issue = _ir_create
ir_module.async_delete_issue = _ir_delete


class _FakeIssueSeverity(StrEnum):
    WARNING = "warning"


ir_module.IssueSeverity = _FakeIssueSeverity
ha_helpers = types.ModuleType("homeassistant.helpers")
ha_helpers.issue_registry = ir_module
sys.modules["homeassistant.helpers"] = ha_helpers
sys.modules["homeassistant.helpers.issue_registry"] = ir_module

i_mgr = FakeTimerManager()
i_hass = FakeHassWithTasks(i_mgr)
i_mgr.handlers["dev1"] = _make_handler([])
i = bridge.SatelliteTimerBridge(i_hass, ["dev1"])
_LOOP.run_until_complete(i.async_load_persisted(FakeStore()))
_LOOP.run_until_complete(i.start())
i_now = datetime.now(timezone.utc)
i._enqueue(bridge.PendingRecord(
    timer_id="I01", device_id="dev1", event_type="cancelled",
    enqueued_at=i_now - timedelta(minutes=6), name="Old",
    total_seconds=600, language="en", is_finished=True,
    delivery_failed=True))
i._evaluate_repair_issues(i_now)
check("repair: old failed CANCELLED opens the issue",
      ("satellite_timers", "undelivered_timer_events")
      in ir_registry.created)
check("repair: issue present in the registry",
      i._repair_issue_present() is True)
i._pending.pop("I01", None)
i._evaluate_repair_issues(i_now)
check("repair: purge clears the issue",
      ("satellite_timers", "undelivered_timer_events")
      in ir_registry.deleted)
i._enqueue(bridge.PendingRecord(
    timer_id="I02", device_id="dev1", event_type="cancelled",
    enqueued_at=i_now - timedelta(minutes=4), name="Young",
    total_seconds=600, language="en", is_finished=True,
    delivery_failed=True))
i._evaluate_repair_issues(i_now)
check("repair: CANCELLED under 5 min opens nothing",
      ir_registry.created.count(
          ("satellite_timers", "undelivered_timer_events")) == 1)
i._pending.pop("I02", None)
i._enqueue(bridge.PendingRecord(
    timer_id="I03", device_id="dev1", event_type="finished",
    enqueued_at=i_now - timedelta(hours=2),
    direct_delivery_attempted=False, name="Soup", total_seconds=600,
    language="en", is_finished=True, delivery_failed=True))
i._evaluate_repair_issues(i_now)
check("repair: FINISHED-only pending opens nothing",
      ir_registry.created.count(
          ("satellite_timers", "undelivered_timer_events")) == 1)
i._pending.pop("I03", None)
i._enqueue(bridge.PendingRecord(
    timer_id="I04", device_id="dev1", event_type="cancelled",
    enqueued_at=i_now - timedelta(minutes=6), name="Old",
    total_seconds=600, language="en", is_finished=True,
    delivery_failed=False))
i._evaluate_repair_issues(i_now)
check("repair: CANCELLED without failed delivery opens nothing",
      ir_registry.created.count(
          ("satellite_timers", "undelivered_timer_events")) == 1)
i.stop()

# ------------------------------------------------------- RB-R2-3 reconcile

# --- no-handler vanish -> flag-false FINISHED, exactly one re-send -----
r_calls = []
r_mgr = FakeTimerManager()
r_hass = FakeHassWithTasks(r_mgr)
r_mgr.handlers["dev1"] = _make_handler(r_calls)
r = bridge.SatelliteTimerBridge(r_hass, ["dev1"])
_LOOP.run_until_complete(r.async_load_persisted(FakeStore()))
_LOOP.run_until_complete(r.start())
r_now0 = datetime.now(timezone.utc)
rt = FakeTimerInfo("R01", "Pasta", 600, "dev1")
r_mgr.timers["R01"] = rt
r_mgr.handlers["dev1"]("started", rt)
_LOOP.run_until_complete(r.reconcile_finished(r_now0))
check("reconcile: live timer seen", "R01" in r._live_seen)
r_mgr.handlers.pop("dev1", None)
r_mgr.timers.pop("R01", None)
_LOOP.run_until_complete(
    r.reconcile_finished(r_now0 + timedelta(seconds=601)))
check("reconcile: vanish past finishes_at creates a finished record",
      "R01" in r._records and r._records["R01"].is_finished)
check("reconcile: finished_at is the last-seen finishes_at",
      abs((r._records["R01"].finished_at
           - (r_now0 + timedelta(seconds=600))).total_seconds()) < 2)
check("reconcile: vanish enqueues flag-false FINISHED",
      "R01" in r._pending
      and r._pending["R01"].event_type == "finished"
      and r._pending["R01"].direct_delivery_attempted is False)
r_mgr.handlers["dev1"] = _make_handler(r_calls)  # trigger 1
check("reconcile: trigger 1 skips false-flag FINISHED while O1 unsettled",
      r_calls.count("finished:R01") == 0 and "R01" in r._pending)
rvt = FakeTimerInfo("R02", "Rice", 300, "dev1")
r_mgr.timers["R02"] = rvt
r_mgr.handlers["dev1"]("started", rvt)  # trigger 3, liveness drain
check("reconcile: exactly one re-send on the liveness drain",
      r_calls.count("finished:R01") == 1)
check("reconcile: record purged after the one re-send",
      "R01" not in r._pending)
r.stop()

# --- guard: paused-then-cancelled, gap > 1 tick -> no record -----------
g_calls = []
g_mgr = FakeTimerManager()
g_hass = FakeHassWithTasks(g_mgr)
g_mgr.handlers["dev1"] = _make_handler(g_calls)
g = bridge.SatelliteTimerBridge(g_hass, ["dev1"])
_LOOP.run_until_complete(g.async_load_persisted(FakeStore()))
_LOOP.run_until_complete(g.start())
g_now0 = datetime.now(timezone.utc)
gt = FakeTimerInfo("G01", "Pasta", 600, "dev1")
g_mgr.timers["G01"] = gt
g_mgr.handlers["dev1"]("started", gt)
_LOOP.run_until_complete(g.reconcile_finished(g_now0))
g_mgr.pause_timer("G01")
_LOOP.run_until_complete(g.reconcile_finished(g_now0 + timedelta(seconds=10)))
check("guard: paused timer seen inactive",
      g._live_seen["G01"].is_active is False)
g_mgr.handlers.pop("dev1", None)
g_mgr.cancel_timer("G01")
_LOOP.run_until_complete(g.reconcile_finished(g_now0 + timedelta(seconds=601)))
check("guard: paused-then-cancelled gap > 1 tick creates no record",
      "G01" not in g._records and "G01" not in g._pending)
g.stop()

# --- guard (e): same-tick pause+cancel -> disclosed false positive -----
e_calls = []
e_mgr = FakeTimerManager()
e_hass = FakeHassWithTasks(e_mgr)
e_mgr.handlers["dev1"] = _make_handler(e_calls)
e = bridge.SatelliteTimerBridge(e_hass, ["dev1"])
_LOOP.run_until_complete(e.async_load_persisted(FakeStore()))
_LOOP.run_until_complete(e.start())
e_now0 = datetime.now(timezone.utc)
et = FakeTimerInfo("E01", "Pasta", 600, "dev1")
e_mgr.timers["E01"] = et
e_mgr.handlers["dev1"]("started", et)
_LOOP.run_until_complete(e.reconcile_finished(e_now0))
e_mgr.pause_timer("E01")
e_mgr.handlers.pop("dev1", None)
e_mgr.cancel_timer("E01")
_LOOP.run_until_complete(e.reconcile_finished(e_now0 + timedelta(seconds=601)))
check("guard (e): same-tick pause+cancel enqueues the disclosed false positive",
      "E01" in e._records and e._records["E01"].is_finished
      and "E01" in e._pending
      and e._pending["E01"].direct_delivery_attempted is False)
e_mgr.handlers["dev1"] = _make_handler(e_calls)  # trigger 1
check("guard (e): trigger 1 skips the false positive while O1 unsettled",
      e_calls.count("finished:E01") == 0 and "E01" in e._pending)
evt = FakeTimerInfo("E02", "Rice", 300, "dev1")
e_mgr.timers["E02"] = evt
e_mgr.handlers["dev1"]("started", evt)  # trigger 3, liveness drain
check("guard (e): false positive re-sends exactly once then purges",
      e_calls.count("finished:E01") == 1 and "E01" not in e._pending)
e.stop()

# --- guard: paused-resumed-then-finished, no handler -> tick sync ------
s_calls = []
s_mgr = FakeTimerManager()
s_hass = FakeHassWithTasks(s_mgr)
s_mgr.handlers["dev1"] = _make_handler(s_calls)
s = bridge.SatelliteTimerBridge(s_hass, ["dev1"])
_LOOP.run_until_complete(s.async_load_persisted(FakeStore()))
_LOOP.run_until_complete(s.start())
s_now0 = datetime.now(timezone.utc)
st = FakeTimerInfo("S01", "Pasta", 30, "dev1")
s_mgr.timers["S01"] = st
s_mgr.handlers["dev1"]("started", st)
_LOOP.run_until_complete(s.reconcile_finished(s_now0))
s_mgr.pause_timer("S01")
s_mgr.handlers.pop("dev1", None)
st.is_active = True
st.updated_at = time.monotonic_ns()
_LOOP.run_until_complete(s.reconcile_finished(s_now0 + timedelta(seconds=5)))
check("guard: resumed timer seen active again",
      s._live_seen["S01"].is_active is True)
s_mgr.timers.pop("S01", None)
_LOOP.run_until_complete(s.reconcile_finished(s_now0 + timedelta(seconds=40)))
check("guard: paused-resumed-then-finished enqueues via tick sync",
      "S01" in s._records and s._records["S01"].is_finished
      and "S01" in s._pending)
s.stop()

# --- active-cancel: no record even after finishes_at passes ------------
a_calls = []
a_mgr = FakeTimerManager()
a_hass = FakeHassWithTasks(a_mgr)
a_mgr.handlers["dev1"] = _make_handler(a_calls)
a = bridge.SatelliteTimerBridge(a_hass, ["dev1"])
_LOOP.run_until_complete(a.async_load_persisted(FakeStore()))
_LOOP.run_until_complete(a.start())
a_now0 = datetime.now(timezone.utc)
at = FakeTimerInfo("A01", "Pasta", 600, "dev1")
a_mgr.timers["A01"] = at
a_mgr.handlers["dev1"]("started", at)
_LOOP.run_until_complete(a.reconcile_finished(a_now0))
a_mgr.cancel_timer("A01")
check("active-cancel: live cancel enqueues a CANCELLED retry",
      "A01" in a._pending and a._pending["A01"].event_type == "cancelled")
_LOOP.run_until_complete(a.reconcile_finished(a_now0 + timedelta(seconds=601)))
check("active-cancel: no finished record after finishes_at passes",
      "A01" not in a._records)
check("active-cancel: no FINISHED queued for a cancelled timer",
      a._pending["A01"].event_type == "cancelled")
a.stop()

# --- restored run-out: load-time case -> no queue, zero re-sends -------
past = datetime.now(timezone.utc) - timedelta(minutes=5)
store_old = FakeStore({
    "version": 1,
    "timers": [{
        "timer_id": "L01", "name": "Soup", "device_id": "dev1",
        "state": "active", "total_seconds": 600, "seconds_left": 0,
        "finishes_at": past.isoformat(), "finished_at": None,
        "started_at": (past - timedelta(seconds=600)).isoformat(),
        "language": "en", "started_via_service": False,
    }],
})
l_calls = []
l_mgr = FakeTimerManager()
l_hass = FakeHassWithTasks(l_mgr)
l_mgr.handlers["dev1"] = _make_handler(l_calls)
l = bridge.SatelliteTimerBridge(l_hass, ["dev1"])
_LOOP.run_until_complete(l.async_load_persisted(store_old))
_LOOP.run_until_complete(l.start())
check("restored run-out (load-time): restores as finished",
      "L01" in l._records and l._records["L01"].is_finished)
check("restored run-out (load-time): no queue record",
      len(l._pending) == 0)
l_mgr.handlers.pop("dev1", None)
l_mgr.handlers["dev1"] = _make_handler(l_calls)  # trigger 1
lvt = FakeTimerInfo("L02", "Rice", 120, "dev1")
l_mgr.timers["L02"] = lvt
l_mgr.handlers["dev1"]("started", lvt)  # trigger 3
_LOOP.run_until_complete(l._trigger4_tick(datetime.now(timezone.utc)))
check("restored run-out (load-time): zero re-sends on any drain",
      l_calls.count("finished:L01") == 0 and len(l._pending) == 0)
l.stop()

# --- restored run-out: post-load case -> flag-false + one re-send ------
future = datetime.now(timezone.utc) + timedelta(seconds=600)
store_future = FakeStore({
    "version": 1,
    "timers": [{
        "timer_id": "M01", "name": "Pasta", "device_id": "dev1",
        "state": "active", "total_seconds": 600, "seconds_left": 600,
        "finishes_at": future.isoformat(), "finished_at": None,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "language": "en", "started_via_service": False,
    }],
})
m_calls = []
m_mgr = FakeTimerManager()
m_hass = FakeHassWithTasks(m_mgr)
m_mgr.handlers["dev1"] = _make_handler(m_calls)
m = bridge.SatelliteTimerBridge(m_hass, ["dev1"])
_LOOP.run_until_complete(m.async_load_persisted(store_future))
_LOOP.run_until_complete(m.start())
check("restored run-out (post-load): active at load",
      "M01" in m._records and m._records["M01"].is_active)
_LOOP.run_until_complete(
    m.reconcile_finished(datetime.now(timezone.utc)
                         + timedelta(seconds=601)))
check("restored run-out (post-load): converts to finished after load",
      m._records["M01"].is_finished)
check("restored run-out (post-load): enqueues flag-false FINISHED",
      "M01" in m._pending
      and m._pending["M01"].direct_delivery_attempted is False)
m_mgr.handlers.pop("dev1", None)
m_mgr.handlers["dev1"] = _make_handler(m_calls)  # trigger 1
check("restored run-out (post-load): trigger 1 skips while O1 unsettled",
      m_calls.count("finished:M01") == 0 and "M01" in m._pending)
mvt = FakeTimerInfo("M02", "Rice", 300, "dev1")
m_mgr.timers["M02"] = mvt
m_mgr.handlers["dev1"]("started", mvt)  # trigger 3, liveness drain
check("restored run-out (post-load): one re-send then purge",
      m_calls.count("finished:M01") == 1 and "M01" not in m._pending)
m.stop()

# --- restart sends CANCELLED for the old id (R2-T4) ---------------------
k_calls = []
k_mgr = FakeTimerManager()
k_hass = FakeHassWithTasks(k_mgr)
k_mgr.handlers["dev1"] = _make_handler(k_calls)
k = bridge.SatelliteTimerBridge(k_hass, ["dev1"])
_LOOP.run_until_complete(k.async_load_persisted(FakeStore()))
_LOOP.run_until_complete(k.start())
kf = FakeTimerInfo("K01", "Pasta", 600, "dev1")
k_mgr.timers["K01"] = kf
k_mgr.timers.pop("K01")
k_mgr.handlers["dev1"]("finished", kf)
check("restart: flag-true FINISHED pending before restart",
      k._pending["K01"].event_type == "finished")
new_id = k.restart_timer("K01")
check("restart: CANCELLED enqueued for the old id",
      "K01" in k._pending and k._pending["K01"].event_type == "cancelled")
check("restart: CANCELLED attempted at once",
      k_calls.count("cancelled:K01") == 1)
check("restart: new timer runs", new_id in k_mgr.timers)
check("restart: old finished record dropped", "K01" not in k._records)
k.stop()

# --- cancel_restored enqueues a CANCELLED retry -------------------------
c_calls = []
c_mgr = FakeTimerManager()
c_hass = FakeHassWithTasks(c_mgr)
c_mgr.handlers["dev1"] = _make_handler(c_calls)
c_store = FakeStore({
    "version": 1,
    "timers": [{
        "timer_id": "C01", "name": "Pasta", "device_id": "dev1",
        "state": "active", "total_seconds": 600, "seconds_left": 600,
        "finishes_at": (datetime.now(timezone.utc)
                        + timedelta(seconds=600)).isoformat(),
        "finished_at": None,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "language": "en", "started_via_service": False,
    }],
})
c = bridge.SatelliteTimerBridge(c_hass, ["dev1"])
_LOOP.run_until_complete(c.async_load_persisted(c_store))
_LOOP.run_until_complete(c.start())
c.cancel_restored("C01")
check("cancel_restored: CANCELLED enqueued",
      "C01" in c._pending and c._pending["C01"].event_type == "cancelled")
check("cancel_restored: CANCELLED attempted at once",
      c_calls.count("cancelled:C01") == 1)
check("cancel_restored: record dropped", "C01" not in c._records)
c.stop()

# --- RB-R2-7: options resolution + constructor wiring ------------------
# resolve_bridge_options: missing keys resolve to the recorded defaults
# (Q11 settled never-ring, Q12 settled never re-send, O1 unsettled, no
# primary); an unknown stored value falls back to its default.
resolved_empty = bridge.resolve_bridge_options(None)
check("options: None resolves to the settled defaults",
      resolved_empty == {
          "q11_ring_timing": "never_ring",
          "q12_auto_stop_resend": "never_resend",
          "o1_alarm_behaviour": "unsettled",
          "primary_satellite": None,
      })
check("options: missing keys resolve to defaults",
      bridge.resolve_bridge_options({}) == resolved_empty)
check("options: settled picks round-trip",
      bridge.resolve_bridge_options({
          "q11_ring_timing": "never_ring",
          "q12_auto_stop_resend": "never_resend",
          "o1_alarm_behaviour": "rings_until_cancelled",
          "primary_satellite": "dev1",
      }) == {
          "q11_ring_timing": "never_ring",
          "q12_auto_stop_resend": "never_resend",
          "o1_alarm_behaviour": "rings_until_cancelled",
          "primary_satellite": "dev1",
      })
check("options: unknown values fall back to defaults",
      bridge.resolve_bridge_options({
          "q11_ring_timing": "bogus",
          "q12_auto_stop_resend": "bogus",
          "o1_alarm_behaviour": "bogus",
          "primary_satellite": 42,
      }) == resolved_empty)
# R2 fix1 (critic F1): the REJECTED Q11/Q12 literals are not unknown
# values — they are known-but-rejected — and they resolve to the
# settled values too, so a pre-fix stored rejected value can never
# linger in _queue_options as if it were the current pick.
check("options: rejected Q11/Q12 literals resolve to settled",
      bridge.resolve_bridge_options({
          "q11_ring_timing": "ring_on_liveness_drain",
          "q12_auto_stop_resend": "resend_once_per_registration",
      }) == resolved_empty)
check("options: rejected Q11(b) literal resolves to settled",
      bridge.resolve_bridge_options({
          "q11_ring_timing": "ring_at_bridge_start",
      })["q11_ring_timing"] == "never_ring")
check("options: empty-string primary is unset",
      bridge.resolve_bridge_options(
          {"primary_satellite": ""})["primary_satellite"] is None)
check("options: no Q13 key exists in the resolved options",
      "q13_bypass_suppression" not in resolved_empty
      and not any("q13" in key for key in resolved_empty))
# The bridge's literals are the same literals as const.py (canonical
# store). bridge.py repeats them inline because the unit tests load it
# with no package context; this pins the two copies equal.
_const_spec = importlib.util.spec_from_file_location(
    "const_under_test",
    Path(__file__).parent / "custom_components" / "satellite_timers" / "const.py",
)
_const_mod = importlib.util.module_from_spec(_const_spec)
_const_spec.loader.exec_module(_const_mod)
check("options: bridge literals match const.py",
      resolved_empty == {
          _const_mod.CONF_Q11_RING_TIMING: _const_mod.DEFAULT_Q11_RING_TIMING,
          _const_mod.CONF_Q12_AUTO_STOP_RESEND: _const_mod.DEFAULT_Q12_AUTO_STOP_RESEND,
          _const_mod.CONF_O1_ALARM_BEHAVIOUR: _const_mod.DEFAULT_O1_ALARM_BEHAVIOUR,
          _const_mod.CONF_PRIMARY_SATELLITE: None,
      })
check("options: const.py carries no Q13 key",
      not any("Q13" in name for name in dir(_const_mod)))

# Constructor: None resolves to defaults (the 16 two-arg constructions
# keep working); explicit options land on the bridge.
opt_hass = FakeHassWithTasks(FakeTimerManager())
opt_default = bridge.SatelliteTimerBridge(opt_hass, ["dev1"])
check("constructor: default options are the settled defaults",
      opt_default._queue_options == resolved_empty)
check("constructor: default O1 is unsettled (None)",
      opt_default._o1_alarm_behaviour is None)
check("constructor: default primary is None",
      opt_default.primary_satellite is None)
opt_settled = bridge.SatelliteTimerBridge(opt_hass, ["dev1"], {
    "q11_ring_timing": "never_ring",
    "q12_auto_stop_resend": "never_resend",
    "o1_alarm_behaviour": "rings_until_cancelled",
    "primary_satellite": "dev1",
})
check("constructor: settled O1 lands on the bridge",
      opt_settled._o1_alarm_behaviour == "rings_until_cancelled")
check("constructor: primary lands on the bridge",
      opt_settled.primary_satellite == "dev1")
check("constructor: settled picks land in _queue_options",
      opt_settled._queue_options["q11_ring_timing"] == "never_ring"
      and opt_settled._queue_options["q12_auto_stop_resend"] == "never_resend")
opt_auto = bridge.SatelliteTimerBridge(opt_hass, ["dev1"], {
    "o1_alarm_behaviour": "auto_stop",
})
check("constructor: auto-stop O1 lands on the bridge",
      opt_auto._o1_alarm_behaviour == "auto_stop")

# --- RB-R2-7: O1 gate at trigger level --------------------------------
# Unsettled blocks trigger 1 and trigger 4 for a false-flag FINISHED,
# but allows trigger 2 and trigger 3 (purge drains re-send regardless
# of O1 — the channel was just proven live and the attempt purges).
# Trigger 2 (out-of-unavailable) with O1 unsettled:
g2_calls = []
g2_mgr = FakeTimerManager()
g2_hass = FakeHassWithTasks(g2_mgr)
g2_mgr.handlers["dev1"] = _make_handler(g2_calls)
g2 = bridge.SatelliteTimerBridge(g2_hass, ["dev1"])
_LOOP.run_until_complete(g2.async_load_persisted(FakeStore()))
_LOOP.run_until_complete(g2.start())
g2._device_entities = {"dev1": "assist_satellite.kitchen_hub"}
g2_now = datetime.now(timezone.utc)
g2._records["G201"] = bridge.TimerRecord(
    "G201", "Pasta", "dev1", False, 600, 0, None, is_finished=True,
    finished_at=g2_now - timedelta(hours=1), language="en")
g2._enqueue(bridge.PendingRecord(
    timer_id="G201", device_id="dev1", event_type="finished",
    enqueued_at=g2_now - timedelta(hours=1),
    direct_delivery_attempted=False, name="Pasta", total_seconds=600,
    language="en", is_finished=True))
check("O1 gate: O1 is unsettled on a default bridge",
      g2._o1_alarm_behaviour is None)
_LOOP.run_until_complete(g2._on_satellite_state_change(
    _FakeEvent("assist_satellite.kitchen_hub",
               _FakeState("unavailable"), _FakeState("idle"))))
check("O1 gate: trigger 2 re-sends false-flag FINISHED while O1 unsettled",
      g2_calls.count("finished:G201") == 1)
check("O1 gate: trigger 2 purges it", "G201" not in g2._pending)
g2.stop()

# Trigger 1 (handler re-registration) with O1 settled rings-until-cancelled:
g1_calls = []
g1_mgr = FakeTimerManager()
g1_hass = FakeHassWithTasks(g1_mgr)
g1 = bridge.SatelliteTimerBridge(g1_hass, ["dev1"], {
    "o1_alarm_behaviour": "rings_until_cancelled",
})
_LOOP.run_until_complete(g1.async_load_persisted(FakeStore()))
_LOOP.run_until_complete(g1.start())
g1_now = datetime.now(timezone.utc)
g1._records["G101"] = bridge.TimerRecord(
    "G101", "Pasta", "dev1", False, 600, 0, None, is_finished=True,
    finished_at=g1_now - timedelta(hours=1), language="en")
g1._enqueue(bridge.PendingRecord(
    timer_id="G101", device_id="dev1", event_type="finished",
    enqueued_at=g1_now - timedelta(hours=1),
    direct_delivery_attempted=False, name="Pasta", total_seconds=600,
    language="en", is_finished=True))
g1_mgr.handlers["dev1"] = _make_handler(g1_calls)  # trigger 1
check("O1 gate: trigger 1 re-sends false-flag FINISHED under rings-until-cancelled",
      g1_calls.count("finished:G101") == 1)
check("O1 gate: trigger 1 never purges", "G101" in g1._pending)
g1.stop()

# Trigger 1 with O1 unsettled: zero sends (direct drain-level check;
# the reconcile-level equivalent is "trigger 1 skips false-flag
# FINISHED while O1 unsettled" above).
g0_calls = []
g0_mgr = FakeTimerManager()
g0_hass = FakeHassWithTasks(g0_mgr)
g0 = bridge.SatelliteTimerBridge(g0_hass, ["dev1"])
_LOOP.run_until_complete(g0.async_load_persisted(FakeStore()))
_LOOP.run_until_complete(g0.start())
g0_now = datetime.now(timezone.utc)
g0._records["G001"] = bridge.TimerRecord(
    "G001", "Pasta", "dev1", False, 600, 0, None, is_finished=True,
    finished_at=g0_now - timedelta(hours=1), language="en")
g0._enqueue(bridge.PendingRecord(
    timer_id="G001", device_id="dev1", event_type="finished",
    enqueued_at=g0_now - timedelta(hours=1),
    direct_delivery_attempted=False, name="Pasta", total_seconds=600,
    language="en", is_finished=True))
g0_mgr.handlers["dev1"] = _make_handler(g0_calls)  # trigger 1
check("O1 gate: trigger 1 sends nothing while O1 unsettled",
      g0_calls.count("finished:G001") == 0 and "G001" in g0._pending)
g0.stop()

b_v.stop()
b_v2.stop()

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("All bridge checks passed.")
