#!/usr/bin/env python3

import json
import io
import unittest
import urllib.error
from unittest import mock

from planner import _safe_http_error, openai_plan, plan_notice, ready_timestamps, validate_plan


READY = {
    "status": "ready",
    "operation": "reschedule",
    "title": "项目周会",
    "original_start_iso": "2026-09-23T15:00:00+10:00",
    "new_start_iso": "2026-09-25T16:00:00+10:00",
    "confirmation_question": None,
    "reasoning_summary": "标题和两个时间都明确。",
}


class FakeResponse:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return json.dumps(self.body, ensure_ascii=False).encode()


class PlannerTest(unittest.TestCase):
    @mock.patch("planner.urllib.request.urlopen")
    def test_openai_uses_strict_schema_and_validates_output(self, urlopen):
        urlopen.return_value = FakeResponse({
            "status": "completed",
            "output": [{
                "type": "message",
                "content": [{"type": "output_text", "text": json.dumps(READY)}],
            }],
        })

        plan = openai_plan(
            "把周三的项目周会改到周五下午四点",
            "Australia/Sydney",
            "2026-09-21T10:00:00+10:00",
            "test-model",
            "test-key",
        )

        self.assertEqual(plan, READY)
        request = urlopen.call_args.args[0]
        payload = json.loads(request.data)
        self.assertTrue(payload["text"]["format"]["strict"])
        self.assertFalse(payload["store"])
        self.assertEqual(payload["reasoning"]["effort"], "minimal")
        self.assertEqual(payload["max_output_tokens"], 4096)
        self.assertEqual(payload["text"]["format"]["schema"]["additionalProperties"], False)
        self.assertNotIn("test-key", request.data.decode())

    @mock.patch("planner.urllib.request.urlopen")
    def test_incomplete_response_reports_safe_reason(self, urlopen):
        urlopen.return_value = FakeResponse({
            "status": "incomplete",
            "incomplete_details": {"reason": "max_output_tokens"},
            "usage": {"output_tokens": 800},
            "output": [],
        })
        with self.assertRaisesRegex(
            RuntimeError, "incomplete.*reason=max_output_tokens.*output_tokens=800"
        ):
            openai_plan(
                "把周三的项目周会改到周五下午四点",
                "Australia/Sydney",
                "2026-09-21T10:00:00+10:00",
                "test-model",
                "test-key",
            )

    def test_needs_confirmation_requires_one_question(self):
        plan = dict(READY)
        plan.update({
            "status": "needs_confirmation",
            "operation": None,
            "title": None,
            "original_start_iso": None,
            "new_start_iso": None,
            "confirmation_question": "你指的是哪一个项目周会？",
        })
        self.assertEqual(validate_plan(plan)["status"], "needs_confirmation")
        plan["confirmation_question"] = None
        with self.assertRaises(ValueError):
            validate_plan(plan)

    def test_ready_rejects_time_without_offset(self):
        plan = dict(READY, new_start_iso="2026-09-25T16:00:00")
        with self.assertRaises(ValueError):
            ready_timestamps(plan)

    def test_auto_without_credentials_uses_deterministic_parser(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            plan, source = plan_notice(
                "把项目周会从 2026-09-23 15:00 改到 2026-09-25 16:00",
                "Australia/Sydney",
                "2026-09-21T10:00:00+10:00",
            )
        self.assertEqual(source, "deterministic")
        self.assertEqual(plan["status"], "ready")

    def test_http_error_does_not_echo_key_fragment(self):
        error = urllib.error.HTTPError(
            "https://api.openai.com/v1/responses", 401, "Unauthorized", {},
            io.BytesIO(json.dumps({
                "error": {
                    "message": "Incorrect API key provided: sk-proj-secret-fragment",
                    "type": "invalid_request_error",
                    "code": "invalid_api_key",
                }
            }).encode()),
        )
        message = _safe_http_error(error)
        self.assertIn("HTTP 401", message)
        self.assertNotIn("secret-fragment", message)


if __name__ == "__main__":
    unittest.main()
