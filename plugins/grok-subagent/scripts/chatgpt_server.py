"""Private ChatGPT MCP entry point, intended for OpenAI Secure MCP Tunnel."""
from copy import deepcopy
import base64
import json
import os
from pathlib import Path
import sys

import server
import store

CONFIG = Path.home() / ".grok-subagent" / "chatgpt" / "config.json"
INSTRUCTIONS = """You control a private local Grok Build installation, not a Grok-hosted app.
Call grok_workspaces first and use a configured workspace alias, never invent a local path.
Local execution is disabled unless the operator has enabled it in that workspace's local policy.
Start only user-authorized tasks. Keep request_id stable on retry; start returns immediately.
Preserve job_id, use grok_read with next_cursor, and inspect asynchronous command receipts.
For a full answer use grok_result; local file paths cannot be opened by ChatGPT.
Interrupt an active turn and await cancelled before sending a correction. Cancellation is not undo.
Permission requests do not authorize themselves. Allow only actions within the user's explicit task.
Never treat allow_execution or permission_policy as an OS sandbox. Grok uses the local account's access.
This connection only exposes tasks created through its own client scope, not Codex tasks.
Read/wait does not arrange a future ChatGPT wake-up. Close idle jobs when no follow-up is needed.
Do not claim successful execution, installation, file delivery or recovery without returned evidence.
"""


