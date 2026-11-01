"""Legacy Git admin collision ids retain exact reciprocal slot ownership."""
import tempfile
import unittest
from pathlib import Path

from mesh_runner.store import Store
from mesh_runner.workspace import RepoSpec, WorkspaceError, WorkspacePool, git
from test_workspace import make_repo


class LegacyAdminCollision(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name).resolve()
        self.base = make_repo(root)
        self.store = Store(root / "state.sqlite")
        self.pool = WorkspacePool(self.store, root / "pool", [RepoSpec("app", self.base)], owner="Garfield")
        physical_owner = root / "volume" / "Garfield"
        physical_owner.mkdir(parents=True)
        self.pool.root.parent.mkdir(parents=True)
        self.pool.root.symlink_to(physical_owner, target_is_directory=True)
        self.recorded = self.pool.root / "app" / "slot-1"
        self.recorded.parent.mkdir(parents=True)
        self.other = root / "Linus" / "app" / "slot-1"
        self.other.parent.mkdir(parents=True)
        # Real Git creates slot-1 for Linus and appends 1 for the colliding Garfield basename.
        git(self.base, "worktree", "add", "--quiet", "--detach", str(self.other), "HEAD")
        git(self.base, "worktree", "add", "--quiet", "--detach", str(self.recorded), "HEAD")
        self.physical = self.recorded.resolve()
        self.admin = Path((self.physical / ".git").read_text().split(": ", 1)[1].strip())
        self.assertEqual(self.admin.name, "slot-11")
        self.pool._set(self.recorded, "app", "leased", "attempt-one")

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def test_real_collision_issues_exact_physical_slot_without_changing_lease(self):
        self.assertEqual(self.pool.issue("attempt-one", str(self.recorded)), str(self.physical))
        self.assertEqual(self.pool.leased_to("attempt-one"), [str(self.recorded)])
        self.assertEqual(Path((self.admin / "gitdir").read_text().strip()).resolve(), self.physical / ".git")

    def test_foreign_attempt_refused(self):
        with self.assertRaisesRegex(WorkspaceError, "not leased to this attempt"):
            self.pool.issue("another-attempt", str(self.recorded))

    def test_stale_lease_refused(self):
        self.pool._set(self.recorded, "app", "free")
        with self.assertRaisesRegex(WorkspaceError, "not leased to this attempt"):
            self.pool.issue("attempt-one", str(self.recorded))

    def test_duplicate_physical_target_refused(self):
        alias = self.pool.root.parent / "another-owner"
        alias.symlink_to(self.pool.root.resolve(), target_is_directory=True)
        self.pool._set(alias / "app" / "slot-1", "app", "leased", "another-attempt")
        with self.assertRaisesRegex(WorkspaceError, "shared with another slot"):
            self.pool.issue("attempt-one", str(self.recorded))

    def test_foreign_backpointer_refused_even_with_matching_legacy_name(self):
        (self.admin / "gitdir").write_text(str(self.other / ".git") + "\n")
        with self.assertRaisesRegex(WorkspaceError, "belongs to another worktree"):
            self.pool.issue("attempt-one", str(self.recorded))

    def test_foreign_commondir_refused(self):
        (self.admin / "commondir").write_text(str(self.other) + "\n")
        with self.assertRaisesRegex(WorkspaceError, "does not belong"):
            self.pool.issue("attempt-one", str(self.recorded))

    def test_nonexact_or_non_numeric_suffix_refused(self):
        original = self.admin
        for name in ("slot-21", "prefix-slot-11", "slot-11-extra", "slot-1x1", "Garfield-slot-11"):
            with self.subTest(name=name):
                renamed = original.with_name(name)
                original.rename(renamed)
                (self.physical / ".git").write_text(f"gitdir: {renamed}\n")
                with self.assertRaisesRegex(WorkspaceError, "not created for this slot"):
                    self.pool.issue("attempt-one", str(self.recorded))
                renamed.rename(original)
                (self.physical / ".git").write_text(f"gitdir: {original}\n")

    def test_second_collision_counter_accepted_with_reciprocal_proof(self):
        renamed = self.admin.with_name("slot-12")
        self.admin.rename(renamed)
        (self.physical / ".git").write_text(f"gitdir: {renamed}\n")
        self.assertEqual(self.pool.issue("attempt-one", str(self.recorded)), str(self.physical))

    def test_physical_target_of_another_owner_refused(self):
        # the alias leads to Linus's slot-1: same basename and legacy-shaped admin, but another owner's tree
        self.pool.root.unlink()
        self.pool.root.symlink_to(self.other.parent.parent, target_is_directory=True)
        with self.assertRaisesRegex(WorkspaceError, "unsafe physical target|belongs to another worktree"):
            self.pool.issue("attempt-one", str(self.recorded))
