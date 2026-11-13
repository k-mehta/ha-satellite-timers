"""Standalone smoke test for bridge.py (the risky part: the handler tap).

Stubs the one Home Assistant import (``homeassistant.core.callback``) and
loads the real bridge.py, then drives it with a fake TimerManager that
behaves like Home Assistant's ``intent/timers.py``.

Run: python3 test_bridge.py
"""

from __future__ import annotations

import importlib.util
import json
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
# dismiss_timer returns (finished snapshot, immediate-send outcome)
# (round-2 B6): the outcome is the direct return, not bridge-global
# side-channel state, so concurrent dismisses cannot cross-talk.
dismissed, dismiss_outcome = b.dismiss_timer("01MNO")
check("dismiss returns the finished snapshot",
      dismissed.timer_id == "01MNO" and dismissed.is_finished)
check("dismiss returns the immediate-send outcome",
      dismiss_outcome == "sent")
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
check("queue: voice STARTED re-sends but does not purge the CANCELLED (B1)",
      q_calls.count("cancelled:Q01") == 4 and "Q01" in q._pending)
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
check("trigger-3: voice STARTED does NOT purge the pending CANCELLED (B1)",
      "T01" in t3._pending)
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
check("trigger-2: out-of-unavailable does NOT purge the CANCELLED (B1)",
      "S01" in t2._pending)
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
# back, the attempt succeeds — the record stays pending (B1: a
# CANCELLED never purges on "sent"). The delivery_failed mark does
# NOT clear on one clean re-send (F3 hysteresis): a single "sent" is
# consistent with a silent drop, so the mark lifts only after three
# consecutive clean sends. Restoring the handler fires trigger 1
# first (re-send, no purge); the out-of-unavailable event then
# re-sends once more without purging; a voice STARTED (trigger 3)
# supplies the third.
n3_mgr.handlers["dev1"] = _make_handler(n3_calls)
check("N3: handler restore (trigger 1) re-sends but does not purge",
      n3_calls.count("cancelled:N01") == 2 and "N01" in n3._pending)
check("N3: one clean re-send does NOT clear delivery_failed (F3)",
      n3._pending["N01"].delivery_failed is True
      and n3._pending["N01"].sent_streak == 1)
_LOOP.run_until_complete(n3._on_satellite_state_change(
    _FakeEvent("assist_satellite.kitchen_hub",
               _FakeState("unavailable"), _FakeState("idle"))))
check("N3: retry on the next liveness signal re-sends once",
      n3_calls.count("cancelled:N01") == 3)
check("N3: two clean re-sends still do not clear it (F3)",
      n3._pending["N01"].delivery_failed is True
      and n3._pending["N01"].sent_streak == 2)
check("N3: retry on the next liveness signal does NOT purge the CANCELLED (B1)",
      "N01" in n3._pending)
n3v = FakeTimerInfo("N02", "Rice", 300, "dev1")
n3_mgr.timers["N02"] = n3v
n3_mgr.handlers["dev1"]("started", n3v)
check("N3: third consecutive clean re-send clears delivery_failed (F3)",
      n3_calls.count("cancelled:N01") == 4
      and n3._pending["N01"].delivery_failed is False
      and n3._pending["N01"].sent_streak == 3)
check("N3: third clean re-send does NOT purge the CANCELLED (B1)",
      "N01" in n3._pending)
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
# F2 backoff: the dismiss's immediate send was attempt 1, so the
# first trigger-4 re-send is not due until 60 s later — a tick right
# now must skip it, and a tick past the wait must attempt (and fail).
_LOOP.run_until_complete(d._trigger4_tick(datetime.now(timezone.utc)))
check("survival: trigger-4 inside the backoff wait makes no attempt",
      d_calls.count("cancelled:D01") == 1
      and d._pending["D01"].delivery_failed is False)
_LOOP.run_until_complete(d._trigger4_tick(
    datetime.now(timezone.utc) + timedelta(seconds=61)))
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

# ------------------------------------------------------- B1 (cop-out fix)
# A dismiss is never confirmed: "sent" is not delivery confirmation,
# so no drain purges a pending CANCELLED on "sent" — it stays pending
# until the 24 h prune, with trigger 4 re-sending on the backoff
# cadence (60 s, then 5 min, then 15 min — F2), not every 60 s. The
# repair issue still fires for observably-failed sends and clears
# only after three consecutive clean sends (F3 hysteresis), not one.

b1_calls = []
b1_mgr = FakeTimerManager()
b1_hass = FakeHassWithTasks(b1_mgr)
b1_mgr.handlers["dev1"] = _make_handler(b1_calls)
b1 = bridge.SatelliteTimerBridge(b1_hass, ["dev1"])
b1_store = FakeStore()
_LOOP.run_until_complete(b1.async_load_persisted(b1_store))
_LOOP.run_until_complete(b1.start())
b1._device_entities = {"dev1": "assist_satellite.kitchen_hub"}
b1f = FakeTimerInfo("B01", "Pasta", 600, "dev1")
b1_mgr.timers["B01"] = b1f
b1_mgr.timers.pop("B01")
b1_mgr.handlers["dev1"]("finished", b1f)
b1.dismiss_timer("B01")
check("B1: dismiss enqueues CANCELLED and sends it once",
      "B01" in b1._pending
      and b1_calls.count("cancelled:B01") == 1)
# Trigger 3 (voice STARTED) and trigger 2 (out of unavailable) both
# re-send and neither purges on "sent".
b1v = FakeTimerInfo("B02", "Rice", 300, "dev1")
b1_mgr.timers["B02"] = b1v
b1_mgr.handlers["dev1"]("started", b1v)
check("B1: trigger-3 re-send does not purge",
      b1_calls.count("cancelled:B01") == 2 and "B01" in b1._pending)
_LOOP.run_until_complete(b1._on_satellite_state_change(
    _FakeEvent("assist_satellite.kitchen_hub",
               _FakeState("unavailable"), _FakeState("idle"))))
check("B1: trigger-2 re-send does not purge",
      b1_calls.count("cancelled:B01") == 3 and "B01" in b1._pending)
# Trigger 4 backs off (F2): the dismiss sent once, trigger 3 and
# trigger 2 re-sent once each, so the next trigger-4 re-send is not
# due until 15 min after the last attempt — ticks inside the wait
# send nothing, and the record stays pending throughout.
b1_now = datetime.now(timezone.utc)
_LOOP.run_until_complete(b1._trigger4_tick(b1_now))
_LOOP.run_until_complete(b1._trigger4_tick(b1_now + timedelta(seconds=60)))
check("B1: trigger-4 backs off and re-sends nothing inside the wait",
      b1_calls.count("cancelled:B01") == 3 and "B01" in b1._pending)
_LOOP.run_until_complete(b1._trigger4_tick(b1_now + timedelta(minutes=15)))
check("B1: trigger-4 re-sends once the backoff wait elapses",
      b1_calls.count("cancelled:B01") == 4 and "B01" in b1._pending)
# Observable failure marks the record; the mark persists, and the
# repair issue opens once the record is 5 min old. The failed attempt
# was the 5th (after the 15-min wait), so the next re-send waits 15 min.
# (The tick itself re-evaluates the repair issue, so the creation
# count is snapshotted BEFORE the tick, not after it.)
_created_before = ir_registry.created.count(
    ("satellite_timers", "undelivered_timer_events"))
b1_mgr.handlers.pop("dev1", None)
_LOOP.run_until_complete(b1._trigger4_tick(b1_now + timedelta(minutes=30)))
check("B1: failed trigger-4 attempt marks delivery_failed",
      b1._pending["B01"].delivery_failed is True)
check("B1: failed mark persists",
      any(p["timer_id"] == "B01" and p["delivery_failed"] is True
          for p in b1_store.data["pending_events"]))
check("B1: old failed CANCELLED opens the repair issue",
      ir_registry.created.count(
          ("satellite_timers", "undelivered_timer_events"))
      == _created_before + 1
      and b1._repair_issue_present() is True)
b1._evaluate_repair_issues(b1_now + timedelta(minutes=6))
check("B1: re-evaluating does not open a duplicate issue",
      ir_registry.created.count(
          ("satellite_timers", "undelivered_timer_events"))
      == _created_before + 1)
_deleted_before = ir_registry.deleted.count(
    ("satellite_timers", "undelivered_timer_events"))
b1_mgr.handlers["dev1"] = _make_handler(b1_calls)
# F3 hysteresis: restoring the handler fires trigger 1, whose clean
# re-send is streak 1 (mark stays); the next trigger-4 re-sends, 15
# min apart on the backoff ladder, are streaks 2 and 3 — the third
# consecutive clean re-send clears the mark.
check("B1: trigger-1 re-send on handler restore is streak 1, mark stays (F3)",
      b1_calls.count("cancelled:B01") == 5
      and b1._pending["B01"].delivery_failed is True
      and b1._pending["B01"].sent_streak == 1)
_LOOP.run_until_complete(b1._trigger4_tick(b1_now + timedelta(minutes=45)))
check("B1: two clean re-sends still do not clear it (F3)",
      b1._pending["B01"].delivery_failed is True
      and b1._pending["B01"].sent_streak == 2)
_LOOP.run_until_complete(b1._trigger4_tick(b1_now + timedelta(minutes=60)))
check("B1: third consecutive clean re-send clears delivery_failed (F3)",
      b1._pending["B01"].delivery_failed is False
      and b1._pending["B01"].sent_streak == 3)
check("B1: cleared mark persists",
      any(p["timer_id"] == "B01" and p["delivery_failed"] is False
          for p in b1_store.data["pending_events"]))
check("B1: record still pending after the mark clears",
      "B01" in b1._pending)
b1._evaluate_repair_issues(b1_now + timedelta(minutes=76))
check("B1: repair issue clears once the path is repeatedly open",
      ir_registry.deleted.count(
          ("satellite_timers", "undelivered_timer_events"))
      == _deleted_before + 1)
# Only the 24 h prune clears an unconfirmed CANCELLED.
b1._pending["B01"] = dataclass_replace(
    b1._pending["B01"],
    enqueued_at=datetime.now(timezone.utc) - timedelta(hours=25))
_LOOP.run_until_complete(b1._trigger4_tick(datetime.now(timezone.utc)))
check("B1: 24 h prune clears the unconfirmed CANCELLED",
      "B01" not in b1._pending)
b1.stop()

# ------------------------------------------------- fix-burst F2: backoff
# A CANCELLED that never gets a liveness signal retries on trigger 4
# at 60 s, then 5 min, then every 15 min — bounded, never infinite.
# Cost over 24 h: 1 + 1 + ~95 ≈ 100 sends, not ~1,440.

f2_calls = []
f2_mgr = FakeTimerManager()
f2_hass = FakeHassWithTasks(f2_mgr)
f2_mgr.handlers["dev1"] = _make_handler(f2_calls)
f2 = bridge.SatelliteTimerBridge(f2_hass, ["dev1"])
_LOOP.run_until_complete(f2.async_load_persisted(FakeStore()))
_LOOP.run_until_complete(f2.start())
f2_now = datetime.now(timezone.utc)
f2._enqueue(bridge.PendingRecord(
    timer_id="F201", device_id="dev1", event_type="cancelled",
    enqueued_at=f2_now, name="Pasta",
    total_seconds=600, language="en", is_finished=True))
_LOOP.run_until_complete(f2._trigger4_tick(f2_now))
check("F2: first trigger-4 attempt is due at once (no attempt yet)",
      f2_calls.count("cancelled:F201") == 1
      and f2._pending["F201"].attempt_count == 1)
_LOOP.run_until_complete(f2._trigger4_tick(f2_now + timedelta(seconds=59)))
check("F2: no re-send 59 s after attempt 1",
      f2_calls.count("cancelled:F201") == 1)
_LOOP.run_until_complete(f2._trigger4_tick(f2_now + timedelta(seconds=60)))
check("F2: re-send due 60 s after attempt 1",
      f2_calls.count("cancelled:F201") == 2
      and f2._pending["F201"].attempt_count == 2)
_LOOP.run_until_complete(f2._trigger4_tick(
    f2_now + timedelta(seconds=60 + 299)))
check("F2: no re-send 299 s after attempt 2",
      f2_calls.count("cancelled:F201") == 2)
_LOOP.run_until_complete(f2._trigger4_tick(
    f2_now + timedelta(seconds=60 + 300)))
check("F2: re-send due 5 min after attempt 2",
      f2_calls.count("cancelled:F201") == 3
      and f2._pending["F201"].attempt_count == 3)
_LOOP.run_until_complete(f2._trigger4_tick(
    f2_now + timedelta(seconds=360 + 899)))
check("F2: no re-send 899 s after attempt 3",
      f2_calls.count("cancelled:F201") == 3)
_LOOP.run_until_complete(f2._trigger4_tick(
    f2_now + timedelta(seconds=360 + 900)))
check("F2: re-send due 15 min after attempt 3",
      f2_calls.count("cancelled:F201") == 4)
_LOOP.run_until_complete(f2._trigger4_tick(
    f2_now + timedelta(seconds=1260 + 900)))
check("F2: steady 15 min cadence after attempt 4",
      f2_calls.count("cancelled:F201") == 5)
check("F2: backoff ladder is 60 s, 5 min, 15 min (not flat 60 s)",
      bridge.SatelliteTimerBridge._CANCELLED_BACKOFF
      == (timedelta(seconds=60), timedelta(minutes=5),
          timedelta(minutes=15)))
# FINISHED re-sends are NOT backoff-gated (different mechanism — the
# Q11/Q12 exactly-once-attempt path): a flag-false FINISHED stamped
# as just-attempted many times still re-sends on the next tick.
f2._o1_alarm_behaviour = "rings_until_cancelled"
f2._records["F203"] = bridge.TimerRecord(
    "F203", "Soup", "dev1", False, 600, 0, None, is_finished=True,
    finished_at=f2_now + timedelta(seconds=2160), language="en")
