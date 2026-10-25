import unittest
from types import SimpleNamespace
from unittest.mock import patch

from mesh_runner import pool_ops
from mesh_runner.runner import Runner


class CompactInGcPass(unittest.TestCase):
    def _runner(self, gc):
        cfg = SimpleNamespace(checkpoint_gc=gc, checkpoint_gc_grace_hours=6.0, ignored_cleanup=False)
        ar = SimpleNamespace(agent=SimpleNamespace(name='a'), pool=object())
        return SimpleNamespace(cfg=cfg, agents=[ar], _measure_checkpoints=lambda: None)

    def test_gc_pass_compacts_old_snapshots_when_gc_is_on(self):
        calls = []
        with patch.object(pool_ops, 'prune_checkpoints', return_value={'freed_bytes': 0}), \
                patch.object(pool_ops, 'compact_checkpoints',
                             side_effect=lambda pool, **kw: calls.append(kw) or {'freed_bytes': 5}):
            r = self._runner(True)
            Runner._prune_ignored_pass(r)
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0]['apply'])
        self.assertEqual(r.checkpoint_gc_freed_bytes, 5)

    def test_gc_pass_leaves_snapshots_alone_when_gc_is_off(self):
        with patch.object(pool_ops, 'compact_checkpoints') as compact:
            Runner._prune_ignored_pass(self._runner(False))
        compact.assert_not_called()

    def test_returned_errors_are_logged(self):
        with patch.object(pool_ops, 'prune_checkpoints', return_value={'freed_bytes': 0}), \
                patch.object(pool_ops, 'compact_checkpoints', return_value={'freed_bytes': 0, 'errors': 2}), \
                self.assertLogs('mesh_runner', 'WARNING') as logs:
            Runner._prune_ignored_pass(self._runner(True))
        self.assertTrue(any('2 error' in line for line in logs.output))

    def test_a_failing_compact_does_not_break_the_pass(self):
        with patch.object(pool_ops, 'prune_checkpoints', return_value={'freed_bytes': 0}), \
                patch.object(pool_ops, 'compact_checkpoints', side_effect=RuntimeError('x')):
            Runner._prune_ignored_pass(self._runner(True))


class CompactablePhases(unittest.TestCase):
    def test_in_flight_phases_are_never_compacted(self):
        self.assertEqual(set(pool_ops._COMPACTABLE_PHASES), {'verified', 'released', 'restored', 'active'})
        self.assertNotIn('releasing', pool_ops._COMPACTABLE_PHASES)
        self.assertNotIn('restoring', pool_ops._COMPACTABLE_PHASES)


class CompactRetryPause(unittest.TestCase):
    def _pool(self, path):
        import sqlite3
        db = sqlite3.connect(':memory:')
        db.row_factory = sqlite3.Row
        db.execute('CREATE TABLE workspace_checkpoints (owner, attempt, repo, phase, checkpoint)')
        db.execute("INSERT INTO workspace_checkpoints VALUES ('o', 1, 'r', 'verified', ?)", (str(path),))
        cps = SimpleNamespace(is_old_format=lambda p: True,
                              compact=lambda p, **kw: (_ for _ in ()).throw(pool_ops.WorkspaceError('bundle base unavailable')))
        return SimpleNamespace(owner='o', store=SimpleNamespace(db=db), repos={'r': SimpleNamespace(base='b')},
                               _checkpoints=lambda: cps)

    def test_a_failed_snapshot_is_not_retried_every_pass(self):
        import contextlib
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            pool = self._pool(Path(tmp))
            locks = []

            @contextlib.contextmanager
            def lock(base, deadline):
                locks.append(base)
                yield True
            with patch.object(pool_ops, '_bounded_base_lock', lock):
                first = pool_ops.compact_checkpoints(pool, apply=True)
                second = pool_ops.compact_checkpoints(pool, apply=True)
                pool.store.db.execute("UPDATE checkpoint_compact_failures SET failed_at = failed_at - ?",
                                      (pool_ops._COMPACT_RETRY_AFTER + 1,))
                third = pool_ops.compact_checkpoints(pool, apply=True)
        self.assertEqual((first['errors'], second['errors'], third['errors']), (1, 0, 1))
        self.assertEqual(len(locks), 2)


if __name__ == '__main__':
    unittest.main()


class CheckpointBudget(unittest.TestCase):
    def _runner(self, root, budget_gb):
        cfg = SimpleNamespace(pool_root=root, checkpoint_budget_gb=budget_gb)
        return SimpleNamespace(cfg=cfg, checkpoint_bytes=None)

    def test_size_is_measured_and_an_overrun_is_logged_not_deleted(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            snap = Path(tmp) / 'checkpoints' / 'a' / 'k'
            snap.mkdir(parents=True)
            (snap / 'blob').write_bytes(b'x' * 2_000_000)
            r = self._runner(Path(tmp), 0.001)              # ~1 MiB budget
            with self.assertLogs('mesh_runner', 'WARNING') as logs:
                Runner._measure_checkpoints(r)
            self.assertGreaterEqual(r.checkpoint_bytes, 2_000_000)
            self.assertTrue(any('over the' in line for line in logs.output))
            self.assertTrue((snap / 'blob').exists())

    def test_no_snapshots_is_zero_bytes(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            r = self._runner(Path(tmp), 4.0)
            Runner._measure_checkpoints(r)
            self.assertEqual(r.checkpoint_bytes, 0)
