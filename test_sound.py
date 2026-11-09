"""Standalone test for sound.py (DESIGN-5, Phase 7).

sound.py is pure (no Home Assistant imports) but uses a relative
import of const.py, so both files are loaded as a synthetic package
— the same approach as test_sensor_device_info.py.

Run: python3 -m unittest test_sound
"""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from datetime import datetime, time as dt_time, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
COMPONENTS = HERE / "custom_components" / "satellite_timers"

pkg = types.ModuleType("satellite_timers")
pkg.__path__ = [str(COMPONENTS)]
sys.modules["satellite_timers"] = pkg


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        f"satellite_timers.{name}", COMPONENTS / f"{name}.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"satellite_timers.{name}"] = mod
    spec.loader.exec_module(mod)
    return mod


const = _load("const")
sound = _load("sound")


def at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, 8, hour, minute, tzinfo=timezone.utc)


class TestClampVolume(unittest.TestCase):
    def test_bounds(self):
        self.assertEqual(sound.clamp_volume_pct(120), 100)
        self.assertEqual(sound.clamp_volume_pct(-5), 0)
        self.assertEqual(sound.clamp_volume_pct(0), 0)
        self.assertEqual(sound.clamp_volume_pct(100), 100)

    def test_coercion_and_garbage(self):
        self.assertEqual(sound.clamp_volume_pct("80"), 80)
        self.assertEqual(sound.clamp_volume_pct(55.6), 55)
        self.assertEqual(sound.clamp_volume_pct("x"), 100)
        self.assertEqual(sound.clamp_volume_pct(None), 100)
        self.assertEqual(sound.clamp_volume_pct(float("nan")), 100)
        self.assertEqual(sound.clamp_volume_pct("x", default=30), 30)


