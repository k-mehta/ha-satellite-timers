"""Read Assist (voice satellite) timers from Home Assistant internals.

Home Assistant keeps voice timers in memory only: the intent integration's
TimerManager holds ``timers`` (id -> TimerInfo) and ``handlers``
(device_id -> one handler per satellite). It fires no bus events and has no
"list all timers" call, so no dashboard card or automation can see them.

This module reads ``timers`` directly and taps ``handlers``: it swaps the
dict for a subclass that wraps every handler set into it, calls the
satellite's original handler first, then notifies listeners. Everything that
touches those internals lives in this file and is checked by
``assist_supported()``; if Home Assistant changes them, the bridge stays off
and a repair issue says why, and nothing else breaks.

A finished timer needs one more piece: TimerManager pops it from ``timers``
the moment it goes off, so the bridge keeps its own record of finished
timers (``_finished``) until they are dismissed or restarted. Without that
the sensor — and the dial — would vanish the instant the timer rings.

Pattern credit: the same tap-and-read approach is used by Second Hand
(mpjanofsky/second-hand-integration, Apache-2.0) to show Assist timers on an
Echo Wall Clock. This bridge uses it to mirror timers into sensor entities
for dashboard dials instead of driving a clock.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
import time
from typing import TYPE_CHECKING, Any

from homeassistant.core import callback

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

DOMAIN = "satellite_timers"
TIMER_DATA = "intent.timer"  # homeassistant.components.intent.const.TIMER_DATA
_TAP = f"{DOMAIN}_assist_tap"

OnChange = Callable[[], None]


@dataclass(frozen=True)
class TimerSnapshot:
    """One voice timer, as the dashboard needs it.

    ``is_finished`` marks a timer that already went off: Home Assistant
    pops it from the TimerManager the moment it finishes, but the bridge
    keeps the record until it is dismissed, so the dashboard can show a
    "done" state instead of the dial vanishing mid-ring. ``language`` is
    the timer's language, kept so a finished timer can be restarted with
    the same voice.
    """

    timer_id: str
    name: str
    device_id: str | None
    is_active: bool
    total_seconds: int
    seconds_left: int
    finishes_at: datetime | None
    is_finished: bool = False
    finished_at: datetime | None = None
    language: str = "en"


# ------------------------------------------------------------------ internals


def normalize_timer_name(name: str) -> str:
    """Lowercase a timer name and drop everything but letters and digits.

    Timer names come from speech, so "Pasta", "pasta" and "Pasta!" must all
    match the same timer. This mirrors how Home Assistant matches names
    when it looks a timer up by name.
    """
    return "".join(ch for ch in name.lower() if ch.isalnum())


def timer_manager(hass: HomeAssistant) -> Any | None:
    """Return Home Assistant's intent TimerManager, or None if not loaded."""
    return hass.data.get(TIMER_DATA)


def assist_supported(hass: HomeAssistant) -> bool:
    """True if HA's TimerManager still looks the way this bridge expects."""
    tm = timer_manager(hass)

    return (
        tm is not None
        and isinstance(getattr(tm, "timers", None), dict)
        and isinstance(getattr(tm, "handlers", None), dict)
        and callable(getattr(tm, "register_handler", None))
    )


def timer_devices(hass: HomeAssistant) -> list[str]:
    """Device ids of the voice satellites that can have timers right now."""
    tm = timer_manager(hass)

    if not assist_supported(hass):
        return []

    return list(tm.handlers)


