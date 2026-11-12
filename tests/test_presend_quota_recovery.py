"""Real native driver journal → recovery → SQLite Runner admission boundary."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import subprocess
import unittest

from mesh_runner import delivery_recovery, interactive, launch_guard, tui_driver as td
from mesh_runner.config import AgentConfig, RunnerConfig
from mesh_runner.runner import AgentRunner
from mesh_runner.store import Store
from test_quota_menu_footer import FRAME, CapturedTmux
from test_runner import Bench
from fakes import FakeMesh, FakeProfileAdapter, calls, task


class PresendQuota(unittest.TestCase):
    def test_real_presend_failure_preserves_prompt_and_requires_fresh_capacity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); state = root / 'state'; state.mkdir()
            profile = root / 'profile'; profile.mkdir()
            log = root / 'turn.jsonl'; log.touch()
            called = root / 'on-limit.json'
            switch = root / 'switch.py'
            # External configured operator process. It proves current failed log is
            # available before prior log restoration; no account or provider access.
            switch.write_text('import json, pathlib, sys\np=pathlib.Path(' + repr(str(log)) + ')\n'
                              'events=[json.loads(x) for x in p.read_text().splitlines()]\n'
                              'assert any(e.get("is_error") and "usage limit" in e.get("result", "") for e in events)\n'
                              'pathlib.Path(' + repr(str(called)) + ').write_text("called")\n'
                              'print("no capacity");sys.exit(1)\n')
            agent = AgentConfig(name='Steward', key='fixture-key', harness='claude', profile_dir=profile, repos=[],
                                on_limit_command=[sys.executable, str(switch)])
            cfg = RunnerConfig(mesh_url='http://127.0.0.1:1', state_dir=state, agents=[agent])
            store = Store(state / 'runner.sqlite'); self.addCleanup(store.db.close)
            runner = AgentRunner(cfg, agent, store)
            attempt = store.create(agent.name, 'fixture-task', 'claude')
            prompt = 'exact pending prompt and wake; never consumed'
            intent = {'kind': 'interactive', 'token': 'fixture-token', 'sha256': hashlib.sha256(prompt.encode()).hexdigest(),
                      'harness': 'claude', 'prompt': prompt, 'log_path': str(log), 'turn': 2,
                      'resume': True, 'workspaces': [], 'source_read_at': 'source-pending',
                      'prior': {'turns': 1, 'log_path': str(root / 'prior.jsonl')}}
            wait = {'interactive_prompt_intent': intent,
                    'queue_delivery': {'key': 'queue-key', 'value': 'pending', 'floor': 'pending-floor'}}
            store.update(attempt, state='running', turns=2, log_path=str(log), wait=wait,
                         wake_text='pending wake', source_read_at='previous-source')
            store.mark(agent.name, attempt.task_id, 'gate', json.dumps({'reservation': 'owned-fixture'}))
            driver_dir = interactive.agent_dir(state, agent.name); driver_dir.mkdir(parents=True, exist_ok=True)
            journal = {'version': 1, 'token': intent['token'], 'sha256': intent['sha256'], 'phase': 'received'}
            interactive.write_delivery(driver_dir, attempt.id, journal)
            boundary = CapturedTmux(FRAME)
            with log.open('w') as output:
                driver = td.Driver('claude', agent.name, driver_dir, boundary, 1, profile, 'fixture-session',
                                   'resume', attempt.id, ['claude'], out=output,
                                   env={'_MESH_RUNNER_PROMPT_INTENT': intent['token'],
                                        '_MESH_RUNNER_PROMPT_SHA256': intent['sha256']})
                with self.assertRaises(td.DriverError) as error:
                    driver.turn(prompt)
                driver.emit_error(str(error.exception))  # same real error emitter used by Driver.run
            self.assertEqual(delivery_recovery.recover(driver_dir, attempt.id, intent, agent.name, boundary), 'quota-unsent')
            self.assertFalse(runner._reconcile_interactive_prompt(store.get(attempt.id)))
            current = store.get(attempt.id)
            self.assertEqual(current.state, 'ready')
            self.assertEqual(current.turns, 1)
            self.assertEqual(current.log_path, intent['prior']['log_path'])
            self.assertEqual(current.wait['interactive_prompt_intent'], dict(intent, retryable=True))
            self.assertEqual(current.wait['queue_delivery'], wait['queue_delivery'])
            self.assertEqual(current.wake_text, 'pending wake')
            self.assertEqual(current.source_read_at, 'previous-source')
            self.assertFalse(store.mark(agent.name, attempt.task_id, 'queue-key'))
            self.assertTrue(runner._quota_admission_required(current))
            self.assertFalse(runner._gate_of(current))
            self.assertTrue(called.exists())
            self.assertNotIn('mesh_runner.prompt_ack', log.read_text())
            self.assertTrue(all(call[0] == 'capture-pane' for call in boundary.calls))
            reason, _ = runner._gate('reserve', current.task_id, current.mode, current)
            self.assertIn('requires a capable start gate', reason)
            # Real configured gate subprocess: a generic success cannot bypass
            # mandatory fresh capacity; only its explicit capacity receipt can.
            agent.start_gate_command = [sys.executable, '-c', 'print("ACCOUNT_CAPACITY_READY=0")']
            self.assertIn('not confirmed', runner._gate('reserve', current.task_id, current.mode, current)[0])
            agent.start_gate_command = [sys.executable, '-c', 'import os;assert os.environ["MESH_RUNNER_REQUIRE_ACCOUNT_CAPACITY"]=="1";print("ACCOUNT_CAPACITY_READY=1")']
            self.assertEqual(runner._gate('reserve', current.task_id, current.mode, current)[0], '')
            # Canonical drain/ownership-loss recovery also consumes the same
            # exact unsent proof, without acknowledging or replaying the prompt.
            store.db.commit()
            self.assertFalse(launch_guard.pending(state))
            store.update(current, state='completed')
            self.assertTrue(runner._retire_proven_unsent_intent(store.get(current.id)))
            self.assertNotIn('interactive_prompt_intent', store.get(current.id).wait)
            self.assertNotIn('mesh_runner.prompt_ack', log.read_text())

    def test_untyped_result_text_does_not_authorize_quota_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); profile = root / 'profile'; profile.mkdir()
            agent = AgentConfig(name='Steward', key='fixture', harness='claude', profile_dir=profile, repos=[])
            cfg = RunnerConfig(mesh_url='http://127.0.0.1:1', state_dir=root / 'state', agents=[agent])
            store = Store(cfg.state_dir / 'runner.sqlite'); self.addCleanup(store.db.close)
            runner = AgentRunner(cfg, agent, store)
            attempt = store.create(agent.name, 'untyped-task', 'claude')
            log = root / 'turn.jsonl'; log.write_text(json.dumps({'type': 'result', 'is_error': True, 'result': 'usage limit reached'}) + '\n')
            intent = {'token': 'token', 'sha256': 'f' * 64, 'harness': 'claude', 'log_path': str(log),
                      'prior': {'turns': 0, 'log_path': ''}, 'prompt': 'exact pending'}
            store.update(attempt, state='running', log_path=str(log), wait={'interactive_prompt_intent': intent})
            driver_dir = interactive.agent_dir(cfg.state_dir, agent.name); driver_dir.mkdir(parents=True, exist_ok=True)
            interactive.write_delivery(driver_dir, attempt.id, {'token': 'token', 'sha256': 'f' * 64, 'phase': 'received'})
            self.assertFalse(runner._reconcile_interactive_prompt(store.get(attempt.id)))
            current = store.get(attempt.id)
            self.assertEqual(current.state, 'ready')
            self.assertFalse(runner._quota_admission_required(current))
            self.assertEqual(current.wait['interactive_prompt_intent']['prompt'], 'exact pending')

    def test_quota_proof_binds_the_actual_pending_text(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prompt = 'exact original pending request'
            intent = {'token': 'token', 'sha256': hashlib.sha256(prompt.encode()).hexdigest(),
                      'harness': 'claude', 'prompt': prompt}
            journal = dict(intent, phase='pre-send-failed', failure_kind='quota')
            interactive.write_delivery(root, 'attempt', journal)
            for changed in ('different request', '', None, 42):
                with self.subTest(prompt=changed):
                    invalid = dict(intent, prompt=changed)
                    self.assertFalse(delivery_recovery.proven_quota_unsent(root, 'attempt', invalid))
                    self.assertFalse(delivery_recovery.proven_unsent(root, 'attempt', invalid))
                    self.assertNotEqual(delivery_recovery.recover(
                        root, 'attempt', invalid, 'Steward', CapturedTmux(FRAME)), 'quota-unsent')
            self.assertTrue(delivery_recovery.proven_quota_unsent(root, 'attempt', intent))

    def test_unknown_failure_wrong_identity_or_other_harness_is_not_quota_proof(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            intent = {'token': 'token', 'sha256': 'f' * 64, 'harness': 'claude'}
            valid = dict(intent, phase='pre-send-failed', failure_kind='quota')
            for changes in ({'failure_kind': 'auth'}, {'harness': 'codex'}, {'token': 'other'}, {'sha256': 'e' * 64}):
                with self.subTest(changes=changes):
                    interactive.write_delivery(root, 'attempt', dict(valid, **changes))
                    self.assertFalse(delivery_recovery.proven_unsent(root, 'attempt', intent))
                    self.assertNotEqual(delivery_recovery.recover(root, 'attempt', intent, 'Steward', CapturedTmux(FRAME)), 'quota-unsent')




class QuotaSessionTmux(CapturedTmux):
    """External tmux boundary: an existing idle quota pane can only be read or closed."""
    def __init__(self, closed):
        super().__init__(FRAME)
        self.closed = closed

    def run(self, *args, **kwargs):
        if args[0] == 'list-panes':
            self.calls.append(args)
            return subprocess.CompletedProcess(args, int(self.closed.exists()), '0', '')
        if args[0] == 'kill-window':
            self.calls.append(args)
            self.closed.touch()
            return subprocess.CompletedProcess(args, 0, '', '')
        return super().run(*args, **kwargs)


class QuotaHandoff(Bench):
    def prepared(self):
        card = task(1, cat='in_progress', description='Preserve the completed workspace work.')
        mesh = FakeMesh([card])
        route = self.root / 'route'
        route.write_text('codex')
        self.agent.alt = {'codex': {'profile': 'fixture'}}
        self.agent.env_command = [sys.executable, '-c',
            'from pathlib import Path;print("MESH_RUNNER_HARNESS="+Path(' + repr(str(route)) + ').read_text())']
        closed, released, switched = (self.root / name for name in ('old-pane-closed', 'old-claim-released', 'switched'))
        gate = self.root / 'capacity'
        gate.write_text('1')
        self.agent.start_gate_command = [sys.executable, '-c',
            'import os;from pathlib import Path;'
            'assert Path(' + repr(str(released)) + ').exists();'
            'assert Path(' + repr(str(switched)) + ').exists();'
            'assert os.environ.get("MESH_RUNNER_REQUIRE_ACCOUNT_CAPACITY")=="1";'
            'print("ACCOUNT_CAPACITY_READY="+Path(' + repr(str(gate)) + ').read_text())']
        self.agent.turn_end_command = [sys.executable, '-c',
            'import os,sys;from pathlib import Path;'
            'sys.exit(0) if os.environ["MESH_RUNNER_OUTCOME"]!="pre-send-quota" else None;'
            'assert Path(' + repr(str(closed)) + ').exists();'
            'assert os.environ["MESH_RUNNER_GATE_RESERVATION"]=="old-owned-claim";'
            'assert os.environ["MESH_RUNNER_OUTCOME"]=="pre-send-quota";'
            'Path(' + repr(str(released)) + ').touch()']
        r = self.runner(mesh); r.me = mesh.me()
        r.alt_adapters = {'codex': FakeProfileAdapter(r.adapter.script, 'fixture', native='codex')}
        a = r.store.create('builder', card['id'], 'claude')
        paths = [str(r.pool.lease(a.id, 'app'))]
        (Path(paths[0]) / 'pending-work.txt').write_text('previous uncommitted work')
        prompt = 'EXACT PENDING REQUEST: retain this instruction once.'
        log = self.root / 'failed-quota.jsonl'
        self.agent.on_limit_command = [sys.executable, '-c',
            'from pathlib import Path;'
            'assert Path(' + repr(str(released)) + ').exists();'
            'assert "weekly limit" in Path(' + repr(str(log)) + ').read_text();'
            'Path(' + repr(str(switched)) + ').touch()']
        r.store.mark('builder', a.task_id, 'gate', json.dumps({'RESERVATION': 'old-owned-claim'}))
        intent = {'kind': 'interactive', 'token': 'quota-token', 'sha256': hashlib.sha256(prompt.encode()).hexdigest(),
                  'harness': 'claude', 'prompt': prompt, 'log_path': str(log), 'turn': 2, 'resume': True,
                  'workspaces': paths, 'source_read_at': 'pending-source',
                  'prior': {'turns': 1, 'session_id': 'old-claude-session', 'log_path': ''}}
        r.store.update(a, state='running', turns=2, workspaces=paths, session_id='old-claude-session',
                       log_path=str(log), wake_text='pending wake', wait={'interactive_prompt_intent': intent,
                       'queue_delivery': {'key': 'queue-key', 'value': 'pending', 'floor': 'pending-floor'}})
        directory = interactive.agent_dir(self.cfg.state_dir, 'builder'); directory.mkdir(parents=True, exist_ok=True)
        interactive.write_delivery(directory, a.id, dict(token=intent['token'], sha256=intent['sha256'], phase='received'))
        with log.open('w') as out:
            d = td.Driver('claude', 'builder', directory, QuotaSessionTmux(closed), 1, self.agent.profile_dir,
                          a.session_id, 'resume', a.id, ['claude'], out=out,
                          env={'_MESH_RUNNER_PROMPT_INTENT': intent['token'], '_MESH_RUNNER_PROMPT_SHA256': intent['sha256']})
            d.meta = {'harness': 'claude', 'digest': d.digest(), 'session_id': a.session_id}
            d.save()
            self.assertEqual(d.run(prompt), 1)
        self.assertTrue(closed.exists())
        self.assertFalse(released.exists())
        self.assertFalse(r._reconcile_interactive_prompt(r.store.get(a.id)))
        self.assertTrue(released.exists())
        self.assertTrue(switched.exists())
        return r, r.store.get(a.id), prompt, directory, gate, route

    def assert_handoff(self, r, a, prompt):
        old = dict(a.wait['interactive_prompt_intent'])
        r._resume_one(a)
        current = r.store.get(a.id)
        self.assertEqual(current.state, 'running')
        self.assertEqual(current.harness, 'codex')
        self.assertNotEqual(current.session_id, 'old-claude-session')
        self.assertTrue(current.wait['queue_delivery'])
        self.assertEqual(current.wake_text, 'pending wake')
        self.assertFalse(r.store.mark('builder', a.task_id, 'queue-key'))
        pending = current.wait['interactive_prompt_intent']
        self.assertNotEqual(pending['token'], old['token'])
        self.assertEqual(pending['prompt'].count(prompt), 1)
        self.assertEqual(pending['prompt'].count('pending wake'), 1)
        self.assertIn('running on the codex harness', pending['prompt'])
        self.assertIn(a.task_id, pending['prompt'])
        self.assertIn(current.workspaces[0], pending['prompt'])
        self.assertEqual((Path(current.workspaces[0]) / 'pending-work.txt').read_text(), 'previous uncommitted work')
        self.assertEqual(r.procs[a.id].wait(timeout=10), 0)
        seen = calls(self.calls)
        self.assertEqual(len(seen), 1)
        self.assertFalse(seen[0]['resume'])
        self.assertIn(prompt, seen[0]['prompt'])
        self.assertTrue(r._reconcile_interactive_prompt(r.store.get(a.id)))
        self.assertNotIn('interactive_prompt_intent', r.store.get(a.id).wait)
        self.assertEqual(r.store.mark('builder', a.task_id, 'queue-key')[0], 'pending')

    def test_real_quota_unsent_continues_on_admitted_codex(self):
        r, a, prompt, *_ = self.prepared()
        self.assert_handoff(r, a, prompt)

    def test_crash_after_harness_switch_before_new_outbox_recovers_once(self):
        r, a, prompt, *_ = self.prepared()
        # Existing _spawn persists this state before writing the replacement outbox.
        r.store.update(a, harness='codex', session_id='')
        self.assert_handoff(r, r.store.get(a.id), prompt)

    def test_denied_capacity_preserves_exact_old_outbox(self):
        r, a, _, directory, gate, _ = self.prepared()
        before = interactive.read_delivery(directory, a.id)
        gate.write_text('0')
        r._resume_one(a)
        current = r.store.get(a.id)
        self.assertEqual(current.harness, 'claude')
        self.assertEqual(current.wait['interactive_prompt_intent'], a.wait['interactive_prompt_intent'])
        self.assertEqual(interactive.read_delivery(directory, a.id), before)
        self.assertFalse(calls(self.calls))

    def test_unproven_or_changed_scope_cannot_handoff(self):
        r, a, _, directory, _, _ = self.prepared()
        original_wait = json.loads(json.dumps(a.wait))
        original_journal = interactive.read_delivery(directory, a.id)
        variants = ('send-intent', 'wrong-token', 'wrong-sha', 'wrong-failure', 'changed-workspace',
                    'missing-mark', 'live-pid', 'foreign-session', 'changed-prompt')
        for variant in variants:
            with self.subTest(variant=variant):
                wait = json.loads(json.dumps(original_wait)); journal = dict(original_journal)
                harness, session, pid = 'claude', 'old-claude-session', 0
                if variant == 'send-intent': journal['phase'] = 'send-intent'
                if variant == 'wrong-token': journal['token'] = 'different'
                if variant == 'wrong-sha': journal['sha256'] = '0' * 64
                if variant == 'wrong-failure': journal['failure_kind'] = 'auth'
                if variant == 'changed-prompt': wait['interactive_prompt_intent']['prompt'] += ' altered'
                if variant == 'changed-workspace': wait['interactive_prompt_intent']['workspaces'] = ['/other']
                if variant == 'live-pid': pid = __import__('os').getpid()
                if variant == 'foreign-session': harness, session = 'codex', 'unrelated-session'
                r.store.mark('builder', a.task_id, 'quota_admission_required', '' if variant == 'missing-mark' else a.id)
                interactive.write_delivery(directory, a.id, journal)
                r.store.update(r.store.get(a.id), state='ready', wait=wait, harness=harness, session_id=session, pid=pid)
                try:
                    r._resume_one(r.store.get(a.id))
                    self.assertEqual(r.store.get(a.id).state, 'ready' if variant == 'missing-mark' else 'unknown')
                    self.assertFalse(calls(self.calls))
                    self.assertEqual(interactive.read_delivery(directory, a.id), journal)
                finally:
                    r.store.update(r.store.get(a.id), pid=0)


if __name__ == '__main__':
    unittest.main()
