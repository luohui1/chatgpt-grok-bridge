"""Opt-in native integration/resource and supervisor-crash verification."""
import json
from pathlib import Path
import statistics
import sys
import time
import uuid

import psutil

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import server
import store
import runtime


def wait(job_id, condition, seconds=180):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        job = store.get(job_id)
        if condition(job):
            return job
        if job["status"] in {"failed", "timeout"}:
            raise RuntimeError(job.get("error", job["status"]))
        time.sleep(0.25)
    raise TimeoutError("Timed out waiting for fixture job")


def main():
    run_id = str(uuid.uuid4())
    workspace = store.DATA / "resource-smoke" / run_id
    workspace.mkdir(parents=True)
    report = {"run_id": run_id, "plugin_version": runtime.VERSION, "checks": {},
              "workspace": str(workspace)}
    job_id = None
    process = None
    grok = None
    try:
        started = time.monotonic()
        start = server.invoke("grok_start", {
            "request_id": "resources-" + run_id, "workspace": str(workspace),
            "prompt": "Do not use any tools. Reply exactly READY.",
            "integration_mode": "native", "timeout_seconds": 120,
        })
        job_id = start["job_id"]
        report["job_id"] = job_id
        report["start_return_s"] = time.monotonic() - started
        job = wait(job_id, lambda j: j["status"] == "completed")
        report["checks"]["real_prompt_completed"] = job["answer_tail"].strip() == "READY"
        process = psutil.Process(job["worker_pid"])
        grok = psutil.Process(job["grok_pid"])
        report["model"] = job.get("model_state", {}).get("currentModelId")
        report["grok_version"] = job.get("agent_version")
        time.sleep(30)
        samples = []
        for _ in range(25):
            children = process.children(recursive=True)
            live = [process, *children]
            rss = private = 0
            names = []
            for child in live:
                try:
                    mem = child.memory_info()
                    rss += mem.rss
                    private += getattr(mem, "private", mem.vms)
                    names.append(child.name())
                except psutil.Error:
                    pass
            samples.append({"rss_mib": rss / 1048576, "private_mib": private / 1048576,
                            "process_names": names})
            time.sleep(0.2)
        report["resources"] = {
            "tree_rss_mib_median": statistics.median(s["rss_mib"] for s in samples),
            "tree_private_mib_median": statistics.median(s["private_mib"] for s in samples),
            "process_names": samples[-1]["process_names"],
            "scope": "Supervisor plus Grok and all observed descendants; 30s settle, 5s sampling",
        }
        report["checks"]["no_observed_mcp_processes"] = all(
            name.lower() in {"python.exe", "grok.exe", "conhost.exe"}
            for s in samples for name in s["process_names"])
        if runtime.original_process_state(job["worker_pid"], job["worker_birth"]) != "alive":
            raise RuntimeError("Cannot confirm fixture supervisor identity")
        print("Native resource sample complete; testing owned supervisor crash", flush=True)
        owned_children = process.children(recursive=True)
        process.kill()
        process.wait(timeout=10)
        _, alive = psutil.wait_procs(owned_children, timeout=10)
        report["checks"]["owned_tree_exited_after_supervisor_crash"] = not alive
        if alive:
            raise RuntimeError("Owned descendants survived supervisor crash")
        while time.time() - store.get(job_id)["updated_at"] <= 46:
            time.sleep(1)
        recovered = server.invoke("grok_recover", {"job_id": job_id, "request_id": "recover-" + run_id})
        report["checks"]["stale_job_recovered"] = recovered["recovered"]
        report["checks"]["workspace_lock_released"] = store.get(job_id)["worker_closed"]
        report["status"] = "passed" if all(report["checks"].values()) else "failed"
    except Exception as exc:
        report.update(status="failed", error=str(exc))
    finally:
        if job_id and not store.get(job_id).get("worker_closed"):
            try:
                server.invoke("grok_close", {"job_id": job_id, "request_id": "cleanup-" + run_id})
                wait(job_id, lambda j: j["worker_closed"], 20)
            except Exception:
                report["cleanup_requires_inspection"] = True
        target = ROOT / "evidence" / "native-resources-2026-09-08.json"
        target.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report, indent=2), flush=True)
    if report.get("status") != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
