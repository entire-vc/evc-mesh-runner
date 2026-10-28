"""A blocking manual pipeline is an action notice, never success or cap escape."""
import copy
import datetime as dt
from types import SimpleNamespace
from unittest import mock

from fakes import FakeGitLab, FakeMesh, task
from mesh_runner.outcome import parse_line
from mesh_runner.store import iso
from mesh_runner.wake import ci_delivery_key
from test_runner import Bench

SHA = "a" * 40
TARGET = "g/app#5"


class ManualAction(Bench):
    def setup_wait(self, status="manual", sha=SHA, pid=5, baseline="failed"):
        self.case_number = getattr(self, "case_number", 0) + 1
        t = task(self.case_number)
        gl = FakeGitLab()
        gl.pipeline_rows[TARGET] = {"id": pid, "sha": sha, "status": status}
        r = self.runner(FakeMesh([t]), gl)
        r.refresh_me()
        a = r.store.create(self.agent.name, t["id"], "claude")
        outcome = parse_line(f"⏳ WAIT pipeline:{TARGET} until=success after={SHA}:5:{baseline}",
                             dt.datetime.now(dt.timezone.utc))
        wait = {"kind": outcome.kind, "target": outcome.target, **outcome.extra,
                "since": iso(), "started": iso()}
        r.store.update(a, state="waiting", wait=wait)
        return r, a, t, gl, wait

    def spawn_notice(self, r, a):
        with mock.patch.object(r.adapter, "spawn", return_value=SimpleNamespace(pid=99999999)):
            self.assertTrue(r._spawn(a, "prompt", False, ("ready",)))

    def test_legacy_nested_observation_keeps_original_and_cas_snapshot_immutable(self):
        for kind in ("pipeline", "mr"):
            with self.subTest(kind=kind):
                r, a, t, gl, _ = self.setup_wait()
                target = TARGET if kind == "pipeline" else "g/app!9"
                key = "pipeline" if kind == "pipeline" else "mr_pipeline"
                before = "running" if kind == "pipeline" else "1:running"
                after = "failed" if kind == "pipeline" else "1:failed"
                wait = {"kind": kind, "target": target, "since": iso(), "started": iso(),
                        "seen": {key: before}}
                r.store.update(a, wait=wait)
                original = a.wait
                snapshot = copy.deepcopy(original)
                gl.pipelines[TARGET] = "failed"
                gl.mr_head[target] = "1:failed"
                update = r.store.update_with_marks
                def observe_update(*args, **kwargs):
                    self.assertEqual(original, snapshot)
                    self.assertEqual(kwargs["expect_wait"], snapshot)
                    self.assertEqual(kwargs["wait"]["seen"][key], after)
                    return update(*args, **kwargs)
                with mock.patch.object(r.store, "update_with_marks", side_effect=observe_update):
                    r._wake_one(a)
                self.assertEqual(a.state, "ready")
                self.assertEqual(original, snapshot)
                self.assertEqual(r.store.get(a.id).wait["seen"][key], after)
                r.store.update(a, state="completed")

    def test_failed_legacy_cas_keeps_original_seen_and_can_retry_event(self):
        r, a, t, gl, _ = self.setup_wait()
        wait = {"kind": "pipeline", "target": TARGET, "since": iso(), "started": iso(),
                "seen": {"pipeline": "running"}}
        r.store.update(a, wait=wait)
        original = a.wait
        gl.pipelines[TARGET] = "failed"
        with mock.patch.object(r.store, "update_with_marks", return_value=False):
            r._wake_one(a)
        self.assertEqual(a.state, "waiting")
        self.assertEqual(original["seen"], {"pipeline": "running"})
        self.assertEqual(r.store.get(a.id).wait["seen"], original["seen"])
        r._wake_one(a)
        self.assertEqual(a.state, "ready")
        self.assertEqual(r.store.get(a.id).wait["seen"], {"pipeline": "failed"})

    def test_nonterminal_nested_observation_is_persisted_without_aliasing(self):
        r, a, t, gl, _ = self.setup_wait()
        wait = {"kind": "pipeline", "target": TARGET, "since": iso(), "started": iso(),
                "seen": {"pipeline": "running"}}
        r.store.update(a, wait=wait)
        original = a.wait
        gl.pipelines[TARGET] = "pending"
        r._wake_one(a)
        self.assertEqual(a.state, "waiting")
        self.assertEqual(original["seen"], {"pipeline": "running"})
        self.assertEqual(r.store.get(a.id).wait["seen"], {"pipeline": "pending"})

    def test_manual_notice_is_not_success_and_completion_receipt_is_separate(self):
        r, a, t, gl, wait = self.setup_wait()
        r._wake_one(a)
        self.assertEqual(a.state, "ready")
        self.assertIn("action required", a.wake_text)
        self.assertIn("success predicate is not met", a.wake_text)
        self.assertEqual(a.wait["ci_change"], wait["ci_change"])
        self.assertFalse(a.wait.get("met"))
        self.assertIsNone(r.store.mark(a.agent, a.task_id, "met_wake"))
        self.assertIsNone(r.store.mark(a.agent, a.task_id, ci_delivery_key(TARGET, 5)))
        action_key = a.wait["ci_action_delivery"]["key"]
        self.assertIsNone(r.store.mark(a.agent, a.task_id, action_key))
        self.spawn_notice(r, a)
        self.assertIsNotNone(r.store.mark(a.agent, a.task_id, action_key))
        r.store.update(a, state="waiting", wait=wait)
        gl.pipeline_rows[TARGET]["status"] = "success"
        r._wake_one(a)
        self.assertEqual(a.state, "ready")
        self.assertEqual(r.store.mark(a.agent, a.task_id, "met_wake")[0], a.id)
        r.store.update(a, state="waiting", wait=wait)
        r._wake_one(a)
        self.assertEqual(a.state, "waiting")

    def test_restart_new_attempt_and_same_manual_baseline_do_not_redeliver(self):
        r, a, t, gl, wait = self.setup_wait(baseline="manual")
        r._wake_one(a)
        self.assertEqual(a.state, "ready")
        self.spawn_notice(r, a)
        r.store.update(a, state="completed")
        restarted = self.runner(r.mesh, gl)
        restarted.refresh_me()
        b = restarted.store.create(a.agent, a.task_id, "claude")
        restarted.store.update(b, state="waiting", wait=wait)
        for _ in range(3):
            restarted._wake_one(b)
            self.assertEqual(b.state, "waiting")
        # A new explicitly requested identity has its own blocking notice.
        new_target = "g/app#6"
        gl.pipeline_rows[new_target] = {"id": 6, "sha": "b" * 40, "status": "manual"}
        newer = {**wait, "target": new_target, "ci_change": {
            "sha": "b" * 40, "pipeline": 6, "status": "failed", "predicate": "success"}}
        restarted.store.update(b, wait=newer)
        restarted._wake_one(b)
        self.assertEqual(b.state, "ready")

    def test_ready_restart_and_refused_gate_retain_unconsumed_notice(self):
        r, a, t, gl, wait = self.setup_wait()
        r._wake_one(a)
        self.assertEqual(a.state, "ready")
        delivery = a.wait["ci_action_delivery"]
        restarted = self.runner(r.mesh, gl)
        restarted.refresh_me()
        b = restarted.store.get(a.id)
        with mock.patch.object(restarted, "_gate", return_value=("held", None)), \
                mock.patch.object(restarted.adapter, "spawn") as spawn:
            restarted._resume_one(b)
            spawn.assert_not_called()
        self.assertEqual(b.state, "ready")
        self.assertEqual(b.wait["ci_action_delivery"], delivery)
        self.assertIsNone(restarted.store.mark(b.agent, b.task_id, delivery["key"]))
        self.spawn_notice(restarted, b)
        self.assertIsNotNone(restarted.store.mark(b.agent, b.task_id, delivery["key"]))

    def test_human_and_turn_cap_do_not_ack_or_start(self):
        for gate in ("human", "cap"):
            with self.subTest(gate=gate):
                r, a, t, gl, wait = self.setup_wait()
                r._wake_one(a)
                self.assertEqual(a.state, "ready")
                delivery = a.wait["ci_action_delivery"]
                if gate == "human":
                    r.mesh.tasks[t["id"]]["human_gate"] = True
                    with mock.patch.object(r, "_gate", return_value=("human gate", None)), \
                            mock.patch.object(r.adapter, "spawn") as spawn:
                        r._resume_one(a)
                        spawn.assert_not_called()
                    self.assertEqual(a.state, "ready")
                else:
                    r.store.update(a, turns=self.cfg.max_turns)
                    with mock.patch.object(r.adapter, "spawn") as spawn:
                        r._resume_one(a)
                        spawn.assert_not_called()
                    self.assertEqual(r.store.get(a.id).state, "failed")
                self.assertIsNone(r.store.mark(a.agent, a.task_id, delivery["key"]))
                r.store.update(a, state="completed")

    def test_existing_cap_observation_does_not_escape_on_manual(self):
        r, a, t, gl, wait = self.setup_wait()
        r.store.update(a, state="failed", note="turn budget: waits exhausted")
        r.store.mark(a.agent, a.task_id, "cap_observation", a.id)
        self.assertEqual(r._cap_observation(a.task_id).id, a.id)
        r._check_cap_event(a)
        self.assertEqual(a.state, "failed")
        self.assertFalse(a.wait.get("cap_wake"))
        self.assertNotIn("ci_action_delivery", a.wait)

    def test_failed_ready_cas_does_not_ack_or_consume(self):
        r, a, t, gl, wait = self.setup_wait()
        with mock.patch.object(r.store, "update_with_marks", return_value=False):
            r._wake_one(a)
        self.assertEqual(a.state, "waiting")
        r._wake_one(a)
        self.assertEqual(a.state, "ready")

    def test_spawn_failure_keeps_notice_and_receipt_failure_stops_writer(self):
        r, a, t, gl, wait = self.setup_wait()
        r._wake_one(a)
        self.assertEqual(a.state, "ready")
        key = a.wait["ci_action_delivery"]["key"]
        with mock.patch.object(r.adapter, "spawn", side_effect=OSError("not started")):
            with self.assertRaises(OSError):
                r._spawn(a, "prompt", False, ("ready",))
        self.assertEqual(a.state, "ready")
        self.assertIsNone(r.store.mark(a.agent, a.task_id, key))
        mark = r.store.mark
        with mock.patch.object(r.adapter, "spawn", return_value=SimpleNamespace(pid=99999999)), \
                mock.patch.object(r.store, "mark", side_effect=lambda *args: (
                    (_ for _ in ()).throw(RuntimeError("receipt failure"))
                    if len(args) == 4 and args[2] == key else mark(*args))), \
                mock.patch("mesh_runner.runner.interrupt_group") as stop:
            with self.assertRaises(RuntimeError):
                r._spawn(a, "prompt", False, ("ready",))
            stop.assert_called_once()
        self.assertEqual(r.store.get(a.id).state, "ready")
        self.assertIsNone(r.store.mark(a.agent, a.task_id, key))

    def test_unknown_or_stale_identity_preserves_bounded_error_recovery(self):
        for row in ({}, {"id": 5, "sha": "b" * 40, "status": "manual"},
                    {"id": 6, "sha": SHA, "status": "manual"},
                    {"id": 5, "sha": SHA, "status": "unknown"}):
            with self.subTest(row=row):
                r, a, t, gl, wait = self.setup_wait()
                gl.pipeline_rows[TARGET] = row
                for _ in range(4):
                    r._wake_one(a)
                    self.assertEqual(a.state, "waiting")
                self.assertEqual(a.wait["observation_state"], "unknown")
                r._wake_one(a)
                self.assertEqual(a.state, "ready")
                self.assertIn("UNKNOWN CI observation", a.wake_text)
                self.assertNotIn("action required", a.wake_text)
                self.assertFalse(a.wait.get("met"))
                self.assertNotIn("ci_action_delivery", a.wait)
                self.assertIsNone(r.store.mark(a.agent, a.task_id, "met_wake"))
                self.assertIsNone(r.store.mark(a.agent, a.task_id, ci_delivery_key(TARGET, 5)))
                r.store.update(a, state="completed")

    def test_api_errors_keep_existing_max_errors_diagnostic(self):
        r, a, t, gl, wait = self.setup_wait()
        with mock.patch.object(gl, "get_pipeline", side_effect=OSError("API unavailable")):
            for _ in range(4):
                r._wake_one(a)
                self.assertEqual(a.state, "waiting")
            r._wake_one(a)
        self.assertEqual(a.state, "ready")
        self.assertIn("UNKNOWN CI observation", a.wake_text)
        self.assertNotIn("API unavailable", a.wake_text)
        self.assertNotIn("ci_action_delivery", a.wait)
        self.assertFalse(a.wait.get("met"))

    def test_missing_after_is_not_an_exact_typed_contract(self):
        for suffix in (" after=:5:failed", " after=abc:5:failed"):
            with self.subTest(suffix=suffix):
                parsed = parse_line(f"⏳ WAIT pipeline:{TARGET} until=success{suffix}",
                                    dt.datetime.now(dt.timezone.utc))
                self.assertEqual(parsed.kind, "unparsed")
                self.assertNotIn("ci_change", parsed.extra)

    def test_missing_after_takes_the_state_at_declaration_as_baseline(self):
        parsed = parse_line(f"⏳ WAIT pipeline:{TARGET} until=success", dt.datetime.now(dt.timezone.utc))
        self.assertEqual(parsed.kind, "pipeline")
        self.assertNotIn("ci_change", parsed.extra)          # the runner fills the baseline in
        self.assertEqual(parsed.extra["ci_default"], {"predicate": "success"})

    def test_disk_preflight_refusal_preserves_pending_action(self):
        r, a, t, gl, wait = self.setup_wait()
        r._wake_one(a)
        self.assertEqual(a.state, "ready")
        delivery = a.wait["ci_action_delivery"]
        with mock.patch.object(r.pool, "disk_hold", return_value="disk below floor"), \
                mock.patch.object(r.adapter, "spawn") as spawn:
            self.assertFalse(r._spawn(a, "prompt", False, ("ready",)))
            spawn.assert_not_called()
        self.assertEqual(a.state, "ready")
        self.assertEqual(a.wait["ci_action_delivery"], delivery)
        self.assertIsNone(r.store.mark(a.agent, a.task_id, delivery["key"]))
        self.spawn_notice(r, a)
        self.assertIsNotNone(r.store.mark(a.agent, a.task_id, delivery["key"]))

    def test_change_predicate_skips_action_path_and_mr_manual_head_takes_it(self):
        r, a, t, gl, wait = self.setup_wait()
        changed = {**wait, "ci_change": {**wait["ci_change"], "predicate": "change"}}
        r.store.update(a, wait=changed)
        r._wake_one(a)
        self.assertEqual(a.state, "ready")
        self.assertIn("changed CI", a.wake_text)
        self.assertEqual(r.store.mark(a.agent, a.task_id, "met_wake")[0], a.id)
        self.assertNotIn("ci_action_delivery", a.wait)
        mr = {**wait, "kind": "mr", "target": "g/app!9"}
        gl.mr_rows[mr["target"]] = {"sha": SHA, "head_pipeline": {
            "sha": SHA, "id": 5, "status": "manual"}}
        r.store.update(a, state="waiting", wait=mr)
        r._wake_one(a)
        # An MR head blocked on manual jobs cannot succeed before review: the owner is told once.
        self.assertEqual(a.state, "ready")
        self.assertIn("action required", a.wake_text)
        self.assertIn("ci_action_delivery", a.wait)

    def test_active_states_do_not_emit_action_and_observer_recovery_can(self):
        r, a, t, gl, wait = self.setup_wait(status="running")
        for status in ("running", "pending"):
            gl.pipeline_rows[TARGET]["status"] = status
            r._wake_one(a)
            self.assertEqual(a.state, "waiting")
        r.gitlab = None
        r._wake_one(a)
        self.assertEqual(a.state, "waiting")
        r.gitlab = gl
        gl.pipeline_rows[TARGET]["status"] = "manual"
        r._wake_one(a)
        self.assertEqual(a.state, "ready")
