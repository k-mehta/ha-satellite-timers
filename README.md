# Satellite Timers

[![HACS validation](https://github.com/k-mehta/ha-satellite-timers/actions/workflows/validate.yml/badge.svg)](https://github.com/k-mehta/ha-satellite-timers/actions/workflows/validate.yml)
[![Tests](https://github.com/k-mehta/ha-satellite-timers/actions/workflows/tests.yml/badge.svg)](https://github.com/k-mehta/ha-satellite-timers/actions/workflows/tests.yml)

Mirror Home Assistant voice-assistant timers into sensor entities, and show
them as big dials on a dashboard.

## Why this exists

When you set a timer by voice ("set a pasta timer for 10 minutes"), Home
Assistant keeps that timer in memory only. It creates no `timer.*` entity,
puts nothing on the satellite entity, fires no event, and offers no way to
list it. So no dashboard card can see it — every card that claims to show
voice timers only works with satellites that publish the timers themselves,
not with a standard voice satellite.

This integration is the missing piece: it reads the timers straight from
Home Assistant's timer system and mirrors each running timer into a sensor.
Voice behaviour does not change at all — setting, pausing, cancelling and
adding time by voice work exactly as before, because the integration only
watches. It never starts, pauses or cancels a timer unless asked through
its services (below).

## What you get

One sensor per running timer, on the satellites you pick:

- Entity id: `sensor.satellite_timer_<id>` (the id is the timer's own id, so
  two timers with the same name never collide)
- Friendly name: the name you spoke ("Pasta")
- State: `active` while it runs, `paused` while paused, `finished`
  after it goes off
- Attributes: `duration` (total seconds), `remaining` (seconds left),
  `finishes_at` (ISO timestamp while running), `finished_at` (ISO
  timestamp once finished), `timer_name`, `timer_id`, `device_id`
- The sensor appears when the timer starts. When the timer finishes it
  stays with state `finished` until it is dismissed or restarted — it
  never just vanishes when it goes off. It disappears when the timer is
  cancelled, dismissed, or restarted. While a timer runs, the remaining
  time refreshes once a second so dials tick.

The timer screen card is bundled in: on setup the integration serves its
own card file and registers it as a Lovelace resource, so there is no
separate card upload and no Resources form to fill in.

## Requirements

- Home Assistant 2025.12.0 or newer.
- At least one voice satellite with timer support (any ESPHome satellite,
  including the Open Home Foundation Linux Voice Assistant).

## Install

### Via HACS (recommended)

1. In HACS, go to Integrations → ⋮ → Custom repositories, add
   `https://github.com/k-mehta/ha-satellite-timers` as an Integration,
   then install **Satellite Timers**.
2. Restart Home Assistant.
3. Settings → Devices & services → Add integration → **Satellite Timers**.
4. Pick the voice satellite(s) whose timers should be mirrored. Only one
   entry is needed.

### Manual install

1. Download `satellite-timers.zip` from the latest
   [release](https://github.com/k-mehta/ha-satellite-timers/releases) and
   unzip it into `/config`, so the integration lands in
   `custom_components/satellite_timers/`. The timer screen card ships
   inside it at `custom_components/satellite_timers/www/`.
2. Restart Home Assistant, then follow steps 3–4 above.

On setup the integration registers the card itself: it serves
`timer-screen-card.js` from its own folder and adds the Lovelace resource
(`/satellite_timers/timer-screen-card.js`, JavaScript Module). There is no
separate card upload to `/config/www` and no Settings → Dashboards →
Resources step. The only dashboard step left is putting the card on a
view (below).

To change the satellites later: Settings → Devices & services →
Satellite Timers → Configure.

If you previously installed the card by hand (file in `/config/www` plus a
`/local/timer-screen-card.js` resource), that copy is now redundant: delete
the resource and the file once the bundled card is confirmed working.

## Controlling timers

Services, called with a timer id (the `timer_id` attribute of a bridge
sensor). They work even when several timers share a name, which the voice
intents cannot tell apart. Only timers on the satellites you picked can be
controlled; any other timer id is refused.

| Service | Arguments |
|---|---|
| `satellite_timers.pause_timer` | `timer_id` |
| `satellite_timers.resume_timer` | `timer_id` |
| `satellite_timers.cancel_timer` | `timer_id` |
| `satellite_timers.add_time` | `timer_id`, `minutes`, `seconds` |
| `satellite_timers.dismiss_timer` | `timer_id` (clears a finished timer) |
| `satellite_timers.restart_timer` | `timer_id`, optional `minutes`, `seconds` (starts the finished timer again, same duration unless an override is given) |

Voice dismissal: "dismiss the pasta timer" (or "dismiss the timer" when
only one has finished) works too. Home Assistant's built-in sentences
cannot do this — "cancel the pasta timer" looks the timer up in the
TimerManager, and a finished timer is already gone from there — so the
integration registers its own intent and writes the sentences for it to
`<config>/custom_sentences/en/satellite_timers_dismiss.yaml` on setup,
only if the file is missing (your edits are never overwritten; delete the
file to remove voice dismissal until the next restart). A running timer
is never dismissed by voice: the answer says it is still running and
points at "cancel" instead.

## Using it with dials

The timer screen card (`type: custom:timer-screen`) renders one big ring
dial per bridge sensor, landscape-first: one row of up to three timers,
then a balanced grid (at most three columns — 4 timers go 2×2, 5–6 go
2×3) so dials and buttons stay big; it stacks vertically only in
portrait. The dial itself is the primary tap target: tap it to pause a
running timer, resume a paused one, or dismiss a finished one. Cancel is
always visible below each dial, full column width; it needs two taps (the
first arms it, "Tap again to cancel", for a few seconds). Adding time is
voice-only ("add 1 minute to the pasta timer") — there is no on-screen
+1 min button. A timer in its last minute turns warm (amber ring, orange
countdown). A finished timer takes focus with a full amber ring, a big
"Done", and a Restart button that runs it again for the same duration —
dismiss it by tapping the dial ("Tap to dismiss" is written in the
ring) or by voice. Put it as the only card on a panel-mode view:
Add card → Manual → paste `type: custom:timer-screen`.

The card adapts to the device it runs on — hub kiosk, desktop, or
phone/tablet, portrait or landscape — and re-detects on resize or
rotation. The hub (1920×1200 landscape touch display) keeps the big
dials and kitchen-grade targets above; a desktop gets a denser grid
(up to four timers in a row) with smaller buttons; a phone gets
readable dials that scroll vertically instead of shrinking (at least
230 px tall in portrait) and 64 px buttons. See
[ADAPTIVE-LAYOUT.md](ADAPTIVE-LAYOUT.md) for the detection mechanism
(shared with the AnyList recipe card) and the layout per device.

The sensors also give automations something to trigger on: a sensor
turning `active` means a timer started; no `active`, `paused` or
`finished` sensors means all timers are done.

## Sound

The satellite plays its own alarm when a timer finishes — the
bridge only decides how loud it should be. Configure it in the
integration's options (Settings → Devices & Services → Satellite
Timers → Configure): timer volume (0–100, default 100), quiet hours
(default 22:00–07:00) with a quiet-hours volume (default 30% of the
timer volume), and escalation (default on: the alarm starts at 40%
of its target and rises 20 points every 10 seconds, reaching full
volume in 30 seconds). A finished timer's sensor carries the
computed level as `sound_volume_pct` (and `quiet_hours`) attributes,
for a speaker or automation that honours them. Stopping is always
available: tap the finished dial, or say "dismiss the pasta timer".

No speaker is specified or assumed by this integration — until a
speaker exists and something honours these attributes, the policy
is configuration only and live sound verification stays open.

## Troubleshooting

- **"Custom element not found: timer-screen"** after install: hard-refresh
  the dashboard (Ctrl+Shift+R). The card file is served by the
  integration, but the browser caches the old dashboard.
- **"Satellite Timers cannot read voice timers" repair issue:** a Home
  Assistant update changed how voice timers work internally. The
  integration switched itself off rather than risk breaking your voice
  timers. Check for an update to Satellite Timers.
- **A finished timer disappears after a Home Assistant restart:** expected.
  Finished timers are kept in memory only; an undismissed finished timer
  does not survive a restart, and neither does the ringing itself (the
  satellite announces it once).

## How it works

Home Assistant's intent integration keeps voice timers in a `TimerManager`
(`timers` by id, one `handler` per satellite device). This integration reads
`timers` directly and *taps* `handlers`: it swaps the dict for a subclass
that wraps every handler set into it, calls the satellite's original handler
first, then tells the integration. The satellite always hears about its
timer first, and a failure in this integration can never break it.

A finished timer needs one more piece: the TimerManager pops a timer the
moment it goes off, so the bridge keeps its own record of finished timers
until they are dismissed or restarted — without that, the sensor (and the
dial) would vanish the instant the timer rings. The record is in-memory
only: an undismissed finished timer does not survive a Home Assistant
restart, and neither does the ringing itself (the satellite is told once).
A finished timer that is ignored keeps showing on the screen until it is
dismissed; the satellite announces it once and does not repeat it.

If a Home Assistant update changes those internals, the integration checks
the shape first (`assist_supported()`), switches itself off, and raises a
repair issue explaining why — instead of guessing and breaking something.

The tap-and-read pattern comes from Second Hand
([mpjanofsky/second-hand-integration](https://github.com/mpjanofsky/second-hand-integration)),
which uses it to show Assist timers on an Echo Wall Clock. This integration
uses the same pattern to feed dashboard dials instead of a clock.

## Contributing

Work happens on feature branches; `main` stays clean. A feature is
squash-merged into `main` only after it is fully tested, and each feature
is its own commit — no "miscellaneous" commits bundling many features.
Before opening a pull request, run the smoke tests from the repository
root:

```sh
python3 test_bridge.py
python3 test_sensor_device_info.py
```

See [BEST_PRACTICES.md](BEST_PRACTICES.md) for the standards this
repository follows (manifest and HACS metadata, validation CI, secrets
hygiene).

## License

MIT — see [LICENSE](LICENSE).
