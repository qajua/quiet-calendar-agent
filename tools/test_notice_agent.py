#!/usr/bin/env python3

import unittest

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


if __name__ == "__main__":
    unittest.main()
