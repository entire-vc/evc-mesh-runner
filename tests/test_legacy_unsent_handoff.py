"""Legacy durable non-delivery reaches a fresh admitted harness without a fake ACK."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

from mesh_runner import delivery_recovery, interactive
import test_presend_quota_recovery as quota_tests
from test_runner import Bench
from fakes import FakeMesh, FakeProfileAdapter, calls, task


class LegacyHandoff(Bench):
    assert_handoff = quota_tests.QuotaHandoff.assert_handoff

    def prepared(self):
        card = task(1, cat='in_progress', description='Preserve existing completed workspace work.')
        mesh = FakeMesh([card])
        self.agent.alt = {'codex': {'profile': 'fixture'}}
        self.agent.env_command = [sys.executable, '-c', 'print("MESH_RUNNER_HARNESS=codex")']
        capacity = self.root / 'capacity'; capacity.write_text('1')
        released = self.root / 'released'
        self.agent.start_gate_command = [sys.executable, '-c',
            'import os;from pathlib import Path;'
            'assert os.environ.get("MESH_RUNNER_REQUIRE_ACCOUNT_CAPACITY")=="1";'
            'assert Path(' + repr(str(released)) + ').exists();'
            'print("RESERVATION=new-owned-claim");'
            'print("ACCOUNT_CAPACITY_READY="+Path(' + repr(str(capacity)) + ').read_text())']
        self.agent.turn_end_command = [sys.executable, '-c',
            'import os;from pathlib import Path;'
            'assert os.environ["MESH_RUNNER_GATE_RESERVATION"] in ("old-owned-claim","new-owned-claim");'
            'Path(' + repr(str(released)) + ').touch();print("RELEASE_CONFIRMED=1")']
        self.agent.on_limit_command = [sys.executable, '-c',
            'from pathlib import Path;Path(' + repr(str(self.root / 'untruthful-quota')) + ').touch()']
        r = self.runner(mesh); r.me = mesh.me()
        r.alt_adapters = {'codex': FakeProfileAdapter(r.adapter.script, 'fixture', native='codex')}
        a = r.store.create('builder', card['id'], 'claude')
        paths = [str(r.pool.lease(a.id, 'app'))]
        (Path(paths[0]) / 'pending-work.txt').write_text('previous uncommitted work')
        prompt = 'EXACT PENDING REQUEST: retain this instruction once.'
        intent = {'version': 1, 'kind': 'interactive', 'token': 'legacy-token',
                  'sha256': hashlib.sha256(prompt.encode()).hexdigest(), 'harness': 'claude',
                  'session_id': 'old-claude-session', 'prompt': prompt, 'log_path': '', 'turn': 3,
                  'resume': True, 'retryable': True, 'workspaces': paths, 'source_read_at': 'pending-source',
                  'prior': {'turns': 2, 'session_id': 'old-claude-session', 'log_path': ''}}
        r.store.update(a, state='ready', turns=2, workspaces=paths, session_id='old-claude-session',
                       wake_text='pending wake', wait={'interactive_prompt_intent': intent,
                       'queue_delivery': {'key': 'queue-key', 'value': 'pending', 'floor': 'pending-floor'}})
        r.store.mark('builder', a.task_id, 'gate', json.dumps({'RESERVATION': 'old-owned-claim'}))
        directory = interactive.agent_dir(self.cfg.state_dir, 'builder'); directory.mkdir(parents=True, exist_ok=True)
        interactive.write_delivery(directory, a.id, {k: intent[k] for k in ('version', 'token', 'sha256')} | {'phase': 'received'})
        # Real finished local driver PID; no live process/group may retain the turn.
        child = subprocess.Popen([sys.executable, '-c', 'pass'], start_new_session=True)
        child.wait(timeout=10)
        interactive.write_progress(directory, a.id, {'pid': child.pid, 'phase': 'done'})
        interactive.write_meta(directory, a.id, {'session_id': a.session_id, 'harness': 'claude', 'busy_pid': child.pid})
        # OS/tmux are external boundaries. Real SQLite, journal, gate subprocesses,
        # workspace leasing, adapters and child ACK/reconciliation remain real.
        self.windows = []
        patcher = mock.patch.object(interactive.Tmux, 'windows_strict', lambda _s: self.windows)
        patcher.start(); self.addCleanup(patcher.stop)
        self.native_argv = ['fixture-idle-process']
        run = subprocess.run
        def kernel_run(argv, *args, **kwargs):
            if argv[:2] == ['/bin/ps', '-ax']:
                return subprocess.CompletedProcess(argv, 0, f'{os.getpid()} {os.geteuid()}\n999999 {os.geteuid()+1}\n328 -2\n', '')
            return run(argv, *args, **kwargs)
        def kernel_argv(pid):
            self.assertEqual(pid, os.getpid(), 'must never inspect a different effective owner')
            return self.native_argv
        for patcher in (mock.patch('mesh_runner.delivery_recovery.subprocess.run', side_effect=kernel_run),
                        mock.patch('mesh_runner.delivery_recovery._process_argv', side_effect=kernel_argv)):
            patcher.start(); self.addCleanup(patcher.stop)
        return r, r.store.get(a.id), prompt, directory, capacity

    def test_legacy_received_handoff_uses_fresh_capacity_without_quota(self):
        r, a, prompt, _, _ = self.prepared()
        self.assertFalse(r._quota_admission_required(a))
        self.assert_handoff(r, a, prompt)
        self.assertFalse((self.root / 'untruthful-quota').exists())
        self.assertFalse(r._quota_admission_required(r.store.get(a.id)))

    def test_legacy_crash_after_session_reset_keeps_wake_exactly_once(self):
        r, a, prompt, _, _ = self.prepared()
        r.store.update(a, harness='codex', session_id='')
        self.assert_handoff(r, r.store.get(a.id), prompt)

    def test_denied_capacity_keeps_original_journal_and_prompt(self):
        r, a, _, directory, capacity = self.prepared(); capacity.write_text('0')
        journal = interactive.read_delivery(directory, a.id)
        r._resume_one(a)
        self.assertFalse(calls(self.calls))
        self.assertEqual(r.store.get(a.id).wait['interactive_prompt_intent'], a.wait['interactive_prompt_intent'])
        self.assertEqual(interactive.read_delivery(directory, a.id), journal)

    def test_legacy_proof_rejects_changed_bytes_and_nonlegacy_journal(self):
        r, a, _, directory, _ = self.prepared()
        intent = a.wait['interactive_prompt_intent']; original = interactive.read_delivery(directory, a.id)
        self.assertTrue(delivery_recovery.proven_legacy_unsent(directory, a.id, intent))
        for changes in ({'phase': 'send-intent'}, {'token': 'other'}, {'version': True},
                        {'sha256': '0'*64}, {'failure_kind': 'quota'}):
            with self.subTest(changes=changes):
                interactive.write_delivery(directory, a.id, original | changes)
                self.assertFalse(delivery_recovery.proven_legacy_unsent(directory, a.id, intent))
        interactive.write_delivery(directory, a.id, original)
        self.assertFalse(delivery_recovery.proven_legacy_unsent(directory, a.id, intent | {'prompt': 'changed'}))

    def test_live_or_unknown_tmux_does_not_release_claim_or_start(self):
        r, a, _, directory, _ = self.prepared()
        for windows in (None, [(interactive.session_name('builder'), a.id)]):
            with self.subTest(windows=windows):
                self.windows = windows
                r._resume_one(r.store.get(a.id))
                self.assertFalse(calls(self.calls))
                self.assertFalse((self.root / 'released').exists())

    def test_detached_native_or_unreadable_live_process_holds(self):
        r, a, _, directory, _ = self.prepared()
        for argv in (['claude', '--resume', 'old-claude-session'], [],
                     ['claude', '--session-id=old-claude-session']):
            with self.subTest(argv=argv):
                self.native_argv = argv
                r._resume_one(r.store.get(a.id))
                self.assertFalse(calls(self.calls))
                self.assertFalse((self.root / 'released').exists())

    def test_missing_capacity_receipt_cannot_handoff(self):
        r, a, _, directory, _ = self.prepared()
        self.agent.start_gate_command = [sys.executable, '-c', 'print("RESERVATION=new-owned-claim")']
        before = interactive.read_delivery(directory, a.id)
        r._resume_one(a)
        self.assertFalse(calls(self.calls))
        self.assertEqual(interactive.read_delivery(directory, a.id), before)
        self.assertEqual(r.store.get(a.id).session_id, 'old-claude-session')

    def test_unknown_old_claim_release_preserves_pointer(self):
        r, a, _, directory, _ = self.prepared()
        self.agent.turn_end_command = [sys.executable, '-c', 'import sys;sys.exit(75)']
        before = r.store.mark('builder', a.task_id, 'gate')
        r._resume_one(a)
        self.assertFalse(calls(self.calls))
        self.assertEqual(r.store.mark('builder', a.task_id, 'gate'), before)

    def test_changed_scope_or_live_driver_is_not_handoff_proof(self):
        r, a, _, directory, _ = self.prepared()
        original = json.loads(json.dumps(a.wait)); progress = interactive.read_progress(directory, a.id)
        for variant in ('workspace', 'session', 'bytes', 'token', 'live-driver', 'missing-progress', 'phase'):
            with self.subTest(variant=variant):
                wait = json.loads(json.dumps(original)); report = dict(progress)
                if variant == 'workspace': wait['interactive_prompt_intent']['workspaces'] = ['/changed']
                if variant == 'session': wait['interactive_prompt_intent']['session_id'] = 'another-session'
                if variant == 'bytes': wait['interactive_prompt_intent']['prompt'] += ' altered'
                if variant == 'token': wait['interactive_prompt_intent']['token'] = 'other'
                if variant == 'live-driver': report['pid'] = os.getpid()
                if variant == 'missing-progress': report = {}
                if variant == 'phase': report['phase'] = 'ready'
                interactive.write_progress(directory, a.id, report)
                r.store.update(r.store.get(a.id), wait=wait)
                r._resume_one(r.store.get(a.id))
                self.assertFalse(calls(self.calls))
                self.assertFalse((self.root / 'released').exists())

    def test_writer_reappears_after_admission_before_spawn(self):
        r, a, _, directory, _ = self.prepared()
        reads = []
        def windows(_tmux):
            reads.append(1)
            return [] if len(reads) < 3 else [(interactive.session_name('builder'), a.id)]
        with mock.patch.object(interactive.Tmux, 'windows_strict', windows):
            with self.assertRaisesRegex(RuntimeError, 'evidence changed'):
                r._resume_one(a)
        self.assertFalse(calls(self.calls))
        self.assertEqual(interactive.read_delivery(directory, a.id)['phase'], 'received')
        self.assertEqual(r.store.get(a.id).session_id, 'old-claude-session')

    def test_normal_metadata_cleanup_requires_current_process_absence(self):
        r, a, prompt, directory, _ = self.prepared()
        interactive.progress_path(directory, a.id).unlink()
        interactive.meta_path(directory, a.id).unlink()
        self.assert_handoff(r, a, prompt)

    def test_live_attempt_driver_without_native_session_cannot_handoff(self):
        r, a, _, directory, _ = self.prepared()
        interactive.progress_path(directory, a.id).unlink()
        interactive.meta_path(directory, a.id).unlink()
        self.native_argv = ['python', 'tui_driver.py', '--key', a.id]
        r._resume_one(a)
        self.assertFalse(calls(self.calls))
        self.assertFalse((self.root / 'released').exists())

    def test_release_requires_fresh_receipt_not_cached_marker_or_rc_zero(self):
        r, a, _, directory, _ = self.prepared()
        self.agent.turn_end_command = [sys.executable, '-c', 'pass']
        r.store.mark('builder', a.task_id, 'gate', json.dumps({'RESERVATION': 'old-owned-claim', 'RELEASE_CONFIRMED': '1'}))
        before = r.store.mark('builder', a.task_id, 'gate')
        r._resume_one(a)
        self.assertFalse(calls(self.calls))
        self.assertEqual(r.store.mark('builder', a.task_id, 'gate'), before)

    def test_existing_empty_or_corrupt_metadata_is_not_normal_cleanup(self):
        r, a, _, directory, _ = self.prepared()
        interactive.progress_path(directory, a.id).unlink()
        for raw in ('{}', '', '{broken'):
            with self.subTest(raw=raw):
                interactive.meta_path(directory, a.id).write_text(raw)
                r._resume_one(r.store.get(a.id))
                self.assertFalse(calls(self.calls))
                self.assertFalse((self.root / 'released').exists())


    def test_corrupt_nonempty_old_gate_is_retained_without_end_hook(self):
        r, a, _, directory, _ = self.prepared()
        for raw in ('{broken', '{}', '[]', 'null', '["claim"]', '"claim"', '""', '1', '0', '1.5', 'true', 'false'):
            with self.subTest(raw=raw):
                r.store.mark('builder', a.task_id, 'gate', raw)
                before = r.store.mark('builder', a.task_id, 'gate')
                r._resume_one(r.store.get(a.id))
                self.assertEqual(r.store.mark('builder', a.task_id, 'gate'), before)
                self.assertFalse(calls(self.calls))
                self.assertFalse((self.root / 'released').exists())


class NativeProcessBoundary(unittest.TestCase):
    def test_real_detached_session_process_is_detected_then_absent(self):
        session = '0000dddd-0000-4000-8000-000000000004'
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)',
                                  '--session-id', session], start_new_session=True)
        run = subprocess.run
        def kernel_run(argv, *args, **kwargs):
            if argv[:2] == ['/bin/ps', '-ax']:
                return subprocess.CompletedProcess(argv, 0, f'{child.pid} {os.geteuid()}\n', '')
            return run(argv, *args, **kwargs)
        try:
            with mock.patch('mesh_runner.delivery_recovery.subprocess.run', side_effect=kernel_run):
                # Only enumeration is controlled; kernel argv and PID checks are real.
                self.assertIn(session, delivery_recovery._process_argv(child.pid))
                self.assertFalse(delivery_recovery._session_process_absent(session, 'attempt'))
                child.terminate(); child.wait(timeout=5)
                self.assertTrue(delivery_recovery._session_process_absent(session, 'attempt'))
        finally:
            if child.poll() is None:
                child.kill(); child.wait(timeout=5)
