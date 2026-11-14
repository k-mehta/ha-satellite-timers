"""Constants for the Satellite Timers integration.

From-scratch build, Wave W2, against the settled architecture
(``architecture-design-settled.md``).

Every numeric constant names its source in its comment (Nov 13
no-magic-numbers rule). No constant here is a guess: where the settled
architecture leaves a value to implementation design time, the comment
says so and states why this value was chosen.
"""

DOMAIN = "satellite_timers"

# --- Config entry -----------------------------------------------------------
# The primary satellite is a config-entry OPTION (settled §08 row 17,
# §04.6). "" in the options flow means unset.
CONF_PRIMARY_SATELLITE = "primary_satellite"
PRIMARY_SATELLITE_UNSET = ""

# --- Services (§09 C1 service table) ---------------------------------------
SERVICE_START_TIMER = "start_timer"
SERVICE_PAUSE_TIMER = "pause_timer"
SERVICE_RESUME_TIMER = "resume_timer"
SERVICE_CANCEL_TIMER = "cancel_timer"
SERVICE_DISMISS_TIMER = "dismiss_timer"
SERVICE_ACKNOWLEDGE_TIMER = "acknowledge_timer"
SERVICE_RESTART_TIMER = "restart_timer"
SERVICE_ADD_TIME = "add_time"
SERVICE_UNDO_REMOVE = "undo_remove"
SERVICE_RING_STARTED = "ring_started"

# --- Store (§08 row 1) -----------------------------------------------------
# Versioned JSON, atomic write (HA Store). Version 2 is this build's
# schema; a payload whose version is not 2 is discarded on load (the
# superseded build's v1 store is never migrated: the old build is not
# a build base, §09 §(c)).
STORE_VERSION = 2
STORE_KEY = "satellite_timers"

# --- Idempotency (§03.2 M1) ------------------------------------------------
# Dedupe entries expire 600 s after completed_at.
DEDUPE_TTL_SECONDS = 600

# --- Ringing (§03.8, §09 C3) -----------------------------------------------
# 3600 s, settled Nov 13 — "like Alexa" (OQ-TB-3).
RING_WINDOW_SECONDS = 3600
# 5 s: the ring-start ack window (A15, §09 C3).
RING_ACK_TIMEOUT_SECONDS = 5

# --- Tick + reconcile (§03.13) ---------------------------------------------
TICK_INTERVAL_SECONDS = 1
RECONCILE_INTERVAL_SECONDS = 30

# --- Undo (§09 C9) ----------------------------------------------------------
UNDO_WINDOW_SECONDS = 5

# --- Duration bounds (v2 §6 A7) --------------------------------------------
# A timer runs from 1 second to 24 hours. 24 h is a design choice, not
# a measured derivation (v2 A7 says so); review may change the value,
# not the existence of the bound.
MIN_TIMER_DURATION_SECONDS = 1
MAX_TIMER_DURATION_SECONDS = 86400

# --- Error standard (ERS-1, §07.9) -----------------------------------------
# One retry ladder: 4 attempts, delays [2, 8, 30] seconds.
RETRY_MAX_ATTEMPTS = 4
RETRY_DELAYS_S = (2, 8, 30)

# --- Re-probe (§08 row 41, ERS-7) ------------------------------------------
# A persistent bridge failure re-appears via a 60 s silent re-probe.
REPROBE_INTERVAL_SECONDS = 60

# --- Persist drain cap (ERS-3) --------------------------------------------
# A burst of mutations arriving during a save is drained in at most this
# many passes; anything left after the cap is re-scheduled as a fresh
# persist task (bridge.py _persist) — the invariant is that every
# mutation is eventually persisted, not that one task does it all.
# 8 passes is far above any realistic burst: a save takes a few ms and
# mutations arrive one per service call or tick transition.
PERSIST_MAX_DRAIN_PASSES = 8

# --- Sensor contract (§03.6, §09 C2) ----------------------------------------
CONTRACT_VERSION = 2

# --- Version sensor (§03.6, §08 row 44) ------------------------------------
# BRIDGE_VERSION is read from the integration manifest at runtime
# (bridge.py), never hardcoded here.
# MIN_CARD_VERSION is an implementation choice, not a measured
# derivation: the new-build cards are v2 (W5); the 0.x cards of the
# superseded build cannot read this bridge's contract, so they
# degrade (G-08 skew matrix). Re-decided when the first v2 card ships.
MIN_CARD_VERSION = "2.0.0"

# --- Frontend plumbing (unchanged from the earlier build) ----------------
# The timer screen card is served from the integration's www/ directory
# and registered as a Lovelace resource. W2 writes no card code; these
# constants are the integration plumbing that serves the existing file.
CARD_URL_PATH = "/satellite_timers"
CARD_FILE = "timer-screen-card.js"
CARD_URL = f"{CARD_URL_PATH}/{CARD_FILE}"
CARD_RESOURCE_TYPE = "module"

# --- Announcement path (§08 row 21, §04.7) ----------------------------------
ANNOUNCE_AUTOMATION_ENTITY_ID = "automation.family_hub_timer_announcement"

# --- Repair issues -----------------------------------------------------------
ISSUE_UNDELIVERED_TIMER_EVENTS = "undelivered_timer_events"
ISSUE_VOICE_ROUTING = "bridge_voice_routing"