class TestParseTime(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(sound.parse_time_hhmm("22:00", "07:00"), dt_time(22, 0))
        self.assertEqual(sound.parse_time_hhmm("7:05", "07:00"), dt_time(7, 5))
        self.assertEqual(sound.parse_time_hhmm("00:00", "07:00"), dt_time(0, 0))

    def test_garbage_falls_back_to_default(self):
        self.assertEqual(sound.parse_time_hhmm("24:00", "07:00"), dt_time(7, 0))
        self.assertEqual(sound.parse_time_hhmm("22:60", "07:00"), dt_time(7, 0))
        self.assertEqual(sound.parse_time_hhmm("x", "07:00"), dt_time(7, 0))
        self.assertEqual(sound.parse_time_hhmm(None, "07:00"), dt_time(7, 0))
        self.assertEqual(sound.parse_time_hhmm("22:00:00", "07:00"), dt_time(7, 0))

    def test_time_object_passes_through(self):
        self.assertEqual(
            sound.parse_time_hhmm(dt_time(22, 0), "07:00"), dt_time(22, 0)
        )

    def test_is_valid_hhmm(self):
        self.assertTrue(sound.is_valid_hhmm("00:00"))
        self.assertTrue(sound.is_valid_hhmm("23:59"))
        self.assertFalse(sound.is_valid_hhmm("24:00"))
        self.assertFalse(sound.is_valid_hhmm("22:60"))
        self.assertFalse(sound.is_valid_hhmm("x"))
        self.assertFalse(sound.is_valid_hhmm(""))
        self.assertFalse(sound.is_valid_hhmm(None))


class TestQuietHours(unittest.TestCase):
    def test_overnight_window(self):
        # Defaults: 22:00-07:00.
        self.assertTrue(sound.is_quiet_hours(at(23), "22:00", "07:00"))
        self.assertTrue(sound.is_quiet_hours(at(3), "22:00", "07:00"))
        self.assertTrue(sound.is_quiet_hours(at(22), "22:00", "07:00"))
        self.assertTrue(sound.is_quiet_hours(at(6, 59), "22:00", "07:00"))
        self.assertFalse(sound.is_quiet_hours(at(7), "22:00", "07:00"))
        self.assertFalse(sound.is_quiet_hours(at(12), "22:00", "07:00"))
        self.assertFalse(sound.is_quiet_hours(at(21, 59), "22:00", "07:00"))

    def test_daytime_window(self):
        self.assertTrue(sound.is_quiet_hours(at(12), "09:00", "17:00"))
        self.assertTrue(sound.is_quiet_hours(at(9), "09:00", "17:00"))
        self.assertFalse(sound.is_quiet_hours(at(17), "09:00", "17:00"))
        self.assertFalse(sound.is_quiet_hours(at(8, 59), "09:00", "17:00"))

    def test_equal_start_end_is_no_quiet_hours(self):
        self.assertFalse(sound.is_quiet_hours(at(12), "09:00", "09:00"))
        self.assertFalse(sound.is_quiet_hours(at(9), "09:00", "09:00"))


class TestTimerVolume(unittest.TestCase):
    def test_daytime_is_full_volume(self):
        self.assertEqual(sound.timer_volume_pct(at(12)), 100)

    def test_quiet_hours_is_30_percent_of_timer_volume(self):
        # Phase 7 DECISION: quiet volume is 30% of the timer volume.
        self.assertEqual(sound.timer_volume_pct(at(23)), 30)
        self.assertEqual(
            sound.timer_volume_pct(at(23), {const.CONF_TIMER_VOLUME: 80}), 24
        )

    def test_custom_quiet_fraction(self):
        self.assertEqual(
            sound.timer_volume_pct(
                at(23),
                {
                    const.CONF_TIMER_VOLUME: 100,
                    const.CONF_QUIET_HOURS_VOLUME_PCT: 50,
                },
            ),
            50,
        )

    def test_custom_window(self):
        options = {
            const.CONF_QUIET_HOURS_START: "20:00",
            const.CONF_QUIET_HOURS_END: "06:00",
        }
        self.assertEqual(sound.timer_volume_pct(at(21), options), 30)
        self.assertEqual(sound.timer_volume_pct(at(12), options), 100)

    def test_garbage_options_fall_back_to_defaults(self):
        options = {
            const.CONF_TIMER_VOLUME: "loud",
            const.CONF_QUIET_HOURS_START: "soon",
            const.CONF_QUIET_HOURS_END: "later",
        }
        self.assertEqual(sound.timer_volume_pct(at(12), options), 100)
        self.assertEqual(sound.timer_volume_pct(at(23), options), 30)


class TestEscalation(unittest.TestCase):
    def test_ramp(self):
        # Phase 7 DECISION: 40% at once, +20 points every 10s, full in 30s.
        self.assertEqual(sound.escalation_volume_pct(100, 0), 40)
        self.assertEqual(sound.escalation_volume_pct(100, 5), 40)
        self.assertEqual(sound.escalation_volume_pct(100, 10), 60)
        self.assertEqual(sound.escalation_volume_pct(100, 20), 80)
        self.assertEqual(sound.escalation_volume_pct(100, 29), 80)
        self.assertEqual(sound.escalation_volume_pct(100, 30), 100)
        self.assertEqual(sound.escalation_volume_pct(100, 120), 100)

    def test_ramp_scales_with_target(self):
        self.assertEqual(sound.escalation_volume_pct(50, 0), 20)
        self.assertEqual(sound.escalation_volume_pct(50, 10), 30)
        self.assertEqual(sound.escalation_volume_pct(50, 30), 50)

    def test_disabled_escalation_is_full_at_once(self):
        options = {const.CONF_ESCALATION: False}
        self.assertEqual(sound.escalation_volume_pct(100, 0, options), 100)
        self.assertEqual(sound.escalation_volume_pct(30, 0, options), 30)

    def test_negative_elapsed_is_start_level(self):
        self.assertEqual(sound.escalation_volume_pct(100, -5), 40)


class TestCurrentVolume(unittest.TestCase):
    def test_daytime_finished_timer_ramps_from_40(self):
        finished = at(12)
        self.assertEqual(
            sound.current_sound_volume_pct(at(12), finished), 40
        )
        self.assertEqual(
            sound.current_sound_volume_pct(
                datetime(2026, 11, 8, 12, 0, 15, tzinfo=timezone.utc), finished
            ),
            60,
        )
        self.assertEqual(
            sound.current_sound_volume_pct(
                datetime(2026, 11, 8, 12, 0, 35, tzinfo=timezone.utc), finished
            ),
            100,
        )

    def test_quiet_hours_finished_timer_ramps_from_12(self):
        # 30% target, 40% of that at once = 12.
        finished = at(23)
        self.assertEqual(
            sound.current_sound_volume_pct(at(23), finished), 12
        )
        self.assertEqual(
            sound.current_sound_volume_pct(
                datetime(2026, 11, 8, 23, 0, 35, tzinfo=timezone.utc), finished
            ),
            30,
        )

    def test_no_finish_time_is_the_target(self):
        self.assertEqual(sound.current_sound_volume_pct(at(12), None), 100)
        self.assertEqual(sound.current_sound_volume_pct(at(23), None), 30)


class TestSoundOptions(unittest.TestCase):
    def test_defaults_for_empty_options(self):
        opts = sound.sound_options({})
        self.assertEqual(opts[const.CONF_TIMER_VOLUME], 100)
        self.assertEqual(opts[const.CONF_QUIET_HOURS_START], dt_time(22, 0))
        self.assertEqual(opts[const.CONF_QUIET_HOURS_END], dt_time(7, 0))
        self.assertEqual(opts[const.CONF_QUIET_HOURS_VOLUME_PCT], 30)
        self.assertTrue(opts[const.CONF_ESCALATION])

    def test_none_is_defaults(self):
        self.assertEqual(sound.sound_options(None)[const.CONF_TIMER_VOLUME], 100)


if __name__ == "__main__":
    unittest.main()
