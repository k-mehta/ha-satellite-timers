"""Sensor platform: one sensor per live timer, plus the bridge version sensor.

One sensor per live timer (``sensor.satellite_timer_<timer_id>``,
§09 C2), unregistered on cancel/dismiss — never left ``unavailable``.
Ringing, in the sensor-state sense, means the bridge's published
ringing record synthesised from its own records, NOT a reading of any
HA/satellite signal (§03.6, AD-C5 scope-pinned sentence, verbatim).

The bridge version sensor (``sensor.family_hub_bridge_version``, §08
row 44) exists with ZERO timers: state = the release tag, attribute
``min_card_version``. It also carries the bridge error slot as a
``bridge_error`` attribute while the slot is set — the one in-memory
diagnostic surface the frontend reads through sensor state (§08 row 41).

Version gating (G-08): the sensor contract's skip in rule R8-1
checks this sensor the same way it checks any timer sensor.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_platform as entity_platform_module
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.loader import async_get_integration

from .bridge import SatelliteTimerBridge
from .const import DOMAIN, MIN_CARD_VERSION
from .records import TimerRecord

_LOGGER = logging.getLogger(__name__)

BRIDGE_VERSION_ENTITY_ID = "sensor.family_hub_bridge_version"
BRIDGE_VERSION_UNIQUE_ID = "satellite_timers_bridge_version"
# Grace before the manager re-attempts a sensor add that never
# landed. An add is an eager task (entity_platform.py
# _async_schedule_add_entities_for_entry, eager_start=True) and lands
# within one event-loop turn in normal operation, so 5 s is far past
# a merely slow add; an add older than this was cancelled (e.g. by an
# entry unload while a start raced the reload) or dropped, and the
# manager re-adds it rather than leaving a live timer with no sensor.
SENSOR_READD_GRACE_SECONDS = 5.0
# Cap on re-add attempts per timer (implementation choice: 3 attempts
# x 5 s grace = 15 s; an add that has not landed in 15 s is not slow,
# it is stuck). Past the cap the manager stops retrying and logs an
# error naming the recovery (an entry reload builds a fresh manager,
# which adds the sensor again) — an unbounded 5 s retry loop with a
# warning every cycle is log spam, not recovery. The attempt counter
# resets when the sensor lands (live or adopted).
SENSOR_READD_MAX_ATTEMPTS = 3


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the sensor platform from a config entry."""
    bridge: SatelliteTimerBridge = hass.data[DOMAIN][entry.entry_id]["bridge"]
    try:
        integration = await async_get_integration(hass, DOMAIN)
        bridge_version = integration.version
    except Exception as err:  # noqa: BLE001 — the version sensor must never crash the platform
        _LOGGER.warning(
            "Could not read the Satellite Timers manifest version (%s); "
            "the version sensor reports 'unknown'",
            err,
        )
        bridge_version = "unknown"
    manager = _SensorManager(hass, bridge, async_add_entities, bridge_version)
    manager.start()
    entry.async_on_unload(manager.stop)


