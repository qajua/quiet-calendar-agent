#!/usr/bin/env python3

import unittest

from e2e_demo import summarize_samples, verify_execution


class E2EDemoTest(unittest.TestCase):
    def test_sample_summary_detects_agent_foreground(self):
        summary = summarize_samples([
            {"foreground_component": "com.example.chat/.Main", "keyboard_shown": True},
            {"foreground_component": "dev.qajua.quietcalendar/.MainActivity", "keyboard_shown": False},
        ])
        self.assertTrue(summary["agent_became_foreground"])
        self.assertEqual(summary["keyboard_visible_samples"], 1)

    def test_execution_verification_accepts_exact_readback(self):
        report = {
            "state": "complete", "event_id": 18, "calendar_id": 1,
            "owner_task_id": "seed-1", "title": "项目周会",
        }
        after = {
            "_id": "18", "calendar_id": "1", "title": "项目周会",
            "dtstart": "2000", "dtend": "3000",
            "description": "[Quiet Calendar Agent task:seed-1]",
        }
        verify_execution(report, after, 2000, 3000)

    def test_execution_verification_rejects_mismatch(self):
        report = {
            "state": "complete", "event_id": 18, "calendar_id": 1,
            "owner_task_id": "seed-1", "title": "项目周会",
        }
        after = {
            "_id": "18", "calendar_id": "1", "title": "项目周会",
            "dtstart": "9999", "dtend": "3000",
            "description": "[Quiet Calendar Agent task:seed-1]",
        }
        with self.assertRaises(RuntimeError):
            verify_execution(report, after, 2000, 3000)


if __name__ == "__main__":
    unittest.main()
