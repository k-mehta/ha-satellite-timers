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
    CONF_O1_ALARM_BEHAVIOUR,
    CONF_PRIMARY_SATELLITE,
    CONF_Q11_RING_TIMING,
    CONF_Q12_AUTO_STOP_RESEND,
    DEFAULT_O1_ALARM_BEHAVIOUR,
    DEFAULT_Q11_RING_TIMING,
    DEFAULT_Q12_AUTO_STOP_RESEND,
    DOMAIN,
    O1_OPTIONS,
    PRIMARY_SATELLITE_UNSET,
)

_LOGGER = logging.getLogger(__name__)


def _device_options(hass, extra_ids: tuple[str, ...] = ()) -> dict[str, str]:
    """Satellite device ids mapped to their display names.

    The UNION of live handlers and the given ids: a configured device
    whose satellite is currently disconnected must still be offered,
    or a Configure submit naming it fails validation ("<id> is not a
    valid option" — critic F2; the options leg-2 submit 400'd exactly
    this way). Live handlers first, then configured extras in order.
    """
    device_reg = dr.async_get(hass)
    options: dict[str, str] = {}
    live = timer_devices(hass)
    for device_id in live + [d for d in extra_ids if d not in live]:
        device = device_reg.async_get(device_id)
        if device is not None and device.name:
            options[device_id] = device.name
        else:
            options[device_id] = device_id
    return options


def _configured_device_ids(config_entry) -> list[str]:
    """Device ids the entry already has configured (options, then data)."""
    if config_entry is None:
        return []
    seen: list[str] = []
    for source in (
        getattr(config_entry, "options", None) or {},
        getattr(config_entry, "data", None) or {},
    ):
        for device_id in source.get(CONF_DEVICES) or []:
            if device_id and device_id not in seen:
                seen.append(device_id)
    return seen


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
    """Change which satellites are mirrored, the primary satellite, and
    the recorded timer-delivery answers.

    The Q11/Q12 fields record the SETTLED answers (Q11: a timer that
    finished while the system was down never rings; Q12: a stopped alarm
    is never re-rung), not a user choice. O1 records the satellite
    firmware's alarm behaviour once the §25 listen test measures it; until
    then it stays on the unsettled option. There is NO Q13 field — Q13 is
    DEAD (no bypass exists to suppress). Changing any option reloads the
    entry (see the update listener in __init__.py), so the re-constructed
    bridge reads the new values at construction.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        devices = _device_options(
            self.hass, tuple(_configured_device_ids(self.config_entry))
        )

        if user_input is not None:
            # Critic F1: Q11/Q12 are SETTLED answers, not choices — the
            # submitted values are ignored and the settled literals are
            # always written. Offering the rejected literals as live
            # choices let a Configure submit record (and then display)
            # the opposite of the user's settled answer, while nothing
            # reads the stored values. O1 keeps its three-option shape.
            return self.async_create_entry(
                title="",
                data={
                    CONF_DEVICES: user_input.get(CONF_DEVICES) or [],
                    CONF_PRIMARY_SATELLITE: (
                        user_input.get(
                            CONF_PRIMARY_SATELLITE, PRIMARY_SATELLITE_UNSET
                        )
                        or PRIMARY_SATELLITE_UNSET
                    ),
                    CONF_Q11_RING_TIMING: DEFAULT_Q11_RING_TIMING,
                    CONF_Q12_AUTO_STOP_RESEND: DEFAULT_Q12_AUTO_STOP_RESEND,
                    CONF_O1_ALARM_BEHAVIOUR: (
                        user_input.get(
                            CONF_O1_ALARM_BEHAVIOUR, DEFAULT_O1_ALARM_BEHAVIOUR
                        )
                        if user_input.get(
                            CONF_O1_ALARM_BEHAVIOUR, DEFAULT_O1_ALARM_BEHAVIOUR
                        )
                        in O1_OPTIONS
                        else DEFAULT_O1_ALARM_BEHAVIOUR
                    ),
                },
            )

        return self.async_show_form(
            step_id="init",
            data_schema=self._schema(devices, self._current_values()),
        )

    def _current_values(self) -> dict[str, Any]:
        """Stored options with defaults, for the form's initial values.

        Q11/Q12 are clamped to the settled literals here too: an entry
        that still carries a rejected literal (stored before the F1
        clamp) must display the settled answer, never the rejected
        value as the current pick.
        """
        options = self.config_entry.options
        q11 = options.get(CONF_Q11_RING_TIMING, DEFAULT_Q11_RING_TIMING)
        q12 = options.get(
            CONF_Q12_AUTO_STOP_RESEND, DEFAULT_Q12_AUTO_STOP_RESEND
        )
        return {
            CONF_DEVICES: options.get(
                CONF_DEVICES, self.config_entry.data.get(CONF_DEVICES, [])
            ),
            CONF_PRIMARY_SATELLITE: options.get(
                CONF_PRIMARY_SATELLITE, PRIMARY_SATELLITE_UNSET
            ),
            CONF_Q11_RING_TIMING: (
                q11 if q11 == DEFAULT_Q11_RING_TIMING else DEFAULT_Q11_RING_TIMING
            ),
            CONF_Q12_AUTO_STOP_RESEND: (
                q12
                if q12 == DEFAULT_Q12_AUTO_STOP_RESEND
                else DEFAULT_Q12_AUTO_STOP_RESEND
            ),
            CONF_O1_ALARM_BEHAVIOUR: options.get(
                CONF_O1_ALARM_BEHAVIOUR, DEFAULT_O1_ALARM_BEHAVIOUR
            ),
        }

    def _schema(
        self, devices: dict[str, str], values: dict[str, Any]
    ) -> vol.Schema:
        primary_choices = {
            PRIMARY_SATELLITE_UNSET: "Not set",
            **devices,
        }
        # Select labels: the stored values are the literals (const.py);
        # the dict values here are only what the form shows. Q11/Q12
        # offer ONLY the settled literal (critic F1): the rejected
        # literals are not live choices, so they are not offered.
        q11_choices = {
            "never_ring": "Never ring (settled)",
        }
        q12_choices = {
            "never_resend": "Never re-send (settled)",
        }
        o1_choices = {
            "unsettled": "Not yet measured",
            "rings_until_cancelled": "Rings until cancelled",
            "auto_stop": "Stops by itself (auto-stop)",
        }
        return vol.Schema(
            {
                vol.Required(
                    CONF_DEVICES, default=values.get(CONF_DEVICES, list(devices))
                ): cv.multi_select(devices),
                vol.Required(
                    CONF_PRIMARY_SATELLITE,
                    default=values.get(
                        CONF_PRIMARY_SATELLITE, PRIMARY_SATELLITE_UNSET
                    ),
                ): vol.In(primary_choices),
                vol.Required(
                    CONF_Q11_RING_TIMING,
                    default=values.get(
                        CONF_Q11_RING_TIMING, DEFAULT_Q11_RING_TIMING
                    ),
                ): vol.In(q11_choices),
                vol.Required(
                    CONF_Q12_AUTO_STOP_RESEND,
                    default=values.get(
                        CONF_Q12_AUTO_STOP_RESEND, DEFAULT_Q12_AUTO_STOP_RESEND
                    ),
                ): vol.In(q12_choices),
                vol.Required(
                    CONF_O1_ALARM_BEHAVIOUR,
                    default=values.get(
                        CONF_O1_ALARM_BEHAVIOUR, DEFAULT_O1_ALARM_BEHAVIOUR
                    ),
                ): vol.In(o1_choices),
            }
        )
