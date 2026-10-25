"""An umbrella task (open children) waiting on a card keeps no worktree lease."""
import unittest

from fakes import FakeMesh, task, write_replies
from test_runner import Bench


class UmbrellaWait(Bench):
    def waiting(self, with_child, release_minutes=0):
        parent = task(81, cat="in_progress", subtask_count=1 if with_child else 0)
        tasks = [parent]
        if with_child:
            tasks.append(task(82, cat="in_progress", parent_task_id=parent["id"]))
        self.agent.workspace_wait_release_minutes = release_minutes
        write_replies(self.replies, {parent["id"][:8]: "⏳ WAIT 2h: the child"})
        r = self.runner(FakeMesh(tasks))
        r.refresh_me()
        a = r.store.create("builder", parent["id"], "claude")
        path = r.pool.lease(a.id, "app")
        r.store.update(a, state="waiting", workspaces=[str(path)], wait={"kind": "card", "target": "82000000", "started": ""})
        return r, r.store.get(a.id)

    def test_parent_with_open_children_returns_its_lease_at_once(self):
        r, a = self.waiting(with_child=True)
        r._suspend_waiting(a)
        self.assertEqual(r.pool.leased_to(a.id), [])

    def test_mr_wait_watching_ci_keeps_the_lease(self):
        r, a = self.waiting(with_child=True)
        r.store.update(a, wait={"kind": "mr", "ci_change": {"sha": "x"}, "started": ""})
        r._suspend_waiting(r.store.get(a.id))
        self.assertEqual(len(r.pool.leased_to(a.id)), 1)

    def test_task_without_children_keeps_its_lease(self):
        r, a = self.waiting(with_child=False)
        r._suspend_waiting(a)
        self.assertEqual(len(r.pool.leased_to(a.id)), 1)


if __name__ == "__main__":
    unittest.main()
