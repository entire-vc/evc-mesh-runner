"""Auth failure is an operator hold, never a product failure or quota switch."""
import contextlib
import io
import json
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

from mesh_runner import auth
from mesh_runner.adapters.claude import ClaudeAdapter
from mesh_runner.cli import main
from mesh_runner.store import Store
from mesh_runner.workspace import WorkspaceError
from mesh_runner.runner import AgentRunner
from mesh_runner.tui_driver import Driver
from fakes import HarnessGate, FakeMesh, calls, task, write_replies
from test_runner import Bench

EXPIRED = "Failed to authenticate: OAuth session expired and could not be refreshed"


class Banner(unittest.TestCase):
    def test_only_structured_terminal_error_banners_are_auth_failures(self):
        cases = [(EXPIRED, True, True), ("Not logged in · Please run /login", True, True),
                 (EXPIRED, False, False), ("Observed: " + EXPIRED, True, False),
                 ("Failed to authenticate. API Error: 401 Invalid authentication credentials", True, False),
                 ("Not logged in; report quotes Please run /login", True, False),
                 ("The report quotes `" + EXPIRED + "`", True, False)]
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "turn.jsonl"
            for reason, error, expected in cases:
                with self.subTest(reason=reason, error=error):
                    path.write_text(json.dumps({"type": "result", "subtype": "success", "is_error": error,
                                                "result": reason}) + "\n")
                    self.assertEqual(ClaudeAdapter().parse(path).auth_failed, expected)
            path.write_text(json.dumps({"type": "assistant", "message": {"content": [
                {"type": "text", "text": EXPIRED}]}}) + "\n")
            self.assertFalse(ClaudeAdapter().parse(path).auth_failed)
            path.write_text(json.dumps({"type": "result", "subtype": "error_during_execution",
                "errors": ["Long harmless context. " * 30, EXPIRED]}) + "\n")
            self.assertTrue(ClaudeAdapter().parse(path).auth_failed)
            self.assertTrue(ClaudeAdapter().parse(path).auth_only)
            path.write_text(json.dumps({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Bash", "input": {"command": "work"}}]}}) + "\n" +
                json.dumps({"type": "result", "is_error": True, "result": EXPIRED}) + "\n")
            self.assertTrue(ClaudeAdapter().parse(path).auth_failed)
            self.assertFalse(ClaudeAdapter().parse(path).auth_only)
            for where in ("assistant", "result"):
                first = {"type": "assistant", "message": {"content": [{"type": "text", "text": EXPIRED}],
                                                          "usage": {"output_tokens": int(where == "assistant")}}}
                terminal = {"type": "result", "subtype": "success", "is_error": True, "result": EXPIRED,
                            "usage": {"output_tokens": int(where == "result")}}
                path.write_text(json.dumps(first) + "\n" + json.dumps(terminal) + "\n")
                self.assertFalse(ClaudeAdapter().parse(path).auth_only)
            # Observed CLI shape: exact assistant banner + identical terminal result,
            # zero usage and no tools. This is still an authentication-only turn.
            path.write_text(json.dumps({"type": "assistant", "message": {"content": [
                {"type": "text", "text": EXPIRED}], "usage": {"input_tokens": 0, "output_tokens": 0}}}) + "\n" +
                json.dumps({"type": "result", "subtype": "success", "is_error": True,
                            "result": EXPIRED, "usage": {"input_tokens": 0, "output_tokens": 0}}) + "\n")
            self.assertTrue(ClaudeAdapter().parse(path).auth_only)
            path.write_text(json.dumps({"type": "user", "message": {"content": [
                {"type": "tool_result", "content": "work completed"}]}}) + "\n" + path.read_text())
            self.assertFalse(ClaudeAdapter().parse(path).auth_only)

    def test_profile_identity_is_shared_across_lanes_and_models_but_not_profiles(self):
        a = auth.profile_id({"HOME": "/h", "CLAUDE_CONFIG_DIR": "/profile-a", "ANTHROPIC_MODEL": "m1"}, Path("/lane-a"))
        self.assertEqual(a, auth.profile_id({"HOME": "/other", "CLAUDE_CONFIG_DIR": "/profile-a",
                                             "ANTHROPIC_MODEL": "m2"}, Path("/lane-b")))
        self.assertNotEqual(a, auth.profile_id({"CLAUDE_CONFIG_DIR": "/profile-b"}, Path("/lane-a")))
        # Reauth in the same keychain/profile does not magically clear a circuit hold.
        self.assertEqual(auth.profile_id({"HOME": "/h"}, Path("/a")),
                         auth.profile_id({"HOME": "/h"}, Path("/b")))


class StopFailureAuth(unittest.TestCase):
    def test_only_exact_hook_error_has_auth_provenance(self):
        cases = [
            ({"hook_event_name": "StopFailure", "error": "authentication_failed"}, True),
            ({"hook_event_name": "StopFailure", "error": "rate_limit_error"}, False),
            ({"hook_event_name": "StopFailure", "message": "authentication_failed"}, False),
            ({"hook_event_name": "StopFailure", "reason": "authentication_failed"}, False),
            ({"hook_event_name": "StopFailure", "error": "quoted authentication_failed"}, False),
            ({"hook_event_name": "Stop", "error": "authentication_failed"}, False),
        ]
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            transcript = root / "transcript.jsonl"
            transcript.write_text(json.dumps({"type": "assistant", "uuid": "a", "message": {
                "content": [{"type": "text", "text": "authentication_failed"}], "usage": {}}}) + "\n")
            for event, expected in cases:
                with self.subTest(event=event):
                    out = io.StringIO()
                    driver = Driver(harness="claude", agent="test", state=root, tmux=object(), max_sessions=1,
                                    cwd=root, session_id="s", mode="resume", key="test", argv=["claude"],
                                    out=out, env={})
                    driver.slice_turn = lambda entries: entries
                    driver.emit_claude({"event": {**event, "transcript_path": str(transcript)}, "offset": 0})
                    path = root / "turn.jsonl"
                    path.write_text(out.getvalue())
                    terminal = json.loads(out.getvalue().splitlines()[-1])
                    self.assertEqual("runner_error" in terminal, expected)
                    self.assertEqual(ClaudeAdapter().parse(path).auth_failed, expected)

    def test_plain_text_or_untrusted_provenance_does_not_hold_auth(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "turn.jsonl"
            for marker in (None, "authentication_failed", {"source": "assistant", "code": "authentication_failed"},
                           {"source": "claude.StopFailure", "code": "rate_limit_error"}):
                for is_error in (False, True):
                    event = {"type": "result", "is_error": is_error, "result": "authentication_failed"}
                    if marker is not None:
                        event["runner_error"] = marker
                    path.write_text(json.dumps(event) + "\n")
                    self.assertFalse(ClaudeAdapter().parse(path).auth_failed)
            path.write_text(json.dumps({"type": "result", "is_error": False, "result": "authentication_failed",
                "runner_error": {"source": "claude.StopFailure", "code": "authentication_failed"}}) + "\n")
            self.assertFalse(ClaudeAdapter().parse(path).auth_failed)


class AuthLoop(Bench):
    def test_typed_stop_failure_at_failure_cap_holds_without_handoff_or_budget_spend(self):
        t = task(31)
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.runner(mesh)
        r.refresh_me()
        a = r.store.create(self.agent.name, t["id"], "claude")
        profile = auth.profile_id(r._fresh_env(), self.agent.profile_dir)
        path = self.root / "typed-auth.jsonl"
        path.write_text(json.dumps({"type": "result", "session_id": "s", "subtype": "error_during_execution",
            "is_error": True, "result": "authentication_failed", "usage": {},
            "runner_error": {"source": "claude.StopFailure", "code": "authentication_failed"}}) + "\n")
        r.store.mark(self.agent.name, t["id"], "auth_launch",
                     json.dumps({"attempt": a.id, "turn": 4, "profile": profile}))
        r.store.update(a, state="running", turns=4, no_outcome=3, log_path=str(path))
        before = dict(mesh.tasks[t["id"]])
        r._hand_up = lambda *args, **kwargs: self.fail("auth failure must not hand up product work")
        r._switch_on_limit = lambda *args, **kwargs: self.fail("auth failure must not switch accounts")
        r.settle(r.store.get(a.id))
        held = r.store.get(a.id)
        self.assertEqual((held.state, held.no_outcome, held.turns), ("ready", 3, 4))
        self.assertEqual(r.store.auth_turns(a.id), 1)
        self.assertTrue(auth.held(r.store, profile))
        self.assertEqual(mesh.tasks[t["id"]], before)
        self.assertEqual(mesh.comment_log, {})

    def settle_auth_after_task_change(self, change):
        t = task(30)
        mesh = FakeMesh([t], max_concurrent=1)
        self.agent.inline_env["CLAUDE_CONFIG_DIR"] = str(self.root / "auth-at-launch")
        r = self.runner(mesh)
        r.refresh_me()
        a = r.store.create(self.agent.name, t["id"], "claude")
        profile = auth.profile_id(r._fresh_env(), self.agent.profile_dir)
        path = self.root / f"{a.id}.jsonl"
        path.write_text(json.dumps({"type": "result", "subtype": "error_during_execution",
                                    "is_error": True, "errors": [EXPIRED]}) + "\n")
        r.store.mark(self.agent.name, t["id"], "auth_launch",
                     json.dumps({"attempt": a.id, "turn": 1, "profile": profile}))
        r.store.update(a, state="running", turns=1, log_path=str(path))

        change(mesh, t)
        changed_task = dict(mesh.tasks[t["id"]])
        r.settle(r.store.get(a.id))

        self.assertTrue(auth.held(r.store, profile))
        self.assertEqual(r.status()["auth_holds"][0]["profile"], profile)
        self.assertEqual(mesh.tasks[t["id"]], changed_task)
        self.assertEqual(mesh.comment_log, {})
        self.assertEqual(r.store.get(a.id).state, "completed")
        self.assertIn("auth_failed", [event[1] for event in r.store.events(a.id)])

    def expire(self, *, turns=0):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        self.agent.inline_env["CLAUDE_CONFIG_DIR"] = str(self.root / "auth-a")
        switched = self.root / "switched"
        self.agent.on_limit_command = [sys.executable, "-c", f"open({str(switched)!r},'w').write('bad')"]
        released = self.root / "released"
        self.agent.start_gate_command = [sys.executable, "-c", "print('RES_ID=auth-reservation')"]
        self.agent.turn_end_command = [sys.executable, "-c",
            f"open({str(released)!r},'a').write('released\\n')"]
        write_replies(self.replies, {t["id"][:8]: "ERROR:" + EXPIRED})
        r = self.runner(mesh)
        if turns:
            r.refresh_me()
            a = r.store.create(self.agent.name, t["id"], "claude")
            paths = [str(r.pool.lease(a.id, repo.name)) for repo in self.agent.repos]
            r.store.update(a, state="ready", turns=turns, workspaces=paths)
        r.tick()
        self.settle_all(r)
        a = r.store.by_state(self.agent.name, ("ready",))[0]
        self.assertFalse(switched.exists())
        self.assertEqual((a.no_outcome, a.wait, a.checkout), (0, {}, {}))
        self.assertEqual(mesh.checkouts, {})
        self.assertEqual(r._gate_of(a), {})
        self.assertTrue(released.exists())
        self.assertTrue(a.workspaces)
        self.assertEqual(r.pool.rows()[0]["state"], "leased")
        self.assertEqual(mesh.comment_log, {})
        self.assertEqual(r._starts_paused_until, 0)
        return r, mesh, t, a, r.status()["auth_holds"][0]["profile"]

    def test_same_profile_is_held_across_restart_but_another_profile_runs(self):
        r, mesh, t, a, profile = self.expire()
        self.assertEqual(a.turns, 1)
        self.assertIn(profile, json.dumps(r.plan()))
        self.assertNotIn(str(self.root / "auth-a"), json.dumps(r.status()))
        # No local in-memory cooldown survives this restart: the durable hold must.
        r = self.runner(mesh)
        for _ in range(3):
            r._resume_hold.clear()
            r.tick()
        self.assertEqual(len(calls(self.calls)), 1)
        self.assertEqual(r.store.get(a.id).turns, 1)
        self.assertFalse(r._quarantine)
        self.assertFalse(mesh.checkouts)
        write_replies(self.replies, {t["id"][:8]: "⏳ WAIT 2h: soak window"})
        self.agent.inline_env["CLAUDE_CONFIG_DIR"] = str(self.root / "auth-b")
        r._resume_hold.clear()
        r.tick()
        self.settle_all(r)
        self.assertEqual((r.store.get(a.id).state, r.store.get(a.id).turns), ("waiting", 2))
        self.assertTrue(auth.held(r.store, profile))

    def test_explicit_clear_recovers_same_profile_without_spending_product_turn_cap(self):
        self.cfg.max_turns = 3
        r, mesh, t, a, profile = self.expire(turns=1)
        self.assertEqual((a.turns, r.store.auth_turns(a.id)), (2, 1))
        # Dirty progress and physical turn history survive reauth and explicit clear.
        progress = Path(a.workspaces[0]) / "progress.txt"
        progress.write_text("keep work")
        self.assertTrue(auth.clear(r.store, profile))
        self.assertFalse(auth.clear(r.store, profile))
        write_replies(self.replies, {t["id"][:8]: "⏳ WAIT 2h: soak window"})
        r._resume_hold.clear()
        r.tick()
        self.settle_all(r)
        a = r.store.get(a.id)
        self.assertEqual((a.state, a.turns), ("waiting", 3))
        self.assertEqual(progress.read_text(), "keep work")
        self.assertEqual(r.status()["auth_holds"], [])
        self.assertEqual(r.store.outcomes_today(self.agent.name, t["id"]), {"wait": 1})

    def test_auth_after_real_work_keeps_spent_cap_and_clear_cannot_spawn_an_extra_turn(self):
        self.cfg.max_turns = 1
        r, mesh, t, a, profile = self.expire()
        path = Path(a.log_path)
        path.write_text(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": "work"}}]}}) + "\n" + path.read_text())
        # Re-settle as the mixed-work stream, without a synthetic prior auth-only discount.
        r.store.db.execute("DELETE FROM events WHERE attempt=? AND kind='auth_only_turn'", (a.id,))
        r.store.update(a, state="running")
        r.settle(a)
        self.assertEqual((r.store.get(a.id).state, r.store.auth_turns(a.id)), ("ready", 0))
        self.assertTrue(auth.clear(r.store, profile))
        r._resume_hold.clear()
        r.tick()
        self.assertEqual((r.store.get(a.id).state, r.store.get(a.id).turns), ("failed", 1))
        self.assertEqual(len(calls(self.calls)), 1)

    def test_held_profile_does_not_restore_suspended_checkpoint_or_lose_its_context(self):
        r, mesh, t, a, profile = self.expire()
        original_session = a.session_id
        metadata = {"session_id": a.session_id, "harness": a.harness,
                    "cwd": str(self.agent.profile_dir), "workspaces": a.workspaces, "wait": {"kind": "card"}}
        r.pool.suspend(a.id, metadata, writer_stopped=True)
        r.store.update(a, state="ready", wake_text="dependency finished; keep this context")
        r._resume_hold.clear()
        r._resume_one(a)
        self.assertEqual(r.pool.leased_to(a.id), [])
        self.assertEqual([row["phase"] for row in r.pool._checkpoint_rows(a.id)], ["released"])
        self.assertEqual(r.store.get(a.id).session_id, original_session)
        auth.clear(r.store, profile)
        write_replies(self.replies, {t["id"][:8]: "⏳ WAIT 2h: soak window"})
        r._resume_hold.clear()
        r.tick()
        self.settle_all(r)
        self.assertEqual(r.store.get(a.id).state, "waiting")
        prompt = calls(self.calls)[-1]["prompt"]
        self.assertIn(original_session, prompt)
        self.assertIn("dependency finished; keep this context", prompt)
        self.assertIn("Restored verified WAIT checkpoint", prompt)

    def test_resume_of_suspended_checkpoint_waits_for_a_free_worktree_without_an_error(self):
        r, mesh, t, a, profile = self.expire()
        metadata = {"session_id": a.session_id, "harness": a.harness,
                    "cwd": str(self.agent.profile_dir), "workspaces": a.workspaces, "wait": {"kind": "card"}}
        r.pool.suspend(a.id, metadata, writer_stopped=True)
        r.store.update(a, state="ready")
        auth.clear(r.store, profile)
        r._resume_hold.clear()
        r.pool.full = lambda name: True                  # every slot of the pool is leased
        def no_slot(*args, **kw):
            raise WorkspaceError("no free worktree for app (max 1; see `mesh-runner slots`)")
        r.pool.lease = no_slot
        r._resume_one(a)                                 # held, not raised: no error:resume strike
        self.assertEqual(r.store.get(a.id).state, "ready")
        self.assertGreater(r._resume_hold[a.id], time.monotonic())
        self.assertEqual([row["phase"] for row in r.pool._checkpoint_rows(a.id)], ["released"])

    def test_shared_profile_hold_blocks_second_lane_but_leaves_its_running_writer_alone(self):
        t2 = task(2)
        mesh2 = FakeMesh([t2], max_concurrent=1)
        other_replies = self.root / "other-replies.json"
        other = replace(self.agent, name="other", inline_env=dict(self.agent.inline_env,
            CLAUDE_CONFIG_DIR=str(self.root / "auth-a"), FAKE_REPLIES=str(other_replies)))
        r2 = AgentRunner(self.cfg, other, Store(self.root / "state" / "r.sqlite"), mesh=mesh2,
                        adapter=self.adapter)
        sibling = HarnessGate(self.root / "sibling")
        write_replies(other_replies, {t2["id"][:8]: sibling.reply("⏳ WAIT 2h: soak window")})
        r2.tick()
        a2 = r2.store.by_state("other", ("running",))[0]
        with sibling.hold(r2.procs[a2.id]):
            r, mesh, t, a, profile = self.expire()
            # Even a slow sibling failure must leave the existing writer alive.
            self.assertEqual(r2.store.get(a2.id).state, "running")
            self.assertIn(a2.id, r2.procs)
            self.assertIsNone(r2.procs[a2.id].poll())
        deadline = time.time() + 10
        while time.time() < deadline and r2.store.by_state("other", ("running", "starting")):
            r2.tick()
            time.sleep(0.1)
        self.assertEqual(r2.store.get(a2.id).state, "waiting")
        r2.store.update(a2, state="ready")
        before = len(calls(self.calls))
        r2._resume_one(a2)
        self.assertEqual((r2.store.get(a2.id).state, r2.store.get(a2.id).turns), ("ready", 1))
        self.assertEqual(len(calls(self.calls)), before)
        self.assertEqual(r2.store.get(a2.id).checkout, {})

    def test_unrecorded_preupgrade_launch_holds_attempt_until_explicit_clear(self):
        r, mesh, t, a, known_profile = self.expire()
        auth.clear(r.store, known_profile)
        r.store.mark(self.agent.name, t["id"], "auth_launch", "")
        r.store.update(a, state="running")
        r.settle(a)
        unknown_profile = r.status()["auth_holds"][0]["profile"]
        self.assertNotEqual(unknown_profile, known_profile)
        self.assertEqual(r.store.auth_turns(a.id), 1)   # replay did not multiply the discount
        r = self.runner(mesh)
        r.tick()
        self.assertEqual((r.store.get(a.id).state, len(calls(self.calls))), ("ready", 1))
        self.assertTrue(auth.clear(r.store, unknown_profile))
        r._resume_hold.clear()
        write_replies(self.replies, {t["id"][:8]: "⏳ WAIT 2h: soak window"})
        r.tick()
        self.settle_all(r)
        self.assertEqual((r.store.get(a.id).state, r.store.get(a.id).turns), ("waiting", 2))

    def test_new_task_on_held_profile_does_not_fill_pool_or_change_product_status(self):
        r, mesh, t, a, profile = self.expire()
        second = task(2)
        mesh.tasks[second["id"]] = second
        original_status = second["status_id"]
        before = r.pool.rows()
        self.assertFalse(r._start_one(second))
        self.assertEqual(r.pool.rows(), before)
        self.assertEqual(mesh.task(second["id"])["status_id"], original_status)
        self.assertFalse(mesh.checkouts)
        self.assertEqual(len(calls(self.calls)), 1)
        self.assertIsNone(r.store.open_for_task(self.agent.name, second["id"]))

    def test_ownership_change_while_auth_held_does_not_restart_or_hand_up(self):
        r, mesh, t, a, profile = self.expire()
        mesh.set_category(t["id"], "done")
        r._resume_hold.clear()
        r.tick()
        self.assertEqual(r.store.get(a.id).state, "completed")
        self.assertEqual(len(calls(self.calls)), 1)
        self.assertTrue(auth.held(r.store, profile))

    def test_terminal_auth_failure_holds_launched_profile_after_owner_changes(self):
        def reassign(mesh, t):
            mesh.assign(t["id"], "agent-lead")
        self.settle_auth_after_task_change(reassign)

    def test_terminal_auth_failure_holds_launched_profile_after_status_changes(self):
        def complete(mesh, t):
            mesh.set_category(t["id"], "done")
        self.settle_auth_after_task_change(complete)


class ClearCommand(unittest.TestCase):
    def test_status_gives_exact_clear_command_and_cli_clears_only_named_profile(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = Path(root) / "runner.toml"
            cfg.write_text(f'[runner]\nmesh_url="http://unused"\nstate_dir={json.dumps(root)}\n')
            store = Store(Path(root) / "runner.sqlite")
            a, b = "a" * 32, "b" * 32
            auth.hold(store, a)
            auth.hold(store, b)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(main(["status", "--config", str(cfg)]), 0)
            rows = json.loads(out.getvalue())
            self.assertIn("--profile " + a, rows[0]["auth_hold"]["clear_command"])
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["clear-auth-hold", "--config", str(cfg), "--profile", a]), 0)
            self.assertFalse(auth.held(store, a))
            self.assertTrue(auth.held(store, b))
