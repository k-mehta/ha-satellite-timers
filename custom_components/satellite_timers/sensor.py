"""Sensor platform: one sensor per active voice timer on the chosen satellites.

Each sensor is named after the timer ("Pasta") and carries what a dial card
needs: state ``active`` / ``paused`` / ``finished``, plus ``duration`` (total
seconds), ``remaining`` (seconds left) and ``finishes_at`` (ISO timestamp)
attributes. The entity id always contains "timer" (``sensor.satellite_timer_<id>``) so
cards that look for timer-like sensors recognise it.

Sensors appear when a timer starts and disappear when it is cancelled or
dismissed. A finished timer keeps its sensor with state ``finished`` until
it is dismissed (or restarted) — it never just vanishes when it goes off.
While a timer runs, the remaining time is refreshed once a second
so the dial ticks.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval

from .bridge import SatelliteTimerBridge, TimerSnapshot
from .const import (
    CONF_QUIET_HOURS_END,
    CONF_QUIET_HOURS_START,
    DOMAIN,
)
from .sound import current_sound_volume_pct, is_quiet_hours, sound_options

_LOGGER = logging.getLogger(__name__)


def _local_now() -> datetime:
    """The hub's local wall-clock time (its configured time zone).

    Quiet hours are wall-clock hours — a user who sets 22:00-07:00
    means 10pm-7am on the hub, not in UTC. homeassistant.util.dt.now()
    returns an aware datetime in the hub's configured time zone. The
    lazy import keeps this module importable without Home Assistant
    (the plain-python tests stub HA out); outside HA the fallback is
    the system local time.
    """
    try:
        from homeassistant.util import dt as dt_util

        return dt_util.now()
    except Exception:  # pragma: no cover - HA is always present live
        return datetime.now().astimezone()


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the sensor platform from a config entry."""
    data = hass.data[DOMAIN][entry.entry_id]
    bridge: SatelliteTimerBridge = data["bridge"]

    manager = _SensorManager(
        hass, bridge, async_add_entities, sound_options(entry.options)
    )
    await manager.async_start()

    entry.async_on_unload(manager.async_stop)
    data["manager"] = manager


class SatelliteTimerSensor(SensorEntity):
    """One voice timer, mirrored as a sensor a dial card can read."""

    _attr_has_entity_name = False
    _attr_icon = "mdi:timer"

    def __init__(
        self,
        hass: HomeAssistant,
        snapshot: TimerSnapshot,
        sound_opts: dict | None = None,
    ) -> None:
        self._timer_id = snapshot.timer_id
        self._sound_options = sound_opts or {}
        self._attr_unique_id = f"{DOMAIN}_{snapshot.timer_id}"
        # The entity id must carry the timer id: two timers with the same
        # spoken name ("Pasta") are different timers and need different ids.
        # Home Assistant ignores _attr_suggested_object_id (verified in HA
        # 2026.2: Entity.suggested_object_id derives from the entity name,
        # so the id fell back to the name -> sensor.pasta, sensor.pasta_2).
        # The supported way to suggest an id is to set entity_id itself
        # before adding; the platform picks it up as the suggested object
        # id. _attr_suggested_object_id is kept too for older versions.
        object_id = f"satellite_timer_{snapshot.timer_id.lower()}"
        self._attr_suggested_object_id = object_id
        self.entity_id = f"sensor.{object_id}"
        self._attr_name = snapshot.name or "Timer"
        self._attr_native_value = None
        self._last_attrs: dict = {}
        self.update_from_snapshot(snapshot, write=False)

        if snapshot.device_id:
            device_reg = dr.async_get(hass)
            if (device := device_reg.async_get(snapshot.device_id)) is not None:
                # HA rejects device info with neither identifiers nor
                # connections, and the kitchen hub's ESPHome device
                # registers with empty identifiers, so only attach the
                # sensor to the device when there is something to link by.
                device_info: dict = {
                    "manufacturer": device.manufacturer,
                    "model": device.model,
                    "name": device.name,
                }
                if device.identifiers:
                    device_info["identifiers"] = set(device.identifiers)
                if device.connections:
                    device_info["connections"] = set(device.connections)
                if "identifiers" in device_info or "connections" in device_info:
                    self._attr_device_info = device_info

    def update_from_snapshot(
        self, snapshot: TimerSnapshot, write: bool = True, now: datetime | None = None
    ) -> None:
        """Refresh state and attributes from a snapshot; write only on change.

        ``now`` is the time the sound policy is evaluated at — the
        hub's LOCAL wall-clock time (see _local_now). It is a parameter
        so the plain-python tests can pin the local-vs-UTC behaviour
        without a live hub; production callers leave it unset.
        """
        if snapshot.is_finished:
            state = "finished"
        else:
            state = "active" if snapshot.is_active else "paused"
        attrs: dict = {
            "duration": snapshot.total_seconds,
            "remaining": snapshot.seconds_left,
            "timer_name": snapshot.name,
            "timer_id": snapshot.timer_id,
            "device_id": snapshot.device_id,
        }
        if snapshot.finishes_at is not None:
            attrs["finishes_at"] = snapshot.finishes_at.isoformat()
        if snapshot.finished_at is not None:
            attrs["finished_at"] = snapshot.finished_at.isoformat()
        if snapshot.started_at is not None:
            # TIMER-11: when the timer started, so duplicate names can
            # be told apart ("Pasta — started 6:02 PM").
            attrs["started_at"] = snapshot.started_at.isoformat()
        # BRIDGE-1: how the timer was started. Chip/preset/Undo starts
        # ("service") must not trigger the timer-jump automation; voice
        # starts ("voice") may.
        attrs["started_via"] = (
            "service" if snapshot.started_via_service else "voice"
        )
        if snapshot.is_finished:
            # DESIGN-5: how loud the finished alarm should sound, right
            # now (target for the finish moment, ramped by the
            # escalation since then — the 1s tick re-applies this, so
            # the value steps up every 10s while escalation runs).
            # Only a finished timer sounds, so only it carries these.
            # The satellite plays its own alarm; a consumer that
            # honours the policy reads these attributes. No speaker is
            # confirmed (S1 unresolved) — until one exists, these are
            # configuration, not verified sound.
            # Quiet hours are LOCAL wall-clock hours (the hub's
            # configured time zone), not UTC: the options UI says
            # HH:MM with no mention of UTC, and a user who sets
            # 22:00-07:00 means 10pm-7am on the hub. Passing
            # datetime.now(timezone.utc) here used to run quiet hours
            # 14:00-23:00 on a Los Angeles hub and sound at full
            # volume overnight (Phase 7 follow-up). `now` is injectable
            # so the plain-python tests can pin this without a live hub.
            if now is None:
                now = _local_now()
            attrs["sound_volume_pct"] = current_sound_volume_pct(
                now, snapshot.finished_at, self._sound_options
            )
            attrs["quiet_hours"] = is_quiet_hours(
                now,
                self._sound_options.get(CONF_QUIET_HOURS_START),
                self._sound_options.get(CONF_QUIET_HOURS_END),
            )

        if state == self._attr_native_value and attrs == self._last_attrs:
            return

        self._attr_native_value = state
        self._attr_extra_state_attributes = attrs
        self._last_attrs = attrs

        if write:
            self.async_write_ha_state()


