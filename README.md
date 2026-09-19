# Quiet Calendar Agent

An Android proof of concept for a phone agent that completes calendar work while the phone owner uses other apps. **Current milestone:** a reversible on-device calendar compatibility probe. Image understanding, background execution, and remote triggering are not implemented yet.

```text
Future: task console → planner → Android app → Calendar Provider → read-back proof
Current: Android probe UI ──────────────→ Calendar Provider → read-back proof
```

## Build and run

1. Install JDK 17 and Android SDK platform 35/build-tools 35.0.0. Set `ANDROID_HOME` to the SDK directory.
2. Run `./gradlew :app:assembleDebug`.
3. Start an Android emulator or connect an Android phone with USB debugging. Check `adb devices`, then run `adb install -r app/build/outputs/apk/debug/app-debug.apk`.
4. Open **Quiet Calendar Agent**. Grant calendar permissions, inspect the list of writable calendars, choose one, create the test event, verify it, and delete it. Nothing is written before you tap **Create**.

## Environment variables

None for this milestone. Do not place API keys in the Android app. Future model and relay secrets will be configured server-side.

## Scope and next gate

The probe must pass on both an emulator and the Huawei Mate 40 Pro (HarmonyOS 4.2) before agent logic is added. The probe does not run in the background or prove non-interference yet. Next: ingest explicitly shared notices, reconcile changes against current calendar state, then verify the final record without touching the foreground screen or keyboard.
