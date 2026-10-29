"""A recovery permission buys one start, never a gate or budget bypass."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from mesh_runner import cap_rearm
from mesh_runner.store import Store
from fakes import FakeMesh, calls, task, write_replies, FUTURE_AT
from test_requeue_on_cap import CapBench


class RearmStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'runner.sqlite'
        self.s = Store(self.path)
        self.addCleanup(self.s.db.close)
        self.s.mark('lane', 'task', 'cap_wait', json.dumps({'kind': 'start cap', 'at': 'now'}))

    def test_preview_does_not_grant_and_consumption_is_atomic_across_connections(self):
        cap_rearm.request(self.s, 'lane', 'task', 'transport repaired')
        self.assertIsNone(cap_rearm.pending(self.s, 'lane', 'task'))
        result = cap_rearm.request(self.s, 'lane', 'task', 'transport repaired', apply=True)
        other = Store(self.path)
        self.addCleanup(other.db.close)
        with self.assertRaisesRegex(ValueError, 'pending rearm'):
            cap_rearm.request(other, 'lane', 'task', 'again', apply=True)
        a = self.s.create('lane', 'task', 'claude', cap_rearm=result['grant'])
        self.s.update(a, state='failed')
        with self.assertRaises(sqlite3.IntegrityError):
            other.create('lane', 'task', 'claude', cap_rearm=result['grant'])
        with self.assertRaisesRegex(ValueError, 'already been rearmed'):
            cap_rearm.request(other, 'lane', 'task', 'again', apply=True)
        self.assertEqual(len(other.by_state('lane', ('starting',))), 0)
        audit = self.s.mark('lane', 'task', 'cap_rearm_audit:' + result['grant'])
        self.assertEqual(json.loads(audit[0])['attempt'], a.id)

    def test_cli_preview_and_explicit_apply(self):
        import contextlib
        import io
        from types import SimpleNamespace
        from unittest.mock import patch
        from mesh_runner.cli import main
        tid = '00000000-0000-0000-0000-000000000001'
        self.s.mark('lane', tid, 'cap_wait', json.dumps({'kind': 'start cap', 'at': 'now'}))
        # Use the canonical state filename used by the CLI.
        cfg = SimpleNamespace(state_dir=self.path.parent, agents=[SimpleNamespace(name='lane')])
        args = ['rearm-cap', '--config', 'unused', '--agent', 'lane', '--task', tid, '--reason', 'repair']
        with patch('mesh_runner.cli.config_mod.load', return_value=cfg), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(args), 0)
            self.assertFalse(json.loads(out.getvalue())['applied'])
            self.assertEqual(main(args + ['--apply']), 0)
        self.assertIsNotNone(cap_rearm.pending(self.s, 'lane', tid))

    def test_simultaneous_operators_issue_only_one_grant(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        barrier = Barrier(2)
        def issue():
            store = Store(self.path)
            try:
                barrier.wait(timeout=5)
                try:
                    cap_rearm.request(store, 'lane', 'task', 'repair', apply=True)
                    return True
                except ValueError:
                    return False
            finally:
                store.db.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: issue(), range(2)))
        self.assertEqual(sorted(results), [False, True])

    def test_simultaneous_consumers_reserve_only_one_attempt(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        grant = cap_rearm.request(self.s, 'lane', 'task', 'repair', apply=True)['grant']
        barrier = Barrier(2)
        def consume():
            store = Store(self.path)
            try:
                barrier.wait(timeout=5)
                try:
                    store.create('lane', 'task', 'claude', cap_rearm=grant)
                    return True
                except sqlite3.IntegrityError:
                    return False
            finally:
                store.db.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: consume(), range(2)))
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(len(self.s.by_state('lane', ('starting',))), 1)

    def test_failed_insert_rolls_back_permission_consumption(self):
        result = cap_rearm.request(self.s, 'lane', 'task', 'repair', apply=True)
        self.s.db.execute("CREATE TRIGGER reject_attempt BEFORE INSERT ON attempts BEGIN SELECT RAISE(ABORT, 'injected'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.s.create('lane', 'task', 'claude', cap_rearm=result['grant'])
        self.assertIsNotNone(cap_rearm.pending(self.s, 'lane', 'task'))
        audit = json.loads(self.s.mark('lane', 'task', 'cap_rearm_audit:' + result['grant'])[0])
        self.assertNotIn('consumed_at', audit)

    def test_wait_open_attempt_and_changed_cap_refuse(self):
        self.s.mark('lane', 'task', 'cap_wait', json.dumps({'kind': 'start cap', 'at': 'now', 'wait': {'kind': 'card'}}))
        with self.assertRaisesRegex(ValueError, 'WAIT'):
            cap_rearm.request(self.s, 'lane', 'task', 'repair', apply=True)
        self.s.mark('lane', 'task', 'cap_wait', json.dumps({'kind': 'start cap', 'at': 'now'}))
        self.s.mark('lane', 'task', 'cap_observation', 'waiting-attempt')
        with self.assertRaisesRegex(ValueError, 'WAIT'):
            cap_rearm.request(self.s, 'lane', 'task', 'repair', apply=True)
        self.s.mark('lane', 'task', 'cap_observation', '')
        a = self.s.create('other-lane', 'task', 'claude')
        with self.assertRaisesRegex(ValueError, 'open attempt'):
            cap_rearm.request(self.s, 'lane', 'task', 'repair', apply=True)
        self.s.update(a, state='completed')
        cap_rearm.request(self.s, 'lane', 'task', 'repair', apply=True)
        self.s.mark('lane', 'task', 'cap_wait', json.dumps({'kind': 'start cap', 'at': 'later'}))
        self.assertIsNone(cap_rearm.pending(self.s, 'lane', 'task'))


class RearmRunner(CapBench):
    def test_recovery_starts_once_with_same_inputs_and_keeps_start_accounting(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        write_replies(self.replies, {t['id'][:8]: f'⏳ WAIT {FUTURE_AT}: external=fixture-window source=card:#abcdef12 soak'})
        r = self.runner(mesh)
        r.refresh_me()
        self.started(r, t)
        r.tick()
        self.assertEqual(calls(self.calls), [])
        cap_rearm.request(r.store, 'builder', t['id'], 'transport repaired', apply=True)
        self.settle_all(r)
        self.assertEqual(len(calls(self.calls)), 1)
        self.assertEqual(r.store.starts_today('builder', t['id']), 3)
        self.assertIsNone(cap_rearm.pending(r.store, 'builder', t['id']))

    def test_human_hold_still_wins_and_does_not_spend_grant(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.runner(mesh)
        r.refresh_me()
        self.started(r, t)
        r.tick()
        cap_rearm.request(r.store, 'builder', t['id'], 'transport repaired', apply=True)
        mesh.tasks[t['id']]['human_gate'] = True
        r.tick()
        self.assertEqual(calls(self.calls), [])
        self.assertIsNotNone(cap_rearm.pending(r.store, 'builder', t['id']))

    def test_dependency_preserves_grant_and_auth_never_launches(self):
        from mesh_runner import auth
        t, blocker = task(1), task(2)
        mesh = FakeMesh([t, blocker], max_concurrent=1)
        r = self.runner(mesh)
        r.refresh_me()
        self.started(r, t)
        r._start_cap(dict(t), 2)
        cap_rearm.request(r.store, 'builder', t['id'], 'transport repaired', apply=True)
        mesh.deps[t['id']] = [{'dependency_type': 'blocks', 'depends_on_task_id': blocker['id']}]
        self.assertFalse(r._start_one(dict(mesh.tasks[t['id']])))
        self.assertEqual(calls(self.calls), [])
        self.assertIsNotNone(cap_rearm.pending(r.store, 'builder', t['id']))
        mesh.deps[t['id']] = []
        r._hold_cache.clear()
        profile = auth.profile_id(r._fresh_env(), self.agent.profile_dir)
        auth.hold(r.store, profile)
        self.assertFalse(r._start_one(dict(mesh.tasks[t['id']])))
        self.assertEqual(calls(self.calls), [])
        self.assertIsNone(cap_rearm.pending(r.store, 'builder', t['id']))
