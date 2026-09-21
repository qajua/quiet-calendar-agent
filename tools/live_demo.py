#!/usr/bin/env python3
"""Run a timed, no-UI calendar demo and save a small, independent proof."""

import argparse
import datetime as dt
import json
from pathlib import Path
import re
import sys
import threading
import time
import uuid

from quiet_cli import PACKAGE, run_adb, send_report


PROOF_DIR = Path(__file__).resolve().parent.parent / "work" / "demo-proofs"


def foreground(args):
    output = run_adb(args, "shell", "dumpsys", "activity", "activities").stdout
    match = re.search(
        r"(?:topResumedActivity=|Resumed:\s*)ActivityRecord\{[^\n]*?\su\d+\s+([^\s}]+)",
        output,
    )
    return match.group(1) if match else None


def device_sample(args):
    component = foreground(args)
    output = run_adb(args, "shell", "dumpsys", "input_method").stdout
    shown = re.findall(r"mInputShown=(true|false)", output)
    requested = re.findall(r"mShowRequested=(true|false)", output)
    return {
        "foreground_component": component,
        "keyboard_shown": shown[-1] == "true" if shown else None,
        "keyboard_requested": requested[-1] == "true" if requested else None,
    }


def check_calendar(args):
    output = run_adb(
        args, "shell", "content", "query", "--uri", "content://com.android.calendar/calendars",
        "--projection", "_id:calendar_displayName:calendar_access_level",
    ).stdout
    row = next((line for line in output.splitlines() if f"_id={args.calendar_id}," in line), None)
    if row is None:
        raise RuntimeError(f"Calendar {args.calendar_id} not found")
    level = re.search(r"calendar_access_level=(\d+)", row)
    if level is None or int(level.group(1)) < 500:
        raise RuntimeError(f"Calendar {args.calendar_id} is not writable")
    return row


def independent_readback(args, report):
    event_id = report["event_id"]
    expected = {
        "_id": event_id,
        "title": report["title"],
        "calendar_id": report["calendar_id"],
        "dtstart": report["start_ms"],
        "dtend": report["end_ms"],
        "description": f"[Quiet Calendar Agent task:{report['task_id']}]",
    }
    observed = {}
    for column, value in expected.items():
        result = run_adb(
            args, "shell", "content", "query", "--uri",
            f"content://com.android.calendar/events/{event_id}", "--projection", column,
        ).stdout.strip()
        observed[column] = result
        if result != f"Row: 0 {column}={value}":
            raise RuntimeError(f"Independent read-back mismatch for {column}: {result}")
    return observed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", choices=("usb", "emulator"), default="usb")
    parser.add_argument("--serial", help="Explicit ADB serial; overrides --target")
    parser.add_argument("--adb", help="Path to adb")
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--calendar-id", type=int, required=True)
    parser.add_argument("--task-id", default=f"live-{uuid.uuid4().hex[:12]}")
    parser.add_argument("--title", default="后台无干扰演示")
    parser.add_argument("--in-minutes", type=int, default=120)
    parser.add_argument("--minutes", type=int, default=30)
    parser.add_argument("--delay", type=int, default=10, help="Seconds to pick up the phone before dispatch")
    args = parser.parse_args()
    try:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", args.task_id):
            raise ValueError("Invalid task ID")
        if args.in_minutes not in range(1, 525_601) or args.minutes not in range(1, 1441):
            raise ValueError("Invalid event time or duration")
        if args.delay not in range(0, 61):
            raise ValueError("--delay must be 0–60 seconds")
        state = run_adb(args, "get-state").stdout.strip()
        if state != "device":
            raise RuntimeError(f"ADB device is not ready: {state}")
        calendar = check_calendar(args)
        before = foreground(args)
        if before is None:
            raise RuntimeError("Unable to inspect the foreground app")
        if before.startswith(PACKAGE):
            raise RuntimeError("Agent app is foreground; switch to a chat or feed app first")

        start_ms = int((dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=args.in_minutes)).timestamp() * 1000)
        proof = {
            "task_id": args.task_id,
            "calendar": calendar,
            "requested_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
            "foreground_before": before,
            "foreground_after": None,
            "result": None,
            "independent_readback": None,
            "ui_samples": None,
            "automated_checks_passed": False,
            "human_no_interruption_observation": "not_recorded",
        }
        print(f"Task ID: {args.task_id}")
        print(f"在 {args.delay} 秒内拿起手机，打开聊天框并持续打字；电脑不会打开 Agent 界面。", flush=True)
        time.sleep(args.delay)
        print("发送任务…", flush=True)
        samples = []
        stop = threading.Event()

        def sample_until_done():
            while not stop.is_set():
                try:
                    samples.append(device_sample(args))
                except RuntimeError:
                    pass
                stop.wait(0.15)

        sampler = threading.Thread(target=sample_until_done, daemon=True)
        sampler.start()
        try:
            report = send_report(
                args, "create", args.task_id,
                {"calendar_id": args.calendar_id, "title": args.title,
                 "start_ms": start_ms, "minutes": args.minutes},
            )
        finally:
            stop.set()
            sampler.join(timeout=2)
        samples.append(device_sample(args))
        print(json.dumps(report, ensure_ascii=False, indent=2))
        proof["result"] = report
        proof["foreground_after"] = foreground(args)
        components = [sample["foreground_component"] for sample in samples if sample["foreground_component"]]
        keyboard = [sample["keyboard_shown"] for sample in samples if sample["keyboard_shown"] is not None]
        proof["ui_samples"] = {
            "count": len(samples),
            "foreground_components": list(dict.fromkeys(components)),
            "agent_became_foreground": any(item.startswith(PACKAGE) for item in components),
            "keyboard_visible_in_all_samples": all(keyboard) if keyboard else None,
        }
        if not report or report.get("state") != "complete":
            raise RuntimeError("Phone did not report a completed task")
        proof["independent_readback"] = independent_readback(args, report)
        if proof["foreground_after"] is None:
            raise RuntimeError("Unable to inspect the foreground app after execution")
        if proof["foreground_after"].startswith(PACKAGE):
            raise RuntimeError("Agent app became foreground")
        if proof["ui_samples"]["agent_became_foreground"]:
            raise RuntimeError("Agent app became foreground during execution")
        proof["automated_checks_passed"] = True
        print("独立回读通过；Agent 未成为前台应用。", flush=True)
        print("先在手机日历确认事件，再运行清理命令；本脚本不会自动删除事件。", flush=True)
        return_code = 0
    except (OSError, RuntimeError, ValueError, KeyError) as error:
        message = str(error)
        if "unauthorized" in message.lower():
            message = "手机尚未授权 USB 调试；请解锁手机并确认电脑授权弹窗后重试"
        elif "no devices found" in message.lower():
            message = "未检测到 USB 真机；请检查数据线和 adb devices -l"
        print(f"error: {message}", file=sys.stderr)
        if "proof" in locals():
            proof["error"] = message
        return_code = 1
    if "proof" in locals():
        proof["finished_at_utc"] = dt.datetime.now(dt.timezone.utc).isoformat()
        PROOF_DIR.mkdir(parents=True, exist_ok=True)
        proof_path = PROOF_DIR / f"{args.task_id}.json"
        proof_path.write_text(json.dumps(proof, ensure_ascii=False, indent=2) + "\n")
        print(f"本地检查记录：{proof_path}")
        selector = f"--serial {args.serial}" if args.serial else f"--target {args.target}"
        print(f"清理命令：python3 tools/quiet_cli.py {selector} cleanup --task-id {args.task_id}")
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
