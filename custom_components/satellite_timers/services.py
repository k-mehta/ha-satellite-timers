"""The service table (W2-3, §09 C1).

Ten services, all carrying ``command_id`` (a UUID v4, required on the
nine timer services, optional on ``ring_started``), all returning the
current state or raising a typed bridge error. ``SupportsResponse.
OPTIONAL``: the card usually calls without a response, and reads the
returned state when it asks for one.

The ``command_id`` shape is enforced twice: the voluptuous schema
rejects a malformed key before the call runs, and the bridge
re-validates it (the intent path calls the bridge directly, with no
schema in between). Both layers enforce the same rule.
"""

from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.helpers import config_validation as cv

from .bridge import SatelliteTimerBridge
from .const import (
    DOMAIN,
    MAX_TIMER_DURATION_SECONDS,
    MIN_TIMER_DURATION_SECONDS,
    SERVICE_ACKNOWLEDGE_TIMER,
    SERVICE_ADD_TIME,
    SERVICE_CANCEL_TIMER,
    SERVICE_DISMISS_TIMER,
    SERVICE_PAUSE_TIMER,
    SERVICE_RESTART_TIMER,
    SERVICE_RESUME_TIMER,
    SERVICE_RING_STARTED,
    SERVICE_START_TIMER,
    SERVICE_UNDO_REMOVE,
)
from .resolution import is_uuid_v4

_LOGGER = logging.getLogger(__name__)


def _uuid_v4(value: object) -> str:
    """Voluptuous validator: only a UUID v4 string passes."""
    if not is_uuid_v4(value):
        raise vol.Invalid("command_id must be a UUID v4 string")
    return value  # type: ignore[return-value]


_COMMAND_ID = vol.Required("command_id")
_TIMER_ID = vol.Required("timer_id")

_TIMER_SCHEMA = vol.Schema(
    {
        _COMMAND_ID: _uuid_v4,
        _TIMER_ID: cv.string,
    }
)


def _register(hass: HomeAssistant, bridge: SatelliteTimerBridge) -> None:
    """Register the ten services against one bridge instance."""

    async def _start(call: ServiceCall) -> dict:
        return await bridge.async_start_timer(
            call.data["command_id"],
            call.data["name"],
            call.data["duration_seconds"],
            device_id=call.data.get("device_id"),
        )

    async def _pause(call: ServiceCall) -> dict:
        return await bridge.async_pause_timer(
            call.data["command_id"], call.data["timer_id"]
        )

    async def _resume(call: ServiceCall) -> dict:
        return await bridge.async_resume_timer(
            call.data["command_id"], call.data["timer_id"]
        )

    async def _cancel(call: ServiceCall) -> dict:
        return await bridge.async_cancel_timer(
            call.data["command_id"], call.data["timer_id"]
        )

    async def _dismiss(call: ServiceCall) -> dict:
        return await bridge.async_dismiss_timer(
            call.data["command_id"], call.data["timer_id"]
        )

    async def _acknowledge(call: ServiceCall) -> dict:
        return await bridge.async_acknowledge_timer(
            call.data["command_id"], call.data["timer_id"]
        )

    async def _restart(call: ServiceCall) -> dict:
        return await bridge.async_restart_timer(
            call.data["command_id"], call.data["timer_id"]
        )

    async def _add_time(call: ServiceCall) -> dict:
        return await bridge.async_add_time(
            call.data["command_id"], call.data["timer_id"], call.data["seconds"]
        )

    async def _undo_remove(call: ServiceCall) -> dict:
        return await bridge.async_undo_remove(
            call.data["command_id"], call.data["timer_id"]
        )

    async def _ring_started(call: ServiceCall) -> dict:
        return await bridge.async_ring_started(
            call.data["timer_id"], call.data.get("command_id")
        )

    services = [
        (
            SERVICE_START_TIMER,
            vol.Schema(
                {
                    _COMMAND_ID: _uuid_v4,
                    vol.Required("name"): cv.string,
                    vol.Required("duration_seconds"): vol.All(
                        vol.Coerce(int),
                        vol.Range(
                            min=MIN_TIMER_DURATION_SECONDS,
                            max=MAX_TIMER_DURATION_SECONDS,
                        ),
                    ),
                    vol.Optional("device_id"): cv.string,
                }
            ),
            _start,
        ),
        (SERVICE_PAUSE_TIMER, _TIMER_SCHEMA, _pause),
        (SERVICE_RESUME_TIMER, _TIMER_SCHEMA, _resume),
        (SERVICE_CANCEL_TIMER, _TIMER_SCHEMA, _cancel),
        (SERVICE_DISMISS_TIMER, _TIMER_SCHEMA, _dismiss),
        (SERVICE_ACKNOWLEDGE_TIMER, _TIMER_SCHEMA, _acknowledge),
        (SERVICE_RESTART_TIMER, _TIMER_SCHEMA, _restart),
        (
            SERVICE_ADD_TIME,
            vol.Schema(
                {
                    _COMMAND_ID: _uuid_v4,
                    _TIMER_ID: cv.string,
                    vol.Required("seconds"): vol.Coerce(int),
                }
            ),
            _add_time,
        ),
        (SERVICE_UNDO_REMOVE, _TIMER_SCHEMA, _undo_remove),
        (
            SERVICE_RING_STARTED,
            vol.Schema(
                {
                    _TIMER_ID: cv.string,
                    vol.Optional("command_id"): _uuid_v4,
                }
            ),
            _ring_started,
        ),
    ]
    for name, schema, handler in services:
        hass.services.async_register(
            DOMAIN, name, handler, schema=schema, supports_response=SupportsResponse.OPTIONAL
        )
    _LOGGER.info("Satellite Timers services registered: %d services", len(services))


def _unregister(hass: HomeAssistant) -> None:
    for name in (
        SERVICE_START_TIMER,
        SERVICE_PAUSE_TIMER,
        SERVICE_RESUME_TIMER,
        SERVICE_CANCEL_TIMER,
        SERVICE_DISMISS_TIMER,
        SERVICE_ACKNOWLEDGE_TIMER,
        SERVICE_RESTART_TIMER,
        SERVICE_ADD_TIME,
        SERVICE_UNDO_REMOVE,
        SERVICE_RING_STARTED,
    ):
        hass.services.async_remove(DOMAIN, name)


def register_services(hass: HomeAssistant, bridge: SatelliteTimerBridge) -> None:
    """Register the service table (called from async_setup_entry)."""
    _register(hass, bridge)


def unregister_services(hass: HomeAssistant) -> None:
    """Remove the service table (called from async_unload_entry)."""
    _unregister(hass)
