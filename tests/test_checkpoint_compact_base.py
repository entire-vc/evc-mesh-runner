"""Compaction of WAIT snapshots whose capture slot is gone.

A slot is a worktree of the repository's base clone; it is evicted, pruned or re-created after
release. The base commit a slim snapshot needs (and its pin ref) lives in the base clone's object
store, so compaction and its verification must resolve the base there, not in the slot.
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mesh_runner import checkpoint as checkpoint_mod
from mesh_runner import pool_ops
from mesh_runner.checkpoint import Checkpoints, fingerprint
from mesh_runner.store import Store
from mesh_runner.workspace import RepoSpec, WorkspacePool, WorkspaceError, git
from test_workspace import make_repo


def _objects(repo: Path) -> Path:
    objects = Path(git(repo, 'rev-parse', '--git-path', 'objects'))
    return objects if objects.is_absolute() else repo / objects


def _strip(checkpoint: Path, *keys: str) -> None:
    """Rewrite a manifest as an earlier runner version wrote it (without the given keys)."""
    path = checkpoint / 'manifest.json'
    data = json.loads(path.read_text())
    for key in keys:
        data.pop(key, None)
    path.write_text(json.dumps(data, sort_keys=True))


class CompactWithoutSourceSlot(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.base = make_repo(self.root)
        self.store = Store(self.root / 's.sqlite')
        self.pool = WorkspacePool(self.store, self.root / 'pool', [RepoSpec('app', self.base, max_slots=1)],
                                  owner='agent')
        self.source = self.pool.lease('attempt', 'app')

    def tearDown(self):
        self.tmp.cleanup()

    def _bulk(self, p: Path) -> str:
        for i in range(8):
            (p / f'tracked{i}.txt').write_bytes(os.urandom(40_000))
        git(p, 'add', '.')
        git(p, 'commit', '-qm', 'bulk')
        return git(p, 'rev-parse', 'HEAD')

    def _drop_slot(self, p: Path) -> None:
        git(self.base, 'worktree', 'remove', '--force', str(p))
        self.assertFalse(p.exists())

    def _restore_in_new_slot(self, cp: Checkpoints, checkpoint: Path) -> Path:
        """Restores land in a slot of the same base clone; a fresh worktree is one."""
        target = self.root / 'new-slot'
        git(self.base, 'worktree', 'add', '--quiet', '--detach', str(target))
        cp.restore(checkpoint, target)
        return target

    def test_full_snapshot_compacts_after_its_slot_is_gone_and_restores(self):
        p = self.source
        base = self._bulk(p)
        (p / 'local.txt').write_text('unpushed')
        git(p, 'add', 'local.txt')
        git(p, 'commit', '-qm', 'local commit')
        head = git(p, 'rev-parse', 'HEAD')
        (p / 'edit.txt').write_text('uncommitted')
        old = Checkpoints(self.root / 'cp', slim=False).create('f1', p, {'base_sha': base, 'repo': 'app'})
        expected = fingerprint(p)
        self._drop_slot(p)
        cp = Checkpoints(self.root / 'cp')
        self.assertTrue(cp.is_old_format(old))
        result = cp.compact(old)
        self.assertTrue(result['compacted'])
        self.assertLess(result['after'], result['before'])
        data = cp.read(old)
        self.assertEqual(data['bundle']['mode'], 'incremental')
        self.assertEqual(Path(data['objects']).resolve(), _objects(self.base).resolve())
        self.assertFalse(cp.is_old_format(old))
        cp.verify(old)                                   # verification no longer needs the slot
        target = self._restore_in_new_slot(cp, old)
        self.assertEqual(git(target, 'rev-parse', 'HEAD'), head)
        self.assertEqual((target / 'edit.txt').read_text(), 'uncommitted')
        self.assertEqual((target / 'tracked5.txt').stat().st_size, 40_000)
        self.assertEqual(fingerprint(target), expected)

    def test_manifest_without_objects_uses_the_base_clone_given_by_the_pool(self):
        p = self.source
        base = self._bulk(p)
        (p / 'edit.txt').write_text('uncommitted')
        old = Checkpoints(self.root / 'cp', slim=False).create('f2', p, {'base_sha': base, 'repo': 'app'})
        _strip(old, 'objects')                           # written by an earlier version
        self._drop_slot(p)
        cp = Checkpoints(self.root / 'cp')
        result = cp.compact(old, base_objects=_objects(self.base))
        self.assertTrue(result['compacted'])
        self.assertEqual(Path(cp.read(old)['objects']).resolve(), _objects(self.base).resolve())
        cp.verify(old)
        target = self._restore_in_new_slot(cp, old)
        self.assertEqual((target / 'edit.txt').read_text(), 'uncommitted')

    def test_slim_snapshot_without_commits_compacts_with_its_slot_gone(self):
        p = self.source
        base = self._bulk(p)
        (p / 'new.txt').write_text('work')
        old = Checkpoints(self.root / 'cp').create('n1', p, {'base_sha': base, 'repo': 'app'})
        self.assertEqual(Checkpoints(self.root / 'cp').read(old)['bundle']['mode'], 'none')
        self._drop_slot(p)
        cp = Checkpoints(self.root / 'cp')
        # Not a candidate under the current rules; compaction of such a snapshot must still be sound
        # (e.g. one written with another exclusion list), so drive it directly.
        with patch.object(Checkpoints, 'is_old_format', return_value=True):
            result = cp.compact(old)
        self.assertTrue(result['compacted'])
        cp.verify(old)
        target = self._restore_in_new_slot(cp, old)
        self.assertEqual((target / 'new.txt').read_text(), 'work')
        self.assertEqual((target / 'tracked1.txt').stat().st_size, 40_000)

    def test_base_absent_from_the_base_clone_is_reported_as_such(self):
        other = self.root / 'other'
        subprocess.run(['git', 'init', '-q', '-b', 'main', str(other)], check=True)
        git(other, 'config', 'user.name', 't')
        git(other, 'config', 'user.email', 't@example.com')
        (other / 'a.txt').write_text('a')
        git(other, 'add', '.')
        git(other, 'commit', '-qm', 'unrelated')
        base = git(other, 'rev-parse', 'HEAD')
        (other / 'b.txt').write_text('b')
        old = Checkpoints(self.root / 'cp', slim=False).create('x1', other, {'base_sha': base})
        shutil.rmtree(other)
        cp = Checkpoints(self.root / 'cp')
        with self.assertRaises(checkpoint_mod.CheckpointBaseMissing):
            cp.compact(old, base_objects=_objects(self.base))
        self.assertEqual([x.name for x in cp.root.iterdir()], ['x1'])     # old snapshot untouched
        cp.verify(old)


class TrackedCacheIsNotRecompacted(unittest.TestCase):
    """A repository that tracks a cache directory (`__pycache__/*.pyc`) has nothing to exclude."""
    setUp = CompactWithoutSourceSlot.setUp
    tearDown = CompactWithoutSourceSlot.tearDown

    def _tracked_cache(self) -> tuple[Path, str]:
        p = self.source
        (p / 'pkg' / '__pycache__').mkdir(parents=True)
        (p / 'pkg' / '__pycache__' / 'm.cpython-311.pyc').write_bytes(b'\0compiled')
        git(p, 'add', '-f', 'pkg')
        git(p, 'commit', '-qm', 'tracked cache')
        base = git(p, 'rev-parse', 'HEAD')
        (p / 'edit.txt').write_text('work')
        return p, base

    def test_slim_migration_happens_once(self):
        p, base = self._tracked_cache()
        old = Checkpoints(self.root / 'cp', slim=False).create('c1', p, {'base_sha': base})
        cp = Checkpoints(self.root / 'cp')
        self.assertTrue(cp.is_old_format(old))
        self.assertTrue(cp.compact(old)['compacted'])
        self.assertFalse(cp.is_old_format(old))
        self.assertFalse(cp.compact(old)['compacted'])
        self.assertEqual(cp.read(old).get('excludes'), [])

    def test_full_form_with_nothing_to_exclude_is_not_old_format(self):
        p, base = self._tracked_cache()
        cp = Checkpoints(self.root / 'cp', slim=False)
        snap = cp.create('c2', p, {'base_sha': base})
        self.assertFalse(cp.is_old_format(snap))
        self.assertEqual(cp.read(snap).get('excludes'), [])

    def test_slim_snapshot_is_never_old_format(self):
        p, base = self._tracked_cache()
        cp = Checkpoints(self.root / 'cp')
        snap = cp.create('c3', p, {'base_sha': base})
        _strip(snap, 'excludes')                         # as an earlier version wrote it
        self.assertFalse(cp.is_old_format(snap))


class CompactPassOnAPool(unittest.TestCase):
    """End to end: WAIT snapshot of a slot, slot evicted, compaction pass, restore into a new slot."""
    setUp = CompactWithoutSourceSlot.setUp
    tearDown = CompactWithoutSourceSlot.tearDown

    def _suspend_full_form(self) -> Path:
        p = self.source
        (p / 'edit.txt').write_text('uncommitted')
        (p / 'node_modules' / 'dep').mkdir(parents=True)
        (p / 'node_modules' / 'dep' / 'blob').write_bytes(b'regenerable' * 1000)
        full = lambda self, key, source, metadata: self._create(key, source, metadata, slim=False)
        with patch.object(Checkpoints, '_create_best', full):
            self.pool.suspend('attempt', {'session_id': 's'}, writer_stopped=True)
        cp = Path(self.store.db.execute("SELECT checkpoint FROM workspace_checkpoints").fetchone()[0])
        _strip(cp, 'objects', 'excludes')                # a snapshot written before this change
        return cp

    def _evict(self) -> None:
        removed = pool_ops.evict(self.pool, idle_hours=0, keep=0, limit=10, apply=True)
        self.assertEqual(removed, [str(self.source)])
        self.assertFalse(self.source.exists())

    def test_pass_compacts_a_snapshot_of_an_evicted_slot_and_restore_works(self):
        cp = self._suspend_full_form()
        self._evict()
        result = pool_ops.compact_checkpoints(self.pool, apply=True)
        self.assertEqual((result['compacted'], result['errors']), (1, 0))
        self.assertFalse((cp / 'tree' / 'node_modules').exists())
        paths, manifests = self.pool.restore('attempt')
        self.assertEqual((Path(paths[0]) / 'edit.txt').read_text(), 'uncommitted')
        self.assertEqual(manifests[0]['metadata']['session_id'], 's')


class CompactFailureMemory(unittest.TestCase):
    """A failing snapshot is remembered in the store, across a restart."""
    setUp = CompactWithoutSourceSlot.setUp
    tearDown = CompactWithoutSourceSlot.tearDown

    def _snapshot(self) -> Path:
        (self.source / 'edit.txt').write_text('w')
        self.pool.suspend('attempt', {}, writer_stopped=True)
        return Path(self.store.db.execute("SELECT checkpoint FROM workspace_checkpoints").fetchone()[0])

    def _restarted(self) -> WorkspacePool:
        # a new process: fresh module state, a new store connection on the same file
        getattr(pool_ops, '_compact_failed', {}).clear()
        return WorkspacePool(Store(self.root / 's.sqlite'), self.root / 'pool',
                             [RepoSpec('app', self.base, max_slots=1)], owner='agent')

    def _age(self, pool: WorkspacePool, seconds: float) -> None:
        pool.store.db.execute("UPDATE checkpoint_compact_failures SET failed_at = failed_at - ?", (seconds,))

    def test_failure_pause_survives_a_restart_and_repeats_are_quiet(self):
        self._snapshot()
        calls = []

        def boom(self_, path, **kw):
            calls.append(path)
            raise WorkspaceError(f'checkpoint: bundle base unavailable ({self_.root}/.verify-{len(calls)}x)')
        with patch.object(Checkpoints, 'is_old_format', return_value=True), \
                patch.object(Checkpoints, 'compact', boom):
            with self.assertLogs('mesh_runner', 'WARNING'):
                first = pool_ops.compact_checkpoints(self.pool, apply=True)
            pool = self._restarted()
            second = pool_ops.compact_checkpoints(pool, apply=True)
            self.assertEqual((first['errors'], second['errors'], len(calls)), (1, 0, 1))
            self._age(pool, pool_ops._COMPACT_RETRY_AFTER + 1)
            with self.assertNoLogs('mesh_runner', 'WARNING'):
                third = pool_ops.compact_checkpoints(self._restarted(), apply=True)
        self.assertEqual((first['errors'], second['errors'], third['errors']), (1, 0, 1))
        self.assertEqual(len(calls), 2)

    def test_snapshot_without_base_is_never_a_candidate_again(self):
        self._snapshot()
        calls = []

        def missing(self_, path, **kw):
            calls.append(path)
            raise checkpoint_mod.CheckpointBaseMissing('checkpoint: bundle base missing from the base clone')
        with patch.object(Checkpoints, 'is_old_format', return_value=True), \
                patch.object(Checkpoints, 'compact', missing):
            with self.assertLogs('mesh_runner', 'WARNING'):
                pool_ops.compact_checkpoints(self.pool, apply=True)
            pool = self._restarted()
            self.assertEqual(pool_ops.compact_checkpoints(pool, apply=True)['errors'], 0)
            self._age(pool, 30 * 24 * 3600)
            later = pool_ops.compact_checkpoints(pool, apply=True)
        self.assertEqual(len(calls), 1)
        self.assertEqual((later['errors'], later['candidates']), (0, []))

    def test_compaction_without_gain_is_final(self):
        self._snapshot()
        calls = []

        def no_gain(self_, path, **kw):
            calls.append(path)
            return {'compacted': True, 'before': 4096, 'after': 4096}
        with patch.object(Checkpoints, 'is_old_format', return_value=True), \
                patch.object(Checkpoints, 'compact', no_gain):
            pool_ops.compact_checkpoints(self.pool, apply=True)
            pool = self._restarted()
            pool_ops.compact_checkpoints(pool, apply=True)
            self.assertEqual(len(calls), 1)
            self._age(pool, 30 * 24 * 3600)
            pool_ops.compact_checkpoints(pool, apply=True)
        self.assertEqual(len(calls), 1)

    def test_memory_of_a_removed_snapshot_is_dropped(self):
        cp = self._snapshot()
        with patch.object(Checkpoints, 'is_old_format', return_value=True), \
                patch.object(Checkpoints, 'compact', side_effect=WorkspaceError('checkpoint: x')):
            with self.assertLogs('mesh_runner', 'WARNING'):
                pool_ops.compact_checkpoints(self.pool, apply=True)
        shutil.rmtree(cp)
        pool_ops.compact_checkpoints(self.pool, apply=True)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM checkpoint_compact_failures").fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
