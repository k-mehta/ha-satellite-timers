"""Voice dismissal of finished timers.

Why this file exists: when a timer finishes, Home Assistant pops it from
the TimerManager, so the built-in sentences can no longer find it — "cancel
the pasta timer" and "stop the pasta timer" both look the timer up there
(the built-in ``<timer_cancel>`` expansion is ``cancel|stop``) and fail
with "no timer". A finished timer can only be dismissed through the
bridge's own records, which is what this intent does.

Home Assistant loads custom sentences only from
``<config>/custom_sentences/<language>/`` — an integration cannot
contribute sentences any other way. So on setup the integration writes
``DISMISS_SENTENCES_YAML`` there (``async_ensure_dismiss_sentences``),
but only if the file is missing: your edits are never overwritten, and
deleting the file removes voice dismissal until the next restart. The
sentences are loaded lazily on the first conversation after a restart,
which is why setup runs before anyone talks to the hub.
"""

from __future__ import annotations

import logging
from pathlib import Path

import voluptuous as vol

from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import intent

from .bridge import SatelliteTimerBridge, TimerRecord, normalize_timer_name
from .const import DISMISS_SENTENCES_FILE, DOMAIN, INTENT_DISMISS_TIMER

_LOGGER = logging.getLogger(__name__)

DISMISS_SENTENCES_YAML = """# Dismiss sentences, installed by the Satellite Timers integration
# (custom_components/satellite_timers). Home Assistant only loads custom
# sentences from <config>/custom_sentences/<language>/, so the integration
# writes this file there on setup when it is missing. It never overwrites
# your edits; delete it to remove voice dismissal of finished timers.
language: en
intents:
  SatelliteTimersDismissTimer:
    data:
      - sentences:
          - "dismiss [the] finished timer"
          - "dismiss [the] {timer_name:name} timer"
          - "dismiss [the] timer"
"""


def _find_bridge(hass: HomeAssistant) -> SatelliteTimerBridge | None:
    """The bridge of the (single) Satellite Timers entry, if it is set up."""
    for entry_data in hass.data.get(DOMAIN, {}).values():
        if isinstance(entry_data, dict) and entry_data.get("bridge") is not None:
            return entry_data["bridge"]
    return None


async def async_ensure_dismiss_sentences(hass: HomeAssistant) -> bool:
    """Write the dismiss sentences file if it is not there already.

    Returns True when the file is present afterwards. Voice dismissal is
    optional — a failure here is logged, never fatal to setup.
    """
    path = Path(hass.config.path("custom_sentences", "en", DISMISS_SENTENCES_FILE))
    if path.exists():
        return True
    try:
        await hass.async_add_executor_job(_write_dismiss_sentences, path)
    except OSError:
        _LOGGER.warning(
            "Could not write the dismiss sentences file %s; "
            "saying 'dismiss the pasta timer' will not work until it exists",
            path,
            exc_info=True,
        )
        return False
    _LOGGER.info(
        "Installed voice dismissal sentences at %s — 'dismiss the pasta "
        "timer' now clears a finished timer",
        path,
    )
    return True


def _write_dismiss_sentences(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(DISMISS_SENTENCES_YAML, encoding="utf-8")


def _describe_finished(snapshot: TimerRecord) -> str:
    """One finished timer, as spoken back: name + start time.

    "Pasta started 6:02 PM" — the start time is what tells two timers
    with the same name apart (TIMER-11). Without a start time the name
    alone is listed.
    """
    label = snapshot.name or "Timer"
    if snapshot.started_at is not None:
        when = (
            snapshot.started_at.astimezone()
            .strftime("%I:%M %p")
            .lstrip("0")
        )
        return f"{label} started {when}"
    return label


class DismissFinishedTimerIntentHandler(intent.IntentHandler):
    """Intent handler for dismissing a finished timer by voice.

    "Dismiss the pasta timer" (or just "dismiss the timer" when only one
    timer has finished). A running timer is never dismissed by this intent
    — the response says so and points at "cancel" instead, because
    dismissing means "clear the done state", not "stop a running timer".

    TIMER-11 (lists-and-asks, never silently picks): when several
    finished timers match the name — or several have finished with no
    name given — the handler does NOT pick one. It lists them by name
    and start time ("Pasta started 6:02 PM, Pasta started 6:20 PM")
    and asks which one, pointing at the Timers view for the
    unambiguous tap. (The built-in cancel intent for RUNNING timers
    already refuses to pick: Home Assistant raises
    MultipleTimersMatchedError when several match by name. What core
    does not do is list them — that listing for running timers is a
    follow-up, since it would need replacing a core intent handler.)
    """

    intent_type = INTENT_DISMISS_TIMER
    description = "Dismisses a finished timer"
    slot_schema = {
        vol.Optional("name"): cv.string,
    }

    async def async_handle(self, intent_obj: intent.Intent) -> intent.IntentResponse:
        hass = intent_obj.hass
        slots = self.async_validate_slots(intent_obj.slots)
        device_id = intent_obj.device_id

        bridge = _find_bridge(hass)
        if bridge is None:
            raise intent.IntentHandleError("Satellite Timers is not set up")

        # A command from a satellite carries its device id; a command
        # typed into Home Assistant (or from the conversation service
        # without a device) carries none, and then finished timers on any
        # satellite match — same rule Home Assistant's own timer lookup
        # uses (_find_timer only filters by device when one is given).
        finished = [
            snapshot
            for snapshot in bridge.finished_timers()
            if device_id is None or snapshot.device_id == device_id
        ]

        name = slots.get("name", {}).get("value")
        target: TimerRecord | None = None

        if name:
            wanted = normalize_timer_name(str(name))
            matches = [
                snapshot
                for snapshot in finished
                if normalize_timer_name(snapshot.name) == wanted
            ]
            if len(matches) == 1:
                target = matches[0]
            elif len(matches) > 1:
                # TIMER-11: list them and ask — never silently pick one.
                listed = "; ".join(
                    _describe_finished(snapshot) for snapshot in matches
                )
                raise intent.IntentHandleError(
                    f"There is more than one finished timer called "
                    f"{name}: {listed}. I can't tell which one you mean "
                    f"— dismiss the one you want from the Timers view"
                )
            elif bridge.has_live_timer(device_id, str(name)):
                raise intent.IntentHandleError(
                    f"The {name} timer is still running — "
                    f"say 'cancel the {name} timer' to stop it"
                )
            else:
                raise intent.IntentHandleError(
                    f"I couldn't find a finished timer called {name}"
                )
        elif len(finished) == 1:
            target = finished[0]
        elif not finished:
            raise intent.IntentHandleError("There are no finished timers to dismiss")
        else:
            listed = "; ".join(
                _describe_finished(snapshot) for snapshot in finished
            )
            raise intent.IntentHandleError(
                f"There are several finished timers: {listed}. "
                f"Say which one to dismiss"
            )

        bridge.dismiss_timer(target.timer_id)
        response = intent_obj.create_response()
        label = target.name or "Timer"
        response.async_set_speech(f"{label} timer dismissed")
        return response
