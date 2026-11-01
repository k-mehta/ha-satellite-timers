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
from .const import CONF_DEVICES, DOMAIN

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
    """Change which satellites are mirrored."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        devices = _device_options(self.hass)
        current = self.config_entry.options.get(
            CONF_DEVICES, self.config_entry.data.get(CONF_DEVICES, [])
        )

        if user_input is not None:
            return self.async_create_entry(
                title="",
                data={CONF_DEVICES: user_input.get(CONF_DEVICES) or []},
            )

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_DEVICES, default=current): cv.multi_select(
                        devices
                    )
                }
            ),
        )
