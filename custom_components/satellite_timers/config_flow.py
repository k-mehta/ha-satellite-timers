"""Config flow for Satellite Timers."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr

from .bridge import assist_supported, timer_devices, timer_manager
from .const import (
    CONF_DEVICES,
    CONF_ESCALATION,
    CONF_QUIET_HOURS_END,
    CONF_QUIET_HOURS_START,
    CONF_QUIET_HOURS_VOLUME_PCT,
    CONF_TIMER_VOLUME,
    DEFAULT_ESCALATION,
    DEFAULT_QUIET_HOURS_END,
    DEFAULT_QUIET_HOURS_START,
    DEFAULT_QUIET_HOURS_VOLUME_PCT,
    DEFAULT_TIMER_VOLUME,
    DOMAIN,
)
from .sound import clamp_volume_pct, is_valid_hhmm

_LOGGER = logging.getLogger(__name__)


def _device_options(hass) -> dict[str, str]:
    """Satellite device ids mapped to their display names."""
    device_reg = dr.async_get(hass)
    options: dict[str, str] = {}
    for device_id in timer_devices(hass):
        device = device_reg.async_get(device_id)
        if device is not None and device.name:
            options[device_id] = device.name
        else:
            options[device_id] = device_id
    return options


class SatelliteTimersConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the initial setup."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")

        if timer_manager(self.hass) is None:
            return self.async_abort(reason="not_ready")

        if not assist_supported(self.hass):
            return self.async_abort(reason="unsupported")

        devices = _device_options(self.hass)
        if not devices:
            return self.async_abort(reason="no_devices")

        if user_input is not None:
            device_ids = user_input.get(CONF_DEVICES) or []
            if not device_ids:
                return self.async_show_form(
                    step_id="user",
                    data_schema=self._schema(devices),
                    errors={"base": "no_selection"},
                )
            return self.async_create_entry(
                title="Satellite Timers", data={CONF_DEVICES: device_ids}
            )

        return self.async_show_form(step_id="user", data_schema=self._schema(devices))

    def _schema(self, devices: dict[str, str]) -> vol.Schema:
        return vol.Schema(
            {vol.Required(CONF_DEVICES, default=list(devices)): cv.multi_select(devices)}
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry) -> OptionsFlow:
        return SatelliteTimersOptionsFlow()


class SatelliteTimersOptionsFlow(OptionsFlow):
    """Change which satellites are mirrored, and how timers sound.

    Sound (DESIGN-5): the satellite plays its own alarm when a timer
    finishes; these options set how loud. No speaker is confirmed,
    ordered or assumed (S1 unresolved) — the finished-timer sensor
    exposes the computed volume as attributes for a consumer that
    honours them. Changing any option reloads the entry (see the
    update listener in __init__.py).
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        devices = _device_options(self.hass)

        if user_input is not None:
            errors: dict[str, str] = {}
            # A time that does not parse is an error, not a silent
            # default — the user typed it, they should know it did not
            # take.
            if not is_valid_hhmm(user_input.get(CONF_QUIET_HOURS_START)):
                errors[CONF_QUIET_HOURS_START] = "invalid_time"
            if not is_valid_hhmm(user_input.get(CONF_QUIET_HOURS_END)):
                errors[CONF_QUIET_HOURS_END] = "invalid_time"
            if errors:
                return self.async_show_form(
                    step_id="init",
                    data_schema=self._schema(devices, user_input),
                    errors=errors,
                )
            return self.async_create_entry(
                title="",
                data={
                    CONF_DEVICES: user_input.get(CONF_DEVICES) or [],
                    CONF_TIMER_VOLUME: clamp_volume_pct(
                        user_input.get(CONF_TIMER_VOLUME), DEFAULT_TIMER_VOLUME
                    ),
                    CONF_QUIET_HOURS_START: str(
                        user_input.get(
                            CONF_QUIET_HOURS_START, DEFAULT_QUIET_HOURS_START
                        )
                    ).strip(),
                    CONF_QUIET_HOURS_END: str(
                        user_input.get(CONF_QUIET_HOURS_END, DEFAULT_QUIET_HOURS_END)
                    ).strip(),
                    CONF_QUIET_HOURS_VOLUME_PCT: clamp_volume_pct(
                        user_input.get(CONF_QUIET_HOURS_VOLUME_PCT),
                        DEFAULT_QUIET_HOURS_VOLUME_PCT,
                    ),
                    CONF_ESCALATION: bool(
                        user_input.get(CONF_ESCALATION, DEFAULT_ESCALATION)
                    ),
                },
            )

        return self.async_show_form(
            step_id="init",
            data_schema=self._schema(devices, self._current_values()),
        )

    def _current_values(self) -> dict[str, Any]:
        """Stored options with defaults, for the form's initial values."""
        options = self.config_entry.options
        return {
            CONF_DEVICES: options.get(
                CONF_DEVICES, self.config_entry.data.get(CONF_DEVICES, [])
            ),
            CONF_TIMER_VOLUME: options.get(CONF_TIMER_VOLUME, DEFAULT_TIMER_VOLUME),
            CONF_QUIET_HOURS_START: options.get(
                CONF_QUIET_HOURS_START, DEFAULT_QUIET_HOURS_START
            ),
            CONF_QUIET_HOURS_END: options.get(
                CONF_QUIET_HOURS_END, DEFAULT_QUIET_HOURS_END
            ),
            CONF_QUIET_HOURS_VOLUME_PCT: options.get(
                CONF_QUIET_HOURS_VOLUME_PCT, DEFAULT_QUIET_HOURS_VOLUME_PCT
            ),
            CONF_ESCALATION: options.get(CONF_ESCALATION, DEFAULT_ESCALATION),
        }

    def _schema(
        self, devices: dict[str, str], values: dict[str, Any]
    ) -> vol.Schema:
        return vol.Schema(
            {
                vol.Required(
                    CONF_DEVICES, default=values.get(CONF_DEVICES, list(devices))
                ): cv.multi_select(devices),
                vol.Required(
                    CONF_TIMER_VOLUME,
                    default=values.get(CONF_TIMER_VOLUME, DEFAULT_TIMER_VOLUME),
                ): vol.All(vol.Coerce(int), vol.Range(min=0, max=100)),
                vol.Required(
                    CONF_QUIET_HOURS_START,
                    default=values.get(
                        CONF_QUIET_HOURS_START, DEFAULT_QUIET_HOURS_START
                    ),
                ): str,
                vol.Required(
                    CONF_QUIET_HOURS_END,
                    default=values.get(
                        CONF_QUIET_HOURS_END, DEFAULT_QUIET_HOURS_END
                    ),
                ): str,
                vol.Required(
                    CONF_QUIET_HOURS_VOLUME_PCT,
                    default=values.get(
                        CONF_QUIET_HOURS_VOLUME_PCT, DEFAULT_QUIET_HOURS_VOLUME_PCT
                    ),
                ): vol.All(vol.Coerce(int), vol.Range(min=0, max=100)),
                vol.Required(
                    CONF_ESCALATION,
                    default=values.get(CONF_ESCALATION, DEFAULT_ESCALATION),
                ): bool,
            }
        )
