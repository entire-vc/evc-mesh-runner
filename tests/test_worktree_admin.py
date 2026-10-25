"""A slot whose .git does not lead back to its own admin dir is held before git runs in it.

Pools of several agents may share one base clone and all name their slots ``slot-<n>``. Git
derives the admin id (``<common>/worktrees/<id>``) from the directory name, so once an admin
dir disappears, the next ``git worktree add`` on that clone may reuse its id; the slot left
behind then resolves into another worktree's HEAD and index while git keeps succeeding.
"""
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fakes import FakeMesh
from test_runner import Bench
from test_workspace import make_repo

from mesh_runner import pool_ops, workspace
from mesh_runner.store import Store
from mesh_runner.workspace import RepoSpec, WorkspaceError, WorkspacePool, git


def admin_of(slot: Path) -> Path:
    """The admin dir the slot's .git file names (whether or not it still exists)."""
    line = (slot / ".git").read_text().strip()
    return Path(os.path.realpath(os.path.join(slot, line[len("gitdir: "):])))


def back_pointer(admin: Path) -> str:
    return os.path.realpath((admin / "gitdir").read_text().strip())


class AdminLink(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.base = make_repo(root)
        self.common = Path(os.path.realpath(self.base / ".git"))
        self.store = Store(root / "s.sqlite")           # one store, like one runner with several agents
        self.a = self.pool("agent-a", root)
        self.b = self.pool("agent-b", root)

    def tearDown(self):
        self.tmp.cleanup()

    def pool(self, owner: str, root: Path | None = None) -> WorkspacePool:
        return WorkspacePool(self.store, (root or Path(self.tmp.name)) / "pool",
                             [RepoSpec("app", self.base, max_slots=3)],
                             identity=("Agent", "agent@example.com"), owner=owner)

    def legacy_slot(self, pool: WorkspacePool, n: int, state: str = "free", attempt: str = "") -> Path:
        """A slot added the way existing pools hold them: admin id = directory name (slot-<n>)."""
        path = pool.root / "app" / f"slot-{n}"
        path.parent.mkdir(parents=True, exist_ok=True)
        git(self.base, "worktree", "add", "--quiet", "--detach", str(path), "origin/main")
        pool._set(path, "app", state, attempt)
        return path

    def row(self, pool: WorkspacePool, path: Path) -> dict:
        return next(r for r in pool.rows() if r["path"] == str(path))

    def shared_admin(self) -> tuple[Path, Path]:
        """Agent A's slot-2 whose admin dir vanished and was reused by agent B's slot-2."""
        self.legacy_slot(self.a, 1)
        a2 = self.legacy_slot(self.a, 2)
        shutil.rmtree(self.common / "worktrees" / "slot-2")      # the admin dir disappears
        b2 = self.legacy_slot(self.b, 2, state="leased", attempt="b-work")   # the freed id is reused
        self.assertEqual(admin_of(a2), admin_of(b2))
        self.assertEqual(back_pointer(admin_of(a2)), os.path.realpath(b2 / ".git"))
        # git in A's slot succeeds, through B's HEAD and index: why status alone cannot catch it
        self.assertEqual(os.path.realpath(git(a2, "rev-parse", "--absolute-git-dir")), str(admin_of(b2)))
        (b2 / "README").write_text("agent b work\n")
        return a2, b2

    # -- detect and hold ------------------------------------------------------------------------
    def test_slot_sharing_another_agents_admin_dir_is_held_not_leased(self):
        a2, b2 = self.shared_admin()
        leases = [self.a.lease("x1", "app"), self.a.lease("x2", "app")]
        self.assertNotIn(a2, leases)
        self.assertEqual(self.row(self.a, a2)["state"], "held")
        self.assertEqual((b2 / "README").read_text(), "agent b work\n")
        self.assertIn("README", git(b2, "status", "--porcelain"))          # B's tree and index untouched

    def test_hold_note_names_the_admin_link(self):
        a2, _ = self.shared_admin()
        reason = self.a.unsafe_reason(a2, self.a.repos["app"])
        self.assertTrue(reason.startswith(workspace.POOL_ADMIN_HOLD_NOTE), reason)
        self.assertIn("belongs to another worktree", reason)
        with self.assertLogs("mesh_runner", "WARNING") as logs:
            self.a.lease("x1", "app")
            self.a.lease("x2", "app")
        self.assertIn(workspace.POOL_ADMIN_HOLD_NOTE, self.row(self.a, a2)["note"])
        self.assertTrue(any(str(a2) in line and "admin listing" in line for line in logs.output), logs.output)

    def test_missing_admin_dir_is_held(self):
        slot = self.a.lease("x1", "app")
        self.a.release("x1")
        shutil.rmtree(admin_of(slot))
        reason = self.a.unsafe_reason(slot, self.a.repos["app"])
        self.assertIn(workspace.POOL_ADMIN_HOLD_NOTE, reason)
        self.assertIn("missing", reason)
        with self.assertLogs("mesh_runner", "WARNING") as logs:
            other = self.a.lease("x2", "app")
        self.assertNotEqual(other, slot)
        self.assertEqual(self.row(self.a, slot)["state"], "held")
        self.assertTrue(any("mtime absent" in line for line in logs.output), logs.output)

    def test_admin_with_foreign_commondir_is_held(self):
        slot = self.a.lease("x1", "app")
        self.a.release("x1")
        (admin_of(slot) / "commondir").write_text(str(Path(self.tmp.name) / "elsewhere") + "\n")
        self.assertIn("does not belong", self.a._admin_reason(slot, self.a.repos["app"]))

    def test_admin_outside_the_base_clone_is_held(self):
        other = make_repo(Path(self.tmp.name) / "second")
        slot = self.a.lease("x1", "app")
        self.a.release("x1")
        foreign = Path(self.tmp.name) / "foreign-wt"
        git(other, "worktree", "add", "--quiet", "--detach", str(foreign), "origin/main")
        shutil.copyfile(foreign / ".git", slot / ".git")
        self.assertIn("is not under", self.a._admin_reason(slot, self.a.repos["app"]))

    def test_suffixed_admin_id_is_valid(self):
        self.legacy_slot(self.a, 1)
        b1 = self.legacy_slot(self.b, 1)           # slot-1 is taken by A: git names B's admin slot-11
        self.assertEqual(admin_of(b1).name, "slot-11")
        self.assertIsNone(self.b._admin_reason(b1, self.b.repos["app"]))
        self.assertEqual(self.b.unsafe_reason(b1, self.b.repos["app"]), "")
        self.assertEqual(self.b.lease("y1", "app"), b1)

    def test_symlinked_dotgit_is_held(self):
        slot = self.a.lease("x1", "app")
        self.a.release("x1")
        real = Path(self.tmp.name) / "dotgit-copy"
        shutil.move(str(slot / ".git"), str(real))
        (slot / ".git").symlink_to(real)
        self.assertIn("symlink", self.a.unsafe_reason(slot, self.a.repos["app"]))
        self.assertNotEqual(self.a.lease("x2", "app"), slot)
        self.assertEqual(self.row(self.a, slot)["state"], "held")

    def test_dangling_dotgit_symlink_is_held_as_an_admin_link(self):
        slot = self.a.lease("x1", "app")
        self.a.release("x1")
        (slot / ".git").unlink()
        (slot / ".git").symlink_to(Path(self.tmp.name) / "no-such-dotgit")
        reason = self.a.unsafe_reason(slot, self.a.repos["app"])
        self.assertIn(workspace.POOL_ADMIN_HOLD_NOTE, reason)
        self.assertNotEqual(reason, "missing")

    def test_malformed_dotgit_is_held(self):
        slot = self.a.lease("x1", "app")
        self.a.release("x1")
        (slot / ".git").write_text((slot / ".git").read_text() + "gitdir: /elsewhere\n")
        self.assertIn("single gitdir line", self.a._admin_reason(slot, self.a.repos["app"]))

    # -- callers that run git in a slot without unsafe_reason ------------------------------------
    def test_suspend_refuses_a_slot_with_a_foreign_admin(self):
        a2, b2 = self.shared_admin()
        self.a._set(a2, "app", "leased", "x")
        with self.assertRaisesRegex(WorkspaceError, "admin link"):
            self.a.suspend("x", {}, writer_stopped=True)
        self.assertEqual(self.row(self.a, a2)["state"], "leased")
        self.assertEqual(self.a._checkpoint_rows("x"), [])
        self.assertEqual((b2 / "README").read_text(), "agent b work\n")
        self.assertIn("README", git(b2, "status", "--porcelain"))

    def test_restore_refuses_a_target_with_a_foreign_admin(self):
        a2, _ = self.shared_admin()
        self.a._set(a2, "app", "leased", "x")
        self.a._checkpoints()
        self.store.db.execute("INSERT INTO workspace_checkpoints VALUES(?,?,?,?,?,?,?)",
                              (self.a.owner, "x", "app", str(Path(self.tmp.name) / "cp"), str(a2),
                               "restoring", str(a2)))
        with self.assertRaisesRegex(WorkspaceError, "restore target .*admin link"):
            self.a.restore("x")

    def test_eviction_quarantines_and_never_removes_a_slot_with_a_foreign_admin(self):
        a2, b2 = self.shared_admin()
        self.assertNotIn(str(a2), pool_ops.evict(self.a, idle_hours=0, keep=0, limit=5))
        pool_ops.evict(self.a, idle_hours=0, keep=0, limit=5, apply=True)
        row = self.row(self.a, a2)
        self.assertEqual((row["state"], row["note"]), ("held", workspace.POOL_ADMIN_HOLD_NOTE))
        self.assertTrue(a2.is_dir())
        self.assertEqual(back_pointer(admin_of(b2)), os.path.realpath(b2 / ".git"))   # B's admin intact

    def symlinked_dotgit_slot(self, dangling: bool) -> Path:
        """A free, idle slot whose .git is a symlink (to a copy of the real file, or to nothing)."""
        slot = self.a.lease("x1", "app")
        self.a.release("x1")
        target = Path(self.tmp.name) / "dotgit-copy"
        shutil.move(str(slot / ".git"), str(target))
        if dangling:
            target.unlink()
        (slot / ".git").symlink_to(target)
        return slot

    def test_eviction_quarantines_a_symlinked_dotgit_and_never_removes_it(self):
        for dangling in (False, True):
            with self.subTest(dangling=dangling):
                self.tearDown()
                self.setUp()
                slot = self.symlinked_dotgit_slot(dangling)
                self.assertEqual(pool_ops.evict(self.a, idle_hours=0, keep=0, limit=5), [])
                real = pool_ops.git
                calls = []

                def spy(cwd, *args, **kwargs):
                    calls.append(args[:2])
                    return real(cwd, *args, **kwargs)
                with patch.object(pool_ops, "git", side_effect=spy):
                    self.assertEqual(pool_ops.evict(self.a, idle_hours=0, keep=0, limit=5, apply=True), [])
                self.assertNotIn(("worktree", "remove"), calls)
                row = self.row(self.a, slot)
                self.assertEqual((row["state"], row["note"]), ("held", workspace.POOL_ADMIN_HOLD_NOTE))
                self.assertTrue(slot.is_dir())
                self.assertTrue((slot / ".git").is_symlink())

    def test_ignored_cleanup_treats_a_symlinked_dotgit_as_an_admin_link(self):
        for dangling in (False, True):
            with self.subTest(dangling=dangling):
                self.tearDown()
                self.setUp()
                self.symlinked_dotgit_slot(dangling)
                res = pool_ops.prune_ignored(self.a, idle_hours=0)
                self.assertEqual(res["skipped"].get("admin link"), 1, res["skipped"])

    def test_audit_holds_a_free_slot_with_a_dangling_dotgit_symlink(self):
        slot = self.symlinked_dotgit_slot(dangling=True)
        with self.assertLogs("mesh_runner", "WARNING"):
            self.assertEqual(self.a.audit_admin(), [str(slot)])
        self.assertEqual(self.row(self.a, slot)["note"], workspace.POOL_ADMIN_HOLD_NOTE)

    def test_audit_leaves_an_absent_dotgit_to_lease_like_unsafe_reason(self):
        slot = self.a.lease("x1", "app")
        self.a.release("x1")
        (slot / ".git").unlink()
        self.assertEqual(self.a.unsafe_reason(slot, self.a.repos["app"]), "missing")
        self.assertEqual(self.a.audit_admin(), [])
        self.assertNotEqual(self.a.lease("x2", "app"), slot)
        self.assertEqual(self.row(self.a, slot)["note"], "found while free: missing")

    def test_suspend_refuses_a_dangling_dotgit_symlink(self):
        slot = self.symlinked_dotgit_slot(dangling=True)
        self.a._set(slot, "app", "leased", "x")
        with self.assertRaisesRegex(WorkspaceError, "admin link"):
            self.a.suspend("x", {}, writer_stopped=True)
        self.assertEqual(self.row(self.a, slot)["state"], "leased")

    def test_ignored_cleanup_skips_a_slot_with_a_foreign_admin(self):
        a2, _ = self.shared_admin()
        (a2 / "cache").mkdir()
        res = pool_ops.prune_ignored(self.a, idle_hours=0)
        self.assertEqual(res["skipped"].get("admin link"), 1)

    # -- periodic audit -------------------------------------------------------------------------
    def test_audit_holds_a_free_slot_once_and_logs_where_to_look(self):
        slot = self.a.lease("x1", "app")
        self.a.release("x1")
        admin = admin_of(slot)
        shutil.rmtree(admin)
        with self.assertLogs("mesh_runner", "WARNING") as logs:
            self.assertEqual(self.a.audit_admin(), [str(slot)])
        row = self.row(self.a, slot)
        self.assertEqual((row["state"], row["note"]), ("held", workspace.POOL_ADMIN_HOLD_NOTE))
        self.assertEqual(len(logs.output), 1)
        self.assertIn(str(admin), logs.output[0])
        self.assertIn(f"admin listing {self.common / 'worktrees'} (mtime 2", logs.output[0])
        with self.assertNoLogs("mesh_runner", "WARNING"):
            self.assertEqual(self.a.audit_admin(), [])
        self.assertEqual(self.a.recheck_held(open_attempts=set()), [])     # needs the operator

    def test_audit_leaves_sound_slots_free(self):
        slot = self.a.lease("x1", "app")
        self.a.release("x1")
        self.legacy_slot(self.a, 2)
        self.assertEqual(self.a.audit_admin(), [])
        self.assertEqual(self.row(self.a, slot)["state"], "free")

    # -- prevent: owner-unique admin ids -------------------------------------------------------
    def test_new_slot_gets_an_owner_prefixed_admin_id(self):
        a1, b1 = self.a.lease("x1", "app"), self.b.lease("y1", "app")
        self.assertEqual((a1.name, b1.name), ("slot-1", "slot-1"))
        self.assertEqual(admin_of(a1).name, "agent-a-slot-1")
        self.assertEqual(admin_of(b1).name, "agent-b-slot-1")
        self.assertEqual(back_pointer(admin_of(a1)), os.path.realpath(a1 / ".git"))
        self.assertEqual(sorted(p.name for p in a1.parent.iterdir()), ["slot-1"])     # no staging left
        self.assertEqual(git(a1, "config", "user.name"), "Agent")
        self.a.release("x1")
        self.assertEqual(self.a.lease("x2", "app"), a1)

    def test_a_vanished_admin_id_is_not_reused_by_another_pool(self):
        self.a.lease("x1", "app")
        a2 = self.a.lease("x2", "app")
        self.a.release("x1"); self.a.release("x2")
        shutil.rmtree(admin_of(a2))
        b1, b2 = self.b.lease("y1", "app"), self.b.lease("y2", "app")
        self.assertEqual({admin_of(b1).name, admin_of(b2).name}, {"agent-b-slot-1", "agent-b-slot-2"})
        self.assertIn("missing", self.a._admin_reason(a2, self.a.repos["app"]))

    def test_owner_is_sanitised_for_the_admin_id(self):
        odd = self.pool("Agent A+1")
        slot = odd.lease("z1", "app")
        self.assertEqual(admin_of(slot).name, "Agent-A-1-slot-1")
        self.assertEqual(odd.unsafe_reason(slot, odd.repos["app"]), "")

    def test_leftover_staging_worktree_is_removed_before_lease(self):
        staging = self.a.root / "app" / "agent-a-slot-1"          # crash between add and move
        staging.parent.mkdir(parents=True)
        git(self.base, "worktree", "add", "--quiet", "--detach", str(staging), "origin/main")
        slot = self.a.lease("x1", "app")
        self.assertEqual(slot.name, "slot-1")
        self.assertFalse(staging.exists())
        self.assertNotIn(os.path.realpath(staging), git(self.base, "worktree", "list", "--porcelain"))
        self.assertEqual(admin_of(slot).name, "agent-a-slot-1")
        self.assertEqual(self.a.unsafe_reason(slot, self.a.repos["app"]), "")

    def test_leftover_staging_worktree_that_is_not_clean_is_kept(self):
        staging = self.a.root / "app" / "agent-a-slot-1"
        staging.parent.mkdir(parents=True)
        git(self.base, "worktree", "add", "--quiet", "--detach", str(staging), "origin/main")
        (staging / "notes.txt").write_text("unexpected\n")
        with self.assertLogs("mesh_runner", "WARNING") as logs:
            slot = self.a.lease("x1", "app")
        self.assertTrue((staging / "notes.txt").exists())
        self.assertTrue(any("leftover staging worktree" in line for line in logs.output), logs.output)
        self.assertEqual(slot.name, "slot-1")
        self.assertTrue(admin_of(slot).name.startswith("agent-a-slot-1"))
        self.assertEqual(self.a.unsafe_reason(slot, self.a.repos["app"]), "")


class MaintenancePass(Bench):
    def test_the_periodic_slot_pass_runs_the_admin_audit(self):
        r = self.runner(FakeMesh([]))
        slot = r.pool.lease("a1", "app")
        r.pool.release("a1")
        shutil.rmtree(admin_of(slot))
        r._last_recheck = -1e9      # monotonic() can be under 600 s on a fresh CI host
        with self.assertLogs("mesh_runner", "WARNING"):
            r.recheck_slots()
        row = next(x for x in r.pool.rows() if x["path"] == str(slot))
        self.assertEqual((row["state"], row["note"]), ("held", workspace.POOL_ADMIN_HOLD_NOTE))


if __name__ == "__main__":
    unittest.main()
