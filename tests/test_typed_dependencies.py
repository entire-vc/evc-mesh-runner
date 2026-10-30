import unittest

from mesh_runner.selection import StatusCategories, blocking_tasks, open_blockers, satisfied


class Mesh:
    """One blocker card; the dependency endpoint returns the given edge."""

    def __init__(self, edge, blocker):
        self.edge, self.blocker, self.calls = edge, blocker, []

    def dependencies(self, tid):
        self.calls.append("deps")
        return {"outgoing": [self.edge], "incoming": []}

    def task(self, tid):
        self.calls.append("task")
        return dict(self.blocker, id=tid)

    def statuses(self, pid):
        self.calls.append("statuses")
        return []


def edge(**extra):
    return dict({"id": "e1", "dependency_type": "blocks", "depends_on_task_id": "b1"}, **extra)


def run(e, blocker):
    mesh = Mesh(e, blocker)
    return [b["id"] for b in blocking_tasks(mesh, StatusCategories(mesh), "t1")], mesh


class TypedDependencySatisfaction(unittest.TestCase):
    def test_server_condition_satisfied_false_keeps_a_cancelled_blocker_blocking(self):
        # an `accepted` edge: a cancelled blocker was never accepted
        ids, _ = run(edge(condition="accepted", condition_satisfied=False), {"status_category": "cancelled"})
        self.assertEqual(ids, ["b1"])

    def test_server_condition_satisfied_false_keeps_a_shipped_blocker_blocking(self):
        # a `deployed` edge the server does not yet count as met, whatever is_shipped says
        ids, _ = run(edge(condition="deployed", condition_satisfied=False), {"status_category": "review", "is_shipped": True})
        self.assertEqual(ids, ["b1"])

    def test_server_condition_satisfied_true_releases_an_unclosed_blocker(self):
        ids, _ = run(edge(condition="artifact_published", condition_satisfied=True), {"status_category": "review"})
        self.assertEqual(ids, [])

    def test_old_server_without_the_field_keeps_the_closed_or_shipped_rule(self):
        self.assertEqual(run(edge(), {"status_category": "done"})[0], [])
        self.assertEqual(run(edge(), {"status_category": "cancelled"})[0], [])
        self.assertEqual(run(edge(), {"status_category": "review", "is_shipped": True})[0], [])
        self.assertEqual(run(edge(), {"status_category": "in_progress"})[0], ["b1"])

    def test_false_on_an_untyped_condition_falls_back_to_the_blocker_state(self):
        # A server may compute condition_satisfied from evidence only, so a
        # task_done edge to a done blocker reads False; that must not hold the dependant
        self.assertEqual(run(edge(condition="task_done", condition_satisfied=False), {"status_category": "done"})[0], [])
        self.assertEqual(run(edge(condition_satisfied=False), {"status_category": "cancelled"})[0], [])
        self.assertEqual(run(edge(condition="access_ready", condition_satisfied=False), {"status_category": "done"})[0], [])
        self.assertEqual(run(edge(condition="task_done", condition_satisfied=False), {"status_category": "in_progress"})[0], ["b1"])

    def test_false_is_authoritative_for_typed_conditions_whatever_the_case(self):
        self.assertEqual(run(edge(condition="Accepted", condition_satisfied=False), {"status_category": "done"})[0], ["b1"])
        self.assertEqual(run(edge(condition=" shipped ", condition_satisfied=False), {"status_category": "done"})[0], ["b1"])

    def test_the_older_field_name_is_not_read(self):
        # only `condition_satisfied` is the server's verdict; a bare `satisfied` is ignored
        self.assertEqual(run(edge(satisfied=False), {"status_category": "done"})[0], [])

    def test_a_non_boolean_satisfied_is_not_authoritative(self):
        self.assertEqual(run(edge(condition_satisfied="false"), {"status_category": "done"})[0], [])
        self.assertEqual(run(edge(condition_satisfied=None), {"status_category": "todo"})[0], ["b1"])

    def test_request_count_is_unchanged_by_the_field(self):
        _, with_field = run(edge(condition_satisfied=False), {"status_category": "todo"})
        _, without = run(edge(), {"status_category": "todo"})
        self.assertEqual(with_field.calls, without.calls)
        self.assertEqual(with_field.calls, ["deps", "task"])

    def test_open_blockers_follows_the_same_rule(self):
        mesh = Mesh(edge(condition="accepted", condition_satisfied=False), {"status_category": "cancelled"})
        self.assertEqual(open_blockers(mesh, StatusCategories(mesh), "t1"), ["b1"])

    def test_satisfied_without_an_edge_is_the_older_rule(self):
        cats = StatusCategories(Mesh(edge(), {}))
        self.assertTrue(satisfied(cats, {"status_category": "done"}))
        self.assertTrue(satisfied(cats, {"status_category": "todo", "is_shipped": True}))
        self.assertFalse(satisfied(cats, {"status_category": "todo"}))
        self.assertFalse(satisfied(cats, {"status_category": "done"}, {"condition": "accepted", "condition_satisfied": False}))
        self.assertTrue(satisfied(cats, {"status_category": "done"}, {"condition_satisfied": False}))


if __name__ == "__main__":
    unittest.main()