class _SensorManager:
    """Diffs the bridge's records against the live sensor entities:
    adds a sensor for each new record, removes the sensor (entity +
    registry entry) for each removed record, and refreshes state on
    every bridge notification (the 1 s tick keeps the dial fresh)."""

    def __init__(
        self,
        hass: HomeAssistant,
        bridge: SatelliteTimerBridge,
        add_entities: AddEntitiesCallback,
        bridge_version: str,
    ) -> None:
        self._hass = hass
        self._bridge = bridge
        self._add_entities = add_entities
        self._version = bridge_version
        self._entities: dict[str, SatelliteTimerSensor] = {}
        self._version_sensor: BridgeVersionSensor | None = None
        self._unsub: Callable[[], None] | None = None
        # Monotonic time from which a never-landed entity may be
        # re-added (SENSOR_READD_GRACE_SECONDS). A queued add is not
        # retried before then — the add is an eager task and lands
        # within a turn in normal operation.
        self._next_readd: dict[str, float] = {}
        # Re-add attempts per timer, capped at
        # SENSOR_READD_MAX_ATTEMPTS (C-NB-01).
        self._readd_attempts: dict[str, int] = {}
        # Timers whose cap-exhausted ERROR has already been logged.
        # The ERROR fires once, when the last re-add's grace elapses
        # with no landing (V2-NB-01) — not on every tick after the
        # cap, and not when the 3rd attempt is issued. Cleared with
        # the other re-add bookkeeping when the sensor lands (live
        # or adopted) or the record goes away.
        self._readd_cap_error_logged: set[str] = set()

    def start(self) -> None:
        self._version_sensor = BridgeVersionSensor(
            self._bridge, self._version
        )
        self._add_entities([self._version_sensor])
        self._sync_entities()
        self._unsub = self._bridge.subscribe(self._on_bridge_change)

    def stop(self) -> None:
        if self._unsub is not None:
            self._unsub()
            self._unsub = None

    @callback
    def _on_bridge_change(self) -> None:
        self._sync_entities()
        for entity in self._entities.values():
            # Only entities the platform has actually added have a hass;
            # a queued add writes its own initial state when it lands.
            if entity.hass is not None:
                entity.async_write_ha_state()
        if self._version_sensor is not None and self._version_sensor.hass is not None:
            self._version_sensor.async_write_ha_state()

    def _live_platform_entity(self, entity_id: str) -> SatelliteTimerSensor | None:
        """The live entity object for ``entity_id``, from the sensor
        platform (``entity_platform.async_get_platforms`` +
        ``platform.entities``) — the object a slow first add actually
        registered. None when the platform holds no such entity (the
        add never landed). Only a SatelliteTimerSensor is ever
        adopted: the manager never takes ownership of a foreign
        object."""
        for platform in entity_platform_module.async_get_platforms(
            self._hass, DOMAIN
        ):
            entity = platform.entities.get(entity_id)
            if isinstance(entity, SatelliteTimerSensor):
                return entity
        return None

    def _sync_entities(self) -> None:
        records = self._bridge.records
        for timer_id, record in records.items():
            entity = self._entities.get(timer_id)
            if entity is None:
                entity = SatelliteTimerSensor(self._hass, self._bridge, record)
                self._entities[timer_id] = entity
                self._add_entities([entity])
                self._next_readd[timer_id] = (
                    time.monotonic() + SENSOR_READD_GRACE_SECONDS
                )
            elif entity.hass is not None:
                # Live: any re-add bookkeeping from an earlier slow
                # add is stale — drop it.
                self._next_readd.pop(timer_id, None)
                self._readd_attempts.pop(timer_id, None)
                self._readd_cap_error_logged.discard(timer_id)
            else:
                # The add never landed: the add task was cancelled (an
                # entry unload while a start raced the reload — the
                # entry cancels its tasks on unload, config_entries.py
                # _async_process_on_unload) or the add failed. Adoption
                # can only matter once a re-add has been issued: before
                # that, the only object that could have landed is the
                # first add itself — and if it had landed, this branch
                # would not run (entity.hass would be set). So the
                # platform scan is skipped on every tick inside the
                # grace (V2-NB-02): it is wasted work until a re-add
                # exists for the slow first add to land after.
                now = time.monotonic()
                attempts = self._readd_attempts.get(timer_id, 0)
                live = (
                    self._live_platform_entity(entity.entity_id)
                    if attempts >= 1
                    else None
                )
                if live is not None:
                    # First check whether the slow first add landed AFTER a
                    # re-add: the manager then holds the dead re-added
                    # object while the live first object owns the state,
                    # and the live object's remaining would freeze (the
                    # manager refreshes only the object it holds). Adopt
                    # the live object instead — it is a
                    # SatelliteTimerSensor this manager created, and the
                    # 1 s tick refreshes it from here on.
                    _LOGGER.info(
                        "Timer sensor %s landed slowly; adopting the "
                        "live sensor object instead of the re-added one",
                        entity.entity_id,
                    )
                    self._entities[timer_id] = live
                    self._next_readd.pop(timer_id, None)
                    self._readd_attempts.pop(timer_id, None)
                    self._readd_cap_error_logged.discard(timer_id)
                elif (
                    attempts < SENSOR_READD_MAX_ATTEMPTS
                    and now >= self._next_readd.get(timer_id, 0.0)
                    and self._hass.states.get(entity.entity_id) is None
                ):
                    # The manager's diff is the guarantee of one
                    # sensor per live timer, so it re-attempts the add
                    # rather than leaving the timer sensor-less. A
                    # fresh object is required — an entity whose add
                    # started cannot be added a second time
                    # (entity.py add_to_platform_start raises on a
                    # second add, and aborting would kill the
                    # in-flight add). No re-add while the entity id
                    # is already in the state machine: a slow add did
                    # land it, and a second add would be a duplicate.
                    # Capped at SENSOR_READD_MAX_ATTEMPTS: past that
                    # the add is stuck, not slow, and the next entry
                    # reload (fresh manager) is the recovery.
                    _LOGGER.warning(
                        "Timer sensor %s never landed; re-adding it "
                        "(attempt %d of %d)",
                        entity.entity_id,
                        attempts + 1,
                        SENSOR_READD_MAX_ATTEMPTS,
                    )
                    entity = SatelliteTimerSensor(
                        self._hass, self._bridge, record
                    )
                    self._entities[timer_id] = entity
                    self._add_entities([entity])
                    self._next_readd[timer_id] = (
                        now + SENSOR_READD_GRACE_SECONDS
                    )
                    self._readd_attempts[timer_id] = attempts + 1
                elif (
                    attempts >= SENSOR_READD_MAX_ATTEMPTS
                    and now >= self._next_readd.get(timer_id, 0.0)
                    and self._hass.states.get(entity.entity_id) is None
                    and timer_id not in self._readd_cap_error_logged
                ):
                    # The cap has bitten: the last re-add's grace has
                    # elapsed with no landing, so the add is stuck
                    # (V2-NB-01). The ERROR is logged HERE, when the
                    # failure is real — not when the 3rd attempt was
                    # issued, which asserted failure one grace period
                    # early and made every run emit an ERROR line by
                    # design. Logged once; the recovery is the entry
                    # reload named in the message (fresh manager).
                    _LOGGER.error(
                        "Timer sensor %s still has not landed "
                        "after %d attempts; it will appear when "
                        "the Satellite Timers entry is reloaded",
                        entity.entity_id,
                        SENSOR_READD_MAX_ATTEMPTS,
                    )
                    self._readd_cap_error_logged.add(timer_id)
        for timer_id in list(self._entities):
            if timer_id not in records:
                self._remove_entity(timer_id)

    def _remove_entity(self, timer_id: str) -> None:
        """Remove the entity AND its registry entry, so a cancelled or
        dismissed timer never lingers as ``unavailable`` (§09 C2).
        An entity the platform never added (a cancel in the same tick
        as the start) has no registry entry: it is only dropped from
        the manager's map, and its own add hook removes it if the
        queued add still lands."""
        entity = self._entities.pop(timer_id, None)
        if entity is None:
            return
        self._next_readd.pop(timer_id, None)
        self._readd_attempts.pop(timer_id, None)
        self._readd_cap_error_logged.discard(timer_id)
        entity_id = entity.entity_id
        if entity.hass is None:
            return
        # Registry first (sync): the entity's removal then sees no
        # registry entry and drops the state instead of writing
        # ``unavailable``.
        er.async_get(self._hass).async_remove(entity_id)
        self._hass.async_create_background_task(
            entity.async_remove(force_remove=True),
            name=f"Remove timer sensor {entity_id}",
        )


