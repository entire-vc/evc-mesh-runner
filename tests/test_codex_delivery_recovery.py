"""Real terminal driver and external Codex CLI fixture; no internal mocks."""
import hashlib
import json
import os
import signal
import time
import uuid
from pathlib import Path

import test_interactive_spawn_gate as shared
from test_runner import Bench
from fakes import FakeMesh, task
from mesh_runner import interactive
from mesh_runner.adapters.base import group_alive, interrupt_group, read_events
from mesh_runner.adapters.codex import CodexAdapter


class CodexDeliveryRecovery(shared.InteractiveSpawnGate):
    # Reuse fixture lifecycle only; inherited crash cases remain in their own suite.
    for _name in shared.InteractiveSpawnGate.__dict__:
        if _name.startswith('test_'):
            locals()[_name] = None
    runner = shared.InteractiveSpawnGate.runner
    tearDown = shared.InteractiveSpawnGate.tearDown
    wait_for = shared.InteractiveSpawnGate.wait_for
    starts = shared.InteractiveSpawnGate.starts
    prompts = shared.InteractiveSpawnGate.prompts
    recovered_runner = shared.InteractiveSpawnGate.recovered_runner
    assert_pending = shared.InteractiveSpawnGate.assert_pending
    pending = shared.InteractiveSpawnGate.pending
    mandatory = shared.InteractiveSpawnGate.mandatory

    def setUp(self):
        shared.InteractiveSpawnGate.setUp(self)
        self.agent.harness = 'codex'
        self.agent.env.update(FAKE_KIND='codex', CODEX_HOME=str(self.root / 'codex-home'), FAKE_ROLLOUT='full')
        self.agent.inline_env = dict(self.agent.env)
        self.agent.declared_env = dict(self.agent.env)
        self.adapter = shared.BoundaryInteractiveAdapter(CodexAdapter(str(self.native)), 'builder',
            self.cfg.state_dir, tmux=str(self.tmux), max_sessions=3)
        self.adapter.witness = self.root / 'boundary.json'

    def prepare(self, r, t):
        r.refresh_me()
        a = r.store.create('builder', t['id'], 'codex')
        r.store.update(a, state='ready', session_id=str(uuid.uuid4()), turns=1,
                       wake_text=self.mandatory, wait=dict(self.pending))
        return a

    def test_driver_death_recovers_only_bound_terminal_notify_and_rollout(self):
        t = task(908, cat='in_progress')
        pid = os.fork()
        if pid == 0:
            try:
                r = self.runner(FakeMesh([t]))
                a = self.prepare(r, t)
                r._resume_one(a)
                a = r.store.get(a.id)
                self.adapter.witness.write_text(json.dumps({'attempt': a.id, 'pid': a.pid, 'stamp': a.stamp}))
                os.kill(os.getpid(), signal.SIGSTOP)
            finally:
                os._exit(0)
        receipt = None
        try:
            self.wait_for(self.adapter.witness.exists)
            receipt = json.loads(self.adapter.witness.read_text())
            directory = interactive.agent_dir(self.cfg.state_dir, 'builder')
            key = receipt['attempt']
            self.wait_for(lambda: interactive.read_delivery(directory, key).get('phase') == 'accepted')
            journal = interactive.read_delivery(directory, key)
            self.assertIsInstance(journal['rollout_cursor'], int)
            self.assertTrue(journal['rollout_prefix'])
            self.assertEqual(len(self.prompts()), 1)
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
            pid = 0
            interrupt_group(receipt['pid'], signal.SIGKILL)
            self.wait_for(lambda: not group_alive(receipt['pid'], receipt['stamp']))
            r = self.recovered_runner(t)
            r.tick()
            a = r.store.get(key)
            self.assertEqual(a.state, 'unknown')
            self.assert_pending(r, a)
            self.assertEqual(self.starts(), 1)
            events_path = directory / (key + '.events.jsonl')
            self.wait_for(lambda: any(e.get('type') == 'agent-turn-complete' for e in read_events(events_path)))
            original = events_path.read_bytes()
            events = read_events(events_path)
            for missing in ('turn-id', 'input-messages', 'thread-id'):
                altered = [dict(e) for e in events]
                for e in altered[journal['native_base']:]:
                    e.pop(missing, None)
                events_path.write_text(''.join(json.dumps(e) + '\n' for e in altered))
                r.settle_finished()
                self.assertEqual(r.store.get(key).state, 'unknown')
                self.assert_pending(r, r.store.get(key))
                self.assertFalse(any(e.get('type') == 'mesh_runner.prompt_ack'
                                     for e in read_events(Path(a.log_path))))
            events_path.write_bytes(original)
            interactive.write_delivery(directory, key, {**journal, 'rollout_prefix': 'wrong'})
            r.settle_finished()
            self.assertEqual(r.store.get(key).state, 'unknown')
            self.assert_pending(r, r.store.get(key))
            interactive.write_delivery(directory, key, journal)
            r.settle_finished()
            done = r.store.get(key)
            self.assertEqual((done.state, done.turns), ('ready', 2))
            self.assertNotIn('interactive_prompt_intent', done.wait)
            for field, value in self.pending.items():
                self.assertNotIn(field, done.wait)
                self.assertEqual(r.store.mark('builder', t['id'], value['key'])[0], value['value'])
            self.assertEqual((self.starts(), len(self.prompts())), (1, 1))
            self.assertEqual(self.prompts()[0].replace('\r', '\n'), a.wait['interactive_prompt_intent']['prompt'])
            self.assertEqual(r.procs, {})
            print('CODEX_RECOVERY_RECEIPT: driver SIGKILL after native input; restart retains pending until '
                  'exact thread/turn/full prompt notify + bound rollout terminal; no-id/wrong cursor retain UNKNOWN; '
                  'one prompt, one native, no replacement spawn, marks committed', flush=True)
        finally:
            if pid:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
            if receipt and group_alive(receipt['pid'], receipt['stamp']):
                interrupt_group(receipt['pid'], signal.SIGKILL)

    def test_fresh_codex_session_is_bound_in_the_journal_before_the_native_result_is_accepted(self):
        t = task(909)
        pid = os.fork()
        if pid == 0:
            try:
                r = self.runner(FakeMesh([t]))
                r.refresh_me()
                assert r._start_one(t)
                rows = []
                for _ in range(200):
                    rows = r.store.by_state('builder', ('running', 'starting', 'ready', 'waiting', 'unknown'))
                    if rows:
                        break
                    time.sleep(0.05)
                a = rows[0]
                self.adapter.witness.write_text(json.dumps({'attempt': a.id, 'pid': a.pid, 'stamp': a.stamp}))
                os.kill(os.getpid(), signal.SIGSTOP)
            finally:
                os._exit(0)
        receipt = None
        try:
            self.wait_for(self.adapter.witness.exists)
            receipt = json.loads(self.adapter.witness.read_text())
            directory = interactive.agent_dir(self.cfg.state_dir, 'builder')
            key = receipt['attempt']
            self.wait_for(lambda: interactive.read_delivery(directory, key).get('phase') == 'accepted')
            self.wait_for(lambda: interactive.read_delivery(directory, key).get('rollout'))
            journal = interactive.read_delivery(directory, key)
            # a fresh session had no thread id at registration: acceptance must bind it now
            self.assertRegex(journal['session_id'], r'^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$')
            self.assertTrue(journal['rollout'].endswith(journal['session_id'] + '.jsonl'))
            raw = Path(journal['rollout']).read_bytes()
            self.assertEqual(journal['rollout_prefix'],
                             hashlib.sha256(raw[:journal['rollout_cursor']]).hexdigest())
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
            pid = 0
            interrupt_group(receipt['pid'], signal.SIGKILL)
            self.wait_for(lambda: not group_alive(receipt['pid'], receipt['stamp']))
            r = self.recovered_runner(t)
            r.tick()
            events_path = directory / (key + '.events.jsonl')
            self.wait_for(lambda: any(e.get('type') == 'agent-turn-complete' for e in read_events(events_path)))
            r.settle_finished()
            done = r.store.get(key)
            self.assertNotIn('interactive_prompt_intent', done.wait)
            self.assertNotEqual(done.state, 'unknown')
            self.assertEqual((self.starts(), len(self.prompts())), (1, 1))
        finally:
            if pid:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
            if receipt and group_alive(receipt['pid'], receipt['stamp']):
                interrupt_group(receipt['pid'], signal.SIGKILL)
