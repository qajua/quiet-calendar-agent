#!/usr/bin/env python3
"""Deterministic calendar conflict detection and free-slot selection."""

from dataclasses import dataclass
import re

from notice_agent import read_field
from quiet_cli import run_adb


@dataclass(frozen=True)
class BusyEvent:
    event_id: int
    title: str
    start_ms: int
    end_ms: int


def overlaps(start_a, end_a, start_b, end_b):
    """Half-open intervals: touching endpoints are not a conflict."""
    return start_a < end_b and end_a > start_b


def conflicts_for(start_ms, end_ms, busy):
    return [event for event in busy if overlaps(start_ms, end_ms, event.start_ms, event.end_ms)]


def choose_slot(requested_start_ms, duration_ms, busy, window_end_ms, policy):
    if duration_ms <= 0:
        raise ValueError("事件时长必须大于零")
    if window_end_ms <= requested_start_ms:
        raise ValueError("搜索截止时间必须晚于目标开始时间")
    if policy not in {"reject", "next_available"}:
        raise ValueError(f"未知冲突策略：{policy}")

    candidate = requested_start_ms
    encountered = []
    while candidate + duration_ms <= window_end_ms:
        collisions = conflicts_for(candidate, candidate + duration_ms, busy)
        if not collisions:
            return candidate, encountered
        encountered.extend(event for event in collisions if event not in encountered)
        if policy == "reject":
            raise RuntimeError("目标时间与已有日程冲突；策略要求停止")
        candidate = max(event.end_ms for event in collisions)
    raise RuntimeError("指定搜索范围内没有足够长的可用时间")


def query_busy_events(args, calendar_id, start_ms, end_ms, exclude_event_id=None):
    result = run_adb(
        args, "shell", "content", "query", "--uri", "content://com.android.calendar/events",
        "--projection", "_id", "--where",
        f"calendar_id={calendar_id} AND dtstart < {end_ms} AND dtend > {start_ms}",
    ).stdout
    ids = [int(value) for value in re.findall(r"_id=(\d+)", result)]
    events = []
    for event_id in ids:
        if event_id == exclude_event_id:
            continue
        event_start = int(read_field(args, event_id, "dtstart"))
        event_end = int(read_field(args, event_id, "dtend"))
        if event_end <= event_start:
            continue
        events.append(BusyEvent(
            event_id=event_id,
            title=read_field(args, event_id, "title"),
            start_ms=event_start,
            end_ms=event_end,
        ))
    return sorted(events, key=lambda event: (event.start_ms, event.end_ms, event.event_id))
