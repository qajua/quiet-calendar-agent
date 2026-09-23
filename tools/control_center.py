#!/usr/bin/env python3
"""Local visual control center for notification-triggered Quiet Calendar Agent tasks."""

import argparse
import datetime as dt
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qs, quote, unquote, urlparse
import webbrowser

from notification_bridge import add_connection_args, listener_status, read_config, read_inbox
from risk_policy import classify_request


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_STATE = ROOT / "work" / "control-center" / "state.json"
BATCH_AGENT = Path(__file__).resolve().parent / "batch_agent.py"
SMART_AGENT = Path(__file__).resolve().parent / "smart_agent.py"
NOTICE_AGENT = Path(__file__).resolve().parent / "notice_agent.py"
QUIET_CLI = Path(__file__).resolve().parent / "quiet_cli.py"


class ControlState:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.data = self._load()

    def _load(self):
        if not self.path.exists():
            return {"version": 1, "tasks": [], "phone": {}, "updated_at": None}
        try:
            value = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError(f"控制台状态文件损坏：{self.path}") from error
        if not isinstance(value, dict) or not isinstance(value.get("tasks"), list):
            raise RuntimeError(f"控制台状态文件格式无效：{self.path}")
        return value

    def _write(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        self.data["updated_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        temporary.write_text(json.dumps(self.data, ensure_ascii=False, indent=2) + "\n")
        temporary.replace(self.path)

    def snapshot(self):
        with self.lock:
            return json.loads(json.dumps(self.data))

    def update_phone(self, value):
        with self.lock:
            self.data["phone"] = value
            self._write()

    def ingest(self, notifications):
        added = 0
        with self.lock:
            known = {item["notification_id"] for item in self.data["tasks"]}
            for notification in notifications:
                notification_id = str(notification.get("id", ""))
                if not notification_id or notification_id in known:
                    continue
                title = str(notification.get("title", ""))
                text = str(notification.get("text", ""))
                policy = classify_request(title, text)
                self.data["tasks"].append({
                    "id": f"notify-{notification_id[:24]}",
                    "notification_id": notification_id,
                    "source_package": str(notification.get("package", "")),
                    "posted_at_ms": int(notification.get("posted_at_ms", 0)),
                    "title": title,
                    "text": text,
                    "request": text.strip() or title.strip(),
                    "risk": policy["risk"],
                    "policy_reason": policy["reason"],
                    "supported": policy["supported"],
                    "executor": policy["executor"],
                    "status": policy["decision"],
                    "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                })
                known.add(notification_id)
                added += 1
            if added:
                self.data["tasks"] = self.data["tasks"][-200:]
                self._write()
        return added

    def transition(self, task_id, allowed, status, **fields):
        with self.lock:
            task = next((item for item in self.data["tasks"] if item["id"] == task_id), None)
            if task is None:
                raise KeyError("任务不存在")
            if task["status"] not in allowed:
                raise ValueError(f"任务状态 {task['status']} 不允许此操作")
            task["status"] = status
            task.update(fields)
            task["updated_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
            self._write()
            return json.loads(json.dumps(task))


class Controller:
    def __init__(self, args, state):
        self.args = args
        self.state = state
        self.stop = threading.Event()

    def poll_once(self):
        try:
            inbox = read_inbox(self.args)
            self.state.ingest(inbox)
            self.state.update_phone({
                "connected": True,
                "listener": listener_status(self.args),
                "config": read_config(self.args),
                "captured_count": len(inbox),
                "last_poll": dt.datetime.now(dt.timezone.utc).isoformat(),
                "error": None,
            })
        except (OSError, RuntimeError, ValueError) as error:
            self.state.update_phone({
                "connected": False,
                "last_poll": dt.datetime.now(dt.timezone.utc).isoformat(),
                "error": str(error),
            })

    def poll_forever(self):
        while not self.stop.is_set():
            self.poll_once()
            self.stop.wait(self.args.poll_interval)

    def approve(self, task_id):
        task = self.state.transition(
            task_id, {"needs_confirmation"}, "executing",
            approved_at=dt.datetime.now(dt.timezone.utc).isoformat(),
        )
        threading.Thread(target=self._execute, args=(task,), daemon=True).start()

    def reject(self, task_id):
        self.state.transition(
            task_id, {"needs_confirmation"}, "rejected",
            rejected_at=dt.datetime.now(dt.timezone.utc).isoformat(),
        )

    def undo(self, task_id):
        task = self.state.transition(task_id, {"complete"}, "undoing")
        threading.Thread(target=self._undo, args=(task,), daemon=True).start()

    def _connection_command(self):
        command = []
        if self.args.serial:
            command += ["--serial", self.args.serial]
        else:
            command += ["--target", self.args.target]
        if self.args.adb:
            command += ["--adb", self.args.adb]
        command += ["--timeout", str(self.args.timeout)]
        return command

    def _execute(self, task):
        executor = task.get("executor")
        script = {"batch": BATCH_AGENT, "smart": SMART_AGENT, "notice": NOTICE_AGENT}.get(executor)
        if script is None:
            self.state.transition(task["id"], {"executing"}, "failed", output="没有可用执行器")
            return
        command = [sys.executable, str(script), *self._connection_command()]
        command += [
            "--calendar-id", str(self.args.calendar_id), "--task-id", task["id"],
            "--timezone", self.args.timezone, "--text", task["request"], "--execute",
        ]
        if executor == "notice":
            command += ["--planner", "openai"]
        if self.args.model:
            command += ["--model", self.args.model]
        self._run_and_finish(task["id"], command, "execute")

    def _undo(self, task):
        if task.get("executor") == "batch":
            command = [
                sys.executable, str(BATCH_AGENT), *self._connection_command(),
                "--task-id", task["id"], "--undo",
            ]
        else:
            command = [
                sys.executable, str(QUIET_CLI), *self._connection_command(),
                "undo", "--task-id", task["id"],
            ]
        self._run_and_finish(task["id"], command, "undo")

    def _run_and_finish(self, task_id, command, kind):
        try:
            result = subprocess.run(
                command, cwd=ROOT, text=True, capture_output=True,
                timeout=self.args.execution_timeout, env=os.environ.copy(), check=False,
            )
            output = (result.stdout + ("\n" + result.stderr if result.stderr else ""))[-30_000:]
            if kind == "execute":
                status = "complete" if result.returncode == 0 else (
                    "needs_clarification" if result.returncode == 2 else "failed"
                )
                allowed = {"executing"}
            else:
                status = "undone" if result.returncode == 0 else "undo_failed"
                allowed = {"undoing"}
            self.state.transition(
                task_id, allowed, status, return_code=result.returncode,
                output=output, finished_at=dt.datetime.now(dt.timezone.utc).isoformat(),
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            allowed = {"executing"} if kind == "execute" else {"undoing"}
            status = "failed" if kind == "execute" else "undo_failed"
            self.state.transition(task_id, allowed, status, output=str(error))


PAGE = r'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Quiet Agent Control Center</title>
<style>
:root{color-scheme:dark;--bg:#07110f;--panel:#10211d;--line:#25463c;--mint:#62f2bd;--gold:#ffd166;--red:#ff6b6b;--muted:#93aaa2}
*{box-sizing:border-box}body{margin:0;font:15px/1.55 ui-sans-serif,system-ui;background:radial-gradient(circle at 15% 0,#17382d 0,transparent 35%),var(--bg);color:#eef9f4}
main{max-width:1120px;margin:auto;padding:34px 20px 70px}header{display:flex;justify-content:space-between;gap:20px;align-items:end;margin-bottom:26px}h1{font-size:31px;margin:0}.eyebrow{color:var(--mint);letter-spacing:.14em;text-transform:uppercase;font-size:12px}.sub{color:var(--muted);margin:5px 0 0}
.status{padding:9px 14px;border:1px solid var(--line);border-radius:999px}.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin:18px 0}.metric,.task{background:rgba(16,33,29,.9);border:1px solid var(--line);border-radius:16px;padding:17px;box-shadow:0 14px 40px #0004}.metric b{display:block;font-size:26px}.metric span{color:var(--muted)}
.toolbar{display:flex;justify-content:space-between;align-items:center;margin:28px 0 12px}.task{margin-bottom:13px}.task-head{display:flex;justify-content:space-between;gap:10px}.badge{font-size:12px;padding:4px 9px;border-radius:999px;border:1px solid var(--line)}.medium{color:var(--gold)}.high{color:var(--red)}.low{color:var(--mint)}.none{color:var(--muted)}
.meta,.reason{color:var(--muted);font-size:13px}.message{white-space:pre-wrap;margin:12px 0}.actions{display:flex;gap:9px}button{border:1px solid var(--line);background:#18362d;color:#effff8;border-radius:9px;padding:8px 13px;cursor:pointer}button.primary{background:var(--mint);color:#052118;border-color:var(--mint);font-weight:700}button.danger{color:#ffb0b0}pre{max-height:240px;overflow:auto;background:#07110f;padding:12px;border-radius:9px;color:#bfe4d6;white-space:pre-wrap}.empty{color:var(--muted);padding:30px;text-align:center;border:1px dashed var(--line);border-radius:15px}@media(max-width:700px){.grid{grid-template-columns:1fr}header{display:block}.status{display:inline-block;margin-top:12px}}
</style></head><body><main>
<header><div><div class="eyebrow">Notification-driven · Human-approved</div><h1>Quiet Agent Control Center</h1><p class="sub">手机保持在用户手里；这里负责风险判断、静默审批、执行证据与撤销。</p></div><div class="status" id="phone">正在连接手机…</div></header>
<section class="grid"><div class="metric"><b id="captured">0</b><span>已采集白名单通知</span></div><div class="metric"><b id="pending">0</b><span>等待无打扰确认</span></div><div class="metric"><b id="complete">0</b><span>已验证完成</span></div></section>
<div class="toolbar"><h2>任务队列</h2><button id="refresh">立即刷新</button></div><section id="tasks"></section>
</main><script>
const token=__TOKEN__; const api=(path,opts={})=>fetch(path,{...opts,headers:{...(opts.headers||{}),'X-Control-Token':token}}).then(async r=>{const j=await r.json();if(!r.ok)throw Error(j.error||r.status);return j});
const el=(tag,cls,text)=>{const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=text;return n};
const outputScrollPositions=new Map();
function captureOutputScroll(root){root.querySelectorAll('.task[data-task-id]').forEach(box=>{const output=box.querySelector('pre');if(output)outputScrollPositions.set(box.dataset.taskId,output.scrollTop)})}
function restoreOutputScroll(root){root.querySelectorAll('.task[data-task-id]').forEach(box=>{const output=box.querySelector('pre');const position=outputScrollPositions.get(box.dataset.taskId);if(output&&position!==undefined)output.scrollTop=position})}
async function act(id,action){try{await api('/api/tasks/'+encodeURIComponent(id)+'/'+action,{method:'POST'});await load()}catch(e){alert(e.message)}}
function renderTask(t){const box=el('article','task');box.dataset.taskId=t.id;const head=el('div','task-head');const left=el('div');left.append(el('strong','',t.title||'无标题通知'),el('div','meta',t.source_package+' · '+new Date(t.posted_at_ms).toLocaleString()+' · '+(t.executor||'不执行')));head.append(left,el('span','badge '+t.risk,t.risk.toUpperCase()+' · '+t.status));box.append(head,el('div','message',t.text||t.title),el('div','reason',t.policy_reason));const actions=el('div','actions');if(t.status==='needs_confirmation'){const yes=el('button','primary','确认并执行');yes.onclick=()=>act(t.id,'approve');const no=el('button','danger','拒绝');no.onclick=()=>act(t.id,'reject');actions.append(yes,no)}if(t.status==='complete'){const undo=el('button','','整体撤销');undo.onclick=()=>act(t.id,'undo');actions.append(undo)}if(actions.children.length)box.append(actions);if(t.output){const p=el('pre','',t.output);box.append(p)}return box}
async function load(){try{const s=await api('/api/state');const phone=s.phone||{};document.querySelector('#phone').textContent=phone.connected?(phone.listener&&phone.listener.enabled?'● 手机在线 · 通知权限已启用':'● 手机在线 · 等待通知权限'):'○ 手机未连接';document.querySelector('#captured').textContent=phone.captured_count||0;document.querySelector('#pending').textContent=s.tasks.filter(t=>t.status==='needs_confirmation').length;document.querySelector('#complete').textContent=s.tasks.filter(t=>t.status==='complete').length;const root=document.querySelector('#tasks');captureOutputScroll(root);root.replaceChildren();[...s.tasks].reverse().forEach(t=>root.append(renderTask(t)));if(!s.tasks.length)root.append(el('div','empty','尚未收到白名单中的日历任务通知。'));restoreOutputScroll(root);requestAnimationFrame(()=>restoreOutputScroll(root))}catch(e){document.querySelector('#phone').textContent='连接错误：'+e.message}}
document.querySelector('#refresh').onclick=load;load();setInterval(load,2000);
</script></body></html>'''


def make_handler(controller, token):
    page = PAGE.replace("__TOKEN__", json.dumps(token, ensure_ascii=False)).encode()

    class Handler(BaseHTTPRequestHandler):
        def _authorized(self):
            query_token = parse_qs(urlparse(self.path).query).get("token", [None])[0]
            presented = self.headers.get("X-Control-Token") or query_token
            return presented is not None and secrets.compare_digest(presented, token)

        def _json(self, status, value):
            body = json.dumps(value, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            parsed = urlparse(self.path)
            if not self._authorized():
                self._json(403, {"error": "forbidden"})
                return
            if parsed.path == "/api/state":
                self._json(200, controller.state.snapshot())
            elif parsed.path == "/":
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(page)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'")
                self.send_header("X-Frame-Options", "DENY")
                self.end_headers()
                self.wfile.write(page)
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self):
            if not self._authorized():
                self._json(403, {"error": "forbidden"})
                return
            pieces = [unquote(piece) for piece in urlparse(self.path).path.split("/") if piece]
            if len(pieces) != 4 or pieces[:2] != ["api", "tasks"]:
                self._json(404, {"error": "not found"})
                return
            task_id, action = pieces[2], pieces[3]
            try:
                if action == "approve": controller.approve(task_id)
                elif action == "reject": controller.reject(task_id)
                elif action == "undo": controller.undo(task_id)
                else: raise ValueError("未知操作")
                self._json(202, {"state": "accepted"})
            except (KeyError, ValueError) as error:
                self._json(409, {"error": str(error)})

        def log_message(self, format, *args):
            return

    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    add_connection_args(parser)
    parser.add_argument("--calendar-id", type=int, required=True)
    parser.add_argument("--timezone", default="Australia/Sydney")
    parser.add_argument("--model", help="OpenAI model; defaults to OPENAI_MODEL")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--poll-interval", type=float, default=2.0)
    parser.add_argument("--execution-timeout", type=float, default=120.0)
    parser.add_argument("--state-file", default=str(DEFAULT_STATE))
    parser.add_argument("--open", action="store_true", help="Open the local dashboard in the default browser")
    args = parser.parse_args()
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        print("error: 控制台只允许绑定回环地址", file=sys.stderr)
        return 1
    if not 0.5 <= args.poll_interval <= 60 or not 10 <= args.execution_timeout <= 600:
        print("error: 轮询或执行超时范围无效", file=sys.stderr)
        return 1
    try:
        state = ControlState(args.state_file)
    except RuntimeError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    controller = Controller(args, state)
    controller.poll_once()
    poller = threading.Thread(target=controller.poll_forever, daemon=True)
    poller.start()
    token = secrets.token_urlsafe(24)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(controller, token))
    url = f"http://{args.host}:{args.port}/?token={quote(token)}"
    print(f"Quiet Agent 控制台：{url}")
    print("仅监听本机回环地址；按 Ctrl+C 停止。")
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        controller.stop.set()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
