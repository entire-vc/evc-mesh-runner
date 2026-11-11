"""The attempt lifecycle table and its enforcement in the store."""
import tempfile
import unittest
from pathlib import Path

from mesh_runner import lifecycle
from mesh_runner.store import ACTIVE, OPEN, Store

# Every state change observed in a production attempt registry (counts are not part of the contract).
OBSERVED = {
    ("aborted", "aborted-notified"): 1,
    ("ready", "running"): 1, ("ready", "starting"): 1, ("ready", "ready"): 1,
    ("ready", "completed"): 1, ("ready", "unknown"): 1, ("ready", "failed"): 1,
    ("running", "waiting"): 1, ("running", "completed"): 1, ("running", "ready"): 1,
    ("running", "running"): 1, ("running", "failed"): 1, ("running", "unknown"): 1,
    ("starting", "running"): 1, ("starting", "aborted"): 1, ("starting", "starting"): 1,
    ("starting", "completed"): 1,
    ("unknown", "ready"): 1,
    ("waiting", "ready"): 1, ("waiting", "completed"): 1, ("waiting", "aborted"): 1,
    ("waiting", "running"): 1,
}


class Table(unittest.TestCase):
    def test_every_change_seen_in_production_is_listed(self):
        for (before, after), count in OBSERVED.items():
            self.assertEqual(lifecycle.check(before, after), lifecycle.OK, (before, after, count))

    def test_the_table_names_every_state_and_only_known_states(self):
        self.assertEqual(set(lifecycle.TRANSITIONS), set(lifecycle.STATES))
        for before, allowed in lifecycle.TRANSITIONS.items():
            self.assertLessEqual(allowed, set(lifecycle.STATES), before)
            self.assertNotIn(before, allowed)                    # an unchanged rewrite needs no entry
        self.assertEqual(set(OPEN), set(lifecycle.LIVE))
        self.assertLessEqual(set(ACTIVE), set(lifecycle.LIVE))

    def test_an_ended_attempt_never_runs_again(self):
        for ended in ("completed", "failed", "aborted-notified"):
            for live in lifecycle.LIVE:
                self.assertEqual(lifecycle.check(ended, live), lifecycle.REFUSED, (ended, live))
        self.assertEqual(lifecycle.check("aborted", "ready"), lifecycle.REFUSED)
        self.assertEqual(lifecycle.check("aborted", "aborted-notified"), lifecycle.OK)
        self.assertEqual(lifecycle.check("completed", "completed"), lifecycle.OK)

    def test_a_change_between_live_states_that_is_not_named_is_unlisted(self):
        self.assertEqual(lifecycle.check("starting", "waiting"), lifecycle.UNLISTED)
        self.assertEqual(lifecycle.check("waiting", "starting"), lifecycle.UNLISTED)
        self.assertEqual(lifecycle.check("running", "starting"), lifecycle.OK)       # a start held after a failed exec
        self.assertEqual(lifecycle.check("from-a-newer-runner", "ready"), lifecycle.UNLISTED)


class Enforcement(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "s.sqlite")
        self.strict = lifecycle.STRICT

    def tearDown(self):
        lifecycle.STRICT = self.strict
        self.store.db.close()
        self.tmp.cleanup()

    def kinds(self, a):
        return [(k, d) for _, k, d in self.store.events(a.id)]

    def test_listed_changes_are_written(self):
        a = self.store.create("builder", "t1", "claude")
        for state in ("running", "waiting", "ready", "running", "completed"):
            self.assertTrue(self.store.update(a, state=state))
        self.assertEqual(self.store.get(a.id).state, "completed")

    def test_a_write_on_an_ended_attempt_is_refused_and_recorded(self):
        a = self.store.create("builder", "t1", "claude")
        self.store.update(a, state="running")
        self.store.update(a, state="completed")
        with self.assertLogs("mesh_runner", level="ERROR"):
            self.assertFalse(self.store.update(a, state="ready", wake_text="again"))
        row = self.store.get(a.id)
        self.assertEqual((row.state, row.wake_text), ("completed", ""))        # nothing of the write landed
        self.assertEqual(a.state, "completed")
        self.assertIn(("refused_transition", "completed->ready"), self.kinds(a))
        self.assertTrue(self.store.update(a, note="still writable"))          # other fields of an ended row are

    def test_an_unlisted_change_is_applied_and_recorded_in_production(self):
        lifecycle.STRICT = False
        a = self.store.create("builder", "t1", "claude")
        with self.assertLogs("mesh_runner", level="WARNING"):
            self.assertTrue(self.store.update(a, state="waiting"))             # starting -> waiting
        self.assertEqual(self.store.get(a.id).state, "waiting")
        self.assertIn(("unlisted_transition", "starting->waiting"), self.kinds(a))

    def test_an_unlisted_change_raises_under_strict(self):
        lifecycle.STRICT = True
        a = self.store.create("builder", "t1", "claude")
        self.store.update(a, state="running", pid=4242, turns=1)
        self.store.update(a, state="waiting", wait={"kind": "card", "target": "abcdef12"})
        with self.assertRaises(lifecycle.UnlistedTransition):
            self.store.update(a, state="starting")
        self.assertEqual(self.store.get(a.id).state, "waiting")

    def test_a_fixture_may_seed_a_never_spawned_attempt_under_strict_only(self):
        lifecycle.STRICT = True
        a = self.store.create("builder", "t1", "claude")
        self.assertTrue(self.store.update(a, state="waiting", wait={"kind": "card", "target": "abcdef12"}))
        self.assertEqual(self.kinds(a)[-1], ("state", "waiting"))                # no unlisted record
        b = self.store.create("builder", "t2", "claude")
        self.store.update(b, pid=4242)                                           # it has a process: not a seed
        with self.assertRaises(lifecycle.UnlistedTransition):
            self.store.update(b, state="waiting")


if __name__ == "__main__":
    unittest.main()