f2._enqueue(bridge.PendingRecord(
    timer_id="F203", device_id="dev1", event_type="finished",
    enqueued_at=f2_now + timedelta(seconds=2160),
    direct_delivery_attempted=False, name="Soup", total_seconds=600,
    language="en", is_finished=True))
f2._pending["F203"] = dataclass_replace(
    f2._pending["F203"], attempt_count=10,
    last_attempt_at=f2_now + timedelta(seconds=2160))
_LOOP.run_until_complete(f2._trigger4_tick(
    f2_now + timedelta(seconds=2160 + 1)))
check("F2: FINISHED re-send ignores the CANCELLED backoff",
      f2_calls.count("finished:F203") == 1
      and f2_calls.count("cancelled:F201") == 5)
f2.stop()

# A liveness drain re-sends immediately even inside the backoff wait
# (backoff gates trigger 4 only) and restarts the clock: after the
# drain's attempt 2 the next trigger-4 re-send waits 5 min, not 60 s.
f2b_calls = []
f2b_mgr = FakeTimerManager()
f2b_hass = FakeHassWithTasks(f2b_mgr)
f2b_mgr.handlers["dev1"] = _make_handler(f2b_calls)
f2b = bridge.SatelliteTimerBridge(f2b_hass, ["dev1"])
_LOOP.run_until_complete(f2b.async_load_persisted(FakeStore()))
_LOOP.run_until_complete(f2b.start())
f2bf = FakeTimerInfo("F2B1", "Pasta", 600, "dev1")
f2b_mgr.timers["F2B1"] = f2bf
f2b_mgr.timers.pop("F2B1")
f2b_mgr.handlers["dev1"]("finished", f2bf)
f2b.dismiss_timer("F2B1")  # attempt 1
f2bv = FakeTimerInfo("F2B2", "Rice", 300, "dev1")
f2b_mgr.timers["F2B2"] = f2bv
f2b_mgr.handlers["dev1"]("started", f2bv)  # trigger 3, attempt 2
check("F2: voice STARTED re-sends inside the backoff wait",
      f2b_calls.count("cancelled:F2B1") == 2)
f2b_now = datetime.now(timezone.utc)
_LOOP.run_until_complete(f2b._trigger4_tick(f2b_now + timedelta(seconds=61)))
check("F2: trigger-4 still waits after the liveness re-send (attempt 2 -> 5 min)",
      f2b_calls.count("cancelled:F2B1") == 2)
_LOOP.run_until_complete(f2b._trigger4_tick(f2b_now + timedelta(seconds=301)))
check("F2: trigger-4 re-sends once the 5 min wait elapses",
      f2b_calls.count("cancelled:F2B1") == 3)
f2b.stop()

# ------------------------------------------------- fix-burst F3: hysteresis
# set -> clear -> set: the repair issue opens on an observably-failed
# send, does NOT flap while the handler flaps (a lone "sent" never
# clears the mark), clears only after three consecutive clean sends,
# and re-opens on a genuine new failure.

f3_calls = []
f3_mgr = FakeTimerManager()
f3_hass = FakeHassWithTasks(f3_mgr)
f3_mgr.handlers["dev1"] = _make_handler(f3_calls)
f3 = bridge.SatelliteTimerBridge(f3_hass, ["dev1"])
_LOOP.run_until_complete(f3.async_load_persisted(FakeStore()))
_LOOP.run_until_complete(f3.start())
f3_now = datetime.now(timezone.utc)
f3._enqueue(bridge.PendingRecord(
    timer_id="F301", device_id="dev1", event_type="cancelled",
    enqueued_at=f3_now - timedelta(minutes=6), name="Pasta",
    total_seconds=600, language="en", is_finished=True,
    delivery_failed=True))
f3_created = ir_registry.created.count(
    ("satellite_timers", "undelivered_timer_events"))
f3_deleted = ir_registry.deleted.count(
    ("satellite_timers", "undelivered_timer_events"))
f3._evaluate_repair_issues(f3_now)
check("F3: old failed CANCELLED opens the issue",
      ir_registry.created.count(
          ("satellite_timers", "undelivered_timer_events"))
      == f3_created + 1
      and f3._repair_issue_present() is True)
_LOOP.run_until_complete(f3._trigger4_tick(f3_now))
check("F3: one clean tick is streak 1, mark stays",
      f3._pending["F301"].delivery_failed is True
      and f3._pending["F301"].sent_streak == 1)
f3._evaluate_repair_issues(f3_now)
check("F3: one clean tick does not delete the issue (no flap)",
      ir_registry.deleted.count(
          ("satellite_timers", "undelivered_timer_events")) == f3_deleted
      and f3._repair_issue_present() is True)
f3_mgr.handlers.pop("dev1", None)
_LOOP.run_until_complete(f3._trigger4_tick(f3_now + timedelta(minutes=5)))
check("F3: failed tick resets the streak, mark stays",
      f3._pending["F301"].delivery_failed is True
      and f3._pending["F301"].sent_streak == 0)
f3._evaluate_repair_issues(f3_now + timedelta(minutes=5))
check("F3: failed tick does not open a duplicate issue",
      ir_registry.created.count(
          ("satellite_timers", "undelivered_timer_events"))
      == f3_created + 1)
f3_mgr.handlers["dev1"] = _make_handler(f3_calls)  # trigger 1: streak 1
check("F3: trigger-1 re-send on restore is streak 1",
      f3._pending["F301"].sent_streak == 1
      and f3._pending["F301"].delivery_failed is True)
_LOOP.run_until_complete(f3._trigger4_tick(f3_now + timedelta(minutes=20)))
check("F3: second consecutive clean tick is streak 2, issue stays open",
      f3._pending["F301"].sent_streak == 2
      and f3._repair_issue_present() is True)
_LOOP.run_until_complete(f3._trigger4_tick(f3_now + timedelta(minutes=35)))
check("F3: third consecutive clean tick clears the mark",
      f3._pending["F301"].delivery_failed is False
      and f3._pending["F301"].sent_streak == 3)
f3._evaluate_repair_issues(f3_now + timedelta(minutes=35))
check("F3: issue deletes once the path is repeatedly open",
      ir_registry.deleted.count(
          ("satellite_timers", "undelivered_timer_events"))
      == f3_deleted + 1)
f3_mgr.handlers.pop("dev1", None)
_LOOP.run_until_complete(f3._trigger4_tick(f3_now + timedelta(minutes=50)))
check("F3: a new failure re-sets the mark",
      f3._pending["F301"].delivery_failed is True
      and f3._pending["F301"].sent_streak == 0)
f3._evaluate_repair_issues(f3_now + timedelta(minutes=50))
check("F3: a new failure re-opens the issue (set -> clear -> set)",
      ir_registry.created.count(
          ("satellite_timers", "undelivered_timer_events"))
      == f3_created + 2
      and f3._repair_issue_present() is True)
f3.stop()

# ------------------------------------------------- fix-burst F4: re-dismiss
# After a dismiss the finished record is gone, but the pending
# CANCELLED is still retrying — redismiss_timer re-sends the stop
# for that id at once, without requiring the finished record.

f4_calls = []
f4_mgr = FakeTimerManager()
f4_hass = FakeHassWithTasks(f4_mgr)
f4_mgr.handlers["dev1"] = _make_handler(f4_calls)
f4 = bridge.SatelliteTimerBridge(f4_hass, ["dev1"])
f4_store = FakeStore()
_LOOP.run_until_complete(f4.async_load_persisted(f4_store))
_LOOP.run_until_complete(f4.start())
f4f = FakeTimerInfo("F401", "Pasta", 600, "dev1")
f4_mgr.timers["F401"] = f4f
f4_mgr.timers.pop("F401")
f4_mgr.handlers["dev1"]("finished", f4f)
check("F4: no pending CANCELLED before dismiss",
      f4.pending_cancelled() == [])
f4.dismiss_timer("F401")
check("F4: dismiss drops the finished record and keeps the CANCELLED",
      "F401" not in f4._records and "F401" in f4._pending)
check("F4: pending_cancelled names the dismissed timer",
      [s.timer_id for s in f4.pending_cancelled()] == ["F401"]
      and f4.pending_cancelled()[0].name == "Pasta")
try:
    f4.dismiss_timer("F401")
    check("F4: dismiss_timer raises KeyError for the dismissed id", False)
except KeyError:
    check("F4: dismiss_timer raises KeyError for the dismissed id", True)
check("F4: redismiss re-sends the stop at once and reports sent",
      f4.redismiss_timer("F401") == "sent"
      and f4_calls.count("cancelled:F401") == 2)
check("F4: redismiss never purges",
      "F401" in f4._pending)
f4_mgr.handlers.pop("dev1", None)
check("F4: redismiss returns the observably-failed outcome",
      f4.redismiss_timer("F401") == "no-handler")
check("F4: failed redismiss marks delivery_failed",
      f4._pending["F401"].delivery_failed is True)
check("F4: failed redismiss still does not purge",
      "F401" in f4._pending)
try:
    f4.redismiss_timer("NOPE")
    check("F4: redismiss raises KeyError for an unknown id", False)
except KeyError:
    check("F4: redismiss raises KeyError for an unknown id", True)
f4._enqueue(bridge.PendingRecord(
    timer_id="F402", device_id="dev1", event_type="finished",
    enqueued_at=datetime.now(timezone.utc),
    direct_delivery_attempted=False, name="Soup", total_seconds=300,
    language="en", is_finished=True))
try:
    f4.redismiss_timer("F402")
    check("F4: redismiss raises KeyError for a pending FINISHED", False)
except KeyError:
    check("F4: redismiss raises KeyError for a pending FINISHED", True)
f4.stop()

# ------------------------------------------------- fix-burst F5: unconfirmed purge
# A FINISHED purge spends the record's one re-ring attempt on an
# unconfirmed "sent" — it is counted and logged as a warning, so a
# first ring lost to a silent drop leaves a trace. The purge
# behaviour itself is unchanged.

f5_calls = []
f5_mgr = FakeTimerManager()
f5_hass = FakeHassWithTasks(f5_mgr)
f5_mgr.handlers["dev1"] = _make_handler(f5_calls)
f5 = bridge.SatelliteTimerBridge(f5_hass, ["dev1"])
_LOOP.run_until_complete(f5.async_load_persisted(FakeStore()))
_LOOP.run_until_complete(f5.start())
f5._device_entities = {"dev1": "assist_satellite.kitchen_hub"}
check("F5: counter starts at zero", f5.unconfirmed_finished_purges == 0)
f5._enqueue(bridge.PendingRecord(
    timer_id="F501", device_id="dev1", event_type="finished",
    enqueued_at=datetime.now(timezone.utc),
    direct_delivery_attempted=False, name="Pasta", total_seconds=600,
    language="en", is_finished=True))
f5_logged_before = len(captured)
_LOOP.run_until_complete(f5._on_satellite_state_change(
    _FakeEvent("assist_satellite.kitchen_hub",
               _FakeState("unavailable"), _FakeState("idle"))))
check("F5: liveness purge sends the first ring once",
      f5_calls.count("finished:F501") == 1)
check("F5: liveness purge removes the record",
      "F501" not in f5._pending)
check("F5: unconfirmed purge is counted",
      f5.unconfirmed_finished_purges == 1)
check("F5: unconfirmed purge is logged distinctly (warning)",
      any("unconfirmed 'sent'" in m for m in captured[f5_logged_before:]))
f5.stop()

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

# ------------------------------------------------- round-2 B1: drain bookkeeping persists
# The critic's exact scenario: dismiss with the handler down, then a
# trigger-4 tick with the handler back. Pre-fix the tick's "sent"
# left (streak=1, count=2) in memory only — the store still said
# (failed, count=1, streak=0) — so a restart reset the backoff
# ladder and the streak, and under repeated restarts the repair
# issue could never clear. Every drain attempt now persists its
# bookkeeping, so the values survive a reload. The handler is
# restored with dict.__setitem__ (bypassing the tap) so the
# re-registration itself does not drain first as trigger 1 — this
# pins the trigger-4 path, the one the critic reproduced.
b1_calls = []
b1_mgr = FakeTimerManager()
b1_hass = FakeHassWithTasks(b1_mgr)
b1_mgr.handlers["dev1"] = _make_handler(b1_calls)
b1 = bridge.SatelliteTimerBridge(b1_hass, ["dev1"])
b1_store = FakeStore()
_LOOP.run_until_complete(b1.async_load_persisted(b1_store))
_LOOP.run_until_complete(b1.start())
b1f = FakeTimerInfo("B101", "Pasta", 600, "dev1")
b1_mgr.timers["B101"] = b1f
b1_mgr.timers.pop("B101")
b1_mgr.handlers["dev1"]("finished", b1f)
b1_mgr.handlers.pop("dev1", None)  # handler down: the dismiss cannot reach it
_, b1_outcome = b1.dismiss_timer("B101")
check("B1: dismiss with handler down reports no-handler",
      b1_outcome == "no-handler")
_LOOP.run_until_complete(b1.persist())
b1_stored = {p["timer_id"]: p for p in b1_store.data["pending_events"]}
check("B1: store after dismiss is (failed, count=1, streak=0)",
      b1_stored["B101"]["delivery_failed"] is True
      and b1_stored["B101"]["attempt_count"] == 1
      and b1_stored["B101"]["sent_streak"] == 0)
b1_last = b1._pending["B101"].last_attempt_at
dict.__setitem__(b1_mgr.handlers, "dev1", _make_handler(b1_calls))
_LOOP.run_until_complete(b1._trigger4_tick(b1_last + timedelta(seconds=61)))
check("B1: trigger-4 tick with handler back sends once",
      b1_calls.count("cancelled:B101") == 1)
