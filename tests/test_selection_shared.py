import unittest

from mesh_runner.mesh import MeshError
from mesh_runner import selection
from mesh_runner.selection import StatusCategories, open_blockers


class Member:
    def statuses(self, pid):
        return [{"id": "st-done-x1", "category": "done"}, {"id": "st-todo-x1", "category": "todo"}]


class Outsider:
    def statuses(self, pid):
        raise MeshError(403, "agent is not a member of this project")


class SharedCategories(unittest.TestCase):
    def setUp(self):
        # readers of other tests must not answer here
        self._readers, self._shared = list(selection._READERS), dict(selection._SHARED)
        selection._READERS.clear()
        selection._SHARED.clear()
        selection._NOBODY.clear()
        selection._READ_BY.clear()

    def tearDown(self):
        for r in self._readers:
            selection._READERS.add(r)
        selection._SHARED.update(self._shared)

    def test_an_outsider_reads_a_blocker_status_another_agent_already_knows(self):
        outsider = StatusCategories(Outsider())
        blocker = {"project_id": "p-x1", "status_id": "st-done-x1"}
        with self.assertRaises(MeshError):          # no agent of this process can read it: unknown
            outsider.category(blocker)
        StatusCategories(Member()).category({"project_id": "p-x1", "status_id": "st-todo-x1"})
        self.assertEqual(outsider.category(blocker), "done")

    def test_an_outsider_asks_another_agent_of_the_process_that_can_read_it(self):
        outsider = StatusCategories(Outsider())
        member = StatusCategories(Member())          # exists, has not read that project
        blocker = {"project_id": "p-x3", "status_id": "st-done-x1"}
        self.assertEqual(outsider.category(blocker), "done")
        del member

    def test_an_unreadable_blocker_card_is_read_by_another_agent(self):
        class Me(Outsider):
            def dependencies(self, tid):
                return {"outgoing": [{"dependency_type": "blocks", "depends_on_task_id": "b1"}]}

            def task(self, tid):
                raise MeshError(403, "forbidden")

        class Other(Member):
            def task(self, tid):
                return {"id": tid, "project_id": "p-x4", "status_id": "st-done-x1"}
        mine = StatusCategories(Me())
        with self.assertRaises(MeshError):
            open_blockers(mine.mesh, mine, "t1")
        other = StatusCategories(Other())
        self.assertEqual(open_blockers(mine.mesh, mine, "t1"), [])     # closed: not blocking
        del other

    def test_other_errors_still_raise(self):
        class Broken:
            def statuses(self, pid):
                raise MeshError(500, "boom")
        StatusCategories(Member()).category({"project_id": "p-x2", "status_id": "st-done-x1"})
        with self.assertRaises(MeshError):
            StatusCategories(Broken()).category({"project_id": "p-x2", "status_id": "st-done-x1"})


if __name__ == "__main__":
    unittest.main()
