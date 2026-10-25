"""An edge operation Mesh refuses with 403 is not asked again on later ticks."""
import unittest

from fakes import FakeMesh, task
from mesh_runner.mesh import MeshError
from test_runner import Bench


class EdgeDenied(Bench):
    def setUp(self):
        super().setUp()
        self.t = task(1)
        self.other = task(2)
        self.mesh = FakeMesh([self.t, self.other])
        self.r = self.runner(self.mesh)
        self.addCleanup(self.r.store.db.close)
        self.r.refresh_me()
        self.a = self.r.store.create(self.agent.name, self.t["id"], "claude")
        self.deletes = []

    def deny_delete(self, status=403):
        def delete(tid, edge_id):
            self.deletes.append(edge_id)
            raise MeshError(status, "workspace access denied" if status == 403 else "unavailable")
        self.mesh.delete_dependency = delete

    def end(self, wait):
        self.r.effects.on_end(self.a, wait, "woken", close_request=False)

    def test_a_403_removal_is_asked_once_even_when_the_wait_keeps_its_edge(self):
        self.deny_delete()
        with self.assertLogs("mesh_runner", level="WARNING") as logs:
            for _ in range(3):
                self.end({"kind": "card", "target": self.other["id"][:8], "edge": "e1", "fx": True})
        self.assertEqual(self.deletes, ["e1"])
        self.assertEqual(len([m for m in logs.output if "edge #" in m]), 1)

    def test_the_memory_survives_a_restart(self):
        self.deny_delete()
        self.end({"kind": "card", "edge": "e1", "fx": True})
        again = self.runner(self.mesh)
        again.refresh_me()
        again.effects.on_end(self.a, {"kind": "card", "edge": "e1", "fx": True}, "woken", close_request=False)
        self.assertEqual(self.deletes, ["e1"])

    def test_another_edge_is_still_tried(self):
        self.deny_delete()
        self.end({"kind": "card", "edge": "e1", "fx": True})
        self.end({"kind": "card", "edge": "e2", "fx": True})
        self.assertEqual(self.deletes, ["e1", "e2"])

    def test_the_denial_is_remembered_per_lane_and_task(self):
        self.deny_delete()
        self.end({"kind": "card", "edge": "e1", "fx": True})
        other_task = self.r.store.create(self.agent.name, self.other["id"], "claude")
        self.r.effects.on_end(other_task, {"kind": "card", "edge": "e1", "fx": True}, "woken", close_request=False)
        self.assertEqual(self.deletes, ["e1", "e1"])

    def test_other_errors_keep_the_retry(self):
        self.deny_delete(503)
        wait = {"kind": "card", "edge": "e1", "fx": True}
        for _ in range(3):
            self.end(wait)
        self.assertEqual(self.deletes, ["e1", "e1", "e1"])
        self.assertEqual(wait.get("edge"), "e1")

    def test_an_unreadable_target_card_is_asked_once(self):
        reads = []

        def short(s):
            reads.append(s)
            raise MeshError(403, "workspace access denied")
        self.mesh.task_by_short = short
        wait = {"kind": "card", "target": "abcdef01"}
        for _ in range(3):
            self.r.effects._add_edge(self.a, wait)
        self.assertEqual(reads, ["abcdef01"])

    def test_a_successful_removal_is_unchanged(self):
        edge = self.mesh.add_dependency(self.t["id"], self.other["id"])
        wait = {"kind": "card", "edge": edge["id"], "fx": True}
        self.end(wait)
        self.assertNotIn("edge", wait)
        self.assertEqual(self.mesh.edges[self.t["id"]], [])


if __name__ == "__main__":
    unittest.main()
