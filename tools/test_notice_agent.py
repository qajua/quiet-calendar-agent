#!/usr/bin/env python3

import contextlib
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

import notice_agent
from notice_agent import parse_notice


class ParseNoticeTest(unittest.TestCase):
    def test_parses_absolute_chinese_reschedule(self):
        title, old_start, new_start = parse_notice(
            "把项目周会从 2026-09-23 15:00 改到 2026-09-25 16:00",
            "Australia/Sydney",
        )
        self.assertEqual(title, "项目周会")
        self.assertEqual(old_start, 1_790_139_600_000)
        self.assertEqual(new_start, 1_790_316_000_000)

    def test_strips_title_quotes(self):
        title, _, _ = parse_notice(
            "请将“项目周会”从 2026-09-23 15:00 调整到 2026-09-25 16:00。",
            "Australia/Sydney",
        )
        self.assertEqual(title, "项目周会")

    def test_rejects_relative_or_ambiguous_time(self):
        with self.assertRaises(ValueError):
            parse_notice("把项目周会从明天下午三点改到周五四点", "Australia/Sydney")

    def test_plan_only_stops_before_adb(self):
        argv = [
            "notice_agent.py", "--calendar-id", "1", "--task-id", "plan-only-test",
            "--planner", "deterministic", "--plan-only",
            "--text", "把项目周会从 2026-09-23 15:00 改到 2026-09-25 16:00",
        ]
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(sys, "argv", argv), \
                mock.patch.object(notice_agent, "PROOF_DIR", Path(directory)), \
                mock.patch.object(notice_agent, "read_status", side_effect=AssertionError("ADB called")), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(notice_agent.main(), 0)


if __name__ == "__main__":
    unittest.main()
