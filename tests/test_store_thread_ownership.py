"""A lane's Store connection belongs to its agent thread.

Real SQLite on a shared state file, real threads; nothing of the store is mocked. Only the
pool_ops pass bodies are replaced by readers that hold a statement open, the way
prune_checkpoints walks its rows while it works on the file system.
"""
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mesh_runner import pool_ops
from mesh_runner.runner import AgentRunner, Runner
from mesh_runner.store import Store
from mesh_runner.workspace import WorkspacePool


class _State(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "runner.sqlite"
        self.lane = Store(self.path)           # the lane's connection, used by its agent thread
        self.other = Store(self.path)          # another lane of the same runner
        self.a = self.lane.create("lane-a", "task-1", "claude")
        for n in range(2, 6):                  # rows to walk: one row ends its statement at the first fetch
            self.other.create("lane-b", f"task-{n}", "claude")

    def tearDown(self):
        self.lane.db.close()
        self.other.db.close()
        self.tmp.cleanup()


class BusyTimeout(_State):
    def test_update_with_marks_waits_for_a_writer_shorter_than_the_busy_timeout(self):
        held, done = threading.Event(), threading.Event()

        def writer():
            self.other.db.execute("BEGIN IMMEDIATE")
            self.other.mark("lane-b", "task-2", "k", "v")
            held.set()
            time.sleep(0.6)                    # well under the 30 s busy timeout
            self.other.db.execute("COMMIT")
            done.set()

        t = threading.Thread(target=writer)
        t.start()
        self.assertTrue(held.wait(5))
        started = time.monotonic()
        ok = self.lane.update_with_marks(self.a, {"delivery": "1"}, expect="starting", state="running")
        waited = time.monotonic() - started
        t.join(5)
        self.assertTrue(ok)
        self.assertTrue(done.is_set())
        self.assertGreaterEqual(waited, 0.4)   # it waited for the writer instead of failing
        self.assertEqual(self.lane.get(self.a.id).state, "running")
        self.assertEqual(self.lane.mark("lane-a", "task-1", "delivery")[0], "1")

    def test_a_statement_open_on_the_lanes_connection_fails_its_write_at_once(self):
        """The failure mode itself (positive control for the tests below): while a statement is open
        on the same connection, the write fails immediately, the 30 s busy timeout unused."""
        cur = self.lane.db.execute("SELECT id FROM attempts")
        cur.fetchone()
        self.other.mark("lane-b", "task-2", "k", "v")         # another lane commits meanwhile
        started = time.monotonic()
        with self.assertRaisesRegex(sqlite3.OperationalError, "database is locked"):
            self.lane.update_with_marks(self.a, {}, expect="starting", state="running")
        self.assertLess(time.monotonic() - started, 1.0)
        cur.close()
        self.assertTrue(self.lane.update_with_marks(self.a, {}, expect="starting", state="running"))


class CleanupPassConnection(_State):
    def runner(self, pool):
        r = Runner.__new__(Runner)
        r.cfg = SimpleNamespace(checkpoint_gc_grace_hours=6.0, ignored_cleanup=True, ignored_cleanup_idle_hours=24.0)
        r.agents = [SimpleNamespace(agent=SimpleNamespace(name="lane-a"), pool=pool)]
        r._measure_checkpoints = lambda: None
        return r

    def test_the_pass_works_through_its_own_connection_and_closes_it(self):
        pool = WorkspacePool(self.lane, Path(self.tmp.name) / "pool", [], owner="lane-a")
        seen = []

        def record(p, **kw):
            seen.append(p.store)
            self.assertEqual(p.owner, "lane-a")
            p.store.db.execute("SELECT count(*) FROM attempts").fetchone()
            return {"freed_bytes": 0}

        with patch.object(pool_ops, "prune_checkpoints", record), \
                patch.object(pool_ops, "prune_ignored", record), \
                patch.object(pool_ops, "compact_checkpoints", record):
            Runner._prune_ignored_pass(self.runner(pool))
        self.assertEqual(len(seen), 3)
        self.assertEqual(len({id(s) for s in seen}), 1)        # one connection for the whole pass
        self.assertIsNot(seen[0], self.lane)
        self.assertEqual(seen[0].path, self.lane.path)
        self.assertIs(pool.store, self.lane)                   # the lane's pool is untouched
        with self.assertRaises(sqlite3.ProgrammingError):      # closed when the pass ends
            seen[0].db.execute("SELECT 1")

    def test_a_pass_reading_during_a_lane_write_no_longer_fails_the_write(self):
        """The production sequence: the pass walks rows of the lane's pool while
        the lane's agent thread delivers a prompt and another lane has committed meanwhile."""
        pool = WorkspacePool(self.lane, Path(self.tmp.name) / "pool", [], owner="lane-a")
        reading, release = threading.Event(), threading.Event()

        def slow_prune(p, **kw):
            cur = p.store.db.execute("SELECT id FROM attempts")
            cur.fetchone()                     # statement open while the pass works on files
            reading.set()
            release.wait(10)
            cur.fetchall()
            return {"freed_bytes": 0}

        with patch.object(pool_ops, "prune_checkpoints", slow_prune), \
                patch.object(pool_ops, "prune_ignored", return_value={"freed_bytes": 0}), \
                patch.object(pool_ops, "compact_checkpoints", return_value={"freed_bytes": 0}):
            t = threading.Thread(target=Runner._prune_ignored_pass, args=(self.runner(pool),))
            t.start()
            try:
                self.assertTrue(reading.wait(10))
                self.other.mark("lane-b", "task-2", "k", "v")
                ok = self.lane.update_with_marks(self.a, {"delivery": "1"}, expect="starting", state="running")
            finally:
                release.set()
                t.join(10)
        self.assertTrue(ok)
        self.assertEqual(self.lane.get(self.a.id).state, "running")


class GuardJournal(_State):
    def lane_runner(self):
        ar = AgentRunner.__new__(AgentRunner)
        ar.agent = SimpleNamespace(name="lane-a")
        ar.store = self.lane
        ar._quarantine, ar._errors, ar._strikes, ar._checkpoint_quarantine, ar._resume_hold = {}, {}, {}, {}, {}
        return ar

    def deliver(self, a):
        self.lane.update_with_marks(a, {}, expect=a.state, note="delivered")

    def test_a_locked_store_keeps_the_failure_the_steps_own(self):
        ar = self.lane_runner()
        cur = self.lane.db.execute("SELECT id FROM attempts")     # the lane's connection is busy
        cur.fetchone()
        self.other.mark("lane-b", "task-2", "k", "v")
        with self.assertLogs("mesh_runner", "WARNING") as logs:
            ar._guard(self.a, "prompt-delivery", self.deliver)    # must not raise: the tick goes on
        self.assertEqual(ar._errors[(self.a.id, "prompt-delivery")], 1)
        self.assertTrue(any("prompt-delivery" in line and "failed (1)" in line for line in logs.output))
        self.assertTrue(any("not journaled" in line for line in logs.output))
        cur.close()
        ar._guard(self.a, "prompt-delivery", self.deliver)        # the next tick delivers
        self.assertNotIn((self.a.id, "prompt-delivery"), ar._errors)
        self.assertEqual(self.lane.get(self.a.id).note, "delivered")

    def test_repeated_locked_failures_quarantine_the_step_without_raising(self):
        ar = self.lane_runner()
        cur = self.lane.db.execute("SELECT id FROM attempts")
        cur.fetchone()
        for i in range(3):
            self.other.mark("lane-b", "task-2", "k", str(i))
            ar._guard(self.a, "prompt-delivery", self.deliver)
        self.assertIn((self.a.id, "prompt-delivery"), ar._quarantine)
        self.assertEqual(ar._strikes[(self.a.id, "prompt-delivery")], 1)
        cur.close()

    def test_a_journal_that_works_still_records_the_failure(self):
        ar = self.lane_runner()

        def broken(a):
            raise RuntimeError("step broke")

        with self.assertLogs("mesh_runner", "WARNING"):
            ar._guard(self.a, "prompt-delivery", broken)
        self.assertIn(("error:prompt-delivery", "step broke"),
                      [(k, d) for _, k, d in self.lane.events(self.a.id)])


if __name__ == "__main__":
    unittest.main()
