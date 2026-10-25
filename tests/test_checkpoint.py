import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from mesh_runner.checkpoint import DEFAULT_EXCLUDE_NAMES, Checkpoints, _gitdir, fingerprint, submodule_plan
from mesh_runner.config import ConfigError, _exclude_names
from mesh_runner.store import Store
from mesh_runner.workspace import RepoSpec, WorkspacePool, WorkspaceError, git
from test_workspace import make_repo


def _modules(path):
    modules = Path(git(path, 'rev-parse', '--git-path', 'modules'))
    return modules if modules.is_absolute() else path / modules


class Checkpoint(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.base = make_repo(self.root)
        self.store = Store(self.root / 's.sqlite')
        self.pool = WorkspacePool(self.store, self.root / 'pool', [RepoSpec('app', self.base, max_slots=1)], owner='agent')
        self.source = self.pool.lease('attempt', 'app')

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_round_trip_restart_and_once_only(self):
        p = self.source
        (p / 'README').write_bytes(b'staged\0binary')
        git(p, 'add', 'README')
        (p / 'README').write_bytes(b'unstaged\0binary')
        (p / 'cache').mkdir()
        (p / 'cache' / 'private').write_bytes(b'ignored data')
        (p / 'odd\nname').write_text('evidence')
        (p / 'executable').write_text('echo yes')
        (p / 'executable').chmod(0o755)
        os.symlink('/missing', p / 'dangling')
        expected = fingerprint(p, None, (), ('cache',))      # git-ignored data is regenerable: not snapshotted
        self.pool.suspend('attempt', {'session_id': 'old', 'wait': {'target': 'card'}}, writer_stopped=True)
        self.assertFalse(self.pool.full('app'))
        other = self.pool.lease('other', 'app')
        (other / 'other').write_text('new owner')
        self.pool.release('other')
        restarted = WorkspacePool(Store(self.root / 's.sqlite'), self.root / 'pool',
                                  [RepoSpec('app', self.base, max_slots=1)], owner='agent')
        paths, manifests = restarted.restore('attempt')
        self.assertEqual(fingerprint(Path(paths[0]), None, (), ('cache',)), expected)
        self.assertEqual(manifests[0]['metadata']['session_id'], 'old')
        (Path(paths[0]) / 'README').write_text('later work')
        restarted.restore('attempt')
        self.assertEqual((Path(paths[0]) / 'README').read_text(), 'later work')

    def test_intent_to_add_entries_do_not_block_suspend(self):
        p = self.source
        (p / 'new.txt').write_text('intent to add')
        git(p, 'add', '-N', 'new.txt')
        before = git(p, 'ls-files', '-s', 'new.txt')
        self.pool.suspend('attempt', {'session_id': 's', 'wait': {'target': 'card'}}, writer_stopped=True)
        paths, _ = self.pool.restore('attempt')
        self.assertEqual((Path(paths[0]) / 'new.txt').read_text(), 'intent to add')
        self.assertEqual(git(Path(paths[0]), 'ls-files', '-s', 'new.txt'), before)

    def test_intent_to_add_path_with_a_leading_space_is_kept(self):
        p = self.source
        (p / ' lead.txt').write_text('leading space')
        git(p, 'add', '-N', ' lead.txt')
        self.pool.suspend('attempt', {'session_id': 's', 'wait': {'target': 'card'}}, writer_stopped=True)
        paths, _ = self.pool.restore('attempt')
        self.assertEqual((Path(paths[0]) / ' lead.txt').read_text(), 'leading space')

    def _oldest(self, key, source, metadata):
        """A snapshot in the pre-exclusion format: full tree and history, no `excludes` in the manifest."""
        old = Checkpoints(self.root / 'cp-old', exclude_names=(), slim=False).create(key, source, metadata)
        manifest = old / 'manifest.json'
        data = json.loads(manifest.read_text())
        data.pop('excludes', None)
        manifest.write_text(json.dumps(data, sort_keys=True))
        return old

    def _dirty_source(self):
        p = self.source
        (p / 'README').write_bytes(b'staged\0binary')
        git(p, 'add', 'README')
        (p / 'README').write_bytes(b'unstaged\0binary')
        (p / 'untracked.txt').write_bytes(b'kept\0bytes')
        for cache in ('node_modules/dep', '.gocache/x', 'pkg/__pycache__', 'cache'):
            (p / cache).mkdir(parents=True)
            (p / cache / 'blob').write_bytes(b'regenerable' * 1000)
        (p / 'target').mkdir()
        (p / 'target' / 'tracked.txt').write_text('tracked inside a cache-named dir')
        git(p, 'add', '-f', 'target/tracked.txt')
        return p

    def _restore_elsewhere(self, cp, checkpoint):
        target = self.root / 'restore-target'
        target.mkdir()
        git(target, 'init', '--quiet')
        cp.restore(checkpoint, target)
        return target

    def test_regenerable_dirs_are_left_out_and_restore_verifies(self):
        p = self._dirty_source()
        cp = Checkpoints(self.root / 'cp')
        checkpoint = cp.create('k1', p, {})
        tree = checkpoint / 'tree'
        for gone in ('node_modules', '.gocache', 'pkg/__pycache__'):
            self.assertFalse((tree / gone).exists(), gone)
        self.assertFalse((tree / 'cache').exists())  # git-ignored: regenerable, not snapshotted
        self.assertTrue((tree / 'target' / 'tracked.txt').is_file())  # staged, so not what HEAD gives back
        data = cp.read(checkpoint)
        self.assertEqual(sorted(data['excludes']), ['.gocache', 'node_modules', 'pkg/__pycache__'])
        target = self._restore_elsewhere(cp, checkpoint)
        self.assertEqual((target / 'untracked.txt').read_bytes(), b'kept\0bytes')
        self.assertEqual((target / 'README').read_bytes(), b'unstaged\0binary')
        self.assertEqual(git(target, 'diff', '--cached', '--stat'), git(p, 'diff', '--cached', '--stat'))
        self.assertEqual(git(target, 'rev-parse', 'HEAD'), git(p, 'rev-parse', 'HEAD'))
        self.assertEqual(git(target, 'symbolic-ref', 'HEAD', check=False), git(p, 'symbolic-ref', 'HEAD', check=False))
        self.assertFalse((target / 'node_modules').exists())

    def test_var_and_nested_node_modules_are_left_out_and_restore_verifies(self):
        p = self.source
        (p / 'untracked.txt').write_bytes(b'kept')
        for cache in ('var/log', 'web/node_modules/dep', 'web/node_modules/dep/node_modules/x'):
            (p / cache).mkdir(parents=True, exist_ok=True)
            (p / cache / 'blob').write_bytes(b'regenerable' * 1000)
        (p / 'src' / 'var').mkdir(parents=True)
        (p / 'src' / 'var' / 'kept.py').write_text('tracked inside a var-named dir')
        git(p, 'add', '-f', 'src/var/kept.py')
        cp = Checkpoints(self.root / 'cp')
        checkpoint = cp.create('kv', p, {})
        tree = checkpoint / 'tree'
        self.assertFalse((tree / 'var').exists())
        self.assertFalse((tree / 'web' / 'node_modules').exists())
        self.assertTrue((tree / 'src' / 'var' / 'kept.py').is_file())  # tracked content is never dropped
        self.assertEqual(sorted(cp.read(checkpoint)['excludes']), ['var', 'web/node_modules'])
        target = self._restore_elsewhere(cp, checkpoint)
        self.assertEqual((target / 'untracked.txt').read_bytes(), b'kept')
        self.assertFalse((target / 'var').exists())

    def test_excluded_dir_content_is_not_a_snapshot_mismatch_but_other_content_is(self):
        p = self._dirty_source()
        cp = Checkpoints(self.root / 'cp')
        checkpoint = cp.create('k2', p, {})
        (checkpoint / 'tree' / 'untracked.txt').write_bytes(b'tampered')
        with self.assertRaises(WorkspaceError):  # the exclusion must not blind the hash check
            cp.verify(checkpoint)

    def test_old_format_snapshot_with_caches_still_restores(self):
        p = self._dirty_source()
        cp = Checkpoints(self.root / 'cp')
        old = self._oldest('k3', p, {})  # the pre-change format
        self.assertNotIn('excludes', cp.read(old))
        self.assertTrue((old / 'tree' / 'node_modules' / 'dep' / 'blob').is_file())
        target = self._restore_elsewhere(cp, old)
        self.assertEqual((target / 'node_modules' / 'dep' / 'blob').read_bytes(), b'regenerable' * 1000)
        self.assertEqual((target / 'untracked.txt').read_bytes(), b'kept\0bytes')

    def test_compact_rewrites_old_snapshot_in_place_and_restores(self):
        p = self._dirty_source()
        cp = Checkpoints(self.root / 'cp')
        old = self._oldest('k4', p, {'repo': 'app'})
        cp.root.rmdir() if not any(cp.root.iterdir()) else None
        cpo = Checkpoints(self.root / 'cp-old')
        self.assertTrue(cpo.is_old_format(old))
        res = cpo.compact(old)
        self.assertTrue(res['compacted'])
        self.assertLess(res['after'], res['before'])
        self.assertFalse(cpo.is_old_format(old))
        self.assertEqual(cpo.read(old)['metadata'], {'repo': 'app'})
        self.assertEqual([x.name for x in cpo.root.iterdir()], ['k4'])
        self.assertFalse((old / 'tree' / 'node_modules').exists())
        target = self._restore_elsewhere(cpo, old)
        self.assertEqual((target / 'untracked.txt').read_bytes(), b'kept\0bytes')
        self.assertFalse(cpo.compact(old)['compacted'])

    def test_compact_failure_leaves_old_snapshot_untouched(self):
        p = self._dirty_source()
        cpo = Checkpoints(self.root / 'cp-old')
        old = self._oldest('k5', p, {})
        with patch.object(Checkpoints, '_verify', side_effect=WorkspaceError('boom')):
            with self.assertRaises(WorkspaceError):
                cpo.compact(old)
        self.assertTrue((old / 'tree' / 'node_modules' / 'dep' / 'blob').is_file())
        self.assertEqual([x.name for x in cpo.root.iterdir()], ['k5'])
        cpo.verify(old)

    def test_compact_failed_swap_puts_the_old_snapshot_back(self):
        p = self._dirty_source()
        cpo = Checkpoints(self.root / 'cp-old')
        old = self._oldest('k6', p, {})
        real, calls = os.rename, []

        def flaky(a, b):
            if str(b) == str(old) and not calls:      # the install; the rollback rename must pass
                calls.append(1)
                raise OSError('swap failed')
            return real(a, b)
        with patch('mesh_runner.checkpoint.os.rename', side_effect=flaky):
            with self.assertRaises(OSError):
                cpo.compact(old)
        self.assertTrue((old / 'tree' / 'node_modules' / 'dep' / 'blob').is_file())
        self.assertEqual([x.name for x in cpo.root.iterdir()], ['k6'])
        cpo.verify(old)

    def test_crash_between_swap_renames_is_recovered_by_read_and_restore(self):
        p = self._dirty_source()
        cpo = Checkpoints(self.root / 'cp-old')
        old = self._oldest('k7', p, {})
        os.rename(old, old.parent / '.retired-k7')        # the state a crash leaves
        self.assertEqual(cpo.read(old)['version'], 1)
        self.assertTrue(old.is_dir())
        target = self._restore_elsewhere(cpo, old)
        self.assertEqual((target / 'untracked.txt').read_bytes(), b'kept\0bytes')

    def test_compact_reports_submodule_snapshots_as_skipped(self):
        p = self._dirty_source()
        cpo = Checkpoints(self.root / 'cp-old')
        old = self._oldest('k8', p, {})
        data = cpo.read(old)
        data['submodules'] = [{'path': 'x'}]
        with patch.object(Checkpoints, 'read', return_value=data):
            self.assertEqual(cpo.compact(old), {'compacted': False, 'skipped': 'submodules'})
        self.assertTrue((old / 'tree' / 'node_modules' / 'dep' / 'blob').is_file())

    def test_config_exclude_names_replace_the_default(self):
        p = self._dirty_source()
        checkpoint = Checkpoints(self.root / 'cp', exclude_names=('cache',)).create('k4', p, {})
        self.assertFalse((checkpoint / 'tree' / 'cache').exists())
        self.assertTrue((checkpoint / 'tree' / 'node_modules' / 'dep' / 'blob').is_file())

    def test_exclude_names_config_default_matches_and_rejects_paths(self):
        self.assertEqual(_exclude_names(None), DEFAULT_EXCLUDE_NAMES)
        self.assertEqual(_exclude_names(['a']), ('a',))
        for bad in ('node_modules', ['a/b'], [''], ['..'], ['.git'], [1]):
            with self.assertRaises(ConfigError):
                _exclude_names(bad)

    def test_restore_keeps_the_current_branch_in_another_slot(self):
        p = self.source
        git(p, 'checkout', '--quiet', '-B', 'review-card')
        (p / 'README').write_text('work on the branch')
        expected = fingerprint(p)
        self.pool.suspend('attempt', {'session_id': 'old', 'wait': {'kind': 'card'}}, writer_stopped=True)
        blocker = self.pool.lease('other', 'app')
        git(blocker, 'checkout', '--quiet', '-B', 'unrelated')
        restarted = WorkspacePool(Store(self.root / 's.sqlite'), self.root / 'pool',
                                  [RepoSpec('app', self.base, max_slots=2)], owner='agent')
        paths, _ = restarted.restore('attempt')
        self.assertNotEqual(Path(paths[0]), blocker)
        self.assertEqual(fingerprint(Path(paths[0])), expected)
        self.assertEqual(git(Path(paths[0]), 'symbolic-ref', '--short', 'HEAD'), 'review-card')

    def test_restore_of_a_detached_head_stays_detached(self):
        p = self.source
        git(p, 'checkout', '--quiet', '--detach')
        expected = fingerprint(p)
        self.pool.suspend('attempt', {'session_id': 'old'}, writer_stopped=True)
        paths, _ = self.pool.restore('attempt')
        self.assertEqual(fingerprint(Path(paths[0])), expected)
        self.assertEqual(git(Path(paths[0]), 'branch', '--show-current'), '')

    def test_live_writer_refused(self):
        with self.assertRaises(WorkspaceError):
            self.pool.suspend('attempt', {})
        self.assertEqual(self.pool.leased_to('attempt'), [str(self.source)])

    def test_low_disk_and_bad_replay_retain_source(self):
        (self.source / 'README').write_text('valuable')
        expected = fingerprint(self.source)
        with patch('mesh_runner.checkpoint.shutil.disk_usage', return_value=type('D', (), {'free': 0})()):
            with self.assertRaisesRegex(WorkspaceError, 'disk budget'):
                self.pool.suspend('attempt', {}, writer_stopped=True)
        self.assertEqual(fingerprint(self.source), expected)
        with patch.object(Checkpoints, 'verify', side_effect=WorkspaceError('bad replay')):
            with self.assertRaises(WorkspaceError):
                self.pool.suspend('attempt', {}, writer_stopped=True)
        self.assertEqual(fingerprint(self.source), expected)
        self.assertEqual(self.pool.rows()[0]['state'], 'leased')

    def test_new_capture_reserves_floor_and_its_peak_before_source_mutation(self):
        self.pool.min_free_gb = 6
        before = fingerprint(self.source)
        for free in (5 * 1024**3, 6 * 1024**3 + 1):
            with self.subTest(free=free), patch('mesh_runner.checkpoint.shutil.disk_usage',
                    return_value=type('D', (), {'free': free})()):
                with self.assertRaisesRegex(WorkspaceError, 'disk budget'):
                    self.pool.suspend('attempt', {}, writer_stopped=True)
                self.assertEqual(fingerprint(self.source), before)
                self.assertEqual(self.pool.leased_to('attempt'), [str(self.source)])
                self.assertEqual(self.pool._checkpoint_rows('attempt'), [])
                self.assertEqual(list(self.pool._checkpoints().root.iterdir()), [])

    def test_affordable_capture_rechecks_free_space_for_the_next_wait(self):
        # One small WAIT can release now; another must stay leased after other writers
        # consume its headroom. No blanket forecast for every WAIT is necessary.
        self.pool.min_free_gb = 6
        with patch('mesh_runner.checkpoint.shutil.disk_usage',
                   return_value=type('D', (), {'free': 7 * 1024**3})()):
            self.pool.suspend('attempt', {}, writer_stopped=True)
        first = self.pool._checkpoint_rows('attempt')[0]
        other = self.pool.lease('other', 'app')
        before = fingerprint(other)
        with patch('mesh_runner.checkpoint.shutil.disk_usage',
                   return_value=type('D', (), {'free': 6 * 1024**3 + 1})()):
            with self.assertRaisesRegex(WorkspaceError, 'disk budget'):
                self.pool.suspend('other', {}, writer_stopped=True)
        self.assertEqual(fingerprint(other), before)
        self.assertEqual(self.pool.leased_to('other'), [str(other)])
        self.assertEqual(self.pool._checkpoint_rows('attempt')[0], first)

    def test_verified_release_recovery_keeps_technical_budget_below_floor(self):
        cp = self.pool._checkpoints().create('already-verified', self.source, {})
        self.pool.min_free_gb = 6
        for phase in ('verified', 'releasing'):
            with self.subTest(phase=phase):
                self.store.db.execute('DELETE FROM workspace_checkpoints')
                self.store.db.execute('INSERT INTO workspace_checkpoints VALUES(?,?,?,?,?,?,?)',
                    ('agent', 'attempt', 'app', str(cp), str(self.source), phase, ''))
                self.store.db.execute("UPDATE slots SET state='leased',attempt='attempt'")
                with patch('mesh_runner.checkpoint.shutil.disk_usage',
                           return_value=type('D', (), {'free': 5 * 1024**3})()), \
                     patch.object(Checkpoints, 'create', side_effect=AssertionError('no recapture')):
                    self.pool.suspend('attempt', {}, writer_stopped=True)
                self.assertEqual(self.pool._checkpoint_rows('attempt')[0]['phase'], 'released')
                self.assertEqual(self.pool.leased_to('attempt'), [])

    def test_capture_floor_off_preserves_existing_technical_reserve(self):
        with patch('mesh_runner.checkpoint.shutil.disk_usage',
                   return_value=type('D', (), {'free': 1024**3})()):
            self.pool.suspend('attempt', {}, writer_stopped=True)
        self.assertEqual(self.pool.leased_to('attempt'), [])

    def test_external_space_loss_before_replay_removes_pending_only(self):
        self.pool.min_free_gb = 6
        (self.source / 'README').write_text('valuable edits')
        before = fingerprint(self.source)
        readings = [type('D', (), {'free': 7 * 1024**3})(),
                    type('D', (), {'free': 6 * 1024**3 + 1})()]
        with patch('mesh_runner.checkpoint.shutil.disk_usage', side_effect=readings):
            with self.assertRaisesRegex(WorkspaceError, 'disk budget'):
                self.pool.suspend('attempt', {}, writer_stopped=True)
        self.assertEqual(fingerprint(self.source), before)
        self.assertEqual(self.pool.leased_to('attempt'), [str(self.source)])
        self.assertEqual(self.pool._checkpoint_rows('attempt'), [])
        self.assertEqual(list(self.pool._checkpoints().root.iterdir()), [])

    def test_unknown_capture_space_fails_closed(self):
        before = fingerprint(self.source)
        with patch('mesh_runner.checkpoint.shutil.disk_usage', side_effect=OSError('unknown')):
            with self.assertRaisesRegex(WorkspaceError, 'disk availability unknown'):
                self.pool.suspend('attempt', {}, writer_stopped=True)
        self.assertEqual(fingerprint(self.source), before)
        self.assertTrue(self.pool.leased_to('attempt'))

    def test_checkpoint_allocation_serializes_across_owners_and_replay(self):
        from concurrent.futures import ThreadPoolExecutor
        first = Checkpoints(self.root / 'owner-one')
        second = Checkpoints(self.root / 'owner-two')
        entered, release, calling, replayed = (threading.Event() for _ in range(4))
        def capture(*args, **kwargs):
            entered.set()
            if not release.wait(10):
                raise AssertionError('test did not release capture')
        def replay(*args):
            replayed.set()
        def run_replay():
            calling.set()
            second.verify(self.root / 'snapshot')
        with patch.object(first, '_create', capture), patch.object(second, '_verify', replay), \
             ThreadPoolExecutor(max_workers=2) as workers:
            capturing = workers.submit(first.create, 'capture', self.source, {})
            try:
                self.assertTrue(entered.wait(10))
                verifying = workers.submit(run_replay)
                self.assertTrue(calling.wait(10))
                # A blocked verifier must not allocate from capture's observed budget.
                self.assertFalse(replayed.wait(0.1))
            finally:
                release.set()
            capturing.result(timeout=10)
            verifying.result(timeout=10)
        self.assertTrue(replayed.is_set())

    def test_corrupt_checkpoint_fails_restore_and_retains_lease(self):
        self.pool.suspend('attempt', {}, writer_stopped=True)
        row = self.pool._checkpoint_rows('attempt')[0]
        (Path(row['checkpoint']) / 'tree' / 'README').write_text('corrupted')
        with self.assertRaisesRegex(WorkspaceError, 'hash mismatch'):
            self.pool.restore('attempt')
        self.assertEqual(len(self.pool.leased_to('attempt')), 1)
        self.assertEqual(self.pool._checkpoint_rows('attempt')[0]['phase'], 'restoring')

    def test_low_disk_restore_with_owned_lease_preserves_target_and_journal(self):
        for phase in ('released', 'restoring'):
            with self.subTest(phase=phase):
                self.pool.suspend('attempt', {}, writer_stopped=True)
                row = self.pool._checkpoint_rows('attempt')[0]
                target = str(self.source)
                # Model restart recovery: this attempt already owns the target while its
                # durable journal says replay is pending.
                self.store.db.execute(
                    "UPDATE slots SET state='leased', attempt=? WHERE path=?",
                    ('attempt', target))
                self.store.db.execute(
                    "UPDATE workspace_checkpoints SET phase=?, target=? WHERE owner=? AND attempt=?",
                    (phase, target, 'agent', 'attempt'))
                before = fingerprint(self.source)
                self.pool.min_free_gb = 6
                with patch('mesh_runner.workspace.shutil.disk_usage',
                           return_value=type('D', (), {'free': 5 * 1024**3})()):
                    with self.assertRaisesRegex(WorkspaceError, 'disk below floor'):
                        self.pool.restore('attempt')
                after = self.pool._checkpoint_rows('attempt')[0]
                self.assertEqual((after['phase'], after['target']), (phase, target))
                self.assertEqual(fingerprint(self.source), before)
                self.assertEqual(self.pool.leased_to('attempt'), [target])
                self.pool.min_free_gb = 0
                # Reset persistent fixture state for the next phase.
                self.store.db.execute("DELETE FROM workspace_checkpoints")
                self.store.db.execute("UPDATE slots SET state='free', attempt='' WHERE path=?", (target,))
                self.source = self.pool.lease('attempt', 'app')

    def test_restored_checkpoint_is_not_replayed_below_disk_floor(self):
        self.pool.suspend('attempt', {}, writer_stopped=True)
        paths, _ = self.pool.restore('attempt')
        target = Path(paths[0])
        (target / 'README').write_text('later work')
        before = fingerprint(target)
        with patch('mesh_runner.workspace.shutil.disk_usage',
                   return_value=type('D', (), {'free': 0})()):
            paths, _ = self.pool.restore('attempt')
        self.assertEqual(fingerprint(Path(paths[0])), before)
        self.assertEqual((target / 'README').read_text(), 'later work')

    def test_release_restart_after_slot_freed_does_not_touch_new_owner(self):
        self.pool.suspend('attempt', {}, writer_stopped=True)
        self.store.db.execute("UPDATE workspace_checkpoints SET phase='releasing'")
        target = self.pool.lease('new-owner', 'app')
        (target / 'README').write_text('new work')
        self.pool.suspend('attempt', {}, writer_stopped=True)
        self.assertEqual((target / 'README').read_text(), 'new work')
        self.assertEqual(self.pool._checkpoint_rows('attempt')[0]['phase'], 'released')

    def test_nested_repo_fails_closed(self):
        (self.source / 'nested').mkdir()
        git(self.source / 'nested', 'init', '--quiet')
        with self.assertRaisesRegex(WorkspaceError, 'nested'):
            self.pool.suspend('attempt', {}, writer_stopped=True)
        self.assertTrue(self.pool.full('app'))

    def add_gitlink(self, name='infra/placeholder', oid='1234567890123456789012345678901234567890'):
        git(self.source, 'update-index', '--add', '--cacheinfo', '160000', oid, name)
        return self.source / name

    def test_empty_and_absent_gitlink_round_trip_restart(self):
        empty = self.add_gitlink()
        empty.mkdir(parents=True); empty.chmod(0o700)
        self.add_gitlink('absent/placeholder')
        git(self.source, 'commit', '-qm', 'Record historical gitlink placeholders')
        # Preserve a staged gitlink OID change as well as the unpublished HEAD.
        self.add_gitlink(oid='2345678901234567890123456789012345678901')
        (self.source / 'untracked').write_text('valuable')
        expected = fingerprint(self.source)
        index_before = git(self.source, 'ls-files', '--stage')
        self.pool.suspend('attempt', {}, writer_stopped=True)
        self.assertFalse(self.pool.full('app'))
        restarted = WorkspacePool(Store(self.root / 's.sqlite'), self.root / 'pool',
                                  [RepoSpec('app', self.base, max_slots=1)], owner='agent')
        paths, _ = restarted.restore('attempt')
        target = Path(paths[0])
        self.assertEqual(fingerprint(target), expected)
        self.assertEqual(git(target, 'ls-files', '--stage'), index_before)
        self.assertEqual((target / 'infra/placeholder').stat().st_mode & 0o777, 0o700)
        self.assertFalse((target / 'absent/placeholder').exists())

    def test_gitlink_newline_path_round_trip(self):
        name = 'newline\nname/placeholder'
        path = self.add_gitlink(name); path.mkdir(parents=True)
        expected = fingerprint(self.source)
        self.pool.suspend('attempt', {}, writer_stopped=True)
        paths, _ = self.pool.restore('attempt')
        self.assertEqual(fingerprint(Path(paths[0])), expected)
        self.assertTrue((Path(paths[0]) / name).is_dir())

    def test_gitlink_nonutf8_path_fails_closed_without_decode_crash(self):
        # APFS cannot materialize this name, but Git indexes can contain it.
        self.add_gitlink(os.fsdecode(b'odd-\xff\nname/placeholder'))
        before = fingerprint(self.source)
        with self.assertRaisesRegex(WorkspaceError, 'non-UTF8'):
            self.pool.suspend('attempt', {}, writer_stopped=True)
        self.assertEqual(fingerprint(self.source), before)
        self.assertTrue(self.pool.full('app'))

    def test_local_submodule_config_with_empty_path_fails_closed(self):
        self.add_gitlink()
        git(self.source, 'config', 'submodule.placeholder.active', 'true')
        with self.assertRaisesRegex(WorkspaceError, 'metadata'):
            self.pool.suspend('attempt', {}, writer_stopped=True)
        self.assertTrue(self.pool.full('app'))

    def test_unmerged_gitlink_retains_raw_index_and_lease(self):
        oid = '1234567890123456789012345678901234567890'
        subprocess.run(['git', '-C', str(self.source), 'update-index', '--index-info'],
                       input=f'160000 {oid} 1\tconflicted\n'.encode(), check=True, capture_output=True)
        index = Path(git(self.source, 'rev-parse', '--git-path', 'index'))
        before = index.read_bytes()
        with self.assertRaisesRegex(WorkspaceError, 'unmerged'):
            self.pool.suspend('attempt', {}, writer_stopped=True)
        self.assertEqual(index.read_bytes(), before)
        self.assertTrue(self.pool.full('app'))

    def test_gitlink_populated_during_verification_retains_source_and_lease(self):
        path = self.add_gitlink(); path.mkdir(parents=True)
        verify = Checkpoints.verify
        def mutate_after_verify(checkpoints, checkpoint):
            verify(checkpoints, checkpoint)
            (path / 'late-untracked').write_text('valuable late data')
        with patch.object(Checkpoints, 'verify', mutate_after_verify):
            with self.assertRaisesRegex(WorkspaceError, 'populated submodule'):
                self.pool.suspend('attempt', {}, writer_stopped=True)
        self.assertEqual((path / 'late-untracked').read_text(), 'valuable late data')
        self.assertTrue(self.pool.full('app'))
        self.assertEqual(self.pool._checkpoint_rows('attempt'), [])
        self.assertEqual(list(self.pool._checkpoints().root.iterdir()), [])

    def test_effective_submodule_config_fails_closed(self):
        self.add_gitlink()
        with patch.dict(os.environ, {'GIT_CONFIG_COUNT':'1',
                'GIT_CONFIG_KEY_0':'submodule.placeholder.active', 'GIT_CONFIG_VALUE_0':'true'}):
            with self.assertRaisesRegex(WorkspaceError, 'metadata'):
                self.pool.suspend('attempt', {}, writer_stopped=True)
        self.assertTrue(self.pool.full('app'))

    def test_staged_gitlink_deletion_or_replacement_fails_closed(self):
        path = self.add_gitlink()
        git(self.source, 'commit', '-qm', 'Record placeholder')
        git(self.source, 'update-index', '--force-remove', 'infra/placeholder')
        for replacement in (False, True):
            if replacement:
                path.parent.mkdir(exist_ok=True)
                path.write_text('replacement data')
                git(self.source, 'add', 'infra/placeholder')
            with self.assertRaisesRegex(WorkspaceError, 'removed or replaced'):
                self.pool.suspend('attempt', {}, writer_stopped=True)
            self.assertTrue(self.pool.full('app'))
        self.assertEqual(path.read_text(), 'replacement data')

    def test_populated_gitlink_untracked_data_refuses_and_retains_lease(self):
        path = self.add_gitlink(); path.mkdir(parents=True)
        (path / 'untracked-private').write_text('valuable')
        before = fingerprint(self.source)
        with self.assertRaisesRegex(WorkspaceError, 'populated submodule'):
            self.pool.suspend('attempt', {}, writer_stopped=True)
        self.assertEqual(fingerprint(self.source), before)
        self.assertTrue(self.pool.full('app'))

    def add_submodule(self, name='materialized'):
        (self.root / f'ext-{name}').mkdir()
        external = make_repo(self.root / f'ext-{name}')
        git(self.source, '-c', 'protocol.file.allow=always', 'submodule', 'add', str(external), name)
        git(self.source, 'commit', '-qm', f'Add {name}')
        return self.source / name

    def restart(self):
        return WorkspacePool(Store(self.root / 's.sqlite'), self.root / 'pool',
                             [RepoSpec('app', self.base, max_slots=1)], owner='agent')

    def test_populated_submodule_round_trip_in_another_slot_after_reopen(self):
        sub = self.add_submodule()
        (sub / 'README').write_text('staged in submodule'); git(sub, 'add', 'README')
        (sub / 'README').write_text('unstaged in submodule')
        (sub / 'untracked').write_text('untracked data')
        (sub / '.gitignore').write_text('cache/\n'); git(sub, 'add', '.gitignore')
        (sub / 'cache').mkdir(); (sub / 'cache' / 'ignored').write_text('ignored data')
        git(sub, '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qm', 'unpublished local commit')
        (sub / 'later').write_text('after the commit')
        plan = submodule_plan(self.source)
        self.assertEqual([(e['path'], e['populated'], e['gitdir']) for e in plan], [('materialized', True, True)])
        expected = fingerprint(self.source, plan)
        base_modules = sorted(p.name for p in (self.base / '.git' / 'modules').glob('*')) \
            if (self.base / '.git' / 'modules').exists() else []
        self.pool.suspend('attempt', {'session_id': 'old', 'wait': {'target': 'card'}}, writer_stopped=True)
        self.pool.lease('other', 'app'); self.pool.release('other')
        paths, manifests = self.restart().restore('attempt')
        restored = Path(paths[0])
        self.assertEqual(fingerprint(restored, plan), expected)
        self.assertEqual(manifests[0]['version'], 2)
        self.assertEqual((restored / 'materialized' / 'untracked').read_text(), 'untracked data')
        self.assertEqual((restored / 'materialized' / 'cache' / 'ignored').read_text(), 'ignored data')
        self.assertEqual((restored / 'materialized' / 'README').read_text(), 'unstaged in submodule')
        self.assertIn('unpublished local commit', git(restored / 'materialized', 'log', '-1', '--format=%s'))
        self.assertTrue((restored / 'materialized' / '.git').read_text().startswith('gitdir: '))
        # The shared base clone gained no submodule gitdir.
        self.assertEqual(sorted(p.name for p in (self.base / '.git' / 'modules').glob('*'))
                         if (self.base / '.git' / 'modules').exists() else [], base_modules)

    def test_checkpoint_holds_no_submodule_config_values(self):
        self.add_submodule()
        git(self.source, 'config', 'submodule.materialized.url', 'https://user:s3cret-token@example.invalid/x.git')
        self.pool.suspend('attempt', {}, writer_stopped=True)
        manifests = list(self.pool._checkpoints().root.rglob('manifest.json'))
        self.assertTrue(manifests)
        for p in manifests:
            self.assertNotIn('s3cret-token', p.read_text())

    def test_empty_configured_submodules_round_trip(self):
        self.add_submodule('initialized-then-emptied')
        git(self.source, 'submodule', 'deinit', '--force', 'initialized-then-emptied')
        self.assertEqual(list((self.source / 'initialized-then-emptied').iterdir()), [])
        # A never initialized one: only .gitmodules and the gitlink.
        git(self.source, 'update-index', '--add', '--cacheinfo', '160000',
            '1234567890123456789012345678901234567890', 'never')
        (self.source / '.gitmodules').write_text((self.source / '.gitmodules').read_text()
                                                  + '[submodule "never"]\n\tpath = never\n\turl = ../never\n')
        git(self.source, 'add', '.gitmodules')
        plan = submodule_plan(self.source)
        self.assertEqual([(e['path'], e['populated'], e['gitdir']) for e in plan],
                         [('initialized-then-emptied', False, True), ('never', False, False)])
        expected = fingerprint(self.source, plan)
        self.pool.suspend('attempt', {}, writer_stopped=True)
        self.pool.lease('other', 'app'); self.pool.release('other')
        paths, _ = self.restart().restore('attempt')
        restored = Path(paths[0])
        self.assertEqual(fingerprint(restored, plan), expected)
        self.assertEqual(git(restored, 'ls-files', '--stage', '--', 'never').split()[0], '160000')
        self.assertTrue((_modules(restored) / 'initialized-then-emptied' / 'HEAD').is_file())

    def test_restore_twice_into_one_target_is_idempotent_and_refuses_new_commits(self):
        self.add_submodule()
        plan = submodule_plan(self.source)
        self.pool.suspend('attempt', {}, writer_stopped=True)
        checkpoints = self.pool._checkpoints()
        cp = next(p for p in checkpoints.root.iterdir() if (p / 'manifest.json').exists())
        target = self.root / 'target'; target.mkdir(); git(target, 'init', '--quiet')
        checkpoints.restore(cp, target)
        want = fingerprint(target, plan)
        checkpoints.restore(cp, target)
        self.assertEqual(fingerprint(target, plan), want)
        git(target / 'materialized', '-c', 'user.name=t', '-c', 'user.email=t@t',
            'commit', '--allow-empty', '-qm', 'new work')
        with self.assertRaisesRegex(WorkspaceError, 'submodule gitdir differs'):
            checkpoints.restore(cp, target)
        self.assertIn('new work', git(target / 'materialized', 'log', '-1', '--format=%s'))

    def _suspended_submodule(self):
        self.add_submodule()
        self.pool.suspend('attempt', {}, writer_stopped=True)
        checkpoints = self.pool._checkpoints()
        cp = next(p for p in checkpoints.root.iterdir() if (p / 'manifest.json').exists())
        target = self.root / 'target'; target.mkdir(); git(target, 'init', '--quiet')
        (target / 'sentinel').write_text('keep me')
        return checkpoints, cp, target

    def test_corrupt_submodule_bundle_refuses_before_touching_target(self):
        checkpoints, cp, target = self._suspended_submodule()
        (cp / 'submodules' / '0' / 'commits.bundle').write_bytes(b'garbage')
        with self.assertRaisesRegex(WorkspaceError, 'bundle invalid'):
            checkpoints.restore(cp, target)
        self.assertEqual((target / 'sentinel').read_text(), 'keep me')

    def test_truncated_submodule_bundle_refuses_before_touching_target(self):
        checkpoints, cp, target = self._suspended_submodule()
        bundle = cp / 'submodules' / '0' / 'commits.bundle'
        bundle.write_bytes(bundle.read_bytes()[:-10])
        with self.assertRaisesRegex(WorkspaceError, 'bundle invalid'):
            checkpoints.restore(cp, target)
        self.assertEqual((target / 'sentinel').read_text(), 'keep me')

    def test_restore_refuses_newer_same_head_submodule_work(self):
        checkpoints, cp, target = self._suspended_submodule()
        checkpoints.restore(cp, target)
        (target / 'materialized' / 'newer').write_text('later untracked work')
        with self.assertRaisesRegex(WorkspaceError, 'work in target differs'):
            checkpoints.restore(cp, target)
        self.assertEqual((target / 'materialized' / 'newer').read_text(), 'later untracked work')
        (target / 'materialized' / 'newer').unlink()
        (target / 'materialized' / 'README').write_text('later staged work')
        git(target / 'materialized', 'add', 'README')
        with self.assertRaisesRegex(WorkspaceError, 'work in target differs'):
            checkpoints.restore(cp, target)
        self.assertEqual((target / 'materialized' / 'README').read_text(), 'later staged work')

    def test_truncated_parent_bundle_refuses_before_touching_target(self):
        checkpoints, cp, target = self._suspended_submodule()
        bundle = cp / 'commits.bundle'
        bundle.write_bytes(bundle.read_bytes()[:-10])
        refs_before = git(target, 'for-each-ref')
        with self.assertRaisesRegex(WorkspaceError, 'bundle invalid'):
            checkpoints.restore(cp, target)
        self.assertEqual((target / 'sentinel').read_text(), 'keep me')
        self.assertEqual(git(target, 'for-each-ref'), refs_before)

    def _external(self, name):
        (self.root / f'ext-{name}').mkdir()
        repo = make_repo(self.root / f'ext-{name}')
        # make_repo commits are byte-identical across calls within one second; make this one unique.
        (repo / 'identity').write_text(name + '\n')
        git(repo, 'add', 'identity')
        git(repo, 'commit', '-qm', f'identity of {name}')
        return repo

    def _stale_target(self, name):
        """A pooled slot left over from an earlier card: its own, clean, initialised submodule."""
        target = self.root / 'stale-slot'; target.mkdir(); git(target, 'init', '--quiet')
        git(target, '-c', 'protocol.file.allow=always', 'submodule', 'add', str(self._external('stale-' + name)), name)
        git(target, '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qm', 'old card')
        (target / 'sentinel').write_text('keep me')
        return target

    def _suspended_uninitialised_submodule(self):
        """The source never initialised its configured submodule: no gitdir was captured."""
        git(self.source, 'update-index', '--add', '--cacheinfo', '160000',
            '1234567890123456789012345678901234567890', 'never')
        (self.source / '.gitmodules').write_text('[submodule "never"]\n\tpath = never\n\turl = ../never\n')
        git(self.source, 'add', '.gitmodules')
        plan = submodule_plan(self.source)
        self.assertEqual([(e['path'], e['populated'], e['gitdir']) for e in plan], [('never', False, False)])
        expected = fingerprint(self.source, plan)
        self.pool.suspend('attempt', {}, writer_stopped=True)
        checkpoints = self.pool._checkpoints()
        cp = next(p for p in checkpoints.root.iterdir() if (p / 'manifest.json').exists())
        return checkpoints, cp, plan, expected

    def test_restore_into_slot_with_stale_submodule_gitdir_when_checkpoint_has_none(self):
        checkpoints, cp, plan, expected = self._suspended_uninitialised_submodule()
        target = self._stale_target('never')
        stale_head = _gitdir(_modules(target) / 'never', 'rev-parse', 'HEAD')
        checkpoints.restore(cp, target)
        self.assertEqual(fingerprint(target, plan), expected)
        self.assertFalse(os.path.lexists(_modules(target) / 'never'))
        self.assertEqual(git(target, 'ls-files', '--stage', '--', 'never').split()[0], '160000')
        # Nothing is deleted: the old gitdir is moved aside, outside the modules directory.
        kept = list((_modules(target).parent / 'runner-stale-submodules').glob('*-never'))
        self.assertEqual(len(kept), 1)
        self.assertEqual(_gitdir(kept[0], 'rev-parse', 'HEAD'), stale_head)
        # The slot is a valid capture source again.
        self.assertEqual(fingerprint(target, submodule_plan(target)), expected)

    def test_restore_replaces_stale_submodule_gitdir_with_the_checkpointed_one(self):
        sub = self.add_submodule()
        (sub / 'README').write_text('staged in submodule'); git(sub, 'add', 'README')
        (sub / 'untracked').write_text('untracked data')
        plan = submodule_plan(self.source)
        expected = fingerprint(self.source, plan)
        self.pool.suspend('attempt', {}, writer_stopped=True)
        checkpoints = self.pool._checkpoints()
        cp = next(p for p in checkpoints.root.iterdir() if (p / 'manifest.json').exists())
        target = self._stale_target('materialized')
        stale_head = _gitdir(_modules(target) / 'materialized', 'rev-parse', 'HEAD')
        self.assertNotEqual(stale_head, expected['submodules']['materialized']['head'])
        checkpoints.restore(cp, target)
        self.assertEqual(fingerprint(target, plan), expected)
        self.assertEqual((target / 'materialized' / 'untracked').read_text(), 'untracked data')
        self.assertTrue((target / 'materialized' / '.git').read_text().startswith('gitdir: '))
        self.assertEqual(git(target / 'materialized', 'diff', '--cached', '--name-only'), 'README')
        kept = list((_modules(target).parent / 'runner-stale-submodules').glob('*-materialized'))
        self.assertEqual([_gitdir(k, 'rev-parse', 'HEAD') for k in kept], [stale_head])

    def test_restore_into_a_deeper_path_relocates_submodule_pointers(self):
        checkpoints, cp, target = self._suspended_submodule()
        deep = self.root / 'a' / 'b' / 'c' / 'slot-9'; deep.mkdir(parents=True); git(deep, 'init', '--quiet')
        checkpoints.restore(cp, deep)
        sub = deep / 'materialized'
        pointer = (sub / '.git').read_text().strip()
        self.assertTrue(pointer.startswith('gitdir: ') and not os.path.isabs(pointer[len('gitdir: '):]))
        self.assertEqual(os.path.realpath(sub / pointer[len('gitdir: '):]),
                         os.path.realpath(_modules(deep) / 'materialized'))
        self.assertEqual(os.path.realpath(git(sub, 'rev-parse', '--show-toplevel')), os.path.realpath(sub))
        self.assertEqual(git(sub, 'status', '--porcelain'), '')

    def _standalone_clone_over(self, target, name):
        """The shape seen in a real pooled slot: an orphan modules gitdir plus a standalone clone in the worktree."""
        remote = git(target / name, 'remote', 'get-url', 'origin')
        shutil.rmtree(target / name)
        subprocess.run(['git', 'clone', '-q', remote, str(target / name)], check=True, capture_output=True)

    def test_stale_slot_with_standalone_clone_in_submodule_worktree_is_restored(self):
        checkpoints, cp, plan, expected = self._suspended_uninitialised_submodule()
        target = self._stale_target('never')
        self._standalone_clone_over(target, 'never')
        self.assertTrue((target / 'never' / '.git').is_dir())
        checkpoints.restore(cp, target)
        self.assertEqual(fingerprint(target, plan), expected)
        self.assertFalse(os.path.lexists(_modules(target) / 'never'))

    def test_stale_slot_with_unpublished_commit_in_standalone_clone_is_refused(self):
        checkpoints, cp, plan, expected = self._suspended_uninitialised_submodule()
        target = self._stale_target('never')
        self._standalone_clone_over(target, 'never')
        git(target / 'never', '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '--allow-empty', '-qm', 'unpublished')
        with self.assertRaisesRegex(WorkspaceError, 'submodule gitdir differs'):
            checkpoints.restore(cp, target)
        self.assertIn('unpublished', git(target / 'never', 'log', '-1', '--format=%s'))
        self.assertTrue(os.path.lexists(_modules(target) / 'never'))

    def _nested_external(self, name):
        """A repository whose own commit records an initialisable submodule."""
        inner = self._external('inner-' + name)
        outer = self._external(name)
        git(outer, '-c', 'protocol.file.allow=always', 'submodule', 'add', str(inner), 'inner')
        git(outer, 'commit', '-qm', 'add inner')
        return outer

    def _stale_nested_target(self, name):
        target = self.root / 'stale-slot'; target.mkdir(); git(target, 'init', '--quiet')
        git(target, '-c', 'protocol.file.allow=always', 'submodule', 'add', str(self._nested_external('o-' + name)), name)
        git(target, '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qm', 'old card')
        git(target / name, '-c', 'protocol.file.allow=always', 'submodule', 'update', '--init')
        return target

    def test_stale_submodule_with_nested_unpublished_commit_is_refused(self):
        checkpoints, cp, plan, expected = self._suspended_uninitialised_submodule()
        target = self._stale_nested_target('never')
        inner = target / 'never' / 'inner'
        git(inner, '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '--allow-empty', '-qm', 'nested unpublished')
        with self.assertRaisesRegex(WorkspaceError, 'submodule gitdir differs.*nested'):
            checkpoints.restore(cp, target)
        self.assertIn('nested unpublished', git(inner, 'log', '-1', '--format=%s'))
        self.assertTrue(os.path.lexists(_modules(target) / 'never'))

    def test_stale_submodule_with_clean_nested_submodule_is_refused_too(self):
        # Nested levels are not inspected, so even a clean one that is on its remote is kept.
        checkpoints, cp, plan, expected = self._suspended_uninitialised_submodule()
        target = self._stale_nested_target('never')
        with self.assertRaisesRegex(WorkspaceError, 'submodule gitdir differs.*nested'):
            checkpoints.restore(cp, target)
        self.assertTrue((target / 'never' / 'inner' / 'README').is_file())

    def test_stale_standalone_clone_with_nested_repository_is_refused(self):
        checkpoints, cp, plan, expected = self._suspended_uninitialised_submodule()
        target = self._stale_target('never')
        shutil.rmtree(target / 'never')
        nested = self._nested_external('o-clone')
        subprocess.run(['git', '-c', 'protocol.file.allow=always', 'clone', '-q', '--recurse-submodules',
                        str(nested), str(target / 'never')], check=True, capture_output=True)
        git(target / 'never' / 'inner', '-c', 'user.name=t', '-c', 'user.email=t@t',
            'commit', '--allow-empty', '-qm', 'nested unpublished')
        with self.assertRaisesRegex(WorkspaceError, 'submodule gitdir differs.*nested'):
            checkpoints.restore(cp, target)
        self.assertIn('nested unpublished', git(target / 'never' / 'inner', 'log', '-1', '--format=%s'))

    def test_stale_submodule_ignored_files_are_set_aside_not_deleted(self):
        checkpoints, cp, plan, expected = self._suspended_uninitialised_submodule()
        target = self._stale_target('never')
        gd = _modules(target) / 'never'
        (gd / 'info').mkdir(exist_ok=True)
        with open(gd / 'info' / 'exclude', 'a') as out:
            out.write('local.env\n')
        (target / 'never' / 'local.env').write_text('ignored secret-ish data')
        (target / 'never' / 'cache').mkdir()
        (target / 'never' / 'cache' / 'blob').write_text('ignored cache data')
        self.assertEqual(git(target / 'never', 'status', '--porcelain', '--ignored').count('!!'), 2)
        checkpoints.restore(cp, target)
        self.assertEqual(fingerprint(target, plan), expected)
        self.assertFalse((target / 'never').exists() and any((target / 'never').iterdir()))
        parked = list((_modules(target).parent / 'runner-stale-submodules').glob('*-never.worktree'))
        self.assertEqual(len(parked), 1)
        self.assertEqual((parked[0] / 'local.env').read_text(), 'ignored secret-ish data')
        self.assertEqual((parked[0] / 'cache' / 'blob').read_text(), 'ignored cache data')
        self.assertTrue((parked[0] / 'README').is_file())

    def test_parked_worktree_and_gitdir_stay_connected(self):
        checkpoints, cp, plan, expected = self._suspended_uninitialised_submodule()
        target = self._stale_target('never')
        gd = _modules(target) / 'never'
        stale_head = _gitdir(gd, 'rev-parse', 'HEAD')
        checkpoints.restore(cp, target)
        park = _modules(target).parent / 'runner-stale-submodules'
        parked = next(park.glob('*-never.worktree'))
        parked_gd = next(p for p in park.glob('*-never') if p.is_dir())
        self.assertEqual(git(parked, 'rev-parse', 'HEAD'), stale_head)
        self.assertEqual(os.path.realpath(git(parked, 'rev-parse', '--show-toplevel')), os.path.realpath(parked))
        self.assertEqual(os.path.realpath(git(parked, 'rev-parse', '--absolute-git-dir')), os.path.realpath(parked_gd))
        self.assertEqual(git(parked, 'status', '--porcelain'), '')
        self.assertTrue((parked / 'README').is_file())

    def test_set_aside_path_that_is_a_symlink_or_a_file_refuses_and_moves_nothing(self):
        checkpoints, cp, plan, expected = self._suspended_uninitialised_submodule()
        target = self._stale_target('never')
        park = _modules(target).parent / 'runner-stale-submodules'
        elsewhere = self.root / 'elsewhere'; elsewhere.mkdir()
        os.symlink(elsewhere, park)
        with self.assertRaisesRegex(WorkspaceError, 'set-aside path'):
            checkpoints.restore(cp, target)
        park.unlink(); park.write_text('not a directory')
        with self.assertRaisesRegex(WorkspaceError, 'set-aside path'):
            checkpoints.restore(cp, target)
        self.assertEqual(list(elsewhere.iterdir()), [])
        self.assertTrue((target / 'never' / 'README').is_file())
        self.assertTrue(os.path.lexists(_modules(target) / 'never'))
        self.assertEqual((target / 'sentinel').read_text(), 'keep me')

    def test_set_aside_failure_puts_the_worktree_back(self):
        checkpoints, cp, plan, expected = self._suspended_uninitialised_submodule()
        target = self._stale_target('never')
        real = os.rename
        def fail_for_gitdir(src, dst):
            if str(src).endswith(os.path.join('modules', 'never')):
                raise OSError(18, 'Invalid cross-device link')
            return real(src, dst)
        with patch('mesh_runner.checkpoint.os.rename', fail_for_gitdir):
            with self.assertRaisesRegex(WorkspaceError, 'cannot set aside'):
                checkpoints.restore(cp, target)
        self.assertTrue((target / 'never' / 'README').is_file())
        self.assertTrue(os.path.lexists(_modules(target) / 'never'))

    def test_stale_submodule_extra_branch_or_local_tag_on_a_remote_commit_is_refused(self):
        checkpoints, cp, plan, expected = self._suspended_uninitialised_submodule()
        target = self._stale_target('never')
        sub = target / 'never'
        git(sub, 'branch', 'extra')                 # on a commit the remote has
        with self.assertRaisesRegex(WorkspaceError, 'submodule gitdir differs.*local branch extra'):
            checkpoints.restore(cp, target)
        git(sub, 'branch', '-D', 'extra')
        git(sub, 'tag', 'mine')
        with self.assertRaisesRegex(WorkspaceError, 'submodule gitdir differs.*local tag mine'):
            checkpoints.restore(cp, target)
        self.assertTrue(os.path.lexists(_modules(target) / 'never'))
        self.assertEqual((target / 'sentinel').read_text(), 'keep me')

    def test_stale_submodule_branch_equal_to_a_remote_branch_and_cloned_tags_are_accepted(self):
        checkpoints, cp, plan, expected = self._suspended_uninitialised_submodule()
        target = self._stale_target('never')
        gd = _modules(target) / 'never'
        head = _gitdir(gd, 'rev-parse', 'HEAD')
        _gitdir(gd, 'update-ref', 'refs/heads/feature/x', head)
        _gitdir(gd, 'update-ref', 'refs/remotes/origin/feature/x', head)
        # Tags that came with the clone are recorded in packed-refs.
        _gitdir(gd, 'update-ref', 'refs/tags/v1', head)
        _gitdir(gd, 'pack-refs', '--all')
        checkpoints.restore(cp, target)
        self.assertEqual(fingerprint(target, plan), expected)
        self.assertFalse(os.path.lexists(gd))

    def test_stale_submodule_gitdir_with_local_only_commit_is_refused(self):
        checkpoints, cp, plan, expected = self._suspended_uninitialised_submodule()
        target = self._stale_target('never')
        git(target / 'never', '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '--allow-empty', '-qm', 'unpublished')
        with self.assertRaisesRegex(WorkspaceError, 'submodule gitdir differs'):
            checkpoints.restore(cp, target)
        self.assertIn('unpublished', git(target / 'never', 'log', '-1', '--format=%s'))
        self.assertEqual((target / 'sentinel').read_text(), 'keep me')
        self.assertFalse((_modules(target).parent / 'runner-stale-submodules').exists())

    def test_stale_submodule_with_untracked_or_staged_work_is_refused(self):
        checkpoints, cp, plan, expected = self._suspended_uninitialised_submodule()
        target = self._stale_target('never')
        (target / 'never' / 'scratch').write_text('valuable')
        with self.assertRaisesRegex(WorkspaceError, 'submodule gitdir differs'):
            checkpoints.restore(cp, target)
        self.assertEqual((target / 'never' / 'scratch').read_text(), 'valuable')
        (target / 'never' / 'scratch').unlink()
        (target / 'never' / 'README').write_text('staged'); git(target / 'never', 'add', 'README')
        with self.assertRaisesRegex(WorkspaceError, 'submodule gitdir differs'):
            checkpoints.restore(cp, target)
        self.assertEqual((target / 'never' / 'README').read_text(), 'staged')
        self.assertEqual((target / 'sentinel').read_text(), 'keep me')

    def test_stale_submodule_with_stash_or_foreign_refs_is_refused(self):
        checkpoints, cp, plan, expected = self._suspended_uninitialised_submodule()
        target = self._stale_target('never')
        gd = _modules(target) / 'never'
        _gitdir(gd, 'update-ref', 'refs/stash', _gitdir(gd, 'rev-parse', 'HEAD'))
        with self.assertRaisesRegex(WorkspaceError, 'submodule gitdir differs'):
            checkpoints.restore(cp, target)
        self.assertTrue(os.path.lexists(gd))

    def test_stale_submodule_with_active_writer_is_refused(self):
        checkpoints, cp, plan, expected = self._suspended_uninitialised_submodule()
        target = self._stale_target('never')
        gd = _modules(target) / 'never'
        (gd / 'index.lock').write_text('')
        with self.assertRaisesRegex(WorkspaceError, 'active git writer'):
            checkpoints.restore(cp, target)
        self.assertTrue(os.path.lexists(gd))

    def test_stale_submodule_gitdir_symlink_is_still_unsupported(self):
        checkpoints, cp, plan, expected = self._suspended_uninitialised_submodule()
        target = self._stale_target('never')
        gd = _modules(target) / 'never'
        moved = self.root / 'moved-gd'; gd.rename(moved); os.symlink(moved, gd)
        with self.assertRaisesRegex(WorkspaceError, 'symlinked submodule gitdir'):
            checkpoints.restore(cp, target)
        self.assertTrue(gd.is_symlink())

    def test_restore_marker_check_sees_populated_submodule_without_nested_repo_error(self):
        checkpoints, cp, target = self._suspended_submodule()
        checkpoints.restore(cp, target)
        (target / 'parent-newer').write_text('later parent work')
        with self.assertRaisesRegex(WorkspaceError, 'newer work since completed restore'):
            checkpoints.restore(cp, target)

    def test_restore_refuses_symlinked_target_submodule_gitdir(self):
        checkpoints, cp, target = self._suspended_submodule()
        checkpoints.restore(cp, target)
        gd = _modules(target) / 'materialized'
        real = self.root / 'external-gitdir'
        gd.rename(real); os.symlink(real, gd)
        index = (real / 'index').read_bytes()
        with self.assertRaisesRegex(WorkspaceError, 'symlinked submodule gitdir'):
            checkpoints.restore(cp, target)
        self.assertEqual((real / 'index').read_bytes(), index)

    def test_restore_refuses_parent_lock_before_any_change(self):
        checkpoints, cp, target = self._suspended_submodule()
        (Path(git(target, 'rev-parse', '--absolute-git-dir')) / 'index.lock').write_text('')
        refs = git(target, 'for-each-ref')
        with self.assertRaisesRegex(WorkspaceError, 'active git writer in target'):
            checkpoints.restore(cp, target)
        self.assertEqual((target / 'sentinel').read_text(), 'keep me')
        self.assertEqual(git(target, 'for-each-ref'), refs)

    def test_restore_refuses_when_target_submodule_has_a_lock(self):
        checkpoints, cp, target = self._suspended_submodule()
        checkpoints.restore(cp, target)
        gd = _modules(target) / 'materialized'
        before = (gd / 'index').read_bytes()
        (gd / 'index.lock').write_text('')
        (target / 'sentinel').write_text('keep me')
        with self.assertRaisesRegex(WorkspaceError, 'active git writer'):
            checkpoints.restore(cp, target)
        self.assertEqual((gd / 'index').read_bytes(), before)
        self.assertEqual((target / 'sentinel').read_text(), 'keep me')

    def test_submodule_fail_closed_cases_keep_source_and_lease(self):
        sub = self.add_submodule()
        modules = _modules(self.source) / 'materialized'
        # active writer
        (modules / 'index.lock').write_text('')
        with self.assertRaisesRegex(WorkspaceError, 'active git writer'):
            self.pool.suspend('attempt', {}, writer_stopped=True)
        (modules / 'index.lock').unlink()
        # pointer leaving the own modules directory
        pointer = (sub / '.git').read_text()
        (sub / '.git').write_text('gitdir: ' + str(self.base / '.git') + '\n')
        with self.assertRaisesRegex(WorkspaceError, 'outside own modules'):
            self.pool.suspend('attempt', {}, writer_stopped=True)
        (sub / '.git').write_text(pointer)
        # recursive submodule
        (modules / 'modules').mkdir()
        with self.assertRaisesRegex(WorkspaceError, 'recursive'):
            self.pool.suspend('attempt', {}, writer_stopped=True)
        (modules / 'modules').rmdir()
        # missing git object makes the bundle fail
        objects = modules / 'objects'
        hidden = []
        for f in [p for p in objects.rglob('*') if p.is_file()]:
            moved = f.with_name(f.name + '.hidden'); f.rename(moved); hidden.append((moved, f))
        with self.assertRaises(WorkspaceError):
            self.pool.suspend('attempt', {}, writer_stopped=True)
        for moved, f in hidden:
            moved.rename(f)
        self.assertTrue(self.pool.full('app'))
        self.assertEqual(self.pool._checkpoint_rows('attempt'), [])

    def test_empty_placeholder_with_hidden_module_metadata_refused(self):
        self.add_gitlink()
        modules = Path(git(self.source, 'rev-parse', '--git-path', 'modules'))
        if not modules.is_absolute():
            modules = self.source / modules
        modules.mkdir(parents=True)
        (modules / 'unpublished').write_text('valuable metadata')
        with self.assertRaisesRegex(WorkspaceError, 'metadata'):
            self.pool.suspend('attempt', {}, writer_stopped=True)
        self.assertEqual((modules / 'unpublished').read_text(), 'valuable metadata')
        self.assertTrue(self.pool.full('app'))

    def test_gitlink_symlink_and_symlink_parent_refused(self):
        outside = self.root / 'outside'; outside.mkdir()
        path = self.add_gitlink(); path.parent.mkdir()
        os.symlink(outside, path)
        with self.assertRaisesRegex(WorkspaceError, 'symlink'):
            self.pool.suspend('attempt', {}, writer_stopped=True)
        path.unlink(); path.parent.rmdir(); os.symlink(outside, path.parent)
        with self.assertRaisesRegex(WorkspaceError, 'symlink'):
            self.pool.suspend('attempt', {}, writer_stopped=True)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertTrue(self.pool.full('app'))

    def test_restore_retries_partial_replay_after_restart(self):
        (self.source / 'README').write_text('staged')
        git(self.source, 'add', 'README')
        (self.source / 'README').write_text('unstaged')
        expected = fingerprint(self.source)
        self.pool.suspend('attempt', {}, writer_stopped=True)
        with patch.object(Checkpoints, 'restore', side_effect=WorkspaceError('interrupted')):
            with self.assertRaises(WorkspaceError):
                self.pool.restore('attempt')
        target = Path(self.pool.leased_to('attempt')[0])
        (target / 'README').write_text('partial')
        restarted = WorkspacePool(Store(self.root / 's.sqlite'), self.root / 'pool',
                                  [RepoSpec('app', self.base, max_slots=1)], owner='agent')
        paths, _ = restarted.restore('attempt')
        self.assertEqual(paths, [str(target)])
        self.assertEqual(fingerprint(target), expected)

    def test_restore_refuses_newer_parent_work_after_completed_restore(self):
        (self.source / 'README').write_text('staged')
        git(self.source, 'add', 'README')
        expected = fingerprint(self.source)
        self.pool.suspend('attempt', {}, writer_stopped=True)
        checkpoint = Path(self.pool._checkpoint_rows('attempt')[0]['checkpoint'])
        checkpoints = self.pool._checkpoints()
        target = self.root / 'target'
        target.mkdir()
        git(target, 'init', '--quiet')
        checkpoints.restore(checkpoint, target)
        self.assertEqual(fingerprint(target), expected)
        # Same checkpoint again with nothing changed is idempotent.
        checkpoints.restore(checkpoint, target)
        (target / 'README').write_text('newer parent work')
        (target / 'extra').write_text('untracked')
        newer = fingerprint(target)
        with self.assertRaisesRegex(WorkspaceError, 'newer work'):
            checkpoints.restore(checkpoint, target)
        self.assertEqual(fingerprint(target), newer)

    def test_manifest_path_traversal_refused_before_restore(self):
        import json
        self.pool.suspend('attempt', {}, writer_stopped=True)
        cp = Path(self.pool._checkpoint_rows('attempt')[0]['checkpoint'])
        data = json.loads((cp / 'manifest.json').read_text())
        data['fingerprint']['tree']['../escape'] = ['file', 0o644, 0, 'bad']
        (cp / 'manifest.json').write_text(json.dumps(data))
        with self.assertRaisesRegex(WorkspaceError, 'invalid manifest'):
            self.pool.restore('attempt')
        self.assertFalse((self.root / 'escape').exists())

    def test_local_commit_and_second_wait_preserved(self):
        git(self.source, 'config', '--worktree', 'user.name', 'test', check=False)
        (self.source / 'README').write_text('local commit')
        git(self.source, 'add', 'README')
        git(self.source, 'commit', '-q', '-m', 'local')
        head = git(self.source, 'rev-parse', 'HEAD')
        self.pool.suspend('attempt', {}, writer_stopped=True)
        paths, _ = self.pool.restore('attempt')
        p = Path(paths[0])
        self.assertEqual(git(p, 'rev-parse', 'HEAD'), head)
        (p / 'README').write_text('next wait')
        self.pool.suspend('attempt', {}, writer_stopped=True)
        paths, _ = self.pool.restore('attempt')
        self.assertEqual(Path(paths[0]).joinpath('README').read_text(), 'next wait')

    def test_partial_multirepo_suspend_never_drops_uncheckpointed_lease(self):
        second_root = self.root / 'second'
        second_root.mkdir()
        base2 = make_repo(second_root)
        repos = [RepoSpec('app', self.base, max_slots=1), RepoSpec('other', base2, max_slots=1)]
        pool = WorkspacePool(self.store, self.root / 'pool', repos, owner='agent')
        other = pool.lease('attempt', 'other')
        (self.source / 'README').write_text('A valuable')
        (other / 'README').write_text('B valuable')
        original = Checkpoints.create
        def fail_second(checkpoints, key, source, metadata):
            if metadata['repo'] == 'other':
                raise WorkspaceError('second capture failed')
            return original(checkpoints, key, source, metadata)
        with patch.object(Checkpoints, 'create', fail_second):
            with self.assertRaises(WorkspaceError):
                pool.suspend('attempt', {}, writer_stopped=True)
        self.assertEqual(pool.leased_to('attempt'), [str(other)])
        restarted = WorkspacePool(Store(self.root / 's.sqlite'), self.root / 'pool', repos, owner='agent')
        with self.assertRaisesRegex(WorkspaceError, 'incomplete repository inventory'):
            restarted.restore('attempt')
        self.assertEqual((other / 'README').read_text(), 'B valuable')
        restarted.suspend('attempt', {}, writer_stopped=True)
        paths, _ = restarted.restore('attempt', expected_repos={'app', 'other'})
        self.assertEqual(len(paths), 2)
        self.assertEqual({Path(p).joinpath('README').read_text() for p in paths}, {'A valuable', 'B valuable'})

    def test_index_publish_interruption_keeps_retryable_index(self):
        (self.source / 'README').write_text('staged-only')
        git(self.source, 'add', 'README')
        self.pool.suspend('attempt', {}, writer_stopped=True)
        replace = os.replace
        def interrupted(source, target):
            if Path(target).name == 'index':
                raise OSError('simulated process stop before index publish')
            return replace(source, target)
        with patch('mesh_runner.checkpoint.os.replace', interrupted):
            with self.assertRaises(OSError):
                self.pool.restore('attempt')
        target = Path(self.pool.leased_to('attempt')[0])
        git(target, 'status', '--porcelain')  # the old index remains parseable
        paths, _ = self.pool.restore('attempt')
        self.assertEqual(Path(paths[0]).joinpath('README').read_text(), 'staged-only')


class SlimSnapshot(unittest.TestCase):
    """Compact form: delta tree (what HEAD does not give back) and a bundle that needs only base_sha."""
    setUp = Checkpoint.setUp
    tearDown = Checkpoint.tearDown
    _restore_elsewhere = Checkpoint._restore_elsewhere

    def _base(self, p):
        return git(p, 'rev-parse', 'HEAD')

    def test_unchanged_tracked_files_and_history_are_not_stored(self):
        p = self.source
        (p / 'big.txt').write_bytes(b'x' * 300_000)
        git(p, 'add', 'big.txt')
        git(p, 'commit', '-qm', 'big tracked file')
        base = self._base(p)
        (p / 'new.txt').write_text('work')
        cp = Checkpoints(self.root / 'cp')
        checkpoint = cp.create('s1', p, {'base_sha': base})
        data = cp.read(checkpoint)
        self.assertFalse((checkpoint / 'tree' / 'big.txt').exists())
        self.assertEqual(data['delta']['files'], ['new.txt'])
        self.assertEqual(data['bundle']['mode'], 'none')        # HEAD is the base: no commits to carry
        self.assertFalse((checkpoint / 'commits.bundle').exists())
        target = self._restore_elsewhere(cp, checkpoint)
        self.assertEqual((target / 'big.txt').stat().st_size, 300_000)
        self.assertEqual((target / 'new.txt').read_text(), 'work')

    def test_local_commits_travel_in_an_incremental_bundle(self):
        p = self.source
        base = self._base(p)
        (p / 'local.txt').write_text('unpushed')
        git(p, 'add', 'local.txt')
        git(p, 'commit', '-qm', 'local commit')
        cp = Checkpoints(self.root / 'cp')
        checkpoint = cp.create('s2', p, {'base_sha': base})
        data = cp.read(checkpoint)
        self.assertEqual((data['bundle']['mode'], data['bundle']['has_head']), ('incremental', True))
        full = Checkpoints(self.root / 'cp-full', slim=False).create('s2', p, {'base_sha': base})
        self.assertLessEqual((checkpoint / 'commits.bundle').stat().st_size, (full / 'commits.bundle').stat().st_size)
        target = self._restore_elsewhere(cp, checkpoint)
        self.assertEqual(git(target, 'rev-parse', 'HEAD'), git(p, 'rev-parse', 'HEAD'))
        self.assertEqual((target / 'local.txt').read_text(), 'unpushed')

    def test_deleted_files_modes_and_empty_dirs_survive(self):
        p = self.source
        (p / 'gone.txt').write_text('tracked')
        (p / 'tool.sh').write_text('#!/bin/sh')
        git(p, 'add', 'gone.txt', 'tool.sh')
        git(p, 'commit', '-qm', 'more')
        base = self._base(p)
        (p / 'gone.txt').unlink()
        (p / 'tool.sh').chmod(0o755)
        (p / 'empty' / 'deeper').mkdir(parents=True)
        (p / 'staged-new.txt').write_text('s')
        git(p, 'add', 'staged-new.txt')
        (p / 'staged-new.txt').unlink()
        cp = Checkpoints(self.root / 'cp')
        checkpoint = cp.create('s3', p, {'base_sha': base})
        self.assertIn('delta', cp.read(checkpoint))
        target = self._restore_elsewhere(cp, checkpoint)
        self.assertFalse((target / 'gone.txt').exists())
        self.assertTrue(os.access(target / 'tool.sh', os.X_OK))
        self.assertTrue((target / 'empty' / 'deeper').is_dir())
        self.assertEqual(fingerprint(target, None, (), cp.read(checkpoint).get('ignored', [])), cp.read(checkpoint)['fingerprint'])

    def test_large_untracked_file_is_left_out_and_small_one_kept(self):
        p = self.source
        (p / 'dump.bin').write_bytes(b'0' * 2000)
        (p / 'note.txt').write_text('small')
        cp = Checkpoints(self.root / 'cp', max_untracked_bytes=1000)
        checkpoint = cp.create('s4', p, {})
        self.assertFalse((checkpoint / 'tree' / 'dump.bin').exists())
        self.assertEqual((checkpoint / 'tree' / 'note.txt').read_text(), 'small')
        target = self._restore_elsewhere(cp, checkpoint)
        self.assertFalse((target / 'dump.bin').exists())

    def test_snapshot_is_much_smaller_than_the_full_form(self):
        p = self.source
        for i in range(20):
            (p / f'tracked{i}.txt').write_bytes(os.urandom(50_000))
        git(p, 'add', '.')
        git(p, 'commit', '-qm', 'bulk')
        base = self._base(p)
        (p / 'edit.txt').write_text('one small change')
        slim = Checkpoints(self.root / 'cp').create('s5', p, {'base_sha': base})
        full = Checkpoints(self.root / 'cp-full', slim=False).create('s5', p, {'base_sha': base})
        from mesh_runner.checkpoint import _du
        self.assertLess(_du(slim) * 10, _du(full))

    def test_incomplete_delta_falls_back_to_the_full_form(self):
        p = self.source
        (p / 'README').write_text('changed')
        git(p, 'update-index', '--skip-worktree', 'README')     # status hides it; the delta would miss it
        (p / 'README').write_text('changed again')
        cp = Checkpoints(self.root / 'cp')
        checkpoint = cp.create('s6', p, {})
        self.assertNotIn('delta', cp.read(checkpoint))
        target = self._restore_elsewhere(cp, checkpoint)
        self.assertEqual((target / 'README').read_text(), 'changed again')

    def test_full_snapshot_is_migrated_to_the_slim_form_and_restores(self):
        p = self.source
        for i in range(10):
            (p / f'tracked{i}.txt').write_bytes(os.urandom(60_000))
        git(p, 'add', '.')
        git(p, 'commit', '-qm', 'bulk')
        base = self._base(p)
        (p / 'edit.txt').write_text('uncommitted')
        old = Checkpoints(self.root / 'cp', slim=False).create('m1', p, {'base_sha': base, 'repo': 'app'})
        cp = Checkpoints(self.root / 'cp')
        self.assertTrue(cp.is_old_format(old))
        result = cp.compact(old)
        self.assertTrue(result['compacted'])
        self.assertLess(result['after'] * 5, result['before'])
        self.assertFalse(cp.is_old_format(old))                 # migrated once, not every pass
        self.assertEqual(cp.read(old)['metadata']['repo'], 'app')
        target = self._restore_elsewhere(cp, old)
        self.assertEqual((target / 'edit.txt').read_text(), 'uncommitted')
        self.assertEqual((target / 'tracked3.txt').stat().st_size, 60_000)

    def test_failed_slim_capture_is_kept_full_and_not_a_migration_candidate(self):
        p = self.source
        git(p, 'update-index', '--skip-worktree', 'README')
        (p / 'README').write_text('hidden change')
        cp = Checkpoints(self.root / 'cp')
        checkpoint = cp.create('m2', p, {})
        self.assertTrue(cp.read(checkpoint).get('slim_failed'))
        self.assertFalse(cp.is_old_format(checkpoint))
