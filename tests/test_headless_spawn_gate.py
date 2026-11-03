"""Real headless handoff/SQLite/crash tests; Mesh/native CLI are external fixtures."""
import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

from fakes import FakeMesh, task
from mesh_runner.adapters.base import group_alive, interrupt_group, proc_stamp, read_events
from mesh_runner import interactive, spawn_gate
from mesh_runner.adapters.claude import ClaudeAdapter
from mesh_runner.adapters.codex import CodexAdapter
from test_runner import Bench

NATIVE = r'''
import json, os, re, subprocess, sys, time, uuid
root = os.environ['HEADLESS_ROOT']
def record(name, value):
    with open(os.path.join(root, name), 'a') as fh:
        fh.write(json.dumps(value) + '\n'); fh.flush(); os.fsync(fh.fileno())
record('starts.jsonl', {'pid': os.getpid(), 'group': os.getpgrp()})
args = sys.argv[1:]
prompt = sys.stdin.read()
record('prompts.jsonl', prompt)
time.sleep(float(os.environ.get('HEADLESS_SLOW', '0')))
if '--session-id' in args:
    sid = args[args.index('--session-id') + 1]
elif '--resume' in args:
    sid = args[args.index('--resume') + 1]
else:
    sid = args[args.index('resume') + 1] if 'resume' in args else str(uuid.uuid4())
if os.environ.get('HEADLESS_BG'):
    subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
mode = os.environ.get('HEADLESS_TERMINAL', '')
if os.environ.get('HEADLESS_ACCEPT'):
    if os.environ['HEADLESS_KIND'] == 'claude':
        settings = json.load(open(args[args.index('--settings') + 1]))
        for group in settings['hooks']['UserPromptSubmit']:
            for hook in group['hooks']:
                code = subprocess.run(hook['command'], shell=True, input=json.dumps({
                    'hook_event_name': 'UserPromptSubmit', 'session_id': sid, 'prompt': prompt}), text=True).returncode
                if code == 2:
                    sys.exit(1)
    else:
        print(json.dumps({'type': 'thread.started', 'thread_id': sid}), flush=True)
        print(json.dumps({'type': 'turn.started'}), flush=True)
if os.environ.get('HEADLESS_BLOCK_AFTER_ACCEPT'):
    record('acceptances.jsonl', {'session_id': sid})
    while not os.path.exists(os.path.join(root, 'release-native')):
        time.sleep(.01)
if mode == 'no-terminal':
    sys.exit(1)
if mode == 'wrong-session':
    sid = str(uuid.uuid4())
if os.environ['HEADLESS_KIND'] == 'claude':
    print(json.dumps({'type': 'system', 'subtype': 'init', 'session_id': sid}))
    terminal = {'type': 'result', 'subtype': 'error_during_execution' if mode else 'success',
                'is_error': bool(mode), 'result': 'fixture native turn failed' if mode else '↻ NEXT', 'session_id': sid}
    print(json.dumps(terminal))
    if mode == 'multiple-terminal':
        print(json.dumps(terminal))
else:
    if not os.environ.get('HEADLESS_ACCEPT'):
        print(json.dumps({'type': 'thread.started', 'thread_id': sid}))
    print(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': '↻ NEXT'}}))
    terminal = {'type': 'turn.failed', 'error': {'message': 'fixture native turn failed'}} if mode else {
        'type': 'turn.completed', 'usage': {'input_tokens': 1, 'output_tokens': 1}}
    print(json.dumps(terminal))
    if mode == 'multiple-terminal':
        print(json.dumps(terminal))
'''


class Boundary:
    def session_known(self, sid, cwd, env):
        return True                  # external CLI fixture supports the supplied saved session

    def spawn(self, launch, log_path):
        callback = launch.on_created
        if self.stage in ('before-register', 'after-register'):
            def stopped(proc):
                if self.stage == 'after-register':
                    callback(proc)
                self.witness.write_text(json.dumps({'pid': proc.pid, 'stamp': proc_stamp(proc.pid)}))
                os.kill(os.getpid(), signal.SIGSTOP)
                if self.stage == 'before-register':
                    callback(proc)
            launch.on_created = stopped
        elif self.stage == 'exec-denied':
            def denied(proc):
                callback(proc)
                Path(self.binary).chmod(0o600)
            launch.on_created = denied
        return super().spawn(launch, log_path)


