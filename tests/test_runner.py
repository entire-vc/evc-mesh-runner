"""End-to-end loop with an in-memory Mesh and real child processes."""
import datetime as dt
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

from mesh_runner.mesh import MeshError
from mesh_runner.outcome import Outcome
from fakes import AGENT_ID, FAKE_AGENT, HarnessGate, FakeAdapter, FakeProfileAdapter, FakeGitLab, FakeMesh, calls, task, write_replies, FUTURE_AT
from test_workspace import make_repo

from mesh_runner.config import AgentConfig, RunnerConfig
from mesh_runner.runner import AgentRunner, AlreadyRunning, Runner, public_harness_error, startup_error_excerpt
from mesh_runner.store import Store
from mesh_runner.workspace import RepoSpec


class Bench(unittest.TestCase):
    """Shared fixture: temp repo, fake agent, in-memory Mesh. No tests here."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.root = root
        base = make_repo(root)
        script = root / "agent.py"
        script.write_text(FAKE_AGENT)
        self.replies, self.calls = root / "replies.json", root / "calls.jsonl"
        profile = root / "profile"
        profile.mkdir()
        self.agent = AgentConfig(name="builder", key="k", harness="claude", profile_dir=profile,
                                 repos=[RepoSpec("app", base, max_slots=4)],
                                 env={"FAKE_REPLIES": str(self.replies), "FAKE_CALLS": str(self.calls)})
        self.cfg = RunnerConfig(mesh_url="http://mesh", state_dir=root / "state", agents=[self.agent],
                                wake_poll_seconds=0, heartbeat_seconds=0,
                                load_hold_per_cpu=0)       # the host's real load must not hold a test's starts
        self.adapter = FakeAdapter(script)

    def tearDown(self):
        self.tmp.cleanup()

    def runner(self, mesh, gitlab=None):
        return AgentRunner(self.cfg, self.agent, Store(self.root / "state" / "r.sqlite"), mesh=mesh,
                           adapter=self.adapter, gitlab=gitlab)

    def settle_all(self, r, timeout=10, *, skip8=None, total_timeout=120):
        """Bound idle polling per turn, not synchronous tick/workspace work.

        A tick can settle one child and start the next. Each new set of active
        turns gets its own wait budget; slow tick work must not consume it
        before that child has even had a chance to exit.
        """
        remaining = timeout
        previous = None
        # Independent ceiling catches endless turn churn and slow ticks once
        # they return; the test command timeout still bounds a blocked syscall.
        deadline = time.monotonic() + total_timeout
        while True:
            r.tick()
            if time.monotonic() >= deadline:
                self.fail(f"settling exceeded {total_timeout}s total budget")
            active = tuple((a.id, a.turns) for a in r.store.by_state(
                self.agent.name, ("starting", "running")) if a.task8 != skip8)
            if not active:
                return
            if active != previous:
                remaining = timeout
                previous = active
            if remaining <= 0:
                self.fail(f"turns did not finish: {active}")
            started = time.monotonic()
            time.sleep(min(0.1, remaining))
            remaining -= time.monotonic() - started

    def finish_turn(self, r):
        """Wait for one real harness, then exercise production process-group settling."""
        active = r.store.by_state(self.agent.name, ("running",))
        self.assertEqual(len(active), 1)
        a = active[0]
        proc = r.procs[a.id]
        try:
            self.assertEqual(proc.wait(timeout=10), 0)
            r.settle_finished()
            return r.store.get(a.id)
        finally:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=10)

    def states(self, r):
        return {a.task8: a.state for a in r.store.by_state(self.agent.name, (
            "starting", "running", "waiting", "ready", "completed", "failed", "aborted"))}


class SettleSynchronization(Bench):
    def test_slow_ticks_do_not_spend_the_next_child_wait_budget(self):
        t1, t2 = task(1), task(2)
        mesh = FakeMesh([t1, t2], max_concurrent=1)
        write_replies(self.replies, {t1["id"][:8]: f"⏳ WAIT {FUTURE_AT}: external=fixture-window source=card:#abcdef12 first",
                                     t2["id"][:8]: f"⏳ WAIT {FUTURE_AT}: external=fixture-window source=card:#abcdef12 second"})
        r = self.runner(mesh)
        r.tick()
        real_tick = r.tick
        elapsed = 0

        def slow_tick():
            nonlocal elapsed
            real_tick()
            elapsed += 10.05

        # Virtual tick cost deterministically crosses the old shared deadline,
        # without delaying the real children or mocking production process settling.
        wall, mono, sleep = time.time, time.monotonic, time.sleep
        clock = mock.Mock()
        clock.time.side_effect = lambda: wall() + elapsed
        clock.monotonic.side_effect = lambda: mono() + elapsed
        clock.sleep.side_effect = sleep
        with mock.patch.object(r, "tick", side_effect=slow_tick), \
                mock.patch(__name__ + ".time", clock):
            self.settle_all(r)
        self.assertEqual(self.states(r), {t1["id"][:8]: "waiting", t2["id"][:8]: "waiting"})
        self.assertEqual(len(calls(self.calls)), 2)

    def test_unfinished_turn_still_exhausts_its_idle_budget(self):
        a = mock.Mock(id=1, turns=1, task8="00000001")
        r = mock.Mock()
        r.store.by_state.return_value = [a]
        clock = mock.Mock()
        clock.monotonic.side_effect = [0, 0, 0, 0.1, 0.1, 0.1, 0.2, 0.2]
        with mock.patch(__name__ + ".time", clock):
            with self.assertRaisesRegex(AssertionError, "turns did not finish"):
                self.settle_all(r, timeout=0.2)
        self.assertEqual(r.tick.call_count, 3)

    def test_new_turns_cannot_reset_the_total_budget(self):
        r = mock.Mock()
        r.store.by_state.side_effect = [[mock.Mock(id=1, turns=1, task8="1")],
                                       [mock.Mock(id=2, turns=1, task8="2")]]
        clock = mock.Mock()
        clock.monotonic.side_effect = [0, 0, 0, 0.1, 0.1, 0.1, 0.2, 1]
        with mock.patch(__name__ + ".time", clock):
            with self.assertRaisesRegex(AssertionError, "total budget"):
                self.settle_all(r, total_timeout=1)
        self.assertEqual(r.tick.call_count, 3)


class Loop(Bench):
    def error_envelope(self, r, t, result, *, failures=0, errors=None, turns=1):
        """Use the production parser and settle path, without starting a harness."""
        r.refresh_me()
        a = r.store.create(self.agent.name, t["id"], "claude")
        path = self.root / f"{a.id}.jsonl"
        path.write_text(json.dumps({"type": "result", "subtype": "success", "is_error": True,
                                    "result": result, "errors": errors or [],
                                    "session_id": "s-error"}) + "\n")
        r.store.update(a, state="running", turns=turns, no_outcome=failures, log_path=str(path))
        r.settle(a)
        return r.store.get(a.id)

    def test_auth_error_is_infrastructure_hold_even_at_failure_and_turn_caps(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        self.cfg.max_turns = 1
        r = self.runner(mesh)
        a = self.error_envelope(r, t,
            "Failed to authenticate: OAuth session expired and could not be refreshed",
            failures=self.cfg.max_failures)
        self.assertEqual((a.state, a.turns, a.no_outcome), ("ready", 1, self.cfg.max_failures))
        self.assertEqual(a.wait, {})
        self.assertEqual(mesh.comment_log, {})
        self.assertIn("authentication held", a.note)

    def test_success_error_envelope_is_not_accepted_as_a_wait(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.runner(mesh)
        a = self.error_envelope(r, t, f"⏳ WAIT {FUTURE_AT}: external=fixture-window source=card:#abcdef12 token=example-private-value")
        self.assertEqual(a.state, "ready")
        self.assertEqual(a.no_outcome, 1)
        self.assertEqual(a.wait, {})
        self.assertNotIn("example-private-value", a.wake_text)
        self.assertIn("harness error [sha256:", a.wake_text)
        self.assertEqual(r.store.outcomes_today(self.agent.name, t["id"]), {})

    def test_success_error_envelope_keeps_failure_cap_and_private_reason_local(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.runner(mesh)
        a = self.error_envelope(r, t, "arbitrary-secret-without-label", failures=self.cfg.max_failures)
        self.assertEqual(a.state, "failed")
        bodies = " ".join(c["body"] for c in mesh.comment_log.get(t["id"], []))
        self.assertIn("4 failed turns: harness error [sha256:", bodies)
        self.assertNotIn("arbitrary-secret-without-label", bodies + a.note)

    def startup_failure(self, r, a, stderr):
        """One more turn of attempt ``a`` whose stream never began: no session id, only stderr."""
        path = self.root / f"{a.id}.jsonl"
        path.write_text("")
        path.with_suffix(".stderr").write_text(stderr)
        r.store.update(a, state="running", turns=a.turns + 1, log_path=str(path), session_id="pre-assigned")
        r.settle(r.store.get(a.id))
        return r.store.get(a.id)

    def test_session_less_failures_stop_after_two_turns_with_error_excerpt(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.runner(mesh)
        r.refresh_me()
        a = r.store.create(self.agent.name, t["id"], "claude")
        err = "prompt could not be pasted into the window token=abc123secretvalue"
        a = self.startup_failure(r, a, err)
        self.assertEqual((a.state, a.turns), ("ready", 1))        # first failure: one retry
        a = self.startup_failure(r, a, err)
        self.assertEqual((a.state, a.turns), ("failed", 2))       # second: handed up, no 3rd/4th turn
        bodies = " ".join(c["body"] for c in mesh.comment_log.get(t["id"], []))
        self.assertIn("did not start a session in 2 turns", bodies)
        self.assertIn("prompt could not be pasted", bodies)
        self.assertNotIn("abc123secretvalue", bodies + a.note)

    def test_startup_error_excerpt_masks_compound_credential_labels(self):
        for leak, value in (("api_key=short1", "short1"), ("OPENAI_API_KEY: abc", "abc"),
                            ("client_secret=xyz", "xyz"), ("db_password=pw1", "pw1"), ("AUTH_TOKEN=t0k", "t0k")):
            with self.subTest(leak=leak):
                out = startup_error_excerpt(f"boom {leak} tail")
                self.assertNotIn(value, out)
                self.assertIn("[redacted]", out)
        self.assertEqual(len(startup_error_excerpt("x " * 400)), 300)

    def test_public_harness_error_only_exposes_fixed_categories(self):
        for reason, category in (("Usage limit reached Bearer superprivate", "provider limit or overload"),
                                 ("Prompt is too long https://user:password@host", "context limit"),
                                 ("No conversation found with session ID: private", "session not found"),
                                 ("ERROR: No saved session found with ID private. Run `codex resume` without an ID",
                                  "session not found")):
            with self.subTest(category=category):
                public = public_harness_error(reason)
                self.assertTrue(public.startswith(category + " [sha256:"))
                self.assertNotIn("superprivate", public)
                self.assertNotIn("password", public)
                self.assertNotIn("ID: private", public)

    def test_long_error_details_preserve_quota_policy_and_session_guards(self):
        for i, signal in enumerate(("Usage limit reached", "flagged for possible cybersecurity risk",
                                    "No conversation found"), 1):
            with self.subTest(signal=signal):
                t = task(i)
                mesh = FakeMesh([t], max_concurrent=1)
                r = self.runner(mesh)
                a = self.error_envelope(r, t, "Long harmless provider context. " * 20,
                                        errors=["Long harmless error context. " * 20, signal], turns=2)
                if i == 1:
                    self.assertEqual((a.state, a.no_outcome), ("ready", 1))
                    self.assertGreater(r._starts_paused_until, time.monotonic())
                    self.assertIn("provider limit or overload", a.wake_text)
                elif i == 2:
                    self.assertEqual(a.state, "failed")
                    self.assertIn("content policy refusal", a.note)
                else:
                    self.assertEqual((a.state, a.session_id, a.no_outcome), ("ready", "", 0))
                    self.assertEqual(r.store.mark(self.agent.name, t["id"], "session_restart")[0], a.id)

    def test_codex_missing_saved_session_starts_the_task_over(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.runner(mesh)
        a = self.error_envelope(r, t, "interactive session exited at start",
                                errors=["ERROR: No saved session found with ID 0199aaaa-bbbb. "
                                        "Run `codex resume` without an ID to choose from existing sessions."], turns=2)
        self.assertEqual((a.state, a.session_id, a.no_outcome), ("ready", "", 0))
        self.assertEqual(r.store.mark(self.agent.name, t["id"], "session_restart")[0], a.id)

    def test_unsupported_child_resume_restarts_once_then_hands_up(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        err = "ERROR: -32600 cannot resume child thread: multi-agent v2 not loaded, parent first"
        r = self.runner(mesh)
        r.refresh_me()
        a = r.store.create(self.agent.name, t["id"], "claude")
        path = self.root / f"{a.id}.jsonl"
        path.write_text(json.dumps({"type": "result", "subtype": "success", "is_error": True,
                                    "result": "x", "errors": [err], "session_id": "s-error"}) + "\n")
        r.store.update(a, state="running", turns=2, log_path=str(path))
        r.settle(a)
        a = r.store.get(a.id)
        self.assertEqual((a.state, a.session_id, a.no_outcome), ("ready", "", 0))   # own task, fresh thread
        r.store.update(a, state="running", turns=3, session_id="s-error")
        r.settle(a)
        self.assertNotEqual(r.store.get(a.id).state, "ready")                       # not four unchanged resumes

    def test_a_bare_invalid_request_code_is_not_a_child_resume(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.runner(mesh)
        a = self.error_envelope(r, t, "x", errors=["ERROR: -32600 invalid request: bad params"], turns=2)
        self.assertEqual(r.store.mark(self.agent.name, t["id"], "session_restart"), None)

    def test_cap_bounds_parallel_sessions_and_waits_free_slots(self):
        t = [task(1, "high"), task(2), task(3, "low")]
        mesh = FakeMesh(t, max_concurrent=2)
        write_replies(self.replies, {x["id"][:8]: f"⏳ WAIT {FUTURE_AT}: external=fixture-window source=card:#abcdef12 CI" for x in t})
        r = self.runner(mesh)
        r.tick()
        started = {a.task8 for a in r.store.by_state("builder", ("running", "starting"))}
        self.assertEqual(started, {t[0]["id"][:8], t[1]["id"][:8]})       # priority order, cap 2
        self.settle_all(r)
        r.tick()                                                           # slots freed by WAIT
        self.settle_all(r)
        st = self.states(r)
        self.assertEqual(set(st.values()), {"waiting"})
        self.assertEqual(len(st), 3)
        self.assertEqual(mesh.checkouts, {})                               # waiting holds no checkout
        self.assertEqual(mesh.category(mesh.tasks[t[0]["id"]]), "in_progress")

    def test_wake_resumes_the_same_session(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        write_replies(self.replies, {t["id"][:8]: "⏳ WAIT decision:@lead", t["id"][:8] + ":resume": f"⏳ WAIT {FUTURE_AT}: external=fixture-window source=card:#abcdef12 soak window"})
        r = self.runner(mesh)
        r.tick()
        self.settle_all(r)
        self.assertEqual(self.states(r)[t["id"][:8]], "waiting")
        mesh.add_comment(t["id"], "SHIP: looks good", author="lead")
        r.tick()                     # wake + resume
        self.settle_all(r)
        c = calls(self.calls)
        self.assertEqual([x["resume"] for x in c][:2], [False, True])
        self.assertEqual(c[0]["sid"], c[1]["sid"])

    def test_task_moved_to_review_completes_and_returns_worktree(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        write_replies(self.replies, {t["id"][:8]: "done"})
        r = self.runner(mesh)
        r.tick()
        a = r.store.by_state("builder", ("running",))[0]
        mesh.set_category(t["id"], "review")
        self.settle_all(r)
        self.assertEqual(r.store.get(a.id).state, "completed")
        self.assertEqual(r.pool.leased_to(a.id), [])
        self.assertEqual(mesh.checkouts, {})
        line = [c["body"] for c in mesh.comment_log[t["id"]] if "completed in" in c["body"]][0]
        self.assertRegex(line, r"^\[runner\] ✅ completed in ~\d+m · session=\w+ · 1 turns · ~\$[\d.]+ notional "
                               r"\(out [\d.]+k tok, cache_rd [\d.]+M\)$")

    def test_background_child_keeps_the_slot_until_it_exits(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        gate = self.root / "background"
        self.agent.inline_env["FAKE_BG_GATE"] = str(gate)
        write_replies(self.replies, {t["id"][:8]: f"BG:GATE:⏳ WAIT {FUTURE_AT}: external=fixture-window source=card:#abcdef12 soak window"})
        r = self.runner(mesh)
        r.tick()
        a = r.store.by_state(self.agent.name, ("running",))[0]
        proc = r.procs[a.id]
        try:
            # Observe the child handshake and reap the parent, independent of CI speed.
            deadline = time.monotonic() + 10
            while not gate.with_suffix(".started").exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(gate.with_suffix(".started").exists(), "background child did not start")
            self.assertEqual(proc.wait(timeout=10), 0)
            r.tick()
            self.assertEqual(self.states(r)[t["id"][:8]], "running")
            self.assertTrue(r.pool.leased_to(a.id))
        finally:
            gate.with_suffix(".released").touch()
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=10)
        self.settle_all(r)
        self.assertEqual(self.states(r)[t["id"][:8]], "waiting")

    def test_missing_outcome_is_asked_once_then_released(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        write_replies(self.replies, {t["id"][:8]: "I did some work."})
        r = self.runner(mesh)
        r.tick()
        self.assertEqual(self.finish_turn(r).state, "ready")  # asked once, same session
        r.tick()
        self.finish_turn(r)       # the second unreadable outcome exhausts the correction
        self.assertEqual(self.states(r)[t["id"][:8]], "failed")
        self.assertEqual([c["resume"] for c in calls(self.calls)], [False, True])
        self.assertTrue(any("without a readable outcome" in c["body"] for c in mesh.comment_log[t["id"]]))
        self.assertEqual(mesh.category(mesh.tasks[t["id"]]), "todo")   # not left looking taken
        self.assertEqual(mesh.checkouts, {})
        r.tick()                                   # not retried while nobody wrote on the task
        self.assertEqual(r.store.by_state("builder", ("running", "starting")), [])
        mesh.add_comment(t["id"], "try again with the new spec", author="lead")
        r.tick()                                   # a new comment from someone else re-opens it
        self.assertEqual(len(r.store.by_state("builder", ("running", "starting"))), 1)
        # the third turn in a row without a readable outcome (no lead above: released again)
        self.assertEqual(self.finish_turn(r).state, "failed")

    def test_checkout_refused_aborts_without_spawning(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        mesh.refuse_checkout.add(t["id"])
        r = self.runner(mesh)
        r.tick()
        self.assertEqual(self.states(r)[t["id"][:8]], "aborted")
        self.assertEqual(calls(self.calls), [])

    def test_open_blocker_holds_the_task(self):
        blocker, t = task(1, cat="in_progress"), task(2)
        blocker["assignee_id"] = "someone-else"
        mesh = FakeMesh([blocker, t], max_concurrent=1)
        mesh.deps[t["id"]] = [{"dependency_type": "blocks", "depends_on_task_id": blocker["id"]}]
        r = self.runner(mesh)
        r.tick()
        self.assertEqual(self.states(r), {})
        mesh.set_category(blocker["id"], "done")
        write_replies(self.replies, {t["id"][:8]: f"⏳ WAIT {FUTURE_AT}: external=fixture-window source=card:#abcdef12 soak window"})
        r._hold_cache.clear()                     # the dependency verdict is re-read after its TTL
        r.tick()
        self.assertIn(t["id"][:8], self.states(r))
        self.settle_all(r)                        # the started turn must end before tearDown removes its dir

    def test_gated_scheduled_parked_and_held_tasks_are_skipped(self):
        later = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)).isoformat()
        tasks = [task(1, human_gate=True), task(2, start_after=later), task(3, labels=["park:external"]),
                 task(4, checked_out_by="other-writer", checkout_expires=later)]
        mesh = FakeMesh(tasks, max_concurrent=4)
        r = self.runner(mesh)
        r.tick()
        self.assertEqual(self.states(r), {})


class ReviewFindings(Bench):
    def test_one_broken_task_does_not_stop_the_others_or_heartbeats(self):
        t1, t2 = task(1, "high"), task(2)
        mesh = FakeMesh([t1, t2], max_concurrent=2)
        write_replies(self.replies, {t1["id"][:8]: f"SLEEP:1:⏳ WAIT {FUTURE_AT}: external=fixture-window source=card:#abcdef12 soak window", t2["id"][:8]: f"SLEEP:3:⏳ WAIT {FUTURE_AT}: external=fixture-window source=card:#abcdef12 soak window"})
        r = self.runner(mesh)
        r.tick()
        mesh.broken_tasks.add(t1["id"])           # settling t1 now fails every tick
        time.sleep(1.3)
        for _ in range(3):
            r.tick()
        self.assertGreater(mesh.heartbeats, 0)
        self.settle_all_but(r, t1["id"][:8])
        self.assertEqual(self.states(r)[t2["id"][:8]], "waiting")

    def settle_all_but(self, r, skip8, timeout=10):
        self.settle_all(r, timeout=timeout, skip8=skip8)

    def test_card_closed_during_a_turn_stops_the_session_and_releases_the_gate(self):
        t1 = task(1)
        mesh = FakeMesh([t1], max_concurrent=1)
        write_replies(self.replies, {t1["id"][:8]: "SLEEP:30:done"})
        gate = self.root / "gate.log"
        hook = self.root / "hook.sh"
        hook.write_text(f'#!/bin/sh\necho "$MESH_RUNNER_PHASE" >> {gate}\necho RES_ID=r1\n')
        hook.chmod(0o755)
        self.agent.start_gate_command = [str(hook)]
        self.agent.turn_end_command = [str(hook)]
        r = self.runner(mesh)
        r.tick()
        a = r.store.by_state("builder", ("starting", "running"))[0]
        pid = a.pid
        mesh.set_category(t1["id"], "done")        # Mesh auto-closes the card ...
        mesh.checkouts.pop(t1["id"], None)         # ... and the server checkout disappears
        r.tick()                                   # first beat: closed seen, session left alone
        os.kill(pid, 0)
        r.tick()                                   # second beat confirms -> kill
        self.settle_all(r, timeout=8)
        self.assertEqual(self.states(r)[t1["id"][:8]], "completed")
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        self.assertIn("end", gate.read_text().split())

    def test_card_closed_but_heartbeat_still_accepted_stops_the_session(self):
        t1 = task(1)
        mesh = FakeMesh([t1], max_concurrent=1)
        write_replies(self.replies, {t1["id"][:8]: "SLEEP:30:done"})
        r = self.runner(mesh)
        r.tick()
        a = r.store.by_state("builder", ("starting", "running"))[0]
        pid = a.pid
        mesh.set_category(t1["id"], "done")        # closed, but the checkout still answers
        r.tick()
        os.kill(pid, 0)                            # first observation: left alone
        r.tick()                                   # second confirms -> kill
        self.settle_all(r, timeout=8)
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_second_runner_on_the_same_state_dir_is_refused(self):
        first = Runner(self.cfg)
        with self.assertRaises(AlreadyRunning):
            Runner(self.cfg)
        self.assertIsNotNone(first)

    def test_agents_tick_independently_and_a_wedged_one_is_reported(self):
        import threading
        from types import SimpleNamespace

        class Stub:
            def __init__(self, name, delay):
                self.agent, self.delay, self.ticks = SimpleNamespace(name=name), delay, 0

            def tick(self):
                time.sleep(self.delay)
                self.ticks += 1
                return {"agent": self.agent.name, "attempts": []}

        self.cfg.poll_seconds = 0.1
        runner = Runner(self.cfg)
        fast, slow = Stub("fast", 0.0), Stub("slow", 5.0)
        runner.agents = [fast, slow]
        stop = threading.Event()
        loop = threading.Thread(target=runner.run_forever, args=(stop,))
        with mock.patch("mesh_runner.runner.MIN_SLEEP_SECONDS", 1.0):   # the real floor is 5 s
            loop.start()
            time.sleep(2.3)                             # loops sleep at least 1 s between ticks
            stop.set()
        loop.join(timeout=15)
        self.assertGreaterEqual(fast.ticks, 2)          # sequential ticks would give 1: held by the slow one
        self.assertLessEqual(slow.ticks, 1)            # stop waits for its tick to finish
        rows = {r["agent"]: r for r in runner.snapshot(now_unix=time.time() + 1200)}
        self.assertIn("no tick finished", rows["slow"]["error"])
        status = json.loads((self.cfg.state_dir / "status.json").read_text())
        self.assertEqual({r["agent"] for r in status["agents"]}, {"fast", "slow"})

    def test_real_agents_work_from_their_threads(self):
        import threading
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        write_replies(self.replies, {t["id"][:8]: f"⏳ WAIT {FUTURE_AT}: external=fixture-window source=card:#abcdef12 soak window"})
        self.cfg.poll_seconds = 0.2
        runner = Runner(self.cfg)
        runner.agents = [self.runner(mesh)]              # store built on this (main) thread
        stop = threading.Event()
        loop = threading.Thread(target=runner.run_forever, args=(stop,))
        loop.start()
        deadline = time.time() + 15
        while time.time() < deadline and self.states(runner.agents[0]).get(t["id"][:8]) != "waiting":
            time.sleep(0.2)
        stop.set()
        loop.join(timeout=30)
        self.assertEqual(self.states(runner.agents[0]).get(t["id"][:8]), "waiting")
        rows = json.loads((self.cfg.state_dir / "status.json").read_text())["agents"]
        self.assertNotIn("error", rows[0])

    def test_mr_wait_on_an_already_green_pipeline_wakes_once(self):
        t = task(1)
        mesh, gl = FakeMesh([t], max_concurrent=1), FakeGitLab()
        gl.mr_head["g/app!7"] = "9:success"
        write_replies(self.replies, {t["id"][:8]: "⏳ WAIT mr:g/app!7: review",
                                     t["id"][:8] + ":resume": "⏳ WAIT mr:g/app!7: still"})
        r = self.runner(mesh, gl)
        r.tick()
        self.settle_all(r)
        r.tick()                                       # the result was final already: wake at once
        self.settle_all(r)
        self.assertIn("already finished", calls(self.calls)[1]["prompt"])
        # the same wait again on that final result: told once more, with the way out
        self.assertEqual(len(calls(self.calls)), 3)
        self.assertIn("cannot progress", calls(self.calls)[2]["prompt"])
        for _ in range(3):                             # declared a third time: correction hold, no timer
            r.tick()
        self.assertEqual(self.states(r)[t["id"][:8]], "failed")
        row = r.store.by_state("builder", ("failed",))[0]
        self.assertTrue(row.note.startswith("wait correction required:"))
        self.assertFalse(mesh.tasks[t["id"]].get("start_after"))
        self.assertEqual(len(calls(self.calls)), 3)
        gl.mr_head["g/app!7"] = "10:failed"           # correction hold requires explicit repair, not another CI update
        r.tick()
        self.assertEqual(len(calls(self.calls)), 3)

    def test_rewait_on_a_failed_pipeline_wakes_then_goes_to_a_time_wait(self):
        # a card woken on "pipeline failed", it declared the same wait on the same failed
        # pipeline and slept on an event that could never come.
        t = task(1)
        mesh, gl = FakeMesh([t], max_concurrent=1), FakeGitLab()
        gl.pipelines["g/app#5"] = "running"
        write_replies(self.replies, {t["id"][:8]: "⏳ WAIT pipeline:g/app#5",
                                     t["id"][:8] + ":resume": "⏳ WAIT pipeline:g/app#5"})
        r = self.runner(mesh, gl)
        r.tick()
        self.settle_all(r)
        gl.pipelines["g/app#5"] = "failed"
        r.tick()
        self.settle_all(r)                             # woken on the failure, declares the same wait again
        self.assertEqual(len(calls(self.calls)), 3)
        self.assertIn("cannot progress", calls(self.calls)[2]["prompt"])
        for _ in range(3):                             # told once, declares it again: no loop, no dead wait
            r.tick()
        self.assertEqual(len(calls(self.calls)), 3)
        row = r.store.by_state("builder", ("failed",))[0]
        self.assertTrue(row.note.startswith("wait correction required:"))
        self.assertFalse(mesh.tasks[t["id"]].get("start_after"))

    def test_rewait_on_a_pipeline_still_in_manual_does_not_loop(self):
        t = task(1)
        mesh, gl = FakeMesh([t], max_concurrent=1), FakeGitLab()
        gl.pipelines["g/app#5"] = "manual"
        write_replies(self.replies, {t["id"][:8]: "⏳ WAIT pipeline:g/app#5", t["id"][:8] + ":resume": "⏳ WAIT pipeline:g/app#5"})
        r = self.runner(mesh, gl)
        r.tick()
        self.settle_all(r)
        r.tick()
        self.settle_all(r)
        self.assertEqual(len(calls(self.calls)), 3)        # manual when declared: told, then the same wait again
        self.assertIn("cannot progress", calls(self.calls)[2]["prompt"])
        for _ in range(3):
            r.tick()
        self.assertEqual(len(calls(self.calls)), 3)        # declared a third time: bounded correction hold
        self.assertEqual(self.states(r)[t["id"][:8]], "failed")

    def test_healthy_unchanged_card_wait_does_not_wake_by_age(self):
        t = task(1)
        mesh = FakeMesh([t, task(0xabcdef12, assignee_id="someone-else")], max_concurrent=1)
        write_replies(self.replies, {t["id"][:8]: "⏳ WAIT card:#abcdef12", t["id"][:8] + ":resume": f"⏳ WAIT {FUTURE_AT}: external=fixture-window source=card:#abcdef12 soak window"})
        r = self.runner(mesh)
        r.tick()
        self.settle_all(r)
        self.age_wait(r, hours=3)                       # past wait_first_recheck_hours (2h)
        r.tick()
        self.settle_all(r)
        self.assertEqual([c["resume"] for c in calls(self.calls)], [False])
        self.assertEqual(self.states(r)[t["id"][:8]], "waiting")

    def test_time_wait_wakes_only_at_its_deadline(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        write_replies(self.replies, {t["id"][:8]: f"⏳ WAIT {FUTURE_AT}: external=fixture-window source=card:#abcdef12 pilot window",
                                     t["id"][:8] + ":resume": f"⏳ WAIT {FUTURE_AT}: external=fixture-window source=card:#abcdef12 pilot window"})
        r = self.runner(mesh)
        r.tick()
        self.settle_all(r)
        self.age_wait(r, hours=7)
        r.tick()
        self.settle_all(r)
        self.assertEqual(len(calls(self.calls)), 1)     # age alone cannot end a time wait
        a = r.store.by_state(self.agent.name, ("waiting",))[0]
        a.wait["until"] = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)).isoformat()
        r.store.update(a, wait=a.wait)
        r._last_wake_check.clear()
        r.tick()
        self.settle_all(r)
        self.assertEqual(len(calls(self.calls)), 2)     # the actual deadline wakes it

    def age_wait(self, r, hours):
        for a in r.store.by_state(self.agent.name, ("waiting",)):
            w = dict(a.wait)
            w["started"] = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)).isoformat()
            r.store.update(a, wait=w)
        r._last_wake_check.clear()

    def test_a_question_mesh_did_not_raise_is_told_once_then_waits(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        write_replies(self.replies, {t["id"][:8]: "❓ Blocking @owner: which plan?"})
        r = self.runner(mesh)
        r.tick()
        self.settle_all(r)
        r.tick()
        self.settle_all(r)
        a = r.store.by_state("builder", ("failed",))[0]
        self.assertTrue(a.note.startswith("wait correction required:"))
        self.assertFalse(r.store.by_state("builder", ("waiting",)))
        self.assertFalse(mesh.tasks[t["id"]].get("human_gate"))       # the runner never sets it
        told = calls(self.calls)[1]
        self.assertTrue(told["resume"])
        self.assertIn("Mesh raised no question to them", told["prompt"])
        self.assertIn("❓ Blocking @owner:", told["prompt"])
        for _ in range(3):
            r.tick()
        self.assertEqual(len(calls(self.calls)), 2)        # told once, no ping-pong without an answer

    def test_a_question_mesh_raised_waits_for_the_person(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        read = mesh.task
        # Mesh wraps the task ({"task": {...}}): the gate it raised must still be seen
        mesh.task = lambda tid: {"task": dict(read(tid), human_gate=True)} if calls(self.calls) else read(tid)
        write_replies(self.replies, {t["id"][:8]: "❓ Blocking @owner: which plan?"})
        r = self.runner(mesh)
        r.tick()
        self.settle_all(r)
        a = r.store.by_state("builder", ("waiting",))[0]
        self.assertEqual(a.wait["kind"], "blocking")
        self.assertEqual(mesh.created, [])
        self.assertEqual(len(calls(self.calls)), 1)

    def test_wait_decision_on_a_person_is_posted_as_a_blocking_ask(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        read = mesh.task
        # Mesh raises the gate from the posted `❓ Blocking` line
        mesh.task = lambda tid: ({"task": dict(read(tid), human_gate=True)}
                                 if any("❓ Blocking @owner:" in c["body"] for c in mesh.comments(tid))
                                 else read(tid))
        write_replies(self.replies, {t["id"][:8]: "⏳ WAIT decision:@owner: which plan?"})
        r = self.runner(mesh)
        r.tick()
        self.settle_all(r)
        a = r.store.by_state("builder", ("waiting",))[0]
        self.assertEqual(a.wait["kind"], "blocking")
        self.assertTrue(any("❓ Blocking @owner: which plan?" in c["body"] for c in mesh.comments(t["id"])))
        self.assertEqual(len(calls(self.calls)), 1)        # the agent is not woken to repeat itself

    def test_person_ask_dedup_is_per_question_and_addressee_and_survives_a_failed_post(self):
        from unittest.mock import patch
        from mesh_runner.outcome import Outcome
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        write_replies(self.replies, {t["id"][:8]: "⏳ WAIT decision:@owner: which plan?"})
        r = self.runner(mesh)
        r.tick()
        self.settle_all(r)
        a = r.store.by_state("builder", ("failed",))[0]
        self.assertTrue(a.note.startswith("wait correction required:"))

        def asks():
            return [c for c in mesh.comments(t["id"]) if "❓ Blocking @" in c["body"]]

        def route(handle, note):
            r._route_ask(a, t, Outcome(kind="decision", handle=handle, note=note), {}, "2026-10-21T00:00:00Z")
        base = len(asks())
        with patch.object(mesh, "add_comment", side_effect=MeshError(503, "down")):
            route("owner2", "pick one?")
        self.assertEqual(len(asks()), base)               # not delivered
        route("owner2", "pick one?")
        self.assertEqual(len(asks()), base + 1)           # retried after the failure
        route("owner2", "pick one?")
        self.assertEqual(len(asks()), base + 1)           # once delivered, not repeated
        route("owner3", "pick one?")
        self.assertEqual(len(asks()), base + 2)           # same text, new addressee
        route("owner2", "other?")
        route("owner2", "pick one?")
        self.assertEqual(len(asks()), base + 3)           # A-B-A: A is not posted again

    def test_a_refused_repeat_question_hands_the_agent_the_earlier_answer(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        write_replies(self.replies, {t["id"][:8]: "❓ Blocking @owner: keep both machines?"})
        comments = mesh.comments

        def with_refusal(tid, limit=20):
            out = comments(tid, limit)
            if calls(self.calls):                    # every ask is refused, each time a new notice
                out.append({"id": f"refusal-{len(calls(self.calls))}",
                            "author_id": "00000000-0000-0000-0000-000000000000",
                            "system_params": {"answered_at": "2026-10-10 11:24"},
                            "author_type": "system", "system_code": "repeat_ask_refused",
                            "body": "🔁 Auto: human_gate not armed — the previous question already has an "
                                    "answer (2026-10-10 11:24): «разрешаю».",
                            "created_at": dt.datetime.now(dt.timezone.utc).isoformat()})
            return out
        mesh.comments = with_refusal
        r = self.runner(mesh)
        r.tick()
        self.settle_all(r)
        r.tick()
        self.settle_all(r)
        told = calls(self.calls)[1]
        self.assertIn("refused it as already answered", told["prompt"])
        self.assertIn("«разрешаю»", told["prompt"])
        self.assertIn("supersedes:", told["prompt"])
        self.assertFalse(mesh.tasks[t["id"]].get("human_gate"))
        for _ in range(3):
            r.tick()
        self.assertEqual(len(calls(self.calls)), 2)        # the same refusal again does not resume it

    def test_a_policy_gate_notice_is_recognised_as_a_refusal(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        notice = "🔒 Gate on Person NOT armed — secret-leak/rotation. Do not rotate and do not ask about rotating."
        mesh.comment_log[t["id"]] = [{"id": "n1", "author_type": "system", "body": notice,
                                      "created_at": dt.datetime.now(dt.timezone.utc).isoformat()}]
        r = self.runner(mesh)
        a = r.store.create("builder", t["id"], "claude")
        wait = {"kind": "blocking", "handle": "owner"}
        told = r._route_ask(a, t, Outcome(kind="blocking", handle="owner", note=""), wait,
                            "2026-10-21T00:00:00Z")
        self.assertIn("Do not rotate", told)
        self.assertTrue(r.store.mark("builder", t["id"], "ask_not_raised")[0].startswith("refused:🔒 Gate on"))

    def test_a_repeated_refused_person_ask_does_not_wait_on_the_person(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        write_replies(self.replies, {t["id"][:8]: "❓ Blocking @owner: rotate the key?"})
        r = self.runner(mesh)
        a = r.store.create("builder", t["id"], "claude")
        r.store.mark("builder", t["id"], "ask_not_raised", "no-marker:blocking")
        wait = {"kind": "blocking", "handle": "owner"}
        told = r._route_ask(a, t, Outcome(kind="blocking", handle="owner", note=""), wait, "2026-10-21T00:00:00Z")
        self.assertEqual(told, "")
        self.assertNotEqual(wait["kind"], "time")
        self.assertEqual(a.state, "failed")
        self.assertTrue(a.note.startswith("wait correction required:"))
        self.assertNotIn("person_ask", wait)

    def test_blocking_addressed_to_an_agent_files_a_decision_request_not_a_gate(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        write_replies(self.replies, {t["id"][:8]: "❓ Blocking @lead: list it from the personal account?"})
        r = self.runner(mesh)
        r.tick()
        self.settle_all(r)
        a = r.store.by_state("builder", ("waiting",))[0]
        self.assertEqual(a.wait["kind"], "decision")
        self.assertFalse(mesh.tasks[t["id"]].get("human_gate"))
        self.assertEqual(mesh.created[0]["assignee_id"], "agent-lead")
        self.assertEqual(len(calls(self.calls)), 1)

    def test_turn_time_limit_kills_the_session(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        self.cfg.max_turn_minutes = 0
        write_replies(self.replies, {t["id"][:8]: f"SLEEP:60:⏳ WAIT {FUTURE_AT}: external=fixture-window source=card:#abcdef12 soak window"})
        r = self.runner(mesh)
        r.tick()
        time.sleep(1.1)
        started = time.time()
        self.settle_all(r, timeout=20)
        self.assertLess(time.time() - started, 15)
        self.assertNotEqual(self.states(r)[t["id"][:8]], "running")

    def test_children_left_behind_are_killed_after_the_grace(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        self.cfg.straggler_grace_minutes = 0
        write_replies(self.replies, {t["id"][:8]: f"BG:120:⏳ WAIT {FUTURE_AT}: external=fixture-window source=card:#abcdef12 soak window"})
        r = self.runner(mesh)
        r.tick()
        self.settle_all(r, timeout=20)
        self.assertEqual(self.states(r)[t["id"][:8]], "waiting")

    def test_error_with_text_is_retried_not_counted_as_no_outcome(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        write_replies(self.replies, {t["id"][:8]: "ERROR:API overloaded, try later",
                                     t["id"][:8] + ":resume": f"⏳ WAIT {FUTURE_AT}: external=fixture-window source=card:#abcdef12 soak window"})
        r = self.runner(mesh)
        r.tick()
        self.settle_all(r)
        self.assertEqual(self.states(r)[t["id"][:8]], "ready")
        self.assertGreater(r._starts_paused_until, 0)       # an overload pauses starts and resumes
        r._starts_paused_until = 0                          # the pause is over
        r.tick()
        self.settle_all(r)
        self.assertEqual(self.states(r)[t["id"][:8]], "waiting")
        self.assertEqual([c["resume"] for c in calls(self.calls)], [False, True])

    def test_resume_of_a_session_the_harness_does_not_know_starts_over(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        write_replies(self.replies, {t["id"][:8]: "⏳ WAIT decision:@lead",
                                     t["id"][:8] + ":resume": "ERROR:No conversation found with session ID: x"})
        r = self.runner(mesh)
        r.tick()
        self.settle_all(r)
        mesh.add_comment(t["id"], "answer", author="lead")
        r.tick()                                  # woken -> resumed -> the harness knows no such session
        self.settle_all(r)
        r.tick()                                  # started over in a new session, not counted as a failure
        self.settle_all(r)
        self.assertEqual([c["resume"] for c in calls(self.calls)], [False, True, False])
        self.assertEqual(self.states(r)[t["id"][:8]], "waiting")

    def test_no_session_restart_happens_once_per_attempt(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        self.cfg.max_failures = 1
        write_replies(self.replies, {t["id"][:8]: "⏳ WAIT decision:@lead",
                                     t["id"][:8] + ":resume": "ERROR:No conversation found with session ID: x"})
        r = self.runner(mesh)
        r.tick()
        self.settle_all(r)
        write_replies(self.replies, {t["id"][:8]: "ERROR:session abc not found",
                                     t["id"][:8] + ":resume": "ERROR:No conversation found with session ID: x"})
        mesh.add_comment(t["id"], "answer", author="lead")
        for _ in range(6):
            r.tick()
            self.settle_all(r)
        resumes = [c["resume"] for c in calls(self.calls)]
        self.assertEqual(resumes[:3], [False, True, False])     # one restart in a new session
        self.assertLessEqual(len(resumes), 5)                   # then ordinary failures, bounded

    def test_content_policy_refusal_is_not_retried_on_the_same_harness(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        write_replies(self.replies, {t["id"][:8]: "ERROR:This content was flagged for possible "
                                                  "cybersecurity risk. If this seems wrong, try rephrasing your request."})
        r = self.runner(mesh)
        for _ in range(6):
            r.tick()
            self.settle_all(r)
        # the same prompt would be refused again: one turn, then the hand-off, not max_failures + 1 turns
        self.assertEqual(len(calls(self.calls)), 1)
        self.assertNotEqual(self.states(r).get(t["id"][:8]), "ready")

    def test_quota_pause_also_stops_resumes(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        write_replies(self.replies, {t["id"][:8]: "ERROR:usage limit reached"})
        r = self.runner(mesh)
        r.tick()
        self.settle_all(r)
        for _ in range(3):
            r.tick()
        self.assertEqual(len(calls(self.calls)), 1)
        self.assertEqual(self.states(r)[t["id"][:8]], "ready")

    def test_wait_age_uses_the_runner_clock_not_an_old_comment(self):
        t, other = task(1), task(2, cat="in_progress")
        other["assignee_id"] = "someone-else"
        mesh = FakeMesh([t, other], max_concurrent=1)
        old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=5)
        mesh.add_comment(t["id"], "old note from the lead", author="lead", at=old)
        write_replies(self.replies, {t["id"][:8]: f"⏳ WAIT card:#{other['id'][:8]}"})
        r = self.runner(mesh)
        r.tick()
        self.settle_all(r)
        for _ in range(3):
            r.tick()
        self.assertEqual(self.states(r)[t["id"][:8]], "waiting")
        self.assertEqual(len(calls(self.calls)), 1)

    def test_full_pool_holds_starts_without_touching_tasks(self):
        # slots leased to open (waiting) tasks: no checkout, no abort, no comment asking an operator
        tasks = [task(1), task(2)]
        mesh = FakeMesh(tasks, max_concurrent=2)
        self.agent.repos[0] = RepoSpec("app", self.agent.repos[0].base, max_slots=0)
        r = self.runner(mesh)
        r.tick()
        r.tick()
        self.assertEqual(self.states(r), {})
        self.assertEqual(calls(self.calls), [])
        self.assertFalse(any("cannot start" in c["body"] for t in tasks for c in mesh.comment_log.get(t["id"], [])))
        self.assertIn("worktree pool full: app", r.status()["pool_hold"])

    def test_pool_filled_within_one_tick_holds_the_rest(self):
        # room for two starts, one worktree: the first start takes it, the second task is untouched
        t1, t2 = task(1), task(2)
        mesh = FakeMesh([t1, t2], max_concurrent=2)
        self.agent.repos[0] = RepoSpec("app", self.agent.repos[0].base, max_slots=1)
        write_replies(self.replies, {t1["id"][:8]: f"⏳ WAIT {FUTURE_AT}: external=fixture-window source=card:#abcdef12 soak window", t2["id"][:8]: f"⏳ WAIT {FUTURE_AT}: external=fixture-window source=card:#abcdef12 soak window"})
        r = self.runner(mesh)
        r.tick()
        self.assertEqual(len(self.states(r)), 1)
        self.assertNotIn("aborted", self.states(r).values())
        self.assertFalse(any("cannot start" in c["body"] for t in (t1, t2) for c in mesh.comment_log.get(t["id"], [])))
        self.assertIn("worktree pool full: app", r.status()["pool_hold"])

    def test_full_pool_frees_when_a_waiting_task_returns_its_slot(self):
        t1, t2 = task(1), task(2)
        mesh = FakeMesh([t1], max_concurrent=1)
        self.agent.repos[0] = RepoSpec("app", self.agent.repos[0].base, max_slots=1)
        write_replies(self.replies, {t1["id"][:8]: f"⏳ WAIT {FUTURE_AT}: external=fixture-window source=card:#abcdef12 soak window", t2["id"][:8]: f"⏳ WAIT {FUTURE_AT}: external=fixture-window source=card:#abcdef12 soak window"})
        r = self.runner(mesh)
        r.tick()
        self.settle_all(r)
        self.assertEqual(self.states(r)[t1["id"][:8]], "waiting")
        mesh.tasks[t2["id"]] = t2
        r.tick()
        self.assertNotIn(t2["id"][:8], self.states(r))
        self.assertIn("pool_hold", r.status())
        r.pool.release(r.store.by_state(self.agent.name, ("waiting",))[0].id)
        r.tick()
        self.settle_all(r)
        self.assertEqual(self.states(r)[t2["id"][:8]], "waiting")    # started into the returned slot

    def test_turns_per_task_are_capped(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        self.cfg.max_turns = 3
        write_replies(self.replies, {t["id"][:8]: "↻ NEXT"})
        r = self.runner(mesh)
        r.tick()
        self.settle_all(r)
        self.assertEqual(self.states(r)[t["id"][:8]], "failed")
        self.assertEqual(len(calls(self.calls)), 3)


class Operations(Bench):
    def test_pause_file_stops_starts_and_resumes(self):
        t1, t2 = task(1), task(2)
        mesh = FakeMesh([t1, t2], max_concurrent=2)
        (self.cfg.state_dir / "pause").mkdir(parents=True, exist_ok=True)
        (self.cfg.state_dir / "pause" / "builder").touch()
        r = self.runner(mesh)
        r.tick()
        self.assertEqual(self.states(r), {})
        self.assertTrue(r.status()["paused"])
        (self.cfg.state_dir / "pause" / "builder").unlink()
        write_replies(self.replies, {t1["id"][:8]: f"⏳ WAIT {FUTURE_AT}: external=fixture-window source=card:#abcdef12 soak window", t2["id"][:8]: f"⏳ WAIT {FUTURE_AT}: external=fixture-window source=card:#abcdef12 soak window"})
        r.tick()
        self.assertEqual(len(self.states(r)), 2)

    def test_env_file_is_reread_before_every_spawn(self):
        t1, t2 = task(1), task(2)
        mesh = FakeMesh([t1], max_concurrent=1)
        env_file = self.root / "agent.env"
        env_file.write_text("export PROBE='first'\n")
        self.agent.env_file = env_file
        write_replies(self.replies, {t1["id"][:8]: f"⏳ WAIT {FUTURE_AT}: external=fixture-window source=card:#abcdef12 soak window", t2["id"][:8]: f"⏳ WAIT {FUTURE_AT}: external=fixture-window source=card:#abcdef12 soak window"})
        r = self.runner(mesh)
        r.tick()
        self.settle_all(r)
        env_file.write_text("export PROBE='second'\n")         # e.g. an account switch
        mesh.tasks[t2["id"]] = t2
        r.tick()
        self.settle_all(r)
        self.assertEqual([c["probe"] for c in calls(self.calls)], ["first", "second"])

    def test_events_status_and_metrics_are_written(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        self.cfg.metrics_file = self.root / "runner.prom"
        write_replies(self.replies, {t["id"][:8]: f"⏳ WAIT {FUTURE_AT}: external=fixture-window source=card:#abcdef12 soak window"})
        from mesh_runner.runner import Runner as R
        runner = R.__new__(R)
        runner.cfg, runner.store, runner._lock = self.cfg, Store(self.root / "state" / "r.sqlite"), None
        runner.agents = [self.runner(mesh)]
        runner.agents[0].store = runner.store
        runner.tick()
        self.settle_all(runner.agents[0])
        runner.tick()
        kinds = [json.loads(l)["kind"] for l in (self.cfg.state_dir / "events.jsonl").read_text().splitlines()]
        self.assertEqual(kinds[:2], ["start", "wait"])
        self.assertIn('mesh_runner_attempts{agent="builder",state="waiting"} 1', self.cfg.metrics_file.read_text())
        self.assertTrue((self.cfg.state_dir / "status.json").exists())


class MeshEffects(Bench):
    def test_runtime_state_follows_the_attempt(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        write_replies(self.replies, {t["id"][:8]: f"SLEEP:1:⏳ WAIT {FUTURE_AT}: external=fixture-window source=card:#abcdef12 soak window"})
        r = self.runner(mesh)
        r.tick()
        self.assertEqual(mesh.runtime[t["id"]]["kind"], "working")
        self.settle_all(r)
        r.tick()
        self.assertEqual((mesh.runtime[t["id"]]["kind"], mesh.runtime[t["id"]]["ref"]), ("waiting", "time"))
        mesh.set_category(t["id"], "done")
        r._last_wake_check.clear()
        r.tick()
        self.assertNotIn(t["id"], mesh.runtime)                  # cleared when the attempt closes

    def test_tasks_it_does_not_run_show_why(self):
        blocker = task(9, cat="in_progress", assignee_id="someone-else")
        t1, t2, t3 = task(1), task(2), task(3)
        mesh = FakeMesh([blocker, t1, t2, t3], max_concurrent=1)
        mesh.deps[t3["id"]] = [{"dependency_type": "blocks", "depends_on_task_id": blocker["id"]}]
        write_replies(self.replies, {t1["id"][:8]: f"SLEEP:1:⏳ WAIT {FUTURE_AT}: external=fixture-window source=card:#abcdef12 soak window", t2["id"][:8]: "✅ done"})
        r = self.runner(mesh)
        r.tick()
        self.assertEqual(mesh.runtime[t1["id"]]["kind"], "working")
        self.assertEqual(mesh.runtime[t2["id"]], {"kind": "queued", "lane": "builder", "ref": "position:1"})
        self.assertEqual(mesh.runtime[t3["id"]], {"kind": "waiting", "ref": f"card:#{blocker['id'][:8]}"})
        self.settle_all(r)
        r._skips_due = 0
        r.tick()                                   # t1 waits: its slot went to t2, which finished
        self.assertEqual(mesh.runtime[t1["id"]]["kind"], "waiting")
        self.assertNotEqual(mesh.runtime.get(t2["id"], {}).get("kind"), "queued")
        mesh.set_category(blocker["id"], "done")
        r._hold_cache.clear()
        r._skips_due = 0
        r.tick()
        self.assertNotEqual(mesh.runtime.get(t3["id"], {}).get("ref"), f"card:#{blocker['id'][:8]}")

    def test_skip_states_clear_within_budget_and_retry_a_refused_clear(self):
        from mesh_runner.effects import Effects
        mesh = FakeMesh([], max_concurrent=1)
        fx = Effects(mesh, None, "b")
        q = {"kind": "queued", "lane": "b", "ref": "position:1"}
        fx.publish_skips({"t1": q, "t2": q, "t3": q})
        self.assertEqual(set(mesh.runtime), {"t1", "t2", "t3"})
        fx.publish_skips({}, budget=2)                       # budget ends with a clear pending
        self.assertEqual(len(mesh.runtime), 1)
        mesh.fail_delete = 1
        fx.publish_skips({})                                 # refused: kept for the next pass
        self.assertEqual(len(mesh.runtime), 1)
        fx.publish_skips({})
        self.assertEqual(mesh.runtime, {})

    def test_skip_state_survives_an_unreadable_reason_and_changes_go_first(self):
        from mesh_runner.effects import Effects
        mesh = FakeMesh([], max_concurrent=1)
        fx = Effects(mesh, None, "b")
        w = {"kind": "waiting", "ref": "card:#abcdef12"}
        fx.publish_skips({"t1": w})
        fx.publish_skips({}, keep={"t1"})                    # reason unreadable this pass
        self.assertEqual(mesh.runtime["t1"], w)
        fx._published["t1"] = (w, fx._published["t1"][1] - dt.timedelta(minutes=10))   # heartbeat due
        new = {"kind": "queued", "lane": "b", "ref": "position:1"}
        fx.publish_skips({"t1": w, "t2": new}, budget=1)
        self.assertEqual(mesh.runtime.get("t2"), new)         # the change won the only write

    def test_status_reports_the_waiting_queue(self):
        t1, t2 = task(1), task(2)
        mesh = FakeMesh([t1, t2], max_concurrent=1)
        write_replies(self.replies, {t1["id"][:8]: f"⏳ WAIT {FUTURE_AT}: external=fixture-window source=card:#abcdef12 soak window", t2["id"][:8]: f"⏳ WAIT {FUTURE_AT}: external=fixture-window source=card:#abcdef12 soak window"})
        r = self.runner(mesh)
        r.tick()
        self.assertEqual(r.status()["queue"], 1)
        self.assertEqual(self.finish_turn(r).state, "waiting")
        r.tick()
        self.assertEqual(self.finish_turn(r).state, "waiting")

    def test_every_turn_reports_its_spend_to_mesh(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        write_replies(self.replies, {t["id"][:8]: f"⏳ WAIT {FUTURE_AT}: external=fixture-window source=card:#abcdef12 soak window"})
        r = self.runner(mesh)
        r.tick()
        self.settle_all(r)
        self.assertEqual(mesh.reports, [{"task": t["id"], "in": 100, "out": 5, "model": "fake-model", "cost": 0.01}])
        a = r.store.by_state("builder", ("waiting",))[0]
        r._report_spend(a, r.adapter_for(a.harness).parse(Path(a.log_path)))   # a second settle of that turn
        self.assertEqual(len(mesh.reports), 1)

    def test_skip_reasons_map_to_mesh_states(self):
        from mesh_runner.effects import skip_state
        self.assertEqual(skip_state("blocked by abcdef12, 12345678", "b"), {"kind": "waiting", "ref": "card:#abcdef12"})
        self.assertEqual(skip_state("waiting for a human decision", "b")["reason_code"], "human_gate")
        self.assertEqual(skip_state("label park:later", "b")["reason_code"], "park")
        self.assertEqual(skip_state("paused", "b")["reason_code"], "pause")
        self.assertEqual(skip_state("review: already reviewed; waits for a new comment", "b")["reason_code"],
                         "review_refeed_hold")
        self.assertEqual(skip_state("umbrella: all open children are parked, gated or in backlog", "b")["reason_code"],
                         "umbrella")
        self.assertEqual(skip_state("start_after 2000-01-01T00:00+00:00", "b"), {"kind": "waiting", "ref": "time"})
        self.assertIsNone(skip_state("dependencies unreadable (503)", "b"))

    def test_card_wait_schedules_and_adds_an_edge_then_undoes_both_on_wake(self):
        t, other = task(1), task(2, cat="in_progress")
        other["assignee_id"] = "someone-else"
        mesh = FakeMesh([t, other], max_concurrent=1)
        write_replies(self.replies, {t["id"][:8]: f"⏳ WAIT card:#{other['id'][:8]}",
                                     t["id"][:8] + ":resume": f"⏳ WAIT {FUTURE_AT}: external=fixture-window source=card:#abcdef12 soak window"})
        r = self.runner(mesh)
        r.tick()
        self.settle_all(r)
        self.assertFalse(mesh.tasks[t["id"]].get("start_after"))     # an event wait writes no hold
        self.assertEqual([e["depends_on_task_id"] for e in mesh.edges[t["id"]]], [other["id"]])
        mesh.set_category(other["id"], "done")
        r.tick()
        self.assertEqual(mesh.edges[t["id"]], [])
        self.settle_all(r)

    def test_decision_wait_on_an_agent_files_and_closes_a_request(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        write_replies(self.replies, {t["id"][:8]: "⏳ WAIT decision:@lead: approve the plan?",
                                     t["id"][:8] + ":resume": f"⏳ WAIT {FUTURE_AT}: external=fixture-window source=card:#abcdef12 soak window"})
        r = self.runner(mesh)
        r.tick()
        self.settle_all(r)
        self.assertEqual(len(mesh.created), 1)
        req = mesh.created[0]
        self.assertEqual(req["assignee_id"], "agent-lead")
        self.assertIn("decision-request", req["labels"])
        self.assertTrue(req["title"].startswith("[decision] approve the plan?"))
        mesh.add_comment(t["id"], "approved", author="agent-lead")
        r.tick()
        self.assertEqual(mesh.category(mesh.tasks[req["id"]]), "done")
        self.settle_all(r)

    def test_wait_begun_without_effects_is_adopted(self):
        # a wait that started under an older runner (or with effects off) still
        # gets its decision request once effects are on
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        write_replies(self.replies, {t["id"][:8]: "⏳ WAIT decision:@lead: approve the plan?"})
        self.cfg.mesh_effects = False
        r = self.runner(mesh)
        r.tick()
        self.settle_all(r)
        self.assertEqual(mesh.created, [])
        self.cfg.mesh_effects = True
        r = self.runner(mesh)
        r.tick()
        r.tick()
        self.assertEqual(len(mesh.created), 1)
        self.assertEqual(mesh.created[0]["assignee_id"], "agent-lead")
        self.assertFalse(mesh.tasks[t["id"]].get("start_after"))     # an event wait writes no hold

    def test_ambiguous_decision_create_requires_reconciliation_instead_of_duplicate(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        mesh.fail_create = 1
        write_replies(self.replies, {t["id"][:8]: "⏳ WAIT decision:@lead: approve the plan?"})
        r = self.runner(mesh)
        r.tick()
        self.settle_all(r)
        r.tick()
        r.tick()
        self.assertEqual(mesh.fail_create, 0)        # the first try did fail
        self.assertEqual(len(mesh.created), 0)       # 503 might have created it: do not repeat POST
        r.tick()
        self.assertEqual(len(mesh.created), 0)
        self.assertEqual(r.effects._decision(t["id"])["state"], "creating")

    def test_decision_wait_on_a_human_files_nothing_and_sets_no_gate(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        write_replies(self.replies, {t["id"][:8]: "⏳ WAIT decision:@person"})
        r = self.runner(mesh)
        r.tick()
        self.assertEqual(self.finish_turn(r).state, "ready")
        self.assertEqual(mesh.created, [])
        self.assertFalse(mesh.tasks[t["id"]].get("human_gate"))   # only Mesh raises a person's gate
        r.tick()
        self.assertEqual(self.finish_turn(r).state, "failed")
        self.assertIn("is for agents", calls(self.calls)[1]["prompt"])
        self.assertEqual(mesh.created, [])
        self.assertFalse(mesh.tasks[t["id"]].get("human_gate"))

    def test_unreadable_declaration_is_quoted_back_then_the_task_goes_to_the_lead(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        self.agent.lead = "lead"
        # a malformed event wait; a non-event WAIT is a time wait and ends in a correction hold instead
        write_replies(self.replies, {t["id"][:8]: "done\n⏳ WAIT card:soon-ish"})
        r = self.runner(mesh)
        r.tick()
        self.settle_all(r)
        prompts_seen = [c["prompt"] for c in calls(self.calls)]
        self.assertEqual(len(prompts_seen), 2)
        self.assertIn("⏳ WAIT card:soon-ish", prompts_seen[1])     # the agent sees what was wrong
        self.assertIn("⏳ WAIT card:#<8hex>", prompts_seen[1])      # and the forms that work
        self.assertEqual(mesh.tasks[t["id"]]["assignee_id"], "agent-lead")
        self.assertEqual(mesh.category(mesh.tasks[t["id"]]), "todo")
        self.assertTrue(any("RUNNER GAVE UP" in c["body"] for c in mesh.comment_log[t["id"]]))

    def test_resume_shows_every_comment_since_the_last_turn_not_only_the_wake_event(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        write_replies(self.replies, {t["id"][:8]: "⏳ WAIT decision:@lead",
                                     t["id"][:8] + ":resume": f"⏳ WAIT {FUTURE_AT}: external=fixture-window source=card:#abcdef12 soak window"})
        r = self.runner(mesh)
        r.tick()
        self.settle_all(r)
        mesh.add_comment(t["id"], "⏸ Gate armed", author="00000000-0000-0000-0000-000000000000")
        r.tick()
        self.assertEqual(self.states(r)[t["id"][:8]], "waiting")     # Mesh's own notice wakes nothing
        mesh.add_comment(t["id"], "first answer", author="lead")
        mesh.add_comment(t["id"], "and a correction", author="lead")
        r.tick()
        self.settle_all(r)
        prompt = calls(self.calls)[1]["prompt"]
        self.assertIn("first answer", prompt)
        self.assertIn("and a correction", prompt)
        self.assertNotIn("Gate armed", prompt)

    def test_an_address_on_someone_elses_task_becomes_a_request_card(self):
        mine, theirs = task(1), task(2, assignee_id="agent-lead")
        mesh = FakeMesh([mine, theirs], max_concurrent=1)
        self.cfg.mention_requests = True
        # recent rows: older than the lookback on a fresh start they would be history
        at = lambda minutes: (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=minutes)).isoformat()  # noqa: E731
        mesh.mention_rows = [
            {"comment_id": "c1", "task_id": theirs["id"], "author_id": "agent-lead", "author_name": "lead",
             "comment_body": "@builder: please re-check the migration", "extracted_at": at(4),
             "mentioned_slug": "builder"},
            {"comment_id": "c2", "task_id": theirs["id"], "author_id": "agent-lead", "author_name": "lead",
             "comment_body": "as @builder said yesterday", "extracted_at": at(3),
             "mentioned_slug": "builder"},
            {"comment_id": "c3", "task_id": mine["id"], "author_id": "agent-lead", "author_name": "lead",
             "comment_body": "@builder: on your own task", "extracted_at": at(2),
             "mentioned_slug": "builder"},
            {"comment_id": "c4", "task_id": theirs["id"], "author_id": "agent-lead", "author_name": "lead",
             "comment_body": "⏳ WAIT decision:@builder: which one?", "extracted_at": at(1),
             "mentioned_slug": "builder"}]
        write_replies(self.replies, {})
        r = self.runner(mesh)
        r.refresh_me()
        r.deliver_mentions()
        made = [t for t in mesh.created if "mention-request" in (t.get("labels") or [])]
        self.assertEqual(len(made), 1)                                     # only the address on a foreign task
        self.assertIn(f"#{theirs['id'][:8]}", made[0]["title"])
        self.assertTrue(all(m.get("seen") for m in mesh.mention_rows))     # everything read once
        r._mentions_at = -1e18
        r.deliver_mentions()
        self.assertEqual(len([t for t in mesh.created if "mention-request" in (t.get("labels") or [])]), 1)

    def test_a_failed_mark_seen_never_files_the_card_twice(self):
        theirs = task(2, assignee_id="agent-lead")
        mesh = FakeMesh([theirs], max_concurrent=1)
        self.cfg.mention_requests = True
        mesh.mention_rows = [{"comment_id": "c1", "task_id": theirs["id"], "author_id": "agent-lead",
                              "author_name": "lead", "comment_body": "@builder. please look",
                              "extracted_at": "2099-01-01T00:00:00Z", "mentioned_slug": "builder"}]
        seen = mesh.mention_seen
        mesh.mention_seen = lambda cid: (_ for _ in ()).throw(MeshError(503, "down"))
        r = self.runner(mesh)
        r.refresh_me()
        for _ in range(3):
            r._mentions_at = -1e18
            r.deliver_mentions()
        mesh.mention_seen = seen
        r._mentions_at = -1e18
        r.deliver_mentions()
        self.assertEqual(len([t for t in mesh.created if "mention-request" in (t.get("labels") or [])]), 1)
        self.assertTrue(mesh.mention_rows[0].get("seen"))

    def test_a_reply_inside_a_conversation_the_agent_is_in_files_nothing(self):
        theirs = task(2, assignee_id="agent-lead")
        mesh = FakeMesh([theirs], max_concurrent=1)
        self.cfg.mention_requests = True
        mesh.add_comment(theirs["id"], "@reviewer: could you check?", author=AGENT_ID)   # builder asked first
        mesh.mention_rows = [{"comment_id": "c9", "task_id": theirs["id"], "author_id": "agent-reviewer",
                              "author_name": "reviewer", "comment_body": "@builder done, looks fine",
                              "extracted_at": "2099-01-01T00:00:00Z", "mentioned_slug": "builder"}]
        r = self.runner(mesh)
        r.refresh_me()
        r.deliver_mentions()
        self.assertEqual([t for t in mesh.created if "mention-request" in (t.get("labels") or [])], [])

    def test_effort_label_reaches_the_session(self):
        t = task(1, labels=["effort:low"])
        mesh = FakeMesh([t], max_concurrent=1)
        write_replies(self.replies, {t["id"][:8]: f"⏳ WAIT {FUTURE_AT}: external=fixture-window source=card:#abcdef12 soak window"})
        r = self.runner(mesh)
        r.tick()
        self.settle_all(r)
        self.assertEqual(calls(self.calls)[0]["effort"], "low")

    def test_a_shipped_blocker_no_longer_blocks(self):
        blocker, t = task(9, cat="review"), task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        mesh.tasks[blocker["id"]] = blocker
        mesh.deps[t["id"]] = [{"depends_on_task_id": blocker["id"], "dependency_type": "blocks"}]
        r = self.runner(mesh)
        self.assertTrue(r.hold_reason(t, "work").startswith("blocked by"))
        blocker["is_shipped"] = True
        r._hold_cache.clear()
        self.assertEqual(r.hold_reason(t, "work"), "")

    def test_a_task_scoped_gate_hold_stands_until_the_task_changes(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        asked = self.root / "asked"
        gate = (f"import sys,pathlib\np=pathlib.Path({str(asked)!r}); p.write_text(p.read_text()+'x' if p.exists() else 'x')\n"
                "print('waits for a human'); sys.exit(76)\n")
        self.agent.start_gate_command = [sys.executable, "-c", gate]
        r = self.runner(mesh)
        for _ in range(3):
            r.tick()
        self.assertEqual(asked.read_text(), "x")                          # asked once, not every tick
        mesh.tasks[t["id"]]["updated_at"] = "2026-10-17T01:00:00Z"        # e.g. the gate was answered
        r.tick()
        self.assertEqual(asked.read_text(), "xx")

    def test_a_review_feed_the_gate_keeps_refusing_leaves_a_notice_and_stops_retrying(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        asked = self.root / "asked"
        gate = (f"import sys,pathlib\np=pathlib.Path({str(asked)!r}); p.write_text(p.read_text()+'x' if p.exists() else 'x')\n"
                "print('qa_refused:reviewer_is_author_or_identity_unknown'); sys.exit(76)\n")
        self.agent.start_gate_command = [sys.executable, "-c", gate]
        r = self.runner(mesh)
        review = dict(mesh.tasks[t["id"]], _mode="review")
        runner_notes = lambda: [c for c in mesh.comment_log.get(t["id"], []) if c["body"].startswith("[runner]")]  # noqa: E731
        r._start_one(review)
        self.assertEqual(runner_notes(), [])                        # first refusal: the usual short hold
        r._gate_hold.clear()
        r._start_one(review)
        self.assertEqual(runner_notes(), [])                        # still inside the grace period
        r.store.db.execute("UPDATE marks SET value=? WHERE task_id=? AND key='review_gate_hold'",
                           ((dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=31)).isoformat(), t["id"]))
        r._gate_hold.clear()
        r._start_one(review)
        notes = runner_notes()
        self.assertEqual(len(notes), 1)                             # past 30 min: said once, plainly
        self.assertIn("No reviewer could be started", notes[0]["body"])
        self.assertIn("qa_refused:reviewer_is_author_or_identity_unknown", notes[0]["body"])
        before = asked.read_text()
        for _ in range(3):                                          # no more start attempts, no more notes
            r._gate_hold.clear()
            r._hold_cache.clear()
            r._start_one(dict(mesh.tasks[t["id"]], _mode="review"))
        self.assertEqual(asked.read_text(), before)
        self.assertEqual(len(runner_notes()), 1)
        self.assertIn("waits for a new comment", r.hold_reason(dict(mesh.tasks[t["id"]], _mode="review"), "review"))
        mesh.add_comment(t["id"], "assigned a reviewer", author="router",
                         at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=1))
        r._hold_cache.clear()
        r._gate_hold.clear()
        r._start_one(dict(mesh.tasks[t["id"]], _mode="review"))
        self.assertGreater(len(asked.read_text()), len(before))     # someone wrote: the gate is asked again

    def _refused_review_with_notice(self, retry_minutes=60.0):
        """A review the gate refuses until it holds with its notice: (runner, mesh, task, asked-file, notes)."""
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        asked = self.root / "asked"
        gate = (f"import sys,pathlib\np=pathlib.Path({str(asked)!r}); p.write_text(p.read_text()+'x' if p.exists() else 'x')\n"
                "print('qa_refused:reviewer_is_author_or_identity_unknown'); sys.exit(76)\n")
        self.agent.start_gate_command = [sys.executable, "-c", gate]
        self.cfg.review_unstartable_retry_minutes = retry_minutes
        r = self.runner(mesh)
        r.store.mark("builder", t["id"], "review_gate_hold",
                     (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=31)).isoformat())
        r._start_one(dict(mesh.tasks[t["id"]], _mode="review"))
        notes = lambda: [c for c in mesh.comment_log.get(t["id"], []) if c["body"].startswith("[runner]")]  # noqa: E731
        self.assertEqual(len(notes()), 1)
        return r, mesh, t, asked, notes

    def _age_hold(self, r, t, minutes):
        old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=minutes)).isoformat()
        r.store.db.execute("UPDATE marks SET at=? WHERE task_id=? AND key IN "
                           "('review_unstartable','review_gate_hold')", (old, t["id"]))
        r._gate_hold.clear()
        r._hold_cache.clear()

    def test_after_the_notice_an_unchanged_card_is_not_retried_and_a_refused_retry_is_one_hold(self):
        r, mesh, t, asked, notes = self._refused_review_with_notice()
        before = asked.read_text()
        review = lambda: dict(mesh.tasks[t["id"]], _mode="review")  # noqa: E731
        self._age_hold(r, t, 59)                                    # the retry moment has not come
        self.assertIn("no reviewer", r.hold_reason(review(), "review"))
        self._age_hold(r, t, 61)                                    # it has, but nothing on the card moved
        self.assertIn("no reviewer", r.hold_reason(review(), "review"))
        r._start_one(review())
        self.assertEqual(asked.read_text(), before)                 # the gate was not asked again
        mesh.tasks[t["id"]]["labels"] = ["verified-by:someone"]     # the card's state changed
        self._age_hold(r, t, 61)
        self.assertNotIn("no reviewer", r.hold_reason(review(), "review"))
        r._start_one(review())
        self.assertEqual(len(asked.read_text()), len(before) + 1)   # one retry, refused again...
        r._gate_hold.clear()
        r._hold_cache.clear()
        self.assertIn("no reviewer", r.hold_reason(review(), "review"))   # ...and quiet at once, not a new grace window
        r._start_one(review())
        self.assertEqual(len(asked.read_text()), len(before) + 1)
        self.assertEqual(len(notes()), 1)                           # and no second notice

    def test_after_the_notice_a_foreign_comment_buys_two_holds_not_a_whole_grace_window(self):
        r, mesh, t, asked, notes = self._refused_review_with_notice()
        before = len(asked.read_text())
        mesh.add_comment(t["id"], "looking at it", author="router", at=dt.datetime.now(dt.timezone.utc))
        for _ in range(6):
            r._gate_hold.clear()
            r._hold_cache.clear()
            r._start_one(dict(mesh.tasks[t["id"]], _mode="review"))
        self.assertEqual(len(asked.read_text()) - before, 2)
        self.assertEqual(len(notes()), 1)

    def test_only_a_no_reviewer_refusal_counts_and_a_stale_streak_starts_over(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.runner(mesh)
        long_ago = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=45)).isoformat()
        r.store.mark("builder", t["id"], "review_gate_hold", long_ago)
        r._review_gate_refused(mesh.tasks[t["id"]], "profile pool full")          # a quota/capacity hold
        self.assertEqual(mesh.comment_log.get(t["id"], []), [])
        self.assertFalse(r.store.mark("builder", t["id"], "review_gate_hold")[0])  # ...breaks the streak
        r.store.mark("builder", t["id"], "review_gate_hold", long_ago)
        self.assertIsNone(r.store.mark("builder", t["id"], "review_unstartable"))
        r.store.db.execute("UPDATE marks SET at=? WHERE task_id=? AND key='review_gate_hold'",
                           ((dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=5)).isoformat(), t["id"]))
        r._review_gate_refused(mesh.tasks[t["id"]], "qa_refused:reviewer_is_author_or_identity_unknown")
        self.assertEqual(mesh.comment_log.get(t["id"], []), [])                   # old streak: starts over, no notice
        self.assertNotEqual(r.store.mark("builder", t["id"], "review_gate_hold")[0], long_ago)

    def test_a_hold_of_the_whole_runner_breaks_the_refusal_streak(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.runner(mesh)
        earlier = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=29)).isoformat()
        r.store.mark("builder", t["id"], "review_gate_hold", earlier)
        r._starts_paused_until = time.monotonic() + 600                  # quota pause: the gate is not asked
        r.start_new(1)
        self.assertFalse(r.store.mark("builder", t["id"], "review_gate_hold")[0])
        r.store.mark("builder", t["id"], "review_gate_hold", earlier)
        r._starts_paused_until = 0
        r._pool_full = lambda: True                                      # every worktree leased
        r.start_new(1)
        self.assertFalse(r.store.mark("builder", t["id"], "review_gate_hold")[0])
        r._pool_full = lambda: False
        r.store.mark("builder", t["id"], "review_gate_hold", earlier)
        r.memgate.check = lambda: "host short of memory"                 # no new sessions
        r.start_new(1)
        self.assertFalse(r.store.mark("builder", t["id"], "review_gate_hold")[0])
        r.memgate.check = lambda: ""
        self.cfg.review_gate_hold_minutes = 0                            # feature off: nothing to clear
        r.store.mark("builder", t["id"], "review_gate_hold", earlier)
        r.start_new(1)
        self.assertEqual(r.store.mark("builder", t["id"], "review_gate_hold")[0], earlier)

    def test_a_resumed_review_the_gate_keeps_refusing_leaves_a_notice_and_stops_retrying(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        asked = self.root / "asked"
        gate = (f"import sys,pathlib\np=pathlib.Path({str(asked)!r}); p.write_text(p.read_text()+'x' if p.exists() else 'x')\n"
                "print('qa_refused:reviewer_is_author_or_identity_unknown'); sys.exit(76)\n")
        self.agent.start_gate_command = [sys.executable, "-c", gate]
        r = self.runner(mesh)
        r._still_ours = lambda task, mode: True
        a = r.store.create("builder", t["id"], "claude", "t", mode="review")
        r.store.update(a, state="ready", wake_text="go on")
        notes = lambda: [c for c in mesh.comment_log.get(t["id"], []) if c["body"].startswith("[runner]")]  # noqa: E731
        r._resume_one(r.store.get(a.id))
        self.assertTrue(r.store.mark("builder", t["id"], "review_gate_hold")[0])     # tracked, not silent
        self.assertEqual(notes(), [])
        r.store.db.execute("UPDATE marks SET value=? WHERE task_id=? AND key='review_gate_hold'",
                           ((dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=31)).isoformat(), t["id"]))
        r._resume_one(r.store.get(a.id))
        self.assertEqual(len(notes()), 1)                                           # past 30 min: said once
        before = asked.read_text()
        for _ in range(3):
            r._resume_hold.clear()
            r._resume_one(r.store.get(a.id))
        self.assertEqual(asked.read_text(), before)                                 # the gate is not asked again
        self.assertEqual(len(notes()), 1)

    def test_turning_the_notice_hold_off_releases_cards_already_held(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.runner(mesh)
        r.store.mark("builder", t["id"], "review_unstartable", "qa_refused:x")
        hold = lambda: r.hold_reason(dict(mesh.tasks[t["id"]], _mode="review"), "review")  # noqa: E731
        self.assertIn("no reviewer", hold())
        self.cfg.review_gate_hold_minutes = 0
        r._hold_cache.clear()
        self.assertNotIn("no reviewer", hold())

    def test_an_admit_phase_no_reviewer_refusal_is_tracked_too(self):
        t = task(1, cat="review")
        mesh = FakeMesh([t], max_concurrent=1)
        gate = ("import os,sys\nif os.environ['MESH_RUNNER_PHASE']=='admit':\n"
                " print('qa_no_selection:attempt_limit'); sys.exit(76)\n")
        self.agent.start_gate_command = [sys.executable, "-c", gate]
        r = self.runner(mesh)
        r.refresh_me()  # _start_one normally runs after tick has loaded the agent identity
        review = dict(mesh.tasks[t["id"]], _mode="review")
        self.assertFalse(r._start_one(review))
        self.assertTrue(r.store.mark("builder", t["id"], "review_gate_hold")[0])

    def test_turn_budget_routes_the_task_to_the_lead(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        self.cfg.daily_next_budget = 2
        self.agent.lead = "lead"
        write_replies(self.replies, {t["id"][:8]: "↻ NEXT"})
        r = self.runner(mesh)
        r.tick()
        for turn in range(3):
            finished = self.finish_turn(r)
            if turn < 2:
                self.assertEqual(finished.state, "ready")
                r.tick()
        self.assertEqual(mesh.tasks[t["id"]]["assignee_id"], "agent-lead")
        self.assertEqual(len(calls(self.calls)), 3)
        self.assertTrue(any("TURN CAP" in c["body"] for c in mesh.comment_log[t["id"]]))


# Relative-time/NL heuristic coverage moved to test_typed_time_guard: a rejected
# declaration cannot become accepted by repetition, short duration, or daily notice.
class FastWaits(Bench):
    def age_wait(self, r, hours):
        for a in r.store.by_state(self.agent.name, ("waiting",)):
            w = dict(a.wait)
            w["started"] = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)).isoformat()
            r.store.update(a, wait=w)
        r._last_wake_check.clear()

    def test_a_card_wait_on_a_card_that_waits_on_this_agent_is_woken_as_a_cycle(self):
        t, other = task(1), task(0xabcdef12, assignee_id="someone-else")
        mesh = FakeMesh([t, other], max_concurrent=1)
        write_replies(self.replies, {t["id"][:8]: "⏳ WAIT card:#abcdef12",
                                     t["id"][:8] + ":resume": "⏳ WAIT decision:@executor: step 0.1.1"})
        r = self.runner(mesh)
        r.tick()
        self.settle_all(r)
        self.assertEqual(len(calls(self.calls)), 1)
        theirs = r.store.create("Executor", other["id"], "claude", "fixture")
        r.store.update(theirs, state="waiting", wait={"kind": "decision", "handle": "builder",
                                                      "line": "⏳ WAIT decision:@builder: step 0.1.1"})
        r._last_wake_check.clear()
        r.tick()
        self.settle_all(r)
        woken = calls(self.calls)[1]["prompt"]            # (a third turn: the fake Mesh knows no @executor)
        self.assertIn("wait cycle", woken)
        self.assertIn("decision:@executor", woken)




    def test_an_unchanged_card_wait_does_not_start_a_blocking_ask_by_age(self):
        t = task(1)
        mesh = FakeMesh([t, task(0xabcdef12, assignee_id="someone-else")], max_concurrent=1)
        write_replies(self.replies, {t["id"][:8]: "⏳ WAIT card:#abcdef12",
                                     t["id"][:8] + ":resume": "❓ Blocking @person: which seller?"})
        r = self.runner(mesh)
        r.tick()
        self.settle_all(r)
        self.age_wait(r, hours=3)
        r.tick()
        self.settle_all(r)
        kinds = [d for a in r.store.db.execute("SELECT id FROM attempts") for (_, k, d) in r.store.events(a[0])
                 if k == "outcome"]
        self.assertEqual(kinds, ["wait"])

    def test_the_same_wait_declared_again_after_a_comment_is_not_counted_by_the_budget(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        write_replies(self.replies, {t["id"][:8]: "⏳ WAIT decision:@lead",
                                     t["id"][:8] + ":resume": "⏳ WAIT decision:@lead"})
        r = self.runner(mesh)
        r.tick()
        self.settle_all(r)
        mesh.add_comment(t["id"], "FYI, nothing changed", author="lead")
        r.tick()
        self.settle_all(r)
        self.assertEqual(r.store.outcomes_today(self.agent.name, t["id"]), {"wait": 1, "recheck": 1})

    def test_a_wait_on_a_different_handle_after_a_comment_is_counted_by_the_budget(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        write_replies(self.replies, {t["id"][:8]: "⏳ WAIT decision:@lead",
                                     t["id"][:8] + ":resume": "⏳ WAIT decision:@executor"})
        r = self.runner(mesh)
        r.tick()
        self.settle_all(r)
        mesh.add_comment(t["id"], "FYI", author="lead")
        r.tick()
        self.settle_all(r)
        self.assertNotIn("recheck", r.store.outcomes_today(self.agent.name, t["id"]))

    def test_a_strangers_comment_on_a_decision_wait_does_not_make_the_next_wait_a_step(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        write_replies(self.replies, {t["id"][:8]: "⏳ WAIT decision:@lead",
                                     t["id"][:8] + ":resume": "⏳ WAIT decision:@executor"})
        r = self.runner(mesh)
        r.tick()
        self.settle_all(r)
        mesh.add_comment(t["id"], "unrelated remark", author="bystander")   # not @lead, no answer marker
        r.tick()
        self.settle_all(r)
        used = r.store.outcomes_today(self.agent.name, t["id"])
        self.assertNotIn("step", used)
        self.assertGreaterEqual(used.get("wait", 0), 2)

    def test_the_addressee_or_an_answer_marker_makes_a_decision_wait_met(self):
        from mesh_runner.wake import check
        w = {"kind": "decision", "handle": "lead", "since": "2026-10-18T00:00:00+00:00",
             "started": "2026-10-18T00:00:00+00:00"}
        now = dt.datetime(2026, 10, 18, 0, 5, tzinfo=dt.timezone.utc)

        def comment(author, body):
            return [{"id": "c", "author_id": author, "author_name": author, "body": body,
                     "created_at": "2026-10-18T00:01:00+00:00"}]
        for author, body, met in (("lead", "ok", True), ("bystander", "ok", False),
                                  ("bystander", "ANSWER: go", True), ("bystander", "✅ Решение: go", True)):
            wait = dict(w)
            self.assertIn("commented", check(None, None, None, {}, wait, "me", comment(author, body), now))
            self.assertEqual(bool(wait.get("met")), met, (author, body))

    def test_a_later_stranger_comment_does_not_hide_the_addressees_answer(self):
        from mesh_runner.wake import check
        w = {"kind": "decision", "handle": "lead", "since": "2026-10-18T00:00:00+00:00",
             "started": "2026-10-18T00:00:00+00:00"}
        now = dt.datetime(2026, 10, 18, 0, 5, tzinfo=dt.timezone.utc)
        comments = [{"id": "1", "author_id": "lead", "author_name": "lead", "body": "go",
                     "created_at": "2026-10-18T00:01:00+00:00"},
                    {"id": "2", "author_id": "bystander", "author_name": "bystander", "body": "noise",
                     "created_at": "2026-10-18T00:02:00+00:00"}]            # oldest first
        check(None, None, None, {}, w, "me", comments, now)
        self.assertTrue(w.get("met"))

    def test_a_wait_declared_after_its_pipeline_finished_is_a_step_not_an_idle_wait(self):
        t = task(1)
        mesh, gl = FakeMesh([t], max_concurrent=1), FakeGitLab()
        gl.pipelines["g/app#5"] = "running"
        write_replies(self.replies, {t["id"][:8]: "⏳ WAIT pipeline:g/app#5",
                                     t["id"][:8] + ":resume": "⏳ WAIT pipeline:g/app#6"})
        r = self.runner(mesh, gl)
        r.tick()
        self.settle_all(r)
        gl.pipelines["g/app#5"] = "success"           # the awaited pipeline finished: the next wait is progress
        r._last_wake_check.clear()
        r.tick()
        self.settle_all(r)
        self.assertEqual(r.store.outcomes_today(self.agent.name, t["id"]), {"wait": 1, "step": 1})

    def test_a_new_wait_after_a_plain_comment_on_a_card_wait_is_still_an_idle_wait(self):
        t = task(1)
        mesh = FakeMesh([t, task(0xabcdef12, assignee_id="someone-else")], max_concurrent=1)
        write_replies(self.replies, {t["id"][:8]: "⏳ WAIT card:#abcdef12",
                                     t["id"][:8] + ":resume": "⏳ WAIT decision:@executor"})
        r = self.runner(mesh)
        r.tick()
        self.settle_all(r)
        mesh.add_comment(t["id"], "FYI", author="lead")   # not the awaited card closing
        r.tick()
        self.settle_all(r)
        used = r.store.outcomes_today(self.agent.name, t["id"])
        # (a third turn follows: the fake Mesh knows no @executor, so the runner tells the agent once)
        self.assertNotIn("step", used)
        self.assertGreaterEqual(used.get("wait", 0), 2)

    def test_steps_have_their_own_budget(self):
        t = task(1)
        mesh, gl = FakeMesh([t], max_concurrent=1), FakeGitLab()
        self.cfg.daily_step_budget = 0
        self.agent.lead = "lead"
        gl.pipelines["g/app#5"] = "running"
        write_replies(self.replies, {t["id"][:8]: "⏳ WAIT pipeline:g/app#5",
                                     t["id"][:8] + ":resume": "⏳ WAIT pipeline:g/app#6"})
        r = self.runner(mesh, gl)
        r.tick()
        self.settle_all(r)
        gl.pipelines["g/app#5"] = "failed"
        r._last_wake_check.clear()
        r.tick()
        self.settle_all(r)
        self.assertEqual(mesh.tasks[t["id"]]["assignee_id"], "agent-lead")
        self.assertTrue(any("1 steps" in c["body"] for c in mesh.comment_log[t["id"]]))




    def test_the_ignore_list_is_read_from_the_config_file(self):
        from mesh_runner.config import RunnerConfig, load
        self.assertEqual(RunnerConfig(mesh_url="x", state_dir=Path("/tmp"), agents=[]).time_wait_ignore_names, [])
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "runner.toml"
            cfg.write_text('[runner]\nmesh_url = "x"\nstate_dir = "%s"\ntime_wait_ignore_names = ["Store"]\n' % tmp)
            self.assertEqual(load(cfg).time_wait_ignore_names, ["Store"])




    def test_a_locally_held_review_does_not_take_the_review_slot_of_the_tick(self):
        stuck, fresh = task(1, cat="review"), task(2, cat="review")
        mesh = FakeMesh([stuck, fresh], max_concurrent=2)
        write_replies(self.replies, {fresh["id"][:8]: "SLEEP:1:accepted"})
        r = self.runner(mesh)
        r.store.mark("builder", stuck["id"], "review_unstartable", "qa_refused:x")
        r.tick()
        running = [a.task_id for a in r.store.by_state("builder", ("running", "starting"))]
        self.assertEqual(running, [fresh["id"]])

    def test_an_unstartable_review_is_retried_after_the_retry_window(self):
        class Clock:
            wall = dt.datetime(2026, 10, 20, 12, 0, tzinfo=dt.timezone.utc)
            mono = 100.0

            def now(self):
                return self.wall

            def monotonic(self):
                return self.mono

            def advance(self, seconds):
                self.wall += dt.timedelta(seconds=seconds)
                self.mono += seconds

        clock = Clock()
        t, delayed = task(1, cat="review"), task(2, cat="review")
        mesh = FakeMesh([t, delayed], max_concurrent=1)
        self.cfg.review_unstartable_retry_minutes = 0.01           # 0.6 s, advanced deterministically
        r = self.runner(mesh)
        with mock.patch("mesh_runner.runner.now", clock.now), \
                mock.patch("mesh_runner.store.now", clock.now), \
                mock.patch("mesh_runner.runner.time", type("ClockTime", (), {"monotonic": clock.monotonic})()):
            # A CI deschedule longer than the window before the first observation must not
            # be confused with a live hold; this was the flaky boundary in the old sleep test.
            r.store.mark("builder", delayed["id"], "review_unstartable", "qa_refused:x")
            clock.advance(0.7)
            delayed_hold = lambda: r.hold_reason(dict(mesh.tasks[delayed["id"]], _mode="review"), "review")  # noqa: E731
            self.assertNotIn("no reviewer", delayed_hold())

            r.store.mark("builder", t["id"], "review_unstartable", "qa_refused:x")
            hold = lambda: r.hold_reason(dict(mesh.tasks[t["id"]], _mode="review"), "review")  # noqa: E731
            self.assertIn("no reviewer", hold())                    # cached only until the retry moment
            clock.advance(0.7)
            self.assertNotIn("no reviewer", hold())
            self.assertNotIn("no reviewer", hold())                 # expired hold does not return from cache


    def test_new_timing_defaults_in_code_and_in_load(self):
        from mesh_runner.config import RunnerConfig, load
        d = RunnerConfig(mesh_url="x", state_dir=Path("/tmp"), agents=[])
        self.assertEqual((d.failed_cooldown_hours, d.wait_first_recheck_hours, d.max_wait_hours,
                          d.time_wait_reason_after_minutes, d.review_unstartable_retry_minutes,
                          d.review_refeed_hours), (0.5, 2.0, 6.0, 60.0, 60.0, 4.0))
        with tempfile.TemporaryDirectory() as tmp:
            key = Path(tmp) / "k"
            key.write_text("secret\n")
            key.chmod(0o600)
            cfg = Path(tmp) / "r.toml"
            cfg.write_text(f'[runner]\nmesh_url = "https://m"\nstate_dir = "{tmp}"\n'
                           f'[[agents]]\nname = "a"\nkey_file = "{key}"\nharness = "claude"\nprofile_dir = "{tmp}"\n')
            c = load(cfg)
        self.assertEqual((c.failed_cooldown_hours, c.wait_first_recheck_hours, c.max_wait_hours,
                          c.time_wait_reason_after_minutes, c.review_unstartable_retry_minutes,
                          c.review_refeed_hours), (0.5, 2.0, 6.0, 60.0, 60.0, 4.0))

    def test_a_failed_task_is_retried_after_half_an_hour_without_a_comment(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.runner(mesh)
        self.cfg.failed_cooldown_hours = 0.5
        recent = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=20)
        old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=31)
        self.assertTrue(r._failed_recently(t["id"], recent))
        self.assertFalse(r._failed_recently(t["id"], old))

    def test_an_expired_review_hold_does_not_come_back_from_the_cache(self):
        t = task(1, cat="review")
        mesh = FakeMesh([t], max_concurrent=1)
        self.cfg.review_unstartable_retry_minutes = 0.01
        r = self.runner(mesh)
        r.store.mark("builder", t["id"], "review_unstartable", "qa_refused:x")
        hold = lambda: r.hold_reason(dict(mesh.tasks[t["id"]], _mode="review"), "review")  # noqa: E731
        self.assertIn("no reviewer", hold())
        time.sleep(0.7)
        self.assertNotIn("no reviewer", hold())
        self.assertNotIn("no reviewer", hold())                    # and not on the next look either

    def test_a_gate_hold_on_a_review_ends_by_the_retry_moment(self):
        t = task(1, cat="review")
        mesh = FakeMesh([t], max_concurrent=1)
        self.cfg.review_unstartable_retry_minutes = 0.01
        r = self.runner(mesh)
        r._gate_hold[t["id"]] = ("qa_refused:x", time.monotonic() + 600, t.get("updated_at") or "")
        r._review_gate_refused(mesh.tasks[t["id"]], "qa_refused:x")
        self.assertLessEqual(r._gate_hold[t["id"]][1], time.monotonic() + 1)

    def test_a_capacity_hold_is_not_shortened_by_the_review_retry(self):
        t = task(1, cat="review")
        mesh = FakeMesh([t], max_concurrent=1)
        self.cfg.review_unstartable_retry_minutes = 0.01
        r = self.runner(mesh)
        until = time.monotonic() + 600
        r._gate_hold[t["id"]] = ("profile pool full", until, "")
        r._review_gate_refused(mesh.tasks[t["id"]], "profile pool full")
        self.assertEqual(r._gate_hold[t["id"]][1], until)

    def test_a_recheck_mark_of_another_attempt_is_not_counted(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        write_replies(self.replies, {t["id"][:8]: f"⏳ WAIT {FUTURE_AT}: external=fixture-window source=card:#abcdef12 scheduled external window"})
        r = self.runner(mesh)
        r.store.mark("builder", t["id"], "recheck_wake", "some-old-attempt:3")
        r.tick()
        self.settle_all(r)
        a = r.store.by_state(self.agent.name, ("waiting",))[0]
        self.assertNotIn("rechecks", a.wait)
        self.assertEqual(r.store.outcomes_today(self.agent.name, t["id"]).get("wait"), 1)

    def test_a_persons_ask_is_rechecked_only_after_a_day(self):
        from mesh_runner.wake import check
        w = {"kind": "blocking", "handle": "person", "since": "2026-10-18T00:00:00+00:00",
             "started": "2026-10-18T00:00:00+00:00"}
        gated = {"human_gate": True}
        at = lambda h: dt.datetime(2026, 10, 18, tzinfo=dt.timezone.utc) + dt.timedelta(hours=h)  # noqa: E731
        kw = dict(max_wait=dt.timedelta(hours=6), first_recheck=dt.timedelta(hours=2))
        self.assertEqual(check(None, None, None, gated, dict(w), "me", [], at(7), **kw), "")
        self.assertIn("re-check", check(None, None, None, gated, dict(w), "me", [], at(25), **kw))

    def test_a_daily_claim_is_taken_once(self):
        r = self.runner(FakeMesh([task(1)], max_concurrent=1))
        self.assertTrue(r.store.claim("*", "t1", "k", "2026-10-18"))
        self.assertFalse(r.store.claim("*", "t1", "k", "2026-10-18"))
        self.assertTrue(r.store.claim("*", "t1", "k", "2026-10-19"))

    def test_an_unchanged_decision_wait_does_not_age_wake_even_with_zero_interval(self):
        from mesh_runner.wake import check
        w = {"kind": "decision", "handle": "x", "since": "2026-10-18T00:00:00+00:00",
             "started": "2026-10-18T00:00:00+00:00"}
        at = dt.datetime(2026, 10, 18, 0, 1, tzinfo=dt.timezone.utc)
        self.assertEqual(check(None, None, None, {}, w, "me", [], at,
                               max_wait=dt.timedelta(hours=6), first_recheck=dt.timedelta(0)), "")

    def test_a_decision_comment_still_wakes_an_old_wait(self):
        from mesh_runner.wake import check
        w = {"kind": "decision", "handle": "x", "since": "2026-10-18T00:00:00+00:00",
             "started": "2026-10-18T00:00:00+00:00"}
        at = dt.datetime(2026, 10, 18, 7, tzinfo=dt.timezone.utc)
        comments = [{"author_id": "human", "author_name": "Human", "created_at": at.isoformat(),
                     "body": "approved"}]
        self.assertIn("Human commented", check(None, None, None, {}, w, "me", comments, at))

    def test_a_review_that_reached_the_gate_and_raised_still_spends_the_slot(self):
        first, second = task(1, cat="review"), task(2, cat="review")
        mesh = FakeMesh([first, second], max_concurrent=2)
        r = self.runner(mesh)
        seen = []

        def boom(task):
            seen.append(task["id"])
            r._gate_asked = True
            raise RuntimeError("harness missing")
        r._start_one = boom
        r.start_new(2)
        self.assertEqual(seen, [first["id"]])                      # the second review waits for the next tick


class DailyBudgetHold(Bench):
    def test_a_no_lead_turn_budget_failure_waits_for_tomorrow_or_a_comment(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.runner(mesh)
        a = r.store.create("builder", t["id"], "claude", "t")
        r.store.update(a, state="failed", note="turn budget: 0 NEXT and 9 waits today on this task")
        hold = lambda: r.hold_reason(dict(mesh.tasks[t["id"]]), "work")  # noqa: E731
        self.cfg.failed_cooldown_hours = 0                  # the cooldown alone would release it
        self.assertIn("daily turn budget spent", hold())
        mesh.add_comment(t["id"], "Person: do it now", author="person",
                         at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=1))
        r._hold_cache.clear()
        self.assertEqual(hold(), "")

    def test_a_comment_releases_the_budget_hold_but_not_a_blocker(self):
        blocker, t = task(9, cat="in_progress", assignee_id="someone-else"), task(1)
        mesh = FakeMesh([blocker, t], max_concurrent=1)
        mesh.deps[t["id"]] = [{"depends_on_task_id": blocker["id"], "dependency_type": "blocks"}]
        r = self.runner(mesh)
        a = r.store.create("builder", t["id"], "claude", "t")
        r.store.update(a, state="failed", note="turn budget: 0 NEXT and 9 waits today on this task")
        mesh.add_comment(t["id"], "go on", author="person",
                         at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=1))
        self.assertIn("blocked by", r.hold_reason(dict(mesh.tasks[t["id"]]), "work"))

    def test_a_comment_also_releases_a_lead_unavailable_budget_backoff(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.runner(mesh)
        a = r.store.create("builder", t["id"], "claude", "t")
        r.store.update(a, state="failed", note="turn budget: 0 NEXT and 9 waits today; lead capped")
        r._keep_until_tomorrow(t["id"], "TURN CAP: x.", "turn budget")
        hold = lambda: r.hold_reason(dict(mesh.tasks[t["id"]]), "work")  # noqa: E731
        self.assertIn("daily turn budget spent", hold())
        self.assertIn("unless someone writes here", mesh.comment_log[t["id"]][-1]["body"])
        mesh.add_comment(t["id"], "go on", author="person",
                         at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=1))
        self.assertEqual(hold(), "")

    def test_a_later_backoff_is_not_taken_for_the_budget_one(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.runner(mesh)
        a = r.store.create("builder", t["id"], "claude", "t")
        r.store.update(a, state="failed", note="turn budget: 0 NEXT and 9 waits today; lead capped")
        r._keep_until_tomorrow(t["id"], "TURN CAP: x.", "turn budget")
        r._task_backoff[t["id"]] = time.monotonic() + 900       # e.g. a checkout refused later
        mesh.add_comment(t["id"], "go on", author="person",
                         at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=1))
        self.assertEqual(r.hold_reason(dict(mesh.tasks[t["id"]]), "work"), "backing off after a failed start")
        self.assertNotIn(t["id"], r._budget_backoff)

    def test_the_budget_hold_is_published_as_a_runtime_state(self):
        from mesh_runner.effects import skip_state
        self.assertEqual(skip_state("daily turn budget spent; waits for tomorrow (UTC) or a new comment", "x")["kind"],
                         "stopped")

    def test_a_budget_failure_from_an_earlier_day_does_not_hold(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.runner(mesh)
        a = r.store.create("builder", t["id"], "claude", "t")
        r.store.update(a, state="failed", note="turn budget: 0 NEXT and 9 waits today on this task")
        r.store.db.execute("UPDATE attempts SET updated_at=? WHERE id=?",
                           ((dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1, hours=1)).isoformat(), a.id))
        self.cfg.failed_cooldown_hours = 6                  # yesterday's budget failure: no cooldown either
        self.assertEqual(r.hold_reason(dict(mesh.tasks[t["id"]]), "work"), "")


class RuntimeStateUntil(unittest.TestCase):
    """Mesh refuses a runtime_state whose until is earlier than its since (= the write time)."""

    def _state(self, until):
        from types import SimpleNamespace
        from mesh_runner.effects import runtime_state
        a = SimpleNamespace(state="waiting", wait={"kind": "time", "until": until})
        return runtime_state(a, "x")

    def test_an_expired_until_is_left_out(self):
        past = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)).isoformat(timespec="milliseconds")
        self.assertEqual(self._state(past), {"kind": "waiting", "ref": "time"})

    def test_an_until_about_to_pass_is_left_out(self):
        soon = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=5)).isoformat(timespec="milliseconds")
        self.assertNotIn("until", self._state(soon))

    def test_a_future_until_is_kept(self):
        later = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=2)).isoformat(timespec="milliseconds")
        self.assertEqual(self._state(later)["until"], later)

    def test_an_unparsable_until_is_left_out(self):
        self.assertNotIn("until", self._state("garbage"))


class Reviews(Bench):
    def test_review_card_is_fed_first_with_the_review_protocol_and_completes_when_decided(self):
        rev, work = task(1, cat="review"), task(2, "urgent")
        mesh = FakeMesh([rev, work], max_concurrent=1)
        review_gate = HarnessGate(self.root / "review")
        work_gate = HarnessGate(self.root / "work")
        write_replies(self.replies, {rev["id"][:8]: review_gate.reply("accepted"),
                                    work["id"][:8]: work_gate.reply(f"⏳ WAIT {FUTURE_AT}: external=fixture-window source=card:#abcdef12 soak window")})
        r = self.runner(mesh)
        r.tick()
        a = r.store.by_state("builder", ("running",))[0]
        review_proc = r.procs[a.id]
        with review_gate.hold(review_proc):
            self.assertEqual((a.task_id, a.mode), (rev["id"], "review"))   # review before an urgent todo
            self.assertEqual(mesh.category(mesh.tasks[rev["id"]]), "review")  # not moved to in_progress
            self.assertNotIn(work["id"], mesh.checkouts)
            mesh.set_category(rev["id"], "done")
            review_gate.release()
            review_proc.wait(timeout=10)
            r.tick()                               # settle this review, then admit the urgent todo
            urgent = r.store.by_state("builder", ("running",))[0]
            with work_gate.hold(r.procs[urgent.id]):
                self.assertEqual(r.store.get(a.id).state, "completed")
                self.assertEqual(urgent.task_id, work["id"])
                self.assertIsNone(r.procs[urgent.id].poll())  # completion must not wait for unrelated work
        r.settle_finished()
        self.assertEqual(r.store.get(urgent.id).state, "waiting")

    def test_review_card_is_not_refed_without_a_new_comment(self):
        rev = task(1, cat="review")
        mesh = FakeMesh([rev], max_concurrent=1)
        write_replies(self.replies, {rev["id"][:8]: "looked at it"})
        r = self.runner(mesh)
        self.cfg.max_no_outcome = 0
        r.tick()
        self.settle_all(r)
        for _ in range(3):
            r.tick()
        self.assertEqual(len(calls(self.calls)), 1)
        mesh.add_comment(rev["id"], "fixed, please re-check", author="author-agent")
        r.tick()
        self.settle_all(r)
        self.assertEqual(len(calls(self.calls)), 2)

    def test_umbrella_with_only_parked_children_is_not_fed(self):
        epic = task(1, subtask_count=2)
        kids = [task(2, cat="backlog", parent_task_id=epic["id"]), task(3, human_gate=True, parent_task_id=epic["id"])]
        for k in kids:
            k["assignee_id"] = "someone-else"
        mesh = FakeMesh([epic, *kids], max_concurrent=1)
        r = self.runner(mesh)
        self.assertIn("umbrella", r.hold_reason(dict(epic, _mode="work"), "work"))

    def test_parent_is_refed_only_when_a_child_changes(self):
        parent = task(1, subtask_count=1)
        kid = task(2, cat="in_progress", parent_task_id=parent["id"])
        kid["assignee_id"] = "someone-else"
        mesh = FakeMesh([parent, kid], max_concurrent=1)
        write_replies(self.replies, {parent["id"][:8]: "⏳ WAIT card:#" + kid["id"][:8]})
        r = self.runner(mesh)
        r.tick()
        self.settle_all(r)
        a = r.store.by_state("builder", ("waiting",))[0]
        r._finish(a, "completed", "test: pretend the wait ended")   # parent back in the queue
        self.assertIn("parent waits", r.hold_reason(dict(mesh.tasks[parent["id"]], _mode="work"), "work"))
        mesh.set_category(kid["id"], "review")
        r._hold_cache.clear()
        self.assertEqual(r.hold_reason(dict(mesh.tasks[parent["id"]], _mode="work"), "work"), "")


class Hardening(Bench):
    def test_store_reads_rows_written_by_a_newer_schema(self):
        # a newer runner (a plan run, or the version rolled back from) may have added
        # columns to the shared file; reading it must not fail every tick
        store = Store(self.root / "state" / "r.sqlite")
        a = store.create("builder", "t1", "claude", "title")
        store.db.execute("ALTER TABLE attempts ADD COLUMN from_the_future TEXT NOT NULL DEFAULT 'x'")
        self.assertEqual(store.get(a.id).task_id, "t1")
        self.assertEqual([x.id for x in store.by_state("builder", (a.state,))], [a.id])

    def test_pool_fetch_uses_the_agent_identity_from_the_first_start(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        self.agent.env_command = [sys.executable, "-c", "print('PROBE=lane-identity')"]
        write_replies(self.replies, {t["id"][:8]: f"⏳ WAIT {FUTURE_AT}: external=fixture-window source=card:#abcdef12 soak window"})
        r = self.runner(mesh)
        seen, lease = [], r.pool.lease
        r.pool.lease = lambda *a, **k: (seen.append(r.pool.env.get("PROBE")), lease(*a, **k))[1]
        r.tick()
        self.settle_all(r)
        self.assertEqual(seen, ["lane-identity"])

    def test_workspace_failure_pauses_starts_and_writes_nothing_on_tasks(self):
        from mesh_runner.workspace import WorkspaceError
        t1, t2 = task(1), task(2)
        mesh = FakeMesh([t1, t2], max_concurrent=2)
        r = self.runner(mesh)

        def denied(*a, **k):
            raise WorkspaceError("git fetch origin --prune: Permission denied (publickey)")
        r.pool.lease = denied
        r.tick()
        self.assertEqual(calls(self.calls), [])
        self.assertEqual(mesh.comment_log.get(t1["id"], []) + mesh.comment_log.get(t2["id"], []), [])
        self.assertIn("Permission denied", r.status()["error"])
        aborted = [a for a in r.store.by_state("builder", ("aborted",))]
        self.assertEqual(len(aborted), 1)                 # the second task was not tried

    def test_harness_that_dies_before_its_stream_reports_its_stderr(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        write_replies(self.replies, {t["id"][:8]: "DIE:Not inside a trusted directory"})
        r = self.runner(mesh)
        for _ in range(self.cfg.max_failures + 2):
            r.tick()
            self.settle_all(r)
        bodies = " ".join(c["body"] for c in mesh.comment_log.get(t["id"], []))
        self.assertIn("Not inside a trusted directory", bodies)

    def test_start_gate_holds_then_passes_and_turn_end_gets_its_output(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        verdict = self.root / "verdict"
        verdict.write_text("hold")
        released = self.root / "released"
        gate = ("import os,sys,pathlib\n"
                f"v=pathlib.Path({str(verdict)!r}).read_text()\n"
                "if v=='hold':\n print('profile pool full'); sys.exit(75)\n"
                "print('RESERVATION=r-'+os.environ['MESH_RUNNER_TASK'][:8])\n")
        end = ("import os,pathlib\n"
               f"pathlib.Path({str(released)!r}).open('a').write("
               "os.environ.get('MESH_RUNNER_GATE_RESERVATION','')+' '+os.environ['MESH_RUNNER_OUTCOME']+'\\n')\n")
        self.agent.start_gate_command = [sys.executable, "-c", gate]
        self.agent.turn_end_command = [sys.executable, "-c", end]
        write_replies(self.replies, {t["id"][:8]: f"⏳ WAIT {FUTURE_AT}: external=fixture-window source=card:#abcdef12 soak window"})
        r = self.runner(mesh)
        r.tick()
        self.assertEqual(calls(self.calls), [])
        self.assertEqual(r.hold_reason(t, "work"), "gate: profile pool full")
        verdict.write_text("go")
        r._gate_hold.clear()
        r.tick()
        self.settle_all(r)
        self.assertEqual(len(calls(self.calls)), 1)
        self.assertEqual(released.read_text().split(), [f"r-{t['id'][:8]}", "turn-end"])

    def test_a_hold_of_one_task_does_not_stop_the_agents_other_starts(self):
        gated, free = task(1, "urgent"), task(2, "low")
        mesh = FakeMesh([gated, free], max_concurrent=2)
        gate = ("import os,sys\n"
                f"if os.environ['MESH_RUNNER_TASK']=={gated['id']!r}:\n print('waits for a human'); sys.exit(76)\n")
        self.agent.start_gate_command = [sys.executable, "-c", gate]
        write_replies(self.replies, {free["id"][:8]: f"⏳ WAIT {FUTURE_AT}: external=fixture-window source=card:#abcdef12 soak window"})
        r = self.runner(mesh)
        r.tick()                                   # the gated task first (urgent), then the free one
        self.settle_all(r)
        self.assertEqual([c["task"] for c in calls(self.calls)], [free["id"][:8]])

    def test_a_lane_wide_hold_still_stops_the_tick(self):
        a, b = task(1, "urgent"), task(2, "low")
        mesh = FakeMesh([a, b], max_concurrent=2)
        self.agent.start_gate_command = [sys.executable, "-c", "import sys; print('pool full'); sys.exit(75)"]
        r = self.runner(mesh)
        r.tick()
        self.assertEqual(calls(self.calls), [])
        self.assertIn(a["id"], r._gate_hold)
        self.assertNotIn(b["id"], r._gate_hold)    # not asked: the pool answer holds for all

    def test_start_cap_routes_the_task_to_the_lead(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        self.cfg.daily_start_cap = 2
        write_replies(self.replies, {t["id"][:8]: "handed on"})
        r = self.runner(mesh)
        for _ in range(2):                          # two starts, each ends without an outcome -> released
            r.tick()
            self.settle_all(r)
            mesh.tasks[t["id"]]["assignee_id"] = AGENT_ID
            mesh.add_comment(t["id"], "again", author="someone")
            r._task_backoff.clear()
        self.agent.lead = "lead"
        r.tick()                                    # the third start is the cap
        self.assertEqual(mesh.tasks[t["id"]]["assignee_id"], "agent-lead")
        self.assertTrue(any("START CAP" in c["body"] for c in mesh.comment_log[t["id"]]))
        self.assertEqual(r.store.starts_today("builder", t["id"]), 2)

    def test_decision_request_is_not_bound_by_the_daily_start_cap(self):
        # one request card is reopened for every new question of the same requester, so each
        # answer is a fresh start; a cap would leave the requester waiting for an answer
        t = task(1)
        t["labels"] = ["decision-request"]
        mesh = FakeMesh([t], max_concurrent=1)
        self.cfg.daily_start_cap = 2
        write_replies(self.replies, {t["id"][:8]: "handed on"})
        r = self.runner(mesh)
        for _ in range(3):
            r.tick()
            self.settle_all(r)
            mesh.tasks[t["id"]]["assignee_id"] = AGENT_ID
            mesh.add_comment(t["id"], "again", author="someone")
            r._task_backoff.clear()
        self.assertEqual(r.store.starts_today("builder", t["id"]), 3)
        self.assertFalse(any("START CAP" in c["body"] for c in mesh.comment_log[t["id"]]))

    def test_compaction_cap_hands_the_task_up(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        self.cfg.max_compactions = 2
        self.agent.lead = "lead"
        write_replies(self.replies, {t["id"][:8]: f"COMPACT:2:⏳ WAIT {FUTURE_AT}: external=fixture-window source=card:#abcdef12 soak window"})
        r = self.runner(mesh)
        r.tick()
        self.settle_all(r)
        self.assertEqual(mesh.tasks[t["id"]]["assignee_id"], "agent-lead")
        self.assertTrue(any("COMPACTION CAP" in c["body"] for c in mesh.comment_log[t["id"]]))

    def _gate_scripts(self, verdicts: dict):
        """A gate that answers per phase from a file; a turn-end hook that logs."""
        vfile, log_file = self.root / "verdicts.json", self.root / "hooklog"
        vfile.write_text(json.dumps(verdicts))
        gate = ("import os,sys,json,pathlib\n"
                f"v=json.loads(pathlib.Path({str(vfile)!r}).read_text()).get(os.environ['MESH_RUNNER_PHASE'],'go')\n"
                f"pathlib.Path({str(log_file)!r}).open('a').write(os.environ['MESH_RUNNER_PHASE']+' '+"
                "os.environ.get('MESH_RUNNER_CHECKOUT_GENERATION','-')+' '+os.environ.get('MESH_RUNNER_GATE_RES','')+'\\n')\n"
                "if v=='hold':\n print('pool full'); sys.exit(75)\n"
                "print('RES=r1')\n"
                "if os.environ['MESH_RUNNER_PHASE']=='admit': print('ADMITTED=1')\n")
        end = (f"import os,pathlib\npathlib.Path({str(log_file)!r}).open('a').write('end '+"
               "os.environ.get('MESH_RUNNER_GATE_ADMITTED','0')+' '+os.environ['MESH_RUNNER_OUTCOME']+'\\n')\n")
        self.agent.start_gate_command = [sys.executable, "-c", gate]
        self.agent.turn_end_command = [sys.executable, "-c", end]
        return vfile, log_file

    def test_gate_phases_reserve_admit_end(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        _, log_file = self._gate_scripts({})
        write_replies(self.replies, {t["id"][:8]: f"⏳ WAIT {FUTURE_AT}: external=fixture-window source=card:#abcdef12 soak window"})
        r = self.runner(mesh)
        r.tick()
        self.settle_all(r)
        lines = log_file.read_text().split("\n")
        self.assertTrue(lines[0].startswith("reserve - "))
        self.assertTrue(lines[1].startswith("admit ") and not lines[1].startswith("admit - "))   # writer known
        self.assertEqual(lines[1].split()[-1], "r1")                          # reserve output reached admit
        self.assertIn("end 1 turn-end", lines)

    def test_hold_at_admit_aborts_quietly_and_releases(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        _, log_file = self._gate_scripts({"admit": "hold"})
        r = self.runner(mesh)
        r.tick()
        self.assertEqual(calls(self.calls), [])
        self.assertEqual(mesh.comment_log.get(t["id"], []), [])
        self.assertIn("end 0 aborted", log_file.read_text())
        self.assertTrue(r.hold_reason(t, "work").startswith("gate: pool full"))

    def test_resume_is_gated_and_finish_releases_a_stale_gate(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        vfile, log_file = self._gate_scripts({})
        write_replies(self.replies, {t["id"][:8]: f"⏳ WAIT {FUTURE_AT}: external=fixture-window source=card:#abcdef12 soak window"})
        r = self.runner(mesh)
        r.tick()
        self.settle_all(r)
        a = [x for x in r.store.by_state("builder", ("waiting",))][0]
        r.store.update(a, state="ready", wake_text="go on")
        vfile.write_text(json.dumps({"reserve": "hold"}))
        r.tick()
        self.assertEqual(len(calls(self.calls)), 1)                            # resume held
        self.assertEqual(r.store.get(a.id).state, "ready")
        # a crash left a gate behind: finishing the attempt releases it
        r.store.mark("builder", t["id"], "gate", json.dumps({"ADMITTED": "1"}))
        r._finish(r.store.get(a.id), "aborted", "test")
        self.assertIn("end 1 aborted", log_file.read_text())
        self.assertEqual(r.store.mark("builder", t["id"], "gate")[0], "")

    def test_routed_harness_per_attempt_and_a_switch_starts_a_new_session(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        route = self.root / "route"
        route.write_text("codex")
        self.agent.env_command = [sys.executable, "-c",
                                  f"import pathlib;h=pathlib.Path({str(route)!r}).read_text();"
                                  "print('MESH_RUNNER_HARNESS='+h);"
                                  "print('MESH_RUNNER_PROFILE=other-acct') if h=='codex' else None"]
        self.agent.alt = {"codex": {"profile": "base"}}
        write_replies(self.replies, {t["id"][:8]: f"⏳ WAIT {FUTURE_AT}: external=fixture-window source=card:#abcdef12 soak window", t["id"][:8] + ":resume": f"⏳ WAIT {FUTURE_AT}: external=fixture-window source=card:#abcdef12 soak window"})
        r = self.runner(mesh)
        r.alt_adapters = {"codex": FakeProfileAdapter(r.adapter.script, "base")}
        r.tick()
        self.settle_all(r)
        a = r.store.by_state("builder", ("waiting",))[0]
        self.assertEqual(a.harness, "codex")
        first = calls(self.calls)[0]
        self.assertEqual(first["profile"], "other-acct")                 # routed profile on the alt harness
        route.write_text("claude")                                       # routed back while it waited
        r.store.update(a, state="ready", wake_text="go on")
        r.tick()
        self.settle_all(r)
        second = calls(self.calls)[-1]
        self.assertFalse(second["resume"])                              # a new session, not a resume
        self.assertEqual(second["profile"], "")                          # the claude-shaped fake
        self.assertEqual(r.store.get(a.id).harness, "claude")

    def test_env_failure_on_resume_keeps_the_session_and_logs_name_the_harness(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        route = self.root / "route"
        route.write_text("codex")
        self.agent.env_command = [sys.executable, "-c",
                                  f"import pathlib,sys;h=pathlib.Path({str(route)!r}).read_text();"
                                  "sys.exit(3) if h=='fail' else print('MESH_RUNNER_HARNESS='+h)"]
        self.agent.alt = {"codex": {"profile": "base"}}
        write_replies(self.replies, {t["id"][:8]: f"⏳ WAIT {FUTURE_AT}: external=fixture-window source=card:#abcdef12 soak window"})
        r = self.runner(mesh)
        r.alt_adapters = {"codex": FakeProfileAdapter(r.adapter.script, "base")}
        r.tick()
        self.settle_all(r)
        a = r.store.by_state("builder", ("waiting",))[0]
        self.assertTrue(Path(a.log_path).name.endswith(".turn1.codex.jsonl"))
        route.write_text("fail")                                         # env command breaks once
        r.store.update(a, state="ready", wake_text="the reviewer answered")
        r.tick()
        kept = r.store.get(a.id)
        self.assertEqual((kept.state, kept.harness, kept.session_id, kept.wake_text),
                         ("ready", "codex", a.session_id, "the reviewer answered"))
        self.assertEqual(len(calls(self.calls)), 1)

    def test_human_gate_flag_can_be_left_to_the_start_gate(self):
        from mesh_runner.selection import Policy, static_skip_reason
        t = task(1, human_gate=True)
        self.assertEqual(static_skip_reason(t, Policy(), dt.datetime.now(dt.timezone.utc)),
                         "waiting for a human decision")
        self.assertEqual(static_skip_reason(t, Policy(human_gate=False), dt.datetime.now(dt.timezone.utc)), "")

    def test_memory_gate_holds_new_sessions(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.runner(mesh)
        r.memgate.t.hold_free_pct, r.memgate.t.release_free_pct = 15, 20
        r.memgate._read = lambda: (10.0, None)
        r.tick()
        self.assertEqual(calls(self.calls), [])
        r.memgate._read, r.memgate._at = (lambda: (25.0, None)), None
        write_replies(self.replies, {t["id"][:8]: f"⏳ WAIT {FUTURE_AT}: external=fixture-window source=card:#abcdef12 soak window"})
        r.tick()
        self.settle_all(r)
        self.assertEqual(len(calls(self.calls)), 1)

    def test_load_gate_holds_new_sessions(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.runner(mesh)
        r.loadgate.t.hold_per_cpu = 4.0
        r.loadgate._read = lambda: (80.0, 10)                      # 8 per CPU
        with self.assertLogs("mesh_runner", "WARNING") as logs:
            r.tick()
        self.assertEqual(calls(self.calls), [])
        self.assertIn("host load: HOLD", "\n".join(logs.output))
        self.assertEqual(r.status()["load_hold"][:10], "load5 80.0")
        r.loadgate._read, r.loadgate._at = (lambda: (20.0, 10)), None   # 2 per CPU
        write_replies(self.replies, {t["id"][:8]: f"⏳ WAIT {FUTURE_AT}: external=fixture-window source=card:#abcdef12 soak window"})
        r.tick()
        self.settle_all(r)
        self.assertEqual(len(calls(self.calls)), 1)
        self.assertNotIn("load_hold", r.status())

    def test_env_command_output_reaches_the_session_and_its_failure_stops_the_start(self):
        t1, t2 = task(1), task(2)
        mesh = FakeMesh([t1], max_concurrent=1)
        self.agent.env_command = [sys.executable, "-c", "print('PROBE=from-command')"]
        write_replies(self.replies, {t1["id"][:8]: f"⏳ WAIT {FUTURE_AT}: external=fixture-window source=card:#abcdef12 soak window", t2["id"][:8]: f"⏳ WAIT {FUTURE_AT}: external=fixture-window source=card:#abcdef12 soak window"})
        r = self.runner(mesh)
        r.tick()
        self.settle_all(r)
        self.assertEqual(calls(self.calls)[0]["probe"], "from-command")
        self.agent.env_command = [sys.executable, "-c", "import sys; sys.exit(3)"]
        mesh.tasks[t2["id"]] = t2
        r.tick()
        self.assertEqual(len(calls(self.calls)), 1)                   # no session on an unknown identity
        self.assertNotIn(t2["id"][:8], {k for k, v in self.states(r).items() if v == "running"})

    def test_env_command_routes_the_harness_profile_per_spawn(self):
        # a Codex lane moved to another account gets another --profile, not an env var
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        self.agent.env_command = [sys.executable, "-c", "print('MESH_RUNNER_PROFILE=other-account')"]
        write_replies(self.replies, {t["id"][:8]: f"⏳ WAIT {FUTURE_AT}: external=fixture-window source=card:#abcdef12 soak window"})
        r = self.runner(mesh)
        r.adapter = FakeProfileAdapter(r.adapter.script, "base")
        r.tick()
        self.settle_all(r)
        call = calls(self.calls)[0]
        self.assertEqual(call["profile"], "other-account")
        self.assertEqual(call["profile_env"], "")
        self.agent.env_command = [sys.executable, "-c", "print('X=1')"]
        r._fresh_env()
        self.assertEqual(r.adapter.profile, "base")                       # routed back: configured profile

    def test_route_env_names_the_routing_without_writing_the_shared_adapter(self):
        # chat takes its harness/profile from _route_env; only a card turn applies it to the shared adapter
        mesh = FakeMesh([], max_concurrent=1)
        r = self.runner(mesh)
        r.adapter = FakeProfileAdapter(r.adapter.script, "base")
        env, harness, profile = r._route_env({"MESH_RUNNER_PROFILE": "other-account", "X": "1"})
        self.assertEqual((env, profile), ({"X": "1"}, "other-account"))
        self.assertEqual(harness, r.agent.harness)
        self.assertEqual(r.adapter.profile, "base")                       # nothing was written
        r._apply_env({"MESH_RUNNER_PROFILE": "other-account"})
        self.assertEqual(r.adapter.profile, "other-account")              # a card turn does apply it
        self.assertEqual(r._route_env({"X": "1"})[2], "base")             # not sticky: no value = configured

    def test_model_label_picks_the_model_for_every_turn(self):
        t1, t2 = task(1, labels=["model:opus"]), task(2)
        mesh = FakeMesh([t1, t2], max_concurrent=2)
        write_replies(self.replies, {t1["id"][:8]: "↻ NEXT", t1["id"][:8] + ":resume": "✅ done",
                                     t2["id"][:8]: "✅ done"})
        r = self.runner(mesh)
        for _ in range(3):
            r.tick()
            self.settle_all(r)
        models = {(c["task"], c["resume"]): c["model"] for c in calls(self.calls)}
        self.assertEqual(models[(t1["id"][:8], False)], "opus")
        self.assertEqual(models[(t1["id"][:8], True)], "opus")      # the resumed turn too
        self.assertEqual(models[(t2["id"][:8], False)], "")         # no label: configured args

    def test_model_label_that_is_not_a_name_is_ignored(self):
        t = task(1, labels=["model:--dangerously-skip-permissions"])
        mesh = FakeMesh([t], max_concurrent=1)
        write_replies(self.replies, {t["id"][:8]: "✅ done"})
        r = self.runner(mesh)
        r.tick()
        self.settle_all(r)
        self.assertEqual(calls(self.calls)[0]["model"], "")

    def test_project_lead_map_is_used_when_the_agent_has_no_lead(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        self.cfg.daily_next_budget = 1
        self.cfg.project_leads = {"p1": "lead"}
        write_replies(self.replies, {t["id"][:8]: "↻ NEXT"})
        r = self.runner(mesh)
        # Each real NEXT turn must finish before settling. A shared polling deadline
        # accidentally made this routing test depend on two harness startup times.
        for expected in ("ready", "completed"):
            r.tick()
            self.assertEqual(self.finish_turn(r).state, expected)
        self.assertEqual(r.store.outcomes_today(self.agent.name, t["id"])["next"], 2)
        self.assertEqual(mesh.tasks[t["id"]]["assignee_id"], "agent-lead")

    def test_lead_given_as_an_id_is_named_in_the_hand_off(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        lead_id = "00000042-0000-4000-8000-000000000000"
        mesh.agents = list(mesh.agents) + [{"id": lead_id, "name": "Chief"}]
        self.cfg.daily_next_budget = 1
        self.cfg.project_leads = {"p1": lead_id}
        r = self.runner(mesh)
        leads, _ = r._lead_chain(dict(t))
        self.assertEqual([x["name"] for x in leads], ["Chief"])
        mesh.agents = []
        r2 = self.runner(mesh)
        leads, _ = r2._lead_chain(dict(t))
        self.assertEqual([x["name"] for x in leads], ["00000042"])

    def test_refused_hand_off_is_reported_as_such(self):
        t = task(1, pinned=True)
        mesh = FakeMesh([t], max_concurrent=1)
        self.cfg.daily_next_budget = 1
        self.agent.lead = "lead"
        write_replies(self.replies, {t["id"][:8]: "↻ NEXT"})
        r = self.runner(mesh)
        r.tick()
        self.settle_all(r)
        self.assertEqual(mesh.tasks[t["id"]]["assignee_id"], AGENT_ID)
        bodies = [c["body"] for c in mesh.comment_log[t["id"]]]
        self.assertTrue(any("Could not hand it to lead" in b for b in bodies))
        self.assertFalse(any("completed in" in b for b in bodies))

    def test_own_work_is_not_fed_back_as_a_review(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        write_replies(self.replies, {t["id"][:8]: "SLEEP:1:done"})
        r = self.runner(mesh)
        r.tick()
        mesh.set_category(t["id"], "review")                     # the agent sent it to review, kept itself
        self.settle_all(r)
        self.assertIn("own work", r.hold_reason(dict(mesh.tasks[t["id"]], _mode="review"), "review"))

    def test_one_review_start_per_tick(self):
        revs = [task(n, cat="review") for n in (1, 2, 3)]
        mesh = FakeMesh(revs, max_concurrent=3)
        write_replies(self.replies, {x["id"][:8]: "SLEEP:2:looked" for x in revs})
        r = self.runner(mesh)
        r.tick()
        self.assertEqual(len(r.store.by_state("builder", ("running", "starting"))), 1)


class OwnWorkInReview(Bench):
    """The agent's own work in review waits for someone else, until something it has
    not seen happens: then the next step is the author's and it is fed with the event."""

    def own_work(self, mesh, r, t, during=None, closing="done; sent to review",
                 reply="SLEEP:1:done"):
        write_replies(self.replies, {t["id"][:8]: reply})
        r.tick()                                                   # the work turn starts
        mesh.add_comment(t["id"], closing, author=AGENT_ID)
        if during:
            during()
        mesh.set_category(t["id"], "review")                       # the agent sent it to review
        self.settle_all(r)
        return r.store.by_state("builder", ("completed",))[0]

    def review_hold(self, r, t):
        r._hold_cache.clear()
        return r.hold_reason(dict(r.mesh.tasks[t["id"]], _mode="review"), "review")

    def review_prompts(self):
        return [c["prompt"] for c in calls(self.calls) if "Review #" in c["prompt"]]

    def later(self, seconds=1):
        return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=seconds)).isoformat()

    def test_a_comment_after_the_agents_last_word_counts_although_the_attempt_closed_later(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.runner(mesh)
        # a merge webhook writes as another agent while the turn is still winding down
        self.own_work(mesh, r, t, during=lambda: mesh.add_comment(
            t["id"], "MR !7 merged", author="agent-reviewer",
            at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(milliseconds=300)),
            # The automatic review must end with an outcome. Plain "done" retries
            # no-outcome turns and makes this event/prompt test depend on CI speed.
            reply="SLEEP:1:⏳ WAIT decision:@lead")
        review = [a for a in r.store.by_state("builder", ("waiting",)) if a.mode == "review"]
        self.assertEqual(len(review), 1)
        self.assertEqual(review[0].no_outcome, 0)
        prompts = self.review_prompts()
        self.assertEqual(len(prompts), 1)                          # fed back to its author
        self.assertIn("comes back to you because something changed", prompts[0])
        self.assertIn("MR !7 merged", prompts[0])
        self.assertIn("accept: everything the task asks for", prompts[0])  # someone else spoke on it

    def test_without_news_it_waits_for_someone_else(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.runner(mesh)
        self.own_work(mesh, r, t)
        self.assertIn("own work", self.review_hold(r, t))
        r.tick()
        self.assertEqual(self.review_prompts(), [])

    def test_a_wait_the_task_left_is_not_the_agents_work(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        write_replies(self.replies, {t["id"][:8]: f"⏳ WAIT {FUTURE_AT}: external=fixture-window source=card:#abcdef12 soak window"})
        r = self.runner(mesh)
        r.tick()
        self.settle_all(r)
        mesh.set_category(t["id"], "review")                       # someone else moved it on
        r._last_wake_check.clear()
        r.tick()
        self.settle_all(r)
        closed = next(a for a in r.store.by_state("builder", ("completed", "failed")) if a.mode == "work")
        self.assertTrue(closed.note.startswith("task left the agent while waiting"))
        self.assertEqual(len(self.review_prompts()), 1)            # fed as its reviewer
        self.assertNotIn("You worked on this task yourself", self.review_prompts()[0])
        self.assertNotIn("own work", r._review_hold(mesh.tasks[t["id"]]))

    def test_an_attempt_without_a_turn_is_not_the_agents_work(self):
        t = task(1, cat="review")
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.runner(mesh)
        r.refresh_me()
        a = r.store.create("builder", t["id"], "claude")
        r.store.update(a, state="completed", note="checkout refused")
        self.assertEqual(self.review_hold(r, t), "")

    def test_a_linked_mr_merged_since_releases_it(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.runner(mesh)
        self.own_work(mesh, r, t)
        self.assertIn("own work", self.review_hold(r, t))
        link = {"link_type": "pr", "url": "https://git.example/g/app/-/merge_requests/7", "title": "!7 fix",
                "status": "merged", "status_synced_at": self.later(30), "created_at": "2026-01-01T00:00:00Z"}
        mesh.links = {t["id"]: [link]}
        self.assertIn("own work", self.review_hold(r, t))          # the webhook echo of its own merge
        link["status_synced_at"] = self.later(300)
        self.assertEqual(self.review_hold(r, t), "")
        r.tick()
        self.settle_all(r)
        prompts = self.review_prompts()
        self.assertEqual(len(prompts), 1)
        self.assertIn("MR !7 fix was merged", prompts[0])
        self.assertIn("You must not accept your own work", prompts[0])     # merged, but nobody reviewed it
        self.assertNotIn("accept: everything the task asks for", prompts[0])
        self.assertIn("already reviewed", r._review_hold(mesh.tasks[t["id"]]))   # fed once, then the refeed rule

    def test_a_pipeline_finished_on_gitlab_since_releases_it(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        gl = FakeGitLab()
        r = self.runner(mesh, gitlab=gl)
        self.own_work(mesh, r, t)
        mesh.links = {t["id"]: [{"link_type": "pr", "url": "https://git.example/g/app/-/merge_requests/7",
                                 "status": "open", "created_at": "2026-01-01T00:00:00Z"}]}
        gl.mr_rows["g/app!7"] = {"state": "opened", "sha": "abcdef1234",
                                 "head_pipeline": {"id": 5, "status": "running", "created_at": "2026-01-01T00:00:00Z"}}
        self.assertIn("own work", self.review_hold(r, t))
        gl.mr_rows["g/app!7"]["head_pipeline"].update(status="success", finished_at=self.later())
        self.assertEqual(self.review_hold(r, t), "")
        self.assertEqual(r._review_why[t["id"]]["event"], "MR g/app!7: pipeline 5 success on abcdef12")

    def test_the_agents_own_declared_mr_wait_counts_without_a_link(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        gl = FakeGitLab()
        r = self.runner(mesh, gitlab=gl)
        self.own_work(mesh, r, t, closing="merge after CI\n⏳ WAIT mr:g/app!9")
        gl.mr_rows["g/app!9"] = {"state": "opened", "sha": "1234"}
        self.assertIn("own work", self.review_hold(r, t))
        gl.mr_rows["g/app!9"] = {"state": "merged", "merged_at": self.later(30), "sha": "1234"}
        self.assertIn("own work", self.review_hold(r, t))           # the echo of its own merge
        gl.mr_rows["g/app!9"]["merged_at"] = self.later(300)
        self.assertEqual(self.review_hold(r, t), "")
        self.assertEqual(r._review_why[t["id"]]["event"], "MR g/app!9 was merged")

    def test_the_agents_own_declared_card_wait_over_releases_it(self):
        other = task(2, cat="in_progress", assignee_id="someone-else")
        t = task(1)
        mesh = FakeMesh([t, other], max_concurrent=1)
        r = self.runner(mesh)
        self.own_work(mesh, r, t, closing=f"⏳ WAIT card:#{other['id'][:8]}")
        self.assertIn("own work", self.review_hold(r, t))
        mesh.set_category(other["id"], "done")
        self.assertEqual(self.review_hold(r, t), "")
        self.assertIn("is over: task", r._review_why[t["id"]]["event"])

    def test_a_verdict_the_author_posted_itself_feeds_it_back_to_act_on(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        gl = FakeGitLab()
        r = self.runner(mesh, gitlab=gl)
        mesh.links = {t["id"]: [{"link_type": "pr", "url": "https://git.example/g/app/-/merge_requests/7",
                                 "status": "open", "created_at": "2026-01-01T00:00:00Z"}]}
        gl.mr_rows["g/app!7"] = {"state": "opened", "sha": "1a2b3c4d5e6f"}
        self.own_work(mesh, r, t, closing="checked by the reviewer\nVerify: SHIP @1a2b3c4")
        prompts = self.review_prompts()
        self.assertEqual(len(prompts), 1)                          # fed to merge its own MR
        self.assertIn("review verdict SHIP by agent-1: Verify: SHIP @1a2b3c4", prompts[0])
        self.assertIn("accept: everything the task asks for", prompts[0])  # SHIP: merge, then accept

    def test_a_verdict_on_an_older_commit_or_with_the_matcher_off_does_not_count(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        gl = FakeGitLab()
        r = self.runner(mesh, gitlab=gl)
        mesh.links = {t["id"]: [{"link_type": "pr", "url": "https://git.example/g/app/-/merge_requests/7",
                                 "status": "open", "created_at": "2026-01-01T00:00:00Z"}]}
        gl.mr_rows["g/app!7"] = {"state": "opened", "sha": "ffff000011112222"}
        self.agent.review_own_verdict_pattern = ""                  # off while the turn runs
        self.own_work(mesh, r, t, closing="Verdict: DO-NOT-SHIP on 1a2b3c4d")
        self.assertIn("own work", self.review_hold(r, t))
        self.agent.review_own_verdict_pattern = None                # the default matcher
        self.assertIn("own work", self.review_hold(r, t))           # not the current head
        gl.mr_rows["g/app!7"]["sha"] = "1a2b3c4d9999"
        self.assertEqual(self.review_hold(r, t), "")
        self.assertIn("review verdict DO-NOT-SHIP", r._review_why[t["id"]]["event"])
        prompt = r._first_prompt(mesh.tasks[t["id"]], "review", [], r._review_why[t["id"]])
        self.assertIn("move it back to\n    in progress and fix it", prompt)
        self.assertNotIn("accept: everything the task asks for", prompt)
        self.agent.review_own_verdict_pattern = r"^LGTM (?P<verdict>ok)"     # an operator's own format
        self.assertIn("own work", self.review_hold(r, t))

    def test_requests_and_prose_are_not_verdicts(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.runner(mesh)
        self.own_work(mesh, r, t, closing="Plan: once I see Verify: SHIP @1a2b3c4 I merge it")
        self.assertIn("own work", self.review_hold(r, t))           # the author's own: not a verdict line
        for body in ("Review requested: reply SHIP or DO-NOT-SHIP on the head",
                     "review: please answer SHIP/DO-NOT-SHIP", "Review note: do not ship before CI"):
            mesh.add_comment(t["id"], body, author=AGENT_ID, at=dt.datetime.now(dt.timezone.utc))
            self.assertIn("own work", self.review_hold(r, t), body)
        self.agent.review_own_verdict_pattern = ""
        self.agent.review_verdict_pattern = r"^\W*(?:review)\b[^\n]*?\b(?P<verdict>(?-i:SHIP|DO-NOT-SHIP))\b"
        own_comments = list(mesh.comment_log[t["id"]])
        for c in own_comments:                                      # the same lines by someone else
            c["author_id"] = "agent-reviewer"
        mesh.comment_log[t["id"]] = [c for c in own_comments if not c["body"].startswith("Plan:")]
        mesh.comment_log[t["id"]].insert(0, {"author_id": AGENT_ID, "body": "sent to review",
                                             "created_at": own_comments[0]["created_at"]})
        r._hold_cache.clear()
        verdict = r._verdict_event(mesh.comment_log[t["id"]], dt.datetime(2000, 1, 1, tzinfo=dt.timezone.utc),
                                   lambda: [])
        self.assertEqual(verdict, "")
        mesh.add_comment(t["id"], "Review done: verdict SHIP", author="agent-reviewer",
                         at=dt.datetime.now(dt.timezone.utc))
        verdict = r._verdict_event(mesh.comment_log[t["id"]], dt.datetime(2000, 1, 1, tzinfo=dt.timezone.utc),
                                   lambda: [])
        self.assertIn("review verdict SHIP by agent-reviewer", verdict)

    def test_a_question_to_a_human_is_not_answered_by_a_missing_gate(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.runner(mesh)
        self.own_work(mesh, r, t, closing="❓ Blocking @owner: which database?")
        self.assertIn("own work", self.review_hold(r, t))           # no gate set on this path: still asked
        mesh.tasks[t["id"]]["human_gate"] = True
        self.assertEqual(self.review_hold(r, t), "waiting for a human decision")
        mesh.tasks[t["id"]]["human_gate"] = False                   # lifted: seen set -> cleared
        self.assertEqual(self.review_hold(r, t), "")
        self.assertIn("gate on your question was lifted", r._review_why[t["id"]]["event"])

    def test_a_question_to_a_human_ends_with_an_answer(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.runner(mesh)
        self.own_work(mesh, r, t, closing="❓ Blocking @owner: which database?")
        mesh.links = {t["id"]: [{"link_type": "commit", "title": "x", "created_at": self.later(600)}]}
        self.assertIn("own work", self.review_hold(r, t))           # code news do not answer a question
        mesh.add_comment(t["id"], "use postgres", author="owner",
                         at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=1))
        self.assertEqual(self.review_hold(r, t), "")
        self.assertIn("owner commented: use postgres", r._review_why[t["id"]]["event"])

    def test_unreviewed_own_work_is_routed_on_never_accepted(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.runner(mesh)
        a = self.own_work(mesh, r, t)
        old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=5)).isoformat()
        r.store.db.execute("UPDATE attempts SET created_at=?, updated_at=? WHERE id=?", (old, old, a.id))
        for c in mesh.comment_log[t["id"]]:
            c["created_at"] = old
        r._hold_cache.clear()
        # Exercise one completed review turn, not retries of a reply with no
        # outcome under settle_all's shared wall-clock deadline.
        write_replies(self.replies, {t["id"][:8]: "⏳ WAIT decision:@lead"})
        r.tick()
        review = self.finish_turn(r)
        self.assertEqual((review.mode, review.state, review.no_outcome), ("review", "waiting", 0))
        prompts = self.review_prompts()
        self.assertEqual(len(prompts), 1)
        self.assertIn("nobody else has reviewed it\nyet (5 h since your last turn on it)", prompts[0])
        self.assertIn("You must not accept your own work: do not move it to done.", prompts[0])
        self.assertNotIn("accept: everything the task asks for", prompts[0])
        self.assertTrue(r._review_hold(mesh.tasks[t["id"]]))     # fed once, then the refeed rule

    def test_own_work_with_a_pending_declared_wait_is_not_fed_as_a_review(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.runner(mesh)
        until = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=40)).strftime("%Y-%m-%dT%H:%MZ")
        a = self.own_work(mesh, r, t, closing=f"merged, watching the window\n\n⏳ WAIT {until}: external=fixture-window source=card:#abcdef12 48h window")
        old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=5)).isoformat()
        r.store.db.execute("UPDATE attempts SET created_at=?, updated_at=? WHERE id=?", (old, old, a.id))
        for c in mesh.comment_log[t["id"]]:
            c["created_at"] = old
        self.assertEqual(self.review_hold(r, t), "review: the agent's own declared wait is not over")

    def test_a_pending_pipeline_wait_hold_ends_after_the_refeed_window(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.runner(mesh)
        a = self.own_work(mesh, r, t, closing="merged\n\n⏳ WAIT pipeline:grp/proj#123")
        ago = lambda h: (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=h)).isoformat()  # noqa: E731
        set_age = lambda h: ([r.store.db.execute("UPDATE attempts SET created_at=?, updated_at=? WHERE id=?", (ago(h), ago(h), a.id))]  # noqa: E731
                             + [c.update(created_at=ago(h)) for c in mesh.comment_log[t["id"]]])
        set_age(3)
        self.assertEqual(self.review_hold(r, t), "review: the agent's own declared wait is not over")
        set_age(5)
        self.assertEqual(self.review_hold(r, t), "")                # past review_refeed_hours it is routed on

    def test_a_day_later_unreviewed_own_work_still_gets_the_routing_prompt(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.runner(mesh)
        a = self.own_work(mesh, r, t)
        ago = lambda h: (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=h)).isoformat()  # noqa: E731
        r.store.db.execute("UPDATE attempts SET created_at=?, updated_at=? WHERE id=?", (ago(30), ago(30), a.id))
        for c in mesh.comment_log[t["id"]]:
            c["created_at"] = ago(30)
        r.store.mark("builder", t["id"], "review_fed", "earlier-feed")
        r.store.db.execute("UPDATE marks SET at=? WHERE task_id=? AND key='review_fed'", (ago(26), t["id"]))
        self.assertEqual(self.review_hold(r, t), "")
        why = r._review_why[t["id"]]
        self.assertEqual(why["unreviewed_hours"], 30)
        prompt = r._first_prompt(mesh.tasks[t["id"]], "review", [], why)
        self.assertIn("You must not accept your own work", prompt)
        self.assertNotIn("accept: everything the task asks for", prompt)
        mesh.add_comment(t["id"], "looked at it: fine", author="agent-reviewer",
                         at=dt.datetime.now(dt.timezone.utc))
        self.assertEqual(self.review_hold(r, t), "")
        self.assertNotIn(t["id"], r._review_why)                     # someone else engaged: a plain review
        mesh.comment_log[t["id"]].pop()
        mesh.add_comment(t["id"], "Verify: SHIP", author=AGENT_ID, at=dt.datetime.now(dt.timezone.utc))
        self.assertEqual(self.review_hold(r, t), "")
        self.assertNotIn(t["id"], r._review_why)                     # a SHIP verdict since: may accept

    def test_one_merge_request_outside_the_token_does_not_switch_gitlab_off(self):
        import urllib.error
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        gl = FakeGitLab()
        rows = {"g/app!7": None, "g/app!8": {"state": "opened", "sha": "1"}}
        reads = []

        def get_mr(target):
            reads.append(target)
            if rows[target] is None:
                raise urllib.error.HTTPError(target, 404, "Not Found", {}, None)
            return dict(rows[target])
        gl.get_mr = get_mr
        r = self.runner(mesh, gitlab=gl)
        mesh.links = {t["id"]: [{"link_type": "pr", "url": f"https://git.example/g/app/-/merge_requests/{n}",
                                 "status": "open", "created_at": "2026-01-01T00:00:00Z"} for n in (7, 8)]}
        self.own_work(mesh, r, t)
        self.assertIn("own work", self.review_hold(r, t))
        rows["g/app!8"] = {"state": "merged", "merged_at": self.later(300), "sha": "1"}
        reads.clear()
        self.assertEqual(self.review_hold(r, t), "")
        self.assertEqual(r._review_why[t["id"]]["event"], "MR g/app!8 was merged")
        self.assertEqual(r._gitlab_down_until, 0.0)                 # GitLab is not down
        self.assertEqual(reads, ["g/app!8"])                         # the unreadable one is not asked again

    def test_gitlab_is_read_once_per_evaluation_and_skipped_while_down(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        gl = FakeGitLab()
        reads = []
        gl.get_mr = lambda target: reads.append(target) or {"state": "opened", "sha": "1a2b3c4d"}
        r = self.runner(mesh, gitlab=gl)
        mesh.links = {t["id"]: [{"link_type": "pr", "url": "https://git.example/g/app/-/merge_requests/7",
                                 "status": "open", "created_at": "2026-01-01T00:00:00Z"}]}
        self.own_work(mesh, r, t, closing="sent\nVerify: SHIP @deadbee")
        reads.clear()
        self.assertIn("own work", self.review_hold(r, t))           # heads() and changed_since share a read
        self.assertEqual(reads, ["g/app!7"])

        def down(target):
            reads.append(target)
            raise OSError("timed out")
        gl.get_mr = down
        reads.clear()
        for _ in range(3):
            self.assertIn("own work", self.review_hold(r, t))
        self.assertEqual(reads, ["g/app!7"])                       # one timeout, then skipped

    def test_the_hold_window_is_configurable(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.runner(mesh)
        a = self.own_work(mesh, r, t)
        self.assertEqual(self.cfg.review_own_hold_hours, 1.0)
        old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=30)).isoformat()
        r.store.db.execute("UPDATE attempts SET created_at=?, updated_at=? WHERE id=?", (old, old, a.id))
        for c in mesh.comment_log[t["id"]]:
            c["created_at"] = old
        self.assertIn("own work", self.review_hold(r, t))           # 30 min < 1 h
        self.cfg.review_own_hold_hours = 0.25
        self.assertEqual(self.review_hold(r, t), "")
        self.cfg.review_own_hold_hours = 1
        self.agent.review_own_hold_hours = 0                        # per agent: no wait, routed at once
        self.assertEqual(self.review_hold(r, t), "")
        self.assertEqual(r._review_why[t["id"]]["unreviewed_hours"], 0)    # whole hours since its own work

    def test_review_own_hold_hours_is_read_from_the_config_file(self):
        from mesh_runner.config import load
        key = self.root / "k.key"
        key.write_text("secret")
        key.chmod(0o600)
        cfg = self.root / "runner.toml"
        cfg.write_text(f"""[runner]
mesh_url = "http://mesh"
state_dir = "{self.root / 's'}"
review_own_hold_hours = 6

[[agents]]
name = "a"
key_file = "{key}"
profile_dir = "{self.root}"

[[agents]]
name = "b"
key_file = "{key}"
profile_dir = "{self.root}"
review_own_hold_hours = 0.5
""")
        loaded = load(cfg)
        self.assertEqual(loaded.review_own_hold_hours, 6.0)
        self.assertEqual([a.review_own_hold_hours for a in loaded.agents], [None, 0.5])
        self.assertIn("SHIP", loaded.review_verdict_pattern)
        self.assertEqual([a.review_verdict_pattern for a in loaded.agents], [None, None])
        cfg.write_text(cfg.read_text().replace("review_own_hold_hours = 0.5", 'review_verdict_pattern = "(bad"'))
        from mesh_runner.config import ConfigError
        with self.assertRaises(ConfigError):
            load(cfg)


class SkipStates(Bench):
    def test_a_blocker_in_backlog_is_named_as_such(self):
        from mesh_runner.effects import skip_state
        blocker = task(9, cat="backlog", assignee_id="someone-else")
        live = task(8, cat="in_progress", assignee_id="someone-else")
        t = task(1)
        mesh = FakeMesh([blocker, live, t], max_concurrent=1)
        mesh.deps[t["id"]] = [{"dependency_type": "blocks", "depends_on_task_id": live["id"]},
                              {"dependency_type": "blocks", "depends_on_task_id": blocker["id"]}]
        r = self.runner(mesh)
        reason = r.hold_reason(dict(t, _mode="work"), "work")
        self.assertEqual(reason, f"blocked by a card in backlog: {blocker['id'][:8]}, {live['id'][:8]}")
        self.assertEqual(skip_state(reason, "b"),
                         {"kind": "waiting", "ref": f"card:#{blocker['id'][:8]}", "reason_code": "blocker_in_backlog",
                          "reason": "blocked by a card in backlog: nobody picks it up"})
        mesh.set_category(blocker["id"], "todo")
        r._hold_cache.clear()
        self.assertEqual(r.hold_reason(dict(t, _mode="work"), "work"),
                         f"blocked by {live['id'][:8]}, {blocker['id'][:8]}")

    def test_a_reason_code_the_server_does_not_know_goes_out_as_text_once_refused(self):
        from mesh_runner.effects import Effects
        mesh = FakeMesh([], max_concurrent=1)
        mesh.known_codes = {"human_gate"}
        fx = Effects(mesh, None, "b")
        state = {"kind": "waiting", "ref": "card:#abcdef12", "reason_code": "blocker_in_backlog",
                 "reason": "blocked by a card in backlog: nobody picks it up"}
        fx.publish_skips({"t1": state})
        self.assertEqual(mesh.runtime["t1"], {k: v for k, v in state.items() if k != "reason_code"})
        fx._published["t1"] = (state, fx._published["t1"][1] - dt.timedelta(minutes=10))   # heartbeat due
        fx.publish_skips({"t1": state})
        self.assertEqual(mesh.refused_puts, 1)                     # learnt once, not refused every time
        mesh.known_codes = None
        fx2 = Effects(mesh, None, "b")
        fx2.publish_skips({"t2": state})
        self.assertEqual(mesh.runtime["t2"]["reason_code"], "blocker_in_backlog")

    def test_clears_that_keep_failing_do_not_starve_new_states(self):
        from mesh_runner.effects import Effects
        mesh = FakeMesh([], max_concurrent=1)
        fx = Effects(mesh, None, "b")
        old = {"kind": "stopped", "reason": "x"}
        ids = [f"t{n:02d}" for n in range(25)]
        fx.publish_skips({i: old for i in ids}, budget=25)
        mesh.broken_tasks = set(ids)                               # every read answers 500
        tried = []
        task = mesh.task
        mesh.task = lambda tid: tried.append(tid) or task(tid)
        new = {"kind": "queued", "lane": "b", "ref": "position:1"}
        fx.publish_skips({"fresh": new})
        self.assertEqual(mesh.runtime.get("fresh"), new)           # published in the same pass
        self.assertEqual(len(tried), 10)
        fx.publish_skips({"fresh": new})
        self.assertEqual(len(set(tried)), 20)                      # the next pass tries the next ones
        for _ in range(6):
            fx.publish_skips({"fresh": new})
        self.assertEqual(set(fx._skipped), {"fresh"})              # given up after 3 failures each

    def test_the_first_sweep_survives_a_restart(self):
        t1, t2 = task(1, cat="done"), task(2, cat="done")
        mesh = FakeMesh([t1, t2], max_concurrent=1)
        mesh.shows_runtime = True
        stale = {"kind": "stopped", "reason": "waits for someone else to review it"}
        mesh.runtime = {t1["id"]: dict(stale), t2["id"]: dict(stale)}
        r = self.runner(mesh)
        for tid in (t1["id"], t2["id"]):
            a = r.store.create("builder", tid, "claude")
            r.store.update(a, state="completed", turns=1)
        r.refresh_me()
        r.effects.publish_skips({}, budget=1)                      # the sweep stops after one task
        self.assertEqual(len(mesh.runtime), 1)
        r2 = self.runner(mesh)                                     # restart: the sweep goes on
        r2.refresh_me()
        r2.publish_skips()
        self.assertEqual(mesh.runtime, {})

    def test_a_restarted_runner_clears_what_it_published_before(self):
        t, t2 = task(1, delegation_level="supervised"), task(2, delegation_level="supervised")
        mesh = FakeMesh([t, t2], max_concurrent=1)
        r = self.runner(mesh)
        r.refresh_me()
        r.publish_skips()
        self.assertEqual(mesh.runtime[t["id"]]["reason_code"], "supervised_todo")
        mesh.set_category(t["id"], "done")
        mesh.set_category(t2["id"], "backlog")
        r2 = self.runner(mesh)                                     # restart: nothing in memory
        r2.refresh_me()
        r2.publish_skips()
        self.assertNotIn(t["id"], mesh.runtime)
        self.assertNotIn(t2["id"], mesh.runtime)
        self.assertEqual(r2.store.marked("builder", "skip_state"), {})
        r2._skips_due = 0
        r2.publish_skips()                                         # idempotent: nothing left to do
        self.assertEqual(mesh.runtime, {})

    def test_a_state_someone_else_wrote_since_is_left_alone(self):
        t = task(1, delegation_level="supervised")
        mesh = FakeMesh([t], max_concurrent=1)
        mesh.shows_runtime = True
        r = self.runner(mesh)
        r.refresh_me()
        r.publish_skips()
        mesh.tasks[t["id"]]["assignee_id"] = "agent-lead"          # handed over; the new owner reports
        mesh.runtime[t["id"]] = {"kind": "waiting", "ref": "mr:g/app!3"}
        r2 = self.runner(mesh)
        r2.refresh_me()
        r2.publish_skips()
        self.assertEqual(mesh.runtime[t["id"]], {"kind": "waiting", "ref": "mr:g/app!3"})
        self.assertEqual(r2.store.marked("builder", "skip_state"), {})

    def test_states_an_older_run_left_are_cleared_once_where_no_other_agent_owns_the_task(self):
        done, theirs = task(1, cat="done"), task(2, assignee_id="agent-lead")
        mesh = FakeMesh([done, theirs], max_concurrent=1)
        mesh.shows_runtime = True
        stale = {"kind": "stopped", "reason": "waits for someone else to review it"}
        mesh.runtime = {done["id"]: dict(stale), theirs["id"]: dict(stale)}
        r = self.runner(mesh)
        for tid in (done["id"], theirs["id"]):                     # an older runner worked on both
            a = r.store.create("builder", tid, "claude")
            r.store.update(a, state="completed", turns=1)
        r.refresh_me()
        r.publish_skips()
        self.assertNotIn(done["id"], mesh.runtime)
        self.assertEqual(mesh.runtime[theirs["id"]], stale)        # another agent's task: not guessed
        mesh.runtime[done["id"]] = dict(stale)
        r2 = self.runner(mesh)
        r2.refresh_me()
        r2.publish_skips()
        self.assertEqual(mesh.runtime[done["id"]], stale)          # the sweep runs once per store


class HandOffs(Bench):
    """A task handed up must not circle: a capped lead is skipped, a task that came back
    stays, a lead the hand-off cannot reach is said so."""

    def lead_agent(self, name, lead=""):
        self.cfg.agents.append(AgentConfig(name=name, key="k", harness="claude", profile_dir=self.root,
                                           repos=[], lead=lead))

    def started(self, r, agent, t, n):
        for _ in range(n):
            a = r.store.create(agent, t["id"], "claude")
            r.store.update(a, state="completed", turns=1, note="task left the agent's working statuses")

    def bodies(self, mesh, t):
        return [c["body"] for c in mesh.comment_log.get(t["id"], [])]

    def test_a_lead_that_hit_the_cap_itself_is_skipped_for_the_next_one_up(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        self.cfg.daily_start_cap = 2
        self.agent.lead = "lead"
        self.lead_agent("lead", lead="reviewer")
        r = self.runner(mesh)
        r.refresh_me()
        self.started(r, "lead", t, 2)
        r._start_cap(dict(mesh.tasks[t["id"]]), 2)
        self.assertEqual(mesh.tasks[t["id"]]["assignee_id"], "agent-reviewer")
        self.assertIn("lead already started it 2 times today", self.bodies(mesh, t)[-1])

    def test_with_every_lead_capped_it_stays_until_tomorrow_said_once(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        self.cfg.daily_start_cap = 2
        self.agent.lead = "lead"
        r = self.runner(mesh)
        r.refresh_me()
        self.started(r, "lead", t, 2)
        for _ in range(2):
            r._start_cap(dict(mesh.tasks[t["id"]]), 2)
        self.assertEqual(mesh.tasks[t["id"]]["assignee_id"], AGENT_ID)
        self.assertEqual(len(self.bodies(mesh, t)), 1)
        self.assertIn("Not started again before tomorrow", self.bodies(mesh, t)[0])
        self.assertGreater(r._task_backoff[t["id"]], time.monotonic() + 60)

    def test_a_task_handed_up_today_that_came_back_stays(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        self.cfg.daily_start_cap = 2
        self.agent.lead = "lead"
        r = self.runner(mesh)
        r.refresh_me()
        r._start_cap(dict(mesh.tasks[t["id"]]), 2)
        self.assertEqual(mesh.tasks[t["id"]]["assignee_id"], "agent-lead")
        mesh.tasks[t["id"]]["assignee_id"] = AGENT_ID                # a rule hands it back
        for _ in range(2):
            r._start_cap(dict(mesh.tasks[t["id"]]), 2)
        self.assertEqual(mesh.tasks[t["id"]]["assignee_id"], AGENT_ID)
        bodies = self.bodies(mesh, t)
        self.assertEqual(len(bodies), 2)                              # handed once, kept once
        self.assertIn("handed up once today already and came back", bodies[1])

    def test_a_task_that_came_back_goes_to_a_lead_with_a_free_slot(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        self.cfg.daily_start_cap = 2
        self.agent.lead = "lead"
        self.lead_agent("lead")
        r = self.runner(mesh)
        r.refresh_me()
        r._start_cap(dict(mesh.tasks[t["id"]]), 2)
        self.assertEqual(mesh.tasks[t["id"]]["assignee_id"], "agent-lead")
        mesh.tasks[t["id"]]["assignee_id"] = AGENT_ID                # the lead hands it back
        r._start_cap(dict(mesh.tasks[t["id"]]), 2)
        self.assertEqual(mesh.tasks[t["id"]]["assignee_id"], "agent-lead")
        self.assertIn("came back once today; lead has a free slot", self.bodies(mesh, t)[-1])
        self.assertNotIn(t["id"], r._task_backoff)

    def test_a_task_that_came_back_stays_when_the_lead_is_busy(self):
        t, other = task(1), task(2)
        mesh = FakeMesh([t, other], max_concurrent=1)
        self.cfg.daily_start_cap = 2
        self.agent.lead = "lead"
        self.lead_agent("lead")
        r = self.runner(mesh)
        r.refresh_me()
        r._start_cap(dict(mesh.tasks[t["id"]]), 2)
        mesh.tasks[t["id"]]["assignee_id"] = AGENT_ID
        busy = r.store.create("lead", other["id"], "claude")
        r.store.update(busy, state="running")
        r._start_cap(dict(mesh.tasks[t["id"]]), 2)
        self.assertEqual(mesh.tasks[t["id"]]["assignee_id"], AGENT_ID)
        body = self.bodies(mesh, t)[-1]
        self.assertIn("handed up once today already and came back", body)
        self.assertIn("lead has no free slot", body)

    def test_a_turn_cap_hand_up_that_came_back_is_not_handed_up_again(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        self.cfg.daily_next_budget = 1
        self.agent.lead = "lead"
        write_replies(self.replies, {t["id"][:8]: "↻ NEXT"})
        r = self.runner(mesh)
        r.tick()
        self.settle_all(r)
        self.assertEqual(mesh.tasks[t["id"]]["assignee_id"], "agent-lead")
        mesh.tasks[t["id"]]["assignee_id"] = AGENT_ID
        mesh.set_category(t["id"], "todo")
        r._task_backoff.clear()
        r.tick()
        self.settle_all(r)
        self.assertEqual(mesh.tasks[t["id"]]["assignee_id"], AGENT_ID)
        caps = [b for b in self.bodies(mesh, t) if "TURN CAP" in b]
        self.assertEqual(len(caps), 2)
        self.assertIn("came back", caps[1])
        self.assertIn(t["id"], r._task_backoff)

    def test_each_kind_of_kept_hand_off_is_said_once_a_day(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.runner(mesh)
        for _ in range(2):
            r._keep_until_tomorrow(t["id"], "🔁 START CAP: x.")
            r._keep_until_tomorrow(t["id"], "🔁 TURN CAP: y.", "turn budget")
        self.assertEqual([b.split(":")[0] for b in self.bodies(mesh, t)], ["[runner] 🔁 START CAP", "[runner] 🔁 TURN CAP"])

    def test_a_lead_outside_the_workspace_is_named_and_the_task_kept(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        self.cfg.daily_next_budget = 1
        self.agent.lead = "faraway"
        write_replies(self.replies, {t["id"][:8]: "↻ NEXT"})
        r = self.runner(mesh)
        with self.assertLogs("mesh_runner", "WARNING") as logs:
            r.tick()
            self.settle_all(r)
        self.assertTrue(any("lead faraway is not in this workspace" in l for l in logs.output))
        self.assertEqual(mesh.tasks[t["id"]]["assignee_id"], AGENT_ID)
        cap = next(b for b in self.bodies(mesh, t) if "TURN CAP" in b)
        self.assertIn("lead faraway is not in this workspace", cap)
        self.assertIn("Not started again before tomorrow", cap)


class Holds(Bench):
    def test_an_unreadable_blocker_shows_why_the_task_waits(self):
        from mesh_runner import selection
        others = list(selection._READERS)
        selection._READERS.clear()                       # no other agent of this process can read it
        try:
            blocker, t = task(9, assignee_id="someone-else"), task(1)
            mesh = FakeMesh([blocker, t], max_concurrent=1)
            mesh.forbidden_tasks = {blocker["id"]}
            mesh.deps[t["id"]] = [{"dependency_type": "blocks", "depends_on_task_id": blocker["id"]}]
            r = self.runner(mesh)
            r.refresh_me()
            self.assertEqual(r.hold_reason(dict(t, _mode="work"), "work"), "dependencies unreadable (403)")
            r.publish_skips()
            self.assertEqual(mesh.runtime[t["id"]], {"kind": "waiting", "reason_code": "dependencies_unreadable",
                                                     "reason": "blocker card is not readable by this agent"})
        finally:
            for x in others:
                selection._READERS.add(x)

    def test_an_unreadable_blocker_is_not_asked_of_every_agent_every_pass(self):
        from mesh_runner import selection
        from mesh_runner.selection import StatusCategories
        saved = (list(selection._READERS), dict(selection._NOBODY))
        selection._READERS.clear()
        selection._NOBODY.clear()
        selection._READ_BY.clear()
        try:
            asked = []

            class Stranger:
                def task(self, tid):
                    asked.append(tid)
                    raise MeshError(403, "forbidden")

                def statuses(self, pid):
                    asked.append(pid)
                    raise MeshError(403, "forbidden")
            others = [StatusCategories(Stranger()) for _ in range(56)]
            blocker, t = task(9, assignee_id="someone-else"), task(1)
            mesh = FakeMesh([blocker, t], max_concurrent=1)
            mesh.forbidden_tasks = {blocker["id"]}
            mesh.deps[t["id"]] = [{"dependency_type": "blocks", "depends_on_task_id": blocker["id"]}]
            deps = []
            read_deps = mesh.dependencies
            mesh.dependencies = lambda tid: deps.append(tid) or read_deps(tid)
            r = self.runner(mesh)
            r.refresh_me()
            for _ in range(5):
                self.assertEqual(r.hold_reason(dict(t, _mode="work"), "work"), "dependencies unreadable (403)")
            self.assertEqual(len(deps), 1)                         # the verdict is cached
            r._hold_cache.clear()
            for _ in range(5):
                r.hold_reason(dict(t, _mode="work"), "work")
                r._hold_cache.clear()
            self.assertEqual(len(asked), 56)                       # nobody could: not asked again for a while

            class Member(Stranger):
                def task(self, tid):
                    asked.append(tid)
                    return {"id": tid, "project_id": "p1", "status_id": "s-done"}
            helper = StatusCategories(Member())                    # a new agent: asked first from now on
            asked.clear()
            for _ in range(3):
                self.assertEqual(r.hold_reason(dict(t, _mode="work"), "work"), "")
            self.assertLessEqual(len(asked), 57 + 2)               # one search, then straight to the reader
            del others, helper
        finally:
            selection._READERS.clear()
            for x in saved[0]:
                selection._READERS.add(x)
            selection._NOBODY.update(saved[1])

    def test_a_new_hold_reason_is_logged_once(self):
        t = task(1, delegation_level="supervised")
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.runner(mesh)
        r.refresh_me()
        with self.assertLogs("mesh_runner", "INFO") as logs:
            for _ in range(3):
                r.hold_reason(dict(t, _mode="work"), "work")
            r.plan()                                     # the dry run logs nothing of its own
            mesh.tasks[t["id"]]["labels"] = ["park:later"]
            r.hold_reason(dict(mesh.tasks[t["id"]], _mode="work"), "work")
        held = [l for l in logs.output if " held: " in l]
        self.assertEqual(held, [f"INFO:mesh_runner:builder: #{t['id'][:8]} held: supervised task",
                                f"INFO:mesh_runner:builder: #{t['id'][:8]} held: label park:later"])


class Limits(Bench):
    def test_limit_switch_command_retries_at_once_instead_of_pausing(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        flag = self.root / "switched"
        self.agent.on_limit_command = [sys.executable, "-c", f"open({str(flag)!r},'w').write('x'); print('to acct-2')"]
        write_replies(self.replies, {t["id"][:8]: "ERROR:usage limit reached", t["id"][:8] + ":resume": f"⏳ WAIT {FUTURE_AT}: external=fixture-window source=card:#abcdef12 soak window"})
        r = self.runner(mesh)
        r.tick()
        self.settle_all(r)
        self.assertTrue(flag.exists())
        self.assertEqual(r._starts_paused_until, 0.0)
        self.assertEqual(self.states(r)[t["id"][:8]], "waiting")       # resumed on the new account

    def test_failed_switch_pauses_starts(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        self.agent.on_limit_command = [sys.executable, "-c", "import sys; print('no capacity'); sys.exit(1)"]
        write_replies(self.replies, {t["id"][:8]: "ERROR:usage limit reached"})
        r = self.runner(mesh)
        r.tick()
        self.settle_all(r)
        self.assertGreater(r._starts_paused_until, 0)


class Plan(Bench):
    def test_plan_reads_only(self):
        blocker, t1, t2 = task(1, cat="in_progress"), task(2, "high"), task(3, human_gate=True)
        blocker["assignee_id"] = "x"
        mesh = FakeMesh([blocker, t1, t2], max_concurrent=2)
        r = self.runner(mesh)
        p = r.plan()
        self.assertEqual(p["cap"], 2)
        self.assertEqual({x["task"]: x["decision"] for x in p["tasks"]},
                         {t1["id"][:8]: "eligible", t2["id"][:8]: "skip: waiting for a human decision"})
        self.assertEqual(p["would_start"], 1)
        self.assertEqual((mesh.checkouts, calls(self.calls)), ({}, []))
        self.assertEqual(self.states(r), {})


class CappedEventObservation(Bench):
    def unresolved_card(self):
        blocker = task(2, assignee_id="someone-else")
        self.cfg.daily_wait_budget = 0
        self.cfg.daily_next_budget = 0
        self.cfg.daily_start_cap = 20
        self.t = task(1)
        self.mesh, self.gl = FakeMesh([self.t, blocker], max_concurrent=1), FakeGitLab()
        write_replies(self.replies, {self.t["id"][:8]: "⏳ WAIT card:#00000002"})
        self.r = self.runner(self.mesh, self.gl)
        self.r.tick()
        self.settle_all(self.r)
        a = self.r.store.by_state("builder", ("failed",))[0]
        self.assertIsNotNone(self.r._cap_observation(self.t["id"]))
        self.assertFalse(a.wait.get("cap_wake"))
        return a

    def test_unresolved_card_observation_survives_unrelated_foreign_comment(self):
        a = self.unresolved_card()
        self.mesh.add_comment(self.t["id"], "The dashboard link has been updated.",
                              author="agent-lead", at=dt.datetime.fromisoformat(a.updated_at) + dt.timedelta(seconds=1))
        self.r.tick()
        self.settle_all(self.r)
        self.assertEqual(len(calls(self.calls)), 1)
        self.assertIn("observed", self.r.hold_reason(self.mesh.tasks[self.t["id"]], "work"))

    def test_unresolved_card_observation_survives_utc_rollover_and_restart(self):
        from unittest.mock import patch
        a = self.unresolved_card()
        # A restarted daemon has no in-memory budget deadline. Both clocks agree
        # on the new UTC day; the persisted observation must still hold admission.
        self.r.store.db.close()
        self.r = self.runner(self.mesh, self.gl)
        tomorrow = dt.datetime.fromisoformat(a.updated_at) + dt.timedelta(days=1)
        with patch("mesh_runner.runner.now", return_value=tomorrow), patch("mesh_runner.store.now", return_value=tomorrow):
            self.r.tick()
            self.settle_all(self.r)
            self.assertEqual(len(calls(self.calls)), 1)
            self.assertIn("observed", self.r.hold_reason(self.mesh.tasks[self.t["id"]], "work"))

    def override_comment(self, a, author="agent-lead", attempt=None, at=None):
        self.cfg.project_leads["p1"] = "lead"
        c = self.mesh.add_comment(self.t["id"], "Use the alternate dataset; the original is no longer required.",
                                  author=author, at=at)
        c["metadata"] = {"runner_cap_override": {"attempt_id": attempt or a.id,
                                                 "action": "instructions_changed"}}
        return c

    def test_addressed_lead_instruction_persists_receipt_and_survives_restart(self):
        a = self.unresolved_card()
        c = self.override_comment(a)
        self.r.check_wakes()
        saved = self.r.store.get(a.id)
        self.assertEqual(saved.wait["cap_override"], {"attempt_id": a.id, "comment_id": c["id"],
                                                      "author_id": "agent-lead", "body": c["body"]})
        self.r.store.db.close()
        self.r = self.runner(self.mesh, self.gl)
        write_replies(self.replies, {self.t["id"][:8]: "↻ NEXT"})
        self.r.tick()
        self.settle_all(self.r)
        self.r.tick()
        self.assertEqual(len(calls(self.calls)), 2)
        self.assertIsNone(self.r._cap_observation(self.t["id"]))
        self.assertTrue(self.r.store.get(a.id).wait["cap_override_consumed"])
        self.assertEqual(len([e for e in self.r.store.events(a.id) if e[1] == "cap_override"]), 1)
        self.assertIn(c["body"], calls(self.calls)[1]["prompt"])
        self.assertIn(c["id"], calls(self.calls)[1]["prompt"])
        self.assertNotIn("met", saved.wait)

    def test_instruction_is_reserved_at_ambiguous_spawn_boundary_and_not_replayed(self):
        from unittest.mock import patch
        a = self.unresolved_card()
        c = self.override_comment(a)
        self.r.check_wakes()
        with patch.object(self.adapter, "spawn", side_effect=RuntimeError("ambiguous spawn boundary")):
            self.r.tick()
        consumed = self.r.store.get(a.id).wait
        self.assertTrue(consumed.get("cap_override_consumed"))
        self.assertFalse(consumed.get("cap_wake"))
        self.assertEqual(consumed["cap_override"]["comment_id"], c["id"])
        self.r.store.db.close()
        self.r = self.runner(self.mesh, self.gl)
        self.r.check_wakes()
        self.assertFalse(self.r.store.get(a.id).wait.get("cap_wake"))
        self.assertEqual(len(calls(self.calls)), 1)
        self.assertIn("ambiguous spawn", self.r.hold_reason(self.mesh.tasks[self.t["id"]], "work"))
        self.assertIn(consumed["cap_override_consumed"], self.r.hold_reason(self.mesh.tasks[self.t["id"]], "work"))

    def test_known_pre_spawn_failure_preserves_instruction_for_normal_restart_cleanup(self):
        from unittest.mock import patch
        a = self.unresolved_card()
        c = self.override_comment(a)
        self.r.check_wakes()
        with patch.object(self.adapter, "start_argv", side_effect=RuntimeError("argv construction failed")):
            self.r.tick()
        saved = self.r.store.get(a.id).wait
        self.assertTrue(saved.get("cap_wake"))
        self.assertNotIn("cap_override_consumed", saved)
        self.r.store.db.close()
        self.r = self.runner(self.mesh, self.gl)
        future = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=6)
        with patch("mesh_runner.runner.now", return_value=future), patch("mesh_runner.store.now", return_value=future):
            self.r.tick()
            self.settle_all(self.r)
        self.assertEqual(len(calls(self.calls)), 2)
        self.assertIn(c["body"], calls(self.calls)[1]["prompt"])
        self.assertTrue(self.r.store.get(a.id).wait.get("cap_override_consumed"))

    def unresolved_with_finalization_gap(self):
        # Model time spent releasing checkout/worktrees after the generation mark,
        # without sleeps or IO stress. Final attempt updated_at is a different clock.
        mark = Store.mark

        def earlier_generation(store, agent, task_id, key, value=None):
            if key == "cap_observation" and value:
                stamp = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=3)
                with mock.patch("mesh_runner.store.now", return_value=stamp):
                    return mark(store, agent, task_id, key, value)
            return mark(store, agent, task_id, key, value)

        with mock.patch.object(Store, "mark", earlier_generation):
            a = self.unresolved_card()
        generation = dt.datetime.fromisoformat(self.r.store.mark("builder", a.task_id, "cap_observation")[1])
        self.assertGreater(dt.datetime.fromisoformat(a.updated_at) - generation, dt.timedelta(seconds=1))
        return a, generation

    def test_fresh_override_between_generation_and_finalization_is_accepted(self):
        a, generation = self.unresolved_with_finalization_gap()
        c = self.override_comment(a, at=generation + dt.timedelta(seconds=1))
        self.assertLess(dt.datetime.fromisoformat(c["created_at"]), dt.datetime.fromisoformat(a.updated_at))
        self.r.check_wakes()
        saved = self.r.store.get(a.id)
        self.assertEqual(saved.wait["cap_override"]["comment_id"], c["id"])
        self.assertTrue(saved.wait["cap_wake"])
        self.assertEqual(len(calls(self.calls)), 1)  # observation does not itself bypass admission

    def test_override_wrong_attempt_foreign_self_stale_or_unknown_lead_is_rejected(self):
        a, generation = self.unresolved_with_finalization_gap()
        cases = [("foreign", {"author": "agent-reviewer"}),
                 ("self", {"author": AGENT_ID}),
                 ("wrong_attempt", {"attempt": "00000000-0000-4000-8000-000000000009"}),
                 ("stale", {"at": generation - dt.timedelta(seconds=1)}),
                 ("unknown_lead", {}), ("self_as_lead", {})]
        for kind, kwargs in cases:
            with self.subTest(kind=kind):
                self.mesh.comment_log[self.t["id"]] = []
                self.override_comment(a, **kwargs)
                if kind == "unknown_lead":
                    self.cfg.project_leads["p1"] = "missing"
                if kind == "self_as_lead":
                    self.cfg.project_leads["p1"] = "builder"
                self.r.tick()
                self.settle_all(self.r)
                self.assertEqual(len(calls(self.calls)), 1)
                self.assertFalse(self.r.store.get(a.id).wait.get("cap_wake"))

    def test_lead_instruction_does_not_override_manual_hold(self):
        a = self.unresolved_card()
        self.override_comment(a)
        self.mesh.tasks[self.t["id"]]["labels"] = ["no-runner"]
        self.r.tick()
        self.settle_all(self.r)
        self.assertEqual(len(calls(self.calls)), 1)
        self.assertEqual(self.r.hold_reason(self.mesh.tasks[self.t["id"]], "work"), "label no-runner")

    def test_metadata_missing_malformed_or_without_server_identity_is_not_override(self):
        a = self.unresolved_card()
        for field, value in (("metadata", None), ("metadata", []), ("id", "not-a-uuid"),
                             ("created_at", "unknown"), ("author_id", None), ("body", "")):
            with self.subTest(field=field, value=value):
                self.mesh.comment_log[self.t["id"]] = []
                c = self.override_comment(a)
                c[field] = value
                self.r.check_wakes()
                self.assertFalse(self.r.store.get(a.id).wait.get("cap_wake"))

    def test_actual_reassignment_keeps_old_runner_from_starting(self):
        self.unresolved_card()
        self.mesh.tasks[self.t["id"]]["assignee_id"] = "agent-lead"
        self.r.tick()
        self.settle_all(self.r)
        self.assertEqual(len(calls(self.calls)), 1)

    def test_terminal_card_event_releases_unresolved_observation_once(self):
        self.unresolved_card()
        self.mesh.set_category(task(2)["id"], "done")
        write_replies(self.replies, {self.t["id"][:8]: "↻ NEXT"})
        self.r.tick()
        self.settle_all(self.r)
        self.r.tick()
        self.assertEqual(len(calls(self.calls)), 2)

    def test_unknown_lead_directory_rejects_override_but_not_actual_terminal_event(self):
        from unittest.mock import patch
        a = self.unresolved_card()
        self.override_comment(a)
        with patch.object(self.mesh, "workspace_agents", side_effect=MeshError(503, "unavailable")):
            self.r.check_wakes()
            self.assertFalse(self.r.store.get(a.id).wait.get("cap_wake"))
            self.mesh.set_category(task(2)["id"], "done")
            self.r.check_wakes()
            saved = self.r.store.get(a.id).wait
            self.assertTrue(saved.get("cap_wake"))
            self.assertNotIn("cap_override", saved)

    def test_unrelated_poll_does_not_read_project_lead_directory(self):
        from unittest.mock import patch
        self.unresolved_card()
        self.cfg.project_leads["p1"] = "lead"
        self.mesh.add_comment(self.t["id"], "Dashboard refreshed.", author="agent-lead")
        with patch.object(self.mesh, "workspace_agents", side_effect=AssertionError("unnecessary directory read")) as directory:
            self.r.check_wakes()
            directory.assert_not_called()
        self.assertFalse(self.r._cap_observation(self.t["id"]).wait.get("cap_wake"))

    def test_instruction_freshness_uses_immutable_observation_not_seen_update(self):
        from unittest.mock import patch
        a = self.cap()
        c = self.override_comment(a)
        # Metadata was omitted by a compact response during a nonterminal seen
        # transition. The same full server comment must remain eligible later.
        full_metadata = c.pop("metadata")
        self.gl.pipelines["g/app#5"] = "pending"
        later = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=2)
        with patch("mesh_runner.store.now", return_value=later):
            self.r.check_wakes()
        saved = self.r.store.get(a.id)
        self.assertGreater(saved.updated_at, c["created_at"])
        self.assertFalse(saved.wait.get("cap_wake"))
        c["metadata"] = full_metadata
        self.r.check_wakes()
        self.assertEqual(self.r.store.get(a.id).wait["cap_override"]["comment_id"], c["id"])

    def test_ambiguous_project_lead_name_rejects_override_but_exact_id_authorizes(self):
        a = self.unresolved_card()
        c = self.override_comment(a)
        self.mesh.agents.append({"id": "another-lead", "name": "lead"})
        self.r.check_wakes()
        self.assertFalse(self.r.store.get(a.id).wait.get("cap_wake"))
        self.cfg.project_leads["p1"] = "agent-lead"
        self.r.check_wakes()
        self.assertEqual(self.r.store.get(a.id).wait["cap_override"]["comment_id"], c["id"])

    def cap(self, status="running", reply="⏳ WAIT pipeline:g/app#5"):
        from mesh_runner.wake import GitLab
        self.cfg.daily_wait_budget = 0
        self.cfg.daily_next_budget = 0
        self.cfg.daily_start_cap = 20
        self.t = task(1)
        self.mesh, self.gl = FakeMesh([self.t], max_concurrent=1), FakeGitLab()
        self.gl.pipelines["g/app#5"] = status
        self.gl.seed = lambda kind, target, now: GitLab.seed(self.gl, kind, target, now)
        self.gl.pipeline_event = lambda target, last: GitLab.pipeline_event(self.gl, target, last)
        write_replies(self.replies, {self.t["id"][:8]: reply})
        self.r = self.runner(self.mesh, self.gl)
        self.r.tick()
        self.settle_all(self.r)
        return self.r.store.by_state("builder", ("failed",))[0]

    def test_cap_keeps_target_and_unresolved_event_then_terminal_wakes_once(self):
        a = self.cap()
        self.assertEqual(a.wait["target"], "g/app#5")
        self.assertEqual(a.wait["seen"], {"pipeline": "running"})
        self.r.check_wakes()
        self.assertEqual(len(calls(self.calls)), 1)
        self.assertIn("daily turn budget", self.r.hold_reason(self.mesh.tasks[self.t["id"]], "work"))
        self.gl.pipelines["g/app#5"] = "success"
        self.r.tick()
        self.settle_all(self.r)
        self.assertEqual(len(calls(self.calls)), 3)
        for _ in range(3):
            self.r.tick()
        self.assertEqual(len(calls(self.calls)), 3)
        self.assertEqual(self.r.store.outcomes_today("builder", self.t["id"]).get("wait"), 1)
        self.assert_spent_budget_holds_after_rewait()
        # Repeated terminal observations require correction, with no timer or
        # automatic release at the next UTC day.
        latest = self.r.store.by_state("builder", ("failed",))[-1]
        tomorrow = dt.datetime.fromisoformat(latest.updated_at) + dt.timedelta(days=1)
        with mock.patch("mesh_runner.runner.now", return_value=tomorrow), mock.patch("mesh_runner.store.now", return_value=tomorrow):
            self.assertTrue(self.r.hold_reason(dict(self.mesh.tasks[self.t["id"]]), "work").startswith(
                "wait correction required;"))
        self.assert_spent_budget_holds_after_rewait()
        self.mesh.add_comment(self.t["id"], "Continue with the next action", author="person",
                              at=dt.datetime.fromisoformat(latest.updated_at) + dt.timedelta(seconds=1))
        self.assertEqual(self.r.hold_reason(dict(self.mesh.tasks[self.t["id"]]), "work"), "")

    def assert_spent_budget_holds_after_rewait(self):
        rows = [a for a in self.r.store.by_state("builder", ("failed",)) if a.task_id == self.t["id"]]
        self.assertTrue(rows)
        self.assertTrue(all(a.note.startswith(("turn budget:", "wait correction required:")) for a in rows))
        self.assertIsNone(self.r.store.open_for_task("builder", self.t["id"]))
        self.assertIsNone(self.r._cap_observation(self.t["id"]))
        self.assertFalse((self.r.store.mark("builder", self.t["id"], "cap_observation") or ("",))[0])
        reason = self.r.hold_reason(dict(self.mesh.tasks[self.t["id"]]), "work")
        if rows[-1].note.startswith("wait correction required:"):
            self.assertTrue(reason.startswith("wait correction required;"))
        else:
            self.assertEqual(reason, "daily turn budget spent; waits for tomorrow (UTC) or a new comment")

    def test_cap_next_has_no_event_escape(self):
        self.cap(reply="↻ NEXT")
        self.gl.pipelines["g/app#5"] = "success"
        for _ in range(3):
            self.r.tick()
        self.assertEqual(len(calls(self.calls)), 1)
        self.assertIsNone(self.r._cap_observation(self.t["id"]))

    def test_already_terminal_canceled_and_manual_each_wake_once(self):
        for status in ("canceled", "manual"):
            with self.subTest(status=status):
                # Each terminal state is a distinct event, with the ordinary start
                # cap and counter still in force; the same state is never a loop.
                if status == "canceled":
                    self.cap(status)
                else:
                    self.gl.pipelines["g/app#5"] = status
                self.r.tick()
                self.settle_all(self.r)
                # the same wait again on that final state is told once more, with the way out
                expected = 3
                self.assertEqual(len(calls(self.calls)), expected)
                self.assert_spent_budget_holds_after_rewait()
                self.r.tick()
                self.r.tick()
                self.assertEqual(len(calls(self.calls)), expected)

    def test_static_hold_still_applies_after_terminal_event(self):
        self.cap()
        # Use an actual default policy hold, rather than an unconfigured label.
        # Wait for children before counting: spawn records arrive asynchronously,
        # and a generic truthy hold can instead be the next turn's spent budget.
        held_task = self.mesh.tasks[self.t["id"]]
        held_task["labels"] = ["no-runner"]
        self.assertEqual(self.r.hold_reason(held_task, "work"), "label no-runner")
        self.gl.pipelines["g/app#5"] = "failed"
        self.r.tick()
        self.settle_all(self.r)
        self.assertEqual(len(calls(self.calls)), 1)
        self.assertFalse(self.r.store.by_state("builder", ("starting", "running")))
        self.assertEqual(self.r.hold_reason(held_task, "work"), "label no-runner")

    def test_restarted_runner_observes_persisted_cap_and_consumes_once(self):
        self.cap()
        self.r.store.db.close()
        self.r = self.runner(self.mesh, self.gl)
        self.gl.pipelines["g/app#5"] = "failed"
        self.r.tick()
        self.settle_all(self.r)
        self.assertEqual(len(calls(self.calls)), 3)
        self.r.tick()
        self.assertEqual(len(calls(self.calls)), 3)
        self.assert_spent_budget_holds_after_rewait()

    def test_event_grants_one_turn_but_next_is_capped_again(self):
        self.cap()
        write_replies(self.replies, {self.t["id"][:8]: "↻ NEXT"})
        self.gl.pipelines["g/app#5"] = "success"
        self.r.tick()
        self.settle_all(self.r)
        self.r.tick()
        self.assertEqual(len(calls(self.calls)), 2)
        self.assertEqual(self.r.store.outcomes_today("builder", self.t["id"])["next"], 1)
        self.assertIsNone(self.r._cap_observation(self.t["id"]))

    def test_next_target_replaces_the_old_observation(self):
        self.cap()
        write_replies(self.replies, {self.t["id"][:8]: "⏳ WAIT pipeline:g/app#6"})
        self.gl.pipelines["g/app#5"] = "success"
        self.r.tick()
        self.settle_all(self.r)
        a = self.r._cap_observation(self.t["id"])
        self.assertEqual(a.wait["target"], "g/app#6")
        self.gl.pipelines["g/app#5"] = "failed"
        self.r.tick()
        self.assertEqual(len(calls(self.calls)), 2)
        self.gl.pipelines["g/app#6"] = "success"
        self.r.tick()
        self.settle_all(self.r)
        self.assertEqual(len(calls(self.calls)), 4)
        self.r.tick()
        self.assertEqual(len(calls(self.calls)), 4)

    def test_pipeline_retry_can_settle_to_the_same_status_after_running_again(self):
        self.cap("failed")
        self.r.tick()
        self.settle_all(self.r)
        self.assertEqual(len(calls(self.calls)), 3)     # woken, then told the repeated wait is dead
        self.gl.pipelines["g/app#5"] = "running"
        self.r.check_wakes()
        self.gl.pipelines["g/app#5"] = "failed"
        self.r.tick()
        self.settle_all(self.r)
        # The spent budget holds the dead re-wait; a later CI retry cannot
        # grant a second event escape after its observation was consumed.
        self.assertEqual(len(calls(self.calls)), 3)
        self.assert_spent_budget_holds_after_rewait()

    def test_mr_merge_event_is_observed_once_under_cap(self):
        from mesh_runner.wake import GitLab
        self.cap(reply="⏳ WAIT mr:g/app!5")
        self.gl.mr_event = lambda target, last, now=None: GitLab.mr_event(self.gl, target, last, now)
        self.gl.mr_rows["g/app!5"] = {"state": "merged"}
        self.r.tick()
        self.settle_all(self.r)
        self.assertEqual(len(calls(self.calls)), 3)
        self.r.tick()
        self.r.tick()
        self.assertEqual(len(calls(self.calls)), 3)
        self.assert_spent_budget_holds_after_rewait()

    def test_event_does_not_bypass_daily_start_cap(self):
        self.cap()
        self.cfg.daily_start_cap = 1
        self.gl.pipelines["g/app#5"] = "success"
        self.r.tick()
        self.r.tick()
        self.assertEqual(len(calls(self.calls)), 1)


if __name__ == "__main__":
    unittest.main()


class WaitWorkspaceCheckpoint(Bench):
    def test_wait_returns_workspace_and_wake_uses_fresh_session(self):
        t = task(71)
        self.cfg.workspace_wait_release_minutes = 30
        write_replies(self.replies, {t['id'][:8]: f'⏳ WAIT {FUTURE_AT}: external=fixture-window source=card:#abcdef12 external work'})
        r = self.runner(FakeMesh([t]))
        r.tick()
        self.settle_all(r)
        a = r.store.by_state('builder', ('waiting',))[0]
        # A real turn ended; pin a long-standing wait, then capture dirty data too.
        wait = dict(a.wait)
        wait['started'] = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)).isoformat()
        r.store.update(a, wait=wait)
        p = Path(a.workspaces[0])
        (p / 'README').write_text('checkpoint work')
        old_sid = a.session_id
        r._suspend_waiting(a)
        self.assertEqual(r.pool.leased_to(a.id), [])
        r.store.update(a, state='ready', wake_text='dependency met')
        write_replies(self.replies, {t['id'][:8]: f'⏳ WAIT {FUTURE_AT}: external=fixture-window source=card:#abcdef12 external work'})
        r._resume_one(a)
        current = r.store.get(a.id)
        self.assertEqual(current.state, 'running')
        self.assertNotEqual(current.session_id, old_sid)
        self.assertEqual(Path(current.workspaces[0]).joinpath('README').read_text(), 'checkpoint work')
        self.settle_all(r)

    def test_wait_never_releases_live_writer_group(self):
        from unittest.mock import patch
        t = task(72)
        self.agent.workspace_wait_release_minutes = 30
        r = self.runner(FakeMesh([t]))
        a = r.store.create('builder', t['id'], 'claude')
        path = r.pool.lease(a.id, 'app')
        r.store.update(a, state='waiting', workspaces=[str(path)], wait={
            'started': (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2)).isoformat()})
        with patch('mesh_runner.runner.group_alive', return_value=True):
            from mesh_runner.workspace import WorkspaceError
            with self.assertRaisesRegex(WorkspaceError, 'still alive'):
                r._suspend_waiting(a)
        self.assertEqual(r.pool.leased_to(a.id), [str(path)])

    def test_wait_releases_when_recorded_pid_was_reused_by_another_process(self):
        import os
        from unittest.mock import patch
        t = task(74)
        self.agent.workspace_wait_release_minutes = 30
        r = self.runner(FakeMesh([t]))
        a = r.store.create('builder', t['id'], 'claude')
        path = r.pool.lease(a.id, 'app')
        r.store.update(a, state='waiting', pid=os.getpid(), stamp='Thu Jan  1 00:00:00 1970',
                       workspaces=[str(path)], wait={
            'started': (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2)).isoformat()})
        a = r.store.get(a.id)
        with patch('mesh_runner.adapters.base.os.killpg', return_value=None):
            r._suspend_waiting(a)
        self.assertEqual(r.pool.leased_to(a.id), [])

    def test_capture_is_disabled_by_default_and_scoped_override_is_opt_in(self):
        self.assertEqual(self.cfg.workspace_wait_release_minutes, 0)
        t = task(73)
        r = self.runner(FakeMesh([t]))
        a = r.store.create('builder', t['id'], 'claude')
        path = r.pool.lease(a.id, 'app')
        r.store.update(a, state='waiting', workspaces=[str(path)], wait={
            'started': (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2)).isoformat()})
        r._suspend_waiting(a)
        self.assertEqual(r.pool.leased_to(a.id), [str(path)])
        self.cfg.workspace_wait_release_minutes = 30
        self.agent.workspace_wait_release_minutes = 0
        r._suspend_waiting(a)
        self.assertEqual(r.pool.leased_to(a.id), [str(path)])
        self.cfg.workspace_wait_release_minutes = 0
        self.agent.workspace_wait_release_minutes = 30
        r._suspend_waiting(a)
        self.assertEqual(r.pool.leased_to(a.id), [])

    def test_ci_waits_and_imminent_time_waits_keep_the_slot(self):
        t = task(75)
        self.agent.workspace_wait_release_minutes = 30
        r = self.runner(FakeMesh([t]))
        old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2)).isoformat()
        soon = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=10)).isoformat()
        later = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=5)).isoformat()
        cases = [({'kind': 'pipeline'}, True), ({'kind': 'mr'}, True),
                 ({'kind': 'mr', 'ci_change': {'predicate': 'change'}}, True), ({'kind': 'time', 'until': soon}, True),
                 ({'kind': 'time'}, True), ({'kind': 'time', 'until': 'not a date'}, True), ({'kind': 'time', 'until': ''}, True),
                 ({'kind': 'card'}, False), ({'kind': 'time', 'until': later}, False)]
        for wait, keeps in cases:
            a = r.store.create('builder', t['id'], 'claude')
            path = r.pool.lease(a.id, 'app')
            r.store.update(a, state='waiting', workspaces=[str(path)], wait=dict(wait, started=old))
            r._suspend_waiting(a)
            self.assertEqual(bool(r.pool.leased_to(a.id)), keeps, wait)
            r.pool.release(a.id)
            r.store.update(a, state='done')

    def test_partial_multirepo_capture_reconciles_after_restart_before_spawn(self):
        from unittest.mock import patch
        from mesh_runner.checkpoint import Checkpoints
        from mesh_runner.workspace import WorkspaceError
        second_root = self.root / 'second-repo'
        second_root.mkdir()
        self.agent.repos.append(RepoSpec('second', make_repo(second_root), max_slots=1))
        self.agent.workspace_wait_release_minutes = 30
        t = task(74)
        mesh = FakeMesh([t])
        write_replies(self.replies, {t['id'][:8]: f'⏳ WAIT {FUTURE_AT}: external=fixture-window source=card:#abcdef12 external work'})
        r = self.runner(mesh)
        a = r.store.create('builder', t['id'], 'claude')
        paths = [str(r.pool.lease(a.id, repo.name)) for repo in self.agent.repos]
        for n, path in enumerate(paths):
            Path(path).joinpath('README').write_text(f'valuable {n}')
        r.store.update(a, state='waiting', workspaces=paths, wait={
            'started': (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2)).isoformat()})
        original = Checkpoints.create
        def fail_second(checkpoints, key, source, metadata):
            if metadata['repo'] == 'second':
                raise WorkspaceError('second capture failed')
            return original(checkpoints, key, source, metadata)
        with patch.object(Checkpoints, 'create', fail_second):
            with self.assertRaises(WorkspaceError):
                r._suspend_waiting(a)
        restarted = self.runner(mesh)
        restarted.refresh_me()
        a = restarted.store.get(a.id)
        restarted.store.update(a, state='ready', wake_text='dependency met')
        restarted._resume_one(a)
        resumed = restarted.store.get(a.id)
        self.assertEqual(resumed.state, 'running')
        self.assertEqual(len(resumed.workspaces), 2)
        self.assertEqual({Path(p).joinpath('README').read_text() for p in resumed.workspaces},
                         {'valuable 0', 'valuable 1'})
        for row in restarted.pool._checkpoint_rows(a.id):
            manifest = restarted.pool._checkpoints().read(Path(row['checkpoint']))
            self.assertIn('started', manifest['metadata']['wait'])
            self.assertEqual(manifest['metadata']['workspaces'], paths)
        self.settle_all(restarted)


