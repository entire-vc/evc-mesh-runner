"""Explicit alert diagnostics use the existing metadata admission and promotion."""
import json
import time
from dataclasses import replace
from unittest.mock import patch

from fakes import FakeMesh, task, write_replies
from test_runner import Bench
from test_workspace import make_repo
from mesh_runner.workspace import RepoSpec, WorkspaceError
from mesh_runner.workspace_scope import METADATA_ONLY, RECEIPT_KEY, WORKSPACE_REQUIRED


def diagnostic(n=1, **kw):
    return task(n, labels=["alert", "auto-incident", METADATA_ONLY, "runner:alert-diagnostics"], **kw)


class MetadataAlert(Bench):
    def full_pool(self, mesh):
        self.agent.repos[0] = replace(self.agent.repos[0], max_slots=1)
        runner = self.runner(mesh)
        held = runner.pool.lease("other-attempt", "app")
        return runner, held

    def test_explicit_diagnostics_start_behind_code_with_full_pool(self):
        normal, alert = task(1, priority="urgent"), diagnostic(2)
        runner, held = self.full_pool(FakeMesh([normal, alert], max_concurrent=1))
        write_replies(self.replies, {alert["id"][:8]: "↻ NEXT"})
        with patch.object(runner.adapter, "spawn", wraps=runner.adapter.spawn) as spawn:
            runner.tick()
        spawn.assert_called_once()
        launch = spawn.call_args.args[0]
        self.assertIn("diagnostic evidence", launch.prompt)
        self.assertIn("runner:workspace-required", launch.prompt)
        current = self.finish_turn(runner)
        self.assertEqual((current.task_id, current.state, current.workspaces), (alert["id"], "ready", []))
        self.assertEqual(runner.pool.leased_to("other-attempt"), [str(held)])
        self.assertEqual(json.loads(runner.store.mark(self.agent.name, alert["id"], RECEIPT_KEY)[0]),
                         {"attempt": current.id, "scope": "metadata-only"})
        restarted = self.runner(runner.mesh)
        restarted.tick()
        self.assertEqual(restarted.store.get(current.id).workspaces, [])
        self.finish_turn(restarted)

    def test_mixed_decision_opt_in_and_review_still_need_workspace(self):
        self.agent.repos[0] = replace(self.agent.repos[0], max_slots=1)
        for n, cat in ((1, "todo"), (2, "review")):
            t = diagnostic(n, cat=cat)
            if cat == "todo":
                t["labels"].append("decision-request")
            runner = self.runner(FakeMesh([t], max_concurrent=1))
            if not runner.pool.rows():
                runner.pool.lease("other-attempt", "app")
            with patch.object(runner, "_spawn") as spawn:
                runner.tick()
            spawn.assert_not_called()

    def assert_start_held(self, kind):
        t = diagnostic(human_gate=kind == "human")
        mesh = FakeMesh([t], max_concurrent=1)
        runner, _ = self.full_pool(mesh)
        if kind == "quota":
            runner._starts_paused_until = time.monotonic() + 600
        if kind == "checkout":
            mesh.refuse_checkout.add(t["id"])
        target, value = {"auth": (runner, "_auth_blocked"), "disk": (runner.pool, "disk_hold")}.get(
            kind, (runner, "paused"))
        with patch.object(target, value, return_value="held" if kind in ("auth", "disk") else False), \
             patch.object(runner, "_spawn") as spawn:
            runner.tick()
        spawn.assert_not_called()
        self.assertFalse(any(row["attempt"] != "other-attempt" for row in runner.pool.rows()))

    def test_human_hold_is_preserved(self):
        self.assert_start_held("human")

    def test_auth_hold_is_preserved(self):
        self.assert_start_held("auth")

    def test_quota_hold_is_preserved(self):
        self.assert_start_held("quota")

    def test_disk_hold_is_preserved(self):
        self.assert_start_held("disk")

    def test_checkout_refusal_is_preserved(self):
        self.assert_start_held("checkout")

    def test_restart_ready_diagnostics_keep_all_holds_and_recover_without_leases(self):
        t = diagnostic()
        mesh = FakeMesh([t], max_concurrent=1)
        runner, _ = self.full_pool(mesh)
        write_replies(self.replies, {t["id"][:8]: "↻ NEXT"})
        runner.tick()
        first = self.finish_turn(runner)
        for kind in ("human", "auth", "quota", "disk"):
            with self.subTest(kind=kind):
                mesh.tasks[t["id"]]["human_gate"] = kind == "human"
                restarted = self.runner(mesh)
                if kind == "quota":
                    restarted._starts_paused_until = time.monotonic() + 600
                with patch.object(restarted, "_auth_blocked", return_value="held" if kind == "auth" else ""), \
                     patch.object(restarted.pool, "disk_hold", return_value="held" if kind == "disk" else ""), \
                     patch.object(restarted.adapter, "spawn") as spawn:
                    restarted.tick()
                spawn.assert_not_called()
                current = restarted.store.get(first.id)
                self.assertEqual((current.state, current.workspaces, current.turns), ("ready", [], 1))
        restarted = self.runner(mesh)
        restarted.tick()
        self.assertEqual(restarted.store.get(first.id).state, "running")
        self.assertEqual(restarted.store.get(first.id).workspaces, [])
        self.finish_turn(restarted)

    def test_promotion_is_durable_before_lease_and_cannot_be_undone_after_crash(self):
        second = self.root / "second"
        second.mkdir()
        self.agent.repos.append(RepoSpec("lib", make_repo(second), max_slots=1))
        t = diagnostic()
        mesh = FakeMesh([t], max_concurrent=1)
        runner = self.runner(mesh)
        write_replies(self.replies, {t["id"][:8]: "↻ NEXT"})
        runner.tick()
        first = self.finish_turn(runner)
        self.assertEqual(first.workspaces, [])
        mesh.tasks[t["id"]]["labels"].append(WORKSPACE_REQUIRED)
        lease = runner.pool.lease
        def interrupt(attempt_id, repo):
            mark = runner.store.mark(self.agent.name, t["id"], RECEIPT_KEY)
            self.assertEqual(json.loads(mark[0]), {"attempt": attempt_id, "scope": "workspace"})
            if repo == "lib":
                raise WorkspaceError("synthetic interrupted promotion")
            return lease(attempt_id, repo)
        with patch.object(runner.pool, "lease", side_effect=interrupt), patch.object(runner, "_spawn") as spawn:
            runner.tick()
        spawn.assert_not_called()
        partial, = runner.pool.leased_to(first.id)
        mesh.tasks[t["id"]]["labels"].remove(WORKSPACE_REQUIRED)
        restarted = self.runner(mesh)
        with patch.object(restarted.pool, "lease", wraps=restarted.pool.lease) as remaining:
            restarted.tick()
        remaining.assert_called_once_with(first.id, "lib")
        resumed = restarted.store.get(first.id)
        self.assertEqual(resumed.state, "running")
        self.assertEqual(len(resumed.workspaces), 2)
        self.assertIn(partial, resumed.workspaces)
        self.finish_turn(restarted)

    def test_revoked_opt_in_promotes_durably_while_full_and_cannot_be_readded_to_bypass(self):
        t = diagnostic()
        mesh = FakeMesh([t], max_concurrent=1)
        runner, _ = self.full_pool(mesh)
        write_replies(self.replies, {t["id"][:8]: "↻ NEXT"})
        runner.tick()
        first = self.finish_turn(runner)
        mesh.tasks[t["id"]]["labels"].remove("runner:alert-diagnostics")
        restarted = self.runner(mesh)
        with patch.object(restarted, "_spawn") as spawn:
            restarted.tick()
        spawn.assert_not_called()
        scope = restarted.store.mark(self.agent.name, t["id"], RECEIPT_KEY)
        self.assertEqual(json.loads(scope[0]), {"attempt": first.id, "scope": "workspace"})
        mesh.tasks[t["id"]]["labels"].append("runner:alert-diagnostics")
        restarted = self.runner(mesh)
        with patch.object(restarted, "_spawn") as spawn:
            restarted.tick()
        spawn.assert_not_called()
        self.assertEqual(restarted.store.get(first.id).state, "ready")
        restarted.pool.release("other-attempt")
        restarted = self.runner(mesh)
        restarted.tick()
        current = restarted.store.get(first.id)
        self.assertEqual(current.state, "running")
        self.assertEqual(len(current.workspaces), 1)
        self.finish_turn(restarted)
