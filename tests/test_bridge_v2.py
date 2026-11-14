"""Wave W2 pure-module tests: idempotency, restart table, undo snapshot,
state-checked evaluation, sensor contract, error table.

Covers the modules with no Home Assistant runtime dependency:
``records.py`` (the state machine), ``resolution.py`` (command_id +
satellite resolution), ``errors.py`` (typed errors + the ERS-1 ladder),
``tm_observer.py`` (the TimerManager tap).

Run: ``venv-2026.10.4/bin/python -m pytest tests/ -v`` from the repo
root. The HA-wired modules are covered by the standalone integration
script ``tests/bridge_integration_check.py`` (real HA from the venv)
and by the rig legs — never by stubbing HA here.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from homeassistant.components.intent.timers import TimerEventType
from satellite_timers import errors as errors_mod
from satellite_timers.errors import (
    AmbiguousSatellite,
    BridgeError,
    InvalidCommandId,
    InvalidDuration,
    SatelliteNotSupported,
    TimerNotFound,
    UndoExpired,
    retry_with_ladder,
)
from satellite_timers.records import (
    DedupeTable,
    TimerRecord,
    WriteAheadIntent,
    evaluate_finish,
    restore_from_snapshot,
    restore_record,
    timer_sensor_attributes,
    undo_snapshot,
)
from satellite_timers.resolution import (
    is_uuid_v4,
    new_command_id,
    resolve_satellite_device,
    validate_command_id,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

NOW = datetime(2026, 11, 14, 12, 0, 0, tzinfo=timezone.utc)


def _record(**overrides) -> TimerRecord:
    base = {
        "timer_id": "01K8TEST0000000000000001",
        "name": "Pasta",
        "duration_seconds": 600,
        "state": "active",
        "started_at": NOW,
        "finishes_at": NOW + timedelta(seconds=600),
        "started_via": "service",
        "announce_on": "assist_satellite.kitchen",
        "device_id": "device-kitchen",
    }
    base.update(overrides)
    return TimerRecord(**base)


def _fake_tm():
    """A minimal TimerManager stand-in for the tap tests: a dict of
    handlers, like the real ``tm.handlers``."""

    class FakeTM:
        def __init__(self):
            self.handlers: dict = {}

    return FakeTM()


# ---------------------------------------------------------------------------
# resolution: UUID v4
# ---------------------------------------------------------------------------


class TestUuidV4:
    def test_valid_v4_passes(self):
        assert is_uuid_v4("123e4567-e89b-42d3-a456-426614174000") is True
        assert is_uuid_v4(new_command_id()) is True

    def test_generated_id_is_v4(self):
        generated = uuid.UUID(new_command_id())
        assert generated.version == 4
        assert generated.variant == uuid.RFC_4122

    @pytest.mark.parametrize(
        "value",
        [
            "not-a-uuid",
            "",
            "123e4567-e89b-12d3-a456-426614174000",  # version 1
            "123e4567-e89b-32d3-a456-426614174000",  # version 3
            "123e4567-e89b-52d3-a456-426614174000",  # version 5
            "123E4567-E89B-42D3-A456-426614174000",  # uppercase (not canonical)
            "{123e4567-e89b-42d3-a456-426614174000}",  # braces
            "123e4567e89b42d3a456426614174000",  # no dashes
            None,
            123,
        ],
    )
    def test_invalid_values_fail(self, value):
        assert is_uuid_v4(value) is False

    def test_validate_returns_the_value(self):
        assert validate_command_id("123e4567-e89b-42d3-a456-426614174000") == (
            "123e4567-e89b-42d3-a456-426614174000"
        )

    def test_validate_raises_typed_error(self):
        with pytest.raises(InvalidCommandId):
            validate_command_id("not-a-uuid")


# ---------------------------------------------------------------------------
# resolution: satellite resolution
# ---------------------------------------------------------------------------


class TestResolveSatellite:
    def test_explicit_device_wins(self):
        assert (
            resolve_satellite_device(
                ["a", "b"], primary="b", explicit_device_id="a"
            )
            == "a"
        )

    def test_explicit_device_not_live_raises(self):
        with pytest.raises(SatelliteNotSupported, match="not a timer satellite"):
            resolve_satellite_device(["a"], primary=None, explicit_device_id="zzz")

    def test_primary_wins_over_single(self):
        assert (
            resolve_satellite_device(["a", "b"], primary="b", explicit_device_id=None)
            == "b"
        )

    def test_primary_missing_is_a_loud_error(self):
        with pytest.raises(SatelliteNotSupported, match="Configure"):
            resolve_satellite_device(["a"], primary="gone", explicit_device_id=None)

    def test_single_live_device(self):
        assert (
            resolve_satellite_device(["only"], primary=None, explicit_device_id=None)
            == "only"
        )

    def test_no_devices_is_a_loud_error(self):
        with pytest.raises(AmbiguousSatellite, match="No timer satellite"):
            resolve_satellite_device([], primary=None, explicit_device_id=None)

    def test_several_without_primary_never_silently_picks(self):
        with pytest.raises(AmbiguousSatellite) as excinfo:
            resolve_satellite_device(["a", "b"], primary=None, explicit_device_id=None)
        message = str(excinfo.value)
        assert "Configure" in message
        assert "Okay Nabu" in message

    def test_empty_primary_means_unset(self):
        assert (
            resolve_satellite_device(["only"], primary="", explicit_device_id=None)
            == "only"
        )


# ---------------------------------------------------------------------------
# records: round-trip + validation
# ---------------------------------------------------------------------------


class TestTimerRecord:
    def test_round_trip(self):
        record = _record()
        parsed = TimerRecord.from_dict(record.to_dict())
        assert parsed == record

    def test_floor_id_round_trips(self):
        # C3-01: the floor core freezes at start rides on the record
        # through the Store, so the matchers never re-derive it.
        record = _record(floor_id="upper")
        parsed = TimerRecord.from_dict(record.to_dict())
        assert parsed == record
        assert parsed.floor_id == "upper"

    def test_floor_id_absent_in_old_store_loads_as_none(self):
        # A record persisted before the floor was stored (no floor_id
        # key) loads with floor_id None — the pre-fix shape, not a
        # crash. (C4-NB-01: that None CAN match the floor tiebreak —
        # the tiebreak is a plain equality, so it matches a
        # floor-less speaker area (None == None) and is invisible
        # to a floored one, exactly as core does for a legitimate
        # None-floor timer; excluding None would break that case,
        # so no matcher change. Such rows can only exist from the
        # unshipped round-2 code, and the live hub never ran any
        # W2 code, so no production row is affected.)
        data = _record().to_dict()
        del data["floor_id"]  # a pre-fix Store row has no such key
        assert TimerRecord.from_dict(data).floor_id is None

    def test_empty_name_is_legal(self):
        # A nameless voice start stores "" — never a guessed name.
        record = _record(name="")
        assert TimerRecord.from_dict(record.to_dict()).name == ""

    @pytest.mark.parametrize(
        "mutation",
        [
            {"timer_id": ""},
            {"name": None},
            {"duration_seconds": 0},
            {"duration_seconds": "600"},
            {"state": "running"},
            {"state": "cancelled"},
            {"started_via": "telepathy"},
            {"alarm_missed_reason": "satellite_on_fire"},
            {"finishes_at": "not-a-time"},
        ],
    )
    def test_bad_shape_rejected(self, mutation):
        data = _record().to_dict()
        data.update(mutation)
        with pytest.raises(ValueError):
            TimerRecord.from_dict(data)

    def test_active_without_finishes_at_rejected(self):
        data = _record().to_dict()
        data["finishes_at"] = None
        with pytest.raises(ValueError):
            TimerRecord.from_dict(data)

    def test_paused_without_frozen_remaining_rejected(self):
        data = _record(state="paused", finishes_at=None, remaining_seconds=None).to_dict()
        with pytest.raises(ValueError):
            TimerRecord.from_dict(data)

    def test_remaining_active_is_computed(self):
        record = _record()
        assert record.remaining_at(NOW + timedelta(seconds=120)) == 480

    def test_remaining_never_negative(self):
        record = _record()
        assert record.remaining_at(NOW + timedelta(seconds=9999)) == 0

    def test_remaining_paused_is_frozen(self):
        record = _record(
            state="paused", finishes_at=None, remaining_seconds=321
        )
        assert record.remaining_at(NOW + timedelta(seconds=9999)) == 321

    def test_remaining_ringing_and_finished_are_zero(self):
        for state in ("ringing", "finished"):
            record = _record(state=state, finishes_at=None)
            assert record.remaining_at(NOW) == 0


# ---------------------------------------------------------------------------
# records: restart table (G-04)
# ---------------------------------------------------------------------------


class TestRestoreRecord:
    def test_finished_unchanged(self):
        record = _record(
            state="finished",
            finishes_at=None,
            finished_at=NOW - timedelta(seconds=30),
        )
        assert restore_record(record, NOW) == record

    def test_ringing_becomes_plain_finished(self):
        record = _record(
            state="ringing",
            finished_at=NOW - timedelta(seconds=10),
            ring_started_at=NOW - timedelta(seconds=10),
            alarm_missed=True,
            alarm_missed_reason="unreachable",
        )
        restored = restore_record(record, NOW)
        assert restored.state == "finished"
        assert restored.finishes_at is None
        assert restored.ring_started_at is None
        assert restored.ring_ack_at is None
        # A stored missed flag is a fact: kept, not re-decided.
        assert restored.alarm_missed is True
        assert restored.alarm_missed_reason == "unreachable"
        assert restored.finished_at == record.finished_at

    def test_active_overdue_becomes_down_finished(self):
        # Q11: down-finished — shown, never rings, never announced.
        record = _record(finishes_at=NOW - timedelta(seconds=5))
        restored = restore_record(record, NOW)
        assert restored.state == "finished"
        assert restored.finished_at == record.finishes_at
        assert restored.finishes_at is None
        assert restored.alarm_missed is False

    def test_active_future_stays_active(self):
        record = _record(finishes_at=NOW + timedelta(seconds=300))
        assert restore_record(record, NOW) == record

    def test_paused_unchanged(self):
        record = _record(
            state="paused", finishes_at=None, remaining_seconds=123
        )
        restored = restore_record(record, NOW)
        assert restored.state == "paused"
        assert restored.remaining_seconds == 123


# ---------------------------------------------------------------------------
# records: finish evaluation (A2 -> A3 -> A13)
# ---------------------------------------------------------------------------


class TestEvaluateFinish:
    def test_unreachable_wins(self):
        assert evaluate_finish(
            satellite_reachable=False, announcer_present=True, muted=None
        ) == ("finished", True, "unreachable")

    def test_unreachable_beats_everything(self):
        assert evaluate_finish(
            satellite_reachable=False, announcer_present=False, muted=True
        ) == ("finished", True, "unreachable")

    def test_muted_is_second(self):
        assert evaluate_finish(
            satellite_reachable=True, announcer_present=True, muted=True
        ) == ("finished", True, "muted")

    def test_muted_unknown_means_not_muted(self):
        assert evaluate_finish(
            satellite_reachable=True, announcer_present=True, muted=None
        ) == ("ringing", False, None)

    def test_no_announcer_is_third(self):
        assert evaluate_finish(
            satellite_reachable=True, announcer_present=False, muted=None
        ) == ("finished", True, "no_announcer")

    def test_all_good_rings(self):
        assert evaluate_finish(
            satellite_reachable=True, announcer_present=True, muted=False
        ) == ("ringing", False, None)


# ---------------------------------------------------------------------------
# records: sensor contract (§09 C2)
# ---------------------------------------------------------------------------


class TestSensorAttributes:
    def test_active_attributes(self):
        attrs = timer_sensor_attributes(_record(), NOW)
        assert attrs["timer_id"] == "01K8TEST0000000000000001"
        assert attrs["timer_name"] == "Pasta"
        assert attrs["duration_seconds"] == 600
        assert attrs["remaining_seconds"] == 600
        assert attrs["finishes_at"] is not None
        assert attrs["finished_at"] is None
        assert attrs["started_at"] is not None
        assert attrs["started_via"] == "service"
        assert attrs["announce_on"] == "assist_satellite.kitchen"
        assert attrs["device_id"] == "device-kitchen"
        assert attrs["alarm_missed"] is False
        assert attrs["alarm_missed_reason"] is None
        assert attrs["contract_version"] == 2

    def test_paused_attributes_null_finishes_at(self):
        record = _record(
            state="paused", finishes_at=None, remaining_seconds=321
        )
        attrs = timer_sensor_attributes(record, NOW)
        assert attrs["finishes_at"] is None
        assert attrs["remaining_seconds"] == 321

    def test_adopted_timer_omits_unobserved_fields(self):
        # A timer adopted from the TimerManager: started_at, started_via
        # and announce_on are omitted — never derived.
        record = _record(started_at=None, started_via=None, announce_on=None)
        attrs = timer_sensor_attributes(record, NOW)
        assert "started_at" not in attrs
        assert "started_via" not in attrs
        assert "announce_on" not in attrs
        assert attrs["contract_version"] == 2

    def test_finished_attributes(self):
        record = _record(
            state="finished",
            finishes_at=None,
            finished_at=NOW - timedelta(seconds=10),
            alarm_missed=True,
            alarm_missed_reason="no_announcer",
        )
        attrs = timer_sensor_attributes(record, NOW)
        assert attrs["finishes_at"] is None
        assert attrs["finished_at"] is not None
        assert attrs["remaining_seconds"] == 0
        assert attrs["alarm_missed"] is True
        assert attrs["alarm_missed_reason"] == "no_announcer"


# ---------------------------------------------------------------------------
# records: undo snapshot (§09 C9, G-09)
# ---------------------------------------------------------------------------


class TestUndo:
    def test_exact_restore(self):
        record = _record()
        snapshot = undo_snapshot(record, NOW)
        restored = restore_from_snapshot(snapshot, NOW + timedelta(seconds=2))
        assert restored.timer_id == record.timer_id
        assert restored.state == record.state
        assert restored.finishes_at == record.finishes_at
        assert restored.duration_seconds == record.duration_seconds
        assert restored.name == record.name

    def test_started_at_is_stripped_even_when_observed(self):
        # G-09: a started_at equal to finished_at - duration FAILS the
        # leg. Stripping is the settled rule, not a derivation.
        record = _record(
            state="finished",
            finishes_at=None,
            finished_at=NOW - timedelta(seconds=30),
        )
        assert record.started_at is not None
        snapshot = undo_snapshot(record, NOW)
        restored = restore_from_snapshot(snapshot, NOW + timedelta(seconds=1))
        assert restored.started_at is None
        # ...and the finished instant survives untouched.
        assert restored.finished_at == record.finished_at

    def test_paused_restore_keeps_frozen_remaining(self):
        record = _record(
            state="paused", finishes_at=None, remaining_seconds=321
        )
        snapshot = undo_snapshot(record, NOW)
        restored = restore_from_snapshot(snapshot, NOW + timedelta(seconds=1))
        assert restored.state == "paused"
        assert restored.remaining_seconds == 321

    def test_ringing_restore_becomes_finished(self):
        # Q12: a stopped alarm is never re-rung.
        record = _record(
            state="ringing",
            finished_at=NOW - timedelta(seconds=5),
            ring_started_at=NOW - timedelta(seconds=5),
        )
        snapshot = undo_snapshot(record, NOW)
        restored = restore_from_snapshot(snapshot, NOW + timedelta(seconds=1))
        assert restored.state == "finished"
        assert restored.finishes_at is None

    def test_window_expiry_refused(self):
        snapshot = undo_snapshot(_record(), NOW)
        with pytest.raises(ValueError):
            restore_from_snapshot(snapshot, NOW + timedelta(seconds=6))

    def test_window_boundary_is_inclusive(self):
        # At exactly 5 s the snapshot still restores (5 s window).
        snapshot = undo_snapshot(_record(), NOW)
        restored = restore_from_snapshot(snapshot, NOW + timedelta(seconds=5))
        assert restored.timer_id == "01K8TEST0000000000000001"

    def test_snapshot_without_removed_at_refused(self):
        snapshot = undo_snapshot(_record(), NOW)
        del snapshot["removed_at"]
        with pytest.raises(ValueError):
            restore_from_snapshot(snapshot, NOW)


# ---------------------------------------------------------------------------
# records: dedupe table (M1)
# ---------------------------------------------------------------------------


class TestDedupeTable:
    def test_store_and_lookup(self):
        table = DedupeTable()
        result = {"timer_id": "x", "state": "active"}
        table.store("cmd-1", "x", result, NOW)
        found = table.lookup("cmd-1", NOW + timedelta(seconds=60))
        assert found is not None
        assert found["result"] == result
        assert found["timer_id"] == "x"

    def test_unknown_command_is_none(self):
        assert DedupeTable().lookup("never-seen", NOW) is None

    def test_entry_expires_after_ttl(self):
        table = DedupeTable()
        table.store("cmd-1", "x", {"state": "active"}, NOW)
        assert table.lookup("cmd-1", NOW + timedelta(seconds=599)) is not None
        assert table.lookup("cmd-1", NOW + timedelta(seconds=600)) is not None
        assert table.lookup("cmd-1", NOW + timedelta(seconds=601)) is None

    def test_prune_drops_only_expired(self):
        table = DedupeTable()
        table.store("old", "a", {}, NOW - timedelta(seconds=700))
        table.store("new", "b", {}, NOW)
        assert table.prune(NOW) == 1
        assert table.lookup("new", NOW) is not None
        assert table.lookup("old", NOW) is None

    def test_prune_agrees_with_lookup_on_the_boundary(self):
        # C-04: prune and lookup must share one TTL boundary — an
        # entry at exactly +600 s is still a hit for both, and at
        # +601 s a miss for both. (The first version of prune used
        # <= and dropped the +600 s entry lookup still returned.)
        table = DedupeTable()
        table.store("edge", "a", {}, NOW - timedelta(seconds=600))
        table.store("past", "b", {}, NOW - timedelta(seconds=601))
        assert table.prune(NOW) == 1
        assert table.lookup("edge", NOW) is not None
        assert table.lookup("past", NOW) is None

    def test_prune_drops_null_completed_at(self):
        # C2-NB-01: a null completed_at (reachable only via a corrupt
        # or hand-edited Store — from_dict keeps it, the writer
        # always writes an ISO string) must not live forever: lookup
        # drops it on access, and prune must drop it too, instead of
        # the old `or now` making it unprunable. No lookup first:
        # prune alone has to drop it.
        table = DedupeTable.from_dict(
            {
                "null-time": {
                    "timer_id": "a",
                    "result": {},
                    "completed_at": None,
                },
                "fresh": {
                    "timer_id": "c",
                    "result": {},
                    "completed_at": NOW.isoformat(),
                },
            }
        )
        assert table.prune(NOW) == 1
        assert table.lookup("null-time", NOW) is None
        assert table.lookup("fresh", NOW) is not None

    def test_prune_drops_unparseable_completed_at(self):
        # Same root as above, via the constructor (from_dict drops an
        # unparseable timestamp at load, so the entry can only arrive
        # this way in-memory): prune drops it and does not raise.
        table = DedupeTable(
            {
                "bad-time": {
                    "timer_id": "b",
                    "result": {},
                    "completed_at": "not-a-time",
                },
                "fresh": {
                    "timer_id": "c",
                    "result": {},
                    "completed_at": NOW.isoformat(),
                },
            }
        )
        assert table.prune(NOW) == 1
        assert table.lookup("fresh", NOW) is not None

    def test_from_dict_drops_malformed_rows(self):
        table = DedupeTable.from_dict(
            {
                "good": {
                    "timer_id": "x",
                    "result": {},
                    "completed_at": NOW.isoformat(),
                },
                "no-timer": {"result": {}, "completed_at": NOW.isoformat()},
                "no-result": {"timer_id": "x", "completed_at": NOW.isoformat()},
                "bad-time": {
                    "timer_id": "x",
                    "result": {},
                    "completed_at": "not-a-time",
                },
            }
        )
        assert table.lookup("good", NOW) is not None
        assert table.lookup("no-timer", NOW) is None
        assert table.lookup("no-result", NOW) is None
        assert table.lookup("bad-time", NOW) is None

    def test_round_trip(self):
        table = DedupeTable()
        table.store("cmd-1", "x", {"state": "active"}, NOW)
        assert DedupeTable.from_dict(table.to_dict()).lookup("cmd-1", NOW) is not None


# ---------------------------------------------------------------------------
# records: write-ahead intent (M2)
# ---------------------------------------------------------------------------


class TestWriteAheadIntent:
    def test_round_trip_without_timer_id(self):
        intent = WriteAheadIntent(
            command_id="cmd-1",
            name="Pasta",
            duration_seconds=600,
            device_id="device-kitchen",
            announce_on=None,
            started_via="service",
            created_at=NOW,
        )
        parsed = WriteAheadIntent.from_dict(intent.to_dict())
        assert parsed == intent
        assert parsed.timer_id is None  # written WITHOUT a timer id

    def test_missing_created_at_rejected(self):
        data = WriteAheadIntent(
            command_id="cmd-1",
            name="Pasta",
            duration_seconds=600,
            device_id="device-kitchen",
            announce_on=None,
            started_via="service",
            created_at=NOW,
        ).to_dict()
        del data["created_at"]
        with pytest.raises(ValueError):
            WriteAheadIntent.from_dict(data)


# ---------------------------------------------------------------------------
# errors: codes + the ERS-1 ladder
# ---------------------------------------------------------------------------


class TestErrorCodes:
    @pytest.mark.parametrize(
        "error_cls, code",
        [
            (TimerNotFound, "timer_not_found"),
            (InvalidCommandId, "invalid_command_id"),
            (AmbiguousSatellite, "ambiguous_satellite"),
            (SatelliteNotSupported, "satellite_not_supported"),
            (InvalidDuration, "invalid_duration"),
            (UndoExpired, "undo_expired"),
        ],
    )
    def test_codes(self, error_cls, code):
        assert error_cls.code == code
        assert isinstance(error_cls("msg"), BridgeError)


class TestRetryLadder:
    def test_succeeds_on_second_attempt(self):
        attempts = {"n": 0}

        async def flaky():
            attempts["n"] += 1
            if attempts["n"] < 2:
                raise RuntimeError("boom")
            return "ok"

        with patch.object(
            errors_mod.asyncio, "sleep", new=AsyncMock()
        ) as mock_sleep:
            result = asyncio.run(retry_with_ladder("test op", flaky))
        assert result == "ok"
        assert attempts["n"] == 2
        mock_sleep.assert_called_once_with(2)

    def test_exhausts_after_four_attempts(self):
        attempts = {"n": 0}

        async def always_fails():
            attempts["n"] += 1
            raise RuntimeError("boom")

        with patch.object(
            errors_mod.asyncio, "sleep", new=AsyncMock()
        ) as mock_sleep:
            with pytest.raises(RuntimeError, match="boom"):
                asyncio.run(retry_with_ladder("test op", always_fails))
        assert attempts["n"] == 4
        # Delays are [2, 8, 30] — no sleep after the fourth attempt.
        assert [call.args[0] for call in mock_sleep.call_args_list] == [2, 8, 30]

    def test_first_attempt_succeeds_without_sleep(self):
        async def fine():
            return 42

        with patch.object(
            errors_mod.asyncio, "sleep", new=AsyncMock()
        ) as mock_sleep:
            assert asyncio.run(retry_with_ladder("test op", fine)) == 42
        mock_sleep.assert_not_called()


# ---------------------------------------------------------------------------
# tm_observer: the tap
# ---------------------------------------------------------------------------


class TestTimerManagerTap:
    def test_original_handler_runs_first_then_listener(self):
        from satellite_timers.tm_observer import TimerManagerTap

        calls: list[str] = []
        tm = _fake_tm()
        tm.handlers["dev-1"] = lambda event, timer: calls.append("original")
        tap = TimerManagerTap(tm, lambda d, e, t: calls.append("listener"))
        tap.install()
        tm.handlers["dev-1"](TimerEventType.STARTED, SimpleNamespace(id="t1"))
        assert calls == ["original", "listener"]

    def test_listener_failure_never_breaks_the_satellite(self):
        from satellite_timers.tm_observer import TimerManagerTap

        calls: list[str] = []
        tm = _fake_tm()
        tm.handlers["dev-1"] = lambda event, timer: calls.append("original")

        def broken_listener(device_id, event, timer):
            raise RuntimeError("listener blew up")

        tap = TimerManagerTap(tm, broken_listener)
        tap.install()
        tm.handlers["dev-1"](TimerEventType.STARTED, SimpleNamespace(id="t1"))
        assert calls == ["original"]

    def test_double_wrap_is_prevented(self):
        from satellite_timers.tm_observer import TimerManagerTap

        seen: list[str] = []
        tm = _fake_tm()
        tm.handlers["dev-1"] = lambda event, timer: seen.append("original")
        tap = TimerManagerTap(tm, lambda d, e, t: seen.append("listener"))
        tap.install()
        tap.install()  # second install is a no-op
        tm.handlers["dev-1"](TimerEventType.STARTED, SimpleNamespace(id="t1"))
        assert seen == ["original", "listener"]

    def test_re_registration_rewraps(self):
        from satellite_timers.tm_observer import TimerManagerTap

        seen: list[str] = []
        tm = _fake_tm()
        tm.handlers["dev-1"] = lambda event, timer: seen.append("first")
        tap = TimerManagerTap(tm, lambda d, e, t: seen.append("listener"))
        tap.install()
        tm.handlers["dev-1"] = lambda event, timer: seen.append("second")
        tm.handlers["dev-1"](TimerEventType.STARTED, SimpleNamespace(id="t1"))
        assert seen == ["second", "listener"]

    def test_uninstall_restores_the_original(self):
        from satellite_timers.tm_observer import TimerManagerTap

        seen: list[str] = []
        tm = _fake_tm()
        original = lambda event, timer: seen.append("original")  # noqa: E731
        tm.handlers["dev-1"] = original
        tap = TimerManagerTap(tm, lambda d, e, t: seen.append("listener"))
        tap.install()
        tap.uninstall()
        assert tm.handlers["dev-1"] is original
        assert not isinstance(tm.handlers, dict) or type(tm.handlers) is dict
        tm.handlers["dev-1"](TimerEventType.STARTED, SimpleNamespace(id="t1"))
        assert seen == ["original"]

    def test_reassert_reinstalls_after_wholesale_replacement(self):
        from satellite_timers.tm_observer import TimerManagerTap

        seen: list[str] = []
        tm = _fake_tm()
        tm.handlers["dev-1"] = lambda event, timer: seen.append("original")
        tap = TimerManagerTap(tm, lambda d, e, t: seen.append("listener"))
        tap.install()
        assert tap.installed is True
        # The handlers dict is replaced wholesale (a new plain dict).
        tm.handlers = {"dev-1": lambda event, timer: seen.append("replaced")}
        assert tap.installed is False
        assert tap.reassert() is True
        assert tap.installed is True
        tm.handlers["dev-1"](TimerEventType.STARTED, SimpleNamespace(id="t1"))
        assert seen == ["replaced", "listener"]

    def test_reassert_is_a_noop_when_the_tap_is_live(self):
        from satellite_timers.tm_observer import TimerManagerTap

        tm = _fake_tm()
        tap = TimerManagerTap(tm, lambda d, e, t: None)
        tap.install()
        assert tap.reassert() is False

    def test_re_registration_calls_the_liveness_callback(self):
        # C-02: every (re-)registration through the tap is liveness —
        # the bridge re-issues pending stops on it (§03.10/AD-C1).
        from satellite_timers.tm_observer import TimerManagerTap

        registered: list[str] = []
        tm = _fake_tm()
        tm.handlers["dev-1"] = lambda event, timer: None
        tap = TimerManagerTap(
            tm,
            lambda d, e, t: None,
            on_handler_registered=registered.append,
        )
        tap.install()
        # Install wraps the pre-existing handler: a handler present at
        # setup is live, so the callback fires for it.
        assert registered == ["dev-1"]
        registered.clear()
        tm.handlers["dev-1"] = lambda event, timer: None
        assert registered == ["dev-1"]
        # A second install is a no-op: no second callback.
        tap.install()
        assert registered == ["dev-1"]

    def test_removing_a_handler_is_not_liveness(self):
        # Dropping a handler (pop) is not a (re-)registration: the tap
        # does not intercept it and no callback fires.
        from satellite_timers.tm_observer import TimerManagerTap

        registered: list[str] = []
        tm = _fake_tm()
        tm.handlers["dev-1"] = lambda event, timer: None
        tap = TimerManagerTap(
            tm,
            lambda d, e, t: None,
            on_handler_registered=registered.append,
        )
        tap.install()
        registered.clear()
        tm.handlers.pop("dev-1", None)
        assert registered == []
