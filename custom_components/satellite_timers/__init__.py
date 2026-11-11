"""Satellite Timers: mirror Assist voice timers into sensor entities.

Voice timers set on a satellite live in Home Assistant's memory only, so no
card or automation can see them. This integration reads them through the
bridge in ``bridge.py`` and exposes one sensor per timer, which a dial card
can then show. It registers services (pause_timer, resume_timer,
cancel_timer, add_time, dismiss_timer, restart_timer, start_timer,
restore_timer, restore_finished_timer) so a dashboard card can control a
timer by its id and start new timers by name and duration, and a "dismiss
the pasta timer" voice intent (see ``intents.py``) for the finished timers
the built-in sentences cannot reach. The bridge persists timers to HA
storage and restores them on start, so timers survive a restart — the
bridge, not the satellite, is the source of truth for timer state.
"""

from __future__ import annotations

import logging
from pathlib import Path

import voluptuous as vol

from homeassistant.components.http import StaticPathConfig
from homeassistant.components.intent.timers import (
    TimerNotFoundError,
    TimersNotSupportedError,
)
from homeassistant.components.lovelace.const import LOVELACE_DATA
from homeassistant.components.lovelace.resources import ResourceStorageCollection
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, SupportsResponse
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import intent as intent_helpers
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.collection import ItemNotFound

from .bridge import SatelliteTimerBridge, assist_supported, timer_devices, timer_manager
from .const import (
    CARD_RESOURCE_TYPE,
    CARD_URL,
    CARD_URL_PATH,
    CONF_DEVICES,
    DOMAIN,
    INTENT_DISMISS_TIMER,
    SERVICE_ADD_TIME,
    SERVICE_CANCEL_TIMER,
    SERVICE_DISMISS_TIMER,
    SERVICE_PAUSE_TIMER,
    SERVICE_RESTART_TIMER,
    SERVICE_RESTORE_FINISHED_TIMER,
    SERVICE_RESTORE_TIMER,
    SERVICE_RESUME_TIMER,
    SERVICE_START_TIMER,
)
from .intents import DismissFinishedTimerIntentHandler, async_ensure_dismiss_sentences

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR]

_TIMER_ID_SCHEMA = vol.Schema({vol.Required("timer_id"): cv.string})
_ADD_TIME_SCHEMA = vol.Schema(
    {
        vol.Required("timer_id"): cv.string,
        vol.Optional("minutes", default=0): vol.All(
            vol.Coerce(int), vol.Range(min=0)
        ),
        vol.Optional("seconds", default=0): vol.All(
            vol.Coerce(int), vol.Range(min=0)
        ),
    }
)
_RESTART_SCHEMA = _ADD_TIME_SCHEMA


def _register_dismiss_intent(hass: HomeAssistant) -> None:
    """Register the "dismiss the pasta timer" voice intent, once.

    The intent is global (not per entry), so a reload must not register
    it twice — the flag lives next to the other per-process state.
    """
    data = hass.data.setdefault(DOMAIN, {})
    if data.get("dismiss_intent_registered"):
        return
    intent_helpers.async_register(hass, DismissFinishedTimerIntentHandler())
    data["dismiss_intent_registered"] = True


