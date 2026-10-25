"""Regenerable ignored dirs are removed only from idle, clean, free slots."""
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from test_workspace import make_repo
from mesh_runner.store import Store
from mesh_runner.workspace import RepoSpec, WorkspacePool, git
from mesh_runner.pool_ops import prune_ignored


class IgnoredCleanup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.base = make_repo(self.root)
        exclude = Path(git(self.base, 'rev-parse', '--path-format=absolute', '--git-path', 'info/exclude'))
        exclude.parent.mkdir(parents=True, exist_ok=True)
        exclude.write_text('node_modules/\ntarget/\n.next/\ndist/\n.venv/\nbuild/\n')
        self.store = Store(self.root / 'state.sqlite')
        self.pool = WorkspacePool(self.store, self.root / 'pool', [RepoSpec('app', self.base, max_slots=5)],
                                  owner='worker')

    def tearDown(self):
        self.tmp.cleanup()

    def slot(self, name='a', fill=('node_modules', 'target', '.next', 'dist', '.venv')):
        path = self.pool.lease(name, 'app')
        for d in fill:
            (path / d).mkdir(parents=True, exist_ok=True)
            (path / d / 'blob').write_text('x' * 4096)
        self.pool.release(name)
        return path

    def run_prune(self, **kw):
        return prune_ignored(self.pool, **{'idle_hours': 0, 'apply': True, **kw})

    def test_allowlisted_ignored_dirs_removed_and_bytes_counted(self):
        slot = self.slot()
        res = self.run_prune()
        self.assertEqual(len(res['dirs']), 5)
        self.assertGreaterEqual(res['freed_bytes'], 5 * 4096)
        for d in ('node_modules', 'target', '.next', 'dist', '.venv'):
            self.assertFalse((slot / d).exists())
        self.assertTrue((slot / 'README').exists())

    def test_dry_run_changes_nothing(self):
        slot = self.slot()
        res = self.run_prune(apply=False)
        self.assertEqual(len(res['dirs']), 5)
        self.assertEqual(res['freed_bytes'], 0)
        self.assertTrue((slot / 'node_modules' / 'blob').exists())

    def test_dir_outside_allowlist_is_kept_even_if_ignored(self):
        slot = self.slot(fill=('build', 'cache', 'node_modules'))
        self.run_prune()
        self.assertTrue((slot / 'build' / 'blob').exists())
        self.assertTrue((slot / 'cache' / 'blob').exists())
        self.assertFalse((slot / 'node_modules').exists())

    def test_unignored_allowlisted_dir_is_kept(self):
        slot = self.slot(fill=())
        (slot / 'dist').mkdir()
        (slot / 'dist' / 'blob').write_text('x')
        # not ignored in this slot: untracked file makes the tree dirty -> skipped as dirty
        git(self.base, 'config', 'core.excludesFile', '/dev/null')
        (Path(git(self.base, 'rev-parse', '--path-format=absolute', '--git-path', 'info/exclude'))).write_text('')
        self.run_prune()
        self.assertTrue((slot / 'dist' / 'blob').exists())

    def test_tracked_dir_is_kept(self):
        (self.base / 'dist').mkdir()
        (self.base / 'dist' / 'tracked.js').write_text('x')
        git(self.base, 'add', '-f', 'dist/tracked.js')
        git(self.base, 'commit', '-qm', 'track dist')
        git(self.base, 'push', '-q', 'origin', 'HEAD:main')
        slot = self.pool.lease('t', 'app')
        (slot / 'dist' / 'ignored.js').write_text('y')
        self.pool.release('t')
        self.run_prune()
        self.assertTrue((slot / 'dist' / 'tracked.js').exists())
        self.assertTrue((slot / 'dist' / 'ignored.js').exists())

    def test_symlink_outside_root_is_not_followed_or_removed(self):
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'precious').write_text('keep')
        with (Path(git(self.base, 'rev-parse', '--path-format=absolute', '--git-path', 'info/exclude'))).open('a') as f:
            f.write('node_modules\n')           # without the slash a symlink is ignored too
        slot = self.slot(fill=())
        os.symlink(outside, slot / 'node_modules')
        (slot / 'target').mkdir()
        os.symlink(outside, slot / 'target' / 'link')
        self.run_prune()
        self.assertTrue((outside / 'precious').exists())
        self.assertTrue((slot / 'node_modules').is_symlink())
        self.assertFalse((slot / 'target').exists())      # real dir removed; link inside not dereferenced
        self.assertTrue((outside / 'precious').exists())

    def test_leased_slot_is_untouched(self):
        slot = self.pool.lease('busy', 'app')
        (slot / 'node_modules').mkdir()
        (slot / 'node_modules' / 'blob').write_text('x')
        self.assertEqual(self.run_prune()['dirs'], [])
        self.assertTrue((slot / 'node_modules' / 'blob').exists())

    def test_slot_leased_between_listing_and_lock_is_rechecked(self):
        slot = self.slot()
        import mesh_runner.pool_ops as ops
        real = ops._bounded_base_lock

        def lease_first(base, deadline):
            self.pool.store.db.execute("UPDATE slots SET state='leased',attempt='z' WHERE path=?", (str(slot),))
            return real(base, deadline)
        with patch.object(ops, '_bounded_base_lock', lease_first):
            self.assertEqual(self.run_prune()['dirs'], [])
        self.assertTrue((slot / 'node_modules' / 'blob').exists())

    def test_dirty_tracked_and_untracked_slots_are_skipped(self):
        a, b = self.pool.lease('a', 'app'), self.pool.lease('b', 'app')
        self.assertNotEqual(a, b)
        for slot in (a, b):
            (slot / 'node_modules').mkdir()
            (slot / 'node_modules' / 'blob').write_text('x')
        # one dirty by tracked edit, the other by a stray untracked file; release would hold them
        self.pool.release('a'); self.pool.release('b')
        (a / 'README').write_text('edited')
        (b / 'scratch.txt').write_text('evidence')
        self.assertEqual(self.run_prune()['dirs'], [])
        self.assertTrue((a / 'node_modules' / 'blob').exists() and (b / 'node_modules' / 'blob').exists())

    def test_unpushed_commit_stash_and_submodule_slots_are_skipped(self):
        a = self.slot('a')
        (a / 'f').write_text('1')
        git(a, 'add', 'f'); git(a, 'commit', '-qm', 'local')
        self.assertEqual(self.run_prune()['dirs'], [])
        git(a, 'reset', '-q', '--hard', 'origin/main')
        (a / 'README').write_text('stash me')
        git(a, 'stash')
        self.assertEqual(self.run_prune()['dirs'], [])
        git(a, 'stash', 'drop')
        self.assertEqual(len(self.run_prune()['dirs']), 5)

    def test_submodule_metadata_slot_is_skipped(self):
        slot = self.slot()
        modules = Path(git(slot, 'rev-parse', '--path-format=absolute', '--git-path', 'modules'))
        modules.mkdir(parents=True, exist_ok=True)
        self.assertEqual(self.run_prune()['dirs'], [])
        self.assertTrue((slot / 'node_modules' / 'blob').exists())

    def test_idle_threshold_is_measured_from_release_not_file_mtime(self):
        slot = self.slot()
        old = time.time() - 30 * 86400
        for d in ('node_modules', 'target'):
            os.utime(slot / d, (old, old))
        self.assertEqual(self.run_prune(idle_hours=24)['dirs'], [])   # freed a moment ago
        self.pool.store.db.execute("UPDATE slots SET updated_at='2020-01-01T00:00:00+00:00' WHERE path=?",
                                   (str(slot),))
        self.assertEqual(len(self.run_prune(idle_hours=24)['dirs']), 5)

    def test_report_separates_eligible_from_freed_and_counts_skip_reasons(self):
        a, b, c = (self.pool.lease(n, 'app') for n in 'abc')
        for slot in (a, b):
            (slot / 'node_modules').mkdir()
            (slot / 'node_modules' / 'blob').write_text('x' * 4096)
        self.pool.release('a'); self.pool.release('b')
        dry = self.run_prune(apply=False, idle_hours=24)
        self.assertEqual(dry['skipped'], {'fresh-released': 2, 'leased': 1})
        self.assertEqual(dry['eligible_slots'], 0)
        self.pool.store.db.execute("UPDATE slots SET updated_at='2020-01-01T00:00:00+00:00' WHERE state='free'")
        dry = self.run_prune(apply=False, idle_hours=24)
        self.assertEqual((dry['eligible_slots'], dry['freed_bytes']), (2, 0))
        self.assertGreaterEqual(dry['eligible_bytes'], 2 * 4096)
        done = self.run_prune(idle_hours=24)
        self.assertGreaterEqual(done['freed_bytes'], 2 * 4096)
        self.assertEqual(done['errors'], 0)
        self.assertEqual(self.run_prune(idle_hours=24)['skipped'], {'nothing-to-prune': 2, 'leased': 1})

    def test_runner_flag_off_skips_cleanup(self):
        from mesh_runner.runner import Runner
        runner = Runner.__new__(Runner)
        runner.cfg = type('C', (), {'ignored_cleanup': False, 'ignored_cleanup_idle_hours': 0,
                                    'ignored_cleanup_interval_minutes': 1})()
        runner._ignored_next = 0.0
        runner.ignored_freed_bytes = 0
        runner.agents = [type('A', (), {'pool': self.pool, 'agent': type('N', (), {'name': 'x'})()})()]
        slot = self.slot()
        runner._prune_ignored_if_due()
        self.assertTrue((slot / 'node_modules' / 'blob').exists())
        runner.cfg.ignored_cleanup = True
        runner._prune_ignored_if_due()
        runner._ignored_worker.join(30)
        self.assertFalse((slot / 'node_modules').exists())
        self.assertGreater(runner.ignored_freed_bytes, 0)

    def test_cleanup_is_off_by_default(self):
        from mesh_runner.config import RunnerConfig as Config
        self.assertFalse(Config.__dataclass_fields__['ignored_cleanup'].default)
        import inspect
        from mesh_runner import config
        self.assertIn('"ignored_cleanup", False) is True', inspect.getsource(config))

    def test_slow_pools_share_one_deadline_and_never_block_the_loop(self):
        from mesh_runner.runner import Runner
        seen = []

        def slow(pool, idle_hours, apply, deadline):
            seen.append(deadline)
            time.sleep(0.3)
            return {'freed_bytes': 1}
        runner = Runner.__new__(Runner)
        runner.cfg = type('C', (), {'ignored_cleanup': True, 'ignored_cleanup_idle_hours': 0,
                                    'ignored_cleanup_interval_minutes': 1})()
        runner._ignored_next = 0.0
        runner.ignored_freed_bytes = 0
        runner.agents = [type('A', (), {'pool': None, 'agent': type('N', (), {'name': f'x{i}'})()})() for i in range(4)]
        with patch('mesh_runner.pool_ops.prune_ignored', slow):
            t0 = time.monotonic()
            runner._prune_ignored_if_due()
            self.assertLess(time.monotonic() - t0, 0.2)          # main loop is not held
            runner._ignored_worker.join(10)
        self.assertEqual(len(seen), 4)
        self.assertEqual(len(set(seen)), 1)                      # one budget, not one per agent
        self.assertEqual(runner.ignored_freed_bytes, 4)

    def test_symlinked_slot_or_ancestor_is_refused(self):
        slot = self.slot('a')
        outside = self.root / 'outside'
        shutil_move = __import__('shutil').move
        shutil_move(str(slot), str(outside))
        slot.symlink_to(outside)                           # slot-N -> directory outside the pool
        self.pool.store.db.execute("UPDATE slots SET updated_at='2020-01-01T00:00:00+00:00'")
        res = self.run_prune(idle_hours=24)
        self.assertTrue((outside / 'node_modules' / 'blob').exists())
        self.assertEqual(res['freed_bytes'], 0)
        self.assertEqual(res['skipped'].get('slot-outside-pool'), 1)

    def test_symlinked_repo_dir_ancestor_is_refused(self):
        slot = self.slot('a')
        repo_dir = slot.parent
        moved = self.root / 'moved-repo-dir'
        repo_dir.rename(moved)
        repo_dir.symlink_to(moved)
        self.pool.store.db.execute("UPDATE slots SET updated_at='2020-01-01T00:00:00+00:00'")
        res = self.run_prune(idle_hours=24)
        self.assertTrue((moved / slot.name / 'node_modules' / 'blob').exists())
        self.assertEqual(res['freed_bytes'], 0)

    def test_no_deletion_starts_after_budget_expires_during_size(self):
        slot = self.slot('a')
        self.pool.store.db.execute("UPDATE slots SET updated_at='2020-01-01T00:00:00+00:00'")
        import mesh_runner.pool_ops as po
        deadline = time.monotonic() + 60

        def late_size(path, dl):
            raise po.WorkspaceError('ignored cleanup budget exhausted')
        with patch.object(po, '_dir_bytes', late_size):
            res = prune_ignored(self.pool, idle_hours=0, apply=True, deadline=deadline)
        self.assertEqual(res['freed_bytes'], 0)
        self.assertTrue((slot / 'node_modules' / 'blob').exists())

    def test_removal_stops_when_budget_runs_out_mid_delete(self):
        import mesh_runner.pool_ops as po
        d = self.root / 'big'
        for i in range(5):
            (d / f'sub{i}').mkdir(parents=True)
            (d / f'sub{i}' / 'f').write_text('x')
        with self.assertRaises(po.WorkspaceError):
            po._bounded_rmtree(d, time.monotonic() - 1)
        self.assertTrue(d.exists())
        po._bounded_rmtree(d, time.monotonic() + 30)
        self.assertFalse(d.exists())

    def test_rmtree_does_not_follow_symlinks(self):
        import mesh_runner.pool_ops as po
        keep = self.root / 'keep'
        keep.mkdir()
        (keep / 'data').write_text('x')
        d = self.root / 'dd'
        d.mkdir()
        (d / 'link').symlink_to(keep)
        po._bounded_rmtree(d, time.monotonic() + 30)
        self.assertFalse(d.exists())
        self.assertTrue((keep / 'data').exists())


    def test_removal_stops_before_the_next_unlink_after_expiry(self):
        import mesh_runner.pool_ops as po
        d = self.root / 'three'
        d.mkdir()
        for i in range(3):
            (d / f'f{i}').write_text('x')
        deadline = time.monotonic() + 0.3
        real = os.unlink

        def slow_unlink(path, *a, **k):
            real(path, *a, **k)
            time.sleep(0.4)                                 # the clock passes the deadline here
        with patch.object(po.os, 'unlink', slow_unlink):
            with self.assertRaises(po.WorkspaceError):
                po._bounded_rmtree(d, deadline)
        self.assertEqual(len(list(d.iterdir())), 2)         # exactly one removed, dir kept

    def test_nested_tree_is_fully_removed_within_budget(self):
        import mesh_runner.pool_ops as po
        d = self.root / 'deep'
        (d / 'a' / 'b').mkdir(parents=True)
        for i in range(600):
            (d / 'a' / f'f{i}').write_text('x')
        (d / 'a' / 'b' / 'g').write_text('x')
        po._bounded_rmtree(d, time.monotonic() + 30)
        self.assertFalse(d.exists())

    def test_no_rmdir_starts_when_the_empty_scan_outlives_the_budget(self):
        import mesh_runner.pool_ops as po
        d = self.root / 'empty'
        d.mkdir()
        deadline = time.monotonic() + 0.3
        real_scandir = os.scandir

        def slow_scandir(path):
            time.sleep(0.4)                                 # the scan finishes after the deadline
            return real_scandir(path)
        with patch.object(po.os, 'scandir', slow_scandir), patch.object(po.os, 'rmdir') as rmdir:
            with self.assertRaises(po.WorkspaceError):
                po._bounded_rmtree(d, deadline)
        self.assertEqual(rmdir.call_count, 0)
        self.assertTrue(d.exists())

    def test_dir_swapped_for_symlink_after_listing_is_not_followed(self):
        import shutil
        import mesh_runner.pool_ops as po
        outside = self.root / 'victim'
        outside.mkdir()
        (outside / 'precious').write_text('x')
        d = self.root / 'tree'
        (d / 'sub').mkdir(parents=True)
        (d / 'sub' / 'f').write_text('x')

        swapped = []

        def swap():
            sub = d / 'sub'
            if not swapped and sub.is_dir() and not sub.is_symlink():
                swapped.append(1)                           # once; shutil below re-enters os.open
                shutil.rmtree(sub)
                sub.symlink_to(outside)
        real_open, real_scandir = os.open, os.scandir

        def open_(path, *a, **k):
            if os.path.basename(str(path)) == 'sub':
                swap()
            return real_open(path, *a, **k)

        def scandir_(path='.'):
            if isinstance(path, str) and path.endswith('/sub'):
                swap()
            return real_scandir(path)
        with patch.object(po.os, 'open', open_), patch.object(po.os, 'scandir', scandir_):
            with self.assertRaises(OSError):
                po._bounded_rmtree(d, time.monotonic() + 30)
        self.assertTrue((outside / 'precious').exists())

    def test_clean_slot_with_unignored_allowlisted_dir_is_rejected_by_check_ignore(self):
        import mesh_runner.pool_ops as po
        exclude = Path(git(self.base, 'rev-parse', '--path-format=absolute', '--git-path', 'info/exclude'))
        exclude.write_text('node_modules/\n')               # dist/ is deliberately NOT ignored
        path = self.pool.lease('a', 'app')
        self.pool.release('a')                              # release cleans untracked files
        (path / 'dist').mkdir()
        (path / 'dist' / 'blob').write_text('x')
        self.pool.store.db.execute("UPDATE slots SET updated_at='2020-01-01T00:00:00+00:00'")
        real_git = po.git

        def fake_git(cwd, *args, **k):
            if args[:2] == ('ls-files', '--others'):
                return 'dist/\0'                            # the listing wrongly offers dist/
            return real_git(cwd, *args, **k)
        with patch.object(po, '_slot_skip_reason', lambda *a, **k: ''), patch.object(po, 'git', fake_git):
            res = self.run_prune(idle_hours=24)
        self.assertTrue((path / 'dist' / 'blob').exists())
        self.assertEqual(res['freed_bytes'], 0)
        self.assertEqual(res['skipped'].get('nothing-to-prune'), 1)

    def _swap_race(self, victim_name):
        """Swap `victim_name` (the slot or its repo dir) for a symlink right before it is opened."""
        import mesh_runner.pool_ops as po
        root = self.root / 'par' / 'proot' if victim_name == 'par' else self.root / 'proot'
        slot = root / 'repo' / 'slot-1'
        (slot / 'node_modules').mkdir(parents=True)
        (slot / 'node_modules' / 'blob').write_text('x')
        outside = self.root / 'elsewhere'
        (outside / 'node_modules').mkdir(parents=True)
        (outside / 'node_modules' / 'precious').write_text('x')
        victim = {'repo': root / 'repo', 'root': root, 'par': root.parent}.get(victim_name, slot)
        if victim_name in ('root', 'par'):                   # <copy>/[proot/]repo/slot-1/node_modules
            outside = self.root / 'poolcopy'
            base = outside / 'proot' if victim_name == 'par' else outside
            (base / 'repo' / 'slot-1' / 'node_modules').mkdir(parents=True)
            (base / 'repo' / 'slot-1' / 'node_modules' / 'precious').write_text('x')
        swapped = []
        real_open = os.open

        def open_(path, *a, **k):
            if os.path.basename(str(path)) == {'root': 'proot'}.get(victim_name, victim_name) and not swapped:
                swapped.append(1)
                victim.rename(self.root / 'moved-away')
                victim.symlink_to(outside)
            return real_open(path, *a, **k)
        if victim_name == 'repo':                           # <elsewhere-parent>/slot-1/node_modules
            (self.root / 'x' / 'slot-1' / 'node_modules').mkdir(parents=True)
            (self.root / 'x' / 'slot-1' / 'node_modules' / 'precious').write_text('x')
            outside = self.root / 'x'
        with patch.object(po.os, 'open', open_):
            with self.assertRaises(OSError):
                po._bounded_rmtree(slot / 'node_modules', time.monotonic() + 30, anchor=root)
        precious = {'repo': outside / 'slot-1' / 'node_modules' / 'precious',
                    'root': outside / 'repo' / 'slot-1' / 'node_modules' / 'precious',
                    'par': outside / 'proot' / 'repo' / 'slot-1' / 'node_modules' / 'precious'}.get(
            victim_name, outside / 'node_modules' / 'precious')
        self.assertTrue(precious.exists())

    def test_slot_swapped_for_symlink_between_check_and_open_is_refused(self):
        self._swap_race('slot-1')

    def test_ancestor_swapped_for_symlink_between_check_and_open_is_refused(self):
        self._swap_race('repo')

    def test_pool_root_swapped_for_symlink_between_check_and_open_is_refused(self):
        self._swap_race('root')

    def test_pool_root_ancestor_swapped_for_symlink_between_check_and_open_is_refused(self):
        self._swap_race('par')

    def test_negative_idle_threshold_is_rejected_everywhere_and_prunes_nothing(self):
        from mesh_runner import cli
        from mesh_runner.config import ConfigError, load
        slot = self.slot('a')                                # released just now: must never qualify
        for bad in (-1, -0.001, float('nan'), float('inf')):
            with self.assertRaises(ValueError):
                prune_ignored(self.pool, idle_hours=bad, apply=True)
        self.assertTrue((slot / 'node_modules' / 'blob').exists())
        with self.assertRaises(SystemExit):                  # argparse rejects the CLI override
            with patch('sys.stderr'):
                cli.main(['pool', '--config', 'x.toml', '--prune-ignored', '--ignored-idle-hours', '-1'])
        for bad in ('-1', '-0.5', 'nan', 'inf', '"soon"'):
            cfg = self.root / 'bad.toml'
            cfg.write_text('[runner]\nmesh_url = "x"\nstate_dir = "%s"\nignored_cleanup_idle_hours = %s\n'
                           % (self.root, bad))
            with self.assertRaises(ConfigError):
                load(cfg)
        cfg.write_text('[runner]\nmesh_url = "x"\nstate_dir = "%s"\nignored_cleanup_idle_hours = 0\n' % self.root)
        self.assertEqual(load(cfg).ignored_cleanup_idle_hours, 0.0)

    def test_non_numeric_idle_threshold_is_rejected(self):
        from mesh_runner.config import ConfigError, _idle_hours, load
        slot = self.slot('a')
        for bad in (None, '1', True, False, [1]):
            with self.assertRaises(ValueError):
                prune_ignored(self.pool, idle_hours=bad, apply=True)
            with self.assertRaises(ConfigError):
                _idle_hours(bad)
        self.assertTrue((slot / 'node_modules' / 'blob').exists())
        for raw in ('"1"', 'true'):
            cfg = self.root / 'bad2.toml'
            cfg.write_text('[runner]\nmesh_url = "x"\nstate_dir = "%s"\nignored_cleanup_idle_hours = %s\n'
                           % (self.root, raw))
            with self.assertRaises(ConfigError):
                load(cfg)
        self.assertEqual(_idle_hours(2), 2.0)

    def test_symlinked_pool_root_is_refused(self):
        slot = self.slot('a')
        real_root = self.pool.root
        moved = self.root / 'pool-moved'
        real_root.rename(moved)
        real_root.symlink_to(moved)                          # pool root itself is a symlink
        self.pool.store.db.execute("UPDATE slots SET updated_at='2020-01-01T00:00:00+00:00'")
        res = self.run_prune(idle_hours=24)
        self.assertTrue((moved / slot.relative_to(real_root) / 'node_modules' / 'blob').exists())
        self.assertEqual(res['freed_bytes'], 0)
        self.assertEqual(res['skipped'].get('slot-outside-pool'), 1)

if __name__ == '__main__':
    unittest.main()
