"""A held slot of a finished attempt whose work cannot resume is freed, after its work is saved."""
import contextlib
import datetime as dt
import io
import json
import sqlite3
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fakes import FakeMesh, task
from test_runner import Bench

from mesh_runner import config as config_mod
from mesh_runner import workspace
from mesh_runner.cli import main
from mesh_runner.runner import DEAD_SLOT_PER_TICK
from mesh_runner.store import Store, iso, now
from mesh_runner.workspace import WorkspaceError, WorkspacePool, git

NOTE = "uncommitted changes"


def archive_refs(base: Path) -> list[str]:
    return git(base, "for-each-ref", "--format=%(refname)", "refs/mesh-runner/archive").split()


class DeadSlots(Bench):
    def setUp(self):
        super().setUp()
        self.base = self.agent.repos[0].base

    def hold(self, r, t, *, state="completed", content="dirty edit\n", untracked=True):
        """A finished attempt on ``t`` whose slot was released dirty, so it is held."""
        a = r.store.create(self.agent.name, t["id"], "claude")
        slot = r.pool.lease(a.id, "app")
        (slot / "README").write_text(content)
        (slot / ".gitignore").write_text("cache/\n")
        (slot / "cache").mkdir(exist_ok=True)
        (slot / "cache" / "dep").write_text("ignored")
        if untracked:
            (slot / "notes.txt").write_text("untracked notes\n")
        r.store.update(a, state=state, note="task left the agent's working statuses")
        kept = r.pool.release(a.id)
        self.assertEqual(kept[0][1], NOTE)
        return a, slot

    def row(self, r, slot):
        return next(x for x in r.pool.rows() if x["path"] == str(slot))

    def fresh(self, mesh):
        r = self.runner(mesh)
        r.refresh_me()
        return r

    def assert_freed_with_work_saved(self, r, a, slot):
        row = self.row(r, slot)
        self.assertEqual((row["state"], row["note"]), ("free", ""))
        refs = archive_refs(self.base)
        self.assertEqual(len(refs), 1)
        ref = refs[0]
        self.assertRegex(ref, rf"^refs/mesh-runner/archive/{a.id[:8]}-slot-1-\d{{8}}T\d{{6}}Z$")
        self.assertEqual(git(self.base, "show", f"{ref}:README"), "dirty edit")        # tracked edit
        self.assertEqual(git(self.base, "show", f"{ref}:notes.txt"), "untracked notes")  # untracked file
        tree = git(self.base, "ls-tree", "-r", "--name-only", ref).split()
        self.assertNotIn("cache/dep", tree)                                              # ignored stays out
        self.assertEqual(git(self.base, "rev-parse", f"{ref}^"), git(self.base, "rev-parse", "origin/main"))
        self.assertEqual(git(slot, "status", "--porcelain", "--untracked-files=no"), "")  # slot is clean again
        self.assertTrue((slot / "cache" / "dep").exists())                                # warm cache kept
        self.assertIn("slot-archived", [e[1] for e in r.store.events(a.id)])
        return ref

    # -- freed ----------------------------------------------------------------------------------
    def test_closed_task_frees_the_slot_and_keeps_the_work_on_a_ref(self):
        t = task(1, cat="done")
        r = self.fresh(FakeMesh([t]))
        a, slot = self.hold(r, t)
        self.assertEqual(r.pool.recheck_held(set()), [])          # the existing path never frees it
        with self.assertLogs("mesh_runner", "INFO") as logs:
            self.assertEqual(r.free_dead_slots(), [str(slot)])
        ref = self.assert_freed_with_work_saved(r, a, slot)
        line = next(m for m in logs.output if "returned to the pool" in m)
        for part in (str(slot), a.id[:8], t["id"][:8], ref, "task is done"):
            self.assertIn(part, line)
        self.assertEqual(r.pool.lease("next", "app"), slot)       # the pool is usable again

    def test_cancelled_task_frees_the_slot(self):
        t = task(1, cat="todo", status_category="cancelled")
        r = self.fresh(FakeMesh([t]))
        a, slot = self.hold(r, t, state="failed")
        self.assertEqual(r.free_dead_slots(), [str(slot)])
        self.assert_freed_with_work_saved(r, a, slot)

    def test_deleted_task_frees_the_slot(self):
        t = task(1, cat="in_progress")
        mesh = FakeMesh([t])
        r = self.fresh(mesh)
        a, slot = self.hold(r, t, state="aborted")
        del mesh.tasks[t["id"]]
        self.assertEqual(r.free_dead_slots(), [str(slot)])
        self.assert_freed_with_work_saved(r, a, slot)

    def test_task_reassigned_to_someone_else_frees_the_slot(self):
        t = task(1, cat="in_progress")
        mesh = FakeMesh([t])
        r = self.fresh(mesh)
        a, slot = self.hold(r, t)
        mesh.tasks[t["id"]]["assignee_id"] = "agent-other"
        self.assertEqual(r.free_dead_slots(), [str(slot)])
        self.assert_freed_with_work_saved(r, a, slot)

    def test_old_hold_without_a_newer_attempt_is_freed_by_age(self):
        t = task(1, cat="in_progress")
        r = self.fresh(FakeMesh([t]))
        a, slot = self.hold(r, t)
        r.pool.store.db.execute("UPDATE slots SET updated_at=? WHERE path=?",
                                (iso(now() - dt.timedelta(hours=30)), str(slot)))
        self.assertEqual(r.free_dead_slots(), [str(slot)])
        self.assert_freed_with_work_saved(r, a, slot)

    def test_a_slot_clean_by_now_is_freed_without_a_ref(self):
        t = task(1, cat="done")
        r = self.fresh(FakeMesh([t]))
        a, slot = self.hold(r, t)
        git(slot, "checkout", "--", "README")
        self.assertEqual(r.free_dead_slots(), [str(slot)])
        self.assertEqual(archive_refs(self.base), [])
        self.assertEqual(self.row(r, slot)["state"], "free")

    # -- untouched ------------------------------------------------------------------------------
    def assert_untouched(self, r, slot, content="dirty edit\n"):
        row = self.row(r, slot)
        self.assertEqual((row["state"], row["note"]), ("held", NOTE))
        self.assertEqual((slot / "README").read_text(), content)
        self.assertEqual((slot / "notes.txt").read_text(), "untracked notes\n")
        self.assertEqual(archive_refs(self.base), [])

    def test_task_still_assigned_with_a_waiting_attempt_is_untouched_even_when_old(self):
        t = task(1, cat="in_progress")
        r = self.fresh(FakeMesh([t]))
        a, slot = self.hold(r, t)
        waiting = r.store.create(self.agent.name, t["id"], "claude")
        r.store.update(waiting, state="waiting")
        r.pool.store.db.execute("UPDATE slots SET updated_at=? WHERE path=?",
                                (iso(now() - dt.timedelta(hours=100)), str(slot)))
        self.assertEqual(r.free_dead_slots(), [])
        self.assert_untouched(r, slot)

    def test_task_still_assigned_and_recent_is_untouched(self):
        t = task(1, cat="in_progress")
        r = self.fresh(FakeMesh([t]))
        a, slot = self.hold(r, t)
        self.assertEqual(r.free_dead_slots(), [])
        self.assert_untouched(r, slot)

    def test_review_status_assigned_to_the_agent_is_untouched(self):
        t = task(1, cat="review")
        r = self.fresh(FakeMesh([t]))
        a, slot = self.hold(r, t)
        self.assertEqual(r.free_dead_slots(), [])
        self.assert_untouched(r, slot)

    def test_age_rule_needs_no_newer_attempt_and_can_be_switched_off(self):
        t = task(1, cat="in_progress")
        r = self.fresh(FakeMesh([t]))
        a, slot = self.hold(r, t)
        r.pool.store.db.execute("UPDATE slots SET updated_at=? WHERE path=?",
                                (iso(now() - dt.timedelta(hours=30)), str(slot)))
        time.sleep(0.01)
        newer = r.store.create(self.agent.name, t["id"], "claude")
        r.store.update(newer, state="completed")
        self.assertEqual(r.free_dead_slots(), [])                 # a newer attempt exists
        self.assert_untouched(r, slot)
        self.tearDown(); self.setUp()
        t = task(1, cat="in_progress")
        r = self.fresh(FakeMesh([t]))
        a, slot = self.hold(r, t)
        r.pool.store.db.execute("UPDATE slots SET updated_at=? WHERE path=?",
                                (iso(now() - dt.timedelta(hours=30)), str(slot)))
        self.cfg.dead_slot_hours = 0                              # age rule off
        self.assertEqual(r.free_dead_slots(), [])
        self.assert_untouched(r, slot)

    def test_attempt_of_another_agent_or_unfinished_attempt_is_untouched(self):
        t = task(1, cat="done")
        r = self.fresh(FakeMesh([t]))
        a, slot = self.hold(r, t)
        r.store.update(a, state="ready")                          # not finished
        self.assertEqual(r.free_dead_slots(), [])
        self.assert_untouched(r, slot)

    def test_unreadable_task_leaves_the_slot_for_a_later_pass(self):
        t = task(1, cat="done")
        mesh = FakeMesh([t])
        r = self.fresh(mesh)
        a, slot = self.hold(r, t)
        mesh.broken_tasks.add(t["id"])
        self.assertEqual(r.free_dead_slots(), [])
        self.assert_untouched(r, slot)

    def test_archive_failure_leaves_the_slot_held_and_unchanged(self):
        t = task(1, cat="done")
        r = self.fresh(FakeMesh([t]))
        a, slot = self.hold(r, t)
        real = workspace.git

        def failing(cwd, *args, **kw):
            if args and args[0] == "commit-tree":
                raise WorkspaceError("disk full")
            return real(cwd, *args, **kw)
        with patch.object(workspace, "git", failing), self.assertLogs("mesh_runner", "WARNING") as logs:
            self.assertEqual(r.free_dead_slots(), [])
        self.assertIn("stays held", logs.output[0])
        self.assertIn("disk full", logs.output[0])
        self.assert_untouched(r, slot)

    def test_a_failing_event_write_does_not_strand_the_slot(self):
        t = task(1, cat="done")
        r = self.fresh(FakeMesh([t]))
        a, slot = self.hold(r, t)
        with patch.object(r.store, "event", side_effect=sqlite3.OperationalError("database is locked")), \
                self.assertLogs("mesh_runner", "WARNING") as logs:
            self.assertEqual(r.free_dead_slots(), [str(slot)])
        warn = next(m for m in logs.output if "event was not recorded" in m)
        self.assertIn("database is locked", warn)
        self.assertEqual(len(archive_refs(self.base)), 1)
        self.assertIn(archive_refs(self.base)[0], warn)
        row = self.row(r, slot)
        self.assertEqual((row["state"], row["note"]), ("free", ""))
        self.assertEqual(git(self.base, "show", f"{archive_refs(self.base)[0]}:README"), "dirty edit")

    def test_a_database_error_before_the_ref_leaves_the_slot_held_and_logged(self):
        t = task(1, cat="done")
        r = self.fresh(FakeMesh([t]))
        a, slot = self.hold(r, t)
        with patch.object(r.pool, "_archive_dirty", side_effect=sqlite3.OperationalError("disk I/O error")), \
                self.assertLogs("mesh_runner", "WARNING") as logs:
            self.assertEqual(r.free_dead_slots(), [])
        self.assertIn("stays held", logs.output[0])
        self.assertIn("disk I/O error", logs.output[0])
        self.assert_untouched(r, slot)

    def test_operator_path_converts_a_database_error_and_logs_it(self):
        t = task(1, cat="done")
        r = self.fresh(FakeMesh([t]))
        a, slot = self.hold(r, t)
        with patch.object(r.pool, "_archive_dirty", side_effect=sqlite3.OperationalError("disk I/O error")), \
                self.assertLogs("mesh_runner", "WARNING") as logs:
            out = r.pool.free_path(str(slot), archive=True)
        self.assertTrue(out.startswith("still unsafe: archive failed"))
        self.assertIn("stays held", logs.output[0])
        self.assert_untouched(r, slot)

    def test_ref_that_does_not_hold_the_snapshot_blocks_the_free(self):
        t = task(1, cat="done")
        r = self.fresh(FakeMesh([t]))
        a, slot = self.hold(r, t)
        real = workspace.git

        def lying(cwd, *args, **kw):
            if args and args[0] == "update-ref" and len(args) > 2:       # point the ref at HEAD instead
                return real(cwd, "update-ref", args[1], "HEAD", "", **kw)
            return real(cwd, *args, **kw)
        with patch.object(workspace, "git", lying):
            self.assertEqual(r.free_dead_slots(), [])
        self.assertEqual(self.row(r, slot)["state"], "held")
        self.assertEqual((slot / "README").read_text(), "dirty edit\n")

    def test_slot_with_another_hold_reason_is_not_archived(self):
        t = task(1, cat="done")
        r = self.fresh(FakeMesh([t]))
        a, slot = self.hold(r, t)
        r.pool.store.db.execute("UPDATE slots SET note='index flags require operator inspection' WHERE path=?",
                                (str(slot),))
        self.assertEqual(r.free_dead_slots(), [])
        with self.assertRaises(WorkspaceError):
            r.pool.archive_and_free(str(slot))
        self.assertEqual(archive_refs(self.base), [])
        self.assertEqual((slot / "README").read_text(), "dirty edit\n")

    def test_any_other_hold_note_is_refused_whatever_it_says(self):
        t = task(1, cat="done")
        r = self.fresh(FakeMesh([t]))
        a, slot = self.hold(r, t)
        for note in ("cannot return: something", "found while free: uncommitted changes", "", "custom reason"):
            r.pool.store.db.execute("UPDATE slots SET note=? WHERE path=?", (note, str(slot)))
            with self.assertRaises(WorkspaceError):
                r.pool.archive_and_free(str(slot))
            self.assertIn("another reason", r.pool.free_path(str(slot), archive=True))
            self.assertEqual(self.row(r, slot)["state"], "held")
            self.assertEqual((slot / "README").read_text(), "dirty edit\n")
            self.assertEqual(archive_refs(self.base), [])

    def test_a_refused_slot_costs_no_git_call_and_no_lock(self):
        t = task(1, cat="done")
        r = self.fresh(FakeMesh([t]))
        a, slot = self.hold(r, t)
        leased_attempt = r.store.create(self.agent.name, t["id"], "claude")
        leased = r.pool.lease(leased_attempt.id, "app")
        cases = [(str(slot), a.id + "-other"),                                   # held for another attempt
                 (str(leased), None),                                            # not held
                 (str(self.root / "no-such-slot"), None)]                        # unknown
        for note in ("custom reason", "", "index flags require operator inspection"):
            cases.append((str(slot), None, note))
        for case in cases:
            path, expect, *note = case
            if note:
                r.pool.store.db.execute("UPDATE slots SET note=? WHERE path=?", (note[0], str(slot)))
            workspace._COMMON_DIRS.clear()                                       # a cold lock lookup would run git
            with patch.object(workspace.subprocess, "run") as run, patch.object(workspace, "base_lock") as lock:
                with self.assertRaises(WorkspaceError):
                    r.pool.archive_and_free(path, expect_attempt=expect)
            run.assert_not_called()
            lock.assert_not_called()
        self.assertEqual((slot / "README").read_text(), "dirty edit\n")
        self.assertEqual(archive_refs(self.base), [])

    def test_a_row_that_changes_while_waiting_for_the_lock_is_refused(self):
        t = task(1, cat="done")
        r = self.fresh(FakeMesh([t]))
        a, slot = self.hold(r, t)

        @contextlib.contextmanager
        def racing_lock(base, **kw):
            r.pool.store.db.execute("UPDATE slots SET note='custom reason' WHERE path=?", (str(slot),))
            yield
        with patch.object(workspace, "base_lock", racing_lock):
            with self.assertRaises(WorkspaceError):
                r.pool.archive_and_free(str(slot))
        self.assertEqual(self.row(r, slot)["state"], "held")
        self.assertEqual((slot / "README").read_text(), "dirty edit\n")
        self.assertEqual(archive_refs(self.base), [])

    def test_operator_archive_is_bound_to_the_attempt_it_inspected(self):
        t = task(1, cat="done")
        r = self.fresh(FakeMesh([t]))
        a, slot = self.hold(r, t)
        real = r.pool.archive_and_free
        seen = {}

        def spy(path, expect_attempt=None):
            seen["expect"] = expect_attempt
            return real(path, expect_attempt=expect_attempt)
        with patch.object(r.pool, "archive_and_free", spy):
            r.pool.free_path(str(slot), archive=True)
        self.assertEqual(seen["expect"], a.id)

    def test_a_leased_slot_is_never_archived(self):
        t = task(1, cat="done")
        r = self.fresh(FakeMesh([t]))
        a = r.store.create(self.agent.name, t["id"], "claude")
        slot = r.pool.lease(a.id, "app")
        (slot / "README").write_text("in use\n")
        with self.assertRaises(WorkspaceError):
            r.pool.archive_and_free(str(slot))
        self.assertIn("not held", r.pool.free_path(str(slot), archive=True))
        self.assertEqual((slot / "README").read_text(), "in use\n")
        self.assertEqual(archive_refs(self.base), [])

    # -- pacing ---------------------------------------------------------------------------------
    def test_each_slot_is_looked_at_once_an_hour(self):
        t = task(1, cat="in_progress")
        mesh = FakeMesh([t])
        r = self.fresh(mesh)
        self.hold(r, t)
        reads = []
        real = r._task
        with patch.object(r, "_task", side_effect=lambda tid: reads.append(tid) or real(tid)):
            r.free_dead_slots()
            r.free_dead_slots()
            self.assertEqual(len(reads), 1)
            r._dead_slot_seen.clear()
            r.free_dead_slots()
            self.assertEqual(len(reads), 2)

    def test_a_pass_examines_a_bounded_number_of_slots(self):
        tasks = [task(n, cat="done") for n in range(1, 5)]
        r = self.fresh(FakeMesh(tasks))
        for t in tasks:
            self.hold(r, t)
        self.assertEqual(len(r.free_dead_slots()), DEAD_SLOT_PER_TICK)
        self.assertEqual(len(r.free_dead_slots()), 4 - DEAD_SLOT_PER_TICK)      # the rest on the next pass
        self.assertEqual(len(archive_refs(self.base)), 4)

    def test_the_maintenance_tick_runs_the_pass(self):
        t = task(1, cat="done")
        r = self.fresh(FakeMesh([t]))
        a, slot = self.hold(r, t)
        r._last_recheck = -1e9      # monotonic() can be under 600 s on a fresh CI host
        r.recheck_slots()
        self.assertEqual(self.row(r, slot)["state"], "free")
        self.assertEqual(len(archive_refs(self.base)), 1)

    # -- never pushed ---------------------------------------------------------------------------
    def test_the_archive_ref_is_never_pushed(self):
        t = task(1, cat="done")
        r = self.fresh(FakeMesh([t]))
        calls = []
        real = workspace.git

        def spy(cwd, *args, **kw):
            calls.append(args)
            return real(cwd, *args, **kw)
        a, slot = self.hold(r, t)
        with patch.object(workspace, "git", spy):
            r.free_dead_slots()
        self.assertEqual(len(archive_refs(self.base)), 1)
        self.assertFalse([c for c in calls if "push" in c])
        # no ordinary push form carries it either
        for args in (["push", "origin", "--all"], ["push", "origin", "--tags"], ["push", "origin"]):
            subprocess.run(["git", "-C", str(self.base), *args], capture_output=True)
        remote = git(self.base, "ls-remote", "origin")
        self.assertNotIn("mesh-runner", remote)
        self.assertNotIn("archive", remote)