class _Tap:
    """Shared by every bridge: wraps satellites' timer handlers.

    The satellite's original handler is always called first; listeners only
    hear about the event afterwards, and a failing listener can never break
    the satellite.
    """

    def __init__(self, tm: Any) -> None:
        self.tm = tm
        self.listeners: list[Callable[[str, Any], None]] = []
        self._original_dict = tm.handlers
        self._tapped_dict = _TappedHandlers(self, tm.handlers)
        tm.handlers = self._tapped_dict
        self._removed = False

    def wrap(self, handler: Callable[..., None]) -> Callable[..., None]:
        if getattr(handler, "_satellite_timers_original", None) is not None:
            return handler

        def tapped(event_type: Any, timer: Any) -> None:
            try:
                handler(event_type, timer)
            finally:
                self._notify(str(event_type), timer)

        tapped._satellite_timers_original = handler  # type: ignore[attr-defined]
        return tapped

    def _notify(self, event_type: str, timer: Any) -> None:
        for listener in list(self.listeners):
            try:
                listener(event_type, timer)
            except Exception:  # never let a bridge problem break a satellite
                _LOGGER.exception("Satellite Timers listener failed")

    def remove(self) -> None:
        """Put the plain dict and the original handlers back.

        Safe to call twice: the second call is a no-op. Without this, a
        second call wipes every satellite's timer handler, because after
        the first call tm.handlers IS the original dict, so clearing it
        and iterating it repopulates nothing — and voice timers stop
        working on every satellite until Home Assistant restarts.
        """
        if self._removed:
            return
        self._removed = True
        if self.tm.handlers is not self._tapped_dict:
            return  # already removed, or superseded by a newer tap
        restored = {
            device_id: getattr(handler, "_satellite_timers_original", handler)
            for device_id, handler in dict.items(self._tapped_dict)
        }
        self.tm.handlers = self._original_dict
        self._original_dict.clear()
        self._original_dict.update(restored)


class _TappedHandlers(dict):
    """TimerManager.handlers replacement: every handler set into it is wrapped."""

    def __init__(self, tap: _Tap, existing: dict[str, Any]) -> None:
        super().__init__()
        self._tap = tap
        for device_id, handler in existing.items():
            self[device_id] = handler

    def __setitem__(self, device_id: str, handler: Any) -> None:
        super().__setitem__(device_id, self._tap.wrap(handler))


# ---------------------------------------------------------------------- bridge


