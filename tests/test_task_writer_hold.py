"""A card handed to another lane while the previous lane still holds the card's
server reservation (``task_writer_active``) is held for that card only, with the holder named,
before any attempt is created when the holder is a lane of this runner; an unconfirmed gate
release is kept and resent instead of being lost."""
import json
import unittest
from unittest.mock import patch

from fakes import FakeMesh, task
from test_runner import Bench
from mesh_runner import runner as rmod
from mesh_runner.runner import gate_hold_reason, GATE_RELEASE_PENDING

WRITER = ("capacity: runtime.capacity: server admit hold acquire_refused:task_writer_active "
          "[holder=Steward state=consumed until release]")


class WriterReason(unittest.TestCase):
    def test_writer_refusal_names_the_holder_not_capacity(self):
        self.assertEqual(gate_hold_reason(WRITER), "card writer still active (holder=Steward, state=consumed)")
        self.assertEqual(gate_hold_reason(WRITER.split("capacity: ", 1)[1]),
                         "card writer still active (holder=Steward, state=consumed)")
        self.assertEqual(gate_hold_reason("runtime.capacity: server admit hold acquire_refused:task_writer_active"),
                         "card writer still active")

    def test_holder_outside_the_pattern_is_not_copied(self):
        reason = gate_hold_reason("capacity: runtime.capacity: server admit hold "
                                  "acquire_refused:task_writer_active [holder=$(id) state=x]")
        self.assertEqual(reason, "card writer still active")

    def test_pool_refusal_stays_capacity(self):
        self.assertEqual(gate_hold_reason("capacity: runtime.capacity: server admit hold acquire_refused:pool_exhausted"),
                         "runtime capacity unavailable")


def pending(r, tid):
    row = r.store.mark("builder", tid, GATE_RELEASE_PENDING)
    return json.loads(row[0]) if row and row[0] else []


