"""Pytest fixtures for the Wave W2 pure-module suite.

The suite tests only the modules with NO Home Assistant runtime
dependency (``records.py``, ``resolution.py``, ``errors.py``,
``tm_observer.py``) — the state machine, the resolution rules, the
error ladder, and the TimerManager tap. The HA-wired modules
(``bridge.py``, ``intents.py``, ``sensor.py``, ``services.py``,
``config_flow.py``, ``__init__.py``) are covered by the standalone
integration script (``tests/bridge_integration_check.py``, run against
the venv's real HA) and by the rig legs — never by stubbing HA here.

How the stubs work: the real ``satellite_timers`` package ``__init__``
imports the full HA stack, so importing the package would drag it in.
Instead this conftest installs a FAKE package module named
``satellite_timers`` whose ``__path__`` points at the real component
directory — the import system then loads ``satellite_timers.records``
etc. from the real files, with relative imports resolving against the
fake package, and the real ``__init__.py`` never executes. The
``homeassistant.*`` modules the pure modules touch are stubbed with
the smallest possible surface (``HomeAssistantError``,
``callback``, ``TimerEventType`` / ``TimerInfo``).
"""

from __future__ import annotations

import sys
import types
from enum import StrEnum
from pathlib import Path

COMPONENTS = Path(__file__).resolve().parents[1] / "custom_components" / "satellite_timers"


def _stub(name: str, **attrs: object) -> types.ModuleType:
    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    sys.modules[name] = mod
    return mod


class HomeAssistantError(Exception):
    """Stand-in for homeassistant.exceptions.HomeAssistantError."""


def _callback(func):
    return func


class TimerEventType(StrEnum):
    """Stand-in for homeassistant.components.intent.timers.TimerEventType."""

    STARTED = "started"
    UPDATED = "updated"
    CANCELLED = "cancelled"
    FINISHED = "finished"


class TimerInfo:
    """Stand-in for homeassistant.components.intent.timers.TimerInfo.

    The tap treats the timer object as opaque, so the stub only needs
    to exist; tests pass SimpleNamespace timers where fields matter.
    """


class TimerManager:
    """Stand-in (unused by the pure modules under test)."""


class TimerNotFoundError(HomeAssistantError):
    pass


class TimersNotSupportedError(HomeAssistantError):
    pass


_stub("homeassistant")
_stub("homeassistant.exceptions", HomeAssistantError=HomeAssistantError)
_stub("homeassistant.core", callback=_callback, HomeAssistant=object)
_stub("homeassistant.components")
_stub("homeassistant.components.intent")
_stub(
    "homeassistant.components.intent.timers",
    TimerEventType=TimerEventType,
    TimerInfo=TimerInfo,
    TimerManager=TimerManager,
    TimerNotFoundError=TimerNotFoundError,
    TimersNotSupportedError=TimersNotSupportedError,
)

# The fake package: real files, no real __init__.py.
_pkg = types.ModuleType("satellite_timers")
_pkg.__path__ = [str(COMPONENTS)]  # type: ignore[attr-defined]
sys.modules["satellite_timers"] = _pkg
