"""Loop controller ownership regressions, isolated from user state and Codex."""
import contextlib
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import loop_supervisor as loop


class RecoveryOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"CODEX_BRIDGE_STATE": self.tmp.name})
        self.env.start()
        self.state = {
            "schema_version": 3, "loop_id": "ownership", "status": "running",
            "attempts": 1, "max_attempts": 3, "thread_id": "saved-thread",
            "worker_spawned_at": "2000-01-01T00:00:00Z", "updated_at": loop.now(),
            "verify_commands": [], "history": [], "allow_commands": ["python3"],
        }
        loop.write_state(self.state)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_owner_only_crash_refuses_replay_until_child_exits(self):
        # A detached owner dies while its independently detached child survives.
        code = '''import subprocess, sys, time, json, os
import loop_supervisor as g
lock=g.try_lock("ownership")
p=subprocess.Popen([sys.executable,"-c","import time; time.sleep(60)"],start_new_session=True)
s=g.load_state("ownership")
s.update(owner_pid=os.getpid(),child_pid=p.pid,child_pgid=p.pid,active_job_id="pid-"+str(p.pid))
g.write_state(s)
print(p.pid,flush=True)
time.sleep(60)
'''
        owner = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True, start_new_session=True)
        child = int(owner.stdout.readline())
        try:
            owner.kill()
            owner.wait(timeout=3)
            state = loop.stale_to_interrupted(loop.load_state("ownership"))
            self.assertEqual(state["status"], "blocked")
            self.assertEqual(state["blocked_reason"], "orphan_child_alive")
            self.assertEqual(state["child_pid"], child)
            self.assertEqual(state["thread_id"], "saved-thread")
            with patch.object(loop, "spawn_worker") as spawn, patch.object(loop, "read_input", return_value={}), contextlib.redirect_stdout(io.StringIO()):
                loop.cmd_recover()
                loop.cmd_resume("ownership")
            spawn.assert_not_called()
            self.assertEqual(loop.load_state("ownership")["attempts"], 1)
        finally:
            try:
                os.kill(child, signal.SIGKILL)
            except ProcessLookupError:
                pass
            owner.stdout.close()

    def test_live_resume_and_recovery_preserve_owner_checkpoint(self):
        lock = loop.try_lock("ownership")
        try:
            state = loop.load_state("ownership")
            state.update(attempts=2, child_pid=os.getpid(), child_pgid=os.getpid(), owner_pid=os.getpid())
            loop.write_state(state)
            before = loop.state_path("ownership").read_bytes()
            with patch.object(loop, "read_input", return_value={"verify_commands": [{"command": "python3", "args": ["-V"]}]}), patch.object(loop, "spawn_worker") as spawn, contextlib.redirect_stdout(io.StringIO()):
                loop.cmd_resume("ownership")
                loop.cmd_recover()
                loop.cmd_status("ownership")
            spawn.assert_not_called()
            self.assertEqual(loop.state_path("ownership").read_bytes(), before)
        finally:
            lock.close()

    def test_direct_worker_preserves_live_child_and_budget_blocks_after_exit(self):
        state = loop.load_state("ownership")
        state.update(child_pid=os.getpid(), child_pgid=os.getpid(), attempts=3)
        loop.write_state(state)
        loop.worker("ownership")
        state = loop.load_state("ownership")
        self.assertEqual(state["blocked_reason"], "orphan_child_alive")
        self.assertEqual(state["child_pid"], os.getpid())
        with patch.object(loop, "process_alive", return_value=False), patch.object(loop, "spawn_worker") as spawn, patch.object(loop, "read_input", return_value={}), contextlib.redirect_stdout(io.StringIO()):
            loop.cmd_resume("ownership")
            spawn.assert_not_called()
            # Direct worker invocation also respects the terminal checkpoint.
            loop.worker("ownership")
        self.assertEqual(loop.load_state("ownership")["status"], "blocked")
        self.assertEqual(loop.load_state("ownership")["attempts"], 3)

    def test_unobservable_child_is_preserved(self):
        state = loop.load_state("ownership")
        state.update(child_pid=12345, child_pgid=12345)
        loop.write_state(state)
        with patch.object(loop.os, "kill", side_effect=PermissionError("unobservable")):
            self.assertTrue(loop.process_alive(12345))
            state = loop.stale_to_interrupted(state)
            self.assertEqual(state["blocked_reason"], "orphan_child_alive")
            self.assertEqual(state["child_pid"], 12345)

    def test_resume_after_orphan_exit_keeps_thread_and_attempt_budget(self):
        state = loop.load_state("ownership")
        state.update(status="blocked", blocked_reason="orphan_child_alive",
                     child_pid=12345, child_pgid=12345, active_job_id="pid-12345")
        loop.write_state(state)
        with patch.object(loop, "process_alive", return_value=False), patch.object(loop, "spawn_worker") as spawn, patch.object(loop, "read_input", return_value={}), contextlib.redirect_stdout(io.StringIO()):
            loop.cmd_resume("ownership")
            spawn.assert_called_once_with("ownership")
        resumed = loop.load_state("ownership")
        self.assertEqual(resumed["status"], "repairing")
        self.assertEqual(resumed["thread_id"], "saved-thread")
        self.assertEqual(resumed["attempts"], 1)
        self.assertIsNone(resumed["child_pid"])
        self.assertIsNone(resumed["blocked_reason"])

    def test_stale_reconciliation_reloads_current_checkpoint(self):
        stale = loop.load_state("ownership")
        current = dict(stale, status="completed", attempts=3, thread_id="new-thread")
        loop.write_state(current)
        restored = loop.stale_to_interrupted(stale)
        self.assertEqual(restored["status"], "completed")
        self.assertEqual(restored["thread_id"], "new-thread")
        self.assertEqual(restored["attempts"], 3)


if __name__ == "__main__":
    unittest.main()
