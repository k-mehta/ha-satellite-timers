"""The bridge: Home Assistant wiring around the pure state machine.

This module is the ONLY place Home Assistant APIs are touched. The
decisions live in ``records.py`` (pure) and ``resolution.py`` (pure);
this module supplies the live signals, calls the state machine, and
persists the result.

What the bridge owns (§03):
- the Store (version 2, atomic, ERS-1 ladder on every save/load);
- the TimerManager tap (``tm_observer.py``) — observes TM-direct
  starts (voice, rig seeds) without displacing any satellite handler;
- the service table (``services.py`` calls into here);
- the 1 s tick, the 30 s reconcile, the 60 s re-probe;
- the ring-stop (bounded stop-retry: a pending-stop record, re-issued
  on liveness within the 3600 s ring window only);
- the one in-memory error slot (ERS-5).

What the bridge NEVER does: read a satellite signal as "ringing"
(§03.6 — ringing is the bridge's published record, synthesised from
its own records); write into the frontend diagnostics (``core/
diagnostics.js`` — an upward call, forbidden by layering §01.1);
derive a value for a sensor attribute (observed-or-omitted, §09 C2).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Coroutine
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from homeassistant.components.intent.timers import (
    TimerEventType,
    TimerInfo,
    TimerManager,
    TimerNotFoundError,
    TimersNotSupportedError,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store

from .const import (
    ANNOUNCE_AUTOMATION_ENTITY_ID,
    CONF_PRIMARY_SATELLITE,
    DEDUPE_TTL_SECONDS,
    DOMAIN,
    ISSUE_UNDELIVERED_TIMER_EVENTS,
    MAX_TIMER_DURATION_SECONDS,
    MIN_TIMER_DURATION_SECONDS,
    PERSIST_MAX_DRAIN_PASSES,
    RECONCILE_INTERVAL_SECONDS,
    REPROBE_INTERVAL_SECONDS,
    RING_ACK_TIMEOUT_SECONDS,
    RING_WINDOW_SECONDS,
    STORE_KEY,
    STORE_VERSION,
    TICK_INTERVAL_SECONDS,
)
from .errors import (
    BridgeNotReady,
    InvalidDuration,
    InvalidName,
    SatelliteNotSupported,
    TimerNotFound,
    UndoExpired,
    retry_with_ladder,
)
from .records import (
    DedupeTable,
    TimerRecord,
    WriteAheadIntent,
    evaluate_finish,
    restore_from_snapshot,
    restore_record,
    timer_sensor_attributes,
    undo_snapshot,
)
from .resolution import (
    VOICE_ALTERNATIVE,
    new_command_id,
    resolve_satellite_device,
    validate_command_id,
)
from .tm_observer import TimerManagerTap

_LOGGER = logging.getLogger(__name__)

# The repair issue's copy names the ring window as the settled bound
# (3600 s, §09 C3) and the manual action (dismiss again). The MEASURED
# liveness-to-clear latency is in the translation copy in strings.json:
# two rig runs measured 0.0024 s and 0.0022 s from handler restore to
# issue clear (the re-issue is synchronous on re-registration); the
# copy states "within about a second". See the implementation log
# (W2-5/B1) — the copy must state measured times, not guesses.


def check_timer_manager_shape(tm: TimerManager) -> None:
    """Fail loudly when the TimerManager is not the shape this build
    was written against (HA 2026.10.4, ``components/intent/
    timers.py``). Used by the config flow (setup gate) and by the
    bridge (setup + 60 s re-probe). A shape failure is a setup failure,
    not a degraded state: the bridge cannot mirror timers it cannot
    read."""
    if not isinstance(getattr(tm, "timers", None), dict):
        raise BridgeNotReady("TimerManager has no timers dict")
    if not isinstance(getattr(tm, "timer_tasks", None), dict):
        raise BridgeNotReady("TimerManager has no timer_tasks dict")
    if not isinstance(getattr(tm, "handlers", None), dict):
        raise BridgeNotReady("TimerManager has no handlers dict")
    if not callable(getattr(tm, "_wait_for_timer", None)):
        raise BridgeNotReady("TimerManager has no _wait_for_timer")
    if not callable(getattr(tm, "start_timer", None)):
        raise BridgeNotReady("TimerManager has no start_timer")


class SatelliteTimerBridge:
    """One bridge per config entry. See the module docstring for the
    ownership split; the per-method rules are cited inline."""

    def __init__(self, hass: HomeAssistant, entry) -> None:
        self.hass = hass
        self._entry = entry
        self._tm: TimerManager | None = None
        self._store: Store | None = None
        self._records: dict[str, TimerRecord] = {}
        self._dedupe = DedupeTable()
        self._intents: dict[str, WriteAheadIntent] = {}
        self._removed: dict[str, dict] = {}
        self._pending_stops: dict[str, dict] = {}
        self._listeners: list[Callable[[], None]] = []
        self._in_flight: set[tuple[str, str, int]] = set()
        # In-flight commands, keyed by command_id (§03.2 M1): the
        # _run_command wrapper's join table. An entry is the owner
        # task, a done event, and the outcome (result or error) — a
        # second caller with the same command_id waits on the event
        # and takes the first call's outcome, never executing the
        # body. The entry is removed once the outcome is set, so a
        # later caller takes the dedupe table's stored result instead.
        self._inflight_commands: dict[str, dict[str, Any]] = {}
        # The timer screen card's registration, retried by the 60 s
        # re-probe while unregistered (ERS-7, C-NB-07): an async
        # callable returning True when settled (registered, or
        # Lovelace is in YAML mode and there is nothing to retry),
        # False when the next cycle should try again. Set by
        # __init__.py only when the first registration attempt fails;
        # None means the card is registered (or was never attempted).
        self.frontend_reprobe: Callable[[], Any] | None = None
        self._ack_tasks: dict[str, asyncio.Task] = {}
        self._tap: TimerManagerTap | None = None
        self._persist_lock = asyncio.Lock()
        self._persist_task: asyncio.Task | None = None
        self._mutation_seq = 0
        self._store_failed = False
        self.error_slot: dict | None = None
        self._unsub_intervals: list[Callable[[], None]] = []
        self._ready = False
        # The built-in intent handlers displaced by the voice overrides
        # (intents.py), keyed by intent type — restored exactly on unload.
        self.displaced_intents: dict = {}

    # -- setup / teardown ---------------------------------------------------

    @property
    def ready(self) -> bool:
        """True once setup completed (services may be called)."""
        return self._ready

    @property
    def primary_satellite(self) -> str:
        """The stored ``primary_satellite`` option ("" = unset)."""
        return self._entry.options.get(CONF_PRIMARY_SATELLITE) or ""

    @property
    def records(self) -> dict[str, TimerRecord]:
        """A read-only view of the live records (intents + sensors use it)."""
        return self._records

    def _now(self) -> datetime:
        return datetime.now(timezone.utc)

    def _check_tm_shape(self, tm: TimerManager) -> None:
        check_timer_manager_shape(tm)

    async def async_setup(self) -> None:
        """Load the Store, restore the records, install the tap, rebuild
        the executor, start the intervals. Raises ConfigEntryNotReady
        when the TimerManager is missing or the wrong shape, or the
        Store cannot be loaded after the ERS-1 ladder (§08 row 2)."""
        tm = self.hass.data.get("intent.timer")
        if tm is None:
            raise ConfigEntryNotReady(
                "the voice timer system (intent.timer) is not loaded yet"
            )
        try:
            self._check_tm_shape(tm)
        except BridgeNotReady as err:
            raise ConfigEntryNotReady(str(err)) from err
        self._tm = tm
        self._store = Store(self.hass, STORE_VERSION, STORE_KEY)
        await self._load_store()
        self._tap = TimerManagerTap(
            tm, self._on_timer_event, self._on_handler_registered
        )
        self._tap.install()
        for record in list(self._records.values()):
            if record.state in ("active", "paused"):
                self._rebuild_tm_timer(record)
        await self._persist()
        self._unsub_intervals.append(
            async_track_time_interval(
                self.hass, self._tick, timedelta(seconds=TICK_INTERVAL_SECONDS)
            )
        )
        self._unsub_intervals.append(
            async_track_time_interval(
                self.hass,
                self._reconcile,
                timedelta(seconds=RECONCILE_INTERVAL_SECONDS),
            )
        )
        self._unsub_intervals.append(
            async_track_time_interval(
                self.hass,
                self._reprobe,
                timedelta(seconds=REPROBE_INTERVAL_SECONDS),
            )
        )
        self._ready = True
        _LOGGER.info(
            "Satellite Timers bridge ready: %d timers restored, "
            "%d pending stops, %d write-ahead intents",
            len(self._records),
            len(self._pending_stops),
            len(self._intents),
        )

    async def async_unload(self) -> None:
        """Stop the intervals and the tap. Best-effort final persist."""
        for unsub in self._unsub_intervals:
            unsub()
        self._unsub_intervals.clear()
        for task in self._ack_tasks.values():
            task.cancel()
        self._ack_tasks.clear()
        if self._tap is not None:
            self._tap.uninstall()
        self._ready = False
        await self._persist()

    # -- listeners ----------------------------------------------------------

    def subscribe(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Register a change listener (the sensor manager). Returns an
        unsubscribe callable."""
        self._listeners.append(listener)

        def _unsub() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return _unsub

    def _notify(self) -> None:
        for listener in list(self._listeners):
            try:
                listener()
            except Exception:  # noqa: BLE001 — one bad listener must not break the rest
                _LOGGER.exception("Satellite Timers change listener failed")

    # -- error slot (ERS-5) ---------------------------------------------------

    def _set_error(self, code: str, message: str) -> None:
        """The ONE in-memory bridge error slot: newer wins, a success
        clears it, a reload clears it (§08 row 41)."""
        now = self._now().isoformat()
        if self.error_slot and self.error_slot.get("code") == code:
            self.error_slot["timestamp"] = now
        else:
            self.error_slot = {
                "code": code,
                "message": message,
                "timestamp": now,
            }
        _LOGGER.warning("Satellite Timers error slot set: %s — %s", code, message)
        self._notify()

    def _clear_error(self) -> None:
        if self.error_slot is not None:
            self.error_slot = None
            self._notify()

    def report_error(self, code: str, message: str) -> None:
        """Public form of the error slot (ERS-5), for the setup
        self-check in ``__init__.py``: a degraded subsystem is reported
        here, never silently swallowed."""
        self._set_error(code, message)

    # -- Store ----------------------------------------------------------------

    def _payload(self) -> dict:
        return {
            "version": STORE_VERSION,
            "timers": {tid: r.to_dict() for tid, r in self._records.items()},
            "dedupe": self._dedupe.to_dict(),
            "intents": {cid: i.to_dict() for cid, i in self._intents.items()},
            "removed": dict(self._removed),
            "pending_stops": dict(self._pending_stops),
        }

    async def _load_store(self) -> None:
        """Load + restore. A non-v2 payload is discarded wholesale
        (never migrated — the superseded build is not a build base).
        A malformed single record is dropped with a log line; it never
        fails setup. Intents older than the dedupe TTL are dropped
        (a start that never completed is retried fresh, §03.2 M2)."""
        assert self._store is not None
        try:
            payload = await retry_with_ladder(
                "load satellite_timers store",
                lambda: self._store.async_load(),
            )
        except Exception as err:
            raise ConfigEntryNotReady(
                f"could not load the timer store after the retry ladder: {err}"
            ) from err
        if payload is None:
            return
        if not isinstance(payload, dict) or payload.get("version") != STORE_VERSION:
            _LOGGER.warning(
                "Discarding satellite_timers store payload (version %r, "
                "expected %d) — never migrating a superseded schema",
                payload.get("version") if isinstance(payload, dict) else None,
                STORE_VERSION,
            )
            return
        now = self._now()
        timers = payload.get("timers")
        if not isinstance(timers, dict):
            timers = {}
        for timer_id, data in timers.items():
            try:
                record = TimerRecord.from_dict(data)
            except ValueError as err:
                _LOGGER.warning("Dropping malformed stored timer %s: %s", timer_id, err)
                continue
            self._records[timer_id] = restore_record(record, now)
        self._dedupe = DedupeTable.from_dict(payload.get("dedupe"))
        # from_dict is shape-only: it keeps expired entries, so the
        # loader drops them here (a restart never resurrects a dead
        # entry). The 30 s reconcile prunes on the same schedule.
        pruned = self._dedupe.prune(now)
        if pruned:
            _LOGGER.info("Dropped %d expired dedupe entries from the store", pruned)
        intents = payload.get("intents")
        if not isinstance(intents, dict):
            intents = {}
        for command_id, data in intents.items():
            try:
                intent = WriteAheadIntent.from_dict(data)
            except ValueError as err:
                _LOGGER.warning("Dropping malformed write-ahead intent %s: %s", command_id, err)
                continue
            if (now - intent.created_at).total_seconds() > DEDUPE_TTL_SECONDS:
                continue  # a start that never completed; retried fresh
            self._intents[command_id] = intent
        removed = payload.get("removed") or {}
        if isinstance(removed, dict):
            for timer_id, snapshot in removed.items():
                if isinstance(snapshot, dict) and isinstance(
                    snapshot.get("removed_at"), str
                ):
                    self._removed[timer_id] = snapshot
        pending = payload.get("pending_stops") or {}
        if isinstance(pending, dict):
            for timer_id, stop in pending.items():
                if isinstance(stop, dict) and isinstance(stop.get("device_id"), str):
                    self._pending_stops[timer_id] = stop

    def _mark_dirty(self) -> None:
        """Every mutation bumps the sequence so a persist in flight
        notices and re-saves (no lost writes under concurrency)."""
        self._mutation_seq += 1

    async def _persist(self) -> bool:
        """Save the Store on the ERS-1 ladder. Returns True on success.
        A failure does NOT raise to the caller: the in-memory state
        stays authoritative (ERS-2 memory-serving), the error slot is
        set, and the 60 s re-probe retries the save. Returns False on
        ladder exhaustion."""
        assert self._store is not None
        async with self._persist_lock:
            # Bounded drain (ERS-3): each pass saves the live payload;
            # if state changed during the save, loop and save again.
            # The loop is capped at PERSIST_MAX_DRAIN_PASSES so a
            # mutation flood can neither spin forever nor hold the lock
            # forever; a leftover is re-scheduled as a fresh pass
            # below (the invariant is eventual persistence, not that
            # one task does it all).
            for _ in range(PERSIST_MAX_DRAIN_PASSES):
                seq = self._mutation_seq
                try:
                    await retry_with_ladder(
                        "persist satellite_timers store",
                        lambda: self._store.async_save(self._payload()),
                    )
                except Exception as err:  # noqa: BLE001 — ladder exhausted
                    self._store_failed = True
                    self._set_error(
                        "store_unavailable",
                        f"Could not save the timers after four attempts "
                        f"({err}). Timers keep running in memory; the "
                        f"hub retries the save every 60 seconds.",
                    )
                    return False
                self._store_failed = False
                if self.error_slot and self.error_slot.get("code") == "store_unavailable":
                    self._clear_error()
                if self._mutation_seq == seq:
                    return True
                # State changed during the save; loop and save again.
            # Drain cap hit with mutations still arriving: schedule one
            # fresh pass for the leftover. It runs after this task
            # releases the lock (a new task starts on a later event
            # loop turn), so the leftover is persisted, not dropped.
            self.hass.async_create_background_task(
                self._persist(), name="Satellite Timers persist (drain)"
            )
            return True

    def _schedule_persist(self) -> None:
        """Coalesced background persist (tap callbacks, tick
        transitions). A running persist re-reads the live state when it
        saves, so a second queued save is redundant."""
        if self._persist_task is not None and not self._persist_task.done():
            return
        self._persist_task = self.hass.async_create_background_task(
            self._persist(), name="Satellite Timers persist"
        )

    # -- satellite resolution (§09 C1-R) --------------------------------------

    def _live_devices(self) -> list[str]:
        assert self._tm is not None
        return list(self._tm.handlers.keys())

    def _resolve_explicit(self, value: str) -> str:
        """An explicit ``device_id`` may be a device id or an
        assist_satellite entity id; both resolve to a live device id.
        Anything else is the loud error (never a silent guess)."""
        live = set(self._live_devices())
        if value in live:
            return value
        registry = er.async_get(self.hass)
        entry = registry.async_get(value)
        if entry is not None and entry.device_id in live:
            return entry.device_id
        raise SatelliteNotSupported(
            f"The satellite {value!r} is not a timer satellite. "
            f"Use one of the configured satellites, or {VOICE_ALTERNATIVE}."
        )

    def _resolve_device(self, explicit: str | None) -> str:
        """Explicit → primary → single → loud error (W2-1)."""
        if explicit is not None:
            explicit = self._resolve_explicit(explicit)
        return resolve_satellite_device(
            self._live_devices(), self.primary_satellite or None, explicit
        )

    def _announce_on_for(self, device_id: str) -> str | None:
        """The assist_satellite entity id for a device, when one exists
        — observed, never derived. Omitted (None) when the device has no
        entity: the record simply carries no announce path."""
        registry = er.async_get(self.hass)
        for entry in registry.entities.values():
            if entry.device_id == device_id and entry.domain == "assist_satellite":
                return entry.entity_id
        return None

    def _satellite_reachable(self, record: TimerRecord) -> bool:
        """A2: only a live satellite entity state counts. unknown /
        unavailable / missing = unreachable; announce_on omitted =
        missing (settled §09 C3)."""
        if record.announce_on is None:
            return False
        state = self.hass.states.get(record.announce_on)
        if state is None:
            return False
        return state.state not in ("unknown", "unavailable")

    def _announcer_present(self) -> bool:
        """A13: the announcement automation exists and is on."""
        state = self.hass.states.get(ANNOUNCE_AUTOMATION_ENTITY_ID)
        return state is not None and state.state == "on"

    def _muted(self) -> bool | None:
        """A3 is CONDITIONAL (XR-16): no real mute signal has been
        cited, so the answer is always unknown — never triggers."""
        return None

    # -- TimerManager executor -----------------------------------------------

    def _timer_info_for(self, record: TimerRecord) -> TimerInfo:
        """A TimerInfo built from a bridge record for the stop
        vehicle only: the two stop paths — `_issue_stop` and the
        pending-stop retry sweep — hand it to the satellite's timer
        event handler. The executor rebuild (`_rebuild_tm_timer`)
        builds its own TimerInfo inline and never calls this. Area
        and floor are omitted on purpose: the only
        `handle_timer_event` in HA 2026.10.4 is ESPHome's
        (`assist_satellite.py:643-660`), which forwards only
        id/name/created_seconds/seconds_left/is_active to firmware
        and reads no area or floor — carrying them here would be
        dead data. ``created_at``/``updated_at`` are monotonic now:
        the info is only ever handed to a handler, never compared
        against the original creation."""
        now_ns = time.monotonic_ns()
        return TimerInfo(
            id=record.timer_id,
            name=record.name or None,
            seconds=record.duration_seconds,
            device_id=record.device_id,
            start_hours=record.start_hours,
            start_minutes=record.start_minutes,
            start_seconds=record.start_seconds,
            created_at=now_ns,
            updated_at=now_ns,
            language=record.language,
            is_active=False,
        )

    def _rebuild_tm_timer(self, record: TimerRecord) -> None:
        """Rebuild the TimerManager executor from a persisted record
        (§08 row 2): same timer id (no new ULID, no id map), a fresh
        ``_wait_for_timer`` task for active timers, no task for paused
        ones. ``_created_seconds`` is restored to the original duration
        so a later add_time mirrors the right total (TimerInfo.add_time
        grows ``_created_seconds`` from it)."""
        assert self._tm is not None
        if record.timer_id in self._tm.timers:
            return  # already registered (defensive; a re-setup skips)
        now = self._now()
        if record.state == "active":
            assert record.finishes_at is not None  # enforced by from_dict
            remaining = max(
                1, int((record.finishes_at - now).total_seconds())
            )
        else:  # paused
            remaining = record.remaining_seconds or 0
        created_ns = time.monotonic_ns()
        info = TimerInfo(
            id=record.timer_id,
            name=record.name or None,
            seconds=remaining,
            device_id=record.device_id,
            start_hours=record.start_hours,
            start_minutes=record.start_minutes,
            start_seconds=record.start_seconds,
            created_at=created_ns,
            updated_at=created_ns,
            language=record.language,
            is_active=(record.state == "active"),
            area_id=record.area_id,
            area_name=record.area_name,
            floor_id=record.floor_id,
        )
        info._created_seconds = record.duration_seconds
        self._tm.timers[record.timer_id] = info
        if record.state == "active":
            self._tm.timer_tasks[record.timer_id] = (
                self.hass.async_create_background_task(
                    self._tm._wait_for_timer(
                        record.timer_id, remaining, created_ns
                    ),
                    name=f"Timer {record.timer_id}",
                )
            )

    # -- the tap: TimerManager events ------------------------------------------

    def _on_handler_registered(self, device_id: str) -> None:
        """Liveness (§03.10/AD-C1): a satellite (re-)registered its
        timer handler through the tap — the satellite is reachable
        again, so any pending stop for it is re-issued NOW, within the
        ring window only. The 30 s reconcile stays as the backstop (a
        stop is also retried there); this is the prompt path the B1
        repair-issue copy describes. It fires at tap install too, for
        handlers already present at setup — a pending stop left from
        before the restart is re-issued at once instead of waiting
        for the next reconcile tick. Synchronous: the retry calls the
        satellite's own handler directly and never re-enters the tap
        (the stop vehicle unwraps to the original handler)."""
        if not any(
            stop.get("device_id") == device_id
            for stop in self._pending_stops.values()
        ):
            return
        self._retry_pending_stops(self._now())

    def _on_timer_event(
        self, device_id: str, event: TimerEventType, timer: TimerInfo
    ) -> None:
        """Synchronous: runs inside the TimerManager call, so a
        TM-direct start is adopted before start_timer returns."""
        if event is TimerEventType.STARTED:
            self._adopt_started(device_id, timer)
        elif event is TimerEventType.FINISHED:
            self._handle_finished(device_id, timer)
        elif event is TimerEventType.CANCELLED:
            self._handle_cancelled(device_id, timer)
        elif event is TimerEventType.UPDATED:
            self._handle_updated(device_id, timer)

    def _adopt_started(self, device_id: str, timer: TimerInfo) -> None:
        """Adopt a timer the TimerManager started outside the bridge
        (voice, rig seed, another integration). No record is ever
        created by guessing an id match — the id comes from TM itself.
        Conversation-command timers are never adopted (TM never fires
        STARTED for them, and the reconcile skips them too)."""
        if timer.id in self._records:
            return
        if timer.conversation_command:
            return
        key = (device_id, timer.name or "", timer.created_seconds)
        if key in self._in_flight:
            return  # the bridge's own start, completing now
        now = self._now()
        record = TimerRecord(
            timer_id=timer.id,
            name=timer.name or "",
            duration_seconds=timer.created_seconds,
            state="active" if timer.is_active else "paused",
            # started_at is NOT derived from the monotonic created_at:
            # a wall-clock start was never observed, so it stays omitted.
            started_at=None,
            finishes_at=(
                now + timedelta(seconds=timer.seconds_left)
                if timer.is_active
                else None
            ),
            remaining_seconds=(timer.seconds if not timer.is_active else None),
            # started_via is omitted: the bridge cannot know the origin.
            started_via=None,
            announce_on=self._announce_on_for(device_id),
            device_id=device_id,
            area_id=timer.area_id,
            area_name=timer.area_name,
            floor_id=timer.floor_id,
            start_hours=timer.start_hours,
            start_minutes=timer.start_minutes,
            start_seconds=timer.start_seconds,
            language=timer.language,
        )
        self._records[timer.id] = record
        self._mark_dirty()
        self._schedule_persist()
        self._notify()

    def _handle_finished(self, device_id: str, timer: TimerInfo) -> None:
        record = self._records.get(timer.id)
        if record is None or record.state != "active":
            return
        self._finish_record(record, self._now())
        self._mark_dirty()
        self._schedule_persist()
        self._notify()

    def _handle_cancelled(self, device_id: str, timer: TimerInfo) -> None:
        """A TM timer cancelled outside the bridge (voice through the
        core path, the satellite itself). Bridge-cancel semantics:
        snapshot + remove, so it stays undoable for 5 s. A record the
        bridge already removed (its own cancel deletes first, then
        calls TM) is a no-op here."""
        record = self._records.get(timer.id)
        if record is None:
            return
        if record.state in ("finished", "ringing"):
            return
        now = self._now()
        self._removed[timer.id] = undo_snapshot(record, now)
        del self._records[timer.id]
        self._mark_dirty()
        self._schedule_persist()
        self._notify()

    def _handle_updated(self, device_id: str, timer: TimerInfo) -> None:
        """Re-read TM state into the record. Idempotent with the bridge
        op's own post-sync (same helper, same values)."""
        record = self._records.get(timer.id)
        if record is None:
            return
        self._sync_from_tm(record, timer)
        self._mark_dirty()
        self._schedule_persist()
        self._notify()

    def _sync_from_tm(self, record: TimerRecord, timer: TimerInfo) -> None:
        """The ONE sync path from a TimerInfo into a record: TM's
        remaining is authoritative (monotonic), the bridge's wall-clock
        finishes_at is derived from it at read time."""
        now = self._now()
        if timer.is_active:
            record.state = "active"
            record.remaining_seconds = None
            record.finishes_at = now + timedelta(seconds=timer.seconds_left)
        else:
            record.state = "paused"
            record.remaining_seconds = timer.seconds
            record.finishes_at = None
        record.duration_seconds = timer.created_seconds

    # -- finish / ring / stop ---------------------------------------------------

    def _clear_ring_fields(self, record: TimerRecord) -> None:
        """A finished record carries no ring state: ring_started_at /
        ring_ack_at are meaningless once the ring is over, and a
        finished record must not look half-ringing in the Store.
        Called on every ringing → finished transition, AFTER the stop
        is issued — the pending-stop expiry is measured from
        ring_started_at, so the fields must still be set when the
        stop is recorded."""
        record.ring_started_at = None
        record.ring_ack_at = None

    def _finish_record(self, record: TimerRecord, now: datetime) -> None:
        """Finish evaluation, precedence A2 → A3 → A13 (§09 C3). Called
        from the FINISHED tap, the 1 s tick, and the 30 s reconcile —
        all converge here, and the state==active guard makes the
        convergence idempotent."""
        if record.state != "active":
            return
        finished_at = record.finishes_at or now
        state, missed, reason = evaluate_finish(
            satellite_reachable=self._satellite_reachable(record),
            announcer_present=self._announcer_present(),
            muted=self._muted(),
        )
        record.state = state
        record.finished_at = finished_at
        record.alarm_missed = missed
        record.alarm_missed_reason = reason
        if state == "ringing":
            record.ring_started_at = now
            record.ring_ack_at = None
            self._schedule_ack_timeout(record)
        else:
            record.finishes_at = None
            self._clear_ring_fields(record)

    def _schedule_ack_timeout(self, record: TimerRecord) -> None:
        """A15: 5 s for the ring_started ack. No ack in the window →
        finished + alarm_missed 'announcer_no_run'. The task is
        cancelled on ack and on unload."""
        self._cancel_ack_task(record.timer_id)

        async def _ack_timeout() -> None:
            await asyncio.sleep(RING_ACK_TIMEOUT_SECONDS)
            rec = self._records.get(record.timer_id)
            if rec is None or rec.state != "ringing":
                return
            if rec.ring_ack_at is not None:
                return
            rec.state = "finished"
            rec.finishes_at = None
            rec.alarm_missed = True
            rec.alarm_missed_reason = "announcer_no_run"
            self._clear_ring_fields(rec)
            self._mark_dirty()
            self._schedule_persist()
            self._notify()

        self._ack_tasks[record.timer_id] = self.hass.async_create_background_task(
            _ack_timeout(), name=f"Ring ack timeout {record.timer_id}"
        )

    def _cancel_ack_task(self, timer_id: str) -> None:
        task = self._ack_tasks.pop(timer_id, None)
        if task is not None:
            task.cancel()

    def _original_handler_for(self, device_id: str | None):
        """The satellite's own handler, unwrapped: the tap stores the
        original on the wrapper, so a stop never re-enters the tap."""
        assert self._tm is not None
        if device_id is None:
            return None
        handler = self._tm.handlers.get(device_id)
        if handler is None:
            return None
        return getattr(handler, "_bridge_original_handler", handler)

    def _issue_stop(self, record: TimerRecord, now: datetime) -> bool:
        """The stop vehicle (§08 row 33, B1): invoke the satellite's
        own TM handler with CANCELLED and a TimerInfo built from the
        record (``esphome/assist_satellite.py:643-660`` forwards it to
        firmware). Issued on ack and on window expiry.

        Returns True when a present handler was reached. A missing or
        failing handler → pending-stop record + the repair issue, and
        the stop is re-issued when the handler is (re-)registered
        through the tap (liveness, §03.10/AD-C1) and on the 30 s
        reconcile backstop, within the ring window only (bounded
        stop-retry, §03.10). A stop that reaches a present handler
        creates NO issue: delivery is unconfirmable, a known
        residual bounded by window expiry, not a flagged failure.

        The copy on the repair issue states the settled bound, the
        manual action, and the measured clear time (about a second
        from handler restore to issue clear — two rig runs measured
        0.0024 s and 0.0022 s)."""
        handler = self._original_handler_for(record.device_id)
        if handler is None:
            self._record_pending_stop(record, now)
            return False
        try:
            handler(TimerEventType.CANCELLED, self._timer_info_for(record))
        except Exception as err:  # noqa: BLE001 — a failing handler is a bounded retry, not a crash
            _LOGGER.warning(
                "Stop for timer %s reached its satellite handler but the "
                "handler raised (%s); the stop is queued for re-issue",
                record.timer_id,
                err,
            )
            self._record_pending_stop(record, now)
            return False
        self._pending_stops.pop(record.timer_id, None)
        self._refresh_undelivered_issue()
        return True

    def _record_pending_stop(self, record: TimerRecord, now: datetime) -> None:
        """A stop that could not be delivered: kept (with its expiry)
        until liveness or the ring window, never forever. The expiry
        is measured from the RING START (ring_started_at), not from
        when the stop was created: §03.10/AD-C1 re-issues within the
        ring window only, and a stop created late in the window must
        not outlive the window by an hour. A stop recorded at window
        expiry is therefore already expired — the next sweep drops it
        without another attempt, and the repair issue names the manual
        action (dismiss again)."""
        ring_start = record.ring_started_at or now
        self._pending_stops[record.timer_id] = {
            "timer_id": record.timer_id,
            "device_id": record.device_id,
            "created_at": now.isoformat(),
            "expires_at": (
                ring_start + timedelta(seconds=RING_WINDOW_SECONDS)
            ).isoformat(),
        }
        self._refresh_undelivered_issue()
        self._mark_dirty()
        self._schedule_persist()

    def _refresh_undelivered_issue(self) -> None:
        """The undelivered_timer_events repair issue exists exactly
        while a pending stop exists; it clears the moment the last one
        clears (B1 — the system no longer states delivery as fact while
        unconfirmed)."""
        if self._pending_stops:
            placeholders = {
                "pending": str(len(self._pending_stops)),
                "window": str(RING_WINDOW_SECONDS // 60),
            }
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                ISSUE_UNDELIVERED_TIMER_EVENTS,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_UNDELIVERED_TIMER_EVENTS,
                translation_placeholders=placeholders,
            )
        else:
            ir.async_delete_issue(
                self.hass, DOMAIN, ISSUE_UNDELIVERED_TIMER_EVENTS
            )

    def _retry_pending_stops(self, now: datetime) -> None:
        """Re-issue pending stops whose handler is back, within the
        ring window only. Called when a handler is (re-)registered
        through the tap (liveness, §03.10/AD-C1 — the prompt path)
        and on the 30 s reconcile (the backstop). Expired stops are
        dropped without another attempt: the window is the bound."""
        if not self._pending_stops:
            return
        changed = False
        for timer_id, stop in list(self._pending_stops.items()):
            expires_at = datetime.fromisoformat(stop["expires_at"])
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if now >= expires_at:
                del self._pending_stops[timer_id]
                changed = True
                continue
            record = self._records.get(timer_id)
            handler = self._original_handler_for(stop.get("device_id"))
            if handler is None:
                continue
            if record is not None:
                info = self._timer_info_for(record)
            else:
                info = TimerInfo(
                    id=timer_id,
                    name=None,
                    seconds=0,
                    device_id=stop.get("device_id"),
                    start_hours=None,
                    start_minutes=None,
                    start_seconds=None,
                    created_at=time.monotonic_ns(),
                    updated_at=time.monotonic_ns(),
                    language="en",
                    is_active=False,
                )
            try:
                handler(TimerEventType.CANCELLED, info)
            except Exception as err:  # noqa: BLE001 — stays pending, retried next time
                _LOGGER.warning(
                    "Re-issued stop for timer %s failed again (%s); "
                    "it stays queued within the ring window",
                    timer_id,
                    err,
                )
                continue
            del self._pending_stops[timer_id]
            changed = True
        if changed:
            self._refresh_undelivered_issue()
            self._mark_dirty()
            self._schedule_persist()

    # -- intervals ------------------------------------------------------------

    async def _tick(self, now: datetime) -> None:
        """1 s tick (§03.13): gated — it early-returns when no
        non-finished record exists, so an idle bridge costs nothing.
        While any non-finished record exists it notifies every tick
        (remaining freshness on the dial) and persists only on a
        transition."""
        if not any(
            r.state in ("active", "paused", "ringing")
            for r in self._records.values()
        ):
            return
        changed = False
        for record in list(self._records.values()):
            if (
                record.state == "active"
                and record.finishes_at is not None
                and record.finishes_at <= now
            ):
                self._finish_record(record, now)
                changed = True
            elif (
                record.state == "ringing"
                and record.ring_started_at is not None
                and (now - record.ring_started_at).total_seconds()
                >= RING_WINDOW_SECONDS
            ):
                # Ring-window expiry: stop, finish, no missed flag.
                record.state = "finished"
                record.finishes_at = None
                record.alarm_missed = False
                record.alarm_missed_reason = None
                self._cancel_ack_task(record.timer_id)
                # The stop is issued BEFORE the ring fields are
                # cleared: its expiry is measured from ring_started_at.
                self._issue_stop(record, now)
                self._clear_ring_fields(record)
                changed = True
        if changed:
            self._mark_dirty()
            self._schedule_persist()
        self._notify()

    async def _reconcile(self, now: datetime) -> None:
        """30 s backstop (§03.13): re-assert the tap, re-run the finish
        evaluation (overdue backstop), adopt TM orphans, remove
        externally-cancelled records, retry pending stops, prune stale
        write-ahead intents and expired dedupe entries."""
        if self._tap is not None and self._tap.reassert():
            _LOGGER.info("Timer tap re-installed: tm.handlers was replaced")
        changed = False
        for record in list(self._records.values()):
            if (
                record.state == "active"
                and record.finishes_at is not None
                and record.finishes_at <= now
            ):
                self._finish_record(record, now)
                changed = True
        assert self._tm is not None
        for timer_id, timer in list(self._tm.timers.items()):
            if timer_id not in self._records and not timer.conversation_command:
                self._adopt_started(timer.device_id, timer)
                changed = True
        for record in list(self._records.values()):
            if (
                record.state in ("active", "paused")
                and record.timer_id not in self._tm.timers
            ):
                # Externally cancelled (TM timer gone, no CANCELLED tap
                # seen): snapshot + remove, same as the tap path.
                self._removed[record.timer_id] = undo_snapshot(record, now)
                del self._records[record.timer_id]
                changed = True
        for command_id, intent in list(self._intents.items()):
            if (now - intent.created_at).total_seconds() > DEDUPE_TTL_SECONDS:
                del self._intents[command_id]
                changed = True
        # The dedupe table is pruned on the same 30 s sweep that prunes
        # intents: without this, an entry for a never-retried command
        # stays in memory and in the Store forever (the lookup drops
        # an entry only when that exact command_id is asked for again).
        if self._dedupe.prune(now):
            changed = True
        if self._pending_stops:
            self._retry_pending_stops(now)
            changed = True
        if changed:
            self._mark_dirty()
            self._schedule_persist()
            self._notify()

    async def _reprobe(self, now: datetime) -> None:
        """60 s re-probe (ERS-7, §08 row 41): TM shape, intent overrides
        still registered, a failed persist re-attempted (one persist
        call per cycle — the ERS-1 ladder inside it is the one retry
        path). A clean re-probe clears the error slot silently."""
        ok = True
        if self._tm is not None:
            try:
                self._check_tm_shape(self._tm)
            except BridgeNotReady as err:
                self._set_error("bridge_not_ready", str(err))
                ok = False
        from . import intents as _intents

        if not _intents.assert_installed(self.hass):
            _LOGGER.warning(
                "Voice routing re-installed by the 60 s re-probe: an "
                "intent override went missing"
            )
            _intents.install(self.hass, self)
            if not _intents.assert_installed(self.hass):
                self._set_error(
                    "bridge_voice_routing",
                    "Voice timer routing is missing and could not be "
                    "re-installed; dashboard timers still work, voice "
                    "timers go through the built-in path.",
                )
                ok = False
        if self._store_failed:
            if await self._persist():
                _LOGGER.info("Timer store save recovered on the re-probe")
            else:
                ok = False
        if self.frontend_reprobe is not None:
            # ERS-7: the timer screen card's registration is retried
            # here, one attempt per 60 s cycle, until settled. The
            # card is optional — a failure never sets the error slot
            # or fails setup; dashboard timers and sensors work
            # without it.
            try:
                settled = await self.frontend_reprobe()
            except Exception:  # noqa: BLE001 — stays unregistered, retried next cycle
                _LOGGER.warning(
                    "Timer screen card registration failed again; "
                    "the 60 s re-probe retries it"
                )
                settled = False
            if settled:
                self.frontend_reprobe = None
                _LOGGER.info("Timer screen card registered on the 60 s re-probe")
        if ok:
            self._clear_error()

    # -- services (the state-checked command table, §09 C1) ---------------------

    async def _run_command(
        self, command_id: str, body: Coroutine[Any, Any, dict]
    ) -> dict:
        """Run one state-checked command exactly once per command_id
        (§03.2 M1, §03.2 M3). This wrapper is the ONE place the
        command scaffold lives — the dedupe check, the dedupe store,
        the persist and the notify — so the nine command methods carry
        no repeated preamble or postamble.

        ``body`` is the command's coroutine, created by the caller
        but not yet started: it is awaited only when this call owns
        the command_id, and closed un-awaited when the call is a
        dedupe hit or joins an in-flight call (closing a coroutine
        that never started runs none of its code, so nothing leaks).

        The dedupe table is the single source of truth for "this
        command already ran or is running": a completed command_id
        returns its stored result and executes nothing; a command_id
        already in flight is JOINED — the second caller waits on the
        in-flight entry's event and takes the first call's outcome,
        never executing the body. That is the card's 3 s ACK-timeout
        retry landing while the first call is still in flight (a
        persist-ladder retry can hold it up to ~40 s, §07.9 ERS-1):
        without the join, the retry would start a second TimerManager
        timer and crash deleting the shared write-ahead intent.

        A re-entrant call from the same task (restart → start with
        the same command_id) runs its body directly — the outer call
        owns the claim and stores the result. A body that raises
        stores nothing (a failed call is not a completed command; the
        retry re-executes), and a waiter that joined it raises the
        same error. The body only computes the result dict, which
        must carry ``timer_id``."""
        validate_command_id(command_id)
        now = self._now()
        stored = self._dedupe_or_none(command_id, now)
        if stored is not None:
            body.close()
            return stored
        current = asyncio.current_task()
        entry = self._inflight_commands.get(command_id)
        if entry is not None:
            if entry["task"] is current:
                return await body
            body.close()
            await entry["done"].wait()
            if entry["error"] is not None:
                raise entry["error"]
            return entry["result"]
        entry = {
            "task": current,
            "done": asyncio.Event(),
            "result": None,
            "error": None,
        }
        self._inflight_commands[command_id] = entry
        try:
            result = await body
            self._dedupe_store(command_id, result["timer_id"], result, now)
            await self._persist()
            self._notify()
            entry["result"] = result
        except BaseException as err:  # noqa: BLE001 — a joined waiter raises the same error
            entry["error"] = err
            raise
        finally:
            # Waiters hold their own reference to the entry, so
            # removing it from the table is safe the moment the
            # outcome is set; a later caller takes the dedupe table's
            # stored result instead.
            entry["done"].set()
            self._inflight_commands.pop(command_id, None)
        return result

    def _state_response(self, record: TimerRecord, now: datetime) -> dict:
        return {
            "timer_id": record.timer_id,
            "state": record.state,
            "remaining_seconds": record.remaining_at(now),
            "name": record.name,
            "duration_seconds": record.duration_seconds,
            "finishes_at": (
                record.finishes_at.isoformat() if record.finishes_at else None
            ),
            "finished_at": (
                record.finished_at.isoformat() if record.finished_at else None
            ),
        }

    def _record_or_raise(self, timer_id: str) -> TimerRecord:
        record = self._records.get(timer_id)
        if record is None:
            raise TimerNotFound(f"No timer with id {timer_id!r}")
        return record

    def _dedupe_or_none(self, command_id: str, now: datetime) -> dict | None:
        stored = self._dedupe.lookup(command_id, now)
        return stored["result"] if stored is not None else None

    def _dedupe_store(self, command_id: str, timer_id: str, result: dict, now: datetime) -> None:
        self._dedupe.store(command_id, timer_id, result, now)
        self._mark_dirty()

    async def async_start_timer(
        self,
        command_id: str,
        name: str,
        duration_seconds: int,
        *,
        device_id: str | None = None,
        started_via: str = "service",
        start_hours: int | None = None,
        start_minutes: int | None = None,
        start_seconds: int | None = None,
        language: str | None = None,
    ) -> dict:
        """Start a timer (M2 write-ahead intent + M1 dedupe, §03.2).

        The intent is persisted WITHOUT a timer id before TM is called;
        the id comes from start_timer's return. A crash between the TM
        start and the record persist leaves an orphan TM timer the
        bridge must NOT cancel by guessing — the 30 s reconcile adopts
        it instead (its id comes from TM itself). The command_id
        dedupe, the in-flight join and the result persist are the
        _run_command wrapper's — this method only computes the result."""
        return await self._run_command(
            command_id,
            self._start_timer(
                command_id,
                name,
                duration_seconds,
                device_id=device_id,
                started_via=started_via,
                start_hours=start_hours,
                start_minutes=start_minutes,
                start_seconds=start_seconds,
                language=language,
            ),
        )

    async def _start_timer(
        self,
        command_id: str,
        name: str,
        duration_seconds: int,
        *,
        device_id: str | None = None,
        started_via: str = "service",
        start_hours: int | None = None,
        start_minutes: int | None = None,
        start_seconds: int | None = None,
        language: str | None = None,
    ) -> dict:
        now = self._now()
        clean_name = (name or "").strip()
        # A name is required for a service start (§09 C1). A voice start
        # may be nameless — core's name slot is optional — so a voice
        # (or restart) record carries an empty name, never a guessed one.
        if not clean_name and started_via == "service":
            raise InvalidName("A timer needs a name")
        if not MIN_TIMER_DURATION_SECONDS <= duration_seconds <= MAX_TIMER_DURATION_SECONDS:
            raise InvalidDuration(
                "A timer can run from 1 second to 24 hours"
            )
        if language is None:
            language = self.hass.config.language
        assert self._tm is not None
        device = self._resolve_device(device_id)
        announce_on = self._announce_on_for(device)
        intent = WriteAheadIntent(
            command_id=command_id,
            name=clean_name,
            duration_seconds=duration_seconds,
            device_id=device,
            announce_on=announce_on,
            started_via=started_via,
            created_at=now,
            start_hours=start_hours,
            start_minutes=start_minutes,
            start_seconds=start_seconds,
            language=language,
        )
        self._intents[command_id] = intent
        self._mark_dirty()
        if not await self._persist():
            # Memory-serving (ERS-2): the intent is in memory and the
            # re-probe retries the save; the start proceeds.
            _LOGGER.warning(
                "Start intent for %r is held in memory only — the store "
                "save failed; the 60 s re-probe retries it",
                command_id,
            )
        key = (device, clean_name, duration_seconds)
        self._in_flight.add(key)
        try:
            if (
                start_hours is None
                and start_minutes is None
                and start_seconds is None
            ):
                tm_hours, tm_minutes, tm_seconds = None, None, duration_seconds
            else:
                tm_hours, tm_minutes, tm_seconds = (
                    start_hours,
                    start_minutes,
                    start_seconds,
                )
            timer_id = self._tm.start_timer(
                device,
                tm_hours,
                tm_minutes,
                tm_seconds,
                language,
                name=clean_name,
            )
        except TimersNotSupportedError as err:
            self._intents.pop(command_id, None)
            self._mark_dirty()
            await self._persist()
            raise SatelliteNotSupported(
                f"The satellite {device!r} does not support timers. "
                f"{VOICE_ALTERNATIVE}."
            ) from err
        finally:
            self._in_flight.discard(key)
        # Core fills in the area, the floor and the start-time fields
        # on the TimerInfo it creates (timers.py start_timer: the area
        # from the device's effective area, normalised, and the floor
        # frozen from that area; a seconds-only start carries the
        # whole duration as start_seconds). The record must carry
        # exactly what core carries — the voice matchers compare
        # records the way core compares TimerInfos, so a record with
        # no area can never match an area slot (C2-01: "cancel all
        # timers in the kitchen" left a ringing timer ringing), and a
        # record with no start_seconds can never match a start-time
        # slot for a service-started timer. The same gap broke the
        # floor fallback in _find_record, which used to derive the
        # floor from the record's area at match time: an area moved
        # to another floor after the timer started changed the
        # derived floor, so a two-way floor tie resolved to one timer
        # and an ambiguous cancel silenced the wrong one (C3-01).
        # Read all six fields back off the timer core just built,
        # instead of re-deriving them here. Both matchers
        # (_find_record and _record_matches_cancel_all) read the
        # record, so this one source fix makes both agree with core.
        tm_timer = self._tm.timers.get(timer_id)
        if tm_timer is not None:
            area_id = tm_timer.area_id
            area_name = tm_timer.area_name
            floor_id = tm_timer.floor_id
            start_hours = tm_timer.start_hours
            start_minutes = tm_timer.start_minutes
            start_seconds = tm_timer.start_seconds
        else:  # defensive: the timer core just created it; never seen.
            # If it ever fired, the record would carry floor_id None —
            # the same legacy-None shape as a pre-fix Store row
            # (C4-NB-01: a None floor can match the tiebreak by None
            # equality, exactly as core matches a legitimate
            # None-floor timer).
            area_id = None
            area_name = None
            floor_id = None
        record = TimerRecord(
            timer_id=timer_id,
            name=clean_name,
            duration_seconds=duration_seconds,
            state="active",
            started_at=now,
            finishes_at=now + timedelta(seconds=duration_seconds),
            started_via=started_via,
            announce_on=announce_on,
            device_id=device,
            area_id=area_id,
            area_name=area_name,
            floor_id=floor_id,
            start_hours=start_hours,
            start_minutes=start_minutes,
            start_seconds=start_seconds,
            language=language,
        )
        self._intents.pop(command_id, None)
        self._records[timer_id] = record
        result = self._state_response(record, now)
        result["entity_id"] = f"sensor.satellite_timer_{timer_id.lower()}"
        return result

    async def async_pause_timer(self, command_id: str, timer_id: str) -> dict:
        """Pause (active only). A wrong-state call returns the current state."""
        return await self._run_command(
            command_id, self._pause_timer(command_id, timer_id)
        )

    async def _pause_timer(self, command_id: str, timer_id: str) -> dict:
        now = self._now()
        record = self._record_or_raise(timer_id)
        if record.state != "active":
            return self._state_response(record, now)
        assert self._tm is not None
        try:
            self._tm.pause_timer(timer_id)
        except TimerNotFoundError as err:
            raise TimerNotFound(
                f"No timer with id {timer_id!r} in the voice timer system"
            ) from err
        tm_timer = self._tm.timers.get(timer_id)
        if tm_timer is not None:
            self._sync_from_tm(record, tm_timer)
        return self._state_response(record, now)

    async def async_resume_timer(self, command_id: str, timer_id: str) -> dict:
        """Resume (paused only). A wrong-state call returns the current state."""
        return await self._run_command(
            command_id, self._resume_timer(command_id, timer_id)
        )

    async def _resume_timer(self, command_id: str, timer_id: str) -> dict:
        now = self._now()
        record = self._record_or_raise(timer_id)
        if record.state != "paused":
            return self._state_response(record, now)
        assert self._tm is not None
        try:
            self._tm.unpause_timer(timer_id)
        except TimerNotFoundError as err:
            raise TimerNotFound(
                f"No timer with id {timer_id!r} in the voice timer system"
            ) from err
        tm_timer = self._tm.timers.get(timer_id)
        if tm_timer is not None:
            self._sync_from_tm(record, tm_timer)
        return self._state_response(record, now)

    async def async_cancel_timer(self, command_id: str, timer_id: str) -> dict:
        """Cancel (active/paused only). Snapshot first, delete the record,
        THEN call TM — so the CANCELLED tap finds no record and cannot
        double-snapshot."""
        return await self._run_command(
            command_id, self._cancel_timer(command_id, timer_id)
        )

    async def _cancel_timer(self, command_id: str, timer_id: str) -> dict:
        now = self._now()
        record = self._record_or_raise(timer_id)
        if record.state not in ("active", "paused"):
            return self._state_response(record, now)
        name, duration = record.name, record.duration_seconds
        self._removed[timer_id] = undo_snapshot(record, now)
        del self._records[timer_id]
        self._cancel_ack_task(timer_id)
        self._mark_dirty()
        assert self._tm is not None
        try:
            self._tm.cancel_timer(timer_id)
        except TimerNotFoundError:
            pass  # TM side already gone; the desired end state holds
        result = {
            "timer_id": timer_id,
            "state": "cancelled",
            "remaining_seconds": 0,
            "name": name,
            "duration_seconds": duration,
            "finishes_at": None,
            "finished_at": None,
        }
        return result

    async def async_dismiss_timer(self, command_id: str, timer_id: str) -> dict:
        """Dismiss (finished only). No stop is issued: a finished timer
        is not ringing — if it rang, its stop was issued at ack or at
        window expiry; if it never rang, there is nothing to silence."""
        return await self._run_command(
            command_id, self._dismiss_timer(command_id, timer_id)
        )

    async def _dismiss_timer(self, command_id: str, timer_id: str) -> dict:
        now = self._now()
        record = self._record_or_raise(timer_id)
        if record.state != "finished":
            return self._state_response(record, now)
        name, duration = record.name, record.duration_seconds
        finished_at = record.finished_at
        self._removed[timer_id] = undo_snapshot(record, now)
        del self._records[timer_id]
        self._mark_dirty()
        result = {
            "timer_id": timer_id,
            "state": "dismissed",
            "remaining_seconds": 0,
            "name": name,
            "duration_seconds": duration,
            "finishes_at": None,
            "finished_at": finished_at.isoformat() if finished_at else None,
        }
        return result

    async def async_acknowledge_timer(self, command_id: str, timer_id: str) -> dict:
        """Acknowledge (ringing only): stop the alarm, finish the record.
        An ack with no ring_started ack inside the 5 s window still
        stops the alarm — and records the honest miss
        (announcer_no_run, G-09 G9(e))."""
        return await self._run_command(
            command_id, self._acknowledge_timer(command_id, timer_id)
        )

    async def _acknowledge_timer(self, command_id: str, timer_id: str) -> dict:
        now = self._now()
        record = self._record_or_raise(timer_id)
        if record.state != "ringing":
            return self._state_response(record, now)
        self._cancel_ack_task(timer_id)
        if record.ring_ack_at is None:
            record.alarm_missed = True
            record.alarm_missed_reason = "announcer_no_run"
        record.state = "finished"
        record.finishes_at = None
        # The stop is issued BEFORE the ring fields are cleared: its
        # expiry is measured from ring_started_at.
        self._issue_stop(record, now)
        self._clear_ring_fields(record)
        return self._state_response(record, now)

    async def async_restart_timer(self, command_id: str, timer_id: str) -> dict:
        """Restart (finished only): a NEW timer, new ULID, same name,
        the FULL original duration, started_via 'restart'. The old
        record is dropped without an undo snapshot (restart is not a
        removal). The new start runs first so a failed start cannot
        lose the finished timer."""
        return await self._run_command(
            command_id, self._restart_timer(command_id, timer_id)
        )

    async def _restart_timer(self, command_id: str, timer_id: str) -> dict:
        now = self._now()
        record = self._record_or_raise(timer_id)
        if record.state != "finished":
            return self._state_response(record, now)
        name, duration, device = record.name, record.duration_seconds, record.device_id
        # The nested start re-uses this command_id in the SAME task,
        # so _run_command runs its body directly (re-entrant): the
        # outer call owns the claim and stores the result.
        result = await self.async_start_timer(
            command_id,
            name,
            duration,
            device_id=device,
            started_via="restart",
        )
        del self._records[timer_id]
        self._mark_dirty()
        return result

    async def async_add_time(
        self, command_id: str, timer_id: str, seconds: int
    ) -> dict:
        """Add (or remove, negative) time — active/paused only. The
        duration follows TM's observed created_seconds after the add.
        A non-positive remaining on an active timer finishes it now
        (the tick would do the same within a second; the response is
        final either way)."""
        return await self._run_command(
            command_id, self._add_time(command_id, timer_id, seconds)
        )

    async def _add_time(
        self, command_id: str, timer_id: str, seconds: int
    ) -> dict:
        now = self._now()
        record = self._record_or_raise(timer_id)
        if record.state not in ("active", "paused"):
            return self._state_response(record, now)
        assert self._tm is not None
        try:
            self._tm.add_time(timer_id, seconds)
        except TimerNotFoundError as err:
            raise TimerNotFound(
                f"No timer with id {timer_id!r} in the voice timer system"
            ) from err
        tm_timer = self._tm.timers.get(timer_id)
        if tm_timer is not None:
            self._sync_from_tm(record, tm_timer)
        if record.state == "active" and record.remaining_at(now) <= 0:
            self._finish_record(record, now)
        return self._state_response(record, now)

    async def async_undo_remove(self, command_id: str, timer_id: str) -> dict:
        """Undo a cancel or dismiss within 5 s (§09 C9): the EXACT
        snapshot, same timer id, same state, same remaining /
        finished_at — except started_at, which is stripped (§03.5).
        The executor is rebuilt for a restored active/paused record."""
        return await self._run_command(
            command_id, self._undo_remove(command_id, timer_id)
        )

    async def _undo_remove(self, command_id: str, timer_id: str) -> dict:
        now = self._now()
        snapshot = self._removed.get(timer_id)
        if snapshot is None:
            raise TimerNotFound(f"No removed timer with id {timer_id!r} to undo")
        try:
            record = restore_from_snapshot(snapshot, now)
        except ValueError as err:
            del self._removed[timer_id]
            self._mark_dirty()
            _LOGGER.info("Undo for timer %s refused: %s", timer_id, err)
            raise UndoExpired(
                "The 5 second undo window has passed for this timer"
            ) from err
        del self._removed[timer_id]
        self._records[record.timer_id] = record
        if record.state in ("active", "paused"):
            self._rebuild_tm_timer(record)
        return self._state_response(record, now)

    async def async_ring_started(
        self, timer_id: str, command_id: str | None = None
    ) -> dict:
        """D5 ring ack (§09 C1, implementation-design-record D5): the
        announcement automation calls this when the ring starts
        playing. ``command_id`` is optional: when the caller supplies
        one it must be a UUID v4 and dedupes; when absent the bridge
        generates one server-side (stdlib uuid4 — verifiably RFC 4122)
        and returns it. OPEN 8's vehicle question is answered this
        way: no HA-side vehicle exists for an automation to generate a
        UUID v4 (template extensions have no uuid function; python_
        script's guarded import allows only _strptime), so the bridge
        generates it. A non-ringing timer acked is a state-checked
        no-op returning the current state. The command_id is OPTIONAL
        here, so this call does not go through the _run_command
        wrapper (there is no id to key an in-flight claim on when the
        caller supplies none); its dedupe check runs only on a
        supplied id, and two concurrent acks with the same supplied
        id are idempotent (both set the same ack time)."""
        now = self._now()
        if command_id is not None:
            validate_command_id(command_id)
        record = self._record_or_raise(timer_id)
        if record.state != "ringing":
            return self._state_response(record, now)
        if command_id is not None:
            stored = self._dedupe_or_none(command_id, now)
            if stored is not None:
                return stored
        else:
            command_id = new_command_id()
        record.ring_ack_at = now
        self._cancel_ack_task(timer_id)
        result = self._state_response(record, now)
        result["command_id"] = command_id
        self._dedupe_store(command_id, timer_id, result, now)
        await self._persist()
        self._notify()
        return result

    # -- read-only helpers for intents / sensors -----------------------------

    def sensor_attributes(self, record: TimerRecord) -> dict:
        """The published attributes for a record (§09 C2)."""
        return timer_sensor_attributes(record, self._now())