check("B1: tick is streak 1, count 2 in memory",
      b1._pending["B101"].sent_streak == 1
      and b1._pending["B101"].attempt_count == 2)
b1_stored = {p["timer_id"]: p for p in b1_store.data["pending_events"]}
check("B1: store after the tick is (failed, count=2, streak=1)",
      b1_stored["B101"]["delivery_failed"] is True
      and b1_stored["B101"]["attempt_count"] == 2
      and b1_stored["B101"]["sent_streak"] == 1)
# A fresh bridge reloading that store keeps the streak and the count:
# the restart no longer resets the backoff ladder or the hysteresis.
b1_mgr2 = FakeTimerManager()
b1_hass2 = FakeHassWithTasks(b1_mgr2)
b1_mgr2.handlers["dev1"] = _make_handler(b1_calls)
b1_reloaded = bridge.SatelliteTimerBridge(b1_hass2, ["dev1"])
_LOOP.run_until_complete(b1_reloaded.async_load_persisted(b1_store))
check("B1: reload keeps streak=1, count=2, last_attempt_at",
      b1_reloaded._pending["B101"].sent_streak == 1
      and b1_reloaded._pending["B101"].attempt_count == 2
      and b1_reloaded._pending["B101"].last_attempt_at
      == b1_last + timedelta(seconds=61))
b1.stop()

# ------------------------------------------------- round-2 B2: repair-issue text states the true clear conditions
_b2_strings = json.loads(
    (Path(__file__).parent / "custom_components" / "satellite_timers"
     / "strings.json").read_text())
_b2_translations = json.loads(
    (Path(__file__).parent / "custom_components" / "satellite_timers"
     / "translations" / "en.json").read_text())
b2_text = _b2_strings["issues"]["undelivered_timer_events"]["description"]
check("B2: strings and translations agree on the repair-issue text",
      _b2_translations["issues"]["undelivered_timer_events"]["description"]
      == b2_text)
# Full-sentence pins (burst-5 delta critic Findings 3-4; burst-6 delta
# critic Finding 2). Each clause is one constant shared by the
# production check and the mutant replace, so a wording drift that
# leaves the replace a no-op is caught by the construction assertion
# instead of rotting silently. Each clause spans its FULL sentence:
# the burst-5 spans stopped at the junction ("...reach the satellite
# again", "...it answered"), so an append landing just past the span
# — "— or after just one attempt" after "reachable again", "— unless
# the hub is busy, in which case 48 hours" after "it answered", or a
# negation before the span — left every clause matching (burst-6
# critic V1, V2, V4). The full sentences catch those. What no span
# can catch is an append AFTER the sentence end (burst-6 critic V4b:
# a new sentence after "dismiss the timer again."), so the three
# negative assertions below pin the forbidden phrases anywhere in
# the text. _b2_clause24 ends at "dismiss the timer again." — the
# sentence end — and swallows nothing beyond it. NOTE (verifier's
# non-blocking observation 2): the coupling is STILL PRESENT, not
# resolved — _b2_clause24 joins the expiry clause to the whole
# "Reaching the satellite…" sentence, so a future legitimate
# rewording of that sentence alone would still trip the 24 h check.
# Measured: the span contains that sentence verbatim, so any edit
# inside it breaks the match. The check below is therefore named
# for what the span actually pins (expiry AND reaching caveat), so
# the name does not mislead when it trips.
_b2_clause46 = ("usually within about 46 minutes after the satellite "
                "is reachable again, but a failed stop attempt restarts "
                "the wait, so one failed attempt can push it to about "
                "91 minutes, and if the satellite keeps failing or keeps "
                "going unavailable it may "
                "not clear until the record expires after 24 hours.")
_b2_clause3 = ("The hub keeps retrying the stop and clears this warning "
               "after three consecutive stop attempts reach the satellite "
               "again — usually within about 46 minutes after the "
               "satellite is reachable again, but a failed stop attempt "
               "restarts the wait, so one failed attempt can push it to "
               "about 91 minutes, and if the satellite keeps failing or "
               "keeps going unavailable it may not clear until the "
               "record expires after 24 hours.")
_b2_clause24 = ("until the record expires after 24 hours. Reaching "
                "the satellite only means it answered — it does not "
                "prove the stop arrived — so if the satellite is still "
                "ringing, dismiss the timer again.")
check("B2: text clears after three consecutive attempts reaching the satellite",
      _b2_clause3 in b2_text)
check("B2: text states the usual 46 min clear time and the failed-attempt overrun (B3)",
      _b2_clause46 in b2_text)
# Negative assertions (burst-6 delta critic Finding 2): no span can
# catch an append after its sentence end, so the forbidden phrases
# are pinned directly — anywhere in the text, not just inside a
# clause. V4b ("Unless the hub is busy, in which case 48 hours." as
# a new sentence) is caught ONLY by these, which is expected.
check("B2: text never promises a one-attempt clear anywhere",
      "or after just one attempt" not in b2_text)
check("B2: text never promises a 48 h expiry anywhere",
      "48 hours" not in b2_text)
check("B2: text never says the warning never clears",
      "never clears" not in b2_text)
# Negative control (burst-4 critic Finding 2): the old bare-substring
# check passed on a wrong-meaning sentence — the critic's mutation C
# rewrote the bound clause to "after the satellite rings for
# 46 minutes", which no longer states when the warning clears, and
# the full suite still passed. The full-clause check must reject
# that mutant, while the bare numeral still matches it (which is
# exactly why the substring check was blind).
_b2_mutant = b2_text.replace(
    _b2_clause46, "after the satellite rings for 46 minutes")
check("B2: mutant construction replaced the clause",
      _b2_mutant != b2_text)
check("B2: the bare numeral still matches the wrong-meaning mutant",
      "46 minutes" in _b2_mutant)
check("B2: the full-clause check rejects the wrong-meaning mutant",
      _b2_clause46 not in _b2_mutant)
# Negative controls (burst-5 delta critic Finding 4): the sibling
# bare-substring checks were blind to the same append trick — a
# mutant preserving "three consecutive" but adding "— or after just
# one attempt", and one preserving "24 hours" but adding ", or after
# 48 hours if the hub is busy", both passed the suite. Each control
# below reproduces the critic's mutant exactly (bare substring still
# matches, full clause does not), with the same construction
# assertion so it cannot rot.
_b2_mutant3 = b2_text.replace(
    _b2_clause3,
    "The hub keeps retrying the stop and clears this warning after "
    "three consecutive stop attempts reach the satellite again — or "
    "after just one attempt — usually within about 46 minutes after "
    "the satellite is reachable again, but a failed stop attempt "
    "restarts the wait, so one failed attempt can push it to about "
    "91 minutes, and if the satellite keeps failing or keeps going "
    "unavailable it may not "
    "clear until the record expires after 24 hours.")
check("B2: three-attempt mutant construction replaced the clause",
      _b2_mutant3 != b2_text)
# B2: this bare-phrase control is meaningful only for mutants preserving the bare phrase; V3-class mutants break the phrase itself and trip the production check instead — _b2_mutant_v3 below drops "consecutive", so the bare phrase no longer matches and the full-clause check must reject it.
check("B2: the bare 'three consecutive' still matches the mutant",
      "three consecutive stop attempts reach the satellite"
      in _b2_mutant3)
check("B2: the full-clause check rejects the one-attempt mutant",
      _b2_clause3 not in _b2_mutant3)
# Negative control (burst-8 delta critic Finding 2): the V3 class —
# a mutant that drops "consecutive" from the three-attempt phrase,
# so the production check itself must trip. The burst-7 comment
# invoked this class without any such mutant existing in-suite, so
# the claim was true only by construction; this mutant makes it
# checkable.
_b2_mutant_v3 = b2_text.replace(
    "three consecutive stop attempts reach the satellite again",
    "three stop attempts reach the satellite again")
check("B2: V3 mutant construction replaced the phrase",
      _b2_mutant_v3 != b2_text)
check("B2: the bare 'three consecutive' no longer matches the V3 mutant",
      "three consecutive stop attempts reach the satellite"
      not in _b2_mutant_v3)
check("B2: the full-clause check rejects the dropped-'consecutive' mutant",
      _b2_clause3 not in _b2_mutant_v3)
check("B2: text states the 24 h expiry and the reaching caveat",
      _b2_clause24 in b2_text)
_b2_mutant24 = b2_text.replace(
    _b2_clause24,
    "until the record expires after 24 hours, or after 48 hours "
    "if the hub is busy. Reaching the satellite only means it "
    "answered — it does not prove the stop arrived — so if the "
    "satellite is still ringing, dismiss the timer again.")
check("B2: 24 h mutant construction replaced the clause",
      _b2_mutant24 != b2_text)
check("B2: the bare '24 hours' still matches the mutant",
      "24 hours" in _b2_mutant24)
check("B2: the full-clause check rejects the 48 h mutant",
      _b2_clause24 not in _b2_mutant24)
check("B2: text no longer promises a supersede clear",
      "newer event" not in b2_text and "replaces it" not in b2_text)
check("B2: text no longer promises one attempt clears it",
      "a later stop attempt reaches the satellite again" not in b2_text)
check("B2: _evaluate_repair_issues docstring drops the supersede clause",
      "replaced by a newer event"
      not in bridge.SatelliteTimerBridge._evaluate_repair_issues.__doc__)
# Docstring-number pins (burst-8 delta critic Finding 3): the
# docstring states the single-failure clear range, and no test
# pinned it — every other docstring number has a docstring-contains
# check. The range sentence wraps across three docstring lines, so
# the first three spans pin it line by line; the fourth pins the
# "about 91 minutes" overrun in the quoted user-facing sentence,
# and the fifth pins that the old "61–91" range is gone.
check("B2: docstring states the single-failure clear range",
      "single failed attempt clears at ~46–91"
      in bridge.SatelliteTimerBridge._evaluate_repair_issues.__doc__)
check("B2: docstring states the range is measured from the last attempt once the backoff ladder is at the",
      "min from the last attempt once the backoff ladder is at the"
      in bridge.SatelliteTimerBridge._evaluate_repair_issues.__doc__)
check("B2: docstring states the 61 min floor is for due-tick failures at the 15-minute step only",
      "61 min is only the floor for failures at due ticks at that"
      in bridge.SatelliteTimerBridge._evaluate_repair_issues.__doc__)
# Burst-10 F2: the pin above ends at "at that", so the sentence tail
# "step." had no coverage — mutating it tripped zero pins. The
# single-line span below pins the tail; the pin above is labelled
# for what its span actually covers ("at the", not "the 15-minute
# step" — the step itself is pinned here and by the B3 pin below).
check("B2: docstring pins the tail of the range sentence at that step",
      "step. The skip and the streak-reset-on-failure"
      in bridge.SatelliteTimerBridge._evaluate_repair_issues.__doc__)
check("B2: docstring quotes the about-91-minutes overrun",
      "can push it to about 91 minutes"
      in bridge.SatelliteTimerBridge._evaluate_repair_issues.__doc__)
check("B2: docstring no longer states the 61–91 range",
      "61–91"
      not in bridge.SatelliteTimerBridge._evaluate_repair_issues.__doc__)

# ------------------------------------------------- round-2 B3: worst-case clear is just under 46 min
# Ladder 60 s -> 5 min -> 15 min; clearing needs 3 consecutive
# "sent"s. Last attempt just before recovery (count >= 3) clears at
# +15, +30, +45 min when the ticks are aligned — but attempts can
# only land on 60 s trigger-4 ticks, and every ladder step is a
# multiple of 60 s, so a phase offset of just under 60 s on the
# first attempt is preserved, not accumulated: off-phase the
# attempts land at +15:59, +30:59 and +45:59. The true bound is
# just under 46 min, not 45 (burst-3 critic Finding 1). The
# repair-issue text states the usual 46 min clear time plus the
# failed-attempt overrun (B2).
b3_calls = []
b3_mgr = FakeTimerManager()
b3_hass = FakeHassWithTasks(b3_mgr)
b3_mgr.handlers["dev1"] = _make_handler(b3_calls)
b3 = bridge.SatelliteTimerBridge(b3_hass, ["dev1"])
_LOOP.run_until_complete(b3.async_load_persisted(FakeStore()))
_LOOP.run_until_complete(b3.start())
b3_t0 = datetime.now(timezone.utc)
b3._enqueue(bridge.PendingRecord(
    timer_id="B301", device_id="dev1", event_type="cancelled",
    enqueued_at=b3_t0 - timedelta(minutes=6), name="Pasta",
    total_seconds=600, language="en", is_finished=True,
    delivery_failed=True, attempt_count=3,
    last_attempt_at=b3_t0))
b3_created = ir_registry.created.count(
    ("satellite_timers", "undelivered_timer_events"))
b3_deleted = ir_registry.deleted.count(
    ("satellite_timers", "undelivered_timer_events"))
b3._evaluate_repair_issues(b3_t0)
check("B3: old failed CANCELLED opens the issue",
      ir_registry.created.count(
          ("satellite_timers", "undelivered_timer_events"))
      == b3_created + 1
      and b3._repair_issue_present() is True)
_LOOP.run_until_complete(b3._trigger4_tick(b3_t0 + timedelta(minutes=15)))
check("B3: +15 min is streak 1, mark and issue stay",
      b3._pending["B301"].sent_streak == 1
      and b3._pending["B301"].delivery_failed is True
      and b3._repair_issue_present() is True)
_LOOP.run_until_complete(b3._trigger4_tick(b3_t0 + timedelta(minutes=30)))
check("B3: +30 min is streak 2, mark and issue stay",
      b3._pending["B301"].sent_streak == 2
      and b3._pending["B301"].delivery_failed is True
      and b3._repair_issue_present() is True)
