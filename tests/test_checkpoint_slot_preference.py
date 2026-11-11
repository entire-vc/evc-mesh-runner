"""Restore prefers its old safe slot; unavailable sources never steal state."""
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from mesh_runner.checkpoint import fingerprint
from mesh_runner.store import Store
from mesh_runner.workspace import RepoSpec, WorkspacePool, POOL_ADMIN_HOLD_NOTE
from test_workspace import make_repo


class RestoreSlotPreference(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.base = make_repo(self.root)
        self.store = Store(self.root / 'state.sqlite')
        self.pool = self.new_pool(self.store)
        self.first = self.pool.lease('earlier', 'app')
        self.source = self.pool.lease('attempt', 'app')
        (self.source / 'README').write_text('checkpoint work')
        self.pool.suspend('attempt', {'session_id': 'same-session'}, writer_stopped=True)
        self.pool.release('earlier')

    def new_pool(self, store):
        return WorkspacePool(store, self.root / 'pool',
                             [RepoSpec('app', self.base, max_slots=2)], owner='agent')

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def assert_restore(self, target, pool=None):
        pool = pool or self.pool
        paths, manifests = pool.restore('attempt')
        self.assertEqual(paths, [str(target)])
        self.assertEqual((target / 'README').read_text(), 'checkpoint work')
        self.assertEqual(manifests[0]['metadata']['session_id'], 'same-session')

    def test_prefers_source_over_earlier_free_slot(self):
        self.assert_restore(self.source)
        self.assertEqual(self.pool.leased_to('attempt'), [str(self.source)])

    def test_preference_survives_store_restart_and_reentry_keeps_later_edits(self):
        self.store.db.close()
        self.store = Store(self.root / 'state.sqlite')
        restarted = self.new_pool(self.store)
        self.assert_restore(self.source, restarted)
        (self.source / 'README').write_text('later work')
        restarted.restore('attempt')
        self.assertEqual((self.source / 'README').read_text(), 'later work')

    def test_new_work_leaves_the_suspended_attempts_slot_for_its_restore(self):
        # both slots are free; ``source`` sorts second by path here, so name the order explicitly
        free = [r['path'] for r in self.pool._rows('app', 'free')]
        self.assertEqual(sorted(free), sorted([str(self.first), str(self.source)]))
        self.assertEqual(self.pool.lease('newcomer', 'app'), self.first)
        self.assert_restore(self.source)

    def test_new_work_skips_an_earlier_slot_a_suspended_attempt_left(self):
        # the suspended attempt's slot is the FIRST free one by path: new work still takes the later one
        pool = WorkspacePool(self.store, self.root / 'pool',
                             [RepoSpec('app', self.base, max_slots=3)], owner='agent')
        early = pool.lease('early', 'app')
        self.assertEqual(early, self.first)
        pool.lease('busy', 'app')                             # takes ``source``: the only free slot
        third = pool.lease('third', 'app')                    # a new slot
        (early / 'README').write_text('early work')
        pool.suspend('early', {'session_id': 'early-session'}, writer_stopped=True)
        pool.release('third')
        self.assertEqual(sorted(r['path'] for r in pool._rows('app', 'free')), sorted([str(early), str(third)]))
        self.assertEqual(pool.lease('newcomer', 'app'), third)
        paths, _ = pool.restore('early')
        self.assertEqual(paths, [str(early)])
        self.assertEqual((early / 'README').read_text(), 'early work')

    def test_the_only_free_slot_is_still_taken_when_a_suspended_attempt_left_it(self):
        self.assertEqual(self.pool.lease('one', 'app'), self.first)
        self.assertEqual(self.pool.lease('two', 'app'), self.source)

    def test_foreign_lease_falls_back_without_touching_its_work(self):
        first = self.pool.lease('blocker', 'app')
        foreign = self.pool.lease('foreign', 'app')
        self.assertEqual(foreign, self.source)
        (foreign / 'README').write_text('foreign work')
        before = fingerprint(foreign)
        self.pool.release('blocker')
        self.assert_restore(first)
        self.assertEqual(fingerprint(foreign), before)
        self.assertEqual(self.pool.leased_to('foreign'), [str(foreign)])

    def test_dirty_source_is_held_and_preserved(self):
        (self.source / 'README').write_text('unclaimed dirty work')
        before = fingerprint(self.source)
        self.assert_restore(self.first)
        self.assertEqual(fingerprint(self.source), before)
        self.assertEqual(self.store.db.execute('SELECT state FROM slots WHERE path=?',
                         (str(self.source),)).fetchone()[0], 'held')

    def test_admin_hold_source_is_not_reclaimed(self):
        self.store.db.execute("UPDATE slots SET state='held', note=? WHERE path=?",
                              (POOL_ADMIN_HOLD_NOTE, str(self.source)))
        before = fingerprint(self.source)
        self.assert_restore(self.first)
        self.assertEqual(fingerprint(self.source), before)
        self.assertEqual(self.store.db.execute('SELECT state,note FROM slots WHERE path=?',
                         (str(self.source),)).fetchone()[:], ('held', POOL_ADMIN_HOLD_NOTE))

    def test_free_source_with_foreign_admin_link_is_held_without_touching_foreign_tree(self):
        (self.source / '.git').write_bytes((self.first / '.git').read_bytes())
        pointer = (self.source / '.git').read_bytes()
        with self.assertLogs('mesh_runner', 'WARNING'):
            self.assert_restore(self.first)
        self.assertEqual((self.source / '.git').read_bytes(), pointer)
        row = self.store.db.execute('SELECT state,note FROM slots WHERE path=?',
                                    (str(self.source),)).fetchone()
        self.assertEqual(row['state'], 'held')
        self.assertIn(POOL_ADMIN_HOLD_NOTE, row['note'])

    def test_concurrent_restore_and_foreign_allocation_get_distinct_slots(self):
        other_store = Store(self.root / 'state.sqlite')
        other = self.new_pool(other_store)
        barrier = threading.Barrier(2)

        def restore():
            barrier.wait(timeout=10)
            return self.pool.restore('attempt')[0][0]

        def allocate():
            barrier.wait(timeout=10)
            return str(other.lease('foreign', 'app', preferred_path=str(self.source)))

        try:
            with ThreadPoolExecutor(max_workers=2) as workers:
                restored, allocated = workers.submit(restore), workers.submit(allocate)
                target, foreign = Path(restored.result(timeout=30)), Path(allocated.result(timeout=30))
            self.assertNotEqual(target, foreign)
            self.assertEqual((target / 'README').read_text(), 'checkpoint work')
            self.assertEqual((foreign / 'README').read_text(), 'x\n')
            self.assertEqual(other.leased_to('foreign'), [str(foreign)])
            self.assertEqual(self.pool.leased_to('attempt'), [str(target)])
        finally:
            other_store.db.close()

    def test_restart_reuses_already_owned_fallback_even_when_source_is_free(self):
        # Crash after allocation but before recording the restoring phase.
        self.assertEqual(self.pool.lease('attempt', 'app'), self.first)
        self.store.db.close()
        self.store = Store(self.root / 'state.sqlite')
        restarted = self.new_pool(self.store)
        self.assert_restore(self.first, restarted)
        self.assertEqual(restarted.leased_to('attempt'), [str(self.first)])

    def test_source_from_another_pool_is_never_an_admission_candidate(self):
        foreign_pool = WorkspacePool(self.store, self.root / 'pool',
                                     [RepoSpec('app', self.base)], owner='foreign')
        foreign = foreign_pool.lease('foreign', 'app')
        before = fingerprint(foreign)
        self.store.db.execute('UPDATE workspace_checkpoints SET source=? WHERE attempt=?',
                              (str(foreign), 'attempt'))
        self.assert_restore(self.first)
        self.assertEqual(fingerprint(foreign), before)
        self.assertEqual(foreign_pool.leased_to('foreign'), [str(foreign)])
