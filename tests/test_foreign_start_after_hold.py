"""A future start_after that no wait of this runner set holds the card at every point a turn could run,
not only at selection (a card its owner scheduled could start seconds after the
date was written, and a run of turns without an outcome could take the date back through the requeue).

- the turn end (no outcome, unparsed, NEXT) closes the attempt and keeps the session, as ``park:*`` does;
- a ready attempt (NEXT, reprompt, a wake) is not resumed before the date;
- the start re-reads the card after its checkout: a date written after the selection's read holds it;
- the requeue (caps, no-outcome, gave-up) takes back only the dates the runner's own waits wrote;
- after the date the ordinary start resumes the same session once;
- the runner's own time wait is still taken back at its end and holds nothing.
"""
import datetime as dt
import json
from unittest import mock

from fakes import AGENT_ID, FakeMesh, calls, next_turn, task, wait_card, wait_manual, wait_time, write_replies
from test_runner import Bench
from test_gate_owner_admission import info
from test_start_after_contract import patches_of
from test_unschedule_retry import RealisticMesh, conflict, mesh_time

from mesh_runner.effects import UNSCHEDULE_MARK
from mesh_runner.store import iso, now

LATER = dt.timedelta(days=7)
# the resumed turn after the date ends on an outcome that settles it (a later external moment)
AFTER = wait_time(why="the vendor window after the date ends")


def scheduled_by_someone(mesh, tid, when=None, origin="agent_evidence"):
    """The card's owner (or a person) wrote a start_after through the API, outside any runner wait."""
    when = when or mesh_time(iso(now() + LATER))
    mesh.tasks[tid].update(start_after=when, start_after_origin=origin, start_after_receipt_id="rcpt-owner",
                           version=mesh.tasks[tid].get("version", 1) + 1)
    return when


def compare_clears(mesh):
    return [b for o, _, b in mesh.start_after_calls if o == "compare-clear"]


