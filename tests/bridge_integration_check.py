"""Standalone in-process integration check for the Wave W2 bridge.

Boots REAL Home Assistant (venv, HA 2026.10.4) in-process — the same
boot shape ``test_timer_controls.py`` uses — and exercises the bridge
end-to-end: services, intents, sensors, the restart table, the ringing
model, and the options flow. Prints PASS/FAIL per check and exits
non-zero on any failure. NOT collected by pytest (it needs a real HA
boot, and the pure modules are pytest's job).

Run:
  ~/workspace/ha-repro/venv-2026.10.4/bin/python tests/bridge_integration_check.py

Uses a fresh per-run config dir under /tmp (mkdtemp), so it never
touches the rebuild rig's config and parallel runs never share a
Store (C-NB-04).
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import sys
import tempfile
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

logging.basicConfig(level=logging.WARNING)
logging.getLogger("homeassistant").setLevel(logging.WARNING)
LOG = logging.getLogger("w2-integration")

SRC = Path(
    "/home/hatch/workspace/goals/diy-family-hub-raspberry-pi"
    "/files/ha-satellite-timers-clone/custom_components/satellite_timers"
)
# A fresh per-run config dir (mkdtemp, C-NB-04): the old fixed
# /tmp/w2-integration-config let a parallel run rewrite the Store
# under this run. The dir is printed at the start so a failed run
# stays diagnosable; nothing deletes it.
CONFIG_DIR = Path(tempfile.mkdtemp(prefix="w2-integration-"))

FAILURES: list[str] = []


def check(label: str, cond: bool) -> None:
    print(f"{'PASS  ' if cond else 'FAIL  '}{label}", flush=True)
    if not cond:
        FAILURES.append(label)


def show(label: str, value) -> None:
    print(f"      {label}: {value!r}", flush=True)


def new_command_id() -> str:
    return str(uuid.uuid4())


async def main() -> int:
    print(f"config dir: {CONFIG_DIR}", flush=True)
    dest = CONFIG_DIR / "custom_components" / "satellite_timers"
    shutil.copytree(SRC, dest)
    (CONFIG_DIR / ".storage").mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(CONFIG_DIR))

    from homeassistant.core import HomeAssistant
    from homeassistant import bootstrap as bootstrap_module
    from homeassistant import config_entries as config_entries_module
    from homeassistant import loader as loader_module
    from homeassistant import setup as setup_module
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er
    from homeassistant.helpers import intent as intent_helpers
    from homeassistant.components.intent import const as intent_const
    from homeassistant.components.intent.timers import (
        TimerManager,
        async_register_timer_handler,
    )

    hass = HomeAssistant(str(CONFIG_DIR))
    try:
        await hass.async_start()
        loader_module.async_setup(hass)
        hass.config_entries = config_entries_module.ConfigEntries(hass, {})
        await bootstrap_module.async_load_base_functionality(hass)

        from homeassistant.auth import auth_manager_from_config

        hass.auth = await auth_manager_from_config(hass, [], [])
        check("auth manager boots", hass.auth is not None)
        check("http boots", await setup_module.async_setup_component(hass, "http", {}))
        check(
            "homeassistant boots",
            await setup_module.async_setup_component(hass, "homeassistant", {}),
        )
        check(
            "intent boots",
            await setup_module.async_setup_component(hass, "intent", {}),
        )
        check(
            "conversation boots",
            await setup_module.async_setup_component(hass, "conversation", {}),
        )

        tm: TimerManager = hass.data[intent_const.TIMER_DATA]

        from homeassistant.config_entries import ConfigEntry, ConfigEntryState
        from homeassistant.data_entry_flow import FlowResultType

        def make_device(name, mac):
            stub = ConfigEntry(
                version=1, minor_version=1, domain="esphome", title=name,
                data={}, source="user", options={},
                discovery_keys=MappingProxyType({}), subentries_data=[],
                unique_id=None,
            )
            hass.config_entries._entries[stub.entry_id] = stub
            return dr.async_get(hass).async_get_or_create(
                config_entry_id=stub.entry_id,
                connections={("mac", mac)},
                manufacturer="Open Home Foundation",
                model="Linux Voice Assistant",
                name=name,
            )

        kitchen = make_device("kitchen_hub", "aa:bb:cc:dd:ee:ff")
        device_id = kitchen.id
        other = make_device("other_sat", "aa:bb:cc:dd:ee:00")
        other_device_id = other.id

        handler_calls: list = []

        def kitchen_handler(event_type, timer):
            handler_calls.append(("kitchen:" + str(event_type), timer.id, timer.name))

        def other_handler(event_type, timer):
            handler_calls.append(("other:" + str(event_type), timer.id, timer.name))

        async_register_timer_handler(hass, device_id, kitchen_handler)

        entry = ConfigEntry(
            version=1, minor_version=1, domain="satellite_timers",
            title="Satellite Timers", data={},
            source="user", options={}, discovery_keys=MappingProxyType({}),
            subentries_data=[], unique_id=None,
        )
        await hass.config_entries.async_add(entry)
        await hass.async_block_till_done()
        check("entry loaded", entry.state is ConfigEntryState.LOADED)

        bridge = hass.data["satellite_timers"][entry.entry_id]["bridge"]

        async def settle():
            await hass.async_block_till_done()
            await asyncio.sleep(0.3)
            await hass.async_block_till_done()

        def sensor_ids():
            return [
                s
                for s in hass.states.async_entity_ids("sensor")
                if "satellite_timer" in s
            ]

        def state_of(entity_id):
            state = hass.states.get(entity_id)
            return state.state if state is not None else None

        async def call_service(service, data, domain="satellite_timers"):
            return await hass.services.async_call(
                domain, service, data, blocking=True, return_response=True
            )

        # ================= I1: version sensor, zero timers ================
        print("\n== I1 version sensor (zero timers) ==", flush=True)
        version_state = hass.states.get("sensor.family_hub_bridge_version")
        check("version sensor exists", version_state is not None)
        if version_state is not None:
            show("version sensor state", version_state.state)
            check("version sensor state is 2.0.0", version_state.state == "2.0.0")
            check(
                "min_card_version is 2.0.0",
                version_state.attributes.get("min_card_version") == "2.0.0",
            )
        check("no timer sensors yet", sensor_ids() == [])

        # ================= I2: TM-direct start adoption ===================
        print("\n== I2 TM-direct start is adopted (rig seed path) ==", flush=True)
        tid_direct = tm.start_timer(device_id, None, 10, None, "en", name="Pasta")
        await settle()
        check("one sensor after a TM-direct start", len(sensor_ids()) == 1)
        entity_id = f"sensor.satellite_timer_{tid_direct.lower()}"
        check("sensor entity id is the timer id", entity_id in sensor_ids())
        state = hass.states.get(entity_id)
        attrs = state.attributes if state is not None else {}
        show("adopted sensor state", state.state if state else None)
        check("adopted sensor state is active", state is not None and state.state == "active")
        check("adopted record omits started_at", "started_at" not in attrs)
        check("adopted record omits started_via", "started_via" not in attrs)
        check("contract_version is 2", attrs.get("contract_version") == 2)
        tm.cancel_timer(tid_direct)
        await settle()
        check("cancel removes the adopted sensor", sensor_ids() == [])

        # ================= I3: service start + dedupe =====================
        print("\n== I3 service start + dedupe ==", flush=True)
        cmd1 = new_command_id()
        r1 = await call_service(
            "start_timer",
            {"command_id": cmd1, "name": "Rice", "duration_seconds": 600},
        )
        show("start response", r1)
        tid1 = r1["timer_id"]
        check("start returns the timer id", bool(tid1))
        check(
            "start returns the sensor entity id",
            r1.get("entity_id") == f"sensor.satellite_timer_{tid1.lower()}",
        )
        await settle()
        check("one sensor after one start", len(sensor_ids()) == 1)
        r1_retry = await call_service(
            "start_timer",
            {"command_id": cmd1, "name": "Rice", "duration_seconds": 600},
        )
        check("same command_id returns the stored result", r1_retry["timer_id"] == tid1)
        await settle()
        check("retry made no duplicate", len(sensor_ids()) == 1)
        r2 = await call_service(
            "start_timer",
            {
                "command_id": new_command_id(),
                "name": "Rice",
                "duration_seconds": 600,
            },
        )
        check("a new command_id starts a second timer", r2["timer_id"] != tid1)
        await settle()
        check("two sensors for two timers", len(sensor_ids()) == 2)
        tid2 = r2["timer_id"]

        # ================= I3b: reconcile prunes the dedupe table =======
        print("\n== I3b reconcile prunes the dedupe table (C-04) ==", flush=True)
        # An entry that expired 700 s ago and one that is still fresh;
        # the 30 s reconcile (called directly here) must drop only the
        # expired one — in memory AND in the Store file. The in-memory
        # check reads the table directly (a lookup would drop an
        # expired entry itself, so it cannot prove prune ran).
        old_cmd = new_command_id()
        fresh_cmd = new_command_id()
        bridge._dedupe.store(
            old_cmd, "x", {"state": "active"},
            bridge._now() - timedelta(seconds=700),
        )
        bridge._dedupe.store(
            fresh_cmd, "y", {"state": "active"}, bridge._now()
        )
        await bridge._reconcile(bridge._now())
        # Reconcile schedules a background persist; wait for a
        # synchronous one before reading the file.
        await bridge._persist()
        dedupe_now = bridge._dedupe.to_dict()
        check("reconcile drops the expired dedupe entry", old_cmd not in dedupe_now)
        check("reconcile keeps the fresh dedupe entry", fresh_cmd in dedupe_now)
        store_data = json.loads(
            (CONFIG_DIR / ".storage" / "satellite_timers").read_text()
        ).get("data") or {}
        store_dedupe = store_data.get("dedupe") or {}
        check("Store file no longer holds the expired entry", old_cmd not in store_dedupe)
        check("Store file keeps the fresh entry", fresh_cmd in store_dedupe)

        # ================= I4: state-checked commands =====================
        print("\n== I4 state-checked commands ==", flush=True)
        await call_service("pause_timer", {"command_id": new_command_id(), "timer_id": tid1})
        r_pause_again = await call_service(
            "pause_timer", {"command_id": new_command_id(), "timer_id": tid1}
        )
        check("pause of a paused timer returns its state", r_pause_again["state"] == "paused")
        await call_service("resume_timer", {"command_id": new_command_id(), "timer_id": tid1})
        r_resume_again = await call_service(
            "resume_timer", {"command_id": new_command_id(), "timer_id": tid1}
        )
        check("resume of an active timer returns its state", r_resume_again["state"] == "active")

        # ================= I5: pause/resume/add_time ======================
        print("\n== I5 pause/resume/add_time ==", flush=True)
        await call_service("pause_timer", {"command_id": new_command_id(), "timer_id": tid1})
        paused = await call_service(
            "pause_timer", {"command_id": new_command_id(), "timer_id": tid1}
        )
        remaining = paused["remaining_seconds"]
        show("frozen remaining", remaining)
        check("pause freezes the remaining", 590 <= remaining <= 600)
        await asyncio.sleep(1.2)
        paused2 = await call_service(
            "pause_timer", {"command_id": new_command_id(), "timer_id": tid1}
        )
        check("frozen remaining does not move", paused2["remaining_seconds"] == remaining)
        await call_service("resume_timer", {"command_id": new_command_id(), "timer_id": tid1})
        r_add = await call_service(
            "add_time",
            {"command_id": new_command_id(), "timer_id": tid1, "seconds": 60},
        )
        show("after +60s", r_add)
        check("add_time grows the duration", r_add["duration_seconds"] == 660)
        check("add_time grows the remaining", 645 <= r_add["remaining_seconds"] <= 660)

        # ================= I6: cancel + undo ==============================
        print("\n== I6 cancel + undo ==", flush=True)
        registry = er.async_get(hass)
        await call_service("cancel_timer", {"command_id": new_command_id(), "timer_id": tid1})
        await settle()
        check("cancel removes the sensor", f"sensor.satellite_timer_{tid1.lower()}" not in sensor_ids())
        check(
            "cancel removes the registry entry",
            registry.async_get(f"sensor.satellite_timer_{tid1.lower()}") is None,
        )
        check(
            "cancel leaves the second timer alone",
            f"sensor.satellite_timer_{tid2.lower()}" in sensor_ids(),
        )
        r_undo = await call_service(
            "undo_remove", {"command_id": new_command_id(), "timer_id": tid1}
        )
        show("undo response", r_undo)
        check("undo restores the same timer id", r_undo["timer_id"] == tid1)
        check("undo restores the same state", r_undo["state"] == "active")
        check("undo restores the remaining", 645 <= r_undo["remaining_seconds"] <= 660)
        await settle()
        state = hass.states.get(f"sensor.satellite_timer_{tid1.lower()}")
        check(
            "undo-restored sensor omits started_at",
            state is not None and "started_at" not in state.attributes,
        )
        # Undo again: the snapshot is consumed.
        try:
            await call_service(
                "undo_remove", {"command_id": new_command_id(), "timer_id": tid1}
            )
            check("second undo raises TimerNotFound", False)
        except Exception as err:  # noqa: BLE001 — the service error is the check
            check("second undo raises TimerNotFound", "No removed timer" in str(err))
        # Cancel again, wait out the window, undo expires.
        await call_service("cancel_timer", {"command_id": new_command_id(), "timer_id": tid1})
        await asyncio.sleep(5.5)
        try:
            await call_service(
                "undo_remove", {"command_id": new_command_id(), "timer_id": tid1}
            )
            check("expired undo raises UndoExpired", False)
        except Exception as err:  # noqa: BLE001 — the service error is the check
            check("expired undo raises UndoExpired", "5 second" in str(err))

        # ================= I7: ringing model ==============================
        print("\n== I7 ringing model ==", flush=True)
        # I7a: no announce path at all -> A2 unreachable.
        cmd = new_command_id()
        r_short = await call_service(
            "start_timer",
            {"command_id": cmd, "name": "Eggs", "duration_seconds": 2},
        )
        tid_short = r_short["timer_id"]
        await asyncio.sleep(3.5)
        await settle()
        state = hass.states.get(f"sensor.satellite_timer_{tid_short.lower()}")
        show("short timer after finish (no announce path)", state.state if state else None)
        check("no announce path -> finished", state is not None and state.state == "finished")
        check(
            "no announce path -> alarm_missed unreachable",
            state is not None
            and state.attributes.get("alarm_missed") is True
            and state.attributes.get("alarm_missed_reason") == "unreachable",
        )
        await call_service("dismiss_timer", {"command_id": new_command_id(), "timer_id": tid_short})
        await settle()

        # I7b: announce path present -> ringing, then the A15 window
        # expires with no ring_started ack.
        registry_entry = er.async_get(hass).async_get_or_create(
            "assist_satellite", "test", "kitchen-sat", device_id=device_id
        )
        announce_entity = registry_entry.entity_id
        show("announce entity", announce_entity)
        hass.states.async_set(announce_entity, "idle")
        hass.states.async_set("automation.family_hub_timer_announcement", "on")
        await settle()
        r_ring = await call_service(
            "start_timer",
            {"command_id": new_command_id(), "name": "Tea", "duration_seconds": 2},
        )
        tid_ring = r_ring["timer_id"]
        await asyncio.sleep(3.5)
        await settle()
        state = hass.states.get(f"sensor.satellite_timer_{tid_ring.lower()}")
        check("announce path present -> ringing", state is not None and state.state == "ringing")
        await asyncio.sleep(6.0)
        await settle()
        state = hass.states.get(f"sensor.satellite_timer_{tid_ring.lower()}")
        check(
            "no ack in 5 s -> finished + announcer_no_run",
            state is not None
            and state.state == "finished"
            and state.attributes.get("alarm_missed") is True
            and state.attributes.get("alarm_missed_reason") == "announcer_no_run",
        )
        check(
            "ack-timeout finish clears the ring fields (C-NB-06)",
            bridge.records[tid_ring].ring_started_at is None
            and bridge.records[tid_ring].ring_ack_at is None,
        )
        await call_service("dismiss_timer", {"command_id": new_command_id(), "timer_id": tid_ring})
        await settle()

        # I7c: ring_started ack, then acknowledge stops the satellite.
        handler_calls.clear()
        r_ring2 = await call_service(
            "start_timer",
            {"command_id": new_command_id(), "name": "Soup", "duration_seconds": 2},
        )
        tid_ring2 = r_ring2["timer_id"]
        await asyncio.sleep(3.5)
        await settle()
        state = hass.states.get(f"sensor.satellite_timer_{tid_ring2.lower()}")
        check("second ring timer is ringing", state is not None and state.state == "ringing")
        r_ack = await call_service(
            "ring_started", {"timer_id": tid_ring2}
        )
        show("ring_started response", r_ack)
        ack_command_id = r_ack.get("command_id", "")
        try:
            parsed = uuid.UUID(ack_command_id)
            ack_is_v4 = parsed.version == 4
        except (ValueError, AttributeError, TypeError):
            ack_is_v4 = False
        check("ring_started generates a UUID v4 command_id", ack_is_v4)
        await asyncio.sleep(6.0)
        await settle()
        state = hass.states.get(f"sensor.satellite_timer_{tid_ring2.lower()}")
        check("acked ring stays ringing past the window", state is not None and state.state == "ringing")
        await call_service(
            "acknowledge_timer", {"command_id": new_command_id(), "timer_id": tid_ring2}
        )
        await settle()
        state = hass.states.get(f"sensor.satellite_timer_{tid_ring2.lower()}")
        check(
            "acknowledge finishes without a missed flag",
            state is not None
            and state.state == "finished"
            and state.attributes.get("alarm_missed") is False,
        )
        check(
            "acknowledge clears the ring fields (C-NB-06)",
            bridge.records[tid_ring2].ring_started_at is None
            and bridge.records[tid_ring2].ring_ack_at is None,
        )
        check(
            "acknowledge issued the stop to the satellite",
            any(
                c[0] == "kitchen:cancelled" and c[1] == tid_ring2
                for c in handler_calls
            ),
        )
        await call_service("dismiss_timer", {"command_id": new_command_id(), "timer_id": tid_ring2})

        # ================= I7d: pending-stop expiry + liveness ==========
        print("\n== I7d pending-stop expiry from ring start + liveness (C-02, C-NB-02) ==", flush=True)
        from homeassistant.helpers import issue_registry as ir

        handler_calls.clear()
        r_d = await call_service(
            "start_timer",
            {"command_id": new_command_id(), "name": "Delta", "duration_seconds": 2},
        )
        tid_d = r_d["timer_id"]
        await asyncio.sleep(3.5)
        await settle()
        state = hass.states.get(f"sensor.satellite_timer_{tid_d.lower()}")
        check("delta timer is ringing", state is not None and state.state == "ringing")
        await call_service("ring_started", {"timer_id": tid_d})
        ring_start = bridge.records[tid_d].ring_started_at
        show("ring_started_at", ring_start)
        # Drop the satellite handler: the stop vehicle is now absent.
        tm.handlers.pop(device_id, None)
        await call_service("acknowledge_timer", {"command_id": new_command_id(), "timer_id": tid_d})
        await settle()
        pending = bridge._pending_stops.get(tid_d)
        check("ack with the handler down records a pending stop", pending is not None)
        if pending is not None:
            expires = datetime.fromisoformat(pending["expires_at"])
            check(
                "pending-stop expiry is measured from the ring start",
                ring_start is not None
                and expires == ring_start + timedelta(seconds=3600),
            )
            show("pending stop", pending)
        issues = [
            i for i in ir.async_get(hass).issues.values()
            if i.issue_id == "undelivered_timer_events"
        ]
        check("repair issue is present while the stop is pending", len(issues) == 1)
        # Restore the handler: the liveness trigger must re-issue the
        # stop AT ONCE — no sleep, no reconcile wait. The pending stop
        # and the issue must already be gone on the next line.
        async_register_timer_handler(hass, device_id, kitchen_handler)
        check(
            "handler re-registration clears the pending stop at once (liveness)",
            tid_d not in bridge._pending_stops,
        )
        check(
            "handler re-registration issues the stop to the satellite",
            any(c[0] == "kitchen:cancelled" and c[1] == tid_d for c in handler_calls),
        )
        issues = [
            i for i in ir.async_get(hass).issues.values()
            if i.issue_id == "undelivered_timer_events"
        ]
        check("repair issue clears with the pending stop", len(issues) == 0)
        state = hass.states.get(f"sensor.satellite_timer_{tid_d.lower()}")
        check(
            "acknowledged timer is finished without a missed flag",
            state is not None and state.state == "finished"
            and state.attributes.get("alarm_missed") is False,
        )
        await call_service("dismiss_timer", {"command_id": new_command_id(), "timer_id": tid_d})
        await settle()

        # ================= I8: voice funnel, all 7 intents ================
        print("\n== I8 voice funnel (all 7 intents) ==", flush=True)

        async def intent_handle(intent_type, slots, device=device_id):
            return await intent_helpers.async_handle(
                hass, "conversation", intent_type,
                slots=slots, device_id=device, language="en",
            )

        await intent_handle(
            "HassStartTimer",
            {"minutes": {"value": 5}, "name": {"value": "Voice Pasta"}},
        )
        await settle()
        voice_sensors = [
            s for s in sensor_ids()
            if hass.states.get(s).attributes.get("started_via") == "voice"
        ]
        check("voice start makes exactly one sensor", len(voice_sensors) == 1)
        voice_tid = hass.states.get(voice_sensors[0]).attributes["timer_id"]
        show("voice timer id", voice_tid)
        await intent_handle("HassPauseTimer", {"name": {"value": "Voice Pasta"}})
        check("voice pause pauses", tm.timers[voice_tid].is_active is False)
        await intent_handle("HassUnpauseTimer", {"name": {"value": "Voice Pasta"}})
        check("voice resume resumes", tm.timers[voice_tid].is_active is True)
        await intent_handle(
            "HassIncreaseTimer",
            {"name": {"value": "Voice Pasta"}, "minutes": {"value": 1}},
        )
        check(
            "voice increase adds a minute",
            350 <= tm.timers[voice_tid].seconds_left <= 365,
        )
        await intent_handle(
            "HassDecreaseTimer",
            {"name": {"value": "Voice Pasta"}, "minutes": {"value": 1}},
        )
        check(
            "voice decrease removes a minute",
            290 <= tm.timers[voice_tid].seconds_left <= 305,
        )
        await intent_handle("HassCancelTimer", {"name": {"value": "Voice Pasta"}})
        await settle()
        check("voice cancel removes the timer", voice_tid not in tm.timers)
        check("voice cancel removes the sensor", voice_sensors[0] not in sensor_ids())
        # Cancel-all: two voice timers, one call. (The second I3 timer
        # is cancelled first so the count is exactly the two voice ones.)
        await call_service("cancel_timer", {"command_id": new_command_id(), "timer_id": tid2})
        await settle()
        await intent_handle("HassStartTimer", {"minutes": {"value": 5}, "name": {"value": "A"}})
        await intent_handle("HassStartTimer", {"minutes": {"value": 5}, "name": {"value": "B"}})
        await settle()
        response = await intent_handle("HassCancelAllTimers", {})
        check(
            "voice cancel-all reports both",
            response.speech_slots.get("canceled") == 2,
        )
        await settle()
        # HassTimerStatus is NOT overridden: its handler still comes
        # from core's intent timers module; the bridge handler comes
        # from this integration's intents module (a plain ``import
        # satellite_timers`` does not work here — HA loads custom
        # integrations through its own loader).
        handlers = {h.intent_type: h for h in intent_helpers.async_get(hass)}
        check(
            "HassTimerStatus is still the built-in handler",
            handlers["HassTimerStatus"].__class__.__module__
            == "homeassistant.components.intent.timers",
        )
        check(
            "HassStartTimer is the bridge handler",
            handlers["HassStartTimer"].__class__.__module__
            == "custom_components.satellite_timers.intents",
        )

        # ================= I8b: cancel-all with a ringing timer ==========
        print("\n== I8b voice cancel-all silences a ringing timer (C-03) ==", flush=True)
        # A ringing timer is no longer in the TimerManager, so the
        # old cancel-all (which iterated only TM timers) missed it
        # entirely. One ringing + one active timer, one
        # HassCancelAllTimers -> both gone, both counted, and the
        # stop reaches the ringing timer's satellite.
        handler_calls.clear()
        r_cr = await call_service(
            "start_timer",
            {"command_id": new_command_id(), "name": "CancelRing", "duration_seconds": 2},
        )
        tid_cr = r_cr["timer_id"]
        r_ca = await call_service(
            "start_timer",
            {"command_id": new_command_id(), "name": "CancelActive", "duration_seconds": 600},
        )
        tid_ca = r_ca["timer_id"]
        await asyncio.sleep(3.5)
        await settle()
        state = hass.states.get(f"sensor.satellite_timer_{tid_cr.lower()}")
        check("cancel-all target is ringing", state is not None and state.state == "ringing")
        await call_service("ring_started", {"timer_id": tid_cr})
        await asyncio.sleep(0.3)
        response = await intent_handle("HassCancelAllTimers", {})
        await settle()
        check(
            "voice cancel-all counts the ringing timer too",
            response.speech_slots.get("canceled") == 2,
        )
        state = hass.states.get(f"sensor.satellite_timer_{tid_cr.lower()}")
        check(
            "cancel-all finishes the ringing timer without a missed flag",
            state is not None and state.state == "finished"
            and state.attributes.get("alarm_missed") is False,
        )
        check(
            "cancel-all issues the stop to the ringing timer's satellite",
            any(c[0] == "kitchen:cancelled" and c[1] == tid_cr for c in handler_calls),
        )
        check(
            "cancel-all leaves no pending stop for the ringing timer",
            tid_cr not in bridge._pending_stops,
        )
        check(
            "cancel-all removes the active timer",
            f"sensor.satellite_timer_{tid_ca.lower()}" not in sensor_ids(),
        )
        await call_service("dismiss_timer", {"command_id": new_command_id(), "timer_id": tid_cr})
        await settle()

        # ================= I9: ambiguity + primary ========================
        print("\n== I9 satellite ambiguity + primary ==", flush=True)
        async_register_timer_handler(hass, other_device_id, other_handler)
        await settle()
        try:
            await call_service(
                "start_timer",
                {"command_id": new_command_id(), "name": "Ambiguous", "duration_seconds": 60},
            )
            check("two satellites, no primary -> loud error", False)
        except Exception as err:  # noqa: BLE001 — the service error is the check
            message = str(err)
            show("ambiguity error", message)
            check("two satellites, no primary -> loud error", "Configure" in message)
            check("ambiguity error names the voice alternative", "Okay Nabu" in message)
        # The options flow: pick the second satellite as primary.
        flow_result = await hass.config_entries.options.async_init(entry.entry_id)
        check(
            "options flow shows the form",
            flow_result["type"] is FlowResultType.FORM,
        )
        configured = await hass.config_entries.options.async_configure(
            flow_result["flow_id"], {"primary_satellite": other_device_id}
        )
        check(
            "options flow creates the entry",
            configured["type"] is FlowResultType.CREATE_ENTRY,
        )
        reloaded = False
        for _ in range(50):
            await asyncio.sleep(0.2)
            await hass.async_block_till_done()
            if (
                entry.state is ConfigEntryState.LOADED
                and hass.data["satellite_timers"][entry.entry_id]["bridge"].primary_satellite == other_device_id
            ):
                reloaded = True
                break
        check("entry reloads with the new primary", reloaded)
        handler_calls.clear()
        r_primary = await call_service(
            "start_timer",
            {"command_id": new_command_id(), "name": "Primary", "duration_seconds": 60},
        )
        check(
            "chip tap rings the primary",
            r_primary["timer_id"] in tm.timers
            and tm.timers[r_primary["timer_id"]].device_id == other_device_id,
        )
        check(
            "the primary satellite got the STARTED event",
            any(
                c[0] == "other:started" and c[1] == r_primary["timer_id"]
                for c in handler_calls
            ),
        )
        await call_service("cancel_timer", {"command_id": new_command_id(), "timer_id": r_primary["timer_id"]})
        await settle()

        # ================= I10: restart table (entry reload) ==============
        print("\n== I10 restart table (entry reload) ==", flush=True)
        r_long = await call_service(
            "start_timer",
            {"command_id": new_command_id(), "name": "Long", "duration_seconds": 3600},
        )
        tid_long = r_long["timer_id"]
        r_paused = await call_service(
            "start_timer",
            {"command_id": new_command_id(), "name": "Paused", "duration_seconds": 600},
        )
        tid_paused = r_paused["timer_id"]
        await call_service(
            "pause_timer", {"command_id": new_command_id(), "timer_id": tid_paused}
        )
        await call_service(
            "add_time",
            {"command_id": new_command_id(), "timer_id": tid_long, "seconds": 60},
        )
        r_quick = await call_service(
            "start_timer",
            {"command_id": new_command_id(), "name": "Quick", "duration_seconds": 2},
        )
        tid_quick = r_quick["timer_id"]
        # Reload the entry; the quick timer finishes while it is down.
        await hass.config_entries.async_unload(entry.entry_id)
        await asyncio.sleep(3.0)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        await settle()
        check("entry reloads", entry.state is ConfigEntryState.LOADED)
        bridge = hass.data["satellite_timers"][entry.entry_id]["bridge"]
        long_record = bridge.records.get(tid_long)
        check("active timer restores with the same id", long_record is not None)
        if long_record is not None:
            check("active timer restores active", long_record.state == "active")
            check(
                "add_time mutation survives the restart",
                long_record.duration_seconds == 3660,
            )
            remaining = long_record.remaining_at(bridge._now())
            check("active timer keeps its remaining", 3600 <= remaining <= 3660)
        paused_record = bridge.records.get(tid_paused)
        check("paused timer restores paused", paused_record is not None and paused_record.state == "paused")
        if paused_record is not None:
            check(
                "paused timer keeps its frozen remaining",
                590 <= (paused_record.remaining_seconds or 0) <= 600,
            )
        quick_record = bridge.records.get(tid_quick)
        check(
            "down-finished timer restores as finished, never rings",
            quick_record is not None and quick_record.state == "finished",
        )
        if quick_record is not None:
            check(
                "down-finished timer is not a miss",
                quick_record.alarm_missed is False,
            )
        check(
            "restored timers have sensors",
            all(
                f"sensor.satellite_timer_{tid.lower()}" in sensor_ids()
                for tid in (tid_long, tid_paused, tid_quick)
                if bridge.records.get(tid) is not None
            ),
        )
        # The second timer from I3 is still around; clean up everything.
        for tid in list(tm.timers):
            try:
                tm.cancel_timer(tid)
            except Exception:
                pass
        await settle()

        # ================= I11: config flow single instance ===============
        print("\n== I11 config flow single instance ==", flush=True)
        flow_result = await hass.config_entries.flow.async_init(
            "satellite_timers", context={"source": "user"}
        )
        check(
            "second config flow aborts single_instance_allowed",
            flow_result["type"] is FlowResultType.ABORT
            and flow_result.get("reason") == "single_instance_allowed",
        )

        # ================= I12: sensor re-add cap + adoption =============
        print("\n== I12 sensor re-add: dropped add, cap, live adoption (C-NB-01) ==", flush=True)
        from homeassistant.helpers.entity_platform import DATA_ENTITY_PLATFORM

        sensor_mod = sys.modules.get("custom_components.satellite_timers.sensor")
        check("sensor module is loaded", sensor_mod is not None)
        # A live timer record for the test managers to track. The
        # real manager tracks it too; the test managers below track
        # the same record with a fake hass and a swallowing add.
        r_m = await call_service(
            "start_timer",
            {"command_id": new_command_id(), "name": "Manager", "duration_seconds": 600},
        )
        tid_m = r_m["timer_id"]
        await settle()
        entity_id_m = f"sensor.satellite_timer_{tid_m.lower()}"
        show("records at I12", sorted(r.name for r in bridge.records.values()))

        adds1: list = []

        def swallow1(entities, **kwargs):
            adds1.append(entities[0])

        # The fake add never sets .hass, exactly like a platform add
        # that never landed: the manager holds a dead object. Counts
        # below are per entity id: other records (e.g. the finished
        # "Quick" timer I7a leaves behind) also get an entity on the
        # first sync, so a raw count would mix them in.
        fake_hass1 = SimpleNamespace(states=SimpleNamespace(get=lambda eid: None), data={})
        m1 = sensor_mod._SensorManager(fake_hass1, bridge, swallow1, "2.0.0")
        # V2-NB-01 / V2-NB-02 instrumentation: capture the sensor
        # logger's ERROR records and count platform scans, so the cap
        # timing and the scan gating are asserted, not just the add
        # counts.
        cap_errors: list = []

        class _CapErrorCapture(logging.Handler):
            def emit(self, record):
                if record.levelno >= logging.ERROR:
                    cap_errors.append(record.getMessage())

        sensor_logger = logging.getLogger(sensor_mod.__name__)
        cap_handler = _CapErrorCapture(level=logging.ERROR)
        sensor_logger.addHandler(cap_handler)
        scan_calls = {"n": 0}
        orig_live1 = m1._live_platform_entity

        def counting_live1(entity_id):
            scan_calls["n"] += 1
            return orig_live1(entity_id)

        m1._live_platform_entity = counting_live1
        m1._sync_entities()
        show("adds1 after first sync", len(adds1))
        adds1_m = [e for e in adds1 if e.entity_id == entity_id_m]
        check("dropped first add is recorded", len(adds1_m) == 1)
        check(
            "adoption scan is not consulted before a re-add is issued",
            scan_calls["n"] == 0,
        )
        # Force the grace to expire three times: three fresh-object
        # re-adds, then the cap (no fourth).
        for _ in range(3):
            m1._next_readd[tid_m] = time.monotonic() - 1
            m1._sync_entities()
        adds1_m = [e for e in adds1 if e.entity_id == entity_id_m]
        check(
            "re-add fires exactly 3 times, then caps",
            len(adds1_m) == 4 and len({id(o) for o in adds1_m}) == 4,
        )
        check(
            "no ERROR is logged when the 3rd re-add is issued",
            len(cap_errors) == 0,
        )
        check(
            "adoption scan runs once a re-add exists",
            scan_calls["n"] == 2,
        )
        m1._next_readd[tid_m] = time.monotonic() - 1
        m1._sync_entities()
        adds1_m = [e for e in adds1 if e.entity_id == entity_id_m]
        check("no re-add past the cap", len(adds1_m) == 4)
        check(
            "cap ERROR is logged once the last grace elapses",
            len(cap_errors) == 1
            and "still has not landed" in cap_errors[0],
        )
        # One more tick sync past the cap: no second ERROR, no re-add.
        m1._next_readd[tid_m] = time.monotonic() - 1
        m1._sync_entities()
        adds1_m = [e for e in adds1 if e.entity_id == entity_id_m]
        check(
            "cap ERROR is not re-logged on every tick",
            len(cap_errors) == 1 and len(adds1_m) == 4,
        )
        sensor_logger.removeHandler(cap_handler)

        # Adoption: the slow first add lands AFTER a re-add, so the
        # manager holds the dead re-added object while the live first
        # object owns the state — it must adopt the live one.
        adds2: list = []

        def swallow2(entities, **kwargs):
            adds2.append(entities[0])

        fake_state = SimpleNamespace(state="active")
        fake_platform = SimpleNamespace(entities={})
        # The state machine has NO entry for the entity id until the
        # slow add lands: a state present from the start would tell
        # the manager the add landed and correctly block the re-add.
        landed: dict = {"state": None}
        fake_hass2 = SimpleNamespace(
            states=SimpleNamespace(get=lambda eid: landed["state"]),
            data={DATA_ENTITY_PLATFORM: {"satellite_timers": [fake_platform]}},
        )
        m2 = sensor_mod._SensorManager(fake_hass2, bridge, swallow2, "2.0.0")
        scan_calls2 = {"n": 0}
        orig_live2 = m2._live_platform_entity

        def counting_live2(entity_id):
            scan_calls2["n"] += 1
            return orig_live2(entity_id)

        m2._live_platform_entity = counting_live2
        m2._sync_entities()
        show("adds2 after first sync", len(adds2))
        adds2_m = [e for e in adds2 if e.entity_id == entity_id_m]
        first_o2 = adds2_m[0]
        check("second manager drops its first add too", len(adds2_m) == 1)
        m2._next_readd[tid_m] = time.monotonic() - 1
        m2._sync_entities()
        adds2_m = [e for e in adds2 if e.entity_id == entity_id_m]
        check("second manager re-adds once", len(adds2_m) == 2)
        dead_o3 = adds2_m[1] if len(adds2_m) > 1 else None
        check(
            "held object is the dead re-add",
            dead_o3 is not None and m2._entities.get(tid_m) is dead_o3,
        )
        # Simulate the slow first add landing: the platform now holds
        # it (with .hass set, as a real platform add does) and the
        # state machine has the entity id.
        first_o2.hass = fake_hass2
        landed["state"] = fake_state
        fake_platform.entities[entity_id_m] = first_o2
        m2._sync_entities()
        check(
            "manager adopts the live object instead of the dead one",
            m2._entities.get(tid_m) is first_o2,
        )
        check(
            "adoption still consults the platform once a re-add exists",
            scan_calls2["n"] == 1,
        )
        adds2_m = [e for e in adds2 if e.entity_id == entity_id_m]
        check("adoption issues no new add", len(adds2_m) == 2)
        m2._sync_entities()
        adds2_m = [e for e in adds2 if e.entity_id == entity_id_m]
        check(
            "adopted object is steady (no re-add, no re-adopt churn)",
            m2._entities.get(tid_m) is first_o2 and len(adds2_m) == 2,
        )
        await call_service("cancel_timer", {"command_id": new_command_id(), "timer_id": tid_m})
        await settle()
    finally:
        await hass.async_stop()

    print(f"\n{'ALL PASS' if not FAILURES else f'{len(FAILURES)} FAILURES'}", flush=True)
    for label in FAILURES:
        print(f"  FAILED: {label}", flush=True)
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
