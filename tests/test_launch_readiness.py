"""Local readiness observations and the real admission/process boundary."""
import errno
import json
import shutil
import subprocess
import os
import shlex
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from fakes import FakeAdapter, FakeMesh, calls, task, write_replies, next_turn
from test_runner import Bench
from test_workspace import make_repo
from mesh_runner.workspace import RepoSpec, WorkspaceError


class ObservationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def executable(self, name, content):
        p = self.root / name
        p.write_bytes(content)
        p.chmod(0o700)
        return p

    def check(self, name, path=None, cwd=None):
        from mesh_runner.launch_readiness import check
        return check(str(name), self.root if cwd is None else cwd,
                     {"PATH": str(self.root) if path is None else path})

    def test_real_native_binary_is_not_decoded_as_text(self):
        self.assertEqual(self.check(sys.executable).state, "exec-required")

    def test_missing_binary_and_changed_fingerprint_after_repair(self):
        first = self.check("tool")
        self.assertEqual((first.state, first.reason), ("deny", "missing-executable"))
        self.assertEqual(first, self.check("tool"))
        (self.root / "tool").symlink_to(sys.executable)
        second = self.check("tool")
        self.assertEqual(second.state, "exec-required")
        self.assertNotEqual(first.fingerprint, second.fingerprint)

    def test_execute_permission_is_required(self):
        p = self.executable("tool", b"#!/bin/sh\nexit 0\n")
        p.chmod(0o600)
        self.assertEqual(self.check(p).state, "deny")

    def test_working_directory_searchability_is_independently_observed_and_fingerprinted(self):
        binary = sys.executable
        missing = self.root / "missing-cwd"
        missing_observation = self.check(binary, cwd=missing)
        self.assertEqual((missing_observation.state, missing_observation.reason),
                         ("deny", "missing-working-directory"))

        file_cwd = self.root / "not-a-directory"
        file_cwd.write_text("fixture\n")
        file_observation = self.check(binary, cwd=file_cwd)
        self.assertEqual((file_observation.state, file_observation.reason),
                         ("deny", "not-directory-working-directory"))

        closed_cwd = self.root / "unsearchable"
        closed_cwd.mkdir()
        closed_cwd.chmod(0o600)
        # CI may run as root, which can search a directory despite missing x bits.
        # Readiness follows the same effective access check as the child process.
        expected_closed = (("exec-required", "native-exec-required")
                           if os.access(closed_cwd, os.X_OK)
                           else ("deny", "unsearchable-working-directory"))
        try:
            closed_observation = self.check(binary, cwd=closed_cwd)
            self.assertEqual((closed_observation.state, closed_observation.reason),
                             expected_closed)
        finally:
            closed_cwd.chmod(0o700)

        self.assertEqual(len({missing_observation.fingerprint, file_observation.fingerprint,
                              closed_observation.fingerprint}), 3)


    def test_executable_text_without_kernel_shebang_is_not_ready(self):
        self.assertEqual(self.check(self.executable("tool", b"exit 0\n")).state, "unknown")

    def test_env_uses_launch_path_not_observer_path(self):
        script = self.executable("tool", b"#!/usr/bin/env node\n")
        self.assertEqual(self.check(script).reason, "missing-interpreter")
        (self.root / "node").symlink_to(sys.executable)
        self.assertEqual(self.check(script).state, "ready")
        self.assertEqual(self.check(script, path="/no-such-path").state, "deny")

    def test_relative_path_entries_resolve_from_child_cwd(self):
        (self.root / "bin").mkdir()
        (self.root / "bin" / "tool").symlink_to(sys.executable)
        self.assertEqual(self.check("tool", path="bin").state, "exec-required")

    def test_path_search_skips_regular_file_prefix_before_valid_executable(self):
        prefix = self.root / "prefix-file"
        prefix.write_text("not a directory\n")
        (self.root / "bin").mkdir()
        (self.root / "bin" / "tool").symlink_to(sys.executable)
        observation = self.check("tool", path=os.pathsep.join((str(prefix), str(self.root / "bin"))))
        self.assertEqual((observation.state, observation.reason), ("exec-required", "native-exec-required"))

    def test_path_search_skips_unsearchable_prefix_before_valid_executable(self):
        blocked = self.root / "blocked"
        blocked.mkdir()
        blocked.chmod(0)
        self.addCleanup(blocked.chmod, 0o700)
        if os.access(blocked, os.X_OK):
            self.skipTest("filesystem permissions are bypassed for this test user")
        (self.root / "bin").mkdir()
        (self.root / "bin" / "tool").symlink_to(sys.executable)
        observation = self.check("tool", path=os.pathsep.join((str(blocked), str(self.root / "bin"))))
        self.assertEqual((observation.state, observation.reason), ("exec-required", "native-exec-required"))

    def test_path_search_reports_permission_failure_when_no_later_match(self):
        blocked = self.root / "blocked"
        blocked.mkdir()
        blocked.chmod(0)
        self.addCleanup(blocked.chmod, 0o700)
        if os.access(blocked, os.X_OK):
            self.skipTest("filesystem permissions are bypassed for this test user")
        observation = self.check("tool", path=str(blocked))
        self.assertEqual((observation.state, observation.reason),
                         ("deny", "executable-search-permission-denied"))

    def test_shebang_only_space_and_tab_separate_interpreter(self):
        for separator in (b"\v", b"\f", b"\xc2\xa0", b"\xe2\x80\x83"):
            for line in (b"/bin/sh" + separator + b"-e",
                         separator + b"/bin/sh",
                         b"/bin/sh" + separator,
                         b"/usr/bin/env" + separator + b"sh"):
                with self.subTest(line=line):
                    p = self.executable("unsupported-space", b"#!" + line + b"\nexit 0\n")
                    self.assertEqual(self.check(p).state, "unknown")
        for line in (b" /bin/sh -e ", b"\t/bin/sh\t-e\t",
                     b"/usr/bin/env\tsh"):
            with self.subTest(line=line):
                p = self.executable("supported-space", b"#!" + line + b"\nexit 0\n")
                self.assertEqual(self.check(p, path="/bin:/usr/bin").state,
                                 "ready" if b"/usr/bin/env" in line else "exec-required")

    def test_kernel_interpreter_argument_is_not_a_second_executable(self):
        p = self.executable("tool", b"#!/bin/sh -e\nexit 0\n")
        self.assertEqual(self.check(p).state, "exec-required")

    def test_unsupported_env_semantics_are_unknown(self):
        for suffix in (b"-S node --flag", b"node --flag", b"NODE=x node", b"-i node", b""):
            with self.subTest(suffix=suffix):
                p = self.executable("tool", b"#!/usr/bin/env " + suffix + b"\n")
                self.assertEqual(self.check(p).state, "unknown")

    def test_invalid_shebang_is_unknown(self):
        for line in (b"#!relative\n", b"#!/bin/sh\r\n", b"#!/bin/sh\x00\n",
                     b"#!/bin/sh", b"#!/" + b"x" * 4096 + b"\n"):
            with self.subTest(line=line[:32]):
                self.assertEqual(self.check(self.executable("tool", line)).state, "unknown")

    def test_nested_missing_interpreter_and_cycle(self):
        p = self.executable("tool", b"#!/not-present-interpreter\n")
        self.assertEqual(self.check(p).reason, "missing-interpreter")
        p.write_text("#!" + str(p) + "\n")
        self.assertEqual(self.check(p).reason, "interpreter-cycle")


