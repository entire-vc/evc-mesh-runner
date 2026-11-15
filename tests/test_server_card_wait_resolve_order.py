"""A server-observed card WAIT woken by a comment ends in Mesh as ``cancelled``, not ``invalid``.

Observed (live, Runner 0.2.55): a server-observed wait woken by a comment on its own card removed its
blocks edge first (``on_end`` -> owner-checked delete) and only then sent ``cancelled``. For a
server-observed wait the edge removal is a TARGET event: the Mesh trigger
``server_card_wait_dependency_event`` ends the open wait as ``invalid``/``dependency_removed``, so the
runner's ``cancelled`` came back 409 "the wait already ended as invalid" (final, logged, an
``mesh_wait_resolve_failed`` event) and the wait's history was wrong.

Order now: the wake (READY) first, then the best-effort resolve with the comment as its cause, while
the edge still stands; the edge goes on the resume cleanup, after the wait has ended. The runner-observed
path (no trigger) and the met path are unchanged.

``TriggerMesh`` is the external Mesh boundary: an owned edge, the wait row and the trigger as evc-mesh
stores them. Runner, Store and Effects are real.
"""
import datetime as dt
import json
import unittest
from unittest import mock

from fakes import AGENT_ID, task
from mesh_runner import lifecycle
from mesh_runner.mesh import MeshError
from test_card_wait_owned_edge import EDGE, OwnerCheckedMesh, server_edge
from test_card_wait_receipt import SourceReceiptMesh
from test_typed_outcome import TypedTurn

TARGET = "0f1e2d3c-0000-4000-8000-000000000001"


class TriggerMesh(SourceReceiptMesh, OwnerCheckedMesh):
    """The wait row lives in ``receipt`` (what GET .../waits/:id returns). Deleting the edge of an OPEN
    server-observed wait ends it as ``invalid``/``dependency_removed`` (evc-mesh migration
    20261111400, ``server_card_wait_dependency_event``); resolving an ended wait with another
    resolution is 409, the same one is idempotent (``typedOutcomeService.ResolveWait``)."""

    def __init__(self, tasks):
        OwnerCheckedMesh.__init__(self, tasks)

    def dependencies(self, tid):                     # the strict SourceReceiptMesh read guard stays
        return SourceReceiptMesh.dependencies(self, tid)

    def delete_wait_dependency(self, tid, edge_id, generation, wait_id):
        OwnerCheckedMesh.delete_wait_dependency(self, tid, edge_id, generation, wait_id)
        w = self.receipt
        if w.get("server_observed") and not w.get("resolved_at") and w.get("task_id") == tid:
            w.update(resolved_at="now", resolution="invalid", resolve_cause="dependency_removed")

    def resolve_wait(self, tid, wait_id, resolution, cause):
        self.resolved = getattr(self, "resolved", [])
        self.resolved.append((tid, wait_id, resolution, cause))
        errors = getattr(self, "resolve_errors", [])
        if errors:
            raise errors.pop(0)
        w = self.receipt
        assert w["id"] == wait_id
        if w.get("resolved_at"):
            if w.get("resolution") == resolution:
                return {"wait": dict(w), "resolved": False}
            raise MeshError(409, f"the wait already ended as {w.get('resolution')}")
        w.update(resolved_at="now", resolution=resolution, resolve_cause=cause)
        return {"wait": dict(w), "resolved": True}


