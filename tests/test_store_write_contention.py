"""A CAS-then-write transition must not fail with "database is locked" when another
connection commits between its read and its write (SQLITE_BUSY_SNAPSHOT is raised
at once, without waiting for the busy timeout)."""
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from mesh_runner.store import Store


class ReadHook:
    """Wraps a connection: runs ``hook`` once, right after the CAS read of ``attempts.wait``."""

    def __init__(self, db, hook):
        self._db, self._hook = db, hook

    def __getattr__(self, name):
        return getattr(self._db, name)

    def execute(self, sql, *args):
        cur = self._db.execute(sql, *args)
        if sql.startswith("SELECT wait FROM attempts") and self._hook:
            hook, self._hook = self._hook, None
            hook()
        return cur


class WriteContention(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "runner.sqlite"
        self.store = Store(self.path)
        self.other = Store(self.path)
        self.a = self.store.create("Lane", "t" * 36, "claude")
        self.store.update(self.a, state="waiting", wait={"kind": "ci"})

    def test_concurrent_commit_between_cas_read_and_write(self):
        # another connection tries to commit right after our CAS read of the wait
        writers = []

        def competing_write():
            t = threading.Thread(target=lambda: self.other.event(self.a.id, "noise", "x"))
            t.start()
            t.join(1.0)                 # a deferred transaction lets it through at once
            writers.append(t)

        self.store.db = ReadHook(self.store.db, competing_write)
        ok = self.store.update_with_marks(self.a, {"k": "v"}, expect="waiting", expect_wait={"kind": "ci"},
                                          state="ready", wake_text="go")
        self.store.db = self.store.db._db
        self.assertTrue(ok)
        writers[0].join(10)
        self.assertFalse(writers[0].is_alive())     # the competitor waited for our commit, then ran
        self.assertEqual(self.store.get(self.a.id).state, "ready")
        self.assertEqual(self.store.mark("Lane", self.a.task_id, "k")[0], "v")
        self.assertIn(("noise", "x"), [(k, d) for _, k, d in self.store.events(self.a.id)])

    def test_same_race_fails_on_a_deferred_savepoint(self):
        """Control: documents the failure mode the immediate transaction avoids."""
        db = sqlite3.connect(str(self.path), isolation_level=None, timeout=30, check_same_thread=False)
        db.execute("SAVEPOINT s")
        db.execute("SELECT wait FROM attempts WHERE id=?", (self.a.id,)).fetchone()
        self.other.event(self.a.id, "noise", "x")
        with self.assertRaisesRegex(sqlite3.OperationalError, "database is locked"):
            db.execute("UPDATE attempts SET note='n' WHERE id=?", (self.a.id,))
        db.close()

    def test_waits_for_an_active_writer_instead_of_failing(self):
        self.other.db.execute("BEGIN IMMEDIATE")        # holds the write lock briefly
        self.store.db.execute("PRAGMA busy_timeout=5000")
        timer = threading.Timer(0.3, lambda: self.other.db.execute("COMMIT"))
        timer.start()
        try:
            ok = self.store.update_with_marks(self.a, {}, expect="waiting", expect_wait={"kind": "ci"},
                                              state="ready")
        finally:
            timer.join()
        self.assertTrue(ok)

    def test_failed_cas_commits_nothing_and_releases_the_lock(self):
        ok = self.store.update_with_marks(self.a, {"k": "v"}, expect="waiting", expect_wait={"kind": "other"},
                                          state="ready")
        self.assertFalse(ok)
        self.assertFalse(self.store.db.in_transaction)
        self.assertIsNone(self.store.mark("Lane", self.a.task_id, "k"))
        self.other.event(self.a.id, "after", "x")       # the write lock is free

    def test_failed_validate_rolls_back_and_releases_the_lock(self):
        ok = self.store.update_with_marks(self.a, {"k": "v"}, expect="waiting", validate=lambda: False,
                                          state="ready")
        self.assertFalse(ok)
        self.assertFalse(self.store.db.in_transaction)
        self.assertEqual(self.store.get(self.a.id).state, "waiting")
        self.other.event(self.a.id, "after", "x")

    def test_exception_rolls_back_and_releases_the_lock(self):
        def boom():
            raise RuntimeError("x")
        with self.assertRaises(RuntimeError):
            self.store.update_with_marks(self.a, {"k": "v"}, expect="waiting", validate=boom, state="ready")
        self.assertFalse(self.store.db.in_transaction)
        self.assertEqual(self.store.get(self.a.id).state, "waiting")
        self.other.event(self.a.id, "after", "x")

    def test_joins_an_outer_transaction(self):
        self.store.db.execute("BEGIN IMMEDIATE")
        self.assertTrue(self.store.update_with_marks(self.a, {}, expect="waiting", state="ready"))
        self.assertTrue(self.store.db.in_transaction)    # the caller still owns it
        self.store.db.execute("ROLLBACK")
        self.assertEqual(self.store.get(self.a.id).state, "waiting")


if __name__ == "__main__":
    unittest.main()
