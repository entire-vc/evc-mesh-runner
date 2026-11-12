import unittest
import json

from mesh_runner.card_wait import receipt_event
from mesh_runner.outcome import from_typed
from datetime import datetime, timezone

from fakes import AGENT_ID, FakeMesh, task
from mesh_runner.mesh import MeshError
from test_typed_outcome import TypedTurn


class ReceiptEvent(unittest.TestCase):
    def setUp(self):
        self.wait = {"mesh_wait_id": "wait-1", "server_observed": True}
        self.receipt = {"id": "wait-1", "task_id": "source", "agent_id": "agent",
                        "kind": "card", "server_observed": True}

    def event(self, **fields):
        return receipt_event(self.wait, dict(self.receipt, **fields), "source", "agent")

    def test_pending_then_met_without_target_state(self):
        self.assertEqual(self.event(), (False, ""))
        met, message = self.event(resolved_at="2026-11-11T16:00:00Z", resolution="met")
        self.assertTrue(met)
        self.assertIn("met", message)

    def test_revocation_wakes_without_certifying_target(self):
        met, message = self.event(resolved_at="now", resolution="invalid", resolve_cause="grant_revoked")
        self.assertFalse(met)
        self.assertIn("revoked", message)

    def test_mismatched_or_unmarked_receipt_never_wakes(self):
        for fields in ({"id": "other"}, {"task_id": "target"}, {"agent_id": "other"},
                       {"kind": "time"}, {"server_observed": False}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                self.event(**fields)

    def test_unknown_resolution_does_not_claim_success(self):
        with self.assertRaises(ValueError):
            self.event(resolved_at="now", resolution="unknown")

    def test_typed_mapping_preserves_server_observer(self):
        target = "0f1e2d3c-0000-4000-8000-000000000001"
        mapped = from_typed({"id": "outcome", "kind": "wait", "wait_id": "wait-1"},
                            dict(self.receipt, target_task_id=target, target_condition="deployed"),
                            datetime.now(timezone.utc))
        self.assertEqual(mapped.kind, "card")
        self.assertIs(mapped.typed["server_observed"], True)


    def test_resolved_deleted_target_still_settles_typed_turn(self):
        mapped = from_typed({"id": "outcome", "kind": "wait", "wait_id": "wait-1"},
                            dict(self.receipt, target_task_id=None, resolved_at="now",
                                 resolution="invalid", resolve_cause="target_deleted"),
                            datetime.now(timezone.utc))
        self.assertEqual(mapped.kind, "card")
        self.assertIs(mapped.typed["server_observed"], True)


class SourceReceiptMesh(FakeMesh):
    """External HTTP boundary; Runner lifecycle and SQLite remain real."""
    strict = False
    receipt_error = None

    def wait_receipt(self, tid, wait_id):
        assert tid == self.source_id and wait_id == self.receipt["id"]
        if self.receipt_error:
            raise self.receipt_error
        return dict(self.receipt)

    def active_wait(self, tid, agent_id):
        raise AssertionError("server-observed settlement must read its exact receipt")

    def task(self, tid):
        if self.strict:
            assert tid == self.source_id, "foreign task read"
        return super().task(tid)

    def task_by_short(self, short):
        if self.strict:
            raise AssertionError("foreign short-ID lookup")
        return super().task_by_short(short)

    def dependencies(self, tid):
        if self.strict:
            raise AssertionError("legacy dependency observer")
        return super().dependencies(tid)

    def add_dependency(self, tid, dep, condition=None):
        raise AssertionError("ordinary dependency write")


class RunnerReceiptFlow(TypedTurn):
    def flow(self, **resolved):
        source = task(1)
        mesh = SourceReceiptMesh([source], max_concurrent=1)
        mesh.source_id = source["id"]
        r = self.run_turn(mesh, source)
        receipt = dict(id="w-1", task_id=source["id"], agent_id=AGENT_ID,
                       kind="card", server_observed=True,
                       target_task_id="0f1e2d3c-0000-4000-8000-000000000001",
                       target_condition="deployed")
        receipt.update(resolved)
        self.typed(mesh, source, "wait", receipt)
        mesh.outcomes[source["id"]][0]["wait_server_observed"] = True
        mesh.receipt = receipt
        mesh.strict = True
        a = self.finish_turn(r)
        self.assertEqual(a.state, "waiting")
        self.assertEqual(a.wait["mesh_wait_id"], "w-1")
        self.assertIs(a.wait["server_observed"], True)
        self.assertEqual(mesh.edges, {})
        return r, a, mesh

    def test_pending_receipt_survives_restart_then_wakes_once(self):
        r, a, mesh = self.flow()
        r._wake_one(a)
        self.assertEqual(r.store.get(a.id).state, "waiting")
        r.store.db.close()
        recovered = self.runner(mesh)
        recovered.refresh_me()
        stale = recovered.store.get(a.id)
        self.assertEqual(stale.wait, a.wait)
        mesh.receipt.update(resolved_at="now", resolution="met")
        recovered._wake_one(stale)
        ready = recovered.store.get(a.id)
        self.assertEqual(ready.state, "ready")
        self.assertEqual(ready.session_id, a.session_id)
        self.assertIn("condition as met", ready.wake_text)
        self.assertNotIn(mesh.receipt["target_task_id"], ready.wake_text)
        before = recovered.store.events(a.id)
        recovered._wake_one(stale)  # duplicate observer using a stale waiting snapshot
        self.assertEqual(recovered.store.events(a.id), before)
        self.assertEqual(recovered.store.mark(a.agent, a.task_id, recovered.WAIT_RESOLVED)[0], "w-1")
        self.assertFalse(getattr(mesh, "resolved", []))
        emitted = [json.loads(line) for line in (self.cfg.state_dir / "events.jsonl").read_text().splitlines()]
        self.assertEqual(len([e for e in emitted if e["kind"] == "wake" and e.get("attempt") == a.id]), 1)

    def test_already_resolved_deleted_target_settles_and_wakes(self):
        r, a, mesh = self.flow(resolved_at="now", resolution="invalid",
                               resolve_cause="target_deleted", target_task_id=None)
        mapped, _ = r._typed_outcome(a, None)
        self.assertEqual(mapped.kind, "card")
        r._wake_one(a)
        self.assertEqual(r.store.get(a.id).state, "ready")
        self.assertFalse(r.store.mark(a.agent, a.task_id, "met_wake"))

    def test_revocation_wakes_without_met_mark(self):
        r, a, mesh = self.flow(resolved_at="now", resolution="invalid",
                               resolve_cause="grant_revoked")
        r._wake_one(a)
        ready = r.store.get(a.id)
        self.assertEqual(ready.state, "ready")
        self.assertIn("membership was revoked", ready.wake_text)
        self.assertFalse(r.store.mark(a.agent, a.task_id, "met_wake"))
        self.assertFalse(getattr(mesh, "resolved", []))

    def test_failed_or_mismatched_receipt_stays_waiting_without_fallback(self):
        r, a, mesh = self.flow()
        for error in (MeshError(404, "not found"), MeshError(503, "unavailable"), OSError("offline")):
            mesh.receipt_error = error
            r._guard(a, "wake", r._wake_one)
            self.assertEqual(r.store.get(a.id).state, "waiting")
        mesh.receipt_error = None
        mesh.receipt.update(resolved_at="now", resolution="met", agent_id="wrong-agent")
        r._wake_one(a)
        self.assertEqual(r.store.get(a.id).state, "waiting")
        self.assertTrue(self.events(r, a, "wake_receipt_invalid"))