class ForeignScheduleHold(Bench):
    def assert_held(self, r, t, a, where, when, logs, missed=False):
        ended = r.store.get(a.id)
        self.assertEqual(ended.state, "completed")
        self.assertTrue(ended.note.startswith("held: start_after "), ended.note)
        events = r.store.events(a.id)
        self.assertEqual([json.loads(d)["at"] for _, k, d in events if k == "schedule_held"], [where])
        # a hold is no miss (``missed``: a turn without an outcome before the date was, and is not counted again)
        self.assertEqual([k for _, k, _ in events].count("no_outcome"), 1 if missed else 0)
        mark = json.loads(r.store.mark(self.agent.name, t["id"], "ownership_loss")[0])
        self.assertEqual((mark["attempt"], mark["session_id"]), (a.id, a.session_id))   # the session is kept
        self.assertTrue(mark["session_id"])
        self.assertEqual(r.mesh.tasks[t["id"]]["start_after"], when)        # the date is untouched
        self.assertEqual(compare_clears(r.mesh), [])
        self.assertEqual(r.mesh.tasks[t["id"]]["assignee_id"], AGENT_ID)
        self.assertTrue(any(f"held: start_after " in line and f"({where}" in line for line in logs.output),
                        logs.output)
        self.assertEqual(r.store.starts_today(self.agent.name, t["id"]), 1)  # only the turn that ran
        before = len(calls(self.calls))
        r.tick()                                                             # the start path holds it too
        self.assertEqual(len(calls(self.calls)), before)
        self.assertEqual(r.store.by_state(self.agent.name, ("starting", "running", "ready", "waiting")), [])

    def after_the_date_once(self, r, t, first):
        """The date passes, nothing else changes: one start, in the same session, the date never cleared."""
        before = len(calls(self.calls))
        later = lambda: dt.datetime.now(dt.timezone.utc) + LATER + dt.timedelta(hours=1)   # noqa: E731
        with mock.patch("mesh_runner.runner.now", side_effect=later):
            r.tick()
            resumed = self.finish_turn(r)
            r.tick()
        self.assertNotEqual(resumed.id, first.id)                            # a new attempt by the ordinary start
        started = calls(self.calls)[before:]
        self.assertEqual([c["resume"] for c in started], [True])
        self.assertEqual(started[0]["sid"], first.session_id)
        ret = [json.loads(d) for _, k, d in r.store.events(resumed.id) if k == "ownership_return"]
        self.assertEqual((ret[0]["intent"], ret[0]["reason"]), ("resume", "ownership_return_same_lane"))
        self.assertEqual(compare_clears(r.mesh), [])                         # someone's date is never cleared

    def test_a_turn_without_an_outcome_on_a_card_scheduled_meanwhile_is_held_not_corrected(self):
        t = task(1)
        write_replies(self.replies, {t["id"][:8]: "I did some work.",
                                     t["id"][:8] + ":resume": AFTER})
        r = self.runner(FakeMesh([t], max_concurrent=1))
        r.tick()
        status = r.mesh.tasks[t["id"]]["status_id"]
        when = scheduled_by_someone(r.mesh, t["id"])                         # written while the turn ran
        with self.assertLogs("mesh_runner", "INFO") as logs:
            first = self.finish_turn(r)
        self.assertEqual(r.mesh.tasks[t["id"]]["status_id"], status)        # the card keeps its status
        self.assertEqual([c["resume"] for c in calls(self.calls)], [False])  # no "ended without an outcome" turn
        self.assert_held(r, t, first, "turn end", when, logs)
        self.after_the_date_once(r, t, first)

    def test_next_on_a_card_scheduled_meanwhile_does_not_resume_it(self):
        t = task(2)
        write_replies(self.replies, {t["id"][:8]: next_turn("more to do"),
                                     t["id"][:8] + ":resume": AFTER})
        r = self.runner(FakeMesh([t], max_concurrent=1))
        r.tick()
        when = scheduled_by_someone(r.mesh, t["id"])
        with self.assertLogs("mesh_runner", "INFO") as logs:
            first = self.finish_turn(r)
        self.assertEqual([c["resume"] for c in calls(self.calls)], [False])
        self.assert_held(r, t, first, "turn end", when, logs)
        self.after_the_date_once(r, t, first)

    def test_an_unparsed_outcome_on_a_card_scheduled_meanwhile_is_held_not_corrected(self):
        t = task(13)
        write_replies(self.replies, {t["id"][:8]: wait_manual(""),          # no note: a precise unparsed
                                     t["id"][:8] + ":resume": AFTER})
        r = self.runner(FakeMesh([t], max_concurrent=1))
        r.tick()
        when = scheduled_by_someone(r.mesh, t["id"])
        with self.assertLogs("mesh_runner", "INFO") as logs:
            first = self.finish_turn(r)
        self.assertEqual([c["resume"] for c in calls(self.calls)], [False])  # no correction turn
        self.assert_held(r, t, first, "turn end", when, logs)
        self.after_the_date_once(r, t, first)

    def test_a_reprompt_is_not_sent_while_someones_date_holds_the_card(self):
        t = task(14)
        write_replies(self.replies, {t["id"][:8]: "I did some work.",
                                     t["id"][:8] + ":resume": AFTER})
        r = self.runner(FakeMesh([t], max_concurrent=1))
        r.tick()
        first = self.finish_turn(r)
        self.assertEqual(first.state, "ready")                              # the correction is due ...
        when = scheduled_by_someone(r.mesh, t["id"])                         # ... and the date comes first
        with self.assertLogs("mesh_runner", "INFO") as logs:
            r.tick()
        self.assertEqual([c["resume"] for c in calls(self.calls)], [False])
        self.assert_held(r, t, first, "resume", when, logs, missed=True)
        self.after_the_date_once(r, t, first)

    def test_a_ready_attempt_is_not_resumed_while_someones_date_holds_the_card(self):
        t = task(3)
        write_replies(self.replies, {t["id"][:8]: next_turn("more to do"),
                                     t["id"][:8] + ":resume": AFTER})
        r = self.runner(FakeMesh([t], max_concurrent=1))
        r.tick()
        first = self.finish_turn(r)
        self.assertEqual(first.state, "ready")                              # NEXT before the date was written
        when = scheduled_by_someone(r.mesh, t["id"])
        with self.assertLogs("mesh_runner", "INFO") as logs:
            r.tick()
        self.assertEqual([c["resume"] for c in calls(self.calls)], [False])
        self.assert_held(r, t, first, "resume", when, logs)
        self.after_the_date_once(r, t, first)

    def test_a_card_wait_that_wakes_before_the_date_starts_no_turn(self):
        t, other = task(4), task(5, cat="in_progress", assignee_id="someone-else")
        write_replies(self.replies, {t["id"][:8]: wait_card(other["id"]),
                                     t["id"][:8] + ":resume": AFTER})
        r = self.runner(FakeMesh([t, other], max_concurrent=1))
        r.tick()
        self.settle_all(r)
        first = r.store.by_state(self.agent.name, ("waiting",))[0]
        self.assertEqual([e["depends_on_task_id"] for e in r.mesh.edges[t["id"]]], [other["id"]])
        when = scheduled_by_someone(r.mesh, t["id"])
        r.mesh.set_category(other["id"], "done")                             # the awaited card closes
        with self.assertLogs("mesh_runner", "INFO") as logs:
            r.tick()
            r.tick()
        self.assertEqual([c["resume"] for c in calls(self.calls)], [False])  # the wake ran no turn
        self.assertEqual(r.mesh.edges[t["id"]], [])                          # the wait's edge is gone with it
        self.assert_held(r, t, r.store.get(first.id), "resume", when, logs)
        self.after_the_date_once(r, t, first)

    def test_a_date_written_after_the_selection_read_holds_the_start_after_checkout(self):
        t = task(6)
        write_replies(self.replies, {t["id"][:8]: next_turn("x")})
        mesh = FakeMesh([t], max_concurrent=1)
        real = mesh.checkout
        written = []

        def checkout_after_the_patch(tid, *a, **kw):
            if not written:                                                  # the PATCH lands between the
                written.append(scheduled_by_someone(mesh, tid))              # feed's read and the checkout
            return real(tid, *a, **kw)
        mesh.checkout = checkout_after_the_patch
        r = self.runner(mesh)
        status = mesh.tasks[t["id"]]["status_id"]
        with self.assertLogs("mesh_runner", "INFO") as logs:
            r.tick()
        self.assertEqual(calls(self.calls), [])                              # no turn
        self.assertEqual(mesh.checkouts, {})                                 # the checkout went back
        self.assertEqual((mesh.tasks[t["id"]]["status_id"], mesh.tasks[t["id"]]["start_after"]),
                         (status, written[0]))
        ended = [a for a in r.store.by_state(self.agent.name, ("aborted",)) if a.task_id == t["id"]]
        self.assertEqual(len(ended), 1)
        self.assertTrue(ended[0].note.startswith("held: start_after "), ended[0].note)
        self.assertTrue(any("held: start_after " in line and "(start" in line for line in logs.output))
        self.assertEqual(r.store.starts_today(self.agent.name, t["id"]), 0)
        r.tick()
        self.assertEqual(calls(self.calls), [])

    def test_the_gate_owners_answer_still_runs_on_a_scheduled_card(self):
        # positive control of the start re-read: the one admitted owner turn passes the date, as at selection
        t = task(7, human_gate=True, human_gate_info=info(), start_after=mesh_time(iso(now() + LATER)))
        write_replies(self.replies, {t["id"][:8]: next_turn("answered")})
        self.agent.policy.human_gate = True
        r = self.runner(FakeMesh([t], max_concurrent=1))
        r.tick()
        self.assertEqual([c["resume"] for c in calls(self.calls)], [])       # the harness logs on exit
        self.finish_turn(r)
        self.assertEqual([c["resume"] for c in calls(self.calls)], [False])