class IntendedExecTests(unittest.TestCase):
    def test_execute_only_compiled_binary_uses_only_intended_launch(self):
        from mesh_runner.adapters.base import Adapter, Launch
        from mesh_runner.launch_readiness import check
        compiler = shutil.which("cc") or shutil.which("clang")
        if not compiler:
            self.skipTest("native C compiler unavailable")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, binary = root / "main.c", root / "native"
            source.write_text("#include <stdio.h>\nint main(void) { return getchar() == 'h' ? 0 : 3; }\n")
            subprocess.run([compiler, str(source), "-o", str(binary)], check=True, capture_output=True)
            binary.chmod(0o111)
            if os.geteuid() != 0:
                self.assertFalse(os.access(binary, os.R_OK))
            # Spy only at the OS process boundary; every exec remains real.
            with patch("mesh_runner.adapters.base.subprocess.Popen", wraps=subprocess.Popen) as intended:
                observation = check(str(binary), root, {})
                self.assertEqual(observation.state, "exec-required")
                self.assertEqual(intended.call_count, 0)
                proc = Adapter(sys.executable).spawn(Launch([str(binary)], root, {}, "hello"), root / "out")
                self.assertEqual(proc.wait(timeout=10), 0)
                self.assertEqual(intended.call_count, 1)

    def test_malformed_and_foreign_magic_fail_at_intended_popen(self):
        from mesh_runner.adapters.base import Adapter, Launch, LaunchExecError
        from mesh_runner.launch_readiness import check
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for magic in (b"\x7fELF", b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf"):
                with self.subTest(magic=magic):
                    binary = root / "bad"
                    binary.write_bytes(magic)
                    binary.chmod(0o700)
                    self.assertEqual(check(str(binary), root, {}).state, "exec-required")
                    with self.assertRaises(LaunchExecError) as caught:
                        Adapter(sys.executable).spawn(Launch([str(binary)], root, {}, "not delivered"), root / "out")
                    self.assertIn(caught.exception.errno,
                                  {errno.ENOEXEC, getattr(errno, "EBADARCH", errno.ENOEXEC)})

    def test_log_open_error_is_not_reported_as_exec_failure(self):
        from mesh_runner.adapters.base import Adapter, Launch, LaunchExecError
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            blocked = root / "directory"
            blocked.mkdir()
            with self.assertRaises(IsADirectoryError):
                Adapter(sys.executable).spawn(Launch([sys.executable], root, {}, "unused"), blocked)

    def test_unreadable_additional_binary_does_not_get_primary_exec_bypass(self):
        from mesh_runner.launch_readiness import check
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binary = root / "wrapper"
            binary.write_bytes(b"\x7fELF")
            binary.chmod(0o111)
            if os.access(binary, os.R_OK):
                self.skipTest("root bypasses read permissions")
            observation = check(sys.executable, root, {}, additional_binaries=(str(binary),))
            self.assertEqual((observation.state, observation.reason),
                             ("unknown", "executable-observation-unavailable"))


class InteractiveReadinessTests(Bench):
    def test_actual_driver_and_wrapped_harness_are_both_checked_without_spawn(self):
        from mesh_runner.interactive import InteractiveAdapter

        t = task(1)
        runner = self.runner(FakeMesh([t]))
        harness = self.root / "harness"
        harness.symlink_to(sys.executable)
        self.adapter.binary = str(harness)
        runner.adapter = InteractiveAdapter(self.adapter, "builder", runner.cfg.state_dir,
                                            python=str(self.root / "missing-python"))
        attempt = runner.store.create(runner.agent.name, t["id"], "claude")
        self.assertFalse(runner._launch_ready(attempt, {"PATH": str(self.root)}, ("starting",)))
        saved = runner.store.mark(runner.agent.name, attempt.task_id, "launch_readiness_hold")
        self.assertEqual(json.loads(saved[0])["reason"], "missing-executable")
        self.assertEqual(runner.procs, {})
        self.assertEqual(calls(self.calls), [])

        runner.adapter.python = sys.executable
        runner.adapter.base.binary = str(self.root / "missing-harness")
        attempt = runner.store.get(attempt.id)
        self.assertFalse(runner._launch_ready(attempt, {"PATH": str(self.root)}, ("ready",)))
        saved = runner.store.mark(runner.agent.name, attempt.task_id, "launch_readiness_hold")
        self.assertEqual(json.loads(saved[0])["reason"], "missing-wrapper")
        self.assertEqual(runner.procs, {})
        self.assertEqual(calls(self.calls), [])



class AdmissionReadiness(Bench):
    def setUp(self):
        super().setUp()

        class ExecutableFixture(FakeAdapter):
            # Like production adapters, argv[0] is the configured executable.
            spawn_calls = 0
            before_exec = None

            def spawn(self, launch, log_path):
                self.spawn_calls += 1
                if self.before_exec:
                    self.before_exec(launch)
                return super().spawn(launch, log_path)

            def start_argv(self, *args):
                return [self.binary, *super().start_argv(*args)[1:]]

            def resume_argv(self, *args):
                return [self.binary, *super().resume_argv(*args)[1:]]

        self.adapter = ExecutableFixture(self.adapter.script)

    def runner(self, mesh, gitlab=None):
        r = super().runner(mesh, gitlab)
        r.refresh_me()
        return r

    def held_multi_repo_attempt(self, mesh, repo_names, *, retained_repo_names=None):
        """Build real Store/worktree state with one dirty held repo and retained leases."""
        app_base = self.agent.repos[0].base
        repos = [RepoSpec("app", app_base, max_slots=2)]
        for name in repo_names[1:]:
            root = self.root / f"{name}-source"
            root.mkdir()
            repos.append(RepoSpec(name, make_repo(root), max_slots=2))
        self.agent.repos = repos
        r = self.runner(mesh)
        t = task(1)
        a = r.store.create(self.agent.name, t["id"], "claude")
        held = r.pool.lease(a.id, "app")
        (held / "README").write_text("preserve held progress\n")
        names_to_lease = (repo_names[1:] if retained_repo_names is None
                          else retained_repo_names)
        retained = [r.pool.lease(a.id, name) for name in names_to_lease]
        held_path = str(held)
        r.pool.release(a.id, paths=[held_path])
        self.assertTrue(any(row["path"] == held_path and row["state"] == "held"
                            for row in r.pool.rows()))
        r.store.update(a, state="ready", workspaces=[held_path, *(str(path) for path in retained)])
        return r, t, r.store.get(a.id), held, retained

    def cold_metadata_attempt(self):
        from mesh_runner.workspace_scope import METADATA_ONLY
        t = task(1, labels=["decision-request", METADATA_ONLY])
        mesh = FakeMesh([t])
        self.adapter.binary = str(self.root / "missing")
        r = self.runner(mesh)
        self.assertFalse(r._start_one(t))
        a, = r.store.by_state(self.agent.name, ("ready",))
        self.assertEqual((a.workspaces, a.turns, a.checkout), ([], 0, {}))
        return t, mesh, r, a

    def test_cold_metadata_repair_restart_keeps_read_only_without_writable_repo(self):
        from mesh_runner.workspace_scope import RECEIPT_KEY, recorded_scope
        t, mesh, r, a = self.cold_metadata_attempt()
        scope = r.store.mark(self.agent.name, t["id"], RECEIPT_KEY)
        self.assertIsNotNone(scope)
        self.assertEqual(recorded_scope(scope[0], a.id), "metadata-only")
        self.adapter.binary = sys.executable
        restarted = self.runner(mesh)
        seen = []
        spawn = self.adapter.spawn
        def capture(launch, log_path):
            seen.append(list(launch.argv))
            return spawn(launch, log_path)
        self.adapter.spawn = capture
        write_replies(self.replies, {t["id"][:8]: next_turn()})
        restarted._resume_one(restarted.store.get(a.id))
        current = restarted.store.get(a.id)
        self.assertEqual((current.state, current.workspaces), ("running", []))
        self.assertEqual(restarted.pool.rows(), [])
        self.assertTrue(current.checkout.get("token"))
        argv, = seen
        denied = argv[argv.index("--disallowedTools") + 1:]
        for tool in ("Edit", "Write", "MultiEdit", "NotebookEdit", "Bash"):
            self.assertIn(tool, denied)
        proc = restarted.procs[a.id]
        self.assertEqual(proc.wait(timeout=10), 0)
        self.assertIn("harness runs it read-only", calls(self.calls)[0]["prompt"])
        restarted.settle_finished()

    def test_cold_metadata_repair_scope_change_requires_normal_admission(self):
        from dataclasses import replace
        from mesh_runner.workspace_scope import WORKSPACE_REQUIRED
        self.agent.repos[0] = replace(self.agent.repos[0], max_slots=1)
        t, mesh, r, a = self.cold_metadata_attempt()
        held = r.pool.lease("other-attempt", "app")
        mesh.tasks[t["id"]]["labels"].append(WORKSPACE_REQUIRED)
        self.adapter.binary = sys.executable
        restarted = self.runner(mesh)
        restarted._resume_one(restarted.store.get(a.id))
        current = restarted.store.get(a.id)
        self.assertEqual((current.state, current.workspaces, current.pid), ("ready", [], 0))
        self.assertEqual(restarted.procs, {})
        self.assertEqual([(row["attempt"], row["path"]) for row in restarted.pool.rows()],
                         [("other-attempt", str(held))])
        self.assertEqual(calls(self.calls), [])

    def test_cold_repair_completes_first_start_settings_and_status(self):
        t = task(1, labels=["model:opus", "effort:low"], subtask_count=1)
        child = task(2, parent_task_id=t["id"])
        mesh = FakeMesh([t, child])
        self.adapter.binary = str(self.root / "missing")
        runner = self.runner(mesh)
        self.assertFalse(runner._start_one(t))
        a, = runner.store.by_state(self.agent.name, ("ready",))
        self.assertEqual(mesh.tasks[t["id"]]["status_id"], "s-todo")
        # Fresh labels, including removal of a stale override, win at recovery.
        runner.store.mark(self.agent.name, t["id"], "model", "stale-model")
        mesh.tasks[t["id"]]["labels"] = ["model:sonnet", "effort:high"]
        self.adapter.binary = sys.executable
        write_replies(self.replies, {t["id"][:8]: next_turn()})
        restarted = self.runner(mesh)
        restarted._resume_one(restarted.store.get(a.id))
        self.finish_turn(restarted)
        children = restarted.store.mark(self.agent.name, t["id"], "children")
        self.assertIsNotNone(children, "first successful start must retain child status inventory")
        self.assertEqual(children[0], child["id"][:8] + ":s-todo")
        call, = calls(self.calls)
        self.assertEqual((call["model"], call["effort"]), ("sonnet", "high"))
        self.assertEqual(restarted.cats.category(mesh.tasks[t["id"]]), "in_progress")
        self.assertEqual(restarted.store.mark(self.agent.name, t["id"], "override_harness")[0], "claude")
        # A real subsequent turn keeps its established settings, not new labels.
        mesh.tasks[t["id"]]["labels"] = ["model:opus", "effort:low"]
        current = restarted.store.get(a.id)
        restarted.store.update(current, state="ready")
        restarted._resume_one(restarted.store.get(a.id))
        self.finish_turn(restarted)
        self.assertEqual([(c["model"], c["effort"]) for c in calls(self.calls)],
                         [("sonnet", "high"), ("sonnet", "high")])

    def test_routed_cold_exec_hold_survives_restart_and_repairs_same_attempt(self):
        binary = self.root / "routed-malformed"
        binary.write_bytes(b"\x7fELF")
        binary.chmod(0o700)
        routed = type(self.adapter)(self.adapter.script)
        routed.binary = str(binary)
        self.agent.inline_env["MESH_RUNNER_HARNESS"] = "codex"
        t = task(1, labels=["model:custom-model", "effort:low"])
        mesh = FakeMesh([t])
        runner = self.runner(mesh)
        runner.alt_adapters["codex"] = routed
        self.assertFalse(runner._start_one(t))
        attempts = runner.store.by_state(self.agent.name, ("ready",))
        self.assertEqual(len(attempts), 1, "routed exec refusal must retain one ready attempt")
        a, = attempts
        self.assertEqual((a.harness, a.turns, a.pid, a.workspaces), ("codex", 0, 0, []))
        self.assertTrue(a.note.startswith("launch readiness exec-denied"))
        self.assertEqual(runner.pool.leased_to(a.id), [])
        self.assertEqual(mesh.checkouts, {})
        before = runner.store.events(a.id)
        restarted = self.runner(mesh)
        restarted.alt_adapters["codex"] = routed
        for _ in range(2):
            restarted._resume_one(restarted.store.get(a.id))
        self.assertEqual(restarted.store.events(a.id), before)
        self.assertEqual(routed.spawn_calls, 1)
        binary.unlink()
        binary.symlink_to(sys.executable)
        write_replies(self.replies, {t["id"][:8]: next_turn()})
        restarted._resume_one(restarted.store.get(a.id))
        self.finish_turn(restarted)
        self.assertEqual(routed.spawn_calls, 2)
        self.assertEqual(restarted.store.get(a.id).turns, 1)
        call, = calls(self.calls)
        self.assertEqual((call["model"], call["effort"]), ("custom-model", "low"))

    def test_cold_hold_survives_restart_without_checkout_session_or_event_flood(self):
        t = task(1)
        mesh = FakeMesh([t])
        self.adapter.binary = str(self.root / "missing")
        r = self.runner(mesh)
        self.assertFalse(r._start_one(t))
        pending = r.store.by_state(self.agent.name, ("ready",))
        self.assertEqual(len(pending), 1)
        a = pending[0]
        self.assertEqual(a.turns, 0)
        self.assertEqual(a.session_id, "")
        self.assertEqual(mesh.checkouts, {})
        self.assertEqual(r.pool.leased_to(a.id), [])
        self.assertEqual(r.procs, {})
        before = r.store.events(a.id)
        saved = r.store.mark(self.agent.name, t["id"], "launch_readiness_hold")
        self.assertEqual(json.loads(saved[0])["state"], "deny")
        # New Runner/SQLite connection, same durable attempt and exact hold.
        restarted = self.runner(mesh)
        for _ in range(3):
            restarted._resume_one(restarted.store.get(a.id))
        self.assertEqual(restarted.store.events(a.id), before)
        self.assertEqual(mesh.checkouts, {})
        self.assertEqual(restarted.pool.leased_to(a.id), [])
        self.assertEqual(calls(self.calls), [])
        self.assertEqual(len(restarted.store.by_state(self.agent.name, ("ready",))), 1)

    def test_eperm_intended_exec_is_durable_hold_without_repeated_launch(self):
        self.adapter.binary = sys.executable
        mesh = FakeMesh([task(1)])
        runner = self.runner(mesh)
        popen_attempts = []
        original_spawn = self.adapter.spawn

        def eperm_at_intended_boundary(launch, log_path):
            def deny(*args, **kwargs):
                popen_attempts.append(True)
                raise PermissionError(errno.EPERM, "operation not permitted")
            with patch("mesh_runner.adapters.base.subprocess.Popen", side_effect=deny):
                return original_spawn(launch, log_path)

        self.adapter.spawn = eperm_at_intended_boundary
        self.assertFalse(runner._start_one(task(1)))
        a, = runner.store.by_state(self.agent.name, ("ready",))
        self.assertEqual((a.turns, a.pid, a.workspaces), (0, 0, []))
        self.assertEqual(runner.pool.leased_to(a.id), [])
        self.assertEqual(mesh.checkouts, {})
        self.assertEqual(runner.procs, {})
        self.assertEqual(calls(self.calls), [])
        saved = runner.store.mark(self.agent.name, a.task_id, "launch_readiness_hold")
        held = json.loads(saved[0])
        self.assertEqual((held["state"], held["reason"]),
                         ("exec-denied", f"intended-exec-failed:{errno.EPERM}"))
        self.assertEqual(len(popen_attempts), 1)
        before = runner.store.events(a.id)

        restarted = self.runner(mesh)
        for _ in range(3):
            restarted._resume_one(restarted.store.get(a.id))
        self.assertEqual(len(popen_attempts), 1)
        self.assertEqual(restarted.store.events(a.id), before)
        self.assertEqual(mesh.checkouts, {})
        self.assertEqual(restarted.pool.leased_to(a.id), [])
        self.assertEqual(calls(self.calls), [])

    def test_exec_refusal_deduplicates_across_restart_then_repairs_same_attempt(self):
        binary = self.root / "malformed"
        binary.write_bytes(b"\x7fELF")
        binary.chmod(0o700)
        self.adapter.binary = str(binary)
        mesh = FakeMesh([task(1)])
        runner = self.runner(mesh)
        self.assertFalse(runner._start_one(task(1)))
        a, = runner.store.by_state(self.agent.name, ("ready",))
        self.assertEqual(self.adapter.spawn_calls, 1)
        self.assertEqual((a.turns, a.pid, a.workspaces), (0, 0, []))
        self.assertEqual(runner.pool.leased_to(a.id), [])
        self.assertEqual(mesh.checkouts, {})
        self.assertEqual(calls(self.calls), [])
        mark = json.loads(runner.store.mark(self.agent.name, a.task_id, "launch_readiness_hold")[0])
        self.assertEqual(mark["state"], "exec-denied")
        self.assertIn("intended-exec-failed:", mark["reason"])
        before = runner.store.events(a.id)
        # Unrelated cwd activity must not masquerade as executable repair.
        (self.agent.profile_dir / "unrelated-noise").write_text("not a repair")
        restarted = self.runner(mesh)
        for _ in range(3):
            restarted._resume_one(restarted.store.get(a.id))
        self.assertEqual(self.adapter.spawn_calls, 1)
        self.assertEqual(restarted.store.events(a.id), before)
        # An actual external repair is observed; ordinary same-attempt admission.
        binary.unlink()
        binary.symlink_to(sys.executable)
        write_replies(self.replies, {"00000001": next_turn()})
        restarted._resume_one(restarted.store.get(a.id))
        proc = restarted.procs[a.id]
        self.addCleanup(lambda: proc.poll() is None and (proc.kill(), proc.wait(timeout=10)))
        self.assertEqual(proc.wait(timeout=10), 0)
        self.assertEqual(self.adapter.spawn_calls, 2)
        self.assertEqual(restarted.store.get(a.id).turns, 1)
        self.assertEqual(len(calls(self.calls)), 1)
        self.assertEqual(restarted.store.mark(self.agent.name, a.task_id, "launch_readiness_hold")[0], "")

    def test_late_exec_eacces_holds_before_prompt_and_releases_fresh_slot(self):
        binary = self.root / "late-permission"
        binary.write_text("#!/bin/sh\nexit 0\n")
        binary.chmod(0o700)
        self.adapter.binary = str(binary)
        # The real process boundary changes permissions after static readiness.
        self.adapter.before_exec = lambda launch: binary.chmod(0o600)
        mesh = FakeMesh([task(1)])
        runner = self.runner(mesh)
        self.assertFalse(runner._start_one(task(1)))
        a, = runner.store.by_state(self.agent.name, ("ready",))
        self.assertEqual((a.turns, a.pid, a.workspaces), (0, 0, []))
        self.assertIn(f"intended-exec-failed:{errno.EACCES}", a.note)
        self.assertEqual(self.adapter.spawn_calls, 1)
        self.assertEqual(runner.pool.leased_to(a.id), [])
        self.assertEqual(mesh.checkouts, {})
        self.assertEqual(calls(self.calls), [])

    def test_exec_failure_resume_preserves_retained_and_holds_dirty_fresh(self):
        binary = self.root / "malformed"
        binary.write_bytes(b"\x7fELF")
        binary.chmod(0o700)
        self.adapter.binary = str(binary)
        extra_root = self.root / "extra"
        extra_root.mkdir()
        self.agent.repos.append(RepoSpec("extra", make_repo(extra_root), max_slots=4))
        mesh = FakeMesh([task(1)])
        runner = self.runner(mesh)
        a = runner.store.create(self.agent.name, task(1)["id"], "claude")
        runner.store.update(a, state="ready")
        retained = runner.pool.lease(a.id, "app")
        (retained / "unfinished").write_text("retained")
        def dirty_fresh(launch):
            for path in runner.pool.leased_to(a.id):
                if path != str(retained):
                    (Path(path) / "README").write_text("fresh progress")
        self.adapter.before_exec = dirty_fresh
        runner._resume_one(a)
        current = runner.store.get(a.id)
        self.assertEqual((current.state, current.turns, current.pid), ("ready", 0, 0))
        self.assertEqual((retained / "unfinished").read_text(), "retained")
        self.assertEqual([row["path"] for row in runner.pool.rows()
                          if row["state"] == "leased"], [str(retained)])
        held = [row for row in runner.pool.rows() if row["state"] == "held"]
        self.assertEqual(len(held), 1, (current.note, self.adapter.spawn_calls, runner.pool.rows()))
        self.assertEqual((Path(held[0]["path"]) / "README").read_text(), "fresh progress")
        self.assertEqual(mesh.checkouts, {})
        self.assertEqual(runner.procs, {})

    def test_exec_failure_resume_releases_clean_fresh_and_preserves_retained(self):
        binary = self.root / "malformed"
        binary.write_bytes(b"\x7fELF")
        binary.chmod(0o700)
        self.adapter.binary = str(binary)
        extra_root = self.root / "extra"
        extra_root.mkdir()
        self.agent.repos.append(RepoSpec("extra", make_repo(extra_root), max_slots=4))
        mesh = FakeMesh([task(1)])
        runner = self.runner(mesh)
        a = runner.store.create(self.agent.name, task(1)["id"], "claude")
        runner.store.update(a, state="ready")
        retained = runner.pool.lease(a.id, "app")
        (retained / "unfinished").write_text("retained")
        runner._resume_one(a)
        current = runner.store.get(a.id)
        self.assertEqual((current.state, current.turns, current.pid), ("ready", 0, 0))
        self.assertEqual((retained / "unfinished").read_text(), "retained")
        self.assertEqual([row["path"] for row in runner.pool.rows()
                          if row["state"] == "leased"], [str(retained)])
        self.assertFalse([row for row in runner.pool.rows() if row["state"] == "held"])
        self.assertEqual(mesh.checkouts, {})
        self.assertEqual(runner.procs, {})

    def test_repair_rechecks_same_attempt_and_starts_one_real_child(self):
        t = task(1)
        mesh = FakeMesh([t])
        self.adapter.binary = str(self.root / "missing")
        r = self.runner(mesh)
        self.assertFalse(r._start_one(t))
        a = r.store.by_state(self.agent.name, ("ready",))[0]
        self.adapter.binary = sys.executable
        write_replies(self.replies, {"00000001": next_turn()})
        r._resume_one(a)
        proc = r.procs[a.id]
        try:
            self.assertEqual(proc.wait(timeout=10), 0)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)
        current = r.store.get(a.id)
        self.assertEqual(current.turns, 1)
        self.assertEqual(len(calls(self.calls)), 1)
        self.assertEqual(r.store.mark(self.agent.name, a.task_id, "launch_readiness_hold")[0], "")
        restored = [e for e in r.store.events(a.id) if e[1] == "launch_readiness_restored"]
        self.assertEqual(len(restored), 1)

    def test_late_readiness_loss_returns_fresh_slot_and_repairs_same_attempt(self):
        t = task(1)
        binary = self.root / "harness"
        binary.symlink_to(sys.executable)

        class ChangedAfterAdmission(FakeMesh):
            # Mesh is the external boundary between admission and final spawn.
            def move(self, *args, **kwargs):
                result = super().move(*args, **kwargs)
                binary.unlink()
                return result

        mesh = ChangedAfterAdmission([t])
        self.adapter.binary = str(binary)
        r = self.runner(mesh)
        self.assertFalse(r._start_one(t))
        a = r.store.by_state(self.agent.name, ("ready",))[0]
        self.assertEqual(a.turns, 0)
        self.assertEqual(mesh.checkouts, {})
        self.assertEqual(r.pool.leased_to(a.id), [])
        self.assertEqual(a.workspaces, [])
        self.assertEqual(r.procs, {})
        self.assertEqual(calls(self.calls), [])
        before = r.store.events(a.id)
        restarted = self.runner(mesh)
        for _ in range(3):
            restarted._resume_one(restarted.store.get(a.id))
        self.assertEqual(restarted.store.events(a.id), before)
        self.assertEqual(len(restarted.store.by_state(self.agent.name, ("ready",))), 1)
        binary.symlink_to(sys.executable)
        write_replies(self.replies, {"00000001": next_turn()})
        restarted._resume_one(restarted.store.get(a.id))
        proc = restarted.procs[a.id]
        self.addCleanup(lambda: proc.poll() is None and (proc.kill(), proc.wait(timeout=10)))
        self.assertEqual(proc.wait(timeout=10), 0)
        self.assertEqual(restarted.store.get(a.id).turns, 1)
        self.assertEqual(len(calls(self.calls)), 1)

    def test_late_readiness_loss_keeps_held_evidence_out_of_repaired_workspace(self):
        t = task(1)
        binary = self.root / "harness"
        binary.symlink_to(sys.executable)

        class ChangedAfterAdmission(FakeMesh):
            def move(self, *args, **kwargs):
                result = super().move(*args, **kwargs)
                attempt = r.store.by_state(r.agent.name, ("starting",))[0]
                path = Path(r.pool.leased_to(attempt.id)[0])
                (path / "README").write_text("unfinished work\n")
                binary.unlink()
                return result

        mesh = ChangedAfterAdmission([t])
        self.adapter.binary = str(binary)
        r = self.runner(mesh)
        self.assertFalse(r._start_one(t))
        a = r.store.by_state(self.agent.name, ("ready",))[0]
        held_path = Path(r.pool.leased_to(a.id)[0])
        self.assertEqual(a.workspaces, [])
        self.assertEqual((held_path / "README").read_text(), "unfinished work\n")
        self.assertEqual(r.pool.rows()[0]["state"], "held")
        self.assertEqual(mesh.checkouts, {})
        self.assertEqual(r.procs, {})

        # Simulate the legacy persisted inventory from the buggy build. Recovery
        # must treat it as evidence, strip it from admitted workspaces and lease cleanly.
        r.store.update(a, workspaces=[str(held_path)])
        binary.symlink_to(sys.executable)
        write_replies(self.replies, {"00000001": next_turn()})
        r._resume_one(r.store.get(a.id))
        proc = r.procs[a.id]
        self.addCleanup(lambda: proc.poll() is None and (proc.kill(), proc.wait(timeout=10)))
        self.assertEqual(proc.wait(timeout=10), 0)
        current = r.store.get(a.id)
        self.assertEqual(current.turns, 1)
        self.assertTrue(current.workspaces)
        self.assertNotIn(str(held_path), current.workspaces)
        self.assertTrue(all(Path(path).exists() for path in current.workspaces))
        self.assertEqual((held_path / "README").read_text(), "unfinished work\n")
        rows = [row for row in r.pool.rows() if row["path"] == str(held_path)]
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["state"], rows[0]["attempt"]), ("held", a.id))
        with self.assertRaisesRegex(WorkspaceError, "not leased to this attempt"):
            r.pool.issue(a.id, str(held_path))
        self.assertIn(t["id"], mesh.checkouts)
        self.assertTrue(current.session_id)

    def test_held_repository_is_reconciled_after_capacity_refusal(self):
        t = task(1)
        mesh = FakeMesh([t, task(2)])
        r, t, a, held, retained = self.held_multi_repo_attempt(mesh, ("app", "extra"))
        blocker = r.store.create(self.agent.name, task(2)["id"], "claude")
        r.pool.lease(blocker.id, "app")
        self.assertTrue(r.pool.full("app"))

        r._resume_one(r.store.get(a.id))

        after_refusal = r.store.get(a.id)
        self.assertEqual(after_refusal.workspaces, [str(retained[0])])
        self.assertEqual(r.procs, {})
        self.assertEqual(calls(self.calls), [])
        self.assertEqual((held / "README").read_text(), "preserve held progress\n")

        r.pool.release(blocker.id)
        write_replies(self.replies, {"00000001": next_turn()})
        r._resume_one(r.store.get(a.id))

        current = r.store.get(a.id)
        paths_by_repo = {row["pool"].split("/", 1)[1]: row["path"]
                         for row in r.pool.rows() if row["attempt"] == a.id
                         and row["state"] == "leased"}
        self.assertEqual(set(paths_by_repo), {"app", "extra"})
        self.assertCountEqual(current.workspaces, paths_by_repo.values())
        self.assertNotIn(str(held), current.workspaces)
        proc = r.procs[a.id]
        self.addCleanup(lambda: proc.poll() is None and (proc.kill(), proc.wait(timeout=10)))
        self.assertEqual(proc.wait(timeout=10), 0)
        self.assertEqual((held / "README").read_text(), "preserve held progress\n")

    def test_held_repository_is_reconciled_after_partial_allocation_exception(self):
        t = task(1)
        mesh = FakeMesh([t])
        r, t, a, held, retained = self.held_multi_repo_attempt(
            mesh, ("app", "extra", "late"), retained_repo_names=("extra",))
        late_parent = r.pool.root / "late"
        hook = self.agent.repos[0].base / ".git" / "hooks" / "post-checkout"
        hook.write_text("#!/bin/sh\nmkdir -p " + shlex.quote(str(r.pool.root))
                        + "\n: > " + shlex.quote(str(late_parent)) + "\n")
        hook.chmod(0o700)

        with self.assertRaises(OSError):
            r._resume_one(r.store.get(a.id))

        partial = r.store.get(a.id)
        leased = {row["pool"].split("/", 1)[1] for row in r.pool.rows()
                  if row["attempt"] == a.id and row["state"] == "leased"}
        self.assertEqual(leased, {"app", "extra"})
        self.assertEqual(partial.workspaces, [str(retained[0])])
        self.assertTrue(late_parent.is_file())
        self.assertEqual(r.procs, {})
        late_parent.unlink()
        hook.unlink()
        write_replies(self.replies, {"00000001": next_turn()})

        r._resume_one(r.store.get(a.id))

        current = r.store.get(a.id)
        paths_by_repo = {row["pool"].split("/", 1)[1]: row["path"]
                         for row in r.pool.rows() if row["attempt"] == a.id
                         and row["state"] == "leased"}
        self.assertEqual(set(paths_by_repo), {"app", "extra", "late"})
        self.assertCountEqual(current.workspaces, paths_by_repo.values())
        self.assertNotIn(str(held), current.workspaces)
        proc = r.procs[a.id]
        self.addCleanup(lambda: proc.poll() is None and (proc.kill(), proc.wait(timeout=10)))
        self.assertEqual(proc.wait(timeout=10), 0)
        self.assertEqual((held / "README").read_text(), "preserve held progress\n")

    def test_resume_readiness_hold_preserves_retained_workspace_progress(self):
        t = task(1)
        mesh = FakeMesh([t])
        r = self.runner(mesh)
        a = r.store.create(self.agent.name, t["id"], "claude")
        path = r.pool.lease(a.id, "app")
        progress = path / "retained-progress.txt"
        progress.write_text("unfinished work\n")
        r.store.update(a, state="ready", workspaces=[str(path)])
        self.adapter.binary = str(self.root / "missing")
        r._resume_one(a)
        self.assertEqual(progress.read_text(), "unfinished work\n")
        self.assertEqual(r.pool.leased_to(a.id), [str(path)])
        self.assertEqual(r.store.get(a.id).workspaces, [str(path)])
        self.assertEqual(mesh.checkouts, {})
        self.assertEqual(r.procs, {})

    def test_resume_late_readiness_loss_releases_only_fresh_clean_slot(self):
        t = task(1)
        extra_root = self.root / "extra"
        extra_root.mkdir()
        extra_base = make_repo(extra_root)
        self.agent.repos.append(RepoSpec("extra", extra_base, max_slots=4))
        binary = self.root / "harness"
        binary.symlink_to(sys.executable)

        class ChangedAfterCheckout(FakeMesh):
            # Mesh is the external boundary between preflight and workspace allocation.
            def checkout(self, *args, **kwargs):
                result = super().checkout(*args, **kwargs)
                binary.unlink()
                return result

        mesh = ChangedAfterCheckout([t])
        self.adapter.binary = str(binary)
        r = self.runner(mesh)
        a = r.store.create(self.agent.name, t["id"], "claude")
        r.store.update(a, state="ready")
        retained = r.pool.lease(a.id, "app")
        progress = retained / "unfinished.txt"
        progress.write_text("retained resume work\n")

        r._resume_one(a)

        current = r.store.get(a.id)
        self.assertEqual((current.state, current.turns), ("ready", 0))
        self.assertEqual(current.workspaces, [])
        self.assertEqual(r.pool.leased_to(a.id), [str(retained)])
        self.assertEqual(progress.read_text(), "retained resume work\n")
        self.assertEqual(mesh.checkouts, {})
        self.assertEqual(r.procs, {})
        extra_rows = [row for row in r.pool.rows() if row["pool"].endswith("/extra")]
        self.assertEqual([row["state"] for row in extra_rows], ["free"])

    def test_resume_late_readiness_loss_holds_dirty_fresh_slot(self):
        t = task(1)
        extra_root = self.root / "extra"
        extra_root.mkdir()
        extra_base = make_repo(extra_root)
        binary = self.root / "harness"
        binary.symlink_to(sys.executable)
        # git worktree add runs post-checkout inside the new disposable slot. This
        # filesystem boundary creates real uncommitted work and then removes the
        # executable before the runner's final readiness check.
        hook = extra_base / ".git" / "hooks" / "post-checkout"
        hook.write_text(f"#!/bin/sh\nprintf 'unfinished\\n' >> README\nrm -f '{binary}'\n")
        hook.chmod(0o700)
        self.agent.repos.append(RepoSpec("extra", extra_base, max_slots=4))
        mesh = FakeMesh([t])
        self.adapter.binary = str(binary)
        r = self.runner(mesh)
        a = r.store.create(self.agent.name, t["id"], "claude")
        r.store.update(a, state="ready")
        retained = r.pool.lease(a.id, "app")
        retained_file = retained / "unfinished.txt"
        retained_file.write_text("keep existing resume work\n")

        r._resume_one(a)

        current = r.store.get(a.id)
        self.assertEqual(current.state, "ready")
        self.assertEqual(current.workspaces, [])
        rows = r.pool.rows()
        self.assertEqual(next(row["state"] for row in rows if row["path"] == str(retained)), "leased")
        extra = next(row for row in rows if row["pool"].endswith("/extra"))
        self.assertEqual(extra["state"], "held")
        self.assertIn("uncommitted changes", extra["note"])
        self.assertTrue((Path(extra["path"]) / "README").read_text().endswith("unfinished\n"))
        self.assertEqual(retained_file.read_text(), "keep existing resume work\n")
        self.assertEqual(mesh.checkouts, {})
        self.assertEqual(r.procs, {})

    def test_unknown_does_not_launch_or_clear_hold(self):
        t = task(1)
        mesh = FakeMesh([t])
        script = self.root / "unknown"
        script.write_text("#!/usr/bin/env -S node --flag\n")
        script.chmod(0o700)
        self.adapter.binary = str(script)
        r = self.runner(mesh)
        self.assertFalse(r._start_one(t))
        a = r.store.by_state(self.agent.name, ("ready",))[0]
        r._resume_one(a)
        self.assertEqual(r.store.get(a.id).turns, 0)
        self.assertEqual(json.loads(r.store.mark(self.agent.name, a.task_id,
                                               "launch_readiness_hold")[0])["state"], "unknown")
        self.assertEqual(mesh.checkouts, {})
        self.assertEqual(r.procs, {})

    def test_final_spawn_check_does_not_mutate_completed_attempt(self):
        t = task(1)
        r = self.runner(FakeMesh([t]))
        a = r.store.create(self.agent.name, t["id"], "claude")
        r.store.update(a, state="completed")
        self.adapter.binary = str(self.root / "missing")
        self.assertFalse(r._spawn(a, "#00000001", resume=False, expect=("starting",)))
        self.assertEqual(r.store.get(a.id).state, "completed")
        self.assertIsNone(r.store.mark(self.agent.name, t["id"], "launch_readiness_hold"))
        self.assertEqual(r.procs, {})


if __name__ == "__main__":
    unittest.main()
