"""Lease maintenance runs independently of ready admission and its early holds."""
import datetime as dt
import json
from pathlib import Path
from unittest.mock import patch

from mesh_runner.mesh import MeshError
from mesh_runner.store import iso, now
import test_held_ready_workspace as held_fixture
from test_runner import Bench
from fakes import write_replies


class ReadyMaintenance(Bench):
    parked = held_fixture.HeldReady.parked
    ready = held_fixture.HeldReady.ready
    age = held_fixture.HeldReady.age

    def test_all_admission_holds_return_aged_leases_without_starting(self):
        for hold in ('paused', 'quota', 'memory', 'room', 'auth', 'checkout'):
            with self.subTest(hold=hold):
                r, a = self.ready()
                old_wait = dict(a.wait)
                self.age(r, a)
                self.agent.paused = hold == 'paused'
                if hold == 'quota':
                    r._starts_paused_until = float('inf')
                with (patch.object(r.memgate, 'check', return_value=hold == 'memory'),
                      patch.object(r, '_auth_blocked', return_value='held' if hold == 'auth' else ''),
                      patch.object(r, '_acquire', return_value=False if hold == 'checkout' else True),
                      patch.object(r, '_spawn') as spawn):
                    r.check_wakes()
                    r.resume_ready(0 if hold == 'room' else 1)
                self.assertEqual(r.pool.leased_to(a.id), [])
                self.assertEqual((r.store.get(a.id).state, r.store.get(a.id).wait), ('ready', old_wait))
                spawn.assert_not_called()
                self.assertFalse(r.procs)
                self.tearDown(); self.setUp()

    def test_clock_and_remote_reads_are_bounded_even_when_admission_never_runs(self):
        r, a = self.ready()
        self.cfg.wake_poll_seconds = 30
        with patch('mesh_runner.runner.time.monotonic', return_value=1000), patch.object(r, '_task') as task:
            r.check_wakes()
            saved = r.store.mark(a.agent, a.task_id, 'workspace_ready_hold')
            self.assertIsNotNone(saved)
            for _ in range(10):
                r.check_wakes()
            task.assert_not_called()  # no ownership reads before the release threshold
        self.age(r, a)
        with patch('mesh_runner.runner.time.monotonic', return_value=1010), patch.object(r, '_task') as task:
            r.check_wakes()
            task.assert_not_called()
        with patch('mesh_runner.runner.time.monotonic', return_value=1031), patch.object(r, '_task', wraps=r._task) as task:
            r.check_wakes()
            for _ in range(10):
                r.check_wakes()
            self.assertEqual(task.call_count, 1)
        self.assertEqual(r.pool.leased_to(a.id), [])

    def test_unknown_owner_retries_only_at_poll_boundary_and_restart_restores(self):
        r, a = self.ready()
        self.age(r, a)
        self.cfg.wake_poll_seconds = 30
        source = Path(a.workspaces[0])
        (source / 'README').write_text('valuable ready progress')
        old_wait = dict(a.wait)
        with (patch('mesh_runner.runner.time.monotonic', return_value=1000),
              patch.object(r, '_task', side_effect=MeshError(429, 'rate limited')) as task):
            for _ in range(10):
                r.check_wakes()
            self.assertEqual(task.call_count, 1)
        self.assertTrue(r.pool.leased_to(a.id))
        self.assertEqual((source / 'README').read_text(), 'valuable ready progress')
        self.assertEqual(r.store.get(a.id).state, 'ready')
        with patch('mesh_runner.runner.time.monotonic', return_value=1010), patch.object(r, '_task') as task:
            r.check_wakes()
            task.assert_not_called()
        with patch('mesh_runner.runner.time.monotonic', return_value=1031):
            r.check_wakes()
        self.assertEqual(r.pool.leased_to(a.id), [])
        restarted = self.runner(r.mesh)
        restarted.refresh_me()
        restarted.check_wakes()
        self.assertEqual(restarted.pool.leased_to(a.id), [])
        self.assertEqual(restarted.store.get(a.id).wait, old_wait)
        write_replies(self.replies, {a.task8: '⏳ WAIT 2h: external work'})
        restarted.resume_ready(1)
        restored = restarted.store.get(a.id)
        self.assertEqual(restored.state, 'running')
        self.assertEqual(Path(restored.workspaces[0], 'README').read_text(), 'valuable ready progress')
        self.assertEqual(restarted.store.mark(a.agent, a.task_id, 'workspace_ready_hold')[0], '')
        self.settle_all(restarted)

    def test_paused_tick_after_restart_returns_only_its_aged_lease(self):
        r, (a, other) = self.parked(2)
        self.agent.workspace_wait_release_minutes = 1
        self.agent.paused = True
        r.store.update(a, state='ready')
        self.age(r, a)
        old_clock = r.store.mark(a.agent, a.task_id, 'workspace_ready_hold')[0]
        old_wait = dict(a.wait)
        other_paths = r.pool.leased_to(other.id)
        restarted = self.runner(r.mesh)
        with patch.object(restarted, '_spawn') as spawn:
            restarted.tick()
        self.assertEqual(restarted.pool.leased_to(a.id), [])
        self.assertEqual(restarted.pool.leased_to(other.id), other_paths)
        self.assertEqual(restarted.store.get(other.id).state, 'waiting')
        self.assertEqual(restarted.store.get(a.id).wait, old_wait)
        self.assertEqual(restarted.store.mark(a.agent, a.task_id, 'workspace_ready_hold')[0], old_clock)
        self.assertTrue(self.agent.paused)
        spawn.assert_not_called()

    def test_maintenance_keeps_ownership_writer_and_disabled_guards(self):
        for refusal in ('owner', 'writer', 'disk', 'inventory', 'disabled', 'future-clock', 'other-attempt'):
            with self.subTest(refusal=refusal):
                r, a = self.ready()
                self.age(r, a)
                if refusal == 'owner':
                    r.mesh.tasks[a.task_id]['assignee_id'] = 'foreign'
                if refusal == 'disabled':
                    self.agent.workspace_wait_release_minutes = 0
                if refusal == 'inventory':
                    (Path(a.workspaces[0]) / 'nested' / '.git').mkdir(parents=True)
                if refusal in ('future-clock', 'other-attempt'):
                    r.store.mark(a.agent, a.task_id, 'workspace_ready_hold', json.dumps({
                        'attempt': 'foreign' if refusal == 'other-attempt' else a.id,
                        'since': iso(now() + dt.timedelta(hours=1)) if refusal == 'future-clock' else
                                 '2000-01-01T00:00:00Z'}))
                with patch('mesh_runner.runner.group_alive', return_value=refusal == 'writer'):
                    if refusal == 'disk':
                        with patch('mesh_runner.checkpoint.shutil.disk_usage',
                                   return_value=type('D', (), {'free': 0})()):
                            r.check_wakes()
                    else:
                        r.check_wakes()
                self.assertTrue(r.pool.leased_to(a.id))
                self.assertEqual(r.store.get(a.id).state, 'ready')
                self.assertFalse(r.procs)
                self.tearDown(); self.setUp()
