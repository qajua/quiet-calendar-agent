#!/usr/bin/env python3

import unittest

from smart_schedule import BusyEvent, choose_slot, overlaps


class SmartScheduleTest(unittest.TestCase):
    def test_touching_events_do_not_overlap(self):
        self.assertFalse(overlaps(100, 200, 200, 300))

    def test_next_available_skips_chained_conflicts(self):
        busy = [
            BusyEvent(1, "A", 100, 160),
            BusyEvent(2, "B", 150, 230),
            BusyEvent(3, "C", 260, 300),
        ]
        chosen, encountered = choose_slot(120, 60, busy, 400, "next_available")
        self.assertEqual(chosen, 300)
        self.assertEqual([event.event_id for event in encountered], [1, 2, 3])

    def test_reject_policy_fails_on_conflict(self):
        with self.assertRaises(RuntimeError):
            choose_slot(100, 30, [BusyEvent(1, "A", 110, 120)], 200, "reject")

    def test_fails_when_window_has_no_slot(self):
        with self.assertRaises(RuntimeError):
            choose_slot(100, 60, [BusyEvent(1, "A", 100, 180)], 200, "next_available")


if __name__ == "__main__":
    unittest.main()