_LOOP.run_until_complete(b3._trigger4_tick(b3_t0 + timedelta(minutes=45)))
check("B3: +45 min is streak 3 and the mark clears",
      b3._pending["B301"].sent_streak == 3
      and b3._pending["B301"].delivery_failed is False)
check("B3: issue deletes at +45 min, not before",
      ir_registry.deleted.count(
          ("satellite_timers", "undelivered_timer_events"))
      == b3_deleted + 1
      and b3._repair_issue_present() is False)
b3.stop()

# Off-phase ticks (burst-3 critic Finding 1): the first due time
# (+15:00) falls just after a tick (+14:59), so the first attempt
# waits for the +15:59 tick; the 60 s phase error is preserved by
# the 15 min steps, so the streak completes at +45:59 — after the
# 45 min mark, inside 46 min.
b3b_calls = []
b3b_mgr = FakeTimerManager()
b3b_hass = FakeHassWithTasks(b3b_mgr)
b3b_mgr.handlers["dev1"] = _make_handler(b3b_calls)
b3b = bridge.SatelliteTimerBridge(b3b_hass, ["dev1"])
_LOOP.run_until_complete(b3b.async_load_persisted(FakeStore()))
_LOOP.run_until_complete(b3b.start())
b3b_t0 = datetime.now(timezone.utc)
b3b._enqueue(bridge.PendingRecord(
    timer_id="B302", device_id="dev1", event_type="cancelled",
    enqueued_at=b3b_t0 - timedelta(minutes=6), name="Pasta",
    total_seconds=600, language="en", is_finished=True,
    delivery_failed=True, attempt_count=3,
    last_attempt_at=b3b_t0))
b3b_created = ir_registry.created.count(
    ("satellite_timers", "undelivered_timer_events"))
b3b_deleted = ir_registry.deleted.count(
    ("satellite_timers", "undelivered_timer_events"))
b3b._evaluate_repair_issues(b3b_t0)
check("B3 off-phase: old failed CANCELLED opens the issue",
      ir_registry.created.count(
          ("satellite_timers", "undelivered_timer_events"))
      == b3b_created + 1
      and b3b._repair_issue_present() is True)
_LOOP.run_until_complete(
    b3b._trigger4_tick(b3b_t0 + timedelta(minutes=14, seconds=59)))
check("B3 off-phase: tick just before the +15 min due time is no attempt",
      b3b._pending["B302"].sent_streak == 0
      and b3b._pending["B302"].delivery_failed is True
      and b3b._repair_issue_present() is True)
_LOOP.run_until_complete(
    b3b._trigger4_tick(b3b_t0 + timedelta(minutes=15, seconds=59)))
check("B3 off-phase: +15:59 is streak 1, mark and issue stay",
      b3b._pending["B302"].sent_streak == 1
      and b3b._pending["B302"].delivery_failed is True
      and b3b._repair_issue_present() is True)
_LOOP.run_until_complete(
    b3b._trigger4_tick(b3b_t0 + timedelta(minutes=30, seconds=59)))
check("B3 off-phase: +30:59 is streak 2, mark and issue stay",
      b3b._pending["B302"].sent_streak == 2
      and b3b._pending["B302"].delivery_failed is True
      and b3b._repair_issue_present() is True)
_LOOP.run_until_complete(b3b._trigger4_tick(b3b_t0 + timedelta(minutes=45)))
check("B3 off-phase: +45:00 is still streak 2 — the 45 min bound understates",
      b3b._pending["B302"].sent_streak == 2
      and b3b._pending["B302"].delivery_failed is True
      and b3b._repair_issue_present() is True)
_LOOP.run_until_complete(
    b3b._trigger4_tick(b3b_t0 + timedelta(minutes=45, seconds=59)))
check("B3 off-phase: +45:59 is streak 3 and the mark clears",
      b3b._pending["B302"].sent_streak == 3
      and b3b._pending["B302"].delivery_failed is False)
check("B3 off-phase: issue deletes at +45:59, inside 46 min",
      ir_registry.deleted.count(
          ("satellite_timers", "undelivered_timer_events"))
      == b3b_deleted + 1
      and b3b._repair_issue_present() is False)
b3b.stop()

# ------------------------------------------------- burst-5 delta critic Finding 1: repeated skips
# Each unavailable-skip at a due tick restarts the backoff clock from
# the actual (delayed) attempt time, so each skip adds up to one
# further 60 s tick — N skips clear at 45:59 + N min. Two skips on
# the first two due ticks (+15:59, +16:59) clear at +47:59 (47.98
# min), not +45:59: the first attempt lands at +17:59, then +32:59,
# then +47:59. The skip itself is correct and unchanged; this pins
# the numbers the docstring states so they are load-bearing.
class _B3cStates:
    def __init__(self):
        self.map = {}

    def get(self, entity_id):
        return self.map.get(entity_id)


b3c_calls = []
b3c_mgr = FakeTimerManager()
b3c_hass = FakeHassWithTasks(b3c_mgr)
b3c_mgr.handlers["dev1"] = _make_handler(b3c_calls)
b3c = bridge.SatelliteTimerBridge(b3c_hass, ["dev1"])
_LOOP.run_until_complete(b3c.async_load_persisted(FakeStore()))
_LOOP.run_until_complete(b3c.start())
b3c._device_entities = {"dev1": "assist_satellite.kitchen_hub"}
b3c_states = _B3cStates()
b3c_hass.states = b3c_states
b3c_t0 = datetime.now(timezone.utc)
b3c._enqueue(bridge.PendingRecord(
    timer_id="B303", device_id="dev1", event_type="cancelled",
    enqueued_at=b3c_t0 - timedelta(minutes=6), name="Pasta",
    total_seconds=600, language="en", is_finished=True,
    delivery_failed=True, attempt_count=3,
    last_attempt_at=b3c_t0))
b3c_created = ir_registry.created.count(
    ("satellite_timers", "undelivered_timer_events"))
b3c_deleted = ir_registry.deleted.count(
    ("satellite_timers", "undelivered_timer_events"))
b3c._evaluate_repair_issues(b3c_t0)
check("B3 two skips: old failed CANCELLED opens the issue",
      ir_registry.created.count(
          ("satellite_timers", "undelivered_timer_events"))
      == b3c_created + 1
      and b3c._repair_issue_present() is True)


def _b3c_tick(when, unavailable):
    b3c_states.map["assist_satellite.kitchen_hub"] = _FakeState(
        "unavailable" if unavailable else "idle")
    _LOOP.run_until_complete(b3c._trigger4_tick(when))


_b3c_tick(b3c_t0 + timedelta(minutes=14, seconds=59), False)
check("B3 two skips: tick just before the +15 min due time is no attempt",
      b3c._pending["B303"].sent_streak == 0
      and b3c_calls.count("cancelled:B303") == 0)
_b3c_tick(b3c_t0 + timedelta(minutes=15, seconds=59), True)
check("B3 two skips: first skip at +15:59 is no attempt, clock untouched",
      b3c._pending["B303"].sent_streak == 0
      and b3c._pending["B303"].last_attempt_at == b3c_t0
      and b3c_calls.count("cancelled:B303") == 0
      and b3c._repair_issue_present() is True)
_b3c_tick(b3c_t0 + timedelta(minutes=16, seconds=59), True)
check("B3 two skips: second skip at +16:59 is no attempt, clock untouched",
      b3c._pending["B303"].sent_streak == 0
      and b3c._pending["B303"].last_attempt_at == b3c_t0
      and b3c_calls.count("cancelled:B303") == 0
      and b3c._repair_issue_present() is True)
_b3c_tick(b3c_t0 + timedelta(minutes=17, seconds=59), False)
check("B3 two skips: +17:59 is streak 1, mark and issue stay",
      b3c._pending["B303"].sent_streak == 1
      and b3c._pending["B303"].delivery_failed is True
      and b3c_calls.count("cancelled:B303") == 1
      and b3c._repair_issue_present() is True)
_b3c_tick(b3c_t0 + timedelta(minutes=31, seconds=59), False)
check("B3 two skips: +31:59 is still streak 1 — next due +32:59",
      b3c._pending["B303"].sent_streak == 1
      and b3c_calls.count("cancelled:B303") == 1)
_b3c_tick(b3c_t0 + timedelta(minutes=32, seconds=59), False)
check("B3 two skips: +32:59 is streak 2, mark and issue stay",
      b3c._pending["B303"].sent_streak == 2
      and b3c._pending["B303"].delivery_failed is True
      and b3c_calls.count("cancelled:B303") == 2
      and b3c._repair_issue_present() is True)
_b3c_tick(b3c_t0 + timedelta(minutes=46, seconds=59), False)
check("B3 two skips: +46:59 is still streak 2 — next due +47:59",
      b3c._pending["B303"].sent_streak == 2
      and b3c_calls.count("cancelled:B303") == 2)
# Non-vacuous "not before" (burst-6 delta critic Finding 4): the old
# terminal check below only asserted the cumulative deleted count
# equals baseline+1 after the clear tick, which is true after ANY
# earlier delete — in the no-skip mutant it passed even though the
# issue deleted at +45:59. Capture the count at the tick before the
# clear tick and assert it is unchanged here.
check("B3 two skips: issue still present at +46:59, not deleted before +47:59",
      ir_registry.deleted.count(
          ("satellite_timers", "undelivered_timer_events"))
      == b3c_deleted
      and b3c._repair_issue_present() is True)
_b3c_tick(b3c_t0 + timedelta(minutes=47, seconds=59), False)
check("B3 two skips: +47:59 is streak 3 and the mark clears",
      b3c._pending["B303"].sent_streak == 3
      and b3c._pending["B303"].delivery_failed is False
      and b3c_calls.count("cancelled:B303") == 3)
check("B3 two skips: issue deletes at +47:59, not before",
      ir_registry.deleted.count(
          ("satellite_timers", "undelivered_timer_events"))
      == b3c_deleted + 1
      and b3c._repair_issue_present() is False)
check("B3 two skips: docstring states the two-skip clear time",
      "two skips at +47:59"
      in bridge.SatelliteTimerBridge._evaluate_repair_issues.__doc__)
b3c.stop()

# ------------------------------------------------- burst-5 delta critic Finding 2: failed attempt resets the streak
# Any outcome other than "sent" in _attempt_send_for_pending resets
# sent_streak to 0 and re-stamps last_attempt_at, restarting the whole
# ladder from that attempt — strictly worse than a skip (+1 tick). A
# single failed trigger-4 attempt at streak 2 clears at +90:59
# (90.98 min) from the last attempt before recovery: the ladder
# restarts at +45:59, so the three "sent"s land at +60:59, +75:59 and
# +90:59. The handler is swapped silently (dict.__setitem__ bypasses
# the tap) so no trigger-1 drain fires on the swap itself.
def _b3d_raising_handler(log):
    def handler(event_type, timer):
        log.append(f"{event_type}:{timer.id}")
        raise RuntimeError("simulated handler failure")
    return handler


b3d_calls = []
b3d_mgr = FakeTimerManager()
b3d_hass = FakeHassWithTasks(b3d_mgr)
b3d_mgr.handlers["dev1"] = _make_handler(b3d_calls)
b3d = bridge.SatelliteTimerBridge(b3d_hass, ["dev1"])
_LOOP.run_until_complete(b3d.async_load_persisted(FakeStore()))
_LOOP.run_until_complete(b3d.start())
b3d_t0 = datetime.now(timezone.utc)
b3d._enqueue(bridge.PendingRecord(
    timer_id="B304", device_id="dev1", event_type="cancelled",
    enqueued_at=b3d_t0 - timedelta(minutes=6), name="Pasta",
    total_seconds=600, language="en", is_finished=True,
    delivery_failed=True, attempt_count=3,
    last_attempt_at=b3d_t0))
b3d_created = ir_registry.created.count(
    ("satellite_timers", "undelivered_timer_events"))
b3d_deleted = ir_registry.deleted.count(
    ("satellite_timers", "undelivered_timer_events"))
b3d._evaluate_repair_issues(b3d_t0)
check("B3 failed attempt: old failed CANCELLED opens the issue",
      ir_registry.created.count(
          ("satellite_timers", "undelivered_timer_events"))
      == b3d_created + 1
      and b3d._repair_issue_present() is True)
_LOOP.run_until_complete(
    b3d._trigger4_tick(b3d_t0 + timedelta(minutes=15, seconds=59)))
check("B3 failed attempt: +15:59 is streak 1, mark and issue stay",
      b3d._pending["B304"].sent_streak == 1
      and b3d._pending["B304"].delivery_failed is True
      and b3d._repair_issue_present() is True)
_LOOP.run_until_complete(
    b3d._trigger4_tick(b3d_t0 + timedelta(minutes=30, seconds=59)))
check("B3 failed attempt: +30:59 is streak 2, mark and issue stay",
      b3d._pending["B304"].sent_streak == 2
      and b3d._pending["B304"].delivery_failed is True
      and b3d._repair_issue_present() is True)
dict.__setitem__(b3d_mgr.handlers, "dev1", _b3d_raising_handler(b3d_calls))
_LOOP.run_until_complete(
    b3d._trigger4_tick(b3d_t0 + timedelta(minutes=45, seconds=59)))
check("B3 failed attempt: failed attempt at +45:59 resets the streak to 0",
      b3d._pending["B304"].sent_streak == 0
      and b3d._pending["B304"].delivery_failed is True
      and b3d._pending["B304"].last_attempt_at
      == b3d_t0 + timedelta(minutes=45, seconds=59)
      and b3d_calls.count("cancelled:B304") == 3
      and b3d._repair_issue_present() is True)
dict.__setitem__(b3d_mgr.handlers, "dev1", _make_handler(b3d_calls))
_LOOP.run_until_complete(
    b3d._trigger4_tick(b3d_t0 + timedelta(minutes=59, seconds=59)))
