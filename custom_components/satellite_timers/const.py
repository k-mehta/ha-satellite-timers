"""Constants for the Satellite Timers integration."""

DOMAIN = "satellite_timers"
CONF_DEVICES = "devices"

# Bridge options (§5.3/§5.4, RB-R2-4/RB-R2-7 — fixes-18 item 30: the exact
# key strings + value literals are this step's implementation choice,
# recorded in the rebuild log). The Q11/Q12 fields record the SETTLED
# answers, not a user choice; O1 stays unsettled until the §25 listen
# test settles it. There is NO Q13 field — Q13 is DEAD (no bypass exists
# to suppress).
CONF_Q11_RING_TIMING = "q11_ring_timing"
CONF_Q12_AUTO_STOP_RESEND = "q12_auto_stop_resend"
CONF_O1_ALARM_BEHAVIOUR = "o1_alarm_behaviour"
CONF_PRIMARY_SATELLITE = "primary_satellite"

# Q11 — settled ~15:02 PST Nov 12: a timer that finished while the system
# was down NEVER rings. "never_ring" is the settled value and the default.
Q11_NEVER_RING = "never_ring"
Q11_RING_ON_LIVENESS_DRAIN = "ring_on_liveness_drain"  # rejected (a)
Q11_RING_AT_BRIDGE_START = "ring_at_bridge_start"  # rejected (b)
Q11_OPTIONS = (Q11_NEVER_RING, Q11_RING_ON_LIVENESS_DRAIN,
               Q11_RING_AT_BRIDGE_START)
DEFAULT_Q11_RING_TIMING = Q11_NEVER_RING

# Q12 — settled (b) ~08:41 PST Nov 12: never re-ring a stopped alarm.
Q12_NEVER_RESEND = "never_resend"
Q12_RESEND_ONCE_PER_REGISTRATION = "resend_once_per_registration"  # rejected (a)
Q12_OPTIONS = (Q12_NEVER_RESEND, Q12_RESEND_ONCE_PER_REGISTRATION)
DEFAULT_Q12_AUTO_STOP_RESEND = Q12_NEVER_RESEND

# O1 — the satellite firmware's alarm behaviour, measured by the §25
# listen test (RB-R0-7). "unsettled" is the default until it settles.
O1_UNSETTLED = "unsettled"
O1_RINGS_UNTIL_CANCELLED = "rings_until_cancelled"
O1_AUTO_STOP = "auto_stop"
O1_OPTIONS = (O1_UNSETTLED, O1_RINGS_UNTIL_CANCELLED, O1_AUTO_STOP)
DEFAULT_O1_ALARM_BEHAVIOUR = O1_UNSETTLED

# Primary satellite (§5.4): the device id the bridge rings when a chip
# tap names no satellite. "" in the options flow means unset.
PRIMARY_SATELLITE_UNSET = ""

SERVICE_PAUSE_TIMER = "pause_timer"
SERVICE_RESUME_TIMER = "resume_timer"
SERVICE_CANCEL_TIMER = "cancel_timer"
SERVICE_ADD_TIME = "add_time"
SERVICE_DISMISS_TIMER = "dismiss_timer"
SERVICE_RETRY_DISMISS = "retry_dismiss"
SERVICE_RESTART_TIMER = "restart_timer"
SERVICE_START_TIMER = "start_timer"
# D6 Undo: put a cancelled / dismissed timer back with its remaining
# time (restore_timer) or its finished record (restore_finished_timer).
# The card calls these from the Undo toast; they are the only way to
# restore a timer the TimerManager no longer holds.
SERVICE_RESTORE_TIMER = "restore_timer"
SERVICE_RESTORE_FINISHED_TIMER = "restore_finished_timer"

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
