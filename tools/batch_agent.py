#!/usr/bin/env python3
"""Execute or undo a verified multi-step calendar Saga."""

import argparse
import datetime as dt
import json
from pathlib import Path
import re
import sys
import threading
import time
import uuid

from batch_planner import batch_timestamps, openai_batch_plan, validate_batch_plan
from e2e_demo import summarize_samples
from live_demo import device_sample, foreground
from notice_agent import find_event, read_snapshot
from quiet_cli import PACKAGE, read_status, send_report
from smart_agent import reminder_snapshot
from smart_schedule import choose_slot, conflicts_for, query_busy_events


PROOF_DIR = Path(__file__).resolve().parent.parent / "work" / "batch-proofs"


def proof_path(task_id):
    return PROOF_DIR / f"{task_id}.json"


def write_proof(path, proof):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(proof, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def compensate(args, completed_steps):
    results = []
    for step in reversed(completed_steps):
        action = "cleanup" if step["type"] == "followup" else "undo"
        try:
            report = send_report(args, action, step["task_id"], {})
            results.append({"step": step["type"], "action": action, "report": report})
        except Exception as error:
            results.append({"step": step["type"], "action": action, "error": str(error)})
    return results


def compensation_succeeded(results):
    expected = {"cleanup": "deleted", "undo": "undone"}
    return all(
        "error" not in item
        and item.get("report", {}).get("state") == expected.get(item.get("action"))
        for item in results
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", choices=("usb", "emulator"), default="usb")
    parser.add_argument("--serial", help="Explicit ADB serial; overrides --target")
    parser.add_argument("--adb", help="Path to adb")
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--calendar-id", type=int)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--text", help="Natural-language compound task")
    source.add_argument("--plan-json", help="Validated plan JSON for offline testing")
    parser.add_argument("--timezone", default="Australia/Sydney")
    parser.add_argument("--model", help="OpenAI model; defaults to OPENAI_MODEL")
    parser.add_argument("--reference-time", default=None)
    parser.add_argument("--task-id", default=f"batch-{uuid.uuid4().hex[:10]}")
    parser.add_argument("--delay", type=int, default=0)
    parser.add_argument("--sample-interval", type=float, default=0.15)
    parser.add_argument("--plan-only", action="store_true")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--undo", action="store_true", help="Undo a completed batch from its local journal")
    args = parser.parse_args()
    safe_id = re.fullmatch(r"[A-Za-z0-9_-]{1,48}", args.task_id)
    path = proof_path(args.task_id if safe_id else f"invalid-{uuid.uuid4().hex[:10]}")

    if args.undo:
        if not safe_id:
            print("error: 批量任务 ID 必须是 1–48 位安全字符", file=sys.stderr)
            return 1
        if args.text or args.plan_json or args.execute or args.plan_only:
            print("error: --undo 不能与规划或执行参数同时使用", file=sys.stderr)
            return 1
        try:
            proof = json.loads(path.read_text())
            if proof.get("state") == "undone":
                print("批量任务已经撤销。")
                return 0
            if proof.get("state") != "complete":
                raise RuntimeError(f"只有 complete 批量任务可撤销，当前为 {proof.get('state')}")
            results = compensate(args, proof.get("completed_steps", []))
            if not compensation_succeeded(results):
                raise RuntimeError(f"批量撤销未完全成功：{results}")
            proof["state"] = "undone"
            proof["undo_results"] = results
            proof["finished_at_utc"] = dt.datetime.now(dt.timezone.utc).isoformat()
            write_proof(path, proof)
            print("批量任务已按相反顺序撤销：先删除会后任务，再恢复原会议和提醒。")
            print(json.dumps(results, ensure_ascii=False, indent=2))
            return 0
        except (OSError, RuntimeError, ValueError, KeyError, json.JSONDecodeError) as error:
            print(f"error: {error}", file=sys.stderr)
            return 1

    if path.exists():
        print("error: 批量任务 ID 已有本地日志；请使用新 ID，或对 complete 任务执行 --undo", file=sys.stderr)
        return 1

    proof = {
        "task_id": args.task_id,
        "state": "planning",
        "request": args.text,
        "timezone": args.timezone,
        "completed_steps": [],
        "started_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    return_code = 1
    try:
        if not safe_id:
            raise ValueError("批量任务 ID 必须是 1–48 位 ASCII 字母、数字、下划线或连字符")
        if args.calendar_id is None:
            raise ValueError("执行批量任务需要 --calendar-id")
        if not args.text and not args.plan_json:
            raise ValueError("需要 --text 或 --plan-json")
        if args.plan_only and args.execute:
            raise ValueError("--plan-only 与 --execute 不能同时使用")
        if args.delay not in range(0, 61) or not 0.05 <= args.sample_interval <= 2:
            raise ValueError("倒计时或采样间隔无效")
        if args.plan_json:
            plan = validate_batch_plan(json.loads(args.plan_json))
            proof["planner"] = "provided_json"
        else:
            plan = openai_batch_plan(
                args.text, args.timezone, args.reference_time, args.model, timeout=args.timeout,
            )
            proof["planner"] = "openai"
        proof["planner_result"] = plan
        print("批量规划结果：")
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        if plan["status"] == "needs_confirmation":
            proof["state"] = "needs_confirmation"
            print(f"需要确认：{plan['confirmation_question']}")
            return_code = 2
            return return_code
        if plan["status"] != "ready":
            raise RuntimeError(f"当前不支持：{plan['reasoning_summary']}")
        title, old_start, requested_start, window_end = batch_timestamps(plan)
        if args.plan_only:
            proof["state"] = "planned"
            print("批量规划校验通过；尚未访问手机。")
            return_code = 0
            return return_code

        move_id = f"{args.task_id}-move"
        followup_id = f"{args.task_id}-follow"
        if read_status(args, move_id) or read_status(args, followup_id):
            raise RuntimeError("手机中已存在同名批量子任务；请更换批量任务 ID")
        target = find_event(args, args.calendar_id, title, old_start)
        meeting_duration = target.pop("duration_ms")
        followup_duration = plan["followup_duration_minutes"] * 60_000
        reserved_duration = meeting_duration + followup_duration
        busy = query_busy_events(
            args, args.calendar_id, requested_start, window_end, exclude_event_id=target["event_id"],
        )
        requested_conflicts = conflicts_for(requested_start, requested_start + reserved_duration, busy)
        chosen_start, encountered = choose_slot(
            requested_start, reserved_duration, busy, window_end, plan["conflict_policy"],
        )
        meeting_end = chosen_start + meeting_duration
        followup_end = meeting_end + followup_duration
        decision = {
            "requested_start_ms": requested_start,
            "chosen_start_ms": chosen_start,
            "meeting_end_ms": meeting_end,
            "followup_start_ms": meeting_end,
            "followup_end_ms": followup_end,
            "reserved_duration_ms": reserved_duration,
            "moved_for_conflict": chosen_start != requested_start,
            "requested_conflicts": [event.__dict__ for event in requested_conflicts],
            "encountered_conflicts": [event.__dict__ for event in encountered],
        }
        move_plan = {
            **target,
            "new_start_ms": chosen_start,
            "new_end_ms": meeting_end,
            "reminder_minutes": plan["reminder_minutes"] if plan["reminder_minutes"] is not None else -1,
        }
        proof.update({
            "state": "planned",
            "decision": decision,
            "move_plan": move_plan,
            "before": read_snapshot(args, target["event_id"]),
            "subtasks": {"move": move_id, "followup": followup_id},
        })
        print("批量确定性决策：")
        print(json.dumps(decision, ensure_ascii=False, indent=2))
        if not args.execute:
            print("当前是批量预演；没有修改日历。确认后添加 --execute。")
            return_code = 0
            return return_code

        if args.delay:
            print(f"请在 {args.delay} 秒内打开聊天框并持续打字。")
            for remaining in range(args.delay, 0, -1):
                print(f"  {remaining}…", flush=True)
                time.sleep(1)
        dispatch = device_sample(args)
        proof["ui_at_dispatch"] = dispatch
        if not dispatch.get("foreground_component") or dispatch["foreground_component"].startswith(PACKAGE):
            raise RuntimeError("执行前请把 Agent 切到后台")

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
            proof["state"] = "executing"
            write_proof(path, proof)
            move_report = send_report(args, "reschedule", move_id, move_plan)
            if move_report.get("state") != "complete":
                raise RuntimeError(move_report.get("error", "改期子任务失败"))
            proof["move_result"] = move_report
            proof["completed_steps"].append({"type": "move", "task_id": move_id})
            write_proof(path, proof)

            followup_report = send_report(args, "create", followup_id, {
                "calendar_id": args.calendar_id,
                "title": plan["followup_title"].strip(),
                "start_ms": meeting_end,
                "minutes": plan["followup_duration_minutes"],
            })
            if followup_report.get("state") != "complete":
                raise RuntimeError(followup_report.get("error", "会后任务创建失败"))
            proof["followup_result"] = followup_report
            proof["completed_steps"].append({"type": "followup", "task_id": followup_id})
            write_proof(path, proof)
        finally:
            stop.set()
            sampler.join(timeout=2)
        samples.append(device_sample(args))
        proof["ui_samples"] = summarize_samples(samples)
        proof["foreground_after"] = foreground(args)

        meeting_after = read_snapshot(args, target["event_id"])
        followup_after = read_snapshot(args, followup_report["event_id"])
        proof["meeting_after"] = meeting_after
        proof["followup_after"] = followup_after
        if int(meeting_after["dtstart"]) != chosen_start or int(meeting_after["dtend"]) != meeting_end:
            raise RuntimeError("改期事件独立回读不一致")
        if int(followup_after["dtstart"]) != meeting_end or int(followup_after["dtend"]) != followup_end:
            raise RuntimeError("会后任务独立回读不一致")
        if plan["reminder_minutes"] is not None:
            reminder_id = move_report.get("reminder_id")
            if not reminder_id:
                raise RuntimeError("改期子任务没有返回提醒 ID")
            proof["reminder"] = reminder_snapshot(args, reminder_id)
            if int(proof["reminder"]["event_id"]) != target["event_id"] or \
                    int(proof["reminder"]["minutes"]) != plan["reminder_minutes"]:
                raise RuntimeError("提醒独立回读不一致")
        if proof["ui_samples"]["agent_became_foreground"]:
            raise RuntimeError("批量执行期间 Agent 成为前台")
        if proof["foreground_after"] and proof["foreground_after"].startswith(PACKAGE):
            raise RuntimeError("批量执行后 Agent 成为前台")

        proof["state"] = "complete"
        proof["verified"] = True
        print("批量事务完成：改期、提醒、会后任务均已独立回读，Agent 未成为前台。")
        print(json.dumps({"move": move_report, "followup": followup_report}, ensure_ascii=False, indent=2))
        print(f"整体撤销：python3 tools/batch_agent.py --task-id {args.task_id} --undo")
        return_code = 0
    except (OSError, RuntimeError, ValueError, KeyError, json.JSONDecodeError) as error:
        proof["error"] = str(error)
        if proof.get("completed_steps"):
            proof["compensation"] = compensate(args, proof["completed_steps"])
            proof["state"] = "rolled_back" if compensation_succeeded(proof["compensation"]) else "rollback_failed"
        else:
            proof["state"] = "failed"
        print(f"error: {error}", file=sys.stderr)
    finally:
        proof["finished_at_utc"] = dt.datetime.now(dt.timezone.utc).isoformat()
        write_proof(path, proof)
        print(f"本地批量日志：{path}")
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
