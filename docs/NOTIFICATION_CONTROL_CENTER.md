# 通知驱动、风险分级与无打扰控制台

这条链路让手机通知成为 Agent 的任务入口，但不会让通知直接获得执行权限：

```text
白名单通知 → 手机私有收件箱 → 本地确定性风险策略
                             ├─ 无关：忽略
                             ├─ 高风险：阻断
                             └─ 日历写入：电脑控制台等待确认
                                               ↓ 用户点击确认
                                      LLM 严格规划 → 手机后台执行
                                               ↓
                                      独立回读 / 撤销 / 审计
```

## 1. 安装并授权

构建并安装 0.4.0 debug APK：

```bash
export JAVA_HOME=/opt/homebrew/opt/openjdk@17/libexec/openjdk.jdk/Contents/Home
export ANDROID_HOME=/opt/homebrew/share/android-commandlinetools
./gradlew :app:assembleDebug
adb -d install -r app/build/outputs/apk/debug/app-debug.apk
```

打开 Quiet Calendar Agent，点击“打开通知使用权设置”，只给它授予通知使用权。也可以在仅用于调试的手机上执行：

```bash
adb -d shell cmd notification allow_listener \
  dev.qajua.quietcalendar/dev.qajua.quietcalendar.QuietNotificationListener
```

通知监听默认白名单为空。演示用 ADB 测试通知来自 `com.android.shell`：

```bash
python3 tools/notification_bridge.py configure com.android.shell
python3 tools/notification_bridge.py status
```

真实应用应使用它的 Android 包名，例如微信通常是 `com.tencent.mm`。只添加演示需要的来源，不建议允许全部应用。

## 2. 启动本地控制台

```bash
read -s "OPENAI_API_KEY?请输入 OpenAI API Key: "; export OPENAI_API_KEY; echo
export OPENAI_MODEL="gpt-5-mini"

python3 tools/control_center.py --calendar-id 1 --open
```

控制台只绑定 `127.0.0.1`，每次启动产生新的随机控制令牌。手机通知保存在 App 私有目录；电脑状态保存在被 Git 忽略的 `work/control-center/`。

风险策略在调用模型之前运行：

- 无关通知：忽略，不发送给模型；
- 只读日程意图：低风险展示，目前不执行；
- 创建或改期：中风险，等待控制台确认；
- 删除、取消、对外发消息、邀请、付款、密码等：高风险阻断，不能在控制台批准。

点击“确认并执行”意味着允许把该条通知正文发送给配置的 OpenAI 规划器，并允许确定性执行器修改 Agent 可安全识别的日历事件。手机不显示确认框，也不会切换前台。

## 3. 发送测试通知

先创建 Agent 所有的测试会议：

```bash
python3 tools/quiet_cli.py create --task-id notify-seed-1 --calendar-id 1 \
  --title "客户复盘" --start "2026-09-24T14:00:00+10:00" --minutes 30
```

发送复合任务通知：

```bash
python3 tools/notification_bridge.py test --title "日程助理" \
  --text "把本周四下午两点的客户复盘改到本周五下午四点；如果冲突就安排到之后最近空档，提前20分钟提醒，并在会议结束后创建一个30分钟的整理会议纪要"
```

通知应在约 2 秒内出现在控制台，并显示 `MEDIUM · needs_confirmation`。点击确认后，控制台会依次显示执行中、完成和整体撤销入口。

高风险阻断测试：

```bash
python3 tools/notification_bridge.py test --title "项目群" \
  --text "取消项目周会并发送消息通知参会人"
```

这条通知应显示 `HIGH · blocked`，且没有确认按钮。

演示结束后：

```bash
python3 tools/notification_bridge.py clear
python3 tools/quiet_cli.py cleanup --task-id notify-seed-1
```

## 安全边界

- 通知监听和 ADB Provider 只是 debug 演示入口，不是生产网络 API。
- 白名单由随机 ADB bridge token 保护，默认拒绝所有来源。
- 通知不会因为“看起来像命令”就直接执行；中风险需要电脑端显式批准。
- 高风险规则是演示用的第一道确定性防线，不应替代生产系统中的身份、组织策略、审计和更完整的授权模型。
- 当前自动执行只覆盖日历改期、冲突避让、提醒和会后任务；其他意图保持忽略、只读或阻断。
