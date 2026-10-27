"""Restoring a checkpoint whose branch is checked out in another worktree of the same base repo.

git refuses `checkout -B <branch>` there. A slot of a live attempt is waited for (a hold, never a
failed resume); a stale runner slot is detached first; any other worktree keeps today's behaviour."""
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mesh_runner.store import Store
from mesh_runner.workspace import BranchInUse, RepoSpec, WorkspaceError, WorkspacePool, base_lock, git
from test_workspace import make_repo
from test_runner import Bench
import test_workspace_requests as requests_fixture

BRANCH = "fix/e9-live-ci-isolation"
META = {"session_id": "s", "wait": {"target": "card"}}


class BranchInUseCases(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.base = make_repo(self.root)
        self.store = Store(self.root / "s.sqlite")
        self.pool = WorkspacePool(self.store, self.root / "pool", [RepoSpec("app", self.base, max_slots=4)],
                                  owner="agent")
        # attempt B worked on BRANCH and was suspended: its slot is free, its checkpoint holds the branch
        self.b = self.attempt("b1", "bbbbbbbb-card", "ready")
        slot = self.pool.lease(self.b.id, "app")
        git(slot, "checkout", "-q", "-b", BRANCH)
        (slot / "README").write_text("work of b\n")
        git(slot, "commit", "-qam", "b work", env=self.ident())
        self.pool.suspend(self.b.id, META, writer_stopped=True)

    def tearDown(self):
        self.tmp.cleanup()

    def ident(self):
        import os
        return {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e.x",
                "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e.x"}

    def attempt(self, attempt_id, task_id, state):
        a = self.store.create("agent", task_id, "claude")
        self.store.db.execute("UPDATE attempts SET id=?, state=? WHERE id=?", (attempt_id, state, a.id))
        return self.store.get(attempt_id)

    def holder(self, attempt_id, task_id, state="running"):
        """An attempt A that holds a leased slot with BRANCH checked out."""
        a = self.attempt(attempt_id, task_id, state)
        slot = self.pool.lease(a.id, "app")
        git(slot, "checkout", "-q", BRANCH)
        return a, slot

    def test_branch_in_live_slot_is_a_hold_not_a_failure(self):
        a, slot = self.holder("a1", "aaaaaaaa-card")
        before = {r["path"] for r in self.pool.rows()}
        with self.assertRaises(BranchInUse) as cm:
            self.pool.restore(self.b.id)
        e = cm.exception
        self.assertTrue(e.live)
        self.assertEqual(e.holder_attempt, "a1")
        self.assertEqual(str(e), f"branch {BRANCH} in use by #aaaaaaaa ({slot.name})")
        # nothing was leased or replayed for the waiting attempt
        self.assertEqual({r["path"] for r in self.pool.rows()}, before)
        self.assertEqual(self.pool.leased_to(self.b.id), [])
        self.assertEqual([r["phase"] for r in self.pool._checkpoint_rows(self.b.id)], ["released"])
        self.assertTrue(self.pool.attempt_holds_slot("a1", str(slot)))
        # the holder ends: the same restore now goes through
        self.store.update(a, state="completed")
        self.pool.release("a1")
        self.assertFalse(self.pool.attempt_holds_slot("a1", str(slot)))
        paths, _ = self.pool.restore(self.b.id)
        self.assertEqual(git(Path(paths[0]), "symbolic-ref", "--short", "HEAD"), BRANCH)
        self.assertEqual((Path(paths[0]) / "README").read_text(), "work of b\n")

    def test_stale_clean_slot_is_detached_then_restore_proceeds(self):
        # a free slot that kept the branch checked out (left behind)
        a, slot = self.holder("a1", "aaaaaaaa-card", state="completed")
        self.pool._set(slot, "app", "free")
        paths, _ = self.pool.restore(self.b.id)
        self.assertEqual(git(Path(paths[0]), "symbolic-ref", "--short", "HEAD"), BRANCH)
        # the freed slot is the one leased again; the branch is checked out exactly once
        holders = [p for p, ref in self.pool._worktree_branches(self.pool.repos["app"])
                   if ref == f"refs/heads/{BRANCH}"]
        self.assertEqual([p.resolve() for p in holders], [Path(paths[0]).resolve()])

    def test_leased_by_a_finished_attempt_counts_as_stale(self):
        a, slot = self.holder("a1", "aaaaaaaa-card", state="completed")
        paths, _ = self.pool.restore(self.b.id)
        self.assertEqual(git(Path(paths[0]), "symbolic-ref", "--short", "HEAD"), BRANCH)
        self.assertEqual(git(slot, "rev-parse", "--abbrev-ref", "HEAD"), "HEAD")

    def test_stale_slot_with_uncommitted_changes_is_held_not_touched(self):
        a, slot = self.holder("a1", "aaaaaaaa-card", state="completed")
        self.pool._set(slot, "app", "free")
        (slot / "README").write_text("unsaved edit\n")
        with self.assertRaises(BranchInUse) as cm:
            self.pool.restore(self.b.id)
        self.assertFalse(cm.exception.live)
        self.assertEqual((slot / "README").read_text(), "unsaved edit\n")
        self.assertEqual(git(slot, "symbolic-ref", "--short", "HEAD"), BRANCH)        # still on the branch
        row = next(r for r in self.pool.rows() if r["path"] == str(slot))
        self.assertEqual(row["state"], "held")
        self.assertIn("uncommitted changes", row["note"])
        self.assertEqual(self.pool.leased_to(self.b.id), [])

    def test_other_worktree_keeps_todays_behaviour_and_is_logged(self):
        other = self.root / "elsewhere"
        git(self.base, "worktree", "add", "-q", str(other), BRANCH)
        with self.assertLogs("mesh_runner", "WARNING") as logs:
            with self.assertRaises(WorkspaceError) as cm:
                self.pool.restore(self.b.id)
        self.assertNotIsInstance(cm.exception, BranchInUse)
        self.assertIn("already used by worktree", str(cm.exception))
        self.assertTrue(any(str(other) in line and "not a runner slot" in line for line in logs.output))

    def test_branch_is_settled_under_the_lock_the_lease_and_checkout_use(self):
        real, held = self.pool._settle_branch, []
        def spy(*args, **kw):
            held.append(base_lock(self.base)._is_owned())
            return real(*args, **kw)
        with mock.patch.object(self.pool, "_settle_branch", side_effect=spy):
            self.pool.restore(self.b.id)
        self.assertEqual(held, [True, True])        # the early all-repos pass and the one before the lease

    def test_failed_worktree_listing_fails_closed_before_any_lease(self):
        real = git
        def broken(cwd, *args, **kw):
            if args[:2] == ("worktree", "list"):
                raise WorkspaceError("git worktree list: boom")
            return real(cwd, *args, **kw)
        with mock.patch("mesh_runner.workspace.git", side_effect=broken):
            with self.assertRaises(WorkspaceError) as cm:
                self.pool.restore(self.b.id)
        self.assertNotIsInstance(cm.exception, BranchInUse)
        self.assertEqual(self.pool.leased_to(self.b.id), [])
        self.assertEqual([r["phase"] for r in self.pool._checkpoint_rows(self.b.id)], ["released"])

    def test_restore_of_a_branch_nobody_holds_is_unchanged(self):
        paths, _ = self.pool.restore(self.b.id)
        self.assertEqual(git(Path(paths[0]), "symbolic-ref", "--short", "HEAD"), BRANCH)


class ResumeHold(Bench):
    """The runner: a resume blocked by a live holder is held without a failure and wakes with the holder."""
    parked = requests_fixture.WorkspaceRequests.parked

    def setUp(self):
        super().setUp()
        self.r, (self.a1, self.a2) = self.parked(2)
        r = self.r
        b_slot = Path(self.a2.workspaces[0])
        git(b_slot, "checkout", "-q", "-b", BRANCH)
        r.pool.suspend(self.a2.id, {"session_id": "s", "harness": "claude", "workspaces": self.a2.workspaces},
                       writer_stopped=True)
        r.store.update(self.a2, state="ready")
        git(Path(self.a1.workspaces[0]), "checkout", "-q", BRANCH)      # attempt A holds the branch

    def test_resume_waits_without_counting_failures_and_wakes_with_the_holder(self):
        r = self.r
        real = r.pool.restore
        with mock.patch.object(r.pool, "restore", wraps=real) as restore, \
                self.assertLogs("mesh_runner", "INFO") as logs:
            for _ in range(6):
                r.resume_ready(5)
            self.assertEqual(restore.call_count, 1)            # one try; the other ticks are a cheap store check
            self.assertEqual(r._errors, {})
            self.assertFalse(r._quarantine)
            self.assertEqual(r.store.get(self.a2.id).state, "ready")
            self.assertIn(self.a2.id, r._branch_wait)
            self.assertFalse(any("failed (" in line for line in logs.output))
            held = [line for line in logs.output if "held: branch" in line]
            self.assertEqual(len(held), 1)
            self.assertIn(f"in use by #{self.a1.task8} (slot-1)", held[0])
            # the holder ends and returns its slot: the next tick restores
            r.store.update(self.a1, state="completed")
            r.pool.release(self.a1.id)
            self.assertFalse(r._branch_blocked(r.store.get(self.a2.id)))
            r.resume_ready(5)
            self.assertEqual(restore.call_count, 2)
            self.assertNotIn(self.a2.id, r._branch_wait)
            self.assertEqual(r._errors, {})
        self.settle_all(r)


if __name__ == "__main__":
    unittest.main()
