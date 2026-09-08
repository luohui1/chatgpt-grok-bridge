"""Local end-to-end ChatGPT adapter test; not a cloud ChatGPT connection test."""
import asyncio
import json
import os
from pathlib import Path
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import store


async def main():
    run_id = str(uuid.uuid4())
    folder = store.DATA / "chatgpt-smoke" / run_id
    workspace = folder / "workspace"
    workspace.mkdir(parents=True)
    config = folder / "policy.json"
    scope = "chatgpt:" + run_id
    config.write_text(json.dumps({
        "schema_version": 1, "client_scope": scope,
        "workspaces": {"fixture": {"path": str(workspace), "allow_execution": True}},
    }), encoding="utf-8")
    report = {"run_id": run_id, "checks": {}, "cloud_chatgpt_tested": False}
    proc = await asyncio.create_subprocess_exec(
        sys.executable, str(ROOT / "scripts/chatgpt_server.py"),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        limit=16 * 1024 * 1024,
        env=dict(os.environ, GROK_CHATGPT_CONFIG=str(config)),
        **({"creationflags": 0x08000000} if os.name == "nt" else {}))
    counter = 0
    job_id = None

    async def request(method, params):
        nonlocal counter
        counter += 1
        proc.stdin.write((json.dumps({"jsonrpc": "2.0", "id": counter, "method": method,
                                     "params": params}) + "\n").encode())
        await proc.stdin.drain()
        result = json.loads(await asyncio.wait_for(proc.stdout.readline(), 30))
        assert result["id"] == counter, result
        return result["result"]

    async def tool(name, args):
        result = await request("tools/call", {"name": name, "arguments": args})
        if result.get("isError"):
            raise RuntimeError(str(result["content"]))
        return result["structuredContent"]

    try:
        init = await request("initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                                           "clientInfo": {"name": "chatgpt-live-fixture", "version": "1"}})
        report["checks"]["instructions_present"] = bool(init.get("instructions"))
        report["optional_icon_present"] = bool(init["serverInfo"].get("icons"))
        available = await tool("grok_workspaces", {})
        report["checks"]["workspace_alias"] = available["workspaces"][0]["name"] == "fixture"
        start = await tool("grok_start", {
            "request_id": "chatgpt-live-" + run_id, "workspace": "fixture",
            "prompt": "Do not use tools. Reply exactly CHATGPT_BRIDGE_OK.", "timeout_seconds": 120})
        job_id = start["job_id"]
        report["job_id"] = job_id
        report["checks"]["no_unusable_local_result_path"] = "result_path" not in start
        deadline = time.monotonic() + 150
        while time.monotonic() < deadline:
            value = await tool("grok_read", {"job_id": job_id, "wait_seconds": 1})
            job = value["job"]
            if job["status"] == "completed":
                break
            if job["status"] in {"failed", "timeout"}:
                raise RuntimeError(job.get("error", job["status"]))
            await asyncio.sleep(0.25)
        else:
            raise TimeoutError("No completed Grok result")
        result = await tool("grok_result", {"job_id": job_id})
        report["checks"]["full_result_available"] = "CHATGPT_BRIDGE_OK" in result["text"]
        report["checks"]["scope_inherited_by_real_worker"] = store.get(job_id)["client_scope"] == scope
        jobs = await tool("grok_list", {})
        report["checks"]["only_own_job_visible"] = [job["job_id"] for job in jobs["jobs"]] == [job_id]
        await tool("grok_close", {"job_id": job_id, "request_id": "close-" + run_id})
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if store.get(job_id)["worker_closed"]:
                report["checks"]["closed"] = True
                break
            await asyncio.sleep(0.25)
        else:
            raise TimeoutError("Fixture worker did not close")
        report["status"] = "passed" if all(report["checks"].values()) else "failed"
    except Exception as exc:
        report.update(status="failed", error=str(exc))
    finally:
        if job_id and not store.get(job_id).get("worker_closed"):
            store.command(job_id, "final-cleanup-" + run_id, "close")
        proc.stdin.close()
        try:
            await asyncio.wait_for(proc.wait(), 10)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
        output = ROOT / "evidence/chatgpt-local-smoke-2026-09-08.json"
        output.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report, indent=2))
    if report.get("status") != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
