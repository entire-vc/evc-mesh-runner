"""Real owner recovery plus immutable request/receipt spool and SQLite."""
import copy
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

from fakes import FakeMesh, task
from mesh_runner import delivery_requests as requests, interactive, workspace_requests
from mesh_runner.adapters.base import proc_stamp, interrupt_group
from mesh_runner.runner import AgentRunner
from mesh_runner.store import Store
from test_runner import Bench


class DeliveryRecheck(Bench):
    def setUp(self):
        super().setUp()
        self.t = task(903)
        self.r = AgentRunner(self.cfg, self.agent, Store(self.cfg.state_dir / "runner.sqlite"),
                             mesh=FakeMesh([self.t]), adapter=self.adapter)
        self.r.refresh_me()
        self.a = self.r.store.create('builder', self.t['id'], 'claude')
        log = self.root / 'bound-turn.jsonl'
        self.intent = dict(token=uuid.uuid4().hex, sha256='1' * 64, turn=2, log_path=str(log),
                           kind='headless', harness='claude', session_id=str(uuid.uuid4()), prompt='MANDATORY',
                           source_read_at='2026-11-02T00:00:00Z', prior=dict(turns=1, session_id='', log_path='',
                           turn_started_at='', source_read_at=''))
        self.pending = {'ci_delivery': {'key': 'fixture-ci', 'value': 'failed:903'}}
        self.r.store.update(self.a, state='unknown', turns=2, log_path=str(log), wake_text='MANDATORY',
                            wait={**self.pending, 'interactive_prompt_intent': self.intent})
        self.directory = interactive.agent_dir(self.cfg.state_dir, 'builder')
        self.journal('native-exec-intent')
        self.children = []

    def tearDown(self):
        for p in self.children:
            if p.poll() is None:
                interrupt_group(p.pid, signal.SIGKILL)
            p.wait(timeout=5)
        self.r.store.db.close()
        super().tearDown()

    def journal(self, phase):
        interactive.write_delivery(self.directory, self.a.id, dict(version=1, token=self.intent['token'],
                                   sha256=self.intent['sha256'], phase=phase))

    def completed_native(self):
        self.journal('native-exited')
        Path(self.intent['log_path']).write_text(json.dumps({'type': 'result', 'subtype': 'success',
                        'result': '↻ NEXT', 'session_id': self.intent['session_id']}) + '\n')

    def observed(self):
        return requests.snapshot(self.cfg.state_dir, 'builder', self.a.id)

    def submit(self, observed=None):
        return requests.submit(self.cfg.state_dir, observed or self.observed(), str(uuid.uuid4()), 300)

    def consume(self, submitted):
        requests.consume(self.cfg.state_dir, 'builder', self.r._delivery_recheck_request)
        return requests.submit(self.cfg.state_dir, submitted['request'], submitted['request']['request_id'], 300)['receipt']['status']

    def assert_pending(self):
        a = self.r.store.get(self.a.id)
        self.assertIn('interactive_prompt_intent', a.wait)
        self.assertEqual(a.wake_text, 'MANDATORY')
        self.assertEqual(a.source_read_at, '')
        self.assertEqual(a.wait['ci_delivery'], self.pending['ci_delivery'])
        self.assertIsNone(self.r.store.mark('builder', self.t['id'], 'fixture-ci'))
        return a

    def test_exact_bound_terminal_owner_validates_and_consumes_once(self):
        self.completed_native()
        submitted = self.submit()
        self.assert_pending()  # CLI publication itself has no outcome authority
        self.assertEqual(self.consume(submitted), 'resolved:exact-native-terminal-ack')
        a = self.r.store.get(self.a.id)
        self.assertNotIn('interactive_prompt_intent', a.wait)
        self.assertEqual(a.wake_text, '')
        self.assertEqual(self.r.store.mark('builder', self.t['id'], 'fixture-ci')[0], 'failed:903')
        before = Path(self.intent['log_path']).read_bytes()
        self.assertEqual(self.consume(submitted), 'resolved:exact-native-terminal-ack')
        self.assertEqual(Path(self.intent['log_path']).read_bytes(), before)

    def test_uncertain_exec_explicit_unresolved_not_manual_ack(self):
        result = self.consume(self.submit())
        self.assertEqual(result, 'unresolved:headless-exec-unconfirmed')
        self.assertEqual(self.assert_pending().state, 'unknown')

    def test_durable_unsent_returns_only_to_normal_admission(self):
        self.journal('received')
        self.assertEqual(self.consume(self.submit()), 'resolved:durable-unsent-normal-admission')
        a = self.assert_pending()
        self.assertEqual(a.state, 'ready')
        self.assertTrue(a.wait['interactive_prompt_intent']['retryable'])
        self.assertEqual(self.r.procs, {})

    def test_terminal_evidence_cannot_resolve_while_whole_group_alive(self):
        self.completed_native()
        p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)'], start_new_session=True)
        self.children.append(p)
        self.r.store.update(self.a, pid=p.pid, stamp=proc_stamp(p.pid))
        self.assertEqual(self.consume(self.submit()), 'unresolved:whole-process-group-live')
        self.assert_pending()

    def test_wrong_session_remains_unresolved(self):
        self.completed_native()
        path = Path(self.intent['log_path'])
        path.write_text(path.read_text().replace(self.intent['session_id'], str(uuid.uuid4())))
        self.assertEqual(self.consume(self.submit()), 'unresolved:headless-native-session-mismatch')
        self.assert_pending()

    def test_evidence_changed_after_request_rejected(self):
        submitted = self.submit()
        self.completed_native()
        self.assertEqual(self.consume(submitted), 'rejected_evidence_changed')
        self.assert_pending()

    def test_each_owner_identity_field_is_fenced(self):
        for key, value in [('agent', 'other'), ('task', str(uuid.uuid4())), ('attempt', str(uuid.uuid4())),
                           ('token', 'f' * 32), ('turn', 99), ('generation', 'f' * 64)]:
            submitted = self.submit()
            r = {**submitted['request'], key: value}
            result = self.r._delivery_recheck_request(r)
            self.assertIn(result, ('rejected_owner', 'rejected_identity'))
            self.assert_pending()

    def test_expired_and_stale_request_ids_never_extend_authority(self):
        submitted = self.submit()
        r = {**submitted['request'], 'expires': int(time.time()) - 1}
        self.assertEqual(self.r._delivery_recheck_request(r), 'rejected_expired')
        self.completed_native()
        with self.assertRaisesRegex(ValueError, 'already-bound'):
            requests.submit(self.cfg.state_dir, self.observed(), submitted['request']['request_id'], 300)
        self.assert_pending()

    def test_cli_snapshot_and_bound_request_never_write_registry(self):
        key = self.root / 'fixture.key'
        key.write_text('fixture-key')
        key.chmod(0o600)
        config = self.root / 'runner.toml'
        config.write_text('[runner]\nmesh_url="http://invalid.local"\nstate_dir=' + json.dumps(str(self.cfg.state_dir)) +
                          '\n[[agents]]\nname="builder"\nharness="claude"\nprofile_dir=' + json.dumps(str(self.root / 'profile')) +
                          '\nkey_file=' + json.dumps(str(key)) + '\n')
        code = [sys.executable, '-m', 'mesh_runner.cli', 'recheck-delivery', '--config', str(config),
                '--agent', 'builder', '--attempt', self.a.id]
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / 'src'))
        observed = subprocess.run(code, text=True, capture_output=True, env=env, timeout=10)
        self.assertEqual(observed.returncode, 0, observed.stderr)
        value = json.loads(observed.stdout)
        request = code + ['--submit', '--task', value['task'], '--token', value['token'], '--turn', str(value['turn']),
                          '--evidence-sha256', value['evidence_sha256'], '--request-id', str(uuid.uuid4())]
        published = subprocess.run(request, text=True, capture_output=True, env=env, timeout=10)
        self.assertEqual(published.returncode, 0, published.stdout + published.stderr)
        submitted = json.loads(published.stdout)
        self.assert_pending()
        self.r.tick()  # The normal owning tick consumes the CLI spool, not a second writer.
        root = workspace_requests.directory(self.cfg.state_dir, 'builder', requests.SPOOL)
        receipt = json.loads((root / (submitted['request']['request_id'] + '.receipt.json')).read_text())
        self.assertEqual(receipt['status'], 'unresolved:headless-exec-unconfirmed')
        self.assert_pending()

    def test_native_evidence_hash_is_not_outcome_attestation(self):
        observed = self.observed()
        self.assertEqual(len(observed['evidence_sha256']), 64)
        result = self.consume(self.submit(observed))
        self.assertTrue(result.startswith('unresolved:'))
        self.assert_pending()


