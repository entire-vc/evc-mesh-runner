"""A task that hit a cap goes back to the queue and restarts on new input, never on a clock."""
import datetime as dt
import json
import time
from unittest import mock

from fakes import AGENT_ID, FakeMesh, calls, task, write_replies
from test_runner import Bench

from mesh_runner.config import AgentConfig
from mesh_runner.effects import UNSCHEDULE_MARK
from mesh_runner.mesh import MeshError
from mesh_runner.runner import CAP_REASSIGNED, CAP_WAIT
from mesh_runner.selection import Policy
from mesh_runner.workspace import RepoSpec


class CapBench(Bench):
    def setUp(self):
        super().setUp()
        self.cfg.requeue_on_cap = True
        self.cfg.daily_start_cap = 2

    def peer(self, name="peer", **kw):
        ag = AgentConfig(name=name, key="k", harness="claude", profile_dir=self.root,
                         repos=list(kw.pop("repos", self.agent.repos)), **kw)
        self.cfg.agents.append(ag)
        return ag

    def started(self, r, t, n=2, agent="builder"):
        for _ in range(n):
            a = r.store.create(agent, t["id"], "claude")
            r.store.update(a, state="completed", turns=1, note="task left the agent's working statuses")

    def bodies(self, mesh, t):
        return [c["body"] for c in mesh.comment_log.get(t["id"], [])]

    def notes(self, r, t):
        return [a.note for a in r.store.by_state("builder", ("completed", "failed"))
                if a.task_id == t["id"] and a.note.startswith("cap:")]


