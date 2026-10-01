import json
import tempfile
import unittest
import io
import urllib.response
from email.message import Message
from pathlib import Path
from unittest.mock import patch
import connection_watchdog as w


class RecoveryTests(unittest.TestCase):
    def drive(self, health, idle=True, times=(0, 30, 60), state=None):
        state = {} if state is None else state
        actions = []
        repairs = []
        for stamp in times:
            state, _ = w.tick(state, health, idle, lambda service, pending: actions.append(service), lambda: repairs.append(True), stamp)
        return state, actions, repairs

    def test_healthy_and_single_failure_do_not_restart(self):
        s, a, r = self.drive({'bridge': True, 'tunnel': True})
        self.assertEqual(a, [])
        self.assertEqual(s['status'], 'healthy')
        self.assertEqual(len(r), 3)
        s, a, _ = self.drive({'bridge': True, 'tunnel': False}, times=(0,))
        self.assertEqual(a, [])

    def test_only_tunnel_restarts_after_threshold(self):
        s, a, _ = self.drive({'bridge': True, 'tunnel': False})
        self.assertEqual(a, ['tunnel'])
        s, a, _ = self.drive({'bridge': True, 'tunnel': False}, times=(90, 120, 150), state=s)
        self.assertEqual(a, [])

    def test_bridge_first_and_busy_guard(self):
        s, a, _ = self.drive({'bridge': False, 'tunnel': False})
        self.assertEqual(a, ['bridge'])
        s, a, _ = self.drive({'bridge': False, 'tunnel': False}, idle=False)
        self.assertEqual(a, [])
        self.assertEqual(s['status'], 'paused_for_active_or_unknown_jobs')

    def test_budget_and_backoff(self):
        s, a, _ = self.drive({'bridge': True, 'tunnel': False}, times=range(0, 3600, 30))
        self.assertEqual(a, ['tunnel'] * 3)
        self.assertEqual(s['status'], 'attention_required')
        s, a, _ = self.drive({'bridge': True, 'tunnel': False}, times=(3700,), state=s)
        self.assertEqual(a, ['tunnel'])

    def test_attempt_is_recorded_before_effect(self):
        s, _, _ = self.drive({'bridge': True, 'tunnel': False}, times=(0, 30))
        def restart(service, pending):
            self.assertEqual(pending['last_restart'][service], 60)
            self.assertEqual(pending['restart_attempts'], [60])
        w.tick(s, {'bridge': True, 'tunnel': False}, True, restart, lambda: None, 60)

    def test_unknown_or_corrupt_journal_never_idle(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertFalse(w.disk_idle(root))
            (root / 'jobs-runtime.json').write_text('not json')
            self.assertFalse(w.disk_idle(root))

    def test_live_job_blocks_dead_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root / 'jobs').mkdir()
            (root / 'jobs-runtime.json').write_text(json.dumps({'version': 1, 'owner_pid': 100}))
            p = root / 'jobs/a.json'
            p.write_text(json.dumps({'child_pid': 200, 'finished': False}))
            with patch.object(w, 'alive', side_effect=lambda pid: pid == 200):
                self.assertFalse(w.disk_idle(root))
                p.write_text(json.dumps({'child_pid': 200, 'finished': True}))
                self.assertTrue(w.disk_idle(root))

    def test_orphan_child_never_replayed(self):
        import os
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root / 'loops').mkdir()
            (root / 'loops/a.json').write_text(json.dumps({'loop_id':'a','child_pid':os.getpid()}))
            with patch.dict(os.environ, {'CODEX_BRIDGE_STATE':tmp}), patch.object(w.subprocess,'run') as run:
                with self.assertRaises(RuntimeError):
                    w.recover_workers()
                run.assert_not_called()

    def test_invalid_target_never_probed(self):
        with patch.object(w.urllib.request,'build_opener') as open_url:
            self.assertFalse(w.probe('https://example.com/runtime','private')['ok'])
            open_url.assert_not_called()

    def test_redirect_never_followed(self):
        for target in ['/redirect-target', 'https://example.com/runtime']:
            headers = Message()
            headers['Location'] = target
            response = urllib.response.addinfourl(io.BytesIO(b''), headers,
                                                  'http://127.0.0.1/runtime', 302)
            response.msg = 'Found'
            # Keep the real opener/error/redirect chain; replace only network I/O.
            with patch.object(w.urllib.request.HTTPHandler, 'http_open', return_value=response) as request:
                result = w.probe('http://127.0.0.1/runtime', 'private')
                self.assertFalse(result['ok'])
                self.assertEqual(result['code'], 302)
                self.assertEqual(request.call_count, 1)
                self.assertEqual(request.call_args[0][0].full_url, 'http://127.0.0.1/runtime')

    def test_child_scan_and_recovery_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'jobs-runtime.json').write_text(json.dumps({'version': 1, 'owner_pid': 100}))
            with patch.object(w, 'alive', return_value=True), patch.object(w.subprocess, 'run') as run:
                run.return_value.stdout = '200 100\n'
                self.assertFalse(w.disk_idle(root))
        def failed():
            raise RuntimeError('fixture')
        s, _ = w.tick({}, {'bridge': True, 'tunnel': True}, True, lambda *args: None, failed, 0)
        self.assertEqual(s['status'], 'worker_recovery_check_failed')


if __name__ == '__main__':
    unittest.main()
