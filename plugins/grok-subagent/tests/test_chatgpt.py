import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import chatgpt_server
import server
import store


class ChatGPTTests(unittest.TestCase):
    def setUp(self):
        executable = patch("store.executable", return_value=sys.executable)
        executable.start()
        self.addCleanup(executable.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.old_data = store.DATA
        store.DATA = Path(self.temp.name) / "data"
        self.workspace = Path(self.temp.name) / "workspace"
        self.workspace.mkdir()
        self.scope = "chatgpt:fixture-connection"
        self.gateway = chatgpt_server.Gateway({
            "schema_version": 1, "client_scope": self.scope,
            "workspaces": {"fixture": {"path": str(self.workspace), "allow_execution": True}},
        })

    def tearDown(self):
        store.DATA = self.old_data
        self.temp.cleanup()

    def start(self, remote=True):
        with patch.dict(os.environ, {"GROK_SUBAGENT_CLIENT_SCOPE": self.scope if remote else "codex"}):
            with patch("store.subprocess.Popen") as launch:
                launch.return_value.pid = 12345
                if remote:
                    return self.gateway.invoke("grok_start", {
                        "workspace": "fixture", "prompt": "hello", "request_id": "same-key"})
                return server.invoke("grok_start", {
                    "workspace": str(self.workspace), "prompt": "hello", "request_id": "same-key"})

    def test_workspace_alias_and_execution_off_by_default(self):
        self.gateway.workspaces["fixture"]["allow_execution"] = False
        with self.assertRaisesRegex(ValueError, "disabled"):
            self.start()
        with self.assertRaisesRegex(ValueError, "alias"):
            self.gateway.invoke("grok_start", {"workspace": str(self.workspace)})
        result = self.gateway.invoke("grok_workspaces", {})
        self.assertNotIn(str(self.workspace), json.dumps(result))
        self.assertFalse(result["workspaces"][0]["allow_execution"])

    def test_remote_can_only_read_its_own_jobs(self):
        local = self.start(remote=False)
        with self.assertRaisesRegex(ValueError, "Unknown job_id"):
            self.gateway.invoke("grok_read", {"job_id": local["job_id"]})
        with self.assertRaisesRegex(ValueError, "Unknown job_id"):
            self.gateway.invoke("grok_close", {"job_id": local["job_id"], "request_id": "close"})
        self.assertEqual(self.gateway.invoke("grok_list", {})["jobs"], [])

    def test_remote_and_codex_share_workspace_lock(self):
        self.start(remote=False)
        with self.assertRaisesRegex(ValueError, "overlaps"):
            self.start(remote=True)

    def test_request_ids_are_namespaced_and_resume_rejects_other_scope(self):
        local = self.start(remote=False)
        job = store.get(local["job_id"])
        job.update(worker_closed=True, session_id="codex-session")
        store.save(job)
        remote = self.start(remote=True)
        self.assertNotEqual(local["job_id"], remote["job_id"])
        self.assertNotIn("result_path", remote)
        remote_job = store.get(remote["job_id"])
        remote_job.update(worker_closed=True)
        store.save(remote_job)
        with patch.dict(os.environ, {"GROK_SUBAGENT_CLIENT_SCOPE": self.scope}):
            with self.assertRaisesRegex(ValueError, "different client scope"):
                self.gateway.invoke("grok_start", {"workspace": "fixture", "prompt": "resume",
                                                  "request_id": "resume", "resume_session_id": "codex-session"})

    def test_paginated_unicode_result_and_scope_filtered_list(self):
        remote = self.start()
        job = store.get(remote["job_id"])
        Path(job["result_path"]).write_text("hello \u4e2d\u6587 result", encoding="utf-8")
        first = self.gateway.invoke("grok_result", {"job_id": remote["job_id"], "limit": 8})
        second = self.gateway.invoke("grok_result", {"job_id": remote["job_id"], "offset": first["next_offset"]})
        self.assertEqual(first["text"] + second["text"], "hello \u4e2d\u6587 result")
        self.assertIsNone(second["next_offset"])
        listing = self.gateway.invoke("grok_list", {})
        self.assertEqual(listing["jobs"][0]["workspace"], "fixture")
        page = self.gateway.invoke("grok_read", {"job_id": remote["job_id"]})
        self.assertNotIn("result_path", page["job"])
        self.assertEqual(page["job"]["workspace"], "fixture")

    def test_result_outside_storage_refused(self):
        remote = self.start()
        job = store.get(remote["job_id"])
        job["result_path"] = str(self.workspace / "not-a-result.txt")
        store.save(job)
        with self.assertRaisesRegex(ValueError, "outside"):
            self.gateway.invoke("grok_result", {"job_id": remote["job_id"]})

    def test_result_storage_alias_is_normalized(self):
        remote = self.start()
        job = store.get(remote["job_id"])
        Path(job["result_path"]).write_text("inside", encoding="utf-8")
        original = store.DATA
        try:
            store.DATA = original / ".." / original.name
            result = self.gateway.invoke("grok_result", {"job_id": remote["job_id"]})
            self.assertEqual(result["text"], "inside")
        finally:
            store.DATA = original

    def test_revoked_execution_still_allows_interruption(self):
        remote = self.start()
        self.gateway.workspaces["fixture"]["allow_execution"] = False
        with self.assertRaisesRegex(ValueError, "disabled"):
            self.gateway.invoke("grok_send", {"job_id": remote["job_id"], "request_id": "send", "prompt": "no"})
        result = self.gateway.invoke("grok_interrupt", {"job_id": remote["job_id"], "request_id": "stop"})
        self.assertTrue(result["accepted"])

    def test_chatgpt_initialize_instructions_and_tools(self):
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                        "clientInfo": {"name": "chatgpt-fixture", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "grok_workspaces", "arguments": {}}},
        ]
        output = io.StringIO()
        with patch("sys.stdin", io.StringIO("\n".join(json.dumps(r) for r in requests))), patch("sys.stdout", output):
            server.serve(tools=self.gateway.tools, handler=self.gateway.invoke,
                         instructions=chatgpt_server.INSTRUCTIONS, server_name="grok-subagent-chatgpt")
        responses = {r["id"]: r for r in map(json.loads, output.getvalue().splitlines())}
        self.assertIn("grok_result", responses[1]["result"]["instructions"])
        self.assertEqual(len(responses[2]["result"]["tools"]), 11)
        self.assertFalse(responses[3]["result"]["isError"])
        for tool in responses[2]["result"]["tools"]:
            self.assertIn("destructiveHint", tool["annotations"])

    def test_real_stdio_entrypoint_without_optional_artwork(self):
        config = Path(self.temp.name) / "chatgpt.json"
        config.write_text(json.dumps({"schema_version": 1, "client_scope": self.scope,
                                      "workspaces": {}}), encoding="utf-8")
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                        "clientInfo": {"name": "stdio-smoke", "version": "1"}}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "grok_workspaces", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
             "params": {"name": "grok_start", "arguments": {
                 "workspace": "not-enabled", "prompt": "must not run", "request_id": "blocked"}}},
        ]
        result = subprocess.run(
            [sys.executable, str(Path(chatgpt_server.__file__))],
            input="\n".join(json.dumps(r) for r in requests) + "\n",
            capture_output=True, text=True, encoding="utf-8", timeout=20,
            env=dict(os.environ, GROK_CHATGPT_CONFIG=str(config), GROK_SUBAGENT_DATA=str(store.DATA)),
            **({"creationflags": 0x08000000} if os.name == "nt" else {}))
        self.assertEqual(result.returncode, 0, result.stderr)
        messages = {r["id"]: r for r in map(json.loads, result.stdout.splitlines())}
        self.assertNotIn("icons", messages[1]["result"]["serverInfo"])
        self.assertEqual(len(messages[2]["result"]["tools"]), 11)
        self.assertEqual(messages[3]["result"]["structuredContent"]["workspaces"], [])
        self.assertTrue(messages[4]["result"]["isError"])


if __name__ == "__main__":
    unittest.main()
