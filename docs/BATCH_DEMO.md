# 多步骤批量任务演示

这个演示把一句自然语言请求变成一个可验证、可回滚的事务：

1. 找到指定的原会议；
2. 检查目标时间以及紧随其后的会后任务是否整体有空；
3. 必要时向后寻找最近的连续空档；
4. 在手机后台改期并添加提醒；
5. 紧接会议结束创建会后任务；
6. 独立回读会议、提醒和会后任务，并确认 Agent 从未成为前台。

任一步失败，已完成步骤会按相反顺序补偿。成功后也可以用一条命令整体撤销。

## 准备

手机通过 USB 连接并授权 ADB，Quiet Calendar Agent debug 版已安装，日历权限已授予：

```bash
adb devices -l
python3 tools/quiet_cli.py calendars
```

设置模型凭证（输入时终端不会回显）：

```bash
read -s "OPENAI_API_KEY?请输入 OpenAI API Key: "; export OPENAI_API_KEY; echo
export OPENAI_MODEL="gpt-5-mini"
```

## 执行

先确保手机中存在“本周四下午两点”的“客户复盘”，然后运行：

```bash
python3 tools/batch_agent.py \
  --calendar-id 1 \
  --task-id batch-demo-1 \
  --reference-time "2026-09-22T10:00:00+10:00" \
  --text "把本周四下午两点的客户复盘改到本周五下午四点；如果冲突就安排到之后最近空档，提前20分钟提醒，并在会议结束后创建一个30分钟的整理会议纪要" \
  --delay 10 \
  --execute
```

倒计时期间在手机上打开聊天应用并持续打字。命令完成后，终端会报告三个对象的独立回读结果与前台/键盘采样证据；完整日志位于 `work/batch-proofs/`。

## 整体撤销

```bash
python3 tools/batch_agent.py --task-id batch-demo-1 --undo
```

撤销顺序是：先删除会后任务，再把会议恢复到原时间并删除 Agent 创建的提醒。每次正式演示都应使用新的 `--task-id`，避免把旧任务误当成新任务重放。
