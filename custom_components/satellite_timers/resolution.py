"""Pure satellite resolution + command_id validation.

No Home Assistant imports: the bridge supplies the live facts
(``live_devices`` = TimerManager handler keys; the entity-registry
lookup result) and this module decides which device answers, or raises
the loud error that names the voice alternative and the Configure
path (W2-1, §08 row 17, §09 C1-R).

Rules:
- Explicit device wins, but only when it IS a live timer device
  (matched exactly — no substring, no sort order, no silent pick).
  An explicit value that is an entity id is resolved by the bridge
  first (via the entity registry); by the time it reaches here it is
  always a device id, or None.
- Then the stored primary (``primary_satellite`` config-entry
  option). A primary that is not a live device is a loud error
  naming the Configure path — never a silent fall-through to the
  single device.
- Then a single live device.
- Otherwise: loud error naming the voice alternative ("say 'Okay
  Nabu, set a timer'") and the Configure path.

``command_id`` (W2-2, §03.2 M1): every mutating call carries one,
a UUID v4 string. ``is_uuid_v4`` is the one validation site.
"""

from __future__ import annotations

import uuid

from .errors import AmbiguousSatellite, InvalidCommandId, SatelliteNotSupported

VOICE_ALTERNATIVE = "say 'Okay Nabu, set a timer' instead"
CONFIGURE_PATH = (
    "Settings → Devices & Services → Satellite Timers → Configure"
)


def is_uuid_v4(value: object) -> bool:
    """True only for a lowercase UUID v4 string (RFC 4122).

    ``uuid.UUID(value, version=4)`` alone is not strict: it accepts
    any version and re-writes the version bits. So the parsed value's
    version and variant are checked, and the canonical string form
    must round-trip exactly — uppercase, braces, or a non-v4 version
    all fail.
    """
    if not isinstance(value, str):
        return False
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return False
    if parsed.version != 4:
        return False
    if parsed.variant != uuid.RFC_4122:
        return False
    return str(parsed) == value


def new_command_id() -> str:
    """One fresh UUID v4 (stdlib ``uuid.uuid4()`` — RFC 4122)."""
    return str(uuid.uuid4())


def validate_command_id(value: object) -> str:
    """Return ``value`` when it is a UUID v4, else raise."""
    if not is_uuid_v4(value):
        raise InvalidCommandId(
            "command_id must be a UUID v4 string; the call was not executed"
        )
    return value


def resolve_satellite_device(
    live_devices: list[str],
    primary: str | None,
    explicit_device_id: str | None,
) -> str:
    """Pick the timer device for a start, or raise the loud error.

    ``live_devices`` is the live TimerManager handler keys — the only
    devices that can actually time. ``primary`` is the stored
    ``primary_satellite`` option ("" / None = unset). ``explicit_device_id``
    is a device id the caller named, already resolved from an entity id
    by the bridge (or None).
    """
    live = set(live_devices)
    if explicit_device_id is not None:
        if explicit_device_id in live:
            return explicit_device_id
        raise SatelliteNotSupported(
            f"The satellite you named is not a timer satellite "
            f"({explicit_device_id!r} has no timer handler). "
            f"Use one of the configured satellites, or {VOICE_ALTERNATIVE}."
        )
    if primary:
        if primary in live:
            return primary
        raise SatelliteNotSupported(
            "The primary satellite is not available right now "
            f"({primary!r} has no timer handler). Pick another at "
            f"{CONFIGURE_PATH}, or {VOICE_ALTERNATIVE}."
        )
    if len(live) == 1:
        return next(iter(live))
    if not live:
        raise AmbiguousSatellite(
            "No timer satellite is configured yet. Set one up, or "
            f"{VOICE_ALTERNATIVE}. Nothing is retried automatically: "
            "the next start attempt re-checks."
        )
    raise AmbiguousSatellite(
        "More than one timer satellite is configured and no primary is "
        f"set — pick one at {CONFIGURE_PATH}, or {VOICE_ALTERNATIVE}. "
        "Nothing is retried automatically: the next start attempt "
        "re-checks whether a primary is set."
    )
