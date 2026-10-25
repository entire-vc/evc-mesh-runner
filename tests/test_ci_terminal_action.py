"""An exact success WAIT reports terminal failure without completing the predicate."""
import copy
import datetime as dt
import unittest

from fakes import FakeGitLab, FakeMesh, calls, task, write_replies
from mesh_runner.outcome import parse_line
from mesh_runner.store import iso
from mesh_runner.wake import ci_change_event, ci_delivery_key
from test_runner import Bench

SHA = "a" * 40
TARGET = "g/app#5"
TERMINAL_FAILURES = ("failed", "canceled", "cancelled", "skipped")


class TerminalEvaluator(unittest.TestCase):
    def test_failure_notice_includes_unchanged_terminal_baseline_without_success(self):
        gl = FakeGitLab()  # external GitLab observations; evaluator stays real
        for status in TERMINAL_FAILURES:
            for baseline in ("running", status):
                with self.subTest(status=status, baseline=baseline):
                    contract = {"sha": SHA, "pipeline": 5, "status": baseline, "predicate": "success"}
                    original = copy.deepcopy(contract)
                    gl.pipeline_rows[TARGET] = {"id": 5, "sha": SHA, "status": status}
                    observation = {}
                    event = ci_change_event(gl, "pipeline", TARGET, contract,
                                            observation=observation, allow_action_required=True)
                    self.assertIn("action required", event)
                    self.assertIn(status, event)
                    self.assertIn("success predicate is not met", event)
                    self.assertTrue(observation["action_required"])
                    self.assertEqual(contract, original)
                    self.assertEqual(ci_change_event(gl, "pipeline", TARGET, contract,
                        delivered=lambda key: observation["value"], allow_action_required=True), "")
                    # Exhausted-budget and review observers do not opt into owner action.
                    self.assertEqual(ci_change_event(gl, "pipeline", TARGET, contract), "")