class ResumeInventoryFixedAtAdmission(Bench):
    """An attempt resumes with the repositories it was admitted with, not today's config."""

    def suspended_one_repo_attempt(self, n):
        self.agent.workspace_wait_release_minutes = 30
        t = task(n)
        mesh = FakeMesh([t])
        r = self.runner(mesh)
        a = r.store.create('builder', t['id'], 'claude')
        path = r.pool.lease(a.id, 'app')
        Path(path).joinpath('README').write_text('old work')
        r.store.update(a, state='waiting', workspaces=[str(path)], wait={
            'started': (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2)).isoformat()})
        r._suspend_waiting(a)
        r.store.update(a, state='ready', wake_text='dependency met')
        return mesh, t, a

    def add_repo(self, name='second'):
        root = self.root / f'{name}-repo'
        root.mkdir()
        self.agent.repos.append(RepoSpec(name, make_repo(root), max_slots=1))

    def test_old_one_repo_attempt_resumes_after_config_grew(self):
        mesh, t, a = self.suspended_one_repo_attempt(81)
        self.add_repo()
        r = self.runner(mesh)
        r.refresh_me()
        r._resume_one(r.store.get(a.id))
        cur = r.store.get(a.id)
        self.assertEqual(cur.state, 'running')
        self.assertEqual(len(cur.workspaces), 1)
        self.assertEqual(Path(cur.workspaces[0]).joinpath('README').read_text(), 'old work')
        self.settle_all(r)

    def test_two_repo_attempt_still_requires_both_repos(self):
        from mesh_runner.workspace import InventoryError
        mesh, t, a = self.suspended_one_repo_attempt(82)
        self.add_repo()
        r = self.runner(mesh)
        r.refresh_me()
        # snapshot claims two workspaces but the journal holds one: a missing checkpoint
        r.store.update(r.store.get(a.id), workspaces=['/x/app', '/x/second'])
        with self.assertRaisesRegex(InventoryError, 'incomplete repository inventory'):
            r._resume_one(r.store.get(a.id))
        self.assertEqual(r.store.get(a.id).state, 'ready')

    def test_duplicate_unknown_and_extra_repos_are_rejected(self):
        from mesh_runner.workspace import InventoryError
        mesh, t, a = self.suspended_one_repo_attempt(83)
        r = self.runner(mesh)
        r.refresh_me()
        cur = r.store.get(a.id)
        path = cur.workspaces[0]
        r.store.update(cur, workspaces=[path, path])  # duplicate / extra
        with self.assertRaises(InventoryError):
            r._resume_one(r.store.get(a.id))
        r.store.update(r.store.get(a.id), workspaces=[path + '-other'])  # same count, foreign path
        with self.assertRaises(InventoryError):
            r._resume_one(r.store.get(a.id))
        r.store.update(r.store.get(a.id), workspaces=[path])
        r.store.db.execute("UPDATE workspace_checkpoints SET repo='gone' WHERE attempt=?", (a.id,))
        with self.assertRaises(InventoryError):  # journal repo no longer configured
            r._resume_one(r.store.get(a.id))

    def test_permanent_refusal_backs_off_and_is_not_retried_in_the_hold(self):
        from unittest.mock import patch
        from mesh_runner import runner as runner_mod
        from mesh_runner.workspace import InventoryError
        mesh, t, a = self.suspended_one_repo_attempt(84)
        r = self.runner(mesh)
        r.refresh_me()
        r.store.update(r.store.get(a.id), workspaces=[])
        holds = []
        for _ in range(3):
            with self.assertRaises(InventoryError):
                r._resume_one(r.store.get(a.id))
            holds.append(round(r._resume_hold[a.id] - __import__('time').monotonic()))
        self.assertGreater(holds[1], holds[0] + 60)
        self.assertGreater(holds[2], holds[1] + 60)
        with patch.object(r, '_resume_one') as resume:
            r.resume_ready(1)
        resume.assert_not_called()
