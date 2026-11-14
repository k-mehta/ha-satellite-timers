"""Config + options flows (W2-1).

The config flow asks for NOTHING: one instance, no device picker, no
satellites to choose — satellites are read live from the TimerManager
handlers at call time, so a satellite set up after the integration
still works. The gate is the TimerManager itself: setup aborts
``not_ready`` while it is not loaded, and ``unsupported`` when its
shape changed (HA changed the voice-timer internals). Zero satellites
at setup is valid — a start then fails loudly at call time (the
first-boot case, MG-06).

The options flow has ONE field: the primary satellite. A primary that
is currently offline stays choosable (the union rule — the old
critic-F2 lesson: a Configure submit naming an offline primary must
not fail validation with "<id> is not a valid option"). No sound
options, no Q11/Q12/O1 fields — those answers are settled, not user
choices (A1 fix = DO NOT BUILD).
"""

from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .bridge import check_timer_manager_shape
from .const import CONF_PRIMARY_SATELLITE, DOMAIN, PRIMARY_SATELLITE_UNSET
from .errors import BridgeNotReady

_LOGGER = logging.getLogger(__name__)


def _live_timer_devices(hass) -> list[str]:
    """The live TimerManager handler keys — the only devices that can
    actually time."""
    timer_manager = hass.data.get("intent.timer")
    if timer_manager is None:
        return []
    return list(timer_manager.handlers.keys())


def _device_options(hass, extra_ids: tuple[str, ...] = ()) -> dict[str, str]:
    """Satellite device ids mapped to their display names.

    The UNION of live handlers and the given ids: a configured device
    whose satellite is currently disconnected must still be offered,
    or a Configure submit naming it fails validation. Live handlers
    first, then configured extras in order."""
    device_reg = dr.async_get(hass)
    options: dict[str, str] = {}
    live = _live_timer_devices(hass)
    for device_id in live + [d for d in extra_ids if d not in live]:
        device = device_reg.async_get(device_id)
        if device is not None and device.name:
            options[device_id] = device.name
        else:
            options[device_id] = device_id
    return options


class SatelliteTimersConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up Satellite Timers (one instance, no questions)."""

    VERSION = 1

    async def async_step_user(self, user_input=None) -> ConfigFlowResult:
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        timer_manager = self.hass.data.get("intent.timer")
        if timer_manager is None:
            return self.async_abort(reason="not_ready")
        try:
            check_timer_manager_shape(timer_manager)
        except BridgeNotReady:
            return self.async_abort(reason="unsupported")
        return self.async_create_entry(title="Satellite Timers", data={})

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return SatelliteTimersOptionsFlow()


class SatelliteTimersOptionsFlow(OptionsFlow):
    """Options: the primary satellite, and nothing else."""

    async def async_step_init(self, user_input=None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        primary = self.config_entry.options.get(CONF_PRIMARY_SATELLITE) or ""
        options = _device_options(
            self.hass, (primary,) if primary else ()
        )
        schema = vol.Schema(
            {
                vol.Optional(CONF_PRIMARY_SATELLITE, default=primary): SelectSelector(
                    SelectSelectorConfig(
                        options=[
                            {"value": PRIMARY_SATELLITE_UNSET, "label": "Not set"},
                            *[
                                {"value": device_id, "label": name}
                                for device_id, name in options.items()
                            ],
                        ],
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                )
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
