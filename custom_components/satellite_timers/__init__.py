"""Satellite Timers (W2, from scratch against the settled architecture).

Voice timers set on a satellite live in Home Assistant's memory only,
so no card or automation can see them. The bridge (``bridge.py``) is
the source of truth for timer state: it mirrors every timer into a
sensor, persists timers to HA storage so they survive a restart, and
owns the service table, the voice intent overrides, and the ringing
model. Timers survive a restart with the same id — the TimerManager
executor is rebuilt from the persisted record, never re-created.

This module is only the entry plumbing: create the bridge, forward the
sensor platform, register services + the voice overrides, serve the
timer screen card (``www/``) as a Lovelace resource. No card code is
written in W2.
"""

from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.components.http import StaticPathConfig
from homeassistant.components.lovelace.const import LOVELACE_DATA
from homeassistant.components.lovelace.resources import ResourceStorageCollection
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.collection import ItemNotFound

from . import intents
from .bridge import SatelliteTimerBridge
from .const import (
    CARD_RESOURCE_TYPE,
    CARD_URL,
    CARD_URL_PATH,
    DOMAIN,
    ISSUE_VOICE_ROUTING,
)
from .services import register_services, unregister_services

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Satellite Timers from a config entry.

    Raises ConfigEntryNotReady when the TimerManager is not loaded or
    its shape changed. A failed voice-routing self-check degrades the
    entry (a repair issue + the error slot) but does NOT fail setup:
    dashboard timers and sensors still work; voice goes through the
    built-in path (AD-C2). A failed card registration degrades the
    same way (the card is unregistered; timers and sensors still
    work) and is retried by the bridge's 60 s re-probe (ERS-7)."""
    bridge = SatelliteTimerBridge(hass, entry)
    await bridge.async_setup()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "bridge": bridge,
        "resource_id": None,
    }
    entry.runtime_data = bridge

    try:
        resource_id = await _async_register_frontend(hass)
    except Exception:  # noqa: BLE001 — the card is optional; sensors must not fail with it
        _LOGGER.warning("Could not register the timer screen card", exc_info=True)
        resource_id = None
    hass.data[DOMAIN][entry.entry_id]["resource_id"] = resource_id
    if resource_id is None:
        # ERS-7: the 60 s re-probe retries the registration — one
        # attempt per cycle (the budget), no separate ladder (the
        # cycle is the backoff); degraded state is "the card is not
        # served", dashboard timers and sensors still work. YAML mode
        # settles at once inside _reprobe_frontend: there is nothing
        # to retry there.
        bridge.frontend_reprobe = lambda: _reprobe_frontend(hass, entry)
        _LOGGER.warning(
            "Timer screen card is not registered; dashboard timers and "
            "sensors still work, and the 60 s re-probe retries the "
            "registration"
        )

    register_services(hass, bridge)
    intents.install(hass, bridge)

    if not intents.assert_installed(hass):
        bridge.report_error(
            "bridge_voice_routing",
            "Voice timer routing did not install. Dashboard timers and "
            "sensors still work; voice timers go through the built-in path.",
        )
        ir.async_create_issue(
            hass,
            DOMAIN,
            ISSUE_VOICE_ROUTING,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_VOICE_ROUTING,
        )
        _LOGGER.warning(
            "Satellite Timers voice routing self-check failed; continuing "
            "degraded (dashboard timers still work)"
        )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload the entry and put the voice handlers back as found."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not unloaded:
        return False
    bridge = entry.runtime_data
    if isinstance(bridge, SatelliteTimerBridge):
        intents.uninstall(hass, bridge)
        await bridge.async_unload()
    unregister_services(hass)
    data = hass.data.get(DOMAIN, {}).pop(entry.entry_id, {})
    await _async_unregister_frontend(hass, data.get("resource_id"))
    return True


async def _update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """An options change (primary satellite) reloads the entry."""
    await hass.config_entries.async_reload(entry.entry_id)


async def _reprobe_frontend(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """One 60 s re-probe attempt for the timer screen card (ERS-7,
    C-NB-07). Returns True when settled — registered, or Lovelace is
    in YAML mode (permanent: the card can never be registered there,
    so there is nothing to retry) — and False when the next re-probe
    cycle should try again (Lovelace not loaded yet, or the attempt
    raised). A ``None`` resource id from a storage-mode Lovelace
    cannot happen (``_async_register_frontend`` returns an id there),
    so it is treated as unsettled too."""
    lovelace_data = hass.data.get(LOVELACE_DATA)
    if lovelace_data is None:
        return False  # Lovelace not loaded yet — transient
    if not isinstance(lovelace_data.resources, ResourceStorageCollection):
        return True  # YAML mode — permanent, nothing to retry
    try:
        resource_id = await _async_register_frontend(hass)
    except Exception:  # noqa: BLE001 — transient; the re-probe retries
        return False
    if resource_id is not None:
        hass.data[DOMAIN][entry.entry_id]["resource_id"] = resource_id
        return True
    return False


async def _async_register_frontend(hass: HomeAssistant) -> str | None:
    """Serve the timer screen card and add its Lovelace resource.

    Returns the resource id, or None when Lovelace is not running in
    storage mode (or is not loaded at all) — the sensors still work.
    The static path is registered once per Home Assistant process; the
    resource is added only if no item with our URL is already present,
    so a reload never duplicates it."""
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
