"""Pinned cleanup uses real journal rows and snapshots; run in CI with the DB tests."""
import datetime as dt
import tempfile
import unittest
from pathlib import Path

from mesh_runner.checkpoint import Checkpoints, manifest_digest
from mesh_runner.pool_ops import compact_checkpoints, prune_checkpoints
from mesh_runner.store import Store, iso
from mesh_runner.workspace import RepoSpec, WorkspacePool, WorkspaceError, git
from test_workspace import make_repo


class CheckpointPins(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.base = make_repo(self.root)
        self.store = Store(self.root / 'store.sqlite')
        self.pool = WorkspacePool(self.store, self.root / 'pool', [RepoSpec('app', self.base)], owner='agent')
        self.cps = self.pool._checkpoints()

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def snapshot(self, key, state='completed', phase='verified'):
        stamp = iso(dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=10))
        self.store.db.execute(
            'INSERT INTO attempts(id,agent,task_id,harness,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',
            (key, 'agent', 'task-' + key, 'codex', state, stamp, stamp))
        path = Checkpoints(self.cps.root, slim=False).create(key, self.base, {'repo': 'app'})
        self.store.db.execute('INSERT INTO workspace_checkpoints VALUES(?,?,?,?,?,?,?)',
                              ('agent', key, 'app', str(path), str(self.base), phase, ''))
        return path

    def test_prune_allowlist_does_not_expand_to_new_checkpoints_or_pending_trash(self):
        chosen = self.snapshot('chosen')
        pin = {chosen.name: manifest_digest(chosen)}
        other = self.snapshot('later')
        for name in ('.pending-other', '.trash-other'):
            target = self.cps.root / name
            target.mkdir(); (target / 'keep').write_bytes(b'keep')
        report = prune_checkpoints(self.pool, grace_hours=0, checkpoint_pins=pin)
        self.assertEqual([e['checkpoint'] for e in report['checkpoints']], [str(chosen)])
        self.assertTrue(chosen.exists())
        report = prune_checkpoints(self.pool, grace_hours=0, apply=True, checkpoint_pins=pin)
        self.assertEqual(report['errors'], 0)
        self.assertFalse(chosen.exists()); self.assertTrue(other.exists())
        for name in ('.pending-other', '.trash-other'):
            self.assertEqual((self.cps.root / name / 'keep').read_bytes(), b'keep')

    def test_both_operations_reject_manifest_drift_before_mutation(self):
        chosen = self.snapshot('chosen')
        pin = {chosen.name: manifest_digest(chosen)}
        with (chosen / 'manifest.json').open('a') as out:
            out.write(' ')
        for operation, kwargs in ((prune_checkpoints, {'grace_hours': 0}), (compact_checkpoints, {})):
            with self.assertRaisesRegex(WorkspaceError, 'manifest drift'):
                operation(self.pool, apply=True, checkpoint_pins=pin, **kwargs)
            self.assertTrue(chosen.exists())
        with self.assertRaisesRegex(WorkspaceError, 'manifest drift'):
            self.cps.compact(chosen, expected_manifest_sha=pin[chosen.name])

    def test_selected_prune_preserves_same_key_ref_in_another_repository(self):
        other_root = self.root / 'other'
        other_root.mkdir()
        other_base = make_repo(other_root)
        prefixes = ('refs/runner-checkpoints/', 'refs/runner-checkpoint-bases/')

        def refs(base):
            return git(base, 'for-each-ref', '--format=%(refname) %(objectname)', *prefixes).splitlines()

        for other_first in (False, True):
            with self.subTest(other_first=other_first):
                repos = [RepoSpec('app', self.base), RepoSpec('other', other_base)]
                self.pool = WorkspacePool(self.store, self.root / 'pool',
                                          repos[::-1] if other_first else repos, owner='agent')
                chosen = self.snapshot('chosen-' + str(int(other_first)))
                for base in (self.base, other_base):
                    for prefix in prefixes:
                        git(base, 'update-ref', prefix + chosen.name, 'HEAD')
                        git(base, 'update-ref', prefix + 'unselected', 'HEAD')
                owner_before = refs(self.base)
                other_before = refs(other_base)

                report = prune_checkpoints(self.pool, grace_hours=0, apply=True,
                                           checkpoint_pins={chosen.name: manifest_digest(chosen)})

                self.assertEqual((report['errors'], report['rows'], report['refs']), (0, 1, 2))
                self.assertFalse(chosen.exists())
                self.assertEqual(refs(other_base), other_before)
                expected = [line for line in owner_before
                            if line.split()[0].rsplit('/', 1)[-1] != chosen.name]
                self.assertEqual(refs(self.base), expected)

    def test_active_unknown_or_transitional_reference_is_refused(self):
        chosen = self.snapshot('chosen')
        pin = {chosen.name: manifest_digest(chosen)}
        for state, phase in (('waiting', 'verified'), ('running', 'restored'), ('completed', 'active'),
                             ('completed', 'restoring')):
            self.store.db.execute('UPDATE attempts SET state=?', (state,))
            self.store.db.execute('UPDATE workspace_checkpoints SET phase=?', (phase,))
            for operation, kwargs in ((prune_checkpoints, {'grace_hours': 0}), (compact_checkpoints, {})):
                with self.assertRaisesRegex(WorkspaceError, 'active or unknown reference'):
                    operation(self.pool, apply=True, checkpoint_pins=pin, **kwargs)
        self.store.db.execute('DELETE FROM attempts')
        with self.assertRaisesRegex(WorkspaceError, 'active or unknown reference'):
            prune_checkpoints(self.pool, grace_hours=0, apply=True, checkpoint_pins=pin)
        self.assertTrue(chosen.exists())

    def test_symlinks_missing_and_unsafe_ids_are_refused(self):
        chosen = self.snapshot('chosen')
        sha = manifest_digest(chosen)
        link = self.cps.root / 'link'; link.symlink_to(chosen, target_is_directory=True)
        for key in ('link', 'missing', '../chosen', '.trash-chosen'):
            with self.assertRaises(WorkspaceError):
                prune_checkpoints(self.pool, grace_hours=0, apply=True, checkpoint_pins={key: sha})
        manifest = chosen / 'manifest.json'
        saved = chosen / 'original'; manifest.rename(saved); manifest.symlink_to(saved)
        with self.assertRaisesRegex(WorkspaceError, 'unsafe manifest'):
            compact_checkpoints(self.pool, apply=True, checkpoint_pins={'chosen': sha})
        self.assertTrue(saved.is_file())

    def test_compact_allowlist_preserves_unselected_snapshot_and_replay(self):
        chosen = self.snapshot('chosen')
        other = self.snapshot('other')
        other_sha = manifest_digest(other)
        pin = {'chosen': manifest_digest(chosen)}
        report = compact_checkpoints(self.pool, checkpoint_pins=pin)
        self.assertEqual([e['checkpoint'] for e in report['candidates']], [str(chosen)])
        report = compact_checkpoints(self.pool, apply=True, checkpoint_pins=pin)
        self.assertEqual((report['compacted'], report['errors']), (1, 0))
        self.assertEqual(manifest_digest(other), other_sha)
        self.cps.verify(chosen)
        with self.assertRaisesRegex(WorkspaceError, 'manifest drift'):
            compact_checkpoints(self.pool, apply=True, checkpoint_pins=pin)
