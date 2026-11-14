"""The runner resolves the Mesh wait it observed when the attempt leaves it: ``met`` when the awaited event
arrived, ``cancelled`` when it was superseded or the card left the lane. Best effort: a failure is recorded
once, retried a bounded number of times, and never holds the wake."""
import datetime as dt
import json
import unittest

from mesh_runner.mesh import MeshError

from fakes import FakeMesh, task
from test_typed_outcome import COMMENT, TypedTurn

HUMAN = {"kind": "human", "addressee": "morgan", "question": "ship tonight?", "note": "asked @morgan",
         "human_gate_ref": f"comment:{COMMENT}"}


class Resolve(TypedTurn):

    def parked(self, wait=None, reply="Recorded the outcome through Mesh.", gate=False):
        """A turn that ended with a typed wait, settled: (runner, mesh, task, attempt)."""
        t, other = task(1), task(2)
        mesh = FakeMesh([t, other], max_concurrent=1)
        r = self.run_turn(mesh, t, reply=reply)
        mesh.tasks[t["id"]]["human_gate"] = gate
        self.typed(mesh, t, "wait", dict(wait or {"kind": "card", "target_task_id": other["id"]}))
        a = self.finish_turn(r)
        self.assertEqual(a.state, "waiting", a.wake_text)
        return r, mesh, t, a

    def test_the_wait_id_is_kept_on_the_attempt(self):
        r, mesh, t, a = self.parked()
        self.assertEqual(a.wait["mesh_wait_id"], "w-1")
        self.assertEqual(json.loads(r.store.db.execute("SELECT wait FROM attempts WHERE id=?",
                                                       (a.id,)).fetchone()[0])["mesh_wait_id"], "w-1")
        r._wake_budgeted(a)                              # nothing happened: still observed, nothing sent
        self.assertEqual(getattr(mesh, "resolved", []), [])

    def test_an_arrived_event_resolves_it_met(self):
        r, mesh, t, a = self.parked(HUMAN, gate=True)
        mesh.tasks[t["id"]]["human_gate"] = False        # the person answered: the gate is lifted
        r._wake_budgeted(a)
        self.assertEqual(r.store.get(a.id).state, "ready")
        self.assertEqual(mesh.resolved, [(t["id"], "w-1", "met", "the human gate was lifted")])
        self.assertIn("w-1:met", [d for _, k, d in r.store.events(a.id) if k == "mesh_wait_resolved"])

    def test_a_wake_on_something_else_cancels_it(self):
        r, mesh, t, a = self.parked()
        mesh.add_comment(t["id"], "x" * 500, author="someone-else",
                         at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=30))
        r._wake_budgeted(a)
        self.assertEqual(r.store.get(a.id).state, "ready")
        [(tid, wait_id, resolution, cause)] = mesh.resolved
        self.assertEqual((wait_id, resolution), ("w-1", "cancelled"))
        self.assertIn("commented: xxxxxxxx", cause)
        self.assertEqual(len(cause), 200)                              # the wake text, cut to 200

    def test_a_next_marker_in_the_agents_own_comment_does_not_supersede_the_wait(self):
        """Only a typed continue_next ends a wait now; marker text in a comment is ignored."""
        r, mesh, t, a = self.parked()
        mesh.add_comment(t["id"], "Picked it up again.\n↻ NEXT: continue", author=r.agent_id,
                         at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=30))
        r._wake_budgeted(a)
        self.assertEqual(r.store.get(a.id).state, "waiting")
        self.assertEqual(getattr(mesh, "resolved", []), [])

    def test_the_card_leaving_the_lane_cancels_it(self):
        r, mesh, t, a = self.parked()
        mesh.tasks[t["id"]]["assignee_id"] = "someone-else"
        r._wake_budgeted(a)
        self.assertEqual(r.store.get(a.id).state, "completed")
        self.assertEqual(mesh.resolved, [(t["id"], "w-1", "cancelled", "task left the agent while waiting")])

    def test_a_marker_in_the_final_message_is_no_outcome_and_resolves_nothing(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.run_turn(mesh, t, reply="Parked.\n❓ Blocking @morgan: ship tonight?")
        a = self.finish_turn(r)
        self.assertEqual((a.state, a.no_outcome, a.wait), ("ready", 1, {}))
        self.assertEqual(getattr(mesh, "resolved", []), [])

    def test_the_wait_id_survives_a_restart(self):
        r, mesh, t, a = self.parked(HUMAN, gate=True)
        r.store.db.close()
        r = self.runner(mesh)
        mesh.tasks[t["id"]]["human_gate"] = False
        r.tick()                                         # the restarted runner's own loop observes it
        self.assertEqual([x[1:3] for x in mesh.resolved], [("w-1", "met")])

    def test_a_transient_failure_is_recorded_once_and_retried_a_bounded_number_of_times(self):
        r, mesh, t, a = self.parked(HUMAN, gate=True)
        mesh.resolve_errors = [MeshError(503, "unavailable"), TimeoutError("timed out"), MeshError(502, "bad")]
        mesh.tasks[t["id"]]["human_gate"] = False
        r._wake_budgeted(a)
        self.assertEqual(r.store.get(a.id).state, "ready")          # the wake was not held by the failure
        for _ in range(5):
            r.check_wakes()
        self.assertEqual(len(mesh.resolved), 3)                       # WAIT_RESOLVE_TRIES, then given up
        failed = [json.loads(d) for _, k, d in r.store.events(a.id) if k == "mesh_wait_resolve_failed"]
        self.assertEqual(failed, [{"wait": "w-1", "resolution": "met", "status": 503, "final": False}])
        self.assertEqual(r.store.marked(r.agent.name, r.WAIT_RESOLVE), {})
        self.assertEqual(r.store.mark(r.agent.name, t["id"], r.WAIT_RESOLVED)[0], "w-1")

    def test_a_retry_that_succeeds_clears_the_pending_resolve(self):
        r, mesh, t, a = self.parked(HUMAN, gate=True)
        mesh.resolve_errors = [MeshError(0, "connection reset")]
        mesh.tasks[t["id"]]["human_gate"] = False
        r._wake_budgeted(a)
        self.assertIn(t["id"], r.store.marked(r.agent.name, r.WAIT_RESOLVE))
        r.check_wakes()
        r.check_wakes()
        self.assertEqual([x[2] for x in mesh.resolved], ["met", "met"])
        self.assertEqual(r.store.marked(r.agent.name, r.WAIT_RESOLVE), {})
        self.assertIn("w-1:met", [d for _, k, d in r.store.events(a.id) if k == "mesh_wait_resolved"])

    def test_not_found_and_conflict_are_final(self):
        for status in (404, 409):
            with self.subTest(status=status):
                r, mesh, t, a = self.parked(HUMAN, gate=True)
                mesh.resolve_errors = [MeshError(status, "ended")]
                mesh.tasks[t["id"]]["human_gate"] = False
                r._wake_budgeted(a)
                r.check_wakes()
                self.assertEqual(len(mesh.resolved), 1)
                failed = [json.loads(d) for _, k, d in r.store.events(a.id) if k == "mesh_wait_resolve_failed"]
                self.assertEqual(failed, [{"wait": "w-1", "resolution": "met", "status": status, "final": True}])
                self.tearDown()
                self.setUp()


if __name__ == "__main__":
    unittest.main()