class FreeSlotArchive(unittest.TestCase):
    def setUp(self):
        from test_workspace import make_repo
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.base = make_repo(self.root)
        self.state = self.root / "state"
        key = self.root / "k"
        key.write_text("secret")
        key.chmod(0o600)
        self.conf = self.root / "runner.toml"
        self.conf.write_text(
            f'[runner]\nmesh_url = "http://unused"\nstate_dir = {json.dumps(str(self.state))}\n'
            f'[[agents]]\nname = "builder"\nkey_file = "{key}"\nprofile_dir = "{self.root}"\n'
            f'[[agents.repos]]\nname = "app"\nbase = {json.dumps(str(self.base))}\n')
        self.cfg = config_mod.load(self.conf)
        self.store = Store(self.state / "runner.sqlite")
        self.pool = WorkspacePool(self.store, self.cfg.pool_root, self.cfg.agents[0].repos, owner="builder")
        self.slot = self.pool.lease("attempt-1", "app")
        (self.slot / "README").write_text("work in progress\n")
        (self.slot / "new.txt").write_text("new\n")
        self.assertEqual(self.pool.release("attempt-1")[0][1], NOTE)

    def tearDown(self):
        self.tmp.cleanup()

    def test_default_keeps_the_safe_refusal(self):
        self.assertEqual(self.pool.free_path(str(self.slot)), "still unsafe: uncommitted changes")
        self.assertEqual(archive_refs(self.base), [])
        self.assertEqual((self.slot / "README").read_text(), "work in progress\n")

    def test_archive_flag_saves_then_frees(self):
        out = self.pool.free_path(str(self.slot), archive=True)
        refs = archive_refs(self.base)
        self.assertEqual(len(refs), 1)
        self.assertIn(refs[0], out)
        self.assertTrue(out.startswith("freed"))
        self.assertEqual(git(self.base, "show", f"{refs[0]}:README"), "work in progress")
        self.assertEqual(git(self.base, "show", f"{refs[0]}:new.txt"), "new")
        self.assertEqual(next(r for r in self.pool.rows())["state"], "free")
        self.assertEqual((self.slot / "README").read_text(), "x\n")

    def test_archive_failure_keeps_the_slot(self):
        with patch.object(self.pool, "_archive_dirty", side_effect=WorkspaceError("no space")):
            out = self.pool.free_path(str(self.slot), archive=True)
        self.assertEqual(out, "still unsafe: no space")
        self.assertEqual(next(r for r in self.pool.rows())["state"], "held")
        self.assertEqual((self.slot / "README").read_text(), "work in progress\n")

    def test_operator_path_logs_like_the_periodic_one(self):
        attempt = self.store.create("builder", "abcdef12-0000-4000-8000-000000000000", "claude")
        self.store.db.execute("UPDATE slots SET attempt=? WHERE path=?", (attempt.id, str(self.slot)))
        with patch.object(self.pool, "_archive_dirty", side_effect=WorkspaceError("no space")), \
                self.assertLogs("mesh_runner", "WARNING") as logs:
            self.pool.free_path(str(self.slot), archive=True)
        line = logs.output[0]
        for part in (str(self.slot), attempt.id[:8], "task #abcdef12", "stays held: no space"):
            self.assertIn(part, line)
        with self.assertLogs("mesh_runner", "INFO") as logs:
            self.pool.free_path(str(self.slot), archive=True)
        ref = archive_refs(self.base)[0]
        line = next(m for m in logs.output if "returned to the pool" in m)
        for part in (str(self.slot), attempt.id[:8], "task #abcdef12", ref):
            self.assertIn(part, line)

    def test_operator_path_without_a_known_attempt_says_task_unknown(self):
        with self.assertLogs("mesh_runner", "INFO") as logs:
            self.pool.free_path(str(self.slot), archive=True)
        self.assertIn("task unknown", next(m for m in logs.output if "returned to the pool" in m))

    def test_command_line_flag(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(main(["free-slot", "--config", str(self.conf), "--path", str(self.slot)]), 0)
        self.assertEqual(out.getvalue().strip(), "still unsafe: uncommitted changes")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(main(["free-slot", "--config", str(self.conf), "--path", str(self.slot),
                                   "--archive"]), 0)
        self.assertTrue(out.getvalue().startswith("freed (uncommitted work saved as refs/mesh-runner/archive/"))
        self.assertEqual(len(archive_refs(self.base)), 1)
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
            main(["slots", "--config", str(self.conf), "--archive"])


class DeadSlotConfig(unittest.TestCase):
    def load(self, extra):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "runner.toml"
            p.write_text(f'[runner]\nmesh_url = "http://mesh"\n{extra}\n')
            return config_mod.load(p)

    def test_default_and_override(self):
        self.assertEqual(self.load("").dead_slot_hours, 24.0)
        self.assertEqual(self.load("dead_slot_hours = 6").dead_slot_hours, 6.0)
        self.assertEqual(self.load("dead_slot_hours = 0").dead_slot_hours, 0.0)

    def test_negative_or_nonfinite_is_refused(self):
        for bad in ("-1", "nan", "inf"):
            with self.assertRaises(config_mod.ConfigError):
                self.load(f"dead_slot_hours = {bad}")


if __name__ == "__main__":
    unittest.main()