check("B3 failed attempt: +59:59 is still streak 0 — ladder restarted, next due +60:59",
      b3d._pending["B304"].sent_streak == 0
      and b3d_calls.count("cancelled:B304") == 3)
_LOOP.run_until_complete(
    b3d._trigger4_tick(b3d_t0 + timedelta(minutes=60, seconds=59)))
check("B3 failed attempt: +60:59 is streak 1, mark and issue stay",
      b3d._pending["B304"].sent_streak == 1
      and b3d._pending["B304"].delivery_failed is True
      and b3d_calls.count("cancelled:B304") == 4
      and b3d._repair_issue_present() is True)
_LOOP.run_until_complete(
    b3d._trigger4_tick(b3d_t0 + timedelta(minutes=75, seconds=59)))
check("B3 failed attempt: +75:59 is streak 2, mark and issue stay",
      b3d._pending["B304"].sent_streak == 2
      and b3d._pending["B304"].delivery_failed is True
      and b3d_calls.count("cancelled:B304") == 5
      and b3d._repair_issue_present() is True)
_LOOP.run_until_complete(
    b3d._trigger4_tick(b3d_t0 + timedelta(minutes=89, seconds=59)))
check("B3 failed attempt: +89:59 is still streak 2 — next due +90:59",
      b3d._pending["B304"].sent_streak == 2
      and b3d_calls.count("cancelled:B304") == 5)
# Non-vacuous "not before" (burst-6 delta critic Finding 4): as in
# B3c above — in the no-reset mutant the old terminal check passed
# even though the issue deleted at +60:59. Assert the deleted count
# is unchanged at the tick before the clear tick.
check("B3 failed attempt: issue still present at +89:59, not deleted before +90:59",
      ir_registry.deleted.count(
          ("satellite_timers", "undelivered_timer_events"))
      == b3d_deleted
      and b3d._repair_issue_present() is True)
_LOOP.run_until_complete(
    b3d._trigger4_tick(b3d_t0 + timedelta(minutes=90, seconds=59)))
check("B3 failed attempt: +90:59 is streak 3 and the mark clears",
      b3d._pending["B304"].sent_streak == 3
      and b3d._pending["B304"].delivery_failed is False
      and b3d_calls.count("cancelled:B304") == 6)
check("B3 failed attempt: issue deletes at +90:59, not before",
      ir_registry.deleted.count(
          ("satellite_timers", "undelivered_timer_events"))
      == b3d_deleted + 1
      and b3d._repair_issue_present() is False)
check("B3 failed attempt: docstring states the failed-attempt clear time",
      "+90:59 (90.98 min)"
      in bridge.SatelliteTimerBridge._evaluate_repair_issues.__doc__)
b3d.stop()

# ------------------------------------------------- burst-8 delta critic Finding 1 (F1): early-ladder failure clears sooner
# The docstring's "~46–91 min" range only summarizes count>=3
# measurements: the backoff delay depends on attempt_count (60 s /
# 5 min / 15 min), so a failure early in the ladder clears sooner. A
# CANCELLED record at attempt_count=1 whose satellite flaps
# (unavailable through the first due ticks) and then fails its first
# post-recovery attempt clears at +40:59 from the last attempt — 5
# min under the "~46" floor: the failed attempt at +5:59 re-stamps
# last_attempt_at at count 2, so the three "sent"s land at +10:59
# (5 min step), +25:59 and +40:59 (15 min steps). This pins the
# numbers behind the docstring's "earlier in the ladder it clears
# sooner" qualification. States + raising-handler swap follow the
# B3c / B3d harness patterns above.
b3e_calls = []
b3e_mgr = FakeTimerManager()
b3e_hass = FakeHassWithTasks(b3e_mgr)
b3e_mgr.handlers["dev1"] = _make_handler(b3e_calls)
b3e = bridge.SatelliteTimerBridge(b3e_hass, ["dev1"])
_LOOP.run_until_complete(b3e.async_load_persisted(FakeStore()))
_LOOP.run_until_complete(b3e.start())
b3e._device_entities = {"dev1": "assist_satellite.kitchen_hub"}
b3e_states = _B3cStates()
b3e_hass.states = b3e_states
b3e_t0 = datetime.now(timezone.utc)
b3e._enqueue(bridge.PendingRecord(
    timer_id="B305", device_id="dev1", event_type="cancelled",
    enqueued_at=b3e_t0 - timedelta(minutes=6), name="Pasta",
    total_seconds=600, language="en", is_finished=True,
    delivery_failed=True, attempt_count=1,
    last_attempt_at=b3e_t0))
b3e_created = ir_registry.created.count(
    ("satellite_timers", "undelivered_timer_events"))
b3e_deleted = ir_registry.deleted.count(
    ("satellite_timers", "undelivered_timer_events"))
b3e._evaluate_repair_issues(b3e_t0)
check("B3 flap-fail: old failed CANCELLED opens the issue",
      ir_registry.created.count(
          ("satellite_timers", "undelivered_timer_events"))
      == b3e_created + 1
      and b3e._repair_issue_present() is True)


def _b3e_tick(when, unavailable):
    b3e_states.map["assist_satellite.kitchen_hub"] = _FakeState(
        "unavailable" if unavailable else "idle")
    _LOOP.run_until_complete(b3e._trigger4_tick(when))


_b3e_tick(b3e_t0 + timedelta(seconds=59), True)
check("B3 flap-fail: +0:59 unavailable is no attempt, streak 0",
      b3e._pending["B305"].sent_streak == 0
      and b3e._pending["B305"].attempt_count == 1
      and b3e_calls.count("cancelled:B305") == 0)
_b3e_tick(b3e_t0 + timedelta(minutes=4, seconds=59), True)
check("B3 flap-fail: +4:59 unavailable is still no attempt, clock untouched",
      b3e._pending["B305"].sent_streak == 0
      and b3e._pending["B305"].attempt_count == 1
      and b3e._pending["B305"].last_attempt_at == b3e_t0
      and b3e_calls.count("cancelled:B305") == 0
      and b3e._repair_issue_present() is True)
dict.__setitem__(b3e_mgr.handlers, "dev1", _b3d_raising_handler(b3e_calls))
_b3e_tick(b3e_t0 + timedelta(minutes=5, seconds=59), False)
check("B3 flap-fail: failed attempt at +5:59 resets the streak to 0, count 2",
      b3e._pending["B305"].sent_streak == 0
      and b3e._pending["B305"].delivery_failed is True
      and b3e._pending["B305"].attempt_count == 2
      and b3e._pending["B305"].last_attempt_at
      == b3e_t0 + timedelta(minutes=5, seconds=59)
      and b3e_calls.count("cancelled:B305") == 1
      and b3e._repair_issue_present() is True)
dict.__setitem__(b3e_mgr.handlers, "dev1", _make_handler(b3e_calls))
_b3e_tick(b3e_t0 + timedelta(minutes=9, seconds=59), False)
check("B3 flap-fail: +9:59 is still streak 0 — ladder restarted, next due +10:59",
      b3e._pending["B305"].sent_streak == 0
      and b3e_calls.count("cancelled:B305") == 1)
_b3e_tick(b3e_t0 + timedelta(minutes=10, seconds=59), False)
check("B3 flap-fail: +10:59 is streak 1, mark and issue stay",
      b3e._pending["B305"].sent_streak == 1
      and b3e._pending["B305"].delivery_failed is True
      and b3e_calls.count("cancelled:B305") == 2
      and b3e._repair_issue_present() is True)
_b3e_tick(b3e_t0 + timedelta(minutes=24, seconds=59), False)
check("B3 flap-fail: +24:59 is still streak 1 — next due +25:59",
      b3e._pending["B305"].sent_streak == 1
      and b3e_calls.count("cancelled:B305") == 2)
_b3e_tick(b3e_t0 + timedelta(minutes=25, seconds=59), False)
check("B3 flap-fail: +25:59 is streak 2, mark and issue stay",
      b3e._pending["B305"].sent_streak == 2
      and b3e._pending["B305"].delivery_failed is True
      and b3e_calls.count("cancelled:B305") == 3
      and b3e._repair_issue_present() is True)
_b3e_tick(b3e_t0 + timedelta(minutes=39, seconds=59), False)
check("B3 flap-fail: +39:59 is still streak 2 — next due +40:59",
      b3e._pending["B305"].sent_streak == 2
      and b3e_calls.count("cancelled:B305") == 3)
# Non-vacuous "not before" (burst-6 delta critic Finding 4): as in
# B3c / B3d above — assert the deleted count is unchanged at the
# tick before the clear tick.
check("B3 flap-fail: issue still present at +39:59, not deleted before +40:59",
      ir_registry.deleted.count(
          ("satellite_timers", "undelivered_timer_events"))
      == b3e_deleted
      and b3e._repair_issue_present() is True)
_b3e_tick(b3e_t0 + timedelta(minutes=40, seconds=59), False)
check("B3 flap-fail: +40:59 is streak 3 and the mark clears",
      b3e._pending["B305"].sent_streak == 3
      and b3e._pending["B305"].delivery_failed is False
      and b3e_calls.count("cancelled:B305") == 4)
check("B3 flap-fail: issue deletes at +40:59, not before",
      ir_registry.deleted.count(
          ("satellite_timers", "undelivered_timer_events"))
      == b3e_deleted + 1
      and b3e._repair_issue_present() is False)
check("B3 flap-fail: docstring states the early-ladder qualification",
      "earlier in the ladder it clears sooner"
      in bridge.SatelliteTimerBridge._evaluate_repair_issues.__doc__)
check("B3 flap-fail: docstring states the range holds at the 15-minute step",
      "15-minute step; earlier in the ladder it clears sooner, and"
      in bridge.SatelliteTimerBridge._evaluate_repair_issues.__doc__)
# Burst-10 F1: the clean-case sentence stated the count>=3 value
# (just under 46 min) as the clean-case value — the same unstated
# ladder-position assumption as burst-9's F1. The docstring now
# qualifies it the same way, and pins the measured off-phase
# clear times per attempt_count (re-measured this burst with the
# B3b harness: count 1 +21:59, count 2 +35:59, count 3 +45:59).
check("B3 clean-case: docstring qualifies just-under-46-min as the 15-minute-step value",
      "takes just under 46 min once the"
      in bridge.SatelliteTimerBridge._evaluate_repair_issues.__doc__)
# Burst-11 delta critic finding (test-only): the pin above stops
# before "15-minute step" — mutating "15-minute"->"15minute" in the
# clean-case sentence only tripped zero pins, and no check spanned
# "once attempt_count is 3 or more". Same class as burst-9 F2: the
# pin label overclaimed its span. Each phrase is pinned within its
# own single docstring line (cross-line spans fail, burst-9).
check("B3 clean-case: docstring qualifies just-under-46-min as the 15-minute-step value (15-minute step line)",
      "backoff ladder is at the 15-minute step"
      in bridge.SatelliteTimerBridge._evaluate_repair_issues.__doc__)
check("B3 clean-case: docstring states the 15-minute-step value holds once attempt_count is 3 or more",
      "once attempt_count is 3 or more"
      in bridge.SatelliteTimerBridge._evaluate_repair_issues.__doc__)
# Burst-12 (union of burst-11 verifier h1-h5 and delta critic G1-G10):
# the same class again — more load-bearing phrases in the clean-case
# passage (bridge.py lines 2898-2916) tripped zero pins when mutated.
# Each phrase is pinned within its own single docstring line
# (cross-line spans fail, burst-9); each label states only what its
# span actually says. G3's bare phrase "earlier in the ladder it"
# occurs twice (clean case L2901, flap-fail L2952), so it is scoped
# with its line's "Finding 1);" prefix to the clean-case occurrence.
# The G5/h2 span pins all three aligned times at once; the G7/h5
# span pins all three off-phase landing times at once.
check("B3 clean-case: docstring defines the clean case as no skip, no failed attempt",
      "no skip, no failed attempt"
      in bridge.SatelliteTimerBridge._evaluate_repair_issues.__doc__)
check("B3 clean-case: docstring states clearing after the",
      "clearing after the"
      in bridge.SatelliteTimerBridge._evaluate_repair_issues.__doc__)
check("B3 clean-case: docstring states earlier in the ladder it",
      "Finding 1); earlier in the ladder it"
      in bridge.SatelliteTimerBridge._evaluate_repair_issues.__doc__)
# G8: the ladder values are ALSO pinned behaviourally via
# _CANCELLED_BACKOFF, so this pin is docstring-text drift coverage
# only — it guards the docstring wording, not the behaviour.
check("B3 clean-case: docstring states the backoff ladder (60 s, 5 min, 15 min)",
      "(60 s, 5 min, 15 min)"
      in bridge.SatelliteTimerBridge._evaluate_repair_issues.__doc__)
check("B3 clean-case: docstring states three consecutive \"sent\" attempts",
      "three consecutive \"sent\" attempts"
      in bridge.SatelliteTimerBridge._evaluate_repair_issues.__doc__)
check("B3 clean-case: docstring spaces the sent attempts at +15, +30 and +45 min",
      "+15, +30 and +45 min"
      in bridge.SatelliteTimerBridge._evaluate_repair_issues.__doc__)
check("B3 clean-case: docstring states attempts land on 60 s trigger-4 ticks",
      "land on 60 s trigger-4 ticks"
      in bridge.SatelliteTimerBridge._evaluate_repair_issues.__doc__)
check("B3 clean-case: docstring states the first attempt is preserved, not accumulated",
      "the first attempt is preserved, not accumulated"
      in bridge.SatelliteTimerBridge._evaluate_repair_issues.__doc__)
