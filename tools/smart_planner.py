#!/usr/bin/env python3
"""Schema-constrained LLM planner for conflict-aware calendar rescheduling."""

import datetime as dt
import json
import os
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from planner import _output_text, _parse_iso, _safe_http_error


SMART_PLAN_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "status": {"type": "string", "enum": ["ready", "needs_confirmation", "unsupported"]},
        "operation": {"type": ["string", "null"], "enum": ["smart_reschedule", None]},
        "title": {"type": ["string", "null"]},
        "original_start_iso": {"type": ["string", "null"]},
        "requested_start_iso": {"type": ["string", "null"]},
        "conflict_policy": {"type": ["string", "null"], "enum": ["reject", "next_available", None]},
        "search_window_end_iso": {"type": ["string", "null"]},
        "reminder_minutes": {"type": ["integer", "null"], "minimum": 0, "maximum": 10080},
        "confirmation_question": {"type": ["string", "null"]},
        "reasoning_summary": {"type": "string"},
    },
    "required": [
        "status", "operation", "title", "original_start_iso", "requested_start_iso",
        "conflict_policy", "search_window_end_iso", "reminder_minutes",
        "confirmation_question", "reasoning_summary",
    ],
}

SMART_INSTRUCTIONS = """你是后台日历任务规划器，只解析意图，绝不声称已经执行。
当前只支持：把一个已有事件改期、检测目标时间冲突、按用户明确要求选择当天之后最近空档，并可设置提前提醒。

- reference_time_iso 与 timezone 是权威时间基准；自然周从周一开始。
- 标题、原时间、目标时间、冲突策略或搜索截止时间有歧义时返回 needs_confirmation，不要猜。
- 用户说“如果冲突就安排到当天最近的空档”时：conflict_policy=next_available，search_window_end_iso=目标日期当天 18:00，除非用户另给截止时间。
- 用户没有授权自动避让冲突时：conflict_policy=reject；搜索截止时间仍设为目标日期当天 18:00。
- 用户给出“提前 N 分钟提醒”时提取 N；未给提醒则为 null。
- ready 的所有时间必须是应用 timezone 后、带 UTC 偏移的 ISO 8601。
- 非改期任务返回 unsupported。
"""


def validate_smart_plan(plan):
    if not isinstance(plan, dict) or set(plan) != set(SMART_PLAN_SCHEMA["required"]):
        raise ValueError("智能规划结果字段不符合约定")
    if plan.get("status") not in {"ready", "needs_confirmation", "unsupported"}:
        raise ValueError("智能规划状态无效")
    if not isinstance(plan.get("reasoning_summary"), str) or not plan["reasoning_summary"].strip():
        raise ValueError("智能规划缺少简短说明")
    if plan["status"] == "needs_confirmation":
        if not isinstance(plan.get("confirmation_question"), str) or not plan["confirmation_question"].strip():
            raise ValueError("需要确认时必须给出问题")
        return plan
    if plan["status"] == "unsupported":
        return plan
    if plan.get("operation") != "smart_reschedule":
        raise ValueError("当前只支持 smart_reschedule")
    title = plan.get("title")
    if not isinstance(title, str) or not 1 <= len(title.strip()) <= 200:
        raise ValueError("会议标题必须为 1–200 个字符")
    old = _parse_iso(plan.get("original_start_iso"), "original_start_iso")
    requested = _parse_iso(plan.get("requested_start_iso"), "requested_start_iso")
    window_end = _parse_iso(plan.get("search_window_end_iso"), "search_window_end_iso")
    if old == requested:
        raise ValueError("原时间和目标时间不能相同")
    if window_end <= requested:
        raise ValueError("搜索截止时间必须晚于目标时间")
    if plan.get("conflict_policy") not in {"reject", "next_available"}:
        raise ValueError("冲突策略无效")
    reminder = plan.get("reminder_minutes")
    if reminder is not None and (isinstance(reminder, bool) or not isinstance(reminder, int) or not 0 <= reminder <= 10080):
        raise ValueError("提醒时间必须为 0–10080 分钟")
    if plan.get("confirmation_question") is not None:
        raise ValueError("ready 规划不应包含确认问题")
    return plan


def smart_timestamps(plan):
    validate_smart_plan(plan)
    if plan["status"] != "ready":
        raise ValueError("智能规划尚未 ready")
    return (
        plan["title"].strip(),
        int(_parse_iso(plan["original_start_iso"], "original_start_iso").timestamp() * 1000),
        int(_parse_iso(plan["requested_start_iso"], "requested_start_iso").timestamp() * 1000),
        int(_parse_iso(plan["search_window_end_iso"], "search_window_end_iso").timestamp() * 1000),
    )


def openai_smart_plan(text, timezone, reference_time, model=None, api_key=None, timeout=30):
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
        "instructions": SMART_INSTRUCTIONS,
        "input": json.dumps({
            "user_notice": text,
            "reference_time_iso": reference.isoformat(),
            "timezone": timezone,
        }, ensure_ascii=False),
        "text": {"format": {
            "type": "json_schema", "name": "smart_calendar_plan", "strict": True,
            "schema": SMART_PLAN_SCHEMA,
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
        return validate_smart_plan(json.loads(_output_text(response)))
    except json.JSONDecodeError as error:
        raise RuntimeError("模型输出不是有效 JSON") from error
