"""One feed request per tick: ``status_category=a,b,c`` instead of one request per category."""
from fakes import FakeMesh, task
from test_runner import Bench

from mesh_runner.mesh import MeshError


class FeedSingleRequest(Bench):
    def test_candidates_use_one_request_for_all_categories(self):
        t = task(1)
        mesh = FakeMesh([t])
        r = self.runner(mesh)
        r.refresh_me()
        mesh.my_tasks_calls = []
        self.assertEqual([x["id"] for x in r.candidates()], [t["id"]])
        self.assertEqual(len(mesh.my_tasks_calls), 1, mesh.my_tasks_calls)
        self.assertEqual(set(mesh.my_tasks_calls[0].split(",")) - {"review"}, {"in_progress", "todo"})

    def test_an_api_without_list_support_falls_back_to_one_request_per_category(self):
        t = task(1)
        mesh = FakeMesh([t])
        r = self.runner(mesh)
        r.refresh_me()
        per_category = mesh.my_tasks

        def my_tasks(cat):
            if "," in cat:
                raise MeshError(400, "unknown status_category")
            return per_category(cat)
        mesh.my_tasks = my_tasks
        self.assertEqual([x["id"] for x in r.candidates()], [t["id"]])

    def test_a_task_that_cannot_be_classified_triggers_the_fallback_not_a_drop(self):
        t = task(1)
        mesh = FakeMesh([t])
        r = self.runner(mesh)
        r.refresh_me()
        r.cats = type("C", (), {"category": lambda self, t: ""})()
        mesh.my_tasks_calls = []
        r._feed(("in_progress", "todo"))
        self.assertEqual(mesh.my_tasks_calls, ["in_progress,todo", "in_progress", "todo"])

    def test_a_server_error_on_the_combined_request_is_not_multiplied(self):
        mesh = FakeMesh([task(1)])
        r = self.runner(mesh)
        r.refresh_me()
        mesh.my_tasks_calls = []

        def my_tasks(cat):
            mesh.my_tasks_calls.append(cat)
            raise MeshError(500, "internal error")
        mesh.my_tasks = my_tasks
        with self.assertRaises(MeshError):
            r._feed(("in_progress", "todo"))
        self.assertEqual(mesh.my_tasks_calls, ["in_progress,todo"])