def _unregister_dismiss_intent(hass: HomeAssistant) -> None:
    """Remove the dismiss intent, if this integration registered it."""
    data = hass.data.get(DOMAIN, {})
    if not data.pop("dismiss_intent_registered", False):
        return
    intent_helpers.async_remove(hass, INTENT_DISMISS_TIMER)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Satellite Timers from a config entry."""
    if timer_manager(hass) is None:
        raise ConfigEntryNotReady("Assist timer manager is not ready yet")

    if not assist_supported(hass):
        ir.async_create_issue(
            hass,
            DOMAIN,
            "assist_unsupported",
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="assist_unsupported",
        )
        return False

    device_ids = entry.options.get(CONF_DEVICES) or entry.data.get(CONF_DEVICES)
    if not device_ids:
        device_ids = timer_devices(hass)

    bridge = SatelliteTimerBridge(hass, device_ids)
    try:
        restored = await bridge.async_load_persisted()
    except Exception:  # persistence is best effort; sensors must not fail
        _LOGGER.warning("Could not restore persisted timers", exc_info=True)
        restored = 0
    if restored:
        _LOGGER.info("Restored %d timers from storage", restored)
    if not bridge.start():
        return False

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {"bridge": bridge}

    _register_services(hass, entry, bridge)
    _register_dismiss_intent(hass)

    try:
        await async_ensure_dismiss_sentences(hass)
    except Exception:  # voice dismissal is optional; sensors must not fail
        _LOGGER.warning(
            "Could not install the dismiss sentences", exc_info=True
        )

    try:
        resource_id = await _async_register_frontend(hass)
    except Exception:  # the card is optional; sensors must not fail with it
        _LOGGER.warning("Could not register the timer screen card", exc_info=True)
        resource_id = None
    hass.data[DOMAIN][entry.entry_id]["resource_id"] = resource_id

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(_update_listener))

    return True


async def _async_register_frontend(hass: HomeAssistant) -> str | None:
    """Serve the timer screen card and add its Lovelace resource.

    Returns the resource id, or None when Lovelace is not running in
    storage mode (or is not loaded at all) — the sensors still work.
    The static path is registered once per Home Assistant process; the
    resource is added only if no item with our URL is already present,
    so a reload never duplicates it.
    """
    data = hass.data.setdefault(DOMAIN, {})
    if not data.get("frontend_static_path"):
        www_path = Path(__file__).resolve().parent / "www"
        await hass.http.async_register_static_paths(
            [StaticPathConfig(CARD_URL_PATH, str(www_path), False)]
        )
        data["frontend_static_path"] = True

    lovelace_data = hass.data.get(LOVELACE_DATA)
    if lovelace_data is None:
        _LOGGER.warning(
            "Lovelace is not loaded; the timer screen card was not registered"
        )
        return None
    collection = lovelace_data.resources
    if not isinstance(collection, ResourceStorageCollection):
        _LOGGER.warning(
            "Lovelace is running in YAML mode; the timer screen card was "
            "not registered"
        )
        return None
    await collection.async_load()
    for item in collection.async_items():
        if item.get("url") == CARD_URL:
            return item["id"]
    item = await collection.async_create_item(
        {"res_type": CARD_RESOURCE_TYPE, "url": CARD_URL}
    )
    return item["id"]


async def _async_unregister_frontend(
    hass: HomeAssistant, resource_id: str | None
) -> None:
    """Remove the Lovelace resource added for this entry, if we added one."""
    if resource_id is None:
        return
    lovelace_data = hass.data.get(LOVELACE_DATA)
    if lovelace_data is None:
        return
    collection = lovelace_data.resources
    if not isinstance(collection, ResourceStorageCollection):
        return
    await collection.async_load()
    try:
        await collection.async_delete_item(resource_id)
    except ItemNotFound:
        pass


_START_TIMER_SCHEMA = vol.Schema(
    {
        vol.Required("name"): cv.string,
        vol.Required("duration"): vol.Coerce(int),
        vol.Optional("satellite"): cv.string,
    }
)


def _parse_iso_datetime(value: str, field: str) -> datetime:
    """Parse an ISO-8601 datetime; a naive value is read as UTC."""
    from datetime import datetime, timezone

    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise HomeAssistantError(f"{field} is not a valid ISO-8601 datetime") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


_RESTORE_TIMER_SCHEMA = vol.Schema(
    {
        vol.Required("name"): cv.string,
        vol.Required("duration"): vol.Coerce(int),
        vol.Optional("paused", default=False): cv.boolean,
        vol.Optional("satellite"): cv.string,
    }
)

_RESTORE_FINISHED_SCHEMA = vol.Schema(
    {
        vol.Required("name"): cv.string,
        vol.Required("total_seconds"): vol.Coerce(int),
        vol.Required("finished_at"): cv.string,
        vol.Optional("satellite"): cv.string,
        vol.Optional("started_via_service", default=False): cv.boolean,
        vol.Optional("started_at"): cv.string,
    }
)


def _resolve_satellite_device(
    hass: HomeAssistant, bridge: SatelliteTimerBridge, satellite: str | None
) -> str:
    """The device id a start_timer call targets (BRIDGE-1).

    ``satellite`` is an entity id (e.g. the kitchen satellite's entity).
    Without one, the default is the kitchen satellite when one can be
    recognised by name, else the first configured satellite. Raises
    HomeAssistantError naming the satellite and the voice alternative
    when nothing usable resolves.
    """
    devices = bridge.devices
    if not devices:
        raise HomeAssistantError(
            "No satellite is configured for timers — say 'Okay Nabu, "
            "set a timer' instead"
        )
    if satellite:
        ent_reg = er.async_get(hass)
        entity = ent_reg.async_get(satellite)
        if entity is None or entity.device_id is None:
            raise HomeAssistantError(
                f"Satellite {satellite} was not found — say 'Okay Nabu, "
                "set a timer' instead"
            )
        if entity.device_id not in devices:
            raise HomeAssistantError(
                f"Satellite {satellite} is not a configured timer "
                "satellite — say 'Okay Nabu, set a timer' instead"
            )
        return entity.device_id
    if len(devices) == 1:
        return next(iter(devices))
    device_reg = dr.async_get(hass)
    for device_id in sorted(devices):
        device = device_reg.async_get(device_id)
        if device is not None and device.name and "kitchen" in device.name.lower():
            return device_id
    return sorted(devices)[0]


def _register_services(
    hass: HomeAssistant, entry: ConfigEntry, bridge: SatelliteTimerBridge
) -> None:
    """Register the timer control services for this entry.

    The services act on a timer by its id (the id a bridge sensor carries
    in its ``timer_id`` attribute), so they work even when several timers
    share a name — the voice intents cannot tell those apart. Only timers
    on the satellites this entry mirrors can be controlled. A timer the
    bridge restored from storage after a restart (BRIDGE-2) is no longer
    in the TimerManager, so pause/resume/cancel/add_time fall back to
    the bridge's own restored-timer methods for those.
    """

    def resolve(timer_id: str):
        """A live timer (TimerManager) or a restored one (bridge).

        Returns ("live", tm, timer) or ("restored", None, snapshot).
        Raises HomeAssistantError when the timer is nowhere to be found
        or is not on a configured satellite.
        """
        tm = timer_manager(hass)
        if tm is not None:
            timer = tm.timers.get(timer_id)
            if timer is not None:
                if timer.device_id not in bridge.devices:
                    raise HomeAssistantError(
                        "Timer is not on a configured satellite"
                    )
                return ("live", tm, timer)
        snapshot = bridge.restored_snapshot(timer_id)
        if snapshot is not None:
            return ("restored", None, snapshot)
        if tm is None:
            raise HomeAssistantError("Assist timer manager is not available")
        raise HomeAssistantError(f"Timer {timer_id} not found")

    async def handle_pause(call) -> None:
        kind, tm, timer = resolve(call.data["timer_id"])
        try:
            if kind == "live":
                tm.pause_timer(timer.id)
            else:
                bridge.pause_restored(timer.timer_id)
        except TimerNotFoundError:
            raise HomeAssistantError(
                f"Timer {call.data['timer_id']} not found"
            ) from None
        except KeyError:
            # A restored timer dropped between resolve() and the action
            # (e.g. a concurrent double-cancel): a clean "not found",
            # not a 500.
            raise HomeAssistantError(
                f"Timer {call.data['timer_id']} not found"
            ) from None
        except ValueError as err:
            raise HomeAssistantError(str(err)) from None

    async def handle_resume(call) -> None:
        kind, tm, timer = resolve(call.data["timer_id"])
        try:
            if kind == "live":
                tm.unpause_timer(timer.id)
            else:
                bridge.resume_restored(timer.timer_id)
        except TimerNotFoundError:
            raise HomeAssistantError(
                f"Timer {call.data['timer_id']} not found"
            ) from None
        except KeyError:
            # A restored timer dropped between resolve() and the action
            # (e.g. a concurrent double-cancel): a clean "not found",
            # not a 500.
            raise HomeAssistantError(
                f"Timer {call.data['timer_id']} not found"
            ) from None
        except ValueError as err:
            raise HomeAssistantError(str(err)) from None

    async def handle_cancel(call) -> None:
        kind, tm, timer = resolve(call.data["timer_id"])
        try:
            if kind == "live":
                tm.cancel_timer(timer.id)
            else:
                bridge.cancel_restored(timer.timer_id)
        except TimerNotFoundError:
            raise HomeAssistantError(
                f"Timer {call.data['timer_id']} not found"
            ) from None
        except KeyError:
            # A restored timer dropped between resolve() and the action
            # (e.g. a concurrent double-cancel): a clean "not found",
            # not a 500.
            raise HomeAssistantError(
                f"Timer {call.data['timer_id']} not found"
            ) from None
        except ValueError as err:
            raise HomeAssistantError(str(err)) from None

    async def handle_add_time(call) -> None:
        kind, tm, timer = resolve(call.data["timer_id"])
        total = call.data["minutes"] * 60 + call.data["seconds"]
        if total == 0:
            return
        try:
            if kind == "live":
                tm.add_time(timer.id, total)
            else:
                bridge.add_time_restored(timer.timer_id, total)
        except TimerNotFoundError:
            raise HomeAssistantError(
                f"Timer {call.data['timer_id']} not found"
            ) from None
        except KeyError:
            # A restored timer dropped between resolve() and the action
            # (e.g. a concurrent double-cancel): a clean "not found",
            # not a 500.
            raise HomeAssistantError(
                f"Timer {call.data['timer_id']} not found"
            ) from None
        except ValueError as err:
            raise HomeAssistantError(str(err)) from None

    async def handle_dismiss(call) -> None:
        try:
            bridge.dismiss_timer(call.data["timer_id"])
        except ValueError:
            raise HomeAssistantError(
                f"Timer {call.data['timer_id']} is still running — "
                "cancel it instead"
            ) from None
        except KeyError:
            raise HomeAssistantError(
                f"Timer {call.data['timer_id']} not found"
            ) from None

    async def handle_restart(call) -> None:
        try:
            bridge.restart_timer(
                call.data["timer_id"],
                minutes=call.data["minutes"],
                seconds=call.data["seconds"],
            )
        except KeyError:
            raise HomeAssistantError(
                f"Timer {call.data['timer_id']} not found"
            ) from None
        except TimersNotSupportedError:
            raise HomeAssistantError(
                "The satellite no longer supports timers"
            ) from None
        except ValueError as err:
            raise HomeAssistantError(str(err)) from None

    async def handle_start(call) -> dict:
        """Start a timer by name and duration in SECONDS (BRIDGE-1).

        Returns the created timer sensor's entity id, so the caller can
        tell duplicate names apart. A chip passes the RECIPE NAME as the
        name. Satellite unreachable (or unknown) fails LOUDLY and names
        the voice alternative — never a silent success.
        """
        name = str(call.data["name"]).strip()
        if not name:
            raise HomeAssistantError("Timer name is required")
        duration = call.data["duration"]
        if duration < 1:
            raise HomeAssistantError(
                "Duration must be at least 1 second"
            )
        satellite = call.data.get("satellite")
        device_id = _resolve_satellite_device(hass, bridge, satellite)
        try:
            timer_id = bridge.start_timer(device_id, name, duration)
        except TimersNotSupportedError:
            label = satellite or "the kitchen satellite"
            raise HomeAssistantError(
                f"Satellite {label} is not reachable right now — say "
                f"'Okay Nabu, set a {name} timer' instead"
            ) from None
        except ValueError as err:
            raise HomeAssistantError(str(err)) from None
        return {"entity_id": f"sensor.satellite_timer_{timer_id.lower()}"}

    async def handle_restore(call) -> dict:
        """Put a cancelled timer back with its remaining time (D6 Undo).

        ``duration`` is the SECONDS LEFT when it was cancelled (not the
        full duration); ``paused`` restores it paused. Returns the new
        timer sensor's entity id. A chip-started Undo must not trigger
        the timer-jump automation — the bridge marks the restore
        service-started, like start_timer.
        """
        name = str(call.data["name"]).strip()
        if not name:
            raise HomeAssistantError("Timer name is required")
        duration = call.data["duration"]
        if duration < 1:
            raise HomeAssistantError(
                "Duration must be at least 1 second"
            )
        satellite = call.data.get("satellite")
        device_id = _resolve_satellite_device(hass, bridge, satellite)
        try:
            timer_id = bridge.restore_timer(
                device_id,
                name,
                duration,
                call.data["paused"],
            )
        except TimersNotSupportedError:
            label = satellite or "the kitchen satellite"
            raise HomeAssistantError(
                f"Satellite {label} is not reachable right now — say "
                f"'Okay Nabu, set a {name} timer' instead"
            ) from None
        except ValueError as err:
            raise HomeAssistantError(str(err)) from None
        return {"entity_id": f"sensor.satellite_timer_{timer_id.lower()}"}

    async def handle_restore_finished(call) -> dict:
        """Put a dismissed finished timer back, WITHOUT re-ringing (D6).

        The finished record is restored with its original ``finished_at``
        (so "Done — N min ago" is identical) and a NEW timer id. The
        satellite is NOT told to ring again. Returns the restored timer
        sensor's entity id.
        """
        name = str(call.data["name"]).strip()
        if not name:
            raise HomeAssistantError("Timer name is required")
        total_seconds = call.data["total_seconds"]
        if total_seconds < 1:
            raise HomeAssistantError(
                "total_seconds must be at least 1 second"
            )
        satellite = call.data.get("satellite")
        device_id = _resolve_satellite_device(hass, bridge, satellite)
        finished_at = _parse_iso_datetime(
            call.data["finished_at"], "finished_at"
        )
        started_at = (
            _parse_iso_datetime(call.data["started_at"], "started_at")
            if call.data.get("started_at")
            else None
        )
        try:
            timer_id = bridge.restore_finished(
                device_id,
                name,
                total_seconds,
                finished_at,
                call.data["started_via_service"],
                started_at=started_at,
            )
        except ValueError as err:
            raise HomeAssistantError(str(err)) from None
        return {"entity_id": f"sensor.satellite_timer_{timer_id.lower()}"}

    hass.services.async_register(
        DOMAIN, SERVICE_PAUSE_TIMER, handle_pause, schema=_TIMER_ID_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_RESUME_TIMER, handle_resume, schema=_TIMER_ID_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_CANCEL_TIMER, handle_cancel, schema=_TIMER_ID_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_ADD_TIME, handle_add_time, schema=_ADD_TIME_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_DISMISS_TIMER, handle_dismiss, schema=_TIMER_ID_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_RESTART_TIMER, handle_restart, schema=_RESTART_SCHEMA
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_START_TIMER,
        handle_start,
        schema=_START_TIMER_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_RESTORE_TIMER,
        handle_restore,
        schema=_RESTORE_TIMER_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_RESTORE_FINISHED_TIMER,
        handle_restore_finished,
        schema=_RESTORE_FINISHED_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )


def _unregister_services(hass: HomeAssistant) -> None:
    """Remove the timer control services."""
    for service in (
        SERVICE_PAUSE_TIMER,
        SERVICE_RESUME_TIMER,
        SERVICE_CANCEL_TIMER,
        SERVICE_ADD_TIME,
        SERVICE_DISMISS_TIMER,
        SERVICE_RESTART_TIMER,
        SERVICE_START_TIMER,
        SERVICE_RESTORE_TIMER,
        SERVICE_RESTORE_FINISHED_TIMER,
    ):
        hass.services.async_remove(DOMAIN, service)


async def _update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload when the chosen satellites change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry and put the satellite handlers back as found."""
    data = hass.data[DOMAIN].pop(entry.entry_id, {})
    if (bridge := data.get("bridge")) is not None:
        bridge.stop()

    _unregister_services(hass)
    _unregister_dismiss_intent(hass)

    await _async_unregister_frontend(hass, data.get("resource_id"))

    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
