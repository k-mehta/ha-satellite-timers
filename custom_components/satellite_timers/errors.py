"""Typed bridge errors + the single retry ladder (ERS-1).

Every error the bridge raises is one of these types, classified at the
raise site (ERS-8). Each carries a machine-readable ``code`` — the
service caller sees the message, the log and the error slot carry the
code. No error is ever a bare ``HomeAssistantError`` with an ad-hoc
string: a new failure mode gets a new type here first.

The retry ladder is ERS-1 (§07.9, §09 C14): 4 attempts, delays
[2, 8, 30] seconds, referenced BY NAME everywhere it is used. No
second ladder exists anywhere in the bridge.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from homeassistant.exceptions import HomeAssistantError

from .const import RETRY_DELAYS_S, RETRY_MAX_ATTEMPTS

_LOGGER = logging.getLogger(__name__)


class BridgeError(HomeAssistantError):
    """Base for every typed bridge error. ``code`` is the machine-readable name."""

    code = "bridge_error"


class TimerNotFound(BridgeError):
    """No bridge record exists for the timer id."""

    code = "timer_not_found"


class InvalidCommandId(BridgeError):
    """``command_id`` is missing or is not a UUID v4 (§09 C1)."""

    code = "invalid_command_id"


class AmbiguousSatellite(BridgeError):
    """No satellite could be resolved: none, or several with no primary (§09 C1-R).

    ERS-10 parts, named because this is a user-action error with no
    automatic recovery path of its own: retry budget — none, nothing
    is retried automatically; backoff — none; degraded state — a
    start that cannot resolve a satellite fails, while existing
    timers keep running.
    Re-probe: the resolution is re-run on every start attempt;
    changing the primary in the options flow reloads the entry.
    The message names the Configure path and the voice alternative
    (a voice start heard by a timer satellite resolves to that
    satellite directly)."""

    code = "ambiguous_satellite"


class SatelliteNotSupported(BridgeError):
    """The named satellite is not a configured timer satellite, or the
    satellite that should ring no longer supports timers."""

    code = "satellite_not_supported"


class InvalidDuration(BridgeError):
    """A duration outside the 1 s – 24 h bound (v2 §6 A7)."""

    code = "invalid_duration"


class InvalidName(BridgeError):
    """A timer name that is empty after stripping (§09 C1: name required)."""

    code = "invalid_name"


class UndoExpired(BridgeError):
    """The undo snapshot for the timer is gone (5 s window, §09 C9)."""

    code = "undo_expired"


class BridgeNotReady(BridgeError):
    """The bridge cannot serve the call yet (setup incomplete or the
    TimerManager shape check failed)."""

    code = "bridge_not_ready"


async def retry_with_ladder(
    operation: str,
    coro_factory,
    *,
    logger: logging.Logger = _LOGGER,
) -> object:
    """Run ``coro_factory()`` on the ERS-1 ladder (§07.9).

    4 attempts, delays [2, 8, 30] seconds. Every attempt is logged with
    its attempt number and an ISO timestamp (ERS-9). The last failure
    is re-raised; the caller decides the degraded state.

    A delay of 0 after the first attempt would be a second ladder; there
    is exactly one ladder, and this is it.
    """
    last_error: Exception | None = None
    for attempt in range(1, RETRY_MAX_ATTEMPTS + 1):
        try:
            return await coro_factory()
        except Exception as err:  # noqa: BLE001 — the ladder retries any failure
            last_error = err
            now = datetime.now(timezone.utc).isoformat()
            if attempt < RETRY_MAX_ATTEMPTS:
                delay = RETRY_DELAYS_S[attempt - 1]
                logger.warning(
                    "ERS-1 %s: attempt %d/%d failed at %s (%s); "
                    "retrying in %d s",
                    operation,
                    attempt,
                    RETRY_MAX_ATTEMPTS,
                    now,
                    err,
                    delay,
                )
                await asyncio.sleep(delay)
            else:
                logger.error(
                    "ERS-1 %s: attempt %d/%d failed at %s (%s); "
                    "ladder exhausted, no further retry",
                    operation,
                    attempt,
                    RETRY_MAX_ATTEMPTS,
                    now,
                    err,
                )
    assert last_error is not None  # the loop always runs at least once
    raise last_error
