"""RUNNER GAVE UP on a card of an agent that ends its escalation chain (no lead of its own, or the
project's lead is the agent itself: lane -> lead -> ... -> ops ends at ops). Nobody else is due
to write on such a card, so instead of waiting in todo for a comment it starts again once in a fresh
session that is told the previous one gave up. A second give-up the same day waits for a comment or
the next UTC day, never forever. A lane with a lead still hands the card to the lead."""
import datetime as dt
from unittest import mock

from fakes import AGENT_ID, FakeMesh, calls, next_turn, task, write_replies
from test_requeue_on_cap import CapBench

from mesh_runner.runner import GAVE_UP_REFED, GAVE_UP_REFED_NOTE


class GaveUpAtTheEndOfTheChain(CapBench):
    def setUp(self):
        super().setUp()
        self.cfg.daily_start_cap = 0
        self.cfg.no_outcome_route_after = 2         # the second miss in a row gives up

    def silent(self, t, text="I did some work."):
        write_replies(self.replies, {t["id"][:8]: text})

    def give_up(self, r):
        """Two turns without a readable outcome: corrected once, then RUNNER GAVE UP."""
        r.tick()
        self.assertEqual(self.finish_turn(r).state, "ready")
        r.tick()
        return self.finish_turn(r)

    def assert_refed(self, r, mesh, t, a):
        self.assertEqual(a.state, "completed")
        self.assertTrue(a.note.startswith(GAVE_UP_REFED_NOTE), a.note)
        got = mesh.tasks[t["id"]]
        self.assertEqual((got["assignee_id"], mesh.category(got)), (AGENT_ID, "todo"))
        self.assertEqual(r.hold_reason(dict(got), "work"), "")              # nothing holds it
        body = " ".join(self.bodies(mesh, t))
        self.assertIn("RUNNER GAVE UP", body)
        self.assertIn("starts again once in a fresh session", body)
        before = len(calls(self.calls))
        r.tick()                                                             # started again, no comment needed
        self.finish_turn(r)                                                  # the fake harness logs its call on exit
        started = calls(self.calls)[before:]
        self.assertEqual(len(started), 1)
        self.assertFalse(started[0]["resume"])                               # a fresh session
        self.assertIn("the runner gave up on your previous session", started[0]["prompt"])

    def test_no_lead_gave_up_starts_again_once_in_a_fresh_session(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)                               # no lead above this lane
        self.silent(t)
        r = self.runner(mesh)
        self.assert_refed(r, mesh, t, self.give_up(r))

    def test_the_project_lead_being_the_agent_itself_is_the_end_of_the_chain(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        self.cfg.project_leads = {"p1": self.agent.name}                     # the agent leads its own project
        self.silent(t)
        r = self.runner(mesh)
        self.assert_refed(r, mesh, t, self.give_up(r))

    def test_the_fresh_session_starts_its_run_of_misses_over(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        self.silent(t)
        r = self.runner(mesh)
        self.give_up(r)
        r.tick()
        self.assertEqual(self.finish_turn(r).state, "ready")                 # its first miss is corrected

    def test_a_second_give_up_the_same_day_waits_for_a_comment_or_the_next_utc_day(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        self.silent(t)
        r = self.runner(mesh)
        self.give_up(r)
        a = self.give_up(r)                                                  # the fresh session gives up too
        self.assertEqual(a.state, "failed")
        self.assertFalse(a.note.startswith(GAVE_UP_REFED_NOTE), a.note)
        got = dict(mesh.tasks[t["id"]])
        self.assertEqual(mesh.category(got), "todo")
        self.assertIn("waits for a comment", r.hold_reason(got, "work"))
        self.assertIn("until tomorrow (UTC) or until someone writes here", self.bodies(mesh, t)[-1])
        tomorrow = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)
        with mock.patch("mesh_runner.runner.now", return_value=tomorrow), \
                mock.patch("mesh_runner.store.now", return_value=tomorrow):
            self.assertEqual(r.hold_reason(got, "work"), "")                 # the next UTC day starts it
        self.assertEqual(r.store.mark(self.agent.name, t["id"], GAVE_UP_REFED)[0],
                         dt.datetime.now(dt.timezone.utc).date().isoformat())

    def test_a_readable_outcome_in_the_fresh_session_is_ordinary_work(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        self.silent(t)
        r = self.runner(mesh)
        self.give_up(r)
        self.silent(t, next_turn("picked it up again"))
        r.tick()
        self.assertEqual(self.finish_turn(r).state, "ready")


class GaveUpWithALead(CapBench):
    def setUp(self):
        super().setUp()
        self.cfg.daily_start_cap = 0
        self.cfg.no_outcome_route_after = 2

    def test_an_ordinary_lane_still_hands_the_card_to_its_lead(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        self.agent.lead = "lead"
        write_replies(self.replies, {t["id"][:8]: "I did some work."})
        r = self.runner(mesh)
        r.tick()
        self.finish_turn(r)
        r.tick()
        a = self.finish_turn(r)
        self.assertTrue(a.note.startswith("routed to lead"), a.note)
        self.assertEqual(mesh.tasks[t["id"]]["assignee_id"], "agent-lead")
        self.assertIsNone(r.store.mark(self.agent.name, t["id"], GAVE_UP_REFED))
        self.assertIn("Handed to", " ".join(self.bodies(mesh, t)))


if __name__ == "__main__":
    import unittest
    unittest.main()