class _SensorManager:
    """Keeps the sensor set in sync with the bridge's snapshots."""

    def __init__(
        self,
        hass: HomeAssistant,
        bridge: SatelliteTimerBridge,
        async_add_entities: AddEntitiesCallback,
        sound_opts: dict | None = None,
    ) -> None:
        self._hass = hass
        self._bridge = bridge
        self._async_add_entities = async_add_entities
        self._sound_options = sound_opts or {}
        self._sensors: dict[str, SatelliteTimerSensor] = {}
        self._unsub_interval = None

    async def async_start(self) -> None:
        """Do an initial sync, then refresh once a second while timers run."""
        self._bridge.add_change_listener(self._schedule_sync)
        await self.async_sync()
        self._purge_stale_sensors()
        self._unsub_interval = async_track_time_interval(
            self._hass, self._tick, timedelta(seconds=1)
        )

    def _purge_stale_sensors(self) -> None:
        """Drop registry entries for timers that no longer exist.

        A reload or restart leaves the previous run's sensors behind as
        "unavailable": finished records are in-memory only and do not
        survive, so the new manager never sees those timers and the normal
        sync path never cleans them up. Without this, every reload adds
        another stale sensor.satellite_timer_* to Settings -> Entities.
        """
        ent_reg = er.async_get(self._hass)
        for registry_entry in list(ent_reg.entities.values()):
            if registry_entry.platform != DOMAIN:
                continue
            unique_id = registry_entry.unique_id
            if not unique_id.startswith(f"{DOMAIN}_"):
                continue
            timer_id = unique_id[len(DOMAIN) + 1 :]
            if timer_id not in self._sensors:
                ent_reg.async_remove(registry_entry.entity_id)

    def async_stop(self) -> None:
        """Stop the refresh loop and drop the change listener."""
        if self._unsub_interval is not None:
            self._unsub_interval()
            self._unsub_interval = None
        self._bridge.remove_change_listener(self._schedule_sync)

    @callback
    def _schedule_sync(self) -> None:
        self._hass.async_create_task(self.async_sync())

    @callback
    def _tick(self, now: datetime) -> None:
        if self._sensors:
            self._hass.async_create_task(self.async_sync())

    async def async_sync(self) -> None:
        """Add, update and remove sensors to match the current timers."""
        snapshots = self._bridge.snapshot(datetime.now(timezone.utc))

        new_sensors: list[SatelliteTimerSensor] = []
        for timer_id, snapshot in snapshots.items():
            sensor = self._sensors.get(timer_id)
            if sensor is None:
                sensor = SatelliteTimerSensor(
                    self._hass, snapshot, self._sound_options
                )
                self._sensors[timer_id] = sensor
                new_sensors.append(sensor)
            else:
                sensor.update_from_snapshot(snapshot)

        if new_sensors:
            self._async_add_entities(new_sensors)

        for timer_id in list(self._sensors):
            if timer_id not in snapshots:
                sensor = self._sensors.pop(timer_id)
                # force_remove: plain async_remove() would leave the state
                # behind as "unavailable" (standard HA behaviour for
                # registry-backed entities). A cancelled or dismissed timer
                # is gone for good, so drop the state and the registry
                # entry too — otherwise every ended timer leaves a stale
                # sensor.satellite_timer_* behind.
                entity_id = sensor.entity_id
                await sensor.async_remove(force_remove=True)
                if entity_id is not None:
                    ent_reg = er.async_get(self._hass)
                    if ent_reg.async_get(entity_id) is not None:
                        ent_reg.async_remove(entity_id)
