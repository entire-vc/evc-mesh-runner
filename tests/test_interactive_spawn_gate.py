"""Interactive crash fence: real Runner/SQLite/driver/private tmux, fixture harness only.

FakeMesh replaces external Mesh HTTP. The native harness is the existing terminal
fixture. BoundaryInteractiveAdapter stops only the test controller at the process
creation boundary, without mocking registry, authorization, stdin, PID, or settling.
"""
import datetime as dt
import json
import os
import shlex
import signal
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path

from mesh_runner import interactive
from mesh_runner.adapters.base import group_alive, interrupt_group, proc_stamp, read_events
from mesh_runner.adapters.claude import ClaudeAdapter
from mesh_runner.delivery_recovery import explain as explain_delivery
from fakes import FakeMesh, task
from test_interactive import FAKE_HARNESS
from tmux_support import TMUX, check_tmux
from test_runner import Bench


class BoundaryInteractiveAdapter(interactive.InteractiveAdapter):
    def _argv(self, *args):
        argv = super()._argv(*args)
        i = argv.index('--')
        return argv[:i] + ['--poll', '0.05', '--settle', '0.1'] + argv[i:]

    def spawn(self, launch, log_path):
        callback = launch.on_created
        stage = getattr(self, 'crash_stage', '')
        if stage == 'capture':
            def capture(proc):
                self.created = proc
                callback(proc)
            launch.on_created = capture
        elif stage:
            def stopped(proc):
                if stage == 'after-register':
                    callback(proc)
                self.witness.write_text(json.dumps({'pid': proc.pid, 'stamp': proc_stamp(proc.pid)}))
                os.kill(os.getpid(), signal.SIGSTOP)
                if stage == 'before-register':
                    callback(proc)
            launch.on_created = stopped
        return super().spawn(launch, log_path)