class BoundaryClaude(Boundary, ClaudeAdapter):
    pass


class BoundaryCodex(Boundary, CodexAdapter):
    pass


class HeadlessFixture(Bench):
    mandatory = 'MANDATORY_EVENT_903: continue after exact failed pipeline observation'
    pending = {
        'ci_delivery': {'key': 'fixture-ci', 'value': 'sha:903:failed'},
        'ci_action_delivery': {'key': 'fixture-action', 'value': 'retry:903'},
        'queue_delivery': {'key': 'fixture-queue', 'value': 'queue:903', 'floor': '903'},
    }

    def setUp(self):
        super().setUp()
        self.native = self.root / 'headless-native'
        self.native.write_text('#!%s\n%s' % (sys.executable, NATIVE))
        self.native.chmod(0o700)
        self.fixture_runners = []

    def configure(self, kind):
        self.agent.harness = kind
        self.agent.env.update(HOME=str(self.root), HEADLESS_ROOT=str(self.root), HEADLESS_KIND=kind,
                              HEADLESS_SLOW='1')
        self.agent.inline_env = dict(self.agent.env)
        self.agent.declared_env = dict(self.agent.env)
        self.adapter = (BoundaryClaude if kind == 'claude' else BoundaryCodex)(str(self.native))
        self.adapter.stage = ''
        self.adapter.witness = self.root / 'boundary.json'

    def runner(self, mesh, gitlab=None):
        r = super().runner(mesh, gitlab)
        self.fixture_runners.append(r)
        return r

    def tearDown(self):
        for r in self.fixture_runners:
            for proc in r.procs.values():
                if proc.poll() is None or group_alive(proc.pid):
                    interrupt_group(proc.pid, signal.SIGKILL)
                proc.wait(timeout=5)
            r.store.db.close()
        super().tearDown()

    def records(self, name):
        path = self.root / (name + '.jsonl')
        return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []

    def wait_for(self, check):
        deadline = time.monotonic() + 15
        while not check():
            if time.monotonic() > deadline:
                self.fail('headless boundary deadline')
            time.sleep(.01)

    def prepare(self, r, t):
        r.refresh_me()
        a = r.store.create('builder', t['id'], self.agent.harness)
        r.store.update(a, state='ready', session_id=str(uuid.uuid4()), turns=1,
                       wake_text=self.mandatory, wait=dict(self.pending))
        return a

    def recovered(self, t):
        r = self.runner(FakeMesh([t]))
        for a in r.store.open_on_task(t['id'][:8]):
            if a.checkout.get('token'):
                r.mesh.checkouts[t['id']] = {'session': a.checkout['session_id']}
        r.refresh_me()
        return r

    def assert_pending(self, r, a):
        self.assertIn(self.mandatory, a.wake_text)
        self.assertEqual(a.source_read_at, '')
        for key, delivery in self.pending.items():
            self.assertEqual(a.wait[key], delivery)
            self.assertIsNone(r.store.mark('builder', a.task_id, delivery['key']))
        self.assertIn('interactive_prompt_intent', a.wait)

    def assert_done(self, r, a):
        self.assertEqual((a.state, a.turns), ('ready', 2))
        self.assertTrue(a.source_read_at)
        self.assertNotIn('interactive_prompt_intent', a.wait)
        for key, delivery in self.pending.items():
            self.assertNotIn(key, a.wait)
            self.assertEqual(r.store.mark('builder', a.task_id, delivery['key'])[0], delivery['value'])
        self.assertEqual(r.store.mark('builder', a.task_id, 'queue_floor')[0], '903')
        self.assertEqual(len(self.records('starts')), 1)
        self.assertEqual(len(self.records('prompts')), 1)
        self.assertIn(self.mandatory, self.records('prompts')[0])

    def crash(self, kind, stage):
        self.configure(kind)
        self.adapter.stage = stage
        t = task(903, cat='in_progress')
        pid = os.fork()
        if pid == 0:
            try:
                r = self.runner(FakeMesh([t]))
                a = self.prepare(r, t)
                r._resume_one(a)
                if stage in ('after-launch', 'supervisor-death'):
                    a = r.store.get(a.id)
                    self.adapter.witness.write_text(json.dumps({'pid': a.pid, 'stamp': a.stamp, 'session': a.session_id}))
                    os.kill(os.getpid(), signal.SIGSTOP)
            finally:
                os._exit(0)
        receipt = None
        try:
            self.wait_for(self.adapter.witness.exists)
            receipt = json.loads(self.adapter.witness.read_text())
            if stage not in ('after-launch', 'supervisor-death'):
                self.assertEqual(self.records('starts'), [])
            else:
                self.wait_for(lambda: len(self.records('prompts')) == 1)
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
            pid = 0
            if stage not in ('after-launch', 'supervisor-death'):
                self.wait_for(lambda: not group_alive(receipt['pid'], receipt['stamp']))
            if stage == 'supervisor-death':
                # Kill only this fixture's group leader. Its ordinary native child
                # must keep the same registered process group occupied.
                os.kill(receipt['pid'], signal.SIGKILL)
                self.wait_for(lambda: not proc_stamp(receipt['pid']))
                self.assertTrue(group_alive(receipt['pid'], receipt['stamp']))
            self.adapter.stage = ''
            r = self.recovered(t)
            a, = r.store.by_state('builder', ('starting', 'running'))
            self.assert_pending(r, a)
            exact = a.wait['interactive_prompt_intent']['prompt']
            r.tick()
            a = r.store.get(a.id)
            self.assertEqual(a.state, 'running')
            self.assert_pending(r, a)
            if stage in ('after-launch', 'supervisor-death'):
                self.assertEqual((a.pid, a.stamp, a.session_id),
                                 (receipt['pid'], receipt['stamp'], receipt['session']))
                self.assertEqual(r.procs, {})
                self.wait_for(lambda: not group_alive(a.pid, a.stamp))
            else:
                self.assertNotEqual(a.pid, receipt['pid'])
                proc = r.procs[a.id]
                self.wait_for(lambda: proc.poll() is not None and not group_alive(a.pid, a.stamp))
            r.settle_finished()
            self.assert_done(r, r.store.get(a.id))
            self.assertEqual(self.records('prompts')[0], exact)
            self.assertFalse(group_alive(a.pid, a.stamp))
            print('HEADLESS_RECEIPT:', kind, stage, 'SIGKILL -> registered identity/unsent reconciliation; '
                  'native starts=1, exact mandatory prompt=1, bound terminal ack -> ready, marks committed', flush=True)
        finally:
            if pid:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
            if receipt and group_alive(receipt['pid'], receipt['stamp']):
                interrupt_group(receipt['pid'], signal.SIGKILL)

