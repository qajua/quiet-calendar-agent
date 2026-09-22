#!/usr/bin/env python3

import unittest

from smart_planner import smart_timestamps, validate_smart_plan


READY = {
    "status": "ready",
    "operation": "smart_reschedule",
    "title": "项目周会",
    "original_start_iso": "2026-09-23T15:00:00+10:00",
    "requested_start_iso": "2026-09-25T16:00:00+10:00",
    "conflict_policy": "next_available",
    "search_window_end_iso": "2026-09-25T18:00:00+10:00",
    "reminder_minutes": 20,
    "confirmation_question": None,
    "reasoning_summary": "目标时间明确；冲突时使用之后最近空档。",
}


class SmartPlannerTest(unittest.TestCase):
    def test_valid_ready_plan(self):
        self.assertEqual(validate_smart_plan(dict(READY))["reminder_minutes"], 20)
        self.assertEqual(smart_timestamps(dict(READY))[0], "项目周会")

    def test_rejects_window_before_requested_time(self):
        plan = dict(READY, search_window_end_iso="2026-09-25T15:00:00+10:00")
        with self.assertRaises(ValueError):
            validate_smart_plan(plan)

    def test_needs_confirmation_requires_question(self):
        plan = dict(READY, status="needs_confirmation", confirmation_question=None)
        with self.assertRaises(ValueError):
            validate_smart_plan(plan)


if __name__ == "__main__":
    unittest.main()
