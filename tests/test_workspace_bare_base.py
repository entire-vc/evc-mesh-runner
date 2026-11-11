"""Real bare bases must remain bare while their linked slots support checkpoints."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mesh_runner.checkpoint import (Checkpoints, _CheckpointListError, _status_entries, fingerprint,
                                    slim_failure_cause)
from mesh_runner.store import Store
from mesh_runner.workspace import RepoSpec, WorkspaceError, WorkspacePool, base_lock, git
from test_workspace import make_repo


class BareBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        source = make_repo(self.root)
        self.base = self.root / "bare.git"
        subprocess.run(["git", "clone", "--bare", "--quiet", str(source), str(self.base)], check=True)
        git(self.base, "config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")
        git(self.base, "config", "user.name", "t")
        git(self.base, "config", "user.email", "t@example.com")
        self.base_identity = git(self.base, "config", "user.name")
        self.store = Store(self.root / "state.sqlite")
        self.addCleanup(self.store.db.close)
        self.pool = WorkspacePool(self.store, self.root / "pool", [RepoSpec("app", self.base, max_slots=2)],
                                  identity=("Agent", "agent@example.com"), owner="agent")

    def assert_worktree(self, path):
        self.assertEqual(git(path, "rev-parse", "--is-bare-repository"), "false")
        self.assertEqual(git(path, "rev-parse", "--is-inside-work-tree"), "true")
        self.assertIn("README", git(path, "ls-files"))
        self.assertEqual(git(path, "status", "--porcelain"), "")
        self.assertEqual(git(self.base, "rev-parse", "--is-bare-repository"), "true")

    def test_new_and_reused_slots_checkpoint_preserves_dirty_state(self):
        first = self.pool.lease("one", "app")
        self.assert_worktree(first)
        second = self.pool.lease("two", "app")
        self.assert_worktree(second)
        second_config = Path(git(second, "rev-parse", "--git-path", "config.worktree"))
        second_before = (second_config.read_bytes(), second_config.stat().st_mode)
        (first / "README").write_text("staged\n")
        git(first, "add", "README")
        (first / "README").write_text("unstaged\n")
        (first / "untracked").write_text("keep me\n")
        os.chmod(first / "untracked", 0o755)
        before = fingerprint(first)
        self.pool.suspend("one", {"session_id": "same-native-session"}, writer_stopped=True)
        paths, manifests = self.pool.restore("one")
        self.assertEqual(fingerprint(Path(paths[0])), before)
        self.assertEqual(manifests[0]["metadata"]["session_id"], "same-native-session")
        self.assertEqual((second_config.read_bytes(), second_config.stat().st_mode), second_before)
        self.assert_worktree(second)
        self.pool.release("two")
        self.assertEqual(self.pool.lease("three", "app"), second)
        self.assert_worktree(second)
        self.assertEqual(git(second, "config", "user.name"), "Agent")
        self.assertEqual(git(self.base, "config", "user.name"), self.base_identity)

    def test_enabling_extension_keeps_existing_foreign_linked_worktree_usable(self):
        other = self.root / "foreign"
        git(self.base, "worktree", "add", "--quiet", "--detach", str(other), "HEAD")
        self.assert_worktree(other)
        (other / "README").write_text("foreign writer\n")
        before = fingerprint(other)
        self.assert_worktree(self.pool.lease("one", "app"))
        self.assertEqual(fingerprint(other), before)
        self.assertEqual(git(other, "rev-parse", "--is-bare-repository"), "false")

    def test_existing_broken_free_slot_is_reused_without_wiping_it(self):
        # Legacy producer enabled the extension but left shared core.bare=true.
        path = self.pool.root / "app" / "slot-1"
        path.parent.mkdir(parents=True)
        git(self.base, "worktree", "add", "--quiet", "--detach", str(path), "HEAD")
        git(self.base, "config", "extensions.worktreeConfig", "true")
        self.pool._set(path, "app", "free")
        self.assertEqual(git(path, "rev-parse", "--is-bare-repository"), "true")
        with self.assertRaises(WorkspaceError):
            git(path, "status", "--porcelain")
        self.assertEqual(self.pool.lease("one", "app"), path)
        self.assert_worktree(path)

    def test_migration_preserves_config_mode_and_main_only_worktree_setting(self):
        git(self.base, "config", "core.worktree", str(self.root / "main-only"))
        config = self.base / "config"
        config.chmod(0o640)
        self.assert_worktree(self.pool.lease("one", "app"))
        self.assertEqual(config.stat().st_mode & 0o777, 0o640)
        self.assertEqual(git(self.base, "config", "--file", str(self.base / "config.worktree"),
                             "--get", "core.worktree"), str(self.root / "main-only"))
        self.assertEqual(git(self.base, "config", "--file", str(config), "--get", "core.worktree",
                             check=False), "")

    def test_external_config_lock_refuses_migration_without_touching_base_or_slot(self):
        path = self.root / "linked"
        git(self.base, "worktree", "add", "--quiet", "--detach", str(path), "HEAD")
        before = (self.base / "config").read_bytes()
        lock = self.base / "config.lock"
        lock.write_bytes(b"another writer")
        with base_lock(self.base), self.assertRaisesRegex(WorkspaceError, "config lock already exists"):
            self.pool._configure(path)
        self.assertEqual(lock.read_bytes(), b"another writer")
        self.assertEqual((self.base / "config").read_bytes(), before)
        self.assert_worktree(path)

    def test_a_new_slot_refused_by_a_config_lock_stays_in_inventory_and_is_reused(self):
        lock = self.base / "config.lock"
        lock.write_bytes(b"another writer")
        with self.assertRaisesRegex(WorkspaceError, "config lock already exists"):
            self.pool.lease("one", "app")
        rows = self.pool._rows("app")
        self.assertEqual([row["state"] for row in rows], ["free"])
        refused = Path(rows[0]["path"])
        self.assertEqual(lock.read_bytes(), b"another writer")
        lock.unlink()
        self.assertEqual(self.pool.lease("one", "app"), refused)       # repaired and reused, no second slot
        self.assert_worktree(refused)
        self.assertEqual([row["state"] for row in self.pool._rows("app")], ["leased"])
        self.assertEqual(sorted(p.name for p in refused.parent.iterdir()), [refused.name])

    def test_broken_bare_base_without_identity_is_safe_for_new_slots(self):
        git(self.base, "config", "extensions.worktreeConfig", "true")
        self.pool.identity = None
        self.assert_worktree(self.pool.lease("one", "app"))

    def test_checkpoint_list_error_reports_bounded_git_cause_without_stderr(self):
        path = self.pool.lease("one", "app")
        # Real legacy failure, isolated to this owned slot.
        git(path, "config", "--worktree", "core.bare", "true")
        with self.assertRaises(WorkspaceError) as caught:
            _status_entries(path)
        self.assertIn("exit=128", str(caught.exception))
        self.assertIn("not-a-work-tree", str(caught.exception))
        self.assertNotIn("never-log-this", str(caught.exception))
        self.assertEqual(slim_failure_cause(caught.exception, "capture-plan"),
                         {"reason": "git-not-a-work-tree", "stage": "capture-plan", "git": "status", "exit": 128})

    def test_unknown_git_error_never_exposes_stderr_or_claims_volume_mismatch(self):
        result = subprocess.CompletedProcess([], 128, b"", b"fatal: token=never-log-this /private/path")
        error = _CheckpointListError("tracked", "ls-files", result)
        self.assertEqual(str(error), "checkpoint: cannot list tracked files: git=ls-files exit=128 reason=git-other")
        self.assertEqual(slim_failure_cause(error, "capture-plan"),
                         {"reason": "git-other", "stage": "capture-plan", "git": "ls-files", "exit": 128})

    def _real_list_error(self):
        # The real Git failure of the incident: `status` in a tree whose config says core.bare=true.
        result = subprocess.run(["git", "-C", str(self.base), "status", "--porcelain"], capture_output=True)
        self.assertEqual(result.returncode, 128)
        return _CheckpointListError("changed", "status", result)

    def test_fallback_log_and_manifest_keep_the_git_exit_code(self):
        source = make_repo(self.root / "fallback")
        (source / "note.txt").write_text("untracked work")
        cp = Checkpoints(self.root / "cp-full")
        with patch.object(Checkpoints, "_overlay_delta", side_effect=self._real_list_error()), \
                self.assertLogs("mesh_runner", "WARNING") as logs:
            path = cp.create("exit-full", source, {"repo": "app"})
        data = cp.read(path)
        self.assertEqual((data["slim_failure_reason"], data["slim_failure_git"], data["slim_failure_exit"]),
                         ("git-not-a-work-tree", "status", 128))
        lines = [line for line in logs.output if "checkpoint slim fallback" in line]
        self.assertEqual(len(lines), 1)
        self.assertIn("reason=git-not-a-work-tree git=status exit=128 outcome=full-form", lines[0])
        self.assertNotIn("fatal", lines[0])
        self.assertNotIn("fatal", (path / "manifest.json").read_text())

    def test_cross_volume_refusal_names_the_git_exit_code(self):
        source = make_repo(self.root / "crossvol")
        (source / "note.txt").write_text("untracked work")
        cp = Checkpoints(self.root / "cp-cross")
        with patch.object(Checkpoints, "_overlay_delta", side_effect=self._real_list_error()), \
                patch("mesh_runner.checkpoint._same_volume", return_value=False), \
                self.assertLogs("mesh_runner", "WARNING") as logs, \
                self.assertRaisesRegex(WorkspaceError, r"/git-not-a-work-tree exit=128\); .*lease retained"):
            cp.create("exit-cross", source, {"repo": "app"})
        self.assertTrue(any("git=status exit=128 outcome=lease-retained" in line for line in logs.output))


def make_bare_base(root: Path) -> Path:
    """A bare base that fetches `origin` the usual way (refs/remotes/origin/*), as the runner's bases do."""
    make_repo(root)                                             # origin.git with one commit on main
    base = root / "bases" / "app.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(base)], check=True)
    git(base, "remote", "add", "origin", str(root / "origin.git"))
    git(base, "fetch", "-q", "origin")
    return base


class BareBaseInstalledRegression(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.base = make_bare_base(self.root)
        self.store = Store(self.root / "s.sqlite")

    def tearDown(self):
        self.tmp.cleanup()

    def pool(self, owner="Ada", identity=("Ada", "ada@example.com")):
        return WorkspacePool(self.store, self.root / "pool" / owner, [RepoSpec("app", self.base, max_slots=3)],
                             identity=identity, owner=owner)

    def assert_work_tree(self, slot: Path):
        self.assertEqual(git(slot, "rev-parse", "--is-inside-work-tree"), "true")
        self.assertEqual(git(slot, "rev-parse", "--is-bare-repository"), "false")
        git(slot, "status", "--porcelain")                      # rc 128 "must be run in a work tree" before the fix
        self.assertIn("README", git(slot, "ls-files"))

    def assert_base_still_bare(self):
        self.assertEqual(git(self.base, "rev-parse", "--is-bare-repository"), "true")
        self.assertEqual(git(self.base, "config", "--file", str(self.base / "config"), "--get", "core.bare",
                             check=False), "")                  # no longer shared with the slots
        self.assertEqual(git(self.base, "config", "--file", str(self.base / "config.worktree"), "--get",
                             "core.bare"), "true")

    def test_a_new_slot_of_a_bare_base_is_a_work_tree_and_the_base_stays_bare(self):
        pool = self.pool()
        first = pool.lease("a1", "app")
        self.assert_work_tree(first)
        self.assert_base_still_bare()
        self.assertEqual(git(first, "config", "user.email"), "ada@example.com")
        second = pool.lease("a2", "app")                        # the next slot and a reused one keep working
        self.assert_work_tree(second)
        pool.release("a1")
        self.assert_work_tree(pool.lease("a3", "app"))
        self.assert_base_still_bare()
        git(self.base, "fetch", "-q", "origin")                 # the base still works as a bare repository

    def test_a_base_left_broken_by_an_earlier_runner_is_repaired_on_the_next_lease(self):
        pool = self.pool()
        slot = pool.lease("a1", "app")
        # the state the earlier runner produced: extension on, core.bare still in the shared config
        git(self.base, "config", "--file", str(self.base / "config"), "core.bare", "true")
        (self.base / "config.worktree").unlink()
        git(slot, "config", "--worktree", "--unset-all", "core.bare")
        broken = subprocess.run(["git", "-C", str(slot), "status", "--porcelain"], capture_output=True, text=True)
        self.assertEqual(broken.returncode, 128, broken.stderr)
        self.assertIn("work tree", broken.stderr)
        other = self.pool("Ben", ("Ben", "ben@example.com")).lease("b1", "app")   # any lease on that base
        self.assert_work_tree(other)
        self.assert_work_tree(slot)                             # the slot that was broken works again, untouched
        self.assert_base_still_bare()

    def test_a_base_without_per_worktree_config_is_left_alone(self):
        slot = self.pool(identity=None).lease("a1", "app")
        self.assert_work_tree(slot)
        self.assertEqual(git(self.base, "config", "--file", str(self.base / "config"), "--get", "core.bare"), "true")
        self.assertFalse((self.base / "config.worktree").exists())

    def test_an_ordinary_non_bare_base_is_left_alone(self):
        base = self.root / "base"                               # the operator clone make_repo created
        pool = WorkspacePool(self.store, self.root / "pool" / "x", [RepoSpec("app2", base, max_slots=2)],
                             identity=("Agent", "agent@example.com"), owner="x")
        slot = pool.lease("a1", "app2")
        self.assert_work_tree(slot)
        self.assertEqual(git(base, "rev-parse", "--is-bare-repository"), "false")
        self.assertFalse((base / ".git" / "config.worktree").exists()
                         and "bare" in (base / ".git" / "config.worktree").read_text())


if __name__ == "__main__":
    unittest.main()