class OwnTimeWaitStillEnds(Bench):
    """Regression: the runner's own time wait is no foreign date. Its end takes the date back and the
    resumed turn runs, also when the clear failed and is still being retried."""

    def own_wait(self, t, mesh):
        write_replies(self.replies, {t["id"][:8]: wait_time(why="vendor maintenance window ends"),
                                     t["id"][:8] + ":resume": AFTER})
        r = self.runner(mesh)
        r.tick()
        self.settle_all(r)
        a = r.store.by_state(self.agent.name, ("waiting",))[0]
        self.assertTrue(a.wait.get("scheduled"))
        self.assertTrue(r.mesh.tasks[t["id"]]["start_after"])
        return r, a

    def wake_and_resume(self, r, t):
        """A comment from someone else wakes the time wait early; returns once the resumed turn runs."""
        r.mesh.add_comment(t["id"], "the window closed early", author="someone-else")
        for _ in range(3):
            r.tick()
            if r.store.by_state(self.agent.name, ("running",)):
                return
        self.fail("the woken attempt was not resumed")

    def test_the_own_date_is_taken_back_on_wake_and_the_turn_runs(self):
        t = task(8)
        r, a = self.own_wait(t, FakeMesh([t], max_concurrent=1))
        self.wake_and_resume(r, t)
        self.assertIsNone(r.mesh.tasks[t["id"]]["start_after"])            # the wait's end took it back
        self.finish_turn(r)
        self.assertEqual([c["resume"] for c in calls(self.calls)], [False, True])
        self.assertEqual([k for _, k, _ in r.store.events(a.id) if k == "schedule_held"], [])

    def test_an_own_date_whose_clear_is_retried_holds_no_resume(self):
        t = task(9, cat="in_progress")
        mesh = RealisticMesh([t])
        mesh.max_concurrent = 1
        r, a = self.own_wait(t, mesh)
        own = mesh.tasks[t["id"]]["start_after"]
        mesh.patch_errors = [conflict(), conflict()]                         # the wake's clear fails twice
        self.wake_and_resume(r, t)
        self.assertEqual(mesh.tasks[t["id"]]["start_after"], own)            # still on the card ...
        self.assertTrue(r.store.mark(self.agent.name, t["id"], UNSCHEDULE_MARK)[0])   # ... kept for the retry
        self.assertNotIn("scheduled", r.store.get(a.id).wait)                # the wait itself forgot it
        self.finish_turn(r)
        self.assertEqual([c["resume"] for c in calls(self.calls)], [False, True])
        self.assertEqual([k for _, k, _ in r.store.events(a.id) if k == "schedule_held"], [])


    def test_a_date_set_again_for_the_same_moment_is_someone_elses(self):
        # the wait's write is known by its receipt: someone who clears it and sets the same moment again
        # gets a new receipt, so that date is neither taken back at the wait's end nor run past
        t = task(15, cat="in_progress")
        mesh = RealisticMesh([t])
        mesh.max_concurrent = 1
        r, a = self.own_wait(t, mesh)
        own = mesh.tasks[t["id"]]
        self.assertEqual(a.wait["scheduled_receipt"], own["start_after_receipt_id"])   # kept from the write
        own.update(start_after_receipt_id="rcpt-owner", version=own.get("version", 1) + 1)
        when = own["start_after"]
        r.mesh.add_comment(t["id"], "the window closed early", author="someone-else")
        with self.assertLogs("mesh_runner", "INFO") as logs:
            for _ in range(3):
                r.tick()
        self.assertEqual([c["resume"] for c in calls(self.calls)], [False])  # the wake ran no turn
        self.assertEqual((mesh.tasks[t["id"]]["start_after"], mesh.tasks[t["id"]]["start_after_receipt_id"]),
                         (when, "rcpt-owner"))
        self.assertEqual(patches_of(mesh, "compare-clear"), [])
        self.assertIsNone(r.store.mark(self.agent.name, t["id"], UNSCHEDULE_MARK))
        ended = r.store.get(a.id)
        self.assertEqual((ended.state, [json.loads(d)["at"] for _, k, d in r.store.events(a.id)
                                        if k == "schedule_held"]), ("completed", ["resume"]))
        self.assertTrue(any("held: start_after " in line for line in logs.output), logs.output)


