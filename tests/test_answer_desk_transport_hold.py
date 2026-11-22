"""A lane whose transport gate holds every turn does not keep its questions silently: the hold is logged once,
the gate is asked at a bounded rate, and the question goes to its request card within ``FALLBACK_SECONDS`` of the
hold (a question is never lost). Measured: a lane's transport gate refused every cardless turn and two questions
sat on its desk for more than 14 hours without a turn, a card or a log line."""
import datetime as dt

from mesh_runner import answer_desk
from mesh_runner.answer_desk import AnswerDesk
from mesh_runner.store import Store

from fakes import FakeMesh, task
from test_answer_desk import (FALLBACK_MIN, HOLD, OTHER, QUESTION, REQUEST, Desk, assert_bounded_gate, gated,
                              held_lines, lead_lane, run_for)


class TransportHold(Desk):
    assertBoundedGate = assert_bounded_gate

    def test_a_permanent_transport_hold_files_the_request_card_within_the_fallback(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        r, lead, desk, a = self.parked(mesh, t)
        calls = gated(self.clock, lead)
        with self.assertLogs("mesh_runner", "INFO") as logs:
            run_for(self.clock, desk, FALLBACK_MIN - 1, r.tick)
            self.assertEqual((self.turns, mesh.created), ([], []))       # the deadline is not up yet
            self.assertEqual(desk.status("lead").get("transport_hold"), HOLD)  # and the hold is visible
            run_for(self.clock, desk, 2, r.tick)
        self.assertEqual(self.turns, [])
        self.assertEqual(len(mesh.created), 1)                           # the request card is filed
        why = r.store.get(a.id).wait["desk_fallback"]
        self.assertIn("transport gate", why)
        self.assertIn(HOLD, why)
        self.assertEqual(desk.state("lead", REQUEST), "carded")
        [held] = held_lines(logs.output)                                 # the reason is logged once
        self.assertIn(HOLD, held)
        filed = [line for line in logs.output if "request card filed" in line]
        self.assertEqual(len(filed), 1, logs.output)
        self.assertIn(HOLD, filed[0])
        self.assertBoundedGate(calls)
        lead.gate_hold = ""                                              # the card has it now
        run_for(self.clock, desk, 2)
        self.assertEqual(self.turns, [])

    def test_a_hold_lifted_before_the_deadline_is_answered_on_the_desk(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        r, lead, desk, a = self.parked(mesh, t)
        calls = gated(self.clock, lead)
        with self.assertLogs("mesh_runner", "INFO") as logs:
            run_for(self.clock, desk, FALLBACK_MIN - 5, r.tick)
            lead.gate_hold = ""
            run_for(self.clock, desk, 1, r.tick)
        self.assertEqual(desk.state("lead", REQUEST), "answered")
        self.assertEqual((len(self.turns), mesh.created), (1, []))
        self.assertEqual(len(held_lines(logs.output)), 1)
        self.assertTrue([line for line in logs.output if "transport gate lets lead's questions through" in line])
        self.assertBoundedGate(calls)

    def test_the_hold_survives_a_restart_of_the_desk(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        r, lead, desk, a = self.parked(mesh, t)
        gated(self.clock, lead)
        run_for(self.clock, desk, FALLBACK_MIN - 5, r.tick)
        restarted = AnswerDesk(self.cfg, [r, lead], desk.store, clock=self.clock, spawn=lambda fn: fn(),
                               run_turn=desk.run_turn).attach()
        run_for(self.clock, restarted, 6, r.tick)
        self.assertEqual(len(mesh.created), 1)
        self.assertIn(HOLD, r.store.get(a.id).wait["desk_fallback"])
        self.assertEqual(self.turns, [])

    def test_waiting_for_memory_still_spends_nothing(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        r, lead, desk, a = self.parked(mesh, t)
        calls = gated(self.clock, lead)
        lead.memgate.holding = True                                      # the host, not the lane's gate
        run_for(self.clock, desk, FALLBACK_MIN + 5, r.tick)
        self.assertEqual((calls, mesh.created), ([], []))
        self.assertEqual(desk.state("lead", REQUEST), "queued")

    def test_a_host_wait_between_holds_restarts_the_hold_clock(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        r, lead, desk, a = self.parked(mesh, t)
        gated(self.clock, lead)
        run_for(self.clock, desk, FALLBACK_MIN - 5, r.tick)              # held 15 minutes
        lead.memgate.holding = True                                      # the host waits: the gate is not asked
        run_for(self.clock, desk, 10, r.tick)
        lead.memgate.holding = False                                     # and the gate still holds
        run_for(self.clock, desk, FALLBACK_MIN - 2, r.tick)
        self.assertEqual(mesh.created, [])                               # its 20 minutes start after the wait
        run_for(self.clock, desk, 3, r.tick)
        self.assertEqual(len(mesh.created), 1)
        self.assertIn(HOLD, r.store.get(a.id).wait["desk_fallback"])


class TwoQuestions(Desk):
    def queued(self, mesh, desk, t, rid, minutes_ago):
        mesh.open_decision(rid, t["id"], QUESTION)
        at = dt.datetime.fromtimestamp(self.clock() - minutes_ago * 60, dt.timezone.utc).isoformat()
        desk._set("lead", rid, {"state": "queued", "at": at, "since": at, "task": t["id"], "from": "builder"})

    def test_a_hold_that_ends_ends_for_the_whole_queue(self):
        t1, t2 = task(1), task(2)
        mesh = FakeMesh([t1, t2])
        lead = lead_lane(mesh)
        pending, ran = [], []

        def run_turn(ar, rid, tid, text):
            ran.append(rid)
            mesh.answer_decision(rid, "yes", author="agent-lead")
            return ""
        desk = AnswerDesk(self.cfg, [lead], Store(self.root / "two.sqlite"), clock=self.clock,
                          spawn=pending.append, run_turn=run_turn)
        self.queued(mesh, desk, t1, REQUEST, 2)
        self.queued(mesh, desk, t2, OTHER, 1)
        gated(self.clock, lead)
        second = {"cardless": {"lane": "lead"}, "decision_request_id": OTHER}
        run_for(self.clock, desk, FALLBACK_MIN - 1)                     # both held 19 minutes
        self.assertEqual(desk.still_waiting(second), "")
        lead.gate_hold = ""
        run_for(self.clock, desk, 1)                                     # the gate lets the oldest through
        self.assertEqual((len(pending), desk.status("lead")["running"]), (1, REQUEST))
        self.clock.advance(2)                                            # its turn holds the lane 2 minutes
        self.assertEqual(desk.still_waiting(second), "")                 # the hold that ended does not count
        self.assertNotIn("held_since", desk._get("lead", OTHER))
        pending.pop()()
        run_for(self.clock, desk, 1)
        self.assertEqual(len(pending), 1)
        pending.pop()()
        self.assertEqual((ran, desk.state("lead", OTHER)), ([REQUEST, OTHER], "answered"))

    def test_a_question_that_comes_between_gate_calls_counts_from_its_arrival(self):
        t1, t2 = task(1), task(2)
        mesh = FakeMesh([t1, t2])
        lead = lead_lane(mesh)
        desk = AnswerDesk(self.cfg, [lead], Store(self.root / "between.sqlite"), clock=self.clock,
                          spawn=lambda fn: fn(), run_turn=lambda *args: "end_turn")
        self.queued(mesh, desk, t1, REQUEST, 0)
        calls = gated(self.clock, lead)
        desk.step()                                                      # the gate holds the first question
        self.assertEqual(len(calls), 1)
        self.clock.now += answer_desk.STEP_SECONDS
        self.queued(mesh, desk, t2, OTHER, 0)                            # the second comes inside the backoff
        arrived = self.clock()
        second = {"cardless": {"lane": "lead"}, "decision_request_id": OTHER}
        while self.clock() + answer_desk.STEP_SECONDS < arrived + answer_desk.FALLBACK_SECONDS:
            self.clock.now += answer_desk.STEP_SECONDS
            desk.step()
            self.assertEqual(desk.still_waiting(second), "")
        self.clock.now = arrived + answer_desk.FALLBACK_SECONDS          # its own 20 minutes, not the gate's next call
        self.assertIn(HOLD, desk.still_waiting(second))
        assert_bounded_gate(self, calls)
