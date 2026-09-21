# Quiet Calendar Agent

Debug-stage Android proof of concept: a computer asks the **same phone** to create a calendar event while another app remains foreground. The phone writes through Calendar Provider, reads the record back, and returns a task result. No screen taps, accessibility service, or keyboard injection are used.

```text
Desktop CLI ─ADB content call─> debug-only provider ─> Calendar Provider
     ↑                       └─ verified result in app-private file ─┘
```

## Deploy and verify

1. Install JDK 17 and Android SDK platform/build-tools 35; set `ANDROID_HOME`. Run `./gradlew :app:assembleDebug`.
2. Connect one USB-debugging Android phone (`adb devices`) and run `adb -d install -r app/build/outputs/apk/debug/app-debug.apk`. Open the app **once** to grant calendar permissions; then switch to a chat or feed app.
3. `python3 tools/quiet_cli.py calendars` lists calendar IDs and access levels; choose a writable, disposable calendar where possible (access level ≥ 500). Run the commands below while continuing to use the phone:

```bash
python3 tools/quiet_cli.py create --task-id demo-001 --calendar-id 1 \
  --title "Quiet demo" --in-minutes 120 --minutes 30
python3 tools/quiet_cli.py status --task-id demo-001
python3 tools/quiet_cli.py cleanup --task-id demo-001
```

Use a **new task ID** each time. For deduplication, repeat `create` with the same ID and the same fixed `--start` timestamp (`--in-minutes` recalculates on each run). The CLI defaults to the USB phone; add `--target emulator` for an emulator. It obtains a random token from the debug app's private storage via ADB `run-as`; no token is committed to Git.

For a filmed simultaneous-use test, run `python3 tools/live_demo.py --calendar-id 1` while the phone is unlocked. You have 10 seconds to switch to a chat app and keep typing. The script samples the foreground app and keyboard during execution, independently reads every event field, saves a local record under ignored `work/demo-proofs/`, and prints a separate cleanup command. **Record the phone and terminal together; the automated record cannot prove the keyboard never flickered or that typing felt uninterrupted.**

## Safe reschedule notice

Create a disposable Agent-owned event, preview an absolute-date Chinese notice, then explicitly execute it:

```bash
python3 tools/quiet_cli.py create --task-id weekly-demo --calendar-id 1 \
  --title "项目周会" --start "2026-09-23T15:00:00+10:00" --minutes 30
python3 tools/notice_agent.py --calendar-id 1 --task-id move-demo \
  --text "把项目周会从 2026-09-23 15:00 改到 2026-09-25 16:00"
python3 tools/notice_agent.py --calendar-id 1 --task-id move-demo \
  --text "把项目周会从 2026-09-23 15:00 改到 2026-09-25 16:00" --execute
python3 tools/quiet_cli.py undo --task-id move-demo
python3 tools/quiet_cli.py cleanup --task-id weekly-demo
```

By default, the command uses the deterministic absolute-date parser when model credentials are absent. To understand freer wording and relative dates, configure a model and select the schema-constrained planner:

```bash
export OPENAI_API_KEY="your project key"
export OPENAI_MODEL="a Responses API model with Structured Outputs"
python3 tools/notice_agent.py --calendar-id 1 --task-id move-natural-1 \
  --planner openai --plan-only --reference-time "2026-09-21T10:00:00+10:00" \
  --text "把周三下午三点的项目周会改到周五下午四点"
```

`--plan-only` stops before any ADB access. Without it, the command reads the phone to match one event but is still a non-writing preview until `--execute` is added. The model can only return a strict JSON plan: `ready`, `needs_confirmation`, or `unsupported`; it has no phone tools. A `ready` plan must then pass the existing deterministic executor checks: one exact event, Agent-owned marker, preserved duration, compare-before-write, independent read-back, and a local proof under ignored `work/notice-proofs/`. `auto` (the default) chooses the model only when both model variables exist; `--planner deterministic` always stays offline.

## Scope and decisions

Validated on Android 15 emulator and Huawei Mate 40 Pro / HarmonyOS 4.2: create, independent read-back, same-ID replay, and guarded cleanup. Huawei blocked background broadcasts, so the debug bridge uses a synchronous ContentProvider call; it starts the app process without opening its Activity. The provider exists **only in debug builds**. Calendar writes are tagged by task ID; cleanup refuses to delete a record whose fields changed.

This is not yet a durable queue or production transport. The automated report proves the Agent Activity did not take foreground and independently checks the event; filmed typing remains the authority for uninterrupted input. Do not expose this debug bridge as a production API; a production transport needs proper authentication and scheduling.

Environment: `ANDROID_HOME` points to the Android SDK; `JAVA_HOME` points to JDK 17. The CLI uses `ANDROID_HOME/platform-tools/adb` or an `adb` on `PATH`. `OPENAI_API_KEY` and `OPENAI_MODEL` are optional and used only by the model planner; never commit the key.
