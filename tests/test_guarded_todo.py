"""Handing up or requeueing never undoes a stage or owner someone else set meanwhile."""
from fakes import AGENT_ID, FakeMesh, task
from test_requeue_on_cap import CapBench


class GuardedTodo(CapBench):
    def setUp(self):
        super().setUp()
        self.t = task(1, cat="in_progress")
        self.mesh = FakeMesh([self.t], max_concurrent=1)
        self.r = self.runner(self.mesh)
        self.r.refresh_me()
        self.tid = self.t["id"]
        self.attempt = self.r.store.create("builder", self.tid, "claude")

    def stale(self):
        return dict(self.mesh.tasks[self.tid])

    def cat(self):
        return self.mesh.category(self.mesh.tasks[self.tid])

    def test_still_ours_in_working_stage_moves_to_todo(self):
        self.r._to_todo(self.attempt, self.stale(), always=True)
        self.assertEqual(self.cat(), "todo")

    def test_user_moved_it_to_review_meanwhile_is_left(self):
        snap = self.stale()
        self.mesh.set_category(self.tid, "review")
        self.r._to_todo(self.attempt, snap, always=True)
        self.assertEqual(self.cat(), "review")

    def test_done_is_left(self):
        snap = self.stale()
        self.mesh.set_category(self.tid, "done")
        self.r._to_todo(self.attempt, snap, always=True)
        self.assertEqual(self.cat(), "done")

    def test_new_owner_is_left(self):
        snap = self.stale()
        self.mesh.tasks[self.tid]["assignee_id"] = "someone-else"
        self.r._to_todo(self.attempt, snap, always=True)
        self.assertEqual(self.cat(), "in_progress")

    def test_business_stage_other_than_ours_is_left(self):
        snap = self.stale()
        self.mesh.tasks[self.tid]["status_id"] = "s-business"     # in_progress category, not the one we held
        self.mesh.category = lambda t: "in_progress"
        self.r.cats.category = lambda t: "in_progress"
        self.r._to_todo(self.attempt, snap, always=True)
        self.assertEqual(self.mesh.tasks[self.tid]["status_id"], "s-business")

    def test_hand_up_expects_the_lead_as_owner(self):
        self.mesh.tasks[self.tid]["assignee_id"] = "agent-lead"
        self.r._to_todo(self.attempt, self.stale(), always=True, expect_assignee="agent-lead")
        self.assertEqual(self.cat(), "todo")

    def test_requeue_helper_leaves_a_moved_card_and_reports_false(self):
        snap = self.stale()
        self.mesh.set_category(self.tid, "review")
        self.assertFalse(self.r._back_to_todo(snap))
        self.assertEqual(self.cat(), "review")

    def test_requeue_helper_accepts_already_todo(self):
        snap = self.stale()
        self.mesh.set_category(self.tid, "todo")
        self.assertTrue(self.r._back_to_todo(snap))

    def test_change_between_read_and_move_is_refused_by_the_server_precondition(self):
        self.mesh.tasks[self.tid]["version"] = 5
        real = self.mesh.move

        def racing(tid, status_id, **kw):
            self.mesh.tasks[tid]["status_id"] = "s-rev"          # a person moves it after our read
            self.mesh.tasks[tid]["version"] = 6
            return real(tid, status_id, **kw)
        self.mesh.move = racing
        self.r._to_todo(self.attempt, self.stale(), always=True)
        self.assertEqual(self.mesh.tasks[self.tid]["status_id"], "s-rev")

    def test_move_sends_the_version_and_stage_it_read(self):
        self.mesh.tasks[self.tid]["version"] = 5
        seen = {}
        real = self.mesh.move
        self.mesh.move = lambda tid, sid, **kw: (seen.update(kw), real(tid, sid, **kw))[1]
        self.r._to_todo(self.attempt, self.stale(), always=True)
        self.assertEqual(seen, {"expected_version": 5, "expected_status_id": "s-prog"})
        self.assertEqual(self.cat(), "todo")

    def test_card_without_a_version_is_not_moved(self):
        real = self.mesh.task
        self.mesh.task = lambda tid: {k: v for k, v in real(tid).items() if k != "version"}
        self.r._to_todo(self.attempt, self.stale(), always=True)
        self.assertEqual(self.cat(), "in_progress")