class TerminalAction(Bench):
    def setup_wait(self, status, baseline="running"):
        self.case_number = getattr(self, "case_number", 0) + 1
        t = task(self.case_number, cat="in_progress")
        gl = FakeGitLab()  # external GitLab observations
        gl.pipeline_rows[TARGET] = {"id": 5, "sha": SHA, "status": status}
        r = self.runner(FakeMesh([t]), gl)  # external Mesh service
        r.refresh_me()
        a = r.store.create(self.agent.name, t["id"], "claude")
        marker = f"⏳ WAIT pipeline:{TARGET} until=success after={SHA}:5:{baseline}"
        outcome = parse_line(marker, dt.datetime.now(dt.timezone.utc))
        wait = {"kind": outcome.kind, "target": outcome.target, **outcome.extra,
                "since": iso(), "started": iso()}
        r.store.update(a, state="waiting", wait=wait, session_id="existing-session")
        return r, a, t, gl, wait, marker

    def deliver(self, r, a, t, marker):
        # Run the production resume/spawn/receipt/settle path with a real child.
        write_replies(self.replies, {t["id"][:8]: marker})
        r._resume_one(a)
        running = r.store.get(a.id)
        self.assertEqual(running.state, "running")
        # The owner acts on the notice in this turn (retries the job) and waits for its result.
        # Re-declaring until=success while the pipeline is still red is sent back at declaration.
        pipeline = r.gitlab.pipeline_rows[TARGET]
        status, pipeline["status"] = pipeline["status"], "running"
        settled = self.finish_turn(r)
        pipeline["status"] = status
        self.assertEqual(settled.state, "waiting")
        self.assertIn("action required", calls(self.calls)[-1]["prompt"])
        return settled

    def test_failure_actions_survive_restart_and_are_delivered_once_per_task_status(self):
        for status in TERMINAL_FAILURES:
            with self.subTest(status=status):
                r, a, t, gl, wait, marker = self.setup_wait(status, baseline=status)
                r._wake_one(a)
                self.assertEqual(a.state, "ready")
                self.assertEqual(a.wait["ci_change"], wait["ci_change"])
                self.assertFalse(a.wait.get("met"))
                self.assertIsNone(r.store.mark(a.agent, a.task_id, "met_wake"))
                self.assertIsNone(r.store.mark(a.agent, a.task_id, ci_delivery_key(TARGET, 5)))
                delivery = a.wait["ci_action_delivery"]
                self.assertIsNone(r.store.mark(a.agent, a.task_id, delivery["key"]))
                # READY restart retains the unconsumed notice; only a real spawn marks it.
                restarted = self.runner(r.mesh, gl)
                restarted.refresh_me()
                a = restarted.store.get(a.id)
                self.assertEqual(a.wait["ci_action_delivery"], delivery)
                a = self.deliver(restarted, a, t, marker)
                self.assertEqual(restarted.store.mark(a.agent, a.task_id, delivery["key"])[0], delivery["value"])
                self.assertIsNone(restarted.store.mark(a.agent, a.task_id, "met_wake"))
                self.assertIsNone(restarted.store.mark(a.agent, a.task_id, ci_delivery_key(TARGET, 5)))
                restarted._wake_one(a)
                self.assertEqual(a.state, "waiting")
                restarted.store.update(a, state="completed")
                another = self.runner(r.mesh, gl)
                another.refresh_me()
                b = another.store.create(a.agent, a.task_id, "claude")
                another.store.update(b, state="waiting", wait=wait)
                another._wake_one(b)
                self.assertEqual(b.state, "waiting")
                # Retry success has a separate completion wake after failure delivery.
                gl.pipeline_rows[TARGET]["status"] = "success"
                another._wake_one(b)
                self.assertEqual(b.state, "ready")
                self.assertIn("success", b.wake_text)
                self.assertNotIn("action required", b.wake_text)
                self.assertEqual(another.store.mark(b.agent, b.task_id, "met_wake")[0], b.id)
                self.assertIsNotNone(another.store.mark(b.agent, b.task_id, ci_delivery_key(TARGET, 5)))
                another.store.update(b, state="completed")

    def test_status_and_task_have_independent_action_deliveries(self):
        r, a, t, gl, wait, marker = self.setup_wait("failed")
        r._wake_one(a)
        self.assertEqual(a.state, "ready")
        failed_key = a.wait["ci_action_delivery"]["key"]
        a = self.deliver(r, a, t, marker)
        gl.pipeline_rows[TARGET]["status"] = "canceled"
        r._wake_one(a)
        self.assertEqual(a.state, "ready")
        self.assertNotEqual(a.wait["ci_action_delivery"]["key"], failed_key)
        r.store.update(a, state="completed")
        other_task = task(2, cat="in_progress")
        r.mesh.tasks[other_task["id"]] = other_task
        b = r.store.create(a.agent, other_task["id"], "claude")
        r.store.update(b, state="waiting", wait=wait)
        gl.pipeline_rows[TARGET]["status"] = "failed"
        r._wake_one(b)
        self.assertEqual(b.state, "ready")
        self.assertEqual(b.wait["ci_action_delivery"]["key"], failed_key)

    def test_active_or_success_states_never_report_failure_action(self):
        r, a, t, gl, wait, marker = self.setup_wait("running")
        for status in ("created", "waiting_for_resource", "preparing", "pending", "running", "scheduled"):
            with self.subTest(status=status):
                gl.pipeline_rows[TARGET]["status"] = status
                r._wake_one(a)
                self.assertEqual(a.state, "waiting")
                self.assertNotIn("ci_action_delivery", a.wait)
        gl.pipeline_rows[TARGET]["status"] = "success"
        r._wake_one(a)
        self.assertEqual(a.state, "ready")
        self.assertNotIn("ci_action_delivery", a.wait)
        self.assertNotIn("action required", a.wake_text)
