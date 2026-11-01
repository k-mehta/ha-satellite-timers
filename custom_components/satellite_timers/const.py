"""Constants for the Satellite Timers integration."""

DOMAIN = "satellite_timers"
CONF_DEVICES = "devices"

SERVICE_PAUSE_TIMER = "pause_timer"
SERVICE_RESUME_TIMER = "resume_timer"
SERVICE_CANCEL_TIMER = "cancel_timer"
SERVICE_ADD_TIME = "add_time"
SERVICE_DISMISS_TIMER = "dismiss_timer"
SERVICE_RESTART_TIMER = "restart_timer"

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
