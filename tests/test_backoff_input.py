"""A failed-start backoff ends early when what the task was waiting for arrives."""
import time
from unittest import mock

from fakes import FakeMesh, calls, task, write_replies
from test_runner import Bench

from mesh_runner.mesh import MeshError

HELD = "backing off after a failed start"


def link(state="open", pipeline="running"):
    return {"url": "https://git.example/g/p/-/merge_requests/7", "status": state, "pipeline_status": pipeline}


class BackoffOnInput(Bench):
    def fail_start(self, r, t):
        """One tick whose start raises: the failed-start backoff, with the inputs seen then."""
        with mock.patch.object(r, "_gate", side_effect=RuntimeError("boom")):
            r.tick()
        self.assertIn(t["id"], r._task_backoff)

    def hold(self, r, mesh, t):
        return r.hold_reason(dict(mesh.tasks[t["id"]]), "work")

    def test_a_comment_from_someone_else_lifts_the_backoff_before_it_expires(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.runner(mesh)
        write_replies(self.replies, {t["id"][:8]: "done"})
        self.fail_start(r, t)
        deadline = r._task_backoff[t["id"]]
        self.assertGreater(deadline, time.monotonic() + 600)
        self.assertEqual(self.hold(r, mesh, t), HELD)
        mesh.add_comment(t["id"], "the dependency is merged, go on", author="person")
        self.assertEqual(self.hold(r, mesh, t), "")
        self.assertNotIn(t["id"], r._task_backoff)
        r.tick()
        self.assertEqual(len(r.store.by_state("builder", ("starting", "running"))), 1)   # started well before
        mesh.set_category(t["id"], "review")                                              # the deadline
        self.settle_all(r)
        self.assertEqual(len(calls(self.calls)), 1)

    def test_a_closed_blocker_lifts_the_backoff(self):
        t, blocker = task(1), task(2, cat="in_progress")
        mesh = FakeMesh([t, blocker], max_concurrent=1)
        mesh.deps[t["id"]] = [{"depends_on_task_id": blocker["id"], "dependency_type": "blocks"}]
        r = self.runner(mesh)
        r.refresh_me()
        r._backoff_on_input(dict(mesh.tasks[t["id"]]), time.monotonic() + 900)
        self.assertEqual(self.hold(r, mesh, t), HELD)
        mesh.set_category(blocker["id"], "done")
        self.assertEqual(self.hold(r, mesh, t), "")
        self.assertNotIn(t["id"], r._task_backoff)

    def test_a_change_of_a_linked_merge_request_lifts_the_backoff(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        mesh.links = {t["id"]: [link()]}
        r = self.runner(mesh)
        self.fail_start(r, t)
        self.assertEqual(self.hold(r, mesh, t), HELD)
        mesh.links = {t["id"]: [link(pipeline="success")]}
        self.assertEqual(self.hold(r, mesh, t), "")

    def test_a_new_assignee_lifts_the_backoff(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.runner(mesh)
        r.refresh_me()
        r._backoff_on_input(dict(mesh.tasks[t["id"]]), time.monotonic() + 900)
        reassigned = dict(mesh.tasks[t["id"]], assignee_id="agent-lead")
        self.assertEqual(r.hold_reason(reassigned, "work"), "")

    def test_nothing_changed_keeps_the_backoff(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.runner(mesh)
        self.fail_start(r, t)
        deadline = r._task_backoff[t["id"]]
        for _ in range(3):
            self.assertEqual(self.hold(r, mesh, t), HELD)
        self.assertEqual(r._task_backoff[t["id"]], deadline)
        self.assertEqual(calls(self.calls), [])

    def test_the_agents_own_comment_is_not_input(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.runner(mesh)
        self.fail_start(r, t)
        mesh.add_comment(t["id"], "cannot start: boom")     # author defaults to the agent itself
        self.assertEqual(self.hold(r, mesh, t), HELD)

    def test_an_unreadable_fingerprint_is_no_news(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.runner(mesh)
        self.fail_start(r, t)
        mesh.add_comment(t["id"], "go on", author="person")
        with mock.patch.object(r, "_input_fingerprint", side_effect=MeshError(500, "down")):
            self.assertEqual(self.hold(r, mesh, t), HELD)
        self.assertIn(t["id"], r._task_backoff)

    def test_a_fingerprint_unreadable_when_the_backoff_is_set_leaves_a_plain_backoff(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.runner(mesh)
        r.refresh_me()
        with mock.patch.object(r, "_input_fingerprint", side_effect=MeshError(500, "down")):
            r._backoff_on_input(dict(mesh.tasks[t["id"]]), time.monotonic() + 900)
        mesh.add_comment(t["id"], "go on", author="person")
        self.assertEqual(self.hold(r, mesh, t), HELD)

    def test_the_fingerprint_is_read_at_most_once_per_poll_interval(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        self.cfg.wake_poll_seconds = 120
        r = self.runner(mesh)
        self.fail_start(r, t)
        r._backoff_checked.clear()                          # the failing tick's own check is not under test
        with mock.patch.object(r, "_input_fingerprint", wraps=r._input_fingerprint) as read:
            for _ in range(5):
                self.assertEqual(self.hold(r, mesh, t), HELD)
            self.assertEqual(read.call_count, 1)
            with mock.patch("mesh_runner.runner.time.monotonic", return_value=time.monotonic() + 121):
                self.hold(r, mesh, t)
            self.assertEqual(read.call_count, 2)

    def test_a_task_with_no_backoff_costs_no_read(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.runner(mesh)
        with mock.patch.object(r, "_input_fingerprint") as read:
            self.assertEqual(self.hold(r, mesh, t), "")
            r._task_backoff[t["id"]] = time.monotonic() + 900          # a backoff with no record
            self.assertEqual(self.hold(r, mesh, t), HELD)
        read.assert_not_called()

    def test_a_refused_checkout_is_not_lifted_by_input(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        mesh.refuse_checkout.add(t["id"])                   # another writer holds the task
        r = self.runner(mesh)
        r.tick()
        self.assertIn(t["id"], r._task_backoff)
        self.assertNotIn(t["id"], r._backoff_inputs)
        mesh.add_comment(t["id"], "go on", author="person")
        self.assertEqual(self.hold(r, mesh, t), HELD)

    def test_a_later_backoff_voids_the_recorded_inputs(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.runner(mesh)
        self.fail_start(r, t)
        r._task_backoff[t["id"]] = time.monotonic() + 900    # e.g. a held workspace later
        mesh.add_comment(t["id"], "go on", author="person")
        self.assertEqual(self.hold(r, mesh, t), HELD)
        self.assertNotIn(t["id"], r._backoff_inputs)

    def test_an_expired_backoff_drops_its_record(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.runner(mesh)
        self.fail_start(r, t)
        with mock.patch("mesh_runner.runner.time.monotonic", return_value=time.monotonic() + 3600):
            self.assertEqual(self.hold(r, mesh, t), "")
        self.assertNotIn(t["id"], r._backoff_inputs)

    def test_the_daily_budget_holds_are_not_lifted_by_input(self):
        for kind in ("turn budget", "start cap"):
            with self.subTest(kind=kind):
                t = task(1)
                mesh = FakeMesh([t], max_concurrent=1)
                mesh.links = {t["id"]: [link()]}
                mesh.deps[t["id"]] = []
                r = self.runner(mesh)
                r.refresh_me()
                r._keep_until_tomorrow(t["id"], "CAP: x.", kind)
                self.assertNotIn(t["id"], r._backoff_inputs)
                mesh.links = {t["id"]: [link(pipeline="success")]}      # linked work moved on
                self.assertIn(t["id"], r._task_backoff)
                self.assertNotEqual(self.hold(r, mesh, t), "")
                self.assertIn(t["id"], r._task_backoff)
