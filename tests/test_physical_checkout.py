"""A slot recorded under a symlink alias is issued to the session by its physical path."""
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

from mesh_runner.workspace import WorkspaceError
from test_runner import Bench
from test_workspace_requests import WorkspaceRequests as _Fixture
from fakes import FakeMesh, task


class PhysicalCheckout(Bench):
    parked = _Fixture.parked

    def aliased(self):
        """The owner directory of the pool is a symlink to a real directory of the same name."""
        r0 = self.runner(FakeMesh([task(1, cat='in_progress')]))
        real = Path(os.path.realpath(self.root)) / "volume" / r0.pool.root.name
        real.mkdir(parents=True)
        r0.pool.root.parent.mkdir(parents=True, exist_ok=True)
        r0.pool.root.symlink_to(real)
        r, (a,) = self.parked()
        return r, a, real / "app"

    def test_alias_issues_physical_path_inventory_keeps_recorded(self):
        r, a, real = self.aliased()
        got = r.store.get(a.id)
        recorded = got.workspaces[0]
        self.assertTrue(Path(recorded).parent.parent.is_symlink())
        issued = r.pool.issue(a.id, recorded)
        self.assertEqual(issued, str(real / "slot-1"))
        self.assertNotEqual(issued, recorded)
        self.assertEqual(r.pool.leased_to(a.id), [recorded])      # lease identity unchanged
        self.assertEqual(r._issued_paths(a.id, [recorded]), [issued])

    def test_plain_path_unchanged_without_checks(self):
        r, (a,) = self.parked()
        p = os.path.realpath(r.store.get(a.id).workspaces[0])
        with patch.object(r.pool, "rows", side_effect=AssertionError("no lookup for a plain path")):
            self.assertEqual(r.pool.issue(a.id, p), p)

    def test_foreign_attempt_refused(self):
        r, a, real = self.aliased()
        with self.assertRaisesRegex(WorkspaceError, "not leased to this attempt"):
            r.pool.issue("other-attempt", r.store.get(a.id).workspaces[0])

    def test_unleased_or_unknown_slot_refused(self):
        r, a, real = self.aliased()
        recorded = r.store.get(a.id).workspaces[0]
        r.pool._set(Path(recorded), "app", "held", note="x")
        with self.assertRaises(WorkspaceError):
            r.pool.issue(a.id, recorded)
        with self.assertRaises(WorkspaceError):
            r.pool.issue(a.id, recorded + "-nope")

    def test_stale_admin_link_refused(self):
        r, a, real = self.aliased()
        recorded = r.store.get(a.id).workspaces[0]
        (real / "slot-1" / ".git").write_text("gitdir: /nonexistent/admin\n")
        with self.assertRaises(WorkspaceError):
            r.pool.issue(a.id, recorded)

    def test_shared_physical_target_refused(self):
        r, a, real = self.aliased()
        recorded = r.store.get(a.id).workspaces[0]
        other = str(r.pool.root / "twin")
        os.symlink(real, other)
        r.pool._set(Path(other + "/slot-1"), "app", "free")
        with self.assertRaisesRegex(WorkspaceError, "shared"):
            r.pool.issue(a.id, recorded)

    def test_resume_launch_carries_physical_dirs_and_notice_keeps_state(self):
        r, a, real = self.aliased()
        recorded = r.store.get(a.id).workspaces[0]
        slot = real / "slot-1"
        (slot / "dirty.txt").write_text("keep")
        head = subprocess.check_output(["git", "-C", str(slot), "rev-parse", "HEAD"], text=True)
        seen = {}

        def spawn(launch, log_path):
            seen["prompt"] = launch.prompt
            raise RuntimeError("stop")

        def resume_argv(sid, cwd, add_dirs):
            seen["argv"] = [str(d) for d in add_dirs]
            return ["x"]
        with patch.object(self.adapter, "spawn", side_effect=spawn, create=True), \
                patch.object(self.adapter, "resume_argv", side_effect=resume_argv, create=True):
            with self.assertRaises(Exception):
                r._spawn(r.store.get(a.id), "wake", resume=True, expect=("ready",))
        self.assertIn(str(slot), seen["argv"])
        self.assertNotIn(recorded, seen["argv"])
        self.assertIn(str(slot), seen["prompt"])
        self.assertIn("aliases", seen["prompt"])
        after = r.store.get(a.id)
        self.assertEqual((after.workspaces, after.session_id), ([recorded], a.session_id))
        self.assertEqual((slot / "dirty.txt").read_text(), "keep")
        self.assertEqual(subprocess.check_output(["git", "-C", str(slot), "rev-parse", "HEAD"], text=True), head)

    def test_refused_slot_does_not_launch(self):
        r, a, real = self.aliased()
        (real / "slot-1" / ".git").write_text("gitdir: /nonexistent/admin\n")
        with patch.object(self.adapter, "spawn", side_effect=AssertionError("must not spawn"), create=True):
            self.assertFalse(r._spawn(r.store.get(a.id), "wake", resume=True, expect=("ready",)))
        self.assertEqual(r.store.get(a.id).workspaces, [r.store.get(a.id).workspaces[0]])

    def test_target_recorded_under_another_owner_refused(self):
        r, a, real = self.aliased()
        recorded = r.store.get(a.id).workspaces[0]
        other = str(self.root / "other-owner-view" / "slot-1")
        os.makedirs(os.path.dirname(other))
        os.rmdir(os.path.dirname(other))
        os.symlink(real, os.path.dirname(other))
        r.pool.store.db.execute(
            "INSERT INTO slots(path,pool,state,attempt,note,updated_at) VALUES(?,?,?,?,?,?)",
            (other, "someone-else/app", "leased", "foreign-attempt", "", "2026-10-31T00:00:00Z"))
        with self.assertRaisesRegex(WorkspaceError, "shared"):
            r.pool.issue(a.id, recorded)

    def test_retargeted_alias_to_another_worktree_refused(self):
        r, a, real = self.aliased()
        recorded = r.store.get(a.id).workspaces[0]
        base = r.pool.repos["app"].base
        # a different, perfectly valid worktree of the same repository, laid out at another tail
        elsewhere = Path(os.path.realpath(self.root)) / "elsewhere" / "other-owner"
        (elsewhere / "app").mkdir(parents=True)
        subprocess.check_call(["git", "-C", str(base), "worktree", "add", "-q", "--detach",
                               str(elsewhere / "app" / "slot-1")])
        os.unlink(r.pool.root)
        os.symlink(elsewhere, r.pool.root)
        with self.assertRaisesRegex(WorkspaceError, "unsafe physical target"):
            r.pool.issue(a.id, recorded)

    def test_same_tail_but_foreign_admin_name_refused(self):
        r, a, real = self.aliased()
        recorded = r.store.get(a.id).workspaces[0]
        admin = Path((real / "slot-1" / ".git").read_text().split(": ", 1)[1].strip())
        renamed = admin.with_name("somebody-elses-worktree")
        admin.rename(renamed)
        (real / "slot-1" / ".git").write_text(f"gitdir: {renamed}\n")
        (renamed / "gitdir").write_text(str(real / "slot-1" / ".git") + "\n")
        with self.assertRaisesRegex(WorkspaceError, "not created for this slot"):
            r.pool.issue(a.id, recorded)
