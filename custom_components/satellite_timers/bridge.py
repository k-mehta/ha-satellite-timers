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

Phase 3 (consolidated spec rev 2, BRIDGE-1..3): the bridge is the source
of truth for timer state, not the satellite. It persists every timer
(name, duration, ``finishes_at``, state) to Home Assistant storage and
re-creates the sensors on start (BRIDGE-2), so timers survive a restart;
on start it re-asserts any finished-but-undismissed timer to the
satellite (BRIDGE-3); and it offers a ``start_timer`` service so cards
can start timers by name and duration (BRIDGE-1). Timers the bridge
restored from storage (the TimerManager no longer has them) can still
be paused, resumed, cancelled and extended through the bridge itself.

Pattern credit: the same tap-and-read approach is used by Second Hand
(mpjanofsky/second-hand-integration, Apache-2.0) to show Assist timers on an
Echo Wall Clock. This bridge uses it to mirror timers into sensor entities
for dashboard dials instead of driving a clock.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace as dataclass_replace
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
# How long a persisted timer for a device this bridge no longer mirrors
# is kept (P8-1): a finished timer's useful life is hours, and a device
# absent 7 days is decommissioned, not temporarily disconnected. A ghost
# device can never be dismissed by anyone, so keeping its records forever
# would leak storage with every dead device. Pruned records are logged.
_UNKNOWN_DEVICE_RETENTION = timedelta(days=7)

OnChange = Callable[[], None]


