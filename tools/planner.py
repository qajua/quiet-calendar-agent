#!/usr/bin/env python3
"""Turn a calendar notice into a validated plan; never touches the phone."""

import datetime as dt
import json
import os
import re
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


NOTICE = re.compile(
    r"^(?:请)?(?:把|将)\s*[“\"']?(?P<title>.+?)[”\"']?\s*从\s*"
    r"(?P<old_date>\d{4}-\d{1,2}-\d{1,2})\s+(?P<old_time>\d{1,2}:\d{2})\s*"
    r"(?:改到|改为|调整到)\s*"
    r"(?P<new_date>\d{4}-\d{1,2}-\d{1,2})\s+(?P<new_time>\d{1,2}:\d{2})\s*[。！!]?$"
)

PLAN_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "status": {"type": "string", "enum": ["ready", "needs_confirmation", "unsupported"]},
        "operation": {"type": ["string", "null"], "enum": ["reschedule", None]},
        "title": {"type": ["string", "null"]},
        "original_start_iso": {"type": ["string", "null"]},
        "new_start_iso": {"type": ["string", "null"]},
        "confirmation_question": {"type": ["string", "null"]},
        "reasoning_summary": {"type": "string"},
    },
    "required": [
        "status", "operation", "title", "original_start_iso", "new_start_iso",
        "confirmation_question", "reasoning_summary",
    ],
}


def parse_notice(text, timezone):
    """Deterministic absolute-date parser retained as the offline/fail-closed path."""
    match = NOTICE.fullmatch(text.strip())
    if not match:
        raise ValueError(
            "通知格式无法安全解析；请使用：把项目周会从 2026-09-23 15:00 改到 2026-09-25 16:00"
        )
    title = match.group("title").strip().strip("“”\"'")
    if not title or len(title) > 200:
        raise ValueError("会议标题必须为 1–200 个字符")
    try:
        zone = ZoneInfo(timezone)
    except ZoneInfoNotFoundError as error:
        raise ValueError(f"未知时区：{timezone}") from error

    def moment(date, clock):
        try:
            return dt.datetime.strptime(f"{date} {clock}", "%Y-%m-%d %H:%M").replace(tzinfo=zone)
        except ValueError as error:
            raise ValueError(f"无效日期时间：{date} {clock}") from error

    old = moment(match.group("old_date"), match.group("old_time"))
    new = moment(match.group("new_date"), match.group("new_time"))
    return title, int(old.timestamp() * 1000), int(new.timestamp() * 1000)


def _output_text(response):
    texts = []
    refusals = []
    for item in response.get("output", []):
        if item.get("type") != "message":
            continue
        for content in item.get("content", []):
            if content.get("type") == "output_text":
                texts.append(content.get("text", ""))
            elif content.get("type") == "refusal":
                refusals.append(content.get("refusal", "model refused"))
    if refusals:
        raise RuntimeError(f"模型拒绝规划：{' '.join(refusals)}")
    if not texts:
        raise RuntimeError("模型响应中没有结构化文本")
    return "".join(texts)


def _parse_iso(value, field):
    try:
        moment = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as error:
        raise ValueError(f"{field} 不是有效 ISO 8601 时间") from error
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError(f"{field} 必须包含 UTC 偏移")
    return moment


def _safe_http_error(error):
    """Keep API diagnostics useful without echoing request-derived secret fragments."""
    try:
        body = error.read(4096).decode("utf-8", errors="replace")
    finally:
        error.close()
    try:
        details = json.loads(body).get("error", {})
    except json.JSONDecodeError:
        details = {}
    code = details.get("code")
    kind = details.get("type")
    suffix = ", ".join(value for value in (code, kind) if isinstance(value, str))
    if error.code == 401:
        return "OpenAI API 认证失败（HTTP 401）；请检查密钥是否有效、未撤销且没有多余字符"
    if suffix:
        return f"OpenAI API 返回 HTTP {error.code}（{suffix}）"
    return f"OpenAI API 返回 HTTP {error.code}"


