#!/usr/bin/env python3
"""Conflict-aware reschedule with nearest-slot selection and an optional reminder."""

import argparse
import datetime as dt
import json
from pathlib import Path
import re
import sys
import threading
import time
import uuid

from e2e_demo import summarize_samples
from live_demo import device_sample, foreground
from notice_agent import find_event, read_snapshot
from quiet_cli import PACKAGE, read_status, run_adb, send_report
from smart_planner import openai_smart_plan, smart_timestamps, validate_smart_plan
from smart_schedule import choose_slot, conflicts_for, query_busy_events


PROOF_DIR = Path(__file__).resolve().parent.parent / "work" / "smart-proofs"


def reminder_snapshot(args, reminder_id):
    result = {}
    for column in ("_id", "event_id", "minutes", "method"):
        output = run_adb(
            args, "shell", "content", "query", "--uri",
            f"content://com.android.calendar/reminders/{reminder_id}", "--projection", column,
        ).stdout.strip()
        prefix = f"Row: 0 {column}="
        if not output.startswith(prefix):
            raise RuntimeError(f"无法回读提醒 {reminder_id} 的 {column}：{output}")
        result[column] = output[len(prefix):]
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", choices=("usb", "emulator"), default="usb")
    parser.add_argument("--serial", help="Explicit ADB serial; overrides --target")
    parser.add_argument("--adb", help="Path to adb")
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--calendar-id", type=int, required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--text", help="Natural-language smart reschedule request")
    source.add_argument("--plan-json", help="Validated plan JSON, useful for offline testing")
    parser.add_argument("--timezone", default="Australia/Sydney")
    parser.add_argument("--model", help="OpenAI model; defaults to OPENAI_MODEL")
    parser.add_argument("--reference-time", default=None)
    parser.add_argument("--task-id", default=f"smart-{uuid.uuid4().hex[:12]}")
    parser.add_argument("--delay", type=int, default=0, help="Seconds to switch to a typing app before execution")
    parser.add_argument("--sample-interval", type=float, default=0.15)
    parser.add_argument("--plan-only", action="store_true")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    proof_name = args.task_id if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", args.task_id) else f"invalid-{uuid.uuid4().hex[:12]}"
    proof = {
        "task_id": args.task_id,
        "request": args.text,
        "timezone": args.timezone,
        "started_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    return_code = 1
    try:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", args.task_id):
            raise ValueError("任务 ID 必须是 1–64 位 ASCII 字母、数字、下划线或连字符")
        if args.plan_only and args.execute:
            raise ValueError("--plan-only 与 --execute 不能同时使用")
        if args.delay not in range(0, 61):
            raise ValueError("--delay 必须为 0–60 秒")
        if not 0.05 <= args.sample_interval <= 2:
            raise ValueError("--sample-interval 必须为 0.05–2 秒")
        if args.plan_json:
            plan = validate_smart_plan(json.loads(args.plan_json))
            proof["planner"] = "provided_json"
        else:
            plan = openai_smart_plan(
                args.text, args.timezone, args.reference_time, args.model, timeout=args.timeout,
            )
            proof["planner"] = "openai"
        proof["planner_result"] = plan
        print("智能规划结果：")
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        if plan["status"] == "needs_confirmation":
            print(f"需要确认：{plan['confirmation_question']}")
            return_code = 2
            return return_code
        if plan["status"] != "ready":
            raise RuntimeError(f"当前不支持：{plan['reasoning_summary']}")
        title, old_start, requested_start, window_end = smart_timestamps(plan)
        if args.plan_only:
            print("规划校验通过；--plan-only 已在访问手机前停止。")
            proof["plan_only"] = True
            return_code = 0
            return return_code
        if read_status(args, args.task_id):
            raise RuntimeError("任务 ID 已存在；智能任务请使用新的任务 ID")

        target = find_event(args, args.calendar_id, title, old_start)
        duration = target.pop("duration_ms")
        busy = query_busy_events(
            args, args.calendar_id, requested_start, window_end,
            exclude_event_id=target["event_id"],
        )
        requested_conflicts = conflicts_for(requested_start, requested_start + duration, busy)
        chosen_start, encountered = choose_slot(
            requested_start, duration, busy, window_end, plan["conflict_policy"],
        )
        chosen_end = chosen_start + duration
        decision = {
            "requested_start_ms": requested_start,
            "requested_end_ms": requested_start + duration,
            "chosen_start_ms": chosen_start,
            "chosen_end_ms": chosen_end,
            "moved_for_conflict": chosen_start != requested_start,
            "requested_conflicts": [event.__dict__ for event in requested_conflicts],
            "encountered_conflicts": [event.__dict__ for event in encountered],
            "policy": plan["conflict_policy"],
            "window_end_ms": window_end,
            "reminder_minutes": plan["reminder_minutes"],
        }
        proof["decision"] = decision
        proof["before"] = read_snapshot(args, target["event_id"])
        execution = {
            **target,
            "new_start_ms": chosen_start,
            "new_end_ms": chosen_end,
            "reminder_minutes": plan["reminder_minutes"] if plan["reminder_minutes"] is not None else -1,
        }
        proof["execution_plan"] = execution
        print("确定性冲突决策：")
        print(json.dumps(decision, ensure_ascii=False, indent=2))
        if not args.execute:
            print("当前是预演；没有修改日历。确认后添加 --execute。")
            return_code = 0
            return return_code

        if args.delay:
            print(f"请在 {args.delay} 秒内打开聊天框并持续打字；倒计时后后台执行智能改期。")
            for remaining in range(args.delay, 0, -1):
                print(f"  {remaining}…", flush=True)
                time.sleep(1)
        dispatch = device_sample(args)
        proof["ui_at_dispatch"] = dispatch
        component = dispatch.get("foreground_component")
        if not component or component.startswith(PACKAGE):
            raise RuntimeError("执行前请把 Agent 切到后台，并打开聊天或信息流应用")
        samples = []
        stop = threading.Event()

        def sample_until_done():
            while not stop.is_set():
                try:
                    samples.append(device_sample(args))
                except RuntimeError:
                    pass
                stop.wait(args.sample_interval)

        sampler = threading.Thread(target=sample_until_done, daemon=True)
        sampler.start()
        try:
            report = send_report(args, "reschedule", args.task_id, execution)
        finally:
            stop.set()
            sampler.join(timeout=2)
        samples.append(device_sample(args))
        ui_summary = summarize_samples(samples)
        proof["ui_samples"] = ui_summary
        proof["foreground_after"] = foreground(args)
        proof["result"] = report
        if report.get("state") != "complete":
            raise RuntimeError(report.get("error", "手机没有报告任务完成"))
        after = read_snapshot(args, target["event_id"])
        proof["after"] = after
        if int(after["dtstart"]) != chosen_start or int(after["dtend"]) != chosen_end:
            raise RuntimeError("事件独立回读时间不一致")
        reminder_minutes = plan["reminder_minutes"]
        if reminder_minutes is not None:
            reminder_id = report.get("reminder_id")
            if not reminder_id:
                raise RuntimeError("手机未返回提醒 ID")
            reminder = reminder_snapshot(args, reminder_id)
            proof["reminder"] = reminder
            if int(reminder["event_id"]) != target["event_id"] or \
                    int(reminder["minutes"]) != reminder_minutes:
                raise RuntimeError("提醒独立回读不一致")
        if ui_summary["agent_became_foreground"]:
            raise RuntimeError("智能任务执行期间 Agent 应用成为前台")
        if proof["foreground_after"] and proof["foreground_after"].startswith(PACKAGE):
            raise RuntimeError("智能任务完成后 Agent 应用成为前台")
        proof["verified"] = True
        print("智能改期、冲突避让、提醒设置和独立回读成功：")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        print(f"撤销：python3 tools/quiet_cli.py undo --task-id {args.task_id}")
        return_code = 0
    except (OSError, RuntimeError, ValueError, KeyError, json.JSONDecodeError) as error:
        proof["error"] = str(error)
        print(f"error: {error}", file=sys.stderr)
        if "report" in locals() and report.get("state") == "complete":
            print(f"任务可能已经写入；请撤销：python3 tools/quiet_cli.py undo --task-id {args.task_id}")
    finally:
        proof["finished_at_utc"] = dt.datetime.now(dt.timezone.utc).isoformat()
        PROOF_DIR.mkdir(parents=True, exist_ok=True)
        path = PROOF_DIR / f"{proof_name}.json"
        path.write_text(json.dumps(proof, ensure_ascii=False, indent=2) + "\n")
        print(f"本地检查记录：{path}")
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
