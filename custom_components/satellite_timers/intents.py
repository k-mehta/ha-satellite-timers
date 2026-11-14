"""Voice intent overrides (W2-6, §03.7).

Seven of the eight timer intents are overridden with
``intent.async_register`` (last-write-wins); ``HassTimerStatus`` is
NOT overridden — status questions keep going to the built-in handler,
which reads the TimerManager directly. The bridge owns routing; the
built-in handlers are the delegation target and are restored exactly
on unload (AD-C3 — never a bare remove).

Delegation rule (§03.7, AD-C2):
- a ``conversation_command`` slot (a delayed command, not a timer)
  goes straight to the displaced handler;
- a timer the TimerManager matches that is NOT a bridge timer goes
  straight to the displaced handler (the bridge never touches a timer
  it does not own);
- a ``_find_timer`` miss consults the bridge records, because a
  ringing/finished timer is no longer in the TimerManager: exactly one
  match is acknowledged (ringing) or dismissed (finished); several
  matches raise the core ``MultipleTimersMatchedError``; none
  delegates (the standard not-found response).

Voice starts are bridge starts (``started_via: voice``) with a
server-side ``command_id`` (``uuid.uuid4()``). A nameless voice start
is allowed (core's name slot is optional); the bridge record carries
an empty name and the sensor shows the card's own fallback.

A bridge error inside a voice handler is SPOKEN, not swallowed: the
handler returns an error response carrying the plain-words message and
its recovery action (ERS-4). A generic ``IntentUnexpectedError`` ("Error
handling HassStartTimer") would tell the user nothing.
"""

from __future__ import annotations

import logging
from typing import Any

import probatio

