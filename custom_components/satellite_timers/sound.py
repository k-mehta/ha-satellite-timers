"""Timer sound policy (DESIGN-5, Phase 7).

The bridge owns sound, the card owns the visual. The satellite plays
its own alarm when a timer finishes; this module only decides HOW
LOUD it should be. A consumer that honours the policy reads the
finished-timer sensor's ``sound_volume_pct`` attribute (and
``quiet_hours``) and sets its volume accordingly.

No speaker is confirmed, ordered or assumed here — S1 (no confirmed
hub speaker) is still unresolved, and the hardware order is LAST.
Until a speaker exists and a consumer honours these attributes, the
policy is configuration only: the numbers are real, the sound is
not. Live verification stays OPEN (needs S1 + a honoring consumer).

Phase 7 DECISION (the spec gives no numbers): the quiet-hours volume
is 30% of the configured timer volume; escalation starts at 40% of
the target and rises 20 points every 10 seconds, reaching full
volume in 30 seconds. These are defaults a household can tune in
the options flow — they are not measurements of any device.

This module is pure (no Home Assistant imports) so it can be unit
tested without the HA test harness.
"""

from __future__ import annotations

from datetime import datetime, time as dt_time

from .const import (
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
)

# Escalation ramp (Phase 7 DECISION): start at 40% of the target,
# +20 points every 10 seconds, full volume in 30 seconds.
ESCALATION_START_FRACTION = 0.4
ESCALATION_STEP_FRACTION = 0.2
ESCALATION_STEP_SECONDS = 10
ESCALATION_FULL_SECONDS = 30


def clamp_volume_pct(value: object, default: int = DEFAULT_TIMER_VOLUME) -> int:
    """Clamp a volume to 0-100; garbage becomes the default."""
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    if number != number:  # NaN
        return default
    if number < 0:
        return 0
    if number > 100:
        return 100
    return int(number)


def is_valid_hhmm(value: object) -> bool:
    """Whether ``value`` is a valid \"HH:MM\" time."""
    try:
        hour_text, minute_text = str(value).strip().split(":")
        hour = int(hour_text)
        minute = int(minute_text)
        return 0 <= hour <= 23 and 0 <= minute <= 59
    except (ValueError, AttributeError):
        return False


def parse_time_hhmm(value: object, default: str) -> dt_time:
    """Parse an \"HH:MM\" time; garbage becomes the default time."""
    if isinstance(value, dt_time):
        return value
    for text in (value, default):
        try:
            hour_text, minute_text = str(text).strip().split(":")
            hour = int(hour_text)
            minute = int(minute_text)
            if not 0 <= hour <= 23 or not 0 <= minute <= 59:
                raise ValueError
            return dt_time(hour, minute)
        except (ValueError, AttributeError):
            continue
    return dt_time(0, 0)


def is_quiet_hours(now: datetime, start: object, end: object) -> bool:
    """Whether ``now`` falls in the quiet-hours window.

    ``start``/``end`` are \"HH:MM\" strings (or ``datetime.time``). A
    window that crosses midnight (22:00-07:00) is quiet from the start
    through midnight and from midnight to the end. A window whose start
    equals its end is empty — no quiet hours at all.
    """
    start_time = start if isinstance(start, dt_time) else parse_time_hhmm(start, DEFAULT_QUIET_HOURS_START)
    end_time = end if isinstance(end, dt_time) else parse_time_hhmm(end, DEFAULT_QUIET_HOURS_END)
    if start_time == end_time:
        return False
    current = now.time()
    if start_time < end_time:
        return start_time <= current < end_time
    return current >= start_time or current < end_time


def sound_options(entry_options: dict | None) -> dict:
    """Normalise stored options (entry.options, or {}) to typed values."""
    options = entry_options or {}
    return {
        CONF_TIMER_VOLUME: clamp_volume_pct(
            options.get(CONF_TIMER_VOLUME, DEFAULT_TIMER_VOLUME)
        ),
        CONF_QUIET_HOURS_START: parse_time_hhmm(
            options.get(CONF_QUIET_HOURS_START, DEFAULT_QUIET_HOURS_START),
            DEFAULT_QUIET_HOURS_START,
        ),
        CONF_QUIET_HOURS_END: parse_time_hhmm(
            options.get(CONF_QUIET_HOURS_END, DEFAULT_QUIET_HOURS_END),
            DEFAULT_QUIET_HOURS_END,
        ),
        CONF_QUIET_HOURS_VOLUME_PCT: clamp_volume_pct(
            options.get(CONF_QUIET_HOURS_VOLUME_PCT, DEFAULT_QUIET_HOURS_VOLUME_PCT),
            DEFAULT_QUIET_HOURS_VOLUME_PCT,
        ),
        CONF_ESCALATION: bool(options.get(CONF_ESCALATION, DEFAULT_ESCALATION)),
    }


def timer_volume_pct(now: datetime, options: dict | None = None) -> int:
    """The volume a finished timer should sound at, right now.

    The configured timer volume, or the quiet-hours volume (30% of
    the timer volume by default) while quiet hours are in effect.
    This is the TARGET — before the escalation ramp is applied.
    """
    opts = sound_options(options)
    volume = opts[CONF_TIMER_VOLUME]
    if is_quiet_hours(now, opts[CONF_QUIET_HOURS_START], opts[CONF_QUIET_HOURS_END]):
        return round(volume * opts[CONF_QUIET_HOURS_VOLUME_PCT] / 100)
    return volume


def escalation_volume_pct(
    target_pct: int, elapsed_seconds: float, options: dict | None = None
) -> int:
    """The volume to sound at ``elapsed_seconds`` after the finish.

    Escalation on (default): 40% of the target at once, +20 points
    every 10 seconds, full volume in 30 seconds. Escalation off (or a
    non-positive elapsed time): the target at once. The result never
    exceeds the target.
    """
    target = clamp_volume_pct(target_pct, DEFAULT_TIMER_VOLUME)
    opts = sound_options(options)
    if not opts[CONF_ESCALATION]:
        return target
    if elapsed_seconds <= 0:
        return round(target * ESCALATION_START_FRACTION)
    steps = min(int(elapsed_seconds // ESCALATION_STEP_SECONDS), 3)
    fraction = ESCALATION_START_FRACTION + ESCALATION_STEP_FRACTION * steps
    return min(target, round(target * fraction))


def current_sound_volume_pct(
    now: datetime, finished_at: datetime | None, options: dict | None = None
) -> int:
    """The volume a finished timer's alarm should be at right now.

    The target for the finish moment, ramped by the escalation since
    then. A timer with no recorded finish time sounds at its target.
    """
    target = timer_volume_pct(now, options)
    if finished_at is None:
        return target
    elapsed = (now - finished_at).total_seconds()
    return escalation_volume_pct(target, elapsed, options)
