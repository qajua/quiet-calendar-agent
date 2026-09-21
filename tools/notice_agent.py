#!/usr/bin/env python3
"""Parse a constrained Chinese reschedule notice and safely update one Agent-owned event."""

import argparse
import datetime as dt
import json
from pathlib import Path
import re
import sys
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from quiet_cli import read_status, run_adb, send_report


PROOF_DIR = Path(__file__).resolve().parent.parent / "work" / "notice-proofs"
NOTICE = re.compile(
    r"^(?:请)?(?:把|将)\s*[“\"']?(?P<title>.+?)[”\"']?\s*从\s*"
    r"(?P<old_date>\d{4}-\d{1,2}-\d{1,2})\s+(?P<old_time>\d{1,2}:\d{2})\s*"
    r"(?:改到|改为|调整到)\s*"
    r"(?P<new_date>\d{4}-\d{1,2}-\d{1,2})\s+(?P<new_time>\d{1,2}:\d{2})\s*[。！!]?$"
)
MARKER = re.compile(r"^\[Quiet Calendar Agent task:([A-Za-z0-9_-]{1,64})]$")


def parse_notice(text, timezone):
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
    parser.add_argument("--text", required=True, help="Chinese absolute-date reschedule notice")
    parser.add_argument("--timezone", default="Australia/Sydney")
    parser.add_argument("--task-id", default=f"reschedule-{uuid.uuid4().hex[:12]}")
    parser.add_argument("--execute", action="store_true", help="Apply after printing the resolved plan")
    args = parser.parse_args()
    proof = {"task_id": args.task_id, "notice": args.text, "timezone": args.timezone}
    try:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", args.task_id):
            raise ValueError("任务 ID 必须是 1–64 位 ASCII 字母、数字、下划线或连字符")
        title, old_start, new_start = parse_notice(args.text, args.timezone)
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
