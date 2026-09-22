# Smart conflict demo

This scenario proves that the quiet execution path can make a multi-step decision without taking the phone UI.

```text
Natural-language constraints → strict JSON plan → read busy intervals
→ deterministic nearest-slot selection → guarded event update + reminder
→ independent read-back → undo time and Agent-created reminder
```

Prepare an Agent-owned target and a blocker:

```bash
python3 tools/quiet_cli.py create --task-id smart-target-demo --calendar-id 1 \
  --title "项目周会" --start "2026-09-24T14:00:00+10:00" --minutes 30
python3 tools/quiet_cli.py create --task-id smart-blocker-demo --calendar-id 1 \
  --title "团队同步" --start "2026-09-25T16:00:00+10:00" --minutes 30
```

Run while typing in another app:

```bash
export OPENAI_MODEL="gpt-5-mini"
python3 tools/smart_agent.py --calendar-id 1 --task-id smart-move-demo \
  --reference-time "2026-09-22T10:00:00+10:00" --delay 10 --execute \
  --text "把本周四下午两点的项目周会改到本周五下午四点；如果冲突就安排到当天之后最近的空档，并提前20分钟提醒我"
```

Expected decision: the 16:00 blocker is detected, so the 30-minute target moves to 16:30 with a 20-minute alert. The proof under ignored `work/smart-proofs/` records conflicts, chosen slot, before/after fields, reminder read-back, foreground components, and keyboard samples.

Undo and clean up:

```bash
python3 tools/quiet_cli.py undo --task-id smart-move-demo
python3 tools/quiet_cli.py cleanup --task-id smart-target-demo
python3 tools/quiet_cli.py cleanup --task-id smart-blocker-demo
```

Current scope deliberately excludes recurring-event instances and all-day events; ambiguous input fails closed.
