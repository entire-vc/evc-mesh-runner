"""Pinned-source crash characterization; OS processes and SQLite remain real.

FakeMesh replaces only the external HTTP service; no production DB or method is mocked.
CrashAdapter is the test harness boundary: it stops the test controller immediately
after production Adapter.spawn returns. The durable creation callback must already
have persisted running/PID; a restarted controller must retain that writer.
"""
import datetime as dt
import json
import os
import signal
import time
from pathlib import Path

from mesh_runner.adapters.base import group_alive, interrupt_group, proc_stamp
from mesh_runner.store import Store
from fakes import FakeAdapter, FakeMesh, FUTURE_AT, task, write_replies
from test_runner import Bench


class CrashAdapter(FakeAdapter):
    def __init__(self, script, witness):
        super().__init__(script)
        self.witness = witness

    def spawn(self, launch, log_path):
        proc = super().spawn(launch, log_path)
        self.witness.write_text(json.dumps({'pid': proc.pid, 'stamp': proc_stamp(proc.pid)}))
        os.kill(os.getpid(), signal.SIGSTOP)
        return proc


class SpawnCrashCharacterization(Bench):
    def wait_for(self, condition):
        deadline = time.monotonic() + 10
        while not condition():
            if time.monotonic() >= deadline:
                self.fail('test process did not reach expected boundary')
            time.sleep(0.01)

    def test_real_controller_sigkill_preserves_registered_writer_without_second_spawn(self):
        t = task(901)
        gate = self.root / 'harness'
        witness = self.root / 'boundary.json'
        write_replies(self.replies, {t['id'][:8]: f'GATE:{gate}:↻ NEXT'})
        self.adapter = CrashAdapter(self.root / 'agent.py', witness)
        controller = os.fork()
        if controller == 0:
            try:
                self.runner(FakeMesh([t])).tick()
            finally:
                os._exit(0)
        receipt = None
        try:
            self.wait_for(witness.exists)
            receipt = json.loads(witness.read_text())
            self.wait_for(gate.with_suffix('.started').exists)
            os.kill(controller, signal.SIGKILL)
            os.waitpid(controller, 0)
            controller = 0
            recovered = self.runner(FakeMesh([t]))
            recovered.refresh_me()
            rows = recovered.store.by_state('builder', ('running',))
            self.assertEqual(len(rows), 1)
            a = rows[0]
            self.assertEqual(a.pid, receipt['pid'])
            self.assertEqual(a.stamp, receipt['stamp'])
            self.assertTrue(a.log_path)
            self.assertTrue(a.session_id)
            self.assertEqual(a.turns, 1)
            self.assertIn('interactive_prompt_intent', a.wait)
            self.assertTrue(group_alive(receipt['pid'], receipt['stamp']))
            # Advance only scratch DB reservation time, not OS or registry semantics.
            recovered.store.update(a, created_at=(dt.datetime.now(dt.timezone.utc)-dt.timedelta(minutes=6)).isoformat())
            recovered.settle_finished()
            after = recovered.store.get(a.id)
            self.assertEqual(after.state, 'running')
            recovered.tick()
            self.assertEqual(recovered.store.get(a.id).pid, receipt['pid'])
            self.assertEqual(len(recovered.store.by_state('builder', ('starting', 'running', 'unknown'))), 1)
            self.assertTrue(group_alive(receipt['pid'], receipt['stamp']))
            self.assertEqual(len(recovered.pool.leased_to(a.id)), 1)
            gate.with_suffix('.released').touch()
            self.wait_for(lambda: not group_alive(receipt['pid'], receipt['stamp']))
            recovered.settle_finished()
            done = recovered.store.get(a.id)
            self.assertEqual((done.state, done.session_id, done.turns), ('ready', a.session_id, 1))
            self.assertNotIn('interactive_prompt_intent', done.wait)
            print('CRASH_RECEIPT: registered writer survives controller SIGKILL; lease retained; restart spawns no second writer; native completion settles', flush=True)
        finally:
            if controller:
                os.kill(controller, signal.SIGKILL)
                os.waitpid(controller, 0)
            if receipt and group_alive(receipt['pid'], receipt['stamp']):
                interrupt_group(receipt['pid'], signal.SIGKILL)

    def test_normal_registered_worker_is_adopted_and_settles_without_second_spawn(self):
        t = task(902)
        gate = self.root / 'harness'
        write_replies(self.replies, {t['id'][:8]: f'GATE:{gate}:↻ NEXT'})
        original = self.runner(FakeMesh([t]))
        original.tick()
        a = original.store.by_state('builder', ('running',))[0]
        proc = original.procs[a.id]
        try:
            self.wait_for(gate.with_suffix('.started').exists)
            recovered = self.runner(FakeMesh([t]))
            recovered.refresh_me()
            self.assertEqual(recovered.procs, {})
            recovered.settle_finished()
            adopted = recovered.store.get(a.id)
            self.assertEqual((adopted.state, adopted.pid, adopted.stamp, adopted.session_id, adopted.turns),
                             ('running', a.pid, a.stamp, a.session_id, 1))
            self.assertEqual(len(recovered.pool.leased_to(a.id)), 1)
            gate.with_suffix('.released').touch()
            self.assertEqual(proc.wait(timeout=10), 0)
            recovered.settle_finished()
            done = recovered.store.get(a.id)
            self.assertEqual(done.state, 'ready')
            self.assertEqual(done.session_id, a.session_id)
            self.assertEqual(done.turns, 1)
            print('ADOPTION_RECEIPT: same attempt/PID/birth/session/turn adopted -> ready, no second spawn', flush=True)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)