check("B3 clean-case: docstring states off-phase attempts land at up to +15:59, +30:59 and +45:59",
      "up to +15:59, +30:59 and +45:59"
      in bridge.SatelliteTimerBridge._evaluate_repair_issues.__doc__)
check("B3 clean-case: docstring states that bound holds only in the clean case",
      "That bound holds only in the clean case"
      in bridge.SatelliteTimerBridge._evaluate_repair_issues.__doc__)
check("B3 clean-case: docstring states no unavailable-skip at a due tick and no failed attempt in",
      "unavailable-skip at a due tick and no failed attempt in"
      in bridge.SatelliteTimerBridge._evaluate_repair_issues.__doc__)
check("B3 clean-case: docstring states earlier in the ladder it takes less",
      "takes less: with the last attempt just before recovery"
      in bridge.SatelliteTimerBridge._evaluate_repair_issues.__doc__)
check("B3 clean-case: docstring states the attempt_count 1 clear time",
      "1 clears at +21:59 (sents +1:59, +6:59,"
      in bridge.SatelliteTimerBridge._evaluate_repair_issues.__doc__)
check("B3 clean-case: docstring states the attempt_count 2 clear time",
      "2 clears at +35:59 (sents +5:59, +20:59,"
      in bridge.SatelliteTimerBridge._evaluate_repair_issues.__doc__)
check("B3 clean-case: docstring states the attempt_count 3 clear time",
      "3 clears at +45:59."
      in bridge.SatelliteTimerBridge._evaluate_repair_issues.__doc__)
b3e.stop()

# ------------------------------------------------- burst-6 delta critic Finding 3: pin the docstring numbers
# The docstring states five measured clear times that no test pinned
# (only B3c +47:59 and B3d +90:59 had docstring-number checks): S1
# one skip +46:59, S3 three skips +48:59 (consecutive and
# non-consecutive), S6 restart-with-failing-drain +65:59, S7
# re-registration failing +80:59, S7c loud recovery at +36:00
# +66:59. Each scenario below pins its number behaviourally (the
# issue deletes at exactly that tick, with the non-vacuous
# pre-clear assertion from Finding 4) and with a docstring-contains
# check, so a wrong number in the docstring fails the suite.
# S6/S7/S7c drive trigger-1 drains, which stamp wall-clock time, so
# they patch bridge.datetime to a sim class for the drain/start
# call only (the harness pattern) and restore it immediately after;
# the test module's own datetime stays real throughout.
class _SimDatetime(datetime):
    _sim = None

    @classmethod
    def now(cls, tz=None):
        return cls._sim


# S1: one unavailable-skip at the first due tick (+15:59) clears at
# +46:59 (46.98 min): the first attempt lands at +16:59, then
# +31:59, then +46:59.
s1_calls = []
s1_mgr = FakeTimerManager()
s1_hass = FakeHassWithTasks(s1_mgr)
s1_mgr.handlers["dev1"] = _make_handler(s1_calls)
s1 = bridge.SatelliteTimerBridge(s1_hass, ["dev1"])
_LOOP.run_until_complete(s1.async_load_persisted(FakeStore()))
_LOOP.run_until_complete(s1.start())
s1._device_entities = {"dev1": "assist_satellite.kitchen_hub"}
s1_states = _B3cStates()
s1_hass.states = s1_states
s1_t0 = datetime.now(timezone.utc)
s1._enqueue(bridge.PendingRecord(
    timer_id="S101", device_id="dev1", event_type="cancelled",
    enqueued_at=s1_t0 - timedelta(minutes=6), name="Pasta",
    total_seconds=600, language="en", is_finished=True,
    delivery_failed=True, attempt_count=3,
    last_attempt_at=s1_t0))
s1_created = ir_registry.created.count(
    ("satellite_timers", "undelivered_timer_events"))
s1_deleted = ir_registry.deleted.count(
    ("satellite_timers", "undelivered_timer_events"))
s1._evaluate_repair_issues(s1_t0)
check("S1 one skip: old failed CANCELLED opens the issue",
      ir_registry.created.count(
          ("satellite_timers", "undelivered_timer_events"))
      == s1_created + 1
      and s1._repair_issue_present() is True)


def _s1_tick(when, unavailable):
    s1_states.map["assist_satellite.kitchen_hub"] = _FakeState(
        "unavailable" if unavailable else "idle")
    _LOOP.run_until_complete(s1._trigger4_tick(when))


_s1_tick(s1_t0 + timedelta(minutes=14, seconds=59), False)
check("S1 one skip: tick just before the +15 min due time is no attempt",
      s1._pending["S101"].sent_streak == 0
      and s1_calls.count("cancelled:S101") == 0)
_s1_tick(s1_t0 + timedelta(minutes=15, seconds=59), True)
check("S1 one skip: skip at +15:59 is no attempt, clock untouched",
      s1._pending["S101"].sent_streak == 0
      and s1._pending["S101"].last_attempt_at == s1_t0
      and s1_calls.count("cancelled:S101") == 0
      and s1._repair_issue_present() is True)
_s1_tick(s1_t0 + timedelta(minutes=16, seconds=59), False)
check("S1 one skip: +16:59 is streak 1, mark and issue stay",
      s1._pending["S101"].sent_streak == 1
      and s1._pending["S101"].delivery_failed is True
      and s1_calls.count("cancelled:S101") == 1
      and s1._repair_issue_present() is True)
_s1_tick(s1_t0 + timedelta(minutes=30, seconds=59), False)
check("S1 one skip: +30:59 is still streak 1 — next due +31:59",
      s1._pending["S101"].sent_streak == 1
      and s1_calls.count("cancelled:S101") == 1)
_s1_tick(s1_t0 + timedelta(minutes=31, seconds=59), False)
check("S1 one skip: +31:59 is streak 2, mark and issue stay",
      s1._pending["S101"].sent_streak == 2
      and s1._pending["S101"].delivery_failed is True
      and s1_calls.count("cancelled:S101") == 2
      and s1._repair_issue_present() is True)
_s1_tick(s1_t0 + timedelta(minutes=45, seconds=59), False)
check("S1 one skip: +45:59 is still streak 2 — next due +46:59",
      s1._pending["S101"].sent_streak == 2
      and s1_calls.count("cancelled:S101") == 2)
check("S1 one skip: issue still present at +45:59, not deleted before +46:59",
      ir_registry.deleted.count(
          ("satellite_timers", "undelivered_timer_events"))
      == s1_deleted
      and s1._repair_issue_present() is True)
_s1_tick(s1_t0 + timedelta(minutes=46, seconds=59), False)
check("S1 one skip: +46:59 is streak 3 and the mark clears",
      s1._pending["S101"].sent_streak == 3
      and s1._pending["S101"].delivery_failed is False
      and s1_calls.count("cancelled:S101") == 3)
check("S1 one skip: issue deletes at +46:59, not before",
      ir_registry.deleted.count(
          ("satellite_timers", "undelivered_timer_events"))
      == s1_deleted + 1
      and s1._repair_issue_present() is False)
check("S1 one skip: docstring states the one-skip clear time",
      "skip at +46:59 (46.98 min)"
      in bridge.SatelliteTimerBridge._evaluate_repair_issues.__doc__)
s1.stop()

# S3 consecutive: three skips on the first three due ticks (+15:59,
# +16:59, +17:59) clear at +48:59 (48.98 min): attempts land at
# +18:59, +33:59, +48:59.
s3_calls = []
s3_mgr = FakeTimerManager()
s3_hass = FakeHassWithTasks(s3_mgr)
s3_mgr.handlers["dev1"] = _make_handler(s3_calls)
s3 = bridge.SatelliteTimerBridge(s3_hass, ["dev1"])
_LOOP.run_until_complete(s3.async_load_persisted(FakeStore()))
_LOOP.run_until_complete(s3.start())
s3._device_entities = {"dev1": "assist_satellite.kitchen_hub"}
s3_states = _B3cStates()
s3_hass.states = s3_states
s3_t0 = datetime.now(timezone.utc)
s3._enqueue(bridge.PendingRecord(
    timer_id="S301", device_id="dev1", event_type="cancelled",
    enqueued_at=s3_t0 - timedelta(minutes=6), name="Pasta",
    total_seconds=600, language="en", is_finished=True,
    delivery_failed=True, attempt_count=3,
    last_attempt_at=s3_t0))
s3_created = ir_registry.created.count(
    ("satellite_timers", "undelivered_timer_events"))
s3_deleted = ir_registry.deleted.count(
    ("satellite_timers", "undelivered_timer_events"))
s3._evaluate_repair_issues(s3_t0)
check("S3 three skips: old failed CANCELLED opens the issue",
      ir_registry.created.count(
          ("satellite_timers", "undelivered_timer_events"))
      == s3_created + 1
      and s3._repair_issue_present() is True)


def _s3_tick(when, unavailable):
    s3_states.map["assist_satellite.kitchen_hub"] = _FakeState(
        "unavailable" if unavailable else "idle")
    _LOOP.run_until_complete(s3._trigger4_tick(when))


_s3_tick(s3_t0 + timedelta(minutes=14, seconds=59), False)
check("S3 three skips: tick just before the +15 min due time is no attempt",
      s3._pending["S301"].sent_streak == 0
      and s3_calls.count("cancelled:S301") == 0)
_s3_tick(s3_t0 + timedelta(minutes=15, seconds=59), True)
check("S3 three skips: first skip at +15:59 is no attempt",
      s3._pending["S301"].sent_streak == 0
      and s3._pending["S301"].last_attempt_at == s3_t0
      and s3_calls.count("cancelled:S301") == 0)
_s3_tick(s3_t0 + timedelta(minutes=16, seconds=59), True)
check("S3 three skips: second skip at +16:59 is no attempt",
      s3._pending["S301"].sent_streak == 0
      and s3._pending["S301"].last_attempt_at == s3_t0
      and s3_calls.count("cancelled:S301") == 0)
_s3_tick(s3_t0 + timedelta(minutes=17, seconds=59), True)
check("S3 three skips: third skip at +17:59 is no attempt",
      s3._pending["S301"].sent_streak == 0
      and s3._pending["S301"].last_attempt_at == s3_t0
      and s3_calls.count("cancelled:S301") == 0
      and s3._repair_issue_present() is True)
_s3_tick(s3_t0 + timedelta(minutes=18, seconds=59), False)
check("S3 three skips: +18:59 is streak 1, mark and issue stay",
      s3._pending["S301"].sent_streak == 1
      and s3._pending["S301"].delivery_failed is True
      and s3_calls.count("cancelled:S301") == 1
      and s3._repair_issue_present() is True)
_s3_tick(s3_t0 + timedelta(minutes=32, seconds=59), False)
check("S3 three skips: +32:59 is still streak 1 — next due +33:59",
      s3._pending["S301"].sent_streak == 1
      and s3_calls.count("cancelled:S301") == 1)
_s3_tick(s3_t0 + timedelta(minutes=33, seconds=59), False)
check("S3 three skips: +33:59 is streak 2, mark and issue stay",
      s3._pending["S301"].sent_streak == 2
      and s3._pending["S301"].delivery_failed is True
      and s3_calls.count("cancelled:S301") == 2
      and s3._repair_issue_present() is True)
_s3_tick(s3_t0 + timedelta(minutes=47, seconds=59), False)
check("S3 three skips: +47:59 is still streak 2 — next due +48:59",
      s3._pending["S301"].sent_streak == 2
      and s3_calls.count("cancelled:S301") == 2)
check("S3 three skips: issue still present at +47:59, not deleted before +48:59",
      ir_registry.deleted.count(
          ("satellite_timers", "undelivered_timer_events"))
      == s3_deleted
      and s3._repair_issue_present() is True)
_s3_tick(s3_t0 + timedelta(minutes=48, seconds=59), False)
check("S3 three skips: +48:59 is streak 3 and the mark clears",
      s3._pending["S301"].sent_streak == 3
      and s3._pending["S301"].delivery_failed is False
      and s3_calls.count("cancelled:S301") == 3)
check("S3 three skips: issue deletes at +48:59, not before",
      ir_registry.deleted.count(
          ("satellite_timers", "undelivered_timer_events"))
      == s3_deleted + 1
      and s3._repair_issue_present() is False)
check("S3 three skips: docstring states the three-skip clear time",
      "+48:59 (48.98 min"
      in bridge.SatelliteTimerBridge._evaluate_repair_issues.__doc__)
s3.stop()

# S3 non-consecutive: the same three skips spread out (+15:59,
# +31:59, +47:59) also clear at +48:59 — position does not matter,
# only the count: attempts land at +16:59, +32:59, +48:59.
s3n_calls = []
s3n_mgr = FakeTimerManager()
s3n_hass = FakeHassWithTasks(s3n_mgr)
s3n_mgr.handlers["dev1"] = _make_handler(s3n_calls)
s3n = bridge.SatelliteTimerBridge(s3n_hass, ["dev1"])
_LOOP.run_until_complete(s3n.async_load_persisted(FakeStore()))
_LOOP.run_until_complete(s3n.start())
s3n._device_entities = {"dev1": "assist_satellite.kitchen_hub"}
s3n_states = _B3cStates()
s3n_hass.states = s3n_states
s3n_t0 = datetime.now(timezone.utc)
s3n._enqueue(bridge.PendingRecord(
    timer_id="S302", device_id="dev1", event_type="cancelled",
    enqueued_at=s3n_t0 - timedelta(minutes=6), name="Pasta",
    total_seconds=600, language="en", is_finished=True,
    delivery_failed=True, attempt_count=3,
    last_attempt_at=s3n_t0))
s3n_created = ir_registry.created.count(
    ("satellite_timers", "undelivered_timer_events"))
s3n_deleted = ir_registry.deleted.count(
    ("satellite_timers", "undelivered_timer_events"))
