"""Durable job mailbox. Python 3.11+, no third-party dependencies."""
from __future__ import annotations

import hashlib
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid

import runtime

DATA = Path(os.environ.get("GROK_SUBAGENT_DATA", str(Path.home() / ".grok-subagent"))).resolve()
FINAL = {"completed", "cancelled", "failed", "timeout", "closed"}


@contextmanager
def connect():
    DATA.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DATA / "jobs.sqlite3", timeout=10)
    db.row_factory = sqlite3.Row
    db.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS jobs (
          id TEXT PRIMARY KEY, request_key TEXT UNIQUE, fingerprint TEXT, data TEXT);
        CREATE TABLE IF NOT EXISTS events (
          seq INTEGER PRIMARY KEY AUTOINCREMENT, job TEXT, data TEXT);
        CREATE INDEX IF NOT EXISTS event_jobs ON events(job,seq);
        CREATE TABLE IF NOT EXISTS commands (
          seq INTEGER PRIMARY KEY AUTOINCREMENT, job TEXT, request_key TEXT,
          data TEXT, result TEXT, UNIQUE(job,request_key));
    """)
    try:
        with db:
            yield db
    finally:
        db.close()


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def get(job_id):
    with connect() as db:
        row = db.execute("SELECT data FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        raise ValueError("Unknown job_id")
    return json.loads(row["data"])


def save(job):
    job["updated_at"] = time.time()
    with connect() as db:
        db.execute("UPDATE jobs SET data=? WHERE id=?", (encode(job), job["job_id"]))


def finish(job):
    """Publish closure and reject queued commands in the same transaction."""
    job["updated_at"] = time.time()
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("UPDATE jobs SET data=? WHERE id=?", (encode(job), job["job_id"]))
        db.execute("UPDATE commands SET result=? WHERE job=? AND result IS NULL",
                   (encode({"error": "Worker closed before command application"}), job["job_id"]))


def event(job_id, kind, data):
    with connect() as db:
        db.execute("INSERT INTO events(job,data) VALUES (?,?)",
                   (job_id, encode({"at": time.time(), "kind": kind, "data": data})))


def executable():
    path = os.environ.get("GROK_SUBAGENT_EXE")
    if not path:
        native = Path.home() / ".grok" / "bin" / ("grok.exe" if os.name == "nt" else "grok")
        path = str(native) if native.is_file() else shutil.which("grok")
    if not path or not Path(path).is_file():
        raise ValueError("Official Grok Build executable not found. Set GROK_SUBAGENT_EXE to its absolute path.")
    if Path(path).suffix.lower() in {".cmd", ".bat", ".ps1"}:
        raise ValueError("Use the native Grok executable, not a shell wrapper.")
    return str(Path(path).resolve())


def nonempty(value, name, maximum=200000):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{name} must be a nonempty string, at most {maximum} characters")
    return value


def start(args):
    key = nonempty(args["request_id"], "request_id", 160)
    client_scope = os.environ.get("GROK_SUBAGENT_CLIENT_SCOPE", "codex")
    if client_scope != "codex":
        key = "scope:" + hashlib.sha256((client_scope + "\0" + key).encode()).hexdigest()
    prompt = nonempty(args["prompt"], "prompt")
    cwd = Path(nonempty(args["workspace"], "workspace", 4096)).expanduser()
    if not cwd.is_absolute() or not cwd.is_dir():
        raise ValueError("workspace must be an existing absolute directory")
    cwd = cwd.resolve()
    timeout = args.get("timeout_seconds", 1800)
    if type(timeout) is not int or not 30 <= timeout <= 86400:
        raise ValueError("timeout_seconds must be an integer from 30 to 86400")
    permission = args.get("permission_policy", "ask")
    if permission not in {"ask", "deny"}:
        raise ValueError("permission_policy must be ask or deny")
    integration = args.get("integration_mode", "native")
    if integration not in {"native", "inherit"}:
        raise ValueError("integration_mode must be native or inherit")
    config = {"prompt": prompt, "workspace": str(cwd), "timeout_seconds": timeout,
              "permission_policy": permission, "executable": executable(), "integration_mode": integration}
    for field in ("model", "effort", "resume_session_id"):
        if args.get(field) is not None:
            config[field] = nonempty(args[field], field, 200)
    fingerprint = hashlib.sha256(encode(config).encode()).hexdigest()
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        prior = db.execute("SELECT fingerprint,data FROM jobs WHERE request_key=?", (key,)).fetchone()
        if prior:
            previous_job = json.loads(prior["data"])
            legacy = dict(config)
            legacy.pop("integration_mode", None)
            legacy_retry = ("integration_mode" not in args and "integration_mode" not in previous_job
                            and prior["fingerprint"] == hashlib.sha256(encode(legacy).encode()).hexdigest())
            if prior["fingerprint"] != fingerprint and not legacy_retry:
                raise ValueError("request_id was already used with different arguments")
            return {**previous_job, "deduplicated": True}
        history = [json.loads(r["data"]) for r in db.execute("SELECT data FROM jobs")]
        previous = config.get("resume_session_id")
        if previous:
            matches = [j for j in history if j.get("session_id") == previous]
            if any(j.get("client_scope", "codex") != client_scope for j in matches):
                raise ValueError("Session belongs to a different client scope")
            if not matches or any(Path(j["workspace"]) != cwd for j in matches):
                raise ValueError("Resume requires a plugin-recorded session and its original workspace")
            if any(not j.get("worker_closed") for j in matches):
                raise ValueError("Close or recover the previous worker before resuming its session")
            latest = max(matches, key=lambda j: j["created_at"])
            if latest.get("integration_mode", "inherit") != integration:
                raise ValueError("Resume must preserve the previous integration_mode")
        # A stale heartbeat is not proof the worker stopped. Keep its scope locked.
        live = [j for j in history if not j.get("worker_closed")]
        if len(live) >= 4:
            raise ValueError("Four workers are already active; close an idle worker first")
        # Serial ownership per workspace, including ancestor/descendant workspaces.
        for other in live:
            other_path = Path(other["workspace"])
            if cwd == other_path or cwd in other_path.parents or other_path in cwd.parents:
                raise ValueError(f"Workspace overlaps live job {other['job_id']}; use a separate checkout or close it")
        job_id = str(uuid.uuid4())
        job = {**config, "job_id": job_id, "status": "queued", "updated_at": time.time(),
               "created_at": time.time(), "worker_closed": False, "turn": 0,
               "session_id": None, "pending_permissions": [], "answer_tail": "",
               "plugin_version": runtime.VERSION,
               "client_scope": client_scope,
               "result_path": str(DATA / job_id / "result.md")}
        db.execute("INSERT INTO jobs VALUES (?,?,?,?)", (job_id, key, fingerprint, encode(job)))
    folder = DATA / job_id
    try:
        folder.mkdir()
        # A detached job must not import files replaced by a plugin upgrade.
        snapshot = folder / "runtime"
        snapshot.mkdir()
        digest = hashlib.sha256()
        for source in sorted(Path(__file__).parent.glob("*.py")):
            content = source.read_bytes()
            (snapshot / source.name).write_bytes(content)
            digest.update(source.name.encode() + b"\0" + content)
        job["runtime_sha256"] = digest.hexdigest()
        save(job)
        with (folder / "worker.log").open("ab") as log:
            kwargs = ({"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
                      if os.name == "nt" else {"start_new_session": True})
            child = subprocess.Popen(
                [sys.executable, str(snapshot / "worker.py"), job_id],
                stdin=subprocess.DEVNULL, stdout=log, stderr=log, close_fds=True,
                cwd=str(snapshot), **kwargs)
            # The child owns status updates. Do not overwrite its newer state here.
        return {**get(job_id), "launched_pid": child.pid}
    except Exception as exc:
        job.update(status="failed", worker_closed=True, error=str(exc))
        finish(job)
        raise


def command(job_id, request_id, action, **payload):
    nonempty(request_id, "request_id", 160)
    data = encode({"action": action, **payload})
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT data FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise ValueError("Unknown job_id")
        job = json.loads(row["data"])
        prior = db.execute("SELECT seq,data,result FROM commands WHERE job=? AND request_key=?",
                           (job_id, request_id)).fetchone()
        if prior:
            if prior["data"] != data:
                raise ValueError("request_id was already used with a different command")
            return {"command_id": prior["seq"], "deduplicated": True,
                    "result": json.loads(prior["result"]) if prior["result"] else None}
        if job.get("worker_closed") or time.time() - job["updated_at"] > 45:
            raise ValueError("Worker is closed or unresponsive; inspect status and resume explicitly in a new job")
        seq = db.execute("INSERT INTO commands(job,request_key,data) VALUES (?,?,?)",
                         (job_id, request_id, data)).lastrowid
    return {"job_id": job_id, "command_id": seq, "accepted": True}


def recover(job_id, request_id):
    """Release a stale job only after proving both recorded processes stopped."""
    nonempty(request_id, "request_id", 160)
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT data FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise ValueError("Unknown job_id")
        job = json.loads(row["data"])
        if job.get("recovery_request_id") == request_id:
            return {"job_id": job_id, "recovered": True, "deduplicated": True}
        if job.get("worker_closed"):
            return {"job_id": job_id, "recovered": False, "already_closed": True}
        if time.time() - job["updated_at"] <= 45:
            raise ValueError("Worker heartbeat is still fresh; use close")
        for label in ("worker", "grok"):
            pid, birth = job.get(label + "_pid"), job.get(label + "_birth")
            if not pid or not birth:
                raise ValueError("Missing process identity; automatic recovery cannot prove ownership")
            if runtime.original_process_state(pid, birth) != "dead":
                raise ValueError(f"{label} process is alive or cannot be verified; scope remains locked")
        job.update(worker_closed=True, pending_permissions=[], status="failed",
                   error="Worker stopped unexpectedly; explicit session resume is available",
                   recovery_request_id=request_id, updated_at=time.time())
        db.execute("UPDATE jobs SET data=? WHERE id=?", (encode(job), job_id))
        db.execute("UPDATE commands SET result=? WHERE job=? AND result IS NULL",
                   (encode({"error": "Worker stopped; command was not replayed"}), job_id))
        db.execute("INSERT INTO events(job,data) VALUES (?,?)",
                   (job_id, encode({"at": time.time(), "kind": "recovered",
                                    "data": {"request_id": request_id, "session_id": job.get("session_id")}})))
    return {"job_id": job_id, "recovered": True, "session_id": job.get("session_id")}


def read(job_id, after=0, wait_seconds=0):
    if type(after) is not int or after < 0:
        raise ValueError("after must be a nonnegative event cursor")
    if type(wait_seconds) is not int or not 0 <= wait_seconds <= 20:
        raise ValueError("wait_seconds must be 0..20")
    deadline = time.monotonic() + wait_seconds
    while True:
        job = get(job_id)
        with connect() as db:
            rows = db.execute("SELECT seq,data FROM events WHERE job=? AND seq>? ORDER BY seq LIMIT 80",
                              (job_id, after)).fetchall()
            commands = db.execute("SELECT seq,result FROM commands WHERE job=? ORDER BY seq DESC LIMIT 10",
                                  (job_id,)).fetchall()
        if rows or job["status"] in FINAL or job["pending_permissions"] or time.monotonic() >= deadline:
            break
        time.sleep(0.15)
    events, size = [], 0
    for row in rows:
        if events and size + len(row["data"]) > 32000:
            break
        events.append({"cursor": row["seq"], **json.loads(row["data"])})
        size += len(row["data"])
    public = {k: v for k, v in job.items() if k not in {"prompt", "executable"}}
    public["heartbeat_stale"] = not job.get("worker_closed") and time.time() - job["updated_at"] > 45
    return {"job": public, "events": events,
            "next_cursor": events[-1]["cursor"] if events else after,
            "commands": [{"command_id": r["seq"], "result": json.loads(r["result"]) if r["result"] else None}
                         for r in commands]}


def list_jobs(client_scope=None):
    with connect() as db:
        if client_scope is None:
            rows = db.execute("SELECT data FROM jobs ORDER BY rowid DESC LIMIT 40").fetchall()
        else:
            rows = db.execute(
                "SELECT data FROM jobs WHERE json_extract(data,'$.client_scope')=? ORDER BY rowid DESC LIMIT 40",
                (client_scope,)).fetchall()
    keys = ("job_id", "status", "workspace", "session_id", "worker_closed", "updated_at")
    return [{k: j.get(k) for k in keys} for j in (json.loads(r["data"]) for r in rows)]
