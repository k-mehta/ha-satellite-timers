"""The TimerManager tap.

The bridge must observe timers started OUTSIDE the bridge — voice
starts are routed through it (``intents.py``), but the TimerManager is
also started directly by the platform (the rig's ``seed_timers``
mirrors that: it calls ``tm.start_timer`` directly). The bridge does
NOT take over the satellite's own timer handler — the
``TimerManager.handlers`` dict holds exactly ONE handler per device
and replacing it would displace the satellite itself. Instead the
bridge installs a wrapping dict subclass over ``tm.handlers`` (a tap):

- ``__setitem__`` wraps the satellite's handler: the ORIGINAL handler
  is called FIRST (the satellite always gets its event, even if the
  bridge listener raises), then the bridge listener runs in a
  ``try/except`` so a listener failure can never break the satellite.
- A wrap marker prevents double-wrapping when the same device is
  re-registered.
- Whichever handler a device is (re)assigned, the tap re-wraps it —
  so a satellite that re-registers after a reconnect keeps the bridge
  observing.
- Every (re-)registration is also a LIVENESS signal
  (``on_registered``): the bridge re-issues any pending stop for
  that device at once (§03.10/AD-C1), instead of waiting for the
  30 s reconcile. It fires when the tap is installed over handlers
  that already exist, too — a handler present at setup is live, so
  a pending stop left from before the restart is re-issued at once.
  A handler being REMOVED (``pop``) is not liveness: the tap does
  not intercept it, and no callback fires.
- Uninstall restores a plain ``dict`` of the unwrapped originals.

The bridge listener only ADOPTS what the TimerManager itself reports
(STARTED with a real timer id); it never guesses an id match, and a
start the bridge itself is in the middle of is suppressed by the
bridge's own suppression set (§03.2 M2) so it is not adopted twice.

Reconcile: if ``tm.handlers`` was replaced wholesale (a new plain
dict), the bridge re-installs the tap on the 30 s reconcile sweep.
"""

from __future__ import annotations

import logging
from typing import Callable

from homeassistant.components.intent.timers import TimerEventType, TimerInfo

_LOGGER = logging.getLogger(__name__)
_WRAP_MARKER = "_bridge_timer_tap_wrapped"


class _TappedHandlers(dict):
    """A ``dict`` subclass that wraps each handler on assignment.

    ``on_event`` is the bridge listener: ``on_event(device_id, event,
    timer)``. The original handler runs first, unconditionally.
    ``on_registered`` is the liveness callback: ``on_registered
    (device_id)`` fires on every (re-)registration, including the
    install-time wrap of handlers that already exist — the bridge
    uses it to re-issue pending stops the moment a satellite is
    reachable again (§03.10/AD-C1)."""

    def __init__(self, *args, on_event: Callable, on_registered: Callable | None = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._on_event = on_event
        self._on_registered = on_registered
        for device_id, handler in list(self.items()):
            self[device_id] = handler

    def __setitem__(self, device_id, handler) -> None:  # noqa: ANN001
        if getattr(handler, _WRAP_MARKER, False):
            super().__setitem__(device_id, handler)
        else:
            original = handler

            def _wrapped(device_id=device_id, original=original):  # noqa: ANN001
                def _handler(event: TimerEventType, timer: TimerInfo) -> None:
                    original(event, timer)
                    try:
                        self._on_event(device_id, event, timer)
                    except Exception:  # noqa: BLE001 — the satellite must never break
                        _LOGGER.exception(
                            "Timer tap listener failed for device %s (%s); "
                            "the satellite handler already ran",
                            device_id,
                            event,
                        )

                return _handler

            wrapped = _wrapped()
            setattr(wrapped, _WRAP_MARKER, True)
            setattr(wrapped, "_bridge_original_handler", original)
            super().__setitem__(device_id, wrapped)
        if self._on_registered is not None:
            self._on_registered(device_id)

    def unwrap_all(self) -> dict:
        """The plain ``dict`` of original handlers (for uninstall)."""
        plain = {}
        for device_id, handler in self.items():
            plain[device_id] = getattr(
                handler, "_bridge_original_handler", handler
            )
        return plain


class TimerManagerTap:
    """Installs / maintains the tap on a ``TimerManager``."""

    def __init__(self, timer_manager, on_event: Callable, on_handler_registered: Callable | None = None) -> None:
        self._tm = timer_manager
        self._on_event = on_event
        self._on_handler_registered = on_handler_registered
        self._installed = False

    @property
    def installed(self) -> bool:
        """True while the tap is the live ``tm.handlers``."""
        return self._installed and isinstance(self._tm.handlers, _TappedHandlers)

    def install(self) -> None:
        """Wrap ``tm.handlers`` (idempotent — a second install is a no-op)."""
        if isinstance(self._tm.handlers, _TappedHandlers):
            self._installed = True
            return
        self._tm.handlers = _TappedHandlers(
            self._tm.handlers,
            on_event=self._on_event,
            on_registered=self._on_handler_registered,
        )
        self._installed = True

    def reassert(self) -> bool:
        """Re-install if ``tm.handlers`` was replaced wholesale.
        Returns True when the tap was (re)installed."""
        if isinstance(self._tm.handlers, _TappedHandlers):
            return False
        self.install()
        return True

    def uninstall(self) -> None:
        """Restore a plain dict of the original handlers."""
        if isinstance(self._tm.handlers, _TappedHandlers):
            self._tm.handlers = self._tm.handlers.unwrap_all()
        self._installed = False
