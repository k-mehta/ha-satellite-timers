"""Timer records and the pure timer state machine.

This module has NO Home Assistant imports: every function here is pure
(record in, record out, with an explicit ``now``) so the state machine
is testable without HA. The bridge (``bridge.py``) supplies the live
signals — satellite reachability, the announcement subscriber, the
clock — and this module decides what the record becomes.

Rules implemented here, each cited:

- §09 C3 lifecycle: ``active ↔ paused → ringing → finished``;
  ``cancelled`` terminal (record removed, kept only as an undo
  snapshot); a stopped alarm is never re-rung (Q12(b)); a timer that
  finished while the system was down restores as finished, shown,
  never rung, never announced (Q11).
- §09 C3 finish-time delivery exceptions, precedence A2 → A3 → A13.
  A3 (muted) is CONDITIONAL on a cited real mute signal (XR-16);
  until cited it never triggers — so ``muted`` arrives as a
  three-valued input: True only on a positive reading, None when the
  signal is unreadable (treated as not muted), False when read as
  unmuted.
- §03.5 attributes: observed-or-omitted, never derived. An
  undo-restored record OMITS ``started_at`` — no derived start time
  in sensor attributes, no start-time line on the card (§09 G-09).
- §09 C9 undo: the snapshot restores the exact pre-removal record
  (same timer id, same state, same remaining / finished_at) within the
  5 s window; ``restore_finished`` (new id) is dead.
- §03.2 M1 dedupe table: ``command_id → {timer_id, result,
  completed_at}``, 600 s TTL.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .const import CONTRACT_VERSION, DEDUPE_TTL_SECONDS, UNDO_WINDOW_SECONDS

TIMER_STATES = ("active", "paused", "ringing", "finished")
STARTED_VIA_VALUES = ("voice", "service", "restart", "undo")
ALARM_MISSED_REASONS = ("unreachable", "muted", "no_announcer", "announcer_no_run")


def _parse_time(value: object) -> datetime | None:
    """Parse an ISO-8601 wall-clock timestamp, or None for null."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"timestamp is not a string: {value!r}")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError(f"timestamp is not ISO-8601: {value!r}") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