class HeadlessSpawnGate(HeadlessFixture):
    def test_claude_before_registration(self):
        self.crash('claude', 'before-register')

    def test_claude_after_registration(self):
        self.crash('claude', 'after-register')

    def test_claude_normal_adoption(self):
        self.crash('claude', 'after-launch')

    def test_codex_before_registration(self):
        self.crash('codex', 'before-register')

    def test_codex_after_registration(self):
        self.crash('codex', 'after-register')

    def test_codex_normal_adoption(self):
        self.crash('codex', 'after-launch')

    def test_claude_supervisor_death_preserves_native_group(self):
        self.crash('claude', 'supervisor-death')

    def test_codex_supervisor_death_preserves_native_group(self):
        self.crash('codex', 'supervisor-death')

    def test_empty_or_partial_gate_input_never_starts_native(self):
        import hashlib
        self.configure('claude')
        directory = self.root / 'gate'
        directory.mkdir()
        prompt, token = 'mandatory complete input', 'fixture-token'
        digest = hashlib.sha256(prompt.encode()).hexdigest()
        for partial in ('', prompt[:-1]):
            interactive.write_delivery(directory, 'fixture', {'token': token, 'sha256': digest, 'phase': 'reserved'})
            read_fd, write_fd = os.pipe()
            try:
                result = subprocess.run([sys.executable, str(Path(spawn_gate.__file__)),
                    str(interactive.delivery_path(directory, 'fixture')), token, digest,
                    str(write_fd), '--', str(self.native)], input=partial.encode(),
                    pass_fds=(write_fd,), capture_output=True, timeout=5)
                self.assertEqual(result.returncode, 0)
                self.assertEqual(self.records('starts'), [])
                self.assertEqual(interactive.read_delivery(directory, 'fixture')['phase'], 'reserved')
            finally:
                os.close(read_fd)
                os.close(write_fd)

    def test_actual_native_exec_denial_keeps_readiness_and_mandatory_pending(self):
        self.configure('claude')
        self.adapter.stage = 'exec-denied'
        t = task(907, cat='in_progress')
        r = self.runner(FakeMesh([t]))
        a = self.prepare(r, t)
        self.assertFalse(r._resume_one(a))
        held = r.store.get(a.id)
        self.assertEqual((held.state, held.pid, held.turns), ('ready', 0, 1))
        self.assertNotIn('interactive_prompt_intent', held.wait)
        self.assertIn(self.mandatory, held.wake_text)
        for key, value in self.pending.items():
            self.assertEqual(held.wait[key], value)
            self.assertIsNone(r.store.mark('builder', t['id'], value['key']))
        observation = json.loads(r.store.mark('builder', t['id'], 'launch_readiness_hold')[0])
        self.assertEqual(observation['state'], 'exec-denied')
        self.assertFalse([e for e in r.store.events(a.id) if e[1] == 'launch_readiness_restored'])
        self.assertEqual(self.records('starts'), [])
        self.assertEqual(r.procs, {})
        print('HEADLESS_EXEC_RECEIPT: actual native EACCES after registration -> ready prior turn, '
              'no native start, pending refs/wake retained, readiness not falsely restored', flush=True)


