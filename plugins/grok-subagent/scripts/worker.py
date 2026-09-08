"""One detached supervisor per Grok ACP session. No shared Grok leader."""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid

import store
import runtime
from owned_process import OwnedJob


class Worker:
    def __init__(self, job_id):
        self.job = store.get(job_id)
        self.pending = {}
        self.permissions = {}
        self.counter = 0
        self.proc = None
        self.turn_task = None
        self.stop = False
        self.replay = False
        self.last_activity = time.monotonic()
        self.cancel_deadline = None
        self.forced_reason = None
        self.turn_deadline = None
        self.owned_job = None
        self.initializing = True
        self.transport_closed = False

    def save(self, **changes):
        self.job.update(changes)
        store.save(self.job)

    def event(self, kind, data):
        store.event(self.job["job_id"], kind, data)

    async def write(self, message):
        if not self.proc or self.proc.returncode is not None:
            raise RuntimeError("Grok process is not running")
        self.proc.stdin.write((store.encode({"jsonrpc": "2.0", **message}) + "\n").encode("utf-8"))
        await self.proc.stdin.drain()

    async def request(self, method, params, timeout=60):
        self.counter += 1
        request_id = self.counter
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            await self.write({"id": request_id, "method": method, "params": params})
            return await asyncio.wait_for(future, timeout) if timeout else await future
        finally:
            self.pending.pop(request_id, None)

    async def response(self, request_id, result):
        await self.write({"id": request_id, "result": result})

    async def reader(self):
        try:
            while line := await self.proc.stdout.readline():
                message = json.loads(line)
                if "method" in message:
                    if "id" in message:
                        if message["method"] == "session/request_permission":
                            await self.permission(message)
                        else:
                            await self.write({"id": message["id"], "error": {
                                "code": -32601, "message": "Client capability is not supported"}})
                    elif message["method"] == "session/update":
                        self.update(message.get("params", {}))
                    continue
                future = self.pending.get(message.get("id"))
                if future and not future.done():
                    if "error" in message:
                        future.set_exception(RuntimeError(store.encode(message["error"])))
                    else:
                        future.set_result(message.get("result") or {})
        except Exception as exc:
            self.event("transport_error", {"message": str(exc)[:2000]})
        finally:
            self.transport_closed = True
            for future in list(self.pending.values()):
                if not future.done():
                    future.set_exception(RuntimeError("Grok ACP connection closed"))

    def update(self, params):
        update = params.get("update", {})
        kind = update.get("sessionUpdate")
        # Reasoning streams are intentionally not copied into the controller context.
        if kind == "agent_thought_chunk":
            return
        if self.replay:
            return
        if kind == "agent_message_chunk":
            text = update.get("content", {}).get("text", "")
            if text:
                with Path(self.job["result_path"]).open("a", encoding="utf-8") as out:
                    out.write(text)
                self.job["answer_tail"] = (self.job["answer_tail"] + text)[-12000:]
        raw = store.encode(update)
        self.event("acp_update", update if len(raw) <= 10000 else {
            "sessionUpdate": kind, "toolCallId": update.get("toolCallId"),
            "status": update.get("status"), "truncated": True, "preview": raw[:8000]})

    async def permission(self, message):
        params = message.get("params", {})
        if self.job["permission_policy"] == "deny" or self.cancel_deadline:
            await self.response(message["id"], {"outcome": {"outcome": "cancelled"}})
            self.event("permission_denied", {"toolCall": params.get("toolCall", {})})
            return
        permission_id = str(uuid.uuid4())
        self.permissions[permission_id] = {"rpc_id": message["id"], "params": params}
        self.save(status="awaiting_permission", pending_permissions=[
            {"permission_id": key, **value["params"]} for key, value in self.permissions.items()])
        self.event("permission_required", {"permission_id": permission_id, **params})

    async def deny_pending(self):
        for item in list(self.permissions.values()):
            await self.response(item["rpc_id"], {"outcome": {"outcome": "cancelled"}})
        self.permissions.clear()
        self.save(pending_permissions=[])

    async def cancel(self, reason):
        if self.turn_task and not self.turn_task.done():
            if self.cancel_deadline:
                # Repeated cancellation must not postpone the kill deadline.
                if reason == "closed":
                    self.forced_reason = reason
                return
            self.forced_reason = reason
            await self.deny_pending()
            await self.write({"method": "session/cancel", "params": {"sessionId": self.job["session_id"]}})
            self.cancel_deadline = time.monotonic() + 8
            self.save(status="cancelling")
            self.event("cancel_requested", {"reason": reason})
        elif reason == "closed":
            self.stop = True
            self.save(status="closed")

    async def turn(self, text):
        self.last_activity = time.monotonic()
        self.turn_deadline = self.last_activity + self.job["timeout_seconds"]
        self.forced_reason = None
        self.cancel_deadline = None
        self.save(status="running", turn=self.job["turn"] + 1, answer_tail="", error=None)
        with Path(self.job["result_path"]).open("a", encoding="utf-8") as out:
            out.write(f"\n\n--- Turn {self.job['turn']} ---\n\n")
        self.event("turn_started", {"turn": self.job["turn"]})
        try:
            response = await self.request("session/prompt", {
                "sessionId": self.job["session_id"],
                "prompt": [{"type": "text", "text": text}],
            }, timeout=None)
            reason = response.get("stopReason")
            status = self.forced_reason or (
                "completed" if reason == "end_turn" else
                "cancelled" if reason == "cancelled" else "failed")
            self.save(status=status, stop_reason=reason)
            self.event("turn_finished", {"status": status, "stop_reason": reason})
        except Exception as exc:
            self.save(status=self.forced_reason or "failed", error=str(exc)[:3000])
            self.event("turn_error", {"message": str(exc)[:3000]})
        finally:
            with contextlib.suppress(Exception):
                await self.deny_pending()
            self.cancel_deadline = None
            self.turn_deadline = None
            self.last_activity = time.monotonic()
            if self.forced_reason in {"closed", "timeout"}:
                self.stop = True

    async def apply_command(self, cmd):
        action = cmd["action"]
        if self.stop:
            raise ValueError("Worker is closing")
        active = self.turn_task and not self.turn_task.done()
        if action == "send":
            if active:
                raise ValueError("Turn is active. Interrupt it, wait for cancelled, then send the correction.")
            self.turn_task = asyncio.create_task(self.turn(cmd["prompt"]))
        elif action in {"interrupt", "close"}:
            await self.cancel("cancelled" if action == "interrupt" else "closed")
        elif action == "permission":
            permission_id = cmd["permission_id"]
            item = self.permissions.get(permission_id)
            if not item:
                raise ValueError("Permission is stale or already answered")
            option_id = cmd["option_id"]
            if option_id not in {o["optionId"] for o in item["params"].get("options", [])}:
                raise ValueError("option_id must exactly match one of the offered options")
            await self.response(item["rpc_id"], {"outcome": {"outcome": "selected", "optionId": option_id}})
            self.permissions.pop(permission_id)
            self.save(status="awaiting_permission" if self.permissions else "running",
                      pending_permissions=[{"permission_id": k, **v["params"]} for k, v in self.permissions.items()])
            self.event("permission_answered", {"permission_id": permission_id, "option_id": option_id})
        else:
            raise ValueError("Unknown action")
        self.last_activity = time.monotonic()
        return {"applied": True}

    async def control(self):
        heartbeat = 0
        while not self.stop:
            if self.transport_closed:
                raise RuntimeError("Grok ACP transport closed")
            if self.proc.returncode is not None:
                raise RuntimeError(f"Grok exited unexpectedly ({self.proc.returncode})")
            now = time.monotonic()
            if self.turn_deadline and now >= self.turn_deadline and not self.cancel_deadline:
                await self.cancel("timeout")
            if self.cancel_deadline and now >= self.cancel_deadline:
                self.event("forced_stop", {"reason": self.forced_reason})
                await self.kill_tree()
                self.stop = True
                break
            if now - heartbeat >= 2:
                self.save()
                heartbeat = now
            with store.connect() as db:
                commands = db.execute("SELECT seq,data FROM commands WHERE job=? AND result IS NULL ORDER BY seq",
                                      (self.job["job_id"],)).fetchall()
            for row in commands:
                try:
                    result = await self.apply_command(json.loads(row["data"]))
                    # Start the newly scheduled turn before processing another command.
                    await asyncio.sleep(0)
                except Exception as exc:
                    result = {"error": str(exc)}
                with store.connect() as db:
                    db.execute("UPDATE commands SET result=? WHERE seq=?", (store.encode(result), row["seq"]))
                self.event("command_result", {"command_id": row["seq"], **result})
            if (not self.turn_task or self.turn_task.done()) and now - self.last_activity > 300:
                self.stop = True
            await asyncio.sleep(0.1)

    async def kill_tree(self):
        if self.owned_job:
            self.owned_job.close()
            self.owned_job = None
            if os.name == "nt" and self.proc:
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self.proc.wait(), 5)
        if not self.proc or self.proc.returncode is not None:
            return
        if os.name == "nt":
            # Only the owned no-leader process tree, never `grok leader kill`.
            killer = await asyncio.create_subprocess_exec(
                "taskkill.exe", "/PID", str(self.proc.pid), "/T", "/F",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW)
            await asyncio.wait_for(killer.wait(), 10)
        else:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.proc.pid, signal.SIGKILL)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self.proc.wait(), 5)

    async def run(self):
        tasks = []
        self.save(status="starting", worker_pid=os.getpid(),
                  worker_birth=runtime.process_identity(os.getpid()).get("birth"))
        # Heartbeat during slow initialization as well as while running.
        async def pulse():
            while True:
                self.save()
                await asyncio.sleep(2)
        tasks.append(asyncio.create_task(pulse()))
        try:
            argv = [self.job["executable"], "--no-auto-update", "--no-subagents",
                    "agent", "--no-leader"]
            if self.job.get("model"):
                argv += ["--model", self.job["model"]]
            if self.job.get("effort"):
                argv += ["--effort", self.job["effort"]]
            argv += ["stdio"]
            kwargs = ({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt"
                      else {"start_new_session": True})
            self.proc = await asyncio.create_subprocess_exec(
                *argv, cwd=self.job["workspace"],
                env=runtime.launch_environment(self.job.get("integration_mode", "inherit")),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, limit=16 * 1024 * 1024, **kwargs)
            self.owned_job = OwnedJob(self.proc.pid)
            self.save(grok_pid=self.proc.pid,
                      grok_birth=runtime.process_identity(self.proc.pid).get("birth"))
            async def startup_control():
                while self.initializing:
                    with store.connect() as db:
                        rows = db.execute("SELECT seq,data FROM commands WHERE job=? AND result IS NULL ORDER BY seq",
                                          (self.job["job_id"],)).fetchall()
                    for row in rows:
                        action = json.loads(row["data"])["action"]
                        if action in {"interrupt", "close"}:
                            self.forced_reason = "closed" if action == "close" else "cancelled"
                            self.stop = True
                            self.save(status=self.forced_reason)
                            with store.connect() as db:
                                db.execute("UPDATE commands SET result=? WHERE seq=?",
                                           (store.encode({"applied": True, "during_startup": True}), row["seq"]))
                            await self.kill_tree()
                            return
                    await asyncio.sleep(0.1)
            tasks.append(asyncio.create_task(startup_control()))
            tasks.append(asyncio.create_task(self.reader()))
            # Drain stderr continuously; do not persist auth/diagnostic payloads.
            async def drain():
                while await self.proc.stderr.read(65536):
                    pass
            tasks.append(asyncio.create_task(drain()))
            init = await self.request("initialize", {
                "protocolVersion": 1, "clientInfo": {"name": "codex-grok-subagent", "version": runtime.VERSION},
                "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False}})
            if init.get("protocolVersion") != 1:
                raise RuntimeError("Unsupported ACP protocol version")
            capabilities = init.get("agentCapabilities", {})
            self.save(agent_version=init.get("_meta", {}).get("agentVersion"),
                      capabilities=capabilities)
            methods = {m["id"] for m in init.get("authMethods", [])}
            method = ("xai.api_key" if os.environ.get("XAI_API_KEY") and "xai.api_key" in methods
                      else "cached_token" if "cached_token" in methods else None)
            if not method:
                raise RuntimeError("No noninteractive authentication method available")
            await self.request("authenticate", {"methodId": method, "_meta": {"headless": True}})
            self.event("authenticated", {"method": method})
            params = {"cwd": self.job["workspace"], "mcpServers": []}
            previous = self.job.get("resume_session_id")
            if previous:
                params["sessionId"] = previous
                if "resume" in capabilities.get("sessionCapabilities", {}):
                    method = "session/resume"
                elif capabilities.get("loadSession"):
                    method = "session/load"
                else:
                    raise RuntimeError("This Grok build cannot resume sessions")
                self.replay = True
                try:
                    session = await self.request(method, params)
                finally:
                    self.replay = False
                session_id = previous
            else:
                session = await self.request("session/new", params)
                session_id = session["sessionId"]
            self.save(session_id=session_id, model_state=session.get("models", {}),
                      status="ready")
            self.initializing = False
            self.event("session_ready", {"session_id": session_id, "resumed": bool(previous)})
            # An early close/interrupt must take effect before sending any prompt.
            with store.connect() as db:
                early = [json.loads(r["data"])["action"] for r in db.execute(
                    "SELECT data FROM commands WHERE job=? AND result IS NULL", (self.job["job_id"],))]
            if "close" in early or "interrupt" in early:
                self.save(status="cancelled")
            else:
                self.turn_task = asyncio.create_task(self.turn(self.job["prompt"]))
                await asyncio.sleep(0)
            await self.control()
        except Exception as exc:
            self.save(status=self.forced_reason or "failed", error=str(exc)[:3000])
            self.event("worker_error", {"message": str(exc)[:3000]})
        finally:
            with contextlib.suppress(Exception):
                await self.deny_pending()
            # Close only this session, when the agent advertised that capability.
            if self.job.get("session_id") and "close" in self.job.get("capabilities", {}).get("sessionCapabilities", {}):
                with contextlib.suppress(Exception):
                    await self.request("session/close", {"sessionId": self.job["session_id"]}, timeout=3)
            cleanup_error = None
            try:
                await self.kill_tree()
            except Exception as exc:
                cleanup_error = str(exc)[:1000]
            if self.turn_task and not self.turn_task.done():
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await asyncio.wait_for(self.turn_task, 2)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            closed = (self.proc is None or self.proc.returncode is not None) and cleanup_error is None
            self.job.update(worker_closed=closed, pending_permissions=[])
            if not closed or cleanup_error:
                self.job.update(status="failed", error="Cleanup could not be confirmed: " + str(cleanup_error))
            store.finish(self.job)
            self.event("worker_closed" if closed else "cleanup_unconfirmed", {"status": self.job["status"]})


if __name__ == "__main__":
    asyncio.run(Worker(sys.argv[1]).run())