@dataclass
class TimerRecord:
    """One timer, as the bridge stores it.

    All timestamps are server (HA) wall-clock, UTC. ``remaining_seconds``
    is only stored for a paused record (the frozen remaining); for an
    active record the remaining is computed from ``finishes_at`` at read
    time, for ringing/finished it is 0.
    """

    timer_id: str
    name: str
    duration_seconds: int
    state: str = "active"
    #: Wall-clock start, when observed. None when the bridge never
    #: observed the start (a timer adopted from the TimerManager, or an
    #: undo-restored record — §03.5: never derived, always omitted).
    started_at: datetime | None = None
    #: Wall-clock finish. Set for active and ringing records; null when
    #: paused/finished (§09 C2).
    finishes_at: datetime | None = None
    #: Wall-clock finish moment. Set at finish, preserved across restart.
    finished_at: datetime | None = None
    remaining_seconds: int | None = None
    started_via: str | None = None
    announce_on: str | None = None
    device_id: str | None = None
    area_id: str | None = None
    area_name: str | None = None
    #: The floor the timer's area was on when the timer started,
    #: frozen at start exactly like core's TimerInfo.floor_id
    #: (timers.py:302) — never re-derived from the area registry
    #: (C3-01: an area moved to another floor after the timer
    #: started changed the derived floor and let an ambiguous
    #: cancel silence the wrong timer). None when the device had
    #: no area at start.
    floor_id: str | None = None
    start_hours: int | None = None
    start_minutes: int | None = None
    start_seconds: int | None = None
    language: str = "en"
    alarm_missed: bool = False
    alarm_missed_reason: str | None = None
    ring_started_at: datetime | None = None
    ring_ack_at: datetime | None = None

    def remaining_at(self, now: datetime) -> int:
        """Seconds left at ``now``: computed, never stored (except paused)."""
        if self.state == "paused":
            return self.remaining_seconds or 0
        if self.state in ("ringing", "finished"):
            return 0
        if self.finishes_at is None:
            return 0
        return max(0, int((self.finishes_at - now).total_seconds()))

    def to_dict(self) -> dict:
        """JSON-able form, as stored in the bridge Store."""
        return {
            "timer_id": self.timer_id,
            "name": self.name,
            "duration_seconds": self.duration_seconds,
            "state": self.state,
            "started_at": _iso(self.started_at),
            "finishes_at": _iso(self.finishes_at),
            "finished_at": _iso(self.finished_at),
            "remaining_seconds": self.remaining_seconds,
            "started_via": self.started_via,
            "announce_on": self.announce_on,
            "device_id": self.device_id,
            "area_id": self.area_id,
            "area_name": self.area_name,
            "floor_id": self.floor_id,
            "start_hours": self.start_hours,
            "start_minutes": self.start_minutes,
            "start_seconds": self.start_seconds,
            "language": self.language,
            "alarm_missed": self.alarm_missed,
            "alarm_missed_reason": self.alarm_missed_reason,
            "ring_started_at": _iso(self.ring_started_at),
            "ring_ack_at": _iso(self.ring_ack_at),
        }

    @classmethod
    def from_dict(cls, data: dict) -> TimerRecord:
        """Parse a stored record. Raises ValueError on a bad shape — the
        loader drops that record (never crashes setup on one bad row)."""
        if not isinstance(data, dict):
            raise ValueError("timer record is not an object")
        timer_id = data.get("timer_id")
        if not isinstance(timer_id, str) or not timer_id:
            raise ValueError("timer record has no timer_id")
        name = data.get("name")
        if not isinstance(name, str):
            raise ValueError(f"timer {timer_id} has no name")
        # An empty name is legal: a nameless voice start (core's name
        # slot is optional) stores "" — never a guessed name.
        duration = data.get("duration_seconds")
        if not isinstance(duration, int) or duration < 1:
            raise ValueError(f"timer {timer_id} has no valid duration")
        state = data.get("state")
        if state not in TIMER_STATES:
            raise ValueError(f"timer {timer_id} has unknown state {state!r}")
        record = cls(
            timer_id=timer_id,
            name=name,
            duration_seconds=duration,
            state=state,
            started_at=_parse_time(data.get("started_at")),
            finishes_at=_parse_time(data.get("finishes_at")),
            finished_at=_parse_time(data.get("finished_at")),
            remaining_seconds=data.get("remaining_seconds"),
            started_via=data.get("started_via"),
            announce_on=data.get("announce_on"),
            device_id=data.get("device_id"),
            area_id=data.get("area_id"),
            area_name=data.get("area_name"),
            floor_id=data.get("floor_id"),
            start_hours=data.get("start_hours"),
            start_minutes=data.get("start_minutes"),
            start_seconds=data.get("start_seconds"),
            language=data.get("language") or "en",
            alarm_missed=bool(data.get("alarm_missed", False)),
            alarm_missed_reason=data.get("alarm_missed_reason"),
            ring_started_at=_parse_time(data.get("ring_started_at")),
            ring_ack_at=_parse_time(data.get("ring_ack_at")),
        )
        if record.state == "active" and record.finishes_at is None:
            raise ValueError(f"timer {timer_id} is active with no finishes_at")
        if record.state == "paused" and record.remaining_seconds is None:
            raise ValueError(f"timer {timer_id} is paused with no frozen remaining")
        if record.started_via is not None and record.started_via not in STARTED_VIA_VALUES:
            raise ValueError(f"timer {timer_id} has unknown started_via")
        if (
            record.alarm_missed_reason is not None
            and record.alarm_missed_reason not in ALARM_MISSED_REASONS
        ):
            raise ValueError(f"timer {timer_id} has unknown alarm_missed_reason")
        return record


def restore_record(record: TimerRecord, now: datetime) -> TimerRecord:
    """The restart table (§03.11, §09 C3, G-04): what a stored record
    becomes when the bridge loads it after a restart.

    - finished: unchanged (finished_at kept).
    - ringing: finished, plain — the pending ack observation is lost,
      a stated 5 s residual (§09 C3), never rung again. A stored
      alarm_missed flag, if already set, is kept: it is a fact, not
      a new claim.
    - active whose finishes_at has passed: finished, finished_at =
      the original finishes_at (Q11 down-finished) — shown, never
      rung, never announced.
    - active whose finishes_at is still ahead: active, unchanged
      (the executor is rebuilt from it by the bridge).
    - paused: unchanged, same frozen remaining.
    """
    if record.state == "finished":
        return record
    if record.state == "ringing":
        # to_dict returns ISO strings; from_dict re-parses them, so the
        # override below goes through the same validation as a load.
        return TimerRecord.from_dict(
            {
                **record.to_dict(),
                "state": "finished",
                "finishes_at": None,
                "ring_started_at": None,
                "ring_ack_at": None,
            }
        )
    if record.state == "active":
        assert record.finishes_at is not None  # enforced by from_dict
        if record.finishes_at <= now:
            return TimerRecord.from_dict(
                {
                    **record.to_dict(),
                    "state": "finished",
                    "finished_at": record.finishes_at.isoformat(),
                    "finishes_at": None,
                }
            )
        return record
    return record  # paused


