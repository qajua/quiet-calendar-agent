#!/usr/bin/env python3
"""Parse a constrained Chinese reschedule notice and safely update one Agent-owned event."""

import argparse
import datetime as dt
import json
from pathlib import Path
import re
import sys
import uuid

from planner import parse_notice, plan_notice, ready_timestamps
from quiet_cli import read_status, run_adb, send_report


PROOF_DIR = Path(__file__).resolve().parent.parent / "work" / "notice-proofs"
MARKER = re.compile(r"^\[Quiet Calendar Agent task:([A-Za-z0-9_-]{1,64})]$")


class PlanningPaused(Exception):
    """The plan needs one answer before any phone lookup or write is allowed."""


def read_field(args, event_id, column):
    result = run_adb(
        args, "shell", "content", "query", "--uri",
        f"content://com.android.calendar/events/{event_id}", "--projection", column,
    ).stdout.strip()
    prefix = f"Row: 0 {column}="
    if not result.startswith(prefix):
        raise RuntimeError(f"无法读取事件 {event_id} 的 {column}：{result}")
    return result[len(prefix):]


def find_event(args, calendar_id, title, start_ms):
    result = run_adb(
        args, "shell", "content", "query", "--uri", "content://com.android.calendar/events",
        "--projection", "_id", "--where", f"calendar_id={calendar_id} AND dtstart={start_ms}",
    ).stdout
    ids = [int(value) for value in re.findall(r"_id=(\d+)", result)]
    matches = []
    for event_id in ids:
        if read_field(args, event_id, "title") == title:
            matches.append(event_id)
    if not matches:
        raise RuntimeError("没有找到标题和原时间都匹配的事件；停止操作")
    if len(matches) > 1:
        raise RuntimeError("找到多个匹配事件，无法安全决定修改哪一个；停止操作")
    event_id = matches[0]
    description = read_field(args, event_id, "description")
    marker = MARKER.fullmatch(description)
    if not marker:
        raise RuntimeError("匹配事件不是本 Agent 创建的测试事件；第一版拒绝修改")
    end_ms = int(read_field(args, event_id, "dtend"))
    if end_ms <= start_ms:
        raise RuntimeError("原事件时长无效；停止操作")
    return {
        "event_id": event_id,
        "calendar_id": calendar_id,
        "owner_task_id": marker.group(1),
        "title": title,
        "expected_start_ms": start_ms,
        "expected_end_ms": end_ms,
        "duration_ms": end_ms - start_ms,
        "description": description,
    }


def read_snapshot(args, event_id):
    return {
        column: read_field(args, event_id, column)
        for column in ("_id", "calendar_id", "title", "dtstart", "dtend", "description")
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", choices=("usb", "emulator"), default="usb")
    parser.add_argument("--serial", help="Explicit ADB serial; overrides --target")
    parser.add_argument("--adb", help="Path to adb")
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--calendar-id", type=int, required=True)
    parser.add_argument("--text", required=True, help="Chinese reschedule notice")
    parser.add_argument("--timezone", default="Australia/Sydney")
    parser.add_argument("--planner", choices=("auto", "deterministic", "openai"), default="auto")
    parser.add_argument("--model", help="OpenAI model; defaults to OPENAI_MODEL")
    parser.add_argument(
        "--reference-time",
        default=None,
        help="ISO 8601 current time supplied to the planner",
    )
    parser.add_argument("--task-id", default=f"reschedule-{uuid.uuid4().hex[:12]}")
    parser.add_argument("--execute", action="store_true", help="Apply after printing the resolved plan")
    args = parser.parse_args()
    proof = {"task_id": args.task_id, "notice": args.text, "timezone": args.timezone}
    try:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", args.task_id):
            raise ValueError("任务 ID 必须是 1–64 位 ASCII 字母、数字、下划线或连字符")
        plan_result, planner_used = plan_notice(
            args.text, args.timezone, args.reference_time, args.planner, args.model,
            timeout=args.timeout,
        )
        proof["planner"] = planner_used
        proof["planner_result"] = plan_result
        print(f"规划器：{planner_used}")
        print(json.dumps(plan_result, ensure_ascii=False, indent=2))
        if plan_result["status"] == "needs_confirmation":
            print(f"需要确认：{plan_result['confirmation_question']}")
            return_code = 2
            raise PlanningPaused
        if plan_result["status"] == "unsupported":
            raise ValueError(f"当前不支持该任务：{plan_result['reasoning_summary']}")
        title, old_start, new_start = ready_timestamps(plan_result)
        existing = read_status(args, args.task_id)
        if existing and existing.get("operation") == "reschedule":
            expected_new_end = new_start + existing["original_end_ms"] - existing["original_start_ms"]
            if not (
                existing.get("title") == title and
                existing.get("original_start_ms") == old_start and
                existing.get("start_ms") == new_start and
                existing.get("end_ms") == expected_new_end
            ):
                raise RuntimeError("任务 ID 已用于不同的改期通知；停止操作")
            if existing.get("state") == "undone":
                raise RuntimeError("该改期任务已撤销；如需再次改期请使用新任务 ID")
            if existing.get("state") != "complete":
                raise RuntimeError(existing.get("error", "既有任务未成功完成"))
            proof["result"] = existing
            proof["after"] = read_snapshot(args, existing["event_id"])
            if int(proof["after"]["dtstart"]) != new_start or int(proof["after"]["dtend"]) != expected_new_end:
                raise RuntimeError("既有任务记录与当前日历不一致；停止操作")
            proof["verified"] = True
            proof["replayed"] = True
            print("同一任务已完成；独立回读仍与通知一致：")
            print(json.dumps(existing, ensure_ascii=False, indent=2))
            print(f"撤销命令：python3 tools/quiet_cli.py undo --task-id {args.task_id}")
            return_code = 0
        else:
            event = find_event(args, args.calendar_id, title, old_start)
            new_end = new_start + event.pop("duration_ms")
            plan = {
                **event,
                "new_start_ms": new_start,
                "new_end_ms": new_end,
            }
            proof["plan"] = plan
            proof["before"] = read_snapshot(args, plan["event_id"])
            print("解析并匹配到唯一事件：")
            print(json.dumps(plan, ensure_ascii=False, indent=2))
            if not args.execute:
                print("当前是预演，没有修改日历。确认后加 --execute 执行。")
                return_code = 0
            else:
                report = send_report(args, "reschedule", args.task_id, plan)
                proof["result"] = report
                proof["after"] = read_snapshot(args, plan["event_id"])
                if report.get("state") != "complete":
                    raise RuntimeError(report.get("error", "手机没有报告改期完成"))
                if int(proof["after"]["dtstart"]) != new_start or int(proof["after"]["dtend"]) != new_end:
                    raise RuntimeError("独立回读发现新时间不匹配")
                proof["verified"] = True
                print("改期并独立回读成功：")
                print(json.dumps(report, ensure_ascii=False, indent=2))
                print(f"撤销命令：python3 tools/quiet_cli.py undo --task-id {args.task_id}")
                return_code = 0
    except PlanningPaused:
        pass
    except (OSError, RuntimeError, ValueError, KeyError) as error:
        proof["error"] = str(error)
        print(f"error: {error}", file=sys.stderr)
        return_code = 1
    proof["finished_at_utc"] = dt.datetime.now(dt.timezone.utc).isoformat()
    PROOF_DIR.mkdir(parents=True, exist_ok=True)
    path = PROOF_DIR / f"{args.task_id}.json"
    path.write_text(json.dumps(proof, ensure_ascii=False, indent=2) + "\n")
    print(f"本地检查记录：{path}")
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