class Gateway:
    def __init__(self, config):
        if not isinstance(config, dict) or config.get("schema_version") != 1:
            raise ValueError("Invalid local ChatGPT configuration")
        scope = config.get("client_scope")
        if not isinstance(scope, str) or not scope.startswith("chatgpt:") or len(scope) < 16:
            raise ValueError("A distinct local ChatGPT client_scope is required")
        self.scope = scope
        self.workspaces = {}
        for alias, entry in config.get("workspaces", {}).items():
            if not isinstance(alias, str) or not alias or not isinstance(entry, dict):
                raise ValueError("Invalid workspace configuration")
            path = Path(entry.get("path", "")).expanduser()
            if not path.is_absolute() or not path.is_dir():
                raise ValueError("Each configured workspace must be an existing absolute directory")
            if type(entry.get("allow_execution", False)) is not bool:
                raise ValueError("allow_execution must be a boolean")
            self.workspaces[alias] = {**entry, "path": path.resolve()}
        self.tools = deepcopy(server.TOOLS)
        start = next(t for t in self.tools if t["name"] == "grok_start")
        start["inputSchema"]["properties"]["workspace"]["description"] = (
            "A workspace alias returned by grok_workspaces; not a filesystem path")
        start["description"] += " For ChatGPT, workspace is a locally configured alias."
        self.tools += [
            server.tool("grok_workspaces", "List configured local workspace aliases and whether execution is enabled. Does not grant access or change settings.", {}, [], True),
            server.tool("grok_result", "Read the full saved answer in bounded text pages. Use next_offset until null; do not try opening a local result_path from ChatGPT.",
                        {**server.JOB, "offset": {"type": "integer", "minimum": 0, "maximum": 10000000},
                         "limit": {"type": "integer", "minimum": 1, "maximum": 12000}}, ["job_id"], True),
        ]

    def workspace(self, alias, execution=False):
        entry = self.workspaces.get(alias)
        if not entry:
            raise ValueError("Unknown workspace alias; call grok_workspaces")
        if not entry["path"].is_dir() or entry["path"].resolve() != entry["path"]:
            raise ValueError("Configured workspace changed; local policy must be reviewed")
        if execution and not entry.get("allow_execution", False):
            raise ValueError("Local execution is disabled; the operator must enable it locally")
        return entry

    def owned(self, job_id, execution=False):
        job = store.get(job_id)
        if job.get("client_scope") != self.scope:
            raise ValueError("Unknown job_id for this ChatGPT connection")
        aliases = [alias for alias, entry in self.workspaces.items() if str(entry["path"]) == job["workspace"]]
        if not aliases:
            raise ValueError("Job workspace is no longer configured for this connection")
        self.workspace(aliases[0], execution=execution)
        return job, aliases[0]

    def invoke(self, name, args):
        if not isinstance(args, dict):
            raise ValueError("Arguments must be an object")
        if name == "grok_workspaces":
            if args:
                raise ValueError("No arguments expected")
            return {"workspaces": [{"name": alias, "allow_execution": entry.get("allow_execution", False)}
                                   for alias, entry in self.workspaces.items()],
                    "boundary": "Workspace selection is not an OS sandbox; execution uses the local account."}
        if name == "grok_list":
            if args:
                raise ValueError("No arguments expected")
            jobs = []
            for brief in store.list_jobs(client_scope=self.scope):
                try:
                    _, alias = self.owned(brief["job_id"])
                except ValueError:
                    continue
                jobs.append({**{k: v for k, v in brief.items() if k != "workspace"}, "workspace": alias})
            return {"jobs": jobs}
        if name == "grok_doctor":
            value = server.invoke(name, args)
            return {key: value[key] for key in ("version", "exit_code", "python", "plugin_version",
                                                "tested_grok_version", "compatibility", "default_integration_mode")}
        if name == "grok_start":
            entry = self.workspace(args.get("workspace"), execution=True)
            value = server.invoke(name, {**args, "workspace": str(entry["path"])})
            return {key: item for key, item in value.items() if key != "result_path"}
        if name == "grok_result":
            allowed = {"job_id", "offset", "limit"}
            if set(args) - allowed or "job_id" not in args:
                raise ValueError("Invalid result arguments")
            job, _ = self.owned(args["job_id"])
            offset, limit = args.get("offset", 0), args.get("limit", 8000)
            if type(offset) is not int or not 0 <= offset <= 10000000:
                raise ValueError("Invalid offset")
            if type(limit) is not int or not 1 <= limit <= 12000:
                raise ValueError("Invalid limit")
            path = Path(job["result_path"]).resolve()
            expected = (store.DATA / job["job_id"] / "result.md").resolve()
            if path != expected or not path.is_relative_to(store.DATA):
                raise ValueError("Result path is outside task storage")
            text = ""
            if path.is_file():
                with path.open(encoding="utf-8") as handle:
                    remaining = offset
                    while remaining:
                        skipped = handle.read(min(remaining, 8192))
                        if not skipped:
                            break
                        remaining -= len(skipped)
                    text = handle.read(limit + 1)
            return {"job_id": job["job_id"], "status": job["status"], "text": text[:limit],
                    "offset": offset, "next_offset": offset + limit if len(text) > limit else None}
        if name not in {tool["name"] for tool in server.TOOLS}:
            raise ValueError("Unknown tool")
        job, alias = self.owned(args.get("job_id"), execution=name in {"grok_send", "grok_permission"})
        value = server.invoke(name, args)
        if name == "grok_read":
            hidden = {"result_path", "worker_pid", "grok_pid", "worker_birth", "grok_birth", "client_scope"}
            value["job"] = {k: v for k, v in value["job"].items() if k not in hidden}
            value["job"]["workspace"] = alias
        return value


def main():
    path = Path(os.environ.get("GROK_CHATGPT_CONFIG", str(CONFIG))).resolve()
    config = json.loads(path.read_text(encoding="utf-8"))
    gateway = Gateway(config)
    # Set once before handling requests; child snapshots inherit the same scope.
    os.environ["GROK_SUBAGENT_CLIENT_SCOPE"] = gateway.scope
    icon_path = Path(__file__).resolve().parents[1] / "assets" / "grok-icon.png"
    icon = None
    if icon_path.is_file():
        icon = {"src": "data:image/png;base64," + base64.b64encode(icon_path.read_bytes()).decode("ascii"),
                "mimeType": "image/png", "sizes": ["512x512"]}
    server.serve(tools=gateway.tools, handler=gateway.invoke, instructions=INSTRUCTIONS,
                 server_name="grok-subagent-chatgpt", icon=icon)


if __name__ == "__main__":
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        main()
    except Exception as exc:
        print("ChatGPT bridge startup refused: " + str(exc), file=sys.stderr)
        raise SystemExit(1)