def evaluate_finish(
    *,
    satellite_reachable: bool,
    announcer_present: bool,
    muted: bool | None,
) -> tuple[str, bool, str | None]:
    """Finish-time delivery exceptions (§09 C3), precedence A2 → A3 → A13.

    Returns ``(state, alarm_missed, alarm_missed_reason)``. ``muted``
    is three-valued: True only on a positive reading; None when the
    mute signal is unreadable (treated as not muted — A3 is
    CONDITIONAL, XR-16, and never triggers until a real mute signal
    is cited); False when read as unmuted.
    """
    if not satellite_reachable:
        return ("finished", True, "unreachable")
    if muted is True:
        return ("finished", True, "muted")
    if not announcer_present:
        return ("finished", True, "no_announcer")
    return ("ringing", False, None)


def timer_sensor_attributes(record: TimerRecord, now: datetime) -> dict:
    """The sensor attributes for a record (§09 C2).

    Observed-or-omitted, never derived: ``started_at``,
    ``started_via`` and ``announce_on`` are omitted when the bridge
    never observed them — an adopted timer (started outside the
    bridge) and an undo-restored timer (started_at stripped, §03.5)
    carry no start line. ``finishes_at`` is null when paused/finished.
    ``contract_version: 2`` is always present (§03.6).
    """
    attrs: dict = {
        "timer_id": record.timer_id,
        "timer_name": record.name,
        "duration_seconds": record.duration_seconds,
        "remaining_seconds": record.remaining_at(now),
        "finishes_at": _iso(record.finishes_at),
        "finished_at": _iso(record.finished_at),
        "alarm_missed": record.alarm_missed,
        "alarm_missed_reason": record.alarm_missed_reason,
        "contract_version": CONTRACT_VERSION,
    }
    if record.started_at is not None:
        attrs["started_at"] = _iso(record.started_at)
    if record.started_via is not None:
        attrs["started_via"] = record.started_via
    if record.announce_on is not None:
        attrs["announce_on"] = record.announce_on
    if record.device_id is not None:
        attrs["device_id"] = record.device_id
    return attrs


def undo_snapshot(record: TimerRecord, removed_at: datetime) -> dict:
    """The bridge-owned exact snapshot (§09 C9): the complete
    pre-removal record plus when it was removed. The restore strips
    ``started_at`` (see ``restore_from_snapshot``)."""
    snapshot = record.to_dict()
    snapshot["removed_at"] = removed_at.isoformat()
    return snapshot


def restore_from_snapshot(snapshot: dict, now: datetime) -> TimerRecord:
    """Restore a snapshot taken by ``undo_snapshot`` (§09 C9, §03.7).

    Same timer id, same state, same remaining / finished_at, within
    the 5 s undo window — otherwise ValueError (the bridge maps it to
    the typed UndoExpired / TimerNotFound error). The restored record
    OMITS ``started_at`` (§03.5): no derived start time, ever — a
    ``started_at`` equal to ``finished_at − duration`` fails the G-09
    leg, and omitting it outright is the only form that cannot be
    misread as derived. A snapshot whose state is ``ringing`` restores
    as ``finished`` (Q12: a stopped alarm is never re-rung).
    """
    removed_at = _parse_time(snapshot.get("removed_at"))
    if removed_at is None:
        raise ValueError("undo snapshot has no removed_at")
    if now - removed_at > timedelta(seconds=UNDO_WINDOW_SECONDS):
        raise ValueError("undo window expired")
    record = TimerRecord.from_dict(snapshot)
    record.started_at = None
    if record.state == "ringing":
        record.state = "finished"
        record.finishes_at = None
        record.ring_started_at = None
        record.ring_ack_at = None
    return record


