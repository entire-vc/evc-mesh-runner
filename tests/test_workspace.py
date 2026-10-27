import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from mesh_runner.store import Store
from mesh_runner.workspace import RepoSpec, WorkspaceError, WorkspacePool, git


def make_repo(root: Path) -> Path:
    """origin (bare) + an operator clone with one commit on main."""
    origin, base = root / "origin.git", root / "base"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(base)], check=True, capture_output=True)
    git(base, "config", "user.name", "t")
    git(base, "config", "user.email", "t@example.com")
    (base / ".gitignore").write_text("cache/\n")
    (base / "README").write_text("x\n")
    git(base, "add", ".")
    git(base, "commit", "-q", "-m", "init")
    git(base, "push", "-q", "origin", "HEAD:main")
    return base


class Pool(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.base = make_repo(root)
        self.store = Store(root / "s.sqlite")
        self.pool = WorkspacePool(self.store, root / "pool", [RepoSpec("app", self.base, max_slots=2)],
                                  identity=("Agent", "agent@example.com"), owner="agent")

    def tearDown(self):
        self.tmp.cleanup()

    def test_two_attempts_get_different_slots_and_identity_is_per_worktree(self):
        a, b = self.pool.lease("a1", "app"), self.pool.lease("a2", "app")
        self.assertNotEqual(a, b)
        self.assertEqual(git(a, "config", "user.name"), "Agent")
        self.assertEqual(git(self.base, "config", "user.name"), "t")   # operator clone untouched

    def test_chat_slot_is_leased_returned_and_refused_when_the_pool_is_empty(self):
        from mesh_runner.chat_host import ChatRefused, lease_slot, return_slot
        from types import SimpleNamespace as NS
        ar = NS(pool=self.pool, agent=NS(repos=[RepoSpec("app", self.base, max_slots=2)]))
        first = lease_slot(ar, "thread-one")
        self.assertEqual(lease_slot(ar, "thread-one"), first)          # one slot per chat, reused by every turn
        self.assertEqual(self.pool.leased_path("chat-threadone"), first)
        lease_slot(ar, "thread-two")
        with self.assertRaises(ChatRefused) as cm:                     # both slots busy: a third chat is refused
            lease_slot(ar, "thread-three")
        self.assertEqual(cm.exception.code, "chat_workspace_busy")
        return_slot(ar, "thread-one")                                  # the slot goes back, clean
        self.assertIsNone(self.pool.leased_path("chat-threadone"))
        self.assertEqual(lease_slot(ar, "thread-three"), first)        # and is leased again

    def test_chat_slot_with_uncommitted_work_stays_held(self):
        from mesh_runner.chat_host import lease_slot, return_slot
        from types import SimpleNamespace as NS
        ar = NS(pool=self.pool, agent=NS(repos=[RepoSpec("app", self.base, max_slots=2)]))
        path = lease_slot(ar, "thread-one")
        (path / "README").write_text("unsaved\n")
        return_slot(ar, "thread-one")
        self.assertIsNone(self.pool.leased_path("chat-threadone"))
        self.assertEqual((path / "README").read_text(), "unsaved\n")      # never wiped
        self.assertEqual([r["state"] for r in self.pool.rows() if r["path"] == str(path)], ["held"])

    def test_max_slots(self):
        self.pool.lease("a1", "app")
        self.pool.lease("a2", "app")
        with self.assertRaises(WorkspaceError):
            self.pool.lease("a3", "app")

    def test_clean_slot_is_reused_warm(self):
        a = self.pool.lease("a1", "app")
        (a / "cache").mkdir()
        (a / "cache" / "deps").write_text("installed")     # ignored: survives reuse
        self.assertEqual(self.pool.release("a1"), [])
        b = self.pool.lease("a2", "app")
        self.assertEqual(a, b)
        self.assertTrue((b / "cache" / "deps").exists())

    def test_dirty_slot_is_held_not_wiped_and_freed_once_clean(self):
        a = self.pool.lease("a1", "app")
        (a / "README").write_text("changed\n")
        kept = self.pool.release("a1")
        self.assertEqual(kept[0][1], "uncommitted changes")
        self.assertTrue((a / "README").read_text().startswith("changed"))
        self.assertNotEqual(self.pool.lease("a2", "app"), a)
        self.assertEqual(self.pool.recheck_held(open_attempts=set()), [])      # still dirty
        git(a, "checkout", "--", "README")
        self.assertEqual(self.pool.recheck_held(open_attempts=set()), [str(a)])

    def test_untracked_scratch_is_archived_and_does_not_hold_the_slot(self):
        a = self.pool.lease("a1", "app")
        (a / "evidence").mkdir()
        (a / "evidence" / "x.txt").write_text("proof")
        self.assertEqual(self.pool.release("a1"), [])
        self.assertFalse((a / "evidence").exists())
        found = list(self.pool.root.parent.parent.glob("slot-archive/**/evidence/x.txt"))
        self.assertEqual(len(found), 1)
        self.assertEqual(self.pool.lease("a2", "app"), a)

    def test_odd_untracked_names_and_dangling_symlinks_are_archived(self):
        a = self.pool.lease("a1", "app")
        os.symlink("/nonexistent", a / "dang")
        (a / " lead").write_text("x")
        self.assertEqual(self.pool.release("a1"), [])
        arch = list(self.pool.root.parent.parent.glob("slot-archive/*/*"))
        self.assertTrue((arch[0] / " lead").exists() and (arch[0] / "dang").is_symlink())
        b = self.pool.lease("a2", "app")
        os.symlink("/nonexistent", b / "dang")
        self.assertEqual(self.pool.release("a2"), [])                  # second archive does not collide
        self.assertEqual(len(list(self.pool.root.parent.parent.glob("slot-archive/*/*"))), 2)

    def test_unpushed_branch_commits_survive_and_the_slot_is_returned(self):
        a = self.pool.lease("a1", "app")
        git(a, "checkout", "-q", "-b", "feature")
        (a / "f").write_text("1")
        git(a, "add", "f")
        git(a, "commit", "-q", "-m", "local only")
        sha = git(a, "rev-parse", "HEAD")
        self.assertEqual(self.pool.release("a1"), [])
        self.assertEqual(git(self.base, "rev-parse", "feature"), sha)       # branch ref kept the work
        self.assertEqual(self.pool.lease("a2", "app"), a)

    def test_detached_commits_are_saved_under_a_keep_ref(self):
        a = self.pool.lease("a1", "app")
        (a / "f").write_text("1")
        git(a, "add", "f")
        git(a, "commit", "-q", "-m", "detached work")
        sha = git(a, "rev-parse", "HEAD")
        kept = self.pool.release("a1")
        self.assertIn("runner-keep/a1-slot-1", kept[0][1])
        self.assertEqual(git(self.base, "rev-parse", "runner-keep/a1-slot-1"), sha)

    def test_squash_merged_and_deleted_branch_does_not_leak_the_slot(self):
        a = self.pool.lease("a1", "app")
        git(a, "checkout", "-q", "-b", "topic")
        (a / "f").write_text("1")
        git(a, "add", "f")
        git(a, "commit", "-q", "-m", "c")
        git(a, "push", "-q", "origin", "topic")
        self.assertEqual(self.pool.release("a1"), [])
        git(self.base, "push", "-q", "origin", "--delete", "topic")             # merged elsewhere, deleted
        for n in range(3):                                                    # reused again and again
            self.assertEqual(self.pool.lease(f"b{n}", "app"), a)
            self.assertEqual(self.pool.release(f"b{n}"), [])

    def test_pools_of_different_agents_do_not_share_slots(self):
        other = WorkspacePool(self.store, Path(self.tmp.name) / "pool", [RepoSpec("app", self.base)],
                              owner="other")
        a, b = self.pool.lease("a1", "app"), other.lease("o1", "app")
        self.assertNotEqual(a.parent.parent, b.parent.parent)
        self.assertEqual(len(other.rows()), 1)

    def test_branch_is_freed_on_release(self):
        a = self.pool.lease("a1", "app")
        git(a, "checkout", "-q", "-b", "topic")
        (a / "f").write_text("1")
        git(a, "add", "f")
        git(a, "commit", "-q", "-m", "c")
        git(a, "push", "-q", "origin", "topic")
        self.assertEqual(self.pool.release("a1"), [])
        b = self.pool.lease("a2", "app")
        git(b, "checkout", "-q", "topic")      # would fail if still checked out in another worktree



class LinkedWorktreeBase(unittest.TestCase):
    """An operator's base may itself be a linked worktree (.git is a file)."""

    def test_pool_leases_from_a_linked_worktree_and_shares_its_lock(self):
        from mesh_runner.workspace import base_lock
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            clone = make_repo(root)
            linked = root / "linked"
            git(clone, "worktree", "add", "-q", "--detach", str(linked), "HEAD")
            self.assertTrue((linked / ".git").is_file())
            self.assertIs(base_lock(linked), base_lock(clone))     # one repository, one lock
            pool = WorkspacePool(Store(root / "s.sqlite"), root / "pool", [RepoSpec("app", linked)],
                                 identity=("Agent", "agent@example.com"), owner="agent")
            path = pool.lease("a1", "app")
            self.assertEqual((path / "README").read_text(), "x\n")
            self.assertEqual(git(path, "config", "user.email"), "agent@example.com")

if __name__ == "__main__":
    unittest.main()