s3n._evaluate_repair_issues(s3n_t0)
check("S3 non-consecutive: old failed CANCELLED opens the issue",
      ir_registry.created.count(
          ("satellite_timers", "undelivered_timer_events"))
      == s3n_created + 1
      and s3n._repair_issue_present() is True)


def _s3n_tick(when, unavailable):
    s3n_states.map["assist_satellite.kitchen_hub"] = _FakeState(
        "unavailable" if unavailable else "idle")
    _LOOP.run_until_complete(s3n._trigger4_tick(when))


_s3n_tick(s3n_t0 + timedelta(minutes=14, seconds=59), False)
check("S3 non-consecutive: tick just before the +15 min due time is no attempt",
      s3n._pending["S302"].sent_streak == 0
      and s3n_calls.count("cancelled:S302") == 0)
_s3n_tick(s3n_t0 + timedelta(minutes=15, seconds=59), True)
check("S3 non-consecutive: skip at +15:59 is no attempt",
      s3n._pending["S302"].sent_streak == 0
      and s3n._pending["S302"].last_attempt_at == s3n_t0
      and s3n_calls.count("cancelled:S302") == 0)
_s3n_tick(s3n_t0 + timedelta(minutes=16, seconds=59), False)
check("S3 non-consecutive: +16:59 is streak 1, mark and issue stay",
      s3n._pending["S302"].sent_streak == 1
      and s3n._pending["S302"].delivery_failed is True
      and s3n_calls.count("cancelled:S302") == 1
      and s3n._repair_issue_present() is True)
_s3n_tick(s3n_t0 + timedelta(minutes=30, seconds=59), False)
check("S3 non-consecutive: +30:59 is still streak 1 — next due +31:59",
      s3n._pending["S302"].sent_streak == 1
      and s3n_calls.count("cancelled:S302") == 1)
_s3n_tick(s3n_t0 + timedelta(minutes=31, seconds=59), True)
check("S3 non-consecutive: skip at +31:59 is no attempt, streak kept",
      s3n._pending["S302"].sent_streak == 1
      and s3n._pending["S302"].last_attempt_at
      == s3n_t0 + timedelta(minutes=16, seconds=59)
      and s3n_calls.count("cancelled:S302") == 1
      and s3n._repair_issue_present() is True)
_s3n_tick(s3n_t0 + timedelta(minutes=32, seconds=59), False)
check("S3 non-consecutive: +32:59 is streak 2, mark and issue stay",
      s3n._pending["S302"].sent_streak == 2
      and s3n._pending["S302"].delivery_failed is True
      and s3n_calls.count("cancelled:S302") == 2
      and s3n._repair_issue_present() is True)
_s3n_tick(s3n_t0 + timedelta(minutes=46, seconds=59), False)
check("S3 non-consecutive: +46:59 is still streak 2 — next due +47:59",
      s3n._pending["S302"].sent_streak == 2
      and s3n_calls.count("cancelled:S302") == 2)
_s3n_tick(s3n_t0 + timedelta(minutes=47, seconds=59), True)
check("S3 non-consecutive: skip at +47:59 is no attempt, streak kept",
      s3n._pending["S302"].sent_streak == 2
      and s3n._pending["S302"].last_attempt_at
      == s3n_t0 + timedelta(minutes=32, seconds=59)
      and s3n_calls.count("cancelled:S302") == 2
      and s3n._repair_issue_present() is True)
check("S3 non-consecutive: issue still present at +47:59, not deleted before +48:59",
      ir_registry.deleted.count(
          ("satellite_timers", "undelivered_timer_events"))
      == s3n_deleted
      and s3n._repair_issue_present() is True)
_s3n_tick(s3n_t0 + timedelta(minutes=48, seconds=59), False)
check("S3 non-consecutive: +48:59 is streak 3 and the mark clears",
      s3n._pending["S302"].sent_streak == 3
      and s3n._pending["S302"].delivery_failed is False
      and s3n_calls.count("cancelled:S302") == 3)
check("S3 non-consecutive: issue deletes at +48:59, not before",
      ir_registry.deleted.count(
          ("satellite_timers", "undelivered_timer_events"))
      == s3n_deleted + 1
      and s3n._repair_issue_present() is False)
check("S3 non-consecutive: docstring states three skips clear identically non-consecutively",
      "+48:59 (48.98 min, consecutive and non-consecutive"
      in bridge.SatelliteTimerBridge._evaluate_repair_issues.__doc__)
s3n.stop()

# S6: a restart whose startup trigger-1 drain fails clears at +65:59
# (65.98 min). Streak 1 at +15:59 survives the reload (B1), then
# the restart at +20:00 with a raising handler fails its startup
# drain, resetting the streak to 0 and re-stamping +20:00 — so the
# three "sent"s land at +35:59, +50:59 and +65:59.
s6_calls = []
s6_mgr = FakeTimerManager()
s6_hass = FakeHassWithTasks(s6_mgr)
s6_mgr.handlers["dev1"] = _make_handler(s6_calls)
s6 = bridge.SatelliteTimerBridge(s6_hass, ["dev1"])
s6_store = FakeStore()
_LOOP.run_until_complete(s6.async_load_persisted(s6_store))
_LOOP.run_until_complete(s6.start())
s6_t0 = datetime.now(timezone.utc)
s6._enqueue(bridge.PendingRecord(
    timer_id="S601", device_id="dev1", event_type="cancelled",
    enqueued_at=s6_t0 - timedelta(minutes=6), name="Pasta",
    total_seconds=600, language="en", is_finished=True,
    delivery_failed=True, attempt_count=3,
    last_attempt_at=s6_t0))
s6_created = ir_registry.created.count(
    ("satellite_timers", "undelivered_timer_events"))
s6_deleted = ir_registry.deleted.count(
    ("satellite_timers", "undelivered_timer_events"))
s6._evaluate_repair_issues(s6_t0)
check("S6 restart failing drain: old failed CANCELLED opens the issue",
      ir_registry.created.count(
          ("satellite_timers", "undelivered_timer_events"))
      == s6_created + 1
      and s6._repair_issue_present() is True)
_LOOP.run_until_complete(
    s6._trigger4_tick(s6_t0 + timedelta(minutes=15, seconds=59)))
check("S6 restart failing drain: +15:59 is streak 1, mark and issue stay",
      s6._pending["S601"].sent_streak == 1
      and s6._pending["S601"].delivery_failed is True
      and s6_calls.count("cancelled:S601") == 1
      and s6._repair_issue_present() is True)
s6_stored = {p["timer_id"]: p for p in s6_store.data["pending_events"]}
check("S6 restart failing drain: store after the tick is (failed, count=4, streak=1)",
      s6_stored["S601"]["delivery_failed"] is True
      and s6_stored["S601"]["attempt_count"] == 4
      and s6_stored["S601"]["sent_streak"] == 1)
s6.stop()
_SimDatetime._sim = s6_t0 + timedelta(minutes=20)
bridge.datetime = _SimDatetime
try:
    s6_mgr2 = FakeTimerManager()
    s6_hass2 = FakeHassWithTasks(s6_mgr2)
    s6_calls2 = []
    s6_mgr2.handlers["dev1"] = _b3d_raising_handler(s6_calls2)
    s6b = bridge.SatelliteTimerBridge(s6_hass2, ["dev1"])
    _LOOP.run_until_complete(s6b.async_load_persisted(s6_store))
    _LOOP.run_until_complete(s6b.start())
finally:
    bridge.datetime = datetime
check("S6 restart failing drain: failing startup drain at +20:00 resets the streak to 0",
      s6b._pending["S601"].sent_streak == 0
      and s6b._pending["S601"].delivery_failed is True
      and s6b._pending["S601"].last_attempt_at
      == s6_t0 + timedelta(minutes=20)
      and s6b._pending["S601"].attempt_count == 5
      and s6_calls2.count("cancelled:S601") == 1
      and s6b._repair_issue_present() is True)
dict.__setitem__(s6_mgr2.handlers, "dev1", _make_handler(s6_calls2))
_LOOP.run_until_complete(
    s6b._trigger4_tick(s6_t0 + timedelta(minutes=20, seconds=59)))
check("S6 restart failing drain: +20:59 is still streak 0 — next due +35:59",
      s6b._pending["S601"].sent_streak == 0
      and s6_calls2.count("cancelled:S601") == 1)
_LOOP.run_until_complete(
    s6b._trigger4_tick(s6_t0 + timedelta(minutes=35, seconds=59)))
check("S6 restart failing drain: +35:59 is streak 1, mark and issue stay",
      s6b._pending["S601"].sent_streak == 1
      and s6b._pending["S601"].delivery_failed is True
      and s6_calls2.count("cancelled:S601") == 2
      and s6b._repair_issue_present() is True)
_LOOP.run_until_complete(
    s6b._trigger4_tick(s6_t0 + timedelta(minutes=50, seconds=59)))
check("S6 restart failing drain: +50:59 is streak 2, mark and issue stay",
      s6b._pending["S601"].sent_streak == 2
      and s6b._pending["S601"].delivery_failed is True
      and s6_calls2.count("cancelled:S601") == 3
      and s6b._repair_issue_present() is True)
_LOOP.run_until_complete(
    s6b._trigger4_tick(s6_t0 + timedelta(minutes=64, seconds=59)))
check("S6 restart failing drain: +64:59 is still streak 2 — next due +65:59",
      s6b._pending["S601"].sent_streak == 2
      and s6_calls2.count("cancelled:S601") == 3)
check("S6 restart failing drain: issue still present at +64:59, not deleted before +65:59",
      ir_registry.deleted.count(
          ("satellite_timers", "undelivered_timer_events"))
      == s6_deleted
      and s6b._repair_issue_present() is True)
_LOOP.run_until_complete(
    s6b._trigger4_tick(s6_t0 + timedelta(minutes=65, seconds=59)))
check("S6 restart failing drain: +65:59 is streak 3 and the mark clears",
      s6b._pending["S601"].sent_streak == 3
      and s6b._pending["S601"].delivery_failed is False
      and s6_calls2.count("cancelled:S601") == 4)
check("S6 restart failing drain: issue deletes at +65:59, not before",
      ir_registry.deleted.count(
          ("satellite_timers", "undelivered_timer_events"))
      == s6_deleted + 1
      and s6b._repair_issue_present() is False)
check("S6 restart failing drain: docstring states the restart-with-failing-drain clear time",
      "+65:59 (65.98 min)"
      in bridge.SatelliteTimerBridge._evaluate_repair_issues.__doc__)
s6b.stop()

# S7: a handler re-registration whose trigger-1 drain fails clears at
# +80:59 (80.98 min). Streak 2 at +30:59, then the re-registration
# at +35:00 with a raising handler fails its drain, resetting the
# streak to 0 and re-stamping +35:00 — so the three "sent"s land at
# +50:59, +65:59 and +80:59. The handler is swapped back silently
# (dict.__setitem__) so no second drain fires on the swap itself.
s7_calls = []
s7_mgr = FakeTimerManager()
s7_hass = FakeHassWithTasks(s7_mgr)
s7_mgr.handlers["dev1"] = _make_handler(s7_calls)
s7 = bridge.SatelliteTimerBridge(s7_hass, ["dev1"])
_LOOP.run_until_complete(s7.async_load_persisted(FakeStore()))
_LOOP.run_until_complete(s7.start())
s7_t0 = datetime.now(timezone.utc)
s7._enqueue(bridge.PendingRecord(
    timer_id="S701", device_id="dev1", event_type="cancelled",
    enqueued_at=s7_t0 - timedelta(minutes=6), name="Pasta",
    total_seconds=600, language="en", is_finished=True,
    delivery_failed=True, attempt_count=3,
    last_attempt_at=s7_t0))
s7_created = ir_registry.created.count(
    ("satellite_timers", "undelivered_timer_events"))
s7_deleted = ir_registry.deleted.count(
    ("satellite_timers", "undelivered_timer_events"))
s7._evaluate_repair_issues(s7_t0)
check("S7 re-registration failing: old failed CANCELLED opens the issue",
      ir_registry.created.count(
          ("satellite_timers", "undelivered_timer_events"))
      == s7_created + 1
      and s7._repair_issue_present() is True)
_LOOP.run_until_complete(
    s7._trigger4_tick(s7_t0 + timedelta(minutes=15, seconds=59)))
check("S7 re-registration failing: +15:59 is streak 1, mark and issue stay",
      s7._pending["S701"].sent_streak == 1
      and s7._pending["S701"].delivery_failed is True
      and s7._repair_issue_present() is True)
_LOOP.run_until_complete(
    s7._trigger4_tick(s7_t0 + timedelta(minutes=30, seconds=59)))
check("S7 re-registration failing: +30:59 is streak 2, mark and issue stay",
      s7._pending["S701"].sent_streak == 2
      and s7._pending["S701"].delivery_failed is True
      and s7._repair_issue_present() is True)
_SimDatetime._sim = s7_t0 + timedelta(minutes=35)
bridge.datetime = _SimDatetime
try:
    s7_mgr.handlers["dev1"] = _b3d_raising_handler(s7_calls)
finally:
    bridge.datetime = datetime
check("S7 re-registration failing: failed drain at +35:00 resets the streak to 0",
      s7._pending["S701"].sent_streak == 0
      and s7._pending["S701"].delivery_failed is True
      and s7._pending["S701"].last_attempt_at
      == s7_t0 + timedelta(minutes=35)
      and s7._pending["S701"].attempt_count == 6
      and s7_calls.count("cancelled:S701") == 3
      and s7._repair_issue_present() is True)
dict.__setitem__(s7_mgr.handlers, "dev1", _make_handler(s7_calls))
_LOOP.run_until_complete(
    s7._trigger4_tick(s7_t0 + timedelta(minutes=35, seconds=59)))
