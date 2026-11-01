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
from dataclasses import replace as dataclass_replace
from datetime import datetime, timedelta, timezone
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


def original_handler(event_type, timer):
    calls.append(f"original:{event_type}")


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
over_id = b.restart_timer("01PQR", minutes=5)
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

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("All bridge checks passed.")