class HeadlessTerminalContract(HeadlessFixture):
    # Reuse the real CLI fixture and cleanup, without inheriting the crash cases.
    def terminal(self, kind, mode, *, live=False, accepted=False):
        self.configure(kind)
        self.agent.inline_env['HEADLESS_TERMINAL'] = mode
        if accepted:
            self.agent.inline_env['HEADLESS_ACCEPT'] = '1'
        self.agent.inline_env['HEADLESS_SLOW'] = '0'
        if live:
            self.agent.inline_env['HEADLESS_BG'] = '1'
        t = task(912 + len(self.fixture_runners), cat='in_progress')
        r = self.runner(FakeMesh([t]))
        a = self.prepare(r, t)
        r._resume_one(a)
        a = r.store.get(a.id)
        proc = r.procs[a.id]
        self.assertEqual(proc.wait(timeout=10), 0)
        if live:
            # The native terminal exists but another process in the same group
            # may still write: group liveness must remain the first boundary.
            # A genuine background child is started by this CLI fixture below.
            self.assertTrue(group_alive(a.pid, a.stamp))
        else:
            self.assertFalse(group_alive(a.pid, a.stamp))
        return r, a

    def test_claude_bound_terminal_error_acknowledges_delivery_not_task_success(self):
        r, a = self.terminal('claude', 'error')
        self.assertTrue(r._reconcile_interactive_prompt(a))
        after = r.store.get(a.id)
        self.assertNotIn('interactive_prompt_intent', after.wait)
        self.assertEqual(after.no_outcome, 0)
        self.assertEqual(len(self.records('prompts')), 1)
        self.assertIn(self.mandatory, self.records('prompts')[0])
        r.settle_finished()
        after = r.store.get(a.id)
        self.assertEqual((after.state, after.no_outcome), ('ready', 1))
        self.assertNotEqual(after.state, 'completed')
        for delivery in self.pending.values():
            self.assertEqual(r.store.mark('builder', after.task_id, delivery['key'])[0], delivery['value'])

    def test_codex_bound_terminal_error_acknowledges_delivery_not_task_success(self):
        r, a = self.terminal('codex', 'error')
        self.assertTrue(r._reconcile_interactive_prompt(a))
        r.settle_finished()
        self.assertEqual((r.store.get(a.id).state, r.store.get(a.id).no_outcome), ('ready', 1))
        self.assertEqual(len(self.records('prompts')), 1)

    def test_wrong_session_error_retains_every_mandatory_delivery(self):
        for kind in ('claude', 'codex'):
            with self.subTest(kind=kind):
                r, a = self.terminal(kind, 'wrong-session')
                self.assertFalse(r._reconcile_interactive_prompt(a))
                self.assertEqual(r.store.get(a.id).state, 'unknown')
                self.assert_pending(r, r.store.get(a.id))

    def test_multiple_terminals_retain_every_mandatory_delivery(self):
        for kind in ('claude', 'codex'):
            with self.subTest(kind=kind):
                r, a = self.terminal(kind, 'multiple-terminal')
                self.assertFalse(r._reconcile_interactive_prompt(a))
                self.assertEqual(r.store.get(a.id).state, 'unknown')
                self.assert_pending(r, r.store.get(a.id))

    def test_missing_native_terminal_retains_every_mandatory_delivery(self):
        for kind in ('claude', 'codex'):
            with self.subTest(kind=kind):
                r, a = self.terminal(kind, 'no-terminal')
                self.assertFalse(r._reconcile_interactive_prompt(a))
                self.assertEqual(r.store.get(a.id).state, 'unknown')
                self.assert_pending(r, r.store.get(a.id))

    def test_live_whole_group_blocks_even_an_exact_error_terminal(self):
        r, a = self.terminal('claude', 'error', live=True)
        self.assertFalse(r._reconcile_interactive_prompt(a))
        self.assertEqual(r.store.get(a.id).state, 'running')
        self.assert_pending(r, r.store.get(a.id))
        self.assertFalse([e for e in read_events(Path(a.log_path)) if e.get('type') == 'mesh_runner.prompt_ack'])

    def test_unbound_journal_blocks_error_terminal(self):
        r, a = self.terminal('claude', 'error')
        directory = interactive.agent_dir(self.cfg.state_dir, self.agent.name)
        journal = interactive.read_delivery(directory, a.id)
        journal['token'] = 'different-owner-generation'
        interactive.write_delivery(directory, a.id, journal)
        self.assertFalse(r._reconcile_interactive_prompt(a))
        self.assertEqual(r.store.get(a.id).state, 'unknown')
        self.assert_pending(r, r.store.get(a.id))

    def test_native_acceptance_without_terminal_allows_delivery_then_operational_retry(self):
        for kind in ('claude', 'codex'):
            with self.subTest(kind=kind):
                r, a = self.terminal(kind, 'no-terminal', accepted=True)
                restarted = self.runner(r.mesh)
                restarted.refresh_me()
                current = restarted.store.get(a.id)
                self.assertTrue(restarted._reconcile_interactive_prompt(current))
                self.assertNotIn('interactive_prompt_intent', current.wait)
                self.assertEqual(len(self.records('prompts')), len(self.fixture_runners) // 2)
                for delivery in self.pending.values():
                    self.assertEqual(restarted.store.mark(a.agent, a.task_id, delivery['key'])[0], delivery['value'])
                restarted.settle_finished()
                current = restarted.store.get(a.id)
                self.assertEqual((current.state, current.no_outcome), ('ready', 1))
                self.assertNotEqual(current.state, 'completed')

    def test_live_group_with_acceptance_and_no_terminal_still_cannot_ack(self):
        r, a = self.terminal('claude', 'no-terminal', live=True, accepted=True)
        self.assertFalse(r._reconcile_interactive_prompt(a))
        self.assertEqual(a.state, 'running')
        self.assert_pending(r, a)

    def test_changed_acceptance_session_cannot_ack_missing_terminal(self):
        r, a = self.terminal('claude', 'no-terminal', accepted=True)
        from mesh_runner import headless_acceptance
        intent = a.wait['interactive_prompt_intent']
        directory = interactive.agent_dir(self.cfg.state_dir, self.agent.name)
        key = headless_acceptance.receipt_key(a.id, intent['token'])
        receipt = interactive.read_delivery(directory, key)
        receipt['session_id'] = 'another-session'
        interactive.write_delivery(directory, key, receipt)
        self.assertFalse(r._reconcile_interactive_prompt(a))
        self.assertEqual(a.state, 'unknown')
        self.assert_pending(r, a)


class HeadlessAcceptedCrash(HeadlessFixture):
    def test_controller_sigkill_after_native_acceptance_does_not_replay_mandatory_prompt(self):
        for index, kind in enumerate(('claude', 'codex')):
            with self.subTest(kind=kind):
                self.configure(kind)
                self.agent.inline_env.update(HEADLESS_ACCEPT='1', HEADLESS_BLOCK_AFTER_ACCEPT='1',
                                             HEADLESS_SLOW='0', HEADLESS_TERMINAL='no-terminal')
                t = task(930 + index, cat='in_progress')
                witness = self.root / ('accepted-controller-' + kind)
                before = len(self.records('starts'))
                controller = os.fork()
                receipt = None
                if controller == 0:
                    try:
                        r = self.runner(FakeMesh([t]))
                        a = self.prepare(r, t)
                        r._resume_one(a)
                        a = r.store.get(a.id)
                        witness.write_text(json.dumps({'attempt': a.id, 'pid': a.pid, 'stamp': a.stamp}))
                        os.kill(os.getpid(), signal.SIGSTOP)
                    finally:
                        os._exit(0)
                try:
                    self.wait_for(witness.exists)
                    receipt = json.loads(witness.read_text())
                    self.wait_for(lambda: len(self.records('acceptances')) == index + 1)
                    os.kill(controller, signal.SIGKILL)
                    os.waitpid(controller, 0)
                    controller = 0
                    restarted = self.runner(FakeMesh([t]))
                    restarted.refresh_me()
                    a = restarted.store.get(receipt['attempt'])
                    restarted.tick()
                    self.assertEqual(restarted.store.get(a.id).state, 'running')
                    self.assertEqual(len(self.records('starts')), before + 1)
                    self.assert_pending(restarted, restarted.store.get(a.id))
                    interrupt_group(receipt['pid'], signal.SIGKILL)
                    self.wait_for(lambda: not group_alive(receipt['pid'], receipt['stamp']))
                    restarted.settle_finished()
                    after = restarted.store.get(a.id)
                    self.assertEqual((after.state, after.no_outcome), ('ready', 1))
                    self.assertNotIn('interactive_prompt_intent', after.wait)
                    self.assertEqual(len(self.records('starts')), before + 1)
                    prompts = self.records('prompts')
                    self.assertEqual(sum(self.mandatory in p for p in prompts), index + 1)
                    for delivery in self.pending.values():
                        self.assertEqual(restarted.store.mark(a.agent, a.task_id, delivery['key'])[0], delivery['value'])
                    # A supported ordinary resume follows only after the old whole
                    # group is dead; the consumed mandatory event is not replayed.
                    self.agent.inline_env.pop('HEADLESS_BLOCK_AFTER_ACCEPT')
                    self.agent.inline_env.pop('HEADLESS_TERMINAL')
                    restarted._resume_one(after)
                    self.assertEqual(restarted.store.get(a.id).state, 'running')
                    proc = restarted.procs[a.id]
                    self.assertEqual(proc.wait(timeout=10), 0)
                    restarted.settle_finished()
                    self.assertEqual(len(self.records('starts')), before + 2)
                    self.assertNotIn(self.mandatory, self.records('prompts')[-1])
                    restarted.store.update(restarted.store.get(a.id), state='completed')
                    print('ACCEPTED_CRASH_RECEIPT: ' + kind + ': controller SIGKILL, live-group fence, '
                          'dead-group native acceptance -> delivery ACK, separate operational retry, no mandatory replay', flush=True)
                finally:
                    if controller:
                        os.kill(controller, signal.SIGKILL)
                        os.waitpid(controller, 0)
                    if receipt and group_alive(receipt['pid'], receipt['stamp']):
                        interrupt_group(receipt['pid'], signal.SIGKILL)


class HeadlessRegistrationAndLease(HeadlessFixture):
    def refused(self, kind, clause):
        self.configure(kind)
        t = task(940)
        r = self.runner(FakeMesh([t]))
        a = self.prepare(r, t)
        r.store.db.execute("CREATE TRIGGER fixture_refuse_registration BEFORE UPDATE OF state ON attempts "
                           "WHEN NEW.state = 'running' BEGIN SELECT RAISE(" + clause + "); END")
        try:
            r._resume_one(a)
        except Exception:
            pass                                  # commit refusal surfaces as an error, CAS loss as a False
        self.time_for_native = time.monotonic() + 1.5
        while time.monotonic() < self.time_for_native:
            time.sleep(.05)
        # the native stayed behind the EOF gate: nothing ran and nothing was pasted
        self.assertEqual((self.records('starts'), self.records('prompts')), ([], []))
        a = r.store.get(a.id)
        self.assertNotEqual(a.state, 'running')
        self.assert_pending(r, a)

    def test_commit_failure_keeps_native_behind_the_gate(self):
        for kind in ('claude', 'codex'):
            with self.subTest(kind=kind):
                self.refused(kind, "ABORT, 'fixture failure'")
                self.setUp()

    def test_failed_compare_and_swap_keeps_native_behind_the_gate(self):
        for kind in ('claude', 'codex'):
            with self.subTest(kind=kind):
                self.refused(kind, 'IGNORE')
                self.setUp()

    def test_accepted_prompt_with_a_dead_controller_keeps_lease_and_watchdog_ends_it_without_a_second_writer(self):
        for index, kind in enumerate(('claude', 'codex')):
            with self.subTest(kind=kind):
                self.configure(kind)
                self.agent.inline_env.update(HEADLESS_ACCEPT='1', HEADLESS_BLOCK_AFTER_ACCEPT='1', HEADLESS_SLOW='0')
                t = task(950 + index, cat='in_progress')
                witness = self.root / ('lease-controller-' + kind)
                before = len(self.records('starts'))
                controller = os.fork()
                receipt = None
                if controller == 0:
                    try:
                        r = self.runner(FakeMesh([t]))
                        a = self.prepare(r, t)
                        r._resume_one(a)
                        a = r.store.get(a.id)
                        witness.write_text(json.dumps({'attempt': a.id, 'pid': a.pid, 'stamp': a.stamp}))
                        os.kill(os.getpid(), signal.SIGSTOP)
                    finally:
                        os._exit(0)
                try:
                    self.wait_for(witness.exists)
                    receipt = json.loads(witness.read_text())
                    self.wait_for(lambda: len(self.records('acceptances')) == index + 1)
                    os.kill(controller, signal.SIGKILL)
                    os.waitpid(controller, 0)
                    controller = 0
                    restarted = self.recovered(t)
                    # phase 1, watchdog off: the live native keeps its lease and is the only writer
                    for _ in range(3):
                        time.sleep(.2)
                        restarted.tick()
                    a = restarted.store.get(receipt['attempt'])
                    self.assertEqual(a.state, 'running')
                    self.assert_pending(restarted, a)
                    self.assertGreater(restarted.mesh.heartbeats, 0)
                    self.assertTrue(group_alive(receipt['pid'], receipt['stamp']))
                    self.assertEqual(len(self.records('starts')), before + 1)
                    # phase 2, watchdog armed: it ends the silent turn, delivery is acknowledged once
                    self.cfg.turn_idle_minutes = 0.0001
                    restarted.tick()
                    self.wait_for(lambda: not group_alive(receipt['pid'], receipt['stamp']))
                    kinds = [k for _, k, _ in restarted.store.events(a.id)]
                    self.assertIn('watchdog', kinds)
                    restarted.settle_finished()
                    after = restarted.store.get(a.id)
                    self.assertNotIn('interactive_prompt_intent', after.wait)
                    self.assertEqual(sum(self.mandatory in p for p in self.records('prompts')), index + 1)
                    for delivery in self.pending.values():
                        self.assertEqual(restarted.store.mark(a.agent, a.task_id, delivery['key'])[0], delivery['value'])
                    self.cfg.turn_idle_minutes = 0
                finally:
                    if controller:
                        os.kill(controller, signal.SIGKILL)
                        os.waitpid(controller, 0)
                    if receipt and group_alive(receipt['pid'], receipt['stamp']):
                        interrupt_group(receipt['pid'], signal.SIGKILL)