def validate_plan(plan):
    if not isinstance(plan, dict):
        raise ValueError("规划结果必须是对象")
    if set(plan) != set(PLAN_SCHEMA["required"]):
        raise ValueError("规划结果字段不符合约定")
    status = plan.get("status")
    if status not in {"ready", "needs_confirmation", "unsupported"}:
        raise ValueError("规划状态无效")
    if not isinstance(plan.get("reasoning_summary"), str) or not plan["reasoning_summary"].strip():
        raise ValueError("规划结果缺少简短说明")

    if status == "ready":
        if plan.get("operation") != "reschedule":
            raise ValueError("当前执行器只支持 reschedule")
        title = plan.get("title")
        if not isinstance(title, str) or not 1 <= len(title.strip()) <= 200:
            raise ValueError("会议标题必须为 1–200 个字符")
        old = _parse_iso(plan.get("original_start_iso"), "original_start_iso")
        new = _parse_iso(plan.get("new_start_iso"), "new_start_iso")
        if old == new:
            raise ValueError("原时间和新时间不能相同")
        if plan.get("confirmation_question") is not None:
            raise ValueError("ready 规划不应包含确认问题")
    elif status == "needs_confirmation":
        question = plan.get("confirmation_question")
        if not isinstance(question, str) or not question.strip():
            raise ValueError("需要确认时必须给出一个明确问题")
    return plan


def deterministic_plan(text, timezone):
    title, old_ms, new_ms = parse_notice(text, timezone)
    zone = ZoneInfo(timezone)
    return validate_plan({
        "status": "ready",
        "operation": "reschedule",
        "title": title,
        "original_start_iso": dt.datetime.fromtimestamp(old_ms / 1000, zone).isoformat(),
        "new_start_iso": dt.datetime.fromtimestamp(new_ms / 1000, zone).isoformat(),
        "confirmation_question": None,
        "reasoning_summary": "确定性解析器识别到标题、原时间和新时间。",
    })


def openai_plan(text, timezone, reference_time, model, api_key, timeout=30, url="https://api.openai.com/v1/responses"):
    if not api_key:
        raise ValueError("缺少 OPENAI_API_KEY")
    if not model:
        raise ValueError("缺少 OPENAI_MODEL 或 --model")
    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError as error:
        raise ValueError(f"未知时区：{timezone}") from error
    reference = _parse_iso(reference_time, "reference_time")
    payload = {
        "model": model,
        "store": False,
        "max_output_tokens": 800,
        "instructions": (
            "你是手机日历任务规划器，只解析用户意图，绝不声称已经执行。"
            "当前执行器只支持把一个已有日历事件改期。标题、原时间或新时间只要有歧义，"
            "就返回 needs_confirmation，并且只问一个最关键的问题。不要猜测。"
            "所有 ready 时间必须是带 UTC 偏移的 ISO 8601。无法支持的动作返回 unsupported。"
        ),
        "input": json.dumps({
            "user_notice": text,
            "reference_time_iso": reference.isoformat(),
            "timezone": timezone,
        }, ensure_ascii=False),
        "text": {
            "format": {
                "type": "json_schema",
                "name": "calendar_reschedule_plan",
                "strict": True,
                "schema": PLAN_SCHEMA,
            }
        },
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as result:
            response = json.loads(result.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        raise RuntimeError(_safe_http_error(error)) from error
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
        raise RuntimeError(f"OpenAI API 请求失败：{error}") from error
    if response.get("status") not in {None, "completed"}:
        raise RuntimeError(f"模型响应未完成：{response.get('status')}")
    try:
        plan = json.loads(_output_text(response))
    except json.JSONDecodeError as error:
        raise RuntimeError("模型输出不是有效 JSON") from error
    return validate_plan(plan)


def plan_notice(text, timezone, reference_time, planner="auto", model=None, api_key=None, timeout=30):
    if reference_time is None:
        try:
            reference_time = dt.datetime.now(ZoneInfo(timezone)).isoformat()
        except ZoneInfoNotFoundError as error:
            raise ValueError(f"未知时区：{timezone}") from error
    api_key = api_key if api_key is not None else os.environ.get("OPENAI_API_KEY")
    model = model or os.environ.get("OPENAI_MODEL")
    if planner == "auto":
        planner = "openai" if api_key and model else "deterministic"
    if planner == "deterministic":
        return deterministic_plan(text, timezone), "deterministic"
    if planner == "openai":
        return openai_plan(text, timezone, reference_time, model, api_key, timeout), "openai"
    raise ValueError(f"未知规划器：{planner}")


def ready_timestamps(plan):
    validate_plan(plan)
    if plan["status"] != "ready":
        raise ValueError("规划尚未 ready")
    return (
        plan["title"].strip(),
        int(_parse_iso(plan["original_start_iso"], "original_start_iso").timestamp() * 1000),
        int(_parse_iso(plan["new_start_iso"], "new_start_iso").timestamp() * 1000),
    )