check("S7 re-registration failing: +35:59 is still streak 0 — next due +50:59",
      s7._pending["S701"].sent_streak == 0
      and s7_calls.count("cancelled:S701") == 3)
_LOOP.run_until_complete(
    s7._trigger4_tick(s7_t0 + timedelta(minutes=50, seconds=59)))
check("S7 re-registration failing: +50:59 is streak 1, mark and issue stay",
      s7._pending["S701"].sent_streak == 1
      and s7._pending["S701"].delivery_failed is True
      and s7_calls.count("cancelled:S701") == 4
      and s7._repair_issue_present() is True)
_LOOP.run_until_complete(
    s7._trigger4_tick(s7_t0 + timedelta(minutes=65, seconds=59)))
check("S7 re-registration failing: +65:59 is streak 2, mark and issue stay",
      s7._pending["S701"].sent_streak == 2
      and s7._pending["S701"].delivery_failed is True
      and s7_calls.count("cancelled:S701") == 5
      and s7._repair_issue_present() is True)
_LOOP.run_until_complete(
    s7._trigger4_tick(s7_t0 + timedelta(minutes=79, seconds=59)))
check("S7 re-registration failing: +79:59 is still streak 2 — next due +80:59",
      s7._pending["S701"].sent_streak == 2
      and s7_calls.count("cancelled:S701") == 5)
check("S7 re-registration failing: issue still present at +79:59, not deleted before +80:59",
      ir_registry.deleted.count(
          ("satellite_timers", "undelivered_timer_events"))
      == s7_deleted
      and s7._repair_issue_present() is True)
_LOOP.run_until_complete(
    s7._trigger4_tick(s7_t0 + timedelta(minutes=80, seconds=59)))
check("S7 re-registration failing: +80:59 is streak 3 and the mark clears",
      s7._pending["S701"].sent_streak == 3
      and s7._pending["S701"].delivery_failed is False
      and s7_calls.count("cancelled:S701") == 6)
check("S7 re-registration failing: issue deletes at +80:59, not before",
      ir_registry.deleted.count(
          ("satellite_timers", "undelivered_timer_events"))
      == s7_deleted + 1
      and s7._repair_issue_present() is False)
check("S7 re-registration failing: docstring states the re-registration clear time",
      "clears at +80:59"
      in bridge.SatelliteTimerBridge._evaluate_repair_issues.__doc__)
s7.stop()

# S7c: the same failed re-registration drain at +35:00, but a
# genuine loud recovery — a good re-registration at +36:00 whose
# drain succeeds — still clears at +66:59 (66.98 min), not +45:59:
# the +36:00 drain is only streak 1, so the remaining "sent"s land
# at +51:59 and +66:59.
s7c_calls = []
s7c_mgr = FakeTimerManager()
s7c_hass = FakeHassWithTasks(s7c_mgr)
s7c_mgr.handlers["dev1"] = _make_handler(s7c_calls)
s7c = bridge.SatelliteTimerBridge(s7c_hass, ["dev1"])
_LOOP.run_until_complete(s7c.async_load_persisted(FakeStore()))
_LOOP.run_until_complete(s7c.start())
s7c_t0 = datetime.now(timezone.utc)
s7c._enqueue(bridge.PendingRecord(
    timer_id="S702", device_id="dev1", event_type="cancelled",
    enqueued_at=s7c_t0 - timedelta(minutes=6), name="Pasta",
    total_seconds=600, language="en", is_finished=True,
    delivery_failed=True, attempt_count=3,
    last_attempt_at=s7c_t0))
s7c_created = ir_registry.created.count(
    ("satellite_timers", "undelivered_timer_events"))
s7c_deleted = ir_registry.deleted.count(
    ("satellite_timers", "undelivered_timer_events"))
s7c._evaluate_repair_issues(s7c_t0)
check("S7c loud recovery: old failed CANCELLED opens the issue",
      ir_registry.created.count(
          ("satellite_timers", "undelivered_timer_events"))
      == s7c_created + 1
      and s7c._repair_issue_present() is True)
_LOOP.run_until_complete(
    s7c._trigger4_tick(s7c_t0 + timedelta(minutes=15, seconds=59)))
check("S7c loud recovery: +15:59 is streak 1, mark and issue stay",
      s7c._pending["S702"].sent_streak == 1
      and s7c._pending["S702"].delivery_failed is True
      and s7c._repair_issue_present() is True)
_LOOP.run_until_complete(
    s7c._trigger4_tick(s7c_t0 + timedelta(minutes=30, seconds=59)))
check("S7c loud recovery: +30:59 is streak 2, mark and issue stay",
      s7c._pending["S702"].sent_streak == 2
      and s7c._pending["S702"].delivery_failed is True
      and s7c._repair_issue_present() is True)
_SimDatetime._sim = s7c_t0 + timedelta(minutes=35)
bridge.datetime = _SimDatetime
try:
    s7c_mgr.handlers["dev1"] = _b3d_raising_handler(s7c_calls)
    check("S7c loud recovery: failed drain at +35:00 resets the streak to 0",
          s7c._pending["S702"].sent_streak == 0
          and s7c._pending["S702"].delivery_failed is True
          and s7c._pending["S702"].last_attempt_at
          == s7c_t0 + timedelta(minutes=35)
          and s7c._pending["S702"].attempt_count == 6
          and s7c_calls.count("cancelled:S702") == 3
          and s7c._repair_issue_present() is True)
    _SimDatetime._sim = s7c_t0 + timedelta(minutes=36)
    s7c_mgr.handlers["dev1"] = _make_handler(s7c_calls)
finally:
    bridge.datetime = datetime
check("S7c loud recovery: good re-registration at +36:00 sends once, streak 1",
      s7c._pending["S702"].sent_streak == 1
      and s7c._pending["S702"].delivery_failed is True
      and s7c._pending["S702"].last_attempt_at
      == s7c_t0 + timedelta(minutes=36)
      and s7c._pending["S702"].attempt_count == 7
      and s7c_calls.count("cancelled:S702") == 4
      and s7c._repair_issue_present() is True)
_LOOP.run_until_complete(
    s7c._trigger4_tick(s7c_t0 + timedelta(minutes=36, seconds=59)))
check("S7c loud recovery: +36:59 is still streak 1 — next due +51:59",
      s7c._pending["S702"].sent_streak == 1
      and s7c_calls.count("cancelled:S702") == 4)
_LOOP.run_until_complete(
    s7c._trigger4_tick(s7c_t0 + timedelta(minutes=51, seconds=59)))
check("S7c loud recovery: +51:59 is streak 2, mark and issue stay",
      s7c._pending["S702"].sent_streak == 2
      and s7c._pending["S702"].delivery_failed is True
      and s7c_calls.count("cancelled:S702") == 5
      and s7c._repair_issue_present() is True)
_LOOP.run_until_complete(
    s7c._trigger4_tick(s7c_t0 + timedelta(minutes=65, seconds=59)))
check("S7c loud recovery: +65:59 is still streak 2 — next due +66:59",
      s7c._pending["S702"].sent_streak == 2
      and s7c_calls.count("cancelled:S702") == 5)
check("S7c loud recovery: issue still present at +65:59, not deleted before +66:59",
      ir_registry.deleted.count(
          ("satellite_timers", "undelivered_timer_events"))
      == s7c_deleted
      and s7c._repair_issue_present() is True)
_LOOP.run_until_complete(
    s7c._trigger4_tick(s7c_t0 + timedelta(minutes=66, seconds=59)))
check("S7c loud recovery: +66:59 is streak 3 and the mark clears",
      s7c._pending["S702"].sent_streak == 3
      and s7c._pending["S702"].delivery_failed is False
      and s7c_calls.count("cancelled:S702") == 6)
check("S7c loud recovery: issue deletes at +66:59, not before",
      ir_registry.deleted.count(
          ("satellite_timers", "undelivered_timer_events"))
      == s7c_deleted + 1
      and s7c._repair_issue_present() is False)
check("S7c loud recovery: docstring states the loud-recovery clear time",
      "66.98 min even with genuine loud recovery at"
      in bridge.SatelliteTimerBridge._evaluate_repair_issues.__doc__)
s7c.stop()

# ------------------------------------------------- round-2 B5: pending snapshots carry started_at
# Two finished timers with the same name, started at different
# times: after dismissing both, the pending CANCELLED snapshots
# still carry distinct started_at values, so the voice re-dismiss
# listing can tell them apart ("Pasta started 6:02 PM, ...").
b5_calls = []
b5_mgr = FakeTimerManager()
b5_hass = FakeHassWithTasks(b5_mgr)
b5_mgr.handlers["dev1"] = _make_handler(b5_calls)
b5 = bridge.SatelliteTimerBridge(b5_hass, ["dev1"])
b5_store = FakeStore()
_LOOP.run_until_complete(b5.async_load_persisted(b5_store))
_LOOP.run_until_complete(b5.start())
b5_early = datetime.now(timezone.utc) - timedelta(minutes=20)
b5_late = datetime.now(timezone.utc) - timedelta(minutes=5)
b5._records["B501"] = bridge.TimerRecord(
    "B501", "Pasta", "dev1", False, 600, 0, None, is_finished=True,
    finished_at=b5_early + timedelta(minutes=10), language="en",
    started_at=b5_early)
b5._records["B502"] = bridge.TimerRecord(
    "B502", "Pasta", "dev1", False, 600, 0, None, is_finished=True,
    finished_at=b5_late + timedelta(minutes=5), language="en",
    started_at=b5_late)
b5.dismiss_timer("B501")
b5.dismiss_timer("B502")
b5_snaps = b5.pending_cancelled()
check("B5: two same-name pending snapshots carry distinct started_at",
      len(b5_snaps) == 2
      and b5_snaps[0].started_at == b5_early
      and b5_snaps[1].started_at == b5_late)
_LOOP.run_until_complete(b5.persist())
b5_mgr2 = FakeTimerManager()
b5_hass2 = FakeHassWithTasks(b5_mgr2)
b5_reloaded = bridge.SatelliteTimerBridge(b5_hass2, ["dev1"])
_LOOP.run_until_complete(b5_reloaded.async_load_persisted(b5_store))
b5_snaps2 = b5_reloaded.pending_cancelled()
check("B5: started_at survives a reload",
      len(b5_snaps2) == 2
      and {s.started_at for s in b5_snaps2} == {b5_early, b5_late})
b5.stop()

# ------------------------------------------------- burst-3 critic Finding 2: _record_finished enqueues started_at
# The FINISHED enqueue was the only enqueue site that dropped
# started_at: the TimerRecord carried the observed start but the
# pending record had None. No user-visible symptom today (dismiss
# copies started_at from the held finished record;
# _snap_for_pending prefers the held record) — latent correctness
# only. Drive a timer through a tap (STARTED then FINISHED) and
# assert the pending FINISHED carries the observed start.
f2c_calls = []
f2c_mgr = FakeTimerManager()
f2c_hass = FakeHassWithTasks(f2c_mgr)
f2c_mgr.handlers["dev1"] = _make_handler(f2c_calls)
f2c = bridge.SatelliteTimerBridge(f2c_hass, ["dev1"])
_LOOP.run_until_complete(f2c.async_load_persisted(FakeStore()))
_LOOP.run_until_complete(f2c.start())
f2c_timer = FakeTimerInfo("F201", "Pasta", 600, "dev1")
f2c_mgr.timers["F201"] = f2c_timer
f2c_mgr.handlers["dev1"]("started", f2c_timer)
f2c_mgr.timers.pop("F201")
f2c_mgr.handlers["dev1"]("finished", f2c_timer)
check("F2c: tap finish records the observed start on the finished record",
      f2c._records["F201"].started_at is not None)
check("F2c: pending FINISHED carries the observed started_at",
      f2c._pending["F201"].event_type == "finished"
      and f2c._pending["F201"].started_at is not None
      and f2c._pending["F201"].started_at
      == f2c._records["F201"].started_at)
f2c.stop()

# ------------------------------------------------- round-2 B6: dismiss_timer returns its own outcome
# (plus: no bridge-global last-outcome state left to cross-talk).
b6_calls = []
b6_mgr = FakeTimerManager()
b6_hass = FakeHassWithTasks(b6_mgr)
b6_mgr.handlers["dev1"] = _make_handler(b6_calls)
b6 = bridge.SatelliteTimerBridge(b6_hass, ["dev1"])
_LOOP.run_until_complete(b6.async_load_persisted(FakeStore()))
_LOOP.run_until_complete(b6.start())
b6f = FakeTimerInfo("B601", "Pasta", 600, "dev1")
b6_mgr.timers["B601"] = b6f
b6_mgr.timers.pop("B601")
b6_mgr.handlers["dev1"]("finished", b6f)
_, b6_outcome = b6.dismiss_timer("B601")
check("B6: dismiss with handler up returns 'sent'",
      b6_outcome == "sent")
b6g = FakeTimerInfo("B602", "Rice", 300, "dev1")
b6_mgr.timers["B602"] = b6g
b6_mgr.timers.pop("B602")
b6_mgr.handlers["dev1"]("finished", b6g)
b6_mgr.handlers.pop("dev1", None)
_, b6_outcome2 = b6.dismiss_timer("B602")
check("B6: dismiss with handler down returns 'no-handler'",
      b6_outcome2 == "no-handler")
check("B6: no bridge-global last-dismiss-outcome state remains",
      not hasattr(b6, "last_dismiss_outcome")
      and not hasattr(b6, "_last_dismiss_outcome"))
b6.stop()

# ------------------------------------------------- round-2 B7: durability wording is ordering-only
check("B7: dismiss docstring states ordering, not a durability guarantee",
      "ordering, not a durability guarantee"
      in bridge.SatelliteTimerBridge.dismiss_timer.__doc__)

b_v.stop()
b_v2.stop()

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("All bridge checks passed.")