class DedupeTable:
    """``command_id → {timer_id, result, completed_at}`` (§03.2 M1).

    Pure: the clock is passed in as ``now`` so the TTL is testable.
    Entries expire 600 s after ``completed_at``; an expired entry is
    gone, so a retry after the TTL is a new command, not a replay.
    """

    def __init__(self, entries: dict | None = None) -> None:
        self._entries: dict[str, dict] = dict(entries or {})

    def lookup(self, command_id: str, now: datetime) -> dict | None:
        """The stored result for ``command_id``, or None (never seen,
        or expired — an expired entry is dropped, not returned)."""
        entry = self._entries.get(command_id)
        if entry is None:
            return None
        completed_at = _parse_time(entry.get("completed_at"))
        if completed_at is None or now - completed_at > timedelta(
            seconds=DEDUPE_TTL_SECONDS
        ):
            del self._entries[command_id]
            return None
        return entry

    def store(
        self, command_id: str, timer_id: str, result: dict, now: datetime
    ) -> None:
        """Record a completed command. Later deliveries of the same key
        return this stored result and execute nothing."""
        self._entries[command_id] = {
            "timer_id": timer_id,
            "result": result,
            "completed_at": now.isoformat(),
        }

    def prune(self, now: datetime) -> int:
        """Drop expired entries. Returns how many were dropped. The
        boundary agrees with ``lookup`` exactly: an entry is expired
        only once it is MORE than DEDUPE_TTL_SECONDS old, so an entry
        at exactly +600 s is still a hit for both (and at +601 s a
        miss for both). A missing or unparseable ``completed_at``
        (a corrupt or hand-edited Store can hold one — ``from_dict``
        keeps a null, and drops an unparseable one at load) is expired
        too: lookup drops a missing one on access and raises ValueError
        on an unparseable one, so prune must not let either live
        forever (C2-NB-01)."""
        expired = []
        for key, entry in self._entries.items():
            try:
                completed_at = _parse_time(entry.get("completed_at"))
            except ValueError:
                completed_at = None  # unparseable: never a valid completion
            if completed_at is None or completed_at + timedelta(
                seconds=DEDUPE_TTL_SECONDS
            ) < now:
                expired.append(key)
        for key in expired:
            del self._entries[key]
        return len(expired)

    def to_dict(self) -> dict:
        return dict(self._entries)

    @classmethod
    def from_dict(cls, data: dict | None) -> DedupeTable:
        if not isinstance(data, dict):
            return cls()
        kept = {}
        for command_id, entry in data.items():
            if not isinstance(entry, dict):
                continue
            if not isinstance(entry.get("timer_id"), str):
                continue
            if not isinstance(entry.get("result"), dict):
                continue
            try:
                _parse_time(entry.get("completed_at"))
            except ValueError:
                continue
            kept[command_id] = entry
        return cls(kept)


@dataclass
class WriteAheadIntent:
    """A start the bridge has committed to but not completed (§03.2 M2).

    Written WITHOUT a timer id; the id is filled in from the
    ``start_timer`` return value before the sensor is published. The
    intent is the commit point: an intent with no timer id after a
    restart means the start never completed, and a retry with the same
    ``command_id`` starts fresh — that is what makes the reload
    mid-start leg (G-02c) produce no duplicate.
    """

    command_id: str
    name: str
    duration_seconds: int
    device_id: str
    announce_on: str | None
    started_via: str
    created_at: datetime
    start_hours: int | None = None
    start_minutes: int | None = None
    start_seconds: int | None = None
    language: str = "en"
    timer_id: str | None = None

    def to_dict(self) -> dict:
        return {
            "command_id": self.command_id,
            "name": self.name,
            "duration_seconds": self.duration_seconds,
            "device_id": self.device_id,
            "announce_on": self.announce_on,
            "started_via": self.started_via,
            "created_at": self.created_at.isoformat(),
            "start_hours": self.start_hours,
            "start_minutes": self.start_minutes,
            "start_seconds": self.start_seconds,
            "language": self.language,
            "timer_id": self.timer_id,
        }

    @classmethod
    def from_dict(cls, data: dict) -> WriteAheadIntent:
        if not isinstance(data, dict):
            raise ValueError("intent is not an object")
        created_at = _parse_time(data.get("created_at"))
        if created_at is None:
            raise ValueError("intent has no created_at")
        command_id = data.get("command_id")
        name = data.get("name")
        device_id = data.get("device_id")
        if not (
            isinstance(command_id, str)
            and isinstance(name, str)
            and isinstance(device_id, str)
        ):
            raise ValueError("intent is missing command_id, name, or device_id")
        duration = data.get("duration_seconds")
        if not isinstance(duration, int) or duration < 1:
            raise ValueError("intent has no valid duration")
        return cls(
            command_id=command_id,
            name=name,
            duration_seconds=duration,
            device_id=device_id,
            announce_on=data.get("announce_on"),
            started_via=data.get("started_via") or "service",
            created_at=created_at,
            start_hours=data.get("start_hours"),
            start_minutes=data.get("start_minutes"),
            start_seconds=data.get("start_seconds"),
            language=data.get("language") or "en",
            timer_id=data.get("timer_id"),
        )
