"""Checkpoints nothing can restore are removed; a live attempt's checkpoint never is."""
import datetime as dt
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

from mesh_runner import checkpoint
from mesh_runner.config import ConfigError, _idle_hours
from mesh_runner.pool_ops import prune_checkpoints
from mesh_runner.store import Store, iso
from mesh_runner.workspace import RepoSpec, WorkspacePool, git
from test_workspace import make_repo

REFS = ('refs/runner-checkpoints/', 'refs/runner-checkpoint-bases/')


class CheckpointGC(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.base = make_repo(self.root)
        self.store = Store(self.root / 's.sqlite')
        self.pool = WorkspacePool(self.store, self.root / 'pool', [RepoSpec('app', self.base, max_slots=2)],
                                  owner='agent')
        self.cps = self.root / 'pool' / 'checkpoints' / 'agent'

    def tearDown(self):
        self.tmp.cleanup()

    def attempt(self, attempt_id, state, ended_hours_ago=0.0):
        stamp = iso(dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=ended_hours_ago))
        self.store.db.execute(
            "INSERT OR REPLACE INTO attempts(id, agent, task_id, harness, state, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?)", (attempt_id, 'agent', 'task-' + attempt_id, 'claude', state, stamp, stamp))

    def suspend(self, attempt_id='attempt', state='waiting', ended_hours_ago=0.0):
        self.attempt(attempt_id, state, ended_hours_ago)
        slot = self.pool.lease(attempt_id, 'app')
        (slot / 'README').write_text('work of ' + attempt_id)
        git(slot, 'add', 'README')
        return Path(self.pool.suspend(attempt_id, {}, writer_stopped=True)[0])

    def refs(self):
        return git(self.base, 'for-each-ref', '--format=%(refname)', *REFS).split()

    def gc(self, **kw):
        return prune_checkpoints(self.pool, **{'grace_hours': 6, 'apply': True, **kw})

    def test_ended_attempt_checkpoint_row_and_refs_removed(self):
        cp = self.suspend()
        self.assertTrue(self.refs())
        self.attempt('attempt', 'completed', ended_hours_ago=7)
        res = self.gc()
        self.assertFalse(cp.exists())
        self.assertEqual(self.pool._checkpoint_rows('attempt'), [])
        self.assertEqual(self.refs(), [])
        self.assertEqual(res['rows'], 1)
        self.assertGreater(res['freed_bytes'], 0)
        self.assertEqual(list(self.cps.iterdir()), [])

    def test_live_attempt_checkpoint_kept_however_old(self):
        for state in ('starting', 'running', 'unknown', 'waiting', 'ready'):
            with self.subTest(state=state):
                cp = self.suspend('a-' + state, state, ended_hours_ago=100)
                os.utime(cp, (0, 0))
                self.gc(grace_hours=0)
                self.assertTrue((cp / 'manifest.json').exists())
                self.assertEqual(len(self.pool._checkpoint_rows('a-' + state)), 1)

    def test_recently_ended_attempt_kept_within_grace(self):
        cp = self.suspend(state='completed', ended_hours_ago=1)
        self.gc()
        self.assertTrue(cp.exists())

    def test_attempt_unknown_to_the_store_is_kept(self):
        cp = self.suspend()
        self.store.db.execute("DELETE FROM attempts")
        self.gc(grace_hours=0)
        self.assertTrue(cp.exists())

    def test_superseded_generation_removed_after_grace_current_kept(self):
        first = self.suspend()
        paths, _ = self.pool.restore('attempt')
        self.store.db.execute("UPDATE workspace_checkpoints SET phase='active'")
        (Path(paths[0]) / 'README').write_text('later work')
        second = Path(self.pool.suspend('attempt', {}, writer_stopped=True)[0])
        self.assertNotEqual(first, second)
        self.gc()                                            # first is younger than the grace period
        self.assertTrue(first.exists())
        os.utime(first, (time.time() - 7 * 3600,) * 2)
        res = self.gc()
        self.assertFalse(first.exists())
        self.assertTrue(second.exists())
        self.assertEqual(res['rows'], 0)
        keys = {r.rsplit('/', 1)[-1] for r in self.refs()}
        self.assertNotIn(first.name, keys)
        self.assertIn(second.name, keys)
        paths, _ = self.pool.restore('attempt')
        self.assertEqual((Path(paths[0]) / 'README').read_text(), 'later work')

    def test_abandoned_pending_removed_only_when_no_capture_runs(self):
        self.suspend()
        pending = self.cps / '.pending-dead'
        (pending / 'tree').mkdir(parents=True)
        held, release = threading.Event(), threading.Event()

        def capture():
            with checkpoint._IO_LOCK:
                held.set()
                release.wait(10)
        t = threading.Thread(target=capture)
        t.start()
        held.wait(10)
        try:
            self.gc(grace_hours=0)
            self.assertTrue(pending.exists())
        finally:
            release.set()
            t.join()
        self.gc(grace_hours=0)
        self.assertFalse(pending.exists())

    def test_dry_run_reports_and_changes_nothing(self):
        cp = self.suspend(state='failed', ended_hours_ago=8)
        refs = self.refs()
        res = self.gc(apply=False)
        self.assertEqual([c['checkpoint'] for c in res['checkpoints']], [str(cp)])
        self.assertGreater(res['checkpoints'][0]['bytes'], 0)
        self.assertEqual(res['freed_bytes'], 0)
        self.assertTrue(cp.exists())
        self.assertEqual(len(self.pool._checkpoint_rows('attempt')), 1)
        self.assertEqual(self.refs(), refs)

    def test_row_of_ended_attempt_with_missing_directory_is_dropped(self):
        cp = self.suspend(state='aborted', ended_hours_ago=8)
        checkpoint.shutil.rmtree(cp)
        res = self.gc()
        self.assertEqual(res['rows'], 1)
        self.assertEqual(self.pool._checkpoint_rows('attempt'), [])

    def test_ref_of_another_owners_checkpoint_kept(self):
        other = WorkspacePool(self.store, self.root / 'pool', [RepoSpec('app', self.base, max_slots=2)], owner='other')
        self.attempt('theirs', 'waiting')
        slot = other.lease('theirs', 'app')
        (slot / 'README').write_text('theirs')
        git(slot, 'add', 'README')
        theirs = Path(other.suspend('theirs', {}, writer_stopped=True)[0])
        self.gc(grace_hours=0)
        keys = {r.rsplit('/', 1)[-1] for r in self.refs()}
        self.assertIn(theirs.name, keys)
        self.assertTrue(theirs.exists())

    def test_without_attempts_table_nothing_is_touched(self):
        cp = self.suspend()
        self.store.db.execute("DROP TABLE attempts")
        res = self.gc(grace_hours=0)
        self.assertTrue(cp.exists())
        self.assertEqual(res['checkpoints'], [])

    def test_grace_must_be_a_non_negative_number(self):
        for bad in (-1, float('nan'), '6', True):
            with self.assertRaises(ValueError):
                prune_checkpoints(self.pool, grace_hours=bad)
        with self.assertRaisesRegex(ConfigError, 'checkpoint_gc_grace_hours'):
            _idle_hours(-1, 'checkpoint_gc_grace_hours')

    def test_runner_pass_runs_gc_only_when_enabled(self):
        from mesh_runner.runner import Runner
        cp = self.suspend(state='completed', ended_hours_ago=8)
        runner = Runner.__new__(Runner)
        runner.cfg = type('C', (), {'ignored_cleanup': False, 'checkpoint_gc': False,
                                    'checkpoint_gc_grace_hours': 6, 'ignored_cleanup_interval_minutes': 1})()
        runner._ignored_next = 0.0
        runner.agents = [type('A', (), {'pool': self.pool, 'agent': type('N', (), {'name': 'agent'})()})()]
        runner._prune_ignored_if_due()
        self.assertIsNone(getattr(runner, '_ignored_worker', None))
        self.assertTrue(cp.exists())
        runner.cfg.checkpoint_gc = True
        runner._prune_ignored_if_due()
        runner._ignored_worker.join(30)
        self.assertFalse(cp.exists())
        self.assertGreater(runner.checkpoint_gc_freed_bytes, 0)

    def test_config_default_off_and_grace_validated(self):
        from unittest.mock import patch
        from mesh_runner import cli
        from mesh_runner.config import RunnerConfig, load
        self.assertFalse(RunnerConfig.__dataclass_fields__['checkpoint_gc'].default)
        cfg = self.root / 'r.toml'
        cfg.write_text('[runner]\nmesh_url = "x"\nstate_dir = "%s"\ncheckpoint_gc = true\ncheckpoint_gc_grace_hours = 2\n' % self.root)
        loaded = load(cfg)
        self.assertTrue(loaded.checkpoint_gc)
        self.assertEqual(loaded.checkpoint_gc_grace_hours, 2.0)
        self.assertFalse(load(self._toml('checkpoint_gc = "yes"')).checkpoint_gc)
        for bad in ('-1', 'nan', '"6"', 'true'):
            with self.assertRaisesRegex(ConfigError, 'checkpoint_gc_grace_hours'):
                load(self._toml('checkpoint_gc_grace_hours = %s' % bad))
        with self.assertRaises(SystemExit):
            with patch('sys.stderr'):
                cli.main(['pool', '--config', 'x.toml', '--prune-checkpoints', '--checkpoint-grace-hours', '-1'])

    def _toml(self, line):
        cfg = self.root / 'line.toml'
        cfg.write_text('[runner]\nmesh_url = "x"\nstate_dir = "%s"\n%s\n' % (self.root, line))
        return cfg


if __name__ == '__main__':
    unittest.main()
