"""Activation of a target with legacy codex bootstrap rows (`unknown`, `session-unbound`) pending.

Real SQLite rows, real journals and stderr on disk, the real guard process; no internal mocks."""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

from mesh_runner import delivery_recovery, interactive, launch_guard as guard
from mesh_runner.store import Store

SOURCE = Path(guard.__file__).resolve().parents[2]
LINE = ("interactive: codex exited during start-up, starting again (2/2) in 30s: Error: account/read failed "
        "during TUI bootstrap: account/read failed: workspace routing discovery unauthorized (401) (code -32603)")


class LegacyRows(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.old = self.root / 'old'
        shutil.copytree(SOURCE / 'src', self.old / 'src', ignore=shutil.ignore_patterns('__pycache__'))
        self.new = self.root / 'new'
        shutil.copytree(self.old, self.new)
        self.state = self.root / 'state'
        self.config = self.root / 'runner.toml'
        self.config.write_text('[runner]\nmesh_url="http://invalid.local"\nstate_dir=' + json.dumps(str(self.state)) + '\n')
        self.home = self.root / 'codex-home'
        self.home.mkdir()
        self.env = dict(os.environ, CODEX_HOME=str(self.home))
        self.assertEqual(self.run_guard(self.old, self.old, '--once').returncode, 0)   # the installed target
        self.rows = []

    def run_guard(self, guard_of, target, *extra):
        return subprocess.run([sys.executable, str(guard_of / 'src/mesh_runner/launch_guard.py'),
                               '--target-release', str(target), '--config', str(self.config), *extra],
                              text=True, capture_output=True, timeout=30, env=self.env)

    def row(self, agent='howard', *, stderr=LINE + '\n', state='unknown', resolution='session-unbound',
            pid=0, kind='interactive', **journal):
        store = Store(self.state / 'runner.sqlite')
        a = store.create(agent, str(uuid.uuid4()), 'codex')
        log = self.root / f'{a.id}.codex.jsonl'
        log.write_text('')
        if stderr is not None:
            log.with_suffix('.stderr').write_text(stderr)
        token, sha = uuid.uuid4().hex, 'sha'
        cwd = self.root / 'work'
        cwd.mkdir(exist_ok=True)
        interactive.write_delivery(interactive.agent_dir(self.state, agent), a.id, {
            'token': token, 'sha256': sha, 'phase': 'send-intent', 'harness': 'codex', 'session_id': '',
            'cwd': str(cwd), 'native_base': 0,
            'native_prefix': hashlib.sha256(json.dumps([], sort_keys=True).encode()).hexdigest(), **journal})
        intent = {'token': token, 'sha256': sha, 'prompt': 'do it', 'log_path': str(log), 'harness': 'codex',
                  'resolution': resolution}
        if kind is not None:
            intent['kind'] = kind
        store.update(a, state=state, pid=pid, wait={'interactive_prompt_intent': intent})
        store.db.close()
        self.rows.append((agent, a.id, intent))

    def test_target_is_entered_with_exactly_the_legacy_rows_and_they_reconcile(self):
        self.row('howard')
        self.row('garfield')
        self.assertTrue(guard.pending(self.state))
        self.assertEqual(self.run_guard(self.old, self.new, '--check').returncode, 2)     # old guard: still refuses
        before = (self.state / guard.MARKER).read_bytes()
        checked = self.run_guard(self.new, self.new, '--check')
        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertEqual((self.state / guard.MARKER).read_bytes(), before)                # check never binds
        activated = self.run_guard(self.new, self.new, '--once')
        self.assertEqual(activated.returncode, 0, activated.stderr)
        self.assertEqual(json.loads((self.state / guard.MARKER).read_text())['release'], str(self.new.resolve()))
        for agent, key, intent in self.rows:                                             # the ordinary recovery path
            self.assertEqual(delivery_recovery.recover(interactive.agent_dir(self.state, agent), key, intent,
                                                       agent, object()), 'unsent')

    def test_typed_bootstrap_failed_phase_row_is_covered(self):
        self.row(stderr=None, phase='bootstrap-failed', bootstrap_error='Error: account/read failed during TUI bootstrap')
        self.assertEqual(self.run_guard(self.new, self.new, '--check').returncode, 0)

    def refused(self):
        result = self.run_guard(self.new, self.new, '--check')
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn('pending-delivery-target-change', result.stderr)

    def test_row_without_typed_stderr_proof_refuses(self):
        self.row('howard')
        self.row('garfield', stderr=None)
        self.refused()

    def test_stderr_that_only_mentions_the_phrase_refuses(self):
        self.row(stderr='prompt mentions failed during TUI bootstrap\n')
        self.refused()

    def test_finished_turn_or_rollout_for_the_cwd_refuses(self):
        self.row(stderr=LINE + '\ntiming: session=1.0\n')
        self.refused()

    def test_bound_session_refuses(self):
        self.row(session_id='s-1')
        self.refused()

    def test_foreign_pending_row_refuses_even_next_to_legacy_ones(self):
        self.row('howard')
        store = Store(self.state / 'runner.sqlite')
        a = store.create('other', str(uuid.uuid4()), 'claude')
        store.update(a, state='unknown', wait={'interactive_prompt_intent': {'token': 'x', 'resolution': 'acceptance-unproven'}})
        store.db.close()
        self.refused()

    def test_in_flight_intent_refuses(self):
        self.row('howard')
        self.row('garfield', state='running', resolution='')
        self.refused()

    def test_live_process_row_refuses(self):
        self.row(pid=os.getpid())
        self.refused()

    def test_missing_or_other_kind_refuses(self):
        for kind in (None, 'headless', 'weird'):
            self.row('howard', kind=kind)
            self.refused()
            store = Store(self.state / 'runner.sqlite')
            for a in store.by_state('howard', ('unknown',)):
                store.update(a, state='done', wait={})
            store.db.close()

    def test_unknown_row_without_intent_refuses(self):
        self.row('howard')
        store = Store(self.state / 'runner.sqlite')
        a = store.create('bare', str(uuid.uuid4()), 'codex')
        store.update(a, state='unknown', wait={})
        store.db.close()
        self.refused()


if __name__ == '__main__':
    unittest.main()
