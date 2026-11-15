"""A turn command recorded after its turn was settled as "no outcome" is that turn's outcome.

An interactive session can go on after the runner settled its turn: a background command finishes, the
harness starts a turn of its own and the session calls ``wait_for``. Mesh accepts it and releases the
checkout. The row's created_at lies between the settle (no outcome) and the launch of the correction
turn, so it belongs to no turn the runner reads: the correction resumed on the dead lease, every command
of it got 409, the second "no outcome" sent the card to todo and the merge wait Mesh held was never run.
The runner reads the window from the start of the turn counted as
"no outcome": before it issues the correction, and again when the correction turn settles."""
import json
import sys
from unittest import mock

import fakes
from fakes import (FakeGitLab, FakeMesh, calls, task, turn_rows, wait_merge, wait_pipeline, wait_time,
                   write_replies)
from test_runner import Bench

SHA = "a" * 40
TARGET = "g/app!632"
GATE = "import sys; sys.exit(0)\n"


class LateTurnOutcome(Bench):
    def setUp(self):
        super().setUp()
        gate = self.root / "gate.py"
        gate.write_text(GATE)
        self.cfg.merge_command = (sys.executable, str(gate), "{iid}", "{project}", "{sha}")

    def bench(self):
        t = task(0xdeadbeef, cat="in_progress")
        mesh = FakeMesh([t], max_concurrent=1)
        gl = FakeGitLab()
        gl.mr_rows[TARGET] = {"sha": SHA, "state": "opened",
                              "head_pipeline": {"id": 32664, "sha": SHA, "status": "success"}}
        r = self.runner(mesh, gl)
        self.addCleanup(r.store.db.close)
        r.refresh_me()
        write_replies(self.replies, {
            # turn 3: a background review started, the turn ends in prose without a command
            t["id"][:8]: "the review started in the background",
            # the correction: whatever it calls, Mesh refuses with 409 on a dead lease and writes nothing
            t["id"][:8] + ":resume": "wait_for on=merge refused: 409 the caller does not hold this card's checkout",
        })
        return r, mesh, gl, t

    def no_outcome_turn(self, r, mesh, t):
        self.assertEqual(r.start_new(1), 0)
        a = self.finish_turn(r)
        self.assertEqual((a.state, a.no_outcome), ("ready", 1), "settled as no outcome: a correction is due")
        self.assertIn(t["id"], mesh.checkouts, "the lease of the no-outcome turn is still the runner's")
        return a

    @staticmethod
    def late_command(t, turn):
        """The session's own turn after the settle: Mesh records the command (its comment included) and
        releases the checkout, as the live MCP does."""
        row, wait = turn_rows(turn)
        rec = {"task8": t["id"][:8], "row": row, "wait": wait, "to": ""}
        with open(fakes.OUTCOME_SINK, "a") as f:
            f.write(json.dumps(rec) + "\n")
        return row, wait

    def assert_merge_wait(self, r, mesh, t, a, row):
        a = r.store.get(a.id)
        self.assertEqual(a.state, "waiting", "the late command is the outcome of the no-outcome turn")
        self.assertEqual((a.wait.get("kind"), a.wait.get("target")), ("merge", TARGET))
        self.assertEqual(a.wait["merge"]["sha"], SHA, "the runner now knows the merge wait and runs its gate")
        self.assertEqual(a.wait.get("mesh_wait_id"), row["wait_id"])
        self.assertEqual(a.no_outcome, 0)
        self.assertFalse(a.checkout.get("token"), "the lease Mesh released with wait_for is forgotten")
        self.assertNotIn(t["id"], mesh.checkouts)
        events = [kind for kind, _ in r.store.db.execute(
            "SELECT kind, detail FROM events WHERE attempt = ?", (a.id,)).fetchall()]
        self.assertIn("late_typed_outcome", events)
        self.assertIn("checkout_released_by_mesh", events)
        self.assertNotIn("requeue", " ".join(events))
        return a

    def test_a_command_recorded_before_the_correction_starts_is_accepted_and_no_correction_runs(self):
        r, mesh, gl, t = self.bench()
        a = self.no_outcome_turn(r, mesh, t)
        row, _ = self.late_command(t, wait_merge("g/app", 632, SHA))
        r.resume_ready(1)
        self.assertEqual([x.id for x in r.store.by_state(self.agent.name, ("running", "starting"))], [],
                         "no correction turn is launched on the dead lease")
        self.assertEqual(len(calls(self.calls)), 1, "the harness ran only the no-outcome turn")
        self.assert_merge_wait(r, mesh, t, a, row)
        self.assertEqual(mesh.category(mesh.tasks[t["id"]]), "in_progress", "the card is not sent to todo")

    def test_a_command_recorded_while_the_correction_starts_is_accepted_at_its_settle(self):
        r, mesh, gl, t = self.bench()
        a = self.no_outcome_turn(r, mesh, t)
        late = []
        spawn = r._spawn

        def racing_spawn(*args, **kwargs):
            # the session's own turn lands after the resume looked and before the correction's anchor
            if not late:
                late.append(self.late_command(t, wait_merge("g/app", 632, SHA)))
            return spawn(*args, **kwargs)

        with mock.patch.object(r, "_spawn", side_effect=racing_spawn):
            r.resume_ready(1)
        self.assertTrue(late, "the correction turn was launched")
        settled = self.finish_turn(r)
        self.assertNotEqual(settled.state, "failed")
        self.assert_merge_wait(r, mesh, t, a, late[0][0])
        self.assertEqual(mesh.category(mesh.tasks[t["id"]]), "in_progress", "the card is not sent to todo")

    def test_a_late_command_whose_wait_is_gone_still_frees_the_dead_lease_for_the_correction(self):
        r, mesh, gl, t = self.bench()
        gl.pipeline_rows["g/app#7"] = {"sha": SHA, "id": 7, "status": "running"}
        a = self.no_outcome_turn(r, mesh, t)
        row, wait = self.late_command(t, wait_pipeline("g/app", 7))
        mesh.waits = {t["id"]: None}             # met or cancelled since: no longer the active wait
        r.resume_ready(1)
        held = r.store.get(a.id)
        self.assertEqual((held.state, held.no_outcome), ("ready", 1), "still the one correction, not a second miss")
        self.assertIn("is no longer the active one", held.wake_text, "the correction names what was recorded")
        self.assertFalse(held.checkout.get("token"), "the dead lease is dropped")
        r.resume_ready(1)
        running = r.store.by_state(self.agent.name, ("running",))
        self.assertEqual(len(running), 1, "the correction runs: there is no outcome the runner can act on")
        self.assertEqual(running[0].id, a.id, "same session")
        self.assertIn(t["id"], mesh.checkouts, "the correction holds a live lease: its commands do not get 409")
        self.assertEqual(mesh.checkouts[t["id"]]["session"], a.id)
        self.finish_turn(r)

    def test_a_correction_that_may_have_reached_the_session_is_left_to_its_own_settle(self):
        r, mesh, gl, t = self.bench()
        a = self.no_outcome_turn(r, mesh, t)
        self.late_command(t, wait_merge("g/app", 632, SHA))
        # a start of the correction whose delivery is not proven unsent: it may be running in the session
        r.store.update(a, wait={**a.wait, "interactive_prompt_intent": {"kind": "headless", "token": "t", "turn": 2}})
        self.assertFalse(r._late_outcome(r.store.get(a.id), dict(mesh.tasks[t["id"]])))
        held = r.store.get(a.id)
        self.assertEqual((held.state, held.no_outcome), ("ready", 1))
        self.assertIsNotNone(r._no_outcome_window(held), "the window stays for the correction's settle")

    def test_the_row_the_settle_already_counted_is_not_read_again_as_late(self):
        r, mesh, gl, t = self.bench()
        write_replies(self.replies, {t["id"][:8]: wait_time("tomorrow", why="later"),
                                     t["id"][:8] + ":resume": "still nothing"})
        self.assertEqual(r.start_new(1), 0)
        a = self.finish_turn(r)
        self.assertEqual((a.state, a.no_outcome), ("ready", 1), "a time wait this runner refuses: one correction")
        r.resume_ready(1)
        running = r.store.by_state(self.agent.name, ("running",))
        self.assertEqual([x.id for x in running], [a.id], "the correction runs: the refused row is not a late command")
        events = [kind for kind, _ in r.store.db.execute(
            "SELECT kind, detail FROM events WHERE attempt = ?", (a.id,)).fetchall()]
        self.assertNotIn("late_typed_outcome", events)
        self.assertNotIn("late_outcome_refused", events)
        self.finish_turn(r)
        self.assertEqual([c["resume"] for c in calls(self.calls)], [False, True])

    def test_without_a_late_command_the_correction_runs_on_its_own_lease(self):
        r, mesh, gl, t = self.bench()
        a = self.no_outcome_turn(r, mesh, t)
        r.resume_ready(1)
        running = r.store.by_state(self.agent.name, ("running",))
        self.assertEqual(len(running), 1)
        self.assertEqual(running[0].checkout.get("token"), a.checkout.get("token"), "the live lease is kept")
        settled = self.finish_turn(r)
        self.assertNotEqual(settled.state, "waiting", "a second turn without a command is no outcome again")


if __name__ == "__main__":
    import unittest
    unittest.main()
