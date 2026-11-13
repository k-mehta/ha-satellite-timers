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
timers (``_records``) until they are dismissed or restarted. Without that
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
import uuid
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
class TimerRecord:
    """One voice timer, as the dashboard needs it.

    ``is_finished`` marks a timer that already went off: Home Assistant
    pops it from the TimerManager the moment it finishes, but the bridge
    keeps the record until it is dismissed, so the dashboard can show a
    "done" state instead of the dial vanishing mid-ring. There is no
    "ringing" state (§5.1): the bridge publishes ``finished_at`` and
    each consumer that needs recency derives a window from it. ``language``
    is the timer's language, kept so a finished timer can be restarted
    with the same voice. ``started_at`` is when the bridge OBSERVED the
    timer start (the STARTED tap for a voice start, or the service call
    that created it) — recorded when observed, omitted when not, never
    derived from ``finishes_at``/``finished_at`` minus duration (§5.1).
    ``started_via_service`` marks timers created through the bridge's
    ``start_timer`` service (recipe chips, preset chips, Undo) as
    opposed to voice: the timer-jump automation must NOT fire for
    those (BRIDGE-1).
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


@dataclass(frozen=True)
class PendingRecord:
    """One durable pending-delivery record (§5.3, RB-R2-2).

    Every outbound satellite event (FINISHED, CANCELLED) is a record in
    the bridge HA Store under the ``pending_events`` key, not a
    fire-and-forget call. ``direct_delivery_attempted`` lives ONLY on
    pending records (§5.3 F2) — never on timer records — and only on
    FINISHED records: it records whether TimerManager invoked the
    handler for the device at the finish. For a finish the bridge
    learns through the tap the flag is always true; ``false`` is
    reachable only through reconciliation or the restore-conversion
    (RB-R2-3). CANCELLED records carry no flag (``None``).

    The remaining fields make a queue record self-contained for a
    re-send: the timer record is usually gone by drain time (dismiss
    pops it), so the drain builds its send snapshot from the record
    itself — ``name`` / ``total_seconds`` / ``language`` /
    ``is_finished`` are the send shape captured at enqueue time.
    ``delivery_failed`` records whether the most recent send attempt
    for this record hit an observably-impossible path (one of
    _send_to_satellite's bare returns, or the handler-raise path) — it
    is the repair-issue condition (§5.3) and is persisted so the
    issue's age (from the original ``enqueued_at``) survives a restart.
    It clears only after ``_DELIVERY_CLEAR_STREAK`` consecutive
    attempts that reach the handler ("sent"): a single "sent" is
    consistent with a silent drop inside the handler (B1), so one
    clean return is not evidence the path is open again — clearing on
    it would let the repair issue false-clear and flap on every tick
    while the handler flaps (fix-burst F3). Clearing the mark is NOT
    delivery confirmation — the record stays pending regardless; only
    the observable-failure signal is reset. ``sent_streak`` counts
    those consecutive "sent" attempts (reset to 0 by any
    observably-impossible attempt); it is persisted with the mark so
    a restart does not reset the hysteresis.

    ``attempt_count`` / ``last_attempt_at`` drive the trigger-4
    backoff for CANCELLED records (fix-burst F2): after the first
    attempt the next re-send waits 60 s, then 5 min, then 15 min
    between attempts — a dismissed timer that never gets a liveness
    signal costs ~100 sends over 24 h, not ~1,440. The backoff
    applies ONLY to the CANCELLED retry cadence on trigger 4: liveness
    drains (triggers 1/2/3) re-send immediately when the channel
    proves live, and FINISHED re-sends are a different mechanism
    (``_finished_may_resend`` + purge on a liveness drain) with their
    own exactly-once-attempt rule — they are never gated by this
    backoff. Every send attempt for the record (immediate dismiss /
    restart / cancel sends and every drain re-send) bumps the count
    and re-stamps the time, so the clock restarts on any attempt.
    Every drain that attempts a send persists the mutated
    bookkeeping — count, last-attempt time, streak and mark — so a
    restart resets neither the backoff ladder nor the hysteresis
    (round-2 B1: pre-fix only a ``delivery_failed`` transition
    counted as a queue change, so a re-send-only drain left the new
    streak/count in memory).
    All four are optional with defaults, so records written by the
    store step (RB-R2-1) load cleanly.

    ``started_at`` is the timer's observed start, carried so a pending
    snapshot can still be told apart from another timer with the same
    name after the finished record is gone (round-2 B5): the voice
    re-dismiss listing renders it ("Pasta started 6:02 PM"), the same
    TIMER-11 rule finished timers use. It is whatever the enqueue
    site knew — the finished record's start, or the observed start
    for a live cancel — or None when the start was never observed, in
    which case the listing falls back to the bare name, exactly as a
    finished timer with no observed start does. Optional with a
    default, so records written before this burst load cleanly.
    """

    timer_id: str
    device_id: str | None
    event_type: str  # "finished" | "cancelled"
    enqueued_at: datetime
    direct_delivery_attempted: bool | None = None
    name: str = ""
    total_seconds: int = 0
    language: str = "en"
    is_finished: bool = False
    delivery_failed: bool = False
    sent_streak: int = 0
    attempt_count: int = 0
    last_attempt_at: datetime | None = None
    started_at: datetime | None = None


@dataclass(frozen=True)
class LiveSeen:
    """The last-seen live state of one served timer (§5.3, RB-R2-3).

    In-memory only, refreshed from ``tm.timers`` on every
    ``reconcile_finished`` call. A vanished id is evaluated exactly
    once, at the first sync that observes the vanish, and its entry is
    dropped then (evaluate-once-then-drop). ``finishes_at`` is None
    while paused (no finish moment) and is computed exactly as
    ``snapshot()`` computes it while active.
    """

    device_id: str | None
    name: str
    total_seconds: int
    language: str
    is_active: bool
    finishes_at: datetime | None
    started_at: datetime | None


@dataclass(frozen=True)
class _LastEvent:
    """The last event type recorded per timer id (§5.3, RB-R2-3).

    New state held in ``reconcile_finished``'s support code. Retained
    until the LATER of (a) its id's ``_live_seen`` entry being dropped
    and (b) the timer's nominal ``finishes_at`` passing — so a
    CANCELLED tap recorded for a handler-registered cancel is
    necessarily still present at the one evaluation that reads it.
    """

    event_type: str
    finishes_at: datetime | None


# ------------------------------------------------------------------ internals


def normalize_timer_name(name: str) -> str:
    """Lowercase a timer name and drop everything but letters and digits.

    Timer names come from speech, so "Pasta", "pasta" and "Pasta!" must all
    match the same timer. This mirrors how Home Assistant matches names
    when it looks a timer up by name.
    """
    return "".join(ch for ch in name.lower() if ch.isalnum())


def resolve_bridge_options(entry_options: dict[str, Any] | None) -> dict[str, Any]:
    """Resolve a config entry's options to the bridge's held options.

    Pure (no Home Assistant): missing keys resolve to the recorded
    defaults, and an unknown stored value falls back to its default
    rather than reaching the gate — a stale or hand-edited option can
    never silently change what the bridge does. The Q11/Q12 defaults
    are the SETTLED answers (never ring; never re-send); O1 defaults to
    unsettled until the §25 listen test settles it. There is no Q13
    key — Q13 is DEAD.

    The key strings + value literals here are the same literals as
    const.py (the canonical store — config_flow.py and __init__.py
    import them from there). They are repeated here, not relatively
    imported, because this module is also loaded standalone by the unit
    tests with no package context; test_bridge.py pins the two copies
    equal against const.py.

    Q11/Q12 clamp (critic F1): only the SETTLED literals are kept — a
    rejected literal already stored (pre-fix) resolves to the settled
    value at construction, so it can never linger in ``_queue_options``
    as if it were the current pick. Nothing reads the stored Q11/Q12
    values (only O1 and primary are read); the clamp is record hygiene.
    """
    options = entry_options or {}
    q11 = options.get("q11_ring_timing", "never_ring")
    if q11 != "never_ring":
        q11 = "never_ring"
    q12 = options.get("q12_auto_stop_resend", "never_resend")
    if q12 != "never_resend":
        q12 = "never_resend"
    o1 = options.get("o1_alarm_behaviour", "unsettled")
    if o1 not in ("unsettled", "rings_until_cancelled", "auto_stop"):
        o1 = "unsettled"
    primary = options.get("primary_satellite") or None
    if not isinstance(primary, str):
        primary = None
    return {
        "q11_ring_timing": q11,
        "q12_auto_stop_resend": q12,
        "o1_alarm_behaviour": o1,
        "primary_satellite": primary,
    }


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

    def __init__(
        self,
        hass: HomeAssistant,
        device_ids: list[str],
        options: dict[str, Any] | None = None,
    ) -> None:
        self._hass = hass
        self._devices = set(device_ids)
        # Bridge-held options (§5.3/§5.4, RB-R2-7): read at construction
        # from the config entry by async_setup_entry; None resolves to
        # the recorded defaults (Q11 settled never-ring, Q12 settled
        # never re-send, O1 unsettled, no primary) so the two-arg test
        # constructions keep working unchanged.
        self._queue_options = resolve_bridge_options(options)
        # Primary satellite (§5.4): the device a chip tap rings when it
        # names no satellite. None = unset (single-device houses resolve
        # by the single-device rule; multi-device houses error loudly).
        self._primary_satellite: str | None = self._queue_options[
            "primary_satellite"
        ]
        self._change_listeners: list[OnChange] = []
        self._running = False
        self._tap: _Tap | None = None
        # ONE store, one lifecycle owner (§5.1, RB-R2-1): a single dict
        # of TimerRecord, keyed by timer id, replacing the four parallel
        # stores on main (_finished / _restored / _started_at cache /
        # HA storage). It holds the timers the bridge itself owns:
        # finished timers (TimerManager popped them the moment they went
        # off — without this the dashboard would have nothing to show
        # for them) and timers restored from storage after a restart
        # (the TimerManager no longer has them, so the bridge is their
        # source of truth until they finish, are cancelled, or are
        # dismissed). Live timers are NOT held here: the TimerManager
        # owns them and snapshot() reads them from it. Persisted to HA
        # storage, so a finished or restored timer survives a restart.
        # State machine (§5.1, §17.7): active ↔ paused → finished →
        # dismissed → purged, cancelled terminal. Dismissed records are
        # removed from this dict; purged/cancelled are queue states
        # (pending-delivery records, RB-R2-2), not timer records.
        self._records: dict[str, TimerRecord] = {}
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
        # When each LIVE timer started (wall clock), recorded ONLY when
        # the bridge observes the start (§5.1): the STARTED tap for a
        # voice start, or the service call that created the timer. This
        # replaces the old _started_at first-sight cache, which was
        # populated by DERIVATION (finishes_at − duration) the first
        # time snapshot() saw a timer. An entry here is an observation,
        # never a derivation; a live timer with no entry has no observed
        # start and its snapshot omits started_at. Entries are dropped
        # when the timer finishes, is cancelled, dismissed or
        # restarted, so the map stays bounded by the live set. Held
        # records carry their own started_at field (from storage, or
        # from the restore call) and never consult this map.
        self._observed_started_at: dict[str, datetime] = {}
        # Pending-delivery records (§5.3, RB-R2-2): the queue. Keyed by
        # timer id (at most one pending record per id — a supersede
        # replaces rather than adds). Enqueue, drain, supersede, purge
        # and prune live here; the load/persist round-trip is the store
        # step's (RB-R2-1).
        self._pending: dict[str, PendingRecord] = {}
        # Reconciliation state (§5.3, RB-R2-3): the last-seen live state
        # of every served, non-conversation_command timer, and the
        # per-id last-event record. Both in-memory only; both are
        # cleared on stop (a stopped bridge observes nothing, and on
        # restart the load-time rules apply instead).
        self._live_seen: dict[str, LiveSeen] = {}
        self._last_event: dict[str, _LastEvent] = {}
        # O1 alarm behaviour (§5.3, §26 Q12(b) settled): None means
        # UNSETTLED (the §25 listen test has not run yet) — triggers 1
        # and 4 skip FINISHED records while unsettled. The settled
        # values are "rings_until_cancelled" and "auto_stop". RB-R2-7
        # wires this to the options flow at construction: it reads the
        # resolved option out of self._queue_options, so the load-time
        # condition is a read of bridge-held options, not a second
        # store. Under auto-stop, trigger 1 NEVER re-sends a false-flag
        # FINISHED (Q12(b) settled ~08:41 PST Nov 12 — never re-ring a
        # stopped alarm), so the only value that opens the unconditioned
        # FINISHED drains is "rings_until_cancelled".
        self._o1_alarm_behaviour: str | None = (
            None
            if self._queue_options["o1_alarm_behaviour"] == "unsettled"
            else self._queue_options["o1_alarm_behaviour"]
        )
        # Device → satellite-entity resolution (§5.3 H11, triggers 2
        # and 4): populated at setup-time resolution and updated on
        # entity-registry events. A served device with no (or an
        # ambiguous) satellite entity is absent here, and triggers 2
        # and 4 do not apply to it.
        self._device_entities: dict[str, str] = {}
        # Trigger subscriptions, all cancelled in stop().
        self._trigger4_unsub: Any | None = None
        self._state_change_unsubs: dict[str, Any] = {}
        self._registry_unsub: Any | None = None
        # Devices whose trigger-4 skip-while-unavailable has already
        # been logged once (the skip is logged once when it starts).
        self._trigger4_skip_logged: set[str] = set()
        # Devices whose entity resolution outcome (zero / ambiguous)
        # has already been logged once at setup.
        self._entity_resolution_logged: set[str] = set()
        # Whether the undelivered_timer_events repair issue is
        # currently open (in-memory mirror of the issue registry).
        self._repair_issue_open: bool = False
        # How many pending FINISHED records have been purged on an
        # unconfirmed "sent" (fix-burst F5 observability): every such
        # purge spends the record's one re-ring attempt on a send that
        # may have been silently dropped, with no retry and no repair
        # signal — the counter (and the warning logged per purge) is
        # the only trace that a first ring may never have happened.
        # In-memory only; it resets on restart.
        self._unconfirmed_finished_purges: int = 0
        # HA storage for persistence (BRIDGE-2). None until
        # async_load_persisted() runs — without it the bridge simply
        # keeps everything in memory (unit tests, or a store failure).
        self._store: Any | None = None
        # True while snapshot() is running. persist() raises when called
        # during a snapshot (§5.1 BUG-T1: a save issued during a snapshot
        # is a programming error, never silently dropped — the old
        # _schedule_save re-entrancy guard swallowed it).
        self._in_snapshot = False

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

    def set_devices(self, device_ids: list[str]) -> None:
        """Change which satellites are mirrored (takes effect on next sync)."""
        self._devices = set(device_ids)
        if self._running:
            # A live device-set swap re-resolves trigger 2/4 (§5.3 F11).
            self._resolve_device_entities()

    @property
    def devices(self) -> frozenset[str]:
        """Ids of the satellites this bridge mirrors."""
        return frozenset(self._devices)

    @property
    def primary_satellite(self) -> str | None:
        """The primary satellite device id (§5.4), or None when unset."""
        return self._primary_satellite

    async def start(self) -> bool:
        """Install the tap and start the queue. False if HA changed.

        Async (§5.3 F3): ``reconcile_finished`` runs here, before the
        re-assert loop, so a restored run-out in the load→start window
        is converted and enqueued before the startup trigger-1 drain
        fires. Order inside start() (§5.3 F1): the entity-registry bus
        listener is registered BEFORE setup-time resolution runs, then
        the trigger-4 interval, then the reconcile scan, then the
        re-assert loop. A sync start() calling the async reconcile
        without awaiting would silently drop the scan — that shape is
        excluded by this declaration.
        """
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
        # Registry listener first (§5.3 F1): a satellite entity
        # created after the listener is caught by the callback, one
        # created before it by the resolution below — no creation in
        # the window between resolution and listener registration is
        # missed. The listener is a plain function: HA helpers are
        # imported lazily, so a stub hass without them degrades to
        # no listener instead of failing start (unit tests).
        self._listen_entity_registry()
        # Setup-time device→entity resolution (triggers 2 and 4).
        self._resolve_device_entities()
        # Trigger 4: the 60 s periodic re-attempt, independent of the
        # sensor-tick gate (§5.3 trigger 4 — the sensor tick does not
        # run in exactly the case the queue exists for: a dismissed
        # timer with no non-finished timer left). The interval is
        # installed unconditionally and no-ops while the queue is
        # empty; it is cancelled in stop().
        self._start_trigger4_interval()
        # The _restored scan runs here, before the re-assert loop:
        # a restored run-out in the load→start window converts and
        # enqueues before the startup trigger-1 drain fires (§5.3).
        now = datetime.now(timezone.utc)
        await self.reconcile_finished(now)
        # Handler (re)registrations are observed through
        # _on_handler_registered — the trigger-1 drain point (§5.3).
        # It re-sends pending queue records only; it never re-asserts
        # a finished timer directly. (On main this loop re-announced
        # every undismissed finished timer on (re)registration
        # (BRIDGE-3); that queue-bypassing FINISHED path is deleted
        # here — a stored finished timer restores display-only and is
        # never re-announced, and a load-time run-out never rings (Q11
        # settled ~15:02 PST Nov 12).) The startup firing below is
        # trigger 1 at HA start: already-registered handlers drain
        # once here, and later (re)registrations drain through the
        # tap. The drain itself never purges, but every attempt it
        # makes mutates the queue's bookkeeping (round-2 B1), so the
        # drain helper reports whether the queue changed, and start()
        # awaits the persist when it did (a re-assert write landing
        # is the §5.1 site-(6) case).
        tap.handler_listeners.append(self._on_handler_registered)
        tm = timer_manager(self._hass)
        changed = False
        if tm is not None:
            for device_id in list(tm.handlers):
                if device_id in self._devices:
                    changed = (
                        self._drain_device(
                            device_id, purge=False, reason="trigger-1"
                        )
                        or changed
                    )
        if changed:
            await self.persist()
        # Repair-issue state after a restart: a qualifying record
        # loaded from storage re-opens the issue from its original
        # enqueued_at, and a record that purged while the bridge was
        # down clears a stale one (§5.3).
        self._evaluate_repair_issues(now)
        return True

    def stop(self) -> None:
        """Remove the tap and put the satellite handlers back as found."""
        if not self._running:
            return

        self._running = False

        # Cancel the queue's subscriptions first: no drain may fire
        # after the tap is gone.
        self._stop_trigger4_interval()
        for unsub in list(self._state_change_unsubs.values()):
            try:
                unsub()
            except Exception:
                pass
        self._state_change_unsubs.clear()
        if self._registry_unsub is not None:
            try:
                self._registry_unsub()
            except Exception:
                pass
            self._registry_unsub = None

        # A stopped bridge observes nothing: the in-memory reconcile
        # state is stale the moment the bridge is down (timers may
        # finish or cancel unobserved), so it is cleared, not kept.
        # On restart the load-time rules apply instead.
        self._live_seen.clear()
        self._last_event.clear()

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

    def snapshot(self, now: datetime) -> dict[str, TimerRecord]:
        """Current timers for the chosen satellites, keyed by timer id.

        PURE (§5.1 BUG-T1): read-only serialisation. It never mutates
        a record, never converts a restored run-out, never announces,
        never notifies, never saves. Live timers come from the
        TimerManager; finished and restored timers come from the
        bridge's own store (``_records``). A restored active timer
        whose ``finishes_at`` has passed is emitted as active with 0 s
        remaining — the conversion to finished lives in
        ``reconcile_finished`` (RB-R2-3), not here; the load-time case
        (a ``finishes_at`` already passed at load) is converted by
        ``async_load_persisted`` before the bridge ever runs. A save
        issued during a snapshot raises (see ``persist()``).
        """
        if not self._running:
            return {}

        self._in_snapshot = True
        try:
            return self._snapshot_locked(now)
        finally:
            self._in_snapshot = False

    def _snapshot_locked(self, now: datetime) -> dict[str, TimerRecord]:
        """The snapshot body; runs with ``_in_snapshot`` set."""
        tm = timer_manager(self._hass)
        out: dict[str, TimerRecord] = {}
        mono = time.monotonic_ns()

        for timer in list(tm.timers.values()):
            try:
                if (
                    getattr(timer, "conversation_command", None)
                    or timer.device_id not in self._devices
                ):
                    continue

                if not timer.is_active:
                    out[timer.id] = TimerRecord(
                        timer.id,
                        timer.name or "",
                        timer.device_id,
                        False,
                        timer.created_seconds,
                        timer.seconds_left,
                        None,
                        started_at=self._observed_started_at.get(timer.id),
                        started_via_service=timer.id in self._service_started,
                    )
                    continue

                finishes_at = self._finishes_at_for(timer, now, mono)
                out[timer.id] = TimerRecord(
                    timer.id,
                    timer.name or "",
                    timer.device_id,
                    True,
                    timer.created_seconds,
                    timer.seconds_left,
                    finishes_at,
                    started_at=self._observed_started_at.get(timer.id),
                    started_via_service=timer.id in self._service_started,
                )
            except (AttributeError, TypeError):
                _LOGGER.debug(
                    "Skipping a timer this version does not understand: %r", timer
                )

        # Held records (BRIDGE-2): the TimerManager does not have
        # them, so they only exist here. A finished record is emitted
        # as-is. An active restored timer is emitted with its
        # remaining time recomputed from finishes_at — a derived view,
        # not a mutation of the held record. A restored active timer
        # whose finishes_at has already passed is emitted as active
        # with 0 s left (see snapshot() — the conversion is
        # reconcile_finished's, RB-R2-3).
        for timer_id, record in list(self._records.items()):
            if record.device_id not in self._devices:
                continue
            if record.is_finished:
                out[timer_id] = record
            elif record.is_active and record.finishes_at is not None:
                remaining = max(
                    0,
                    int((record.finishes_at - now).total_seconds()),
                )
                out[timer_id] = dataclass_replace(
                    record, seconds_left=remaining
                )
            else:
                out[timer_id] = record

        return out

    @staticmethod
    def _finishes_at_for(timer: Any, now: datetime, mono_ns: int) -> datetime:
        """Wall-clock finish moment for a live timer.

        The same computation ``snapshot()`` uses (``updated_at`` +
        monotonic seconds left): reconcile's ``_live_seen`` entries
        carry the same ``finishes_at`` the snapshot would show, so the
        vanish condition (ii) reads the same instant.
        """
        left_ns = timer.updated_at + timer.seconds * 1_000_000_000 - mono_ns
        return now + timedelta(microseconds=max(0, left_ns) // 1000)

    async def reconcile_finished(self, now: datetime) -> None:
        """Learn no-handler finishes by reconciliation (§5.3, RB-R2-3).

        The ONLY no-tap finish detector. Host: the sensor manager's
        sync calls this BEFORE ``snapshot()`` on every sync, and
        ``start()`` awaits it before the re-assert loop. It observes
        two things ``snapshot()`` cannot: live timers vanishing from
        ``tm.timers`` with no handler registered, and restored timers
        whose ``finishes_at`` passes after load. It never lives inside
        ``snapshot()``, which stays pure (BUG-T1).

        Two scans:
        (a) the ``_live_seen`` vanish scan — a served timer absent
        from ``tm.timers`` is a finish iff all five conditions hold:
        (i) last seen ACTIVE as of the last sensor sync, (ii)
        last-seen ``finishes_at`` has passed, (iii) no CANCELLED tap
        recorded for the id since last seen, (iv) the id is not
        already finished in ``_records``, (v) the device is served
        and the timer is not a ``conversation_command`` timer. The
        finished record takes ``finished_at`` = the last-seen
        ``finishes_at`` (the true finish moment, not the sync time)
        and ``started_at`` only if observed, and a FINISHED record is
        enqueued with ``direct_delivery_attempted = false`` — no
        handler was registered, so no direct delivery ever happened.
        Evaluate-once-then-drop: the entry is dropped at the first
        sync that observes the vanish, whether it classified as a
        finish or a cancel.
        (b) the ``_restored`` scan — a held active record whose
        ``finishes_at`` has passed converts exactly as the load-time
        conversion does (``finished_at`` = the passed
        ``finishes_at``), and a flag-``false`` FINISHED record is
        enqueued. This is a POST-LOAD conversion: the finish happened
        while the system was up, so it keeps the §5.3 once-only
        re-send (coordinator scope note + §27 FAIL-1) — it is NOT the
        settled-Q11 load-time run-out, which ``async_load_persisted``
        converts with NO enqueue. See r2-queue-implementation-log.md
        §0 for the plan-clause conflict recorded against this line.

        False positives are the architecture's named six (a)–(f);
        the disclosed same-tick pause+cancel case (e) is expected to
        enqueue here. After conversions by either scan, site-(2)
        ``persist()`` writes them (§5.1).
        """
        tm = timer_manager(self._hass)
        if tm is None:
            return
        mono = time.monotonic_ns()
        converted = 0

        # (a) Refresh _live_seen from tm.timers, then evaluate vanishes.
        for timer in list(tm.timers.values()):
            try:
                if (
                    getattr(timer, "conversation_command", None)
                    or timer.device_id not in self._devices
                ):
                    continue
                is_active = bool(timer.is_active)
                self._live_seen[timer.id] = LiveSeen(
                    device_id=timer.device_id,
                    name=getattr(timer, "name", "") or "",
                    total_seconds=getattr(timer, "created_seconds", 0) or 0,
                    language=getattr(timer, "language", "en") or "en",
                    is_active=is_active,
                    finishes_at=(
                        self._finishes_at_for(timer, now, mono)
                        if is_active
                        else None
                    ),
                    started_at=self._observed_started_at.get(timer.id),
                )
            except (AttributeError, TypeError):
                _LOGGER.debug(
                    "Skipping a timer reconcile does not understand: %r",
                    timer,
                )
        for timer_id in list(self._live_seen):
            if timer_id in tm.timers:
                continue
            seen = self._live_seen.pop(timer_id)
            last = self._last_event.get(timer_id)
            already_finished = (
                timer_id in self._records
                and self._records[timer_id].is_finished
            )
            if (
                seen.is_active
                and seen.finishes_at is not None
                and now >= seen.finishes_at
                and (last is None or last.event_type != "cancelled")
                and not already_finished
                and seen.device_id in self._devices
            ):
                finished_at = seen.finishes_at
                started_at = self._observed_started_at.pop(timer_id, None)
                self._records[timer_id] = TimerRecord(
                    timer_id,
                    seen.name,
                    seen.device_id,
                    False,
                    seen.total_seconds,
                    0,
                    None,
                    is_finished=True,
                    finished_at=finished_at,
                    language=seen.language,
                    started_at=started_at if started_at is not None else seen.started_at,
                    started_via_service=timer_id in self._service_started,
                )
                self._enqueue(
                    PendingRecord(
                        timer_id=timer_id,
                        device_id=seen.device_id,
                        event_type="finished",
                        enqueued_at=now,
                        direct_delivery_attempted=False,
                        name=seen.name,
                        total_seconds=seen.total_seconds,
                        language=seen.language,
                        is_finished=True,
                        started_at=(
                            started_at
                            if started_at is not None
                            else seen.started_at
                        ),
                    )
                )
                converted += 1

        # (b) _restored scan: held active records whose finishes_at
        # has passed since load. (The load-time run-out is already
        # converted by async_load_persisted with NO enqueue — see its
        # docstring — so any active record reaching this scan had a
        # finishes_at in the future at load and ran out while the
        # system was up: the post-load case, flag-false enqueue.)
        # A record whose id is live in the TimerManager is never
        # converted here: the live timer is authoritative.
        for timer_id, record in list(self._records.items()):
            if (
                record.is_active
                and not record.is_finished
                and record.finishes_at is not None
                and record.finishes_at <= now
                and record.device_id in self._devices
                and timer_id not in tm.timers
            ):
                self._records[timer_id] = dataclass_replace(
                    record,
                    is_active=False,
                    seconds_left=0,
                    finishes_at=None,
                    is_finished=True,
                    finished_at=record.finishes_at,
                )
                self._enqueue(
                    PendingRecord(
                        timer_id=timer_id,
                        device_id=record.device_id,
                        event_type="finished",
                        enqueued_at=now,
                        direct_delivery_attempted=False,
                        name=record.name,
                        total_seconds=record.total_seconds,
                        language=record.language,
                        is_finished=True,
                        started_at=record.started_at,
                    )
                )
                converted += 1

        # Prune per-id last-event records: retained until the LATER
        # of the _live_seen entry being dropped and the nominal
        # finishes_at passing. (The evaluation above already ran, so
        # a record whose finishes_at has passed is safe to drop.)
        for timer_id in list(self._last_event):
            if timer_id in self._live_seen or timer_id in tm.timers:
                continue
            last = self._last_event[timer_id]
            if last.finishes_at is None or now >= last.finishes_at:
                del self._last_event[timer_id]

        if converted:
            await self.persist()

    def finished_timers(self) -> list[TimerRecord]:
        """Finished timers still awaiting dismissal, oldest first."""
        return sorted(
            (s for s in self._records.values()
             if s.is_finished and s.device_id in self._devices),
            key=lambda s: s.finished_at or datetime.min.replace(tzinfo=timezone.utc),
        )

    def restored_snapshot(self, timer_id: str) -> TimerRecord | None:
        """A timer restored from storage (BRIDGE-2), if it is still held.

        Only a held, not-finished record counts as restored: a finished
        record is finished, not restorable, and a live timer is the
        TimerManager's, not the bridge's.
        """
        record = self._records.get(timer_id)
        if record is None or record.is_finished:
            return None
        return record

    def has_restored(self, timer_id: str) -> bool:
        """True when the bridge holds a restored timer with this id."""
        return self.restored_snapshot(timer_id) is not None

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
        # The STARTED tap fires synchronously inside tm.start_timer and
        # records the observed start (§5.1). This is belt-and-braces
        # for paths where the tap never ran (the bridge was never
        # started): the service call itself is an observation of the
        # start, so the call time is recorded, never derived.
        self._observed_started_at.setdefault(
            timer_id, datetime.now(timezone.utc)
        )
        self._notify_listeners()
        return timer_id

    # ------------------------------------------------------- undo restore
    #
    # D6: Undo on a cancelled or dismissed timer puts the timer back
    # with its REMAINING time (not the full duration), in the state it
    # was in (a paused timer comes back paused). There is no
    # card-side way to do that atomically: re-create-then-pause through
    # the public services leaves a window where the 1 s sensor tick
    # can observe the restored timer as active. So the bridge does it
    # in one call, with no await between start and pause — inside one
    # event-loop turn nothing else can observe the intermediate state.

    def restore_timer(
        self,
        device_id: str,
        name: str,
        seconds_left: int,
        paused: bool,
        language: str = "en",
    ) -> str:
        """Re-create a cancelled timer with its remaining time (D6 Undo).

        ``seconds_left`` is the time left when it was cancelled (min
        1 s); a paused timer comes back paused, a running one comes
        back running. The new timer gets a NEW id (ids never repeat)
        and is marked service-started, so the timer-jump automation
        does not fire for it. Its started_at is the restore time:
        the live snapshot derives started_at from the new start, so
        an original start time passed in would be overwritten on the
        first tick (a restored timer re-appeared now — that is the
        honest value, and it still distinguishes duplicate names).
        Raises ValueError for a bad device, duration or pause failure.
        """
        if seconds_left < 1:
            raise ValueError("Duration must be at least 1 second")
        if device_id not in self._devices:
            raise ValueError(f"Device {device_id} is not a configured satellite")

        tm = timer_manager(self._hass)
        if tm is None:
            raise ValueError("Assist timer manager is not available")

        hours, remainder = divmod(seconds_left, 3600)
        mins, secs = divmod(remainder, 60)
        # Same ordering as start_timer (BRIDGE-1): HA fires STARTED
        # synchronously inside start_timer, so the in-flight flag marks
        # the new id in _event before any listener reads a snapshot.
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
        # As in start_timer: the STARTED tap records the observed start;
        # this is belt-and-braces for the no-tap case.
        self._observed_started_at.setdefault(
            timer_id, datetime.now(timezone.utc)
        )
        if paused:
            try:
                tm.pause_timer(timer_id)
            except Exception:
                # A half-restored timer (running when it should be
                # paused) is worse than a failed Undo: drop the new
                # timer and fail loudly, so nothing is left in an
                # unexpected state.
                try:
                    tm.cancel_timer(timer_id)
                except Exception:
                    pass
                self._service_started.discard(timer_id)
                self._observed_started_at.pop(timer_id, None)
                raise ValueError(
                    f"Could not pause restored timer {timer_id}"
                ) from None
        self._notify_listeners()
        return timer_id

    def restore_finished(
        self,
        device_id: str,
        name: str,
        total_seconds: int,
        finished_at: datetime,
        started_via_service: bool,
        language: str = "en",
        started_at: datetime | None = None,
    ) -> str:
        """Put back a dismissed finished timer, WITHOUT re-ringing (D6).

        Inserts a finished record directly with a NEW timer id — no
        announcement helper is called: the satellite was already told
        to stop ringing when the timer was dismissed, and Undo must not
        ring it again. ``finished_at`` is preserved, so "Done — N min ago"
        is identical to before the dismiss. ``started_at`` is whatever
        the caller passes, and is otherwise OMITTED — never derived as
        ``finished_at − duration`` (§5.1): an Undo has no observed
        start, and a derived start would be a fabrication (the
        finished_at − duration identity is only true for a timer that
        ran its full length without pausing). The record restores as
        finished and is SHOWN, never announced: it enqueues NO pending
        record (Q11 settled ~15:02 PST Nov 12 — a timer that finished
        while the system was down never rings). It also does NOT touch
        ``self._pending``: any CANCELLED pending for the OLD id (item
        23, RB-R2-10 decision: CARRY, disclosed) stays in the queue —
        the restore is a cross-id operation (new id) and the §5.3
        supersede rule applies per timer id, so there is nothing to
        supersede; the pending CANCELLED keeps retrying on the
        trigger-4 backoff cadence as a duplicate stop for an alarm
        that already stopped at dismiss (a no-op at the satellite),
        and can be re-sent at once with ``redismiss_timer`` for the
        OLD id — re-dismissing the restored timer sends CANCELLED for
        the NEW id, which cannot stop the satellite's ringing for the
        OLD id. Returns the new timer
        id. Raises ValueError for a bad device, duration or missing
        finished_at.
        """
        if total_seconds < 1:
            raise ValueError("Duration must be at least 1 second")
        if device_id not in self._devices:
            raise ValueError(f"Device {device_id} is not a configured satellite")
        if finished_at is None:
            raise ValueError("finished_at is required")

        timer_id = uuid.uuid4().hex
        snap = TimerRecord(
            timer_id,
            name,
            device_id,
            False,
            total_seconds,
            0,
            None,
            is_finished=True,
            finished_at=finished_at,
            language=language,
            started_at=started_at,
            started_via_service=started_via_service,
        )
        self._records[timer_id] = snap
        if started_via_service:
            self._service_started.add(timer_id)
        self._notify_listeners()
        return timer_id

    # ------------------------------------------- restored-timer controls
    #
    # After a restart the TimerManager no longer has these timers, so the
    # bridge itself pauses, resumes, cancels and extends them (BRIDGE-2).
    # Each method raises KeyError for a timer the bridge does not hold as
    # restored, and ValueError for a state that cannot take the action.

    def pause_restored(self, timer_id: str) -> TimerRecord:
        """Pause a restored active timer; the remaining time freezes."""
        snap = self._records.get(timer_id)
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
        self._records[timer_id] = updated
        self._notify_listeners()
        return updated

    def resume_restored(self, timer_id: str) -> TimerRecord:
        """Resume a restored paused timer; it finishes remaining later."""
        snap = self._records.get(timer_id)
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
        self._records[timer_id] = updated
        self._notify_listeners()
        return updated

    def cancel_restored(self, timer_id: str) -> TimerRecord:
        """Cancel a restored timer and drop its record.

        The satellite is told CANCELLED, best effort: if it still holds
        a countdown for this timer, that stops it; if it holds nothing,
        the event is harmless. The CANCELLED is enqueued BEFORE the
        record is dropped (§5.3) and delivery is attempted at once via
        ``_send_to_satellite``; the attempt does not purge. The
        handler awaits persist() before returning (site (3)).
        """
        snap = self._records.get(timer_id)
        if snap is None:
            raise KeyError(timer_id)
        self._enqueue(
            PendingRecord(
                timer_id=snap.timer_id,
                device_id=snap.device_id,
                event_type="cancelled",
                enqueued_at=datetime.now(timezone.utc),
                name=snap.name,
                total_seconds=snap.total_seconds,
                language=snap.language,
                is_finished=snap.is_finished,
                started_at=snap.started_at,
            )
        )
        # No await between the get and the pop, so the record cannot
        # disappear in between — the pop below always finds it.
        self._records.pop(timer_id, None)
        self._service_started.discard(timer_id)
        outcome = self._send_to_satellite(snap, "cancelled")
        # The immediate send counts as attempt 1 for the trigger-4
        # backoff (F2).
        self._record_attempt(timer_id)
        if outcome != "sent":
            pending = self._pending.get(timer_id)
            if pending is not None:
                self._pending[timer_id] = dataclass_replace(
                    pending, delivery_failed=True
                )
        self._notify_listeners()
        return snap

    def add_time_restored(self, timer_id: str, seconds: int) -> TimerRecord:
        """Add seconds to a restored timer (active or paused)."""
        snap = self._records.get(timer_id)
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
        self._records[timer_id] = updated
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
        """Load persisted timers into _records (and pending records into _pending).

        Returns how many timer records were restored. Timers that are
        live in the TimerManager again win over their stored record (an
        entry reload, as opposed to a restart, keeps the live timers).
        A stored active timer whose finishes_at has passed restores as
        finished — it went off while Home Assistant was down — and is
        shown, never announced: no pending record is enqueued for it
        (Q11 settled ~15:02 PST Nov 12), and the converted record is
        persisted before returning (persist site (1), §5.1).

        RB-R2-9: every record is processed inside its own try/except —
        a corrupt record is logged with its id and skipped, and every
        record after it still loads. The caller never reports
        ``restored = 0`` for a partial load. The same guard covers the
        pending-events records.
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
        converted = False
        unknown_devices: dict[str, list[str]] = {}
        for record in data.get("timers", []) or []:
            try:
                snap = self._snapshot_from_record(record, now)
            except Exception:
                record_id = (
                    record.get("timer_id") if isinstance(record, dict) else None
                )
                _LOGGER.warning(
                    "Skipping corrupt persisted timer %s: %r",
                    record_id,
                    record,
                    exc_info=True,
                )
                continue
            if snap is None or snap.timer_id in live_ids:
                continue
            if (
                isinstance(record, dict)
                and record.get("state") == "active"
                and snap.is_finished
            ):
                # Load-time run-out: the stored record said active and
                # the load converted it to finished. The store must
                # carry the converted record before returning.
                converted = True
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
            self._records[snap.timer_id] = snap
            if snap.started_via_service:
                self._service_started.add(snap.timer_id)
            restored += 1
        # Pending-delivery records (§5.3): a v3-era store has no
        # pending_events key — that is a clean empty queue, not an
        # error. Each record has the same per-record guard as the
        # timer records.
        for record in data.get("pending_events", []) or []:
            try:
                pending = self._pending_from_record(record)
            except Exception:
                record_id = (
                    record.get("timer_id") if isinstance(record, dict) else None
                )
                _LOGGER.warning(
                    "Skipping corrupt pending event %s: %r",
                    record_id,
                    record,
                    exc_info=True,
                )
                continue
            if pending is None:
                _LOGGER.warning(
                    "Skipping malformed pending event: %r", record
                )
                continue
            self._pending[pending.timer_id] = pending
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
        if converted:
            await self.persist()
        return restored

    @staticmethod
    def _pending_from_record(record: Any) -> PendingRecord | None:
        """Rebuild a pending-delivery record, or None if malformed.

        A malformed record is skipped, never stored: a FINISHED record
        without a boolean ``direct_delivery_attempted`` flag cannot be
        drained correctly (§5.3 F2), and a CANCELLED record carries no
        flag at all, so a flag on one is a shape violation. A missing
        or unparseable ``enqueued_at`` is likewise malformed.
        """
        if not isinstance(record, dict):
            return None
        timer_id = record.get("timer_id")
        if not timer_id or not isinstance(timer_id, str):
            return None
        event_type = record.get("event_type")
        if event_type not in ("finished", "cancelled"):
            return None
        enqueued_at = record.get("enqueued_at")
        if not enqueued_at or not isinstance(enqueued_at, str):
            return None
        try:
            parsed = datetime.fromisoformat(enqueued_at)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        flag = record.get("direct_delivery_attempted")
        if event_type == "finished":
            if not isinstance(flag, bool):
                return None
        elif flag is not None:
            return None
        device_id = record.get("device_id")
        if device_id is not None and not isinstance(device_id, str):
            return None
        # Send-shape fields (RB-R2-2): optional with defaults, so
        # records written by the store step load cleanly.
        name = record.get("name", "")
        if not isinstance(name, str):
            return None
        total_seconds = record.get("total_seconds", 0)
        if not isinstance(total_seconds, int) or isinstance(
            total_seconds, bool
        ):
            return None
        language = record.get("language", "en")
        if not isinstance(language, str):
            return None
        is_finished = record.get("is_finished", False)
        if not isinstance(is_finished, bool):
            return None
        delivery_failed = record.get("delivery_failed", False)
        if not isinstance(delivery_failed, bool):
            return None
        # Hysteresis + backoff state (fix-burst F2/F3): optional with
        # defaults, so records written before this burst load cleanly
        # (streak 0, no attempts yet — a legacy record is due at once).
        sent_streak = record.get("sent_streak", 0)
        if not isinstance(sent_streak, int) or isinstance(
            sent_streak, bool
        ) or sent_streak < 0:
            return None
        attempt_count = record.get("attempt_count", 0)
        if not isinstance(attempt_count, int) or isinstance(
            attempt_count, bool
        ) or attempt_count < 0:
            return None
        last_attempt_at = record.get("last_attempt_at")
        if last_attempt_at is not None:
            if not isinstance(last_attempt_at, str):
                return None
            try:
                last_attempt = datetime.fromisoformat(last_attempt_at)
            except ValueError:
                return None
            if last_attempt.tzinfo is None:
                last_attempt = last_attempt.replace(tzinfo=timezone.utc)
        else:
            last_attempt = None
        # Observed start (round-2 B5): optional, so records written
        # before this burst load cleanly with started_at None.
        started_at = record.get("started_at")
        if started_at is not None:
            if not isinstance(started_at, str):
                return None
            try:
                started = datetime.fromisoformat(started_at)
            except ValueError:
                return None
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
        else:
            started = None
        return PendingRecord(
            timer_id=timer_id,
            device_id=device_id,
            event_type=event_type,
            enqueued_at=parsed,
            direct_delivery_attempted=flag,
            name=name,
            total_seconds=total_seconds,
            language=language,
            is_finished=is_finished,
            delivery_failed=delivery_failed,
            sent_streak=sent_streak,
            attempt_count=attempt_count,
            last_attempt_at=last_attempt,
            started_at=started,
        )

    @staticmethod
    def _unknown_record_is_stale(snap: TimerRecord, now: datetime) -> bool:
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
        for timer_id, snap in list(self._records.items()):
            if snap.device_id in self._devices:
                continue
            if self._unknown_record_is_stale(snap, now):
                del self._records[timer_id]
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
        keeps MORE than the sensors: every held record, for any device
        (P8-1). A timer whose device id changed (re-pair, HA restore)
        is preserved in storage across saves instead of being silently
        erased on the next save; it surfaces as a sensor again as soon
        as its device is configured. Stale unknown-device records past
        the retention bound are pruned first (see
        _prune_stale_unknown_records). Pending-delivery records (§5.3)
        are stored alongside the timers under ``pending_events`` — the
        queue round-trips through the same store file.
        """
        self._prune_stale_unknown_records(now)
        snaps = dict(self.snapshot(now))
        for timer_id, snap in self._records.items():
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
        pending = [
            {
                "timer_id": record.timer_id,
                "device_id": record.device_id,
                "event_type": record.event_type,
                "enqueued_at": record.enqueued_at.isoformat(),
                "direct_delivery_attempted": record.direct_delivery_attempted,
                "name": record.name,
                "total_seconds": record.total_seconds,
                "language": record.language,
                "is_finished": record.is_finished,
                "delivery_failed": record.delivery_failed,
                "sent_streak": record.sent_streak,
                "attempt_count": record.attempt_count,
                "last_attempt_at": (
                    record.last_attempt_at.isoformat()
                    if record.last_attempt_at is not None
                    else None
                ),
                "started_at": (
                    record.started_at.isoformat()
                    if record.started_at is not None
                    else None
                ),
            }
            for record in self._pending.values()
        ]
        return {"version": 1, "timers": timers, "pending_events": pending}

    async def persist(self) -> None:
        """Write the current store to HA storage, explicitly (§5.1).

        This is the ONLY way the store reaches disk: callers await it
        at the persist sites (load-time conversion, service/mutation
        paths, queue drains, re-assert — see §5.1 B-2) instead of
        relying on a background save. A call during a snapshot raises
        RuntimeError (BUG-T1): a save issued while a snapshot is being
        built is a programming error, and silently dropping it would
        hide a lost write. Without a store the bridge keeps everything
        in memory (unit tests, or a store failure) and this is a no-op.
        A save failure is logged, never raised — a storage hiccup must
        not break the dashboard action that triggered the save.
        """
        if self._in_snapshot:
            raise RuntimeError(
                "persist() called during snapshot(): a save issued "
                "while a snapshot is being built is a programming "
                "error (BUG-T1)"
            )
        if self._store is None:
            return
        try:
            data = self._persisted_data(datetime.now(timezone.utc))
        except Exception:
            _LOGGER.warning("Could not build persisted timers", exc_info=True)
            return
        try:
            await self._store.async_save(data)
        except Exception:
            _LOGGER.warning("Could not save persisted timers", exc_info=True)

    @staticmethod
    def _snapshot_from_record(
        record: dict, now: datetime
    ) -> TimerRecord | None:
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
            # started_at is whatever the store carries, or omitted —
            # never derived as finished_at − duration (§5.1).
            return TimerRecord(
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
            return TimerRecord(
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
                # timer, restored as finished (TIMER-12). It is shown,
                # never announced — no pending record is enqueued for
                # it (Q11 settled ~15:02 PST Nov 12); the caller
                # persists the converted record (persist site (1)).
                return TimerRecord(
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
            return TimerRecord(
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

    def dismiss_timer(self, timer_id: str) -> tuple[TimerRecord, str]:
        """Drop a finished timer's record so its sensor disappears.

        Dismissing also tells the satellite to stop ringing: the
        satellite only stops its alarm when its timer handler hears
        a CANCELLED event, and nothing else sends one for a timer
        that already finished (Home Assistant pops it from the
        TimerManager the moment it goes off). Clearing only the
        bridge's record leaves the satellite ringing.

        Returns ``(finished_record, outcome)``: the outcome is the
        immediate send's result ("sent" / "no-handler" / ..., see
        ``_send_to_satellite``), returned directly so the caller —
        the voice reply — reports THIS dismiss's outcome. It is
        deliberately not kept on the bridge (round-2 B6): a
        bridge-global "last outcome" read after an ``await`` lets a
        concurrent dismiss on another satellite overwrite the field,
        so one handler reports another's outcome.

        Durability is ordering, not a durability guarantee: the
        enqueue reaches storage before the dismiss returns ONLY on
        paths that await ``persist()`` afterwards (the service path
        and the voice path) — and ``persist()`` logs a save failure
        instead of raising, per its contract, so a failed save still
        returns success with no signal to the caller (round-2 B7).
        This method itself is sync and persists nothing.

        Raises KeyError when there is no such timer at all, and ValueError
        when the timer is still running (cancel it instead — a running
        timer has not finished, so there is nothing to dismiss).

        FOLLOW-UP (fix-burst F4, not built here): dismissing drops the
        sensor at once, so the dashboard gives no sign the stop is
        still retrying — no "dismissing" sensor state, and no
        re-dismiss button on the card. The bridge-level half is built
        (``pending_cancelled`` / ``redismiss_timer`` + the
        ``retry_dismiss`` service + voice re-dismiss), but the sensor
        "dismissing" state and its card affordance remain a follow-up
        — the retry is automatic and invisible until then.
        """
        finished = self._records.pop(timer_id, None)
        if finished is not None:
            self._service_started.discard(timer_id)
            # Enqueue BEFORE dropping the display record (§5.3): the
            # CANCELLED is a durable pending-delivery record, and the
            # immediate send is attempted at once — but the attempt is
            # NOT a liveness signal and does NOT purge (a Wyoming
            # silent drop returns cleanly while the event never
            # arrives). The record stays pending until the 24 h prune
            # removes it — no drain purges a CANCELLED on "sent" (B1:
            # "sent" is not delivery confirmation), and supersede is
            # not an exit for a finished id (F2). On durability, see
            # the docstring: ordering-only on paths that await
            # persist() afterwards; this method itself is sync and
            # persists nothing.
            # The finished record's observed start is carried on the
            # pending record (round-2 B5), so the re-dismiss listing
            # can still tell same-name timers apart after the
            # finished record is gone.
            self._enqueue(
                PendingRecord(
                    timer_id=finished.timer_id,
                    device_id=finished.device_id,
                    event_type="cancelled",
                    enqueued_at=datetime.now(timezone.utc),
                    name=finished.name,
                    total_seconds=finished.total_seconds,
                    language=finished.language,
                    is_finished=True,
                    started_at=finished.started_at,
                )
            )
            outcome = self._stop_satellite_ringing(finished)
            # The immediate send counts as attempt 1 for the trigger-4
            # backoff (F2), so the first re-send waits 60 s.
            self._record_attempt(timer_id)
            if outcome != "sent":
                pending = self._pending.get(timer_id)
                if pending is not None:
                    self._pending[timer_id] = dataclass_replace(
                        pending, delivery_failed=True
                    )
            self._notify_listeners()
            return finished, outcome

        tm = timer_manager(self._hass)
        if tm is not None and timer_id in tm.timers:
            raise ValueError(f"Timer {timer_id} is still running")
        raise KeyError(timer_id)

    def pending_cancelled(self) -> list[TimerRecord]:
        """Send snapshots for every pending CANCELLED record (F4).

        The voice re-dismiss path and the re-dismiss service list
        these: after a dismiss the finished record is gone, so the
        pending CANCELLED is the only thing left that names the timer
        — without this there is no way to say "stop it again". Sorted
        by enqueue time, oldest first, so listings are stable.
        """
        return [
            self._snap_for_pending(record)
            for record in sorted(
                self._pending.values(), key=lambda r: r.enqueued_at
            )
            if record.event_type == "cancelled"
        ]

    def redismiss_timer(self, timer_id: str) -> str:
        """Re-send CANCELLED for a pending id, without a finished record (F4).

        The re-dismiss / retry affordance: after a dismiss the
        finished record is gone, so ``dismiss_timer`` raises KeyError
        for the id — but the pending CANCELLED is still retrying, and
        this re-sends it NOW instead of waiting for the trigger-4
        backoff. Returns the send outcome (see ``_send_to_satellite``)
        so the caller can surface an observably-failed send. Raises
        KeyError when no CANCELLED is pending for the id (nothing to
        re-send — including when only a FINISHED is pending, which is
        a ringing timer, not a dismissed one). The record stays
        pending either way: a re-send never purges (B1). The attempt
        counts toward the backoff clock and the delivery_failed
        hysteresis, like any drain re-send. The caller persists
        afterwards (service site (3) / intent handler, F1): the
        attempt bookkeeping must survive a restart like the record.
        """
        pending = self._pending.get(timer_id)
        if pending is None or pending.event_type != "cancelled":
            raise KeyError(timer_id)
        return self._attempt_send_for_pending(pending, "cancelled")

    def _stop_satellite_ringing(self, finished: TimerRecord) -> str:
        """Tell the satellite its finished timer is over, so it stops ringing.

        The satellite's timer handler is what starts the alarm when it
        hears FINISHED, and only a CANCELLED event for the same timer
        stops it. Home Assistant never sends one for a timer that
        already finished, so the bridge sends it here. A failure to reach
        the satellite never breaks the dismiss itself (the sensor is
        already cleared) — it is logged instead. Returns the send
        outcome (see ``_send_to_satellite``).
        """
        return self._send_to_satellite(finished, "cancelled")

    def _on_handler_registered(self, device_id: str) -> None:
        """A satellite (re)registered its timer handler — trigger 1 (§5.3).

        The trigger-1 drain: re-sends the pending queue records for
        that device only (FINISHED only when the flag is ``false``
        AND O1 settles "rings until cancelled" — until O1 is measured,
        trigger 1 skips FINISHED records; under auto-stop it NEVER
        re-sends a ``false``-flag FINISHED (Q12(b) settled)). It never
        iterates ``finished_timers()`` and never re-asserts a restored
        finished timer with no queue record. It does NOT purge:
        registration precedes connection, so it proves nothing about
        reachability. It also does NOT fire on a plain network
        reconnect — the handler stays registered across a blip — so
        trigger 1 cannot be the reconnect drain for a blip. A persist
        is issued only when a drain write landed in this registration
        (§5.1 site (6)); any drain attempt mutates the queue's
        bookkeeping (round-2 B1), so a drain that re-sent owes a
        persist, not only one that purged or flipped the mark.
        """
        if device_id not in self._devices:
            return
        changed = self._drain_device(device_id, purge=False, reason="trigger-1")
        if changed:
            create_task = getattr(self._hass, "async_create_task", None)
            if create_task is not None:
                try:
                    create_task(self.persist())
                except Exception:
                    _LOGGER.warning(
                        "Could not save persisted timers", exc_info=True
                    )

    def _send_to_satellite(self, snap: TimerRecord, event_type: str) -> str:
        """Send one timer event to a satellite's ORIGINAL handler.

        ``event_type`` is "cancelled" (stop ringing / stop a countdown)
        or "finished" (start ringing). The original handler is called
        directly, bypassing the tap: the bridge already knows about
        this timer, and going through the tap would re-record it.
        Failures are logged, never raised — a satellite that cannot be
        reached must not break the dashboard action.

        Returns the send outcome, so the queue can tell an
        observably-impossible delivery from a call that reached the
        handler: "sent" (the handler was invoked — which, for Wyoming,
        is still consistent with a silent drop inside the handler,
        §5.3), "no-device-id", "import-error", "no-timer-manager",
        "no-handler", or "handler-raised". The four bare returns are
        all logged warnings with timer id + reason (on main three of
        them were silent); a handler-raise is logged too. A "sent" is
        NOT delivery confirmation — there is no confirmation signal
        (§5.3); it only means the attempt reached the handler.
        """
        if snap.device_id is None:
            _LOGGER.warning(
                "Could not tell the satellite for timer %s about %s: "
                "no device id",
                snap.timer_id,
                event_type,
            )
            return "no-device-id"
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
            return "import-error"

        tm = timer_manager(self._hass)
        if tm is None:
            _LOGGER.warning(
                "Could not tell the satellite for timer %s about %s: "
                "no TimerManager",
                snap.timer_id,
                event_type,
            )
            return "no-timer-manager"
        wrapped = tm.handlers.get(snap.device_id)
        if wrapped is None:
            _LOGGER.warning(
                "Could not tell the satellite for timer %s about %s: "
                "no timer handler registered for device %s",
                snap.timer_id,
                event_type,
                snap.device_id,
            )
            return "no-handler"
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
            return "handler-raised"
        return "sent"

    # ------------------------------------------------------- queue (§5.3)

    def _enqueue(self, record: PendingRecord) -> bool:
        """Add a pending-delivery record, applying the supersede rule.

        A newer event for the same timer id replaces the pending one —
        a CANCELLED supersedes a pending FINISHED for the same id; a
        FINISHED never supersedes a CANCELLED, because a cancelled
        timer id is terminal (a restart creates a new id). Returns
        True when the record was stored, False when it was dropped by
        the supersede rule (logged with timer id + reason).
        """
        existing = self._pending.get(record.timer_id)
        if existing is not None:
            if (
                existing.event_type == "cancelled"
                and record.event_type == "finished"
            ):
                _LOGGER.info(
                    "Dropping pending FINISHED for timer %s: a CANCELLED "
                    "is already pending for the id (FINISHED never "
                    "supersedes CANCELLED)",
                    record.timer_id,
                )
                return False
            if existing.event_type != record.event_type:
                _LOGGER.info(
                    "Pending %s for timer %s supersedes pending %s",
                    record.event_type,
                    record.timer_id,
                    existing.event_type,
                )
        self._pending[record.timer_id] = record
        return True

    def _snap_for_pending(self, record: PendingRecord) -> TimerRecord:
        """The send snapshot for a pending record.

        The live timer record wins when it still exists (a FINISHED
        record stays held until dismissed); otherwise the send shape
        captured on the pending record at enqueue time is used (a
        dismissed timer's record is gone, but the satellite still
        needs the stop for its id).
        """
        held = self._records.get(record.timer_id)
        if held is not None:
            return held
        return TimerRecord(
            record.timer_id,
            record.name,
            record.device_id,
            False,
            record.total_seconds,
            record.total_seconds,
            None,
            is_finished=record.is_finished,
            language=record.language,
            started_at=record.started_at,
        )

    def _finished_may_resend(
        self, record: PendingRecord, *, purge_drain: bool
    ) -> bool:
        """Whether a drain may re-send a pending FINISHED record (§5.3).

        A ``true``-flag record is NEVER re-sent by any trigger: at
        enqueue time the satellite had already heard FINISHED if it
        was up, and a re-send restarts its ringing window or rings an
        alarm the firmware already stopped. A ``false``-flag record
        re-sends on a liveness-signalled (purge) drain regardless of
        O1 — the channel was just proven live and the attempt purges.
        On a non-purge drain it re-sends only when O1 settles "rings
        until cancelled": never under auto-stop (Q12(b) settled — never
        re-ring a stopped alarm), and never while O1 is unsettled
        (conservative default).

        What the purge on a liveness drain buys — and what it costs
        (fix-burst F5): the purge is what stops a re-ring LOOP. Under
        "rings until cancelled" a ``false``-flag record that never
        purged would re-ring on every liveness drain and every
        trigger-4 tick, forever. The purge buys at most ONE re-ring
        ATTEMPT, not one ring: "sent" is not delivery confirmation
        (B1 — a Wyoming silent drop returns cleanly), so a purge
        spends the record's only attempt on a send that may never
        have arrived, and the record is gone afterwards — no retry,
        no repair signal. Q11 and Q12 do NOT justify the purge: Q11
        is a timer that finished while the system was down (restores
        display-only, never rings) and Q12 is never re-ring a STOPPED
        alarm — neither covers an undelivered FIRST ring. The real
        justification is the re-ring-loop avoidance plus the
        architecture's purge-on-live-send rule; the cost is disclosed
        here and counted/logged per purge in ``_drain_device``
        (``unconfirmed_finished_purges``), because no in-HA fix exists
        for the missing delivery signal itself (same root as C10).
        The purge behaviour itself is unchanged.
        """
        if record.direct_delivery_attempted is not False:
            return False
        if purge_drain:
            return True
        return self._o1_alarm_behaviour == "rings_until_cancelled"

    def _cancelled_backoff_due(
        self, record: PendingRecord, now: datetime
    ) -> bool:
        """Whether a CANCELLED record is due a trigger-4 re-send (F2).

        No attempt yet: due at once (a live-cancel enqueue never had
        an immediate send). Otherwise the wait after attempt N is 60 s
        (N=1), 5 min (N=2), then 15 min (N>=3) — the backoff ladder.
        FINISHED records never reach this helper: their re-send rule
        is ``_finished_may_resend`` + purge on a liveness drain, and
        gating them here would break the Q11/Q12 exactly-once-attempt
        path.
        """
        if record.last_attempt_at is None:
            return True
        if record.attempt_count <= 1:
            delay = self._CANCELLED_BACKOFF[0]
        elif record.attempt_count == 2:
            delay = self._CANCELLED_BACKOFF[1]
        else:
            delay = self._CANCELLED_BACKOFF[2]
        return now >= record.last_attempt_at + delay

    def _record_attempt(
        self, timer_id: str, *, now: datetime | None = None
    ) -> None:
        """Book one immediate send for a pending record (F2).

        Immediate sends (dismiss / restart / cancel) go through
        ``_send_to_satellite`` directly, not ``_attempt_send_for_pending``,
        so they would otherwise leave the backoff clock untouched and
        trigger 4 would re-send 60 s after a send that just happened.
        Bumps ``attempt_count`` and re-stamps ``last_attempt_at``; the
        delivery_failed / streak bookkeeping stays with
        ``_attempt_send_for_pending`` and the caller's own
        observably-failed check, so this never clears a failure mark.
        """
        pending = self._pending.get(timer_id)
        if pending is None:
            return
        self._pending[timer_id] = dataclass_replace(
            pending,
            attempt_count=pending.attempt_count + 1,
            last_attempt_at=now or datetime.now(timezone.utc),
        )

    def _attempt_send_for_pending(
        self, record: PendingRecord, event_type: str,
        now: datetime | None = None,
    ) -> str:
        """Re-send a pending record and track whether delivery is observably possible.

        Returns the send outcome. When the attempt hits an
        observably-impossible path (a bare return or a handler raise),
        the pending record is replaced with ``delivery_failed=True``
        and ``sent_streak`` reset to 0. When the attempt reaches the
        handler ("sent"), the streak increments — but the mark clears
        only once the streak reaches ``_DELIVERY_CLEAR_STREAK``
        consecutive "sent" attempts (F3 hysteresis): a single "sent"
        is consistent with a silent drop inside the handler (B1), so
        clearing on it would delete the repair issue while the
        CANCELLED may never have arrived, and a flapping handler
        would toggle the issue open/closed on every tick. The record
        stays pending either way — only the observable-failure signal
        is reset, never the retry. Every attempt bumps
        ``attempt_count`` and re-stamps ``last_attempt_at`` (F2
        backoff clock). ``now`` is the drain's clock (trigger 4
        passes its tick time so the backoff is deterministic in
        tests); other drains use wall-clock time. Every attempt
        mutates the record's bookkeeping — ``attempt_count``,
        ``last_attempt_at``, and the streak / ``delivery_failed``
        mark — so every attempt is a queue change, and the caller
        persists it (site (4) for drains, site (3) for service
        paths, site (5) for taps). A caller that only persists on a
        mark transition loses the mid-streak values to a restart
        (round-2 B1).
        """
        snap = self._snap_for_pending(record)
        outcome = self._send_to_satellite(snap, event_type)
        stamp = now or datetime.now(timezone.utc)
        if outcome != "sent":
            self._pending[record.timer_id] = dataclass_replace(
                record,
                delivery_failed=True,
                sent_streak=0,
                attempt_count=record.attempt_count + 1,
                last_attempt_at=stamp,
            )
        elif record.delivery_failed:
            streak = record.sent_streak + 1
            self._pending[record.timer_id] = dataclass_replace(
                record,
                delivery_failed=streak < self._DELIVERY_CLEAR_STREAK,
                sent_streak=streak,
                attempt_count=record.attempt_count + 1,
                last_attempt_at=stamp,
            )
        else:
            self._pending[record.timer_id] = dataclass_replace(
                record,
                sent_streak=0,
                attempt_count=record.attempt_count + 1,
                last_attempt_at=stamp,
            )
        return outcome

    def _drain_device(
        self, device_id: str, *, purge: bool, reason: str
    ) -> bool:
        """Re-send the pending queue records for one device (§5.3).

        CANCELLED records always re-send; FINISHED records re-send
        only per ``_finished_may_resend``. A drain attempt purges a
        FINISHED record ONLY when the attempt was made in response to
        a liveness signal (``purge=True``: trigger 2 out of
        ``unavailable``, trigger 3 voice-STARTED) AND the attempt
        observably succeeded (outcome ``"sent"``) — a purge-drain
        attempt that observably failed (no handler, handler raised)
        does NOT purge: the record stays pending with
        ``delivery_failed=True`` so the retry and the repair signal
        survive (critic N3). The purge buys at most one re-ring
        ATTEMPT, not one ring (F5 — see ``_finished_may_resend``):
        "sent" is not delivery confirmation, so the purge is counted
        (``unconfirmed_finished_purges``) and logged as a warning.
        A pending CANCELLED is NEVER purged by
        any drain (B1): "sent" means the handler was invoked without
        raising, which for Wyoming is consistent with a silent drop
        inside the handler (``wyoming/assist_satellite.py:913-915``
        — the handler returns normally while the event never
        arrives) and otherwise only schedules the write as a
        background task (``:941-944``) whose outcome the caller never
        sees — there is no delivery signal in HA 2026.10.4, so a
        "sent" CANCELLED is unconfirmed and the record stays pending
        until the 24 h prune removes it. "Supersede" is NOT an exit
        for it (F2): a FINISHED never supersedes a CANCELLED, and no
        second CANCELLED can be produced for a finished id — a second
        dismiss raises KeyError, ``restart_timer`` needs the finished
        record, a live ``_event`` cancelled fires only for live
        timers, and ``restore_finished`` mints a new id. The only
        exits are the prune and a re-dismiss of the same id (which
        re-sends, it does not purge); trigger 4 keeps re-sending it
        on the backoff cadence (60 s, then 5 min, then 15 min)
        until then.
        Attempts made by triggers 1 and 4 never purge, however
        cleanly they return — they are unconditioned attempts that
        may have been silently dropped. Returns True when the queue
        changed — a purge, or ANY attempt's bookkeeping mutation:
        every attempt bumps ``attempt_count``, re-stamps
        ``last_attempt_at`` and moves the streak / ``delivery_failed``
        mark, so a re-send-only pass still changes the record
        (round-2 B1: pre-fix only a ``delivery_failed`` transition
        counted, so mid-streak values 1–2 never reached storage and
        a restart reset both the backoff ladder and the hysteresis).
        The mark is the repair-issue condition, and it must be
        durable across a restart (a reload re-reads the store file,
        so a mark that never reaches the file is lost and the issue
        cannot age from its original ``enqueued_at``) — so any
        attempt in a drain owes a persist (§5.1 site (4)). Every
        purge is logged with timer id + reason; every skipped purge
        is logged the same way.
        """
        changed = False
        for record in list(self._pending.values()):
            if record.device_id != device_id:
                continue
            if record.event_type == "finished":
                if not self._finished_may_resend(
                    record, purge_drain=purge
                ):
                    continue
                event_type = "finished"
            else:
                event_type = "cancelled"
            was = record
            outcome = self._attempt_send_for_pending(record, event_type)
            after = self._pending.get(record.timer_id)
            if after is None or after != was:
                # Any attempt mutates the record's bookkeeping
                # (attempt count, last-attempt time, streak, mark) —
                # a queue change that owes a persist, like a purge,
                # even when the mark itself did not flip (round-2
                # B1). ``after is None`` is the purge case, already
                # a change; the comparison covers the re-send-only
                # pass, where the record survives with new values.
                changed = True
            if (
                purge
                and outcome == "sent"
                and record.event_type == "finished"
            ):
                del self._pending[record.timer_id]
                changed = True
                # F5: the purge spends the record's only re-ring
                # attempt on an unconfirmed "sent" — count it and log
                # it distinctly (warning, not info), so a first ring
                # lost to a silent drop leaves a trace.
                self._unconfirmed_finished_purges += 1
                _LOGGER.warning(
                    "Purged pending finished for timer %s (%s) on an "
                    "unconfirmed 'sent': the first ring attempt is "
                    "spent — if the handler dropped it silently the "
                    "timer never rang, with no retry (unconfirmed "
                    "purge #%d)",
                    record.timer_id,
                    reason,
                    self._unconfirmed_finished_purges,
                )
            elif purge and outcome == "sent":
                # B1: a CANCELLED is never purged on "sent". "Sent"
                # is not delivery confirmation — the handler may have
                # dropped it silently — so the record stays pending
                # until the 24 h prune removes it (supersede is not
                # an exit for a finished id — see the docstring).
                _LOGGER.info(
                    "Not purging pending cancelled for timer %s "
                    "(%s): 'sent' is not delivery confirmation",
                    record.timer_id,
                    reason,
                )
            elif purge:
                # The purge-drain attempt observably failed (outcome
                # is "no-handler", "handler-raised", ... — never
                # "sent"): purging would destroy the only retry and
                # the repair signal, so the record stays pending
                # with delivery_failed set (critic N3).
                _LOGGER.info(
                    "Not purging pending %s for timer %s (%s): "
                    "purge-drain attempt failed (%s)",
                    record.event_type,
                    record.timer_id,
                    reason,
                    outcome,
                )
        return changed

    # ------------------------------------------------- triggers 2 and 4

    _PRUNE_AFTER = timedelta(hours=24)
    _REPAIR_ISSUE_AFTER = timedelta(minutes=5)
    # Hysteresis before the delivery_failed mark clears (fix-burst
    # F3): a record needs this many consecutive attempts that reach
    # the handler before the mark lifts. One "sent" proves nothing —
    # a Wyoming silent drop returns cleanly — so clearing on the first
    # clean return false-clears the repair issue while the CANCELLED
    # may never have arrived.
    _DELIVERY_CLEAR_STREAK = 3
    # Trigger-4 backoff for CANCELLED re-sends (fix-burst F2): the
    # wait after attempt N before the next trigger-4 attempt. The
    # first re-send comes 60 s after the dismiss, the second 5 min
    # after that, then every 15 min — bounded, never infinite retry.
    # Applies to CANCELLED records on trigger 4 ONLY (see
    # PendingRecord); liveness drains and FINISHED re-sends are
    # untouched.
    _CANCELLED_BACKOFF = (
        timedelta(seconds=60),
        timedelta(minutes=5),
        timedelta(minutes=15),
    )

    @property
    def unconfirmed_finished_purges(self) -> int:
        """FINISHED records purged on an unconfirmed "sent" (F5).

        Each one spent the record's single re-ring attempt on a send
        that may have been silently dropped; the counter is the only
        trace, since the purge leaves no retry and no repair signal.
        """
        return self._unconfirmed_finished_purges

    def _resolve_device_entities(self) -> None:
        """Resolve each served device to its satellite entity (§5.3 H11).

        Device→entity via ``async_entries_for_device``: exactly one
        ``assist_satellite`` entry is that device's satellite entity;
        zero means triggers 2 and 4 do not apply to it (logged once at
        setup); more than one is ambiguous and triggers 2 and 4 do
        not subscribe/read for it (logged once). (The existing
        ``_resolve_satellite_device`` is the opposite direction —
        entity id → device id, for service calls — not this lookup.)
        HA helpers are imported lazily, so a stub hass without them
        degrades to an empty map instead of failing (unit tests).
        """
        try:
            from homeassistant.helpers import entity_registry as er
        except ImportError:
            return
        try:
            registry = er.async_get(self._hass)
        except Exception:
            return
        for device_id in sorted(self._devices):
            try:
                entries = er.async_entries_for_device(registry, device_id)
            except Exception:
                continue
            satellites = [
                entry
                for entry in entries
                if getattr(entry, "domain", None) == "assist_satellite"
            ]
            if len(satellites) == 1:
                self._set_device_entity(device_id, satellites[0].entity_id)
            elif len(satellites) == 0:
                if device_id not in self._entity_resolution_logged:
                    self._entity_resolution_logged.add(device_id)
                    _LOGGER.info(
                        "Device %s has no assist_satellite entity: "
                        "triggers 2 and 4 do not apply to it",
                        device_id,
                    )
                self._drop_device_entity(device_id)
            else:
                if device_id not in self._entity_resolution_logged:
                    self._entity_resolution_logged.add(device_id)
                    _LOGGER.warning(
                        "Device %s has %d assist_satellite entities: "
                        "ambiguous, triggers 2 and 4 do not apply",
                        device_id,
                        len(satellites),
                    )
                self._drop_device_entity(device_id)

    def _set_device_entity(self, device_id: str, entity_id: str) -> None:
        """Map a device to its entity and subscribe trigger 2 for it."""
        old = self._device_entities.get(device_id)
        if old == entity_id:
            return
        if old is not None:
            self._drop_state_change_sub(old)
        self._device_entities[device_id] = entity_id
        self._entity_resolution_logged.discard(device_id)
        try:
            from homeassistant.helpers.event import (
                async_track_state_change_event,
            )
        except ImportError:
            return
        try:
            unsub = async_track_state_change_event(
                self._hass, entity_id, self._on_satellite_state_change
            )
        except Exception:
            _LOGGER.warning(
                "Could not subscribe to state changes for %s", entity_id,
                exc_info=True,
            )
            return
        self._state_change_unsubs[entity_id] = unsub

    def _drop_device_entity(self, device_id: str) -> None:
        """Forget a device's entity (and its trigger-2 subscription)."""
        entity_id = self._device_entities.pop(device_id, None)
        if entity_id is not None:
            self._drop_state_change_sub(entity_id)

    def _drop_state_change_sub(self, entity_id: str) -> None:
        unsub = self._state_change_unsubs.pop(entity_id, None)
        if unsub is not None:
            try:
                unsub()
            except Exception:
                pass

    def _listen_entity_registry(self) -> None:
        """Subscribe to raw entity-registry updates (§5.3 F11).

        The raw bus event is the mechanism that can observe creation
        for unknown ids (``async_track_entity_registry_updated_event``
        only matches ids already subscribed to). The callback filters
        by the ``assist_satellite`` domain first and is cheap for
        other domains. Registered in ``start()``, removed in
        ``stop()``; if the bridge is stopped, setup-time resolution
        covers the gap.
        """
        if self._registry_unsub is not None:
            return
        try:
            from homeassistant.helpers.entity_registry import (
                EVENT_ENTITY_REGISTRY_UPDATED,
            )
        except ImportError:
            return
        bus = getattr(self._hass, "bus", None)
        if bus is None:
            return
        try:
            self._registry_unsub = bus.async_listen(
                EVENT_ENTITY_REGISTRY_UPDATED,
                self._on_entity_registry_updated,
            )
        except Exception:
            self._registry_unsub = None

    @callback
    def _on_entity_registry_updated(self, event: Any) -> None:
        """Re-resolve the affected device on a registry change (§5.3 F11)."""
        data = getattr(event, "data", {}) or {}
        entity_id = data.get("entity_id")
        if not entity_id or not str(entity_id).startswith("assist_satellite."):
            return
        action = data.get("action")
        if action == "remove":
            # The entry is already popped before the event fires, so
            # the device is derived from the remembered map instead.
            for device_id, mapped in list(self._device_entities.items()):
                if mapped == entity_id:
                    self._drop_device_entity(device_id)
                    _LOGGER.info(
                        "Satellite entity %s removed: triggers 2 and 4 "
                        "stop applying to device %s until one reappears",
                        entity_id,
                        device_id,
                    )
            return
        # create / update: look the entry up and re-run the lookup
        # for its device. On update with old_entity_id (a rename),
        # drop the old entity's subscription; on a device_id change,
        # drop the old device's mapping first so two map entries
        # never point at one entity.
        try:
            from homeassistant.helpers import entity_registry as er
        except ImportError:
            return
        try:
            registry = er.async_get(self._hass)
            entry = registry.async_get(entity_id)
        except Exception:
            return
        if entry is None:
            return
        old_entity_id = data.get("old_entity_id")
        if old_entity_id and old_entity_id != entity_id:
            self._drop_state_change_sub(old_entity_id)
            for device_id, mapped in list(self._device_entities.items()):
                if mapped == old_entity_id:
                    self._device_entities.pop(device_id, None)
        new_device_id = getattr(entry, "device_id", None)
        if new_device_id is None:
            return
        for device_id, mapped in list(self._device_entities.items()):
            if mapped == entity_id and device_id != new_device_id:
                self._drop_device_entity(device_id)
        if new_device_id not in self._devices:
            _LOGGER.info(
                "Ignoring assist_satellite entity %s for unserved "
                "device %s",
                entity_id,
                new_device_id,
            )
            return
        self._resolve_device_entities()

    async def _on_satellite_state_change(self, event: Any) -> None:
        """Trigger 2: any state change of a served satellite entity (§5.3).

        A transition OUT of ``unavailable`` purges a re-sent FINISHED
        record (liveness proven — on satellite types whose entity
        goes ``unavailable``, i.e. ESPHome; O3 settled per type:
        Wyoming/VoIP entities NEVER go ``unavailable``, so trigger 2
        never purges for the kitchen satellite). A pending CANCELLED
        is re-sent but never purged on "sent" (B1: "sent" is not
        delivery confirmation). A transition between two
        non-``unavailable`` states is a drain attempt that does NOT
        purge. FINISHED re-sends follow ``_finished_may_resend``
        (purge drains ignore O1; non-purge drains need O1 "rings
        until cancelled").
        """
        data = getattr(event, "data", {}) or {}
        entity_id = data.get("entity_id")
        if entity_id is None:
            return
        device_id = None
        for candidate, mapped in self._device_entities.items():
            if mapped == entity_id:
                device_id = candidate
                break
        if device_id is None or device_id not in self._devices:
            return
        old_state = data.get("old_state")
        new_state = data.get("new_state")
        if new_state is None:
            return
        old_value = getattr(old_state, "state", None) if old_state else None
        new_value = getattr(new_state, "state", None)
        purge = old_value == "unavailable" and new_value != "unavailable"
        changed = self._drain_device(
            device_id,
            purge=purge,
            reason=(
                "trigger-2-out-of-unavailable" if purge else "trigger-2"
            ),
        )
        if changed:
            await self.persist()
        self._evaluate_repair_issues(datetime.now(timezone.utc))

    def _start_trigger4_interval(self) -> None:
        """Start the 60 s periodic re-attempt (trigger 4, §5.3).

        Host: ``async_track_time_interval(60 s)`` in ``start()``,
        stopped in ``stop()`` — explicitly independent of the
        sensor-tick gate. Never purges, however cleanly it returns.
        """
        if self._trigger4_unsub is not None:
            return
        try:
            from homeassistant.helpers.event import async_track_time_interval
        except ImportError:
            return
        try:
            self._trigger4_unsub = async_track_time_interval(
                self._hass, self._trigger4_callback, timedelta(seconds=60)
            )
        except Exception:
            self._trigger4_unsub = None
            _LOGGER.warning(
                "Could not start the queue re-attempt interval",
                exc_info=True,
            )

    def _stop_trigger4_interval(self) -> None:
        if self._trigger4_unsub is not None:
            try:
                self._trigger4_unsub()
            except Exception:
                pass
            self._trigger4_unsub = None

    async def _trigger4_callback(self, now: datetime) -> None:
        """The 60 s interval callback — see ``_trigger4_tick``."""
        await self._trigger4_tick(now)

    async def _trigger4_tick(self, now: datetime) -> None:
        """One periodic re-attempt pass (trigger 4, §5.3).

        Prune first, then re-send: a pending record older than 24 h
        is pruned (logged with timer id + reason, never re-sent
        again) — measured from ``finished_at`` for FINISHED records
        (read from the timer record) and from ``enqueued_at`` for
        CANCELLED records. While the served entity reads
        ``unavailable``, the attempt for that device is skipped (no
        attempt, no purge — the satellite is observed down; the skip
        is logged once when it starts) — on satellite types whose
        entity can read ``unavailable`` (ESPHome) only; for Wyoming /
        VoIP the entity never reads ``unavailable``, so the skip
        branch can never engage and trigger 4 keeps attempting on the
        backoff cadence (60 s, then 5 min, then 15 min) while the
        satellite is down — bounded, never every 60 s flat. Trigger 4 NEVER purges. FINISHED
        re-sends need the flag ``false`` AND O1 "rings until
        cancelled" (never under auto-stop; skipped while O1 is
        unsettled). CANCELLED re-sends are BACKED OFF (F2): the first
        re-send waits 60 s after the last attempt, the second 5 min,
        then 15 min between attempts (``_cancelled_backoff_due``) —
        the retry is bounded, never infinite, and a dismiss that
        never gets a liveness signal costs ~100 sends over 24 h,
        not ~1,440. A prune persists the queue change (§5.1 site (4));
        a pass that re-sends persists too, because every attempt
        mutates the record's bookkeeping — count, last-attempt time,
        streak and mark (round-2 B1: pre-fix a re-send-only pass
        wrote nothing, so a restart reset the backoff ladder and the
        streak, and the repair issue could never clear under
        repeated restarts). The repair-issue state is re-evaluated on
        every tick.
        """
        changed = False
        for record in list(self._pending.values()):
            if record.event_type == "finished":
                held = self._records.get(record.timer_id)
                anchor = (
                    held.finished_at
                    if held is not None and held.finished_at is not None
                    else record.enqueued_at
                )
            else:
                anchor = record.enqueued_at
            if now - anchor > self._PRUNE_AFTER:
                del self._pending[record.timer_id]
                changed = True
                _LOGGER.info(
                    "Pruned pending %s for timer %s: older than 24 h "
                    "(anchor %s)",
                    record.event_type,
                    record.timer_id,
                    anchor.isoformat(),
                )
                continue
            if record.device_id is not None:
                entity_id = self._device_entities.get(record.device_id)
                if entity_id is not None and self._entity_is_unavailable(
                    entity_id
                ):
                    if record.device_id not in self._trigger4_skip_logged:
                        self._trigger4_skip_logged.add(record.device_id)
                        _LOGGER.info(
                            "Skipping trigger-4 re-attempt for timer %s: "
                            "satellite entity %s reads unavailable",
                            record.timer_id,
                            entity_id,
                        )
                    continue
                self._trigger4_skip_logged.discard(record.device_id)
            was = record
            if record.event_type == "finished":
                if not self._finished_may_resend(
                    record, purge_drain=False
                ):
                    continue
                self._attempt_send_for_pending(record, "finished", now)
            else:
                # F2 backoff: a CANCELLED re-sends only when its wait
                # since the last attempt has elapsed. Skipping here
                # costs nothing — the record stays pending and the
                # next tick re-checks it.
                if not self._cancelled_backoff_due(record, now):
                    continue
                self._attempt_send_for_pending(record, "cancelled", now)
            after = self._pending.get(record.timer_id)
            if after is None or after != was:
                # Any attempt mutates the record's bookkeeping
                # (attempt count, last-attempt time, streak, mark) —
                # a queue change that owes a persist, like a prune,
                # even when the mark itself did not flip (round-2
                # B1).
                changed = True
        if changed:
            await self.persist()
        self._evaluate_repair_issues(now)

    def _entity_is_unavailable(self, entity_id: str) -> bool:
        """True when the named entity currently reads ``unavailable``."""
        states = getattr(self._hass, "states", None)
        if states is None:
            return False
        try:
            state = states.get(entity_id)
        except Exception:
            return False
        return state is not None and getattr(state, "state", None) == "unavailable"

    # ------------------------------------------------------- repair issue

    def _repair_issue_present(self) -> bool | None:
        """Whether the issue is open in the registry, or None if unreadable."""
        try:
            from homeassistant.helpers import issue_registry as ir
        except ImportError:
            return None
        try:
            registry = ir.async_get(self._hass)
        except Exception:
            return None
        try:
            issues = registry.issues
        except Exception:
            return None
        return (DOMAIN, "undelivered_timer_events") in issues

    def _evaluate_repair_issues(self, now: datetime) -> None:
        """Create/clear ``undelivered_timer_events`` (§5.3, RB-R2-2).

        The bridge creates the issue when a pending CANCELLED record
        ages past 5 min AND its delivery is observably impossible —
        the most recent send attempt for that record hit an
        observably-impossible path (``delivery_failed``). A record
        whose latest attempt reached the handler NEVER raises the
        issue, however long it stays pending (an ordinary connected
        dismiss followed by five quiet minutes produces NO issue).
        Only CANCELLED records age toward the issue: a pending
        FINISHED is the expected ringing state, not a failure. The
        issue clears when the path is observably open again — which
        takes ``_DELIVERY_CLEAR_STREAK`` consecutive attempts that
        reach the handler (F3 hysteresis), not one: a single "sent"
        is consistent with a silent drop inside the handler, so
        clearing on it would false-clear the issue and let a flapping
        handler toggle it open/closed on every tick. With hysteresis
        a flap (fail, sent, fail, sent, ...) never reaches the streak
        and the issue stays open until the path is genuinely, repeatedly
        open. The issue also clears when the record prunes at 24 h.
        No newer event can replace it first (round-2 B2): a second
        dismiss raises KeyError, ``restart_timer`` needs the finished
        record, a live-cancel tap fires only for live timers, and
        ``restore_finished`` mints a new id — and a FINISHED never
        supersedes a CANCELLED. A CANCELLED never purges (B1), so
        clearing depends only on the streak or the prune. In the clean
        case — no skip, no failed attempt — clearing after the
        satellite recovers takes just under 46 min once the
        backoff ladder is at the 15-minute step (round-2 B3;
        burst-3 critic Finding 1); earlier in the ladder it
        takes less: with the last attempt just before recovery
        the backoff ladder (60 s, 5 min, 15 min) spaces the
        three consecutive "sent" attempts at +15, +30 and +45 min
        once attempt_count is 3 or more — but attempts can only
        land on 60 s trigger-4 ticks, and every ladder step is a
        multiple of 60 s, so a phase offset of just under 60 s on
        the first attempt is preserved, not accumulated: off-phase
        the attempts land at up to +15:59, +30:59 and +45:59.
        Measured on that off-phase grid, the clear time depends
        on attempt_count: 1 clears at +21:59 (sents +1:59, +6:59,
        +21:59), 2 clears at +35:59 (sents +5:59, +20:59,
        +35:59), 3 clears at +45:59.
        That bound holds only in the clean case (burst-4 critic
        Finding 1; burst-5 delta critic Findings 1-2): no
        unavailable-skip at a due tick and no failed attempt in
        between. A skip at a due tick restarts the backoff clock
        from the actual (delayed) attempt time, so each skip adds
        up to one further 60 s tick regardless of position —
        measured on the B3b 59 s phase grid from the last attempt
        before recovery: no skip clears at +45:59 (45.98 min), one
        skip at +46:59 (46.98 min), two skips at +47:59 (47.98 min,
        consecutive and non-consecutive identical), three skips at
        +48:59 (48.98 min, consecutive and non-consecutive identical);
        N skips clear at 45:59 + N min, so a
        satellite flapping at every due tick pushes clearing
        arbitrarily far. The skip needs an ESPHome-type satellite
        (Wyoming / VoIP entities never read ``unavailable``) and a
        flap landing exactly on a due tick. A failed attempt is
        strictly worse and is NOT ESPHome-only: any outcome other
        than "sent" in ``_attempt_send_for_pending`` resets
        ``sent_streak`` to 0 and re-stamps ``last_attempt_at``,
        restarting the whole ladder from that attempt — so one
        failed attempt can add up to a further ~46 min. It is
        type-agnostic (handler down / no-handler, or handler
        raising) and hits any satellite whose entity reads
        available while its handler is down or raising; all drain
        paths (triggers 1/2/3/4) funnel through that same failure
        branch. Measured: a single failed trigger-4 attempt at
        streak 2 clears at +90:59 (90.98 min) from the last attempt
        before recovery, with no skip, no restart, and the
        satellite reachable throughout in the entity sense; a
        handler re-registration whose drain fails clears at +80:59
        (80.98 min) — 66.98 min even with genuine loud recovery at
        +36:00; a restart whose startup trigger-1 drain fails
        clears at +65:59 (65.98 min). A restart on its own does NOT
        reset the streak: a load-only restart at streak 1 still
        clears at +45:59, and a restart whose startup drain
        succeeds advances it — only a failed drain during the
        restart resets it. A single failed attempt clears at ~46–91
        min from the last attempt once the backoff ladder is at the
        15-minute step; earlier in the ladder it clears sooner, and
        61 min is only the floor for failures at due ticks at that
        step. The skip and the streak-reset-on-failure
        are both correct and unchanged; the bound is what is
        overstated, so the user-facing text states the usual case
        and names the failure overrun separately — usually within
        about 46 minutes after the satellite is reachable again,
        but a failed stop attempt restarts the wait, so one failed
        attempt can push it to about 91 minutes, and if the
        satellite keeps failing or keeps going unavailable it may
        not clear until the record expires after 24 hours. Whether the
        streak-reset-on-failure itself should change is a design
        question, not settled here.
        There is no second escalation step. The wording says
        "may still be ringing" — a warning the hub cannot confirm the
        stop, not a claim the speaker is ringing. The Wyoming
        silent-drop gap is disclosed, not closed: a dismiss issued
        during a silent drop is indistinguishable from a connected
        dismiss, so the issue does NOT fire for it either.
        """
        try:
            from homeassistant.helpers import issue_registry as ir
        except ImportError:
            return
        qualifying = [
            record
            for record in self._pending.values()
            if record.event_type == "cancelled"
            and record.delivery_failed
            and now - record.enqueued_at > self._REPAIR_ISSUE_AFTER
        ]
        desired = bool(qualifying)
        present = self._repair_issue_present()
        if present is None:
            present = self._repair_issue_open
        if desired and not present:
            try:
                ir.async_create_issue(
                    self._hass,
                    DOMAIN,
                    "undelivered_timer_events",
                    is_fixable=False,
                    severity=ir.IssueSeverity.WARNING,
                    translation_key="undelivered_timer_events",
                )
            except Exception:
                _LOGGER.warning(
                    "Could not create the undelivered-timer-events "
                    "repair issue",
                    exc_info=True,
                )
                return
            self._repair_issue_open = True
            _LOGGER.warning(
                "Undelivered timer events: %d pending CANCELLED "
                "record(s) older than 5 min with observably impossible "
                "delivery (%s)",
                len(qualifying),
                ", ".join(sorted(r.timer_id for r in qualifying)),
            )
        elif not desired and present:
            try:
                ir.async_delete_issue(
                    self._hass, DOMAIN, "undelivered_timer_events"
                )
            except Exception:
                _LOGGER.warning(
                    "Could not clear the undelivered-timer-events "
                    "repair issue",
                    exc_info=True,
                )
                return
            self._repair_issue_open = False

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
        finished = self._records.get(timer_id)
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
        # R2-T4 (§5.3): restart sends CANCELLED for the old timer id
        # BEFORE starting the new timer, and attempts delivery at once
        # (the immediate send — the old alarm stops promptly while the
        # new timer runs). The CANCELLED is enqueued before the old
        # record is dropped, and supersedes the pending FINISHED for
        # the old id. If the new start then fails, the old record stays
        # (the sensor remains) and the CANCELLED stays pending — a
        # duplicate stop that keeps retrying on the trigger-4 backoff
        # cadence (60 s, then 5 min, then 15 min) until the 24 h prune.
        self._enqueue(
            PendingRecord(
                timer_id=finished.timer_id,
                device_id=finished.device_id,
                event_type="cancelled",
                enqueued_at=datetime.now(timezone.utc),
                name=finished.name,
                total_seconds=finished.total_seconds,
                language=finished.language,
                is_finished=True,
                started_at=finished.started_at,
            )
        )
        outcome = self._send_to_satellite(finished, "cancelled")
        # The immediate send counts as attempt 1 for the trigger-4
        # backoff (F2).
        self._record_attempt(timer_id)
        if outcome != "sent":
            pending = self._pending.get(timer_id)
            if pending is not None:
                self._pending[timer_id] = dataclass_replace(
                    pending, delivery_failed=True
                )
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
        self._records.pop(timer_id, None)
        self._service_started.discard(timer_id)
        self._service_started.add(new_id)
        self._observed_started_at.setdefault(
            new_id, datetime.now(timezone.utc)
        )
        self._notify_listeners()
        return new_id

    def _notify_listeners(self) -> None:
        """Tell change listeners the timer set may have changed.

        Notify ONLY: persistence is explicit (``persist()``, §5.1
        BUG-T1) and no longer rides along on a notification — a
        save issued during a snapshot raises, and a listener that
        reads a snapshot must never trigger a nested save.
        """
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
        ``started_at`` is the observed start recorded by the STARTED
        tap, or omitted when the bridge never observed one (§5.1) —
        never derived as finished_at − duration. The observed entry is
        discarded after recording: a finished timer's start lives on
        its record, not in the live-timer map.
        """
        finished_at = datetime.now(timezone.utc)
        started_at = self._observed_started_at.pop(timer.id, None)
        self._records[timer.id] = TimerRecord(
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
        # §5.3: when _record_finished runs it enqueues a FINISHED
        # record for that timer id. The flag is always true for a
        # finish the bridge learns through the tap — _event fires only
        # from the tap's finally after TimerManager invoked the
        # handler, synchronously with no await between — so no
        # tap-observed finish can ever carry false. A true-flag record
        # is NEVER re-sent by any drain; it stays pending until a
        # CANCELLED supersedes it (dismiss / restart) or the 24 h
        # prune removes it.
        self._enqueue(
            PendingRecord(
                timer_id=timer.id,
                device_id=getattr(timer, "device_id", None),
                event_type="finished",
                enqueued_at=finished_at,
                direct_delivery_attempted=True,
                name=getattr(timer, "name", "") or "",
                total_seconds=getattr(timer, "created_seconds", 0) or 0,
                language=getattr(timer, "language", "en") or "en",
                is_finished=True,
                started_at=started_at,
            )
        )

    @callback
    def _event(self, event_type: str, timer: Any) -> None:
        if (
            getattr(timer, "device_id", None) not in self._devices
            or getattr(timer, "conversation_command", None)
        ):
            return

        # The per-id last-event record (§5.3, RB-R2-3): reconcile's
        # vanish condition (iii) reads it. Retained until the later of
        # the _live_seen entry being dropped and the nominal
        # finishes_at passing (pruned in reconcile_finished). The
        # finishes_at kept here is the last-seen one — the nominal
        # finish moment for this id.
        seen = self._live_seen.get(timer.id)
        self._last_event[timer.id] = _LastEvent(
            event_type=event_type,
            finishes_at=seen.finishes_at if seen is not None else None,
        )

        if event_type == "started":
            # The STARTED tap IS the observation of the start (§5.1):
            # record the tap time, never a derivation. setdefault so
            # a service call's earlier observation (start_timer's
            # belt-and-braces record) is kept when both exist.
            self._observed_started_at.setdefault(
                timer.id, datetime.now(timezone.utc)
            )
            if self._service_start_in_flight:
                # This STARTED event fired synchronously inside one of
                # this bridge's own start_timer / restart_timer calls
                # (HA generates the id inside the call, so the mark
                # cannot be set before it). Mark the id BEFORE
                # notifying listeners, so the first snapshot any
                # listener reads already carries started_via_service
                # (BRIDGE-1).
                self._service_started.add(timer.id)
            elif timer.id not in self._service_started:
                # Trigger 3 (§5.3): a voice-originated STARTED — the
                # voice command had to arrive over the satellite
                # connection, so the channel was live at that moment.
                # Pending events for that device are re-sent. A
                # FINISHED re-send on this drain may purge, but a
                # CANCELLED never purges on "sent" (B1): the voice
                # command proves the satellite could reach HA, not
                # that HA's CANCELLED reached the satellite — the
                # handler may drop it silently, and there is no
                # delivery signal to confirm it. UPDATED, CANCELLED
                # and FINISHED events, and service-originated STARTED
                # events, are NOT liveness signals and never drain
                # the queue. The re-send runs synchronously here; the
                # persist for a purge is issued as a background task
                # (site (4)), like the tap-path save below.
                changed = self._drain_device(
                    timer.device_id, purge=True, reason="trigger-3"
                )
                if changed:
                    create_task = getattr(
                        self._hass, "async_create_task", None
                    )
                    if create_task is not None:
                        try:
                            create_task(self.persist())
                        except Exception:
                            _LOGGER.warning(
                                "Could not save persisted timers",
                                exc_info=True,
                            )
                self._evaluate_repair_issues(datetime.now(timezone.utc))
        elif event_type == "cancelled":
            # A voice-cancelled service-started timer must not keep
            # its mark forever (ids never repeat, but the set would
            # otherwise grow without bound). Its observed start goes
            # with it — a cancelled timer has no record to carry one.
            # The start is read BEFORE the pop below, so the pending
            # CANCELLED carries it for the re-dismiss listing (B5).
            cancelled_started_at = self._observed_started_at.get(timer.id)
            self._service_started.discard(timer.id)
            self._observed_started_at.pop(timer.id, None)
            # The live-cancel enqueue site (§5.3): TimerManager's own
            # direct CANCELLED already fired inside cancel_timer (the
            # bridge makes NO second attempt at enqueue time), and the
            # bridge enqueues the retry record for it. A
            # voice-originated CANCELLED is no exception: the command
            # proves the channel live at arrival, but that evidence
            # predates both deliveries, so the record stays pending
            # like any other — until the 24 h prune removes it. It is
            # NEVER purged by a voice STARTED (a liveness drain
            # re-sends a CANCELLED but never purges it on "sent", B1:
            # "sent" is not delivery confirmation), and supersede is
            # not an exit for it (F2: a FINISHED never supersedes a
            # CANCELLED, and no second CANCELLED can be produced for
            # a finished id). Trigger 4 re-sends it on the backoff
            # cadence (60 s, then 5 min, then 15 min). With no handler
            # registered no tap fires at all, so no record is enqueued
            # for a handlerless cancel — the satellite was never told
            # the timer existed, so there is nothing to retry.
            self._enqueue(
                PendingRecord(
                    timer_id=timer.id,
                    device_id=getattr(timer, "device_id", None),
                    event_type="cancelled",
                    enqueued_at=datetime.now(timezone.utc),
                    name=getattr(timer, "name", "") or "",
                    total_seconds=getattr(timer, "created_seconds", 0) or 0,
                    language=getattr(timer, "language", "en") or "en",
                    is_finished=False,
                    started_at=cancelled_started_at,
                )
            )

        if event_type == "finished":
            self._record_finished(timer)

        self._notify_listeners()
        # Persist site (5): the tap path. Every tap that changes the
        # timer set is written to storage, best effort, off the call
        # stack — a voice start, pause or cancel must survive an
        # immediate restart. The save runs as a background task so the
        # tap stays synchronous; a hass without async_create_task
        # (unit tests) simply skips it.
        if self._store is not None:
            create_task = getattr(self._hass, "async_create_task", None)
            if create_task is not None:
                try:
                    create_task(self.persist())
                except Exception:
                    _LOGGER.warning(
                        "Could not save persisted timers", exc_info=True
                    )