class WriterHold(Bench):
    def _gated(self, r):
        self.agent.start_gate_command = ["operator-gate"]
        self.agent.turn_end_command = ["operator-end"]
        return r

    def test_admit_writer_refusal_holds_the_card_not_the_lane(self):
        r = self._gated(self.runner(FakeMesh([])))
        with patch.object(r, "_run_hook", return_value=(75, WRITER, "")):
            reason, _ = r._gate("admit", "one", "work")
        self.assertEqual(reason, "card writer still active (holder=Steward, state=consumed)")
        self.assertTrue(r._gate_task_only)
        self.assertIsNone(r._agent_gate_hold)      # the lane's other cards keep starting
        self.assertFalse(r._gate_failures)

    def test_admit_refusal_note_and_log_name_the_holder(self):
        t = task(1)
        r = self._gated(self.runner(FakeMesh([t])))

        def hook(argv, env, timeout):
            if env["MESH_RUNNER_PHASE"] == "reserve":
                return 0, "RES_ID=r1\nRES_PROFILE=p\nRES_BACKEND=server", ""
            if env["MESH_RUNNER_PHASE"] == "admit":
                return 75, WRITER, ""
            return 0, "RELEASE_CONFIRMED=1", ""
        with patch.object(r, "_run_hook", side_effect=hook), self.assertLogs("mesh_runner", "INFO") as logs:
            r.tick()
        attempts = r.store.by_state("builder", ("aborted",))
        self.assertEqual([a.note for a in attempts], ["held: card writer still active (holder=Steward, state=consumed)"])
        self.assertFalse(r._start_held)
        self.assertTrue(any("holder=Steward" in line for line in logs.output))
        self.assertNotIn("runtime capacity unavailable", "\n".join(logs.output))

    def test_local_holder_holds_before_any_attempt_and_lifts_on_release(self):
        t = task(1)
        r = self._gated(self.runner(FakeMesh([t])))
        other = r.store.create("Steward", t["id"], "claude", "task 1")
        r.store.mark("Steward", t["id"], "gate", json.dumps({"RES_ID": "r0"}))
        mesh = r.mesh
        with patch.object(r, "_run_hook") as hook, self.assertLogs("mesh_runner", "INFO") as logs:
            r.tick()
        hook.assert_not_called()                                   # no reserve, no checkout, no admit
        self.assertEqual(r.store.by_state("builder", rmod.ACTIVE + ("aborted", "completed", "ready")), [])
        self.assertEqual(mesh.checkouts, {})
        self.assertTrue(any("card writer still active (holder=Steward, state=running, local)" in line
                            for line in logs.output))
        r.store.mark("Steward", t["id"], "gate", "")                 # the holder's turn released it
        self.assertEqual(r._local_writer(t["id"]), "")
        self.assertIsNotNone(other)

    def test_stale_gate_mark_without_a_live_turn_holds_nothing(self):
        t = task(1)
        r = self._gated(self.runner(FakeMesh([t])))
        r.store.mark("Steward", t["id"], "gate", json.dumps({"RES_ID": "r0"}))
        self.assertEqual(r._local_writer(t["id"]), "")

    def test_unconfirmed_release_is_kept_and_retried_until_confirmed(self):
        t = task(1)
        r = self._gated(self.runner(FakeMesh([t])))
        a = r.store.create("builder", t["id"], "claude", "task 1")
        r.store.mark("builder", t["id"], "gate",
                     json.dumps({"RES_ID": "r1", "RES_BACKEND": "server", "RES_ADMITTED": "1"}))
        with patch.object(r, "_run_hook", return_value=(0, "", "")):    # end ran, release not confirmed
            r._release_gate(a, "turn-end")
        [pend] = pending(r, t["id"])
        self.assertEqual(pend["gate"]["RES_ID"], "r1")
        # another lane of this runner sees the pending writer before creating an attempt
        peer = self.runner(FakeMesh([t]))
        peer.agent = type(self.agent)(**{**self.agent.__dict__, "name": "peer"})
        self.assertIn("state=release pending", peer._local_writer(t["id"]))
        with patch.object(r, "_run_hook") as hook:
            r.retry_gate_releases()                                  # not due yet
        hook.assert_not_called()
        r.store.mark("builder", t["id"], GATE_RELEASE_PENDING, json.dumps([dict(pend, next_at="2000-01-01T00:00:00Z")]))
        with patch.object(r, "_run_hook", return_value=(0, "", "")):
            r.retry_gate_releases()                                  # still unconfirmed: kept, backoff grows
        [again] = pending(r, t["id"])
        self.assertEqual(again["tries"], 1)
        r.store.mark("builder", t["id"], GATE_RELEASE_PENDING, json.dumps([dict(again, next_at="2000-01-01T00:00:00Z")]))
        with patch.object(r, "_run_hook", return_value=(0, "RELEASE_CONFIRMED=1", "")) as hook:
            r.retry_gate_releases()
        self.assertEqual(hook.call_args[0][1]["MESH_RUNNER_PHASE"], "end")
        self.assertEqual(hook.call_args[0][1]["MESH_RUNNER_GATE_RES_ID"], "r1")
        self.assertEqual(pending(r, t["id"]), [])
        self.assertEqual(peer._local_writer(t["id"]), "")

    def test_reservation_without_an_end_hook_is_kept_pending(self):
        t = task(1)
        r = self.runner(FakeMesh([t]))
        self.agent.start_gate_command = ["operator-gate"]
        self.agent.turn_end_command = []
        a = r.store.create("builder", t["id"], "claude", "task 1")
        r.store.mark("builder", t["id"], "gate", json.dumps({"RES_ID": "r1", "RES_ADMITTED": "1"}))
        r._release_gate(a, "turn-end")
        self.assertEqual([x["gate"]["RES_ID"] for x in pending(r, t["id"])], ["r1"])

    def test_exhausted_unconfirmed_release_escalates_and_stops_retrying(self):
        t = task(1)
        r = self._gated(self.runner(FakeMesh([t])))
        a = r.store.create("builder", t["id"], "claude", "task 1")
        old = "2000-01-01T00:00:00Z"
        r.store.mark("builder", t["id"], GATE_RELEASE_PENDING, json.dumps([{
            "gate": {"RES_ID": "r1", "RES_BACKEND": "server", "RES_ADMITTED": "1"},
            "mode": "work", "attempt": a.id, "outcome": "turn-end",
            "tries": rmod.GATE_RELEASE_MAX_RETRIES - 1,
            "first_at": old, "next_at": old}]))
        alerts = []
        orig_emit = r.emit
        def capture(kind, attempt, **fields):
            if kind == "operator_alert":
                alerts.append(fields)
            return orig_emit(kind, attempt, **fields)
        with patch.object(r, "emit", side_effect=capture), \
                patch.object(r, "_run_hook", return_value=(0, "", "")) as hook, \
                self.assertLogs("mesh_runner", "WARNING") as logs:
            r.retry_gate_releases()
        self.assertEqual(hook.call_count, 1)
        [kept] = pending(r, t["id"])
        self.assertEqual(kept["tries"], rmod.GATE_RELEASE_MAX_RETRIES)
        self.assertTrue(kept["escalated"])
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["incident"], "gate_release_exhausted")
        self.assertEqual(alerts[0]["reservation_id"], "r1")
        self.assertTrue(any("operator alert" in line for line in logs.output))
        self.assertIn("gate_release_escalated", [k for _, k, _ in r.store.events(a.id)])
        self.assertIn("state=release escalated", r._local_writer(t["id"]))

        # Subsequent ticks (even past 6h) do NOT invoke the end hook again and never repeat the alert
        with patch.object(r, "emit", side_effect=capture), \
                patch.object(r, "_run_hook") as hook2:
            r.retry_gate_releases()
            r._escalate_gate_release(t["id"], pending(r, t["id"])[0], rmod.GATE_RELEASE_MAX_RETRIES,
                                     rmod.parse_time("2099-01-01T00:00:00Z"))
        hook2.assert_not_called()
        self.assertEqual(len(alerts), 1)
        self.assertEqual(pending(r, t["id"])[0]["tries"], rmod.GATE_RELEASE_MAX_RETRIES)

    def test_legacy_pending_already_at_or_over_cap_escalates_without_hook_call(self):
        t = task(1)
        r = self._gated(self.runner(FakeMesh([t])))
        a = r.store.create("builder", t["id"], "claude", "task 1")
        r.store.mark("builder", t["id"], GATE_RELEASE_PENDING, json.dumps([{
            "gate": {"RES_ID": "r-old", "RES_BACKEND": "server", "RES_ADMITTED": "1"},
            "mode": "work", "attempt": a.id, "outcome": "turn-end",
            "tries": 232, "first_at": "2000-01-01T00:00:00Z", "next_at": "2099-01-01T00:00:00Z"}]))
        alerts = []
        orig_emit = r.emit
        def capture(kind, attempt, **fields):
            if kind == "operator_alert":
                alerts.append(fields)
            return orig_emit(kind, attempt, **fields)
        with patch.object(r, "emit", side_effect=capture), \
                patch.object(r, "_run_hook") as hook, \
                self.assertLogs("mesh_runner", "WARNING"):
            r.retry_gate_releases()
        hook.assert_not_called()
        [kept] = pending(r, t["id"])
        self.assertEqual(kept["tries"], 232)
        self.assertTrue(kept["escalated"])
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["incident"], "gate_release_exhausted")

    def test_pending_or_escalated_release_blocks_resume_before_checkout(self):
        t = task(1, cat="in_progress")
        mesh = FakeMesh([t])
        r = self._gated(self.runner(mesh))
        a = r.store.create("builder", t["id"], "claude", "task 1")
        r.store.update(a, state="ready")
        r.store.mark("builder", t["id"], GATE_RELEASE_PENDING, json.dumps([{
            "gate": {"RES_ID": "r1", "RES_BACKEND": "server", "RES_ADMITTED": "1"},
            "mode": "work", "attempt": a.id, "outcome": "turn-end",
            "tries": rmod.GATE_RELEASE_MAX_RETRIES, "escalated": True,
            "first_at": "2000-01-01T00:00:00Z", "next_at": "2000-01-01T00:00:00Z"}]))
        with patch.object(r, "_run_hook") as hook, \
                patch.object(r, "_launch_ready", return_value=True), \
                patch.object(r, "_fresh_env", return_value={}):
            r._resume_one(r.store.get(a.id))
        hook.assert_not_called()
        self.assertEqual(mesh.checkouts, {})

    def test_non_admitted_intent_is_never_kept_or_retried_in_gate_release_pending(self):
        t = task(1)
        r = self._gated(self.runner(FakeMesh([t])))
        for gate_doc in ({"RES_ID": "r-intent", "RES_BACKEND": "server"},
                         {"RES_ID": "r-intent-0", "RES_BACKEND": "server", "RES_ADMITTED": "0"}):
            a = r.store.create("builder", t["id"], "claude", "task 1")
            r.store.mark("builder", t["id"], "gate", json.dumps(gate_doc))
            with patch.object(r, "_run_hook", return_value=(0, "", "")) as hook:
                r._release_gate(a, "not-started")
            self.assertEqual(hook.call_count, 1)
            self.assertEqual(pending(r, t["id"]), [])
            r.store.update(a, state="completed")

        # Pre-existing legacy non-admitted entry in gate_release_pending is cleaned up on retry
        r.store.mark("builder", t["id"], GATE_RELEASE_PENDING, json.dumps([{
            "gate": {"RES_ID": "r-legacy-intent", "RES_BACKEND": "server"},
            "mode": "work", "attempt": "", "outcome": "not-started", "tries": 42,
            "first_at": "2000-01-01T00:00:00Z", "next_at": "2099-01-01T00:00:00Z"}]))
        with patch.object(r, "_run_hook", return_value=(0, "", "")) as hook:
            r.retry_gate_releases()
        self.assertEqual(hook.call_count, 1)
        self.assertEqual(pending(r, t["id"]), [])

    def test_reservation_is_recorded_before_the_end_hook_runs(self):
        t = task(1)
        r = self._gated(self.runner(FakeMesh([t])))
        a = r.store.create("builder", t["id"], "claude", "task 1")
        r.store.mark("builder", t["id"], "gate", json.dumps({"RES_ID": "r1", "RES_ADMITTED": "1"}))
        seen = []

        def hook(argv, env, timeout):            # a crash here must leave the reservation recorded
            seen.append([x["gate"]["RES_ID"] for x in pending(r, t["id"])])
            return 0, "RELEASE_CONFIRMED=1", ""
        with patch.object(r, "_run_hook", side_effect=hook):
            r._release_gate(a, "turn-end")
        self.assertEqual(seen, [["r1"]])
        self.assertEqual(pending(r, t["id"]), [])                   # confirmed: dropped

    def test_two_unconfirmed_reservations_are_both_kept_and_hold_the_lane_itself(self):
        t = task(1)
        r = self._gated(self.runner(FakeMesh([t])))
        for res in ("r1", "r2"):
            a = r.store.create("builder", t["id"], "claude", "task 1")
            r.store.mark("builder", t["id"], "gate", json.dumps({"RES_ID": res, "RES_ADMITTED": "1"}))
            with patch.object(r, "_run_hook", return_value=(0, "", "")):
                r._release_gate(a, "turn-end")
            r.store.update(a, state="completed")
        self.assertEqual(sorted(x["gate"]["RES_ID"] for x in pending(r, t["id"])), ["r1", "r2"])
        self.assertIn("holder=builder, state=release pending", r._local_writer(t["id"]))
        r.store.mark("builder", t["id"], GATE_RELEASE_PENDING,
                     json.dumps([dict(x, next_at="2000-01-01T00:00:00Z") for x in pending(r, t["id"])]))
        with patch.object(r, "_run_hook", side_effect=[(0, "RELEASE_CONFIRMED=1", ""), (0, "", "")]):
            r.retry_gate_releases()
        self.assertEqual([x["gate"]["RES_ID"] for x in pending(r, t["id"])], ["r2"])

    def test_gate_without_a_reservation_leaves_nothing_pending(self):
        t = task(1)
        r = self._gated(self.runner(FakeMesh([t])))
        a = r.store.create("builder", t["id"], "claude", "task 1")
        r.store.mark("builder", t["id"], "gate", json.dumps({"ROUTE": "x"}))
        with patch.object(r, "_run_hook", return_value=(0, "", "")):
            r._release_gate(a, "turn-end")
        self.assertIsNone(r.store.mark("builder", t["id"], GATE_RELEASE_PENDING))


if __name__ == "__main__":
    unittest.main()