class CommentWakeResolvesBeforeTheEdge(TypedTurn):
    def flow(self, condition="artifact_published"):
        self.cfg.comment_wake = "addressed"
        source = task(1)
        mesh = TriggerMesh([source])
        mesh.source_id = source["id"]
        r = self.run_turn(mesh, source)
        mesh.deps[source["id"]] = [server_edge(source, {"id": TARGET}, condition, "w-1")]
        receipt = dict(id="w-1", task_id=source["id"], agent_id=AGENT_ID, kind="card", server_observed=True,
                       target_task_id=TARGET, target_condition=condition,
                       dependency_id=EDGE, dependency_generation=1, dependency_owned=True)
        self.typed(mesh, source, "wait", receipt)
        mesh.outcomes[source["id"]][0]["wait_server_observed"] = True
        mesh.receipt = receipt
        mesh.strict = True                           # no target, edge-set or foreign read
        a = self.finish_turn(r)
        self.assertEqual((a.state, a.wait["server_observed"]), ("waiting", True))
        self.assertEqual((a.wait["edge"], a.wait["edge_owner_wait"]), (EDGE, "w-1"))   # the live shape
        return r, a, mesh, source

    def question(self, mesh, a, body="подтверди, что экспорт снова включён"):
        c = mesh.add_comment(a.task_id, f"@{self.agent.name} {body}", author="Canary User",
                             at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=10))
        c["author_type"] = "user"
        return c

    def resume_cleanup(self, r, a):
        """``_resume_one`` up to the launch: the cleanup a READY attempt's wake left behind runs first."""
        woken = r.store.get(a.id)
        self.assertTrue(r.effects.pending_cleanup(woken.wait))
        with mock.patch.object(type(r), "_launch_ready", return_value=False):
            r._resume_one(woken)
        return r.store.get(a.id).wait

    # AC1: the live case, red on 0.2.55 (invalid/dependency_removed + 409), green with the fix
    def test_comment_wake_ends_the_wait_cancelled_with_the_comment_as_cause(self):
        r, a, mesh, s = self.flow()
        self.question(mesh, a)
        r._wake_budgeted(a)
        b = r.store.get(a.id)
        self.assertEqual(b.state, "ready")
        self.assertIn("экспорт снова включён", b.wake_text)
        self.assertIsNone(r.store.mark(self.agent.name, a.task_id, "met_wake"))
        self.assertEqual((mesh.receipt["resolution"], mesh.receipt.get("resolve_cause") != "dependency_removed"),
                         ("cancelled", True))
        self.assertIn("экспорт снова включён", mesh.receipt["resolve_cause"])
        self.assertEqual([x[:3] for x in mesh.resolved], [(s["id"], "w-1", "cancelled")])
        self.assertFalse(self.events(r, a, "mesh_wait_resolve_failed"))
        self.assertEqual(r.store.mark(self.agent.name, a.task_id, r.WAIT_RESOLVED)[0], "w-1")
        # the target stays held until the wait has ended; the edge goes on the resume cleanup
        self.assertEqual([e["id"] for e in mesh.deps[s["id"]]], [EDGE])
        self.assertEqual(mesh.wait_deletes, [])
        wait = self.resume_cleanup(r, a)
        self.assertEqual(mesh.wait_deletes, [(s["id"], EDGE, 1, "w-1")])
        self.assertEqual(mesh.deps[s["id"]], [])
        self.assertFalse({"edge", "edge_generation", "edge_owner_wait"} & set(wait))
        self.assertEqual(mesh.receipt["resolution"], "cancelled")   # the trigger leaves an ended wait alone
        self.assertEqual(len(mesh.resolved), 1)

    def test_wait_cycle_wake_ends_the_wait_cancelled_too(self):
        r, a, mesh, s = self.flow()
        theirs = r.store.create("owner-lane", TARGET, "claude", "fixture")
        r.store.update(theirs, state="waiting", wait={
            "kind": "decision", "handle": self.agent.name, "since": a.wait["since"], "started": a.wait["since"],
            "line": f"⏳ WAIT decision:@{self.agent.name}: подтверди путь установки"})
        r._wake_budgeted(a)
        self.assertIn("wait cycle", r.store.get(a.id).wake_text)
        self.assertEqual(mesh.receipt["resolution"], "cancelled")
        self.assertEqual([e["id"] for e in mesh.deps[s["id"]]], [EDGE])

    # AC2: wake first, then the best-effort resolve; a failed resolve loses no wake
    def test_a_failed_resolve_keeps_the_wake_and_resolves_before_the_edge_goes(self):
        r, a, mesh, s = self.flow()
        self.question(mesh, a)
        mesh.resolve_errors = [MeshError(503, "unavailable")]
        r._wake_budgeted(a)
        b = r.store.get(a.id)
        self.assertEqual(b.state, "ready")                               # the wake is kept
        self.assertIn("экспорт снова включён", b.wake_text)
        self.assertIsNone(mesh.receipt.get("resolved_at"))               # still open: nothing ended it
        self.assertTrue(self.events(r, a, "mesh_wait_resolve_failed"))
        self.resume_cleanup(r, a)                                        # the pending resolve goes first
        self.assertEqual([x[2] for x in mesh.resolved], ["cancelled", "cancelled"])
        self.assertEqual(mesh.receipt["resolution"], "cancelled")
        self.assertEqual(mesh.deps[s["id"]], [])
        self.assertIsNone(r.store.mark(self.agent.name, a.task_id, r.WAIT_RESOLVE)[0] or None)

    def test_a_resolve_still_failing_at_resume_keeps_the_edge_and_holds_the_resume(self):
        """Review finding: the resume must not remove the edge while the resolve is still pending."""
        r, a, mesh, s = self.flow()
        self.question(mesh, a)
        mesh.resolve_errors = [MeshError(503, "unavailable"), MeshError(503, "unavailable")]
        r._wake_budgeted(a)
        launched = []
        with mock.patch.object(type(r), "_launch_ready", side_effect=lambda *x, **k: launched.append(1) or False):
            r._resume_one(r.store.get(a.id))                             # second send fails too
            b = r.store.get(a.id)
            self.assertEqual((b.state, b.wait["edge"], launched), ("ready", EDGE, []))   # held, not launched
            self.assertEqual([e["id"] for e in mesh.deps[s["id"]]], [EDGE])
            self.assertEqual(mesh.wait_deletes, [])
            self.assertIsNone(mesh.receipt.get("resolved_at"))           # never ended as invalid
            r._resume_one(r.store.get(a.id))                             # third send goes through
        self.assertEqual(launched, [1])
        self.assertEqual([x[2] for x in mesh.resolved], ["cancelled"] * 3)
        self.assertEqual(mesh.receipt["resolution"], "cancelled")
        self.assertEqual(mesh.deps[s["id"]], [])
        self.assertNotIn("edge", r.store.get(a.id).wait)

    def test_the_resume_hold_is_bounded_by_the_resolve_tries(self):
        """A resolve endpoint that keeps failing does not hold the card: after ``WAIT_RESOLVE_TRIES``
        sends the resolve is final (given up, logged) and the resume removes the edge."""
        r, a, mesh, s = self.flow()
        self.question(mesh, a)
        mesh.resolve_errors = [MeshError(503, "unavailable")] * r.WAIT_RESOLVE_TRIES
        r._wake_budgeted(a)
        for _ in range(r.WAIT_RESOLVE_TRIES - 1):
            self.assertTrue(r.effects.pending_cleanup(r.store.get(a.id).wait))
            self.resume_cleanup(r, a)
        self.assertEqual(len(mesh.resolved), r.WAIT_RESOLVE_TRIES)
        self.assertEqual(mesh.deps[s["id"]], [])
        self.assertEqual(r.store.mark(self.agent.name, a.task_id, r.WAIT_RESOLVED)[0], "w-1")

    def test_an_unexpected_resolve_exception_counts_as_a_try_and_keeps_the_edge(self):
        """Second review finding: an exception other than MeshError/OSError from the send is one more
        failed try, not "nothing pending": the edge stays until the resolve is final."""
        r, a, mesh, s = self.flow()
        self.question(mesh, a)
        mesh.resolve_errors = [RuntimeError("unexpected"), RuntimeError("unexpected")]
        r._wake_budgeted(a)
        self.assertEqual(r.store.get(a.id).state, "ready")
        self.assertTrue(self.events(r, a, "mesh_wait_resolve_failed"))
        self.resume_cleanup(r, a)                                        # second send raises too: held
        self.assertEqual(r.store.get(a.id).wait["edge"], EDGE)
        self.assertEqual((mesh.wait_deletes, mesh.receipt.get("resolved_at")), ([], None))
        self.resume_cleanup(r, a)                                        # third send goes through
        self.assertEqual(mesh.receipt["resolution"], "cancelled")
        self.assertEqual(mesh.deps[s["id"]], [])

    def test_an_unreadable_resolve_state_at_resume_keeps_the_edge(self):
        r, a, mesh, s = self.flow()
        self.question(mesh, a)
        mesh.resolve_errors = [MeshError(503, "unavailable")]
        r._wake_budgeted(a)

        def broken(*_a, **_k):
            raise RuntimeError("store unreadable")
        launched = []
        with mock.patch.object(type(r), "_settle_wait_resolve", side_effect=broken), \
                mock.patch.object(type(r), "_launch_ready", side_effect=lambda *x, **k: launched.append(1) or False):
            r._resume_one(r.store.get(a.id))
        b = r.store.get(a.id)
        self.assertEqual((b.state, b.wait["edge"], launched), ("ready", EDGE, []))
        self.assertEqual((mesh.wait_deletes, mesh.receipt.get("resolved_at")), ([], None))
        self.resume_cleanup(r, a)                                        # readable again: sent, then cleaned
        self.assertEqual(mesh.receipt["resolution"], "cancelled")
        self.assertEqual(mesh.deps[s["id"]], [])

    def test_a_resolve_that_raises_outright_still_keeps_the_wake(self):
        r, a, mesh, s = self.flow()
        self.question(mesh, a)

        def broken(*_a, **_k):
            raise RuntimeError("bookkeeping bug")
        r._queue_wait_resolve = broken
        r._wake_budgeted(a)
        self.assertEqual(r.store.get(a.id).state, "ready")
        self.assertIsNone(mesh.receipt.get("resolved_at"))           # nothing was queued
        self.resume_cleanup(r, a)                                    # the resume resolves it first
        self.assertEqual(mesh.receipt["resolution"], "cancelled")
        self.assertIn("экспорт снова включён", mesh.receipt["resolve_cause"])
        self.assertEqual(mesh.deps[s["id"]], [])

    def corrupt_record(self, r, a):
        r.store.mark(self.agent.name, a.task_id, r.WAIT_RESOLVE, "{not json")

    def test_a_corrupt_resolve_record_is_rebuilt_before_the_edge_goes(self):
        """Third review finding: an unreadable record is unknown, never final."""
        r, a, mesh, s = self.flow()
        self.question(mesh, a)
        mesh.resolve_errors = [MeshError(503, "unavailable")]
        r._wake_budgeted(a)
        self.corrupt_record(r, a)
        mesh.resolve_errors = [MeshError(503, "unavailable")]
        self.resume_cleanup(r, a)                                    # rebuilt, sent, failed: held
        self.assertEqual((r.store.get(a.id).wait["edge"], mesh.receipt.get("resolved_at")), (EDGE, None))
        self.assertEqual(mesh.wait_deletes, [])
        self.resume_cleanup(r, a)                                    # sent: cancelled, then the edge
        self.assertEqual(mesh.receipt["resolution"], "cancelled")
        self.assertIn("экспорт снова включён", mesh.receipt["resolve_cause"])
        self.assertEqual(mesh.deps[s["id"]], [])

    def malformed_record_is_rebuilt(self, bad):
        """Fourth review finding: a JSON object naming this wait with a bad field (``"tries": "bad"``)
        raised before the send and before the try was counted, so the resume was held for good."""
        r, a, mesh, s = self.flow()
        self.question(mesh, a)
        mesh.resolve_errors = [MeshError(503, "unavailable")]
        r._wake_budgeted(a)
        good = json.loads(r.store.mark(self.agent.name, a.task_id, r.WAIT_RESOLVE)[0])
        record = {k: v for k, v in {**good, **bad}.items() if v is not DROP}
        r.store.mark(self.agent.name, a.task_id, r.WAIT_RESOLVE, json.dumps(record))
        mesh.resolve_errors = [MeshError(503, "unavailable")]
        self.resume_cleanup(r, a)                                    # rebuilt, sent, failed: held
        self.assertEqual((r.store.get(a.id).wait["edge"], mesh.receipt.get("resolved_at")), (EDGE, None))
        self.assertEqual(json.loads(r.store.mark(self.agent.name, a.task_id, r.WAIT_RESOLVE)[0])["tries"], 1)
        self.resume_cleanup(r, a)                                    # sent: cancelled, then the edge
        self.assertEqual(mesh.receipt["resolution"], "cancelled")
        self.assertIn("экспорт снова включён", mesh.receipt["resolve_cause"])
        self.assertEqual(mesh.deps[s["id"]], [])

    def test_the_tick_retry_drops_a_malformed_record_instead_of_raising(self):
        r, a, mesh, s = self.flow()
        self.question(mesh, a)
        mesh.resolve_errors = [MeshError(503, "unavailable")]
        r._wake_budgeted(a)
        good = json.loads(r.store.mark(self.agent.name, a.task_id, r.WAIT_RESOLVE)[0])
        r.store.mark(self.agent.name, a.task_id, r.WAIT_RESOLVE, json.dumps({**good, "tries": "bad"}))
        r._retry_wait_resolves()                                     # no exception out of the tick
        self.assertEqual(r.store.marked(self.agent.name, r.WAIT_RESOLVE), {})
        self.assertEqual(len(mesh.resolved), 1)
        self.resume_cleanup(r, a)                                    # the resume rebuilds and sends it
        self.assertEqual((mesh.receipt["resolution"], mesh.deps[s["id"]]), ("cancelled", []))

    def test_another_waits_record_does_not_release_this_edge(self):
        r, a, mesh, s = self.flow()
        self.question(mesh, a)
        mesh.resolve_errors = [MeshError(503, "unavailable")]
        r._wake_budgeted(a)
        r.store.mark(self.agent.name, a.task_id, r.WAIT_RESOLVED, "w-older")
        r.store.mark(self.agent.name, a.task_id, r.WAIT_RESOLVE, json.dumps(
            {"attempt": a.id, "wait": "w-older", "resolution": "cancelled", "cause": "x", "tries": 0}))
        mesh.resolve_errors = [MeshError(503, "unavailable")]
        self.resume_cleanup(r, a)
        self.assertEqual((r.store.get(a.id).wait["edge"], mesh.receipt.get("resolved_at")), (EDGE, None))
        self.resume_cleanup(r, a)
        self.assertEqual((mesh.receipt["resolution"], mesh.deps[s["id"]]), ("cancelled", []))

    def test_lost_ready_cas_writes_nothing_to_mesh(self):
        """Before: the edge was gone before the READY CAS; a lost CAS left an invalid wait behind."""
        r, a, mesh, s = self.flow()
        self.question(mesh, a)
        with mock.patch.object(lifecycle.AttemptStateMachine, "to_ready", return_value=False):
            r._wake_budgeted(a)
        self.assertEqual(r.store.get(a.id).state, "waiting")
        self.assertIsNone(mesh.receipt.get("resolved_at"))
        self.assertEqual((mesh.wait_deletes, getattr(mesh, "resolved", [])), ([], []))

    def test_read_failure_wake_ends_the_wait_cancelled_too(self):
        """The other wake that leaves a server-observed wait unresolved by Mesh: its receipt unreadable."""
        r, a, mesh, s = self.flow()
        mesh.receipt_error = MeshError(503, "unavailable")
        for _ in range(r.WAKE_READ_FAILURES - 1):
            r._wake_budgeted(r.store.get(a.id))
        state = json.loads(r.store.mark(self.agent.name, a.task_id, r.READ_FAILURES)[0])
        state["first"] = (dt.datetime.now(dt.timezone.utc) - r.WAKE_READ_FAILURE_SPAN
                          - dt.timedelta(minutes=1)).isoformat()
        r._read_failures[a.id] = state
        r._wake_budgeted(r.store.get(a.id))
        b = r.store.get(a.id)
        self.assertEqual(b.state, "ready")
        self.assertIn("UNKNOWN", b.wake_text)
        self.assertEqual((mesh.receipt["resolution"], [x[2] for x in mesh.resolved]), ("cancelled", ["cancelled"]))
        self.assertEqual([e["id"] for e in mesh.deps[s["id"]]], [EDGE])
        mesh.receipt_error = None
        self.resume_cleanup(r, a)
        self.assertEqual((mesh.deps[s["id"]], mesh.receipt["resolution"]), ([], "cancelled"))

    # unchanged: the met path (Mesh resolved the receipt itself; nothing is sent)
    def test_met_receipt_is_not_resolved_again_and_its_edge_goes_on_resume(self):
        r, a, mesh, s = self.flow()
        mesh.receipt.update(resolved_at="now", resolution="met")
        r._wake_budgeted(a)
        b = r.store.get(a.id)
        self.assertEqual(b.state, "ready")
        self.assertIn("condition as met", b.wake_text)
        self.assertFalse(getattr(mesh, "resolved", []))
        self.resume_cleanup(r, a)
        self.assertEqual(mesh.deps[s["id"]], [])
        self.assertEqual(mesh.receipt["resolution"], "met")