class SatelliteTimerSensor(SensorEntity):
    """One live timer. State = the record's state; attributes =
    the record's observed attributes (§09 C2)."""

    _attr_should_poll = False

    def __init__(
        self,
        hass: HomeAssistant,
        bridge: SatelliteTimerBridge,
        record: TimerRecord,
    ) -> None:
        self._hass = hass
        self._bridge = bridge
        self._timer_id = record.timer_id
        # The entity id is preset before adding (ULIDs lowercased:
        # entity ids must be lowercase).
        self.entity_id = f"sensor.satellite_timer_{record.timer_id.lower()}"
        self._attr_unique_id = f"satellite_timers_{record.timer_id}"
        # A nameless voice timer shows the plain fallback as its entity
        # name; the record itself still carries the empty name.
        self._attr_name = record.name or "Timer"

    @property
    def _record(self) -> TimerRecord | None:
        return self._bridge.records.get(self._timer_id)

    async def async_added_to_hass(self) -> None:
        """A queued add can land after the record is already gone (a
        cancel in the same tick as the start). The entity then removes
        itself at once instead of lingering with no timer."""
        if self._record is None:
            er.async_get(self._hass).async_remove(self.entity_id)
            self._hass.async_create_background_task(
                self.async_remove(force_remove=True),
                name=f"Remove orphan timer sensor {self.entity_id}",
            )

    @property
    def state(self) -> str | None:  # noqa: ANN401 — HA's SensorEntity.state signature
        record = self._record
        return record.state if record is not None else None

    @property
    def extra_state_attributes(self) -> dict | None:
        record = self._record
        if record is None:
            return None
        return self._bridge.sensor_attributes(record)

    @property
    def device_info(self):  # noqa: ANN201 — HA's DeviceInfo type
        """Attach the sensor to the satellite's device, when the device
        is in the registry — the registry entry's own identifiers AND
        connections, so the sensor lands on the same device the
        satellite belongs to. No device info at all when the entry has
        neither (the platform rejects an empty identifiers set)."""
        record = self._record
        if record is None or record.device_id is None:
            return None
        device = dr.async_get(self._hass).async_get(record.device_id)
        if device is None:
            return None
        identifiers = set(device.identifiers)
        connections = set(device.connections)
        if not identifiers and not connections:
            return None
        return {
            "identifiers": identifiers,
            "connections": connections,
            "name": device.name,
            "manufacturer": device.manufacturer,
            "model": device.model,
        }


class BridgeVersionSensor(SensorEntity):
    """The bridge version sensor (§03.6, §08 row 44): always present,
    even with zero timers. State = the release tag; attribute
    ``min_card_version`` names the oldest card contract this bridge
    speaks. While the bridge error slot is set, the slot is published
    as the ``bridge_error`` attribute — the frontend's only read path
    for bridge diagnostics."""

    _attr_should_poll = False

    def __init__(self, bridge: SatelliteTimerBridge, version: str) -> None:
        self._bridge = bridge
        self._version = version
        self.entity_id = BRIDGE_VERSION_ENTITY_ID
        self._attr_unique_id = BRIDGE_VERSION_UNIQUE_ID
        self._attr_name = "Family Hub Bridge Version"

    @property
    def state(self) -> str:  # noqa: ANN401 — HA's SensorEntity.state signature
        return self._version

    @property
    def extra_state_attributes(self) -> dict:
        attrs: dict = {"min_card_version": MIN_CARD_VERSION}
        if self._bridge.error_slot is not None:
            attrs["bridge_error"] = dict(self._bridge.error_slot)
        return attrs
