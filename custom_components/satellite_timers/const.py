"""Constants for the Satellite Timers integration."""

DOMAIN = "satellite_timers"
CONF_DEVICES = "devices"

# Sound policy (DESIGN-5, Phase 7). The bridge owns sound, the card
# owns the visual; these options set HOW LOUD a finished timer
# sounds. Phase 7 DECISION (the spec gives no numbers): quiet-hours
# volume is 30% of the timer volume; escalation starts at 40% of the
# target and rises 20 points every 10s, reaching full volume in 30s.
CONF_TIMER_VOLUME = "timer_volume"
CONF_QUIET_HOURS_START = "quiet_hours_start"
CONF_QUIET_HOURS_END = "quiet_hours_end"
CONF_QUIET_HOURS_VOLUME_PCT = "quiet_hours_volume_pct"
CONF_ESCALATION = "escalation"

DEFAULT_TIMER_VOLUME = 100
DEFAULT_QUIET_HOURS_START = "22:00"
DEFAULT_QUIET_HOURS_END = "07:00"
DEFAULT_QUIET_HOURS_VOLUME_PCT = 30
DEFAULT_ESCALATION = True

SERVICE_PAUSE_TIMER = "pause_timer"
SERVICE_RESUME_TIMER = "resume_timer"
SERVICE_CANCEL_TIMER = "cancel_timer"
SERVICE_ADD_TIME = "add_time"
SERVICE_DISMISS_TIMER = "dismiss_timer"
SERVICE_RESTART_TIMER = "restart_timer"
SERVICE_START_TIMER = "start_timer"

# Voice dismissal of a finished timer. Home Assistant's built-in timer
# sentences cannot do this: "cancel/stop the pasta timer" looks the timer
# up in the TimerManager, and a finished timer is already gone from there.
# So the bridge registers its own intent and ships the sentences for it
# (see intents.py).
INTENT_DISMISS_TIMER = "SatelliteTimersDismissTimer"
DISMISS_SENTENCES_FILE = "satellite_timers_dismiss.yaml"

CARD_URL_PATH = "/satellite_timers"
CARD_FILE = "timer-screen-card.js"
CARD_URL = f"{CARD_URL_PATH}/{CARD_FILE}"
CARD_RESOURCE_TYPE = "module"