@dataclass(frozen=True)
class TimerSnapshot:
    """One voice timer, as the dashboard needs it.

    ``is_finished`` marks a timer that already went off: Home Assistant
    pops it from the TimerManager the moment it finishes, but the bridge
    keeps the record until it is dismissed, so the dashboard can show a
    "done" state instead of the dial vanishing mid-ring. ``language`` is
    the timer's language, kept so a finished timer can be restarted with
    the same voice. ``started_at`` is when the timer started (wall
    clock), so duplicate names can be told apart ("Pasta — started
    6:02 PM", TIMER-11). ``started_via_service`` marks timers created
    through the bridge's ``start_timer`` service (recipe chips, preset
    chips, Undo) as opposed to voice: the timer-jump automation must
    NOT fire for those (BRIDGE-1).
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
    started_at: datetime | None = None
    started_via_service: bool = False


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
        # Called with a device id whenever a satellite (re)registers its
        # timer handler — the bridge uses it to re-assert finished timers
        # to a satellite that reconnects (TIMER-12 missed-timer
        # announcement, BRIDGE-3 re-assertion).
        self.handler_listeners: list[Callable[[str], None]] = []
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

    def _notify_handler_registered(self, device_id: str) -> None:
        for listener in list(self.handler_listeners):
            try:
                listener(device_id)
            except Exception:  # never let a bridge problem break a satellite
                _LOGGER.exception("Satellite Timers handler listener failed")

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
        self._tap._notify_handler_registered(device_id)


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
        # nothing to show for it. Persisted to HA storage (BRIDGE-2), so
        # a finished timer survives a restart too — not in-memory only.
        self._finished: dict[str, TimerSnapshot] = {}
        # Timers restored from storage after a restart (BRIDGE-2): the
        # TimerManager no longer has them, so the bridge itself is their
        # source of truth until they finish, are cancelled, or are
        # dismissed. Keyed by timer id, like _finished.
        self._restored: dict[str, TimerSnapshot] = {}
        # Timer ids created through the bridge's start_timer service
        # (BRIDGE-1): the timer-jump automation must not fire for them.
        self._service_started: set[str] = set()
        # How many bridge start_timer/restart_timer calls are currently
        # inside HA's TimerManager.start_timer (BRIDGE-1 ordering). HA
        # generates the timer id inside that call and fires the STARTED
        # event synchronously within it, so the id cannot be marked
        # before the call — instead the flag tells _event that a
        # "started" event arriving right now belongs to this bridge's
        # own service call, and _event marks the id before any listener
        # reads a snapshot. A count, not a bool, so nested service
        # calls (none exist today) stay balanced; always reset in a
        # finally around the TimerManager call.
        self._service_start_in_flight = 0
        # When each timer started (wall clock), cached while the timer
        # is active so a paused timer keeps its exact start time
        # (TIMER-11). Populated from finishes_at - duration for active
        # timers, from finished_at - duration for finished ones.
        self._started_at: dict[str, datetime] = {}
        # HA storage for persistence (BRIDGE-2). None until
        # async_load_persisted() runs — without it the bridge simply
        # keeps everything in memory (unit tests, or a store failure).
        self._store: Any | None = None
        self._saving = False  # guards _persisted_data() re-entrancy

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
        # Re-assert finished timers to satellites that are already
        # connected (BRIDGE-3), and to any that (re)connect later: the
        # tap notifies handler_listeners on every (re)registration.
        tap.handler_listeners.append(self._on_handler_registered)
        tm = timer_manager(self._hass)
        if tm is not None:
            for device_id in list(tm.handlers):
                if device_id in self._devices:
                    self._on_handler_registered(device_id)
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

        if self._on_handler_registered in tap.handler_listeners:
            tap.handler_listeners.remove(self._on_handler_registered)

        if not tap.listeners:
            tap.remove()
            if self._hass.data.get(_TAP) is tap:
                self._hass.data.pop(_TAP, None)

    def snapshot(self, now: datetime) -> dict[str, TimerSnapshot]:
        """Current timers for the chosen satellites, keyed by timer id.

        Live timers come from the TimerManager; finished timers come from
        the bridge's own records (see _record_finished) and stay until
        dismissed; restored timers (BRIDGE-2) come from storage — the
        TimerManager lost them in the restart, so the bridge is their
        source of truth. A restored active timer whose finishes_at has
        passed becomes finished here (its finished_at is the true finish
        time, so "Done — N min ago" counts from when it actually went
        off) and the satellite hears FINISHED so it rings.
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
                        started_at=self._started_at.get(timer.id),
                        started_via_service=timer.id in self._service_started,
                    )
                    continue

                left_ns = timer.updated_at + timer.seconds * 1_000_000_000 - mono
                finishes_at = now + timedelta(microseconds=max(0, left_ns) // 1000)
                total = timer.created_seconds
                started_at = finishes_at - timedelta(seconds=total)
                self._started_at[timer.id] = started_at
                out[timer.id] = TimerSnapshot(
                    timer.id,
                    timer.name or "",
                    timer.device_id,
                    True,
                    total,
                    timer.seconds_left,
                    finishes_at,
                    started_at=started_at,
                    started_via_service=timer.id in self._service_started,
                )
            except (AttributeError, TypeError):
                _LOGGER.debug(
                    "Skipping a timer this version does not understand: %r", timer
                )

        # Restored timers (BRIDGE-2): the TimerManager does not have
        # them, so they only exist here. An active one that has run out
        # while Home Assistant was down (or since the last tick) becomes
        # finished — and the satellite is told, so it rings.
        changed = False
        for timer_id, restored in list(self._restored.items()):
            if restored.device_id not in self._devices:
                continue
            if (
                restored.is_active
                and not restored.is_finished
                and restored.finishes_at is not None
                and restored.finishes_at <= now
            ):
                finished = dataclass_replace(
                    restored,
                    is_active=False,
                    seconds_left=0,
                    finishes_at=None,
                    is_finished=True,
                    finished_at=restored.finishes_at,
                )
                self._finished[timer_id] = finished
                del self._restored[timer_id]
                self._announce_finished(finished)
                changed = True
                out[timer_id] = finished
                continue
            if restored.is_active and restored.finishes_at is not None:
                remaining = max(
                    0,
                    int((restored.finishes_at - now).total_seconds()),
                )
                out[timer_id] = dataclass_replace(
                    restored, seconds_left=remaining
                )
            else:
                out[timer_id] = restored

        for timer_id, finished in self._finished.items():
            if finished.device_id in self._devices:
                out[timer_id] = finished

        if changed:
            self._notify_listeners()
        return out

    def finished_timers(self) -> list[TimerSnapshot]:
        """Finished timers still awaiting dismissal, oldest first."""
        return sorted(
            (s for s in self._finished.values() if s.device_id in self._devices),
            key=lambda s: s.finished_at or datetime.min.replace(tzinfo=timezone.utc),
        )

    def restored_snapshot(self, timer_id: str) -> TimerSnapshot | None:
        """A timer restored from storage (BRIDGE-2), if it is still held."""
        return self._restored.get(timer_id)

    def has_restored(self, timer_id: str) -> bool:
        """True when the bridge holds a restored timer with this id."""
        return timer_id in self._restored

    # ------------------------------------------------------- start_timer

    def start_timer(
        self, device_id: str, name: str, total_seconds: int, language: str = "en"
    ) -> str:
        """Start a timer on a satellite through the TimerManager (BRIDGE-1).

        The name is the display name (a recipe chip passes the RECIPE
        NAME); the duration is in SECONDS. Duplicate names are accepted
        — the returned timer id is what distinguishes them. The new
        timer is marked as service-started, so the timer-jump automation
        can exclude it (chip-started timers must not yank the cook to
        the Timers view). Raises ValueError for a bad device or
        duration; the TimerManager raises TimersNotSupportedError when
        the satellite has no timer handler (unreachable).
        """
        if total_seconds <= 0:
            raise ValueError("Duration must be at least 1 second")
        if device_id not in self._devices:
            raise ValueError(f"Device {device_id} is not a configured satellite")

        tm = timer_manager(self._hass)
        if tm is None:
            raise ValueError("Assist timer manager is not available")

        hours, remainder = divmod(total_seconds, 3600)
        mins, secs = divmod(remainder, 60)
        # HA generates the timer id INSIDE start_timer and fires the
        # STARTED event synchronously within the call, before the id is
        # returned — so the mark cannot be set before the call. The
        # in-flight flag is what marks it: _event sees "started" while
        # the flag is set and adds the id to _service_started BEFORE
        # any listener reads a snapshot. (An earlier comment here
        # claimed the mark landed "before that notification" — false:
        # the notification fires inside start_timer.) The explicit add
        # below is belt-and-braces for paths where the event did not
        # reach _event (e.g. the bridge was never started).
        self._service_start_in_flight += 1
        try:
            timer_id = tm.start_timer(
                device_id,
                hours or None,
                mins or None,
                secs or None,
                language,
                name=name or None,
            )
        finally:
            self._service_start_in_flight -= 1
        self._service_started.add(timer_id)
        self._notify_listeners()
        return timer_id

    # ------------------------------------------- restored-timer controls
    #
    # After a restart the TimerManager no longer has these timers, so the
    # bridge itself pauses, resumes, cancels and extends them (BRIDGE-2).
    # Each method raises KeyError for a timer the bridge does not hold as
    # restored, and ValueError for a state that cannot take the action.

    def pause_restored(self, timer_id: str) -> TimerSnapshot:
        """Pause a restored active timer; the remaining time freezes."""
        snap = self._restored.get(timer_id)
        if snap is None:
            raise KeyError(timer_id)
        if snap.is_finished:
            raise ValueError(f"Timer {timer_id} has already finished")
        if not snap.is_active:
            return snap
        now = datetime.now(timezone.utc)
        remaining = snap.seconds_left
        if snap.finishes_at is not None:
            remaining = max(0, int((snap.finishes_at - now).total_seconds()))
        updated = dataclass_replace(
            snap, is_active=False, seconds_left=remaining, finishes_at=None
        )
        self._restored[timer_id] = updated
        self._notify_listeners()
        return updated

    def resume_restored(self, timer_id: str) -> TimerSnapshot:
        """Resume a restored paused timer; it finishes remaining later."""
        snap = self._restored.get(timer_id)
        if snap is None:
            raise KeyError(timer_id)
        if snap.is_finished:
            raise ValueError(f"Timer {timer_id} has already finished")
        if snap.is_active:
            return snap
        now = datetime.now(timezone.utc)
        updated = dataclass_replace(
            snap,
            is_active=True,
            finishes_at=now + timedelta(seconds=snap.seconds_left),
        )
        self._restored[timer_id] = updated
        self._notify_listeners()
        return updated

    def cancel_restored(self, timer_id: str) -> TimerSnapshot:
        """Cancel a restored timer and drop its record.

        The satellite is told CANCELLED, best effort: if it still holds
        a countdown for this timer, that stops it; if it holds nothing,
        the event is harmless.
        """
        snap = self._restored.pop(timer_id, None)
        if snap is None:
            raise KeyError(timer_id)
        self._service_started.discard(timer_id)
        self._started_at.pop(timer_id, None)
        self._send_to_satellite(snap, "cancelled")
        self._notify_listeners()
        return snap

    def add_time_restored(self, timer_id: str, seconds: int) -> TimerSnapshot:
        """Add seconds to a restored timer (active or paused)."""
        snap = self._restored.get(timer_id)
        if snap is None:
            raise KeyError(timer_id)
        if snap.is_finished:
            raise ValueError(f"Timer {timer_id} has already finished")
        if snap.is_active and snap.finishes_at is not None:
            updated = dataclass_replace(
                snap, finishes_at=snap.finishes_at + timedelta(seconds=seconds)
            )
        else:
            updated = dataclass_replace(
                snap, seconds_left=snap.seconds_left + seconds
            )
        self._restored[timer_id] = updated
        self._notify_listeners()
        return updated

    # -------------------------------------------------------- persistence
    #
    # BRIDGE-2: the bridge persists every timer to Home Assistant storage
    # on every change (via _notify_listeners) and re-creates them on
    # start. The store is a plain HA Store (key "satellite_timers",
    # version 1); a caller may inject a fake store with async_load /
    # async_save for tests. Without a store the bridge keeps everything
    # in memory only.

    async def async_load_persisted(self, store: Any | None = None) -> int:
        """Load persisted timers into _finished / _restored.

        Returns how many records were restored. Timers that are live in
        the TimerManager again win over their stored record (an entry
        reload, as opposed to a restart, keeps the live timers). A
        stored active timer whose finishes_at has passed restores as
        finished — it went off while Home Assistant was down.
        """
        if store is None:
            from homeassistant.helpers.storage import Store

            store = Store(self._hass, 1, "satellite_timers")
        self._store = store
        try:
            data = await store.async_load()
        except Exception:
            _LOGGER.warning("Could not load persisted timers", exc_info=True)
            return 0
        if not isinstance(data, dict):
            return 0
        tm = timer_manager(self._hass)
        live_ids = set(tm.timers) if tm is not None else set()
        now = datetime.now(timezone.utc)
        restored = 0
        unknown_devices: dict[str, list[str]] = {}
        for record in data.get("timers", []) or []:
            snap = self._snapshot_from_record(record, now)
            if snap is None or snap.timer_id in live_ids:
                continue
            if snap.device_id not in self._devices:
                # P8-1: keep the record (a re-pair must not silently
                # erase it), but say so in the log — and drop it if
                # the device has been gone longer than the retention
                # bound, so dead devices cannot leak storage forever.
                if self._unknown_record_is_stale(snap, now):
                    _LOGGER.warning(
                        "Dropping persisted timer %s for unknown device "
                        "%s: it finished over %d days ago",
                        snap.timer_id,
                        snap.device_id,
                        _UNKNOWN_DEVICE_RETENTION.days,
                    )
                    continue
                unknown_devices.setdefault(str(snap.device_id), []).append(
                    snap.timer_id
                )
            if snap.is_finished:
                self._finished[snap.timer_id] = snap
            else:
                self._restored[snap.timer_id] = snap
            if snap.started_via_service:
                self._service_started.add(snap.timer_id)
            if snap.started_at is not None:
                self._started_at[snap.timer_id] = snap.started_at
            restored += 1
        for device_id, timer_ids in sorted(unknown_devices.items()):
            _LOGGER.warning(
                "Kept %d persisted timer(s) for unknown device %s "
                "(not a configured satellite — the timer(s) %s are "
                "preserved in storage but have no sensor until the "
                "device is configured again)",
                len(timer_ids),
                device_id,
                ", ".join(sorted(timer_ids)),
            )
        if restored:
            _LOGGER.info("Restored %d timers from storage", restored)
        return restored

    @staticmethod
    def _unknown_record_is_stale(snap: TimerSnapshot, now: datetime) -> bool:
        """True when a record for an unknown device is past retention.

        A finished record ages from finished_at; an active one from
        finishes_at (a finish time more than 7 days in the past means
        it went off over a week ago). A paused record has neither
        timestamp, so it is never stale by this test and is kept.
        """
        ref = snap.finished_at if snap.is_finished else snap.finishes_at
        return ref is not None and ref < now - _UNKNOWN_DEVICE_RETENTION

    def _prune_stale_unknown_records(self, now: datetime) -> None:
        """Drop held records for unknown devices that are past retention.

        Runs before a save is built, so a stale ghost record is pruned
        from storage on the next save instead of being preserved
        forever. Each prune is logged with the device id.
        """
        for held in (self._finished, self._restored):
            for timer_id, snap in list(held.items()):
                if snap.device_id in self._devices:
                    continue
                if self._unknown_record_is_stale(snap, now):
                    del held[timer_id]
                    _LOGGER.warning(
                        "Pruned persisted timer %s for unknown device "
                        "%s: it finished over %d days ago",
                        timer_id,
                        snap.device_id,
                        _UNKNOWN_DEVICE_RETENTION.days,
                    )

    def _persisted_data(self, now: datetime) -> dict:
        """All current timers as a JSON-able storage record.

        The device-filtered snapshot is the sensor set — but storage
        keeps MORE than the sensors: every held _finished / _restored
        record, for any device (P8-1). A timer whose device id changed
        (re-pair, HA restore) is preserved in storage across saves
        instead of being silently erased on the next save; it surfaces
        as a sensor again as soon as its device is configured. Stale
        unknown-device records past the retention bound are pruned
        first (see _prune_stale_unknown_records).
        """
        self._prune_stale_unknown_records(now)
        snaps = dict(self.snapshot(now))
        for held in (self._finished, self._restored):
            for timer_id, snap in held.items():
                if timer_id not in snaps:
                    snaps[timer_id] = snap
        timers = []
        for snap in snaps.values():
            state = (
                "finished"
                if snap.is_finished
                else "active"
                if snap.is_active
                else "paused"
            )
            timers.append(
                {
                    "timer_id": snap.timer_id,
                    "name": snap.name,
                    "device_id": snap.device_id,
                    "state": state,
                    "total_seconds": snap.total_seconds,
                    "seconds_left": snap.seconds_left,
                    "finishes_at": (
                        snap.finishes_at.isoformat()
                        if snap.finishes_at is not None
                        else None
                    ),
                    "finished_at": (
                        snap.finished_at.isoformat()
                        if snap.finished_at is not None
                        else None
                    ),
                    "started_at": (
                        snap.started_at.isoformat()
                        if snap.started_at is not None
                        else None
                    ),
                    "language": snap.language,
                    "started_via_service": snap.started_via_service,
                }
            )
        return {"version": 1, "timers": timers}

    def _schedule_save(self) -> None:
        """Persist the current timers, best effort, off the call stack."""
        if self._store is None or self._saving:
            return
        self._saving = True
        try:
            data = self._persisted_data(datetime.now(timezone.utc))
        except Exception:
            _LOGGER.warning("Could not build persisted timers", exc_info=True)
            return
        finally:
            self._saving = False
        create_task = getattr(self._hass, "async_create_task", None)
        if create_task is None:
            return
        try:
            create_task(self._store.async_save(data))
        except Exception:
            _LOGGER.warning("Could not save persisted timers", exc_info=True)

    @staticmethod
    def _snapshot_from_record(
        record: dict, now: datetime
    ) -> TimerSnapshot | None:
        """Rebuild a snapshot from one stored record, or None if invalid."""
        if not isinstance(record, dict):
            return None
        timer_id = record.get("timer_id")
        if not timer_id or not isinstance(timer_id, str):
            return None

        def _parse(value: Any) -> datetime | None:
            if not value or not isinstance(value, str):
                return None
            try:
                parsed = datetime.fromisoformat(value)
            except ValueError:
                return None
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed

        total = int(record.get("total_seconds") or 0)
        state = record.get("state")
        finishes_at = _parse(record.get("finishes_at"))
        finished_at = _parse(record.get("finished_at"))
        started_at = _parse(record.get("started_at"))
        if state == "finished":
            if finished_at is None:
                finished_at = finishes_at or now
            if started_at is None and finished_at is not None and total:
                started_at = finished_at - timedelta(seconds=total)
            return TimerSnapshot(
                timer_id,
                str(record.get("name") or ""),
                record.get("device_id"),
                False,
                total,
                0,
                None,
                is_finished=True,
                finished_at=finished_at,
                language=str(record.get("language") or "en"),
                started_at=started_at,
                started_via_service=bool(record.get("started_via_service")),
            )
        if state == "paused":
            return TimerSnapshot(
                timer_id,
                str(record.get("name") or ""),
                record.get("device_id"),
                False,
                total,
                int(record.get("seconds_left") or 0),
                None,
                language=str(record.get("language") or "en"),
                started_at=started_at,
                started_via_service=bool(record.get("started_via_service")),
            )
        if state == "active":
            if finishes_at is not None and finishes_at <= now:
                # It went off while Home Assistant was down: a missed
                # timer, restored as finished (TIMER-12).
                if started_at is None and total:
                    started_at = finishes_at - timedelta(seconds=total)
                return TimerSnapshot(
                    timer_id,
                    str(record.get("name") or ""),
                    record.get("device_id"),
                    False,
                    total,
                    0,
                    None,
                    is_finished=True,
                    finished_at=finishes_at,
                    language=str(record.get("language") or "en"),
                    started_at=started_at,
                    started_via_service=bool(record.get("started_via_service")),
                )
            if started_at is None and finishes_at is not None and total:
                started_at = finishes_at - timedelta(seconds=total)
            return TimerSnapshot(
                timer_id,
                str(record.get("name") or ""),
                record.get("device_id"),
                True,
                total,
                int(record.get("seconds_left") or 0),
                finishes_at,
                language=str(record.get("language") or "en"),
                started_at=started_at,
                started_via_service=bool(record.get("started_via_service")),
            )
        return None

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
            self._service_started.discard(timer_id)
            self._started_at.pop(timer_id, None)
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
        already finished, so the bridge sends it here. A failure to reach
        the satellite never breaks the dismiss itself (the sensor is
        already cleared) — it is logged instead.
        """
        self._send_to_satellite(finished, "cancelled")

    def _announce_finished(self, finished: TimerSnapshot) -> None:
        """Tell the satellite a timer finished, so it rings (BRIDGE-3).

        Used when the satellite (re)connects with a finished timer
        still undismissed (TIMER-12 missed-timer announcement), and
        when a restored timer runs out after a restart. The event goes
        to the satellite's ORIGINAL handler, not the tap-wrapped one:
        the bridge already recorded this finish, and re-notifying
        itself would reset the finished_at clock.
        """
        self._send_to_satellite(finished, "finished")

    def _on_handler_registered(self, device_id: str) -> None:
        """A satellite (re)registered its timer handler.

        Re-assert every finished-but-undismissed timer on that device
        (BRIDGE-3): if the satellite never heard FINISHED (it was
        unreachable when the timer went off), it hears it now and rings;
        if it is already ringing, the event simply re-asserts the state.
        """
        if device_id not in self._devices:
            return
        for finished in self.finished_timers():
            if finished.device_id == device_id:
                self._announce_finished(finished)

    def _send_to_satellite(self, snap: TimerSnapshot, event_type: str) -> None:
        """Send one timer event to a satellite's ORIGINAL handler.

        ``event_type`` is "cancelled" (stop ringing / stop a countdown)
        or "finished" (start ringing). The original handler is called
        directly, bypassing the tap: the bridge already knows about
        this timer, and going through the tap would re-record it.
        Failures are logged, never raised — a satellite that cannot be
        reached must not break the dashboard action.
        """
        if snap.device_id is None:
            return
        try:
            from homeassistant.components.intent.timers import (
                TimerEventType,
                TimerInfo,
            )
        except ImportError:
            _LOGGER.warning(
                "Could not import TimerInfo/TimerEventType; the satellite "
                "for timer %s was not told about %s",
                snap.timer_id,
                event_type,
            )
            return

        tm = timer_manager(self._hass)
        if tm is None:
            return
        wrapped = tm.handlers.get(snap.device_id)
        if wrapped is None:
            return
        handler = getattr(wrapped, "_satellite_timers_original", wrapped)

        now_ns = time.monotonic_ns()
        timer = TimerInfo(
            id=snap.timer_id,
            name=snap.name or None,
            seconds=snap.total_seconds,
            device_id=snap.device_id,
            start_hours=None,
            start_minutes=None,
            start_seconds=None,
            created_at=now_ns,
            updated_at=now_ns,
            language=snap.language,
        )
        if snap.is_finished:
            # A finished timer: no time left, not ticking. finish()
            # sets seconds to 0 and is_active to False while keeping
            # the created duration, matching what the TimerManager
            # produced.
            timer.finish()
        elif not snap.is_active:
            # A paused timer keeps its remaining time, frozen.
            timer.seconds = snap.seconds_left
            timer.is_active = False
        else:
            # A running timer: the seconds left, still ticking.
            timer.seconds = snap.seconds_left
        try:
            handler(TimerEventType(event_type), timer)
        except Exception:
            _LOGGER.warning(
                "Could not tell the satellite for timer %s about %s",
                snap.timer_id,
                event_type,
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
        # Same ordering as start_timer (BRIDGE-1): HA fires STARTED
        # synchronously inside start_timer, so the in-flight flag marks
        # the new id in _event before any listener reads a snapshot.
        # The finished record is dropped only after the call succeeds,
        # so a failed restart keeps the old sensor.
        self._service_start_in_flight += 1
        try:
            new_id = tm.start_timer(
                finished.device_id,
                hours or None,
                mins or None,
                secs or None,
                finished.language,
                name=finished.name or None,
            )
        finally:
            self._service_start_in_flight -= 1
        # A restart is a card action, not a voice start, so the new
        # timer is marked service-started and does not re-trigger the
        # timer-jump; drop the finished record so the old sensor does
        # not linger next to the new one.
        self._finished.pop(timer_id, None)
        self._service_started.discard(timer_id)
        self._started_at.pop(timer_id, None)
        self._service_started.add(new_id)
        self._notify_listeners()
        return new_id

    def _notify_listeners(self) -> None:
        """Tell change listeners the timer set may have changed."""
        for listener in list(self._change_listeners):
            try:
                listener()
            except Exception:  # never let a listener break the satellite
                _LOGGER.exception("Satellite Timers change listener failed")
        # Every change is also a persistence point (BRIDGE-2): the
        # stored record is what a restart restores from.
        self._schedule_save()

    def _record_finished(self, timer: Any) -> None:
        """Keep a finished timer visible until it is dismissed.

        TimerManager pops the timer before calling the handler, so by the
        time this runs the timer is already gone from ``tm.timers`` — the
        record here is the only thing left, and it is what keeps the
        sensor (and the dial) around after the timer goes off.
        """
        finished_at = datetime.now(timezone.utc)
        started_at = self._started_at.get(timer.id)
        if started_at is None:
            total = getattr(timer, "created_seconds", 0) or 0
            if total:
                started_at = finished_at - timedelta(seconds=total)
        self._finished[timer.id] = TimerSnapshot(
            timer.id,
            getattr(timer, "name", "") or "",
            getattr(timer, "device_id", None),
            False,
            getattr(timer, "created_seconds", 0) or 0,
            0,
            None,
            is_finished=True,
            finished_at=finished_at,
            language=getattr(timer, "language", "en") or "en",
            started_at=started_at,
            started_via_service=timer.id in self._service_started,
        )

    @callback
    def _event(self, event_type: str, timer: Any) -> None:
        if (
            getattr(timer, "device_id", None) not in self._devices
            or getattr(timer, "conversation_command", None)
        ):
            return

        if event_type == "started" and self._service_start_in_flight:
            # This STARTED event fired synchronously inside one of this
            # bridge's own start_timer / restart_timer calls (HA
            # generates the id inside the call, so the mark cannot be
            # set before it). Mark the id BEFORE notifying listeners,
            # so the first snapshot any listener reads already carries
            # started_via_service (BRIDGE-1).
            self._service_started.add(timer.id)
        elif event_type == "cancelled":
            # A voice-cancelled service-started timer must not keep
            # its mark forever (ids never repeat, but the set would
            # otherwise grow without bound).
            self._service_started.discard(timer.id)

        if event_type == "finished":
            self._record_finished(timer)

        self._notify_listeners()
