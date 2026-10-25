"""Disk admission stays opt-in and pool telemetry is read-only."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_runner import Bench
from fakes import FakeMesh, task, write_replies
from test_workspace import make_repo
from mesh_runner.store import Store
from mesh_runner.workspace import (POOL_EVICTION_HOLD_NOTE, POOL_INDEX_FLAGS_HOLD_NOTE,
                                   RepoSpec, WorkspacePool, WorkspaceError, git)
from mesh_runner.pool_ops import evict, summary


class DiskPool(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.base = make_repo(self.root)
        self.store = Store(self.root / 'state.sqlite')
        self.pool = WorkspacePool(self.store, self.root / 'pool', [RepoSpec('app', self.base, max_slots=5)],
                                  owner='worker')

    def tearDown(self):
        self.tmp.cleanup()

    def free_slot(self, attempt='a'):
        p = self.pool.lease(attempt, 'app')
        self.pool.release(attempt)
        return p

    def test_pressure_blocks_add_and_reuse_without_fetch_or_lease_changes(self):
        self.pool.min_free_gb = 1e12
        with patch('mesh_runner.workspace.git') as commands:
            with self.assertRaisesRegex(WorkspaceError, 'disk below floor'):
                self.pool.lease('new', 'app')
            self.assertFalse(any(c.args[1:2] in (("fetch",), ("worktree",), ("checkout",))
                                 for c in commands.call_args_list))
        self.assertEqual(self.pool.rows(), [])
        self.pool.min_free_gb = 0
        path = self.free_slot()
        before = self.pool.rows()
        self.pool.min_free_gb = 1e12
        with self.assertRaises(WorkspaceError):
            self.pool.lease('reuse', 'app')
        self.assertEqual(before, self.pool.rows())
        self.assertTrue(path.exists())

    def test_default_off_does_not_measure_disk(self):
        with patch('mesh_runner.workspace.shutil.disk_usage', side_effect=OSError):
            path = self.pool.lease('a', 'app')
        self.assertTrue(path.exists())

    def test_dry_run_then_bounded_eviction_and_persisted_counter(self):
        first, second = self.pool.lease('a', 'app'), self.pool.lease('b', 'app')
        self.pool.release('a'); self.pool.release('b')
        self.assertEqual(evict(self.pool, idle_hours=0, keep=1, limit=1), [str(second)])
        self.assertTrue(second.exists())
        self.assertEqual(evict(self.pool, idle_hours=0, keep=1, limit=1, apply=True), [str(second)])
        self.assertTrue(first.exists()); self.assertFalse(second.exists())
        info = summary(Store(self.root / 'state.sqlite'), self.root / 'pool')
        self.assertEqual(info['slots_total'], 1)
        self.assertEqual(info['evicted_total'], 1)
        self.assertIsNotNone(info['pool_size_mb'])
        self.assertEqual(evict(self.pool, idle_hours=0, keep=1, limit=1, apply=True), [])

    def test_ignored_untracked_ahead_leased_and_held_are_never_removed(self):
        paths = [self.pool.lease(str(i), 'app') for i in range(5)]
        for i in range(3): self.pool.release(str(i))
        (paths[0] / 'cache').mkdir(); (paths[0] / 'cache' / 'valuable').write_text('keep')
        (paths[1] / 'evidence').write_text('keep')
        (paths[2] / 'README').write_text('ahead')
        git(paths[2], 'commit', '-qam', 'private')
        self.pool._set(paths[4], 'app', 'held', '4', 'dirty')
        self.assertEqual(evict(self.pool, idle_hours=0, keep=0, limit=20, apply=True), [])
        self.assertTrue(all(p.exists() for p in paths))
        self.assertEqual((paths[0] / 'cache' / 'valuable').read_text(), 'keep')
        self.assertEqual((paths[1] / 'evidence').read_text(), 'keep')

    def test_recent_and_outside_managed_paths_are_not_candidates(self):
        path = self.free_slot()
        self.assertEqual(evict(self.pool, idle_hours=6, keep=0, limit=1, apply=True), [])
        outside = self.root / 'outside'
        git(self.base, 'worktree', 'add', '--detach', str(outside), 'origin/main')
        self.pool._set(outside, 'app', 'free')
        self.assertEqual(evict(self.pool, idle_hours=0, keep=0, limit=1, apply=True), [str(path)])
        self.assertTrue(outside.exists())

    def test_failed_remove_holds_slot_and_retains_files(self):
        path = self.free_slot()
        from mesh_runner import pool_ops
        actual = pool_ops.git
        def fail(cwd, *args, **kwargs):
            if args[:2] == ('worktree', 'remove'):
                raise WorkspaceError('test failure')
            return actual(cwd, *args, **kwargs)
        with patch.object(pool_ops, 'git', side_effect=fail):
            self.assertEqual(evict(self.pool, idle_hours=0, keep=0, limit=1, apply=True), [])
        self.assertEqual(self.pool.rows()[0]['state'], 'held')
        self.assertEqual(self.pool.rows()[0]['note'], POOL_EVICTION_HOLD_NOTE)
        self.assertTrue(path.exists())

        # A clean row would normally be auto-freed on startup. The durable
        # eviction marker must survive restart until explicit operator review.
        restarted_store = Store(self.root / 'state.sqlite')
        restarted = WorkspacePool(restarted_store, self.root / 'pool',
                                  [RepoSpec('app', self.base, max_slots=5)], owner='worker')
        self.assertEqual(restarted.unsafe_reason(path, restarted.repos['app']), '')
        self.assertEqual(restarted.recheck_held(set()), [])
        row = restarted.rows()[0]
        self.assertEqual((row['state'], row['note']), ('held', POOL_EVICTION_HOLD_NOTE))
        self.assertTrue(path.exists())
        self.assertEqual(restarted.free_path(str(path)), 'freed')

    def test_hidden_tracked_edits_are_quarantined_for_both_index_flags(self):
        for flag in ('--assume-unchanged', '--skip-worktree'):
            with self.subTest(flag=flag):
                path = self.free_slot(flag)
                git(path, 'update-index', flag, '--', 'README')
                (path / 'README').write_text(f'local edit hidden by {flag}\n')
                self.assertTrue(self.pool.index_flags_reason(path))
                self.assertIn('index flags', self.pool.unsafe_reason(path, self.pool.repos['app']))
                self.assertEqual(evict(self.pool, idle_hours=0, keep=0, limit=1), [])
                self.assertEqual(evict(self.pool, idle_hours=0, keep=0, limit=1, apply=True), [])
                row = next(r for r in self.pool.rows() if r['path'] == str(path))
                self.assertEqual((row['state'], row['note']), ('held', POOL_INDEX_FLAGS_HOLD_NOTE))
                self.assertEqual((path / 'README').read_text(), f'local edit hidden by {flag}\n')
                restarted = WorkspacePool(Store(self.root / 'state.sqlite'), self.root / 'pool',
                                          [RepoSpec('app', self.base, max_slots=5)], owner='worker')
                self.assertEqual(restarted.recheck_held(set()), [])
                row = next(r for r in restarted.rows() if r['path'] == str(path))
                self.assertEqual((row['state'], row['note']), ('held', POOL_INDEX_FLAGS_HOLD_NOTE))
                self.assertEqual((path / 'README').read_text(), f'local edit hidden by {flag}\n')

    def test_index_flag_scan_uses_the_bounded_inspection_timeout(self):
        path = self.free_slot()
        original = self.pool.index_flags_reason
        seen = []

        def bounded(path, *, timeout):
            seen.append(timeout)
            return original(path, timeout=timeout)

        with patch.object(self.pool, 'index_flags_reason', side_effect=bounded):
            self.assertEqual(evict(self.pool, idle_hours=0, keep=0, limit=1), [str(path)])
        self.assertEqual(len(seen), 1)
        self.assertGreater(seen[0], 0)
        self.assertLessEqual(seen[0], 3)

    def test_invalid_byte_index_path_is_a_controlled_hold_not_an_exception(self):
        import subprocess
        slot = self.pool.lease('bytes', 'app')
        blob = subprocess.check_output(['git', '-C', str(slot), 'rev-parse', 'HEAD:README']).strip()
        subprocess.run(['git', '-C', str(slot), 'update-index', '-z', '--index-info'],
                       input=b'100644 ' + blob + b'\tbad-\xff\0', check=True)
        self.assertEqual(self.pool.index_flags_reason(slot), '')
        kept = self.pool.release('bytes')   # must not raise UnicodeDecodeError
        self.assertEqual([k[0] for k in kept], [str(slot)])
        row = next(r for r in self.pool.rows() if r['path'] == str(slot))
        self.assertEqual(row['state'], 'held')

    def test_eviction_lock_resolution_stays_inside_the_deadline(self):
        import os, sys, shutil, time
        from mesh_runner.workspace import _COMMON_DIRS
        slot = self.free_slot('late')
        wrapper_dir = self.root / 'bin'; wrapper_dir.mkdir()
        real = shutil.which('git')
        wrapper = wrapper_dir / 'git'
        wrapper.write_text('#!' + sys.executable + '\nimport os, sys, time\n'
                           'if sys.argv[-2:] == ["rev-parse", "--git-common-dir"]: time.sleep(2.0)\n'
                           f'os.execv({real!r}, [{real!r}] + sys.argv[1:])\n')
        wrapper.chmod(0o755)
        old = os.environ['PATH']
        _COMMON_DIRS.clear()
        os.environ['PATH'] = str(wrapper_dir) + os.pathsep + old
        try:
            start = time.monotonic()
            removed = evict(self.pool, idle_hours=0, keep=0, limit=1, apply=True, deadline=start + 0.6)
            elapsed = time.monotonic() - start
        finally:
            os.environ['PATH'] = old
        self.assertLess(elapsed, 1.5)
        self.assertEqual(removed, [])
        self.assertTrue(slot.exists())


class PoolCli(unittest.TestCase):
    def test_pool_command_reports_previews_and_apply_needs_the_exclusive_lock(self):
        import contextlib, io, json
        from mesh_runner.cli import main
        from mesh_runner.runner import Runner
        with tempfile.TemporaryDirectory() as root:
            cfg = Path(root) / 'runner.toml'
            cfg.write_text(f'[runner]\nmesh_url="http://unused"\nstate_dir={json.dumps(root)}\n')
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(main(['pool', '--config', str(cfg)]), 0)
            self.assertIn('evicted_total', json.loads(out.getvalue())['pool'])
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(main(['pool', '--config', str(cfg), '--evict']), 0)
            self.assertEqual(json.loads(out.getvalue())['evict'], {'applied': False, 'slots': []})
            holder = Runner(__import__('mesh_runner.config', fromlist=['load']).load(str(cfg)))
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(main(['pool', '--config', str(cfg), '--evict', '--apply']), 2)
            self.assertIn('another runner owns', err.getvalue())
            holder.lock_file.close() if hasattr(holder, 'lock_file') else None
            del holder


class Admission(Bench):
    def ready_with_lease(self, session='previous-session'):
        t = task(1 if not session else 2, cat='in_progress')
        mesh = FakeMesh([t])
        runner = self.runner(mesh)
        runner.refresh_me()
        a = runner.store.create(self.agent.name, t['id'], 'claude')
        source = runner.pool.lease(a.id, 'app')
        (source / 'README').write_text('valuable unfinished work')
        runner.store.update(a, state='ready', session_id=session, workspaces=[str(source)],
                            wait={'kind': 'time', 'until': '2099-01-01T00:00:00Z'})
        runner.pool.min_free_gb = 6
        return runner, a, source

    def test_existing_lease_resume_below_floor_never_reserves_or_starts(self):
        for session in ('', 'previous-session'):
            with self.subTest(session=session):
                runner, a, source = self.ready_with_lease(session)
                before = runner.store.get(a.id)
                with (patch('mesh_runner.workspace.shutil.disk_usage',
                            return_value=type('D', (), {'free': 5 * 1024**3})()),
                      patch.object(runner, '_gate', return_value=('', {})) as gate,
                      patch.object(runner, '_acquire') as checkout,
                      patch.object(runner, '_spawn') as spawn):
                    runner._resume_one(a)
                self.assertEqual(runner.store.get(a.id), before)
                self.assertEqual(runner.pool.leased_to(a.id), [str(source)])
                self.assertEqual((source / 'README').read_text(), 'valuable unfinished work')
                self.assertEqual(runner.pool._checkpoint_rows(a.id), [])
                self.assertEqual(runner.mesh.tasks[a.task_id]['assignee_id'], runner.agent_id)
                gate.assert_not_called(); checkout.assert_not_called(); spawn.assert_not_called()
                self.tearDown(); self.setUp()

    def test_floor_rechecked_before_session_preparation_and_writer(self):
        for session in ('', 'previous-session'):
            with self.subTest(session=session):
                runner, a, source = self.ready_with_lease(session)
                before_wait = dict(a.wait)
                with (patch.object(runner.pool, 'disk_hold', side_effect=['', 'worktree disk below floor']),
                      patch.object(runner.adapter, 'prepare_resume', return_value='') as prepare,
                      patch.object(runner.adapter, 'spawn', side_effect=AssertionError('writer must not start below floor')) as spawn):
                    runner._resume_one(a)
                prepare.assert_not_called(); spawn.assert_not_called()
                current = runner.store.get(a.id)
                self.assertEqual((current.state, current.wait, current.pid), ('ready', before_wait, 0))
                self.assertEqual(runner.pool.leased_to(a.id), [str(source)])
                self.assertEqual(runner.mesh.checkouts, {})
                self.assertGreater(runner._resume_hold[a.id], 0)
                self.tearDown(); self.setUp()

    def test_existing_lease_resume_above_floor_still_starts_both_session_paths(self):
        for session in ('', 'previous-session'):
            with self.subTest(session=session):
                runner, a, source = self.ready_with_lease(session)
                write_replies(self.replies, {a.task8: '⏳ WAIT 2h: external work'})
                with patch('mesh_runner.workspace.shutil.disk_usage',
                           return_value=type('D', (), {'free': 7 * 1024**3})()):
                    runner._resume_one(a)
                current = runner.store.get(a.id)
                self.assertEqual(current.state, 'running')
                self.assertGreater(current.pid, 0)
                self.assertEqual((source / 'README').read_text(), 'valuable unfinished work')
                self.finish_turn(runner)
                self.tearDown(); self.setUp()

    def test_space_lost_during_native_prepare_does_not_consume_authorization_or_spawn(self):
        runner, a, source = self.ready_with_lease()
        with (patch.object(runner.pool, 'disk_hold', side_effect=['', 'worktree disk below floor']),
              patch.object(runner.adapter, 'prepare_resume', return_value='') as prepare,
              patch.object(runner.adapter, 'spawn') as spawn):
            from unittest.mock import Mock
            before_spawn = Mock()
            started = runner._spawn(a, 'continue', resume=True, expect=('ready',),
                                    before_spawn=before_spawn)
        self.assertFalse(started)
        prepare.assert_called_once()
        spawn.assert_not_called(); before_spawn.assert_not_called()
        self.assertEqual(runner.pool.leased_to(a.id), [str(source)])
        self.assertEqual(runner.store.get(a.id).state, 'ready')

    def test_active_checkpoint_is_preserved_by_existing_lease_floor_hold(self):
        runner, a, source = self.ready_with_lease()
        cp = runner.pool._checkpoints().create(a.id, source, {'wait': a.wait})
        runner.store.db.execute('INSERT INTO workspace_checkpoints VALUES(?,?,?,?,?,?,?)',
            (self.agent.name, a.id, 'app', str(cp), str(source), 'active', ''))
        journal = runner.pool._checkpoint_rows(a.id)
        with patch.object(runner.pool, 'disk_hold', return_value='worktree disk below floor'):
            runner._resume_one(a)
        self.assertEqual(runner.pool._checkpoint_rows(a.id), journal)
        self.assertEqual(runner.pool.leased_to(a.id), [str(source)])
        self.assertEqual((source / 'README').read_text(), 'valuable unfinished work')

    def test_low_disk_partial_release_never_captures_unjournaled_repository(self):
        from dataclasses import replace
        runner, a, source = self.ready_with_lease()
        # A second existing lease has not reached verification at the crash boundary.
        runner.pool.min_free_gb = 0
        runner.pool.repos['other'] = replace(self.agent.repos[0], name='other')
        other = runner.pool.lease(a.id, 'other')
        runner.pool.min_free_gb = 6
        runner.store.update(a, workspaces=[str(source), str(other)])
        cp = runner.pool._checkpoints().create(a.id, source, {'wait': a.wait})
        runner.store.db.execute('INSERT INTO workspace_checkpoints VALUES(?,?,?,?,?,?,?)',
            (self.agent.name, a.id, 'app', str(cp), str(source), 'verified', ''))
        before = runner.pool._checkpoint_rows(a.id)
        with (patch.object(runner.pool, 'disk_hold', return_value='worktree disk below floor'),
              patch.object(runner.pool, 'suspend') as suspend,
              patch.object(runner, '_gate') as gate):
            runner._resume_one(a)
        suspend.assert_not_called(); gate.assert_not_called()
        self.assertEqual(runner.pool._checkpoint_rows(a.id), before)
        self.assertEqual(set(runner.pool.leased_to(a.id)), {str(source), str(other)})

    def test_low_disk_resume_finishes_verified_release(self):
        self._assert_low_disk_release('verified')

    def test_low_disk_resume_finishes_releasing_release(self):
        self._assert_low_disk_release('releasing')

    def _assert_low_disk_release(self, phase):
        mesh = FakeMesh([task(1, cat='in_progress')])
        runner = self.runner(mesh)
        runner.refresh_me()
        a = runner.store.create(self.agent.name, task(1)['id'], 'claude')
        source = runner.pool.lease(a.id, 'app')
        (source / 'README').write_text('checkpointed work')
        wait = {'kind': 'time', 'until': '2099-01-01T00:00:00Z'}
        runner.store.update(a, state='ready', workspaces=[str(source)], wait=wait)
        checkpoints = runner.pool._checkpoints()
        cp = checkpoints.create(a.id, source, {'wait': wait})
        runner.store.db.execute(
            'INSERT INTO workspace_checkpoints VALUES(?,?,?,?,?,?,?)',
            (self.agent.name, a.id, 'app', str(cp), str(source), phase, ''))
        runner.pool.min_free_gb = 6
        with patch('mesh_runner.workspace.shutil.disk_usage',
                   return_value=type('D', (), {'free': 5 * 1024**3})()), \
             patch('mesh_runner.checkpoint.Checkpoints.create',
                   side_effect=AssertionError('must not recapture')), \
             patch('mesh_runner.checkpoint.Checkpoints.restore') as replay, \
             patch.object(runner.pool, 'lease') as lease, \
             patch.object(runner, '_gate') as gate, \
             patch.object(runner, '_acquire') as checkout, \
             patch.object(runner, '_spawn') as spawn:
            runner._resume_one(a)
        journal = runner.pool._checkpoint_rows(a.id)[0]
        self.assertEqual((journal['phase'], journal['target']), ('released', ''))
        self.assertEqual(runner.pool.leased_to(a.id), [])
        self.assertEqual(runner.pool.rows()[0]['state'], 'free')
        checkpoints.verify(cp)
        current = runner.store.get(a.id)
        self.assertEqual((current.state, current.wait, current.pid), ('ready', wait, 0))
        replay.assert_not_called()
        lease.assert_not_called()
        spawn.assert_not_called()
        gate.assert_not_called()
        checkout.assert_not_called()

    def test_disk_hold_precedes_mesh_checkout_and_does_not_fail_product_task(self):
        self.cfg.min_free_gb = 1e12
        mesh = FakeMesh([task(1)])
        runner = self.runner(mesh)
        runner.tick()
        self.assertEqual(mesh.checkouts, {})
        self.assertEqual(runner.pool.rows(), [])
        self.assertEqual(self.states(runner), {})
        self.assertIn('disk below floor', runner.plan()['disk_hold'])
        self.assertIn('disk below floor', runner.plan()['tasks'][0]['decision'])
        self.assertEqual(mesh.comment_log, {})

    def test_floor_config_is_explicit_opt_in_and_rejects_invalid_numbers(self):
        from mesh_runner.config import load, ConfigError
        config = self.root / 'runner.toml'
        config.write_text('[runner]\nmesh_url="http://mesh"\n')
        self.assertEqual(load(config).min_free_gb, 0)
        config.write_text('[runner]\nmesh_url="http://mesh"\nmin_free_gb=15\n')
        self.assertEqual(load(config).min_free_gb, 15)
        for value in ['-1', 'nan', 'inf']:
            config.write_text('[runner]\nmesh_url="http://mesh"\nmin_free_gb='+value+'\n')
            with self.assertRaises(ConfigError):
                load(config)
