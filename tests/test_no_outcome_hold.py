"""A turn that ends without a readable outcome: an unsourced time wait is corrected once and then
held for correction, `until=` without `after=` takes the declared state as baseline,
and consecutive misses wait for new input (no timer) and route to the lead only on the third in a row,
never twice for the same run of misses."""
import datetime as dt
from unittest import mock

from fakes import AGENT_ID, FakeGitLab, FakeMesh, calls, task, write_replies
from test_requeue_on_cap import CapBench
from test_runner import Bench

from mesh_runner.outcome import TIME_WAIT_GUIDANCE, parse_outcome

SHA = "a" * 40
NOW = dt.datetime(2026, 10, 28, 12, 0, tzinfo=dt.timezone.utc)


def p(text):
    return parse_outcome(text, NOW)


def row(pid=7, status="running", sha=SHA, state="opened"):
    return {"sha": sha, "state": state, "head_pipeline": {"sha": sha, "id": pid, "status": status}}


class UnsourcedTimeWait(Bench):
    """1. A time WAIT without a sourced external timestamp is unreadable: a relative interval, a
    reason naming an event, `poll:`, a bare date and a moment already past are all refused with
    the same guidance (which names the event forms), never scheduled and never turned into NEXT."""

    LINE = "⏳ WAIT 20m: идёт независимое ревью PR77, закончится вердиктом SHIP"

    def test_every_unsourced_form_is_refused_with_the_guidance(self):
        for line in (self.LINE, "⏳ WAIT 6h: soak window", "⏳ WAIT 6h: poll: Telegram inbox for the reply",
                     "⏳ WAIT 2026-10-30T06:00Z: decision from the lead", "⏳ WAIT 2026-10-28: today, date only",
                     "⏳ WAIT 2026-10-28T07:05Z: external=night-run source=card:#abcdef12 already past",
                     "⏳ WAIT 2026-10-28T12:00Z: external=night-run source=card:#abcdef12 exactly now",
                     "⏳ WAIT tomorrow", "⏳ WAIT in 2 hours: the export", "⏳ WAIT until the vendor replies"):
            o = p(line)
            self.assertEqual((o.kind, o.until, o.note), ("unparsed", None, TIME_WAIT_GUIDANCE), line)
        for form in ("⏳ WAIT card:#<8hex>", "⏳ WAIT decision:@<agent>", "⏳ WAIT mr:<group>/<project>!<iid>",
                     "⏳ WAIT pipeline:<group>/<project>#<id>"):
            self.assertIn(form, TIME_WAIT_GUIDANCE)

    def test_a_malformed_event_wait_keeps_its_own_reading(self):
        for line in ("⏳ WAIT card:xyz", "⏳ WAIT decision: someone", "⏳ WAIT mr: soon"):
            self.assertNotEqual(p(line).note, TIME_WAIT_GUIDANCE, line)

    def test_a_sourced_moment_just_ahead_is_kept_exactly(self):
        o = p("⏳ WAIT 2026-10-28T12:02Z: external=night-run source=card:#abcdef12 the export starts")
        self.assertEqual((o.kind, o.until), ("time", NOW + dt.timedelta(minutes=2)))

    def test_the_first_is_corrected_in_the_same_session_and_schedules_nothing(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        write_replies(self.replies, {t["id"][:8]: self.LINE})
        r = self.runner(mesh)
        r.tick()
        a = self.finish_turn(r)
        self.assertEqual((a.state, a.no_outcome), ("ready", 1))
        self.assertIn("external=<stable-reference>", a.wake_text)
        self.assertEqual(a.wait, {})
        self.assertFalse(mesh.tasks[t["id"]].get("start_after"))

    def test_repeating_it_after_the_correction_is_a_correction_hold(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        line = "⏳ WAIT tomorrow: later"
        write_replies(self.replies, {t["id"][:8]: line, t["id"][:8] + ":resume": line})
        r = self.runner(mesh)
        r.tick()
        self.settle_all(r)
        a = r.store.by_state(self.agent.name, ("failed",))[0]
        self.assertTrue(a.note.startswith("wait correction required:"), a.note)
        self.assertTrue(any(c["body"].startswith("[runner] ⚠️ WAIT CORRECTION REQUIRED") for c in mesh.comment_log[t["id"]]))
        self.assertFalse(mesh.tasks[t["id"]].get("start_after"))
        self.assertEqual([c["resume"] for c in calls(self.calls)], [False, True])


class CiWaitWithoutBaseline(Bench):
    """3. `until=` without `after=` takes the state at the moment of declaring as its baseline."""

    def test_parser_marks_the_default(self):
        o = p("⏳ WAIT pipeline:g/app#18827 until=success")
        self.assertEqual((o.kind, o.target, o.extra), ("pipeline", "g/app#18827", {"ci_default": {"predicate": "success"}}))
        o = p("⏳ WAIT pipeline:g/app#18827 until=change: CI")
        self.assertEqual((o.kind, o.extra["ci_default"]["predicate"], o.note), ("pipeline", "change", "CI"))
        o = p("⏳ WAIT mr:g/app!9 until=success")
        self.assertEqual((o.kind, o.target, o.extra), ("mr", "g/app!9", {"ci_default": {"predicate": "success"}}))
        o = p("⏳ WAIT mr:g/app!9 until=merged")
        self.assertEqual((o.kind, o.extra), ("mr", {"mr_default": {"predicate": "merged"}}))

    def test_malformed_typed_waits_are_still_refused(self):
        for line in ("⏳ WAIT pipeline:g/app#1 until=success after=nonsense",
                     "⏳ WAIT pipeline:g/app#1 until=whenever",
                     "⏳ WAIT mr:g/app#9 until=success",
                     "⏳ WAIT pipeline:g/app#1 after=" + SHA + ":1:running"):
            self.assertEqual(p(line).kind, "unparsed", line)

    def test_full_form_is_unchanged(self):
        o = p(f"⏳ WAIT pipeline:g/app#7 until=success after={SHA}:7:running")
        self.assertEqual(o.extra["ci_change"]["status"], "running")
        self.assertNotIn("ci_default", o.extra)

    def test_pipeline_baseline_is_its_state_now(self):
        gl = FakeGitLab()
        gl.pipeline_rows["g/app#7"] = {"sha": SHA, "id": 7, "status": "running"}
        r = self.runner(FakeMesh([task(1)], max_concurrent=1), gl)
        o = r._parse("⏳ WAIT pipeline:g/app#7 until=success", NOW)
        self.assertEqual(o.extra, {"ci_change": {"predicate": "success", "sha": SHA, "pipeline": 7,
                                                  "status": "running"}})
        o = r._parse("⏳ WAIT pipeline:g/app#7 until=change", NOW)
        self.assertEqual(o.extra["ci_change"]["predicate"], "change")

    def test_mr_baseline_is_the_head_pipeline_now(self):
        gl = FakeGitLab()
        gl.mr_rows["g/app!9"] = row(pid=11, status="pending")
        r = self.runner(FakeMesh([task(1)], max_concurrent=1), gl)
        o = r._parse("⏳ WAIT mr:g/app!9 until=success", NOW)
        self.assertEqual(o.extra["ci_change"], {"predicate": "success", "sha": SHA, "pipeline": 11, "status": "pending"})
        o = r._parse("⏳ WAIT mr:g/app!9 until=merged", NOW)
        self.assertEqual(o.extra["mr_state"], {"predicate": "merged", "sha": SHA, "state": "opened"})

    def test_a_result_that_already_satisfies_success_is_the_plain_wait(self):
        gl = FakeGitLab()
        gl.pipeline_rows["g/app#7"] = {"sha": SHA, "id": 7, "status": "success"}
        r = self.runner(FakeMesh([task(1)], max_concurrent=1), gl)
        o = r._parse("⏳ WAIT pipeline:g/app#7 until=success", NOW)
        self.assertEqual((o.kind, o.target, o.extra), ("pipeline", "g/app#7", {}))

    def test_a_pipeline_already_in_any_final_state_is_the_plain_wait(self):
        for status in ("success", "failed", "canceled", "cancelled", "skipped", "manual"):
            for predicate in ("success", "change"):
                with self.subTest(status=status, predicate=predicate):
                    gl = FakeGitLab()
                    gl.pipeline_rows["g/app#7"] = {"sha": SHA, "id": 7, "status": status}
                    r = self.runner(FakeMesh([task(1)], max_concurrent=1), gl)
                    o = r._parse(f"⏳ WAIT pipeline:g/app#7 until={predicate}", NOW)
                    self.assertEqual((o.kind, o.target, o.extra), ("pipeline", "g/app#7", {}))

    def test_a_non_final_pipeline_keeps_the_typed_baseline(self):
        for status in ("created", "waiting_for_resource", "preparing", "pending", "running", "scheduled"):
            with self.subTest(status=status):
                gl = FakeGitLab()
                gl.pipeline_rows["g/app#7"] = {"sha": SHA, "id": 7, "status": status}
                r = self.runner(FakeMesh([task(1)], max_concurrent=1), gl)
                o = r._parse("⏳ WAIT pipeline:g/app#7 until=change", NOW)
                self.assertEqual(o.extra["ci_change"]["status"], status)

    def test_an_mr_whose_head_pipeline_is_final_is_the_plain_wait(self):
        for status in ("success", "failed", "canceled", "skipped", "manual"):
            for predicate in ("success", "change"):
                with self.subTest(status=status, predicate=predicate):
                    gl = FakeGitLab()
                    gl.mr_rows["g/app!9"] = row(pid=11, status=status)
                    r = self.runner(FakeMesh([task(1)], max_concurrent=1), gl)
                    o = r._parse(f"⏳ WAIT mr:g/app!9 until={predicate}", NOW)
                    self.assertEqual((o.kind, o.target, o.extra), ("mr", "g/app!9", {}))

    def test_an_mr_already_merged_or_closed_is_the_plain_wait(self):
        for state in ("merged", "closed"):
            for line in ("until=change", "until=success", "until=merged"):
                with self.subTest(state=state, line=line):
                    gl = FakeGitLab()
                    gl.mr_rows["g/app!9"] = row(pid=11, status="running", state=state)
                    r = self.runner(FakeMesh([task(1)], max_concurrent=1), gl)
                    o = r._parse(f"⏳ WAIT mr:g/app!9 {line}", NOW)
                    self.assertEqual((o.kind, o.target, o.extra), ("mr", "g/app!9", {}))

    def test_unreadable_target_or_no_gitlab_falls_back_to_the_plain_wait(self):
        r = self.runner(FakeMesh([task(1)], max_concurrent=1), FakeGitLab())      # nothing known about the target
        for line, kind in (("⏳ WAIT pipeline:g/app#7 until=success", "pipeline"),
                           ("⏳ WAIT mr:g/app!9 until=success", "mr"),
                           ("⏳ WAIT mr:g/app!9 until=merged", "mr")):
            o = r._parse(line, NOW)
            self.assertEqual((o.kind, o.extra), (kind, {}), line)
        r = self.runner(FakeMesh([task(1)], max_concurrent=1))                    # no GitLab at all
        self.assertEqual(r._parse("⏳ WAIT pipeline:g/app#7 until=success", NOW).extra, {})

    def test_the_session_waits_on_the_pipeline(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        gl = FakeGitLab()
        gl.pipelines["g/app#7"] = "running"
        gl.pipeline_rows["g/app#7"] = {"sha": SHA, "id": 7, "status": "running"}
        write_replies(self.replies, {t["id"][:8]: "⏳ WAIT pipeline:g/app#7 until=success"})
        r = self.runner(mesh, gl)
        r.tick()
        a = self.finish_turn(r)
        self.assertEqual((a.state, a.wait["kind"], a.wait["target"]), ("waiting", "pipeline", "g/app#7"))
        self.assertEqual(a.wait["ci_change"], {"predicate": "success", "sha": SHA, "pipeline": 7, "status": "running"})
        self.assertEqual(len(calls(self.calls)), 1)           # no correction turn


class NoOutcomeRun(CapBench):
    """4. Misses in a row: correct once, then wait for new input; the third in a row goes to the lead."""

    def setUp(self):
        super().setUp()
        self.cfg.daily_start_cap = 0

    def silent(self, t, text="I did some work."):
        write_replies(self.replies, {t["id"][:8]: text})

    def two_misses(self, r):
        r.tick()
        self.assertEqual(self.finish_turn(r).state, "ready")          # miss 1: corrected in the same session
        r.tick()
        return self.finish_turn(r)                                    # miss 2

    def test_second_miss_waits_for_new_input_with_the_same_lane_and_no_timer(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        self.agent.lead = "lead"
        self.silent(t)
        r = self.runner(mesh)
        with mock.patch.object(r, "_free_peer", side_effect=AssertionError("must keep the lane")):
            a = self.two_misses(r)
        self.assertEqual((a.state, a.note), ("completed", "cap: requeued (no outcome)"))
        got = mesh.tasks[t["id"]]
        self.assertEqual((got["assignee_id"], mesh.category(got), got.get("start_after")), (AGENT_ID, "todo", None))
        self.assertEqual(r.store.by_state("builder", ("waiting",)), [])            # no timer wait
        self.assertEqual(r._cap_wait(t["id"])["kind"], "no outcome")
        self.assertIn("waiting for new input", r.hold_reason(dict(got), "work"))
        body = " ".join(self.bodies(mesh, t))
        self.assertIn("NO OUTCOME", body)
        self.assertIn("Nothing restarts it on a timer", body)
        self.assertNotIn("RUNNER GAVE UP", body)
        r.tick()                                                      # nothing new: nothing starts
        self.assertEqual(len(calls(self.calls)), 2)

    def test_new_input_restarts_it_and_the_third_miss_in_a_row_goes_to_the_lead(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        self.agent.lead = "lead"
        self.silent(t)
        r = self.runner(mesh)
        self.two_misses(r)
        mesh.add_comment(t["id"], "please look again", author="someone")
        r.tick()
        self.finish_turn(r)                                           # miss 3 in a row
        self.assertEqual(mesh.tasks[t["id"]]["assignee_id"], "agent-lead")
        body = " ".join(self.bodies(mesh, t))
        self.assertIn("RUNNER GAVE UP", body)
        self.assertIn("3 turns in a row", body)

    def test_the_threshold_is_a_knob(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        self.agent.lead = "lead"
        self.cfg.no_outcome_route_after = 2
        self.silent(t)
        self.two_misses(self.runner(mesh))
        self.assertEqual(mesh.tasks[t["id"]]["assignee_id"], "agent-lead")

    def test_misses_separated_by_a_readable_outcome_never_route(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        self.agent.lead = "lead"
        self.cfg.no_outcome_route_after = 2                           # two in a row would already route
        r = self.runner(mesh)
        self.silent(t)
        r.tick()
        self.assertEqual(self.finish_turn(r).state, "ready")          # miss
        for _ in range(3):
            self.silent(t, "↻ NEXT: did a piece")
            r.tick()
            self.assertEqual(self.finish_turn(r).state, "ready")      # readable outcome resets the run
            self.silent(t)
            r.tick()
            self.assertEqual(self.finish_turn(r).state, "ready")      # a miss again: the first of a new run
        self.assertEqual(mesh.tasks[t["id"]]["assignee_id"], AGENT_ID)
        self.assertNotIn("RUNNER GAVE UP", " ".join(self.bodies(mesh, t)))
        self.assertEqual(r.store.events_since_readable(t["id"], "no_outcome"), 1)

    def test_misses_are_counted_across_lanes(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        self.agent.lead = "lead"
        r = self.runner(mesh)
        other = r.store.create("someone-else", t["id"], "claude")
        for _ in range(2):
            r.store.event(other.id, "no_outcome", "x")
        self.silent(t)
        r.tick()
        self.finish_turn(r)                                           # the 3rd in a row on this card
        self.assertEqual(mesh.tasks[t["id"]]["assignee_id"], "agent-lead")

    def test_a_lead_that_receives_such_a_card_does_not_hand_it_further_up(self):
        """The card went up for a missing outcome (by another lane); this lane is the receiving lead.
        However many turns it misses, the card stays here: no reassignment chain."""
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        self.agent.lead = "lead"                                      # there is a lead above this lane
        r = self.runner(mesh)
        previous = r.store.create("junior", t["id"], "claude")
        for _ in range(3):
            r.store.event(previous.id, "no_outcome", "x")
        r.store.event(previous.id, "no_outcome_routed", "3 turns in a row")
        self.silent(t)
        for _ in range(3):
            r.tick()
            self.assertEqual(self.finish_turn(r).state, "ready")      # corrected in the same session
            r.tick()
            self.assertEqual(self.finish_turn(r).note, "cap: requeued (no outcome)")   # then waits for input
            mesh.add_comment(t["id"], "new input", author="someone")
        self.assertEqual(mesh.tasks[t["id"]]["assignee_id"], AGENT_ID)
        self.assertNotIn("RUNNER GAVE UP", " ".join(self.bodies(mesh, t)))

    def test_without_requeue_a_routed_card_stays_in_todo_and_an_unrouted_one_goes_up(self):
        self.cfg.requeue_on_cap = False                               # no wait-for-input mechanism: as before
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        self.agent.lead = "lead"
        self.silent(t)
        self.two_misses(self.runner(mesh))
        self.assertEqual(mesh.tasks[t["id"]]["assignee_id"], "agent-lead")
        t2 = task(2)
        mesh2 = FakeMesh([t2], max_concurrent=1)
        r2 = self.runner(mesh2)
        previous = r2.store.create("junior", t2["id"], "claude")
        r2.store.event(previous.id, "no_outcome", "x")
        r2.store.event(previous.id, "no_outcome_routed", "x")
        write_replies(self.replies, {t2["id"][:8]: "nothing"})
        r2.tick()
        self.assertEqual(self.finish_turn(r2).state, "ready")
        r2.tick()
        self.assertEqual(self.finish_turn(r2).state, "failed")
        self.assertEqual(mesh2.tasks[t2["id"]]["assignee_id"], AGENT_ID)
        self.assertNotIn("RUNNER GAVE UP", " ".join(self.bodies(mesh2, t2)))

    def test_a_card_nobody_could_take_is_not_marked_as_routed(self):
        self.cfg.requeue_on_cap = False
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)                        # no lead above this lane
        self.cfg.no_outcome_route_after = 2
        self.silent(t)
        r = self.runner(mesh)
        self.two_misses(r)
        self.assertEqual(self.states(r)[t["id"][:8]], "failed")
        self.assertEqual(r.store.events_since_readable(t["id"], "no_outcome_routed"), 0)

    def test_the_routed_marker_survives_a_readable_outcome(self):
        t = task(1)
        r = self.runner(FakeMesh([t], max_concurrent=1))
        a = r.store.create("lead", t["id"], "claude")
        r.store.event(a.id, "no_outcome_routed", "x")
        r.store.event(a.id, "readable_outcome")
        self.assertEqual(r.store.events_since_readable(t["id"], "no_outcome_routed"), 0)
        self.assertEqual(r.store.events_on_task(t["id"], "no_outcome_routed"), 1)

    def test_the_run_is_reset_by_a_readable_outcome_in_the_store(self):
        t = task(1)
        r = self.runner(FakeMesh([t], max_concurrent=1))
        a = r.store.create("builder", t["id"], "claude")
        r.store.event(a.id, "no_outcome", "")
        r.store.event(a.id, "no_outcome", "")
        self.assertEqual(r.store.events_since_readable(t["id"], "no_outcome"), 2)
        r.store.event(a.id, "readable_outcome")
        self.assertEqual(r.store.events_since_readable(t["id"], "no_outcome"), 0)
        r.store.event(a.id, "no_outcome", "")
        self.assertEqual(r.store.events_since_readable(t["id"], "no_outcome"), 1)
        self.assertEqual(r.store.events_since_readable("other-task", "no_outcome"), 0)


if __name__ == "__main__":
    import unittest
    unittest.main()