DROP = object()                                      # the field is left out of the record
MALFORMED = {"tries_text": {"tries": "bad"}, "tries_negative": {"tries": -1}, "tries_bool": {"tries": True},
             "tries_float": {"tries": 1.5}, "tries_null": {"tries": None}, "tries_missing": {"tries": DROP},
             "resolution_unknown": {"resolution": "invalid"}, "resolution_null": {"resolution": None},
             "resolution_missing": {"resolution": DROP}, "cause_number": {"cause": 5},
             "cause_null": {"cause": None}, "cause_missing": {"cause": DROP}, "attempt_list": {"attempt": ["x"]},
             "attempt_null": {"attempt": None}, "attempt_missing": {"attempt": DROP},
             "wait_empty": {"wait": ""}, "wait_missing": {"wait": DROP}}
for _name, _bad in MALFORMED.items():
    setattr(CommentWakeResolvesBeforeTheEdge, f"test_a_malformed_record_{_name}_is_rebuilt_before_the_edge_goes",
            lambda self, bad=_bad: self.malformed_record_is_rebuilt(bad))


class RunnerObservedCommentWakeUnchanged(TypedTurn):
    """The runner-observed wait (same workspace, no trigger) still removes its edge at the wake and is
    resolved ``cancelled``, as on 0.2.54."""

    def test_runner_observed_held_wake_removes_the_edge_at_the_wake(self):
        self.cfg.comment_wake = "addressed"
        source, target = task(1), task(3)
        mesh = OwnerCheckedMesh([source, target])
        r = self.run_turn(mesh, source)
        mesh.deps[source["id"]] = [server_edge(source, target, "artifact_published", "w-1")]
        self.typed(mesh, source, "wait", {"kind": "card", "target_task_id": target["id"],
                                          "target_condition": "artifact_published", "dependency_id": EDGE,
                                          "dependency_generation": 1, "dependency_owned": True})
        a = self.finish_turn(r)
        self.assertNotIn("server_observed", a.wait)
        c = mesh.add_comment(a.task_id, f"@{self.agent.name} вопрос", author="Canary User",
                             at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=10))
        c["author_type"] = "user"
        r._wake_budgeted(a)
        b = r.store.get(a.id)
        self.assertEqual(b.state, "ready")
        self.assertEqual(mesh.wait_deletes, [(source["id"], EDGE, 1, "w-1")])
        self.assertNotIn("edge", b.wait)
        self.assertEqual([x[2] for x in mesh.resolved], ["cancelled"])


if __name__ == "__main__":
    unittest.main()
