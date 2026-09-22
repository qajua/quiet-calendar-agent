#!/usr/bin/env python3

from types import SimpleNamespace
import unittest
from unittest import mock

import batch_agent


class BatchAgentTest(unittest.TestCase):
    @mock.patch("batch_agent.send_report")
    def test_compensation_runs_in_reverse_order(self, send_report):
        send_report.side_effect = [
            {"state": "deleted"},
            {"state": "undone"},
        ]
        steps = [
            {"type": "move", "task_id": "batch-1-move"},
            {"type": "followup", "task_id": "batch-1-follow"},
        ]
        results = batch_agent.compensate(SimpleNamespace(), steps)
        self.assertEqual([item["action"] for item in results], ["cleanup", "undo"])
        self.assertEqual(send_report.call_args_list[0].args[1:3], ("cleanup", "batch-1-follow"))
        self.assertEqual(send_report.call_args_list[1].args[1:3], ("undo", "batch-1-move"))

    def test_compensation_requires_expected_states(self):
        self.assertTrue(batch_agent.compensation_succeeded([
            {"action": "cleanup", "report": {"state": "deleted"}},
            {"action": "undo", "report": {"state": "undone"}},
        ]))
        self.assertFalse(batch_agent.compensation_succeeded([
            {"action": "cleanup", "report": {"state": "failed"}},
        ]))
        self.assertFalse(batch_agent.compensation_succeeded([
            {"action": "undo", "error": "offline"},
        ]))


if __name__ == "__main__":
    unittest.main()
