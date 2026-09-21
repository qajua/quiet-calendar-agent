#!/usr/bin/env python3
"""Debug-only desktop bridge for a phone whose screen remains with its owner."""

import argparse
import datetime as dt
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import uuid


PACKAGE = "dev.qajua.quietcalendar"
BRIDGE_URI = f"content://{PACKAGE}.debug"


def adb_binary(explicit):
    if explicit:
        return explicit
    sdk = os.environ.get("ANDROID_HOME")
    if sdk:
        candidate = os.path.join(sdk, "platform-tools", "adb")
        if os.path.isfile(candidate):
            return candidate
    return shutil.which("adb") or "/opt/homebrew/share/android-commandlinetools/platform-tools/adb"


def run_adb(args, *command, check=True):
    selector = ["-s", args.serial] if args.serial else ["-d" if args.target == "usb" else "-e"]
    remote = ["shell", shlex.join(command[1:])] if command and command[0] == "shell" else list(command)
    result = subprocess.run(
        [adb_binary(args.adb), *selector, *remote],
        text=True,
        capture_output=True,
        check=False,
    )
    if check and result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip())
    return result


def read_status(args, task_id):
    result = run_adb(args, "shell", "run-as", PACKAGE, "cat", f"files/task-{task_id}.json", check=False)
    if result.returncode:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def bridge_token(args):
    def read():
        result = run_adb(args, "shell", "run-as", PACKAGE, "cat", "files/bridge-token.txt", check=False)
        return result.stdout.strip() if result.returncode == 0 else ""

    token = read()
    if not token:
        response = run_adb(args, "shell", "content", "call", "--uri", BRIDGE_URI, "--method", "bootstrap")
        if "Error while accessing provider" in response.stdout:
            raise RuntimeError(response.stdout.splitlines()[0])
        deadline = time.monotonic() + 5
        while not token and time.monotonic() < deadline:
            time.sleep(0.2)
            token = read()
    if len(token) != 64 or any(char not in "0123456789abcdef" for char in token):
        raise RuntimeError("Debug bridge token unavailable; check app installation and ADB authorization")
    return token


def send_report(args, action, task_id, fields):
    command_id = uuid.uuid4().hex
    token = bridge_token(args)
    payload = {"bridge_token": token, "task_id": task_id, "command_id": command_id, **fields}
    response = run_adb(
        args, "shell", "content", "call", "--uri", BRIDGE_URI,
        "--method", action, "--arg", json.dumps(payload, ensure_ascii=False),
    )
    if "Error while accessing provider" in response.stdout:
        raise RuntimeError(response.stdout.splitlines()[0])
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        report = read_status(args, task_id)
        if report and report.get("command_id") == command_id:
            return report
        time.sleep(0.3)
    raise TimeoutError(f"No completed result for task {task_id} within {args.timeout}s")


def send(args, action, task_id, fields):
    report = send_report(args, action, task_id, fields)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("state") in {"complete", "deleted"} else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", choices=("usb", "emulator"), default="usb")
    parser.add_argument("--serial", help="Explicit ADB serial; overrides --target")
    parser.add_argument("--adb", help="Path to adb; defaults to ANDROID_HOME/platform-tools/adb")
    parser.add_argument("--timeout", type=float, default=30)
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("calendars", help="List calendars without reading personal events")
    create = sub.add_parser("create", help="Create and read back one calendar event")
    create.add_argument("--task-id", default=None)
    create.add_argument("--calendar-id", type=int, required=True)
    create.add_argument("--title", required=True)
    when = create.add_mutually_exclusive_group(required=True)
    when.add_argument("--start", help="ISO time with UTC offset, e.g. YYYY-MM-DDTHH:MM:SS+10:00")
    when.add_argument("--in-minutes", type=int, help="Start this many minutes from now")
    create.add_argument("--minutes", type=int, default=30)
    cleanup = sub.add_parser("cleanup", help="Delete only this task's unchanged event")
    cleanup.add_argument("--task-id", required=True)
    status = sub.add_parser("status", help="Read a task result without touching the phone UI")
    status.add_argument("--task-id", required=True)
    args = parser.parse_args()
    try:
        if args.action == "calendars":
            result = run_adb(
                args, "shell", "content", "query", "--uri", "content://com.android.calendar/calendars",
                "--projection", "_id:calendar_displayName:calendar_access_level",
            )
            print(result.stdout.strip())
            return 0
        task_id = getattr(args, "task_id", None) or f"demo-{uuid.uuid4().hex[:12]}"
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", task_id):
            raise ValueError("Task ID must be 1–64 ASCII letters, digits, underscores, or hyphens")
        if args.action == "status":
            report = read_status(args, task_id)
            if not report:
                raise RuntimeError("No task result found")
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
        if args.action == "cleanup":
            return send(args, "cleanup", task_id, {})
        if args.in_minutes is not None:
            if args.in_minutes < 1 or args.in_minutes > 525_600:
                raise ValueError("--in-minutes must be 1–525600")
            start = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=args.in_minutes)
        else:
            start = dt.datetime.fromisoformat(args.start)
            if start.tzinfo is None or start.utcoffset() is None:
                raise ValueError("--start must include a timezone offset")
        start_ms = int(start.timestamp() * 1000)
        print(f"task_id={task_id}", flush=True)
        return send(
            args, "create", task_id,
            {"calendar_id": args.calendar_id, "title": args.title,
             "start_ms": start_ms, "minutes": args.minutes},
        )
    except (OSError, RuntimeError, TimeoutError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