class InteractiveSpawnGate(Bench):
    def setUp(self):
        super().setUp()
        check_tmux(self)
        self.socket = 'mr-spawn-gate-%s-%s' % (os.getpid(), id(self))
        self.native = self.root / 'native.py'
        # The fixture's diagnostic prompt echo contains Runner instructions, including
        # busy-screen phrases; real final replies do not echo this whole contract.
        native = FAKE_HARNESS.replace(' + f" [{prompt}]"', '')
        # The production first prompt includes Unicode; the old short-prompt
        # fixture decodes one terminal byte at a time, corrupting those markers.
        native = native.replace('buf, pasting, data =',
                                "import codecs\ndecoder = codecs.getincrementaldecoder('utf-8')('replace')\nbuf, pasting, data =")
        native = native.replace('data.decode(errors="replace")', 'decoder.decode(data)')
        native = native.replace('def say(prompt):', 'def say(prompt):\n    with open(os.path.join(root, "prompts.jsonl"), "a") as fh:\n        fh.write(json.dumps(prompt) + "\\n")')
        self.native.write_text('#!%s\n%s' % (sys.executable, native))
        self.native.chmod(0o700)
        self.tmux = self.root / 'private-tmux'
        self.tmux.write_text('#!/bin/sh\nexec %s -L %s -f /dev/null "$@"\n' %
                             (shlex.quote(TMUX), shlex.quote(self.socket)))
        self.tmux.chmod(0o700)
        self.cfg.tmux_binary = str(self.tmux)
        self.agent.env.update(HOME=str(self.root), CLAUDE_CONFIG_DIR=str(self.root / 'claude-config'), FAKE_ROOT=str(self.root), FAKE_KIND='claude', FAKE_REPLY='↻ NEXT', FAKE_SLOW='0.3')
        self.agent.inline_env = dict(self.agent.env)
        self.agent.declared_env = dict(self.agent.env)
        self.adapter = BoundaryInteractiveAdapter(ClaudeAdapter(str(self.native)), 'builder', self.cfg.state_dir,
                                                  tmux=str(self.tmux), max_sessions=3)
        self.adapter.witness = self.root / 'boundary.json'
        self.fixture_runners = []

    def runner(self, mesh, gitlab=None):
        runner = super().runner(mesh, gitlab)
        self.fixture_runners.append(runner)
        return runner

    def tearDown(self):
        # Only direct Popen children created by this fixture are eligible for cleanup.
        for runner in self.fixture_runners:
            for proc in runner.procs.values():
                if proc.poll() is None:
                    interrupt_group(proc.pid, signal.SIGKILL)
                proc.wait(timeout=5)
            runner.store.db.close()
        if hasattr(self, 'socket'):
            subprocess.run([TMUX, '-L', self.socket, 'kill-server'], capture_output=True)
        super().tearDown()

    def wait_for(self, check):
        deadline = time.monotonic() + 20
        while not check():
            if time.monotonic() >= deadline:
                self.fail('boundary did not resolve within test deadline')
            time.sleep(0.01)

    def starts(self):
        p = self.root / 'starts'
        return len(p.read_text().splitlines()) if p.exists() else 0

    mandatory = 'MANDATORY_EVENT_903: continue after exact failed pipeline observation'
    pending = {
        'ci_delivery': {'key': 'fixture-ci-delivery', 'value': 'sha:903:failed'},
        'ci_action_delivery': {'key': 'fixture-ci-action', 'value': 'retry:903'},
        'queue_delivery': {'key': 'fixture-queue', 'value': 'queue:903', 'floor': '903'},
    }

    def prepare_pending(self, r, t):
        r.refresh_me()
        a = r.store.create('builder', t['id'], 'claude')
        sid = str(uuid.uuid4())
        transcript = self.root / 'claude-config' / 'projects' / 'fixture' / (sid + '.jsonl')
        transcript.parent.mkdir(parents=True, exist_ok=True)
        transcript.write_text('{}\n')        # real adapter session lookup, isolated account
        r.store.update(a, state='ready', session_id=sid, turns=1,
                       wake_text=self.mandatory, wait=dict(self.pending))
        return a

    def recovered_runner(self, t):
        r = self.runner(FakeMesh([t]))
        # External Mesh survives a controller crash. Restore its fixture checkout
        # independently of Runner's in-process Popen table (which stays empty).
        for a in r.store.open_on_task(t['id'][:8]):
            if a.checkout.get('token'):
                r.mesh.checkouts[t['id']] = {'session': a.checkout['session_id']}
        return r

    def assert_pending(self, r, a):
        self.assertIn(self.mandatory, a.wake_text)
        for field, delivery in self.pending.items():
            self.assertEqual(a.wait[field], delivery)
            self.assertIsNone(r.store.mark('builder', a.task_id, delivery['key']))
        self.assertIsNone(r.store.mark('builder', a.task_id, 'queue_floor'))
        self.assertIn('interactive_prompt_intent', a.wait)
        self.assertIn(self.mandatory, a.wait['interactive_prompt_intent']['prompt'])
        self.assertEqual(a.source_read_at, '')

    def prompts(self):
        p = self.root / 'prompts.jsonl'
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    def crash(self, stage):
        t = task(903, cat='in_progress')
        self.adapter.crash_stage = stage
        self.agent.inline_env['FAKE_SLOW'] = '3'
        pid = os.fork()
        if pid == 0:
            try:
                original = self.runner(FakeMesh([t]))
                a = self.prepare_pending(original, t)
                original._resume_one(a)
            finally:
                os._exit(0)
        try:
            self.wait_for(self.adapter.witness.exists)
            receipt = json.loads(self.adapter.witness.read_text())
            self.assertEqual(self.starts(), 0)
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
            pid = 0
            self.wait_for(lambda: not group_alive(receipt['pid'], receipt['stamp']))
            self.adapter.crash_stage = ''
            recovered = self.recovered_runner(t)
            recovered.refresh_me()
            a, = recovered.store.by_state('builder', ('starting', 'running'))
            self.assert_pending(recovered, a)
            exact_prompt = a.wait['interactive_prompt_intent']['prompt']
            recovered.tick()           # proven unsent -> normal admission, no blind replay
            a = recovered.store.get(a.id)
            self.assertEqual(a.state, 'running')
            self.assertNotEqual(a.pid, receipt['pid'])
            self.assert_pending(recovered, a)
            self.assertFalse(group_alive(receipt['pid'], receipt['stamp']))
            self.wait_for(lambda: len(self.prompts()) == 1)
            self.assertIn(self.mandatory, self.prompts()[0])
            recovered.tick()
            self.assert_pending(recovered, recovered.store.get(a.id))
            self.assertEqual(self.starts(), 1)
            self.assertEqual(recovered.store.active_count('builder'), 1)
            self.assertTrue(recovered.pool.leased_to(a.id))
            # This retry is our own Popen child, unlike the adopted orphan above.
            # Reap it exactly as Runner._turn_over does before checking the group.
            proc = recovered.procs[a.id]
            self.wait_for(lambda: proc.poll() is not None and not group_alive(a.pid, a.stamp))
            recovered.settle_finished()
            done = recovered.store.get(a.id)
            self.assertEqual((done.state, done.turns), ('ready', 2))
            self.assertEqual(len(self.prompts()), 1)
            self.assertEqual(self.prompts()[0].replace('\r', '\n'), exact_prompt)
            for field, delivery in self.pending.items():
                self.assertNotIn(field, done.wait)
                self.assertEqual(recovered.store.mark('builder', t['id'], delivery['key'])[0], delivery['value'])
            print('GATE_RECEIPT:', stage, 'SIGKILL -> native prompts=0; durable unsent phase -> normal admission; '
                  'one exact mandatory prompt delivered, acked, marks committed; old group gone', flush=True)
        finally:
            if pid:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)

    def test_sigkill_before_registration_preserves_mandatory_delivery(self):
        self.crash('before-register')

    def test_sigkill_after_registration_before_stdin_preserves_mandatory_delivery(self):
        self.crash('after-register')

    def registration_refused(self, clause, exception):
        t = task(905)
        self.adapter.crash_stage = 'capture'
        r = self.runner(FakeMesh([t]))
        r.refresh_me()
        prior = json.dumps({'state': 'exec-denied', 'fingerprint': 'fixture-old-executable-facts'})
        r.store.mark('builder', t['id'], 'launch_readiness_hold', prior)
        r.store.db.execute("CREATE TRIGGER fixture_refuse_registration BEFORE UPDATE OF state ON attempts "
                           "WHEN NEW.state = 'running' BEGIN SELECT RAISE(" + clause + "); END")
        with self.assertRaises(exception):
            r._start_one(t)
        proc = self.adapter.created
        proc.wait(timeout=10)
        self.assertEqual(self.starts(), 0)
        self.assertFalse(r.procs)
        self.assertEqual(r.store.mark('builder', t['id'], 'launch_readiness_hold')[0], prior)
        a, = r.store.by_state('builder', ('starting',))
        self.assertEqual((a.pid, a.turns, a.source_read_at), (0, 0, ''))
        self.assertFalse([e for e in r.store.events(a.id) if e[1] == 'launch_readiness_restored'])
        print('REGISTRATION_RECEIPT:', clause.split(',')[0], '-> native starts=0, no identity/turn committed', flush=True)

    def test_sqlite_commit_failure_keeps_native_behind_eof_gate(self):
        self.registration_refused("ABORT, 'fixture failure'", sqlite3.IntegrityError)

    def test_failed_compare_and_swap_keeps_native_behind_eof_gate(self):
        self.registration_refused('IGNORE', RuntimeError)

    def test_commit_failure_after_the_journal_leaves_no_intent_and_no_unknown_state(self):
        t = task(910)
        r = self.runner(FakeMesh([t]))
        r.refresh_me()
        r.store.db.execute("CREATE TRIGGER fixture_refuse_intent BEFORE UPDATE OF wait ON attempts "
                           "WHEN NEW.wait LIKE '%interactive_prompt_intent%' BEGIN SELECT RAISE(ABORT, 'fixture'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            r._start_one(t)
        directory = interactive.agent_dir(self.cfg.state_dir, 'builder')
        a, = r.store.open_on_task(t['id'][:8])
        # the journal was written first: an orphan reservation, and no DB intent naming it
        self.assertEqual(interactive.read_delivery(directory, a.id).get('phase'), 'reserved')
        self.assertNotIn('interactive_prompt_intent', a.wait or {})
        self.assertEqual(self.starts(), 0)
        # a restart over this state must not read it as an unproven native prompt
        r = self.recovered_runner(t)
        r.refresh_me()
        r.tick()
        self.assertNotEqual(r.store.get(a.id).state, 'unknown')
        self.assertNotIn('prompt_delivery_unknown', [e[1] for e in r.store.events(a.id)])
        self.assertEqual((self.starts(), len(self.prompts())), (0, 0))

    def test_unresolved_pending_delivery_keeps_its_lease_heartbeat_and_is_not_cut_by_the_watchdog(self):
        t = task(907, cat='in_progress')
        self.agent.inline_env['FAKE_SLOW'] = '3'
        pid = os.fork()
        if pid == 0:
            try:
                original = self.runner(FakeMesh([t]))
                a = self.prepare_pending(original, t)
                original._resume_one(a)
                a = original.store.get(a.id)
                self.adapter.witness.write_text(json.dumps({'attempt': a.id, 'pid': a.pid, 'stamp': a.stamp}))
                os.kill(os.getpid(), signal.SIGSTOP)
            finally:
                os._exit(0)
        receipt = None
        try:
            self.wait_for(self.adapter.witness.exists)
            receipt = json.loads(self.adapter.witness.read_text())
            events = interactive.agent_dir(self.cfg.state_dir, 'builder') / (receipt['attempt'] + '.events.jsonl')
            self.wait_for(lambda: any(e.get('hook_event_name') == 'UserPromptSubmit' for e in read_events(events)))
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
            pid = 0
            interrupt_group(receipt['pid'], signal.SIGKILL)
            self.wait_for(lambda: not group_alive(receipt['pid'], receipt['stamp']))
            # every limit armed and already exceeded: the watchdog would end an ordinary quiet turn now
            self.cfg.turn_idle_minutes = 0.0001
            self.cfg.turn_first_output_minutes = 0.0001
            self.cfg.turn_external_wait_minutes = 0.0001
            recovered = self.recovered_runner(t)
            time.sleep(0.5)
            recovered.tick()                     # adopts the orphan and moves it to UNKNOWN
            a = recovered.store.get(receipt['attempt'])
            self.assertEqual(a.state, 'unknown')
            before = recovered.mesh.heartbeats   # from here on only the pending intent can justify a beat
            for _ in range(3):
                recovered.tick()
            a = recovered.store.get(receipt['attempt'])
            self.assertEqual(a.state, 'unknown')
            self.assert_pending(recovered, a)
            self.assertGreater(recovered.mesh.heartbeats, before)            # lease kept alive while unresolved
            self.assertTrue(recovered.pool.leased_to(a.id))
            kinds = [kind for _, kind, _ in recovered.store.events(a.id)]
            self.assertNotIn('watchdog', kinds)
            self.assertNotIn('kill', kinds)
            self.assertEqual((self.starts(), len(self.prompts())), (1, 1))   # no second writer
        finally:
            if pid:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
            if receipt and group_alive(receipt['pid'], receipt['stamp']):
                interrupt_group(receipt['pid'], signal.SIGKILL)

    def test_native_accepted_but_driver_died_before_ack_retains_pending_without_replay(self):
        t = task(906, cat='in_progress')
        self.agent.inline_env['FAKE_SLOW'] = '3'
        pid = os.fork()
        if pid == 0:
            try:
                original = self.runner(FakeMesh([t]))
                a = self.prepare_pending(original, t)
                original._resume_one(a)
                a = original.store.get(a.id)
                self.adapter.witness.write_text(json.dumps({'attempt': a.id, 'pid': a.pid, 'stamp': a.stamp}))
                os.kill(os.getpid(), signal.SIGSTOP)
            finally:
                os._exit(0)
        receipt = None
        try:
            self.wait_for(self.adapter.witness.exists)
            receipt = json.loads(self.adapter.witness.read_text())
            self.wait_for(lambda: len(self.prompts()) == 1)
            # Recording fixture input precedes the native hook; this crash must
            # occur after actual acceptance evidence, not merely terminal paste.
            self.wait_for(lambda: any(e.get('hook_event_name') == 'UserPromptSubmit'
                                     for e in read_events(interactive.agent_dir(
                                         self.cfg.state_dir, 'builder') / (receipt['attempt'] + '.events.jsonl'))))
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
            pid = 0
            interrupt_group(receipt['pid'], signal.SIGKILL)  # our fixture driver, after native input
            self.wait_for(lambda: not group_alive(receipt['pid'], receipt['stamp']))
            recovered = self.recovered_runner(t)
            recovered.tick()
            a = recovered.store.get(receipt['attempt'])
            self.assertEqual(a.state, 'unknown')
            observed, = recovered.status()['attempts']
            # status carries the operator explanation of the unresolved delivery
            self.assertEqual(observed['prompt_delivery'],
                             {'pending': True, 'turn': 2, 'resolution_required': True, 'reason': 'native-running-or-incomplete',
                              'explanation': explain_delivery('native-running-or-incomplete')})
            self.assertEqual(observed['prompt_delivery']['explanation']['replay'], 'forbidden')
            self.assertTrue(any(json.loads(row)['kind'] == 'prompt_delivery_unknown'
                                for row in (self.cfg.state_dir / 'events.jsonl').read_text().splitlines()))
            self.assert_pending(recovered, a)
            recovered.tick()
            self.assertEqual(recovered.store.get(a.id).state, 'unknown')
            self.assertEqual(self.starts(), 1)
            self.assertEqual(len(self.prompts()), 1)
            self.assertIn(self.mandatory, self.prompts()[0])
            self.assertTrue(recovered.pool.leased_to(a.id))
            self.wait_for(lambda: any(e.get('hook_event_name') == 'Stop'
                                     for e in read_events(
                                         interactive.agent_dir(self.cfg.state_dir, 'builder') / (a.id + '.events.jsonl'))))
            directory = interactive.agent_dir(self.cfg.state_dir, 'builder')
            journal = interactive.read_delivery(directory, a.id)
            # A terminal event alone cannot ack a different session, old cursor, or prompt.
            for field, wrong in (('session_id', 'wrong-native-session'), ('native_prefix', 'wrong-prefix')):
                interactive.write_delivery(directory, a.id, {**journal, field: wrong})
                recovered.settle_finished()
                retained = recovered.store.get(a.id)
                self.assertEqual(retained.state, 'unknown')
                self.assert_pending(recovered, retained)
                self.assertFalse(any(e.get('type') == 'mesh_runner.prompt_ack'
                                     for e in read_events(Path(retained.log_path))))
                self.assertEqual(len(self.prompts()), 1)
            interactive.write_delivery(directory, a.id, journal)
            # Older/no-id emitters never prove a bound native turn. Missing IDs
            # must retain the mandatory outbox, even with a matching Stop.
            native_path = directory / (a.id + '.events.jsonl')
            original_events = native_path.read_bytes()
            no_ids = read_events(native_path)
            for event in no_ids[journal['native_base']:]:
                event.pop('prompt_id', None)
            native_path.write_text(''.join(json.dumps(e) + '\n' for e in no_ids))
            recovered.settle_finished()
            retained = recovered.store.get(a.id)
            self.assertEqual(retained.state, 'unknown')
            self.assert_pending(recovered, retained)
            self.assertFalse(any(e.get('type') == 'mesh_runner.prompt_ack'
                                 for e in read_events(Path(retained.log_path))))
            native_path.write_bytes(original_events)
            recovered.settle_finished()
            done = recovered.store.get(a.id)
            self.assertEqual((done.state, done.turns), ('ready', 2))
            self.assertNotIn('interactive_prompt_intent', done.wait)
            for field, delivery in self.pending.items():
                self.assertNotIn(field, done.wait)
                self.assertEqual(recovered.store.mark('builder', t['id'], delivery['key'])[0], delivery['value'])
            self.assertEqual(len(self.prompts()), 1)
            self.assertEqual(self.starts(), 1)
            self.assertTrue(any(e[1] == 'prompt_native_result_recovered' for e in recovered.store.events(a.id)))
            print('NATIVE_RECOVERY_RECEIPT: native prompt=1, driver SIGKILL before ack; replacement ticks retain '
                  'UNKNOWN pending while native runs; session/prompt-id/Stop + quiescence reconstruct result -> ready; '
                  'marks committed, prompts=1, no driver/native spawn', flush=True)
        finally:
            if pid:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
            if receipt and group_alive(receipt['pid'], receipt['stamp']):
                interrupt_group(receipt['pid'], signal.SIGKILL)

    def test_normal_registered_interactive_worker_adopts_same_identity_and_settles(self):
        t = task(904, cat='in_progress')
        # Keep the real fixture turn alive while the replacement Runner ticks.
        self.agent.inline_env['FAKE_SLOW'] = '3'
        pid = os.fork()
        if pid == 0:
            try:
                original = self.runner(FakeMesh([t]))
                a = self.prepare_pending(original, t)
                original._resume_one(a)
                a = original.store.get(a.id)
                self.adapter.witness.write_text(json.dumps({'attempt': a.id, 'pid': a.pid,
                                                            'stamp': a.stamp, 'session': a.session_id}))
                os.kill(os.getpid(), signal.SIGSTOP)
            finally:
                os._exit(0)
        receipt = None
        try:
            self.wait_for(self.adapter.witness.exists)
            receipt = json.loads(self.adapter.witness.read_text())
            self.wait_for(lambda: self.starts() == 1)
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
            pid = 0
            recovered = self.recovered_runner(t)
            recovered.refresh_me()
            self.assertEqual(recovered.procs, {})
            recovered.tick()            # scheduling must not launch a replacement writer
            live = recovered.store.get(receipt['attempt'])
            self.assertEqual((live.state, live.pid, live.stamp, live.session_id, live.turns),
                             ('running', receipt['pid'], receipt['stamp'], receipt['session'], 2))
            self.assert_pending(recovered, live)
            self.assertEqual(self.starts(), 1)
            self.assertTrue(group_alive(live.pid, live.stamp))
            self.wait_for(lambda: not group_alive(live.pid, live.stamp))
            recovered.settle_finished()
            done = recovered.store.get(live.id)
            self.assertEqual((done.state, done.session_id, done.turns), ('ready', receipt['session'], 2))
            self.assertEqual(self.starts(), 1)
            self.assertEqual(len(self.prompts()), 1)
            self.assertIn(self.mandatory, self.prompts()[0])
            self.assertTrue(done.source_read_at)
            self.assertNotIn('interactive_prompt_intent', done.wait)
            for field, delivery in self.pending.items():
                self.assertNotIn(field, done.wait)
                self.assertEqual(recovered.store.mark('builder', t['id'], delivery['key'])[0], delivery['value'])
            self.assertEqual(recovered.store.mark('builder', t['id'], 'queue_floor')[0], '903')
            print('INTERACTIVE_ADOPTION_RECEIPT: controller SIGKILL after launch; replacement tick adopts same '
                  'PID/birth/session -> ready; native starts=1, prompts=1, turn=2; mandatory wake acked, marks committed', flush=True)
        finally:
            if pid:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
            if receipt and group_alive(receipt['pid'], receipt['stamp']):
                interrupt_group(receipt['pid'], signal.SIGKILL)
