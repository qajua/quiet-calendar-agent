#!/usr/bin/env python3

import tempfile
from pathlib import Path
import unittest

from control_center import ControlState, PAGE


class ControlStateTest(unittest.TestCase):
    def test_dashboard_preserves_task_output_scroll_position(self):
        self.assertIn("const outputScrollPositions=new Map()", PAGE)
        self.assertIn("box.dataset.taskId=t.id", PAGE)
        self.assertIn("captureOutputScroll(root);root.replaceChildren()", PAGE)
        self.assertIn("requestAnimationFrame(()=>restoreOutputScroll(root))", PAGE)

    def test_ingest_classifies_and_deduplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            state = ControlState(Path(directory) / "state.json")
            notification = {
                "id": "abc123",
                "package": "com.android.shell",
                "posted_at_ms": 123,
                "title": "项目群",
                "text": "把本周三的项目周会改到本周五",
            }
            self.assertEqual(state.ingest([notification]), 1)
            self.assertEqual(state.ingest([notification]), 0)
            task = state.snapshot()["tasks"][0]
            self.assertEqual(task["status"], "needs_confirmation")
            self.assertEqual(task["risk"], "medium")
            self.assertEqual(task["executor"], "notice")

    def test_blocked_task_cannot_be_approved(self):
        with tempfile.TemporaryDirectory() as directory:
            state = ControlState(Path(directory) / "state.json")
            state.ingest([{
                "id": "danger", "package": "com.android.shell", "posted_at_ms": 123,
                "title": "项目群", "text": "取消会议并发送消息通知参会人",
            }])
            with self.assertRaises(ValueError):
                state.transition("notify-danger", {"needs_confirmation"}, "executing")


if __name__ == "__main__":
    unittest.main()
