#!/usr/bin/env python3
"""One-command filmed demo: plan, match, background-reschedule, and prove no UI takeover."""

import argparse
import datetime as dt
import json
from pathlib import Path
import re
import sys
import threading
import time
import uuid

from live_demo import check_calendar, device_sample, foreground
from notice_agent import find_event, read_snapshot
from planner import plan_notice, ready_timestamps
from quiet_cli import PACKAGE, read_status, run_adb, send_report


PROOF_DIR = Path(__file__).resolve().parent.parent / "work" / "e2e-proofs"


def summarize_samples(samples):
    components = [item["foreground_component"] for item in samples if item.get("foreground_component")]
    keyboard = [item["keyboard_shown"] for item in samples if item.get("keyboard_shown") is not None]
    return {
        "count": len(samples),
        "foreground_components": list(dict.fromkeys(components)),
        "agent_became_foreground": any(item.startswith(PACKAGE) for item in components),
        "keyboard_visible_samples": sum(keyboard),
        "keyboard_known_samples": len(keyboard),
    }


def verify_execution(report, after, new_start, new_end):
    if report.get("state") != "complete":
        raise RuntimeError(report.get("error", "手机没有报告改期完成"))
    expected = {
        "_id": str(report["event_id"]),
        "calendar_id": str(report["calendar_id"]),
        "title": report["title"],
        "dtstart": str(new_start),
        "dtend": str(new_end),
        "description": f"[Quiet Calendar Agent task:{report['owner_task_id']}]",
    }
    mismatches = {key: (expected[key], after.get(key)) for key in expected if after.get(key) != expected[key]}
    if mismatches:
        raise RuntimeError(f"独立回读不一致：{mismatches}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", choices=("usb", "emulator"), default="usb")
    parser.add_argument("--serial", help="Explicit ADB serial; overrides --target")
    parser.add_argument("--adb", help="Path to adb")
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--calendar-id", type=int, required=True)
    parser.add_argument("--text", required=True, help="Natural-language reschedule notice")
    parser.add_argument("--timezone", default="Australia/Sydney")
    parser.add_argument("--planner", choices=("auto", "deterministic", "openai"), default="openai")
    parser.add_argument("--model", help="OpenAI model; defaults to OPENAI_MODEL")
    parser.add_argument("--reference-time", default=None, help="ISO 8601 current time for relative dates")
    parser.add_argument("--task-id", default=f"e2e-{uuid.uuid4().hex[:12]}")
    parser.add_argument("--delay", type=int, default=10, help="Seconds to switch to a chat app")
    parser.add_argument("--sample-interval", type=float, default=0.15)
    parser.add_argument("--execute", action="store_true", help="Required: apply the matched reschedule")
    args = parser.parse_args()
    proof_name = args.task_id if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", args.task_id) else f"invalid-{uuid.uuid4().hex[:12]}"
    proof = {
        "task_id": args.task_id,
        "notice": args.text,
        "timezone": args.timezone,
        "reference_time": args.reference_time,
        "started_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "automated_checks_passed": False,
        "human_no_interruption_observation": "not_recorded",
    }
    return_code = 1
    try:
        if not args.execute:
            raise ValueError("一键演示会修改测试事件；确认后必须显式添加 --execute")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", args.task_id):
            raise ValueError("任务 ID 必须是 1–64 位 ASCII 字母、数字、下划线或连字符")
        if args.delay not in range(0, 61):
            raise ValueError("--delay 必须为 0–60 秒")
        if not 0.05 <= args.sample_interval <= 2:
            raise ValueError("--sample-interval 必须为 0.05–2 秒")
        if run_adb(args, "get-state").stdout.strip() != "device":
            raise RuntimeError("ADB 设备未就绪")
        proof["calendar"] = check_calendar(args)
        if read_status(args, args.task_id):
            raise RuntimeError("任务 ID 已存在；一键演示必须使用新的任务 ID")

        print("1/4 规划器正在生成受 JSON Schema 约束的计划…", flush=True)
        plan_result, planner_used = plan_notice(
            args.text, args.timezone, args.reference_time, args.planner, args.model,
            timeout=args.timeout,
        )
        proof["planner"] = planner_used
        proof["planner_result"] = plan_result
        print(json.dumps(plan_result, ensure_ascii=False, indent=2))
        if plan_result["status"] == "needs_confirmation":
            print(f"需要确认：{plan_result['confirmation_question']}")
            return_code = 2
            return return_code
        if plan_result["status"] != "ready":
            raise RuntimeError(f"规划未就绪：{plan_result['reasoning_summary']}")

        title, old_start, new_start = ready_timestamps(plan_result)
        print("2/4 正在确定性匹配手机里的唯一 Agent 事件…", flush=True)
        event = find_event(args, args.calendar_id, title, old_start)
        duration = event.pop("duration_ms")
        new_end = new_start + duration
        execution_plan = {**event, "new_start_ms": new_start, "new_end_ms": new_end}
        proof["execution_plan"] = execution_plan
        proof["before"] = read_snapshot(args, event["event_id"])
        print(json.dumps(execution_plan, ensure_ascii=False, indent=2))

        print(f"3/4 请在 {args.delay} 秒内打开聊天框并持续打字；倒计时结束后后台改期。", flush=True)
        for remaining in range(args.delay, 0, -1):
            print(f"  {remaining}…", flush=True)
            time.sleep(1)
        dispatch_sample = device_sample(args)
        proof["ui_at_dispatch"] = dispatch_sample
        component = dispatch_sample.get("foreground_component")
        if not component:
            raise RuntimeError("无法读取执行前台应用")
        if component.startswith(PACKAGE):
            raise RuntimeError("Agent 应用仍在前台；请切换到聊天或信息流应用")

        samples = []
        stop = threading.Event()

        def sample_until_done():
            while not stop.is_set():
                try:
                    samples.append(device_sample(args))
                except RuntimeError:
                    pass
                stop.wait(args.sample_interval)

        print("4/4 Agent 正在后台改期；请继续使用手机…", flush=True)
        sampler = threading.Thread(target=sample_until_done, daemon=True)
        sampler.start()
        try:
            report = send_report(args, "reschedule", args.task_id, execution_plan)
            proof["result"] = report
        finally:
            stop.set()
            sampler.join(timeout=2)
        samples.append(device_sample(args))
        after = read_snapshot(args, event["event_id"])
        summary = summarize_samples(samples)
        proof["after"] = after
        proof["ui_samples"] = summary
        proof["foreground_after"] = foreground(args)
        verify_execution(report, after, new_start, new_end)
        if summary["agent_became_foreground"]:
            raise RuntimeError("执行期间 Agent 应用成为前台")
        if proof["foreground_after"] and proof["foreground_after"].startswith(PACKAGE):
            raise RuntimeError("执行后 Agent 应用成为前台")
        proof["automated_checks_passed"] = True
        print("完成：改期已独立回读，Agent 全程未成为前台应用。")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        print(f"撤销：python3 tools/quiet_cli.py undo --task-id {args.task_id}")
        print(f"撤销后清理：python3 tools/quiet_cli.py cleanup --task-id {event['owner_task_id']}")
        return_code = 0
    except (OSError, RuntimeError, ValueError, KeyError) as error:
        message = str(error)
        proof["error"] = message
        print(f"error: {message}", file=sys.stderr)
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
