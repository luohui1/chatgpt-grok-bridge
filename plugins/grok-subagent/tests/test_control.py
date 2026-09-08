import asyncio
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import time
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import server
import store
import runtime
from worker import Worker

FAKE = str(Path(__file__).with_name("fake_acp.py"))


class StoreTests(unittest.TestCase):
    def setUp(self):
        executable = patch("store.executable", return_value=sys.executable)
        executable.start()
        self.addCleanup(executable.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.old = store.DATA
        store.DATA = Path(self.temp.name) / "data"
        self.workspace = Path(self.temp.name) / "space 中文"
        self.workspace.mkdir()

    def tearDown(self):
        store.DATA = self.old
        self.temp.cleanup()

    def start(self, key="request-1", prompt="hello"):
        with patch("store.subprocess.Popen") as launch:
            launch.return_value.pid = 12345
            return store.start({"workspace": str(self.workspace), "prompt": prompt, "request_id": key})

    def test_start_idempotent_conflicting_retry_refused(self):
        first = self.start()
        self.assertEqual(self.start()["job_id"], first["job_id"])
        with self.assertRaisesRegex(ValueError, "different arguments"):
            self.start(prompt="different")

    def test_workspace_overlap_refused(self):
        self.start()
        with self.assertRaisesRegex(ValueError, "overlaps"):
            self.start(key="request-2")

    def test_command_idempotency_and_cursor(self):
        job = self.start()
        first = store.command(job["job_id"], "cancel-1", "interrupt")
        duplicate = store.command(job["job_id"], "cancel-1", "interrupt")
        self.assertEqual(first["command_id"], duplicate["command_id"])
        with self.assertRaisesRegex(ValueError, "different command"):
            store.command(job["job_id"], "cancel-1", "close")
        store.event(job["job_id"], "sample", {"text": "中文\n\"quoted\""})
        page = store.read(job["job_id"])
        self.assertEqual(page["events"][0]["data"]["text"], "中文\n\"quoted\"")
        self.assertEqual(store.read(job["job_id"], page["next_cursor"])["events"], [])

    def test_schema_rejects_wait_boolean_and_unknown_flag(self):
        with self.assertRaises(ValueError):
            server.invoke("grok_read", {"job_id": "x", "wait_seconds": True})
        with self.assertRaises(ValueError):
            server.invoke("grok_start", {"request_id": "x", "workspace": str(self.workspace),
                                       "prompt": "hello", "always_approve": True})

    def test_native_mode_is_process_scoped_and_snapshot_exists(self):
        job = self.start()
        self.assertEqual(job["integration_mode"], "native")
        self.assertEqual(job["plugin_version"], runtime.VERSION)
        snapshot = store.DATA / job["job_id"] / "runtime" / "worker.py"
        self.assertTrue(snapshot.is_file())
        env = runtime.launch_environment("native")
        self.assertEqual(env["GROK_CURSOR_MCPS_ENABLED"], "0")
        self.assertEqual(env["GROK_CLAUDE_MCPS_ENABLED"], "0")

    def test_resume_rejects_unknown_session_and_wrong_workspace(self):
        with self.assertRaisesRegex(ValueError, "plugin-recorded"):
            store.start({"request_id": "unknown-resume", "workspace": str(self.workspace),
                         "prompt": "resume", "resume_session_id": "unknown"})
        job = self.start()
        job.update(worker_closed=True, session_id="known-session")
        store.save(job)
        other = self.workspace.parent / "other"
        other.mkdir()
        with self.assertRaisesRegex(ValueError, "original workspace"):
            store.start({"request_id": "wrong-resume", "workspace": str(other),
                         "prompt": "resume", "resume_session_id": "known-session"})

    def test_finish_acknowledges_unhandled_commands(self):
        job = self.start()
        receipt = store.command(job["job_id"], "unhandled", "send", prompt="later")
        job.update(worker_closed=True, status="closed")
        store.finish(job)
        page = store.read(job["job_id"])
        self.assertEqual(page["commands"][0]["command_id"], receipt["command_id"])
        self.assertIn("error", page["commands"][0]["result"])
        with self.assertRaisesRegex(ValueError, "closed"):
            store.command(job["job_id"], "after-close", "send", prompt="later")

    def test_recovery_refuses_live_or_unknown_and_accepts_dead(self):
        job = self.start()
        job.update(worker_pid=100, worker_birth="a", grok_pid=200, grok_birth="b")
        store.save(job)
        # Persist a stale timestamp without save() refreshing it.
        with store.connect() as db:
            job["updated_at"] = time.time() - 100
            db.execute("UPDATE jobs SET data=? WHERE id=?", (store.encode(job), job["job_id"]))
        for state in ("alive", "unknown"):
            with patch("runtime.original_process_state", return_value=state):
                with self.assertRaisesRegex(ValueError, "scope remains locked"):
                    store.recover(job["job_id"], "recover")
        with patch("runtime.original_process_state", return_value="dead"):
            result = store.recover(job["job_id"], "recover")
            self.assertTrue(result["recovered"])
            self.assertTrue(store.recover(job["job_id"], "recover")["deduplicated"])
        self.assertTrue(store.get(job["job_id"])["worker_closed"])

    def test_pid_reuse_is_dead_but_access_failure_is_unknown(self):
        with patch("runtime.process_identity", return_value={"state": "alive", "birth": "new"}):
            self.assertEqual(runtime.original_process_state(123, "old"), "dead")
        with patch("runtime.process_identity", return_value={"state": "unknown"}):
            self.assertEqual(runtime.original_process_state(123, "old"), "unknown")

    def test_legacy_start_retry_does_not_launch_again(self):
        job = self.start()
        job.pop("integration_mode")
        config = {key: job[key] for key in ("prompt", "workspace", "timeout_seconds",
                                           "permission_policy", "executable")}
        with store.connect() as db:
            db.execute("UPDATE jobs SET data=?,fingerprint=? WHERE id=?",
                       (store.encode(job), hashlib.sha256(store.encode(config).encode()).hexdigest(),
                        job["job_id"]))
        self.assertTrue(self.start()["deduplicated"])


class McpSchedulingTests(unittest.TestCase):
    def test_many_waiters_cannot_block_close(self):
        release = threading.Event()
        observations = []

        def invoke(name, args):
            if name == "grok_read":
                observations.append(release.wait(2))
            else:
                release.set()
            return {}

        requests = [{"jsonrpc": "2.0", "id": number, "method": "tools/call",
                     "params": {"name": "grok_read", "arguments": {}}} for number in range(8)]
        requests.append({"jsonrpc": "2.0", "id": 99, "method": "tools/call",
                         "params": {"name": "grok_close", "arguments": {}}})
        stdin = io.StringIO("\n".join(json.dumps(r) for r in requests))
        with patch("server.invoke", side_effect=invoke), patch("sys.stdin", stdin), patch("sys.stdout", io.StringIO()):
            server.serve()
        self.assertEqual(observations, [True] * 8)


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        executable = patch("store.executable", return_value=sys.executable)
        executable.start()
        self.addCleanup(executable.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.old = store.DATA
        store.DATA = Path(self.temp.name) / "data"
        self.workspace = Path(self.temp.name) / "工作区 with spaces"
        self.workspace.mkdir()
        real_spawn = asyncio.create_subprocess_exec

        async def fake_spawn(*args, **kwargs):
            if args[0] == store.executable():
                return await real_spawn(sys.executable, FAKE, **kwargs)
            return await real_spawn(*args, **kwargs)

        self.patch = patch("worker.asyncio.create_subprocess_exec", fake_spawn)
        self.patch.start()
        self.tasks = []

    async def asyncTearDown(self):
        for worker, task in self.tasks:
            if not task.done():
                await worker.cancel("closed")
                worker.stop = True
                await asyncio.wait_for(task, 15)
        self.patch.stop()
        store.DATA = self.old
        self.temp.cleanup()

    async def start(self, prompt, policy="ask"):
        with patch("store.subprocess.Popen") as launch:
            launch.return_value.pid = 12345
            job = store.start({"workspace": str(self.workspace), "prompt": prompt,
                               "request_id": str(time.time_ns()), "permission_policy": policy})
        worker = Worker(job["job_id"])
        task = asyncio.create_task(worker.run())
        self.tasks.append((worker, task))
        return worker, task

    async def until(self, worker, predicate):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            job = store.get(worker.job["job_id"])
            if predicate(job):
                return job
            await asyncio.sleep(0.05)
        self.fail(f"Timeout: {store.get(worker.job['job_id'])}")

    async def test_followup_uses_same_session_and_preserves_unicode(self):
        worker, _ = await self.start("remember:中文 'quotes' $HOME `literal`")
        first = await self.until(worker, lambda j: j["status"] == "completed")
        store.command(first["job_id"], "send-1", "send", prompt="recall")
        second = await self.until(worker, lambda j: j["turn"] == 2 and j["status"] == "completed")
        self.assertEqual(first["session_id"], second["session_id"])
        self.assertIn("中文 'quotes' $HOME `literal`", second["answer_tail"])

    async def test_cancel_is_nonblocking_and_corrected_followup_works(self):
        worker, _ = await self.start("wait")
        job = await self.until(worker, lambda j: j["status"] == "running")
        store.command(job["job_id"], "interrupt-1", "interrupt")
        await self.until(worker, lambda j: j["status"] == "cancelled")
        store.command(job["job_id"], "send-1", "send", prompt="corrected")
        final = await self.until(worker, lambda j: j["turn"] == 2 and j["status"] == "completed")
        self.assertEqual(final["answer_tail"], "corrected")

    async def test_permission_invalid_option_then_allow(self):
        worker, _ = await self.start("permission")
        job = await self.until(worker, lambda j: j["status"] == "awaiting_permission")
        pid = job["pending_permissions"][0]["permission_id"]
        store.command(job["job_id"], "bad-1", "permission", permission_id=pid, option_id="invented")
        await asyncio.sleep(0.3)
        self.assertEqual(store.get(job["job_id"])["status"], "awaiting_permission")
        store.command(job["job_id"], "allow-1", "permission", permission_id=pid, option_id="allow-once")
        await self.until(worker, lambda j: j["status"] == "completed")

    async def test_permission_denial_does_not_stall(self):
        worker, _ = await self.start("permission", "deny")
        job = await self.until(worker, lambda j: j["status"] == "completed")
        self.assertEqual(job["pending_permissions"], [])

    async def test_crash_is_failed_and_owned_worker_closes(self):
        worker, task = await self.start("crash")
        await asyncio.wait_for(task, 15)
        job = store.get(worker.job["job_id"])
        self.assertEqual(job["status"], "failed")
        self.assertTrue(job["worker_closed"])

    async def test_timeout_cancels_and_closes(self):
        worker, task = await self.start("wait")
        await self.until(worker, lambda j: j["status"] == "running")
        worker.turn_deadline = time.monotonic() - 1
        await asyncio.wait_for(task, 15)
        job = store.get(worker.job["job_id"])
        self.assertEqual(job["status"], "timeout")
        self.assertTrue(job["worker_closed"])

    async def test_close_during_startup_prevents_first_prompt(self):
        worker, task = await self.start("must-not-run")
        store.command(worker.job["job_id"], "close-before-start", "close")
        await asyncio.wait_for(task, 15)
        job = store.get(worker.job["job_id"])
        self.assertEqual(job["turn"], 0)
        self.assertEqual(job["status"], "closed")
        self.assertTrue(job["worker_closed"])

    async def test_force_close_of_owned_process(self):
        worker, task = await self.start("wait")
        await self.until(worker, lambda j: j["status"] == "running")
        worker.forced_reason = "cancelled"
        worker.cancel_deadline = time.monotonic() - 1
        await asyncio.wait_for(task, 15)
        job = store.get(worker.job["job_id"])
        self.assertIsNotNone(worker.proc.returncode)
        self.assertEqual(job["status"], "cancelled")
        self.assertTrue(job["worker_closed"])

    async def test_repeated_cancel_preserves_deadline(self):
        worker, _ = await self.start("wait")
        await self.until(worker, lambda j: j["status"] == "running")
        worker.cancel_deadline = time.monotonic() + 4
        deadline = worker.cancel_deadline
        worker.forced_reason = "cancelled"
        await worker.cancel("closed")
        self.assertEqual(worker.cancel_deadline, deadline)
        self.assertEqual(worker.forced_reason, "closed")
        # Let normal teardown deliver the real cancellation.
        worker.cancel_deadline = None

    async def test_no_send_after_close(self):
        worker, _ = await self.start("hello")
        await self.until(worker, lambda j: j["status"] == "completed")
        await worker.apply_command({"action": "close"})
        with self.assertRaisesRegex(ValueError, "closing"):
            await worker.apply_command({"action": "send", "prompt": "must not run"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
