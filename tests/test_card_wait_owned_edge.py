"""A typed ``wait_for on=card`` owns the blocks edge Mesh made for it (receipt ``dependency_owned``) and
removes exactly that edge when the wait ends, with the owner-checked delete.

Before: the runner saw the server's edge as an operator's (``observe_edge``) and never removed it, so a
later wait the other way round was refused as a dependency cycle (422). An edge this wait does not own
(no owner, another wait's) is still never touched.

FakeMesh is the external Mesh boundary (edge generation + owner as evc-mesh stores them); Runner, Store
and Effects are real.
"""
import datetime as dt
import unittest

from fakes import AGENT_ID, FakeMesh, task
from mesh_runner.mesh import MeshClient, MeshError
from mesh_runner.outcome import from_typed
from test_typed_outcome import TypedTurn

EDGE = "0e0e0e0e-0000-4000-8000-000000000001"
NOW = dt.datetime(2026, 11, 14, tzinfo=dt.timezone.utc)


class OwnerCheckedMesh(FakeMesh):
    """Edges with ``generation`` and ``owner_wait_id``; DELETE with expected_generation and
    expected_owner_wait_id is the storage compare-delete of evc-mesh (task_dependency_repo.deleteIf)."""

    def __init__(self, tasks):
        super().__init__(tasks, max_concurrent=1)
        self.wait_deletes, self.plain_deletes, self.wait_delete_errors, self.receipts = [], [], [], {}

    def delete_dependency(self, tid, edge_id):
        self.plain_deletes.append((tid, edge_id))
        self.deps[tid] = [e for e in self.deps.get(tid, []) if e["id"] != edge_id]
        super().delete_dependency(tid, edge_id)

    def delete_wait_dependency(self, tid, edge_id, generation, wait_id):
        self.wait_deletes.append((tid, edge_id, generation, wait_id))
        if self.wait_delete_errors:
            raise self.wait_delete_errors.pop(0)
        edge = next((e for e in self.deps.get(tid, []) if e["id"] == edge_id), None)
        if edge is None:
            raise MeshError(404, "TaskDependency not found")
        if edge["generation"] != generation or edge.get("owner_wait_id") != wait_id:
            raise MeshError(409, "stale dependency generation")
        self.deps[tid] = [e for e in self.deps[tid] if e["id"] != edge_id]

    def wait_receipt(self, tid, wait_id):
        return dict(self.receipts[wait_id])


def server_edge(source, target, condition, owner):
    """The row linkWaitTarget writes: blocks, the wait's condition, generation 1, the owning wait."""
    return {"id": EDGE, "task_id": source["id"], "depends_on_task_id": target["id"], "dependency_type": "blocks",
            "condition": condition, "condition_satisfied": False, "generation": 1, "owner_wait_id": owner}


class ReceiptFields(unittest.TestCase):
    def test_card_outcome_carries_the_receipt_edge(self):
        row = {"id": "o-1", "kind": "wait", "wait_id": "w-1"}
        wait = {"id": "w-1", "kind": "card", "target_task_id": "0f1e2d3c-0000-4000-8000-000000000001",
                "dependency_id": EDGE, "dependency_generation": 1, "dependency_owned": True}
        out = from_typed(row, wait, NOW)
        self.assertEqual(out.extra["card_dependency"], {"id": EDGE, "generation": 1, "owned": True})
        out = from_typed(row, dict(wait, dependency_owned=False), NOW)
        self.assertIs(out.extra["card_dependency"]["owned"], False)
        older = {k: v for k, v in wait.items() if not k.startswith("dependency_")}
        self.assertNotIn("card_dependency", from_typed(row, older, NOW).extra)   # an older Mesh: nothing known

    def test_client_delete_sends_both_ownership_predicates(self):
        sent = []
        client = MeshClient("https://mesh.example", "k")
        client.request = lambda method, path, body=None, query=None, **kw: sent.append((method, path, query))
        client.delete_wait_dependency("t1", EDGE, 1, "w-1")
        self.assertEqual(sent, [("DELETE", f"/tasks/t1/dependencies/{EDGE}",
                                 {"expected_generation": 1, "expected_owner_wait_id": "w-1"})])


