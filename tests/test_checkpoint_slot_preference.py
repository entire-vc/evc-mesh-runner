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
        self.pool.restore('attempt')                          # ``source`` is busy again
        early = pool.lease('early', 'app')
        self.assertEqual(early, self.first)
        third = pool.lease('third', 'app')                    # a new slot: the pool is at its maximum now
        (early / 'README').write_text('early work')
        pool.suspend('early', {'session_id': 'early-session'}, writer_stopped=True)
        pool.release('third')
        self.assertEqual(sorted(r['path'] for r in pool._rows('app', 'free')), sorted([str(early), str(third)]))
        self.assertEqual(pool.lease('newcomer', 'app'), third)
        paths, _ = pool.restore('early')
        self.assertEqual(paths, [str(early)])
        self.assertEqual((early / 'README').read_text(), 'early work')

    def test_new_work_leaves_the_slot_of_a_card_that_may_come_back(self):
        """A card left the lane and its attempt ended: the ownership-loss note names its slot. Other work
        takes the plain free slot first, so the card finds its paths (and its session) when it returns."""
        import json
        pool = WorkspacePool(self.store, self.root / 'pool',
                             [RepoSpec('app', self.base, max_slots=3)], owner='agent')
        self.pool.restore('attempt')                          # the suspended attempt is back on ``source``
        left = pool.lease('left', 'app')                      # the earliest free slot: ``first``
        self.assertEqual(left, self.first)
        third = pool.lease('third', 'app')                    # a new slot
        pool.release('left'); pool.release('third')
        self.store.mark('agent', 'task-left', 'ownership_loss', json.dumps({"attempt": "left", "observed_status": "review", "workspaces": [str(left)]}))
        self.assertEqual(sorted(r['path'] for r in pool._rows('app', 'free')), sorted([str(left), str(third)]))
        self.assertEqual(pool.lease('newcomer', 'app'), third)
        # the returning card asks for its slot and gets it
        self.assertEqual(pool.lease('left-again', 'app', preferred_path=str(left)), left)

    def test_a_pool_that_may_grow_makes_a_new_slot_instead_of_taking_one_kept_aside(self):
        """Below the maximum the slot of a card that may come back is not taken at all: the newcomer gets a
        new slot, and the returning card still finds its own."""
        import json
        pool = WorkspacePool(self.store, self.root / 'pool',
                             [RepoSpec('app', self.base, max_slots=4)], owner='agent')
        self.pool.restore('attempt')                          # ``source`` is busy again
        left = pool.lease('left', 'app')                      # ``first``
        pool.release('left')
        self.store.mark('agent', 'task-left', 'ownership_loss', json.dumps({"attempt": "left", "observed_status": "review", "workspaces": [str(left)]}))
        self.assertEqual([r['path'] for r in pool._rows('app', 'free')], [str(left)])
        new = pool.lease('newcomer', 'app')
        self.assertNotIn(new, (left, self.source))
        self.assertEqual(len(pool._rows('app')), 3)
        self.assertEqual(pool.lease('left-again', 'app', preferred_path=str(left)), left)

    def test_a_pool_that_may_grow_leaves_a_suspended_attempts_slot_too(self):
        pool = WorkspacePool(self.store, self.root / 'pool',
                             [RepoSpec('app', self.base, max_slots=3)], owner='agent')
        self.assertEqual(pool.lease('one', 'app'), self.first)       # the plain free slot
        new = pool.lease('two', 'app')                               # not ``source``: a new slot
        self.assertNotIn(new, (self.first, self.source))
        self.assert_restore(self.source, pool)

    def test_a_caller_that_names_a_suspended_attempts_slot_gets_it(self):
        pool = WorkspacePool(self.store, self.root / 'pool',
                             [RepoSpec('app', self.base, max_slots=3)], owner='agent')
        self.assertEqual(pool.lease('named', 'app', preferred_path=str(self.source)), self.source)

    def test_at_the_maximum_a_kept_aside_slot_is_taken_as_before(self):
        import json
        pool = WorkspacePool(self.store, self.root / 'pool',
                             [RepoSpec('app', self.base, max_slots=2)], owner='agent')
        self.pool.restore('attempt')
        left = pool.lease('left', 'app')
        pool.release('left')
        self.store.mark('agent', 'task-left', 'ownership_loss', json.dumps({"attempt": "left", "observed_status": "review", "workspaces": [str(left)]}))
        self.assertEqual(pool.lease('newcomer', 'app'), left)

    def _prune(self, pool, path):
        """What a slot pruner does to a free slot: the worktree and its inventory row go away."""
        import subprocess
        # slots are locked against a foreign prune; the pruner unlocks before it removes one
        subprocess.run(['git', '-C', str(self.base), 'worktree', 'unlock', str(path)], check=True)
        subprocess.run(['git', '-C', str(self.base), 'worktree', 'remove', '--force', str(path)], check=True)
        self.store.db.execute("DELETE FROM slots WHERE path=? AND state='free'", (str(path),))
        self.assertFalse(Path(path).exists())

    def test_a_returning_card_gets_its_pruned_slot_back_at_the_same_path(self):
        pool = WorkspacePool(self.store, self.root / 'pool',
                             [RepoSpec('app', self.base, max_slots=3)], owner='agent')
        self.pool.restore('attempt')                          # ``source`` (slot-2) is busy again
        third = pool.lease('left', 'app', preferred_path=None)
        self.assertEqual(third, self.first)                   # slot-1
        extra = pool.lease('other', 'app')                    # slot-3
        pool.release('other')
        self._prune(pool, extra)
        # the card that held slot-3 comes back: slot-1 is busy, slot-3 is gone; it is made again in place
        back = pool.lease('other-again', 'app', preferred_path=str(extra))
        self.assertEqual(back, extra)
        self.assertTrue((back / '.git').exists())
        self.assertEqual(pool.leased_to('other-again'), [str(extra)])

    def test_a_pruned_slot_is_remade_even_when_another_slot_is_free(self):
        pool = WorkspacePool(self.store, self.root / 'pool',
                             [RepoSpec('app', self.base, max_slots=3)], owner='agent')
        self.pool.restore('attempt')
        extra = pool.lease('other', 'app')                    # slot-1 (free in setUp)
        newer = pool.lease('newer', 'app')                    # slot-3
        pool.release('other'); pool.release('newer')
        self._prune(pool, newer)
        self.assertEqual(pool.lease('newer-again', 'app', preferred_path=str(newer)), newer)
        self.assertEqual([r['path'] for r in pool._rows('app', 'free')], [str(extra)])

    def test_a_new_slot_does_not_take_the_name_of_a_pruned_slot_somebody_may_come_back_to(self):
        """The suspended attempt's slot was pruned while it was parked. Other work that needs a new slot gets
        the next number, not that path; the attempt's restore then makes its slot again in place."""
        import json
        pool = WorkspacePool(self.store, self.root / 'pool',
                             [RepoSpec('app', self.base, max_slots=5)], owner='agent')
        self.assertEqual(pool.lease('busy', 'app'), self.first)          # slot-1 is taken
        self._prune(pool, self.source)                                    # slot-2: the suspended source, pruned
        fresh = pool.lease('newcomer', 'app')
        self.assertEqual(fresh.name, 'slot-3')
        self.assert_restore(self.source, pool)                            # remade at slot-2
        # the same for a card that left and may return: its pruned slot's name is not reused either
        left = pool.lease('left', 'app')
        self.assertEqual(left.name, 'slot-4')
        pool.release('left')
        self.store.mark('agent', 'task-left', 'ownership_loss', json.dumps({"attempt": "left", "observed_status": "review", "workspaces": [str(left)]}))
        self._prune(pool, left)
        self.assertEqual(pool.lease('other', 'app').name, 'slot-5')

    def test_a_gone_path_outside_the_pool_or_at_the_maximum_is_ignored(self):
        pool = WorkspacePool(self.store, self.root / 'pool',
                             [RepoSpec('app', self.base, max_slots=2)], owner='agent')
        self.pool.restore('attempt')                          # slot-2 busy; the pool is at its maximum
        self.assertEqual(pool.lease('a', 'app', preferred_path=str(self.first.parent / 'slot-9')), self.first)
        pool.release('a')
        roomy = WorkspacePool(self.store, self.root / 'pool',
                              [RepoSpec('app', self.base, max_slots=4)], owner='agent')
        for bad in (str(self.root / 'elsewhere' / 'slot-3'), str(self.first.parent / 'not-a-slot'),
                    str(self.first.parent / 'slot-0')):
            self.assertEqual(roomy.lease('b', 'app', preferred_path=bad), self.first)
            roomy.release('b')

    def test_a_spent_or_foreign_note_keeps_no_slot_aside(self):
        import json
        pool = WorkspacePool(self.store, self.root / 'pool',
                             [RepoSpec('app', self.base, max_slots=3)], owner='agent')
        self.pool.restore('attempt')
        left = pool.lease('left', 'app')
        third = pool.lease('third', 'app')
        pool.release('left'); pool.release('third')
        self.store.mark('agent', 'task-left', 'ownership_loss', '')                       # spent by a return
        self.store.mark('other-agent', 'task-x', 'ownership_loss', json.dumps({"workspaces": [str(left)]}))
        self.store.mark('agent', 'task-bad', 'ownership_loss', 'not json')
        self.store.mark('agent', 'task-done', 'ownership_loss',                     # closed when it left
                        json.dumps({"observed_status": "done", "workspaces": [str(left)]}))
        self.assertEqual(pool._returning_sources(), set())
        self.assertEqual(pool.lease('newcomer', 'app'), left)

    def test_an_answered_decision_request_keeps_its_slot_aside(self):
        """A decision request is closed (done) when answered and reopened by its asker with the next question,
        and that return keeps the session (``decision_request_reopened``): its slot is a returning one. A
        cancelled request and an ordinary closed card name nothing, as before."""
        import json
        pool = WorkspacePool(self.store, self.root / 'pool',
                             [RepoSpec('app', self.base, max_slots=4)], owner='agent')
        self.pool.restore('attempt')
        left = pool.lease('left', 'app')
        pool.release('left')
        self.store.mark('agent', 'task-req', 'ownership_loss',
                        json.dumps({"observed_status": "done", "decision_request": True, "workspaces": [str(left)]}))
        self.assertEqual(pool._returning_sources(), {str(left)})
        newcomer = pool.lease('newcomer', 'app')
        self.assertNotEqual(newcomer, left)
        pool.release('newcomer')
        for status, flag in (("cancelled", True), ("done", False), ("done", "yes")):
            self.store.mark('agent', 'task-req', 'ownership_loss',
                            json.dumps({"observed_status": status, "decision_request": flag, "workspaces": [str(left)]}))
            self.assertEqual(pool._returning_sources(), set(), (status, flag))

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
