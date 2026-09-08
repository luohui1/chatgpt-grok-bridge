"""Explicit live verification: consumes a few real Grok turns in its own fixture."""
import asyncio
import json
import os
from pathlib import Path
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import server
import store


async def main():
    run_id = str(uuid.uuid4())
    workspace = store.DATA / "smoke" / run_id
    workspace.mkdir(parents=True)
    marker = "grok-proof-" + run_id[:8]
    report = {"run_id": run_id, "workspace": str(workspace), "checks": {}}
    live_jobs = []

    async def wait(job_id, predicate, seconds=180):
        deadline = time.monotonic() + seconds
        last_status = None
        while time.monotonic() < deadline:
            job = store.get(job_id)
            if job["status"] != last_status:
                print(f"{job_id[:8]}: {job['status']}", flush=True)
                last_status = job["status"]
            if predicate(job):
                return job
            if job["status"] in {"failed", "timeout"}:
                raise RuntimeError(json.dumps(job, ensure_ascii=True))
            if job["pending_permissions"]:
                # Live fixture grants only a single file-edit request it explicitly asked for.
                for permission in job["pending_permissions"]:
                    call = permission.get("toolCall", {})
                    if call.get("kind") != "edit":
                        raise RuntimeError("Live fixture requested unexpected permission: " + json.dumps(call))
                    locations = call.get("locations") or []
                    if locations and any(Path(item["path"]).resolve() != workspace / "proof.txt" for item in locations):
                        raise RuntimeError("Permission targets a path outside the fixture")
                    allowed = next((o for o in permission.get("options", []) if o.get("kind") == "allow_once"), None)
                    if not allowed:
                        raise RuntimeError("No allow-once permission option")
                    server.invoke("grok_permission", {
                        "job_id": job_id, "request_id": "permission-" + permission["permission_id"],
                        "permission_id": permission["permission_id"], "option_id": allowed["optionId"]})
                    report["checks"]["live_permission_handled"] = True
            await asyncio.sleep(0.5)
        raise TimeoutError("Live verification wait expired")

    try:
        # A real MCP client starts a job, then exits before Grok finishes.
        proc = await asyncio.create_subprocess_exec(
            sys.executable, str(ROOT / "scripts/server.py"),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **({"creationflags": 0x08000000} if os.name == "nt" else {}))

        async def request(rid, method, params):
            proc.stdin.write((json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}) + "\n").encode())
            await proc.stdin.drain()
            return json.loads(await asyncio.wait_for(proc.stdout.readline(), 30))

        init = await request(1, "initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                                            "clientInfo": {"name": "live-test", "version": "1"}})
        assert "result" in init
        listing = await request(2, "tools/list", {})
        report["checks"]["mcp_tool_count"] = len(listing["result"]["tools"])
        start_time = time.monotonic()
        response = await request(3, "tools/call", {"name": "grok_start", "arguments": {
            "request_id": "live-start-" + run_id, "workspace": str(workspace),
            "prompt": f"Do not use any tools. Remember this exact marker for later: {marker}. Reply only READY.",
            "timeout_seconds": 180}})
        assert not response["result"].get("isError"), response
        job_id = response["result"]["structuredContent"]["job_id"]
        live_jobs.append(job_id)
        report["job_id"] = job_id
        report["checks"]["start_return_seconds"] = round(time.monotonic() - start_time, 3)
        proc.stdin.close()
        await asyncio.wait_for(proc.wait(), 10)
        first = await wait(job_id, lambda j: j["status"] == "completed")
        report["checks"]["survived_mcp_disconnect"] = "READY" in first["answer_tail"]
        report["agent_version"] = first.get("agent_version")
        report["model"] = first.get("model_state", {}).get("currentModelId")
        server.invoke("grok_send", {"job_id": job_id, "request_id": "recall",
                                  "prompt": "Without tools, reply only with the exact marker I asked you to remember."})
        recalled = await wait(job_id, lambda j: j["turn"] == 2 and j["status"] == "completed")
        assert marker in recalled["answer_tail"], recalled["answer_tail"]
        report["checks"]["same_session_followup"] = True

        server.invoke("grok_send", {"job_id": job_id, "request_id": "cancel-test",
                                  "prompt": "Do not use tools. Write 1000 long numbered paragraphs about arithmetic."})
        await wait(job_id, lambda j: j["turn"] == 3 and j["status"] == "running")
        began_cancel = time.monotonic()
        server.invoke("grok_interrupt", {"job_id": job_id, "request_id": "cancel-turn"})
        cancelled = await wait(job_id, lambda j: j["status"] == "cancelled")
        report["checks"]["cancel_seconds"] = round(time.monotonic() - began_cancel, 3)
        report["checks"]["cancelled_session_still_live"] = not cancelled["worker_closed"]

        server.invoke("grok_send", {"job_id": job_id, "request_id": "write-proof",
            "prompt": f"Use the native file write/edit tool to create only {workspace / 'proof.txt'} containing exactly {marker}. "
                      "Then use a read tool to check it. Do not run shell commands, network tools, or subagents. Reply VERIFIED."})
        await wait(job_id, lambda j: j["turn"] == 4 and j["status"] == "completed")
        # This file is a generated test assertion, not workspace discovery.
        assert (workspace / "proof.txt").read_text(encoding="utf-8").strip() == marker
        report["checks"]["file_write_and_read"] = True
        session_id = store.get(job_id)["session_id"]
        server.invoke("grok_close", {"job_id": job_id, "request_id": "close-original"})
        await wait(job_id, lambda j: j["worker_closed"])
        report["checks"]["closed"] = True

        resumed = server.invoke("grok_start", {
            "request_id": "resume-" + run_id, "workspace": str(workspace), "resume_session_id": session_id,
            "prompt": "Without tools, reply only with the exact marker I asked you to remember.", "timeout_seconds": 180})
        live_jobs.append(resumed["job_id"])
        after = await wait(resumed["job_id"], lambda j: j["status"] == "completed")
        assert marker in after["answer_tail"], after["answer_tail"]
        report["checks"]["resume_after_process_close"] = True
        report["status"] = "passed"
    except Exception as exc:
        report["status"] = "failed"
        report["error"] = str(exc)
    finally:
        for job_id in live_jobs:
            job = store.get(job_id)
            if not job.get("worker_closed"):
                try:
                    store.command(job_id, "final-close-" + run_id, "close")
                    await wait(job_id, lambda j: j["worker_closed"], 20)
                except Exception:
                    pass
        evidence = ROOT / "evidence"
        evidence.mkdir(exist_ok=True)
        (evidence / "live-smoke.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=True, indent=2), flush=True)
    if report["status"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