class StartCap(CapBench):
    def test_start_cap_requeues_in_place_without_backoff(self):
        t = task(1, cat="in_progress", start_after="2099-01-01T00:00:00Z")
        mesh = FakeMesh([t], max_concurrent=1)
        self.agent.lead = "lead"                       # a lead exists and must not get the task
        r = self.runner(mesh)
        r.refresh_me()
        self.started(r, t)
        r._start_cap(dict(mesh.tasks[t["id"]]), 2)
        got = mesh.tasks[t["id"]]
        self.assertEqual(got["assignee_id"], AGENT_ID)
        self.assertEqual(mesh.category(got), "todo")
        self.assertIsNone(got["start_after"])
        self.assertNotIn(t["id"], r._task_backoff)
        self.assertIn("cap: requeued (start cap)", self.notes(r, t))
        body = self.bodies(mesh, t)
        self.assertEqual(len(body), 1)
        self.assertIn("START CAP", body[0])
        self.assertIn("new", body[0])
        self.assertTrue(r._cap_wait(t["id"]))
        self.assertEqual(r.store.starts_today("builder", t["id"]), 2)

    def test_unchanged_inputs_do_not_start_and_new_comment_starts_once(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        write_replies(self.replies, {t["id"][:8]: "⏳ WAIT 2h: soak"})
        r = self.runner(mesh)
        r.refresh_me()
        self.started(r, t)
        for _ in range(3):
            r.tick()                                    # the first tick is the cap, the rest find nothing new
        self.assertEqual(calls(self.calls), [])
        self.assertIn("waiting for new input", r.hold_reason(dict(mesh.tasks[t["id"]]), "work"))
        mesh.add_comment(t["id"], "please look again", author="someone")
        self.settle_all(r)
        self.assertEqual(len(calls(self.calls)), 1)
        self.assertFalse(r._cap_wait(t["id"]))         # the new input is being worked
        self.assertEqual(len(self.bodies(mesh, t)) - 1, 1)   # one cap comment, plus the agent's own wait

    def test_a_comment_after_the_last_session_is_not_swallowed_by_the_start_cap(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        write_replies(self.replies, {t["id"][:8]: "⏳ WAIT 2h: soak"})
        r = self.runner(mesh)
        r.refresh_me()
        self.started(r, t)
        mesh.add_comment(t["id"], "do the prod screenshots", author="someone")
        self.settle_all(r)
        self.assertEqual(len(calls(self.calls)), 1)            # started although the cap is reached
        self.assertFalse(r._cap_wait(t["id"]))
        self.assertEqual(self.notes(r, t), [])

    def test_a_comment_before_the_last_session_still_hits_the_start_cap(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.runner(mesh)
        r.refresh_me()
        mesh.add_comment(t["id"], "already read", author="someone",
                         at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1))
        self.started(r, t)
        r.tick()
        self.assertEqual(calls(self.calls), [])
        self.assertIn("cap: requeued (start cap)", self.notes(r, t))

    def test_runner_notes_and_own_comments_are_not_input(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.runner(mesh)
        r.refresh_me()
        self.started(r, t)
        r._start_cap(dict(mesh.tasks[t["id"]]), 2)
        mesh.add_comment(t["id"], "[runner] something", author="someone")
        mesh.add_comment(t["id"], "my own note")
        self.assertTrue(r.hold_reason(dict(mesh.tasks[t["id"]]), "work").startswith("waiting for new input"))

    def test_a_queue_request_by_the_agents_own_key_is_input(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.runner(mesh)
        r.refresh_me()
        self.started(r, t)
        r._start_cap(dict(mesh.tasks[t["id"]]), 2)
        mesh.add_comment(t["id"], "[fleet queue-request:{\"version\":1,\"observed_at\":1,\"targets\":[]}] idle")
        self.assertEqual(r.hold_reason(dict(mesh.tasks[t["id"]]), "work"), "")

    def test_a_queue_request_by_the_system_author_is_not_input(self):
        from mesh_runner.wake import SYSTEM_AUTHOR
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.runner(mesh)
        r.refresh_me()
        self.started(r, t)
        r._start_cap(dict(mesh.tasks[t["id"]]), 2)
        mesh.add_comment(t["id"], "[fleet queue-request:{\"version\":1,\"observed_at\":1,\"targets\":[]}] idle",
                         author=SYSTEM_AUTHOR)
        self.assertTrue(r.hold_reason(dict(mesh.tasks[t["id"]]), "work").startswith("waiting for new input"))

    def test_each_kind_of_input_counts(self):
        for label, change in (
                ("merge request pipeline", lambda m, t: m.links.update({t["id"]: [{"url": "u", "status": "open",
                                                                                     "pipeline_status": "failed"}]})),
                ("head commit", lambda m, t: m.links.update({t["id"]: [{"url": "u", "status": "open",
                                                                          "pipeline_status": "", "head_sha": "abc"}]})),
                ("dependency closed", lambda m, t: m.tasks[t["id"] + "-dep"].update(status_id="s-done")),
                ("assignee", lambda m, t: m.tasks[t["id"]].update(assignee_id="agent-reviewer"))):
            with self.subTest(label):
                self.setUp()
                t = task(1)
                dep = dict(task(2), id=t["id"] + "-dep")
                mesh = FakeMesh([t, dep], max_concurrent=1)
                mesh.links = {t["id"]: [{"url": "u", "status": "open", "pipeline_status": "running"}]}
                mesh.deps = {t["id"]: [{"depends_on_task_id": dep["id"], "dependency_type": "blocks"}]}
                r = self.runner(mesh)
                r.refresh_me()
                self.started(r, t)
                r._start_cap(dict(mesh.tasks[t["id"]]), 2)
                rec = r._cap_wait(t["id"])
                self.assertFalse(r._inputs_changed(dict(mesh.tasks[t["id"]]), rec))
                change(mesh, t)
                r._input_checked.clear()
                self.assertTrue(r._inputs_changed(dict(mesh.tasks[t["id"]]), rec))
                self.tmp.cleanup()

    def test_an_unreadable_fingerprint_is_not_news(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.runner(mesh)
        r.refresh_me()
        self.started(r, t)
        r._start_cap(dict(mesh.tasks[t["id"]]), 2)
        rec = r._cap_wait(t["id"])
        with mock.patch.object(mesh, "comments", side_effect=MeshError(503, "down")):
            self.assertFalse(r._inputs_changed(dict(mesh.tasks[t["id"]]), rec))

    def test_unreadable_inputs_at_the_cap_keep_the_older_path(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        self.agent.lead = "lead"
        r = self.runner(mesh)
        r.refresh_me()
        self.started(r, t)
        with mock.patch.object(mesh, "comments", side_effect=MeshError(503, "down")):
            r._start_cap(dict(mesh.tasks[t["id"]]), 2)
        self.assertEqual(mesh.tasks[t["id"]]["assignee_id"], "agent-lead")
        self.assertFalse(r._cap_wait(t["id"]))

    def test_a_failed_move_takes_the_hand_up_path_without_a_mark(self):
        t = task(1, cat="in_progress", start_after="2099-01-01T00:00:00Z")
        mesh = FakeMesh([t], max_concurrent=1)
        self.agent.lead = "lead"
        r = self.runner(mesh)
        r.refresh_me()
        self.started(r, t)
        real_move = mesh.move
        mesh.move = lambda tid, sid, **kw: (_ for _ in ()).throw(MeshError(503, "down")) \
            if mesh.tasks[tid]["assignee_id"] == AGENT_ID else real_move(tid, sid, **kw)
        r._start_cap(dict(mesh.tasks[t["id"]]), 2)
        self.assertFalse(r._cap_wait(t["id"]))
        self.assertEqual(self.notes(r, t), [])
        self.assertFalse(any("Back in the queue" in b for b in self.bodies(mesh, t)))
        self.assertEqual(mesh.tasks[t["id"]]["assignee_id"], "agent-lead")   # the old path ran

    def test_a_start_after_that_will_not_clear_does_not_send_the_task_up(self):
        t = task(1, cat="in_progress", start_after="2099-01-01T00:00:00Z")
        mesh = FakeMesh([t], max_concurrent=1)
        self.agent.lead = "lead"
        r = self.runner(mesh)
        r.refresh_me()
        self.started(r, t)
        mesh.patch_task = lambda tid, f: (_ for _ in ()).throw(MeshError(503, "down"))
        r._start_cap(dict(mesh.tasks[t["id"]]), 2)
        got = mesh.tasks[t["id"]]
        self.assertEqual((got["assignee_id"], mesh.category(got)), (AGENT_ID, "todo"))   # requeued in place
        self.assertTrue(r._cap_wait(t["id"]))
        self.assertIn("cap: requeued (start cap)", self.notes(r, t))
        # the clear is kept and retried by the tick, not given up
        self.assertIn(t["id"], r.store.marked("builder", UNSCHEDULE_MARK))

    def test_a_failed_move_after_a_turn_cap_keeps_the_attempt_for_the_old_path(self):
        t = task(1, cat="in_progress")
        mesh = FakeMesh([t], max_concurrent=1)
        self.agent.lead = "lead"
        r = self.runner(mesh)
        r.refresh_me()
        a = r.store.create("builder", t["id"], "claude")
        with mock.patch.object(mesh, "move", side_effect=MeshError(503, "down")):
            r._hand_up(a, dict(mesh.tasks[t["id"]]), "TURN CAP", "x", "turn budget")
        self.assertFalse(r._cap_wait(t["id"]))
        self.assertTrue(r.store.get(a.id).note.startswith("routed to lead"))
        self.assertFalse(any("Back in the queue" in b for b in self.bodies(mesh, t)))

    def test_a_task_waiting_for_input_sorts_after_ready_work_and_holds_no_slot(self):
        capped, ready = task(1, priority="urgent"), task(2, priority="low")
        mesh = FakeMesh([capped, ready], max_concurrent=1)
        r = self.runner(mesh)
        r.refresh_me()
        self.started(r, capped)
        r._start_cap(dict(mesh.tasks[capped["id"]]), 2)
        self.assertEqual([t["id"] for t in r.candidates()], [ready["id"], capped["id"]])
        self.assertEqual(r.store.active_count("builder"), 0)
        self.assertEqual(r.hold_reason(dict(mesh.tasks[ready["id"]]), "work"), "")

    def test_the_hold_reason_is_published_as_a_waiting_state(self):
        from mesh_runner.effects import skip_state
        self.assertEqual(skip_state("waiting for new input after start cap", "builder")["kind"], "waiting")

    def test_a_task_moved_by_a_cap_to_an_agent_is_not_moved_on_again_the_same_day(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        self.peer()
        mesh.agents.append({"id": "agent-peer", "name": "peer"})
        r = self.runner(mesh)
        r.refresh_me()
        self.started(r, t)
        r.store.mark("builder", t["id"], CAP_REASSIGNED, time.strftime("%Y-%m-%d", time.gmtime()))
        r._start_cap(dict(mesh.tasks[t["id"]]), 2)
        self.assertEqual(mesh.tasks[t["id"]]["assignee_id"], AGENT_ID)
        self.assertTrue(r._cap_wait(t["id"]))


class Peers(CapBench):
    def capped(self, mesh, t):
        r = self.runner(mesh)
        r.refresh_me()
        self.started(r, t)
        r._start_cap(dict(mesh.tasks[t["id"]]), 2)
        return r

    def mesh_with_peer(self, t):
        mesh = FakeMesh([t], max_concurrent=1)
        mesh.agents.append({"id": "agent-peer", "name": "peer", "max_concurrent_tasks": 1})
        return mesh

    def test_a_free_peer_of_the_same_repos_and_lead_gets_it_at_once(self):
        t = task(1)
        self.peer()
        mesh = self.mesh_with_peer(t)
        r = self.capped(mesh, t)
        self.assertEqual(mesh.tasks[t["id"]]["assignee_id"], "agent-peer")
        self.assertEqual(mesh.category(mesh.tasks[t["id"]]), "todo")
        self.assertIn("reassigned to peer", self.notes(r, t)[0])
        self.assertFalse(r._cap_wait(t["id"]))            # it is the peer's task now
        self.assertIn("reassigned to peer", self.bodies(mesh, t)[-1])
        self.assertEqual(r.store.mark("peer", t["id"], CAP_REASSIGNED)[0], time.strftime("%Y-%m-%d", time.gmtime()))

    def test_pool_name_overrides_the_repos_rule(self):
        t = task(1)
        self.agent.pool = "dev"
        self.peer(repos=[], pool="dev")
        mesh = self.mesh_with_peer(t)
        self.capped(mesh, t)
        self.assertEqual(mesh.tasks[t["id"]]["assignee_id"], "agent-peer")

    def test_a_peer_of_another_pool_or_lead_is_not_a_peer(self):
        for kw in ({"lead": "lead"}, {"repos": []}, {"pool": "other"}):
            with self.subTest(kw=kw):
                self.setUp()
                t = task(1)
                self.peer(**kw)
                mesh = self.mesh_with_peer(t)
                r = self.capped(mesh, t)
                self.assertEqual(mesh.tasks[t["id"]]["assignee_id"], AGENT_ID)
                self.assertTrue(r._cap_wait(t["id"]))
                self.tmp.cleanup()

    def test_a_paused_busy_capped_or_unserving_peer_is_skipped(self):
        def paused_file(r):
            (self.cfg.state_dir / "pause").mkdir(parents=True, exist_ok=True)
            (self.cfg.state_dir / "pause" / "peer").write_text("")

        def busy(r):
            a = r.store.create("peer", "other-task", "claude")
            r.store.update(a, state="running")

        def capped_on_it(r):
            self.started(r, task(1), 2, agent="peer")

        def waiting_on_it(r):
            r.store.mark("peer", task(1)["id"], CAP_WAIT, json.dumps({"kind": "start cap", "fp": {}, "at": "x"}))

        for label, prep, kw in (("pause file", paused_file, {}), ("config pause", lambda r: None, {"paused": True}),
                                ("no free slot", busy, {"max_parallel": 1}), ("capped itself", capped_on_it, {}),
                                ("waiting for input", waiting_on_it, {}),
                                ("another project", lambda r: None, {"policy": Policy(project_ids=("other",))})):
            with self.subTest(label):
                self.setUp()
                t = task(1)
                self.peer(**kw)
                mesh = self.mesh_with_peer(t)
                r = self.runner(mesh)
                r.refresh_me()
                prep(r)
                self.started(r, t)
                r._start_cap(dict(mesh.tasks[t["id"]]), 2)
                self.assertEqual(mesh.tasks[t["id"]]["assignee_id"], AGENT_ID)
                self.assertTrue(r._cap_wait(t["id"]))
                self.tmp.cleanup()

    def test_a_pinned_task_stays_and_waits_for_input(self):
        t = task(1, pinned=True)
        self.peer()
        mesh = self.mesh_with_peer(t)
        r = self.capped(mesh, t)
        self.assertEqual(mesh.tasks[t["id"]]["assignee_id"], AGENT_ID)
        self.assertTrue(r._cap_wait(t["id"]))
        self.assertNotIn("reassigned", self.bodies(mesh, t)[-1])


class OtherCaps(CapBench):
    def test_turn_budget_requeues_in_place(self):
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
        self.assertEqual(mesh.tasks[t["id"]]["assignee_id"], AGENT_ID)
        self.assertEqual(mesh.category(mesh.tasks[t["id"]]), "todo")
        self.assertIn("cap: requeued (turn budget)", self.notes(r, t))
        self.assertTrue(any("TURN CAP" in b for b in self.bodies(mesh, t)))
        self.assertNotIn(t["id"], r._task_backoff)
        self.assertNotIn(t["id"], r._budget_backoff)
        before = len(calls(self.calls))
        r.tick()
        r.tick()
        self.assertEqual(len(calls(self.calls)), before)              # nothing new: nothing starts

    def test_turn_cap_on_a_decision_wait_still_files_the_request(self):
        # The cap used to run before the ask was routed: the card went back to todo waiting for
        # "new input", but the addressee was never asked, so nothing could ever arrive.
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        self.cfg.daily_next_budget = 2
        self.cfg.daily_wait_budget = 0
        self.peer("lead", repos=[])                    # an addressee this runner serves
        write_replies(self.replies, {t["id"][:8]: "⏳ WAIT decision:@lead: approve the plan?"})
        r = self.runner(mesh)
        r.tick()
        self.finish_turn(r)
        self.assertEqual(len(mesh.created), 1)
        req = mesh.created[0]
        self.assertEqual(req["assignee_id"], "agent-lead")
        self.assertIn("decision-request", req["labels"])
        self.assertIn("cap: requeued (turn budget)", self.notes(r, t))
        self.assertEqual(mesh.category(mesh.tasks[t["id"]]), "todo")
        mesh.add_comment(t["id"], "approved", author="agent-lead")
        self.assertNotIn("waiting for new input", r.hold_reason(dict(mesh.tasks[t["id"]]), "work") or "")

    def test_turn_cap_files_the_request_when_the_agent_shares_its_slug_with_a_service_user(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        self.cfg.daily_next_budget = 2
        self.cfg.daily_wait_budget = 0
        self.peer("lead", repos=[])
        lead_id = next(a["id"] for a in mesh.agents if a["name"] == "lead")
        mesh.recipients = [
            {"id": lead_id, "kind": "agent", "slug": "lead", "display_name": "lead"},
            {"id": "u-lead", "kind": "user", "slug": "lead", "display_name": "lead (QA)",
             "is_service_user": True, "agent_id": lead_id}]
        write_replies(self.replies, {t["id"][:8]: "⏳ WAIT decision:@lead: approve the plan?"})
        r = self.runner(mesh)
        r.tick()
        self.finish_turn(r)
        self.assertEqual(len(mesh.created), 1)

    def test_turn_cap_does_not_file_a_request_under_a_human_gate(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        self.cfg.daily_next_budget = 2
        self.cfg.daily_wait_budget = 0
        self.peer("lead", repos=[])
        write_replies(self.replies, {t["id"][:8]: "⏳ WAIT decision:@lead: approve the plan?"})
        r = self.runner(mesh)
        r.tick()
        mesh.tasks[t["id"]]["human_gate"] = True
        self.finish_turn(r)
        self.assertEqual(mesh.created, [])

    def test_turn_cap_on_a_person_ask_does_not_raise_anything(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        self.cfg.daily_next_budget = 2
        self.cfg.daily_wait_budget = 0
        write_replies(self.replies, {t["id"][:8]: "⏳ WAIT decision:@owner: which plan?"})
        r = self.runner(mesh)
        r.tick()
        self.finish_turn(r)
        self.assertEqual(mesh.created, [])
        self.assertNotIn("Blocking", " ".join(self.bodies(mesh, t)))
        self.assertIn("cap: requeued (turn budget)", self.notes(r, t))

    def test_compaction_cap_requeues_in_place(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        self.cfg.max_compactions = 2
        self.agent.lead = "lead"
        write_replies(self.replies, {t["id"][:8]: "COMPACT:2:⏳ WAIT 2h: soak window"})
        r = self.runner(mesh)
        r.tick()
        self.settle_all(r)
        self.assertEqual(mesh.tasks[t["id"]]["assignee_id"], AGENT_ID)
        self.assertIn("cap: requeued (compaction cap)", self.notes(r, t))
        self.assertTrue(any("COMPACTION CAP" in b for b in self.bodies(mesh, t)))

    def test_gave_up_still_goes_to_the_lead(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        self.agent.lead = "lead"
        r = self.runner(mesh)
        r.refresh_me()
        a = r.store.create("builder", t["id"], "claude")
        r._give_up(a, dict(mesh.tasks[t["id"]]), "no readable outcome")
        self.assertEqual(mesh.tasks[t["id"]]["assignee_id"], "agent-lead")
        self.assertIn("routed to lead", r.store.get(a.id).note)
        self.assertFalse(r._cap_wait(t["id"]))

    def test_cap_wake_next_day_restarts_a_leadless_agent_only_on_a_new_utc_day(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        self.cfg.cap_wake_next_day = True
        r = self.runner(mesh)
        r.refresh_me()
        self.started(r, t)
        r._start_cap(dict(mesh.tasks[t["id"]]), 2)
        cur = dict(mesh.tasks[t["id"]])
        self.assertIn("waiting for new input", r.hold_reason(cur, "work"))
        rec = json.loads(r.store.mark("builder", t["id"], CAP_WAIT)[0])
        rec["at"] = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)).isoformat()
        r.store.mark("builder", t["id"], CAP_WAIT, json.dumps(rec))
        with self.assertLogs("mesh_runner", "INFO") as lg:
            self.assertEqual(r.hold_reason(cur, "work"), "")
        self.assertTrue(any("cap wake" in m for m in lg.output))
        self.agent.lead = "lead"                       # a lead takes the cap path, no clock
        self.assertIn("waiting for new input", r.hold_reason(cur, "work"))
        self.agent.lead = ""
        self.cfg.cap_wake_next_day = False             # rollback flag
        self.assertIn("waiting for new input", r.hold_reason(cur, "work"))


class FlagOff(CapBench):
    def setUp(self):
        super().setUp()
        self.cfg.requeue_on_cap = False

    def test_start_cap_hands_to_the_lead_as_before(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        self.agent.lead = "lead"
        r = self.runner(mesh)
        r.refresh_me()
        self.started(r, t)
        with mock.patch.object(r, "_requeue_on_event") as requeue:
            r._start_cap(dict(mesh.tasks[t["id"]]), 2)
        requeue.assert_not_called()
        self.assertEqual(mesh.tasks[t["id"]]["assignee_id"], "agent-lead")
        self.assertFalse(r._cap_wait(t["id"]))

    def test_start_cap_without_a_lead_keeps_it_until_tomorrow_as_before(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.runner(mesh)
        r.refresh_me()
        self.started(r, t)
        r._start_cap(dict(mesh.tasks[t["id"]]), 2)
        self.assertGreater(r._task_backoff[t["id"]], time.monotonic() + 60)
        self.assertIn("Not started again before tomorrow", self.bodies(mesh, t)[0])

    def test_turn_budget_routes_to_the_lead_as_before(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        self.cfg.daily_next_budget = 2
        self.agent.lead = "lead"
        write_replies(self.replies, {t["id"][:8]: "↻ NEXT"})
        r = self.runner(mesh)
        r.tick()
        for turn in range(3):
            self.finish_turn(r)
            if turn < 2:
                r.tick()
        self.assertEqual(mesh.tasks[t["id"]]["assignee_id"], "agent-lead")
        self.assertTrue(any(a.note.startswith("routed to lead") for a in r.store.by_state("builder", ("completed",))))

    def test_old_marks_do_not_hold_when_the_flag_is_off(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.runner(mesh)
        r.refresh_me()
        r.store.mark("builder", t["id"], CAP_WAIT, json.dumps({"kind": "start cap", "fp": {}, "at": "x"}))
        self.assertEqual(r.hold_reason(dict(mesh.tasks[t["id"]]), "work"), "")
        self.assertEqual([x["id"] for x in r.candidates()], [t["id"]])


class StartCapStaleSnapshot(CapBench):
    def test_second_hand_up_from_a_stale_snapshot_is_skipped(self):
        t = task(7, cat="todo")
        mesh = FakeMesh([t], max_concurrent=1)
        self.cfg.requeue_on_cap = False
        self.agent.lead = "lead"
        r = self.runner(mesh)
        r.refresh_me()
        self.started(r, t)
        stale = dict(mesh.tasks[t["id"]])
        mesh.tasks[t["id"]]["assignee_id"] = "someone-else"      # the first hand-off already landed
        r._start_cap(stale, 2)
        self.assertEqual(mesh.tasks[t["id"]]["assignee_id"], "someone-else")
        self.assertEqual(self.bodies(mesh, t), [])

    def test_unassigned_task_is_skipped(self):
        for missing in (None, ""):
            with self.subTest(assignee=missing):
                t = task(9, cat="todo")
                mesh = FakeMesh([t], max_concurrent=1)
                self.cfg.requeue_on_cap = False
                self.agent.lead = "lead"
                r = self.runner(mesh)
                r.refresh_me()
                self.started(r, t)
                stale = dict(mesh.tasks[t["id"]])
                mesh.tasks[t["id"]]["assignee_id"] = missing
                r._start_cap(stale, 2)
                self.assertEqual(mesh.tasks[t["id"]]["assignee_id"], missing)
                self.assertEqual(self.bodies(mesh, t), [])

    def test_fresh_read_showing_this_agent_hands_up_exactly_once(self):
        t = task(10, cat="todo")
        mesh = FakeMesh([t], max_concurrent=1)
        self.cfg.requeue_on_cap = False
        self.agent.lead = "lead"
        r = self.runner(mesh)
        r.refresh_me()
        self.started(r, t)
        self.assertEqual(mesh.tasks[t["id"]]["assignee_id"], AGENT_ID)
        r._start_cap(dict(mesh.tasks[t["id"]]), 2)
        self.assertEqual(mesh.tasks[t["id"]]["assignee_id"], "agent-lead")
        self.assertEqual(len(self.bodies(mesh, t)), 1)
        self.assertIn("START CAP", self.bodies(mesh, t)[0])

    def test_unreadable_task_defers_the_hand_up(self):
        t = task(11, cat="todo")
        mesh = FakeMesh([t], max_concurrent=1)
        self.cfg.requeue_on_cap = False
        self.agent.lead = "lead"
        r = self.runner(mesh)
        r.refresh_me()
        self.started(r, t)
        with mock.patch.object(mesh, "task", side_effect=MeshError(503, "down")):
            r._start_cap(dict(mesh.tasks[t["id"]]), 2)
        self.assertEqual(mesh.tasks[t["id"]]["assignee_id"], AGENT_ID)
        self.assertEqual(self.bodies(mesh, t), [])
        r._start_cap(dict(mesh.tasks[t["id"]]), 2)              # readable again: hands up once
        self.assertEqual(mesh.tasks[t["id"]]["assignee_id"], "agent-lead")
        self.assertEqual(len(self.bodies(mesh, t)), 1)

    def test_checked_out_by_another_writer_is_skipped(self):
        t = task(8, cat="todo")
        mesh = FakeMesh([t], max_concurrent=1)
        self.cfg.requeue_on_cap = False
        r = self.runner(mesh)
        r.refresh_me()
        self.started(r, t)
        mesh.tasks[t["id"]]["checked_out_by"] = "other-agent"
        r._start_cap(dict(mesh.tasks[t["id"]]), 2)
        self.assertEqual(self.bodies(mesh, t), [])
