#!/usr/bin/env python3

import unittest

from risk_policy import classify_request


class RiskPolicyTest(unittest.TestCase):
    def test_calendar_mutation_requires_confirmation(self):
        result = classify_request("项目群", "把本周三的项目周会改到本周五")
        self.assertEqual(result["risk"], "medium")
        self.assertEqual(result["decision"], "needs_confirmation")
        self.assertTrue(result["supported"])
        self.assertEqual(result["executor"], "notice")

    def test_routes_smart_and_batch_requests(self):
        smart = classify_request("项目群", "把项目周会改到四点，如果冲突找最近空档并提醒")
        batch = classify_request("项目群", "把项目周会改到四点，会后创建会议纪要日程")
        self.assertEqual(smart["executor"], "smart")
        self.assertEqual(batch["executor"], "batch")

    def test_high_risk_overrides_calendar_match(self):
        result = classify_request("项目群", "取消会议并发送消息通知参会人")
        self.assertEqual(result["risk"], "high")
        self.assertEqual(result["decision"], "blocked")

    def test_unrelated_notification_is_ignored(self):
        result = classify_request("快递", "包裹已放入驿站")
        self.assertEqual(result["decision"], "ignored")


if __name__ == "__main__":
    unittest.main()
