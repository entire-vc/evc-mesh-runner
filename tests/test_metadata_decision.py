"""Per-card no-worktree decisions preserve the normal runner authority gates."""
import json
from dataclasses import replace
from unittest.mock import patch

from fakes import FakeMesh, task, write_replies
from test_runner import Bench
from test_workspace import make_repo
from mesh_runner.workspace import RepoSpec, WorkspaceError
from mesh_runner.workspace_scope import METADATA_ONLY, WORKSPACE_REQUIRED, RECEIPT_KEY


def decision(n=1, **kw):
    return task(n, labels=["decision-request", METADATA_ONLY], **kw)


class MetadataDecision(Bench):
    def finish_turn(self, runner):
        attempt, = runner.store.by_state(self.agent.name, ("running",))
        runner.procs[attempt.id].wait(timeout=10)
        runner.settle_finished()
        return runner.store.get(attempt.id)

    def test_queue_proof_wakes_metadata_attempt_without_losing_scope_on_restart(self):
        from mesh_runner import queue_wait as q
        from test_queue_wait_freshness import NOW, description, proof

        self.cfg.queue_wait_freshness_v1 = True
        self.agent.repos[0] = replace(self.agent.repos[0], max_slots=1)
        t = decision(description=description(), human_gate=False)
        t["labels"].append("fleet-starvation-wake")
        mesh = FakeMesh([t], max_concurrent=1)
        runner = self.runner(mesh)
        held = runner.pool.lease("other-attempt", "app")
        write_replies(self.replies, {t["id"][:8]: "⏳ WAIT 2h: queue empty"})
        with patch("mesh_runner.runner.time.time", return_value=NOW):
            runner.tick()
            a = self.finish_turn(runner)
            self.assertEqual((a.state, a.wait["kind"], a.workspaces), ("waiting", "queue", []))
            runner._wake_one(a)  # UNKNOWN is not permission to resume.
            self.assertEqual(a.state, "waiting")
            req = a.wait["queue_request"]
            # AgentRunner fixture has no outer Runner.write_status publisher.
            # Simulate the real outbox before constructing its observer receipt.
            manifest = q.publish(runner.store,
                                 self.cfg.state_dir / "queue-wait-requests.json",
                                 self.cfg.state_dir / "queue-wait-receipts.json")
            receipt = proof(req)
            receipt.update(manifest_revision=manifest["revision"], manifest_digest=q.digest(manifest))
            q.write_json(self.cfg.state_dir / "queue-wait-receipts.json", receipt)
            mesh.tasks[t["id"]]["human_gate"] = True
            runner._wake_one(a)
            self.assertEqual(a.state, "waiting")
            self.assertIsNotNone(q.pending(runner.store, a, req, runner.agent_id))
        mesh.tasks[t["id"]]["human_gate"] = False
        with patch("mesh_runner.runner.time.time", return_value=NOW + 300):
            runner._wake_one(a)
            self.assertEqual(a.state, "ready")
            restarted = self.runner(mesh)
            restarted.tick()
            resumed = restarted.store.get(a.id)
            self.assertEqual((resumed.state, resumed.workspaces), ("running", []))
            self.assertEqual([(r["attempt"], r["path"]) for r in restarted.pool.rows()],
                             [("other-attempt", str(held))])
            self.finish_turn(restarted)

    def test_opted_in_decision_starts_without_leases_when_pool_is_full(self):
        self.agent.repos[0] = replace(self.agent.repos[0], max_slots=1)
        t = decision()
        runner = self.runner(FakeMesh([t], max_concurrent=1))
        held = runner.pool.lease("other-attempt", "app")
        write_replies(self.replies, {t["id"][:8]: "⏳ WAIT 2h: awaiting evidence"})
        runner.tick()
        attempt, = runner.store.by_state(self.agent.name, ("running",))
        self.assertEqual((attempt.agent, attempt.workspaces), (self.agent.name, []))
        self.assertTrue(attempt.checkout.get("token"))
        self.assertEqual([(r["attempt"], r["path"]) for r in runner.pool.rows()],
                         [("other-attempt", str(held))])
        self.assertEqual(self.finish_turn(runner).state, "waiting")

    def test_full_pool_does_not_hide_metadata_decision_behind_normal_work(self):
        self.agent.repos[0] = replace(self.agent.repos[0], max_slots=1)
        normal = task(1, priority="urgent")
        choice = decision(2)
        runner = self.runner(FakeMesh([normal, choice], max_concurrent=1))
        runner.pool.lease("other-attempt", "app")
        write_replies(self.replies, {choice["id"][:8]: "⏳ WAIT 2h: awaiting evidence"})
        runner.tick()
        attempt, = runner.store.by_state(self.agent.name, ("running",))
        self.assertEqual(attempt.task_id, choice["id"])
        self.finish_turn(runner)

    def test_review_alert_and_untyped_cards_still_require_normal_workspace(self):
        self.agent.repos[0] = replace(self.agent.repos[0], max_slots=1)
        for t in (task(1, labels=[METADATA_ONLY, "alert"]),
                  task(2, labels=["decision-request"]), decision(3, cat="review")):
            with self.subTest(task=t["id"]):
                runner = self.runner(FakeMesh([t]))
                if not runner.pool.rows():
                    runner.pool.lease("other-attempt", "app")
                runner.tick()
                self.assertEqual(runner.store.by_state(self.agent.name, ("starting", "running")), [])

    def test_human_gate_keeps_the_opted_in_decision_unstarted(self):
        runner = self.runner(FakeMesh([decision(human_gate=True)]))
        with patch.object(runner, "_spawn") as spawn:
            runner.tick()
        spawn.assert_not_called()
        self.assertEqual(runner.pool.rows(), [])

    def test_conflicting_alert_types_cannot_bypass_a_full_pool(self):
        self.agent.repos[0] = replace(self.agent.repos[0], max_slots=1)
        for n, label in enumerate(("alert", "auto-incident"), 1):
            with self.subTest(label=label):
                t = decision(n)
                t["labels"].append(label)
                runner = self.runner(FakeMesh([t], max_concurrent=1))
                if not runner.pool.rows():
                    runner.pool.lease("other-attempt", "app")
                with patch.object(runner, "_spawn", return_value=False) as spawn:
                    runner.tick()
                spawn.assert_not_called()

    def test_new_alert_type_requires_code_admission_before_metadata_resume(self):
        self.agent.repos[0] = replace(self.agent.repos[0], max_slots=1)
        t = decision()
        mesh = FakeMesh([t], max_concurrent=1)
        runner = self.runner(mesh)
        write_replies(self.replies, {t["id"][:8]: "↻ NEXT"})
        runner.tick()
        first = self.finish_turn(runner)
        self.assertEqual(first.workspaces, [])
        runner.pool.lease("other-attempt", "app")
        mesh.tasks[t["id"]]["labels"].append("alert")
        with patch.object(runner, "_spawn") as spawn:
            runner.tick()
        spawn.assert_not_called()
        self.assertEqual(runner.store.get(first.id).state, "ready")

    def test_authentication_hold_still_precedes_any_metadata_harness(self):
        runner = self.runner(FakeMesh([decision()]))
        with patch.object(runner, "_auth_blocked", return_value="held-profile"), \
             patch.object(runner, "_spawn") as spawn, patch.object(runner.pool, "lease") as lease:
            runner.tick()
        spawn.assert_not_called()
        lease.assert_not_called()

    def test_pause_and_dependencies_are_not_bypassed_by_the_scope_label(self):
        t, blocker = decision(), task(2)
        blocker["assignee_id"] = "another-agent"
        mesh = FakeMesh([t, blocker])
        mesh.deps[t["id"]] = [{"depends_on_task_id": blocker["id"], "dependency_type": "blocks"}]
        runner = self.runner(mesh)
        with patch.object(runner, "_spawn") as spawn:
            runner.tick()
        spawn.assert_not_called()
        mesh.deps[t["id"]] = []
        with patch.object(runner, "paused", return_value=True), patch.object(runner, "_spawn") as spawn:
            runner.tick()
        spawn.assert_not_called()

    def test_global_parallel_capacity_still_limits_metadata_starts(self):
        choices = [decision(1), decision(2)]
        runner = self.runner(FakeMesh(choices, max_concurrent=1))
        write_replies(self.replies, {t["id"][:8]: "⏳ WAIT 2h: awaiting evidence" for t in choices})
        runner.tick()
        started = runner.store.by_state(self.agent.name, ("running",))
        self.assertEqual(len(started), 1)
        self.finish_turn(runner)

    def test_scope_is_refreshed_after_checkout_before_skipping_a_lease(self):
        t = decision()
        mesh = FakeMesh([t], max_concurrent=1)
        runner = self.runner(mesh)
        checkout = mesh.checkout
        def change_scope(*args, **kw):
            result = checkout(*args, **kw)
            mesh.tasks[t["id"]]["labels"].append(WORKSPACE_REQUIRED)
            return result
        write_replies(self.replies, {t["id"][:8]: "⏳ WAIT 2h: now code work"})
        with patch.object(mesh, "checkout", side_effect=change_scope):
            runner.tick()
        a = self.finish_turn(runner)
        self.assertEqual(len(a.workspaces), 1)
        self.assertEqual(runner.pool.leased_to(a.id), a.workspaces)

    def test_code_transition_admits_workspace_before_resuming_same_attempt(self):
        t = decision()
        mesh = FakeMesh([t], max_concurrent=1)
        runner = self.runner(mesh)
        write_replies(self.replies, {t["id"][:8]: "↻ NEXT"})
        runner.tick()
        first = self.finish_turn(runner)
        self.assertEqual((first.state, first.workspaces), ("ready", []))
        mesh.tasks[t["id"]]["labels"].append(WORKSPACE_REQUIRED)
        write_replies(self.replies, {t["id"][:8]: "⏳ WAIT 2h: working from leased checkout"})
        runner.tick()
        resumed = runner.store.get(first.id)
        self.assertEqual(resumed.state, "running")
        self.assertEqual(len(resumed.workspaces), 1)
        self.assertEqual(runner.pool.leased_to(first.id), resumed.workspaces)
        self.finish_turn(runner)

    def test_code_transition_waits_quietly_for_normal_workspace_admission(self):
        self.agent.repos[0] = replace(self.agent.repos[0], max_slots=1)
        t = decision()
        mesh = FakeMesh([t], max_concurrent=1)
        runner = self.runner(mesh)
        write_replies(self.replies, {t["id"][:8]: "↻ NEXT"})
        runner.tick()
        first = self.finish_turn(runner)
        self.assertEqual(first.workspaces, [])
        runner.pool.lease("other-attempt", "app")
        mesh.tasks[t["id"]]["labels"].append(WORKSPACE_REQUIRED)
        with patch.object(runner, "_spawn") as spawn:
            runner.tick()
        spawn.assert_not_called()
        current = runner.store.get(first.id)
        self.assertEqual((current.state, current.workspaces, current.pid), ("ready", [], 0))

    def test_restart_preserves_metadata_scope_without_filling_the_pool(self):
        self.agent.repos[0] = replace(self.agent.repos[0], max_slots=1)
        t = decision()
        mesh = FakeMesh([t], max_concurrent=1)
        runner = self.runner(mesh)
        write_replies(self.replies, {t["id"][:8]: "↻ NEXT", t["id"][:8] + ":resume": "⏳ WAIT 2h: awaiting evidence"})
        runner.tick()
        first = self.finish_turn(runner)
        runner.pool.lease("other-attempt", "app")
        restarted = self.runner(mesh)
        restarted.tick()
        resumed = restarted.store.get(first.id)
        self.assertEqual((resumed.state, resumed.workspaces), ("running", []))
        self.finish_turn(restarted)

    def test_receipt_from_another_attempt_never_opts_a_ready_attempt_in(self):
        t = decision()
        runner = self.runner(FakeMesh([t], max_concurrent=1))
        runner.refresh_me()
        a = runner.store.create(self.agent.name, t["id"], self.agent.harness)
        runner.store.update(a, state="ready")
        runner.store.mark(self.agent.name, t["id"], RECEIPT_KEY,
                          json.dumps({"attempt": "a-different-attempt", "scope": "metadata-only"}))
        write_replies(self.replies, {t["id"][:8]: "⏳ WAIT 2h: normal admission"})
        runner.tick()
        resumed = runner.store.get(a.id)
        self.assertEqual(resumed.state, "running")
        self.assertEqual(len(resumed.workspaces), 1)
        self.finish_turn(runner)

    def test_new_human_gate_stops_a_ready_metadata_attempt(self):
        t = decision()
        mesh = FakeMesh([t], max_concurrent=1)
        runner = self.runner(mesh)
        write_replies(self.replies, {t["id"][:8]: "↻ NEXT"})
        runner.tick()
        first = self.finish_turn(runner)
        mesh.tasks[t["id"]]["human_gate"] = True
        with patch.object(runner, "_spawn") as spawn:
            runner.tick()
        spawn.assert_not_called()
        self.assertEqual(runner.store.get(first.id).state, "ready")

    def test_disk_floor_holds_both_new_and_ready_metadata_turns(self):
        t = decision()
        runner = self.runner(FakeMesh([t], max_concurrent=1))
        with patch.object(runner.pool, "disk_hold", return_value="worktree disk below floor"), \
             patch.object(runner, "_spawn") as spawn:
            runner.tick()
        spawn.assert_not_called()
        write_replies(self.replies, {t["id"][:8]: "↻ NEXT"})
        runner.tick()
        first = self.finish_turn(runner)
        with patch.object(runner.pool, "disk_hold", return_value="worktree disk below floor"), \
             patch.object(runner.adapter, "spawn") as native:
            runner.tick()
        native.assert_not_called()
        self.assertEqual(runner.store.get(first.id).state, "ready")
        self.assertFalse(runner.pool.leased_to(first.id))

    def test_restart_reuses_partial_transition_even_if_metadata_is_requested_again(self):
        second = self.root / "second"
        second.mkdir()
        self.agent.repos.append(RepoSpec("lib", make_repo(second), max_slots=1))
        t = decision()
        mesh = FakeMesh([t], max_concurrent=1)
        runner = self.runner(mesh)
        write_replies(self.replies, {t["id"][:8]: "↻ NEXT"})
        runner.tick()
        first = self.finish_turn(runner)
        mesh.tasks[t["id"]]["labels"].append(WORKSPACE_REQUIRED)
        lease = runner.pool.lease
        def interrupt_after_first(attempt_id, repo):
            if repo == "lib":
                raise WorkspaceError("injected interruption after first durable lease")
            return lease(attempt_id, repo)
        with patch.object(runner.pool, "lease", side_effect=interrupt_after_first):
            runner.tick()
        partial, = runner.pool.leased_to(first.id)
        self.assertEqual(runner.store.get(first.id).workspaces, [])
        mesh.tasks[t["id"]]["labels"].remove(WORKSPACE_REQUIRED)
        restarted = self.runner(mesh)
        write_replies(self.replies, {t["id"][:8]: "⏳ WAIT 2h: admitted code work",
                                     t["id"][:8] + ":resume": "⏳ WAIT 2h: admitted code work"})
        with patch.object(restarted.pool, "lease", wraps=restarted.pool.lease) as resumed_lease:
            restarted.tick()
        resumed_lease.assert_called_once_with(first.id, "lib")
        current = restarted.store.get(first.id)
        self.assertEqual(current.state, "running")
        self.assertEqual(len(current.workspaces), 2)
        self.assertIn(partial, current.workspaces)
        self.assertEqual(current.session_id, first.session_id)
        self.finish_turn(restarted)

    def test_human_gate_also_holds_an_interrupted_code_transition(self):
        t = decision(human_gate=True)
        runner = self.runner(FakeMesh([t], max_concurrent=1))
        runner.refresh_me()
        a = runner.store.create(self.agent.name, t["id"], self.agent.harness)
        runner.store.update(a, state="ready")
        runner.store.mark(self.agent.name, t["id"], RECEIPT_KEY,
                          json.dumps({"attempt": a.id, "scope": "workspace"}))
        with patch.object(runner, "_spawn") as spawn, patch.object(runner.pool, "lease") as lease:
            runner.tick()
        spawn.assert_not_called()
        lease.assert_not_called()
        self.assertEqual(runner.store.get(a.id).state, "ready")

    def test_label_change_never_discards_an_existing_suspended_checkpoint(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        runner = self.runner(mesh)
        write_replies(self.replies, {t["id"][:8]: "⏳ WAIT 2h: awaiting evidence"})
        runner.tick()
        a = self.finish_turn(runner)
        paths = list(a.workspaces)
        runner.pool.suspend(a.id, {"session_id": a.session_id, "harness": a.harness,
                                  "cwd": str(self.agent.profile_dir), "workspaces": paths,
                                  "wait": a.wait}, writer_stopped=True)
        saved = [r["checkpoint"] for r in runner.pool._checkpoint_rows(a.id)]
        mesh.tasks[t["id"]]["labels"] = [METADATA_ONLY, "decision-request"]
        runner.store.update(a, state="ready", pid=0, stamp="")
        runner.tick()
        current = runner.store.get(a.id)
        self.assertEqual(current.state, "running")
        self.assertTrue(current.workspaces)
        self.assertEqual(runner.pool.leased_to(a.id), current.workspaces)
        self.assertEqual([r["checkpoint"] for r in runner.pool._checkpoint_rows(a.id)], saved)
        self.finish_turn(runner)


class MetadataAttemptIsolation(Bench):
    """The no-worktree attempt is confined by the harness, not only by its prompt."""

    def argv_of_first_turn(self, runner):
        seen = []
        real = self.adapter.spawn

        def spy(launch, log_path):
            seen.append(list(launch.argv))
            return real(launch, log_path)
        self.adapter.spawn = spy
        runner.tick()
        return seen

    def test_claude_metadata_argv_denies_every_write_path(self):
        from mesh_runner.adapters.claude import ClaudeAdapter
        argv = ClaudeAdapter().with_read_only(["claude", "-p"])
        denied = argv[argv.index("--disallowedTools") + 1:]
        for tool in ("Edit", "Write", "MultiEdit", "NotebookEdit", "Bash"):
            self.assertIn(tool, denied)

    def test_codex_metadata_argv_runs_in_the_read_only_sandbox(self):
        from mesh_runner.adapters.codex import CodexAdapter
        argv = CodexAdapter().with_read_only(["codex", "-C", "/p", "exec", "--json", "-"])
        self.assertEqual(argv[argv.index("--sandbox") + 1], "read-only")
        self.assertLess(argv.index("--sandbox"), argv.index("exec"))

    def test_codex_equals_form_sandbox_is_replaced(self):
        from mesh_runner.adapters.codex import CodexAdapter
        argv = CodexAdapter().with_read_only(["codex", "--sandbox=danger-full-access", "exec", "-"])
        self.assertEqual([a for a in argv if "sandbox" in a], ["--sandbox"])
        self.assertEqual(argv[argv.index("--sandbox") + 1], "read-only")

    def test_codex_interactive_argv_without_exec_gets_read_only(self):
        from mesh_runner.adapters.codex import CodexAdapter
        for argv in (["codex", "-C", "/p"], ["codex", "-C", "/p", "resume", "sid"]):
            out = CodexAdapter().with_read_only(argv)
            self.assertEqual(out[out.index("--sandbox") + 1], "read-only")
            self.assertEqual(out.count("--sandbox"), 1)
            if "resume" in out:
                self.assertLess(out.index("--sandbox"), out.index("resume"))

    def test_metadata_attempt_is_launched_read_only(self):
        t = decision()
        runner = self.runner(FakeMesh([t], max_concurrent=1))
        write_replies(self.replies, {t["id"][:8]: "⏳ WAIT 2h: awaiting evidence"})
        argv, = self.argv_of_first_turn(runner)
        self.assertIn("--disallowedTools", argv)

    def test_code_attempt_keeps_its_tools(self):
        plain = task(2)
        runner = self.runner(FakeMesh([plain], max_concurrent=1))
        write_replies(self.replies, {plain["id"][:8]: "⏳ WAIT 2h: x"})
        argv, = self.argv_of_first_turn(runner)
        self.assertNotIn("--disallowedTools", argv)

    def test_alert_diagnostics_keep_their_tools(self):
        from test_metadata_alert import diagnostic
        t = diagnostic(3)
        runner = self.runner(FakeMesh([t], max_concurrent=1))
        write_replies(self.replies, {t["id"][:8]: "↻ NEXT"})
        argv, = self.argv_of_first_turn(runner)
        self.assertNotIn("--disallowedTools", argv)
