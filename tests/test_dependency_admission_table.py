"""Contract table: Runner admission against the server's DependencyReleasedSQL / Released.

Server rule per edge condition (evc-mesh internal/domain/task.go):
  evidence satisfied      -> released, whatever the blocker is
  shipped                 -> blocker is_shipped OR done
  deployed                -> blocker is_shipped OR a recorded deploy
  accepted                -> done
  task_done / other       -> done OR cancelled     (is_shipped does NOT release)
Mesh returns that verdict as the edge's `condition_satisfied`; it is authoritative.
"""
import unittest

from mesh_runner.mesh import MeshError
from mesh_runner.selection import StatusCategories, blocking_edges, blocking_tasks, satisfied


class Mesh:
    def __init__(self, edge, blocker=None, task_error=None):
        self.edge, self.blocker, self.task_error = edge, blocker, task_error

    def dependencies(self, tid):
        return {"outgoing": [self.edge], "incoming": []}

    def task(self, tid):
        if self.task_error:
            raise MeshError(self.task_error, "gone")
        return dict(self.blocker, id=tid)

    def statuses(self, pid):
        return []


def edge(**extra):
    return dict({"id": "e1", "dependency_type": "blocks", "depends_on_task_id": "b1"}, **extra)


def blocked(e, blocker=None, task_error=None):
    mesh = Mesh(e, blocker, task_error)
    return [b["id"] for b in blocking_tasks(mesh, StatusCategories(mesh), "t1")]


class AdmissionTable(unittest.TestCase):
    def test_task_done_false_is_not_turned_true_by_is_shipped(self):
        # RED before the fix: the server says held, the Runner released it
        self.assertEqual(blocked(edge(condition="task_done", condition_satisfied=False),
                                 {"status_category": "review", "is_shipped": True}), ["b1"])

    def test_untyped_false_is_not_turned_true_by_is_shipped(self):
        self.assertEqual(blocked(edge(condition_satisfied=False),
                                 {"status_category": "review", "is_shipped": True}), ["b1"])

    def test_missing_verdict_is_unknown_not_fulfilled_by_is_shipped(self):
        self.assertEqual(blocked(edge(), {"status_category": "review", "is_shipped": True}), ["b1"])

    def test_missing_verdict_typed_conditions_follow_the_server_table(self):
        shipped = {"status_category": "review", "is_shipped": True}
        self.assertEqual(blocked(edge(condition="shipped"), shipped), [])
        self.assertEqual(blocked(edge(condition="deployed"), shipped), [])
        self.assertEqual(blocked(edge(condition="accepted"), shipped), ["b1"])
        self.assertEqual(blocked(edge(condition="accepted"), {"status_category": "cancelled"}), ["b1"])
        self.assertEqual(blocked(edge(condition="deployed"), {"status_category": "done"}), ["b1"])

    def test_true_verdict_releases_without_reading_the_blocker(self):
        # a deleted/unreadable blocker with a released edge: nothing to read, nothing to hold
        self.assertEqual(blocked(edge(condition_satisfied=True), task_error=410), [])
        self.assertEqual(blocked(edge(condition_satisfied=True), task_error=404), [])

    def test_gone_blocker_with_a_live_unreleased_edge_is_a_diagnosed_block(self):
        # RED before the fix: 404/410 silently skipped the edge the server still holds
        for status in (404, 410):
            mesh = Mesh(edge(condition_satisfied=False), task_error=status)
            pairs = blocking_edges(mesh, StatusCategories(mesh), "t1")
            self.assertEqual([b["id"] for b, _ in pairs], ["b1"])
            self.assertEqual(pairs[0][0].get("unavailable"), status)

    def test_gone_blocker_with_no_verdict_is_also_held(self):
        self.assertEqual(blocked(edge(), task_error=410), ["b1"])

    def test_edge_absence_is_not_a_block(self):
        class NoEdges(Mesh):
            def dependencies(self, tid):
                return {"outgoing": [], "incoming": []}
        mesh = NoEdges(edge())
        self.assertEqual(blocking_tasks(mesh, StatusCategories(mesh), "t1"), [])

    def test_closed_blocker_still_releases_an_old_server_false_on_task_done(self):
        # an older server computes the verdict from evidence only
        self.assertEqual(blocked(edge(condition="task_done", condition_satisfied=False), {"status_category": "done"}), [])
        self.assertEqual(blocked(edge(condition="task_done", condition_satisfied=False), {"status_category": "cancelled"}), [])

    def test_satisfied_without_an_edge_is_task_done(self):
        cats = StatusCategories(Mesh(edge(), {}))
        self.assertFalse(satisfied(cats, {"status_category": "review", "is_shipped": True}))
        self.assertTrue(satisfied(cats, {"status_category": "done"}))


if __name__ == "__main__":
    unittest.main()