class UnprovenIsolation(DeliveryRecheck):
    """S2: unresolved acceptance stays honest, isolated, idempotent and explained; never replayed."""

    def comments(self):
        return [c for c in self.r.mesh.comments if 'MANDATORY DELIVERY PENDING' in str(c)] \
            if hasattr(self.r.mesh, 'comments') else []

    def restart(self):
        self.r.store.db.close()
        self.r = AgentRunner(self.cfg, self.agent, Store(self.cfg.state_dir / 'runner.sqlite'),
                             mesh=FakeMesh([self.t]), adapter=self.adapter)
        self.r.refresh_me()

    def test_missing_ack_and_empty_log_never_prove_unsent(self):
        Path(self.intent['log_path']).write_text('')          # empty transcript
        for _ in range(2):                                     # recheck is repeatable
            self.assertEqual(self.consume(self.submit()), 'unresolved:headless-exec-unconfirmed')
            a = self.assert_pending()
            self.assertEqual(a.state, 'unknown')
            self.assertNotIn('retryable', a.wait['interactive_prompt_intent'])
        self.assertEqual(self.r.procs, {})

    def test_foreign_and_conflicting_terminals_stay_unresolved(self):
        self.completed_native()
        path = Path(self.intent['log_path'])
        line = path.read_text()
        path.write_text(line + line)                           # two terminals: conflicting evidence
        self.assertTrue(self.consume(self.submit()).startswith('unresolved:'))
        path.write_text(line.replace(self.intent['session_id'], str(uuid.uuid4())))  # foreign session
        self.assertTrue(self.consume(self.submit()).startswith('unresolved:'))
        self.assert_pending()

    def test_stale_request_after_new_evidence_is_rejected_not_applied(self):
        stale = self.submit()
        Path(self.intent['log_path']).write_text('{"type":"noise"}\n')
        self.assertEqual(self.consume(stale), 'rejected_evidence_changed')
        self.assertEqual(self.assert_pending().state, 'unknown')

    def test_recheck_without_new_evidence_is_idempotent_across_restart(self):
        self.consume(self.submit())
        before = self.r.store.get(self.a.id)
        events = len(self.r.store.db.execute('SELECT 1 FROM events WHERE attempt=?', (self.a.id,)).fetchall()) \
            if self._has_events() else 0
        self.restart()
        self.consume(self.submit())
        self.r.tick()
        after = self.r.store.get(self.a.id)
        self.assertEqual((after.state, after.wait['interactive_prompt_intent']['token']),
                         (before.state, before.wait['interactive_prompt_intent']['token']))
        self.assertEqual(self.r.procs, {})
        if self._has_events():
            self.assertEqual(len(self.r.store.db.execute('SELECT 1 FROM events WHERE attempt=?', (self.a.id,)).fetchall()), events)
        self.assert_pending()

    def _has_events(self):
        return bool(self.r.store.db.execute("SELECT 1 FROM sqlite_master WHERE name='events'").fetchone())

    def test_status_explains_missing_evidence_and_next_action(self):
        observed = self.observed()
        self.assertEqual(observed['state'], 'unknown')
        info = observed['explanation']
        self.assertEqual(info['replay'], 'forbidden')
        self.assertIn('recheck-delivery', info['next_action'])
        self.assertIn('other lanes keep running', info['scope'])
        row = next(x for x in self.r.status()['attempts'] if x['task_id'] == self.t['id'])
        self.assertEqual(row['prompt_delivery']['explanation'], delivery_explain(row['prompt_delivery']['reason']))

    def test_explanation_never_appears_for_resolved_ready_attempt(self):
        self.journal('received')
        self.consume(self.submit())
        row = next(x for x in self.r.status()['attempts'] if x['task_id'] == self.t['id'])
        self.assertNotIn('explanation', row['prompt_delivery'])
        self.assertEqual(row['state'], 'ready')


def delivery_explain(reason):
    from mesh_runner import delivery_recovery
    return delivery_recovery.explain(reason)
