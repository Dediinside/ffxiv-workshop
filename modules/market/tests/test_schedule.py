"""Timed-node calendar boundaries; no live market or machine timezone dependence.

Independent clock reference: Teamcraft EorzeanTimeService maps Unix epoch by
3600/175, i.e. an ET day lasts 4200 real seconds.
https://github.com/ffxiv-teamcraft/ffxiv-teamcraft/blob/master/apps/client/src/app/core/eorzea/eorzean-time.service.ts
"""
import unittest

from marketscout.analysis import hhmm, next_window, schedule_text


def source(*windows, kind="timed"):
    return {"kind": kind, "windows": [
        {"start_hour": start, "duration_hours": duration}
        for start, duration in windows
    ]}


class ScheduleTests(unittest.TestCase):
    def test_epoch_maps_to_et_midnight(self):
        self.assertEqual(next_window(source((1, 2)), now=0),
                         {"wait": 175.0, "start": 175.0, "end": 525.0, "active": False})

    def test_real_day_repeats_after_seventy_minutes(self):
        first = next_window(source((8, 2)), now=100)
        later = next_window(source((8, 2)), now=100 + 4200)
        self.assertEqual(later["wait"], first["wait"])
        self.assertEqual(later["start"] - first["start"], 4200)

    def test_start_is_inclusive(self):
        window = next_window(source((8, 2)), now=1400)
        self.assertTrue(window["active"])
        self.assertEqual(window["wait"], 0)
        self.assertEqual(window["end"], 1750)

    def test_end_is_exclusive(self):
        window = next_window(source((8, 2)), now=1750)
        self.assertFalse(window["active"])
        self.assertEqual(window["start"], 5600)
        self.assertEqual(window["wait"], 3850)

    def test_cross_midnight_window_uses_previous_day(self):
        # At day 1, 00:30 ET, yesterday's 23:00-01:00 ET window is open.
        window = next_window(source((23, 2)), now=4200 + 87.5)
        self.assertTrue(window["active"])
        self.assertEqual(window["start"], 4025)
        self.assertEqual(window["end"], 4375)

    def test_cross_midnight_end_advances_to_next_start(self):
        window = next_window(source((23, 2)), now=4375)
        self.assertFalse(window["active"])
        self.assertEqual(window["start"], 8225)

    def test_fractional_hours_retain_minute_precision(self):
        window = next_window(source((8.5, 0.5)), now=0)
        self.assertEqual(window["start"], 1487.5)
        self.assertEqual(window["end"], 1575)
        self.assertIn("08:30–09:00 ET", schedule_text(source((8.5, 0.5)), now=0))

    def test_unsorted_multiple_windows_pick_nearest_opening(self):
        window = next_window(source((20, 2), (8, 2), (13, 2)), now=1750)
        self.assertEqual(window["start"], 2275)
        self.assertEqual(window["wait"], 525)

    def test_active_window_wins_over_future_window(self):
        window = next_window(source((20, 2), (8, 2)), now=1500)
        self.assertTrue(window["active"])
        self.assertEqual(window["start"], 1400)

    def test_invalid_intervals_are_not_calendar_events(self):
        self.assertIsNone(next_window(source((24, 1), (-1, 1), (8, 0), (8, 25)), now=0))

    def test_unknown_source_is_not_permanently_available(self):
        unknown = {"kind": "unknown", "windows": []}
        self.assertIsNone(next_window(unknown, now=100))
        self.assertIn("неизвестны", schedule_text(unknown, now=100))
        self.assertIn("постоянная доступность не предполагается", schedule_text(unknown, now=100))

    def test_always_available_source_has_no_expiry(self):
        self.assertEqual(next_window({"kind": "always"}, now=123),
                         {"wait": 0.0, "start": 123, "end": None, "active": True})

    def test_ephemeral_calendar_and_active_label(self):
        ephemeral = source((20, 4), kind="ephemeral")
        self.assertIn("ephemeral", schedule_text(ephemeral, now=4000))
        self.assertIn("открыто сейчас", schedule_text(ephemeral, now=4000))
        self.assertIn("20:00–00:00 ET", schedule_text(ephemeral, now=4000))

    def test_clock_display_wraps_end_at_midnight(self):
        self.assertEqual(hhmm(24), "00:00")
        self.assertEqual(hhmm(25.5), "01:30")


if __name__ == "__main__":
    unittest.main()
