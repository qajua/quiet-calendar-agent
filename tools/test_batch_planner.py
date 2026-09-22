#!/usr/bin/env python3

import unittest

from batch_planner import batch_timestamps, validate_batch_plan


READY = {
    "status": "ready",
    "operation": "compound_schedule",
    "title": "客户复盘",
    "original_start_iso": "2026-09-24T14:00:00+10:00",
    "requested_start_iso": "2026-09-25T16:00:00+10:00",
    "conflict_policy": "next_available",
    "search_window_end_iso": "2026-09-25T18:00:00+10:00",
    "reminder_minutes": 20,
    "followup_title": "整理会议纪要",
    "followup_duration_minutes": 30,
    "followup_offset_minutes": 0,
    "confirmation_question": None,
    "reasoning_summary": "改期后紧接着创建会后任务。",
}


class BatchPlannerTest(unittest.TestCase):
    def test_valid_compound_plan(self):
        self.assertEqual(validate_batch_plan(dict(READY))["followup_duration_minutes"], 30)
        self.assertEqual(batch_timestamps(dict(READY))[0], "客户复盘")

    def test_rejects_non_contiguous_followup(self):
        with self.assertRaises(ValueError):
            validate_batch_plan(dict(READY, followup_offset_minutes=10))

    def test_rejects_missing_confirmation_question(self):
        with self.assertRaises(ValueError):
            validate_batch_plan(dict(READY, status="needs_confirmation", confirmation_question=None))


if __name__ == "__main__":
    unittest.main()