from homeassistant.components.intent.timers import (
    FindTimerFilter,
    MultipleTimersMatchedError,
    TimerNotFoundError,
    _find_timer,
    _find_timers,
    _get_total_seconds,
    _normalize_name,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import intent

from .bridge import SatelliteTimerBridge
from .errors import BridgeError
from .records import TimerRecord
from .resolution import new_command_id

_LOGGER = logging.getLogger(__name__)


class _BridgeHandler(intent.IntentHandler):
    """Base for the seven overrides. Subclasses implement ``_handle``;
    this wrapper turns a bridge error into a spoken error response and
    lets core intent errors (not-found, multiple-matched) propagate so
    the conversation agent gives its standard reply."""

    def __init__(
        self,
        bridge: SatelliteTimerBridge,
        displaced: intent.IntentHandler | None,
    ) -> None:
        self._bridge = bridge
        self._displaced = displaced

    async def async_handle(self, intent_obj: intent.Intent) -> intent.IntentResponse:
        slots = self.async_validate_slots(intent_obj.slots)
        try:
            response = await self._handle(intent_obj, slots)
        except BridgeError as err:
            response = intent_obj.create_response()
            response.async_set_error(
                intent.IntentResponseErrorCode.FAILED_TO_HANDLE, str(err)
            )
            return response
        if response is not None:
            return response
        return intent_obj.create_response()

    async def _handle(
        self, intent_obj: intent.Intent, slots: dict[str, Any]
    ) -> intent.IntentResponse | None:
        raise NotImplementedError

    async def _delegate(self, intent_obj: intent.Intent) -> intent.IntentResponse:
        """Hand the intent to the displaced built-in handler, untouched."""
        if self._displaced is None:
            raise TimerNotFoundError
        return await self._displaced.async_handle(intent_obj)


class BridgeStartTimerHandler(_BridgeHandler):
    """Start a timer by voice. The hearing satellite answers when it is
    a timer device; otherwise the bridge's primary/single/loud-error
    resolution decides (W2-6)."""

    intent_type = intent.INTENT_START_TIMER
    description = "Starts a new timer"
    slot_schema = {  # verbatim from components/intent/timers.py StartTimerIntentHandler
        probatio.Required(probatio.Any("hours", "minutes", "seconds")): cv.positive_int,
        probatio.Optional("name"): cv.string,
        probatio.Optional("conversation_command"): cv.string,
    }

    async def _handle(
        self, intent_obj: intent.Intent, slots: dict[str, Any]
    ) -> intent.IntentResponse | None:
        if "conversation_command" in slots and slots["conversation_command"]["value"].strip():
            # A delayed command, not a timer: the built-in handler owns it.
            return await self._delegate(intent_obj)
        hours = int(slots["hours"]["value"]) if "hours" in slots else None
        minutes = int(slots["minutes"]["value"]) if "minutes" in slots else None
        seconds = int(slots["seconds"]["value"]) if "seconds" in slots else None
        name = slots["name"]["value"] if "name" in slots else None
        duration = (hours or 0) * 3600 + (minutes or 0) * 60 + (seconds or 0)
        timer_manager = intent_obj.hass.data["intent.timer"]
        device_id = None
        if intent_obj.device_id and timer_manager.is_timer_device(intent_obj.device_id):
            device_id = intent_obj.device_id
        await self._bridge.async_start_timer(
            new_command_id(),
            name or "",
            duration,
            device_id=device_id,
            started_via="voice",
            start_hours=hours,
            start_minutes=minutes,
            start_seconds=seconds,
            language=intent_obj.language,
        )
        return None


class BridgeCancelTimerHandler(_BridgeHandler):
    """Cancel a timer by voice — including a ringing or finished one,
    which the built-in handler can no longer find."""

    intent_type = intent.INTENT_CANCEL_TIMER
    description = "Cancels a timer"
    slot_schema = {  # verbatim from components/intent/timers.py CancelTimerIntentHandler
        probatio.Any("start_hours", "start_minutes", "start_seconds"): cv.positive_int,
        probatio.Optional("name"): cv.string,
        probatio.Optional("area"): cv.string,
    }

    async def _handle(
        self, intent_obj: intent.Intent, slots: dict[str, Any]
    ) -> intent.IntentResponse | None:
        hass = intent_obj.hass
        try:
            timer = _find_timer(hass, intent_obj.device_id, slots)
        except TimerNotFoundError:
            # The TimerManager holds no match: ask the bridge records
            # (a ringing/finished timer lives there).
            record = _find_record(hass, self._bridge, intent_obj.device_id, slots)
            if record.state == "ringing":
                await self._bridge.async_acknowledge_timer(
                    new_command_id(), record.timer_id
                )
            elif record.state == "finished":
                await self._bridge.async_dismiss_timer(
                    new_command_id(), record.timer_id
                )
            else:
                await self._bridge.async_cancel_timer(
                    new_command_id(), record.timer_id
                )
            return None
        if timer.id in self._bridge.records:
            await self._bridge.async_cancel_timer(new_command_id(), timer.id)
            return None
        return await self._delegate(intent_obj)


class BridgeCancelAllTimersHandler(_BridgeHandler):
    """Cancel all timers by voice. Bridge timers go through the bridge
    (so each gets its undo snapshot); the rest go straight to the
    TimerManager. A RINGING timer is no longer in the TimerManager,
    so the TM loop never sees it — the sweep over bridge records
    below acknowledges it (which issues the stop to the satellite),
    exactly like the single-cancel handler's _find_record fallback.
    The response counts every cancellation, as core's does — ringing
    included. Finished timers are left alone: whether cancel-all
    should dismiss them is unspecified in the settled architecture."""

    intent_type = intent.INTENT_CANCEL_ALL_TIMERS
    description = "Cancels all timers"
    slot_schema = {  # verbatim from components/intent/timers.py CancelAllTimersIntentHandler
        probatio.Optional("area"): cv.string,
    }

    async def _handle(
        self, intent_obj: intent.Intent, slots: dict[str, Any]
    ) -> intent.IntentResponse | None:
        hass = intent_obj.hass
        timer_manager = hass.data["intent.timer"]
        canceled = 0
        handled: set[str] = set()
        for timer in _find_timers(hass, intent_obj.device_id, slots):
            handled.add(timer.id)
            if timer.id in self._bridge.records:
                await self._bridge.async_cancel_timer(new_command_id(), timer.id)
            else:
                timer_manager.cancel_timer(timer.id)
            canceled += 1
        # A ringing timer is no longer in the TimerManager, so
        # _find_timers never returns it — but "cancel all timers" is
        # the silence-everything utterance, and a ringing timer is
        # the one that most needs silencing. Sweep the bridge records
        # for ringing timers the TM loop missed (same _find_timers
        # slot filters: name, area, start time — device only orders
        # in core, it never filters) and acknowledge each: the
        # acknowledge issues the stop to the satellite and finishes
        # the record, and it is counted like any other cancellation.
        for record in list(self._bridge.records.values()):
            if record.state != "ringing":
                continue
            if record.timer_id in handled:
                continue
            if record.timer_id in timer_manager.timers:
                continue  # belt-and-braces: a ringing timer has no TM timer
            if not _record_matches_cancel_all(record, slots):
                continue
            await self._bridge.async_acknowledge_timer(
                new_command_id(), record.timer_id
            )
            canceled += 1
        response = intent_obj.create_response()
        speech_slots: dict[str, Any] = {"canceled": canceled}
        if "area" in slots:
            speech_slots["area"] = slots["area"]["value"]
        response.async_set_speech_slots(speech_slots)
        return response


class BridgeIncreaseTimerHandler(_BridgeHandler):
    """Add time to a timer by voice."""

    intent_type = intent.INTENT_INCREASE_TIMER
    description = "Adds more time to a timer"
    slot_schema = {  # verbatim from components/intent/timers.py IncreaseTimerIntentHandler
        probatio.Any("hours", "minutes", "seconds"): cv.positive_int,
        probatio.Any("start_hours", "start_minutes", "start_seconds"): cv.positive_int,
        probatio.Optional("name"): cv.string,
        probatio.Optional("area"): cv.string,
    }

    async def _handle(
        self, intent_obj: intent.Intent, slots: dict[str, Any]
    ) -> intent.IntentResponse | None:
        total_seconds = _get_total_seconds(slots)
        timer = _find_timer(intent_obj.hass, intent_obj.device_id, slots)
        if timer.id in self._bridge.records:
            await self._bridge.async_add_time(
                new_command_id(), timer.id, total_seconds
            )
            return None
        return await self._delegate(intent_obj)


class BridgeDecreaseTimerHandler(_BridgeHandler):
    """Remove time from a timer by voice."""

    intent_type = intent.INTENT_DECREASE_TIMER
    description = "Removes time from a timer"
    slot_schema = {  # verbatim from components/intent/timers.py DecreaseTimerIntentHandler
        probatio.Required(probatio.Any("hours", "minutes", "seconds")): cv.positive_int,
        probatio.Any("start_hours", "start_minutes", "start_seconds"): cv.positive_int,
        probatio.Optional("name"): cv.string,
        probatio.Optional("area"): cv.string,
    }

    async def _handle(
        self, intent_obj: intent.Intent, slots: dict[str, Any]
    ) -> intent.IntentResponse | None:
        total_seconds = _get_total_seconds(slots)
        timer = _find_timer(intent_obj.hass, intent_obj.device_id, slots)
        if timer.id in self._bridge.records:
            await self._bridge.async_add_time(
                new_command_id(), timer.id, -total_seconds
            )
            return None
        return await self._delegate(intent_obj)


class BridgePauseTimerHandler(_BridgeHandler):
    """Pause a running timer by voice."""

    intent_type = intent.INTENT_PAUSE_TIMER
    description = "Pauses a running timer"
    slot_schema = {  # verbatim from components/intent/timers.py PauseTimerIntentHandler
        probatio.Any("start_hours", "start_minutes", "start_seconds"): cv.positive_int,
        probatio.Optional("name"): cv.string,
        probatio.Optional("area"): cv.string,
    }

    async def _handle(
        self, intent_obj: intent.Intent, slots: dict[str, Any]
    ) -> intent.IntentResponse | None:
        timer = _find_timer(
            intent_obj.hass,
            intent_obj.device_id,
            slots,
            find_filter=FindTimerFilter.ONLY_ACTIVE,
        )
        if timer.id in self._bridge.records:
            await self._bridge.async_pause_timer(new_command_id(), timer.id)
            return None
        return await self._delegate(intent_obj)


class BridgeUnpauseTimerHandler(_BridgeHandler):
    """Resume a paused timer by voice."""

    intent_type = intent.INTENT_UNPAUSE_TIMER
    description = "Resumes a paused timer"
    slot_schema = {  # verbatim from components/intent/timers.py UnpauseTimerIntentHandler
        probatio.Any("start_hours", "start_minutes", "start_seconds"): cv.positive_int,
        probatio.Optional("name"): cv.string,
        probatio.Optional("area"): cv.string,
    }

    async def _handle(
        self, intent_obj: intent.Intent, slots: dict[str, Any]
    ) -> intent.IntentResponse | None:
        timer = _find_timer(
            intent_obj.hass,
            intent_obj.device_id,
            slots,
            find_filter=FindTimerFilter.ONLY_INACTIVE,
        )
        if timer.id in self._bridge.records:
            await self._bridge.async_resume_timer(new_command_id(), timer.id)
            return None
        return await self._delegate(intent_obj)


_HANDLERS: tuple[tuple[str, type[_BridgeHandler]], ...] = (
    (intent.INTENT_START_TIMER, BridgeStartTimerHandler),
    (intent.INTENT_CANCEL_TIMER, BridgeCancelTimerHandler),
    (intent.INTENT_CANCEL_ALL_TIMERS, BridgeCancelAllTimersHandler),
    (intent.INTENT_INCREASE_TIMER, BridgeIncreaseTimerHandler),
    (intent.INTENT_DECREASE_TIMER, BridgeDecreaseTimerHandler),
    (intent.INTENT_PAUSE_TIMER, BridgePauseTimerHandler),
    (intent.INTENT_UNPAUSE_TIMER, BridgeUnpauseTimerHandler),
)


def _record_matches_cancel_all(
    record: TimerRecord, slots: dict[str, Any]
) -> bool:
    """The core ``_find_timers`` slot filters (name → area → start
    time), over a bridge record: a ringing timer the TimerManager no
    longer holds still has to respect a "cancel all timers in the
    kitchen". Same comparisons as ``_find_record`` above, so the two
    handlers agree; device only orders in core, it never filters, so
    there is no device filter here either."""
    if "name" in slots:
        if _normalize_name(record.name) != _normalize_name(slots["name"]["value"]):
            return False
    if "area" in slots:
        if (record.area_name or "") != _normalize_name(slots["area"]["value"]):
            return False
    start_hours = int(slots["start_hours"]["value"]) if "start_hours" in slots else None
    start_minutes = (
        int(slots["start_minutes"]["value"]) if "start_minutes" in slots else None
    )
    start_seconds = (
        int(slots["start_seconds"]["value"]) if "start_seconds" in slots else None
    )
    if (
        start_hours is not None
        or start_minutes is not None
        or start_seconds is not None
    ):
        if not (
            (record.start_hours == start_hours)
            and (record.start_minutes == start_minutes)
            and (record.start_seconds == start_seconds)
        ):
            return False
    return True


def _find_record(
    hass: HomeAssistant,
    bridge: SatelliteTimerBridge,
    device_id: str | None,
    slots: dict[str, Any],
) -> TimerRecord:
    """The core ``_find_timer`` cascade, over bridge records instead of
    TimerManager timers: name → area → start-time → device → area/
    floor. A ringing or finished timer is only findable here (the
    TimerManager popped it). Same errors as core: one match returns it,
    several raise ``MultipleTimersMatchedError``, none raises
    ``TimerNotFoundError``."""
    matching: list[TimerRecord] = list(bridge.records.values())
    has_filter = False
    if "name" in slots:
        has_filter = True
        name_norm = _normalize_name(slots["name"]["value"])
        matching = [r for r in matching if _normalize_name(r.name) == name_norm]
        if len(matching) == 1:
            return matching[0]
    if "area" in slots:
        has_filter = True
        area_norm = _normalize_name(slots["area"]["value"])
        matching = [r for r in matching if r.area_name == area_norm]
        if len(matching) == 1:
            return matching[0]
    start_hours = int(slots["start_hours"]["value"]) if "start_hours" in slots else None
    start_minutes = (
        int(slots["start_minutes"]["value"]) if "start_minutes" in slots else None
    )
    start_seconds = (
        int(slots["start_seconds"]["value"]) if "start_seconds" in slots else None
    )
    if (
        start_hours is not None
        or start_minutes is not None
        or start_seconds is not None
    ):
        has_filter = True
        matching = [
            r
            for r in matching
            if (r.start_hours == start_hours)
            and (r.start_minutes == start_minutes)
            and (r.start_seconds == start_seconds)
        ]
        if len(matching) == 1:
            return matching[0]
    if (not has_filter) and (len(matching) == 1):
        return matching[0]
    if matching and device_id:
        by_device = [r for r in matching if r.device_id == device_id]
        if len(by_device) == 1:
            return by_device[0]
        device_registry = dr.async_get(hass)
        area_registry = ar.async_get(hass)
        device = device_registry.async_get(device_id)
        if device is not None:
            area_id = dr.async_get_effective_area_id(hass, device)
            area = area_registry.async_get_area(area_id) if area_id else None
            if area is not None:
                by_area = [r for r in matching if r.area_id == area.id]
                if len(by_area) == 1:
                    return by_area[0]
                by_floor = [
                    r
                    for r in matching
                    if _record_floor_id(r) == area.floor_id
                ]
                if len(by_floor) == 1:
                    return by_floor[0]
    if matching:
        raise MultipleTimersMatchedError
    raise TimerNotFoundError


def _record_floor_id(record: TimerRecord) -> str | None:
    """A record's floor, frozen at start like core's
    TimerInfo.floor_id (timers.py:302) — read off the record, never
    re-derived from the area registry (C3-01: an area moved to
    another floor after the timer started changed the derived floor,
    so an ambiguous cancel resolved a two-way floor tie to one
    timer and silenced the wrong one)."""
    return record.floor_id


def install(hass: HomeAssistant, bridge: SatelliteTimerBridge) -> None:
    """Override the seven timer intents, capturing the displaced
    built-in handlers on the bridge for the unload. Idempotent: a
    re-install keeps the originally displaced handlers."""
    existing = hass.data.get(intent.DATA_KEY, {})
    displaced: dict[str, intent.IntentHandler] = {}
    for intent_type, handler_cls in _HANDLERS:
        current = existing.get(intent_type)
        if isinstance(current, _BridgeHandler):
            continue  # already ours; the displaced handler is already stored
        if current is not None:
            displaced[intent_type] = current
        intent.async_register(hass, handler_cls(bridge, current))
    stored = dict(bridge.displaced_intents)
    stored.update(displaced)
    bridge.displaced_intents = stored
    _LOGGER.info(
        "Satellite Timers voice routing installed: %d timer intents overridden",
        len(_HANDLERS),
    )


def uninstall(hass: HomeAssistant, bridge: SatelliteTimerBridge) -> None:
    """Re-register the displaced handlers exactly (AD-C3 — never a
    bare remove). A handler another integration has since overridden
    is left alone: the bridge never clobbers a newer registration."""
    existing = hass.data.get(intent.DATA_KEY, {})
    displaced = dict(bridge.displaced_intents)
    for intent_type, _handler_cls in _HANDLERS:
        current = existing.get(intent_type)
        if not isinstance(current, _BridgeHandler):
            continue
        original = displaced.get(intent_type)
        if original is not None:
            intent.async_register(hass, original)
        else:
            intent.async_remove(hass, intent_type)
    bridge.displaced_intents = {}
    _LOGGER.info("Satellite Timers voice routing removed: built-in handlers restored")


def assert_installed(hass: HomeAssistant) -> bool:
    """The AD-C2 startup self-check: all seven overrides resolve to
    bridge handlers, and HassTimerStatus is NOT one of ours."""
    handlers = hass.data.get(intent.DATA_KEY, {})
    for intent_type, _handler_cls in _HANDLERS:
        if not isinstance(handlers.get(intent_type), _BridgeHandler):
            return False
    if isinstance(handlers.get(intent.INTENT_TIMER_STATUS), _BridgeHandler):
        return False
    return True
