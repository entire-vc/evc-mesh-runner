"""Real SQLite, exec and inherited singleton tests; no internal mocks."""
import fcntl
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace

from mesh_runner import launch_guard as guard
from mesh_runner.store import Store

SOURCE = Path(guard.__file__).resolve().parents[2]


class LaunchGuard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.release = self.root / 'release'
        shutil.copytree(SOURCE / 'src', self.release / 'src', ignore=shutil.ignore_patterns('__pycache__'))
        self.state = self.root / 'state'
        self.config = self.root / 'runner.toml'
        self.config.write_text('[runner]\nmesh_url="http://invalid.local"\nstate_dir=' + json.dumps(str(self.state)) + '\n')
        self.program = self.release / 'src/mesh_runner/launch_guard.py'
        self.children = []

    def tearDown(self):
        for p in self.children:
            if p.poll() is None:
                p.kill()
            p.wait(timeout=5)
            if p.stdout:
                p.stdout.close()
            if p.stderr:
                p.stderr.close()
        self.tmp.cleanup()

    def command(self, release=None, *extra):
        return [sys.executable, str(self.program), '--target-release', str(release or self.release),
                '--config', str(self.config), *extra]

    def invoke(self, release=None, *extra):
        return subprocess.run(self.command(release, *extra), text=True, capture_output=True, timeout=15)

    def make_pending(self, status='unknown', intent=True):
        store = Store(self.state / 'runner.sqlite')
        a = store.create('fixture', str(uuid.uuid4()), 'claude')
        store.update(a, state=status, wait={'interactive_prompt_intent': {'token': 'fixture'}} if intent else {})
        store.db.close()

    def test_real_exec_once_then_pending_same_target_restart(self):
        first = self.invoke(None, '--once')
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(json.loads(first.stdout), [])
        self.make_pending()
        binding = (self.state / guard.MARKER).read_bytes()
        second = self.invoke(None, '--once')
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(binding, (self.state / guard.MARKER).read_bytes())
        self.assertTrue(guard.pending(self.state))  # restart did not fake success

    def test_existing_cli_launch_actually_uses_guard(self):
        env = dict(os.environ, PYTHONPATH=str(self.release / 'src'))
        command = [sys.executable, '-m', 'mesh_runner.cli', 'once',
                   '--config', str(self.config), '--log-level', 'WARNING']
        first = subprocess.run(command, env=env, capture_output=True, text=True, timeout=15)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertTrue((self.state / guard.MARKER).is_file())
        self.make_pending()
        second = subprocess.run(command, env=env, capture_output=True, text=True, timeout=15)
        self.assertEqual(second.returncode, 0, second.stderr)
        changed = self.config.read_text() + '\n# changed configured target binding\n'
        self.config.write_text(changed)
        # the same release reads its own journal: a config change enters with the row still pending
        third = subprocess.run(command, env=env, capture_output=True, text=True, timeout=15)
        self.assertEqual(third.returncode, 0, third.stderr)
        self.assertEqual(json.loads((self.state / guard.MARKER).read_text())['config_sha256'],
                         guard.digest(changed.encode()))
        self.assertTrue(guard.pending(self.state))

    def test_actual_controller_sigkill_then_same_target_exec(self):
        p = subprocess.Popen(self.command(), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.children.append(p)
        deadline = time.monotonic() + 10
        while not (self.state / 'runner.sqlite').exists() and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertIsNone(p.poll())
        # Existing singleton really fences a second launch, independently of PID.
        refused = self.invoke(None, '--check')
        self.assertEqual(refused.returncode, 2)
        self.assertIn('runner-already-owns-state', refused.stderr)
        os.kill(p.pid, signal.SIGKILL)
        p.wait(timeout=5)
        self.make_pending()
        restarted = self.invoke(None, '--once')
        self.assertEqual(restarted.returncode, 0, restarted.stderr)
        self.assertTrue(guard.pending(self.state))

    def test_different_target_pending_denied_but_clear_target_allowed(self):
        self.assertEqual(self.invoke(None, '--once').returncode, 0)
        other = self.root / 'other'
        shutil.copytree(self.release, other)
        self.make_pending()
        before = (self.state / guard.MARKER).read_bytes()
        result = self.invoke(other, '--once')
        self.assertEqual(result.returncode, 2)
        self.assertIn('pending-delivery-target-change', result.stderr)
        self.assertEqual((self.state / guard.MARKER).read_bytes(), before)
        store = Store(self.state / 'runner.sqlite')
        for a in store.by_state('fixture', ('unknown',)):
            store.update(a, state='completed', wait={})
        store.db.close()
        self.assertEqual(self.invoke(other, '--once').returncode, 0)

    def own_guard(self, release, *extra):
        program = release / 'src/mesh_runner/launch_guard.py'
        return subprocess.run([sys.executable, str(program), '--target-release', str(release),
                               '--config', str(self.config), *extra], text=True, capture_output=True, timeout=15)

    def test_new_target_takes_pending_rows_over_through_its_own_guard(self):
        self.assertEqual(self.invoke(None, '--once').returncode, 0)
        other = self.root / 'other'
        shutil.copytree(self.release, other)
        # a different tree: the next release
        module = other / 'src/mesh_runner/__init__.py'
        module.write_text(module.read_text() + '\n# next release\n')
        self.make_pending()
        self.make_pending(intent=False)
        result = self.own_guard(other, '--once')
        self.assertEqual(result.returncode, 0, result.stderr)
        marker = json.loads((self.state / guard.MARKER).read_text())
        self.assertEqual(marker['release'], str(other.resolve()))
        self.assertEqual(marker['tree'], guard.target_identity(other))
        self.assertTrue(guard.pending(self.state))          # nothing was declared safe: the rows stay pending
        # and the way back is the same move
        back = self.own_guard(self.release, '--once')
        self.assertEqual(back.returncode, 0, back.stderr)
        self.assertEqual(json.loads((self.state / guard.MARKER).read_text())['release'], str(self.release.resolve()))

    def test_pending_rows_do_not_cross_a_journal_contract_change(self):
        self.assertEqual(self.invoke(None, '--once').returncode, 0)
        other = self.root / 'other'
        shutil.copytree(self.release, other)
        self.make_pending()
        marker = self.state / guard.MARKER
        activated = json.loads(marker.read_text())
        # the previously activated release declared another journal contract
        foreign = self.root / 'foreign'
        (foreign / 'src/mesh_runner').mkdir(parents=True)
        (foreign / 'src/mesh_runner/launch_guard.py').write_text('CAPABILITY = 2\n')
        marker.write_text(json.dumps({**activated, 'release': str(foreign)}))
        result = self.own_guard(other, '--once')
        self.assertEqual(result.returncode, 2)
        self.assertIn('pending-delivery-target-change', result.stderr)
        # ... or is gone, so nobody can say what it wrote
        marker.write_text(json.dumps({**activated, 'release': str(self.root / 'removed')}))
        self.assertEqual(self.own_guard(other, '--once').returncode, 2)
        # ... or belonged to another state
        marker.write_text(json.dumps({**activated, 'state': str(self.root / 'elsewhere')}))
        self.assertEqual(self.own_guard(other, '--once').returncode, 2)
        self.assertTrue(guard.pending(self.state))

    def test_unknown_row_without_intent_also_fences_target_change(self):
        self.assertEqual(self.invoke(None, '--once').returncode, 0)
        self.make_pending(intent=False)
        other = self.root / 'other'
        shutil.copytree(self.release, other)
        self.assertEqual(self.invoke(other, '--once').returncode, 2)

    def test_old_and_unknown_capability_target_denied(self):
        other = self.root / 'old'
        (other / 'src/mesh_runner').mkdir(parents=True)
        self.assertEqual(self.invoke(other, '--once').returncode, 2)
        (other / 'src/mesh_runner/launch_guard.py').write_text('CAPABILITY = 987\n')
        result = self.invoke(other, '--once')
        self.assertEqual(result.returncode, 2)
        self.assertIn('target-capability-unsupported', result.stderr)
        self.assertFalse((self.state / guard.MARKER).exists())

    def test_check_does_not_activate_or_replace_binding(self):
        result = self.invoke(None, '--check')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.state / guard.MARKER).exists())
        self.assertEqual(self.invoke(None, '--once').returncode, 0)
        old = (self.state / guard.MARKER).read_bytes()
        other = self.root / 'other'
        shutil.copytree(self.release, other)
        self.assertEqual(self.invoke(other, '--check').returncode, 0)
        self.assertEqual((self.state / guard.MARKER).read_bytes(), old)

    def rejected_handoff(self, value, fd, config=None):
        env = dict(os.environ, PYTHONPATH=str(self.release / 'src'))
        env[guard.HANDOFF] = json.dumps(value)
        code = 'from mesh_runner.config import load; from mesh_runner.runner import Runner; Runner(load(__import__("sys").argv[1]))'
        result = subprocess.run([sys.executable, '-c', code, str(config or self.config)], env=env,
                                pass_fds=(fd,), capture_output=True, text=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('inherited-singleton-invalid', result.stderr)
        self.assertFalse((self.state / 'runner.sqlite').exists())

    def test_config_cas_race_after_prepare_before_runner_rejected(self):
        lock, value = guard.prepare(self.release, self.config)
        try:
            self.config.write_text(self.config.read_text() + '\n# changed after handoff\n')
            self.rejected_handoff(value, lock.fileno())
        finally:
            lock.close()

    def test_tree_cas_race_after_prepare_before_runner_rejected(self):
        lock, value = guard.prepare(self.release, self.config)
        try:
            p = self.release / 'src/mesh_runner/__init__.py'
            p.write_text(p.read_text() + '\n# changed after handoff\n')
            self.rejected_handoff(value, lock.fileno())
        finally:
            lock.close()

    def test_same_inode_unheld_fd_rejected(self):
        lock, value = guard.prepare(self.release, self.config)
        lock.close()
        fd = os.open(self.state / 'runner.lock', os.O_RDWR)
        try:
            self.rejected_handoff({**value, 'fd': fd}, fd)
        finally:
            os.close(fd)

    def test_wrong_inode_or_fabricated_fd_rejected(self):
        lock, value = guard.prepare(self.release, self.config)
        try:
            fd = os.open(self.root / 'wrong', os.O_CREAT | os.O_RDWR, 0o600)
            try:
                self.rejected_handoff({**value, 'fd': fd}, fd)
                self.rejected_handoff({**value, 'fd': 99999}, fd)
            finally:
                os.close(fd)
        finally:
            lock.close()

    def test_implicit_state_or_unbound_pending_denied(self):
        self.config.write_text('[runner]\nmesh_url="http://invalid.local"\n')
        self.assertEqual(self.invoke(None, '--check').returncode, 2)
        self.config.write_text('[runner]\nmesh_url="http://invalid.local"\nstate_dir=' + json.dumps(str(self.state)) + '\n')
        self.state.mkdir()
        self.make_pending()
        result = self.invoke(None, '--once')
        self.assertEqual(result.returncode, 2)
        self.assertIn('pending-delivery-target-change', result.stderr)