class RequeueKeepsSomeoneElsesDate(Bench):
    """The requeue (caps, no-outcome, gave-up) takes back only what the runner's waits wrote."""

    def requeue(self, mesh, t):
        self.agent.lead = "lead"
        r = self.runner(mesh)
        r.refresh_me()
        ok = r._requeue_on_event(dict(mesh.tasks[t["id"]]), "no outcome", "NO OUTCOME", "no readable outcome",
                                 None, keep_lane=True)
        self.assertTrue(ok)
        got = mesh.tasks[t["id"]]
        self.assertEqual((got["assignee_id"], mesh.category(got)), (AGENT_ID, "todo"))
        return r

    def test_an_agents_date_stays_and_holds_the_requeued_card(self):
        t = task(10, cat="in_progress")
        mesh = RealisticMesh([t])
        when = scheduled_by_someone(mesh, t["id"])
        r = self.requeue(mesh, t)
        self.assertEqual(mesh.tasks[t["id"]]["start_after"], when)
        self.assertEqual(patches_of(mesh, "compare-clear"), [])
        self.assertIsNone(r.store.mark(self.agent.name, t["id"], UNSCHEDULE_MARK))   # nothing to retry
        self.assertTrue(r.hold_reason(dict(mesh.tasks[t["id"]], _mode="work"), "work").startswith("start_after "))

    def test_a_date_an_ended_wait_of_the_runner_wrote_is_taken_back(self):
        t = task(11, cat="in_progress")
        mesh = RealisticMesh([t])
        when = scheduled_by_someone(mesh, t["id"])
        r = self.runner(mesh)
        old = r.store.create(self.agent.name, t["id"], "claude")             # the wait that wrote it
        r.store.update(old, state="completed", wait={"kind": "time", "scheduled": when})
        self.requeue(mesh, t)
        self.assertIsNone(mesh.tasks[t["id"]]["start_after"])
        self.assertEqual(len(patches_of(mesh, "compare-clear")), 1)

    def test_a_date_set_again_for_the_same_moment_stays_and_the_runners_own_receipt_goes(self):
        for receipt, cleared in (("rcpt-runner", False), ("rcpt-owner", True)):
            with self.subTest(wait_receipt=receipt):
                t = task(16 if cleared else 17, cat="in_progress")
                mesh = RealisticMesh([t])
                when = scheduled_by_someone(mesh, t["id"])                   # the card: receipt rcpt-owner
                r = self.runner(mesh)
                old = r.store.create(self.agent.name, t["id"], "claude")
                r.store.update(old, state="completed",
                               wait={"kind": "time", "scheduled": when, "scheduled_receipt": receipt})
                self.requeue(mesh, t)
                self.assertEqual(mesh.tasks[t["id"]]["start_after"], None if cleared else when)
                self.assertEqual(len(patches_of(mesh, "compare-clear")), 1 if cleared else 0)

    def test_a_gave_up_refeed_keeps_someone_elses_date(self):
        t = task(12, cat="in_progress")
        mesh = FakeMesh([t], max_concurrent=1)
        when = scheduled_by_someone(mesh, t["id"])
        r = self.runner(mesh)
        r.refresh_me()
        a = r.store.create(self.agent.name, t["id"], "claude")
        r.store.update(a, state="running", mode="work")
        self.assertTrue(r._refeed_gave_up(r.store.get(a.id), dict(mesh.tasks[t["id"]]), "RUNNER GAVE UP", "x"))
        self.assertEqual(mesh.category(mesh.tasks[t["id"]]), "todo")
        self.assertEqual(mesh.tasks[t["id"]]["start_after"], when)
        self.assertEqual(compare_clears(mesh), [])


if __name__ == "__main__":
    import unittest
    unittest.main()