class SatelliteTimerBridge:
    """Mirror Assist timers for chosen satellites into snapshots."""

    def __init__(self, hass: HomeAssistant, device_ids: list[str]) -> None:
        self._hass = hass
        self._devices = set(device_ids)
        self._change_listeners: list[OnChange] = []
        self._running = False
        self._tap: _Tap | None = None
        # Finished timers, kept until dismissed: TimerManager pops a timer
        # the moment it finishes, so without this the dashboard would have
        # nothing to show for it. In-memory only — an undismissed finished
        # timer does not survive a Home Assistant restart.
        self._finished: dict[str, TimerSnapshot] = {}

    def add_change_listener(self, listener: OnChange) -> None:
        """Call ``listener`` whenever a timer starts, changes or ends."""
        self._change_listeners.append(listener)

    def remove_change_listener(self, listener: OnChange) -> None:
        if listener in self._change_listeners:
            self._change_listeners.remove(listener)

    def set_devices(self, device_ids: list[str]) -> None:
        """Change which satellites are mirrored (takes effect on next sync)."""
        self._devices = set(device_ids)

    @property
    def devices(self) -> frozenset[str]:
        """Ids of the satellites this bridge mirrors."""
        return frozenset(self._devices)

    def start(self) -> bool:
        """Install the tap. False if HA's internals changed (caller gives up)."""
        if not self._devices:
            return True

        if not assist_supported(self._hass):
            return False

        tap: _Tap | None = self._hass.data.get(_TAP)

        if tap is None or tap.tm is not timer_manager(self._hass):
            tap = self._hass.data[_TAP] = _Tap(timer_manager(self._hass))

        self._tap = tap
        tap.listeners.append(self._event)
        self._running = True
        return True

    def stop(self) -> None:
        """Remove the tap and put the satellite handlers back as they were."""
        if not self._running:
            return

        self._running = False

        tap = self._tap
        self._tap = None

        if tap is None:
            return

        if self._event in tap.listeners:
            tap.listeners.remove(self._event)

        if not tap.listeners:
            tap.remove()
            if self._hass.data.get(_TAP) is tap:
                self._hass.data.pop(_TAP, None)

    def snapshot(self, now: datetime) -> dict[str, TimerSnapshot]:
        """Current timers for the chosen satellites, keyed by timer id.

        Live timers come from the TimerManager; finished timers come from
        the bridge's own records (see _record_finished) and stay until
        dismissed.
        """
        if not self._running:
            return {}

        tm = timer_manager(self._hass)
        out: dict[str, TimerSnapshot] = {}
        mono = time.monotonic_ns()

        for timer in list(tm.timers.values()):
            try:
                if (
                    getattr(timer, "conversation_command", None)
                    or timer.device_id not in self._devices
                ):
                    continue

                if not timer.is_active:
                    out[timer.id] = TimerSnapshot(
                        timer.id,
                        timer.name or "",
                        timer.device_id,
                        False,
                        timer.created_seconds,
                        timer.seconds_left,
                        None,
                    )
                    continue

                left_ns = timer.updated_at + timer.seconds * 1_000_000_000 - mono
                finishes_at = now + timedelta(microseconds=max(0, left_ns) // 1000)
                out[timer.id] = TimerSnapshot(
                    timer.id,
                    timer.name or "",
                    timer.device_id,
                    True,
                    timer.created_seconds,
                    timer.seconds_left,
                    finishes_at,
                )
            except (AttributeError, TypeError):
                _LOGGER.debug(
                    "Skipping a timer this version does not understand: %r", timer
                )

        for timer_id, finished in self._finished.items():
            if finished.device_id in self._devices:
                out[timer_id] = finished

        return out

    def finished_timers(self) -> list[TimerSnapshot]:
        """Finished timers still awaiting dismissal, oldest first."""
        return sorted(
            (s for s in self._finished.values() if s.device_id in self._devices),
            key=lambda s: s.finished_at or datetime.min.replace(tzinfo=timezone.utc),
        )

    def has_live_timer(self, device_id: str | None, name: str) -> bool:
        """True when a still-running timer on this device has this name.

        A device id of None matches any device: a command typed into
        Home Assistant carries no device, and Home Assistant's own timer
        lookup only filters by device when one is given.
        """
        tm = timer_manager(self._hass)
        if tm is None:
            return False
        wanted = normalize_timer_name(name)
        return any(
            (device_id is None or getattr(timer, "device_id", None) == device_id)
            and normalize_timer_name(getattr(timer, "name", "") or "") == wanted
            for timer in tm.timers.values()
        )

    def dismiss_timer(self, timer_id: str) -> TimerSnapshot:
        """Drop a finished timer's record so its sensor disappears.

        Dismissing also tells the satellite to stop ringing: the
        satellite only stops its alarm when its timer handler hears
        a CANCELLED event, and nothing else sends one for a timer
        that already finished (Home Assistant pops it from the
        TimerManager the moment it goes off). Clearing only the
        bridge's record leaves the satellite ringing.

        Raises KeyError when there is no such timer at all, and ValueError
        when the timer is still running (cancel it instead — a running
        timer has not finished, so there is nothing to dismiss).
        """
        finished = self._finished.pop(timer_id, None)
        if finished is not None:
            self._stop_satellite_ringing(finished)
            self._notify_listeners()
            return finished

        tm = timer_manager(self._hass)
        if tm is not None and timer_id in tm.timers:
            raise ValueError(f"Timer {timer_id} is still running")
        raise KeyError(timer_id)

    def _stop_satellite_ringing(self, finished: TimerSnapshot) -> None:
        """Tell the satellite its finished timer is over, so it stops ringing.

        The satellite's timer handler is what starts the alarm when it
        hears FINISHED, and only a CANCELLED event for the same timer
        stops it. Home Assistant never sends one for a timer that
        already finished, so the bridge sends it here, rebuilding a
        finished TimerInfo from its own record. A failure to reach the
        satellite never breaks the dismiss itself (the sensor is
        already cleared) — it is logged instead.
        """
        if finished.device_id is None:
            return
        try:
            from homeassistant.components.intent.timers import (
                TimerEventType,
                TimerInfo,
            )
        except ImportError:
            _LOGGER.warning(
                "Could not import TimerInfo/TimerEventType; the satellite "
                "for finished timer %s was not told to stop ringing",
                finished.timer_id,
            )
            return

        tm = timer_manager(self._hass)
        if tm is None:
            return
        handler = tm.handlers.get(finished.device_id)
        if handler is None:
            return

        now_ns = time.monotonic_ns()
        timer = TimerInfo(
            id=finished.timer_id,
            name=finished.name or None,
            seconds=finished.total_seconds,
            device_id=finished.device_id,
            start_hours=None,
            start_minutes=None,
            start_seconds=None,
            created_at=now_ns,
            updated_at=now_ns,
            language=finished.language,
        )
        # A finished timer: no time left, not ticking. finish() sets
        # seconds to 0 and is_active to False while keeping the
        # created duration, matching what the TimerManager produced.
        timer.finish()
        try:
            handler(TimerEventType.CANCELLED, timer)
        except Exception:
            _LOGGER.warning(
                "Could not tell the satellite for finished timer %s to "
                "stop ringing",
                finished.timer_id,
                exc_info=True,
            )

    def restart_timer(
        self, timer_id: str, minutes: int = 0, seconds: int = 0
    ) -> str:
        """Start a fresh timer from a finished one and drop the old record.

        Without an override the new timer has the finished timer's name
        and total duration; with minutes/seconds it runs for that long
        instead (the override is for the service caller — the card's
        Restart button always reuses the original duration). Returns the
        new timer's id. Raises KeyError when the timer never finished (or
        was already dismissed).
        """
        finished = self._finished.get(timer_id)
        if finished is None:
            raise KeyError(timer_id)

        total = minutes * 60 + seconds
        if total <= 0:
            total = finished.total_seconds
        if total <= 0:
            raise ValueError(f"Timer {timer_id} has no duration to restart with")

        tm = timer_manager(self._hass)
        if tm is None:
            raise ValueError("Assist timer manager is not available")

        hours, remainder = divmod(total, 3600)
        mins, secs = divmod(remainder, 60)
        new_id = tm.start_timer(
            finished.device_id,
            hours or None,
            mins or None,
            secs or None,
            finished.language,
            name=finished.name or None,
        )
        # The STARTED event fires inside start_timer and already notifies
        # listeners; drop the finished record either way so the old sensor
        # does not linger next to the new one.
        self._finished.pop(timer_id, None)
        self._notify_listeners()
        return new_id

    def _notify_listeners(self) -> None:
        """Tell change listeners the timer set may have changed."""
        for listener in list(self._change_listeners):
            try:
                listener()
            except Exception:  # never let a listener break the satellite
                _LOGGER.exception("Satellite Timers change listener failed")

    def _record_finished(self, timer: Any) -> None:
        """Keep a finished timer visible until it is dismissed.

        TimerManager pops the timer before calling the handler, so by the
        time this runs the timer is already gone from ``tm.timers`` — the
        record here is the only thing left, and it is what keeps the
        sensor (and the dial) around after the timer goes off.
        """
        self._finished[timer.id] = TimerSnapshot(
            timer.id,
            getattr(timer, "name", "") or "",
            getattr(timer, "device_id", None),
            False,
            getattr(timer, "created_seconds", 0) or 0,
            0,
            None,
            is_finished=True,
            finished_at=datetime.now(timezone.utc),
            language=getattr(timer, "language", "en") or "en",
        )

    @callback
    def _event(self, event_type: str, timer: Any) -> None:
        if (
            getattr(timer, "device_id", None) not in self._devices
            or getattr(timer, "conversation_command", None)
        ):
            return

        if event_type == "finished":
            self._record_finished(timer)

        self._notify_listeners()
