#!/usr/bin/env python3
"""Configure and inspect the phone's allowlisted private notification inbox."""

import argparse
import json
import sys

from quiet_cli import BRIDGE_URI, PACKAGE, bridge_token, run_adb


INBOX_FILE = "files/notification-inbox.json"
CONFIG_FILE = "files/notification-config.json"


def read_private_json(args, path, fallback):
    result = run_adb(args, "shell", "run-as", PACKAGE, "cat", path, check=False)
    if result.returncode:
        return fallback
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"手机通知文件不是有效 JSON：{path}") from error


def read_inbox(args):
    value = read_private_json(args, INBOX_FILE, [])
    if not isinstance(value, list):
        raise RuntimeError("手机通知收件箱格式无效")
    return value


def read_config(args):
    value = read_private_json(args, CONFIG_FILE, {"state": "unconfigured", "packages": []})
    if not isinstance(value, dict):
        raise RuntimeError("手机通知配置格式无效")
    return value


def call_notification_method(args, method, fields=None):
    payload = {"bridge_token": bridge_token(args), **(fields or {})}
    response = run_adb(
        args, "shell", "content", "call", "--uri", BRIDGE_URI,
        "--method", method, "--arg", json.dumps(payload, ensure_ascii=False),
    )
    if "Error while accessing provider" in response.stdout:
        raise RuntimeError(response.stdout.splitlines()[0])
    return response.stdout.strip()


def listener_status(args):
    component = f"{PACKAGE}/{PACKAGE}.QuietNotificationListener"
    enabled = run_adb(
        args, "shell", "settings", "get", "secure", "enabled_notification_listeners",
        check=False,
    ).stdout.strip()
    return {"component": component, "enabled": component in enabled, "raw": enabled}


def add_connection_args(parser):
    parser.add_argument("--target", choices=("usb", "emulator"), default="usb")
    parser.add_argument("--serial", help="Explicit ADB serial; overrides --target")
    parser.add_argument("--adb", help="Path to adb")
    parser.add_argument("--timeout", type=float, default=30)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    add_connection_args(parser)
    sub = parser.add_subparsers(dest="action", required=True)
    configure = sub.add_parser("configure", help="Replace the notification source allowlist")
    configure.add_argument("packages", nargs="+", help="Android package names")
    sub.add_parser("status", help="Show listener access, allowlist, and captured count")
    sub.add_parser("inbox", help="Print captured allowlisted notifications")
    sub.add_parser("clear", help="Clear captured notifications")
    test = sub.add_parser("test", help="Post a local test notification as com.android.shell")
    test.add_argument("--title", default="日程助理")
    test.add_argument("--text", required=True)
    args = parser.parse_args()
    try:
        if args.action == "configure":
            call_notification_method(args, "notification_config", {"packages": args.packages})
            print(json.dumps(read_config(args), ensure_ascii=False, indent=2))
        elif args.action == "clear":
            call_notification_method(args, "notification_clear")
            print("手机通知收件箱已清空。")
        elif args.action == "inbox":
            print(json.dumps(read_inbox(args), ensure_ascii=False, indent=2))
        elif args.action == "test":
            result = run_adb(
                args, "shell", "cmd", "notification", "post", "-S", "bigtext",
                "-t", args.title, "quiet-agent-test", args.text,
            )
            print(result.stdout.strip() or "测试通知已发送。")
        else:
            print(json.dumps({
                "listener": listener_status(args),
                "config": read_config(args),
                "captured_count": len(read_inbox(args)),
            }, ensure_ascii=False, indent=2))
        return 0
    except (OSError, RuntimeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
