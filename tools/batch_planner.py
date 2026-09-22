#!/usr/bin/env python3
"""Strict LLM plan for a reschedule + reminder + follow-up compound task."""

import datetime as dt
import json
import os
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from planner import _output_text, _parse_iso, _safe_http_error


BATCH_PLAN_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "status": {"type": "string", "enum": ["ready", "needs_confirmation", "unsupported"]},
        "operation": {"type": ["string", "null"], "enum": ["compound_schedule", None]},
        "title": {"type": ["string", "null"]},
        "original_start_iso": {"type": ["string", "null"]},
        "requested_start_iso": {"type": ["string", "null"]},
        "conflict_policy": {"type": ["string", "null"], "enum": ["reject", "next_available", None]},
        "search_window_end_iso": {"type": ["string", "null"]},
        "reminder_minutes": {"type": ["integer", "null"], "minimum": 0, "maximum": 10080},
        "followup_title": {"type": ["string", "null"]},
        "followup_duration_minutes": {"type": ["integer", "null"], "minimum": 1, "maximum": 1440},
        "followup_offset_minutes": {"type": ["integer", "null"], "enum": [0, None]},
        "confirmation_question": {"type": ["string", "null"]},
        "reasoning_summary": {"type": "string"},
    },
    "required": [
        "status", "operation", "title", "original_start_iso", "requested_start_iso",
        "conflict_policy", "search_window_end_iso", "reminder_minutes", "followup_title",
        "followup_duration_minutes", "followup_offset_minutes", "confirmation_question",
        "reasoning_summary",
    ],
}

BATCH_INSTRUCTIONS = """你是后台日历批量任务规划器，只解析意图，绝不声称已经执行。
当前只支持一个固定但多步骤的事务：改期已有会议、可添加提前提醒、并紧接会议结束创建一个会后跟进事件。

- reference_time_iso 与 timezone 是权威时间基准；自然周从周一开始。
- 用户说冲突时安排到“之后最近空档”：conflict_policy=next_available，搜索截止时间默认为目标日期18:00。
- 没有授权自动避让：conflict_policy=reject。
- followup_offset_minutes 当前必须为0，即跟进任务紧接会议结束。
- 标题、时间、冲突策略、提醒、跟进标题或跟进时长不明确时返回 needs_confirmation，只问一个最关键问题。
- ready 时间必须是带正确UTC偏移的ISO 8601。
- 不是“改期并创建会后任务”的请求时返回 unsupported。
"""


def validate_batch_plan(plan):
    if not isinstance(plan, dict) or set(plan) != set(BATCH_PLAN_SCHEMA["required"]):
        raise ValueError("批量规划结果字段不符合约定")
    status = plan.get("status")
    if status not in {"ready", "needs_confirmation", "unsupported"}:
        raise ValueError("批量规划状态无效")
    if not isinstance(plan.get("reasoning_summary"), str) or not plan["reasoning_summary"].strip():
        raise ValueError("批量规划缺少说明")
    if status == "needs_confirmation":
        if not isinstance(plan.get("confirmation_question"), str) or not plan["confirmation_question"].strip():
            raise ValueError("需要确认时必须给出问题")
        return plan
    if status == "unsupported":
        return plan
    if plan.get("operation") != "compound_schedule":
        raise ValueError("当前只支持 compound_schedule")
    for field in ("title", "followup_title"):
        value = plan.get(field)
        if not isinstance(value, str) or not 1 <= len(value.strip()) <= 200:
            raise ValueError(f"{field} 必须为 1–200 个字符")
    old = _parse_iso(plan.get("original_start_iso"), "original_start_iso")
    requested = _parse_iso(plan.get("requested_start_iso"), "requested_start_iso")
    window_end = _parse_iso(plan.get("search_window_end_iso"), "search_window_end_iso")
    if old == requested or window_end <= requested:
        raise ValueError("原时间、目标时间或搜索截止时间无效")
    if plan.get("conflict_policy") not in {"reject", "next_available"}:
        raise ValueError("冲突策略无效")
    reminder = plan.get("reminder_minutes")
    if reminder is not None and (isinstance(reminder, bool) or not isinstance(reminder, int) or not 0 <= reminder <= 10080):
        raise ValueError("提醒时间无效")
    duration = plan.get("followup_duration_minutes")
    if isinstance(duration, bool) or not isinstance(duration, int) or not 1 <= duration <= 1440:
        raise ValueError("跟进任务时长无效")
    if plan.get("followup_offset_minutes") != 0:
        raise ValueError("当前跟进任务必须紧接会议结束")
    if plan.get("confirmation_question") is not None:
        raise ValueError("ready 规划不应包含确认问题")
    return plan


def batch_timestamps(plan):
    validate_batch_plan(plan)
    if plan["status"] != "ready":
        raise ValueError("批量规划尚未 ready")
    return (
        plan["title"].strip(),
        int(_parse_iso(plan["original_start_iso"], "original_start_iso").timestamp() * 1000),
        int(_parse_iso(plan["requested_start_iso"], "requested_start_iso").timestamp() * 1000),
        int(_parse_iso(plan["search_window_end_iso"], "search_window_end_iso").timestamp() * 1000),
    )


def openai_batch_plan(text, timezone, reference_time, model=None, api_key=None, timeout=30):
    api_key = api_key if api_key is not None else os.environ.get("OPENAI_API_KEY")
    model = model or os.environ.get("OPENAI_MODEL")
    if not api_key:
        raise ValueError("缺少 OPENAI_API_KEY")
    if not model:
        raise ValueError("缺少 OPENAI_MODEL 或 --model")
    try:
        zone = ZoneInfo(timezone)
    except ZoneInfoNotFoundError as error:
        raise ValueError(f"未知时区：{timezone}") from error
    reference = _parse_iso(reference_time, "reference_time") if reference_time else dt.datetime.now(zone)
    payload = {
        "model": model,
        "store": False,
        "max_output_tokens": 4096,
        "reasoning": {"effort": "minimal"},
        "instructions": BATCH_INSTRUCTIONS,
        "input": json.dumps({
            "user_notice": text, "reference_time_iso": reference.isoformat(), "timezone": timezone,
        }, ensure_ascii=False),
        "text": {"format": {
            "type": "json_schema", "name": "calendar_batch_plan", "strict": True,
            "schema": BATCH_PLAN_SCHEMA,
        }},
    }
    request = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(payload, ensure_ascii=False).encode(),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as result:
            response = json.loads(result.read().decode())
    except urllib.error.HTTPError as error:
        raise RuntimeError(_safe_http_error(error)) from error
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
        raise RuntimeError(f"OpenAI API 请求失败：{error}") from error
    if response.get("status") != "completed":
        reason = (response.get("incomplete_details") or {}).get("reason", response.get("status", "unknown"))
        raise RuntimeError(f"模型响应未完成：{reason}")
    try:
        return validate_batch_plan(json.loads(_output_text(response)))
    except json.JSONDecodeError as error:
        raise RuntimeError("模型输出不是有效 JSON") from error