class TypedCardWaitEdge(TypedTurn):
    def flow(self, *, owned=True, owner="w-1", condition="artifact_published"):
        source, target = task(1), task(3)
        mesh = OwnerCheckedMesh([source, target])
        r = self.run_turn(mesh, source)
        mesh.deps[source["id"]] = [server_edge(source, target, condition, owner)]
        receipt = {"kind": "card", "target_task_id": target["id"], "target_condition": condition,
                   "dependency_id": EDGE, "dependency_generation": 1, "dependency_owned": owned}
        self.typed(mesh, source, "wait", receipt)          # the receipt is wait w-1
        a = self.finish_turn(r)
        self.assertEqual((a.state, a.wait["kind"], a.wait["mesh_wait_id"]), ("waiting", "card", "w-1"))
        return r, a, mesh, source, target

    def satisfy(self, mesh, source):
        mesh.deps[source["id"]][0]["condition_satisfied"] = True     # the producer's evidence released it

    def test_owned_edge_is_bound_and_removed_owner_checked_after_the_wake(self):
        """AC1, the live case: artifact_published satisfied -> wake -> the next turn's cleanup deletes
        exactly this wait's edge; the reverse wait then has no edge to close a cycle with."""
        r, a, mesh, s, g = self.flow()
        self.assertEqual((a.wait["edge"], a.wait["edge_generation"], a.wait["edge_owner_wait"]), (EDGE, 1, "w-1"))
        self.assertNotIn("observe_edge", a.wait)
        self.assertEqual(mesh.edges, {})                                     # the runner wrote no edge itself
        self.satisfy(mesh, s)
        r._wake_one(a)
        woken = r.store.get(a.id)
        self.assertEqual(woken.state, "ready")
        self.assertIn("artifact_published", woken.wake_text)
        self.assertTrue(r.effects.pending_cleanup(woken.wait))              # the resume path cleans it up
        wait = dict(woken.wait)
        r.effects.on_end(woken, wait, "resuming", close_request=False)       # what _resume_one runs first
        self.assertEqual(mesh.wait_deletes, [(s["id"], EDGE, 1, "w-1")])
        self.assertEqual(mesh.plain_deletes, [])                             # never the unconditional delete
        self.assertEqual(mesh.dependencies(s["id"])["outgoing"], [])
        self.assertFalse(r.effects.pending_cleanup(wait))
        self.assertFalse({"edge", "edge_generation", "edge_owner_wait"} & set(wait))

    def test_owned_task_done_edge_is_removed_when_the_target_closes(self):
        r, a, mesh, s, g = self.flow(condition="task_done")
        g["status_id"] = "s-done"
        self.satisfy(mesh, s)
        r._wake_one(a)
        self.assertEqual(r.store.get(a.id).state, "ready")
        self.assertEqual(mesh.wait_deletes, [(s["id"], EDGE, 1, "w-1")])
        self.assertEqual(mesh.dependencies(s["id"])["outgoing"], [])

    def unowned_stays(self, owner):
        r, a, mesh, s, g = self.flow(owned=False, owner=owner, condition="task_done")
        self.assertEqual(a.wait.get("observe_edge"), EDGE)
        self.assertNotIn("edge", a.wait)
        g["status_id"] = "s-done"
        self.satisfy(mesh, s)
        r._wake_one(a)
        woken = r.store.get(a.id)
        self.assertEqual(woken.state, "ready")
        r.effects.on_end(woken, dict(woken.wait), "resuming", close_request=False)
        self.assertEqual((mesh.wait_deletes, mesh.plain_deletes), ([], []))
        self.assertEqual([e["id"] for e in mesh.dependencies(s["id"])["outgoing"]], [EDGE])

    def test_edge_without_owner_is_never_removed(self):
        """AC2: an edge with no owner (a person's, a lead's) stays after the wake."""
        self.unowned_stays(None)

    def test_edge_of_another_wait_is_never_removed(self):
        """AC2: an edge another wait owns (owned=false on this receipt) stays after the wake."""
        self.unowned_stays("w-other")

    def test_conflict_leaves_the_edge_and_is_not_retried(self):
        """AC3: 409 (an operator or a newer wait took the edge) does not fail the tick, is asked once."""
        r, a, mesh, s, g = self.flow(condition="task_done")
        mesh.deps[s["id"]][0].update(generation=2)                          # an operator changed it since
        g["status_id"] = "s-done"
        self.satisfy(mesh, s)
        r._guard(a, "wake", r._wake_one)
        woken = r.store.get(a.id)
        self.assertEqual(woken.state, "ready")
        self.assertEqual(len(mesh.wait_deletes), 1)
        self.assertNotIn("edge", woken.wait)
        self.assertFalse(r.effects.pending_cleanup(woken.wait))             # resume will not ask again
        r.effects.on_end(woken, dict(woken.wait), "resuming", close_request=False)
        self.assertEqual(len(mesh.wait_deletes), 1)
        self.assertEqual([e["id"] for e in mesh.dependencies(s["id"])["outgoing"]], [EDGE])

    def test_transient_failure_keeps_the_edge_for_the_next_cleanup(self):
        r, a, mesh, s, g = self.flow()
        self.satisfy(mesh, s)
        r._wake_one(a)
        woken = r.store.get(a.id)
        mesh.wait_delete_errors = [MeshError(503, "unavailable")]
        wait = dict(woken.wait)
        r.effects.on_end(woken, wait, "resuming", close_request=False)
        self.assertEqual(wait["edge"], EDGE)
        self.assertTrue(r.effects.pending_cleanup(wait))
        r.effects.on_end(woken, wait, "resuming", close_request=False)
        self.assertEqual(len(mesh.wait_deletes), 2)
        self.assertEqual(mesh.dependencies(s["id"])["outgoing"], [])

    def legacy_end(self, owned):
        """A wait an older runner parked (observe_edge, no receipt fields on it) reads its receipt at the
        end and removes the edge only if Mesh says it owns it."""
        r, a, mesh, s, g = self.flow(owned=owned)
        legacy = {k: v for k, v in a.wait.items() if k not in ("card_dependency", "edge", "edge_generation", "edge_owner_wait", "edge_condition")}
        legacy["observe_edge"] = EDGE
        mesh.receipts["w-1"] = {"id": "w-1", "task_id": s["id"], "agent_id": AGENT_ID, "kind": "card",
                                "dependency_id": EDGE, "dependency_generation": 1, "dependency_owned": owned}
        r.effects.on_end(a, legacy, "woken", close_request=False)
        self.assertNotIn("observe_edge", legacy)
        return mesh, s

    def test_a_wait_declared_before_the_fix_removes_its_owned_edge(self):
        mesh, s = self.legacy_end(True)
        self.assertEqual(mesh.wait_deletes, [(s["id"], EDGE, 1, "w-1")])
        self.assertEqual(mesh.dependencies(s["id"])["outgoing"], [])

    def test_a_wait_declared_before_the_fix_keeps_an_edge_it_does_not_own(self):
        mesh, s = self.legacy_end(False)
        self.assertEqual((mesh.wait_deletes, mesh.plain_deletes), ([], []))
        self.assertEqual([e["id"] for e in mesh.dependencies(s["id"])["outgoing"]], [EDGE])

if __name__ == "__main__":
    unittest.main()
